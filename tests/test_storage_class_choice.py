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
        # Installed with the Helm chart, which names no class (#1125).
        self.enterContext(mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", ""))
        self.kube = self.enterContext(Kube(
            klass("longhorn-r2"), klass("longhorn-v1-1x", default=True, numberOfReplicas="1"),
            klass("lh-8b6ca866-806d-430a-8fd4-d584f0f06128", backingImage="default-image-151065"),
            klass("longhorn-image-ubuntu", backingImage="default-image-ubuntu"),
            klass("homestead-restore-155ade32ea106399", fromBackup="s3://b/backups?volume=x")))

    def claim(self, name, status, **meta):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": "lab", "name": name, **meta.pop("metadata", {})}, **meta,
                       "status": status})

    def sent(self):
        return [(method, path) for method, path, _, _ in self.kube.writes]

    def test_image_and_restore_classes_are_never_offered(self):
        self.assertEqual(["longhorn-v1-1x", "longhorn-r2"], server.selectable_storage_classes())

    def new_volume(self):
        return server.new_claims([{"type": "pvc", "create": True, "source": "data"}])[0]["storage_class"]

    def test_with_no_install_class_the_clusters_default_is_homesteads_default(self):
        # Making a class the default in Volumes is how such an install says which.
        self.assertEqual(("longhorn-v1-1x",) * 2, (self.new_volume(), server.VM_ROUTES.vm_default_class()))

    def test_the_install_class_comes_before_the_clusters_default(self):
        with mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", "longhorn-r2"):
            self.assertEqual(("longhorn-r2",) * 2, (self.new_volume(), server.VM_ROUTES.vm_default_class()))
            self.assertEqual(["longhorn-r2", "longhorn-v1-1x"], server.selectable_storage_classes())

    def test_without_a_default_longhorns_usual_class_is_used(self):
        self.kube.add(klass("longhorn-v1-1x", numberOfReplicas="1"))
        self.assertEqual("longhorn-r2", self.new_volume())

    def homesteads(self):
        """The class list's word on Homestead's own class: (name, why)."""
        rows = STORAGE_CLASSES.storage_class_inventory()
        return [(row["name"], row["homestead_reason"]) for row in rows if row.get("homestead_default")]

    def test_the_class_list_says_which_class_homestead_uses_and_why(self):
        # A chart install follows the cluster's default.
        self.assertEqual([("longhorn-v1-1x", "default")], self.homesteads())
        # An install class that is here wins, whatever the cluster marks.
        with mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", "longhorn-r2"):
            self.assertEqual([("longhorn-r2", "install")], self.homesteads())
        # Nothing marked: Longhorn's usual name.
        self.kube.add(klass("longhorn-v1-1x", numberOfReplicas="1"))
        self.assertEqual([("longhorn-r2", "fallback")], self.homesteads())

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
