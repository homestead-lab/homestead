"""A weekly trim that fails on an attached volume is a health issue on the
Dashboard, and clears when the next run trims it (#749). Skips never are."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as ALERTS
import homestead_overview_server as OV
import homestead_weekly_trim as WT
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

FAILED = {"volume": "pvc-demo-c", "pvc": "data-c", "message": "Longhorn refused the trim: engine is not running"}


def run(trimmed=(), skipped=(), failed=(), unconfirmed=()):
    return {"enabled": True, "last": {"slot": 1, "at": 2, "trimmed": list(trimmed), "skipped": list(skipped),
                                      "unconfirmed": list(unconfirmed), "failed": list(failed)}}


class TrimHealthTests(unittest.TestCase):
    def test_a_run_with_only_skips_or_unconfirmed_raises_nothing(self):
        self.assertEqual([], WT.issues(run(["pvc-a"], [{"volume": "pvc-b", "pvc": "b", "why": "detached"},
                                                       {"volume": "pvc-v", "pvc": "v", "why": WT.VM_DISK}],
                                           unconfirmed=[{"volume": "pvc-d", "pvc": "d", "why": "no answer"}])))
        self.assertEqual([], WT.issues({}), "never run")

    def test_a_failure_on_an_attached_volume_raises_one_issue(self):
        issues = OV.protection_issues([], {}, run(failed=[FAILED]))
        self.assertEqual([("Trim", "data-c", "degraded")], [(i["kind"], i["name"], i["severity"]) for i in issues])
        self.assertIn("engine is not running", issues[0]["reason"])
        self.assertIn("next Saturday", issues[0]["reason"])
        nameless = dict(FAILED, pvc="")
        self.assertEqual("pvc-demo-c", WT.issues(run(failed=[nameless]))[0]["name"], "no claim: the volume's name")

    def test_none_while_the_weekly_trim_is_off(self):
        self.assertEqual([], WT.issues({**run(failed=[FAILED]), "enabled": False}))

    def test_the_next_clean_run_clears_it(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        keep_bound(self, ALERTS)
        ALERTS.bind(temp.name)
        facts = lambda state: ALERTS.health_facts({"health_issues": OV.protection_issues([], {}, state)})
        with mock.patch.object(ALERTS.HISTORY, "maintenance_view", return_value=(set(), set(), {"open": {}, "ran_out": {}, "ending": {}})):
            ALERTS.observe({"health": facts(run(failed=[FAILED]))}, 1000)
            raised = ALERTS.observe({"health": facts(run(failed=[FAILED]))}, 1000 + ALERTS.HOLD)
            self.assertEqual([("raised", "Weekly trim failed: data-c")], [(e["phase"], e["title"]) for e in raised])
            ALERTS.observe({"health": facts(run(trimmed=["pvc-demo-c"]))}, 2000)
            cleared = ALERTS.observe({"health": facts(run(trimmed=["pvc-demo-c"]))}, 2000 + ALERTS.HOLD)
        self.assertEqual([("resolved", "Weekly trim worked again: data-c")], [(e["phase"], e["title"]) for e in cleared])

    def test_a_failed_trim_job_of_their_own_is_still_not_a_backup_issue(self):
        jobs = [{"name": "weekly-trim", "task": "filesystem-trim", "covers": 3, "last_failed": True}]
        self.assertEqual([], OV.protection_issues(jobs, {"configured": True, "available": True}))

    def test_health_facts_names_the_trim(self):
        with mock.patch.object(ALERTS.HISTORY, "maintenance_view", return_value=(set(), set(), {"open": {}, "ran_out": {}, "ending": {}})):
            facts = ALERTS.health_facts({"health_issues": [{"kind": "Trim", "name": "data-c", "severity": "degraded",
                                                            "reason": "x"}]})
        self.assertEqual(("Weekly trim failed: data-c", "Weekly trim worked again: data-c", "/data-protection#housekeeping"),
                         (facts[0]["title"], facts[0]["resolved"], facts[0]["href"]))

    def test_last_state_reads_the_saved_result_and_is_empty_when_it_cannot(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        keep_bound(self, WT)
        WT.bind(temp.name)
        self.assertEqual({}, WT.last_state())
        with open(os.path.join(temp.name, WT.STATE), "w", encoding="utf-8") as handle:
            json.dump(run(failed=[FAILED]), handle)
        self.assertEqual(run(failed=[FAILED]), WT.last_state())
        with open(os.path.join(temp.name, WT.STATE), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual({}, WT.last_state())


if __name__ == "__main__":
    unittest.main()
