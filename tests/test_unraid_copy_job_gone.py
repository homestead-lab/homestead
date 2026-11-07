"""An Unraid or Proxmox VM import whose copy Job has gone isn't still copying. The Job
goes an hour after it ends, or by hand, and that can't be told apart afterwards, so
the import ends without claiming which, keeps the disks and lets go of the name. On
the shared fake API and the real job list, walking the import's own phases."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_operations as JOBS
import homestead_unraid_vms as UVMS
import homestead_vm_create as VM_CREATE
import test_unraid_vms as fixture
from fakes.kube import Kube

DVS = ("win11-disk", "win11-disk-2")
NS = "lab"
CLAIMS = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/"
PODS = f"/api/v1/namespaces/{NS}/pods/"
JOB_API = f"/apis/batch/v1/namespaces/{NS}/jobs/"
CLEAR_LIMIT = 30 * 60
SECRETS = f"/api/v1/namespaces/{NS}/secrets/"


class Crash(BaseException):
    """Homestead stops here: nothing in it gets to handle this or save anything more."""


class CopyJobGoneTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(JOBS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(
            {"apiVersion": "v1", "kind": "Service", "metadata": {"namespace": "cdi", "name": "cdi-uploadproxy",
                                                                 "labels": {"cdi.kubevirt.io": "cdi-uploadproxy"}}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": "cdi", "name": "cdi-uploadproxy-signer-bundle"},
             "data": {"ca-bundle.crt": "a-ca"}},
            groups=("cdi.kubevirt.io", "kubevirt.io")))
        self.tokens_before_crash = None
        self.kube.react("POST", f"{UVMS.UPLOAD_API}/namespaces/{NS}/uploadtokenrequests", self.token)
        for name in ("IMP", "kget", "ksend", "OPS"):
            self.addCleanup(setattr, UVMS, name, getattr(UVMS, name, None))
        self.imp = fixture.FakeImports(fixture.listing_lines())
        self.imp._get_or_none = lambda path: K.get_optional(path, read=K.kget)
        UVMS.bind(self.imp, K.kget, K.ksend, JOBS)
        self.enterContext(mock.patch.dict(JOBS.RESOLVERS, {}))
        self.enterContext(mock.patch.dict(JOBS.CANCELLERS, {}))
        UVMS.register(JOBS)
        self.now = 1000.0
        self.enterContext(mock.patch.object(UVMS, "_clock", lambda: self.now))
        # server.py binds this to its own cluster reader, so a run that has loaded server.py
        # and then cleared that reader would refuse to make the VM.
        self.enterContext(mock.patch.object(UVMS, "platform", lambda: {}))
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})["id"]

    def token(self, method, path, body):
        if self.tokens_before_crash == 0:
            self.tokens_before_crash = None
            raise Crash()
        if self.tokens_before_crash:
            self.tokens_before_crash -= 1
        return {"status": {"token": "a-token"}}

    def path(self, name):
        return f"{UVMS.CDI_API}/namespaces/{NS}/datavolumes/{name}"

    def phase(self, phase, names=DVS):
        for name in names:
            dv = self.kube.object(self.path(name))
            dv["status"] = {"phase": phase}
            self.kube.add(dv)

    def look(self):
        return next(job for job in JOBS.list_operations() if job["id"] == self.ident)

    def ref(self):
        return next(item for item in JOBS._read() if item["id"] == self.ident)["ref"]

    def set_ref(self, **values):
        with JOBS.transaction():
            items = JOBS._read()
            next(item for item in items if item["id"] == self.ident)["ref"].update(values)
            JOBS._write(items)

    def to_copy(self):
        """Through the import's own phases: CDI is ready and the copies start."""
        self.phase("UploadReady")
        self.look()
        self.assertEqual("copy", self.ref()["phase"])
        return [JOB_API + disk["job"] for disk in self.ref()["disks"]]

    def job_ended(self, path, **status):
        job = self.kube.object(path)
        job["status"] = status
        self.kube.add(job)

    def real_disk_check(self):
        self.imp.vm_disk_import_plan = VM_CREATE.vm_disk_import_plan
        self.enterContext(mock.patch.object(VM_CREATE.I, "kget", K.kget))

    def assert_kept_and_let_go(self, jobs):
        for name in DVS:
            self.assertTrue(self.kube.object(self.path(name)), name + " stays")
        for path in jobs:
            self.assertIsNone(self.kube.object(path), "its Job goes")
            self.assertIsNone(self.kube.object(SECRETS + path.rsplit("/", 1)[-1] + "-upload"),
                              "and the Secret holding its upload token: nothing else removes that")
        self.assertFalse(self.ref()["retain_resources"], "the job lets go of the name")

    def claim(self, name, owner, status="Bound", volume=""):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": NS, "name": name, "annotations": {}, "ownerReferences": owner},
                       "spec": {"volumeName": volume} if volume else {}, "status": {"phase": status}})
        return self.kube.object(CLAIMS + name)["metadata"]["uid"]

    def cdi_disk(self, name, target_status="Bound", prime_status="Lost"):
        """What CDI leaves of a disk with volume populators once it has finished: the target
        on its volume, the old prime claim Lost, its worker kept by the retain annotation and
        that worker's scratch. Returns the disk's own paths and CDI's temporary ones."""
        owner = lambda kind, who, uid: [{"kind": kind, "name": who, "uid": uid, "controller": True}]
        volume = "pv-" + name
        target = self.claim(name, owner("DataVolume", name, self.kube.object(self.path(name))["metadata"]["uid"]),
                            target_status, volume)
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolume",
                       "metadata": {"name": volume},
                       "spec": {"claimRef": {"namespace": NS, "name": name, "uid": target},
                                "persistentVolumeReclaimPolicy": "Delete",
                                "csi": {"driver": "driver.longhorn.io", "volumeHandle": volume}},
                       "status": {"phase": "Bound"}})
        prime = "prime-" + target
        prime_uid = self.claim(prime, owner("PersistentVolumeClaim", name, target), prime_status, volume)
        pod = "upload-" + name
        self.kube.add({"apiVersion": "v1", "kind": "Pod",
                       "metadata": {"namespace": NS, "name": pod, "labels": {"cdi.kubevirt.io": "cdi-upload-server"},
                                    "ownerReferences": owner("PersistentVolumeClaim", prime, prime_uid)},
                       "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": prime}},
                                            {"persistentVolumeClaim": {"claimName": prime + "-scratch"}}]},
                       "status": {"phase": "Succeeded"}})
        pod_uid = self.kube.object(PODS + pod)["metadata"]["uid"]
        self.claim(prime + "-scratch", owner("Pod", pod, pod_uid), volume="pv-scratch-" + name)
        return ([CLAIMS + name, "/api/v1/persistentvolumes/" + volume],
                [CLAIMS + prime, PODS + pod, CLAIMS + prime + "-scratch"])

    def finished_and_gone(self, **shape):
        jobs = self.to_copy()
        self.phase("Succeeded")
        kept, temporary = [], []
        for name in DVS:
            own, cdis = self.cdi_disk(name, **shape)
            kept += own
            temporary += cdis
        for path in jobs:
            self.kube.discard(path)
        return jobs, kept, temporary

    def end(self, rounds=5):
        for _ in range(rounds):
            job = self.look()
            if job["status"] != "running":
                break
        return job

    # ------------------------------------------------------------ what a gone Job means

    def test_a_job_gone_while_cdi_has_not_finished_says_what_is_known(self):
        jobs = self.to_copy()
        for path in jobs:
            self.kube.discard(path)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertEqual(
            "The copy Jobs for win11-disk, win11-disk-2 are gone, so Homestead can't tell how those copies ended. "
            "No VM was made. The VM on Unraid is unchanged. "
            "No disk was removed. If CDI does finish, a VM can be made from one disk under Import, and Homestead can't "
            "attach the others yet; otherwise import again under another name. An admin can remove what you don't want under Import > Disk images.",
            job["message"])
        self.assert_kept_and_let_go(jobs)

    def test_a_job_gone_when_cdi_reports_the_disks_finished_still_makes_no_vm(self):
        # The copy's own exit code is the only proof of a whole copy: curl ends an upload
        # cleanly when ssh dies, and only the pipeline's status says so. That went with the Job.
        self.imp.commit_vm = mock.Mock()
        jobs = self.to_copy()
        self.phase("Succeeded")
        for path in jobs:
            self.kube.discard(path)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.imp.commit_vm.assert_not_called()
        self.assertEqual(
            "The copy Jobs for win11-disk, win11-disk-2 are gone, so Homestead can't tell how those copies ended. "
            "No VM was made. The VM on Unraid is unchanged. "
            "CDI reports them finished, but that doesn't prove all the data arrived. They are kept "
            "and hold their storage. A VM can be made from one disk under Import, and Homestead can't attach the "
            "others yet; otherwise import again under another name. Or an admin can remove them under Import > Disk images"
            "; temporary storage not all cleared", job["message"])
        self.assertNotIn("kubectl", job["message"])
        self.assertNotIn("Volumes", job["message"], "that page removes no DataVolume")
        self.assert_kept_and_let_go(jobs)

    # ------------------------------------------------------------ all finished: the storage comes back

    def test_finished_disks_stay_and_cdis_workers_and_scratch_are_cleared(self):
        self.imp.commit_vm = mock.Mock()
        jobs, kept, temporary = self.finished_and_gone()
        job = self.end()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("under Import > Disk images; temporary storage cleared"), job["message"])
        self.assertIn("The VM on Unraid is unchanged", job["message"])
        self.imp.commit_vm.assert_not_called()
        for path in temporary:
            self.assertIsNone(self.kube.object(path), path + " is temporary and holds storage")
        for path in kept:
            self.assertTrue(self.kube.object(path), path + " is the disk")
        self.assert_kept_and_let_go(jobs)

    def test_the_copy_output_is_saved_before_the_clearing(self):
        self.finished_and_gone()
        self.end()
        self.assertTrue(any("copy" in row["title"] for row in self.ref()["diagnostics"]), self.ref().get("diagnostics"))

    def test_not_finished_touches_nothing_of_cdis(self):
        jobs = self.to_copy()
        self.phase("Succeeded", DVS[:1])
        kept, temporary = [], []
        for name in DVS:
            own, cdis = self.cdi_disk(name)
            kept += own
            temporary += cdis
        for path in jobs:
            self.kube.discard(path)
        job = self.end()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertNotIn("temporary", job["message"])
        self.assertIn(f"CDI reports {DVS[0]} finished, and it is kept.", job["message"])
        self.assertTrue(job["message"].endswith("An admin can remove what you don't want under Import > Disk images."), job["message"])
        self.assertNotIn("kubectl", job["message"])
        for path in kept + temporary:
            self.assertTrue(self.kube.object(path), path)
        self.assert_kept_and_let_go(jobs)

    def test_a_claim_not_bound_when_it_is_decided_ends_it_and_touches_nothing_of_cdis(self):
        # The keep-disks clean-up raises on such a claim, which would hold the job running for ever.
        jobs = self.to_copy()
        self.phase("Succeeded")
        one, temporary = self.cdi_disk(DVS[0])
        two, more = self.cdi_disk(DVS[1], target_status="Pending")
        for path in jobs:
            self.kube.discard(path)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("; temporary storage not all cleared"), job["message"])
        for path in one + two + temporary + more:
            self.assertTrue(self.kube.object(path), path)
        self.assert_kept_and_let_go(jobs)

    def test_a_failed_read_of_the_claims_is_not_a_decision(self):
        jobs, kept, temporary = self.finished_and_gone()
        self.kube.fail(f"/api/v1/namespaces/{NS}/persistentvolumeclaims", 500, method="GET", skip=1)
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        for path in temporary:
            self.assertTrue(self.kube.object(path), path)
        self.assertEqual("failed", self.end()["status"])

    def test_a_claim_that_stops_being_bound_during_the_clearing_ends_it_too(self):
        jobs, kept, temporary = self.finished_and_gone()
        self.kube.fail(temporary[1], 500, method="DELETE")
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertEqual(99, job["progress"])
        self.assertTrue(self.ref()["retain_resources"])
        claim = self.kube.object(kept[0])
        claim["status"] = {"phase": "Pending"}
        self.kube.add(claim)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("; temporary storage not all cleared"), job["message"])
        for path in kept:
            self.assertTrue(self.kube.object(path), path)
        self.assert_kept_and_let_go(jobs)

    def test_a_clearing_kubernetes_cant_answer_is_retried_and_holds_the_name(self):
        jobs, kept, temporary = self.finished_and_gone()
        self.kube.fail(temporary[1], 500, method="DELETE")
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertTrue(self.ref()["retain_resources"], "the job holds the name until it's done")
        job = self.end()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("; temporary storage cleared"), job["message"])
        self.assertFalse(self.ref()["retain_resources"])

    def test_a_lost_reply_removing_a_worker_carries_on_next_round(self):
        jobs, kept, temporary = self.finished_and_gone()
        self.kube.fail(temporary[1], OSError("lost"), method="DELETE", applied=True)
        job = self.end()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("; temporary storage cleared"), job["message"])
        for path in temporary:
            self.assertIsNone(self.kube.object(path), path)

    def test_someone_elses_secret_stops_the_clearing_but_not_the_ending(self):
        jobs, kept, temporary = self.finished_and_gone()
        secret = SECRETS + jobs[0].rsplit("/", 1)[-1] + "-upload"
        mine = self.kube.object(secret)
        mine["metadata"]["annotations"] = {}
        self.kube.add(mine)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("; temporary storage not all cleared"), job["message"])
        self.assertTrue(self.kube.object(secret))
        self.assertFalse(self.ref()["retain_resources"])

    def test_a_clearing_that_never_completes_ends_at_its_limit(self):
        # The old prime claim goes only once CDI has marked it Lost. If it never does, the job can't wait for ever.
        jobs, kept, temporary = self.finished_and_gone(prime_status="Bound")
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertEqual("clearing", self.ref()["phase"])
        self.now += CLEAR_LIMIT - 1
        self.assertEqual("running", self.look()["status"])
        self.now += 1
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("; temporary storage not all cleared"), job["message"])
        for path in kept:
            self.assertTrue(self.kube.object(path), path)
        self.assert_kept_and_let_go(jobs)

    def vm_on_the_disk(self, disk):
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"namespace": NS, "name": "by-hand"},
                       "spec": {"template": {"spec": {"volumes": [{"name": "root", "dataVolume": {"name": disk}}]}}}})

    def test_cancel_during_the_clearing_finishes_it_and_keeps_the_disks_as_its_review_says(self):
        jobs, kept, temporary = self.finished_and_gone()
        self.kube.fail(temporary[1], 500, method="DELETE")
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertTrue(job["cancellable"])
        plan = JOBS.cancel_plan(self.ident)
        self.assertEqual("Finish ending the import and keep its disks", plan["action"])
        self.assertIn("temporary", " ".join(plan["undo"]))
        self.assertIn(DVS[0], " ".join(plan["keeps"]))
        JOBS.cancel(self.ident, {}, by="a person")
        job = self.look()
        self.assertEqual("cancelled", job["status"], job["message"])
        for path in kept + [self.path(name) for name in DVS]:
            self.assertTrue(self.kube.object(path), path)
        for path in temporary:
            self.assertIsNone(self.kube.object(path), path)
        self.assert_kept_and_let_go(jobs)

    def test_cancel_during_a_clearing_that_cant_finish_says_so_and_a_vm_keeps_its_disk(self):
        # The job told the person to make a VM from these disks, and someone has.
        jobs, kept, temporary = self.finished_and_gone(prime_status="Bound")
        self.assertEqual("running", self.look()["status"])
        self.vm_on_the_disk(DVS[0])
        with self.assertRaisesRegex(ValueError, "disks are kept"):
            JOBS.cancel(self.ident, {}, by="a person")
        job = self.look()
        self.assertEqual("running", job["status"], "the ending goes on: " + job["message"])
        self.assertEqual("clearing", self.ref()["phase"])
        for path in kept + [self.path(name) for name in DVS]:
            self.assertTrue(self.kube.object(path), path)
        self.now += CLEAR_LIMIT
        self.assertEqual("failed", self.look()["status"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_a_failed_read_during_the_clearing_is_retried_and_doesnt_end_it(self):
        jobs, kept, temporary = self.finished_and_gone(prime_status="Bound")
        self.look()
        self.assertEqual("clearing", self.ref()["phase"])
        for answer in (500, TimeoutError("no answer")):
            with self.subTest(answer=answer):
                self.kube.fail(f"/api/v1/namespaces/{NS}/persistentvolumeclaims", answer, method="GET")
                job = self.look()
                self.assertEqual("running", job["status"], job["message"])
                self.assertIn("cleanup pending", job["message"])
                self.assertEqual("clearing", self.ref()["phase"])
                self.assertTrue(self.ref()["retain_resources"])
        for prime in (temporary[0], temporary[3]):   # CDI catches up
            claim = self.kube.object(prime)
            claim["status"] = {"phase": "Lost"}
            self.kube.add(claim)
        job = self.end()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].endswith("; temporary storage cleared"), job["message"])

    def one_disk_gone(self, finished):
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "solo", "skip_disks": [1]})["id"]
        self.phase("UploadReady", ("solo-disk",))
        self.look()
        self.assertEqual(1, len(self.ref()["disks"]))
        if finished:
            self.phase("Succeeded", ("solo-disk",))
        self.kube.discard(JOB_API + self.ref()["disks"][0]["job"])
        return self.look()["message"]

    def test_one_disk_is_told_to_make_its_vm_and_check_it_boots(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                if finished:
                    self.kube.discard(self.path("solo-disk"))
                message = self.one_disk_gone(finished)
                self.assertIn("ake the VM from it under Import and check it boots: its bus, MAC and TPM aren't carried "
                              "over. If not, import again under another name.", message)
                self.assertNotIn("can't attach", message)

    def test_several_disks_are_not_promised_a_vm_with_all_of_them(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                message, _ = self.gone_under("many" + str(finished).lower(), finished=finished)
                self.assertIn("VM can be made from one disk under Import, and Homestead can't attach the others yet; "
                              "otherwise import again under another name.", message)
                self.assertNotIn("the VM under Import", message)

    def test_an_older_import_with_no_receipt_clears_nothing_either(self):
        jobs = self.to_copy()
        self.phase("Succeeded")
        _, temporary = self.cdi_disk(DVS[0])
        self.set_ref(cdi_cleanup=None)
        for path in jobs:
            self.kube.discard(path)
        writes = len(self.kube.writes)
        self.assertEqual("failed", self.look()["status"])
        self.assertEqual(writes, len(self.kube.writes))
        for path in temporary:
            self.assertTrue(self.kube.object(path), path)

    def test_a_retry_after_the_clearing_is_refused_by_the_disk_not_the_job(self):
        self.real_disk_check()
        self.finished_and_gone()
        self.assertEqual("failed", self.end()["status"])
        with self.assertRaises(ValueError) as refused:
            UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        self.assertIn("already exists", str(refused.exception))
        self.assertNotIn("still active", str(refused.exception))

    def test_one_disk_is_named_once_and_the_rest_of_the_import_goes_with_it(self):
        jobs = self.to_copy()
        self.job_ended(jobs[1], succeeded=1)
        self.kube.discard(jobs[0])
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["message"].startswith(
            "The copy Job for win11-disk is gone, so Homestead can't tell how that copy ended."),
            job["message"])
        self.assert_kept_and_let_go(jobs)

    def gone_under(self, name, finished=False):
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": name})["id"]
        names = (name + "-disk", name + "-disk-2")
        self.phase("UploadReady", names)
        self.look()
        if finished:
            self.phase("Succeeded", names)
        for disk in self.ref()["disks"]:
            self.kube.discard(JOB_API + disk["job"])
        job = self.look()
        self.assertEqual("failed", job["status"])
        return job["message"], names

    def test_the_disks_are_named_within_a_jobs_limit(self):
        # A job's message is cut at 500 characters.
        message, names = self.gone_under("w" * 20)
        self.assertIn(f"{names[0]}, {names[1]}", message)
        self.assertTrue(message.endswith("An admin can remove what you don't want under Import > Disk images."), message)

    def test_a_finished_message_fits_a_jobs_limit(self):
        message, _ = self.gone_under("x" * 15, finished=True)
        self.assertTrue(message.endswith("not all cleared"), message)

    def test_a_proxmox_import_names_its_own_source(self):
        jobs = self.to_copy()
        self.set_ref(kind="proxmox")
        for path in jobs:
            self.kube.discard(path)
        job = self.look()
        self.assertIn("The VM on Proxmox is unchanged", job["message"])

    def test_the_end_keeps_cdis_objects_and_the_disks_claims(self):
        jobs = self.to_copy()
        uid = self.kube.object(self.path(DVS[0]))["metadata"]["uid"]
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": NS, "name": DVS[0], "annotations": {}, "ownerReferences": [
                           {"kind": "DataVolume", "name": DVS[0], "uid": uid, "controller": True}]},
                       "status": {"phase": "Pending"}})
        for path in jobs:
            self.kube.discard(path)
        self.assertEqual("failed", self.look()["status"])
        self.assertTrue(self.kube.object(CLAIMS + DVS[0]))
        deleted = [path for method, path, *_ in self.kube.writes if method == "DELETE"]
        self.assertTrue(deleted)
        for path in deleted:
            self.assertTrue(path.startswith(SECRETS) or path.startswith(JOB_API), path)

    def test_the_copy_output_is_still_there_to_read(self):
        jobs = self.to_copy()
        for path in jobs:
            self.kube.discard(path)
        self.look()
        self.assertTrue(any("copy" in row["title"] for row in self.ref()["diagnostics"]), self.ref().get("diagnostics"))

    def test_a_job_that_has_failed_says_so_and_clears_as_it_always_did(self):
        # With no proof of either, the one Job that did say how it ended is believed.
        jobs = self.to_copy()
        self.kube.discard(jobs[0])
        self.job_ended(jobs[1], failed=1)
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertIn("failed:", job["message"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])))

    def test_cdi_failing_the_disk_clears_as_it_always_did(self):
        jobs = self.to_copy()
        self.kube.discard(jobs[0])
        self.phase("Failed")
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertIn("CDI could not take the disk", job["message"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])))

    # ------------------------------------------------------------ what isn't a gone Job

    def test_a_read_that_fails_is_not_a_gone_job(self):
        jobs = self.to_copy()
        for answer in (500, 403, 503, TimeoutError("no answer"), OSError("connection reset")):
            with self.subTest(answer=answer):
                self.kube.fail(jobs[0], answer, method="GET")
                writes = len(self.kube.writes)
                job = self.look()
                self.assertEqual("running", job["status"], job["message"])
                self.assertNotIn("gone", job["message"])
                self.assertEqual("copy", self.ref()["phase"])
                self.assertEqual(writes, len(self.kube.writes), "nothing is removed on a failed read")
                self.assertTrue(self.kube.object(jobs[0]))
        self.assertEqual("running", self.look()["status"])
        self.job_ended(jobs[0], succeeded=1)
        self.job_ended(jobs[1], succeeded=1)
        self.assertEqual("running", self.look()["status"])
        self.assertEqual("convert", self.ref()["phase"])

    def test_a_job_just_made_is_read_as_copying(self):
        self.to_copy()
        job = self.look()
        self.assertEqual("running", job["status"])
        self.assertIn("Copying from nas", job["message"])

    def test_a_lost_reply_making_a_job_never_reaches_the_copy(self):
        # So the copy phase only holds Jobs Kubernetes said it made.
        self.phase("UploadReady")
        self.kube.fail(f"/apis/batch/v1/namespaces/{NS}/jobs", OSError("lost"), method="POST", applied=True)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("Could not start the disk copy", job["message"])
        self.assertNotIn("gone", job["message"])

    def test_a_crash_after_making_the_jobs_is_setup_interrupted_not_a_gone_job(self):
        self.phase("UploadReady")
        with mock.patch.object(JOBS, "_finish", side_effect=Crash):
            with self.assertRaises(Crash):
                self.look()
        self.assertEqual("disks", self.ref()["phase"], "the Jobs were made and the phase was never saved")
        self.assertTrue(all(disk["job"] for disk in self.ref()["disks"]))
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("Copy setup was interrupted", job["message"])
        self.assertNotIn("gone", job["message"])

    def test_a_crash_between_two_jobs_is_setup_interrupted_too(self):
        self.phase("UploadReady")
        self.tokens_before_crash = 1
        with self.assertRaises(Crash):
            self.look()
        self.assertEqual("disks", self.ref()["phase"])
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("Copy setup was interrupted", job["message"])

    # ------------------------------------------------------------ a Job seen finished

    def test_a_job_seen_finished_and_then_removed_by_its_time_limit_stays_finished(self):
        # A disk finishing over an hour before the next is no outage: Homestead saw it
        # end. This is what got stuck for ever, and must not now fail a good import.
        self.imp.prepare_vm = lambda cfg, platform, source: cfg
        self.imp._recheck_vm_creation = lambda prepared: None
        self.imp.commit_vm = mock.Mock()
        jobs = self.to_copy()
        self.job_ended(jobs[0], succeeded=1)
        before = self.look()
        self.assertEqual("running", before["status"])
        self.kube.discard(jobs[0])
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertNotIn("gone", job["message"])
        self.assertEqual(before["progress"], job["progress"], "the finished disk keeps its progress")
        self.job_ended(jobs[1], succeeded=1)
        self.look()
        self.assertEqual("convert", self.ref()["phase"])
        self.phase("Succeeded")
        self.look()
        self.imp.commit_vm.assert_called_once()

    def test_a_disk_seen_finished_is_not_read_again(self):
        jobs = self.to_copy()
        self.job_ended(jobs[0], succeeded=1)
        self.look()
        self.kube.fail(jobs[0], 500, method="GET", times=50)
        job = self.look()
        self.assertEqual("running", job["status"])
        self.assertNotIn("temporarily", job["message"])

    # ------------------------------------------------------------ the others still copying

    def test_a_copy_still_running_is_left_to_end_and_the_job_says_it_will_stop(self):
        jobs = self.to_copy()
        self.kube.discard(jobs[0])
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        for words in ("Copying from nas", "The copy Job for win11-disk is gone", "this import stops when the others end"):
            self.assertIn(words, job["message"])
        self.assertTrue(self.kube.object(jobs[1]), "a working copy isn't stopped")
        self.assertTrue(job["cancellable"])
        self.job_ended(jobs[1], succeeded=1)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].startswith("The copy Job for win11-disk is gone"), job["message"])
        self.assert_kept_and_let_go(jobs)

    def test_a_copy_failing_after_another_has_gone_clears_as_it_always_did(self):
        jobs = self.to_copy()
        self.kube.discard(jobs[0])
        self.assertEqual("running", self.look()["status"])
        self.job_ended(jobs[1], failed=1)
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertIn("failed:", job["message"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])))

    def test_cancel_while_the_other_copy_runs_removes_what_it_made(self):
        jobs = self.to_copy()
        self.kube.discard(jobs[0])
        self.look()
        JOBS.cancel(self.ident, {}, by="a person")
        self.assertEqual("cancelled", self.look()["status"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])))
        self.assertIsNone(self.kube.object(jobs[1]))

    # ------------------------------------------------------------ ending takes writes

    def test_a_lost_reply_removing_the_jobs_ends_the_same_way_next_round(self):
        jobs = self.to_copy()
        self.kube.discard(jobs[0])
        self.job_ended(jobs[1], succeeded=1)
        self.kube.fail(jobs[1], OSError("lost"), method="DELETE", applied=True)
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertTrue(job["message"].startswith("The copy Job for win11-disk is gone"), job["message"])
        self.assertIn("Still removing", job["message"])
        self.assertEqual("leaving", self.ref()["phase"], "what was decided stands")
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].startswith("The copy Job for win11-disk is gone"), job["message"])
        self.assertNotIn("Jobs for", job["message"], "and it isn't decided again from the Job it removed")
        self.assert_kept_and_let_go(jobs)

    def test_a_secret_that_cant_be_removed_yet_holds_the_name_and_is_retried(self):
        self.real_disk_check()
        jobs = self.to_copy()
        for path in jobs:
            self.kube.discard(path)
        secret = SECRETS + jobs[0].rsplit("/", 1)[-1] + "-upload"
        self.kube.fail(secret, 500, method="DELETE")
        job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertTrue(self.ref()["retain_resources"])
        self.assertEqual("failed", self.look()["status"])
        self.assertIsNone(self.kube.object(secret))
        self.assertFalse(self.ref()["retain_resources"])

    def test_someone_elses_secret_under_that_name_is_left_alone(self):
        jobs = self.to_copy()
        for path in jobs:
            self.kube.discard(path)
        secret = SECRETS + jobs[0].rsplit("/", 1)[-1] + "-upload"
        mine = self.kube.object(secret)
        mine["metadata"]["annotations"] = {}
        self.kube.add(mine)
        self.assertEqual("failed", self.look()["status"])
        self.assertTrue(self.kube.object(secret))

    # ------------------------------------------------------------ afterwards

    def ended(self):
        jobs = self.to_copy()
        for path in jobs:
            self.kube.discard(path)
        self.assertEqual("failed", self.look()["status"])

    def test_a_retry_under_the_same_name_says_what_is_in_the_way(self):
        self.real_disk_check()
        self.ended()
        before = [item["id"] for item in JOBS._read()]
        with self.assertRaises(ValueError) as refused:
            UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        for words in (DVS[0], "already exists", "earlier import", "under another name", "under Import"):
            self.assertIn(words, str(refused.exception))
        self.assertNotIn("still active", str(refused.exception), "no job holds it: the disk does")
        self.assertEqual(before, [item["id"] for item in JOBS._read()], "no job that can only fail")

    def test_a_retry_runs_under_another_name_or_once_the_disks_are_removed(self):
        self.real_disk_check()
        self.ended()
        other = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11b"})
        self.assertEqual("running", other["status"])
        self.assertTrue(self.kube.object(self.path("win11b-disk")))
        for name in DVS:
            self.kube.discard(self.path(name))
        again = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        self.assertEqual("running", again["status"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_an_older_import_with_no_receipt_ends_too_and_removes_nothing(self):
        jobs = self.to_copy()
        secret = SECRETS + jobs[0].rsplit("/", 1)[-1] + "-upload"
        self.set_ref(cdi_cleanup=None)
        for path in jobs:
            self.kube.discard(path)
        writes = len(self.kube.writes)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("are gone", job["message"])
        self.assertEqual(writes, len(self.kube.writes))
        self.assertTrue(self.kube.object(self.path(DVS[0])))
        self.assertTrue(self.kube.object(secret))


if __name__ == "__main__":
    unittest.main()
