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
            server.set_homestead_replicas(2)
        self.assertEqual([], self.c.sent)
        server.set_homestead_replicas(1)

    def test_a_readwriteonce_volume_blocks_it_too_and_a_shared_one_allows_it(self):
        self.use(Cluster("longhorn", ("ReadWriteOnce",)))
        self.assertIn("ReadWriteOnce", server.homestead_data_volume()["reason"])
        self.use(Cluster("longhorn"))
        self.assertTrue(server.homestead_data_volume()["shareable"])
        server.set_homestead_replicas(2)
        self.assertEqual(2, self.c.sent[-1][2]["spec"]["replicas"])

    def test_moving_copies_on_the_attached_node_then_switches_the_claim(self):
        self.use(Cluster())
        with self.assertRaisesRegex(ValueError, "on longhorn-r2 already"):
            server.move_homestead_data("longhorn-r2")
        server.move_homestead_data("longhorn")
        pvc = next(b for m, p, b in self.c.sent if p.endswith("/persistentvolumeclaims"))
        self.assertEqual(("homestead-data-shared", "longhorn", ["ReadWriteMany"], "2Gi"),
                         (pvc["metadata"]["name"], pvc["spec"]["storageClassName"], pvc["spec"]["accessModes"],
                          pvc["spec"]["resources"]["requests"]["storage"]))
        job = next(b for m, p, b in self.c.sent if p.endswith("/jobs"))
        spec = job["spec"]["template"]["spec"]
        self.assertEqual("harvester-node1", spec["nodeName"])
        self.assertEqual(["homestead-data", "homestead-data-shared"],
                         [v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"]])
        item = {"ref": {"namespace": server.SELF.NS, "job": job["metadata"]["name"], "old": "homestead-data",
                        "new": "homestead-data-shared"}}
        self.assertEqual("running", server._data_move_status(item)[0])
        self.c.jobs[job["metadata"]["name"]] = {"status": {"succeeded": 1}}
        self.assertEqual("running", server._data_move_status(item)[0])
        self.assertEqual("homestead-data-shared",
                         self.c.dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        # the new pods, reading the copied record, find the switch made
        self.assertEqual("succeeded", server._data_move_status(item)[0])


    def test_it_can_move_to_a_class_one_node_mounts(self):
        """Not only for redundancy: any class, as a volume's class change is."""
        CLASSES["items"].append({"metadata": {"name": "longhorn-v1-1x"}, "provisioner": "driver.longhorn.io",
                                 "parameters": {"migratable": "true", "numberOfReplicas": "1"}})
        self.addCleanup(CLASSES["items"].pop)
        self.use(Cluster("longhorn"))
        self.assertIn({"name": "longhorn-v1-1x", "shareable": False}, server.homestead_data_volume()["classes"])
        server.move_homestead_data("longhorn-v1-1x")
        pvc = next(b for m, p, b in self.c.sent if p.endswith("/persistentvolumeclaims"))
        self.assertEqual(("homestead-data-moved", ["ReadWriteOnce"]), (pvc["metadata"]["name"], pvc["spec"]["accessModes"]))
        job = next(b for m, p, b in self.c.sent if p.endswith("/jobs"))
        self.c.jobs[job["metadata"]["name"]] = {"status": {"succeeded": 1}}
        item = {"ref": {"namespace": server.SELF.NS, "job": job["metadata"]["name"], "old": "homestead-data",
                        "new": "homestead-data-moved", "storage_class": "longhorn-v1-1x", "shareable": False}}
        server._data_move_status(item)
        # One node mounts it, so the old copy goes before the new one starts.
        self.assertEqual("Recreate", self.c.dep["spec"]["strategy"]["type"])

    def test_a_one_node_class_needs_one_copy_first(self):
        CLASSES["items"].append({"metadata": {"name": "longhorn-v1-1x"}, "provisioner": "driver.longhorn.io",
                                 "parameters": {"migratable": "true"}})
        self.addCleanup(CLASSES["items"].pop)
        cluster = Cluster("longhorn")
        cluster.dep["spec"]["replicas"] = 2
        self.use(cluster)
        with self.assertRaisesRegex(ValueError, "set Redundancy to one copy first"):
            server.move_homestead_data("longhorn-v1-1x")

    def test_nothing_moves_while_a_job_is_running(self):
        """The copy is of the moment: a job running meanwhile would come back
        after the restart from an older step."""
        self.use(Cluster())
        self.running = [{"status": "running", "kind": "reclass", "title": "Move qdirstat-appdata to longhorn-v1-1x"}]
        with self.assertRaisesRegex(ValueError, "1 job is still running .Move qdirstat-appdata"):
            server.move_homestead_data("longhorn")
        self.assertEqual([], self.c.sent)


if __name__ == "__main__":
    unittest.main()
