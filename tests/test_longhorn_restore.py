import sys
import unittest
import urllib.error
import copy
from unittest import mock
from unittest.mock import patch
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as KUBE
import homestead_longhorn as longhorn
import homestead_longhorn_backups as BACKUPS
import homestead_longhorn_restore as RESTORE
import homestead_csi_restore as CSI_RESTORE
from fakes.kube import Kube
from fakes.storage_classes import storage_classes


class RestoreClassTests(unittest.TestCase):
    """A class that makes a volume only once an app mounts it cannot take a
    restore: the claim is left Pending, and a restore has to finish before
    anything mounts it. The review says so and asks for another class."""
    BACKUP = "backup-123"

    def setUp(self):
        storage_classes(self, "longhorn-r2")
        self.kube = self.enterContext(Kube(groups=("longhorn.io", "storage.k8s.io")))
        self.enterContext(mock.patch.multiple(longhorn, kget=KUBE.kget, ksend=KUBE.ksend))
        self.kube.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "lab"}})
        for name, mode in (("longhorn-r2", "Immediate"), ("longhorn-late", "WaitForFirstConsumer"), ("longhorn-plain", None)):
            self.kube.add({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": name},
                           "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "2"},
                           **({"volumeBindingMode": mode} if mode else {})})
        self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
                       "metadata": {"name": self.BACKUP, "namespace": "longhorn-system"},
                       "status": {"state": "Completed", "url": "nfs://backup/vol?backup=backup-123", "volumeName": "source-vol",
                                  "volumeSize": str(5 * 1024 ** 3), "backupCreatedAt": "2026-09-20T01:02:03Z"}})

    def test_the_review_offers_only_classes_a_restore_can_use_and_says_why_not_the_others(self):
        plan = RESTORE.restore_plan(self.BACKUP, "lab", "restored-data")
        self.assertEqual({"longhorn-r2", "longhorn-plain"}, set(plan["storage_classes"]))
        self.assertIn("once an app mounts it", plan["refused_classes"]["longhorn-late"])

    def test_a_restore_onto_it_is_refused_before_anything_is_made(self):
        with self.assertRaisesRegex(ValueError, "longhorn-late.*once an app mounts it.*choose another storage class"):
            RESTORE.restore_backup({"backup": self.BACKUP, "namespace": "lab", "name": "restored-data",
                                    "storage_class": "longhorn-late"})
        self.assertEqual([], self.kube.writes)

    def test_a_cross_cluster_move_keeps_its_own_refusal(self):
        late = KUBE.kget("/apis/storage.k8s.io/v1/storageclasses/longhorn-late")
        with self.assertRaisesRegex(ValueError, "Migrated volumes need an Immediate"):
            RESTORE.validate_restore_class(late, migration=True)

    def test_the_refusal_for_a_volume_mode_names_a_migratable_class_for_filesystem_only(self):
        self.kube.add({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "longhorn-rwx"},
                       "provisioner": "driver.longhorn.io", "parameters": {"migratable": "true"}})
        migratable = KUBE.kget("/apis/storage.k8s.io/v1/storageclasses/longhorn-rwx")
        self.assertIn("migratable disabled", RESTORE.class_refusal(migratable, "Filesystem"))
        self.assertEqual("", RESTORE.class_refusal(migratable, "Block"))
        with self.assertRaisesRegex(ValueError, "migratable disabled"):
            RESTORE.validate_restore_class(migratable, "Filesystem")

    def agent_plan(self):
        import homestead_agent_tools as TOOLS
        answer = lambda method, path, query, **_: RESTORE.restore_plan(query["backup"], query["ns"], query["name"])
        with mock.patch.object(TOOLS, "call", answer), mock.patch.object(TOOLS, "_as", lambda ctx: {}):
            return TOOLS, TOOLS._plan_restore({"backup": self.BACKUP, "namespace": "lab", "name": "restored-data"}, {})

    def test_the_agent_tool_says_so_at_plan_time_when_the_default_class_is_refused(self):
        storage_classes(self, "longhorn-late")
        import homestead_agent_tools as TOOLS
        with self.assertRaisesRegex(TOOLS.ToolError, "longhorn-late.*once an app mounts it"):
            self.agent_plan()

    def test_the_agent_tool_plans_a_restore_on_a_default_class_that_can_take_it(self):
        _, (shown, _body) = self.agent_plan()
        self.assertEqual("longhorn-r2", shown["plan"]["storage_class"])

    def test_a_class_that_binds_at_once_is_not_refused(self):
        for name in ("longhorn-r2", "longhorn-plain"):
            RESTORE.validate_restore_class(KUBE.kget(f"/apis/storage.k8s.io/v1/storageclasses/{name}"))


class LonghornRestoreTests(unittest.TestCase):
    def setUp(self):
        storage_classes(self, "longhorn-r2")   # the class new volumes go on here
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
        self.objects["/apis/snapshot.storage.k8s.io/v1"] = {"resources": [{"name": x} for x in CSI_RESTORE.CRDS]}
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

        saved = (longhorn.kget, longhorn.ksend, longhorn._cache, longhorn.DEFAULT_NS)
        self.addCleanup(lambda: longhorn.bind(*saved))   # put back what server.py bound
        longhorn.bind(get, send, {})

    def test_plan_reports_minimum_size_and_existing_pvc_conflict(self):
        plan = RESTORE.restore_plan("backup-123", "lab", "restored-data")
        self.assertEqual(5, plan["minimum_size_gb"])
        self.assertIsNone(plan["conflict"])
        self.assertNotIn("url", plan)
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"] = {
            "metadata": {"name": "restored-data"}}
        conflict = RESTORE.restore_plan("backup-123", "lab", "restored-data")
        self.assertEqual("PersistentVolumeClaim", conflict["conflict"]["kind"])

    def test_backup_inventory_marks_restore_readiness_without_exposing_url(self):
        backup = self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123"]
        self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups"
        ] = {"items": [backup]}
        row = BACKUPS.backups()[0]
        self.assertTrue(row["restorable"])
        self.assertEqual(5, row["volume_size_gb"])
        self.assertNotIn("url", row)

    def test_plan_rejects_malformed_kubernetes_names(self):
        with self.assertRaisesRegex(ValueError, "PVC name"):
            RESTORE.restore_plan("backup-123", "lab", "bad..name")

    def restore(self, **extra):
        return RESTORE.restore_backup({"backup": "backup-123", "namespace": "lab", "name": "restored-data", **extra})

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

    def test_a_reviewed_restore_whose_claim_cannot_be_made_removes_its_snapshot_again(self):
        """No job follows a refused first create: nothing else would remove them."""
        original = longhorn.ksend
        def fail(method, path, body=None, **kwargs):
            if path.endswith("/persistentvolumeclaims"):
                raise urllib.error.HTTPError(path, 422, "invalid", {}, None)
            return original(method, path, body, **kwargs)
        with patch.object(longhorn, "ksend", fail):
            with self.assertRaises(urllib.error.HTTPError):
                RESTORE.restore_backup({"backup": "backup-123", "namespace": "lab", "name": "restored-data",
                                        "restore_id": "attempt-one"}, discard_on_failure=True)
        deletes = [path for method, path, _, _ in self.sent if method == "DELETE"]
        self.assertEqual(2, len(deletes))
        self.assertFalse(any("volumesnapshot" in path for path in self.objects if "homestead-restore-" in path))
        self.assertIn("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/backups/backup-123", self.objects)

    def test_dismissed_failed_attempt_and_new_attempt_use_distinct_metadata(self):
        first = self.restore(restore_id="attempt-one")
        self.objects.pop("/api/v1/namespaces/lab/persistentvolumeclaims/restored-data")
        second = self.restore(restore_id="attempt-two")
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(first["storage_class"], second["storage_class"])

    def test_foreign_snapshot_or_changed_handle_is_never_adopted(self):
        result = self.restore()
        self.objects.pop("/api/v1/namespaces/lab/persistentvolumeclaims/restored-data")
        content = self.objects[CSI_RESTORE.API + "/volumesnapshotcontents/" + result["snapshot"]]
        content["spec"]["source"]["snapshotHandle"] = "bak://other/backup-other"
        with self.assertRaisesRegex(ValueError, "different settings"):
            self.restore()
        content["spec"]["source"]["snapshotHandle"] = "bak://source-vol/backup-123"
        content["metadata"]["labels"] = {}
        with self.assertRaisesRegex(ValueError, "different settings"):
            self.restore()

    def test_snapshot_error_is_reported_while_pvc_is_pending(self):
        result = self.restore()
        snapshot = self.objects[CSI_RESTORE.API + "/namespaces/lab/volumesnapshots/" + result["snapshot"]]
        snapshot["status"] = {"error": {"message": "backup is unavailable"}}
        pvc = self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"]
        self.assertEqual("backup is unavailable", RESTORE.restore_problem(pvc))

    def test_cleanup_only_removes_bound_owned_retained_metadata_using_identity_preconditions(self):
        result = self.restore()
        prefix = CSI_RESTORE.API
        content = self.objects[prefix + "/volumesnapshotcontents/" + result["snapshot"]]
        self.objects[prefix + "/volumesnapshotcontents?labelSelector=homestead.io/restore-snapshot%3Dtrue"] = {"items": [content]}
        RESTORE.cleanup_restore_snapshots()
        self.assertFalse(any(method == "DELETE" for method, _, _, _ in self.sent))
        pvc = self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"]
        pvc["status"] = {"phase": "Bound"}
        content["spec"]["deletionPolicy"] = "Delete"
        RESTORE.cleanup_restore_snapshots()
        self.assertFalse(any(method == "DELETE" for method, _, _, _ in self.sent))
        content["spec"]["deletionPolicy"] = "Retain"
        RESTORE.cleanup_restore_snapshots()
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
        with patch.object(CSI_RESTORE, "ensure_support", return_value={"ready": False, "message": "Installing CSI snapshots"}):
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
