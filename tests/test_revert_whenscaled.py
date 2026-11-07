"""A rollback stops a StatefulSet by scaling it to 0. With whenScaled: Delete
Kubernetes then deletes its claims, the one being rolled back included, and
the volume and its snapshots can go with it. So the review blocks it, as a
storage move's does. Through plan and start, on the shared fake API."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_reclass as RECLASS
import homestead_revert as REVERT
from fakes.kube import Kube

NS, CLAIM, VOLUME = "lab", "data-db-0", "pvc-1"
DB = f"/apis/apps/v1/namespaces/{NS}/statefulsets/db"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


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
    return {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "db", "namespace": NS}, "spec": spec}


class RollingBackAStatefulSet(unittest.TestCase):
    def review(self, retention):
        self.kube = self.enterContext(Kube(
            statefulset(retention),
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": CLAIM, "namespace": NS},
             "spec": {"volumeName": VOLUME}, "status": {"phase": "Bound"}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
             "metadata": {"name": VOLUME, "namespace": "longhorn-system"},
             "status": {"state": "attached", "kubernetesStatus": {"namespace": NS, "pvcName": CLAIM}}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
             "metadata": {"name": "snap-a", "namespace": "longhorn-system"},
             "spec": {"volume": VOLUME}, "status": {"readyToUse": True}},
            groups=("kubevirt.io",)))
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
            REVERT.start(VOLUME, "snap-a", ops)
        ops.start.assert_not_called()
        self.assertEqual([], [w for w in self.kube.writes if w[1] == DB])

    def test_retain_is_not_blocked(self):
        for retention in (None, {"whenScaled": "Retain"}, {"whenDeleted": "Delete"}):
            with self.subTest(retention=retention):
                plan = self.review(retention)
                self.assertEqual([], plan["blockers"])
                self.assertTrue(plan["ready"])


class ChangedAfterTheReview(unittest.TestCase):
    """The review is minutes old by the stop, and the stop retries: each stop of
    a StatefulSet reads it first, and is conditional on what it read."""

    def setUp(self):
        web = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NS},
               "spec": {"replicas": 2, "template": {"spec": {"volumes": [
                   {"name": "d", "persistentVolumeClaim": {"claimName": CLAIM}}]}}}}
        self.kube = self.enterContext(Kube(
            statefulset(), web,
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": CLAIM, "namespace": NS},
             "spec": {"volumeName": VOLUME}, "status": {"phase": "Bound"}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
             "metadata": {"name": VOLUME, "namespace": "longhorn-system"},
             "status": {"state": "detached", "kubernetesStatus": {"namespace": NS, "pvcName": CLAIM}}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
             "metadata": {"name": "snap-a", "namespace": "longhorn-system"},
             "spec": {"volume": VOLUME}, "status": {"readyToUse": True}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica",
             "metadata": {"name": "r-1", "namespace": "longhorn-system"},
             "spec": {"volumeName": VOLUME, "nodeID": "node-a"}, "status": {"currentState": "stopped"}},
            groups=("kubevirt.io",)))
        keep_bound(self)
        RECLASS.bind(K.kget, K.ksend, None, lambda: [])
        REVERT.bind(K.kget, K.ksend, RECLASS, lambda ns, name: False, self.longhorn)
        for patch in (mock.patch.object(REVERT.ACCESS, "reachable", return_value=""),
                      mock.patch.object(REVERT.OPS, "checkpoint", lambda item: None),
                      mock.patch.object(REVERT, "_admission", return_value={"blocked": False}),
                      mock.patch.object(REVERT, "RETRY_AFTER", 0)):
            patch.start()
            self.addCleanup(patch.stop)
        ops = mock.Mock()
        ops.start.side_effect = lambda kind, title, target, link, ref, message: ref
        self.item = {"id": "job-1", "ref": REVERT.start(VOLUME, "snap-a", ops)}
        self.calls = []

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
        K.ksend("PATCH", DB, {"spec": {"persistentVolumeClaimRetentionPolicy": {"whenScaled": "Delete"}}},
                ctype="application/merge-patch+json")

    def replicas(self, path=DB):
        return self.kube.object(path)["spec"]["replicas"]

    def stops(self):
        return [w for w in self.kube.writes if w[:2] == ("PATCH", DB) and w[2].get("spec", {}).get("replicas") == 0]

    def test_a_policy_changed_after_the_start_is_never_stopped_and_the_job_says_why(self):
        self.to_delete()
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.replicas())
        self.assertEqual([], self.stops())
        self.assertIn("db now deletes its volumes when stopped", message)
        self.assertIn("Nothing was changed", message)
        self.assertNotIn("starting again", message, "nothing had stopped")
        self.assertEqual([], self.calls)

    def test_once_something_had_stopped_the_job_says_the_volume_was_not_changed(self):
        web = f"/apis/apps/v1/namespaces/{NS}/deployments/web"
        order = [c["name"] for c in self.item["ref"]["consumers"]]
        self.item["ref"]["consumers"][order.index("web")]["stopped"] = True      # already stopped by an earlier round
        K.ksend("PATCH", web, {"spec": {"replicas": 0}}, ctype="application/merge-patch+json")
        self.to_delete()
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertIn("The volume was not changed", message)
        self.assertNotIn("Nothing was changed", message)
        self.assertIn("starting again", message)

    def test_nothing_else_is_stopped_for_it(self):
        self.to_delete()
        REVERT.resolve(self.item)
        web = f"/apis/apps/v1/namespaces/{NS}/deployments/web"
        self.assertEqual(2, self.replicas(web))
        self.assertEqual([], [w for w in self.kube.writes if w[:2] == ("PATCH", web)])

    def test_a_statefulset_replaced_after_the_start_is_never_stopped(self):
        self.kube.discard(DB)
        self.kube.add(statefulset())
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.replicas())
        self.assertEqual([], self.stops())
        self.assertIn("db was replaced", message)

    def test_the_stop_is_conditional_on_what_it_read(self):
        version = self.kube.object(DB)["metadata"]["resourceVersion"]
        REVERT.resolve(self.item)
        self.assertEqual([version], [w[2]["metadata"]["resourceVersion"] for w in self.stops()])
        self.assertEqual(0, self.replicas())

    def test_a_policy_changed_in_the_instant_before_the_stop_is_caught_by_its_condition(self):
        def beat_it(method, path, body):
            self.kube._reactions.clear()
            self.to_delete()
        self.kube.react("PATCH", DB, beat_it)
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("running", status, message)
        self.assertEqual(1, self.replicas(), "the conditional write lost")
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.replicas())
        self.assertIn("db now deletes its volumes when stopped", message)

    def test_a_read_that_fails_sends_no_stop_and_is_tried_again(self):
        self.kube.fail(DB, 500, method="GET")
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("running", status, message)
        self.assertEqual([], self.stops())
        self.assertIn("db could not be read", message)

    def test_a_stop_that_landed_with_its_reply_lost_is_not_sent_again_and_it_starts_again(self):
        self.kube.fail(DB, OSError("timed out"), method="PATCH", applied=True)
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("running", status, message)
        self.assertEqual(0, self.replicas())
        REVERT.resolve(self.item)
        self.assertEqual(1, len(self.stops()), "seen on the next read")
        REVERT.cancel_run(self.item, {})
        self.assertEqual(1, self.replicas(), "this job stopped it, so it starts it")

    def test_a_lost_stop_that_did_not_land_then_a_policy_change_is_not_stopped(self):
        self.kube.fail(DB, OSError("timed out"), method="PATCH")
        REVERT.resolve(self.item)
        self.to_delete()
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.replicas())
        self.assertIn("db now deletes its volumes when stopped", message)

    # A stop whose reply was lost: it may have landed, and the job must still start it ------

    WEB = f"/apis/apps/v1/namespaces/{NS}/deployments/web"

    def test_another_apps_lost_stop_then_a_refusal_starts_it_again(self):
        self.kube.fail(self.WEB, OSError("timed out"), method="PATCH", applied=True)
        self.assertEqual("running", REVERT.resolve(self.item)[0])
        self.assertEqual(0, self.replicas(self.WEB))
        self.to_delete()
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertEqual(2, self.replicas(self.WEB))
        self.assertIn("The volume was not changed", message)
        self.assertIn("starting again", message)

    def test_a_lost_stop_then_a_cancel_starts_it_again(self):
        self.kube.fail(DB, OSError("timed out"), method="PATCH", applied=True)
        REVERT.resolve(self.item)
        self.assertEqual(0, self.replicas())
        REVERT.cancel_run(self.item, {})
        self.assertEqual(1, self.replicas())

    def test_a_lost_stop_then_giving_up_starts_it_again(self):
        self.kube.fail(DB, OSError("timed out"), method="PATCH", applied=True)
        REVERT.resolve(self.item)
        self.kube.fail(DB, 500, method="GET")
        self.item["ref"]["since"] -= REVERT.WAIT["stopping"] + 1
        for _ in range(6):
            status, _, message = REVERT.resolve(self.item)
            if status != "running":
                break
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.replicas())

    def test_a_lost_stop_that_never_landed_gets_no_start_write(self):
        self.kube.fail(self.WEB, OSError("timed out"), method="PATCH")
        REVERT.resolve(self.item)
        before = [w for w in self.kube.writes if w[:2] == ("PATCH", self.WEB)]
        REVERT.cancel_run(self.item, {})
        self.assertEqual(2, self.replicas(self.WEB))
        self.assertEqual(before, [w for w in self.kube.writes if w[:2] == ("PATCH", self.WEB)])

    def test_that_a_stop_went_out_is_saved_before_it_goes(self):
        """The pod dies in the stop, the write stands: the next leader only has what was saved."""
        class Died(BaseException):
            pass
        kept = []
        with mock.patch.object(REVERT.OPS, "checkpoint", lambda item: kept.append(json.loads(json.dumps(item)))):
            self.kube.fail(DB, Died(), method="PATCH", applied=True)
            with self.assertRaises(Died):
                REVERT.resolve(self.item)
        self.assertEqual(0, self.replicas())
        REVERT.cancel_run(kept[-1], {})
        self.assertEqual(1, self.replicas(), "the saved job knew the stop had gone out")

    # A landed stop, then the policy flips ---------------------------------------------------

    def test_a_policy_flip_after_the_stop_landed_with_the_claim_still_there_goes_on_and_starts_it(self):
        REVERT.resolve(self.item)
        self.assertEqual(0, self.replicas())
        self.to_delete()
        for _ in range(8):
            status, _, message = REVERT.resolve(self.item)
            if status != "running":
                break
        self.assertIn("snapshotRevert", self.calls)
        self.assertEqual(1, self.replicas())

    def test_a_claim_being_deleted_is_not_rolled_back(self):
        """Stopped at 0 and flipped to Delete: Kubernetes may delete the claim. The revert is the last moment to look."""
        REVERT.resolve(self.item)
        self.to_delete()
        pvc = self.kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{CLAIM}")
        pvc["metadata"]["deletionTimestamp"] = "2026-11-06T00:00:00Z"
        pvc["metadata"]["finalizers"] = ["kubernetes.io/pvc-protection"]
        self.kube.add(pvc)
        for _ in range(6):
            status, _, message = REVERT.resolve(self.item)
            if status != "running":
                break
        self.assertEqual("failed", status, message)
        self.assertNotIn("snapshotRevert", self.calls)
        self.assertIn(f"the claim {CLAIM} is gone or being removed", message)

    # What the person is told -----------------------------------------------------------------

    def test_a_person_stopping_it_after_the_review_is_left_as_it_is(self):
        """It went to 0 with no stop of ours: not this job's to start, and not a lost stop."""
        K.ksend("PATCH", DB, {"spec": {"replicas": 0}}, ctype="application/merge-patch+json")
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertEqual(0, self.replicas())
        self.assertIn("db was stopped by something else since this rollback was reviewed", message)
        self.assertNotIn("starting again", message)

    def test_policy_delete_at_zero_with_no_stop_of_ours_does_not_go_on(self):
        K.ksend("PATCH", DB, {"spec": {"replicas": 0, "persistentVolumeClaimRetentionPolicy": {"whenScaled": "Delete"}}},
                ctype="application/merge-patch+json")
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("failed", status, message)
        self.assertEqual([], self.calls)

    def test_a_conflict_says_it_looks_again(self):
        self.kube.fail(DB, 409, method="PATCH")
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("running", status, message)
        self.assertIn("db changed as it was being stopped; looking at it again", message)

    def test_the_words_name_the_field_to_change(self):
        self.to_delete()
        self.assertIn("whenScaled", REVERT.resolve(self.item)[2])

    def test_a_gone_statefulset_is_gone_not_a_refusal(self):
        self.kube.discard(DB)
        status, _, message = REVERT.resolve(self.item)
        self.assertEqual("running", status, message)
        self.assertTrue(next(c for c in self.item["ref"]["consumers"] if c["name"] == "db")["gone"])


if __name__ == "__main__":
    unittest.main()
