import copy
import unittest
import urllib.error

import test_vm_resources as fixtures
import homestead_vm_state as state


class PersistentStateTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.spec = self.vm["spec"]["template"]["spec"]
        self.spec["domain"]["devices"] = {"tpm": {"persistent": True}}
        self.base = "/api/v1/namespaces/lab/persistentvolumeclaims"
        self.sc_path = "/apis/storage.k8s.io/v1/storageclasses/storage"
        self.profile_path = "/apis/cdi.kubevirt.io/v1beta1/storageprofiles/storage"
        self.sc = {"metadata": {"name": "storage", "uid": "sc-uid", "resourceVersion": "1",
                                "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}},
                   "volumeBindingMode": "WaitForFirstConsumer"}
        self.objects = {self.base: {"items": []}, self.sc_path: self.sc,
                        "/apis/storage.k8s.io/v1/storageclasses": {"items": [self.sc]}}
        self.reads = []

    def read(self, path):
        self.reads.append(path)
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        value = self.objects[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def inspect(self, **kwargs):
        return state.inspect(self.vm, self.spec, kwargs.pop("config", {}), self.read, **kwargs)

    def claim(self, name="persistent-state-for-guest-abc", phase="Bound"):
        pvc = fixtures.child(self.vm, "VirtualMachine", name, "state-uid")
        pvc["metadata"]["labels"] = {state.PREFIX: "guest"}
        pvc["spec"] = {"volumeName": "state-pv", "volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"], "storageClassName": "storage"}
        pvc["status"] = {"phase": phase}
        self.objects[self.base]["items"] = [pvc]
        self.objects[self.base + "/" + name] = pvc
        self.objects["/api/v1/persistentvolumes/state-pv"] = {
            "metadata": {"name": "state-pv", "uid": "pv-uid", "resourceVersion": "1"},
            "spec": {"claimRef": {"name": name, "namespace": "lab", "uid": "state-uid"}}}
        return pvc

    def test_no_state_means_no_extra_inventory(self):
        self.spec["domain"]["devices"]["tpm"]["persistent"] = False
        self.assertEqual([], self.inspect()["volumes"])
        self.assertEqual([], self.reads)

    def test_persistent_efi_and_active_cbt_require_state(self):
        self.spec["domain"].pop("devices")
        self.spec["domain"]["firmware"] = {"bootloader": {"efi": {"persistent": True}}}
        self.assertTrue(self.inspect()["planned_claims"])
        self.spec["domain"].pop("firmware")
        for value in ("Initializing", "Enabled"):
            self.vm["status"] = {"changedBlockTracking": {"state": value}}
            self.assertTrue(self.inspect()["planned_claims"])

    def test_owned_state_is_projected_and_identity_bound_without_changes(self):
        pvc = self.claim()
        before = copy.deepcopy(self.objects)
        result = self.inspect()
        self.assertEqual([], result["blockers"])
        self.assertEqual(pvc["metadata"]["name"], result["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual("state-uid", result["dependencies"][self.base + "/" + pvc["metadata"]["name"]]["uid"])
        self.assertEqual("pv-uid", result["dependencies"]["/api/v1/persistentvolumes/state-pv"]["uid"])
        self.assertEqual({}, result["planned_claims"])
        self.assertEqual(before, self.objects)

    def test_legacy_unlabelled_state_requires_vm_uid_not_name(self):
        pvc = self.claim("persistent-state-for-guest")
        pvc["metadata"].pop("labels")
        self.assertFalse(self.inspect()["blockers"])
        pvc["metadata"]["ownerReferences"][0]["uid"] = "old-vm"
        self.assertIn("not be adopted", " ".join(self.inspect()["blockers"]))

    def test_new_vm_must_not_adopt_an_existing_label_match(self):
        self.claim()
        self.vm["metadata"].pop("uid")
        self.assertTrue(self.inspect()["blockers"])

    def test_duplicate_deleting_unowned_and_block_claims_are_not_reused(self):
        for mutation in (lambda p: p["metadata"].update(deletionTimestamp="now"),
                         lambda p: p["metadata"].pop("ownerReferences"),
                         lambda p: p["metadata"]["ownerReferences"].append(copy.deepcopy(p["metadata"]["ownerReferences"][0])),
                         lambda p: p["spec"].update(volumeMode="Block"),
                         lambda p: p["spec"].update(accessModes=["ReadOnlyMany"]),
                         lambda p: self.objects[self.base]["items"].append(copy.deepcopy(p))):
            pvc = self.claim()
            mutation(pvc)
            self.assertTrue(self.inspect()["blockers"])

    def test_pv_binding_missing_or_replaced_is_a_hard_block(self):
        self.claim()
        for change in ({"uid": "other"}, {"namespace": "other"}, {"name": "other"}):
            pv = self.objects["/api/v1/persistentvolumes/state-pv"]
            original = copy.deepcopy(pv["spec"]["claimRef"])
            pv["spec"]["claimRef"].update(change)
            self.assertTrue(self.inspect()["blockers"])
            pv["spec"]["claimRef"] = original
        self.objects.pop("/api/v1/persistentvolumes/state-pv")
        self.assertTrue(self.inspect()["blockers"])

    def test_pending_wffc_allowed_but_immediate_lost_unknown_not_ready(self):
        pvc = self.claim(phase="Pending")
        self.assertFalse(self.inspect()["blockers"])
        self.sc["volumeBindingMode"] = "Immediate"
        self.assertTrue(self.inspect()["blockers"])
        for phase in ("Lost", "", "Unknown"):
            pvc["status"]["phase"] = phase
            self.assertTrue(self.inspect()["blockers"])

    def test_reported_state_cannot_be_missing_or_differ_from_selection(self):
        vmi = fixtures.child(self.vm, "VirtualMachine", "guest", "vmi-uid")
        vmi["status"]["volumeStatus"] = [{"name": "persistent-state-for-this-vm",
            "persistentVolumeClaimInfo": {"claimName": "state-a"}}]
        self.assertTrue(self.inspect(vmi=vmi)["blockers"])
        self.claim()
        self.assertIn("disagrees", " ".join(self.inspect(vmi=vmi)["blockers"]))
        vmi["status"]["volumeStatus"][0]["persistentVolumeClaimInfo"]["claimName"] = "persistent-state-for-guest-abc"
        self.assertFalse(self.inspect(vmi=vmi)["blockers"])
        vmi["status"]["migrationState"] = {"migrationUid": "moving"}
        self.assertIn("migrating", " ".join(self.inspect(vmi=vmi)["blockers"]))

    def test_running_vm_missing_state_is_not_fresh_initialization(self):
        self.vm["status"] = {"created": True}
        self.assertTrue(self.inspect()["blockers"])
        self.assertFalse(self.inspect()["planned_claims"])
        self.vm["status"] = {}
        vmi = fixtures.child(self.vm, "VirtualMachine", "guest", "vmi-uid")
        self.assertTrue(self.inspect(vmi=vmi)["blockers"])
        self.assertFalse(self.inspect(vmi=vmi)["planned_claims"])

    def test_fresh_state_is_only_a_planned_filesystem_with_explicit_warning(self):
        before = copy.deepcopy(self.vm)
        result = self.inspect(version="v1.9.0")
        self.assertFalse(result["blockers"])
        planned = result["planned_claims"]["persistent-state-for-guest"]
        self.assertEqual("storage", planned["storage_class"])
        self.assertEqual("ReadWriteOnce", planned["access_mode"])
        self.assertEqual("Filesystem", planned["volume_mode"])
        self.assertEqual(str(10 * 1024**2), planned["size"])
        self.assertIn("fresh state", " ".join(result["warnings"]))
        self.assertEqual(before, self.vm)

    def test_explicit_class_defaults_rwx_profile_filesystem_modes_take_precedence(self):
        cfg = {"vmStateStorageClass": "storage"}
        self.assertEqual("ReadWriteMany", self.inspect(config=cfg)["planned_claims"]["persistent-state-for-guest"]["access_mode"])
        self.objects[self.profile_path] = {"metadata": {"name": "storage", "uid": "profile-uid", "resourceVersion": "1",
            "annotations": {"cdi.kubevirt.io/minimumSupportedPvcSize": "4Gi"}},
            "status": {"claimPropertySets": [{"volumeMode": "Block", "accessModes": ["ReadWriteMany"]},
                                            {"volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"]}]}}
        result = self.inspect(config=cfg)
        self.assertFalse(result["blockers"])
        planned = result["planned_claims"]["persistent-state-for-guest"]
        self.assertEqual("ReadWriteOnce", planned["access_mode"])
        self.assertEqual(str(4 * 1024**3), planned["size"])
        self.assertEqual("profile-uid", result["dependencies"][self.profile_path]["uid"])

    def test_version_selects_default_and_unknown_disagreement_is_not_guessed(self):
        other = copy.deepcopy(self.sc)
        other["metadata"].update(name="virt", uid="virt-uid", annotations={"storageclass.kubevirt.io/is-default-virt-class": "true"})
        self.objects["/apis/storage.k8s.io/v1/storageclasses"]["items"].append(other)
        self.objects["/apis/storage.k8s.io/v1/storageclasses/virt"] = other
        for version, expected in (("v1.3.1", "storage"), ("v1.4.0", "virt"), ("v1.9.0", "virt")):
            result = self.inspect(version=version)
            self.assertEqual(expected, result["planned_claims"]["persistent-state-for-guest"]["storage_class"])
        self.assertTrue(self.inspect()["blockers"])
        self.assertFalse(self.inspect(config={"vmStateStorageClass": "storage"})["blockers"])

    def test_incomplete_inventory_and_external_errors_do_not_leak_or_create_plan(self):
        for value in ({"items": [], "metadata": {"continue": "page2"}}, {}, ValueError("private-password"),
                      urllib.error.HTTPError(self.base, 403, "private-password", {}, None)):
            self.objects[self.base] = value
            result = self.inspect()
            self.assertTrue(result["blockers"])
            self.assertFalse(result["planned_claims"])
            self.assertNotIn("private-password", str(result))


if __name__ == "__main__":
    unittest.main()
