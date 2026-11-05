"""The node probe's helpers under two Homestead copies (#974 parts 2 and 4).

The SMART helper reads a host's drives side by side and keeps each drive's
reading for a few minutes, so a second caller is answered without touching
the drives again. The probe keeps its disk-rate window per caller, so a
second caller does not shorten the first one's.

Real threads, a real HTTP server on 127.0.0.1 and real sockets are used where
the fakes would hide the behaviour: concurrency, the caller's address."""
import http.client
import json
import os
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
sys.path.insert(0, str(ROOT / "server" / "probe"))
sys.path.insert(0, str(ROOT / "tests"))

import probe  # noqa: E402
from homestead_http import BoundedHTTPServer  # noqa: E402
from test_smart_readings import load_helper  # noqa: E402

IDLE = {"smart_support": {"available": True, "enabled": True}, "smart_status": {"passed": True},
        "temperature": {"current": 40},
        "ata_smart_data": {"self_test": {"status": {"string": "No self-test running"},
                                         "polling_minutes": {"short": 2, "extended": 60}}}}
TESTING = {"smart_support": {"available": True, "enabled": True}, "smart_status": {"passed": True},
           "ata_smart_data": {"self_test": {"status": {"string": "Self-test routine in progress",
                                                       "remaining_percent": 60},
                                            "polling_minutes": {"short": 2}}}}


class Smartctl:
    """A fake smartctl that records each argv; reads may wait on a barrier
    or a delay, as a real one spinning a drive up does."""

    def __init__(self, barrier=None, delay=0.0):
        self.argvs, self.barrier, self.delay = [], barrier, delay
        self.answer = IDLE
        self._lock = threading.Lock()

    def reads(self, name=None):
        return [argv for argv in self.argvs
                if "-a" in argv and (name is None or argv[-1].endswith("/" + name))]

    def __call__(self, argv, **_):
        with self._lock:
            self.argvs.append(list(argv))
        if "-a" in argv:
            if self.barrier is not None:
                self.barrier.wait()
            if self.delay:
                time.sleep(self.delay)
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.answer), stderr="")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class SmartHelperBase(unittest.TestCase):
    DRIVES = [("sda", "/host/dev/sda"), ("sdb", "/host/dev/sdb")]

    def setUp(self):
        self.smart = load_helper()
        self.smart.device_type = lambda name: "sat"
        self.smart.disks = lambda: list(self.DRIVES)
        self.smart.signature_ok = lambda *_: True
        # The test paths are not real device nodes; each stands for one drive.
        self.real_identity, self.smart.identity = self.smart.identity, lambda path: (path,)
        self.clock = Clock()
        self.smart._clock = self.clock
        self.smartctl = Smartctl()
        patch = mock.patch.object(self.smart.subprocess, "run", self.smartctl)
        patch.start()
        self.addCleanup(patch.stop)

    def get(self, path="/"):
        handler = self.smart.H.__new__(self.smart.H)
        handler.path, handler.headers, sent = path, {}, []
        handler.send_json = lambda code, value: sent.append((code, value))
        handler.do_GET()
        return sent[0]

    def serve(self):
        server = BoundedHTTPServer(("127.0.0.1", 0), self.smart.H, max_connections=8)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        return server.server_address[1]

    def request(self, port, method="GET", path="/", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            raw = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if raw is not None else {}
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode())
        finally:
            connection.close()


class ParallelReadTests(SmartHelperBase):
    def test_the_drives_are_read_side_by_side(self):
        # Each read waits until the other drive's read has started too: read
        # one after the other, the first would wait out the barrier and fail.
        self.smartctl.barrier = threading.Barrier(2, timeout=3)
        code, value = self.get()
        self.assertEqual(200, code, value)
        self.assertEqual(["sda", "sdb"], [row["name"] for row in value["disks"]])
        for argv in self.smartctl.reads():
            self.assertEqual(["-n", "standby,3"], argv[1:3], argv)

    def test_a_host_with_many_drives_is_read_by_a_bounded_number_of_threads(self):
        self.DRIVES = [(f"sd{chr(97 + n)}", f"/host/dev/sd{chr(97 + n)}") for n in range(20)]
        seen, lock, most = [0], threading.Lock(), [0]
        answer = self.smartctl

        def counting(argv, **kwargs):
            with lock:
                seen[0] += 1
                most[0] = max(most[0], seen[0])
            try:
                time.sleep(0.02)
                return answer(argv, **kwargs)
            finally:
                with lock:
                    seen[0] -= 1
        with mock.patch.object(self.smart.subprocess, "run", counting):
            code, value = self.get()
        self.assertEqual(200, code)
        self.assertEqual(20, len(value["disks"]))
        self.assertGreater(most[0], 1)
        self.assertLessEqual(most[0], self.smart.READERS)

    def test_one_drive_that_fails_still_fails_the_read_as_before(self):
        def failing(argv, **kwargs):
            if argv[-1].endswith("/sdb"):
                raise self.smart.subprocess.TimeoutExpired(argv, 15)
            return self.smartctl(argv, **kwargs)
        with mock.patch.object(self.smart.subprocess, "run", failing):
            code, _ = self.get()
        self.assertEqual(400, code)


class SharedReadingTests(SmartHelperBase):
    def test_two_callers_within_four_minutes_read_each_drive_once(self):
        self.assertEqual(200, self.get()[0])
        self.clock.now += 239
        code, value = self.get()
        self.assertEqual(200, code)
        self.assertEqual(1, len(self.smartctl.reads("sda")))
        self.assertEqual(1, len(self.smartctl.reads("sdb")))
        self.assertEqual(40, value["disks"][0]["temperature_c"])

    def test_after_four_minutes_the_drives_are_read_again(self):
        self.get()
        self.clock.now += 241
        self.get()
        self.assertEqual(2, len(self.smartctl.reads("sda")))
        self.assertEqual(2, len(self.smartctl.reads("sdb")))

    def test_the_probe_loops_own_cadence_always_gets_a_fresh_reading(self):
        # One Homestead copy asks every SMART_EVERY seconds; the kept reading
        # must be gone by then, or a copy on its own would read every other round.
        import homestead_node_probes as PROBES
        self.get()
        self.clock.now += PROBES.SMART_EVERY
        self.get()
        self.assertEqual(2, len(self.smartctl.reads("sda")))

    def test_callers_at_the_same_moment_share_one_read_over_real_sockets(self):
        self.smartctl.delay = 0.3
        port = self.serve()
        answers, start = [], threading.Barrier(3, timeout=5)

        def call():
            start.wait()
            answers.append(self.request(port))
        threads = [threading.Thread(target=call) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual([200, 200, 200], [code for code, _ in answers])
        self.assertEqual(1, len(self.smartctl.reads("sda")))
        self.assertEqual(1, len(self.smartctl.reads("sdb")))

    def test_a_drive_running_a_self_test_is_not_kept_so_its_progress_moves(self):
        self.smartctl.answer = TESTING
        _, value = self.get()
        self.assertTrue(value["disks"][0]["test"]["active"])
        self.clock.now += 15
        self.get()
        self.assertEqual(2, len(self.smartctl.reads("sda")))

    def test_starting_a_self_test_drops_that_drives_kept_reading(self):
        port = self.serve()
        self.assertEqual(200, self.request(port)[0])
        code, answer = self.request(port, "POST", "/", {"node": self.smart.NODE, "disk": "sda",
                                                        "test": "short"})
        self.assertEqual(200, code, answer)
        self.request(port)
        self.assertEqual(2, len([argv for argv in self.smartctl.reads("sda") if "-n" in argv]))
        self.assertEqual(1, len(self.smartctl.reads("sdb")))

    def test_aborting_a_self_test_drops_that_drives_kept_reading(self):
        self.smartctl.answer = IDLE
        port = self.serve()
        self.request(port)
        code, _ = self.request(port, "POST", "/", {"node": self.smart.NODE, "disk": "sdb",
                                                   "test": "abort"})
        self.assertEqual(200, code)
        self.request(port)
        self.assertEqual(1, len(self.smartctl.reads("sda")))
        self.assertEqual(2, len(self.smartctl.reads("sdb")))

    def test_one_drive_asked_for_by_name_is_read_afresh_awake(self):
        self.get()
        code, value = self.get("/?disk=sda")
        self.assertEqual(200, code)
        named = self.smartctl.reads("sda")[-1]
        self.assertNotIn("-n", named)
        self.assertEqual(2, len(self.smartctl.reads("sda")))

    def test_a_drive_no_longer_there_is_forgotten(self):
        self.get()
        self.DRIVES = [("sda", "/host/dev/sda")]
        self.get()
        self.DRIVES = SmartHelperBase.DRIVES
        self.get()                          # back again: read afresh
        self.assertEqual(1, len(self.smartctl.reads("sda")))
        self.assertEqual(2, len(self.smartctl.reads("sdb")))

    def test_a_drive_swapped_in_the_same_slot_is_read_afresh(self):
        # A replaced drive comes back at the same /dev path but as a new
        # device node; its old reading must not be served as the new drive's.
        nodes = {"/host/dev/sda": (2048, 11), "/host/dev/sdb": (2064, 12)}
        self.smart.identity = lambda path: nodes[path]
        self.get()
        nodes["/host/dev/sdb"] = (2064, 99)
        self.clock.now += 10
        self.get()
        self.assertEqual(1, len(self.smartctl.reads("sda")))
        self.assertEqual(2, len(self.smartctl.reads("sdb")))

    def test_the_identity_of_a_real_device_node_comes_from_stat(self):
        with tempfile.NamedTemporaryFile() as one, tempfile.NamedTemporaryFile() as other:
            self.assertEqual(self.real_identity(one.name), self.real_identity(one.name))
            self.assertIsNotNone(self.real_identity(one.name))
            self.assertNotEqual(self.real_identity(one.name), self.real_identity(other.name))
        self.assertIsNone(self.real_identity("/nonexistent/sdz"))

    def test_a_read_in_flight_when_a_test_starts_is_not_kept(self):
        # The read began before the self-test was started; its "no test
        # running" row must not stand for the next 240 s.
        answer = self.smartctl

        def racing(argv, **kwargs):
            done = answer(argv, **kwargs)
            if "-a" in argv and argv[-1].endswith("/sda") and len(answer.reads("sda")) == 1:
                self.smart.drop_kept("sda")        # a POST start landing mid-read
            return done
        with mock.patch.object(self.smart.subprocess, "run", racing):
            self.get()
            self.get()
        self.assertEqual(2, len(self.smartctl.reads("sda")))
        self.assertEqual(1, len(self.smartctl.reads("sdb")))

    def test_a_drive_that_could_not_be_opened_is_not_kept(self):
        def unopenable(argv, **kwargs):
            if argv[-1].endswith("/sda"):
                answer.argvs.append(list(argv))
                return SimpleNamespace(returncode=2, stdout="", stderr="open failed")
            return answer(argv, **kwargs)
        answer = self.smartctl
        with mock.patch.object(self.smart.subprocess, "run", unopenable):
            _, value = self.get()
            self.assertEqual("open failed", value["disks"][0]["unavailable_reason"])
            self.get()
        self.assertEqual(2, len(self.smartctl.reads("sda")))
        self.assertEqual(1, len(self.smartctl.reads("sdb")))

    def test_a_drive_stuck_in_an_earlier_read_answers_unavailable_not_never(self):
        # A smartctl hung on one drive must not hold every later read of the host.
        self.smart.LOCK_SECONDS = 0.2
        release, started, answer = threading.Event(), threading.Event(), self.smartctl

        def stuck(argv, **kwargs):
            if "-a" in argv and argv[-1].endswith("/sda") and not release.is_set():
                started.set()
                release.wait(10)
            return answer(argv, **kwargs)
        with mock.patch.object(self.smart.subprocess, "run", stuck):
            first = threading.Thread(target=self.get)
            first.start()
            self.assertTrue(started.wait(5))
            code, value = self.get()
            release.set()
            first.join(10)
        self.assertEqual(200, code)
        sda, sdb = value["disks"]
        self.assertFalse(sda["available"])
        self.assertIn("still being read", sda["unavailable_reason"])
        self.assertTrue(sdb["available"])


def _usb_tree(root, drives):
    """A fake /sys: each drive's device link points where the kernel would put it."""
    devices = Path(root, "devices")
    block = Path(root, "class", "block")
    block.mkdir(parents=True)
    for name, where in drives.items():
        target = devices / where
        target.mkdir(parents=True)
        (block / name).mkdir()
        (block / name / "device").symlink_to(target)


class UsbBridgeTests(SmartHelperBase):
    DRIVES = [("sda", "/host/dev/sda"), ("sdb", "/host/dev/sdb"), ("sdc", "/host/dev/sdc"),
              ("nvme0n1", "/host/dev/nvme0n1")]
    TREE = {"sda": "pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0",
            "sdb": "pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:1",
            "sdc": "pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0",
            "nvme0n1": "pci0000:00/0000:00:1d.0/0000:03:00.0/nvme/nvme0"}

    def setUp(self):
        super().setUp()
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        _usb_tree(root, self.TREE)
        self.smart.SYS = root

    def test_drives_behind_one_usb_bridge_are_read_one_at_a_time(self):
        lock, busy, overlap = threading.Lock(), set(), []
        answer = self.smartctl

        def watching(argv, **kwargs):
            name = argv[-1].rsplit("/", 1)[1]
            with lock:
                busy.add(name)
                overlap.append(set(busy))
            try:
                time.sleep(0.1)
                return answer(argv, **kwargs)
            finally:
                with lock:
                    busy.discard(name)
        with mock.patch.object(self.smart.subprocess, "run", watching):
            code, value = self.get()
        self.assertEqual(200, code)
        self.assertEqual(["sda", "sdb", "sdc", "nvme0n1"], [row["name"] for row in value["disks"]])
        self.assertFalse(any({"sda", "sdb"} <= seen for seen in overlap), overlap)
        # Drives on other transports are still read side by side.
        self.assertTrue(any(len(seen) >= 2 for seen in overlap), overlap)

    def test_the_usb_bridge_is_the_device_on_the_root_port(self):
        self.assertEqual(self.smart.usb_bridge("sda"), self.smart.usb_bridge("sdb"))
        self.assertIsNotNone(self.smart.usb_bridge("sda"))
        self.assertIsNone(self.smart.usb_bridge("sdc"))
        self.assertIsNone(self.smart.usb_bridge("nvme0n1"))


def _diskstats(root, sectors):
    block = Path(root, "sys", "class", "block", "sda")
    (block / "device").mkdir(parents=True, exist_ok=True)
    (block / "size").write_text("1953525168\n")
    proc = Path(root, "proc")
    proc.mkdir(exist_ok=True)
    fields = ["8", "0", "sda", "100", "0", str(sectors), "0", "50", "0", str(sectors // 2), "0",
              "0", "0", "0"]
    (proc / "diskstats").write_text(" ".join(fields) + "\n")


class DiskRateTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        for name, value in (("SYS", os.path.join(self.root, "sys")),
                            ("PROC", os.path.join(self.root, "proc")),
                            ("DEV", os.path.join(self.root, "dev"))):
            patch = mock.patch.object(probe, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.clock = Clock()
        patch = mock.patch.object(probe, "_clock", self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        probe._DISK_PREV.clear()
        self.addCleanup(probe._DISK_PREV.clear)

    def rate(self, caller):
        return probe.disk_activity(caller)[0]["read_mbps"]

    def test_a_second_caller_does_not_shorten_the_first_ones_window(self):
        _diskstats(self.root, 0)
        self.rate("198.51.100.1")
        self.clock.now += 15
        _diskstats(self.root, 1_000_000)
        self.rate("198.51.100.2")          # the other copy, between the first one's reads
        self.clock.now += 5
        # 1,000,000 sectors of 512 bytes over the first caller's 20 s.
        self.assertEqual(25.6, self.rate("198.51.100.1"))

    def test_each_caller_starts_its_own_window(self):
        _diskstats(self.root, 0)
        self.rate("198.51.100.1")
        self.clock.now += 10
        _diskstats(self.root, 1_000_000)
        self.assertEqual(0.0, self.rate("198.51.100.2"))     # its first request has no window yet
        self.assertEqual(51.2, self.rate("198.51.100.1"))

    def test_callers_kept_are_bounded_and_old_ones_dropped(self):
        _diskstats(self.root, 0)
        for n in range(3 * probe.CALLERS):
            self.rate(f"198.51.100.{n}")
        self.assertLessEqual(len(probe._DISK_PREV), probe.CALLERS)
        self.clock.now += probe.CALLER_SECONDS + 1
        self.rate("198.51.100.250")
        self.assertEqual(["198.51.100.250"], list(probe._DISK_PREV))

    def test_the_caller_is_told_apart_by_its_address_over_real_sockets(self):
        # Two Homestead copies are two pods with two addresses. 127.0.0.2 is
        # loopback too on Linux, so a real connection can come from either.
        server = BoundedHTTPServer(("127.0.0.1", 0), probe.H, max_connections=8)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        port = server.server_address[1]

        def read(source):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10,
                                                    source_address=(source, 0))
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                disks = json.loads(response.read().decode())["disks"]
                return next(disk for disk in disks if disk["name"] == "sda")["read_mbps"]
            finally:
                connection.close()

        _diskstats(self.root, 0)
        read("127.0.0.1")
        self.clock.now += 15
        _diskstats(self.root, 1_000_000)
        read("127.0.0.2")
        self.clock.now += 5
        self.assertEqual(25.6, read("127.0.0.1"))
        self.assertEqual(["127.0.0.1", "127.0.0.2"], sorted(probe._DISK_PREV))


if __name__ == "__main__":
    unittest.main()
