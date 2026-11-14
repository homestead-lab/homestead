"""The node probe and the runtime helpers run Homestead's own image, of the
running release: one image Homestead builds, upgrades and scans, rather than a
stock python:3.12-alpine that can lag Alpine's security fixes (November 2026,
krb5-libs)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_names as NAMES
import homestead_probe as PROBE
import homestead_runtime as RUNTIME
import homestead_version


class OwnImageTests(unittest.TestCase):
    def test_the_probe_runs_homesteads_image_of_this_release_as_root(self):
        daemonset = PROBE.manifest("2.8.400")[1]
        containers = {c["name"]: c for c in daemonset["spec"]["template"]["spec"]["containers"]}
        for name in ("probe", "smart"):
            self.assertEqual(NAMES.IMAGE + ":2.8.400", containers[name]["image"], name)
        # The stock image ran it as root; Homestead's defaults to its own user.
        security = containers["probe"]["securityContext"]
        self.assertEqual((0, False, ["ALL"], True),
                         (security["runAsUser"], security["allowPrivilegeEscalation"],
                          security["capabilities"]["drop"], security["readOnlyRootFilesystem"]))

    def test_runtime_helpers_run_homesteads_image_of_this_release(self):
        with mock.patch("homestead_platform.detect", lambda *_: {"distribution": "k3s"}):
            pod = RUNTIME.pod("p", "lab", "node1", "true", "image-scan")
        self.assertEqual(NAMES.IMAGE + ":" + homestead_version.VERSION, pod["spec"]["containers"][0]["image"])

    def test_no_stock_python_image_is_pulled_or_shipped(self):
        # homestead_probe names it only to replace it in a probe installed before.
        self.assertNotIn("python:3.12-alpine", repr(PROBE.manifest("2.8.400")))
        self.assertEqual(("python:3.12-alpine",), PROBE.MANAGED_IMAGES)
        for rel in ("server/homestead_runtime.py", "deploy/nodeprobe.yaml",
                    "charts/homestead/templates/nodeprobe.yaml", "charts/homestead/Chart.yaml"):
            with self.subTest(rel=rel):
                self.assertNotIn("python:3.12-alpine", (ROOT / rel).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
