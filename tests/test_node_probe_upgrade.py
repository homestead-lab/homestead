"""An upgrade has to finish itself: the probe reads scripts Homestead ships."""
import sys
import unittest
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_probe as probe


class ShippedScriptTests(unittest.TestCase):
    def test_the_manifest_in_this_repo_yields_scripts_that_compile(self):
        """If this breaks, an upgrade would push a broken probe to every node."""
        scripts = probe.shipped_scripts(ROOT / "deploy" / "nodeprobe.yaml")

        self.assertEqual({"probe.py", "smart.py"}, set(scripts))
        for name, body in scripts.items():
            with self.subTest(script=name):
                compile(body, name, "exec")

    def test_a_missing_manifest_is_not_an_exception(self):
        self.assertEqual({}, probe.shipped_scripts(ROOT / "deploy" / "nope.yaml"))


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

    def test_a_probe_installed_before_the_rename_is_updated_where_it_lives(self):
        """Creating a second DaemonSet beside it would run two probes."""
        self._install("harvui-nodeprobe", {"probe.py": "print(0)" + chr(10) + "",
                                           "smart.py": "print(0)" + chr(10) + ""})

        result = probe.reconcile("2.8.49")

        self.assertEqual("updated", result["state"])
        self.assertIn("harvui-nodeprobe", result["detail"])
        self.assertEqual(["/api/v1/namespaces/lab/configmaps/harvui-nodeprobe",
                          "/apis/apps/v1/namespaces/lab/daemonsets/harvui-nodeprobe"],
                         [p for _, p, _ in self.sent])

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
