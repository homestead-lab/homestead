import sys
import unittest
import urllib.error
import copy
from unittest.mock import patch
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as longhorn


class LonghornRestoreTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/api/v1/namespaces/lab": {"metadata": {"name": "lab"}},
            "/apis/storage.k8s.io/v1/storageclasses/longhorn-r2": {
                "metadata": {"name": "longhorn-r2"},
                "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True,
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
        self.objects["/apis/storage.k8s.io/v1/storageclasses"] = {"items": [self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"]]}
        self.objects["/apis/snapshot.storage.k8s.io/v1"] = {"resources": [{"name": x} for x in longhorn.CSI_RESTORE.CRDS]}
        self.objects["/apis/apps/v1/deployments"] = {"items": [{"metadata": {"generation": 1},
            "spec": {"template": {"spec": {"containers": [{"image": "registry.k8s.io/sig-storage/snapshot-controller:v8.2.0"}]}}},
            "status": {"availableReplicas": 1, "observedGeneration": 1}},
            {"metadata": {"namespace": "longhorn-system"}, "status": {"availableReplicas": 1},
             "spec": {"template": {"spec": {"containers": [{"image": "registry.k8s.io/sig-storage/csi-snapshotter:v8.2.0"}]}}}}]}
        self.sent = []

        def get(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return self.objects[path]

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body, kwargs))
            if method == "POST":
                out = copy.deepcopy(body)
                out["metadata"].update(uid="uid-" + out["metadata"]["name"], resourceVersion="1")
                self.objects[path + "/" + body["metadata"]["name"]] = out
                return out
            if method == "DELETE":
                self.objects.pop(path, None)
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

    def restore(self, **extra):
        return longhorn.restore_backup({"backup": "backup-123", "namespace": "lab", "name": "restored-data", **extra})

    def test_restore_uses_normal_class_and_retained_backup_snapshot(self):
        original = copy.deepcopy(self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"])
        result = self.restore(size_gb=8, access_mode="ReadWriteMany")
        self.assertEqual("longhorn-r2", result["storage_class"])
        self.assertEqual(original, self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"])
        self.assertFalse(any(path.endswith("/storageclasses") for _, path, _, _ in self.sent))
        content, snapshot, pvc = [body for _, _, body, _ in self.sent]
        self.assertEqual("Retain", content["spec"]["deletionPolicy"])
        self.assertEqual("bak://source-vol/backup-123", content["spec"]["source"]["snapshotHandle"])
        self.assertEqual(content["metadata"]["name"], snapshot["spec"]["source"]["volumeSnapshotContentName"])
        self.assertEqual(snapshot["metadata"]["name"], pvc["spec"]["dataSource"]["name"])
        self.assertEqual("snapshot.storage.k8s.io", pvc["spec"]["dataSource"]["apiGroup"])
        self.assertEqual(["ReadWriteMany"], pvc["spec"]["accessModes"])
        self.assertEqual(str(5 * 1024 ** 3), pvc["spec"]["resources"]["requests"]["storage"])

    def test_restore_refuses_smaller_destination_before_any_writes(self):
        with self.assertRaisesRegex(ValueError, "cannot be smaller"):
            self.restore(size_gb=4)
        self.assertEqual([], self.sent)

    def test_chosen_configured_class_is_used_without_copying_or_changing_it(self):
        original = {"metadata": {"name": "fast"}, "provisioner": "driver.longhorn.io",
            "parameters": {"numberOfReplicas": "3", "diskSelector": "ssd", "nodeSelector": "storage", "fsType": "xfs"},
            "mountOptions": ["noatime"], "allowedTopologies": [{"matchLabelExpressions": []}]}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/fast"] = copy.deepcopy(original)
        self.restore(storage_class="fast", size_gb=5)
        self.assertEqual("fast", self.sent[-1][2]["spec"]["storageClassName"])
        self.assertEqual(original, self.objects["/apis/storage.k8s.io/v1/storageclasses/fast"])

    def test_restore_refuses_incomplete_backup(self):
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123"]["status"]["state"] = "InProgress"
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.restore()
        self.assertEqual([], self.sent)

    def test_reconstructed_legacy_class_is_untouched_by_new_restore(self):
        repaired = {"metadata": {"name": "homestead-restore-0123456789abcdef"}, "parameters": {"fromBackup": "old"}}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/homestead-restore-0123456789abcdef"] = repaired
        self.restore()
        self.assertEqual("longhorn-r2", self.sent[-1][2]["spec"]["storageClassName"])
        self.assertEqual({"fromBackup": "old"}, repaired["parameters"])

    def test_retry_after_pvc_create_failure_reuses_snapshot_metadata(self):
        original = longhorn.ksend
        def fail(method, path, body=None, **kwargs):
            if path.endswith("/persistentvolumeclaims"):
                raise urllib.error.HTTPError(path, 503, "temporary", {}, None)
            return original(method, path, body, **kwargs)
        with patch.object(longhorn, "ksend", fail):
            with self.assertRaises(urllib.error.HTTPError):
                self.restore(restore_id="attempt-one")
        self.restore(restore_id="attempt-one")
        self.assertEqual(1, sum(path.endswith("/volumesnapshotcontents") for _, path, _, _ in self.sent))
        self.assertEqual(1, sum(path.endswith("/volumesnapshots") for _, path, _, _ in self.sent))

    def test_dismissed_failed_attempt_and_new_attempt_use_distinct_metadata(self):
        first = self.restore(restore_id="attempt-one")
        self.objects.pop("/api/v1/namespaces/lab/persistentvolumeclaims/restored-data")
        second = self.restore(restore_id="attempt-two")
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(first["storage_class"], second["storage_class"])

    def test_foreign_snapshot_or_changed_handle_is_never_adopted(self):
        result = self.restore()
        self.objects.pop("/api/v1/namespaces/lab/persistentvolumeclaims/restored-data")
        content = self.objects[longhorn.CSI_RESTORE.API + "/volumesnapshotcontents/" + result["snapshot"]]
        content["spec"]["source"]["snapshotHandle"] = "bak://other/backup-other"
        with self.assertRaisesRegex(ValueError, "different settings"):
            self.restore()
        content["spec"]["source"]["snapshotHandle"] = "bak://source-vol/backup-123"
        content["metadata"]["labels"] = {}
        with self.assertRaisesRegex(ValueError, "different settings"):
            self.restore()

    def test_snapshot_error_is_reported_while_pvc_is_pending(self):
        result = self.restore()
        snapshot = self.objects[longhorn.CSI_RESTORE.API + "/namespaces/lab/volumesnapshots/" + result["snapshot"]]
        snapshot["status"] = {"error": {"message": "backup is unavailable"}}
        pvc = self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"]
        self.assertEqual("backup is unavailable", longhorn.restore_problem(pvc))

    def test_cleanup_only_removes_bound_owned_retained_metadata_using_identity_preconditions(self):
        result = self.restore()
        prefix = longhorn.CSI_RESTORE.API
        content = self.objects[prefix + "/volumesnapshotcontents/" + result["snapshot"]]
        self.objects[prefix + "/volumesnapshotcontents?labelSelector=homestead.io/restore-snapshot%3Dtrue"] = {"items": [content]}
        longhorn.cleanup_restore_snapshots()
        self.assertFalse(any(method == "DELETE" for method, _, _, _ in self.sent))
        pvc = self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"]
        pvc["status"] = {"phase": "Bound"}
        content["spec"]["deletionPolicy"] = "Delete"
        longhorn.cleanup_restore_snapshots()
        self.assertFalse(any(method == "DELETE" for method, _, _, _ in self.sent))
        content["spec"]["deletionPolicy"] = "Retain"
        longhorn.cleanup_restore_snapshots()
        deletes = [(path, body) for method, path, body, _ in self.sent if method == "DELETE"]
        self.assertEqual(2, len(deletes))
        self.assertTrue(all("preconditions" in body for _, body in deletes))
        self.assertTrue(all("volumesnapshot" in path for path, _ in deletes))
        self.assertIn("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123", self.objects)

    def test_class_compatibility_rejects_image_restore_and_filesystem_migratable_classes(self):
        klass = self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"]
        for key in ("fromBackup", "backingImage"):
            klass["parameters"][key] = "special"
            with self.assertRaisesRegex(ValueError, "regular"):
                self.restore()
            klass["parameters"].pop(key)
        klass["parameters"]["migratable"] = "true"
        with self.assertRaisesRegex(ValueError, "migratable disabled"):
            self.restore()
        self.assertEqual([], self.sent)

    def test_block_vm_restore_preserves_the_class_and_prepared_backing_image(self):
        klass = self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"]
        klass["parameters"]["migratable"] = "true"
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backingimages/example-image"] = {"metadata": {"name": "example-image"}}
        self.restore(volume_mode="Block", backing_image="example-image")
        self.assertEqual("Block", self.sent[-1][2]["spec"]["volumeMode"])
        self.assertEqual("Block", self.sent[0][2]["spec"]["sourceVolumeMode"])
        self.assertEqual("true", klass["parameters"]["migratable"])
        self.assertNotIn("backingImage", klass["parameters"])

    def test_dependency_installation_waits_without_creating_snapshot_or_pvc(self):
        with patch.object(longhorn.CSI_RESTORE, "ensure_support", return_value={"ready": False, "message": "Installing CSI snapshots"}):
            result = self.restore()
        self.assertFalse(result["created"])
        self.assertEqual([], self.sent)

    def test_existing_destination_is_never_replaced(self):
        self.restore()
        before = len(self.sent)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.restore()
        self.assertEqual(before, len(self.sent))


if __name__ == "__main__":
    unittest.main()
