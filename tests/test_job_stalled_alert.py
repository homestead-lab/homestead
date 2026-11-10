"""A job that holds apps, or is undoing itself, and stays at one step for 30 minutes
raises one alert naming the job and its Cancel (#1311, part 1)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as A
import homestead_operations as OPS
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

T0 = 1_000_000.0
STALL = 30 * 60


def item(ref, status="running", **extra):
    return {"id": "op-1", "kind": "node-power", "title": "Restart host node1", "status": status, "ref": ref, **extra}


class PublicJobTests(unittest.TestCase):
    def step(self, ref, status="running"):
        return OPS._public(item(ref, status)).get("stalled_step")

    def test_a_job_holding_apps_that_are_not_started_again_is_at_a_step(self):
        self.assertEqual("draining", self.step({"held": ["Deployment/lab/web"], "restored": None, "phase": "draining"}))

    def test_a_job_that_started_them_again_is_not(self):
        self.assertIsNone(self.step({"held": ["Deployment/lab/web"], "restored": {"started": ["web"]}, "phase": "restoring"}))

    def test_a_job_that_holds_nothing_is_not(self):
        self.assertIsNone(self.step({"phase": "draining"}))

    def test_a_job_undoing_itself_is(self):
        self.assertEqual("rolling-back", self.step({"phase": "rolling-back"}))

    def test_a_job_that_ended_is_not(self):
        self.assertIsNone(self.step({"held": ["Deployment/lab/web"], "restored": None, "phase": "draining"}, "failed"))


class StalledJobAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        keep_bound(self, A)
        A.bind(self.tmp.name)

    def look(self, now, step="draining", cancellable=True, status="running"):
        op = {"id": "op-1", "title": "Restart host node1", "status": status, "cancellable": cancellable}
        if step:
            op["stalled_step"] = step
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

    def test_a_job_that_failed_is_the_failed_alert_only(self):
        facts = A.job_facts([{"id": "op-1", "title": "Restart host node1", "status": "failed", "message": "no", "stalled_step": "draining"}])
        self.assertEqual(["jobs:op-1"], [f["key"] for f in facts])


if __name__ == "__main__":
    unittest.main()
