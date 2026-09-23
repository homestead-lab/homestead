"""An upgrade has to finish itself: the probe reads scripts Homestead ships."""
import sys
import unittest
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_probe as probe


class ShippedScriptTests(unittest.TestCase):
    def test_the_scripts_in_this_repo_compile(self):
        """If this breaks, an upgrade would push a broken probe to every node."""
        scripts = probe.shipped_scripts(ROOT / "server" / "probe")

        self.assertEqual({"probe.py", "smart.py"}, set(scripts))
        for name, body in scripts.items():
            with self.subTest(script=name):
                compile(body, name, "exec")

    def test_a_missing_script_directory_is_not_an_exception(self):
        self.assertEqual({}, probe.shipped_scripts(ROOT / "server" / "nope"))


class RenderedManifestTests(unittest.TestCase):
    """The file people apply and the objects Homestead installs are the same."""

    def test_the_checked_in_manifest_matches_what_the_code_describes(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import render_nodeprobe

        rendered = render_nodeprobe.render(render_nodeprobe.current_version())
        checked_in = (ROOT / "deploy" / "nodeprobe.yaml").read_text(encoding="utf-8")

        self.assertEqual(checked_in, rendered,
                         "run scripts/render_nodeprobe.py — the manifest is generated")

    def test_the_smart_sidecar_runs_this_release(self):
        containers = probe.manifest("9.9.9")[1]["spec"]["template"]["spec"]["containers"]
        smart = next(c for c in containers if c["name"] == "smart")

        self.assertEqual("ghcr.io/wjcloudy/homestead:9.9.9", smart["image"])

    def test_only_the_smart_sidecar_is_privileged(self):
        """The telemetry container reads sensors; it needs nothing special."""
        containers = probe.manifest("9.9.9")[1]["spec"]["template"]["spec"]["containers"]
        by_name = {c["name"]: c["securityContext"] for c in containers}

        self.assertTrue(by_name["smart"]["privileged"])
        self.assertNotIn("privileged", by_name["probe"])
        self.assertFalse(by_name["probe"]["allowPrivilegeEscalation"])
        self.assertEqual(["ALL"], by_name["probe"]["capabilities"]["drop"])


class InstallTests(unittest.TestCase):
    """Installing is a decision, so Homestead does it only when asked."""

    def setUp(self):
        self.objects = {}
        self.sent = []
        probe.bind(self._get, self._send, "lab")
        real = probe.shipped_scripts
        self.addCleanup(setattr, probe, "shipped_scripts", real)
        probe.shipped_scripts = lambda directory=None: {"probe.py": "x", "smart.py": "y"}

    def _get(self, path):
        key = path.split("?")[0]
        if key not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return self.objects[key]

    def _send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path.split("?")[0], body))
        return body or {}

    def test_install_creates_the_configmap_and_the_daemonset(self):
        result = probe.install("2.8.51")

        self.assertEqual("installed", result["state"])
        self.assertEqual(["/api/v1/namespaces/lab/configmaps",
                          "/apis/apps/v1/namespaces/lab/daemonsets"],
                         [p for _, p, _ in self.sent])
        daemonset = self.sent[1][2]
        self.assertEqual("homestead-nodeprobe", daemonset["metadata"]["name"])

    def test_installing_twice_is_refused_rather_than_duplicated(self):
        self.objects["/apis/apps/v1/namespaces/lab/daemonsets/homestead-nodeprobe"] = {
            "metadata": {"name": "homestead-nodeprobe"}}

        with self.assertRaisesRegex(ValueError, "already installed"):
            probe.install("2.8.51")
        self.assertEqual([], self.sent)

    def test_remove_takes_the_probe_away(self):
        result = probe.remove()

        self.assertEqual("absent", result["state"])
        deleted = [p for m, p, _ in self.sent if m == "DELETE"]
        self.assertIn("/apis/apps/v1/namespaces/lab/daemonsets/homestead-nodeprobe", deleted)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.sent = []
        probe.bind(self._get, self._send, "lab")
        real = probe.shipped_scripts
        self.addCleanup(setattr, probe, "shipped_scripts", real)
        probe.shipped_scripts = lambda path=None: {
            "probe.py": "print(1)" + chr(10), "smart.py": "print(2)" + chr(10)}

    def _get(self, path):
        key = path.split("?")[0]
        if key not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return self.objects[key]

    def _send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path.split("?")[0], body))
        return body or {}

    def _install(self, name, scripts):
        self.objects[f"/apis/apps/v1/namespaces/lab/daemonsets/{name}"] = {
            "metadata": {"name": name}}
        self.objects[f"/api/v1/namespaces/lab/configmaps/{name}"] = {
            "metadata": {"name": name}, "data": scripts}

    def test_an_out_of_date_probe_is_updated_and_restarted(self):
        self._install("homestead-nodeprobe", {"probe.py": "print(0)" + chr(10) + "",
                                              "smart.py": "print(0)" + chr(10) + ""})

        result = probe.reconcile("2.8.49")

        self.assertEqual("updated", result["state"])
        written = next(b for m, p, b in self.sent if m == "PUT" and "configmaps" in p)
        self.assertEqual("print(1)" + chr(10), written["data"]["probe.py"])
        self.assertIn(("PATCH", "/apis/apps/v1/namespaces/lab/daemonsets/homestead-nodeprobe"),
                      [(m, p) for m, p, _ in self.sent])

    def test_a_probe_already_current_is_left_alone(self):
        self._install("homestead-nodeprobe", {"probe.py": "print(1)" + chr(10),
                                              "smart.py": "print(2)" + chr(10)})

        self.assertEqual("current", probe.reconcile("2.8.49")["state"])
        self.assertEqual([], self.sent, "no needless restart of every node's probe")

    def test_an_uninstalled_probe_is_not_installed_uninvited(self):
        """The SMART sidecar is privileged; running one is the operator's call."""
        result = probe.reconcile("2.8.49")

        self.assertEqual("absent", result["state"])
        self.assertEqual([], self.sent)

    def test_other_keys_in_the_configmap_survive(self):
        self._install("homestead-nodeprobe", {"probe.py": "print(0)" + chr(10) + "",
                                              "smart.py": "print(0)" + chr(10) + "",
                                              "local.conf": "keep me"})

        probe.reconcile("2.8.49")

        written = next(b for m, p, b in self.sent if m == "PUT" and "configmaps" in p)
        self.assertEqual("keep me", written["data"]["local.conf"])


if __name__ == "__main__":
    unittest.main()
