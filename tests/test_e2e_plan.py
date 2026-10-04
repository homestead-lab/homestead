"""The release suite's planner: what runs for a release, on demand, and from
an e2e/ branch - and that the suite's own code at least imports."""
import json
import py_compile
import sys
import unittest
from pathlib import Path
from unittest import mock

E2E = Path(__file__).resolve().parent / "e2e"
sys.path.insert(0, str(E2E))
import plan as PLAN  # noqa: E402


class PlanTests(unittest.TestCase):
    def matrix(self, out):
        """The (distribution, suite) pairs, once each, in order: a suite may be several jobs."""
        return list(dict.fromkeys((row["distro"], row["suite"]) for row in json.loads(out["matrix"])["include"]))

    def test_a_release_fits_the_twenty_jobs_a_repository_runs_at_once(self):
        rows = json.loads(PLAN.plan(tag="v2.9.0")["matrix"])["include"]
        self.assertLessEqual(len(rows), 20)
        self.assertTrue(any(r["distro"] == "rke2" for r in rows), "RKE2 runs where it differs")
        self.assertEqual(len({r["slug"] for r in rows}), len(rows), "each job its own artifact name")
        self.assertIn({"distro": "k3s", "suite": "single", "scenarios": "single-host power-off",
                       "label": "single: single-host power-off", "slug": "k3s-single-single-host-power-off"}, rows)

    def test_every_prod_release_runs_by_itself_and_no_dev_one(self):
        self.assertEqual("true", PLAN.plan(tag="v2.9.0")["run"])
        self.assertEqual("true", PLAN.plan(tag="v2.8.312")["run"])
        self.assertEqual("false", PLAN.plan(tag="v2.9.0-dev.1")["run"])
        self.assertEqual("false", PLAN.plan(tag="v2.8.313-dev.2")["run"])

    def test_on_demand_any_published_version_suites_and_distros(self):
        out = PLAN.plan(version="2.8.312-dev.1", suites="power,single", distros="rke2")
        self.assertEqual(("true", "2.8.312-dev.1"), (out["run"], out["version"]))
        self.assertEqual([("rke2", "power"), ("rke2", "single")], self.matrix(out))
        self.assertEqual(2 * len(PLAN.SUITES), len(self.matrix(PLAN.plan(version="2.8.312"))), "every suite on both distributions")
        with self.assertRaises(SystemExit):
            PLAN.plan(version="2.8.312", suites="nonsense")

    def test_an_e2e_branch_names_what_runs_on_the_newest_release(self):
        with mock.patch.object(PLAN, "newest_release", return_value="2.8.312"):
            self.assertEqual([("k3s", "single")], self.matrix(PLAN.plan(branch="e2e/k3s-single")))
            self.assertEqual(len(PLAN.SUITES), len(self.matrix(PLAN.plan(branch="e2e/rke2-all"))))
            self.assertEqual(2 * len(PLAN.SUITES), len(self.matrix(PLAN.plan(branch="e2e/all"))))
            self.assertEqual("2.8.312", PLAN.plan(branch="e2e/all")["version"])

    def test_the_suite_compiles(self):
        for path in E2E.rglob("*.py"):
            py_compile.compile(str(path), doraise=True)


if __name__ == "__main__":
    unittest.main()
