"""Host OS, bond, bridge and copy jobs are resolved outside the job store's lock
(#622 step 5): a slow resolver holds up no start, cancel or look at the jobs;
its result is saved only if nobody changed the job meanwhile; and a canceller
never runs while that job's resolver does."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_operations as OPS

KIND = "host-os"   # one of the kinds resolved outside the lock


class OutsideTheLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.tmp))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS))
        self.enterContext(mock.patch.dict(OPS.CANCELLERS))
        self.release = threading.Event()
        self.entered = threading.Event()
        self.resolving = threading.Event()
        self.overlap = []

        def slow(item):
            self.resolving.set()
            self.entered.set()
            self.release.wait(5)
            self.resolving.clear()
            return "running", 40, "Resolver says halfway"

        def cancel_run(item, options):
            self.overlap.append(self.resolving.is_set())
            return "Stopped"
        OPS.RESOLVERS[KIND] = slow
        OPS.CANCELLERS[KIND] = (lambda item: {"mode": "stop", "needs": "operator"}, cancel_run)
        self.op = self.start(KIND, "node-a")

    def start(self, kind, name):
        return OPS.start(kind, "A job", {"kind": "Node", "name": name, "namespace": ""}, "/", {})["id"]

    def stored(self, op):
        return next(item for item in OPS.stored() if item["id"] == op)

    def round_in_background(self):
        thread = threading.Thread(target=OPS.list_operations, daemon=True)
        thread.start()
        self.assertTrue(self.entered.wait(3), "the resolver did not start")
        return thread

    def test_a_slow_resolver_does_not_hold_up_a_start(self):
        thread = self.round_in_background()
        began = time.monotonic()
        self.start("other-kind", "node-b")
        self.assertLess(time.monotonic() - began, 1.0, "start waited for the resolver")
        self.release.set(); thread.join(5)

    def test_the_resolvers_result_is_saved_when_nobody_changed_the_job(self):
        thread = self.round_in_background()
        self.release.set(); thread.join(5)
        self.assertEqual("Resolver says halfway", self.stored(self.op)["message"])

    def test_a_cancel_made_mid_resolve_is_recorded_at_once_and_applied_after_the_step(self):
        seen = []

        def creates_upstream(item):
            self.resolving.set(); self.entered.set()
            item["ref"]["job_uid"] = "upstream-1"   # what it created in Kubernetes
            OPS.checkpoint(item)
            self.release.wait(5)
            self.resolving.clear()
            return "running", 40, "Created the upstream job"
        OPS.RESOLVERS[KIND] = creates_upstream
        OPS.CANCELLERS[KIND] = (lambda item: {"mode": "stop", "needs": "operator"},
                                lambda item, options: seen.append(item["ref"].get("job_uid")) or "Removed")
        thread = self.round_in_background()
        began = time.monotonic()
        answer = OPS.cancel(self.op)
        self.assertLess(time.monotonic() - began, 1.0, "the cancel waited for the step")
        self.assertTrue(answer.get("accepted"))
        self.assertEqual([], seen, "the canceller ran inside the request")
        self.release.set(); thread.join(5)
        OPS.list_operations()   # the loop's next tick
        self.assertEqual(["upstream-1"], seen, "the canceller did not see what the step created")
        self.assertEqual("cancelled", self.stored(self.op)["status"])

    def test_a_cancel_wakes_the_loop(self):
        OPS._wake.clear()
        OPS.cancel(self.op)
        self.assertTrue(OPS._wake.is_set())

    def test_a_cancel_during_a_long_step_is_accepted_with_202_not_a_500(self):
        import types
        holding, done = threading.Event(), threading.Event()

        def hold():   # a host step running past 60 s holds the job's lock
            with OPS._job_lock(self.op):
                holding.set(); done.wait(10)
        holder = threading.Thread(target=hold, daemon=True); holder.start()
        self.assertTrue(holding.wait(2))
        try:
            began = time.monotonic()
            request = types.SimpleNamespace(body={"id": self.op}, role="admin", user="alice")
            reply = OPS.ROUTES[("POST", "/api/operations/cancel")][1](request)
            self.assertLess(time.monotonic() - began, 1.0)
        finally:
            done.set(); holder.join(5)
        self.assertEqual(202, reply.code)
        self.assertTrue(reply.body["accepted"])
        self.assertIn("Cancel requested", reply.body["operation"]["message"])

    def test_a_second_cancel_while_one_is_waiting_is_refused(self):
        OPS.cancel(self.op)
        with self.assertRaises(ValueError):
            OPS.cancel(self.op)

    def test_a_waiting_cancel_is_not_applied_when_the_job_moved_to_a_step_that_cannot_be_cancelled(self):
        OPS.cancel(self.op)
        ran = []
        OPS.CANCELLERS[KIND] = (lambda item: {"mode": "stop", "needs": "operator", "can": False,
                                              "why_not": "a swap is in progress"},
                                lambda item, options: ran.append(1) or "Stopped")
        OPS.list_operations()
        saved = self.stored(self.op)
        self.assertEqual([], ran)
        self.assertNotEqual("cancelled", saved["status"])
        self.assertIn("a swap is in progress", saved["message"])

    def test_a_slow_resolver_does_not_hold_up_cancelling_another_job(self):
        other = self.start(KIND, "node-z")
        quick = self.start("other-kind", "node-y")
        OPS.CANCELLERS["other-kind"] = (lambda item: {"mode": "stop", "needs": "operator"}, lambda item, options: "Stopped")
        # Its step runs at once now, beside the slow one (#933): it must not end the job.
        OPS.RESOLVERS["other-kind"] = lambda item: ("running", 1, "y")
        blocked = threading.Event()
        OPS.RESOLVERS[KIND] = lambda item: (blocked.wait(5) if item["id"] == self.op else None, self.entered.set(),
                                            ("running", 1, "x"))[2]
        thread = threading.Thread(target=OPS.list_operations, daemon=True)
        thread.start()
        time.sleep(0.3)
        for job in (other, quick):
            began = time.monotonic()
            OPS.cancel(job)
            self.assertLess(time.monotonic() - began, 1.0, f"cancelling {job} waited for another job's resolver")
        blocked.set(); thread.join(5)

    def test_a_job_whose_lock_is_held_is_skipped_at_once_not_the_whole_round(self):
        other = self.start(KIND, "node-w")
        quick = self.start("other-kind", "node-v")
        OPS.RESOLVERS[KIND] = OPS.RESOLVERS["other-kind"] = lambda item: ("running", 70, "Moved on")
        holding, done = threading.Event(), threading.Event()

        def hold():   # another replica's (or an old leader's) step on this job
            with OPS._job_lock(self.op):
                holding.set(); done.wait(10)
        holder = threading.Thread(target=hold, daemon=True); holder.start()
        self.assertTrue(holding.wait(2))
        try:
            began = time.monotonic()
            OPS.list_operations()
            self.assertLess(time.monotonic() - began, 1.0, "the round waited for a busy job's lock")
        finally:
            done.set(); holder.join(5)
        self.assertNotEqual("Moved on", self.stored(self.op)["message"])
        self.assertEqual("Moved on", self.stored(other)["message"])
        self.assertEqual("Moved on", self.stored(quick)["message"])

    def test_a_canceller_never_runs_while_its_jobs_resolver_does(self):
        thread = self.round_in_background()
        OPS.cancel(self.op)
        self.release.set(); thread.join(5)
        OPS.list_operations()
        self.assertEqual([False], self.overlap)

    def test_a_change_made_mid_resolve_drops_the_resolvers_result(self):
        thread = self.round_in_background()
        self.edit(self.op)
        self.release.set(); thread.join(5)
        saved = self.stored(self.op)
        self.assertTrue(saved["ref"].get("edited"), "the concurrent change was overwritten")
        self.assertNotEqual("Resolver says halfway", saved["message"])
        OPS.list_operations()   # resolved again next tick
        self.assertEqual("Resolver says halfway", self.stored(self.op)["message"])

    def test_a_checkpoint_after_a_concurrent_change_is_refused_before_the_upstream_write(self):
        posted = []

        def checkpoint_then_post(item):
            self.entered.set(); self.release.wait(5)
            item["ref"]["step"] = "posted"
            OPS.checkpoint(item)
            posted.append(1)   # the upstream write the checkpoint guards
            return "running", 40, "Resolver says halfway"
        OPS.RESOLVERS[KIND] = checkpoint_then_post
        thread = self.round_in_background()
        self.edit(self.op)
        self.release.set(); thread.join(5)
        saved = self.stored(self.op)
        self.assertEqual([], posted, "the step went on to its upstream write")
        self.assertTrue(saved["ref"].get("edited"), "the checkpoint overwrote the concurrent change")
        self.assertNotIn("step", saved["ref"])
        self.assertNotEqual("Resolver says halfway", saved["message"])

    def edit(self, op):
        with OPS.transaction() as tx:
            items = tx.items
            next(i for i in items if i["id"] == op)["ref"]["edited"] = True
            tx.save(items)

    def test_a_resolver_that_fails_keeps_the_job_running_with_the_reason(self):
        OPS.RESOLVERS[KIND] = lambda item: (_ for _ in ()).throw(RuntimeError("node unreachable"))
        OPS.list_operations()
        saved = self.stored(self.op)
        self.assertEqual("running", saved["status"])
        self.assertIn("Status temporarily unavailable", saved["message"])

    def test_a_resolvers_own_checkpoint_does_not_drop_its_result(self):
        def with_checkpoint(item):
            item["ref"]["step"] = "posted"
            OPS.checkpoint(item)
            return "running", 60, "Checkpointed"
        OPS.RESOLVERS[KIND] = with_checkpoint
        OPS.list_operations()
        saved = self.stored(self.op)
        self.assertEqual(("Checkpointed", "posted"), (saved["message"], saved["ref"]["step"]))

    def test_other_kinds_are_still_resolved_under_the_lock(self):
        held = []
        OPS.RESOLVERS["other-kind"] = lambda item: (held.append(OPS._lock._depth > 0), ("running", 1, "x"))[1]
        self.start("other-kind", "node-c")
        self.release.set()
        OPS.list_operations()
        self.assertEqual([True], held)


if __name__ == "__main__":
    unittest.main()
