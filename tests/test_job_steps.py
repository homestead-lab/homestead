"""The jobs loop runs the steps of jobs resolved outside the store's lock side
by side, a few at a time (#933). One slow step - a host step, a copy, a trim -
no longer holds up every other job; a job never runs two steps at once; jobs
on the same host or object still take turns; and a stop waits for the steps
in flight."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_job_steps as STEPS
import homestead_operations as OPS


class Gate:
    """A resolver that blocks until released, and remembers who ran when."""

    def __init__(self):
        self.lock = threading.Lock()
        self.released = threading.Event()
        self.held = set()        # job ids that block until released
        self.calls = []          # job ids, in the order their steps began
        self.running = set()
        self.most = 0            # the most steps ever running at once
        self.overlaps = []       # (a, b) pairs that ran at the same time

    def __call__(self, item):
        with self.lock:
            self.calls.append(item["id"])
            for other in self.running:
                self.overlaps.append((other, item["id"]))
            self.running.add(item["id"])
            self.most = max(self.most, len(self.running))
        try:
            if item["id"] in self.held:
                self.released.wait(10)
            else:
                time.sleep(0.05)
        finally:
            with self.lock:
                self.running.discard(item["id"])
        return "running", 50, f"Step {len([c for c in self.calls if c == item['id']])}"

    def count(self, op):
        with self.lock:
            return self.calls.count(op)


def until(test, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return True
        time.sleep(0.01)
    return False


class JobStepsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.tmp))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS))
        self.enterContext(mock.patch.dict(OPS._tidied, {"at": time.time()}))  # tidied already today
        self.addCleanup(OPS._wake.clear)
        self.gate = Gate()
        for kind in ("host-os", "host-bond", "host-bridge", "workload-copy", "weekly-trim"):
            OPS.RESOLVERS[kind] = self.gate
        # Never leave a step, an error or a turn of this test to the next one.
        self.addCleanup(lambda: (STEPS._errors.clear(), STEPS._last.clear()))
        self.addCleanup(lambda: self.assertTrue(STEPS.wait(10), "a step of this test never ended"))
        self.addCleanup(self.gate.released.set)
        self.beats = []

    def beat(self, error=None):
        self.beats.append(error)

    def host_job(self, node, kind="host-os"):
        return OPS.start(kind, f"Update {node}", {"kind": "Node", "name": node}, "/nodes", {"node": node})["id"]

    def copy_job(self, name, claims):
        """A data move, saved as its start leaves it (its start needs a reviewed approval)."""
        n = len(OPS.stored()) + 1
        ref = {"namespace": "lab", "name": name, "moves": [{"from": c, "to": c + "-new"} for c in claims],
               "review_digest": f"{n:064x}"}
        item = {"id": f"{n:024x}", "kind": "workload-copy", "title": f"Move {name}'s data",
                "resource": {"kind": "Deployment", "namespace": "lab", "name": name}, "href": "/", "ref": ref,
                "status": "running", "progress": 10, "message": "Copying", "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "", "finished_at": "", "history": []}
        with OPS.transaction() as jobs:
            jobs.save(jobs.items + [item])
        return item["id"]

    def trim_job(self):
        return OPS.start("weekly-trim", "Weekly trim", {"kind": "Longhorn", "name": "weekly trim"}, "/", {})["id"]

    def message(self, op):
        return next(item for item in OPS.stored() if item["id"] == op)["message"]

    def tick(self):
        began = time.monotonic()
        OPS.tick(lambda: True, self.beat)
        return time.monotonic() - began

    def test_a_slow_step_in_one_job_does_not_delay_another_jobs_step(self):
        slow, quick = self.host_job("node-1"), self.host_job("node-2")
        self.gate.held.add(slow)
        self.assertLess(self.tick(), 1.0, "the round waited for the slow step")
        self.assertTrue(until(lambda: self.message(quick) == "Step 1"), "the other job's step waited")
        # Rounds go on while the slow step runs: the other job keeps moving.
        self.assertTrue(until(lambda: self.tick() < 1.0 and self.gate.count(quick) >= 3),
                        "later rounds waited for the slow step")
        self.assertEqual(1, self.gate.count(slow), "a job ran a second step while its first was in flight")
        self.gate.released.set()
        self.assertTrue(until(lambda: self.message(slow) == "Step 1"), "the slow step's result was not saved")

    def test_two_jobs_on_the_same_host_still_take_turns(self):
        first, second = self.host_job("node-1"), self.host_job("node-1", kind="host-bond")
        self.gate.held.add(first)
        self.tick(); self.tick()
        self.assertTrue(until(lambda: self.gate.count(first) == 1))
        time.sleep(0.2)
        self.assertEqual(0, self.gate.count(second), "a second job on the same host stepped beside the first")
        self.gate.released.set()
        self.assertTrue(until(lambda: (self.tick(), self.gate.count(second))[1] >= 1))
        self.assertEqual([], self.gate.overlaps)

    def test_two_jobs_on_the_same_volume_still_take_turns(self):
        first, second = self.copy_job("app-a", ["data"]), self.copy_job("app-b", ["data"])
        self.gate.held.add(first)
        self.tick(); self.tick()
        self.assertTrue(until(lambda: self.gate.count(first) == 1))
        time.sleep(0.2)
        self.assertEqual(0, self.gate.count(second), "two jobs stepped on one volume at once")
        self.gate.released.set()
        self.assertTrue(until(lambda: (self.tick(), self.gate.count(second))[1] >= 1))
        self.assertEqual([], self.gate.overlaps)

    def test_the_weekly_trim_never_steps_beside_a_data_move(self):
        trim, move = self.trim_job(), self.copy_job("app-a", ["data"])
        self.gate.held.add(trim)
        self.tick(); self.tick()
        time.sleep(0.2)
        self.assertEqual(0, self.gate.count(move), "a data move stepped while the trim touched every volume")
        self.gate.released.set()
        self.assertTrue(until(lambda: (self.tick(), self.gate.count(move))[1] >= 1))
        self.assertEqual([], self.gate.overlaps)

    def test_a_job_waiting_on_a_clash_is_not_overtaken_by_a_later_one(self):
        """The trim claims every volume: two data moves taking turns must not
        keep it out for ever."""
        first = self.copy_job("app-a", ["data-a"])
        self.gate.held.add(first)
        self.tick()
        self.assertTrue(until(lambda: self.gate.count(first) == 1))
        trim, second = self.trim_job(), self.copy_job("app-b", ["data-b"])
        self.tick()
        time.sleep(0.2)
        self.assertEqual(0, self.gate.count(second), "a later move overtook the trim waiting for the first")
        self.gate.released.set()
        self.assertTrue(STEPS.wait(60), "a step never finished")
        self.tick()
        self.assertTrue(until(lambda: self.gate.count(trim) == 1), "the trim never stepped")

    def test_jobs_on_different_hosts_and_objects_step_side_by_side(self):
        jobs = [self.host_job("node-1"), self.copy_job("app-a", ["data"])]
        self.gate.held.update(jobs)
        self.tick()
        self.assertTrue(until(lambda: self.gate.most == 2), "independent jobs did not step side by side")

    def test_no_more_steps_run_at_once_than_the_pool_allows(self):
        jobs = [self.host_job(f"node-{n}") for n in range(STEPS.WORKERS + 3)]
        self.gate.held.update(jobs)
        for _ in range(3):
            self.tick()
        self.assertTrue(until(lambda: self.gate.most == STEPS.WORKERS))
        time.sleep(0.2)
        self.assertEqual(STEPS.WORKERS, self.gate.most)
        self.gate.released.set()
        self.assertTrue(STEPS.wait(60), "a step never finished")
        self.assertLessEqual(self.gate.most, STEPS.WORKERS)

    def test_a_full_pool_takes_the_jobs_that_waited_longest_first(self):
        self.enterContext(mock.patch.object(STEPS, "WORKERS", 1))
        jobs = [self.host_job(f"node-{n}") for n in range(3)]
        for _ in range(6):
            self.tick()
            self.assertTrue(STEPS.wait(60), "a step never finished")
        self.assertEqual([2, 2, 2], [self.gate.count(op) for op in jobs], self.gate.calls)

    def test_a_stop_waits_for_the_steps_in_flight(self):
        op = self.host_job("node-1")
        self.gate.held.add(op)
        stop = threading.Event()
        loop = threading.Thread(target=OPS.run_jobs, args=(lambda: True, stop), daemon=True)
        loop.start()
        self.assertTrue(until(lambda: self.gate.count(op) == 1))
        stop.set(); OPS.advance_soon()
        loop.join(1.5)
        self.assertTrue(loop.is_alive(), "the loop ended with a step still in flight")
        self.gate.released.set()
        loop.join(5)
        self.assertFalse(loop.is_alive())
        self.assertEqual("Step 1", self.message(op), "the step's result was lost at the stop")

    def test_a_stop_is_not_held_for_ever_by_a_hung_step(self):
        self.enterContext(mock.patch.object(STEPS, "OVERDUE_SECONDS", 0.3))
        op = self.host_job("node-1")
        self.gate.held.add(op)
        stop = threading.Event()
        loop = threading.Thread(target=OPS.run_jobs, args=(lambda: True, stop), daemon=True)
        loop.start()
        self.assertTrue(until(lambda: self.gate.count(op) == 1))
        stop.set(); OPS.advance_soon()
        loop.join(3)
        self.assertFalse(loop.is_alive(), "a hung step held the stop for ever")

    def test_a_step_that_cannot_start_leaves_its_job_free(self):
        op = self.host_job("node-1")
        with mock.patch.object(STEPS.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                STEPS.submit(op, {("host", "node-1")}, lambda: None)
        self.assertNotIn(op, STEPS.in_flight())

    def test_a_direct_round_gives_a_job_in_flight_its_own_step(self):
        op = self.host_job("node-1")
        self.gate.held.add(op)
        self.tick()
        self.assertTrue(until(lambda: self.gate.count(op) == 1))
        threading.Timer(0.3, self.gate.released.set).start()
        OPS.list_operations()
        self.assertEqual(2, self.gate.count(op), "the direct round counted the loop's step as its own")

    def test_a_direct_round_raises_its_own_steps_error_not_the_loops(self):
        loops, mine = self.host_job("node-1"), self.host_job("node-2")
        boom = {loops: RuntimeError("loop's step"), mine: RuntimeError("my step")}
        real = OPS._resolve_outside

        def resolve(job_id):
            if job_id in boom:
                raise boom.pop(job_id)
            return real(job_id)
        with mock.patch.object(OPS, "_resolve_outside", resolve), mock.patch("builtins.print"):
            STEPS.submit(loops, {("host", "node-1")}, lambda: resolve(loops))
            self.assertTrue(STEPS.wait(60), "a step never finished")
            with self.assertRaisesRegex(RuntimeError, "my step"):
                OPS.list_operations()
            OPS.tick(lambda: True, self.beat)
        self.assertEqual("loop's step", str(self.beats[-1]), "the loop's error was lost")

    def test_a_standby_replica_starts_no_step_and_a_step_in_flight_still_saves(self):
        op = self.host_job("node-1")
        self.gate.held.add(op)
        self.tick()
        self.assertTrue(until(lambda: self.gate.count(op) == 1))
        OPS.tick(lambda: False, self.beat)          # leadership lost meanwhile
        other = self.host_job("node-2")
        OPS.tick(lambda: False, self.beat)
        self.assertEqual(0, self.gate.count(other), "a standby replica started a step")
        # A new leader (another replica) finds the job's lock held and leaves it.
        self.assertFalse(OPS._resolve_outside(op))
        self.gate.released.set()
        self.assertTrue(until(lambda: self.message(op) == "Step 1"), "the step in flight lost its result")
        self.assertEqual(1, self.gate.count(op))

    def test_a_hung_step_frees_its_worker_and_shows_in_health(self):
        self.enterContext(mock.patch.object(STEPS, "WORKERS", 1))
        self.enterContext(mock.patch.object(STEPS, "OVERDUE_SECONDS", 0.3))
        hung, other = self.host_job("node-1"), self.host_job("node-2")
        self.gate.held.add(hung)
        self.tick()
        self.assertTrue(until(lambda: self.gate.count(hung) == 1))
        self.tick()
        self.assertEqual(0, self.gate.count(other), "the pool's one worker was shared")
        time.sleep(0.4)
        self.beats.clear()
        self.tick()
        self.assertTrue(until(lambda: self.gate.count(other) >= 1), "a hung step held its worker for ever")
        self.assertEqual(1, self.gate.count(hung), "a hung job was given a second step")
        self.assertTrue(self.beats and self.beats[-1] is not None, "health did not hear of the hung step")
        self.assertIn("Update node-1", str(self.beats[-1]))

    def test_hung_steps_never_take_more_than_twice_the_pool(self):
        self.enterContext(mock.patch.object(STEPS, "WORKERS", 1))
        self.enterContext(mock.patch.object(STEPS, "OVERDUE_SECONDS", 0.1))
        jobs = [self.host_job(f"node-{n}") for n in range(4)]
        self.gate.held.update(jobs)
        for _ in range(6):
            self.tick()
            time.sleep(0.15)
        self.assertEqual(2, self.gate.most)

    def test_a_step_that_raises_beats_its_error_and_the_loop_keeps_its_pace(self):
        op = self.host_job("node-1")
        with mock.patch.object(OPS, "_resolve_outside", side_effect=RuntimeError("store unreadable")), \
                mock.patch("builtins.print"):
            self.tick()
            # On a loaded machine the failing step can take longer than a few
            # seconds to be scheduled: wait for it, and say so if it never ends.
            self.assertTrue(STEPS.wait(60), "the failing step never finished")
            wait = OPS.tick(lambda: True, self.beat)
        self.assertEqual("store unreadable", str(self.beats[-1]))
        self.assertEqual(OPS.ACTIVE_SECONDS, wait, "one job's failure slowed every job")
        self.assertTrue(op)

    def test_a_direct_round_still_moves_every_job_one_step(self):
        """list_operations() on its own (tests, tools) returns once every
        active job has taken its step, as before."""
        self.enterContext(mock.patch.object(STEPS, "WORKERS", 1))
        jobs = [self.host_job("node-1"), self.host_job("node-1", kind="host-bridge"), self.host_job("node-2")]
        listed = OPS.list_operations()
        self.assertEqual([1, 1, 1], [self.gate.count(op) for op in jobs])
        self.assertEqual({"Step 1"}, {row["message"] for row in listed if row["id"] in jobs})


class ClaimTests(unittest.TestCase):
    def test_claims_meet_on_the_same_host_or_object_only(self):
        self.assertTrue(STEPS.overlap({("host", "node-1")}, {("host", "node-1")}))
        self.assertFalse(STEPS.overlap({("host", "node-1")}, {("host", "node-2")}))
        self.assertTrue(STEPS.overlap({("object", "lab", "pvc/data")}, {("object", "*")}))
        self.assertFalse(STEPS.overlap({("object", "lab", "pvc/data")}, {("host", "node-1")}))
        self.assertTrue(STEPS.overlap({("all",)}, {("host", "node-1")}))

    def test_a_kind_with_no_claims_steps_alone(self):
        self.assertEqual({("all",)}, OPS.step_claims({"kind": "something-new", "ref": {}, "resource": {}}))

    def test_every_kind_resolved_outside_the_lock_names_its_claims(self):
        for kind in OPS.OUTSIDE_LOCK:
            claims = OPS.step_claims({"kind": kind, "ref": {"node": "node-1", "namespace": "lab", "name": "a",
                                                            "moves": [{"from": "d", "to": "e"}]},
                                      "resource": {"name": "node-1"}})
            self.assertNotIn(("all",), claims, kind)


if __name__ == "__main__":
    unittest.main()
