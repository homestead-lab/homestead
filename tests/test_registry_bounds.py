"""Registry answers are capped in size, and one deadline covers each read."""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_fetch as FETCH
import homestead_update_registry as REGISTRY
import homestead_volowner as VOLOWNER


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Answer:
    def __init__(self, body, clock=None, step=0.0, length=None):
        self.body, self.clock, self.step = body, clock, step
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def read1(self, size):
        if self.clock:
            self.clock.now += self.step
        piece, self.body = self.body[:min(size, 1)], self.body[min(size, 1):]
        return piece

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


PARSED = REGISTRY.parse_image("example.com/team/app:1.0.0")


class RegistryReadTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for patch in (mock.patch.object(FETCH.time, "monotonic", self.clock),
                      mock.patch.object(REGISTRY.time, "monotonic", self.clock, create=True),
                      mock.patch.object(REGISTRY.time, "sleep", self.clock.sleep)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_an_answer_has_a_size_cap_even_when_the_caller_names_none(self):
        huge = Answer(b"x", length=REGISTRY.ANSWER_MAX_BYTES + 1)
        with mock.patch.object(REGISTRY, "_open", return_value=huge):
            with self.assertRaises(ValueError):
                REGISTRY._registry_json(PARSED, "tags/list?n=1000", {})

    def test_a_trickling_registry_is_given_up_on_at_the_deadline(self):
        slow = Answer(b'{"tags": []}' * 1000, self.clock, step=1.0)
        with mock.patch.object(REGISTRY, "_open", return_value=slow):
            with self.assertRaises(TimeoutError):
                REGISTRY._registry_json(PARSED, "tags/list?n=1000", {})
        self.assertLessEqual(self.clock.now - 1000.0, REGISTRY.READ_DEADLINE + 1)

    def test_each_connection_waits_no_longer_than_the_time_left(self):
        timeouts = []

        def opener(req, credential=None, timeout=12):
            timeouts.append(timeout)
            self.clock.now += timeout          # each connection waits as long as it may
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, None)

        with mock.patch.object(REGISTRY, "_open", opener), mock.patch.object(REGISTRY, "READ_DEADLINE", 30.0):
            with self.assertRaises((urllib.error.HTTPError, TimeoutError)):
                REGISTRY._registry_json(PARSED, "tags/list", {})
        self.assertEqual([12, 12, 6], timeouts)
        self.assertLessEqual(self.clock.now - 1000.0, 30.0)

    def test_no_rate_limit_pause_runs_past_the_deadline(self):
        calls = []

        def opener(req, credential=None, timeout=12):
            calls.append(timeout)
            self.clock.now += REGISTRY.READ_DEADLINE - 0.5
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "3"}, None)

        with mock.patch.object(REGISTRY, "_open", opener):
            with self.assertRaises((urllib.error.HTTPError, TimeoutError)):
                REGISTRY._registry_json(PARSED, "tags/list", {})
        self.assertEqual(1, len(calls), "the pause would end after the deadline, so it is not taken")

    def test_a_caller_can_share_one_deadline_over_several_reads(self):
        slow = Answer(b"{}" * 50, self.clock, step=1.0)
        with mock.patch.object(REGISTRY, "_open", return_value=slow):
            with self.assertRaises(TimeoutError):
                REGISTRY._registry_json(PARSED, "manifests/1.0.0", {}, deadline=self.clock.now + 5)
        self.assertLessEqual(self.clock.now - 1000.0, 6)

    def test_the_token_answer_is_capped_too(self):
        challenge = urllib.error.HTTPError("https://example.com/v2/team/app/tags/list", 401, "Unauthorized",
                                           {"WWW-Authenticate": 'Bearer realm="https://example.com/token"'}, None)
        token = Answer(b"x", length=REGISTRY.TOKEN_MAX_BYTES + 1)
        opener = mock.Mock()
        opener.open.return_value = token
        with mock.patch.object(REGISTRY.urllib.request, "urlopen", side_effect=challenge), \
             mock.patch.object(REGISTRY.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                REGISTRY._open(REGISTRY.urllib.request.Request("https://example.com/v2/team/app/tags/list"))


class VolumeOwnerRegistryTests(unittest.TestCase):
    def reads(self, config_descriptor):
        seen = []
        manifest = json.dumps({"config": config_descriptor, "layers": []}).encode()

        def registry_json(parsed, path, auths, accept="application/json", limit=None, deadline=None):
            seen.append((path, limit, deadline))
            return (manifest if path.startswith("manifests/") else b'{"config": {}}'), {}

        with mock.patch.object(REGISTRY, "_registry_json", registry_json):
            VOLOWNER._registry("example.com/team/app:1.0.0")
        return seen

    def test_the_config_is_read_no_further_than_its_declared_size(self):
        seen = self.reads({"digest": "sha256:" + "c" * 64, "size": 1234})
        self.assertEqual(1234, seen[-1][1])

    def test_a_config_without_a_usable_size_is_still_capped(self):
        for descriptor in ({"digest": "sha256:" + "c" * 64},
                           {"digest": "sha256:" + "c" * 64, "size": 1 << 40}):
            with self.subTest(descriptor):
                self.assertEqual(VOLOWNER.CONFIG_MAX_BYTES, self.reads(descriptor)[-1][1])

    def test_the_reads_share_one_deadline(self):
        seen = self.reads({"digest": "sha256:" + "c" * 64, "size": 10})
        deadlines = {deadline for _, _, deadline in seen}
        self.assertEqual(1, len(deadlines))
        self.assertIsNotNone(deadlines.pop())


if __name__ == "__main__":
    unittest.main()
