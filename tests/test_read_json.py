"""A store on the data volume that is there but cannot be read is not empty,
and nothing is written over it (#620)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_agent_requests as REQUESTS
import homestead_alerts as ALERTS
import homestead_api_keys as KEYS
import homestead_changes as CHANGES
import homestead_push as PUSH
import homestead_shared as SHARED
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

BROKEN = '{"half a file'
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def put(self, name, text):
        with open(self.path(name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def text(self, name):
        with open(self.path(name), encoding="utf-8") as handle:
            return handle.read()


class ReadJsonTests(Folder):
    def test_a_missing_file_is_a_fresh_copy_of_the_default(self):
        default = {"apps": {}}
        found = SHARED.read_json(self.path("absent.json"), default)
        self.assertEqual({"apps": {}}, found)
        found["apps"]["web"] = 1
        self.assertEqual({"apps": {}}, default)

    def test_a_stored_value_is_returned(self):
        self.put("rows.json", '[{"id": 1}]')
        self.assertEqual([{"id": 1}], SHARED.read_json(self.path("rows.json"), []))

    def test_a_file_that_is_not_json_raises(self):
        self.put("rows.json", BROKEN)
        with self.assertRaisesRegex(SHARED.Unreadable, "rows.json"):
            SHARED.read_json(self.path("rows.json"), [])

    def test_a_file_of_another_shape_raises(self):
        self.put("rows.json", '{"not": "a list"}')
        with self.assertRaises(SHARED.Unreadable):
            SHARED.read_json(self.path("rows.json"), [])

    def test_a_file_that_cannot_be_opened_raises(self):
        os.mkdir(self.path("rows.json"))
        with self.assertRaises(SHARED.Unreadable):
            SHARED.read_json(self.path("rows.json"), [])

    def test_unreadable_is_a_value_error_so_requests_answer_with_its_message(self):
        self.assertTrue(issubclass(SHARED.Unreadable, ValueError))


class ChangeHistoryTests(Folder):
    def setUp(self):
        super().setUp()
        keep_bound(self, CHANGES)
        CHANGES.bind(self.dir.name)
        self.put("change-history.json", BROKEN)

    def test_observing_does_not_write_over_unread_history(self):
        dep = {"metadata": {"namespace": "apps", "name": "web"}, "spec": {"replicas": 1, "template": {}}}
        with self.assertRaises(SHARED.Unreadable):
            CHANGES.observe([dep], now=1000)
        self.assertEqual(BROKEN, self.text("change-history.json"))

    def test_history_and_undo_say_they_could_not_read_it(self):
        with self.assertRaises(SHARED.Unreadable):
            CHANGES.history()
        with self.assertRaises(SHARED.Unreadable):
            CHANGES.kept_before("apps", "web", "abc")


class AgentRequestTests(Folder):
    def setUp(self):
        super().setUp()
        keep_bound(self, REQUESTS)
        REQUESTS.bind(self.dir.name)
        self.put(REQUESTS.FILE, BROKEN)

    def test_requests_are_not_written_over(self):
        for read in (lambda: REQUESTS.listing("admin"), REQUESTS.pending_count):
            with self.assertRaises(SHARED.Unreadable):
                read()
        self.assertEqual(BROKEN, self.text(REQUESTS.FILE))


class PushTests(Folder):
    def setUp(self):
        super().setUp()
        keep_bound(self, PUSH)
        PUSH.bind(self.dir.name)
        PUSH._key_cache.clear()
        self.addCleanup(PUSH._key_cache.clear)

    def test_subscriptions_are_not_written_over(self):
        self.put("push.json", BROKEN)
        with self.assertRaises(SHARED.Unreadable):
            PUSH.subscribe("ada", {"endpoint": ENDPOINT})
        with self.assertRaises(SHARED.Unreadable):
            PUSH.unsubscribe("ada", ENDPOINT)
        self.assertEqual(BROKEN, self.text("push.json"))

    def test_an_unreadable_server_key_is_not_replaced(self):
        # A new key would orphan every browser subscribed with the old one.
        for text in (BROKEN, '{"created": 1}', '{"private": "not hex"}'):
            self.put("vapid.json", text)
            with self.assertRaises(SHARED.Unreadable):
                PUSH.private_key()
            self.assertEqual(text, self.text("vapid.json"))

    def test_a_key_file_that_cannot_be_looked_at_is_not_replaced(self):
        # A stale handle on a network volume is not a missing file.
        self.put("vapid.json", '{"private": "' + "1" * 64 + '"}')
        real, key = os.stat, self.path("vapid.json")
        def stat(path, *args, **kwargs):
            if os.fspath(path) == key:
                raise OSError(116, "Stale file handle")
            return real(path, *args, **kwargs)
        with mock.patch("os.stat", stat), self.assertRaises(SHARED.Unreadable):
            PUSH.private_key()
        self.assertEqual('{"private": "' + "1" * 64 + '"}', self.text("vapid.json"))

    def test_a_missing_server_key_is_made_once(self):
        first = PUSH.private_key()
        PUSH._key_cache.clear()
        self.assertEqual(first, PUSH.private_key())


class BellTests(unittest.TestCase):
    def test_the_bell_answers_with_unknown_devices_and_requests(self):
        def unread(*args):
            raise SHARED.Unreadable("Homestead could not read push.json")
        saved = ALERTS.DEVICES[0], ALERTS.AGENTS[0]
        self.addCleanup(lambda: (ALERTS.DEVICES.__setitem__(0, saved[0]), ALERTS.AGENTS.__setitem__(0, saved[1])))
        ALERTS.DEVICES[0], ALERTS.AGENTS[0] = unread, unread
        request = type("Request", (), {"user": "ada"})()
        answer = ALERTS.ROUTES[("GET", "/api/alerts")][1](request)
        self.assertIsNone(answer["devices"])
        self.assertIsNone(answer["agents"])


class KeyUsageTests(Folder):
    def setUp(self):
        super().setUp()
        keep_bound(self, KEYS)
        KEYS.bind(self.dir.name)
        KEYS._usage_written.clear()
        self.put("api-key-usage.json", BROKEN)

    def test_using_a_key_does_not_write_over_unread_usage(self):
        KEYS._note_use("k1", "192.0.2.10", 10_000)
        self.assertEqual(BROKEN, self.text("api-key-usage.json"))


if __name__ == "__main__":
    unittest.main()
