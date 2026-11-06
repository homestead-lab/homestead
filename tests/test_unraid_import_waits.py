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
        self.enterContext(mock.patch.object(UVMS, "_clock", lambda: self.now, create=True))
        self.ident = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})["id"]

    def path(self, name):
        return f"{UVMS.CDI_API}/namespaces/{NS}/datavolumes/{name}"

    def phase(self, phase):
        for name in self.dvs:
            dv = self.kube.object(self.path(name))
            dv["status"] = {"phase": phase}
            self.kube.add(dv)

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
        self.now += CONVERT_LIMIT - 1
        self.assertEqual("running", self.look()["status"], "a moment longer is still a wait")
        self.now += 1
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        for words in ("6 hours", "not finished", DVS[0], "under Import", "under another name", "Unraid", "unchanged"):
            self.assertIn(words, job["message"])
        self.assertNotIn("cleared", job["message"], "nothing of the disk's was cleared")
        self.assertNotIn("Volumes", job["message"], "that page lists no Pending claim and removes no DataVolume")
        self.assertTrue(job["message"].endswith(f"`kubectl -n {NS} delete datavolume {DVS[0]} {DVS[1]}`"),
                        "the command is whole, within a job's limit: " + job["message"])
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
        self.now += DISKS_LIMIT - 1
        self.assertEqual("running", self.look()["status"])
        self.now += 1
        job = self.look(3)
        self.assertEqual("failed", job["status"])
        self.assertIn("30 minutes", job["message"])
        self.assertIsNone(self.kube.object(self.path(DVS[0])), "what it made goes, so a retry can run")
        UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})

    def test_clearing_takes_cdis_populator_claims_and_leaves_a_lookalike_alone(self):
        self.phase("UploadScheduled")
        made = self.populator_disk(DVS[0])
        self.claim("win11-disk-backup", [], volume="pv-mine")
        self.look()
        self.now += DISKS_LIMIT
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
        self.now += DISKS_LIMIT - 1   # slow to be ready, then a long copy
        self.phase("UploadReady")
        self.look()
        self.now += 2 * CONVERT_LIMIT
        self.assertEqual("running", self.look()["status"], "a copy has no limit here")
        for disk in self.ref()["disks"]:
            job = self.kube.object(JOB_API + disk["job"])
            job["status"] = {"succeeded": 1}
            self.kube.add(job)
        self.assertEqual("running", self.look()["status"])
        self.assertEqual("convert", self.ref()["phase"])
        self.assertEqual("running", self.look()["status"], "time spent before this wait doesn't count towards it")
        self.now += CONVERT_LIMIT - 1
        self.assertEqual("running", self.look()["status"])
        self.now += 1
        self.assertEqual("failed", self.look()["status"])

    def test_the_copy_output_is_kept_when_its_job_goes(self):
        self.to_convert()
        self.look()
        self.now += CONVERT_LIMIT
        self.look()
        self.assertTrue(any("copy" in row["title"] for row in self.ref()["diagnostics"]), self.ref().get("diagnostics"))

    def test_a_disk_cdi_did_finish_is_named_as_finished_and_never_offered_for_removal(self):
        self.to_convert()
        dv = self.kube.object(self.path(DVS[0]))
        dv["status"] = {"phase": "Succeeded"}
        self.kube.add(dv)
        self.look()
        self.now += CONVERT_LIMIT
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertIn(f"CDI has not finished {DVS[1]} after", job["message"])
        self.assertIn(f"{DVS[0]} is finished", job["message"])
        self.assertTrue(job["message"].endswith(f"delete datavolume {DVS[1]}`"), job["message"])
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
        self.now += CONVERT_LIMIT
        job = self.look()
        self.assertEqual("failed", job["status"], job["message"])
        return job["message"]

    def test_past_a_jobs_message_limit_the_command_names_no_disk_and_the_message_counts_them(self):
        message = self.ended_with_vm_name("a-long-vm-name-" + "x" * 45)
        self.assertLessEqual(len(message), 500)
        self.assertTrue(message.startswith("CDI has not finished 2 disks after 6 hours"), message)
        self.assertTrue(message.endswith(f"`kubectl -n {NS} delete datavolume <name>` for each disk CDI has not finished"), message)
        self.assertNotIn(self.dvs[0], message)

    def test_past_the_limit_a_finished_disk_is_counted_too_and_the_rest_are_still_not_named(self):
        message = self.ended_with_vm_name("a-long-vm-name-" + "y" * 45, first_finished=True)
        self.assertLessEqual(len(message), 500)
        self.assertIn("<name>", message)
        self.assertTrue(message.startswith("CDI has not finished 1 disk after 6 hours"), message)
        self.assertIn("1 disk is finished and kept", message)
        self.assertNotIn(self.dvs[0], message)

    def test_a_cut_can_never_turn_one_disk_into_another_at_any_name_length(self):
        # <name>-disk is a prefix of <name>-disk-2, and the shorter one may be the finished
        # one. A whole name or none: the message is read as the job list keeps it.
        kinds = set()
        for length in range(15, 64):   # either side of where the command stops fitting
            for first_finished in (False, True):
                with self.subTest(length=length, first_finished=first_finished):
                    message = self.ended_with_vm_name(("f" if first_finished else "n") * length, first_finished)
                    self.assertLessEqual(len(message), 500)
                    if "<name>" in message:
                        kinds.add("no name")
                        self.assertNotIn(self.dvs[0], message)
                        self.assertTrue(message.endswith("for each disk CDI has not finished"), message)
                    else:
                        kinds.add("named")
                        wanted = self.dvs[1] if first_finished else f"{self.dvs[0]} {self.dvs[1]}"
                        self.assertTrue(message.endswith(f"delete datavolume {wanted}`"), message)
                    history = next(i for i in JOBS._read() if i["id"] == self.ident)["history"]
                    self.assertFalse(any("delete datavolume" in row["m"] for row in history),
                                     "the log keeps 240 characters: too few for the command to be cut in it")
        self.assertEqual({"named", "no name"}, kinds, "both sides of the limit were walked")

    def test_the_command_is_given_only_when_it_fits_to_the_last_character(self):
        command = f"`kubectl -n {NS} delete datavolume {DVS[0]} {DVS[1]}`"
        fits = "x" * (500 - len(command))
        self.assertEqual(command, UVMS._delete_command(NS, DVS, fits))
        self.assertIsNone(UVMS._delete_command(NS, DVS, fits + "x"))

    def test_a_copy_job_held_on_a_finalizer_keeps_the_job_running_until_it_has_gone(self):
        copies = self.to_convert()
        job = self.kube.object(copies[0][0])
        job["metadata"]["finalizers"] = ["example.com/held"]
        self.kube.add(job)
        self.look()
        self.now += CONVERT_LIMIT
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
        self.now += CONVERT_LIMIT
        self.assertEqual("failed", self.look()["status"])
        self.assertIsNone(self.kube.object(copies[0][1]))

    def test_a_secret_someone_replaced_is_left_alone(self):
        copies = self.to_convert()
        secret = self.kube.object(copies[0][1])
        secret["metadata"]["annotations"] = {}
        self.kube.add(secret)
        self.look()
        self.now += CONVERT_LIMIT
        self.assertEqual("failed", self.look()["status"])
        self.assertTrue(self.kube.object(copies[0][1]))
        self.assertIsNone(self.kube.object(copies[1][1]))

    def test_a_copy_job_that_cant_be_removed_yet_is_retried_and_the_job_says_so(self):
        copies = self.to_convert()
        self.look()
        self.now += CONVERT_LIMIT
        self.kube.fail(copies[0][0], 500, method="DELETE")
        job = self.look()
        self.assertEqual("running", job["status"])
        self.assertIn("not finished", job["message"])
        self.assertIn("copy Job", job["message"])
        self.assertTrue(self.ref()["retain_resources"])
        with self.assertRaisesRegex(ValueError, "still active"):
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
        self.now += CONVERT_LIMIT
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
        self.assertIn("delete datavolume", job["message"], "what to do next is still said")
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
        self.now += CONVERT_LIMIT
        message = self.look()["message"]
        for words in ("make the VM from it under Import", "check it boots", "bus, MAC and TPM", "under another name"):
            self.assertIn(words, message)
        self.assertNotIn("attach", message)
        self.assertTrue(message.endswith("delete datavolume single-disk`"), message)

    def test_several_disks_are_not_promised_a_vm_with_all_of_them(self):
        self.to_convert()
        self.look()
        self.now += CONVERT_LIMIT
        message = self.look()["message"]
        for words in ("from one disk", "can't attach the others", "under another name"):
            self.assertIn(words, message)
        self.assertNotIn("make the VM from them", message)

    def real_disk_check(self):
        self.imp.vm_disk_import_plan = VM_CREATE.vm_disk_import_plan
        self.enterContext(mock.patch.object(VM_CREATE.I, "kget", K.kget))

    def ended_after_six_hours(self):
        self.to_convert()
        self.look()
        self.now += CONVERT_LIMIT
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
        self.assertNotIn("emove", str(refused.exception), "it may be a finished copy, and Volumes can't remove one")
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
        # A person running the kubectl command the job gives: Homestead has no way to
        # do this yet. Deleting a DataVolume takes its claims and pods with it.
        for name in DVS:
            self.kube.discard(self.path(name))
        UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_an_older_import_with_no_receipt_ends_too_and_removes_nothing(self):
        self.phase("UploadScheduled")
        self.set_ref(cdi_cleanup=None)
        self.look()
        self.now += DISKS_LIMIT
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_an_older_import_past_six_hours_ends_too_and_removes_nothing(self):
        self.phase("UploadReady")
        self.set_ref(cdi_cleanup=None, phase="convert")
        self.look()
        self.now += CONVERT_LIMIT
        job = self.look()
        self.assertEqual("failed", job["status"])
        self.assertIn("not finished", job["message"])
        self.assertTrue(self.kube.object(self.path(DVS[0])))

    def test_disks_that_become_ready_in_time_are_not_failed(self):
        self.phase("UploadScheduled")
        self.look()
        self.now += DISKS_LIMIT - 1
        self.phase("UploadReady")
        self.now += 1
        self.assertNotEqual("failed", self.look()["status"])
        self.assertEqual("copy", self.ref()["phase"])

    def test_a_disk_that_finishes_in_time_makes_the_vm(self):
        self.to_convert()
        self.look()
        self.now += CONVERT_LIMIT - 1
        self.phase("Succeeded")
        self.imp.prepare_vm = lambda cfg, platform, source: cfg
        self.imp._recheck_vm_creation = lambda prepared: None
        self.imp.commit_vm = mock.Mock()
        self.now += 1
        self.look()
        self.imp.commit_vm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
