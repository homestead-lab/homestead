import sys, unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_kube as K
import homestead_storage_classes as STORAGE_CLASSES
from fakes.kube import Kube

CLAIMS = "/api/v1/persistentvolumeclaims"


def klass(name, default=False, **parameters):
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": name, "annotations": (
                {"storageclass.kubernetes.io/is-default-class": "true"} if default else {})},
            "provisioner": "driver.longhorn.io", "parameters": parameters}


class ClassChoiceTests(unittest.TestCase):
    """Pickers offered every image's and every restore's class, and making
    another class the default in Volumes changed nothing Homestead chose.
    On the shared fake API (tests/fakes/kube.py)."""

    def setUp(self):
        # STORAGE_CLASSES reads the cluster through the shared client here, whatever another test bound.
        for name in ("kget", "ksend"):
            self.addCleanup(setattr, STORAGE_CLASSES, name, getattr(STORAGE_CLASSES, name))
            setattr(STORAGE_CLASSES, name, getattr(K, name))
        self.saved = server.STORAGE_CLASS, server.LC.STORAGE_CLASS, server.LH.STORAGE_CLASS
        self.kube = self.enterContext(Kube(
            klass("longhorn-r2"), klass("longhorn-v1-1x", default=True, numberOfReplicas="1"),
            klass("lh-8b6ca866-806d-430a-8fd4-d584f0f06128", backingImage="default-image-151065"),
            klass("longhorn-image-ubuntu", backingImage="default-image-ubuntu"),
            klass("homestead-restore-155ade32ea106399", fromBackup="s3://b/backups?volume=x")))

    def tearDown(self):
        server.STORAGE_CLASS, server.LC.STORAGE_CLASS, server.LH.STORAGE_CLASS = self.saved

    def claim(self, name, status, **meta):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": "lab", "name": name, **meta.pop("metadata", {})}, **meta,
                       "status": status})

    def sent(self):
        return [(method, path) for method, path, _, _ in self.kube.writes]

    def test_image_and_restore_classes_are_never_offered(self):
        self.assertEqual(["longhorn-v1-1x", "longhorn-r2"], server.selectable_storage_classes())

    def test_the_clusters_default_is_homesteads_default(self):
        server.storage_classes()
        self.assertEqual("longhorn-v1-1x", server.STORAGE_CLASS)
        self.assertEqual("longhorn-v1-1x", server.LC.STORAGE_CLASS)

    def test_a_vm_disk_follows_the_one_rule(self):
        # The install class while it is here (#1125); else the cluster's default.
        with mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", "longhorn-r2"):
            self.assertEqual("longhorn-r2", server.VM_ROUTES.vm_default_class())
        with mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", ""):
            self.assertEqual("longhorn-v1-1x", server.VM_ROUTES.vm_default_class())

    def test_without_a_default_the_installed_class_stands(self):
        self.kube.add(klass("longhorn-v1-1x", numberOfReplicas="1"))
        server.storage_classes()
        self.assertEqual("longhorn-r2", server.STORAGE_CLASS)

    def test_spent_restore_classes_are_removed_and_needed_ones_kept(self):
        self.kube.add(klass("homestead-restore-deee238ba869b8e0", fromBackup="s3://b/backups?volume=y"))
        self.claim("pending", {"phase": "Pending"}, spec={"storageClassName": "homestead-restore-deee238ba869b8e0"})
        self.claim("bound", {"phase": "Bound"}, spec={"storageClassName": "homestead-restore-155ade32ea106399"})
        self.kube.add(klass("homestead-restore-unused", fromBackup="s3://b/backups?volume=z"))
        removed = STORAGE_CLASSES.cleanup_restore_classes()
        self.assertEqual(["homestead-restore-unused"], removed)
        self.assertEqual([("DELETE", "/apis/storage.k8s.io/v1/storageclasses/homestead-restore-unused")], self.sent())

    def test_legacy_claim_and_terminating_claim_keep_their_class(self):
        self.claim("legacy", {"phase": "Bound"}, metadata={
            "deletionTimestamp": "2026-01-01T00:00:00Z", "finalizers": ["kubernetes.io/pvc-protection"],
            "annotations": {"volume.beta.kubernetes.io/storage-class": "homestead-restore-155ade32ea106399"}})
        self.assertEqual([], STORAGE_CLASSES.cleanup_restore_classes())
        self.assertEqual([], self.sent())

    def test_failed_claim_inventory_never_deletes_classes(self):
        self.kube.fail(CLAIMS, 403)
        self.assertEqual([], STORAGE_CLASSES.cleanup_restore_classes())
        self.assertEqual([], self.sent())

    def test_incomplete_claim_inventory_never_deletes_classes(self):
        self.kube.react("GET", CLAIMS, lambda *_: {"items": [], "metadata": {"continue": "next-page"}})
        self.assertEqual([], STORAGE_CLASSES.cleanup_restore_classes())
        self.assertEqual([], self.sent())

    def test_retained_backing_volume_keeps_its_restore_class_without_a_claim(self):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "pv-kept"},
                       "spec": {"storageClassName": "homestead-restore-155ade32ea106399",
                                "persistentVolumeReclaimPolicy": "Retain"}, "status": {"phase": "Released"}})
        self.assertEqual([], STORAGE_CLASSES.cleanup_restore_classes())
        self.assertEqual([], self.sent())


if __name__ == "__main__":
    unittest.main()
