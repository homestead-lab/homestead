import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "server"))
import render_chart  # noqa: E402


class ChartTests(unittest.TestCase):
    """The chart is generated from the manifests, so it cannot drift from them."""

    def test_the_checked_in_chart_is_what_the_manifests_render(self):
        release = render_chart.version()
        expected = render_chart.files(release)
        chart = ROOT / "charts" / "homestead"
        on_disk = {str(p.relative_to(chart)).replace("\\", "/"): p.read_text(encoding="utf-8")
                   for p in chart.rglob("*") if p.is_file()}
        self.assertEqual(sorted(expected), sorted(on_disk), "run python scripts/render_chart.py")
        for name, text in expected.items():
            self.assertEqual(text, on_disk[name], f"{name} is stale: run python scripts/render_chart.py")

    def test_the_chart_is_the_release(self):
        chart = render_chart.files(render_chart.version())["Chart.yaml"]
        self.assertIn(f"version: {render_chart.version()}\n", chart)
        self.assertIn(f'appVersion: "{render_chart.version()}"', chart)

    def test_permissions_come_from_deploy_yaml_with_both_namespaces(self):
        rbac = render_chart.rbac()
        self.assertIn("kind: ClusterRole\n", rbac)
        self.assertIn("  namespace: {{ .Values.workloadNamespace.name }}", rbac)   # console role, where apps are
        self.assertIn("    namespace: {{ .Release.Namespace }}", rbac)             # the service account
        self.assertNotIn("namespace: lab", rbac)

    def test_the_probe_ships_its_scripts_and_follows_the_image(self):
        files = render_chart.files(render_chart.version())
        self.assertIn("files/probe/probe.py", files)
        daemonset = files["templates/nodeprobe.yaml"]
        self.assertIn('image: {{ include "homestead.image" . }}', daemonset)
        self.assertIn("checksum/scripts", daemonset)


if __name__ == "__main__":
    unittest.main()
