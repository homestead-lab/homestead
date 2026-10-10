import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server

CLASSES = {"items": [
    {"metadata": {"name": "longhorn-r2"}, "provisioner": "driver.longhorn.io", "parameters": {"migratable": "true"}},
    {"metadata": {"name": "longhorn"}, "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "3"}},
]}


class Cluster:
    def __init__(self, klass="longhorn-r2", modes=("ReadWriteMany",)):
        self.dep = {"metadata": {"name": "homestead"}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "homestead"}},
                    "template": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data"}}]}}}}
        self.pvc = {"metadata": {"name": "homestead-data"}, "spec": {"storageClassName": klass, "accessModes": list(modes)},
                    "status": {"capacity": {"storage": "2Gi"}}}
        self.jobs = {}
        self.sent = []

    def get(self, path, **kw):
        if "storageclasses" in path:
            return copy.deepcopy(CLASSES)
        if path.endswith("/deployments/homestead"):
            return copy.deepcopy(self.dep)
        if path.endswith("/persistentvolumeclaims/homestead-data"):
            return copy.deepcopy(self.pvc)
        if path.endswith("/persistentvolumeclaims"):
            return {"items": [self.pvc]}
        if "/pods?" in path:
            return {"items": [{"status": {"phase": "Running"}, "spec": {"nodeName": "harvester-node1"}}]}
        if "/jobs/" in path:
            return self.jobs.get(path.rsplit("/", 1)[1], {"status": {"active": 1}})
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if method == "PUT" and path.endswith("/deployments/homestead"):
            self.dep = body
        return body


class DataMoveTests(unittest.TestCase):
    running = []

    def use(self, cluster):
        self.c = cluster
        patches = [mock.patch.object(server, "kget", cluster.get), mock.patch.object(server, "ksend", cluster.send),
                   mock.patch.object(server.OPS, "start", lambda *a, **k: {"id": "op"}),
                   mock.patch.object(server.OPS, "list_operations", lambda: self.running)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_migratable_data_volume_blocks_a_second_copy(self):
        self.use(Cluster())
        info = server.homestead_data_volume()
        self.assertFalse(info["shareable"])
        self.assertIn("migratable", info["reason"])
        self.assertEqual(["longhorn"], info["candidates"])
        with self.assertRaisesRegex(ValueError, "Move Homestead's data"):
            server.SELF_HEALTH.set_replicas(2)
        self.assertEqual([], self.c.sent)
        server.SELF_HEALTH.set_replicas(1)

    def test_a_readwriteonce_volume_blocks_it_too_and_a_shared_one_allows_it(self):
        self.use(Cluster("longhorn", ("ReadWriteOnce",)))
        self.assertIn("ReadWriteOnce", server.homestead_data_volume()["reason"])
        self.use(Cluster("longhorn"))
        self.assertTrue(server.homestead_data_volume()["shareable"])
        server.SELF_HEALTH.set_replicas(2)
        self.assertEqual(2, self.c.sent[-1][2]["spec"]["replicas"])

    def test_legacy_live_copy_is_disabled_without_cluster_writes(self):
        self.use(Cluster())
        with self.assertRaisesRegex(ValueError, "live-copy mover was retired"):
            server.move_homestead_data("longhorn")
        self.assertEqual([], self.c.sent)

    def test_local_path_and_unknown_destination_classes_are_not_assumed_rwx(self):
        self.use(Cluster())
        rows = [{"name": "local-path", "provisioner": "rancher.io/local-path", "migratable": False},
                {"name": "unknown", "provisioner": "example.test", "migratable": False},
                {"name": "longhorn", "provisioner": "driver.longhorn.io", "migratable": False}]
        with mock.patch.object(server, "storage_classes", return_value=rows):
            info = server.homestead_data_volume()
        self.assertEqual(["longhorn"], info["candidates"])
        self.assertEqual({"local-path": False, "unknown": False, "longhorn": True}, {r["name"]: r["shareable"] for r in info["classes"]})

    def test_old_jobs_never_switch_a_claim_even_when_the_job_succeeded(self):
        self.use(Cluster())
        self.c.jobs["old-job"] = {"status": {"succeeded": 1}}
        item = {"ref": {"namespace": server.SELF.NS, "job": "old-job", "old": "homestead-data", "new": "target"}}
        self.assertEqual("failed", server._data_move_status(item)[0])
        self.assertTrue(item["ref"]["retain_resources"])
        self.assertEqual([], self.c.sent)

    def test_legacy_cancel_cannot_delete_an_unverified_destination(self):
        import homestead_cancel as cancel
        item = {"ref": {"namespace": "lab", "job": "old", "old": "source", "new": "target"}}
        with mock.patch.object(cancel, "_delete") as delete:
            self.assertFalse(cancel.self_move_plan(item)["can"])
            with self.assertRaisesRegex(ValueError, "both volumes are retained"):
                cancel.self_move_cancel(item, {})
        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
