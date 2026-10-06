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

    def test_an_on_demand_suite_is_left_out_of_a_release_but_runs_when_asked_for(self):
        on_demand = sorted(s for s, spec in PLAN.SUITES.items() if spec.get("on_demand"))
        self.assertIn("existing", on_demand)
        released = {row["suite"] for row in json.loads(PLAN.plan(tag="v2.9.0")["matrix"])["include"]}
        self.assertFalse(released & set(on_demand))
        asked = json.loads(PLAN.plan(version="2.9.0", suites="all")["matrix"])["include"]
        self.assertEqual({"k3s", "rke2"}, {r["distro"] for r in asked if r["suite"] == "existing"})
        self.assertEqual(["plain cluster,longhorn first"],
                         sorted({r["scenarios"] for r in asked if r["suite"] == "existing"}), "one job, one runner")

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


class SeedTests(unittest.TestCase):
    """The hosts' cloud-init: a line YAML misreads drops the whole file, and
    with it the user and key the suite signs in with."""
    def test_every_host_setup_is_valid_cloud_config(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        import tempfile
        from harness import vms
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(vms, "sh", lambda *a, **k: ""):
            for prepare, pages in ((False, 0), (False, 1100), (True, 0)):
                lab = vms.Lab(Path(tmp) / f"lab-{prepare}-{pages}", 1, hugepages=pages)
                lab.dir.mkdir(parents=True)
                Path(lab.key + ".pub").write_text("ssh-ed25519 AAAA e2e")
                node = lab.nodes[0]
                node.dir.mkdir(parents=True)
                lab._seed(node, prepare=prepare)
                doc = yaml.safe_load((node.dir / "user-data").read_text())
                self.assertEqual(vms.USER, doc["users"][0]["name"])
                self.assertTrue(all(isinstance(c, str) for c in doc["runcmd"]), doc["runcmd"])


class SecondNicTests(unittest.TestCase):
    """A lab with a spare NIC per host, and the runner pulling its cable."""

    def test_a_second_nic_on_the_bridge_and_a_cable_to_pull(self):
        import tempfile
        from harness import vms
        calls, sent = [], []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(vms, "sh", lambda *a, **k: calls.append(a) or ""), \
                mock.patch.object(vms.subprocess, "Popen") as popen:
            lab = vms.Lab(Path(tmp) / "lab", 1, nics=2)
            lab._network()
            node = lab.nodes[0]
            node.power_on()
            args = popen.call_args[0][0]
            popen.call_args[1]["stdout"].close()
            node.qmp = lambda command, arguments=None: sent.append((command, arguments))
            node.cable("lan2", False)
        self.assertIn(f"tap,id=lan2,ifname={node.tap2},script=no,downscript=no", args)
        self.assertIn(f"virtio-net-pci,netdev=lan2,mac={node.mac2},id=nic-lan2", args)
        self.assertTrue(any(node.tap2 in call for call in calls))
        self.assertEqual([("set_link", {"name": "nic-lan2", "up": False})], sent)

    def test_bonds_ride_in_the_network_suites_vip_job(self):
        from suites import SUITES, jobs
        self.assertEqual(2, SUITES["network"]["nics"])
        self.assertIn(["vip", "bonds"], jobs("network"))
