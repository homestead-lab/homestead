import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server
sys.path.insert(0, str(ROOT / "tests"))
from fakes.kube import Kube

CLASSES = [
    {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "longhorn-r2"},
     "provisioner": "driver.longhorn.io", "parameters": {"migratable": "true"}},
    {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "longhorn"},
     "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "3"}},
]


def cluster(klass="longhorn-r2", modes=("ReadWriteMany",), jobs=()):
    """Homestead's Deployment, its data claim and its running pod, on the
    shared fake API (tests/fakes/kube.py)."""
    ns = server.SELF.NS
    return Kube(
        *CLASSES,
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "homestead", "namespace": ns},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "homestead"}},
                  "template": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data"}}]}}}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "homestead-data", "namespace": ns},
         "spec": {"storageClassName": klass, "accessModes": list(modes)}, "status": {"capacity": {"storage": "2Gi"}}},
        {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "homestead-1", "namespace": ns, "labels": {"app": "homestead"}},
         "status": {"phase": "Running"}, "spec": {"nodeName": "harvester-node1"}},
        *({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": ns}, "status": status}
          for name, status in jobs))


class DataMoveTests(unittest.TestCase):
    running = []

    def use(self, kube):
        self.kube = self.enterContext(kube)
        # The shared fake runs the real write guards; the storage guard takes the job store's
        # lock, which lives under OPS.DATA_DIR.
        data = self.enterContext(tempfile.TemporaryDirectory())
        patches = [mock.patch.object(server.OPS, "start", lambda *a, **k: {"id": "op"}),
                   mock.patch.object(server.OPS, "list_operations", lambda: self.running),
                   mock.patch.object(server.OPS, "DATA_DIR", data)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_migratable_data_volume_blocks_a_second_copy(self):
        self.use(cluster())
        info = server.SELF_DATA_ROUTES.homestead_data_volume()
        self.assertFalse(info["shareable"])
        self.assertIn("migratable", info["reason"])
        self.assertEqual(["longhorn"], info["candidates"])
        with self.assertRaisesRegex(ValueError, "Move Homestead's data"):
            server.SELF_HEALTH.set_replicas(2)
        self.assertEqual([], self.kube.writes)
        server.SELF_HEALTH.set_replicas(1)

    def test_a_readwriteonce_volume_blocks_it_too_and_a_shared_one_allows_it(self):
        self.use(cluster("longhorn", ("ReadWriteOnce",)))
        self.assertIn("ReadWriteOnce", server.SELF_DATA_ROUTES.homestead_data_volume()["reason"])
        self.use(cluster("longhorn"))
        self.assertTrue(server.SELF_DATA_ROUTES.homestead_data_volume()["shareable"])
        server.SELF_HEALTH.set_replicas(2)
        self.assertEqual(("PUT", 2), (self.kube.writes[-1][0], self.kube.writes[-1][2]["spec"]["replicas"]))
        stored = self.kube.object(f"/apis/apps/v1/namespaces/{server.SELF.NS}/deployments/homestead")
        self.assertEqual(2, stored["spec"]["replicas"], "the second copy is really asked for")

    def test_legacy_live_copy_is_disabled_without_cluster_writes(self):
        self.use(cluster())
        with self.assertRaisesRegex(ValueError, "live-copy mover was retired"):
            server.SELF_DATA_ROUTES.move_homestead_data("longhorn")
        self.assertEqual([], self.kube.writes)

    def test_local_path_and_unknown_destination_classes_are_not_assumed_rwx(self):
        self.use(cluster())
        rows = [{"name": "local-path", "provisioner": "rancher.io/local-path", "migratable": False},
                {"name": "unknown", "provisioner": "example.test", "migratable": False},
                {"name": "longhorn", "provisioner": "driver.longhorn.io", "migratable": False}]
        with mock.patch.object(server, "storage_classes", return_value=rows):
            info = server.SELF_DATA_ROUTES.homestead_data_volume()
        self.assertEqual(["longhorn"], info["candidates"])
        self.assertEqual({"local-path": False, "unknown": False, "longhorn": True}, {r["name"]: r["shareable"] for r in info["classes"]})

    def test_old_jobs_never_switch_a_claim_even_when_the_job_succeeded(self):
        self.use(cluster(jobs=[("old-job", {"succeeded": 1})]))
        item = {"ref": {"namespace": server.SELF.NS, "job": "old-job", "old": "homestead-data", "new": "target"}}
        self.assertEqual("failed", server.SELF_DATA_ROUTES._data_move_status(item)[0])
        self.assertTrue(item["ref"]["retain_resources"])
        self.assertEqual([], self.kube.writes)

    def test_legacy_cancel_cannot_delete_an_unverified_destination(self):
        import homestead_cancel as cancel
        item = {"ref": {"namespace": "lab", "job": "old", "old": "source", "new": "target"}}
        with mock.patch.object(cancel, "_delete") as delete:
            self.assertFalse(cancel.self_move_plan(item)["can"])
            with self.assertRaisesRegex(ValueError, "Both volumes are kept"):
                cancel.self_move_cancel(item, {})
        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
