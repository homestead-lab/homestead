"""Rolling a volume back must not leave its apps stopped for good.

Two ways it did (the "apps left stopped" sweep, storage revert):
- another app on the same volume that Flux or Helm had already deleted made
  the stop step's 404 fail the job, with the first app stopped;
- an error starting the apps again after the rollback was raised every round,
  for ever, with them stopped and Cancel refused.

Starting an app again is also checked first: it is the same object (UID), it
still mounts the same claim on the same volume, nothing else holds that claim,
no copy or import holds the app, and it still fits. Any doubt leaves it
stopped, and the job ends saying why and what to do.

On the shared fake API (tests/fakes/kube.py), with a fake clock that only
homestead_revert sees (the global time.time is left alone for other threads)."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_reclass as RECLASS
import homestead_revert as REVERT
from fakes.kube import Kube

NS, CLAIM, VOLUME = "lab", "data", "pvc-1"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
APPS = f"/apis/apps/v1/namespaces/{NS}/deployments"


def deployment(name, replicas, claim=CLAIM, annotations=None):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": NS, "annotations": annotations or {}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}}, "spec": {
                         "containers": [{"name": "app", "image": "example.com/app:1"}],
                         "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}},
            "status": {"readyReplicas": replicas}}


def pod(name, deleting=False):
    meta = {"name": name, "namespace": NS}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-31T00:00:00Z"
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "status": {"phase": "Running"},
            "spec": {"nodeName": "node-a", "containers": [{"name": "app", "image": "example.com/app:1"}],
                     "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": CLAIM}}]}}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class RevertStopTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(
            deployment("app-a", 2), deployment("app-b", 1),
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": CLAIM, "namespace": NS},
             "spec": {"volumeName": VOLUME}, "status": {"phase": "Bound"}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
             "metadata": {"name": VOLUME, "namespace": "longhorn-system"},
             "status": {"state": "attached", "kubernetesStatus": {"namespace": NS, "pvcName": CLAIM}}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
             "metadata": {"name": "snap-a", "namespace": "longhorn-system"},
             "spec": {"volume": VOLUME}, "status": {"readyToUse": True}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica",
             "metadata": {"name": "r-1", "namespace": "longhorn-system"},
             "spec": {"volumeName": VOLUME, "nodeID": "node-a"}, "status": {"currentState": "stopped"}},
            groups=("kubevirt.io",)))
        RECLASS.bind(K.kget, K.ksend, None, lambda: [])
        self.calls = []
        REVERT.bind(K.kget, K.ksend, RECLASS, lambda ns, name: False, self.lh_call)
        self.clock = Clock()
        for patch in (mock.patch.object(REVERT, "time", types.SimpleNamespace(time=self.clock)),
                      mock.patch.object(REVERT.ACCESS, "reachable", return_value=""),
                      mock.patch.object(REVERT.OPS, "checkpoint", lambda item: None),
                      mock.patch.object(REVERT, "_admission", side_effect=self.admit, create=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.fits = True
        self.admitted = []
        ops = mock.Mock()
        ops.start.side_effect = lambda kind, title, target, link, ref, message: ref
        self.item = {"ref": REVERT.start(VOLUME, "snap-a", ops)}
        self.recorded = ops.start.call_args.args[4]

    def admit(self, obj):
        self.admitted.append((obj["metadata"]["name"], obj["spec"]["replicas"]))
        return {"blocked": not self.fits, "candidates": [
            {"name": "node-a", "eligible": self.fits, "reasons": getattr(self, "reasons", [])}]}

    def lh_call(self, method, path, body=None):
        """Longhorn's own API: the volume follows what is asked of it."""
        self.calls.append(path.split("action=")[-1])
        if path.endswith("action=attach"):
            self.state("attached")
        elif path.endswith("action=snapshotCreate"):
            self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
                           "metadata": {"name": body["name"], "namespace": "longhorn-system"},
                           "spec": {"volume": VOLUME}})
        elif path.endswith("action=detach"):
            self.state("detached")
        return {}

    def state(self, state):
        volume = self.kube.object(f"{LH}/volumes/{VOLUME}")
        volume["status"]["state"] = state
        self.kube.add(volume)

    def owned_pod(self, name, app, deleting=False):
        """A pod of app, owned the way Kubernetes owns it: through a ReplicaSet."""
        owner = self.kube.object(f"{APPS}/{app}")["metadata"]["uid"]
        self.kube.add({"apiVersion": "apps/v1", "kind": "ReplicaSet",
                       "metadata": {"name": f"{app}-rs", "namespace": NS, "ownerReferences": [
                           {"kind": "Deployment", "name": app, "uid": owner, "controller": True}]}})
        rs = self.kube.object(f"/apis/apps/v1/namespaces/{NS}/replicasets/{app}-rs")["metadata"]["uid"]
        row = pod(name, deleting)
        row["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": f"{app}-rs", "uid": rs, "controller": True}]
        self.kube.add(row)

    def replicas(self, name):
        return (self.kube.object(f"{APPS}/{name}") or {}).get("spec", {}).get("replicas")

    def resolve(self):
        return REVERT.resolve(self.item)

    def stop(self):
        status, _, message = self.resolve()
        self.assertEqual("running", status, message)

    def rollback(self):
        """Once stopped: let go, attached, kept, reverted, let go; then starting again."""
        self.state("detached")
        self.resolve()                                  # attaches
        self.resolve()                                  # keeps, reverts, lets go
        return self.resolve()                           # starts again

    def until_done(self, rounds=10):
        """Resolve round after round (each past the retry pause) until the job ends."""
        for _ in range(rounds):
            status, progress, message = self.resolve()
            if status != "running":
                break
            self.clock.now += REVERT.RETRY_AFTER + 1
        return status, progress, message

    def late(self):
        self.clock.now += REVERT.WAIT["restarting"] + 1
        return self.resolve()

    # Recorded before anything is changed -------------------------------------

    def test_what_each_app_ran_and_which_object_it_is_are_recorded_before_the_stop(self):
        rows = {c["name"]: c for c in self.recorded["consumers"]}
        self.assertEqual({"app-a": 2, "app-b": 1}, {name: row["replicas"] for name, row in rows.items()})
        self.assertEqual(self.kube.object(f"{APPS}/app-a")["metadata"]["uid"], rows["app-a"]["uid"])
        self.assertEqual(self.kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{CLAIM}")["metadata"]["uid"],
                         self.recorded["claim_uid"])
        self.assertEqual([], [w for w in self.kube.writes if w[0] != "GET"], "nothing changes before the job starts")

    # The 404 in the stop step ---------------------------------------------------

    def test_an_app_already_deleted_does_not_fail_the_stop_and_the_rest_start_again(self):
        self.kube.discard(f"{APPS}/app-b")              # Flux or Helm removed it
        self.stop()
        self.assertEqual(0, self.replicas("app-a"))
        status, _, message = self.rollback()
        self.assertEqual("running", status, message)
        self.assertEqual(2, self.replicas("app-a"), "app A runs again as it did")
        self.assertIsNone(self.kube.object(f"{APPS}/app-b"), "a deleted app is never made again")
        status, _, message = self.resolve()
        self.assertEqual("succeeded", status, message)
        self.assertIn("app-b", message)

    def test_a_stop_that_fails_is_not_taken_as_gone(self):
        """A 500 is not a 404: app B may still be running on the volume."""
        self.kube.fail(f"{APPS}/app-b", 500, times=1000, method="PATCH")
        status, _, message = self.resolve()
        self.assertEqual("running", status, message)
        self.assertIn("app-b", message)
        self.assertFalse(any(c.get("gone") for c in self.item["ref"]["consumers"]))
        self.state("detached")
        self.resolve()
        self.assertEqual([], self.calls, "nothing is attached while app B may still use it")
        self.clock.now += REVERT.WAIT["stopping"] + 1
        status, _, message = self.resolve()
        self.assertEqual("failed", status, message)
        self.assertIn("Nothing was changed", message)
        self.assertEqual(2, self.replicas("app-a"), "app A is put back when the stop cannot finish")

    # Starting again after the rollback ---------------------------------------

    def test_a_lasting_error_starting_again_ends_the_job_and_says_what_to_do(self):
        self.kube.fail(f"{APPS}/app-a", 403, times=1000, method="PATCH", skip=1)   # the stop goes through
        self.stop()
        status, _, message = self.rollback()
        self.assertEqual("running", status, message)
        self.assertEqual(0, self.replicas("app-a"))
        self.assertFalse(REVERT.cancel_plan(self.item)["can"])
        status, _, message = self.late()
        self.assertEqual("failed", status, message)
        self.assertIn("Rolled back to snap-a", message)
        self.assertIn("Containers → app-a → Start with 2 replicas", message)
        self.assertEqual(1, self.replicas("app-b"), "what could start, started")

    def test_an_app_replaced_while_stopped_is_left_as_it_is(self):
        self.stop()
        self.kube.discard(f"{APPS}/app-a")
        self.kube.add(deployment("app-a", 0))           # made again by Flux: a different object
        self.rollback()
        status, _, message = self.late()
        self.assertEqual("failed", status, message)
        self.assertEqual(0, self.replicas("app-a"), "never started on another object's behalf")
        self.assertIn("app-a was replaced", message)

    def test_an_app_that_no_longer_mounts_the_claim_is_left_stopped(self):
        self.stop()
        moved = self.kube.object(f"{APPS}/app-a")
        moved["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other"
        self.kube.add(moved)
        self.rollback()
        status, _, message = self.late()
        self.assertEqual("failed", status, message)
        self.assertEqual(0, self.replicas("app-a"))
        self.assertIn(f"no longer uses {CLAIM}", message)

    def test_a_pod_still_on_the_claim_keeps_everything_stopped_until_it_goes(self):
        self.stop()
        self.state("detached")
        self.resolve()
        self.resolve()
        self.kube.add(pod("app-a-old", deleting=True))  # still terminating
        status, _, message = self.resolve()
        self.assertEqual("running", status, message)
        self.assertEqual((0, 0), (self.replicas("app-a"), self.replicas("app-b")))
        self.assertIn("app-a-old", message)
        self.kube.discard(f"/api/v1/namespaces/{NS}/pods/app-a-old")
        self.resolve()
        self.assertEqual((2, 1), (self.replicas("app-a"), self.replicas("app-b")))

    def test_a_claim_bound_to_another_volume_keeps_everything_stopped(self):
        self.stop()
        claim = self.kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{CLAIM}")
        claim["spec"]["volumeName"] = "pvc-2"
        self.kube.add(claim)
        self.rollback()
        status, _, message = self.late()
        self.assertEqual("failed", status, message)
        self.assertEqual((0, 0), (self.replicas("app-a"), self.replicas("app-b")))
        self.assertIn("pvc-2", message)

    def test_a_copy_hold_keeps_that_app_stopped(self):
        self.stop()
        held = self.kube.object(f"{APPS}/app-a")
        held["metadata"]["annotations"]["homestead.io/storage-copy-job"] = "op-1"
        self.kube.add(held)
        self.rollback()
        status, _, message = self.late()
        self.assertEqual("failed", status, message)
        self.assertEqual(0, self.replicas("app-a"))
        self.assertIn("storage copy", message)

    def test_an_app_that_no_longer_fits_is_left_stopped(self):
        self.fits = False
        self.stop()
        self.rollback()
        status, _, message = self.late()
        self.assertEqual("failed", status, message)
        self.assertEqual(0, self.replicas("app-a"))
        self.assertIn(("app-a", 2), self.admitted, "admitted at the count it ran before")
        self.assertIn("no longer fits", message)

    def test_an_app_that_cannot_be_read_is_not_taken_as_gone_when_starting(self):
        self.stop()
        self.kube.fail(f"{APPS}/app-a", 500, times=1000, method="GET")
        self.rollback()
        status, _, message = self.late()
        self.assertEqual("failed", status, message)
        self.assertIn("could not read app-a", message)
        self.assertEqual(1, len([w for w in self.kube.writes if w[:2] == ("PATCH", f"{APPS}/app-a")]),
                         "only the stop was sent")

    def test_the_start_is_conditional_on_the_object_read(self):
        self.stop()
        self.rollback()
        starts = [w for w in self.kube.writes if w[:2] == ("PATCH", f"{APPS}/app-a") and w[2]["spec"]["replicas"]]
        self.assertEqual(1, len(starts))
        self.assertTrue(starts[0][2]["metadata"]["resourceVersion"])

    # After the review of the first version ---------------------------------

    def test_the_pods_of_an_app_started_again_do_not_block_the_next(self):
        self.kube.fail(f"{APPS}/app-b", 500, times=1, method="PATCH", skip=1)   # B's first start fails
        self.stop()
        status, _, message = self.rollback()
        self.assertEqual("running", status, message)
        self.assertEqual((2, 0), (self.replicas("app-a"), self.replicas("app-b")))
        self.owned_pod("app-a-1", "app-a")             # A is up
        status, _, message = self.resolve()
        self.assertEqual("running", status, message)
        self.assertEqual(1, self.replicas("app-b"), "A's own new pod is not an old writer")

    def test_a_start_whose_reply_was_lost_is_seen_as_started(self):
        self.kube.fail(f"{APPS}/app-a", 504, times=1, method="PATCH", skip=1, applied=True)
        self.stop()
        self.rollback()
        self.assertEqual(2, self.replicas("app-a"), "the write went through")
        self.owned_pod("app-a-1", "app-a")
        status, _, message = self.resolve()
        self.assertEqual("running", status, message)
        self.assertEqual("starting", self.item["ref"]["phase"], message)
        status, _, message = self.resolve()
        self.assertEqual("succeeded", status, message)
        self.assertNotIn("left stopped", message)

    def test_a_put_back_tells_pods_apart_by_owner_not_by_name(self):
        """app-a-db-1 belongs to app-b: it keeps B stopped, not A."""
        self.owned_pod("app-a-db-1", "app-b", deleting=True)
        self.stop()
        self.clock.now += REVERT.WAIT["stopping"] + 1
        status, _, message = self.until_done()
        self.assertEqual("failed", status, message)
        self.assertEqual((2, 0), (self.replicas("app-a"), self.replicas("app-b")))
        self.assertIn("app-b was left stopped: app-a-db-1 still uses data", message)

    def test_a_put_back_tries_a_failed_start_again_a_few_times(self):
        self.kube.fail(f"{APPS}/app-a", 500, times=1, method="PATCH", skip=1)
        self.kube.fail(f"{APPS}/app-b", 500, times=1000, method="PATCH")
        self.stop()
        self.clock.now += REVERT.WAIT["stopping"] + 1
        rounds = []
        for _ in range(REVERT.TRIES + 2):
            status, _, message = self.resolve()
            rounds.append(status)
            self.clock.now += REVERT.RETRY_AFTER + 1
            if status != "running":
                break
        self.assertEqual("failed", status, message)
        self.assertLessEqual(len(rounds), REVERT.TRIES)
        self.assertEqual(2, self.replicas("app-a"), "a passing failure is ridden out")
        self.assertIn("Nothing was changed", message)

    def test_the_hosts_reasons_are_named_when_an_app_does_not_fit(self):
        self.fits = False
        self.reasons = ["memory request exceeds remaining scheduler capacity"]
        self.stop()
        self.rollback()
        status, _, message = self.late()
        self.assertIn("memory request exceeds remaining scheduler capacity", message)

    def test_a_scheduled_job_says_where_to_resume_it(self):
        how = REVERT._how({"kind": "CronJob", "name": "backup"})
        self.assertIn("Scheduled jobs", how)
        self.assertIn("suspend", how)


if __name__ == "__main__":
    unittest.main()
