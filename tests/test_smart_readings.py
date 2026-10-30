"""A host's SMART page answers from the last reading, and the SMART helper
answers only Homestead's signed requests."""
import hashlib
import hmac
import importlib.util
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_node_probes as PROBES
import homestead_nodes as NODES
import homestead_smart as SMART

KEY = b"test-signing-key"
ROWS = [{"name": "sda", "available": True, "health": "PASSED"},
        {"name": "sdb", "available": True, "health": "PASSED"}]


def probe_pods(_path):
    return {"items": [{
        "metadata": {"namespace": "lab", "name": "probe-node-1", "uid": "uid-1"},
        "spec": {"nodeName": "node-1"},
        "status": {"phase": "Running", "podIP": "192.0.2.9",
                   "containerStatuses": [{"name": "smart", "ready": True}]},
    }]}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Helper:
    """A fake SMART helper: counts its reads and keeps what it was sent."""

    def __init__(self, delay=0.0):
        self.delay, self.calls, self.requests, self.fail = delay, 0, [], None

    def urlopen(self, request, timeout=None):
        url = getattr(request, "full_url", request)
        self.requests.append(request)
        if ":9100/" not in url:
            return _Response(json.dumps({"disks": [{"name": "sda"}]}).encode())
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return _Response(json.dumps({"node": "node-1", "disks": ROWS}).encode())


class Base(unittest.TestCase):
    def setUp(self):
        SMART.bind(probe_pods, "lab", lambda: KEY)
        SMART.reset()
        self.addCleanup(SMART.reset)
        self.now = [1000.0]
        self.helper = Helper()
        for patch in (mock.patch.object(SMART, "_clock", lambda: self.now[0]),
                      mock.patch.object(SMART.NAMES, "nodeprobe_pods",
                                        lambda namespace: probe_pods("")["items"]),
                      mock.patch.object(SMART.urllib.request, "urlopen", self.helper.urlopen)):
            patch.start()
            self.addCleanup(patch.stop)


class LatestReadingTests(Base):
    def test_two_requests_within_the_cache_time_read_smart_once(self):
        first = SMART.latest("node-1")
        self.now[0] += SMART.READING_SECONDS - 1
        second = SMART.latest("node-1")
        self.assertEqual(1, self.helper.calls)
        self.assertEqual(["sda", "sdb"], [row["name"] for row in second["disks"]])
        self.assertEqual(first, second)

    def test_a_reading_older_than_the_cache_time_is_read_again(self):
        SMART.latest("node-1")
        self.now[0] += SMART.READING_SECONDS + 1
        SMART.latest("node-1")
        self.assertEqual(2, self.helper.calls)

    def test_one_drive_comes_from_the_same_reading(self):
        SMART.latest("node-1")
        row = SMART.latest_disk("node-1", "sdb")
        self.assertEqual("sdb", row["name"])
        self.assertEqual(1, self.helper.calls)
        with self.assertRaises(ValueError):
            SMART.latest_disk("node-1", "sdz")

    def test_changing_a_served_row_does_not_change_the_reading(self):
        SMART.latest_disk("node-1", "sda")["health_assessment"] = "changed"
        self.assertNotIn("health_assessment", SMART.latest_disk("node-1", "sda"))

    def test_requests_at_the_same_time_share_one_read(self):
        self.helper.delay = 0.2
        threads = [threading.Thread(target=SMART.latest, args=("node-1",)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(1, self.helper.calls)

    def test_a_failed_read_is_not_repeated_for_every_request(self):
        self.helper.fail = OSError("timed out")
        for _ in range(3):
            with self.assertRaises(Exception):
                SMART.latest("node-1")
        self.assertEqual(1, self.helper.calls)
        self.now[0] += SMART.FAILED_SECONDS + 1
        self.helper.fail = None
        self.assertEqual(2, len(SMART.latest("node-1")["disks"]))
        self.assertEqual(2, self.helper.calls)

    def test_the_probe_loops_reading_is_served_without_reading_again(self):
        SMART.remember("node-1", {row["name"]: row for row in ROWS})
        self.assertEqual("sdb", SMART.latest_disk("node-1", "sdb")["name"])
        self.assertEqual(0, self.helper.calls)

    def test_starting_a_test_drops_the_hosts_reading(self):
        SMART.latest("node-1")
        with mock.patch.object(SMART, "disk", return_value={
                "name": "sda", "available": True, "supported_tests": ["short"], "test": {"active": False}}), \
             mock.patch.object(SMART, "_request", return_value={"expected_seconds": 60}):
            SMART.start_test("node-1", "sda", "short")
        SMART.latest("node-1")
        self.assertEqual(2, self.helper.calls)

    def test_a_finished_test_drops_the_hosts_reading(self):
        SMART.latest("node-1")
        ref = {"node": "node-1", "disk": "sda", "test": "short", "baseline": "old",
               "started_epoch": int(time.time()) - 100, "expected_seconds": 60}
        done = {"name": "sda", "test": {"active": False},
                "self_tests": [{"signature": "new", "status": "Completed without error"}]}
        with mock.patch.object(SMART, "disk", return_value={"name": "sda", "test": {"active": True}}):
            self.assertEqual("running", SMART.progress(ref)[0])
        SMART.latest("node-1")
        self.assertEqual(1, self.helper.calls)
        with mock.patch.object(SMART, "disk", return_value=done):
            self.assertEqual("succeeded", SMART.progress(ref)[0])
        SMART.latest("node-1")
        self.assertEqual(2, self.helper.calls)

    def test_while_a_test_runs_the_drive_is_read_again_at_the_pages_pace(self):
        ROWS[0]["test"] = {"active": True, "remaining_percent": 70}
        self.addCleanup(ROWS[0].pop, "test")
        SMART.latest_disk("node-1", "sda")
        self.now[0] += SMART.ACTIVE_SECONDS
        SMART.latest_disk("node-1", "sda")
        self.assertEqual(2, self.helper.calls)

    def test_without_a_test_two_requests_15_s_apart_read_once(self):
        SMART.latest_disk("node-1", "sda")
        self.now[0] += 15
        SMART.latest_disk("node-1", "sda")
        self.assertEqual(1, self.helper.calls)

    def test_the_route_answers_from_the_reading(self):
        request = SimpleNamespace(query={"node": ["node-1"], "disk": ["sda"]})
        with mock.patch.object(NODES, "get_app_settings", return_value={}), \
             mock.patch.object(NODES, "smart_disk_health", return_value={"state": "healthy"}):
            for _ in range(3):
                report = NODES._smart(request)
        self.assertEqual("sda", report["name"])
        self.assertIn("health_assessment", report)
        NODES._smart(SimpleNamespace(query={"node": ["node-1"]}))
        self.assertEqual(1, self.helper.calls)


class ProbeLoopTests(Base):
    def setUp(self):
        super().setUp()
        PROBES.stop()
        PROBES.reset()
        self.addCleanup(PROBES.reset)
        patch = mock.patch.object(PROBES.NAMES, "kget", probe_pods, create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_probe_loops_reading_feeds_the_page(self):
        PROBES.run_round(now=1000, smart=True)
        self.assertEqual(1, self.helper.calls)
        self.assertEqual(["sda", "sdb"], [row["name"] for row in SMART.latest("node-1")["disks"]])
        self.assertEqual(1, self.helper.calls)

    def test_the_probe_loops_reading_lasts_until_its_next_one_has_had_time_to_land(self):
        PROBES.run_round(now=1000, smart=True)
        self.now[0] += PROBES.SMART_EVERY + PROBES.SMART_TIMEOUT + PROBES.INTERVAL - 1
        SMART.latest("node-1")
        self.assertEqual(1, self.helper.calls, "the page read every drive before the loop's next reading was due")
        self.now[0] += 2
        SMART.latest("node-1")
        self.assertEqual(2, self.helper.calls)

    def test_the_probe_loops_smart_read_is_signed(self):
        PROBES.run_round(now=1000, smart=True)
        request = next(r for r in self.helper.requests if ":9100/" in getattr(r, "full_url", r))
        headers = dict(request.header_items())
        stamp = headers["X-homestead-time"]
        expected = hmac.new(KEY, stamp.encode() + b".GET /", hashlib.sha256).hexdigest()
        self.assertEqual(expected, headers["X-homestead-signature"])


class SignedReadTests(Base):
    def test_the_servers_read_of_one_drive_is_signed_over_its_path(self):
        with mock.patch.object(SMART.urllib.request, "urlopen") as opened:
            opened.return_value = _Response(json.dumps({"disk": {"name": "sda"}}).encode())
            SMART.disk("node-1", "sda")
        request = opened.call_args[0][0]
        headers = dict(request.header_items())
        stamp = headers["X-homestead-time"]
        expected = hmac.new(KEY, stamp.encode() + b".GET /?disk=sda", hashlib.sha256).hexdigest()
        self.assertEqual(expected, headers["X-homestead-signature"])
        self.assertEqual("GET", request.get_method())


def load_helper():
    spec = importlib.util.spec_from_file_location("smart_helper_under_test", ROOT / "server" / "probe" / "smart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.smart = load_helper()
        store = tempfile.NamedTemporaryFile(delete=False)
        store.write(KEY)
        store.close()
        self.addCleanup(lambda: Path(store.name).unlink())
        self.smart.AUTH_STORE = store.name
        self.reads = []
        self.smart.disks = lambda: [("sda", "/host/dev/sda")]
        self.smart.report = lambda name, path: self.reads.append(name) or {"name": name}

    def get(self, path, headers=None):
        handler = self.smart.H.__new__(self.smart.H)
        handler.path, handler.headers, sent = path, headers or {}, []
        handler.send_json = lambda code, value: sent.append((code, value))
        handler.do_GET()
        return sent[0]

    def signed(self, message, stamp=None):
        stamp = str(int(time.time())) if stamp is None else stamp
        return {"X-Homestead-Time": stamp,
                "X-Homestead-Signature": hmac.new(KEY, stamp.encode() + b"." + message,
                                                  hashlib.sha256).hexdigest()}

    def test_an_unsigned_read_is_refused_and_reads_no_drive(self):
        code, _ = self.get("/")
        self.assertEqual(403, code)
        code, _ = self.get("/?disk=sda")
        self.assertEqual(403, code)
        self.assertEqual([], self.reads)

    def test_a_signed_read_is_answered(self):
        code, value = self.get("/", self.signed(b"GET /"))
        self.assertEqual(200, code)
        self.assertEqual([{"name": "sda"}], value["disks"])
        code, value = self.get("/?disk=sda", self.signed(b"GET /?disk=sda"))
        self.assertEqual((200, "sda"), (code, value["disk"]["name"]))

    def test_a_signature_for_another_path_or_an_old_one_is_refused(self):
        self.assertEqual(403, self.get("/?disk=sda", self.signed(b"GET /"))[0])
        self.assertEqual(403, self.get("/", self.signed(b"GET /", str(int(time.time()) - 120)))[0])
        self.assertEqual([], self.reads)

    def test_the_signature_window_is_30_seconds(self):
        now = int(time.time())
        self.assertEqual(200, self.get("/", self.signed(b"GET /", str(now - 29)))[0])
        self.assertEqual(200, self.get("/", self.signed(b"GET /", str(now + 29)))[0])
        self.assertEqual(403, self.get("/", self.signed(b"GET /", str(now - 31)))[0])
        self.assertEqual(403, self.get("/", self.signed(b"GET /", str(now + 31)))[0])

    def test_the_health_check_needs_no_signature(self):
        self.assertEqual(200, self.get("/healthz")[0])


if __name__ == "__main__":
    unittest.main()
