import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_specs as SPECS
import homestead_shares as shares
import homestead_manifests as MANIFESTS
import server
import test_shares


class SpecTests(unittest.TestCase):
    def test_zero_values_kubernetes_drops_do_not_count(self):
        wanted = [{"name": "v", "mountPath": "/shares/a", "readOnly": False, "subPath": ""}]
        stored = [{"mountPath": "/shares/a", "name": "v"}]
        self.assertTrue(SPECS.same(wanted, stored))
        self.assertFalse(SPECS.same([{"name": "v", "mountPath": "/shares/a", "readOnly": True}], stored))
        self.assertFalse(SPECS.same({"port": 445}, {"port": 446}))


class SmbTests(unittest.TestCase):
    """The share tests' fixture, for what changed here."""

    setUp = test_shares.ShareTests.setUp

    def test_a_writable_share_mounts_without_readonly_so_reconcile_is_quiet(self):
        deployment = shares.configured_deployment(copy.deepcopy(self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]),
                                                  [{"name": "secure", "pvc": "share-secure", "user": "lab", "public": False}],
                                                  {"secure": {"user": "lab", "password": "p"}})
        mount = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]
        self.assertNotIn("readOnly", mount)

    def test_a_failed_share_takes_its_new_volume_with_it(self):
        with mock.patch.object(shares, "apply_samba", side_effect=ValueError("Samba could not start")):
            with self.assertRaisesRegex(ValueError, "could not start"):
                shares.create_share("media", 10, "lab", "a-long-password", False)
        self.assertIn(("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/share-media", None),
                      [(m, p, b) for m, p, b in self.sent if m == "DELETE"])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.nodes = [{"metadata": {"name": "k3s-1", "labels": {"node-role.kubernetes.io/control-plane": "true"}},
                       "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                      {"metadata": {"name": "k3s-2", "labels": {}}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
        self.runs = []

        class Host:
            @staticmethod
            def run(node, script, timeout=60):
                self.runs.append((node, script))
                return "/var/lib/rancher/k3s/server/manifests/homestead.yaml\ndone\n", ""

        self.platform = {"distribution": "k3s"}
        MANIFESTS.bind(lambda path: {"items": self.nodes}, Host, lambda force=False: self.platform, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_server_node_is_marked_once(self):
        self.assertEqual({"k3s-1": ["/var/lib/rancher/k3s/server/manifests/homestead.yaml"]}, MANIFESTS.tick())
        self.assertEqual(["k3s-1"], [node for node, _ in self.runs], "servers only: agents have no auto-deploy folder")
        self.assertEqual({}, MANIFESTS.tick(), "done once")
        script = self.runs[0][1]
        for part in ("homestead.yaml", "longhorn.yaml", "kubevirt-cr.yaml", ".skip", "rke2/server/manifests"):
            self.assertIn(part, script)
        self.assertNotIn("rm ", script, "files are marked, never removed")

    def test_harvester_and_other_clusters_are_left_alone(self):
        for platform in ({"harvester": True, "distribution": "harvester"}, {"distribution": "kubernetes"}):
            self.platform = platform
            self.assertEqual({}, MANIFESTS.tick())
        self.assertEqual([], self.runs)


LOCAL = {"name": "local-path", "provisioner": "rancher.io/local-path", "migratable": False, "internal": False,
         "made_for": "", "default": True, "parameters": {}}
LONGHORN = {"name": "longhorn", "provisioner": "driver.longhorn.io", "migratable": False, "internal": False,
            "made_for": "", "default": True, "parameters": {}}


class StorageClassTests(unittest.TestCase):
    def test_local_path_does_not_share(self):
        self.assertEqual(["longhorn"], server.shared_storage_classes([dict(LOCAL), dict(LONGHORN)]))

    def test_a_shared_volume_on_local_path_is_refused_with_the_reason(self):
        with mock.patch.object(server, "storage_classes", return_value=[dict(LOCAL), dict(LONGHORN)]):
            with self.assertRaisesRegex(ValueError, "serves one node at a time"):
                server.create_pvc("lab", "media", 10, "local-path", "ReadWriteMany", send=lambda *a, **k: None)

    def test_the_default_chosen_here_is_kept_when_k3s_marks_local_path_again(self):
        demoted = []
        with tempfile.TemporaryDirectory() as data, \
                mock.patch.object(server, "DATA_DIR", data), \
                mock.patch.object(server, "storage_classes", return_value=[dict(LOCAL), dict(LONGHORN)]), \
                mock.patch.object(server, "ksend", lambda method, path, body=None, **k: demoted.append(path)):
            Path(data, "default-class.json").write_text(json.dumps({"name": "longhorn"}))
            self.assertEqual(("longhorn", ["local-path"]), server.reconcile_default_class())
        self.assertEqual(["/apis/storage.k8s.io/v1/storageclasses/local-path"], demoted)

    def test_with_nothing_chosen_longhorn_wins_over_local_path(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.object(server, "DATA_DIR", data), \
                mock.patch.object(server, "storage_classes", return_value=[dict(LOCAL), dict(LONGHORN)]), \
                mock.patch.object(server, "ksend", lambda *a, **k: None):
            self.assertEqual(("longhorn", ["local-path"]), server.reconcile_default_class())

    def test_one_default_is_left_alone(self):
        with mock.patch.object(server, "storage_classes", return_value=[dict(LOCAL), dict(LONGHORN, default=False)]):
            self.assertIsNone(server.reconcile_default_class())


class ShutdownTests(unittest.TestCase):
    def test_the_probe_and_helpers_stop_when_asked(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("probe.py", "smart.py"):
            self.assertIn("signal.SIGTERM", (root / "charts/homestead/files/probe" / name).read_text(encoding="utf-8"))
        for module in ("homestead_files.py", "homestead_nodeshell.py", "homestead_hostrun.py"):
            self.assertIn("trap 'exit 0' TERM", (root / "server" / module).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
