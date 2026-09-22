import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as longhorn


class LonghornRestoreTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/api/v1/namespaces/lab": {"metadata": {"name": "lab"}},
            "/apis/storage.k8s.io/v1/storageclasses/longhorn-r2": {
                "metadata": {"name": "longhorn-r2"},
                "provisioner": "driver.longhorn.io",
                "parameters": {"numberOfReplicas": "2", "staleReplicaTimeout": "30",
                               "fsType": "ext4"},
            },
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123": {
                "metadata": {"name": "backup-123", "labels": {"backup-volume": "source-vol"}},
                "status": {"state": "Completed", "url": "nfs://backup/vol?backup=backup-123",
                           "volumeName": "source-vol", "volumeSize": str(5 * 1024 ** 3),
                           "size": str(900 * 1024 ** 2), "backupTargetName": "default",
                           "backupCreatedAt": "2026-09-20T01:02:03Z"},
            },
        }
        self.sent = []

        def get(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return self.objects[path]

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body, kwargs))
            if path == "/apis/storage.k8s.io/v1/storageclasses":
                self.objects[path + "/" + body["metadata"]["name"]] = body
            if path.endswith("/persistentvolumeclaims"):
                return {"metadata": {"name": body["metadata"]["name"], "uid": "pvc-uid"}}
            return body or {}

        longhorn.bind(get, send, {}, "longhorn-r2")

    def test_plan_reports_minimum_size_and_existing_pvc_conflict(self):
        plan = longhorn.restore_plan("backup-123", "lab", "restored-data")
        self.assertEqual(5, plan["minimum_size_gb"])
        self.assertIsNone(plan["conflict"])
        self.assertNotIn("url", plan)
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"] = {
            "metadata": {"name": "restored-data"}}
        conflict = longhorn.restore_plan("backup-123", "lab", "restored-data")
        self.assertEqual("PersistentVolumeClaim", conflict["conflict"]["kind"])

    def test_backup_inventory_marks_restore_readiness_without_exposing_url(self):
        backup = self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123"]
        self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups"
        ] = {"items": [backup]}
        row = longhorn.backups()[0]
        self.assertTrue(row["restorable"])
        self.assertEqual(5, row["volume_size_gb"])
        self.assertNotIn("url", row)

    def test_plan_rejects_malformed_kubernetes_names(self):
        with self.assertRaisesRegex(ValueError, "PVC name"):
            longhorn.restore_plan("backup-123", "lab", "bad..name")

    def test_restore_creates_reusable_csi_class_and_new_pvc(self):
        result = longhorn.restore_backup({
            "backup": "backup-123", "namespace": "lab", "name": "restored-data",
            "size_gb": 8, "replicas": 3, "access_mode": "ReadWriteMany",
        })
        self.assertEqual("restored-data", result["name"])
        sc = next(body for method, path, body, _ in self.sent
                  if path == "/apis/storage.k8s.io/v1/storageclasses")
        self.assertEqual("driver.longhorn.io", sc["provisioner"])
        self.assertEqual("3", sc["parameters"]["numberOfReplicas"])
        self.assertEqual("false", sc["parameters"]["migratable"])
        self.assertEqual("nfs://backup/vol?backup=backup-123", sc["parameters"]["fromBackup"])
        pvc = next(body for method, path, body, _ in self.sent if path.endswith("persistentvolumeclaims"))
        self.assertEqual(["ReadWriteMany"], pvc["spec"]["accessModes"])
        self.assertEqual("8Gi", pvc["spec"]["resources"]["requests"]["storage"])
        self.assertEqual(result["storage_class"], pvc["spec"]["storageClassName"])

    def test_restore_refuses_smaller_destination(self):
        with self.assertRaisesRegex(ValueError, "cannot be smaller"):
            longhorn.restore_backup({"backup": "backup-123", "namespace": "lab",
                                     "name": "restored-data", "size_gb": 4})
        self.assertFalse(any(path.endswith("persistentvolumeclaims") for _, path, _, _ in self.sent))

    def test_restore_refuses_incomplete_backup(self):
        self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123"
        ]["status"]["state"] = "InProgress"
        with self.assertRaisesRegex(ValueError, "not ready"):
            longhorn.restore_plan("backup-123", "lab", "restored-data")

    def test_restore_refuses_foreign_conflicting_storage_class(self):
        _, url, _ = longhorn._restore_source(self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123"])
        class_name = longhorn._restore_class_name(url, 2)
        self.objects[f"/apis/storage.k8s.io/v1/storageclasses/{class_name}"] = {
            "metadata": {"name": class_name}, "provisioner": "driver.longhorn.io",
            "parameters": {"fromBackup": "different"}}
        with self.assertRaisesRegex(ValueError, "different settings"):
            longhorn.restore_backup({"backup": "backup-123", "namespace": "lab",
                                     "name": "restored-data", "size_gb": 5})


if __name__ == "__main__":
    unittest.main()
