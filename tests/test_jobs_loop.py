"""Only the leader advances jobs, in one loop (#622 step 6).

A viewer's GET /api/operations reads the saved jobs and nothing more: no lock,
no resolver, no write. The leader's jobs loop runs the resolvers every 3 s
while a job is active and every 20 s otherwise; a standby replica's loop does
nothing. Code that needs a job moved on sooner wakes the loop."""
import ast
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_operations as OPS
import source_scan as SS
from fakes.job_store import store_writes


class JobsLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.tmp))
        self.saved = dict(OPS.RESOLVERS)
        self.addCleanup(lambda: (OPS.RESOLVERS.clear(), OPS.RESOLVERS.update(self.saved)))
        self.resolved = []

        def resolver(item):
            self.resolved.append(item["id"])
            return "running", 50, "Halfway"
        OPS.RESOLVERS["loop-test"] = resolver
        self.op = OPS.start("loop-test", "A job", {"kind": "Thing", "name": "t", "namespace": "lab"}, "/", {})["id"]

    def get_operations(self):
        return OPS.ROUTES[("GET", "/api/operations")][1](None)

    def test_a_viewers_get_runs_no_resolver_and_writes_nothing(self):
        with store_writes(OPS) as written:
            jobs = self.get_operations()
        self.assertEqual([], self.resolved)
        self.assertEqual([], written)
        self.assertEqual([self.op], [job["id"] for job in jobs])

    def test_a_viewers_get_takes_no_lock(self):
        with mock.patch.object(OPS.SHARED.SharedLock, "__enter__", side_effect=AssertionError("locked")):
            self.get_operations()

    def test_the_leader_advances_an_active_job_and_asks_again_in_3_s(self):
        self.assertEqual(OPS.ACTIVE_SECONDS, OPS.tick(lambda: True))
        self.assertEqual([self.op], self.resolved)
        self.assertEqual("Halfway", next(i for i in OPS.stored() if i["id"] == self.op)["message"])

    def test_a_standby_replica_advances_nothing(self):
        with store_writes(OPS) as written:
            OPS.tick(lambda: False)
        self.assertEqual([], self.resolved)
        self.assertEqual([], written)

    def test_with_nothing_active_the_leader_asks_again_in_20_s(self):
        OPS.RESOLVERS["loop-test"] = lambda item: ("succeeded", 100, "Done")
        OPS.tick(lambda: True)
        self.assertEqual(OPS.IDLE_SECONDS, OPS.tick(lambda: True))
        self.assertEqual((3, 20), (OPS.ACTIVE_SECONDS, OPS.IDLE_SECONDS))

    def test_a_failure_in_a_tick_does_not_stop_the_loop(self):
        with mock.patch.object(OPS, "list_operations", side_effect=RuntimeError("store unreadable")):
            self.assertEqual(OPS.IDLE_SECONDS, OPS.tick(lambda: True))

    def test_advance_soon_wakes_the_loop(self):
        woke = threading.Event()
        stop = threading.Event()
        with mock.patch.object(OPS, "tick", side_effect=lambda is_leader, beat=None: woke.set() or 3600):
            thread = threading.Thread(target=OPS.run_jobs, args=(lambda: True, stop), daemon=True)
            thread.start()
            self.assertTrue(woke.wait(2))
            woke.clear()
            OPS.advance_soon()
            self.assertTrue(woke.wait(2), "the loop did not wake")
            stop.set()
            OPS.advance_soon()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_starting_a_job_wakes_the_loop(self):
        OPS._wake.clear()
        OPS.start("loop-test", "Another", {"kind": "Thing", "name": "u", "namespace": "lab"}, "/", {})
        self.assertTrue(OPS._wake.is_set(), "a new job would wait up to 20 s for its first step")

    def test_resuming_a_job_wakes_the_loop(self):
        with OPS.transaction() as tx:
            items = tx.items
            next(i for i in items if i["id"] == self.op).update(status="failed")
            tx.save(items)
        self.enterContext(mock.patch.dict(OPS.RESUMABLE, {"loop-test": lambda item: ""}))
        OPS._wake.clear()
        OPS.resume(self.op)
        self.assertTrue(OPS._wake.is_set())

    def test_a_finished_cancel_wakes_the_loop(self):
        OPS._wake.clear()
        OPS.cancel(self.op)
        self.assertTrue(OPS._wake.is_set())

    def test_the_get_lists_newest_first_as_before(self):
        second = OPS.start("loop-test", "Another", {"kind": "Thing", "name": "u", "namespace": "lab"}, "/", {})["id"]
        with mock.patch.object(OPS, "_now", return_value="2099-01-01T00:00:00Z"):
            third = OPS.start("loop-test", "Newest", {"kind": "Thing", "name": "v", "namespace": "lab"}, "/", {})["id"]
        self.assertEqual(third, self.get_operations()[0]["id"])
        self.assertIn(second, [job["id"] for job in self.get_operations()])


class JobsLoopHealthTests(unittest.TestCase):
    """The loop says it is alive (a heartbeat), wakes when its replica becomes
    leader, and runs at most one round a second however often it is asked."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.tmp))
        self.enterContext(mock.patch.dict(OPS._tidied, {"at": time.time()}))  # tidied already today
        self.addCleanup(OPS._wake.clear)
        self.beats = []

    def beat(self, error=None):
        self.beats.append(error)

    def test_a_stop_during_the_pause_ends_the_loop_at_once(self):
        stop = threading.Event()
        self.enterContext(mock.patch.object(OPS, "MIN_ROUND_SECONDS", 30))
        with mock.patch.object(OPS, "tick", lambda is_leader, beat=None: stop.set() or 0):
            loop = threading.Thread(target=OPS.run_jobs, args=(lambda: True, stop), daemon=True)
            loop.start()
            loop.join(3)
        self.assertFalse(loop.is_alive(), "the loop sat out its pause after being stopped")

    def test_each_round_on_the_leader_beats(self):
        OPS.tick(lambda: True, self.beat)
        self.assertEqual([None], self.beats)

    def test_a_round_that_fails_beats_with_its_error(self):
        with mock.patch.object(OPS, "list_operations", side_effect=RuntimeError("store unreadable")), \
                mock.patch("builtins.print"):
            OPS.tick(lambda: True, self.beat)
        self.assertEqual(["store unreadable"], [str(e) for e in self.beats])

    def test_a_standby_replica_does_not_beat(self):
        OPS.tick(lambda: False, self.beat)
        self.assertEqual([], self.beats)

    def bound_jobs_loop(self):
        import homestead_cluster_server as CLUSTER
        import homestead_leader as LEADER
        calls, run = [], mock.Mock()
        self.addCleanup(LEADER._listeners.__setitem__, slice(None), list(LEADER._listeners))
        with mock.patch.object(CLUSTER, "beat", lambda *a, **k: calls.append((a, k)), create=True), \
                mock.patch.object(CLUSTER.OPS, "run_jobs", run):
            CLUSTER._jobs_loop()
            beat = run.call_args.kwargs["beat"]
            beat()
            beat(RuntimeError("x"))
        return LEADER, calls

    def test_the_jobs_loop_beats_as_jobs_on_the_leader_only(self):
        _, calls = self.bound_jobs_loop()
        self.assertEqual((("jobs", OPS.JOBS_BEAT_EVERY, None), {"leader_only": True}), calls[0])
        self.assertEqual("x", str(calls[1][0][2]))
        self.assertEqual({"leader_only": True}, calls[1][1])

    def state(self, age):
        import homestead_self_health as SELF_HEALTH
        now = 100000.0
        row = {"every": OPS.JOBS_BEAT_EVERY, "leader_only": True, "last_ok": now - age, "error": "", "error_at": 0}
        return next(s for s in SELF_HEALTH.loop_states({"jobs": row}, {}, True, now) if s["name"] == "jobs")

    def test_settings_names_the_jobs_loop(self):
        self.assertEqual(("ok", "Jobs"), (self.state(0)["state"], self.state(0)["label"]))

    def test_one_slow_host_step_is_not_called_late(self):
        self.assertEqual("ok", self.state(150)["state"], "a 90 s host step plus a round is normal")

    def test_a_hung_step_shows_as_late_within_a_few_minutes(self):
        self.assertEqual("late", self.state(181)["state"])

    def test_becoming_leader_wakes_the_loop(self):
        LEADER, _ = self.bound_jobs_loop()
        self.addCleanup(LEADER._state.update, dict(LEADER._state))
        LEADER._state.update(leader=False, holder="")
        OPS._wake.clear()
        LEADER._set(True, LEADER.IDENTITY)
        self.assertTrue(OPS._wake.is_set(), "a new leader would sit out its idle wait first")

    def test_rounds_run_at_most_once_a_second_however_often_asked(self):
        starts, stop, hammer = [], threading.Event(), threading.Event()

        def tick(is_leader, beat=None):
            starts.append(time.monotonic())
            return 3600

        def ask():
            while not hammer.is_set():
                OPS.advance_soon()
                time.sleep(0.01)
        with mock.patch.object(OPS, "tick", tick):
            loop = threading.Thread(target=OPS.run_jobs, args=(lambda: True, stop), daemon=True)
            asker = threading.Thread(target=ask, daemon=True)
            loop.start()
            asker.start()
            deadline = time.monotonic() + 10
            while len(starts) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            stop.set()
            hammer.set()
            OPS.advance_soon()
            loop.join(3)
            asker.join(1)
        self.assertGreaterEqual(len(starts), 3)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(gap >= 0.9 for gap in gaps), gaps)


class JobLockTidyTests(unittest.TestCase):
    """Lock files of jobs that are gone are removed once they are a day old
    (wrap-up row 25), never one in use or one whose job is still kept."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.tmp))
        self.saved = dict(OPS.RESOLVERS)
        self.addCleanup(lambda: (OPS.RESOLVERS.clear(), OPS.RESOLVERS.update(self.saved)))
        OPS.RESOLVERS["loop-test"] = lambda item: ("running", 50, "Halfway")
        self.kept = OPS.start("loop-test", "A job", {"kind": "Thing", "name": "t", "namespace": "lab"}, "/", {})["id"]
        self.locks = os.path.join(self.tmp, ".locks")
        os.makedirs(self.locks, exist_ok=True)

    def lock(self, operation_id, age):
        path = os.path.join(self.locks, f"operations-job-{OPS._job_lock_name(operation_id)}.lock")
        Path(path).touch()
        moment = time.time() - age
        os.utime(path, (moment, moment))
        return path

    def test_an_old_lock_of_a_job_gone_from_the_store_is_removed(self):
        gone = self.lock("ab" * 12, 2 * 86400)
        self.assertEqual(1, OPS.tidy_job_locks())
        self.assertFalse(os.path.exists(gone))

    def test_a_lock_under_a_day_old_is_kept(self):
        fresh = self.lock("cd" * 12, 3600)
        self.assertEqual(0, OPS.tidy_job_locks())
        self.assertTrue(os.path.exists(fresh))

    def test_the_lock_of_a_job_still_kept_stays_however_old(self):
        mine = self.lock(self.kept, 30 * 86400)
        crc_named = self.lock("not a hex id", 30 * 86400)
        OPS.tidy_job_locks()
        self.assertTrue(os.path.exists(mine))
        self.assertFalse(os.path.exists(crc_named), "its job is not in the store")

    def test_the_store_lock_and_other_files_are_left_alone(self):
        other = [os.path.join(self.locks, name) for name in ("operations.lock", "something-else.lock")]
        for path in other:
            Path(path).touch()
            os.utime(path, (0, 0))
        OPS.tidy_job_locks()
        self.assertTrue(all(os.path.exists(path) for path in other))

    def test_a_lock_someone_holds_is_kept(self):
        held = self.lock("ef" * 12, 2 * 86400)
        result = []
        with OPS._job_lock("ef" * 12):
            tidy = threading.Thread(target=lambda: result.append(OPS.tidy_job_locks()))
            tidy.start()
            tidy.join(5)
        self.assertEqual([0], result)
        self.assertTrue(os.path.exists(held))

    def test_nothing_is_removed_while_the_volume_is_held(self):
        gone = self.lock("ab" * 12, 2 * 86400)
        with mock.patch.object(OPS, "WRITE_GUARD", mock.Mock(side_effect=OPS.STORAGE_JOURNAL.Held("moving"))):
            self.assertIsNone(OPS.tidy_job_locks(), "not tidied: try again later")
        self.assertTrue(os.path.exists(gone))

    def test_a_volume_held_part_way_stops_the_tidy_quietly(self):
        gone = self.lock("ab" * 12, 2 * 86400)
        with mock.patch.object(OPS.SHARED, "WRITE_SCOPE", mock.Mock(side_effect=OPS.STORAGE_JOURNAL.Held("moving"))):
            self.assertIsNone(OPS.tidy_job_locks())
        self.assertTrue(os.path.exists(gone))

    def test_the_leaders_rounds_tidy_at_most_once_a_day(self):
        self.enterContext(mock.patch.dict(OPS._tidied, {"at": 0}))
        with mock.patch.object(OPS, "tidy_job_locks", return_value=0) as tidy:
            OPS.tick(lambda: True)
            OPS.tick(lambda: True)
            OPS.tick(lambda: False)
        self.assertEqual(1, tidy.call_count)

    def test_the_tidy_comes_after_the_rounds_jobs(self):
        self.enterContext(mock.patch.dict(OPS._tidied, {"at": 0}))
        order = []
        with mock.patch.object(OPS, "tidy_job_locks", lambda: order.append("tidy") or 0), \
                mock.patch.object(OPS, "list_operations", lambda wait=True: order.append("jobs") or []):
            OPS.tick(lambda: True)
        self.assertEqual(["jobs", "tidy"], order, "a new leader's first round moves jobs on first")

    def test_a_tidy_that_could_not_run_is_tried_again_within_the_hour(self):
        for outcome in (None, RuntimeError("busy")):
            with self.subTest(outcome=outcome):
                self.enterContext(mock.patch.dict(OPS._tidied, {"at": 0}))
                tidy = mock.Mock(side_effect=[outcome] if isinstance(outcome, Exception) else None,
                                 return_value=outcome)
                with mock.patch.object(OPS, "tidy_job_locks", tidy), mock.patch("builtins.print"):
                    OPS.tick(lambda: True)
                self.assertLessEqual(OPS._tidied["at"] + OPS.LOCK_TIDY_EVERY,
                                     time.time() + OPS.LOCK_TIDY_RETRY + 1, "not left a whole day")


class NoRequestAdvancesJobsTests(unittest.TestCase):
    """Server code reads the saved jobs (snapshot()); only the loop calls
    list_operations(), the function that runs the resolvers."""

    def test_a_reference_to_list_operations_is_seen_not_only_a_call(self):
        for source in ("OPS.list_operations()\n", "fn = OPS.list_operations\n",
                       "threading.Thread(target=OPS.list_operations).start()\n",
                       "from homestead_operations import list_operations\nlist_operations()\n",
                       "from homestead_operations import list_operations as step\n",
                       "run = list_operations\n", "step = getattr(OPS, \"list_operations\")\n"):
            with self.subTest(source=source):
                self.assertTrue(list_operations_references(SS.code(source)), source)
        self.assertEqual([], list_operations_references(SS.code(
            "def list_operations():\n    pass\n# OPS.list_operations()\ntext = \"list_operations\"\n")))

    def test_only_the_jobs_loop_calls_list_operations(self):
        callers = [f"{path.name}:{line}" for path in sorted(SS.SERVER.glob("*.py"))
                   if path.name != "homestead_operations.py"
                   for line in list_operations_references(path)]
        self.assertEqual([], callers)


def list_operations_references(source):
    """The lines that name list_operations in source: a call, a reference
    handed on (target=OPS.list_operations), an import of it, or getattr with
    its name. Its own def, a comment or another string is not one."""
    found = []
    for node in ast.walk(SS.tree(source)):
        named = ((isinstance(node, ast.Attribute) and node.attr == "list_operations")
                 or (isinstance(node, ast.Name) and node.id == "list_operations")
                 or (isinstance(node, ast.alias) and node.name == "list_operations")
                 or (isinstance(node, ast.Call) and SS.dotted(node.func) == "getattr"
                     and any(isinstance(arg, ast.Constant) and arg.value == "list_operations"
                             for arg in node.args)))
        if named:
            found.append(node.lineno)
    return sorted(found)


if __name__ == "__main__":
    unittest.main()
