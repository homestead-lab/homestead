"""VMs start on kernels that stack AppArmor profiles: on hosts that run them,
the leader sets kernel.apparmor_restrict_unprivileged_unconfined to 0 once
(design section 6; confirmed on Ubuntu 7.0.0-43, 14 November 2026)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_apparmor_fix as FIX
import homestead_hostrun as HOSTRUN
from rebind import keep_bound

SH = "/bin/sh" if Path("/bin/sh").exists() and sys.platform != "win32" else None


def node(name, ready=True, vms=True):
    return {"metadata": {"name": name, "labels": {"kubevirt.io/schedulable": "true"} if vms else {}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class FixTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        keep_bound(self, FIX)
        self.nodes = [node("node1"), node("node2", vms=False), node("node3", ready=False)]
        self.ran, self.answer = [], "SET 1"
        runner = mock.Mock()
        runner.run = lambda name, script, **k: (self.ran.append((name, k["change"])) or (self.answer, ""))
        FIX.bind(lambda path: {"items": self.nodes}, runner, lambda: {"distribution": "k3s"}, tmp.name)

    def test_only_ready_hosts_that_run_vms_and_only_once(self):
        self.assertEqual({"node1": "kernel.apparmor_restrict_unprivileged_unconfined 1 -> 0"}, FIX.tick())
        self.assertEqual(["node1"], [name for name, _ in self.ran])
        self.assertIn("Let VMs start on node1", self.ran[0][1])
        self.assertEqual({}, FIX.tick(), "a person who sets it back keeps it")
        self.assertEqual(1, len(self.ran))

    def test_kept_or_absent_is_recorded_and_says_nothing(self):
        for answer in ("KEPT 0", "ABSENT"):
            with self.subTest(answer=answer), mock.patch.object(FIX, "DATA_DIR", tempfile.mkdtemp()):
                self.answer = answer
                self.assertEqual({}, FIX.tick())
                self.assertIn("node1", FIX._load())

    def test_harvester_and_other_distributions_are_left_alone(self):
        for found in ({"harvester": True, "distribution": "rke2"}, {"distribution": "kubeadm"}):
            FIX.platform = lambda found=found: found
            self.assertEqual({}, FIX.tick())
        self.assertEqual([], self.ran)

    def test_a_busy_host_is_tried_next_round_and_an_odd_answer_is_an_error(self):
        FIX.hostrun.run = mock.Mock(side_effect=HOSTRUN.Busy("busy"))
        self.assertEqual({}, FIX.tick())
        self.assertNotIn("node1", FIX._load())
        FIX.hostrun.run = lambda *a, **k: ("sh: denied", "")
        with self.assertRaisesRegex(ValueError, "could not check node1's AppArmor setting"):
            FIX.tick()

    def test_its_own_file_not_the_inotify_one(self):
        self.assertEqual("/etc/sysctl.d/91-homestead-apparmor.conf", FIX.CONF)
        self.assertIn("> /etc/sysctl.d/91-homestead-apparmor.conf", FIX.script())

    @unittest.skipUnless(SH, "no POSIX shell here")
    def test_the_script_sets_it_only_when_it_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, conf = Path(tmp, "setting"), Path(tmp, "91.conf")
            script = (FIX.script().replace("/proc/sys/kernel/apparmor_restrict_unprivileged_unconfined", str(proc))
                      .replace(FIX.CONF, str(conf)).replace("sysctl -q -w", "echo sysctl"))
            run = lambda: subprocess.run([SH, "-c", script], capture_output=True, text=True).stdout
            self.assertEqual("ABSENT", run().strip())
            proc.write_text("1\n")
            out = run()
            self.assertIn("SET 1", out)
            self.assertIn("kernel.apparmor_restrict_unprivileged_unconfined = 0", conf.read_text())
            proc.write_text("0\n")
            self.assertEqual("KEPT 0", run().strip())


if __name__ == "__main__":
    unittest.main()
