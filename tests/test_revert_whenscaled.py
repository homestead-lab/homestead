"""A rollback stops a StatefulSet by scaling it to 0. With whenScaled: Delete
Kubernetes then deletes its claims, the one being rolled back included, and
the volume and its snapshots can go with it. So the review blocks it, as a
storage move's does, and the revert looks at the claim once more before it goes
out. A policy changed after the review is refused by the fenced stop: see
test_revert_fenced. Through plan, start, resolve and cancel_run, on the shared
fake API."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_deploy_server as DEPLOY
import homestead_kube as K
import homestead_reclass as RECLASS
import homestead_revert as REVERT
from fakes.kube import Kube

NS, CLAIM, VOLUME = "lab", "data-db-0", "pvc-1"
DB = f"/apis/apps/v1/namespaces/{NS}/statefulsets/db"
PVC = f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{CLAIM}"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
MERGE = "application/merge-patch+json"


def keep_bound(test):
    """Put back what the binds below set, once the test ends."""
    for module, names in ((RECLASS, ("kget", "ksend", "ktext", "storage_classes", "capacity", "OWN_NS")),
                          (REVERT, ("kget", "ksend", "reclass", "is_self", "lh_call"))):
        for name in names:
            test.addCleanup(setattr, module, name, getattr(module, name))


def statefulset(retention=None):
    spec = {"replicas": 1, "selector": {"matchLabels": {"app": "db"}},
            "volumeClaimTemplates": [{"metadata": {"name": "data"}}],
            "template": {"metadata": {"labels": {"app": "db"}}, "spec": {
                "containers": [{"name": "db", "image": "example.com/db:1"}]}}}
    if retention:
        spec["persistentVolumeClaimRetentionPolicy"] = retention
    return {"apiVersion": "apps/v1", "kind": "StatefulSet",
            "metadata": {"name": "db", "namespace": NS, "generation": 1}, "spec": spec}


def cluster(*apps, state="detached"):
    return Kube(
        *apps,
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": CLAIM, "namespace": NS},
         "spec": {"volumeName": VOLUME}, "status": {"phase": "Bound"}},
        {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
         "metadata": {"name": VOLUME, "namespace": "longhorn-system"},
         "status": {"state": state, "kubernetesStatus": {"namespace": NS, "pvcName": CLAIM}}},
        {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
         "metadata": {"name": "snap-a", "namespace": "longhorn-system"},
         "spec": {"volume": VOLUME}, "status": {"readyToUse": True}},
        {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica",
         "metadata": {"name": "r-1", "namespace": "longhorn-system"},
         "spec": {"volumeName": VOLUME, "nodeID": "node-a"}, "status": {"currentState": "stopped"}},
        groups=("kubevirt.io",))


class RollingBackAStatefulSet(unittest.TestCase):
    def review(self, retention):
        self.kube = self.enterContext(cluster(statefulset(retention), state="attached"))
        keep_bound(self)
        RECLASS.bind(K.kget, K.ksend, None, lambda: [])
        REVERT.bind(K.kget, K.ksend, RECLASS, lambda ns, name: False, lambda *a, **k: {})
        return REVERT.plan(VOLUME, "snap-a")

    def test_when_scaled_delete_is_a_blocker_the_person_can_act_on(self):
        plan = self.review({"whenScaled": "Delete"})
        self.assertFalse(plan["ready"])
        self.assertEqual(1, len(plan["blockers"]))
        self.assertIn("StatefulSet db can delete its volumes when stopped", plan["blockers"][0])
        self.assertIn("Retain", plan["blockers"][0])

    def test_starting_anyway_is_refused_and_writes_nothing(self):
        self.review({"whenScaled": "Delete"})
        ops = mock.Mock()
        with mock.patch.object(REVERT.ACCESS, "reachable", return_value=""), \
                self.assertRaisesRegex(ValueError, "can delete its volumes"):
            REVERT.start(VOLUME, "snap-a", ops, "any")
        ops.start.assert_not_called()
        self.assertEqual([], [w for w in self.kube.writes if w[1] == DB])

    def test_retain_is_not_blocked(self):
        for retention in (None, {"whenScaled": "Retain"}, {"whenDeleted": "Delete"}):
            with self.subTest(retention=retention):
                plan = self.review(retention)
                self.assertEqual([], plan["blockers"])
                self.assertTrue(plan["ready"])


class Job(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(cluster(statefulset()))
        self.calls = []
        keep_bound(self)
        RECLASS.bind(K.kget, K.ksend, None, lambda: [])
        REVERT.bind(K.kget, K.ksend, RECLASS, lambda ns, name: False, self.longhorn)
        for patch in (mock.patch.object(REVERT.ACCESS, "reachable", return_value=""),
                      mock.patch.object(REVERT.OPS, "checkpoint", lambda item: None),
                      mock.patch.object(REVERT, "_admission", return_value={"blocked": False}),
                      mock.patch.object(DEPLOY, "copy_admission", return_value={"blocked": False}),
                      mock.patch.object(REVERT, "RETRY_AFTER", 0)):
            patch.start()
            self.addCleanup(patch.stop)

    def begin(self):
        ops = mock.Mock()
        ops.start.side_effect = lambda kind, title, target, link, ref, message: ref
        return {"id": "job-1", "ref": REVERT.start(VOLUME, "snap-a", ops, REVERT.plan(VOLUME, "snap-a")["review_token"])}

    def longhorn(self, method, path, body=None):
        self.calls.append(path.split("action=")[-1])
        if path.endswith("action=attach"):
            self.state("attached")
        elif path.endswith("action=detach"):
            self.state("detached")
        return {}

    def state(self, state):
        volume = self.kube.object(f"{LH}/volumes/{VOLUME}")
        volume["status"]["state"] = state
        self.kube.add(volume)

    def to_delete(self):
        K.ksend("PATCH", DB, {"spec": {"persistentVolumeClaimRetentionPolicy": {"whenScaled": "Delete"}}}, ctype=MERGE)

    def replicas(self):
        return self.kube.object(DB)["spec"]["replicas"]

    def rounds(self, item, limit=10):
        for _ in range(limit):
            status, _, message = REVERT.resolve(item)
            if status != "running":
                break
        return status, message


class TheClaimBeforeTheRevert(Job):
    def test_a_policy_flip_after_the_stop_landed_with_the_claim_still_there_goes_on_and_starts_it(self):
        item = self.begin()
        REVERT.resolve(item)
        self.assertEqual(0, self.replicas())
        self.to_delete()
        self.rounds(item)
        self.assertIn("snapshotRevert", self.calls)
        self.assertEqual(1, self.replicas())

    def test_a_claim_being_deleted_is_not_rolled_back(self):
        """Stopped at 0 and flipped to Delete: Kubernetes may delete the claim. The revert is the last moment to look."""
        item = self.begin()
        REVERT.resolve(item)
        self.to_delete()
        pvc = self.kube.object(PVC)
        pvc["metadata"]["deletionTimestamp"] = "2026-11-06T00:00:00Z"
        pvc["metadata"]["finalizers"] = ["kubernetes.io/pvc-protection"]
        self.kube.add(pvc)
        status, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertNotIn("snapshotRevert", self.calls)
        self.assertIn(f"the claim {CLAIM} is gone or being removed", message)

    def test_a_claim_already_gone_is_not_rolled_back(self):
        item = self.begin()
        REVERT.resolve(item)
        self.kube.discard(PVC)
        status, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertNotIn("snapshotRevert", self.calls)
        self.assertIn(f"the claim {CLAIM} is gone or being removed", message)


class AJobFromTheReleaseBefore(Job):
    """It has no record, and may hold `sent`: that its plain stop went out."""

    def job(self, **row):
        uid = self.kube.object(DB)["metadata"]["uid"]
        return {"id": "old-job", "ref": {"volume": VOLUME, "snapshot": "snap-a", "namespace": NS, "claim": CLAIM,
                                         "phase": "stopping", "since": REVERT.time.time(),
                                         "consumers": [{"kind": "StatefulSet", "name": "db", "replicas": 1, "uid": uid, **row}]}}

    def test_a_stop_that_went_out_and_landed_is_started_again_on_a_cancel(self):
        K.ksend("PATCH", DB, {"spec": {"replicas": 0}}, ctype=MERGE)
        message = REVERT.cancel_run(self.job(sent=True), {})
        self.assertEqual(1, self.replicas())
        self.assertIn("starting again", message)

    def test_a_stop_that_went_out_and_never_landed_gets_no_write(self):
        before = len(self.kube.writes)
        REVERT.cancel_run(self.job(sent=True), {})
        self.assertEqual(1, self.replicas())
        self.assertEqual(before, len(self.kube.writes))

    def test_nothing_writes_sent_any_more(self):
        item = self.begin()
        REVERT.resolve(item)
        self.assertFalse(any("sent" in c for c in item["ref"]["consumers"]))

    def test_with_no_record_and_a_policy_flip_it_is_not_stopped(self):
        self.to_delete()
        status, message = self.rounds(self.job())
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.replicas())
        self.assertIn("db was left as it is: it now deletes its volumes when stopped", message)
        self.assertEqual([], self.calls)


if __name__ == "__main__":
    unittest.main()
