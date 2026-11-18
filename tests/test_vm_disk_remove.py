"""Removing an import disk from Import > Disk images (#1217): a review that says what
goes and refuses a disk in use, then a job that clears it the way an import's clean-up
does. On the shared fake API and the real job list."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_cdi_cleanup as CDI_CLEANUP
import homestead_imports as I
import homestead_kube as K
import homestead_operations as JOBS
import homestead_storage_conflicts as CONFLICTS
import homestead_vm_create as VM_CREATE
import homestead_vm_disk_remove as REMOVE
import homestead_volumes as VOLUMES
from fakes.kube import Kube

NS = "lab"
DISK = "guest-disk"
CLAIMS = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/"
PODS = f"/api/v1/namespaces/{NS}/pods/"
PVS = "/api/v1/persistentvolumes/"
DV = f"{CDI_CLEANUP.API}/namespaces/{NS}/datavolumes/"
VMS = f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachines"


def owned_by(kind, name, uid):
    return [{"kind": kind, "name": name, "uid": uid, "controller": True}]


class Base(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(JOBS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(groups=("cdi.kubevirt.io", "kubevirt.io", "longhorn.io")))
        self.enterContext(mock.patch.object(I, "kget", K.kget))
        self.enterContext(mock.patch.object(I, "ksend", K.ksend))
        self.enterContext(mock.patch.object(VOLUMES, "kget", K.kget))
        self.busted = []
        self.enterContext(mock.patch.object(I, "_bust", lambda *keys: self.busted.append(keys)))

    def disk(self, name=DISK, phase="ImportInProgress", stamp="a-stamp", label="true"):
        annotations = {CDI_CLEANUP.STAMP: stamp} if stamp else {}
        labels = {I.VM_DISK_LABEL: label} if label else {}
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
                       "metadata": {"namespace": NS, "name": name, "labels": labels, "annotations": annotations},
                       "spec": {"storage": {"storageClassName": "fast",
                                            "resources": {"requests": {"storage": "20Gi"}}}},
                       "status": {"phase": phase}})
        return self.kube.object(DV + name)["metadata"]["uid"]

    def claim(self, name, owner, status="Bound", volume=""):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": NS, "name": name, "ownerReferences": owner},
                       "spec": {"volumeName": volume} if volume else {}, "status": {"phase": status}})
        return self.kube.object(CLAIMS + name)["metadata"]["uid"]

    def pod(self, name, owner, claims, labels=None, phase="Running", finalizers=()):
        self.kube.add({"apiVersion": "v1", "kind": "Pod",
                       "metadata": {"namespace": NS, "name": name, "ownerReferences": owner, "finalizers": list(finalizers),
                                    "labels": {"cdi.kubevirt.io": "cdi-upload-server"} if labels is None else labels},
                       "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": c}} for c in claims]},
                       "status": {"phase": phase}})
        return self.kube.object(PODS + name)["metadata"]["uid"]

    def volume(self, name, claim_name, claim_uid, policy="Delete", phase="Bound"):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": name},
                       "spec": {"claimRef": {"namespace": NS, "name": claim_name, "uid": claim_uid},
                                "csi": {"driver": "driver.longhorn.io", "volumeHandle": "lh-" + name},
                                "persistentVolumeReclaimPolicy": policy},
                       "status": {"phase": phase}})

    def populator_disk(self, **fields):
        """CDI with populators: the target is Pending, the copy on its prime claim."""
        uid = self.disk(**fields)
        target = self.claim(DISK, owned_by("DataVolume", DISK, uid), "Pending")
        prime = "prime-" + target
        prime_uid = self.claim(prime, owned_by("PersistentVolumeClaim", DISK, target), volume="pv-prime")
        pod_uid = self.pod("worker", owned_by("PersistentVolumeClaim", prime, prime_uid), (prime, prime + "-scratch"))
        self.claim(prime + "-scratch", owned_by("Pod", "worker", pod_uid))
        self.volume("pv-prime", prime, prime_uid)
        return uid, prime

    def finished_disk(self, policy="Delete"):
        uid = self.disk(phase="Succeeded")
        target = self.claim(DISK, owned_by("DataVolume", DISK, uid), volume="pv-disk")
        self.volume("pv-disk", DISK, target, policy)
        return uid, target

    def vm(self, volume, name="web", kind="VirtualMachine"):
        spec = {"volumes": [{"name": "root", **volume}]}
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": kind, "metadata": {"namespace": NS, "name": name},
                       "spec": {"template": {"spec": spec}} if kind == "VirtualMachine" else spec})

    def controller(self, kind, name, volume):
        """A Deployment, StatefulSet or DaemonSet, or a CronJob, whose pods name the claim."""
        template = {"spec": {"containers": [{"name": "app", "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                             "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": volume}}]}}
        spec = ({"schedule": "0 0 * * *", "jobTemplate": {"spec": {"template": template}}} if kind == "CronJob"
                else {"replicas": 0, "template": template})
        self.kube.add({"apiVersion": "batch/v1" if kind == "CronJob" else "apps/v1", "kind": kind,
                       "metadata": {"namespace": NS, "name": name}, "spec": spec})

    def job_on(self, kind, status, **ref):
        operation = JOBS.start(kind, "Job", {}, "/", {"namespace": NS, **ref})
        with JOBS.transaction():
            items = JOBS._read()
            next(i for i in items if i["id"] == operation["id"])["status"] = status
            JOBS._write(items)
        return operation

    def look(self, ident):
        return next(job for job in JOBS.list_operations() if job["id"] == ident)

    def sent(self, method, path):
        return [write for write in self.kube.writes if write[0] == method and write[1] == path]


class ReviewTests(Base):
    def test_the_review_says_what_goes_for_an_unfinished_disk(self):
        uid, prime = self.populator_disk()
        plan = REMOVE.plan(NS, DISK)
        self.assertEqual((NS, DISK, uid, False, []), (plan["namespace"], plan["name"], plan["uid"], plan["finished"], plan["blocking"]))
        self.assertEqual((DISK, "ImportInProgress", "20Gi", "fast"), (plan["confirm"], plan["phase"], plan["capacity"], plan["storage_class"]))
        goes = "\n".join(plan["goes"])
        for name in (f"DataVolume {DISK}", f"claim {DISK}", prime, prime + "-scratch", "worker", "pv-prime"):
            self.assertIn(name, goes)

    def test_the_review_says_when_a_retain_volume_is_given_back(self):
        self.finished_disk(policy="Retain")
        goes = "\n".join(REMOVE.plan(NS, DISK)["goes"])
        self.assertRegex(goes, "pv-disk.*Retain")

    def test_a_review_writes_nothing(self):
        self.populator_disk()
        REMOVE.plan(NS, DISK)
        self.assertEqual([], self.kube.writes)

    def test_a_datavolume_homestead_didnt_import_is_refused(self):
        self.disk(label="")
        with self.assertRaisesRegex(ValueError, "is not a disk"):
            REMOVE.plan(NS, DISK)

    def test_an_absent_disk_is_refused(self):
        with self.assertRaisesRegex(ValueError, "is not a disk"):
            REMOVE.plan(NS, "no-such-disk")

    def test_a_name_that_isnt_one_is_refused_before_any_read(self):
        with self.assertRaisesRegex(ValueError, "lowercase"):
            REMOVE.plan(NS, "../secrets")
        self.assertEqual([], self.kube.reads)

    def test_a_vm_using_it_by_claim_blocks_it(self):
        self.finished_disk()
        self.vm({"persistentVolumeClaim": {"claimName": DISK}})
        self.assertEqual(["web uses it"], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_vm_using_it_by_datavolume_blocks_it(self):
        self.finished_disk()
        self.vm({"dataVolume": {"name": DISK}})
        self.assertEqual(["web uses it"], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_running_vmi_with_no_vm_blocks_it(self):
        self.finished_disk()
        self.vm({"persistentVolumeClaim": {"claimName": DISK}}, name="loose", kind="VirtualMachineInstance")
        self.assertEqual(["loose uses it"], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_vm_on_another_disk_doesnt_block_it(self):
        self.finished_disk()
        self.vm({"dataVolume": {"name": DISK + "-2"}})
        self.assertEqual([], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_pod_mounting_it_blocks_it(self):
        self.finished_disk()
        self.pod("reader", [], (DISK,), labels={})
        self.assertEqual(["Pod reader uses it"], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_finished_pod_blocks_it_and_says_why_and_how(self):
        self.finished_disk()
        self.pod("reader", [], (DISK,), labels={}, phase="Succeeded")
        self.pod("failed-reader", [], (DISK,), labels={}, phase="Failed")
        blocking = REMOVE.plan(NS, DISK)["blocking"]
        self.assertEqual(2, len(blocking), blocking)
        for pod, why in zip(("failed-reader", "reader"), blocking):
            self.assertIn(f"Pod {pod} has ended but Kubernetes keeps the claim until it is removed", why)
            self.assertIn(f"kubectl -n {NS} delete pod {pod}", why)

    def test_a_finished_job_whose_pod_has_gone_doesnt_block_it(self):
        self.finished_disk()
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": NS, "name": "done"},
                       "spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": DISK}}]}}},
                       "status": {"succeeded": 1}})
        self.assertEqual([], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_running_job_naming_it_blocks_it(self):
        self.finished_disk()
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": NS, "name": "running"},
                       "spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": DISK}}]}}},
                       "status": {"active": 1}})
        self.assertEqual(["Job running uses it"], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_stopped_app_or_a_cronjob_naming_it_blocks_it(self):
        self.finished_disk()
        self.controller("Deployment", "stopped", DISK)
        self.controller("CronJob", "nightly", DISK)
        self.assertEqual(["CronJob nightly uses it", "Deployment stopped uses it"], REMOVE.plan(NS, DISK)["blocking"])

    def test_an_unreadable_list_of_apps_refuses(self):
        self.finished_disk()
        self.kube.fail(f"/apis/apps/v1/namespaces/{NS}/deployments", 500)
        with self.assertRaisesRegex(ValueError, "could not read what uses this disk"):
            REMOVE.plan(NS, DISK)

    def test_a_vm_or_pod_on_cdis_work_claims_blocks_it(self):
        _, prime = self.populator_disk()
        self.vm({"persistentVolumeClaim": {"claimName": prime}})
        self.pod("reader", [], (prime + "-scratch",), labels={})
        self.controller("Deployment", "stopped", prime)
        self.assertEqual(["web uses it", "Pod reader uses it", "Deployment stopped uses it"],
                         REMOVE.plan(NS, DISK)["blocking"])

    def test_a_claim_someone_else_owns_blocks_it(self):
        self.disk()
        self.claim(DISK, owned_by("DataVolume", "other", "other-uid"))
        self.assertIn("owner of the import", " ".join(REMOVE.plan(NS, DISK)["blocking"]))

    def test_an_active_import_says_what_can_be_done_with_it(self):
        self.finished_disk()
        with mock.patch.object(JOBS, "CDI_CANCELLABLE", {"unraid-vm-import"}):
            for kind, ref, words in (("unraid-vm-import", {"disks": [{"dv": DISK}]}, "Cancel that import"),
                                     ("vm-disk-import", {"name": DISK}, "cannot be cancelled yet")):
                operation = self.job_on(kind, "running", **ref)
                self.assertIn(words, REMOVE.plan(NS, DISK)["blocking"][0], kind)
                with JOBS.transaction():
                    JOBS._write([i for i in JOBS._read() if i["id"] != operation["id"]])

    def test_a_url_import_is_pointed_at_its_cancel(self):
        # #1338: its canceller deletes only what carries its receipt, so it has Cancel.
        import homestead_cancel as CANCEL
        self.enterContext(mock.patch.object(JOBS, "CDI_CANCELLABLE", set()))
        self.enterContext(mock.patch.dict(JOBS.CANCELLERS))
        self.enterContext(mock.patch.dict(JOBS.VIEWS))
        self.enterContext(mock.patch.object(JOBS, "CLEANUPS", set(JOBS.CLEANUPS)))
        # Still downloading: the import's own step would read the fake API, which this test is not about.
        self.enterContext(mock.patch.dict(JOBS.RESOLVERS, {"vm-disk-import": lambda item: ("running", 15, "Downloading")}))
        CANCEL.register(JOBS)
        self.finished_disk()
        self.job_on("vm-disk-import", "running", name=DISK, cdi_cleanup={"id": "x", "disks": {}})
        self.assertIn("Cancel that import", REMOVE.plan(NS, DISK)["blocking"][0])
        self.assertTrue(next(j for j in JOBS.list_operations(wait=False))["cancellable"])

    def test_an_import_that_ended_still_holding_it_blocks_it(self):
        self.finished_disk()
        self.job_on("unraid-vm-import", "failed", disks=[{"dv": DISK}], retain_resources=True)
        self.assertEqual(["An import is still clearing up after itself. Wait for it to end, then review this again"],
                         REMOVE.plan(NS, DISK)["blocking"])

    def test_an_import_that_ended_lets_go(self):
        self.finished_disk()
        self.job_on("unraid-vm-import", "failed", disks=[{"dv": DISK}])
        self.job_on("vm-disk-import", "succeeded", name=DISK)
        self.assertEqual([], REMOVE.plan(NS, DISK)["blocking"])

    def test_an_import_of_another_disk_or_namespace_doesnt_block_it(self):
        self.finished_disk()
        self.job_on("vm-disk-import", "running", name="other-disk")
        JOBS.start("vm-disk-import", "Job", {}, "/", {"namespace": "elsewhere", "name": DISK})
        self.assertEqual([], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_storage_move_on_it_blocks_it(self):
        self.finished_disk()
        self.job_on("reclass", "running", claim=DISK)
        self.assertIn("storage move", " ".join(REMOVE.plan(NS, DISK)["blocking"]))

    def test_a_storage_move_on_its_volume_or_longhorn_volume_blocks_it(self):
        self.finished_disk()
        for ref in ({"claim": "other", "old_pv": "pv-disk"},
                    {"claim": "other", "copy_claims": {"x": {"pv": "elsewhere", "csi_driver": "driver.longhorn.io",
                                                             "csi_handle": "lh-pv-disk"}}}):
            operation = self.job_on("reclass", "running", **ref)
            self.assertIn("storage move", " ".join(REMOVE.plan(NS, DISK)["blocking"]), ref)
            with JOBS.transaction():
                JOBS._write([i for i in JOBS._read() if i["id"] != operation["id"]])

    def test_a_storage_move_on_a_volume_with_no_longhorn_handle_blocks_it(self):
        self.finished_disk()
        row = self.kube.object(PVS + "pv-disk")
        row["spec"]["csi"] = {"driver": "example.com/other"}
        self.kube.add(row)
        self.job_on("reclass", "running", claim="other", old_pv="pv-disk")
        self.assertIn("storage move", " ".join(REMOVE.plan(NS, DISK)["blocking"]))

    def test_a_storage_move_on_cdis_work_claim_blocks_it(self):
        _, prime = self.populator_disk()
        self.job_on("reclass", "running", claim=prime)
        self.assertIn("storage move", " ".join(REMOVE.plan(NS, DISK)["blocking"]))

    def test_an_unreadable_vm_list_refuses(self):
        self.finished_disk()
        self.kube.fail(VMS, 500)
        with self.assertRaises(urllib.error.HTTPError):
            REMOVE.plan(NS, DISK)

    def test_an_unreadable_pod_list_refuses(self):
        self.finished_disk()
        self.kube.fail(f"/api/v1/namespaces/{NS}/pods", 500)
        with self.assertRaises(urllib.error.HTTPError):
            REMOVE.plan(NS, DISK)

    def test_a_cluster_without_kubevirt_has_no_vm_using_it(self):
        self.finished_disk()
        self.kube.absent_groups.add("kubevirt.io")
        self.kube.groups.discard("kubevirt.io")
        self.assertEqual([], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_disk_from_before_stamps_can_be_reviewed(self):
        self.populator_disk(stamp="")
        self.assertEqual([], REMOVE.plan(NS, DISK)["blocking"])

    def test_a_finished_disk_says_a_vm_can_still_be_made(self):
        self.finished_disk()
        plan = REMOVE.plan(NS, DISK)
        self.assertTrue(plan["finished"])
        self.assertIn("Create VM", plan["note"])

    def test_an_unfinished_disk_doesnt_offer_a_vm(self):
        self.populator_disk()
        self.assertNotIn("Create VM", REMOVE.plan(NS, DISK)["note"])

    def test_the_vm_check_cleanup_makes_is_the_shared_one(self):
        """cleanup's refusal is not changed now it goes through vm_users."""
        uid, _ = self.finished_disk()
        self.vm({"persistentVolumeClaim": {"claimName": DISK}})
        work = CDI_CLEANUP.receipt([DISK])
        work["id"] = "a-stamp"
        CDI_CLEANUP.observe(K.kget, NS, work)
        with self.assertRaisesRegex(ValueError, "A VM now uses an import disk"):
            CDI_CLEANUP.cleanup(K.kget, K.ksend, NS, work, keep_disks=False)
        self.assertEqual([], self.kube.writes)


class RemovalTests(Base):
    def start(self, uid, confirmation=DISK):
        return REMOVE.remove({"namespace": NS, "name": DISK, "uid": uid, "confirmation": confirmation}, "someone")

    def provisioner_reclaims(self):
        """What CSI does, which the fake doesn't: a Delete volume goes once its claim has."""
        for row in K.kget("/api/v1/persistentvolumes")["items"]:
            spec = row["spec"]
            if spec["persistentVolumeReclaimPolicy"] == "Delete" and K.get_optional(
                    CLAIMS + spec["claimRef"]["name"], read=K.kget) is None:
                self.kube.discard(PVS + row["metadata"]["name"])

    def until_ended(self, ident, limit=6):
        for _ in range(limit):
            job = self.look(ident)
            if job["status"] in JOBS.TERMINAL:
                return job
            self.provisioner_reclaims()
        self.fail(f"still {job['status']}: {job['message']}")

    def test_removing_clears_the_disk_its_claim_and_cdis_work_space(self):
        uid, prime = self.populator_disk()
        result = self.start(uid)
        self.assertTrue(result["ok"])
        job = self.until_ended(result["operation"]["id"])
        self.assertEqual("succeeded", job["status"], job["message"])
        self.assertIn(f"Disk {DISK} removed", job["message"])
        for path in (DV + DISK, CLAIMS + DISK, CLAIMS + prime, CLAIMS + prime + "-scratch", PODS + "worker"):
            with self.assertRaises(urllib.error.HTTPError, msg=path):
                K.kget(path)
        self.assertTrue(VM_CREATE.vm_disk_import_plan(NS, DISK)["ready"], "the name is free")
        self.assertIn(("vms", "vol"), self.busted)

    def test_a_retain_class_gives_its_storage_back(self):
        uid, target = self.finished_disk(policy="Retain")
        ident = self.start(uid)["operation"]["id"]
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.assertEqual("Retain", K.kget(PVS + "pv-disk")["spec"]["persistentVolumeReclaimPolicy"], "while it is Bound")
        released = K.kget(PVS + "pv-disk")
        released["status"]["phase"] = "Released"
        self.kube.add(released)
        job = self.look(ident)
        self.assertEqual("running", job["status"], "a patch is not completion")
        self.assertEqual("Delete", K.kget(PVS + "pv-disk")["spec"]["persistentVolumeReclaimPolicy"])
        self.kube.discard(PVS + "pv-disk")
        job = self.until_ended(ident)
        self.assertEqual("succeeded", job["status"], job["message"])

    def test_a_pending_claim_with_no_volume_finishes(self):
        uid = self.disk(phase="WaitForFirstConsumer")
        self.claim(DISK, owned_by("DataVolume", DISK, uid), "Pending")
        job = self.until_ended(self.start(uid)["operation"]["id"])
        self.assertEqual("succeeded", job["status"], job["message"])

    def test_a_disk_from_before_stamps_is_removed(self):
        uid, _ = self.populator_disk(stamp="")
        job = self.until_ended(self.start(uid)["operation"]["id"])
        self.assertEqual("succeeded", job["status"], job["message"])

    def test_the_delete_names_the_reviewed_disk(self):
        uid, _ = self.finished_disk()
        self.look(self.start(uid)["operation"]["id"])
        self.assertEqual(uid, self.sent("DELETE", DV + DISK)[0][2]["preconditions"]["uid"])

    def test_a_stale_review_starts_nothing(self):
        self.finished_disk()
        with self.assertRaisesRegex(ValueError, "changed after this review"):
            self.start("an-older-uid")
        self.assertEqual([], JOBS.stored())
        self.assertEqual([], self.kube.writes)

    def test_a_wrong_name_starts_nothing(self):
        uid, _ = self.finished_disk()
        for typed in ("wrong", "", None):
            with self.assertRaisesRegex(ValueError, "Type the disk's name"):
                self.start(uid, typed)
        self.assertEqual([], JOBS.stored())
        self.assertEqual([], self.kube.writes)

    def test_a_vm_arriving_before_the_send_starts_nothing(self):
        uid, _ = self.finished_disk()
        self.vm({"dataVolume": {"name": DISK}})
        with self.assertRaisesRegex(PermissionError, "web uses it"):
            self.start(uid)
        self.assertEqual([], JOBS.stored())

    def test_an_import_starting_before_the_send_starts_nothing(self):
        uid, _ = self.finished_disk()
        self.job_on("vm-disk-import", "running", name=DISK)
        with self.assertRaisesRegex(PermissionError, "still working on it"):
            self.start(uid)
        self.assertEqual(1, len(JOBS.stored()))
        self.assertEqual([], self.kube.writes)

    def test_a_vm_arriving_after_the_review_ends_it_with_the_disk_kept(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.vm({"persistentVolumeClaim": {"claimName": DISK}})
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("web uses it", job["message"])
        self.assertIn("kept", job["message"])
        self.assertEqual(uid, K.kget(DV + DISK)["metadata"]["uid"])
        K.kget(CLAIMS + DISK)
        self.assertEqual([], self.kube.writes)
        self.assertFalse(next(i for i in JOBS.stored() if i["id"] == ident)["ref"].get("retain_resources"))
        self.assertEqual([], REMOVE.plan(NS, DISK)["blocking"][1:], "only the VM holds it now")

    def test_a_pod_arriving_after_the_review_ends_it_with_the_disk_kept(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.pod("reader", [], (DISK,), labels={})
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("reader", job["message"])
        self.assertEqual([], self.kube.writes)

    def test_a_read_that_fails_before_the_send_removes_nothing_and_waits(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.kube.fail(VMS, 500)
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.assertEqual([], self.kube.writes)
        self.assertEqual("succeeded", self.until_ended(ident)["status"])

    def test_a_vm_arriving_after_the_disk_was_asked_to_go_waits_and_says_so(self):
        uid, _ = self.finished_disk()
        held = self.kube.object(DV + DISK)
        held["metadata"]["finalizers"] = ["cdi.kubevirt.io/held"]
        self.kube.add(held)
        ident = self.start(uid)["operation"]["id"]
        self.look(ident)
        self.assertTrue(K.kget(DV + DISK)["metadata"]["deletionTimestamp"])
        self.vm({"dataVolume": {"name": DISK}})
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.assertIn("A VM now uses an import disk", job["message"])

    def test_two_removals_cant_both_start(self):
        uid, _ = self.finished_disk()
        self.start(uid)
        with self.assertRaisesRegex(PermissionError, "already being removed"):
            self.start(uid)
        self.assertEqual(1, len(JOBS.stored()))

    def test_an_import_under_that_name_is_refused_while_it_runs(self):
        uid, _ = self.finished_disk()
        self.start(uid)
        with self.assertRaisesRegex(ValueError, "still running or cleaning up"):
            JOBS.start("vm-disk-import", "Import", {}, "/", {"namespace": NS, "name": DISK})
        with self.assertRaisesRegex(ValueError, "still running or cleaning up"):
            JOBS.start("unraid-vm-import", "Import", {}, "/", {"namespace": NS, "disks": [{"dv": DISK}]})

    def test_an_import_under_that_name_is_allowed_once_it_ends(self):
        uid, _ = self.finished_disk()
        self.until_ended(self.start(uid)["operation"]["id"])
        JOBS.start("vm-disk-import", "Import", {}, "/", {"namespace": NS, "name": DISK})

    def test_a_lost_reply_is_only_retried(self):
        uid, _ = self.finished_disk()
        self.kube.fail(DV + DISK, 500, method="DELETE", applied=True)
        ident = self.start(uid)["operation"]["id"]
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        with self.assertRaises(urllib.error.HTTPError):
            K.kget(DV + DISK)
        job = self.until_ended(ident)
        self.assertEqual("succeeded", job["status"], job["message"])
        self.assertEqual(1, len(self.sent("DELETE", DV + DISK)), "one DELETE: the retry saw it had gone")

    def test_a_disk_replaced_under_the_job_is_not_removed(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.kube.discard(CLAIMS + DISK)
        self.kube.discard(DV + DISK)
        replaced = self.disk(stamp="another-stamp")
        self.assertNotEqual(uid, replaced)
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("kept", job["message"])
        self.assertEqual(replaced, K.kget(DV + DISK)["metadata"]["uid"])
        self.assertEqual([], self.sent("DELETE", DV + DISK))

    def cdi_round(self, worker, claim_name):
        """What CDI does between rounds: a terminating worker goes, and one that has gone
        comes back, same name and new uid, while its claim isn't being deleted. A claim
        held by pvc-protection goes once nothing mounts it."""
        pod = self.kube.object(PODS + worker)
        if pod and pod["metadata"].get("deletionTimestamp"):
            self.kube.discard(PODS + worker)
            pod = None
        claim = self.kube.object(CLAIMS + claim_name)
        if not pod and claim and not claim["metadata"].get("deletionTimestamp"):
            self.pod(worker, owned_by("PersistentVolumeClaim", claim_name, claim["metadata"]["uid"]), (claim_name,),
                     finalizers=("example.com/grace",))
        elif not pod and claim:
            self.kube.discard(CLAIMS + claim_name)

    def working_disk(self):
        """A disk CDI is still working on: a bound target with its worker on it."""
        uid = self.disk(phase="ImportInProgress")
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"namespace": NS, "name": DISK, "finalizers": ["kubernetes.io/pvc-protection"],
                                    "ownerReferences": owned_by("DataVolume", DISK, uid)},
                       "status": {"phase": "Pending"}})
        self.cdi_round("worker", DISK)
        return uid

    def test_a_disk_cdi_is_still_working_on_is_removed_though_cdi_remakes_its_worker(self):
        uid = self.working_disk()
        ident = self.start(uid)["operation"]["id"]
        for _ in range(6):
            job = self.look(ident)
            if job["status"] in JOBS.TERMINAL:
                break
            self.cdi_round("worker", DISK)
        self.assertEqual("succeeded", job["status"], job["message"])
        self.assertEqual(1, len(self.sent("DELETE", DV + DISK)))
        self.assertTrue(VM_CREATE.vm_disk_import_plan(NS, DISK)["ready"])

    def test_a_failed_import_clears_up_though_cdi_remakes_its_worker(self):
        """The same clean-up: what an Unraid import that failed its copy, or a URL import, runs."""
        uid = self.working_disk()
        work = REMOVE._receipt(self.kube.object(DV + DISK))
        item = {"ref": {"namespace": NS, "cdi_cleanup": work, "phase": "cleanup", "cleanup_outcome": "failed",
                        "cleanup_detail": "Upload refused"}}
        for _ in range(6):
            state, _, message = CDI_CLEANUP.finish(item, K.kget, K.ksend, lambda _: None)
            if state != "running":
                break
            self.cdi_round("worker", DISK)
        self.assertEqual("failed", state, message)
        self.assertEqual(uid, self.sent("DELETE", DV + DISK)[0][2]["preconditions"]["uid"])
        self.assertIsNone(self.kube.object(CLAIMS + DISK))

    def test_a_volume_policy_waits_for_the_worker_though_the_disk_doesnt(self):
        uid, target = self.finished_disk(policy="Retain")
        # Its volumes aren't listed, so only the wait for the worker itself can hold the policy.
        self.pod("worker", owned_by("PersistentVolumeClaim", DISK, target), (), finalizers=("example.com/grace",))
        row = self.kube.object(PVS + "pv-disk")
        ident = self.start(uid)["operation"]["id"]
        row["status"]["phase"] = "Released"
        self.kube.add(row)
        self.look(ident)
        self.assertEqual(1, len(self.sent("DELETE", DV + DISK)), "the disk is asked to go at once")
        self.assertEqual("Retain", K.kget(PVS + "pv-disk")["spec"]["persistentVolumeReclaimPolicy"])
        self.kube.discard(PODS + "worker")
        self.look(ident)
        self.assertEqual("Delete", K.kget(PVS + "pv-disk")["spec"]["persistentVolumeReclaimPolicy"])

    def test_the_claim_is_asked_to_go_while_the_worker_is_still_on_it(self):
        """A DataVolume held on a finalizer takes nothing with it, and CDI only stops
        remaking the worker once its claim is being deleted."""
        uid = self.working_disk()
        held = self.kube.object(DV + DISK)
        held["metadata"]["finalizers"] = ["cdi.kubevirt.io/held"]
        self.kube.add(held)
        self.look(self.start(uid)["operation"]["id"])
        self.assertTrue(self.kube.object(PODS + "worker")["metadata"]["deletionTimestamp"], "still terminating")
        self.assertEqual(1, len(self.sent("DELETE", CLAIMS + DISK)))
        self.assertTrue(self.kube.object(CLAIMS + DISK)["metadata"]["deletionTimestamp"])

    def test_a_finished_pod_arriving_after_the_review_ends_it_with_the_disk_kept(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.pod("reader", [], (DISK,), labels={}, phase="Succeeded")
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("has ended but Kubernetes keeps the claim until it is removed", job["message"])
        self.assertIn("kept", job["message"])
        self.assertEqual([], self.kube.writes)

    def test_a_clean_up_with_no_review_names_a_finished_pod_and_deletes_nothing(self):
        """A failed import: a finished pod on its work space used to refuse, with a reason."""
        _, prime = self.populator_disk()
        self.pod("reader", [], (prime + "-scratch",), labels={}, phase="Succeeded")
        work = REMOVE._receipt(self.kube.object(DV + DISK))
        item = {"ref": {"namespace": NS, "cdi_cleanup": work, "phase": "cleanup", "cleanup_outcome": "failed",
                        "cleanup_detail": "Upload refused"}}
        state, _, message = CDI_CLEANUP.finish(item, K.kget, K.ksend, lambda _: None)
        self.assertEqual("running", state)
        self.assertIn("Pod reader has ended but Kubernetes keeps the claim until it is removed", message)
        self.assertEqual([], self.kube.writes)

    def test_a_clean_up_with_no_review_names_a_running_pod_too(self):
        _, prime = self.populator_disk()
        self.pod("reader", [], (prime + "-scratch",), labels={})
        work = REMOVE._receipt(self.kube.object(DV + DISK))
        with self.assertRaisesRegex(ValueError, "Another pod uses the import's work space .reader."):
            CDI_CLEANUP.observe(K.kget, NS, work)
            CDI_CLEANUP.cleanup(K.kget, K.ksend, NS, work, keep_disks=False)
        self.assertEqual([], self.kube.writes)

    def test_a_pod_taking_a_workers_name_isnt_taken_for_it(self):
        uid, prime = self.populator_disk()
        ident = self.start(uid)["operation"]["id"]
        self.kube.discard(PODS + "worker")
        self.pod("worker", [], (prime,), labels={})
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("Pod worker uses it", job["message"])
        self.assertEqual([], self.kube.writes)

    def test_a_worker_someone_took_over_stops_it_before_the_disk_is_asked_to_go(self):
        uid, _ = self.populator_disk()
        ident = self.start(uid)["operation"]["id"]
        worker = self.kube.object(PODS + "worker")
        worker["metadata"]["ownerReferences"] = owned_by("Deployment", "someone", "someone-uid")
        self.kube.add(worker)
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.assertIn("owner of the import", job["message"])
        self.assertEqual([], self.kube.writes, "no DELETE of the disk, or anything")

    def test_a_failed_import_with_a_worker_someone_took_over_keeps_the_disk(self):
        self.populator_disk()
        work = REMOVE._receipt(self.kube.object(DV + DISK))
        CDI_CLEANUP.observe(K.kget, NS, work)
        worker = self.kube.object(PODS + "worker")
        worker["metadata"]["ownerReferences"] = owned_by("Deployment", "someone", "someone-uid")
        self.kube.add(worker)
        with self.assertRaisesRegex(ValueError, "owner of the import"):
            CDI_CLEANUP.cleanup(K.kget, K.ksend, NS, work, keep_disks=False)
        self.assertEqual([], self.kube.writes)

    def arriving_mid_round(self, pod_claim, work):
        """A pod that appears between cleanup's first look at the pods and its second."""
        reads = {"pods": 0}

        def read(path):
            if path == f"/api/v1/namespaces/{NS}/pods":
                reads["pods"] += 1
                if reads["pods"] == 2:
                    self.pod("late", [], (pod_claim,), labels={})
            return K.kget(path)
        return CDI_CLEANUP.cleanup(read, K.ksend, NS, work, keep_disks=False)

    def test_a_pod_arriving_mid_round_stops_the_claims_delete(self):
        self.finished_disk()
        held = self.kube.object(DV + DISK)
        held["metadata"]["finalizers"] = ["cdi.kubevirt.io/held"]
        self.kube.add(held)
        work = REMOVE._receipt(held)
        CDI_CLEANUP.observe(K.kget, NS, work)
        self.assertFalse(self.arriving_mid_round(DISK, work))
        self.assertEqual([], self.sent("DELETE", CLAIMS + DISK))

    def test_a_pod_arriving_mid_round_stops_the_volumes_policy_change(self):
        self.finished_disk(policy="Retain")
        work = REMOVE._receipt(self.kube.object(DV + DISK))
        CDI_CLEANUP.observe(K.kget, NS, work)
        self.kube.discard(CLAIMS + DISK)
        row = self.kube.object(PVS + "pv-disk")
        row["status"]["phase"] = "Released"
        self.kube.add(row)
        self.assertFalse(self.arriving_mid_round(DISK, work))
        self.assertEqual([], self.sent("PATCH", PVS + "pv-disk"))

    def test_a_stopped_app_arriving_after_the_review_ends_it_with_the_disk_kept(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.controller("Deployment", "stopped", DISK)
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("kept", job["message"])
        self.assertEqual([], self.kube.writes)

    def test_once_the_disk_has_gone_nothing_says_it_was_kept(self):
        uid, target = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.look(ident)
        self.assertIsNone(self.kube.object(DV + DISK))
        self.vm({"persistentVolumeClaim": {"claimName": DISK}})
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.assertNotIn("kept", job["message"])

    def test_a_running_removal_cant_be_dismissed(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        with self.assertRaisesRegex(ValueError, "active operation"):
            JOBS.dismiss(ident)

    def test_a_storage_move_is_refused_while_it_runs_and_not_after(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        move = {"namespace": NS, "claim": DISK}
        self.assertEqual([ident], [i["id"] for i in CONFLICTS.conflicts(JOBS.stored(), "reclass", move)])
        self.until_ended(ident)
        self.assertEqual([], CONFLICTS.conflicts(JOBS.stored(), "reclass", move))

    def test_a_storage_move_is_allowed_after_a_kept_ending(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        self.vm({"dataVolume": {"name": DISK}})
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertEqual([], CONFLICTS.conflicts(JOBS.stored(), "reclass", {"namespace": NS, "claim": DISK}))

    def test_a_storage_move_is_allowed_after_stopping_following(self):
        JOBS.cancel(self.stuck())
        self.assertEqual([], CONFLICTS.conflicts(JOBS.stored(), "reclass", {"namespace": NS, "claim": DISK}))

    def stuck(self):
        """The disk has been asked to go, and then a VM appears: the clean-up can only wait."""
        uid, _ = self.finished_disk()
        for path, finalizer in ((DV + DISK, "cdi.kubevirt.io/held"), (CLAIMS + DISK, "kubernetes.io/pvc-protection")):
            held = self.kube.object(path)
            held["metadata"]["finalizers"] = [finalizer]
            self.kube.add(held)
        ident = self.start(uid)["operation"]["id"]
        self.look(ident)
        self.vm({"dataVolume": {"name": DISK}})
        self.assertEqual("running", self.look(ident)["status"])
        return ident

    def test_a_stuck_removal_can_stop_being_followed(self):
        ident = self.stuck()
        self.assertTrue(self.look(ident)["cancellable"])
        plan = JOBS.cancel_plan(ident)
        self.assertEqual(("forget", "admin", True), (plan["mode"], plan["needs"], plan["can"]))
        writes = list(self.kube.writes)
        result = JOBS.cancel(ident)
        self.assertEqual(writes, self.kube.writes, "it must not be able to delete anything")
        self.assertEqual("cancelled", next(j for j in JOBS.list_operations() if j["id"] == ident)["status"])
        self.assertIn("Homestead sends no more deletes", result["detail"])
        self.assertTrue(self.kube.object(DV + DISK))
        self.assertEqual([], JOBS.cdi_holds(NS, DISK), "the name is free once that is said")

    def test_what_is_being_deleted_isnt_called_still_there(self):
        ident = self.stuck()
        said = {"plan": " ".join(JOBS.cancel_plan(ident)["keeps"]), "ending": JOBS.cancel(ident)["detail"]}
        for where, words in said.items():
            with self.subTest(where):
                self.assertIn(f"Being deleted, and Kubernetes will finish this: DataVolume {DISK}, claim {DISK}, volume pv-disk", words)
                self.assertNotIn("Still there", words)
                self.assertNotIn("nothing more", words.lower())

    def test_a_retained_volume_is_still_there_beside_what_is_being_deleted(self):
        uid, _ = self.finished_disk(policy="Retain")
        for path, finalizer in ((DV + DISK, "cdi.kubevirt.io/held"), (CLAIMS + DISK, "kubernetes.io/pvc-protection")):
            held = self.kube.object(path)
            held["metadata"]["finalizers"] = [finalizer]
            self.kube.add(held)
        ident = self.start(uid)["operation"]["id"]
        self.look(ident)
        detail = JOBS.cancel(ident)["detail"]
        self.assertIn(f"Being deleted, and Kubernetes will finish this: DataVolume {DISK}, claim {DISK}", detail)
        self.assertIn("Still there: volume pv-disk", detail)

    def test_before_the_disk_is_asked_to_go_all_of_it_is_still_there(self):
        uid, _ = self.finished_disk()
        ident = self.start(uid)["operation"]["id"]
        detail = JOBS.cancel(ident)["detail"]
        self.assertIn(f"Still there: DataVolume {DISK}, claim {DISK}", detail)
        self.assertNotIn("Being deleted", detail)
        self.assertEqual([], self.kube.writes)

    def test_stopping_a_removal_that_would_write_next_writes_nothing(self):
        uid, _ = self.finished_disk(policy="Retain")
        ident = self.start(uid)["operation"]["id"]
        self.look(ident)
        released = self.kube.object(PVS + "pv-disk")
        released["status"]["phase"] = "Released"
        self.kube.add(released)
        writes = list(self.kube.writes)
        JOBS.cancel(ident)
        JOBS.list_operations()
        self.assertEqual(writes, self.kube.writes, "the next round would have patched the volume")
        self.assertEqual("Retain", K.kget(PVS + "pv-disk")["spec"]["persistentVolumeReclaimPolicy"])

    def test_it_says_when_it_cant_read_what_is_left_and_still_deletes_nothing(self):
        ident = self.stuck()
        writes = list(self.kube.writes)
        self.kube.fail(DV + DISK, 500, times=9)
        result = JOBS.cancel(ident)
        self.assertEqual(writes, self.kube.writes)
        self.assertIn("could not read", result["detail"])

    def test_a_viewer_or_operator_cant_stop_following_it(self):
        ident = self.stuck()
        with self.assertRaisesRegex(PermissionError, "admin"):
            JOBS.cancel(ident, allowed=lambda role: role == "operator")

    def test_the_job_keeps_no_source_address(self):
        uid, _ = self.finished_disk()
        self.start(uid)
        self.assertNotIn("http", str(JOBS.stored()[0]["ref"]))


class CreateVmTests(Base):
    def making(self, **more):
        with mock.patch.object(I, "NS", NS):
            VM_CREATE.prepare_vm({"name": "web", "namespace": NS, "disk_import": DISK, **more})

    def being_deleted(self, name):
        row = self.kube.object(DV + name)
        row["metadata"]["finalizers"] = ["cdi.kubevirt.io/held"]
        self.kube.add(row)
        self.kube("DELETE", DV + name)
        self.assertTrue(K.kget(DV + name)["metadata"]["deletionTimestamp"])

    def test_a_disk_being_deleted_cant_become_a_vm(self):
        self.finished_disk()
        self.being_deleted(DISK)
        with self.assertRaisesRegex(ValueError, "being removed"):
            self.making()

    def test_a_disk_whose_removal_has_started_cant_become_a_vm(self):
        uid, _ = self.finished_disk()
        REMOVE.remove({"namespace": NS, "name": DISK, "uid": uid, "confirmation": DISK})
        self.assertIsNone(K.kget(DV + DISK)["metadata"].get("deletionTimestamp"), "not asked to go yet")
        with self.assertRaisesRegex(ValueError, "being removed"):
            self.making()

    def test_a_second_disk_being_removed_cant_join_a_vm(self):
        self.finished_disk()
        self.disk(name="other-disk", phase="Succeeded")
        self.being_deleted("other-disk")
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.making(extra_disks=[{"name": "other-disk"}])

    def test_a_second_disk_whose_removal_has_started_cant_join_a_vm(self):
        self.finished_disk()
        uid = self.disk(name="other-disk", phase="Succeeded")
        REMOVE.remove({"namespace": NS, "name": "other-disk", "uid": uid, "confirmation": "other-disk"})
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.making(extra_disks=[{"name": "other-disk"}])

    def test_an_import_making_the_vm_from_its_own_disks_isnt_refused(self):
        """An Unraid import holds its disks while it makes its VM."""
        self.finished_disk()
        self.job_on("unraid-vm-import", "running", disks=[{"dv": DISK}])
        self.making()


if __name__ == "__main__":
    unittest.main()
