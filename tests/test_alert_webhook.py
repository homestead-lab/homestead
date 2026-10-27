"""#741 part 2: alerts, not only outages, can go out to a webhook in Generic
JSON (the default), ntfy or Gotify - with part 1's formatter, address checks,
hiding and test limit (homestead_outage), not copies of them."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_alerts as ALERTS
import homestead_alert_webhook as H
import homestead_outage as O
from test_outage_formats import FakeCase

SECRET = "AbCdEf123SECRET"
REAL_KIND = O._kind


def entry(severity="critical", phase="raised", category="outage", title="Host node-1 is not ready",
          body="Kubernetes reports it NotReady", key="health:Node:node-1", id=7):
    return {"id": id, "at": 100, "phase": phase, "key": key, "category": category, "severity": severity,
            "title": title, "body": body, "href": "/nodes", "signals": {"condition": 1}, "event": False}


class Store(FakeCase):
    """A data folder of its own, and sends run at once rather than on a thread."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patch = mock.patch.object(ALERTS, "DATA_DIR", folder.name)
        patch.start()
        self.addCleanup(patch.stop)
        # The leader's shared state, in memory: what was sent, and the last call.
        self.shared = {}
        for name, fake in (("put", lambda section, value, **kw: self.shared.__setitem__(section, value)),
                           ("get", lambda section, *a, **kw: (self.shared.get(section), 0))):
            leader = mock.patch.object(H.LEADER_STATE, name, side_effect=fake)
            leader.start()
            self.addCleanup(leader.stop)
        H._last.clear()
        self.addCleanup(H._last.clear)
        O._tests.clear()
        self.addCleanup(O._tests.clear)

    def set(self, **cfg):
        H.save(cfg)

    def deliver(self, fresh):
        return H.deliver(fresh, start=lambda run: run())


class FormatTests(Store):
    def test_an_alert_sent_as_ntfy_is_plain_text_with_title_priority_and_tags(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/homestead"), format="ntfy")
        self.deliver([entry()])
        got = fake.got[0]
        self.assertEqual("/homestead", got["path"])
        self.assertEqual(b"Kubernetes reports it NotReady", got["body"])
        self.assertTrue(got["headers"]["Content-Type"].startswith("text/plain"))
        self.assertEqual("Host node-1 is not ready", got["headers"]["Title"])
        self.assertEqual("high", got["headers"]["Priority"])
        self.assertEqual("rotating_light", got["headers"]["Tags"])

    def test_ntfy_priority_and_tag_follow_the_alert(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/homestead"), format="ntfy", min_severity="info")
        self.deliver([entry("degraded", id=1), entry("info", id=2), entry("critical", "resolved", id=3, title="Host node-1 is ready")])
        seen = [(g["headers"]["Priority"], g["headers"]["Tags"]) for g in fake.got]
        self.assertEqual([("default", "warning"), ("low", "information_source"), ("default", "white_check_mark")], seen)

    def test_an_alert_sent_to_gotify_is_its_json(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, f"/message?token={SECRET}"), format="gotify")
        self.deliver([entry()])
        got = fake.got[0]
        self.assertEqual(f"/message?token={SECRET}", got["path"])
        self.assertEqual("application/json", got["headers"]["Content-Type"])
        self.assertEqual({"title": "Host node-1 is not ready", "message": "Kubernetes reports it NotReady", "priority": 8},
                         json.loads(got["body"]))

    def test_gotify_priority_follows_the_alert(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/message"), format="gotify", min_severity="info")
        self.deliver([entry("degraded", id=1), entry("info", id=2)])
        self.assertEqual([5, 2], [json.loads(g["body"])["priority"] for g in fake.got])

    def test_generic_json_is_the_default_with_a_sentence_for_chat_services(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/hook"))
        self.assertEqual("generic", H.load()["format"])
        self.deliver([entry()])
        got = json.loads(fake.got[0]["body"])
        self.assertEqual("application/json", fake.got[0]["headers"]["Content-Type"])
        sentence = "Host node-1 is not ready: Kubernetes reports it NotReady"
        self.assertEqual((sentence, sentence, sentence), (got["text"], got["content"], got["message"]))
        self.assertEqual(("alert", "raised", "critical", "outage", "health:Node:node-1"),
                         (got["event"], got["phase"], got["severity"], got["category"], got["key"]))

    def test_an_alert_without_a_body_uses_its_title_as_the_message(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy")
        self.deliver([entry(body="")])
        self.assertEqual(b"Host node-1 is not ready", fake.got[0]["body"])

    def test_a_title_that_is_not_plain_ascii_or_one_line_still_goes_as_a_header(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy")
        self.deliver([entry(title="Drive café\r\nX-Evil: 1 needs attention")])
        title = fake.got[0]["headers"]["Title"]
        self.assertNotIn("X-Evil", fake.got[0]["headers"])
        self.assertTrue(title.startswith("=?utf-8?"), title)

    def test_outage_calls_are_unchanged(self):
        body = O.payload({"kind": "app", "ns": "lab", "name": "frigate"}, "down", {"state": "down"}, now=10)
        _, data, headers = O.request_for("https://ntfy.example.com/x", body, "ntfy")
        self.assertEqual((b"frigate is down", "Homestead", "high", "rotating_light", "Homestead outage actions"),
                         (data, headers["Title"], headers["Priority"], headers["Tags"], headers["User-Agent"]))
        _, data, _ = O.request_for("https://gotify.example.com/message", body, "gotify")
        self.assertEqual(b'{"title": "Homestead", "message": "frigate is down", "priority": 8}', data)
        _, data, _ = O.request_for("https://h.example.com/x", body, "generic")
        self.assertEqual(json.dumps(body).encode(), data)


class WhatIsSentTests(Store):
    def test_nothing_is_sent_while_no_webhook_is_set(self):
        with mock.patch.object(H.OUTAGE, "post") as post:
            self.deliver([entry()])
        post.assert_not_called()
        self.assertIsNone(H.load())

    def test_warnings_and_critical_by_default_and_never_a_device_test(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy")
        self.deliver([entry("info", id=1, title="info"), entry("degraded", id=2, title="warn"),
                      entry("critical", id=3, title="crit"), entry("info", category="test", id=4, title="test")])
        self.assertEqual(["warn", "crit"], [g["headers"]["Title"] for g in fake.got])

    def test_critical_only(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy", min_severity="critical")
        self.deliver([entry("degraded", id=1, title="warn"), entry("critical", id=2, title="crit")])
        self.assertEqual(["crit"], [g["headers"]["Title"] for g in fake.got])

    def test_a_burst_is_cut_short_with_one_summary(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy")
        self.deliver([entry(id=i, title=f"a{i}", key=f"k{i}") for i in range(H.MAX_PER_ROUND + 5)])
        self.assertEqual(H.MAX_PER_ROUND + 1, len(fake.got))
        self.assertIn(b"5 more alerts", fake.got[-1]["body"])

    def test_the_last_call_is_kept_and_shown_without_secrets(self):
        self.set(webhook=f"https://ntfy.example.com/{SECRET}", format="ntfy")
        with mock.patch.object(H.OUTAGE, "post", side_effect=OSError(f"refused for /{SECRET}")):
            self.deliver([entry()])
        shown = json.dumps(H.public())
        self.assertNotIn(SECRET, shown)
        self.assertEqual("https://ntfy.example.com/(hidden)", H.public()["webhook"])
        self.assertFalse(H.public()["last"]["ok"])

    def test_an_alert_sent_is_cleared_even_when_it_fell_below_the_level_first(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy", min_severity="critical")
        self.deliver([entry("critical", id=1, title="crit")])
        # It eased to a warning (no event), then cleared at that severity.
        self.deliver([entry("degraded", "resolved", id=2, title="cleared")])
        self.assertEqual(["crit", "cleared"], [g["headers"]["Title"] for g in fake.got])

    def test_a_clear_of_an_alert_never_sent_is_not_sent(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy", min_severity="critical")
        self.deliver([entry("degraded", id=1, title="warn")])
        self.deliver([entry("degraded", "resolved", id=2, title="cleared")])
        self.assertEqual([], fake.got)

    def test_rounds_go_out_one_at_a_time_in_order(self):
        import threading
        import time as clock
        self.set(webhook="https://ntfy.example.com/t", format="ntfy")
        seen, inside, overlap = [], [0], []

        def slow(url, data, fmt, lan, shown):
            inside[0] += 1
            overlap.append(inside[0] > 1)
            clock.sleep(0.05)
            seen.append(shown["title"])
            inside[0] -= 1
        with mock.patch.object(H.OUTAGE, "post", side_effect=slow):
            # As the alert loop sends them: on Homestead's own thread, not at once.
            H.deliver([entry(id=1, title="raised", key="k")])
            H.deliver([entry(id=2, phase="resolved", title="cleared", key="k")])
            done = threading.Event()
            H._queue.put(done.set)
            self.assertTrue(done.wait(5))
        self.assertEqual(["raised", "cleared"], seen)
        self.assertFalse(any(overlap))

    def test_a_store_that_cannot_be_read_can_still_be_turned_off(self):
        Path(H._path()).write_text("not json")
        H.save({"webhook": ""})
        self.assertFalse(Path(H._path()).exists())

    def test_a_burst_of_clears_says_so(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy")
        self.deliver([entry(id=i, title=f"a{i}", key=f"k{i}") for i in range(H.MAX_PER_ROUND + 3)])
        fake.got.clear()
        self.deliver([entry(id=100 + i, phase="resolved", title=f"c{i}", key=f"k{i}") for i in range(H.MAX_PER_ROUND + 3)])
        self.assertIn(b"3 more alerts cleared", fake.got[-1]["body"])
        self.assertEqual("white_check_mark", fake.got[-1]["headers"]["Tags"])

    def test_alert_calls_say_they_come_from_homestead_alerts(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy")
        self.deliver([entry()])
        self.assertEqual("Homestead alerts", fake.got[0]["headers"]["User-Agent"])

    def test_the_housekeeping_list_of_stores_names_it(self):
        import homestead_housekeeping as HK
        self.assertTrue(any("alert-webhook.json" in files for _, files, *_ in HK.STORES))

    def test_the_store_is_readable_only_by_homestead(self):
        self.set(webhook="https://ntfy.example.com/t")
        self.assertEqual(0o600, Path(H._path()).stat().st_mode & 0o777)


class AddressTests(Store):
    def test_the_existing_address_check_stays(self):
        for bad in ("ftp://h.example.com", "https://a b", "https://" + "a" * 500):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.set(webhook=bad)

    def test_an_unknown_format_or_severity_is_refused(self):
        with self.assertRaises(ValueError):
            self.set(webhook="https://h.example.com/x", format="telegram")
        with self.assertRaises(ValueError):
            self.set(webhook="https://h.example.com/x", min_severity="loud")

    def test_loopback_and_metadata_are_refused_on_save_even_with_the_lan_allowed(self):
        for ip in ("127.0.0.1", "169.254.169.254"):
            # The real classifier, on what the name resolves to.
            with self.subTest(ip=ip), mock.patch.object(O, "_kind", REAL_KIND), \
                    mock.patch.object(O, "_resolve", return_value=[ip]), self.assertRaises(ValueError):
                self.set(webhook="http://ntfy.example.com/a", lan=True)
        self.assertIsNone(H.load())

    def test_a_lan_address_needs_the_allowance(self):
        with mock.patch.object(O, "_kind", return_value="lan"):
            with self.assertRaises(ValueError):
                self.set(webhook="http://ntfy.example.com/a", format="ntfy")
            self.set(webhook="http://ntfy.example.com/a", format="ntfy", lan=True)
        self.assertTrue(H.load()["lan"])

    def test_every_send_is_checked_again(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, "/t"), format="ntfy")
        with mock.patch.object(O, "_kind", return_value="lan"):
            self.deliver([entry()])
        self.assertEqual([], fake.got)
        self.assertIn("LAN", H.public()["last"]["error"])

    def test_the_address_is_shown_hidden_and_saving_it_as_shown_keeps_it(self):
        self.set(webhook=f"https://ntfy.example.com/{SECRET}", format="ntfy")
        shown = H.public()["webhook"]
        self.assertNotIn(SECRET, shown)
        self.set(webhook=shown, format="gotify", min_severity="critical")
        self.assertEqual((f"https://ntfy.example.com/{SECRET}", "gotify"), (H.load()["webhook"], H.load()["format"]))

    def test_a_hidden_part_never_moves_to_another_host(self):
        self.set(webhook=f"https://ntfy.example.com/{SECRET}")
        with self.assertRaises(ValueError):
            self.set(webhook="https://evil.example.com/(hidden)")

    def test_turning_it_off_forgets_the_address(self):
        self.set(webhook="https://ntfy.example.com/t")
        self.set(webhook="")
        self.assertIsNone(H.load())
        self.assertEqual({"webhook": "", "format": "generic", "min_severity": "degraded", "lan": False, "last": None}, H.public())


class RouteTests(Store):
    def route(self, method, path):
        return H.ROUTES[(method, path)]

    def test_saving_and_testing_are_admin_only_and_reading_is_hidden(self):
        self.assertEqual("admin", self.route("POST", "/api/alerts/webhook")[0])
        self.assertEqual("admin", self.route("POST", "/api/alerts/webhook/test")[0])
        self.assertEqual("viewer", self.route("GET", "/api/alerts/webhook")[0])

    def test_a_test_goes_in_the_format_chosen_and_shares_the_outage_test_limit(self):
        fake = self.fake()
        answer = H.test_webhook({"webhook": self.named(fake, "/t"), "format": "ntfy"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual("default", fake.got[0]["headers"]["Priority"])
        self.assertIn(b"test", fake.got[0]["body"])
        O._tests[:] = [O.time.time()] * O.TEST_LIMIT
        self.assertEqual(429, H.test_webhook({"webhook": self.named(fake, "/t"), "format": "ntfy"}).code)

    def test_a_test_of_the_address_as_shown_uses_the_stored_one(self):
        fake = self.fake()
        self.set(webhook=self.named(fake, f"/{SECRET}"), format="ntfy")
        answer = H.test_webhook({"webhook": H.public()["webhook"], "format": "ntfy"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(f"/{SECRET}", fake.got[0]["path"])

    def test_a_tests_failure_never_shows_the_secret(self):
        with mock.patch.object(H.OUTAGE, "post", side_effect=OSError(f"bad /{SECRET}")):
            answer = H.test_webhook({"webhook": f"https://ntfy.example.com/{SECRET}", "format": "ntfy"})
        self.assertFalse(answer["ok"])
        self.assertNotIn(SECRET, answer["detail"])


class LoopTests(unittest.TestCase):
    def test_the_alert_loop_hands_fresh_alerts_to_the_webhook(self):
        import homestead_overview_server as OV
        fresh = [entry()]
        with mock.patch.object(OV.ALERT_WEBHOOK, "deliver") as deliver, \
                mock.patch.object(OV.AUTH, "list_users", return_value=[]), \
                mock.patch.object(OV.PUSH, "send", return_value={}):
            OV.push_alerts(fresh)
        deliver.assert_called_once_with(fresh)

    def test_a_failing_webhook_never_stops_push(self):
        import homestead_overview_server as OV
        with mock.patch.object(OV.ALERT_WEBHOOK, "deliver", side_effect=RuntimeError("x")), \
                mock.patch.object(OV.AUTH, "list_users", return_value=[]), \
                mock.patch.object(OV.PUSH, "send", return_value={"sent": 0}) as send:
            OV.push_alerts([entry()])
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
