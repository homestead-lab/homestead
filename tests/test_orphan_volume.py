import copy
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_volumes as volumes

API = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


class OrphanVolumeTests(unittest.TestCase):
    def setUp(self):
        self.pv = {"metadata": {"name": "pv", "uid": "pv-uid", "resourceVersion": "9"},
                   "spec": {"claimRef": {"name": "ubuntu-cloud-disk", "namespace": "lab"},
                            "persistentVolumeReclaimPolicy": "Retain", "csi": {"driver": "driver.longhorn.io", "volumeHandle": "vol"}},
                   "status": {"phase": "Released"}}
        self.lh = {"metadata": {"name": "vol", "uid": "lh-uid", "resourceVersion": "5"}, "spec": {},
                   "status": {"state": "detached", "kubernetesStatus": {"namespace": "lab", "pvcName": "ubuntu-cloud-disk"}}}
        self.objects = {f"{API}/volumes/vol": self.lh, "/api/v1/persistentvolumes": {"items": [self.pv]}}
        self.sent = []
        volumes.bind(self.get, lambda *a, **kw: self.sent.append((a, kw)), lambda _: [], lambda _: [], {}, {"kube-system"}, "lab")
        self.cfg = dict(namespace="lab", name="ubuntu-cloud-disk", volume="vol", uid="lh-uid", pv_uid="pv-uid",
                        confirmation="ubuntu-cloud-disk", action="delete_data")

    def get(self, path):
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        if path.endswith("/ubuntu-cloud-disk"):
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return {"items": []}

    def plan(self):
        return volumes.deletion_plan("lab", "ubuntu-cloud-disk", "vol")

    def test_absent_pvc_inspects_retained_volume_and_never_offers_claim_delete(self):
        p = self.plan()
        self.assertTrue(p["orphan"])
        self.assertFalse(p["blocked"])
        self.assertFalse(p["actions"]["delete_claim"]["enabled"])
        self.assertEqual("pv", p["pv"]["name"])

    def test_retained_pv_is_reclaimed_by_csi_with_atomic_identity_tests(self):
        volumes.delete(self.cfg)
        args, kw = self.sent[0]
        self.assertEqual(("PATCH", "/api/v1/persistentvolumes/pv"), args[:2])
        self.assertEqual("application/json-patch+json", kw["ctype"])
        self.assertEqual(["pv-uid", "9", "Released", "Delete"], [p["value"] for p in args[2]])
        self.assertEqual(1, len(self.sent))

    def test_no_pv_can_delete_only_detached_lh_with_uid_and_revision(self):
        self.objects["/api/v1/persistentvolumes"]["items"] = []
        volumes.delete({**self.cfg, "pv_uid": ""})
        args, _ = self.sent[0]
        self.assertEqual(("DELETE", f"{API}/volumes/vol"), args[:2])
        self.assertEqual({"uid": "lh-uid", "resourceVersion": "5"}, args[2]["preconditions"])

    def test_attached_bound_or_replaced_objects_cannot_be_deleted(self):
        for case in ["attached", "bound", "lh-replaced", "pv-replaced", "claim-recreated"]:
            with self.subTest(case=case):
                self.setUp()
                if case == "attached": self.lh["status"]["state"] = "attached"
                if case == "bound": self.pv["status"]["phase"] = "Bound"
                if case == "lh-replaced": self.lh["metadata"]["uid"] = "new"
                if case == "pv-replaced": self.pv["metadata"]["uid"] = "new"
                if case == "claim-recreated":
                    self.objects["/api/v1/persistentvolumeclaims"] = {"items": [{"metadata": {"namespace": "lab", "name": "ubuntu-cloud-disk"}}]}
                with self.assertRaises((ValueError, PermissionError)):
                    volumes.delete(self.cfg)
                self.assertEqual([], self.sent)

    def test_attachments_partial_inventory_and_pod_references_block(self):
        cases = {
            "/apis/storage.k8s.io/v1/volumeattachments": {"items": [{"spec": {"source": {"persistentVolumeName": "pv"}}}]},
            f"{API}/volumeattachments": {"items": [{"spec": {"volume": "vol", "attachmentTickets": {"mount": {}}}}]},
            f"{API}/snapshots": {"items": [], "metadata": {"continue": "next"}},
            "/api/v1/namespaces/lab/pods": {"items": [{"metadata": {"name": "holding"}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "ubuntu-cloud-disk"}}]}}]}}
        for path, value in cases.items():
            with self.subTest(path=path):
                self.setUp()
                self.objects[path] = value
                with self.assertRaises(PermissionError): volumes.delete(self.cfg)
                self.assertEqual([], self.sent)

    def test_missing_pvc_without_explicit_backing_identity_does_not_guess(self):
        with self.assertRaises(urllib.error.HTTPError):
            volumes.deletion_plan("lab", "ubuntu-cloud-disk")

    def test_forbidden_pvc_read_is_not_treated_as_missing(self):
        original = volumes.kget
        def get(path):
            if path.endswith("/ubuntu-cloud-disk"):
                raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
            return original(path)
        volumes.kget = get
        with self.assertRaises(urllib.error.HTTPError): self.plan()


if __name__ == "__main__":
    unittest.main()
