"""An Unraid or Proxmox VM import that CDI never moves on ends, and lets go of the
VM's name: the "disks" and "convert" waits had no deadline and raise nothing, so
the job stayed "running" and its disk targets refused a retry. On the shared fake
API and the real job list, walking the import's own phases."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_joblogs as JOBLOGS
import homestead_job_errors as ERRORS
import homestead_kube as K
import homestead_operations as JOBS
import homestead_unraid_vms as UVMS
import homestead_vm_create as VM_CREATE
import test_unraid_vms as fixture
from fakes.kube import Kube

DISKS_LIMIT = 30 * 60
CONVERT_LIMIT = 6 * 60 * 60
DVS = ("win11-disk", "win11-disk-2")
NS = "lab"
CLAIMS = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/"
PODS = f"/api/v1/namespaces/{NS}/pods/"
JOB_API = f"/apis/batch/v1/namespaces/{NS}/jobs/"
SECRETS = f"/api/v1/namespaces/{NS}/secrets/"


class Restart(BaseException):
    """Homestead going away part way: nothing in the jobs loop catches this."""


def owned_by(kind, name, uid):
    return [{"kind": kind, "name": name, "uid": uid, "controller": True}]


class ImportWaitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(JOBS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(
            {"apiVersion": "v1", "kind": "Service", "metadata": {"namespace": "cdi", "name": "cdi-uploadproxy",
                                                                 "labels": {"cdi.kubevirt.io": "cdi-uploadproxy"}}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": "cdi", "name": "cdi-uploadproxy-signer-bundle"},
             "data": {"ca-bundle.crt": "a-ca"}},
            groups=("cdi.kubevirt.io", "kubevirt.io")))
        self.kube.react("POST", f"{UVMS.UPLOAD_API}/namespaces/{NS}/uploadtokenrequests",
                        lambda method, path, body: {"status": {"token": "a-token"}})
        for name in ("IMP", "kget", "ksend", "OPS"):
            self.addCleanup(setattr, UVMS, name, getattr(UVMS, name, None))
        self.imp = fixture.FakeImports(fixture.listing_lines())
        self.imp._get_or_none = lambda path: K.get_optional(path, read=K.kget)
        UVMS.bind(self.imp, K.kget, K.ksend, JOBS)
        self.enterContext(mock.patch.dict(JOBS.RESOLVERS, {}))
        self.enterContext(mock.patch.dict(JOBS.CANCELLERS, {}))
        # A pod's log is read through raw_get, which the fake API doesn't answer.
        self.enterContext(mock.patch.object(JOBLOGS, "ktext", lambda path: ""))
        UVMS.register(JOBS)
        self.now = 1000.0
        self.dvs = DVS
        # Hours of waits walked in few looks: a gap is an hour here, not 5 minutes.
        self.enterContext(mock.patch.object(ERRORS, "GAP", 3600))
        self.enterContext(mock.patch.object(UVMS, "_clock", lambda: self.now, create=True))
        # server.py binds this to its own cluster reader, so a run that has loaded server.py
        # and then cleared that reader would refuse to make the VM.
        self.enterContext(mock.patch.object(UVMS, "platform", lambda: {}))
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})["id"]

    def path(self, name):
        return f"{UVMS.CDI_API}/namespaces/{NS}/datavolumes/{name}"

    def phase(self, phase):
        for name in self.dvs:
            dv = self.kube.object(self.path(name))
            dv["status"] = {"phase": phase}
            self.kube.add(dv)

    def wait(self, seconds, every=50 * 60):
        """Time passes with the jobs loop looking at least every few minutes, as it does."""
        while seconds > every:
            self.now += every
            seconds -= every
            self.look()
        self.now += seconds

    def look(self, times=1):
        for _ in range(times):
            job = next(job for job in JOBS.list_operations() if job["id"] == self.ident)
        return job

    def ref(self):
        return next(item for item in JOBS._read() if item["id"] == self.ident)["ref"]

    def set_ref(self, **values):
        with JOBS.transaction():
            items = JOBS._read()
            next(item for item in items if item["id"] == self.ident)["ref"].update(values)
            JOBS._write(items)

    def to_convert(self):
        """Through the import's own phases: CDI is ready, the copies run and end."""
        self.phase("UploadReady")
        self.look()
        self.assertEqual("copy", self.ref()["phase"])
        for disk in self.ref()["disks"]:
            job = self.kube.object(JOB_API + disk["job"])
            job["status"] = {"succeeded": 1}
            self.kube.add(job)
        self.look()
        self.assertEqual("convert", self.ref()["phase"])
        return [(JOB_API + d["job"], SECRETS + d["job"] + "-upload") for d in self.ref()["disks"]]

    def claim(self, name, owner, status="Bound", volume=""):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": NS, "name": name, "annotations": {}, "ownerReferences": owner},
                       "spec": {"volumeName": volume} if volume else {}, "status": {"phase": status}})
        return self.kube.object(CLAIMS + name)["metadata"]["uid"]

    def upload_pod(self, name, owner, claims):
        self.kube.add({"apiVersion": "v1", "kind": "Pod",
                       "metadata": {"namespace": NS, "name": name, "labels": {"cdi.kubevirt.io": "cdi-upload-server"},
                                    "ownerReferences": owner},
                       "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": c}} for c in claims]},
                       "status": {"phase": "Running"}})
        return self.kube.object(PODS + name)["metadata"]["uid"]

    def populator_disk(self, name):
        """CDI with volume populators, as tests/test_cdi_import_cleanup.py has it: the
        target stays Pending until the very end and the copy is on its prime claim."""
        target = self.claim(name, owned_by("DataVolume", name, self.kube.object(self.path(name))["metadata"]["uid"]), "Pending")
        prime = "prime-" + target
        prime_uid = self.claim(prime, owned_by("PersistentVolumeClaim", name, target), volume="pv-" + name)
        pod = "upload-" + name
        pod_uid = self.upload_pod(pod, owned_by("PersistentVolumeClaim", prime, prime_uid), (prime, prime + "-scratch"))
        self.claim(prime + "-scratch", owned_by("Pod", pod, pod_uid), volume="pv-scratch-" + name)
        return [CLAIMS + name, CLAIMS + prime, PODS + pod, CLAIMS + prime + "-scratch"]

    def bound_disk(self, name):
        """CDI with no populator: the target itself is bound, and the pod hangs off it."""
        target = self.claim(name, owned_by("DataVolume", name, self.kube.object(self.path(name))["metadata"]["uid"]), volume="pv-" + name)
        pod = "upload-" + name
        pod_uid = self.upload_pod(pod, owned_by("PersistentVolumeClaim", name, target), (name, name + "-scratch"))
        self.claim(name + "-scratch", owned_by("Pod", pod, pod_uid), volume="pv-scratch-" + name)
        return [CLAIMS + name, PODS + pod, CLAIMS + name + "-scratch"]

    def assert_ended_with_everything_of_cdis_left(self, kept):
        copies = self.to_convert()
        self.assertEqual("CDI is finishing the disk", self.look()["message"])
        self.wait(CONVERT_LIMIT - 1)
        self.assertEqual("running", self.look()["status"], "a moment longer is still a wait")
        self.now += 1
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        for words in ("6 hours", "not finished", DVS[0], "under Import", "under another name", "Unraid", "unchanged"):
            self.assertIn(words, job["message"])
        self.assertNotIn("cleared", job["message"], "nothing of the disk's was cleared")
        self.assertNotIn("Volumes", job["message"], "that page lists no Pending claim and removes no DataVolume")
        self.assertTrue(job["message"].endswith("An admin can remove anything not wanted under Import > Disk images."), "what to do is whole, within a job's limit: " + job["message"])
        self.assertNotIn("kubectl", job["message"])
        for path in kept + [self.path(name) for name in DVS]:
            self.assertTrue(self.kube.object(path), path + " is CDI's or the disk's, and stays")
        for job_path, secret in copies:
            self.assertIsNone(self.kube.object(job_path), "the copy is over: its Job goes")
            self.assertIsNone(self.kube.object(secret), "and so does the Secret with its upload token")
        self.assertFalse(self.ref()["retain_resources"], "the job lets go of the name")
        return job

    def test_cdi_never_ready_ends_the_import_and_clears_what_it_made(self):
        self.phase("UploadScheduled")
        self.assertEqual("running", self.look()["status"])
        self.wait(DISKS_LIMIT - 1)
        self.assertEqual("running", self.look()["status"])
        self.now += 1
        job = self.look(3)
        self.assertEqual("failed", job["status"])
        self.assertIn("30 minutes", job["message"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])), "what it made goes, so a retry can run")
        UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})

    def test_time_not_watched_does_not_count_toward_the_ready_limit(self):
        self.phase("UploadScheduled")
        self.look()
        self.wait(DISKS_LIMIT - 60)
        self.look()
        self.now += 2 * 3600      # Homestead was down, or the clock stepped
        self.assertEqual("running", self.look()["status"], "the wait starts again after a gap")
        self.wait(DISKS_LIMIT - 1)
        self.assertEqual("running", self.look()["status"])
        self.wait(1)
        self.assertEqual("failed", self.look(3)["status"])

    def test_time_not_watched_does_not_count_toward_the_finish_limit(self):
        self.to_convert()
        self.look()
        self.wait(CONVERT_LIMIT - 60)
        self.look()
        self.now += 8 * 3600
        self.assertEqual("running", self.look()["status"], "the wait starts again after a gap")
        self.now -= 3600          # a clock stepped back is a gap too
        self.assertEqual("running", self.look()["status"])
        self.wait(CONVERT_LIMIT - 1)
        self.assertEqual("running", self.look()["status"])
        self.wait(1)
        self.assertEqual("failed", self.look()["status"])

    def test_clearing_takes_cdis_populator_claims_and_leaves_a_lookalike_alone(self):
        self.phase("UploadScheduled")
        made = self.populator_disk(DVS[0])
        self.claim("win11-disk-backup", [], volume="pv-mine")
        self.look()
        self.wait(DISKS_LIMIT)
        self.look(3)
        for path in made + [self.path(DVS[0])]:
            self.assertIsNone(self.kube.object(path), path)
        self.assertTrue(self.kube.object(CLAIMS + "win11-disk-backup"))

    def test_after_six_hours_a_populator_disk_is_left_as_it_is_and_the_job_ends(self):
        # The usual shape of a stuck finish: the claim the disk will end up on isn't bound yet.
        self.phase("UploadReady")
        kept = [path for name in DVS for path in self.populator_disk(name)]
        self.assert_ended_with_everything_of_cdis_left(kept)

    def test_after_six_hours_a_bound_disk_is_left_as_it_is_and_the_job_ends(self):
        self.phase("UploadReady")
        kept = [path for name in DVS for path in self.bound_disk(name)]
        self.assert_ended_with_everything_of_cdis_left(kept)

    def test_after_six_hours_a_disk_with_no_claim_at_all_ends_too(self):
        self.assert_ended_with_everything_of_cdis_left([])

    def test_the_finish_limit_counts_from_the_end_of_the_copy(self):
        self.phase("UploadScheduled")
        self.look()
        self.wait(DISKS_LIMIT - 1)   # slow to be ready, then a long copy
        self.phase("UploadReady")
        self.look()
        self.wait(2 * CONVERT_LIMIT)
        self.assertEqual("running", self.look()["status"], "a copy has no limit here")
        for disk in self.ref()["disks"]:
            job = self.kube.object(JOB_API + disk["job"])
            job["status"] = {"succeeded": 1}
            self.kube.add(job)
        self.assertEqual("running", self.look()["status"])
        self.assertEqual("convert", self.ref()["phase"])
        self.assertEqual("running", self.look()["status"], "time spent before this wait doesn't count towards it")
        self.wait(CONVERT_LIMIT - 1)
        self.assertEqual("running", self.look()["status"])
        self.now += 1
        self.assertEqual("failed", self.look()["status"])

    def test_the_copy_output_is_kept_when_its_job_goes(self):
        self.to_convert()
        self.look()
        self.wait(CONVERT_LIMIT)
        self.look()
        self.assertTrue(any("copy" in row["title"] for row in self.ref()["diagnostics"]), self.ref().get("diagnostics"))

    def test_a_disk_cdi_did_finish_is_named_as_finished(self):
        self.to_convert()
        dv = self.kube.object(self.path(DVS[0]))
        dv["status"] = {"phase": "Succeeded"}
        self.kube.add(dv)
        self.look()
        self.wait(CONVERT_LIMIT)
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertIn(f"CDI has not finished {DVS[1]} after", job["message"])
        self.assertIn(f"{DVS[0]} is finished", job["message"])
        self.assertTrue(job["message"].endswith("An admin can remove anything not wanted under Import > Disk images."), job["message"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def ended_with_vm_name(self, name, first_finished=False):
        """A fresh import under this VM name, past the six hours. Its disks are <name>-disk
        and <name>-disk-2: the first name is a prefix of the second."""
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": name})["id"]
        self.dvs = tuple(UVMS.disk_names(name, 2))
        self.to_convert()
        if first_finished:
            dv = self.kube.object(self.path(self.dvs[0]))
            dv["status"] = {"phase": "Succeeded"}
            self.kube.add(dv)
        self.look()
        self.wait(CONVERT_LIMIT)
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        return job["message"]

    def test_the_names_are_given_only_when_they_fit_to_the_last_character(self):
        self.assertEqual("x" * 500, UVMS._fit("x" * 500, "counted"))
        self.assertEqual("counted", UVMS._fit("x" * 501, "counted"))

    def test_past_the_limit_a_finished_disk_is_counted_too(self):
        message = self.ended_with_vm_name("a-long-vm-name-" + "y" * 48, first_finished=True)
        self.assertLessEqual(len(message), 500)
        self.assertTrue(message.startswith("CDI has not finished 1 disk after 6 hours"), message)
        self.assertIn("1 disk is finished and kept", message)
        self.assertNotIn(self.dvs[0], message)

    def test_a_cut_can_never_lose_what_to_do_at_any_name_length(self):
        # A whole name or none, and the end of the message always there: it is read as
        # the job list keeps it.
        kinds = set()
        for length in range(15, 64):   # either side of where the names stop fitting
            for first_finished in (False, True):
                with self.subTest(length=length, first_finished=first_finished):
                    message = self.ended_with_vm_name(("f" if first_finished else "n") * length, first_finished)
                    self.assertLessEqual(len(message), 500)
                    self.assertTrue(message.endswith("An admin can remove anything not wanted under Import > Disk images."), message)
                    self.assertNotIn("kubectl", message)
                    if self.dvs[1] in message:
                        kinds.add("named")
                    else:
                        kinds.add("counted")
                        self.assertNotIn(self.dvs[0], message)
        self.assertEqual({"named", "counted"}, kinds, "both sides of the limit were walked")

    def test_a_copy_job_held_on_a_finalizer_keeps_the_job_running_until_it_has_gone(self):
        copies = self.to_convert()
        job = self.kube.object(copies[0][0])
        job["metadata"]["finalizers"] = ["example.com/held"]
        self.kube.add(job)
        self.look()
        self.wait(CONVERT_LIMIT)
        seen = self.look(2)
        self.assertEqual("running", seen["status"], "the delete was accepted, but the Job is still there")
        self.assertIn("Still removing", seen["message"])
        self.assertTrue(self.kube.object(copies[0][0])["metadata"].get("deletionTimestamp"))
        self.assertTrue(self.ref()["retain_resources"])
        self.kube.discard(copies[0][0])   # its finalizer let go
        self.assertEqual("failed", self.look()["status"])
        self.assertFalse(self.ref()["retain_resources"])

    def test_a_copy_job_already_gone_is_no_obstacle(self):
        copies = self.to_convert()
        self.kube.discard(copies[0][0])   # its time to live is an hour
        self.look()
        self.wait(CONVERT_LIMIT)
        self.assertEqual("failed", self.look()["status"])
        self.assertIsNone(self.kube.object(copies[0][1]))

    def test_a_secret_someone_replaced_is_left_alone(self):
        copies = self.to_convert()
        secret = self.kube.object(copies[0][1])
        secret["metadata"]["annotations"] = {}
        self.kube.add(secret)
        self.look()
        self.wait(CONVERT_LIMIT)
        self.assertEqual("failed", self.look()["status"])
        self.assertTrue(self.kube.object(copies[0][1]))
        self.assertIsNone(self.kube.object(copies[1][1]))

    def test_a_copy_job_that_cant_be_removed_yet_is_retried_and_the_job_says_so(self):
        copies = self.to_convert()
        self.look()
        self.wait(CONVERT_LIMIT)
        self.kube.fail(copies[0][0], 500, method="DELETE")
        job = self.look()
        self.assertEqual("running", job["status"])
        self.assertIn("not finished", job["message"])
        self.assertIn("copy Job", job["message"])
        self.assertTrue(self.ref()["retain_resources"])
        with self.assertRaisesRegex(ValueError, "still running"):
            UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        self.phase("Succeeded")
        job = self.look()
        self.assertEqual("failed", job["status"], "what was decided stands: it isn't decided again")
        self.assertIsNone(self.kube.object(copies[0][0]))
        self.assertFalse(self.ref()["retain_resources"])

    def ending_held_up(self, times=1):
        """Past the limit, told the disks are kept, the copy Job not removed yet."""
        copies = self.to_convert()
        kept = self.populator_disk(DVS[0])
        self.look()
        self.wait(CONVERT_LIMIT)
        self.kube.fail(copies[0][0], 500, times=times, method="DELETE")
        job = self.look()
        self.assertEqual("running", job["status"])
        self.assertTrue(job["cancellable"])
        return copies, kept

    def test_cancel_while_ending_finishes_the_ending_and_keeps_the_disks_as_its_review_says(self):
        copies, kept = self.ending_held_up()
        plan = JOBS.cancel_plan(self.ident)
        review = " ".join([plan["action"], *plan["undo"], *plan["keeps"]])
        self.assertNotIn("remove its disks", review)
        for words in ("copy Job", DVS[0], DVS[1], "kept", "Unraid"):
            self.assertIn(words, review)
        JOBS.cancel(self.ident, {}, by="a person")
        job = self.look()
        self.assertEqual("cancelled", job["status"])
        for path in kept + [self.path(name) for name in DVS]:
            self.assertTrue(self.kube.object(path), path + ": the job said it stays")
        for job_path, secret in copies:
            self.assertIsNone(self.kube.object(job_path))
            self.assertIsNone(self.kube.object(secret))
        self.assertIn("Disk images", job["message"], "what to do next is still said")
        self.assertFalse(self.ref()["retain_resources"], "it lets go of the name")

    def test_cancel_while_ending_that_cant_remove_the_copy_job_says_so_and_the_ending_goes_on(self):
        copies, _ = self.ending_held_up(times=2)
        with self.assertRaisesRegex(ValueError, "copy Job"):
            JOBS.cancel(self.ident, {}, by="a person")
        self.assertEqual("leaving", self.ref()["phase"])
        job = self.look()
        self.assertEqual("failed", job["status"], "the ending finishes by itself")
        self.assertTrue(self.kube.object(self.path(DVS[0])))
        self.assertIsNone(self.kube.object(copies[0][0]))

    def test_cancel_before_the_ending_still_removes_the_disks_as_its_review_says(self):
        self.phase("UploadReady")
        self.look()
        self.assertIn("remove its disks", JOBS.cancel_plan(self.ident)["action"])
        JOBS.cancel(self.ident, {}, by="a person")
        self.assertEqual("cancelled", self.look()["status"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])))

    def test_one_disk_is_told_to_make_its_vm_and_check_it_boots(self):
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "single", "skip_disks": [1]})["id"]
        self.dvs = ("single-disk",)
        self.to_convert()
        self.look()
        self.wait(CONVERT_LIMIT)
        message = self.look()["message"]
        for words in ("create the VM from it under Import", "check that it starts", "bus, MAC and TPM", "under another name"):
            self.assertIn(words, message)
        self.assertNotIn("attach", message)
        self.assertTrue(message.endswith("An admin can remove anything not wanted under Import > Disk images."), message)

    def test_several_disks_are_not_promised_a_vm_with_all_of_them(self):
        self.to_convert()
        self.look()
        self.wait(CONVERT_LIMIT)
        message = self.look()["message"]
        for words in ("from one disk", "not with the others", "under another name"):
            self.assertIn(words, message)
        self.assertNotIn("make the VM from them", message)

    def real_disk_check(self):
        self.imp.vm_disk_import_plan = VM_CREATE.vm_disk_import_plan
        self.enterContext(mock.patch.object(VM_CREATE.I, "kget", K.kget))

    def ended_after_six_hours(self):
        self.to_convert()
        self.look()
        self.wait(CONVERT_LIMIT)
        self.assertEqual("failed", self.look()["status"])

    def test_a_retry_after_it_says_what_is_in_the_way(self):
        self.real_disk_check()
        self.ended_after_six_hours()
        before = [item["id"] for item in JOBS._read()]
        uid = self.kube.object(self.path(DVS[0]))["metadata"]["uid"]
        with self.assertRaises(ValueError) as refused:
            UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        for words in (DVS[0], "already exists", "earlier import", "under another name", "finished disk", "under Import"):
            self.assertIn(words, str(refused.exception))
        self.assertIn("can remove any disk no VM uses there", str(refused.exception))
        self.assertNotIn("Volumes", str(refused.exception), "that page removes no DataVolume")
        self.assertEqual(before, [item["id"] for item in JOBS._read()], "no job that can only fail")
        self.assertEqual(uid, self.kube.object(self.path(DVS[0]))["metadata"]["uid"])

    def test_it_lets_go_of_the_name_so_another_name_or_a_disk_removed_by_hand_can_run(self):
        self.real_disk_check()
        for name in DVS:
            self.populator_disk(name)
        self.ended_after_six_hours()
        other = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11b"})
        self.assertEqual("running", other["status"])
        self.assertTrue(self.kube.object(self.path("win11b-disk")))
        # Remove on the page has gone (tests/test_vm_disk_remove.py walks that): deleting
        # a DataVolume takes its claims and pods with it.
        for name in DVS:
            self.kube.discard(self.path(name))
        UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_an_older_import_with_no_receipt_ends_too_and_removes_nothing(self):
        self.phase("UploadScheduled")
        self.set_ref(cdi_cleanup=None)
        self.look()
        self.wait(DISKS_LIMIT)
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_an_older_import_past_six_hours_ends_too_and_removes_nothing(self):
        self.phase("UploadReady")
        self.set_ref(cdi_cleanup=None, phase="convert")
        self.look()
        self.wait(CONVERT_LIMIT)
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertIn("not finished", job["message"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_disks_that_become_ready_in_time_are_not_failed(self):
        self.phase("UploadScheduled")
        self.look()
        self.wait(DISKS_LIMIT - 1)
        self.phase("UploadReady")
        self.now += 1
        self.assertNotEqual("failed", self.look()["status"])
        self.assertEqual("copy", self.ref()["phase"])

    def test_a_disk_that_finishes_in_time_makes_the_vm(self):
        self.to_convert()
        self.look()
        self.wait(CONVERT_LIMIT - 1)
        self.phase("Succeeded")
        self.imp.prepare_vm = lambda cfg, platform, source: cfg
        self.imp._recheck_vm_creation = lambda prepared: None
        self.imp.commit_vm = mock.Mock()
        self.now += 1
        self.look()
        self.imp.commit_vm.assert_called_once()

    def finished_disks(self):
        self.to_convert()
        self.look()
        self.phase("Succeeded")
        self.imp.prepare_vm = lambda cfg, platform, source: cfg
        self.imp._recheck_vm_creation = lambda prepared: None
        self.imp.commit_vm = mock.Mock()

    def test_a_platform_that_cannot_be_read_waits_and_does_not_fail_the_import(self):
        # #1329: right after a restart, with no cached answer and the API unreachable,
        # reading the platform raises ValueError. That says nothing about the VM.
        self.finished_disks()

        def unread():
            raise ValueError("Homestead could not read the cluster's platform")
        with mock.patch.object(UVMS, "platform", unread):
            job = self.look()
        self.assertEqual("running", job["status"], job["message"])
        self.assertEqual("convert", self.ref()["phase"])
        self.imp.commit_vm.assert_not_called()
        self.assertTrue(self.kube.object(self.path(DVS[0])), "the disks are kept")
        self.look()
        self.imp.commit_vm.assert_called_once()

    def test_a_refused_vm_says_it_could_not_be_created(self):
        self.finished_disks()
        self.imp.commit_vm = mock.Mock(side_effect=ValueError("Kubernetes refused it (HTTP 422)"))
        job = self.look(3)
        self.assertIn("The disks arrived, but the VM could not be created: Kubernetes refused it (HTTP 422).",
                      job["message"])

    def walk(self, seconds, every=4 * 60):
        """Time passes with the jobs loop looking every few minutes."""
        while seconds > every:
            self.now += every
            seconds -= every
            self.look()
        self.now += seconds

    def refused(self):
        def observe(read, ns, work):
            raise ValueError("a temporary claim of the import is owned by something else")
        return mock.patch.object(UVMS.CDI_CLEANUP, "observe", observe)

    def test_a_lasting_refusal_while_cdi_finishes_counts_toward_its_limit(self):
        self.to_convert()
        self.look()
        with self.refused():
            self.assertTrue(self.look()["message"].startswith("Import paused: "))
            self.walk(CONVERT_LIMIT - 1)
            self.assertEqual("running", self.look()["status"])
            self.walk(1)
            job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertTrue(job["message"].startswith(
            "Homestead could not check the import's temporary storage for 6 hours. The last check said a temporary "
            "claim of the import is owned by something else. This import has stopped, and no VM was created."),
            job["message"])
        self.assertLess(len(job["message"].split(". ")[0]) + 1, 90, "the short version stands alone")
        self.assertTrue(job["message"].endswith(UVMS.REMOVE_THERE), job["message"])
        self.assertTrue(self.kube.object(self.path(DVS[0])), "nothing of the disks is removed")
        self.assertFalse(self.ref()["retain_resources"], "the job lets go of the name")

    def test_a_lasting_refusal_before_cdi_is_ready_counts_toward_its_limit(self):
        self.phase("UploadScheduled")
        self.look()
        with self.refused():
            self.walk(DISKS_LIMIT - 1)
            self.assertEqual("running", self.look()["status"])
            self.walk(1)
            job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("for 30 minutes", job["message"])
        self.assertTrue(self.kube.object(self.path(DVS[0])), "what it cannot check, it does not remove")

    SECRET = SECRETS + "win11-cloudinit"
    VM = f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachines/win11"

    def cut_off_after(self, *bodies):
        """A VM create that sends these POSTs and is then cut off (Homestead restarting)."""
        def commit(prepared, send=None):
            for path, body in bodies:
                send("POST", path, body)
            raise Restart()
        self.imp.commit_vm = commit
        with self.assertRaises(Restart):
            self.look()

    def secret_body(self):
        return (SECRETS.rstrip("/"), {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "win11-cloudinit", "namespace": NS}})

    def test_a_vm_create_cut_off_removes_what_it_created_by_uid_and_tries_again(self):
        # #1329: commit_vm keeps no record, so a restart part way left what it had created.
        self.finished_disks()
        self.cut_off_after(self.secret_body())
        made = self.kube.object(self.SECRET)["metadata"]["uid"]
        self.kube.writes.clear()
        self.imp.commit_vm = mock.Mock()
        job = self.look()
        deletes = [(path, body) for method, path, body, _ in self.kube.writes if method == "DELETE" and path == self.SECRET]
        self.assertEqual([(self.SECRET, {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": made}})],
                         deletes, "removed by the uid its POST answered with")
        self.assertIsNone(self.kube.object(self.SECRET))
        self.imp.commit_vm.assert_called_once()
        self.assertNotEqual("failed", job["status"], job["message"])

    def test_one_created_again_under_the_name_since_is_kept(self):
        self.finished_disks()
        self.cut_off_after(self.secret_body())
        self.kube.discard(self.SECRET)
        self.kube.add(self.secret_body()[1])      # someone's own, by the same name
        theirs = self.kube.object(self.SECRET)["metadata"]["uid"]
        self.imp.commit_vm = mock.Mock()
        self.look()
        self.assertEqual(theirs, self.kube.object(self.SECRET)["metadata"]["uid"])

    def test_a_vm_create_cut_off_once_the_vm_was_sent_keeps_everything_and_says_so(self):
        self.finished_disks()
        vm = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": "win11", "namespace": NS}}
        self.cut_off_after(self.secret_body(), (self.VM.rsplit("/", 1)[0], vm))
        self.imp.commit_vm = mock.Mock()
        job = self.look(3)
        self.imp.commit_vm.assert_not_called()
        self.assertNotEqual("done", self.ref()["phase"], "the import ends without claiming a VM")
        self.assertTrue(job["message"].startswith(
            "The VM create was cut off after Kubernetes accepted win11. win11 may exist, so it is kept with everything "
            "created for it. Check it under Virtual machines. The disks are kept as win11-disk, win11-disk-2."),
            job["message"])
        self.assertNotIn("could not be created", job["message"])
        self.assertTrue(self.kube.object(self.SECRET))
        self.assertTrue(self.kube.object(self.VM))
        self.assertTrue(self.kube.object(self.path(DVS[0])), "the disks are kept")

    def test_a_vm_sent_with_no_answer_may_exist_and_is_kept(self):
        self.finished_disks()
        self.kube.fail(self.VM.rsplit("/", 1)[0], 500, method="POST", applied=True)
        vm = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": "win11", "namespace": NS}}
        self.imp.commit_vm = lambda prepared, send=None: send("POST", self.VM.rsplit("/", 1)[0], vm)
        self.look()
        self.imp.commit_vm = mock.Mock()
        job = self.look(3)
        self.assertTrue(job["message"].startswith(
            "The VM create was cut off after win11 was sent with no answer. win11 may exist, so it is kept with "
            "everything created for it."), job["message"])

    def test_a_pause_after_the_vm_was_accepted_does_not_say_no_vm_was_created(self):
        self.finished_disks()
        vm = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": "win11", "namespace": NS}}
        self.cut_off_after((self.VM.rsplit("/", 1)[0], vm))
        with self.refused():
            self.walk(CONVERT_LIMIT)
            job = self.look()
        self.assertIn("This import has stopped. win11 may exist, so it is kept. Check it under Virtual machines.",
                      job["message"])
        self.assertNotIn("no VM was created", job["message"])

    def test_a_write_with_no_answer_is_kept_and_named(self):
        self.finished_disks()
        self.kube.fail(SECRETS.rstrip("/"), 500, method="POST", applied=True)   # landed, its answer lost
        self.imp.commit_vm = lambda prepared, send=None: send("POST", *self.secret_body())
        self.assertEqual("running", self.look()["status"], "no answer is no refusal: asked again")
        self.imp.commit_vm = mock.Mock()
        job = self.look(3)
        self.imp.commit_vm.assert_not_called()
        self.assertTrue(self.kube.object(self.SECRET), "not known to be this try's: kept")
        self.assertIn("The VM create was cut off part way. win11-cloudinit may be what it created, so it is kept. "
                      "Check before creating the VM.", job["message"])


if __name__ == "__main__":
    unittest.main()
