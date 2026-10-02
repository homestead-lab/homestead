import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def klass(name, default=False, **parameters):
    return {"metadata": {"name": name, "annotations": (
                {"storageclass.kubernetes.io/is-default-class": "true"} if default else {})},
            "provisioner": "driver.longhorn.io", "parameters": parameters}


class ClassChoiceTests(unittest.TestCase):
    """Pickers offered every image's and every restore's class, and making
    another class the default in Volumes changed nothing Homestead chose."""

    def setUp(self):
        self.saved = server.kget, server.ksend, server.STORAGE_CLASS, server.LC.STORAGE_CLASS, server.LH.STORAGE_CLASS
        self.classes = [klass("longhorn-r2"), klass("longhorn-v1-1x", default=True, numberOfReplicas="1"),
                        klass("lh-8b6ca866-806d-430a-8fd4-d584f0f06128", backingImage="default-image-151065"),
                        klass("longhorn-image-ubuntu", backingImage="default-image-ubuntu"),
                        klass("homestead-restore-155ade32ea106399", fromBackup="s3://b/backups?volume=x")]
        self.pvcs, self.pvs, self.sent = [], [], []

        def kget(path):
            if path == "/apis/storage.k8s.io/v1/storageclasses":
                return {"items": self.classes}
            if path == "/api/v1/persistentvolumeclaims":
                return {"items": self.pvcs}
            if path == "/api/v1/persistentvolumes":
                return {"items": self.pvs}
            return {"items": []}
        server.kget = kget
        server.ksend = lambda method, path, body=None, **kw: self.sent.append((method, path))

    def tearDown(self):
        (server.kget, server.ksend, server.STORAGE_CLASS, server.LC.STORAGE_CLASS,
         server.LH.STORAGE_CLASS) = self.saved

    def test_image_and_restore_classes_are_never_offered(self):
        self.assertEqual(["longhorn-v1-1x", "longhorn-r2"], server.selectable_storage_classes())

    def test_the_clusters_default_is_homesteads_default(self):
        server.storage_classes()
        self.assertEqual("longhorn-v1-1x", server.STORAGE_CLASS)
        self.assertEqual("longhorn-v1-1x", server.LC.STORAGE_CLASS)
        self.assertEqual("longhorn-v1-1x", server.vm_default_class())

    def test_without_a_default_the_installed_class_stands(self):
        self.classes[1]["metadata"]["annotations"] = {}
        server.storage_classes()
        self.assertEqual("longhorn-r2", server.STORAGE_CLASS)

    def test_spent_restore_classes_are_removed_and_needed_ones_kept(self):
        self.classes.append(klass("homestead-restore-deee238ba869b8e0", fromBackup="s3://b/backups?volume=y"))
        self.pvcs = [{"spec": {"storageClassName": "homestead-restore-deee238ba869b8e0"}, "status": {"phase": "Pending"}},
                     {"spec": {"storageClassName": "homestead-restore-155ade32ea106399"}, "status": {"phase": "Bound"}}]
        self.classes.append(klass("homestead-restore-unused", fromBackup="s3://b/backups?volume=z"))
        removed = server.cleanup_restore_classes()
        self.assertEqual(["homestead-restore-unused"], removed)
        self.assertEqual([("DELETE", "/apis/storage.k8s.io/v1/storageclasses/homestead-restore-unused")], self.sent)

    def test_legacy_claim_and_terminating_claim_keep_their_class(self):
        self.pvcs = [{"metadata": {"deletionTimestamp": "2026-01-01T00:00:00Z",
                                   "annotations": {"volume.beta.kubernetes.io/storage-class": "homestead-restore-155ade32ea106399"}},
                      "status": {"phase": "Bound"}}]
        self.assertEqual([], server.cleanup_restore_classes())
        self.assertEqual([], self.sent)

    def test_failed_claim_inventory_never_deletes_classes(self):
        server.kget = lambda path: (_ for _ in ()).throw(RuntimeError("API unavailable"))
        self.assertEqual([], server.cleanup_restore_classes())
        self.assertEqual([], self.sent)

    def test_incomplete_claim_inventory_never_deletes_classes(self):
        server.kget = lambda path: {"items": [], "metadata": {"continue": "next-page"}}
        self.assertEqual([], server.cleanup_restore_classes())
        self.assertEqual([], self.sent)

    def test_retained_backing_volume_keeps_its_restore_class_without_a_claim(self):
        self.pvs = [{"spec": {"storageClassName": "homestead-restore-155ade32ea106399",
                              "persistentVolumeReclaimPolicy": "Retain"}, "status": {"phase": "Released"}}]
        self.assertEqual([], server.cleanup_restore_classes())
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
