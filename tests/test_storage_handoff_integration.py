"""Exercise the staged handoff with real placement, not allow-all callbacks."""
import unittest
from unittest import mock

from test_reclass_handoff import CopyCluster
import homestead_storage_admission as admission
import homestead_place as place
import homestead_storage_journal as journal


class AdmissionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.cluster = CopyCluster()
        self.nodes = [{"name": "a", "status": "Ready", "schedulable": True, "hardware": {},
            "labels": {"kubernetes.io/hostname": "a"}, "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "100"},
            "mem_cap_gb": 8, "mem_used_gb": 1, "mem_metrics_available": True}]
        for name in ("old", "new"):
            path = "/apis/storage.k8s.io/v1/storageclasses/" + name
            obj = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
                "metadata": {"name": name, "uid": "class-" + name, "resourceVersion": "1"},
                "provisioner": "test.storage", "reclaimPolicy": "Delete", "volumeBindingMode": "WaitForFirstConsumer"}
            self.cluster.objects[path] = obj
            self.cluster.item["ref"]["review_fences"][path] = journal.identity(obj)
        patch = mock.patch.object(place, "hardware_features", return_value=[])
        patch.start(); self.addCleanup(patch.stop)

    def helper(self, manifest):
        return place.manifest_plan(manifest, "lab", manifest["metadata"]["name"],
            pod_snapshot=self.cluster.read("/api/v1/pods")["items"], nodes_snapshot=self.nodes, read=self.cluster.read)

    def restarts(self, proposals):
        return admission.plan(self.cluster.item, proposals, self.cluster.read, self.nodes, 88)

    def copy(self):
        self.assertEqual(12, self.cluster.stage(self.helper)[1])
        self.assertEqual(20, self.cluster.stage(self.helper)[1])
        self.cluster.complete()
        self.assertEqual(75, self.cluster.stage(self.helper)[1])

    def cutover(self):
        for _ in range(15):
            self.cluster.cutover(self.restarts)
            if self.cluster.item["ref"].get("cutover_complete"):
                return
        self.fail("cutover did not complete")

    def test_capacity_lost_during_copy_blocks_cutover_without_deleting_claims(self):
        self.copy()
        self.nodes[0]["allocatable"]["memory"] = "512Mi"
        before = len(self.cluster.sent)
        with self.assertRaises(journal.Held): self.cluster.cutover(self.restarts)
        self.assertEqual(before, len(self.cluster.sent))
        for name in ("data", "data-copy"):
            self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/" + name, self.cluster.objects)
        self.nodes[0]["allocatable"]["memory"] = "8Gi"
        self.cutover()

    def test_capacity_lost_after_cutover_holds_restart_and_keeps_old_copy(self):
        self.copy(); self.cutover()
        self.nodes[0]["allocatable"]["memory"] = "512Mi"
        before = len(self.cluster.sent)
        with self.assertRaises(journal.Held): self.cluster.restart(self.restarts)
        self.assertEqual(before, len(self.cluster.sent))
        original = self.cluster.objects["/api/v1/persistentvolumes/pv-data"]
        self.assertEqual("Retain", original["spec"]["persistentVolumeReclaimPolicy"])
        self.nodes[0]["allocatable"]["memory"] = "8Gi"
        self.assertEqual(93, self.cluster.restart(self.restarts)[1])
        self.assertEqual(96, self.cluster.restart(self.restarts)[1])
        dep = self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 1,
                         "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}
        self.assertEqual("succeeded", self.cluster.restart(self.restarts)[0])
