"""Cancelling an Unraid or Proxmox VM import keeps a disk a VM or a pod uses (#1312).

Cancel removed every DataVolume the import stamped, whatever had happened to it
since: after the import made its VM, or once a person had made one from a
paused import's disk, Cancel took the VM's disks. On the shared fake API and the
real job list, walking the import's own phases (tests/test_unraid_import_waits.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_operations as JOBS
import homestead_unraid_vms as UVMS
import test_unraid_import_waits as waits

DVS, NS, CLAIMS = waits.DVS, waits.NS, waits.CLAIMS
VMS = f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachines"
VMIS = f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachineinstances"
BASE = waits.ImportWaitTests


class CancelKeepsDisksInUseTests(unittest.TestCase):
    # The import walk of tests/test_unraid_import_waits.py, without its own tests.
    setUp = BASE.setUp
    path, phase, look, ref, to_convert, claim, upload_pod, bound_disk = (
        BASE.path, BASE.phase, BASE.look, BASE.ref, BASE.to_convert, BASE.claim, BASE.upload_pod, BASE.bound_disk)

    def vm(self, name, disks, running=False):
        volumes = [{"name": f"v{i}", "dataVolume": {"name": d}} for i, d in enumerate(disks)]
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
                       "metadata": {"namespace": NS, "name": name},
                       "spec": {"template": {"spec": {"volumes": volumes}}}})
        if running:
            self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                           "metadata": {"namespace": NS, "name": name}, "spec": {"volumes": volumes}})

    def pod(self, name, claim):
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"namespace": NS, "name": name},
                       "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]},
                       "status": {"phase": "Running"}})

    def copying(self):
        self.phase("UploadReady")
        self.look()
        self.assertEqual("copy", self.ref()["phase"])
        self.assertTrue(self.look()["cancellable"])

    def review(self):
        plan = JOBS.cancel_plan(self.ident)
        return " ".join([plan["action"], *plan["undo"], *plan["keeps"]])

    # ------------------------------------------------------------ before the VM is made

    def test_a_disk_a_vm_uses_is_kept_and_the_other_is_removed(self):
        self.copying()
        self.vm("made-by-hand", [DVS[0]], running=True)
        self.assertIn("A disk that a VM or an app uses is kept", self.review())
        JOBS.cancel(self.ident, {}, by="a person")
        job = self.look()
        self.assertEqual("cancelled", job["status"], job["message"])
        self.assertTrue(self.kube.object(self.path(DVS[0])), "a VM uses it")
        self.assertIsNone(self.kube.object(self.path(DVS[1])), "nothing uses it: it goes as before")
        self.assertIn(f"VM made-by-hand uses {DVS[0]}, so it is kept.", job["message"])

    def test_a_stopped_vm_counts_too(self):
        self.copying()
        self.vm("stopped", list(DVS))
        JOBS.cancel(self.ident, {}, by="a person")
        job = self.look()
        for name in DVS:
            self.assertTrue(self.kube.object(self.path(name)), name)
        self.assertIn(f"VM stopped uses {DVS[0]}, {DVS[1]}, so they are kept.", job["message"])
        self.assertNotIn("temporary storage", job["message"], "no disk went, so none of its storage did")

    def test_a_pod_mounting_the_disk_keeps_it_but_cdis_own_worker_does_not(self):
        self.copying()
        self.pod("backup-reader", DVS[0])
        self.bound_disk(DVS[1])   # CDI's upload pod mounts that disk's claim: it is the import's own
        JOBS.cancel(self.ident, {}, by="a person")
        job = self.look()
        self.assertTrue(self.kube.object(self.path(DVS[0])))
        self.assertIsNone(self.kube.object(self.path(DVS[1])))
        self.assertIn(f"Pod backup-reader uses {DVS[0]}, so it is kept.", job["message"])

    def test_with_no_answer_on_the_vms_nothing_is_removed(self):
        self.copying()
        self.kube.fail(VMS, 500, times=5)
        with self.assertRaisesRegex(ValueError, "disks are kept"):
            JOBS.cancel(self.ident, {}, by="a person")
        for name in DVS:
            self.assertTrue(self.kube.object(self.path(name)), "unread is not unused: " + name)

    def test_with_no_answer_on_the_pods_nothing_is_removed_either(self):
        self.copying()
        self.kube.fail(f"/api/v1/namespaces/{NS}/pods", 500, times=5)
        with self.assertRaisesRegex(ValueError, "disks are kept") as caught:
            JOBS.cancel(self.ident, {}, by="a person")
        self.assertIn("a VM or an app", str(caught.exception), "the pods are not called a VM")
        for name in DVS:
            self.assertTrue(self.kube.object(self.path(name)), "unread is not unused: " + name)

    # ------------------------------------------------------------ after the VM is made

    def made_vm_cleanup_waiting(self):
        """Through the import's own phases to a made VM, its clean-up held by a finalizer."""
        for name in DVS:
            self.bound_disk(name)
        self.to_convert()
        scratch = self.kube.object(CLAIMS + DVS[0] + "-scratch")
        scratch["metadata"]["finalizers"] = ["example.com/held"]
        self.kube.add(scratch)
        self.imp.prepare_vm = lambda cfg, *_: cfg
        self.imp._recheck_vm_creation = lambda _prepared: None
        self.imp.commit_vm = lambda prepared, send=None: self.vm("win11", list(DVS))
        self.phase("Succeeded")
        job = self.look(2)
        self.assertEqual(("running", "cleanup", "succeeded"),
                         (job["status"], self.ref()["phase"], self.ref()["cleanup_outcome"]), job["message"])
        self.assertTrue(job["cancellable"])
        return scratch

    def test_after_the_vm_is_made_cancel_keeps_its_disks_as_its_review_says(self):
        scratch = self.made_vm_cleanup_waiting()
        review = self.review()
        self.assertNotIn("remove its disks", review)
        for words in ("keep its disks", DVS[0], DVS[1], "are kept"):
            self.assertIn(words, review)
        self.kube.discard(CLAIMS + scratch["metadata"]["name"])   # its finalizer let go
        JOBS.cancel(self.ident, {}, by="a person")
        job = self.look()
        self.assertEqual("cancelled", job["status"], job["message"])
        for name in DVS:
            self.assertTrue(self.kube.object(self.path(name)), "the VM's disk: " + name)
            self.assertTrue(self.kube.object(CLAIMS + name), "and its claim: " + name)
        self.assertIn("temporary storage was cleared", job["message"])

    def test_after_the_vm_is_made_a_clean_up_still_waiting_keeps_going_and_the_disks_stay(self):
        self.made_vm_cleanup_waiting()
        with self.assertRaisesRegex(ValueError, "Its disks are kept"):
            JOBS.cancel(self.ident, {}, by="a person")
        for name in DVS:
            self.assertTrue(self.kube.object(self.path(name)), name)
        self.assertEqual("running", self.look()["status"], "the clean-up goes on by itself")


if __name__ == "__main__":
    unittest.main()
