import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

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

    def throttle_attachments(self, response=None, persistent=False, headers=None):
        original = volumes.kget
        calls = []
        def get(path):
            if path == f"{API}/volumeattachments":
                calls.append(path)
                if persistent or len(calls) == 1:
                    raise urllib.error.HTTPError(path, 429, "too many requests", headers or {}, None)
                return response if response is not None else {"items": []}
            return original(path)
        volumes.kget = get
        return calls

    def test_throttled_attachment_read_retries_once_and_keeps_preview_read_only(self):
        calls = self.throttle_attachments(headers={"Retry-After": "1"})
        with patch.object(volumes.time, "sleep") as sleep:
            plan = self.plan()
        self.assertFalse(plan["blocked"])
        self.assertTrue(plan["inventory_complete"])
        self.assertEqual(2, len(calls))
        sleep.assert_called_once_with(1.0)
        self.assertEqual([], self.sent)

    def test_persistent_throttling_blocks_deletion_without_writes(self):
        calls = self.throttle_attachments(persistent=True)
        with patch.object(volumes.time, "sleep") as sleep:
            plan = self.plan()
            self.assertTrue(plan["blocked"])
            self.assertFalse(plan["inventory_complete"])
            self.assertFalse(plan["actions"]["delete_data"]["enabled"])
            self.assertTrue(any("HTTP 429" in w and "refresh" in w for w in plan["warnings"]))
            with self.assertRaises(PermissionError):
                volumes.delete(self.cfg)
        self.assertEqual(4, len(calls))  # deletion gets its own fresh impact review
        self.assertEqual(2, sleep.call_count)
        self.assertEqual([], self.sent)

    def test_retry_still_checks_attachment_tickets_and_partial_inventories(self):
        for response in [
            {"items": [{"spec": {"volume": "vol", "attachmentTickets": {"mount": {}}}}]},
            {"items": [], "metadata": {"continue": "next-page"}},
            {"metadata": {}},
        ]:
            with self.subTest(response=response):
                self.setUp()
                calls = self.throttle_attachments(response=response)
                with patch.object(volumes.time, "sleep"):
                    with self.assertRaises(PermissionError):
                        volumes.delete(self.cfg)
                self.assertEqual(2, len(calls))
                self.assertEqual([], self.sent)

    def test_retry_delay_is_bounded_even_for_bad_server_headers(self):
        for value, expected in [("0", 0.0), ("0.25", 0.25), ("999", 2.0),
                                ("-1", 0.5), ("nan", 0.5), ("inf", 0.5),
                                ("invalid", 0.5), (None, 0.5)]:
            with self.subTest(value=value):
                self.setUp()
                self.throttle_attachments(headers={"Retry-After": value})
                with patch.object(volumes.time, "sleep") as sleep:
                    self.assertFalse(self.plan()["blocked"])
                sleep.assert_called_once_with(expected)

    def test_non_throttling_inventory_errors_are_not_retried_or_ignored(self):
        for code in [403, 404, 500, 503]:
            with self.subTest(code=code):
                self.setUp()
                original = volumes.kget
                calls = []
                def get(path):
                    if path == f"{API}/volumeattachments":
                        calls.append(path)
                        raise urllib.error.HTTPError(path, code, "unavailable", {}, None)
                    return original(path)
                volumes.kget = get
                with patch.object(volumes.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError):
                        volumes.delete(self.cfg)
                self.assertEqual(1, len(calls))
                sleep.assert_not_called()
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
