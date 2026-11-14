"""/readyz (#736): open, cheap, one bit; the self-data fence answers it as it
answers /healthz."""
import os
import sys
import tempfile
import http.client
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import server
import homestead_fleet as FLEET
import homestead_metrics as METRICS
import homestead_readyz as READYZ
import homestead_routes as MODULE_ROUTES
import homestead_self_data_fence as FENCE
import homestead_shared as SHARED
from homestead_storage_journal import Held
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import no_cluster


def setUpModule():
    # No cluster here: a read on the way to what a test checks fails at once, as on CI,
    # instead of looking the API server's name up (tests/fakes/kube.py no_cluster).
    unittest.enterModuleContext(no_cluster())


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ReadyzChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.api = mock.Mock(return_value='{"major": "1"}')
        for patch in (mock.patch.dict(READYZ._state, READYZ.START),
                      mock.patch.object(READYZ, "_clock", self.clock),
                      mock.patch.object(READYZ, "DATA_DIR", self.temp.name),
                      mock.patch.object(READYZ.KUBE, "raw_get", self.api)):
            patch.start(); self.addCleanup(patch.stop)

    def answer(self):
        reply = READYZ.answer()
        return reply.code, reply.body

    def test_ready_when_the_api_answers_and_data_is_writable(self):
        self.assertEqual((200, {"ready": True}), self.answer())
        self.api.assert_called_once_with("/version", timeout=2)
        self.assertEqual([], list(Path(self.temp.name).iterdir()), "the probe file is removed again")

    def test_not_ready_before_the_first_check_has_finished(self):
        """Another caller is running the first check: no optimistic answer."""
        with READYZ._lock:
            self.assertEqual((503, {"ready": False}), self.answer())
        self.api.assert_not_called()

    def test_not_ready_when_the_api_last_answered_more_than_30_seconds_ago(self):
        self.assertEqual(200, self.answer()[0])
        self.api.side_effect = OSError("connection refused")
        self.clock.now += 25
        self.assertEqual(200, self.answer()[0], "a failure within 30 s of the last answer is not yet unready")
        self.clock.now += 6
        self.assertEqual((503, {"ready": False}), self.answer())

    def test_not_ready_when_data_cannot_be_written(self):
        with mock.patch.object(READYZ, "DATA_DIR", os.path.join(self.temp.name, "missing")):
            self.assertEqual((503, {"ready": False}), self.answer())

    def test_a_burst_of_probes_costs_one_api_call_per_10_s_and_one_write_per_30_s(self):
        with mock.patch.object(READYZ, "_write_probe", wraps=READYZ._write_probe) as write:
            for _ in range(50):
                self.answer()
                self.clock.now += 0.19
            self.assertEqual(1, self.api.call_count)
            self.assertEqual(1, write.call_count)
            self.clock.now = 1010.0
            self.answer()
            self.assertEqual(2, self.api.call_count)
            self.assertEqual(1, write.call_count)
            self.clock.now = 1030.0
            self.answer()
            self.assertEqual(2, write.call_count)

    def test_the_probe_write_goes_through_the_fence(self):
        """The fence holds after the guard let /readyz through: no write, a
        clean not-ready, never a 500."""
        with mock.patch.object(SHARED, "WRITE_SCOPE", mock.Mock(side_effect=Held("held"))) as scope:
            self.assertEqual((503, {"ready": False}), self.answer())
        scope.assert_called_once_with(os.path.join(self.temp.name, f".readyz-{READYZ.POD_NAME}"))
        self.assertEqual([], list(Path(self.temp.name).iterdir()))

    def test_the_probe_write_checks_the_write_guard(self):
        with mock.patch.object(SHARED, "WRITE_GUARD", mock.Mock(side_effect=Held("held"))) as guard:
            self.assertEqual((503, {"ready": False}), self.answer())
        guard.assert_called_once()
        self.assertEqual([], list(Path(self.temp.name).iterdir()))

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_the_probe_write_does_not_race_a_freeze(self):
        """A freeze drains activity; the probe's write is activity, so while
        frozen it is refused, not written past the freeze."""
        locks = tempfile.TemporaryDirectory(); self.addCleanup(locks.cleanup)
        mine, mover = FENCE.WriteBarrier(locks.name, lambda: None), FENCE.WriteBarrier(locks.name, lambda: None)
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: mine.activity()):
            with mover.freeze(timeout=.05):
                self.assertEqual((503, {"ready": False}), self.answer())
                self.assertEqual([], list(Path(self.temp.name).iterdir()))
            self.clock.now += READYZ.DATA_EVERY
            self.assertEqual((200, {"ready": True}), self.answer(), "after the freeze it writes again")

    def test_a_probe_file_that_cannot_be_removed_still_counts_as_written(self):
        with mock.patch.object(READYZ.os, "remove", side_effect=OSError("busy")):
            self.assertEqual(200, self.answer()[0])

    def test_without_the_data_probe_only_the_api_is_checked(self):
        """What /metrics asks: the API check, and the last /data result,
        never a write of its own."""
        with mock.patch.object(READYZ, "_write_probe", side_effect=AssertionError("wrote")):
            state = READYZ.state(probe_data=False)
        self.assertEqual({"kube_api": True, "data_writable": None}, state["checks"])
        self.assertFalse(state["ready"])
        self.answer()
        self.assertTrue(READYZ.state(probe_data=False)["checks"]["data_writable"])

    def test_state_has_the_detail_the_answer_withholds(self):
        self.api.side_effect = OSError("down")
        self.answer()
        state = READYZ.state()
        self.assertEqual({"kube_api": False, "data_writable": True}, state["checks"])
        self.assertFalse(state["ready"])

    def test_answered_locally_never_relayed_to_a_linked_cluster(self):
        self.assertIn("/readyz", FLEET.LOCAL)
        self.assertIn("/metrics", FLEET.LOCAL)


def handler(path, method="GET"):
    h = object.__new__(server.H)
    h.command, h.path, h.headers = method, path, {}
    h._send = mock.Mock(); h._who = mock.Mock(return_value=None)
    return h


class ReadyzThroughTheServer(unittest.TestCase):
    """The same request the kubelet or an uptime checker sends: no session, no key."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.fence = mock.Mock()
        self.fence.inspect.return_value = {"mode": "start", "writable": False}
        self.fence.require_write.side_effect = Held("held")
        self.writes = mock.Mock(side_effect=AssertionError("/readyz wrote to /data during the fence"))
        for patch in (mock.patch.object(server, "_self_data_fence", self.fence),
                      mock.patch.object(server, "_self_data_boot_pending", True),
                      mock.patch.object(server, "_self_data_boot_failed", False),
                      mock.patch.object(server.OPS, "DATA_DIR", self.temp.name),
                      mock.patch.object(server.AUTH, "review_signing_key", return_value=b"read-only-fixture"),
                      mock.patch.object(server.CFACCESS, "enabled", return_value=False),
                      mock.patch.dict(READYZ._state, READYZ.START),
                      mock.patch.object(READYZ, "DATA_DIR", self.temp.name),
                      mock.patch.object(READYZ, "_write_probe", self.writes)):
            patch.start(); self.addCleanup(patch.stop)

    def sent(self, path):
        h = handler(path)
        self.assertTrue(h._guard(path), path)
        return h._send.call_args.args[:2]

    def test_answered_without_a_session_or_key(self):
        self.fence.require_write.side_effect = None
        self.writes.side_effect = None
        with mock.patch.object(server, "_self_data_boot_pending", False), \
             mock.patch.object(READYZ.KUBE, "raw_get", return_value="{}"):
            h = handler("/readyz")
            self.assertIsNone(h._guard("/readyz"))
            h.do_GET()
        self.assertEqual((200, {"ready": True}), h._send.call_args.args[:2])
        h._who.assert_not_called()

    def test_boot_handoff_pending_with_a_readable_status(self):
        self.assertEqual((200, {"ok": True, "data_handoff": True, "read_only": True}), self.sent("/healthz"))
        self.assertEqual((200, {"ready": True}), self.sent("/readyz"))

    def test_boot_handoff_held(self):
        with mock.patch.object(server, "self_data_boot_status", side_effect=server.SELF_DATA_FENCE.Held("held")):
            for path in ("/healthz", "/readyz"):
                code, body = self.sent(path)
                self.assertEqual(503, code, path)
            self.assertEqual({"ready": False}, body)

    def test_fence_holds_with_a_recovery_record(self):
        self.fence.recovery.return_value = {"mode": "recovery", "writable": False, "operation": "a" * 24}
        with mock.patch.object(server, "_self_data_boot_pending", False):
            code, body = self.sent("/healthz")
            self.assertEqual(200, code); self.assertTrue(body["recovery"])
            self.assertEqual((200, {"ready": True}), self.sent("/readyz"))

    def test_fence_holds_without_a_recovery_record(self):
        self.fence.recovery.side_effect = RuntimeError("no record")
        with mock.patch.object(server, "_self_data_boot_pending", False):
            self.assertEqual(503, self.sent("/healthz")[0])
            self.assertEqual((503, {"ready": False}), self.sent("/readyz"))

    def test_data_only_answers_as_healthz_under_the_fence(self):
        """All four rows of the fence's table, through the whole of do_GET:
        same status as /healthz, and the probe's write never runs (tearDown)."""
        def got(path):
            h = handler(path)
            h.do_GET()
            return h._send.call_args.args[:2]

        def row(expected):
            code, body = got("/readyz?check=data")
            self.assertEqual(expected, code)
            self.assertEqual({"ready": expected == 200}, body)
            self.assertEqual(expected, got("/healthz")[0])

        row(200)  # boot handoff pending, status readable
        with mock.patch.object(server, "self_data_boot_status", side_effect=server.SELF_DATA_FENCE.Held("held")):
            row(503)  # boot handoff held
        with mock.patch.object(server, "_self_data_boot_pending", False):
            self.fence.recovery.return_value = {"mode": "recovery", "writable": False, "operation": "a" * 24}
            row(200)  # the fence holds, with a recovery record
            self.fence.recovery.side_effect = RuntimeError("no record")
            row(503)  # the fence holds, without one

    def test_metrics_while_the_fence_holds_never_writes_to_data(self):
        """/metrics is not in the guard's fence paths: it is read while the
        fence holds, and must not reach the probe write."""
        MODULE_ROUTES.forget("metrics"); self.addCleanup(MODULE_ROUTES.forget, "metrics")
        h = handler("/metrics")
        h._who.return_value = {"user": "viewer1", "role": "viewer", "remember": False, "started": 0}
        h.client_address = ("192.0.2.10", 40000)
        with mock.patch.object(server, "_self_data_boot_pending", False), \
             mock.patch.object(METRICS.SETTINGS_HELPERS, "get_app_settings", return_value={"metrics": {"enabled": True}}), \
             mock.patch.object(READYZ.KUBE, "raw_get", return_value="{}"):
            h.do_GET()
        code, body = h._send.call_args.args[:2]
        self.assertEqual(200, code)
        self.assertIn('homestead_metrics_source_up{source="kube_api"} 1', body)

    def tearDown(self):
        # In every fence state the guard answers first: /readyz never writes
        # its probe file to a volume that is being handed off.
        if self.writes.side_effect is not None:
            self.writes.assert_not_called()
            self.assertEqual([], list(Path(self.temp.name).iterdir()))


class ReadyzDataCheck(unittest.TestCase):
    """/readyz?check=data (#791): what the pod's readiness probe asks. A volume
    that can't be written counts; an API outage and a deliberate hold don't."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.api = mock.Mock(return_value='{"major": "1"}')
        for patch in (mock.patch.dict(READYZ._state, READYZ.START),
                      mock.patch.object(READYZ, "_clock", self.clock),
                      mock.patch.object(READYZ, "DATA_DIR", self.temp.name),
                      mock.patch.object(READYZ.KUBE, "raw_get", self.api)):
            patch.start(); self.addCleanup(patch.stop)

    def asked(self, check=None):
        query = {"check": [check]} if check else {}
        reply = READYZ.answer(MODULE_ROUTES.Request("GET", "/readyz", query, None, None, None))
        return reply.code, reply.body

    def unwritable(self):
        """A real folder the probe's user can't write, as a read-only volume is."""
        os.chmod(self.temp.name, 0o555)
        self.addCleanup(os.chmod, self.temp.name, 0o755)
        return self.temp.name

    def test_data_only_stays_ready_through_an_api_outage(self):
        self.api.side_effect = OSError("connection refused")
        self.assertEqual((200, {"ready": True}), self.asked("data"))
        self.api.assert_not_called()
        self.assertEqual((503, {"ready": False}), self.asked())
        self.api.assert_called_once()

    def test_data_only_not_ready_when_the_write_fails(self):
        with mock.patch.object(READYZ.os, "write", side_effect=OSError(28, "No space left on device")):
            self.assertEqual((503, {"ready": False}), self.asked("data"))

    @unittest.skipIf(os.geteuid() == 0, "root writes through mode 0555")
    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_data_only_not_ready_on_an_unwritable_volume_through_the_real_barrier(self):
        """What a read-only volume does on a real cluster: the barrier can't open
        its lock, and that must not read as a data move holding the write."""
        barrier = FENCE.WriteBarrier(self.unwritable(), lambda: None)
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: barrier.activity()):
            self.assertEqual((503, {"ready": False}), self.asked("data"))

    @unittest.skipIf(os.geteuid() == 0, "root writes through mode 0555")
    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_data_only_not_ready_when_the_barrier_lock_opens_but_the_file_cant_be_made(self):
        """The lock file is already there from before the fault: the barrier
        lets the write through and the probe's own file is what fails."""
        barrier = FENCE.WriteBarrier(self.temp.name, lambda: None)
        with barrier.activity():
            pass
        self.unwritable()
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: barrier.activity()):
            self.assertEqual((503, {"ready": False}), self.asked("data"))

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_data_only_not_ready_when_the_lock_cannot_be_taken(self):
        barrier = FENCE.WriteBarrier(self.temp.name, lambda: None)
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: barrier.activity()), \
             mock.patch.object(FENCE.fcntl, "flock", side_effect=OSError(5, "I/O error")):
            self.assertEqual((503, {"ready": False}), self.asked("data"))

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_data_only_ready_while_a_freeze_drains(self):
        """A data move's drain is not a bad volume: if it counted, the source
        could leave the Service before the fence is published."""
        locks = tempfile.TemporaryDirectory(); self.addCleanup(locks.cleanup)
        mine, mover = FENCE.WriteBarrier(locks.name, lambda: None), FENCE.WriteBarrier(locks.name, lambda: None)
        with mock.patch.object(SHARED, "WRITE_SCOPE", lambda path: mine.activity()):
            with mover.freeze(timeout=.05):
                self.assertEqual((200, {"ready": True}), self.asked("data"))
                self.assertEqual([], list(Path(self.temp.name).iterdir()), "held, so nothing written")
                self.assertEqual((503, {"ready": False}), self.asked(), "plain /readyz keeps saying no")
                self.assertIs(False, READYZ.state(probe_data=False)["checks"]["data_writable"])

    def test_data_only_ready_while_the_fence_refuses_the_write(self):
        with mock.patch.object(SHARED, "WRITE_GUARD", mock.Mock(side_effect=Held("held"))):
            self.assertEqual((200, {"ready": True}), self.asked("data"))
            self.assertEqual((503, {"ready": False}), self.asked())

    def test_data_only_recovers(self):
        with mock.patch.object(READYZ.os, "write", side_effect=OSError(30, "Read-only file system")):
            self.assertEqual((503, {"ready": False}), self.asked("data"))
        self.assertEqual((503, {"ready": False}), self.asked("data"), "the same moment: no new write")
        self.clock.now += 10
        self.assertEqual((200, {"ready": True}), self.asked("data"))

    def probes(self, times, fail_writes):
        """Probes at these seconds from now; the first fail_writes writes fail.
        The answers' codes, and how many writes were tried."""
        real, tried = os.write, []

        def write(fd, data):
            tried.append(fd)
            if len(tried) <= fail_writes:
                raise OSError(5, "Input/output error")
            return real(fd, data)

        start, codes = self.clock.now, []
        with mock.patch.object(READYZ.os, "write", write):
            for at in times:
                self.clock.now = start + at
                codes.append(self.asked("data")[0])
        return codes, len(tried)

    def healthy_and_due(self):
        self.assertEqual(200, self.asked("data")[0])
        self.clock.now += 30

    def test_one_failed_write_is_retried_at_the_next_probe(self):
        """kubelet takes a copy out after 3 failures 10 s apart: a failure kept
        for 30 s would turn one bad write into that."""
        self.healthy_and_due()
        codes, tried = self.probes([0, 10, 20, 30, 39], fail_writes=1)
        self.assertEqual([503, 200, 200, 200, 200], codes)
        self.assertEqual(2, tried, "and then once per 30 s again")

    def test_one_failed_write_with_a_probe_a_little_early_is_still_two_failures_at_most(self):
        self.healthy_and_due()
        codes, _ = self.probes([0, 9.9, 19.8, 29.7], fail_writes=1)
        self.assertEqual([503, 503, 200, 200], codes)

    def test_a_lasting_fault_fails_three_probes_in_a_row(self):
        self.healthy_and_due()
        codes, tried = self.probes([0, 10, 20], fail_writes=99)
        self.assertEqual([503, 503, 503], codes)
        self.assertEqual(3, tried, "a real write each time, not one result kept")

    def test_a_burst_while_failing_costs_one_write_per_10_s(self):
        self.healthy_and_due()
        _, tried = self.probes([i * .19 for i in range(50)], fail_writes=99)
        self.assertEqual(1, tried)

    def test_a_hold_is_asked_again_at_the_next_probe(self):
        """A bad read can pass as a hold when it falls between the guard's read
        and the barrier's: it stands for one probe, not 30 s."""
        with mock.patch.object(SHARED, "WRITE_GUARD", mock.Mock(side_effect=Held("held"))):
            self.assertEqual(200, self.asked("data")[0])
        self.clock.now += 10
        with mock.patch.object(READYZ.os, "write", side_effect=OSError(5, "Input/output error")):
            self.assertEqual((503, {"ready": False}), self.asked("data"))

    def test_a_hold_then_a_bad_volume_is_not_ready(self):
        with mock.patch.object(SHARED, "WRITE_GUARD", mock.Mock(side_effect=Held("held"))):
            self.assertEqual(200, self.asked("data")[0])
        self.clock.now += READYZ.DATA_EVERY
        with mock.patch.object(READYZ.os, "write", side_effect=OSError(30, "Read-only file system")):
            self.assertEqual((503, {"ready": False}), self.asked("data"))

    def test_a_stuck_write_stops_counting_as_ready(self):
        """A hung volume never fails the write: without a limit the last result,
        True, stands for ever."""
        self.assertEqual(200, self.asked("data")[0])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hang(fd):
            entered.set()
            release.wait(30)

        self.clock.now += READYZ.DATA_EVERY
        with mock.patch.object(READYZ.os, "fsync", hang):
            worker = threading.Thread(target=self.asked, args=("data",), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5))
            self.clock.now += 59
            self.assertEqual((200, {"ready": True}), self.asked("data"), "the last result, for now")
            self.assertIs(True, READYZ.state(probe_data=False)["checks"]["data_writable"])
            self.clock.now += 2
            self.assertEqual((503, {"ready": False}), self.asked("data"))
            self.assertIs(False, READYZ.state(probe_data=False)["checks"]["data_writable"], "and for plain /readyz")
            READYZ._state["api_ok_at"] = self.clock.now
            self.assertIs(False, READYZ.state()["ready"], "plain /readyz's answer, with the API fine")
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual((200, {"ready": True}), self.asked("data"), "the write finished after all")

    def test_a_hung_write_leaves_the_api_check_running(self):
        """The write hangs for real (a thread blocked on an event); callers
        meanwhile still check the API, so /metrics does not report an API
        outage that is not there (#1323)."""
        self.assertEqual(200, self.asked("data")[0])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hang(fd):
            entered.set()
            release.wait(30)

        self.clock.now += READYZ.DATA_EVERY
        with mock.patch.object(READYZ.os, "fsync", hang):
            worker = threading.Thread(target=self.asked, args=("data",), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5))
            self.api.reset_mock()
            self.clock.now += READYZ.API_EVERY
            self.assertTrue(READYZ.state(probe_data=False)["checks"]["kube_api"])
            self.api.assert_called_once_with("/version", timeout=2)
            self.assertEqual(self.clock.now, READYZ._state["api_ok_at"], "the API answer was refreshed")
            self.clock.now += READYZ.API_FRESH + 1
            self.assertTrue(READYZ.state()["checks"]["kube_api"], "plain /readyz checks the API too")
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_a_quiet_minute_is_not_a_stuck_write(self):
        """A write that finished long ago isn't in flight, however long nobody asks."""
        self.assertEqual(200, self.asked("data")[0])
        self.clock.now += READYZ.DATA_STUCK + 1
        self.assertIs(True, READYZ.state(probe_data=False)["checks"]["data_writable"])

    def test_a_stuck_write_after_a_hold_is_not_ready(self):
        with mock.patch.object(SHARED, "WRITE_GUARD", mock.Mock(side_effect=Held("held"))):
            self.assertEqual(200, self.asked("data")[0])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hang(fd):
            entered.set()
            release.wait(30)

        self.clock.now += READYZ.DATA_EVERY
        with mock.patch.object(READYZ.os, "fsync", hang):
            worker = threading.Thread(target=self.asked, args=("data",), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5))
            self.clock.now += READYZ.DATA_STUCK + 1
            self.assertEqual((503, {"ready": False}), self.asked("data"))
            release.set()
            worker.join(5)

    def test_another_check_value_is_the_whole_answer(self):
        self.api.side_effect = OSError("connection refused")
        self.assertEqual((503, {"ready": False}), self.asked("api"))

    def test_the_answer_never_says_which_check_failed(self):
        self.assertEqual({"ready"}, set(self.asked("data")[1]))

    def test_metrics_reads_the_result_the_probe_left(self):
        self.asked("data")
        self.assertEqual({"kube_api": True, "data_writable": True},
                         READYZ.state(probe_data=False)["checks"])


class ReadyzThroughTheRealFence(unittest.TestCase):
    """The real Fence and barrier, whose check reads the marker on /data, through
    do_GET, as a real install has them. What each probe answers is what decides
    whether a bad volume only leaves the Service or also restarts the pod."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chmod, self.temp.name, 0o755)
        api = mock.Mock(side_effect=OSError("connection refused"))
        self.fence = FENCE.Fence(api, "lab", "homestead", "pod-1", "homestead", self.temp.name)
        self.fence.checked = True
        self.barrier = FENCE.WriteBarrier(self.temp.name, server.require_self_data_write)
        for patch in (mock.patch.object(server, "_self_data_fence", self.fence),
                      mock.patch.object(server, "_self_data_barrier", self.barrier),
                      mock.patch.object(server, "_self_data_boot_pending", False),
                      mock.patch.object(server, "DATA_DIR", self.temp.name),
                      mock.patch.object(server.SELF_DATA_ROUTES, "DATA_DIR", self.temp.name),
                      mock.patch.object(server.CFACCESS, "enabled", return_value=False),
                      mock.patch.dict(READYZ._state, READYZ.START),
                      mock.patch.object(READYZ, "DATA_DIR", self.temp.name),
                      mock.patch.object(READYZ.KUBE, "raw_get", api)):
            patch.start(); self.addCleanup(patch.stop)

    def both(self):
        codes = []
        for path in ("/healthz", "/readyz?check=data"):
            h = handler(path)
            h.do_GET()
            codes.append(h._send.call_args.args[0])
        return codes

    @unittest.skipIf(os.geteuid() == 0, "root reads and writes through any mode")
    def test_a_folder_that_refuses_writes_leaves_healthz_answering(self):
        """Read-only or full: out of the Service, and no restart."""
        os.chmod(self.temp.name, 0o555)
        self.assertEqual([200, 503], self.both())

    @unittest.skipIf(os.geteuid() == 0, "root reads and writes through any mode")
    def test_a_folder_that_cannot_be_read_fails_both(self):
        """As an I/O error does: the guard reads the marker for /healthz too, so
        liveness fails and the pod restarts."""
        os.chmod(self.temp.name, 0o000)
        self.assertEqual([503, 503], self.both())

    def test_an_io_error_on_the_marker_fails_both(self):
        real = os.lstat

        def lstat(path, *args, **kwargs):
            if str(path).endswith(FENCE.MARKER):
                raise OSError(5, "Input/output error")
            return real(path, *args, **kwargs)

        with mock.patch.object(FENCE.os, "lstat", lstat):
            self.assertEqual([503, 503], self.both())

    def test_a_healthy_volume_answers_both(self):
        self.assertEqual([200, 200], self.both())
        self.assertEqual([".self-data-access-v1.lock"], os.listdir(self.temp.name),
                         "the probe went through the real barrier and left its lock file")

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_a_drain_answers_both(self):
        mover = FENCE.WriteBarrier(self.temp.name, lambda: None)
        with mover.freeze(timeout=.05):
            self.assertEqual([200, 200], self.both())

    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_a_lock_file_that_cannot_be_verified_stays_ready(self):
        """Every write is refused and a move can't start, but the page is the
        only thing that shows why: taking the address down would hide it."""
        lock = os.path.join(self.temp.name, ".self-data-access-v1.lock")
        with open(lock, "w", encoding="utf-8"):
            pass
        os.link(lock, lock + ".second")
        self.assertEqual([200, 200], self.both())
        with self.assertRaisesRegex(FENCE.MoveHeld, "cannot be verified"):
            with self.barrier.activity():
                pass


class DataUnavailableIsStillAMoveHold(unittest.TestCase):
    """Every existing catcher of the barrier's refusals sees what it saw before."""

    @unittest.skipIf(os.geteuid() == 0, "root writes through mode 0555")
    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_activity_refuses_with_the_same_kind_and_words(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        os.chmod(temp.name, 0o555); self.addCleanup(os.chmod, temp.name, 0o755)
        with self.assertRaises(FENCE.DataUnavailable) as caught:
            with FENCE.WriteBarrier(temp.name, lambda: None).activity():
                pass
        self.assertIsInstance(caught.exception, FENCE.MoveHeld)
        self.assertEqual("The shared data lock is unavailable, so no change was allowed", str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root writes through mode 0555")
    @unittest.skipUnless(FENCE.fcntl is not None, "Linux shared locking")
    def test_freeze_on_an_unwritable_volume_stays_a_plain_hold(self):
        """A plain Held from freeze keeps its own reply; a MoveHeld answers as
        a request held by a move, which this isn't yet."""
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        os.chmod(temp.name, 0o555); self.addCleanup(os.chmod, temp.name, 0o755)
        with self.assertRaises(Held) as caught:
            with FENCE.WriteBarrier(temp.name, lambda: None).freeze(timeout=.05):
                pass
        self.assertNotIsInstance(caught.exception, FENCE.MoveHeld)
        self.assertEqual("The shared data lock is unavailable, so no change was allowed", str(caught.exception))


class ReadyzDataCheckOverARealSocket(unittest.TestCase):
    """A real listener and a real GET with the query, as a kubelet's probe is."""

    def test_the_query_reaches_the_check(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        listener = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        answers = {}
        with mock.patch.dict(READYZ._state, READYZ.START), \
             mock.patch.object(READYZ, "DATA_DIR", temp.name), \
             mock.patch.object(READYZ.KUBE, "raw_get", side_effect=OSError("connection refused")), \
             mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
             mock.patch.object(server.H, "log_message"):
            thread.start()
            try:
                for path in ("/readyz?check=data", "/readyz"):
                    client = http.client.HTTPConnection(*listener.server_address, timeout=5)
                    try:
                        client.request("GET", path)
                        response = client.getresponse()
                        answers[path] = (response.status, response.read())
                    finally:
                        client.close()
            finally:
                listener.shutdown(); thread.join(3); listener.server_close()
        self.assertEqual(200, answers["/readyz?check=data"][0])
        self.assertEqual(503, answers["/readyz"][0])


if __name__ == "__main__":
    unittest.main()
