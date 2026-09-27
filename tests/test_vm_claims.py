import unittest
import urllib.error

import test_vm_resources as fixtures
import homestead_vm_claims as claims


class PlannedVMClaimsTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.vm["spec"]["dataVolumeTemplates"] = [{"metadata": {"name": "root"}, "spec": {"storage": {
            "storageClassName": "vm-storage", "resources": {"requests": {"storage": "20Gi"}}}}}]
        self.sc = {"metadata": {"name": "vm-storage", "uid": "sc", "resourceVersion": "1"}}
        self.profile = {"status": {"claimPropertySets": [{"accessModes": ["ReadWriteMany"], "volumeMode": "Block"}]}}

    def read(self,path):
        if path.endswith("/storageclasses/vm-storage"): return self.sc
        if path.endswith("/storageprofiles/vm-storage"): return self.profile
        if path.endswith("/storageclasses"): return {"items": [self.sc]}
        raise urllib.error.HTTPError(path,404,"missing",{},None)

    def test_profile_modes_are_reported_without_making_claims(self):
        result=claims.plans(self.vm,self.read)["root"]
        self.assertEqual("ReadWriteMany",result["access_mode"])
        self.assertEqual("Block",result["volume_mode"])
        self.assertEqual("20Gi",result["size"])

    def test_profile_mode_matches_explicit_volume_mode(self):
        self.vm["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]["volumeMode"]="Filesystem"
        with self.assertRaisesRegex(ValueError,"unresolved"):
            claims.plans(self.vm,self.read)

    def test_unknown_profile_modes_are_not_guessed(self):
        self.profile={}
        with self.assertRaisesRegex(ValueError,"unresolved"):
            claims.plans(self.vm,self.read)

    def test_storage_class_must_have_identity_and_not_be_deleting(self):
        for meta in ({"name":"vm-storage"},{"name":"vm-storage","uid":"sc","resourceVersion":"1","deletionTimestamp":"now"}):
            self.sc["metadata"]=meta
            with self.assertRaisesRegex(ValueError,"identity/readiness"):
                claims.plans(self.vm,self.read)

    def test_no_default_does_not_become_invented_storage(self):
        del self.vm["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]["storageClassName"]
        with self.assertRaisesRegex(ValueError,"single default"):
            claims.plans(self.vm,self.read)

    def test_duplicate_controller_claim_name_is_rejected(self):
        self.vm["spec"]["dataVolumeTemplates"]*=2
        with self.assertRaisesRegex(ValueError,"more than once"):
            claims.plans(self.vm,self.read)


if __name__ == "__main__": unittest.main()
