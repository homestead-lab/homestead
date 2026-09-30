import sys, unittest, unittest.mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_runtime as RUNTIME


class K3sToolsTests(unittest.TestCase):
    """/usr/local/bin/k3s run as crictl in a pod died: "extracting data"."""

    def test_k3s_mounts_the_unpacked_binaries_not_the_launcher(self):
        with unittest.mock.patch("homestead_platform.detect", lambda: {"distribution": "k3s"}):
            tools = RUNTIME.binaries()
            paths = {v["name"]: v["hostPath"]["path"] for v in RUNTIME.pod("p", "lab", "n", "true", "t")["spec"]["volumes"]}
        self.assertEqual("/var/lib/rancher/k3s/data/current/bin/crictl", tools["crictl"])
        self.assertEqual("/var/lib/rancher/k3s/data/current/bin/ctr", tools["ctr"])
        self.assertNotIn("/usr/local/bin/k3s", paths.values())

    def test_rke2_keeps_its_own_tools(self):
        with unittest.mock.patch("homestead_platform.detect", lambda: {"distribution": "rke2"}):
            self.assertEqual("/var/lib/rancher/rke2/bin/crictl", RUNTIME.binaries()["crictl"])


if __name__ == "__main__":
    unittest.main()
