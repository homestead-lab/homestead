import sys, unittest, urllib.error
import copy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server

PVC = "/api/v1/namespaces/lab/persistentvolumeclaims/example-data"
LH_VOLUME = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-123"
SC = "/apis/storage.k8s.io/v1/storageclasses/example-storage"


class VolumeEditTests(unittest.TestCase):
    """Growing a volume said "404 page not found": the replica count went to
    a Longhorn path without its namespace, after the size had changed."""

    def setUp(self):
        self.saved = server.kget, server.ksend
        self.sent = []
        self.replicas = 1
        self.pvc = {"spec": {"storageClassName": "example-storage", "volumeName": "pvc-123",
                             "resources": {"requests": {"storage": "5Gi"}}}, "status": {"phase": "Bound"}}
        self.storage_class = {"allowVolumeExpansion": True}
        self.class_error = None
        self.objects = {}

        def kget(path):
            if path == PVC:
                return self.pvc
            if path == SC:
                if self.class_error:
                    raise urllib.error.HTTPError(path, self.class_error, "lookup failed", None, None)
                return self.storage_class
            if path == LH_VOLUME:
                return {"spec": {"numberOfReplicas": self.replicas}}
            if path in self.objects:
                return self.objects[path]
            raise urllib.error.HTTPError(path, 404, "page not found", None, None)

        def ksend(method, path, body=None, **kw):
            if path not in (PVC, LH_VOLUME, "/apis/storage.k8s.io/v1/storageclasses"):
                raise urllib.error.HTTPError(path, 404, "page not found", None, None)
            self.sent.append((method, path, body))
            return body

        server.kget, server.ksend = kget, ksend

    def tearDown(self):
        server.kget, server.ksend = self.saved

    def test_growing_sends_only_the_new_size(self):
        result = server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 10, "replicas": 1})

        self.assertEqual([("PATCH", PVC, {"spec": {"resources": {"requests": {"storage": "10Gi"}}}})], self.sent)
        self.assertIn("growing to 10 GB", result["detail"])

    def test_the_replica_count_goes_to_longhorns_namespace(self):
        server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 5, "replicas": 3})

        self.assertEqual([("PATCH", LH_VOLUME, {"spec": {"numberOfReplicas": 3}})], self.sent)

    def test_a_smaller_size_is_refused_before_anything_changes(self):
        with self.assertRaisesRegex(ValueError, "grow but not shrink"):
            server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 2, "replicas": 3})
        self.assertEqual([], self.sent)

    def test_nothing_changed_sends_nothing(self):
        result = server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 5, "replicas": 1})

        self.assertEqual([], self.sent)
        self.assertIn("unchanged", result["detail"])

    def test_disabled_expansion_blocks_all_changes(self):
        self.storage_class["allowVolumeExpansion"] = False
        with self.assertRaisesRegex(ValueError, "does not allow volume expansion"):
            server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 10, "replicas": 3})
        self.assertEqual([], self.sent)

    def test_replica_only_change_still_works_without_expansion(self):
        self.storage_class = {}
        server.edit_volume({"namespace": "lab", "name": "example-data", "replicas": 3})
        self.assertEqual([("PATCH", LH_VOLUME, {"spec": {"numberOfReplicas": 3}})], self.sent)

    def test_missing_class_unbound_and_unreadable_are_explained(self):
        for phase, class_name, error, reason in [
            ("Pending", "example-storage", None, "must be bound"),
            ("Bound", "", None, "no StorageClass"),
            ("Bound", "example-storage", 404, "was removed"),
            ("Bound", "example-storage", 403, "Could not check"),
        ]:
            with self.subTest(reason=reason):
                self.pvc["status"]["phase"] = phase
                self.pvc["spec"]["storageClassName"] = class_name
                self.class_error = error
                options = server.volume_edit_options("lab", "example-data")
                self.assertFalse(options["can_expand"])
                self.assertIn(reason, options["reason"])
                with self.assertRaisesRegex(ValueError, reason):
                    server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 10})
                self.assertEqual([], self.sent)

    def test_save_rechecks_after_class_changes(self):
        self.assertTrue(server.volume_edit_options("lab", "example-data")["can_expand"])
        self.storage_class["allowVolumeExpansion"] = False
        with self.assertRaisesRegex(ValueError, "does not allow"):
            server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 10})
        self.assertEqual([], self.sent)

    def test_expansion_does_not_require_provisioner_annotations(self):
        options = server.volume_edit_options("lab", "example-data")
        self.assertTrue(options["can_expand"])
        self.assertEqual(5, options["requested_gb"])
        self.assertEqual([], self.sent)

    def test_legacy_storage_class_annotation_is_supported(self):
        self.pvc["spec"].pop("storageClassName")
        self.pvc["metadata"] = {"annotations": {"volume.beta.kubernetes.io/storage-class": "example-storage"}}
        self.assertTrue(server.volume_edit_options("lab", "example-data")["can_expand"])

    def test_invalid_replicas_are_rejected_before_growing(self):
        with self.assertRaisesRegex(ValueError, "replica count"):
            server.edit_volume({"namespace": "lab", "name": "example-data", "size_gb": 10, "replicas": 9})
        self.assertEqual([], self.sent)

    def prepare_restore_repair(self):
        self.class_name = "homestead-restore-0123456789abcdef"
        self.pvc["spec"]["storageClassName"] = self.class_name
        self.pvc["metadata"] = {"name": "example-data", "namespace": "lab", "uid": "claim-uid",
                                "labels": {"app.kubernetes.io/managed-by": "homestead", "homestead.io/restored-volume": "true"},
                                "annotations": {"homestead.io/restored-from-backup": "example-backup"}}
        self.objects["/api/v1/persistentvolumeclaims"] = {"items": [self.pvc]}
        self.pv = {"spec": {"storageClassName": self.class_name, "claimRef": {"name": "example-data", "namespace": "lab", "uid": "claim-uid"},
                            "persistentVolumeReclaimPolicy": "Retain", "csi": {"driver": "driver.longhorn.io", "fsType": "ext4",
                                "volumeAttributes": {"fromBackup": "s3://example/backup", "diskSelector": "hdd", "numberOfReplicas": "2", "share": "true",
                                                     "storage.kubernetes.io/csiProvisionerIdentity": "example-controller"}}}, "status": {"phase": "Bound"}}
        self.objects["/api/v1/persistentvolumes/pvc-123"] = self.pv
        return {"namespace": "lab", "name": "example-data", "confirm": self.class_name}

    def test_missing_restore_class_can_be_recreated_without_touching_data(self):
        cfg = self.prepare_restore_repair()
        self.assertTrue(server.volume_edit_options("lab", "example-data")["repair_class"])
        self.assertEqual([], self.sent)
        server.repair_volume_class(cfg)
        self.assertEqual(1, len(self.sent))
        method, path, body = self.sent[0]
        self.assertEqual(("POST", "/apis/storage.k8s.io/v1/storageclasses"), (method, path))
        self.assertEqual(self.class_name, body["metadata"]["name"])
        self.assertTrue(body["allowVolumeExpansion"])
        self.assertEqual("Retain", body["reclaimPolicy"])
        self.assertEqual({"fromBackup": "s3://example/backup", "diskSelector": "hdd", "numberOfReplicas": "2", "fsType": "ext4"}, body["parameters"])

    def test_resize_support_repair_is_an_admin_action(self):
        self.assertEqual("admin", server.ROUTE_POLICY.role("/api/volumes/repair-class", "POST"))

    def test_restore_repair_refuses_unrelated_or_changed_volumes(self):
        for case in ("unmanaged", "wrong_uid", "other_driver", "pending", "missing_backup", "incomplete_inventory", "no_review"):
            with self.subTest(case=case):
                cfg = self.prepare_restore_repair()
                if case == "unmanaged": self.pvc["metadata"]["labels"] = {}
                if case == "wrong_uid": self.pv["spec"]["claimRef"]["uid"] = "replacement-uid"
                if case == "other_driver": self.pv["spec"]["csi"]["driver"] = "example.io"
                if case == "pending": self.pvc["status"]["phase"] = "Pending"
                if case == "missing_backup": self.pv["spec"]["csi"]["volumeAttributes"].pop("fromBackup")
                if case == "incomplete_inventory": self.objects["/api/v1/persistentvolumeclaims"]["metadata"] = {"continue": "next"}
                if case == "no_review": cfg["confirm"] = ""
                with self.assertRaises(ValueError): server.repair_volume_class(cfg)
                self.assertEqual([], self.sent)
                self.pvc["status"]["phase"] = "Bound"

    def test_repair_refuses_existing_class_and_conflicting_shared_parameters(self):
        cfg = self.prepare_restore_repair()
        self.objects["/apis/storage.k8s.io/v1/storageclasses/" + self.class_name] = {"allowVolumeExpansion": False}
        with self.assertRaisesRegex(ValueError, "already exists"):
            server.repair_volume_class(cfg)
        self.objects.pop("/apis/storage.k8s.io/v1/storageclasses/" + self.class_name)
        other_claim, other_pv = copy.deepcopy(self.pvc), copy.deepcopy(self.pv)
        other_claim["metadata"].update(name="example-other", uid="other-uid")
        other_claim["spec"]["volumeName"] = "pvc-other"
        other_pv["spec"]["claimRef"].update(name="example-other", uid="other-uid")
        other_pv["spec"]["csi"]["volumeAttributes"]["diskSelector"] = "ssd"
        self.objects["/api/v1/persistentvolumeclaims"]["items"].append(other_claim)
        self.objects["/api/v1/persistentvolumes/pvc-other"] = other_pv
        with self.assertRaisesRegex(ValueError, "different storage settings"):
            server.repair_volume_class(cfg)
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
