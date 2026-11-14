"""A job that holds apps, or is undoing itself, and stays at one step for 30 minutes
raises one alert naming the job and its Cancel (#1311, part 1)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as A
import homestead_operations as OPS
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

T0 = 1_000_000.0
STALL = 30 * 60


def item(ref, status="running", kind="node-power", **extra):
    return {"id": "op-1", "kind": kind, "title": "Restart host node1", "status": status, "ref": ref, **extra}


class StoredJobTests(unittest.TestCase):
    def step(self, ref, status="running", kind="node-power"):
        return A._stalled_step(item(ref, status, kind)) or None

    def test_a_job_holding_apps_that_are_not_started_again_is_at_a_step(self):
        self.assertEqual("draining", self.step({"held": ["Deployment/lab/web"], "restored": None, "phase": "draining"}))

    def test_a_job_that_started_them_again_is_not(self):
        self.assertIsNone(self.step({"held": ["Deployment/lab/web"], "restored": {"started": ["web"]}, "phase": "restoring"}))

    def test_a_job_that_holds_nothing_is_not(self):
        self.assertIsNone(self.step({"phase": "draining"}))

    def test_a_job_undoing_itself_is(self):
        self.assertEqual("rolling-back", self.step({"phase": "rolling-back"}, kind="reclass"))

    def test_a_power_job_that_has_sent_power_is_not(self):
        """A deliberate power-off keeps its apps held while the host is off."""
        held = {"held": ["Deployment/lab/web"], "restored": None}
        self.assertIsNone(self.step({**held, "phase": "observing"}))
        self.assertIsNone(self.step({**held, "phase": "sending"}))

    def test_a_power_job_still_preparing_is(self):
        self.assertEqual("draining", self.step({"held": ["Deployment/lab/web"], "restored": None, "phase": "draining"}))

    def test_a_job_that_ended_is_not(self):
        self.assertIsNone(self.step({"held": ["Deployment/lab/web"], "restored": None, "phase": "draining"}, "failed"))


class StalledJobAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        keep_bound(self, A)
        A.bind(self.tmp.name)
        self.stored = []
        self.enterContext(mock.patch.object(OPS, "stored", lambda: self.stored))

    def look(self, now, step="draining", cancellable=True, status="running"):
        """The page's view of the job (no steps in it), and the stored job at `step` ("" for none held)."""
        op = {"id": "op-1", "title": "Restart host node1", "status": status, "cancellable": cancellable}
        ref = {"held": ["Deployment/lab/web"], "restored": None, "phase": step} if step else {}
        self.stored = [item(ref, status, kind="storage-class-move")]
        return A.observe({"jobs": A.job_facts([op])}, now)

    def test_nothing_is_raised_before_thirty_minutes(self):
        self.assertEqual([], self.look(T0))
        self.assertEqual([], self.look(T0 + STALL - 60))

    def test_one_degraded_alert_names_the_job_and_its_cancel(self):
        self.look(T0)
        raised = self.look(T0 + STALL)
        self.assertEqual(1, len(raised))
        entry = raised[0]
        self.assertEqual(("raised", "degraded"), (entry["phase"], entry["severity"]))
        self.assertIn("Restart host node1", entry["title"])
        self.assertIn("Choose Cancel on the job", entry["body"])
        self.assertIn('the step "Moving apps and VMs off the host"', entry["body"])
        self.assertNotIn("draining", entry["body"])
        self.assertLess(len(entry["body"].split(". ")[0]), 90)
        self.assertEqual([], self.look(T0 + STALL + 600), "said once, not again")

    def test_a_job_that_cannot_be_cancelled_says_so(self):
        self.look(T0, cancellable=False)
        entry = self.look(T0 + STALL, cancellable=False)[0]
        self.assertIn("Cancel is not offered", entry["body"])
        self.assertNotIn("Choose Cancel", entry["body"])

    def test_a_step_that_changes_starts_the_thirty_minutes_again(self):
        self.look(T0, step="draining")
        self.look(T0 + STALL - 60, step="draining")
        self.assertEqual([], self.look(T0 + STALL - 30, step="verifying"))
        self.assertEqual([], self.look(T0 + STALL + 600, step="verifying"), "30 minutes from the change, not from the start")

    def test_it_resolves_when_the_job_moves_on(self):
        self.look(T0)
        self.look(T0 + STALL)
        self.look(T0 + STALL + 10, step=None)
        resolved = self.look(T0 + STALL + 10 + A.HOLD + 1, step=None)
        self.assertEqual(["resolved"], [e["phase"] for e in resolved])

    def test_a_job_retrying_every_minute_still_alerts_after_thirty_minutes_at_the_step(self):
        """Its updated_at moves each minute while the step stays rolling-back: the time is
        counted from when the step first showed, not from the last update."""
        raised = []
        for minute in range(0, 40):
            self.stored = [item({"phase": "rolling-back"}, kind="reclass")]
            op = {"id": "op-1", "title": "Move volume data", "status": "running", "cancellable": True,
                  "updated_at": f"2026-11-10T10:{minute:02d}:00Z"}
            raised += [(minute, e["phase"]) for e in A.observe({"jobs": A.job_facts([op])}, T0 + minute * 60)]
        self.assertEqual([(30, "raised")], raised, "once, at 30 minutes")

    def test_a_job_that_failed_is_the_failed_alert_only(self):
        self.stored = [item({"held": ["Deployment/lab/web"], "restored": None, "phase": "draining"}, "failed")]
        facts = A.job_facts([{"id": "op-1", "title": "Restart host node1", "status": "failed", "message": "no"}])
        self.assertEqual(["jobs:op-1"], [f["key"] for f in facts])

    def test_an_unreadable_store_neither_fails_nor_resolves_a_raised_alert(self):
        self.look(T0)
        self.look(T0 + STALL)
        with mock.patch.object(OPS, "stored", side_effect=OSError("disk")):
            for minute in range(1, 20):
                self.assertEqual([], A.observe({"jobs": A.job_facts([{"id": "op-1", "title": "T", "status": "running"}])}, T0 + STALL + minute * 120))
        self.assertEqual([], self.look(T0 + STALL + 3000), "still the same alert: not resolved and raised again")


if __name__ == "__main__":
    unittest.main()
