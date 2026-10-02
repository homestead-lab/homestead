import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_cdi_cleanup as cleanup
import homestead_operations as operations

NS = "/api/v1/namespaces/lab"
DV = cleanup.API + "/namespaces/lab/datavolumes"
PV = "/api/v1/persistentvolumes"
ROOT = "10000000-0000-4000-8000-000000000001"


def obj(name, uid, **fields):
    return {"metadata": {"name": name, "uid": uid, "resourceVersion": "1", "namespace": "lab"}, **fields}


def owned(row, kind, uid):
    row["metadata"]["ownerReferences"] = [{"kind": kind, "uid": uid, "controller": True}]
    return row


class Api:
    def __init__(self):
        self.objects, self.writes = {}, []
        self.before_patch = None
        self.lost_patch = False
        self.hold_deletes = set()

    def read(self, path):
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        if path in (NS + "/persistentvolumeclaims", NS + "/pods", PV,
                    "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines",
                    "/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances"):
            return {"items": [copy.deepcopy(row) for key, row in self.objects.items()
                              if key.startswith(path + "/") and "/" not in key[len(path) + 1:]]}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kwargs):
        self.writes.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            row = copy.deepcopy(body)
            row["metadata"].update(uid="dv-uid", resourceVersion="1")
            self.objects[path + "/" + row["metadata"]["name"]] = row
            return copy.deepcopy(row)
        row = self.objects[path]
        if method == "DELETE":
            self.assert_preconditions(row, body["preconditions"])
            if path in self.hold_deletes:
                row["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
            else:
                del self.objects[path]
            return {}
        if method == "PATCH":
            if self.before_patch:
                self.before_patch(row)
            for patch in body:
                parts = patch["path"].strip("/").split("/")
                container = row
                for part in parts[:-1]:
                    container = container[part]
                if patch["op"] == "test":
                    if container[parts[-1]] != patch["value"]:
                        raise urllib.error.HTTPError(path, 409, "changed", {}, None)
                else:
                    container[parts[-1]] = patch["value"]
            if self.lost_patch:
                self.lost_patch = False
                raise OSError("lost reply")
            return copy.deepcopy(row)
        raise AssertionError(method)

    @staticmethod
    def assert_preconditions(row, preconditions):
        if any(row["metadata"][key] != value for key, value in preconditions.items()):
            raise AssertionError("unsafe deletion")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.api = Api()
        self.work = cleanup.receipt(["guest-disk"])
        dv = obj("guest-disk", "dv-uid", status={"phase": "ImportInProgress"})
        dv["metadata"]["annotations"] = {cleanup.STAMP: self.work["id"]}
        self.api.objects[DV + "/guest-disk"] = dv
        self.target = owned(obj("guest-disk", ROOT, spec={}, status={"phase": "Pending"}), "DataVolume", "dv-uid")
        self.api.objects[NS + "/persistentvolumeclaims/guest-disk"] = self.target
        self.prime_name = "prime-" + ROOT
        self.prime = owned(obj(self.prime_name, "prime-uid", spec={"volumeName": "pv-data"}, status={"phase": "Bound"}),
                           "PersistentVolumeClaim", ROOT)
        self.api.objects[NS + "/persistentvolumeclaims/" + self.prime_name] = self.prime
        pod = owned(obj("upload", "pod-uid", spec={"volumes": [
            {"persistentVolumeClaim": {"claimName": self.prime_name}},
            {"persistentVolumeClaim": {"claimName": self.prime_name + "-scratch"}}]}), "PersistentVolumeClaim", "prime-uid")
        pod["metadata"]["labels"] = {"cdi.kubevirt.io": "cdi-upload-server"}
        self.api.objects[NS + "/pods/upload"] = pod
        scratch = owned(obj(self.prime_name + "-scratch", "scratch-uid", spec={"volumeName": "pv-scratch"}), "Pod", "pod-uid")
        self.api.objects[NS + "/persistentvolumeclaims/" + self.prime_name + "-scratch"] = scratch
        self.add_pv("pv-data", self.prime_name, "prime-uid")
        self.add_pv("pv-scratch", self.prime_name + "-scratch", "scratch-uid")

    def add_pv(self, name, claim_name, claim_uid, phase="Bound"):
        row = obj(name, name + "-uid", spec={"persistentVolumeReclaimPolicy": "Retain",
            "claimRef": {"namespace": "lab", "name": claim_name, "uid": claim_uid},
            "csi": {"driver": "driver.longhorn.io", "volumeHandle": name}}, status={"phase": phase})
        self.api.objects[PV + "/" + name] = row
        return row

    def observe(self):
        cleanup.observe(self.api.read, "lab", self.work)

    def clean(self, keep=True):
        return cleanup.cleanup(self.api.read, self.api.send, "lab", self.work, keep)

    def promote(self):
        self.observe()
        self.target.update(spec={"volumeName": "pv-data"}, status={"phase": "Bound"})
        self.prime["status"]["phase"] = "Lost"
        self.api.objects[PV + "/pv-data"]["spec"]["claimRef"] = {"namespace": "lab", "name": "guest-disk", "uid": ROOT}
        self.api.objects[PV + "/pv-scratch"]["status"]["phase"] = "Released"

    def test_success_reclaims_scratch_and_preserves_the_promoted_disk_retention(self):
        self.promote()
        self.observe()
        self.assertFalse(self.clean())
        data = self.api.objects[PV + "/pv-data"]
        self.assertEqual("Retain", data["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual("Delete", self.api.objects[PV + "/pv-scratch"]["spec"]["persistentVolumeReclaimPolicy"])
        self.assertIn(NS + "/persistentvolumeclaims/guest-disk", self.api.objects)
        del self.api.objects[PV + "/pv-scratch"]
        self.assertTrue(self.clean())

    def test_classic_cdi_import_without_a_prime_claim_cleans_its_scratch(self):
        self.api.objects.clear()
        dv = obj("guest-disk", "dv-uid")
        dv["metadata"]["annotations"] = {cleanup.STAMP: self.work["id"]}
        self.api.objects[DV + "/guest-disk"] = dv
        target = owned(obj("guest-disk", ROOT, spec={"volumeName": "pv-data"}, status={"phase": "Bound"}), "DataVolume", "dv-uid")
        self.api.objects[NS + "/persistentvolumeclaims/guest-disk"] = target
        pod = owned(obj("importer", "pod-uid", spec={}), "PersistentVolumeClaim", ROOT)
        pod["metadata"]["labels"] = {"app": "containerized-data-importer"}
        self.api.objects[NS + "/pods/importer"] = pod
        scratch = owned(obj("guest-disk-scratch", "scratch-uid", spec={"volumeName": "pv-scratch"}), "Pod", "pod-uid")
        self.api.objects[NS + "/persistentvolumeclaims/guest-disk-scratch"] = scratch
        self.add_pv("pv-data", "guest-disk", ROOT)
        self.add_pv("pv-scratch", "guest-disk-scratch", "scratch-uid", "Released")
        self.observe()
        self.assertFalse(self.clean())
        self.assertEqual("Retain", self.api.objects[PV + "/pv-data"]["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual("Delete", self.api.objects[PV + "/pv-scratch"]["spec"]["persistentVolumeReclaimPolicy"])

    def test_failure_reclaims_prime_and_scratch_after_claims_and_workers_leave(self):
        self.observe()
        self.assertFalse(self.clean(False))
        for name in ("pv-data", "pv-scratch"):
            self.api.objects[PV + "/" + name]["status"]["phase"] = "Released"
        self.assertFalse(self.clean(False))
        for name in ("pv-data", "pv-scratch"):
            self.assertEqual("Delete", self.api.objects[PV + "/" + name]["spec"]["persistentVolumeReclaimPolicy"])
            del self.api.objects[PV + "/" + name]
        self.assertTrue(self.clean(False))

    def test_restart_recovers_released_prime_scratch_created_and_deleted_between_polls(self):
        self.observe()
        scratch_name = self.prime_name + "-scratch"
        self.add_pv("pv-earlier-scratch", scratch_name, "earlier-scratch-uid", "Released")
        del self.api.objects[NS + "/persistentvolumeclaims/" + scratch_name]
        self.work = json.loads(json.dumps(self.work))
        self.observe()
        self.assertIn("pv-earlier-scratch", self.work["disks"]["guest-disk"]["pvs"])
        foreign = self.add_pv("pv-other", "prime-unrelated-scratch", "unrelated", "Released")
        self.clean(False)
        self.assertEqual("Retain", foreign["spec"]["persistentVolumeReclaimPolicy"])

    def test_replacement_disk_or_claim_is_never_deleted(self):
        for resource in ("dv", "claim"):
            with self.subTest(resource=resource):
                self.setUp()
                self.observe()
                row = self.api.objects[DV + "/guest-disk"] if resource == "dv" else self.target
                row["metadata"]["uid"] = "replacement"
                with self.assertRaisesRegex(ValueError, "replaced|ownership changed"):
                    self.observe()
                self.assertEqual([], self.api.writes)

    def test_reparented_scratch_or_worker_is_preserved(self):
        for resource in ("scratch", "worker"):
            with self.subTest(resource=resource):
                self.setUp()
                self.promote()
                path = NS + "/pods/upload" if resource == "worker" else NS + "/persistentvolumeclaims/" + self.prime_name + "-scratch"
                self.api.objects[path]["metadata"]["ownerReferences"] = [{"kind": "Pod", "uid": "foreign", "controller": True}]
                with self.assertRaisesRegex(ValueError, "ownership changed"):
                    self.clean()
                self.assertEqual([], self.api.writes)

    def test_foreign_pod_holds_cleanup_even_while_terminating(self):
        self.promote()
        pod = obj("consumer", "other-pod", spec={"volumes": [{"persistentVolumeClaim": {"claimName": self.prime_name + "-scratch"}}]})
        pod["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        self.api.objects[NS + "/pods/consumer"] = pod
        with self.assertRaisesRegex(ValueError, "Another pod uses"):
            self.clean()
        self.assertEqual("Retain", self.api.objects[PV + "/pv-scratch"]["spec"]["persistentVolumeReclaimPolicy"])

    def test_rebound_or_replaced_pv_is_preserved(self):
        self.promote()
        for changed in ("uid", "claim", "handle"):
            with self.subTest(changed=changed):
                pv = self.api.objects[PV + "/pv-scratch"]
                saved = copy.deepcopy(pv)
                if changed == "uid":
                    pv["metadata"]["uid"] = "other"
                elif changed == "claim":
                    pv["spec"]["claimRef"]["uid"] = "other"
                else:
                    pv["spec"]["csi"]["volumeHandle"] = "other"
                with self.assertRaisesRegex(ValueError, "replaced|rebound"):
                    self.clean()
                self.assertEqual("Retain", pv["spec"]["persistentVolumeReclaimPolicy"])
                self.api.objects[PV + "/pv-scratch"] = saved

    def test_cas_blocks_a_rebind_between_inspection_and_patch(self):
        self.promote()
        def race(row):
            row["metadata"]["resourceVersion"] = "2"
            row["spec"]["claimRef"]["uid"] = "foreign"
        self.api.before_patch = race
        with self.assertRaises(urllib.error.HTTPError):
            self.clean()
        self.assertEqual("Retain", self.api.objects[PV + "/pv-scratch"]["spec"]["persistentVolumeReclaimPolicy"])

    def test_lost_patch_reply_resumes_without_repatching_or_forcing_finalizers(self):
        self.promote()
        self.api.lost_patch = True
        with self.assertRaises(OSError):
            self.clean()
        self.assertFalse(self.clean())
        patches = [row for row in self.api.writes if row[0] == "PATCH"]
        self.assertEqual(1, len(patches))
        self.assertNotIn("finalizers", repr(patches))

    def test_pv_absence_is_not_complete_until_longhorn_volume_is_gone(self):
        self.promote()
        self.clean()
        del self.api.objects[PV + "/pv-scratch"]
        lh_path = cleanup.RESOURCES.LH + "/volumes/pv-scratch"
        self.api.objects[lh_path] = obj("pv-scratch", "lh-uid")
        self.assertFalse(self.clean())
        del self.api.objects[lh_path]
        self.assertTrue(self.clean())

    def test_vm_using_a_failed_import_disk_blocks_destructive_cleanup(self):
        self.observe()
        self.api.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"] = obj("guest", "vm-uid",
            spec={"template": {"spec": {"volumes": [{"dataVolume": {"name": "guest-disk"}}]}}})
        with self.assertRaisesRegex(ValueError, "VM now uses"):
            self.clean(False)
        self.assertEqual([], self.api.writes)

    def test_incomplete_inventory_cannot_trigger_cleanup(self):
        self.api.objects[PV] = {"items": [], "metadata": {"continue": "next-page"}}
        with self.assertRaisesRegex(ValueError, "complete resource inventory"):
            self.observe()
        self.assertEqual([], self.api.writes)

    def test_terminating_import_worker_must_disappear_before_reclamation(self):
        for keep in (True, False):
            with self.subTest(keep=keep):
                self.setUp()
                self.promote()
                self.api.hold_deletes.add(NS + "/pods/upload")
                self.assertFalse(self.clean(keep))
                self.assertIn(DV + "/guest-disk", self.api.objects)
                self.assertIn(NS + "/persistentvolumeclaims/guest-disk", self.api.objects)
                self.assertIn(NS + "/persistentvolumeclaims/" + self.prime_name + "-scratch", self.api.objects)
                self.assertEqual("Retain", self.api.objects[PV + "/pv-scratch"]["spec"]["persistentVolumeReclaimPolicy"])

    def test_recreated_scratch_of_the_same_import_reclaims_both_backing_volumes(self):
        self.observe()
        name = self.prime_name + "-scratch"
        replacement = owned(obj(name, "scratch-second", spec={"volumeName": "pv-scratch-second"}), "Pod", "pod-uid")
        self.api.objects[NS + "/persistentvolumeclaims/" + name] = replacement
        self.add_pv("pv-scratch-second", name, "scratch-second", "Released")
        self.promote()
        self.observe()
        self.assertFalse(self.clean())
        for pv_name in ("pv-scratch", "pv-scratch-second"):
            self.assertEqual("Delete", self.api.objects[PV + "/" + pv_name]["spec"]["persistentVolumeReclaimPolicy"])

    def test_failed_vm_creation_keeps_complete_disks_but_cleans_work_space(self):
        self.promote()
        item = {"ref": {"namespace": "lab", "cdi_cleanup": self.work, "phase": "cleanup",
                        "cleanup_outcome": "failed", "cleanup_keep_disks": True,
                        "cleanup_detail": "The disks arrived, but the VM could not be made"}}
        self.assertEqual("running", cleanup.finish(item, self.api.read, self.api.send, lambda row: None)[0])
        del self.api.objects[PV + "/pv-scratch"]
        self.assertEqual("failed", cleanup.finish(item, self.api.read, self.api.send, lambda row: None)[0])
        self.assertIn(DV + "/guest-disk", self.api.objects)
        self.assertIn(NS + "/persistentvolumeclaims/guest-disk", self.api.objects)
        self.assertEqual("Retain", self.api.objects[PV + "/pv-data"]["spec"]["persistentVolumeReclaimPolicy"])

    def test_cleanup_error_is_visible_and_keeps_the_job_active(self):
        self.promote()
        item = {"ref": {"namespace": "lab", "cdi_cleanup": self.work,
                        "phase": "cleanup", "cleanup_outcome": "succeeded", "cleanup_detail": "Disk arrived"}}
        self.api.before_patch = lambda row: row["metadata"].update(resourceVersion="2")
        checkpoints = []
        state, pct, message = cleanup.finish(item, self.api.read, self.api.send,
                                            lambda row: checkpoints.append(copy.deepcopy(row)))
        self.assertEqual(("running", 99), (state, pct))
        self.assertIn("cleanup pending", message)
        self.assertTrue(item["ref"]["retain_resources"])
        self.assertEqual(1, len(checkpoints))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = Api()
        self.previous = (operations.kget, operations.DATA_DIR, operations.cdi_send)
        self.addCleanup(self.restore)
        operations.bind(self.api.read, self.tmp.name, lambda *args: {})
        operations.cdi_send = self.api.send

    def restore(self):
        operations.kget, operations.DATA_DIR, operations.cdi_send = self.previous

    def body(self):
        return {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
                "metadata": {"name": "guest-disk", "namespace": "lab"},
                "spec": {"source": {"http": {"url": "https://example.test/image?token=private"}}}}

    def test_creation_receipt_precedes_post_and_never_stores_the_source_url(self):
        original = self.api.send
        seen = []
        def send(*args, **kwargs):
            seen.extend(operations._read())
            return original(*args, **kwargs)
        cleanup.dispatch(self.api.read, send, operations, "vm-disk-import", "Import disk", {}, "/import",
                         {"namespace": "lab", "name": "guest-disk"}, [self.body()], "import")
        self.assertEqual("creating", seen[0]["ref"]["phase"])
        self.assertTrue(seen[0]["ref"]["cdi_cleanup"]["id"])
        self.assertNotIn("private", repr(operations._read()))
        annotations = self.api.objects[DV + "/guest-disk"]["metadata"]["annotations"]
        self.assertEqual("true", annotations[cleanup.RETAIN_WORKER])

    def test_lost_create_reply_is_cleaned_from_its_saved_identity(self):
        def send(*args, **kwargs):
            self.api.send(*args, **kwargs)
            raise OSError("connection lost")
        op = cleanup.dispatch(self.api.read, send, operations, "vm-disk-import", "Import disk", {}, "/import",
                              {"namespace": "lab", "name": "guest-disk"}, [self.body()], "import")
        self.assertEqual("running", op["status"])
        self.assertEqual("failed", operations.list_operations()[0]["status"])
        self.assertNotIn(DV + "/guest-disk", self.api.objects)

    def test_interrupted_setup_never_reposts_and_tracking_cannot_be_forgotten(self):
        work = cleanup.receipt(["guest-disk"])
        operations.start("vm-disk-import", "Import disk", {}, "/import",
                         {"namespace": "lab", "name": "guest-disk", "phase": "creating", "cdi_cleanup": work})
        item = operations._read()[0]
        self.assertFalse(operations._plan_for(item)["can"])
        self.assertEqual("failed", operations.list_operations()[0]["status"])
        self.assertEqual([], self.api.writes)

    def test_retry_is_blocked_until_prior_attempt_finishes_cleaning_up(self):
        work = cleanup.receipt(["guest-disk"])
        ref = {"namespace": "lab", "name": "guest-disk", "phase": "cleanup", "cdi_cleanup": work}
        operations.start("vm-disk-import", "Import disk", {}, "/import", ref)
        for kind, other in (("vm-disk-import", {"namespace": "lab", "name": "guest-disk"}),
                            ("unraid-vm-import", {"namespace": "lab", "disks": [{"dv": "guest-disk"}]})):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "still active or cleaning up"):
                operations.start(kind, "Retry", {}, "/import", other)

    def test_external_disk_deletion_enters_cleanup_instead_of_ending_tracking(self):
        work = cleanup.receipt(["guest-disk"])
        work["disks"]["guest-disk"]["dv_uid"] = "deleted-dv"
        operations.start("vm-disk-import", "Import disk", {}, "/import",
                         {"namespace": "lab", "name": "guest-disk", "phase": "import", "cdi_cleanup": work})
        result = operations.list_operations()[0]
        self.assertEqual("failed", result["status"])
        self.assertIn("temporary import storage cleared", result["message"])

    def test_missing_inventory_keeps_tracking_for_later_cleanup(self):
        work = cleanup.receipt(["guest-disk"])
        operations.start("vm-disk-import", "Import disk", {}, "/import",
                         {"namespace": "lab", "name": "guest-disk", "phase": "import", "cdi_cleanup": work})
        original = self.api.read
        def unavailable(path):
            if path == NS + "/persistentvolumeclaims":
                raise urllib.error.HTTPError(path, 404, "namespace missing", {}, None)
            return original(path)
        operations.kget = unavailable
        result = operations.list_operations()[0]
        self.assertEqual("running", result["status"])
        self.assertFalse(result["dismissible"])
        self.assertIn("could not verify", result["message"])

    def test_failed_second_disk_setup_cleans_the_first_disk(self):
        bodies = [self.body(), self.body()]
        bodies[1]["metadata"]["name"] = "second-disk"
        def send(method, path, body=None, **kwargs):
            if body["metadata"]["name"] == "second-disk":
                raise OSError("unavailable")
            return self.api.send(method, path, body, **kwargs)
        cleanup.dispatch(self.api.read, send, operations, "vm-disk-import", "Import disk", {}, "/import",
                         {"namespace": "lab", "name": "guest-disk"}, bodies, "import")
        result = operations.list_operations()[0]
        self.assertEqual("failed", result["status"])
        self.assertTrue(result["dismissible"])
        self.assertNotIn(DV + "/guest-disk", self.api.objects)
        self.assertEqual(1, operations.dismiss_finished()["dismissed"])

    def test_rejected_creation_leaves_a_racing_foreign_disk_and_cleans_only_our_disk(self):
        bodies = [self.body(), self.body()]
        bodies[1]["metadata"]["name"] = "second-disk"
        foreign = obj("second-disk", "foreign-uid")
        self.api.objects[DV + "/second-disk"] = foreign
        def send(method, path, body=None, **kwargs):
            if body["metadata"]["name"] == "second-disk":
                raise urllib.error.HTTPError(path, 409, "exists", {}, None)
            return self.api.send(method, path, body, **kwargs)
        cleanup.dispatch(self.api.read, send, operations, "vm-disk-import", "Import disk", {}, "/import",
                         {"namespace": "lab", "name": "guest-disk"}, bodies, "import")
        self.assertEqual("failed", operations.list_operations()[0]["status"])
        self.assertNotIn(DV + "/guest-disk", self.api.objects)
        self.assertEqual(foreign, self.api.objects[DV + "/second-disk"])


if __name__ == "__main__":
    unittest.main()
