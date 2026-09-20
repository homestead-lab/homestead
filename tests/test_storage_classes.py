import copy, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server

CLASSES = {"items": [
    {"metadata": {"name": "harvester-longhorn", "annotations": {
        "storageclass.kubernetes.io/is-default-class": "true"}},
     "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True,
     "parameters": {"migratable": "true", "numberOfReplicas": "3"}},
    {"metadata": {"name": "longhorn"}, "provisioner": "driver.longhorn.io",
     "parameters": {"numberOfReplicas": "3"}},
    {"metadata": {"name": "longhorn-r2"}, "provisioner": "driver.longhorn.io",
     "parameters": {"migratable": "true", "numberOfReplicas": "2"}},
    {"metadata": {"name": "longhorn-static"}, "provisioner": "driver.longhorn.io", "parameters": {}},
    {"metadata": {"name": "vmstate-persistence", "annotations": {
        "harvesterhci.io/is-reserved-storageclass": "true"}},
     "provisioner": "driver.longhorn.io", "parameters": {"migratable": "true"}},
]}


class StorageClassTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.claims = {"items": [{"spec": {"storageClassName": "longhorn-r2"}}]}
        server.kget = lambda path, **kw: (copy.deepcopy(CLASSES) if "storageclasses" in path
                                          else copy.deepcopy(self.claims) if "persistentvolumeclaims" in path
                                          else {"items": []})
        server.ksend = lambda method, path, body=None, **kw: self.sent.append((method, path, body)) or body

    def test_internal_classes_are_never_offered(self):
        self.assertEqual(["harvester-longhorn", "longhorn", "longhorn-r2"],
                         server.selectable_storage_classes())

    def test_only_non_migratable_classes_can_serve_rwx(self):
        self.assertEqual(["longhorn"], server.shared_storage_classes())

    def test_rwx_claim_on_a_migratable_class_is_refused_with_the_alternative(self):
        with self.assertRaisesRegex(ValueError, "longhorn"):
            server.create_pvc("lab", "media", 5, "longhorn-r2", "ReadWriteMany")
        self.assertEqual([], self.sent, "nothing is created when the class cannot serve RWX")

    def test_rwo_on_the_same_class_is_still_fine(self):
        server.create_pvc("lab", "appdata", 5, "longhorn-r2")
        self.assertEqual("POST", self.sent[0][0])
        self.assertEqual(["ReadWriteOnce"], self.sent[0][2]["spec"]["accessModes"])

    def test_rwx_on_a_plain_class_is_allowed(self):
        server.create_pvc("lab", "media", 5, "longhorn", "ReadWriteMany")
        self.assertEqual("longhorn", self.sent[0][2]["spec"]["storageClassName"])


    def test_a_new_class_carries_the_longhorn_parameters_it_was_given(self):
        server.create_storage_class({"name": "longhorn-r3", "replicas": 3, "expandable": True,
                                     "reclaim_policy": "Retain"})

        method, path, body = self.sent[-1]
        self.assertEqual(("POST", "/apis/storage.k8s.io/v1/storageclasses"), (method, path))
        self.assertEqual({"numberOfReplicas": "3", "staleReplicaTimeout": "30"}, body["parameters"])
        self.assertEqual("driver.longhorn.io", body["provisioner"])
        self.assertEqual("Retain", body["reclaimPolicy"])
        self.assertTrue(body["allowVolumeExpansion"])
        self.assertNotIn("migratable", body["parameters"], "container storage is not migratable")

    def test_making_a_class_default_clears_the_previous_one(self):
        server.set_default_storage_class("longhorn")

        cleared = [(path, body) for method, path, body in self.sent if method == "PATCH"]
        self.assertEqual("/apis/storage.k8s.io/v1/storageclasses/harvester-longhorn", cleared[0][0])
        self.assertEqual("false", cleared[0][1]["metadata"]["annotations"][server.DEFAULT_CLASS_ANNOTATION])
        self.assertEqual("/apis/storage.k8s.io/v1/storageclasses/longhorn", cleared[-1][0])
        self.assertEqual("true", cleared[-1][1]["metadata"]["annotations"][server.DEFAULT_CLASS_ANNOTATION])

    def test_a_class_in_use_or_reserved_or_default_is_not_deletable(self):
        for name, reason in (("longhorn-r2", "still backs"), ("longhorn-static", "reserved"),
                             ("harvester-longhorn", "default")):
            with self.assertRaisesRegex(ValueError, reason):
                server.delete_storage_class(name)
        self.assertEqual([], self.sent)

    def test_an_unused_class_is_deleted_and_says_volumes_survive(self):
        result = server.delete_storage_class("longhorn")

        self.assertEqual(("DELETE", "/apis/storage.k8s.io/v1/storageclasses/longhorn"),
                         self.sent[-1][:2])
        self.assertIn("untouched", result["message"])

    def test_duplicate_names_and_impossible_replica_counts_are_refused(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            server.create_storage_class({"name": "longhorn"})
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            server.create_storage_class({"name": "longhorn-r9", "replicas": 9})
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
