"""homestead_app_hold.pods_of: the pods an app still has, followed through
its controllers by uid - Deployment through ReplicaSet, CronJob through Job,
VirtualMachine through its instance - and None whenever that can't be told."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_app_hold as HOLD
from fakes.kube import Kube

NS = "apps"
PODS = f"/api/v1/namespaces/{NS}/pods"


def obj(kind, name, uid, api="apps/v1", owner=None, **extra):
    meta = {"name": name, "namespace": NS, "uid": uid, **extra.pop("meta", {})}
    if owner:
        meta["ownerReferences"] = [{"apiVersion": owner["apiVersion"], "kind": owner["kind"],
                                    "name": owner["metadata"]["name"], "uid": owner["metadata"]["uid"],
                                    "controller": True}]
    return {"apiVersion": api, "kind": kind, "metadata": meta, **extra}


def pod(name, owner=None, phase="Running", **meta):
    return obj("Pod", name, f"uid-{name}", api="v1", owner=owner, status={"phase": phase}, meta=meta)


def names(pods):
    return sorted(p["metadata"]["name"] for p in pods)


class PodsOfTest(unittest.TestCase):
    def kube(self, *objects):
        return self.enterContext(Kube(*objects))

    def test_deployment_pods_include_terminating(self):
        web = obj("Deployment", "web", "d-web")
        rs = obj("ReplicaSet", "web-abc12", "rs-web", owner=web)
        old = pod("web-abc12-old", rs, deletionTimestamp="2026-11-01T00:00:00Z")
        live = pod("web-abc12-new", rs)
        done = pod("web-abc12-done", rs, phase="Succeeded")
        self.kube(web, rs, old, live, done)
        self.assertEqual(names(HOLD.pods_of(web)), ["web-abc12-new", "web-abc12-old"])

    def test_statefulset_pods(self):
        db = obj("StatefulSet", "db", "s-db")
        other = obj("StatefulSet", "db2", "s-db2")
        self.kube(db, other, pod("db-0", db), pod("db-1", db, phase="Pending"),
                  pod("db-2", db, phase="Failed"), pod("db2-0", other))
        self.assertEqual(names(HOLD.pods_of(db)), ["db-0", "db-1"])

    def test_cronjob_pods_through_jobs(self):
        cron = obj("CronJob", "backup", "c-backup", api="batch/v1")
        job = obj("Job", "backup-2900", "j-1", api="batch/v1", owner=cron)
        stray = obj("Job", "backup-manual", "j-2", api="batch/v1")
        self.kube(cron, job, stray, pod("backup-2900-x", job), pod("backup-manual-y", stray),
                  pod("backup-2900-z", job, phase="Succeeded"))
        self.assertEqual(names(HOLD.pods_of(cron)), ["backup-2900-x"])

    def test_vm_pods_through_vmi(self):
        vm = obj("VirtualMachine", "vm1", "vm-1", api="kubevirt.io/v1")
        vmi = obj("VirtualMachineInstance", "vm1", "vmi-1", api="kubevirt.io/v1", owner=vm)
        other_vm = obj("VirtualMachine", "vm2", "vm-2", api="kubevirt.io/v1")
        other_vmi = obj("VirtualMachineInstance", "vm2", "vmi-2", api="kubevirt.io/v1", owner=other_vm)
        launcher = pod("virt-launcher-vm1-abcde", vmi, labels={"kubevirt.io": "virt-launcher"})
        # A migration target known by the VMI's uid in kubevirt.io/created-by counts too.
        target = pod("virt-launcher-vm1-fghij", labels={"kubevirt.io/created-by": "vmi-1"})
        self.kube(vm, vmi, other_vm, other_vmi, launcher, target,
                  pod("virt-launcher-vm2-zzzzz", other_vmi))
        self.assertEqual(names(HOLD.pods_of(vm)), ["virt-launcher-vm1-abcde", "virt-launcher-vm1-fghij"])

    def test_no_pods_left_is_an_empty_list(self):
        db = obj("StatefulSet", "db", "s-db")
        self.kube(db)
        self.assertEqual(HOLD.pods_of(db), [])

    def test_unreadable_pods_is_none(self):
        web = obj("Deployment", "web", "d-web")
        db = obj("StatefulSet", "db", "s-db")
        cron = obj("CronJob", "backup", "c-backup", api="batch/v1")
        vm = obj("VirtualMachine", "vm1", "vm-1", api="kubevirt.io/v1")
        kube = self.kube(web, db, cron, vm)
        kube.fail(PODS, 500, times=8)
        for app in (web, db, cron, vm):
            with self.subTest(app["kind"]):
                self.assertIsNone(HOLD.pods_of(app))

    def test_unreadable_owners_is_none(self):
        web = obj("Deployment", "web", "d-web")
        cron = obj("CronJob", "backup", "c-backup", api="batch/v1")
        vm = obj("VirtualMachine", "vm1", "vm-1", api="kubevirt.io/v1")
        kube = self.kube(web, cron, vm)
        for app, path in ((web, f"/apis/apps/v1/namespaces/{NS}/replicasets"),
                          (cron, f"/apis/batch/v1/namespaces/{NS}/jobs"),
                          (vm, f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachineinstances")):
            with self.subTest(app["kind"]):
                kube.fail(path, 500)
                self.assertIsNone(HOLD.pods_of(app))

    def test_broken_owner_chain_is_none(self):
        # A live pod whose controller (ReplicaSet, Job, VMI) is not in the
        # list may be the app's: whose it is can't be told.
        web = obj("Deployment", "web", "d-web")
        gone_rs = obj("ReplicaSet", "web-old", "rs-gone", owner=web)
        cron = obj("CronJob", "backup", "c-backup", api="batch/v1")
        gone_job = obj("Job", "backup-1", "j-gone", api="batch/v1", owner=cron)
        vm = obj("VirtualMachine", "vm1", "vm-1", api="kubevirt.io/v1")
        gone_vmi = obj("VirtualMachineInstance", "vm1", "vmi-gone", api="kubevirt.io/v1", owner=vm)
        for app, orphan in ((web, pod("web-old-a", gone_rs)), (cron, pod("backup-1-a", gone_job)),
                            (vm, pod("virt-launcher-vm1-a", gone_vmi))):
            with self.subTest(app["kind"]), Kube(app, orphan):
                self.assertIsNone(HOLD.pods_of(app))

    def test_an_app_without_a_uid_is_none(self):
        db = obj("StatefulSet", "db", "s-db")
        self.kube(db, pod("db-0", db))
        self.assertIsNone(HOLD.pods_of({**db, "metadata": {"name": "db", "namespace": NS}}))

    def test_other_kinds_are_none(self):
        rs = obj("ReplicaSet", "lone", "rs-lone")
        self.kube(rs, pod("lone-a", rs))
        self.assertIsNone(HOLD.pods_of(rs))

    def test_a_name_prefix_alone_never_counts(self):
        web = obj("Deployment", "web", "d-web")
        web_db = obj("Deployment", "web-db", "d-web-db")
        rs = obj("ReplicaSet", "web-db-abc12", "rs-web-db", owner=web_db)
        sts = obj("StatefulSet", "web", "s-web")
        sts_db = obj("StatefulSet", "web-db", "s-web-db")
        self.kube(web, web_db, rs, sts, sts_db, pod("web-db-abc12-x", rs), pod("web-db-0", sts_db),
                  pod("web-0-lookalike"))
        self.assertEqual(HOLD.pods_of(web), [])
        self.assertEqual(HOLD.pods_of(sts), [])


if __name__ == "__main__":
    unittest.main()
