"""A snapshot revert stops an app only as it was reviewed, and keeps a record.

Between the review and the stop an app can be edited, scaled or applied by
Flux: the revert refuses it and leaves it alone. What it does stop carries a
mark and a saved record (homestead_app_hold), so an interrupted revert starts
exactly that again. A write that landed but lost its reply is never sent
twice, and nothing a person reads says an app was left alone when it was
stopped. Through plan, start, resolve and cancel_run, on the shared fake API."""
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_agent_tools as AGENT
import homestead_app_hold as HOLD
import homestead_deploy_server as DEPLOY
import homestead_kube as K
import homestead_reclass as RECLASS
import homestead_revert as REVERT
from fakes.kube import Kube

NS, CLAIM, VOLUME = "lab", "data", "pvc-1"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
APPS = f"/apis/apps/v1/namespaces/{NS}/deployments"
A, B = f"{APPS}/app-a", f"{APPS}/app-b"
MERGE = "application/merge-patch+json"
JOB = "job-1"
MARK = f"snapshot-revert:{JOB}"


class Died(BaseException):
    """The pod goes away mid-step: nothing after this point is saved."""


def volumes(claim=CLAIM):
    return [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]


def deployment(name, replicas, claim=CLAIM, annotations=None):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": NS, "generation": 1, "annotations": annotations or {}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}}, "spec": {
                         "containers": [{"name": "app", "image": "example.com/app:1"}], "volumes": volumes(claim)}}},
            "status": {"readyReplicas": replicas}}


def statefulset(name, replicas):
    return {**deployment(name, replicas), "kind": "StatefulSet"}


def cronjob(name, suspend):
    return {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": name, "namespace": NS, "generation": 1},
            "spec": {"schedule": "0 1 * * *", "suspend": suspend, "jobTemplate": {"spec": {"template": {"spec": {
                "containers": [{"name": "app", "image": "example.com/app:1"}], "volumes": volumes()}}}}}}


def vm(name, strategy):
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": name, "namespace": NS, "generation": 1},
            "spec": {"runStrategy": strategy, "template": {"spec": {"volumes": volumes()}}},
            "status": {"printableStatus": "Running"}}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    apps = (lambda: (deployment("app-a", 2), deployment("app-b", 1)))

    def setUp(self):
        self.kube = self.enterContext(Kube(
            *type(self).apps(),
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
        RECLASS.bind(K.kget, K.ksend, None, lambda: [])
        self.clock = Clock()
        self.calls = []
        REVERT.bind(K.kget, K.ksend, RECLASS, lambda ns, name: False, self.lh_call)
        self.fits = True
        for patch in (mock.patch.object(REVERT, "time", types.SimpleNamespace(time=self.clock)),
                      mock.patch.object(REVERT.ACCESS, "reachable", return_value=""),
                      mock.patch.object(REVERT.OPS, "checkpoint", lambda item: None),
                      mock.patch.object(DEPLOY, "copy_admission", lambda obj: {"blocked": not self.fits}),
                      mock.patch.object(REVERT, "_admission", lambda obj: {"blocked": not self.fits}),
                      mock.patch.object(REVERT, "RETRY_AFTER", 0)):
            patch.start()
            self.addCleanup(patch.stop)
        self.ops = mock.Mock()
        self.ops.start.side_effect = lambda kind, title, target, link, ref, message: ref

    def lh_call(self, method, path, body=None):
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

    def review(self):
        return REVERT.plan(VOLUME, "snap-a")["review_token"]

    def begin(self):
        """Reviewed and started: the job, as the store keeps it."""
        return {"id": JOB, "ref": json.loads(json.dumps(REVERT.start(VOLUME, "snap-a", self.ops, self.review())))}

    def person_scales(self, path, replicas):
        K.ksend("PATCH", path, {"spec": {"replicas": replicas}}, ctype=MERGE)

    def controller_writes_status(self, path):
        K.ksend("PATCH", path, {"status": {"observedGeneration": self.clock.now}}, ctype=MERGE)

    def replicas(self, path):
        return self.kube.object(path)["spec"]["replicas"]

    def notes(self, path):
        return self.kube.object(path)["metadata"].get("annotations") or {}

    def writes(self, path):
        return [w for w in self.kube.writes if w[:2] == ("PATCH", path)]

    def rounds(self, item, until=("succeeded", "failed"), limit=14):
        """Round after round until the job ends."""
        for _ in range(limit):
            status, progress, message = REVERT.resolve(item)
            if status in until:
                return status, progress, message
        self.fail(f"still running: {message}")

    def late(self):
        self.clock.now += REVERT.WAIT["stopping"] + 1

    def saved(self, item):
        """What a new leader reads: the job as it was saved before this round."""
        return json.loads(json.dumps(item))


class ChangedAfterTheReview(Base):
    def test_a_scale_after_the_review_refuses_that_app_by_name_and_writes_nothing(self):
        token = self.review()
        self.person_scales(A, 3)
        before = len(self.kube.writes)
        with self.assertRaises(ValueError) as refused:
            REVERT.start(VOLUME, "snap-a", self.ops, token)
        self.assertIn("app-a changed after this rollback was reviewed", str(refused.exception))
        self.assertIn("Nothing was changed", str(refused.exception))
        self.ops.start.assert_not_called()
        self.assertEqual(before, len(self.kube.writes))

    def scale_on_read(self, nth, replicas):
        """A person scales app-a as Homestead makes its nth read of it."""
        count = [0]

        def read(method, path, body):
            count[0] += 1
            if count[0] == nth:
                self.person_scales(A, replicas)
        self.kube.react("GET", A, read)

    def test_a_scale_between_the_last_look_and_the_record_refuses(self):
        token = self.review()
        self.scale_on_read(2, 3)                        # 1: start's plan; 2: the read it records
        with self.assertRaisesRegex(ValueError, "app-a changed after this rollback was reviewed"):
            REVERT.start(VOLUME, "snap-a", self.ops, token)
        self.ops.start.assert_not_called()

    def test_a_scale_during_the_review_itself_starts_it_at_the_new_count(self):
        """Between the review's list and its read of the app: its token is of the
        new one, and the plan at the start lists the new count."""
        self.scale_on_read(1, 3)
        item = self.begin()
        rows = {c["name"]: c for c in item["ref"]["consumers"]}
        self.assertEqual(3, rows["app-a"]["replicas"])
        K.ksend("PATCH", A, {"status": {"readyReplicas": 3}}, ctype=MERGE)
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertEqual(3, self.replicas(A))

    def test_an_edit_that_keeps_the_count_refuses_too(self):
        token = self.review()
        K.ksend("PATCH", A, {"spec": {"template": {"metadata": {"labels": {"app": "app-a", "edited": "yes"}}}}}, ctype=MERGE)
        with self.assertRaisesRegex(ValueError, "app-a changed after this rollback was reviewed"):
            REVERT.start(VOLUME, "snap-a", self.ops, token)

    def test_an_app_replaced_after_the_review_refuses(self):
        token = self.review()
        self.kube.discard(A)
        self.kube.add(deployment("app-a", 2))
        with self.assertRaisesRegex(ValueError, "app-a changed after this rollback was reviewed"):
            REVERT.start(VOLUME, "snap-a", self.ops, token)

    def test_an_app_that_began_using_the_claim_after_the_review_refuses(self):
        token = self.review()
        self.kube.add(deployment("app-new", 1))
        with self.assertRaisesRegex(ValueError, "app-new began using data after this rollback was reviewed"):
            REVERT.start(VOLUME, "snap-a", self.ops, token)

    def test_an_app_that_stopped_using_the_claim_after_the_review_refuses(self):
        token = self.review()
        self.kube.discard(B)
        with self.assertRaisesRegex(ValueError, "app-b no longer uses data"):
            REVERT.start(VOLUME, "snap-a", self.ops, token)

    def test_a_controller_writing_status_is_not_a_change(self):
        token = self.review()
        self.controller_writes_status(A)
        REVERT.start(VOLUME, "snap-a", self.ops, token)
        self.ops.start.assert_called_once()

    def test_a_start_with_no_review_is_refused(self):
        for token in (None, "", "not a review", "e30"):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, "reload the page and open the rollback again"):
                REVERT.start(VOLUME, "snap-a", self.ops, token)
        self.ops.start.assert_not_called()

    def test_the_route_passes_what_the_person_reviewed(self):
        token = self.review()
        self.person_scales(A, 3)
        with mock.patch.object(REVERT.STORAGE_GUARD, "volume", mock.MagicMock()), \
                self.assertRaisesRegex(ValueError, "app-a changed"):
            REVERT.start_guarded({"volume": VOLUME, "snapshot": "snap-a", "review_token": token})

    def test_an_agents_request_carries_the_review_a_person_approves(self):
        answer = {"volume": VOLUME, "snapshot": "snap-a", "consumers": [], "blockers": [], "review_token": "reviewed"}
        with mock.patch.object(AGENT, "call", return_value=answer):
            _, args = AGENT._plan_revert({"volume": VOLUME, "snapshot": "snap-a"}, {"user": "someone", "role": "admin"})
        self.assertEqual("reviewed", args["review_token"])

    def test_an_app_another_job_holds_is_refused_before_anything_changes(self):
        K.ksend("PATCH", A, {"metadata": {"annotations": {HOLD.HELD_BY: "node-power:other", HOLD.HELD_AS: "2"}}}, ctype=MERGE)
        before = len(self.kube.writes)
        with self.assertRaisesRegex(ValueError, "app-a is held by another job \\(other\\).*Nothing was changed"):
            REVERT.start(VOLUME, "snap-a", self.ops, self.review())
        self.assertEqual(before, len(self.kube.writes))


class OurOwnMarks(Base):
    """A mark is an annotation, so on a real Deployment our own write moves the
    generation (the fake models it). That must never read as someone's change."""

    def generation(self, path=A):
        return self.kube.object(path)["metadata"]["generation"]

    def test_a_deployments_generation_moves_on_an_annotation_and_a_statefulsets_does_not(self):
        before = self.generation()
        K.ksend("PATCH", A, {"metadata": {"annotations": {"example.com/note": "x"}}}, ctype=MERGE)
        self.assertEqual(before + 1, self.generation())
        path = self.kube.add(statefulset("db", 1))
        before = self.generation(path)
        K.ksend("PATCH", path, {"metadata": {"annotations": {"example.com/note": "x"}}}, ctype=MERGE)
        self.assertEqual(before, self.generation(path))

    def test_an_annotation_after_the_review_on_a_deployment_is_refused(self):
        token = self.review()
        K.ksend("PATCH", A, {"metadata": {"annotations": {"example.com/note": "x"}}}, ctype=MERGE)
        with self.assertRaisesRegex(ValueError, "app-a changed after this rollback was reviewed"):
            REVERT.start(VOLUME, "snap-a", self.ops, token)

    def test_the_stop_retried_after_a_lost_reply_is_not_its_own_change(self):
        item = self.begin()
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        REVERT.resolve(item)
        before = self.generation()
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertGreater(self.generation(), before)
        self.assertEqual((2, 1), (self.replicas(A), self.replicas(B)))

    def test_a_second_app_retried_after_the_first_was_marked_goes_on(self):
        item = self.begin()
        self.kube.fail(B, 500, method="PATCH")
        REVERT.resolve(item)
        self.assertEqual(0, self.replicas(A))
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertEqual((2, 1), (self.replicas(A), self.replicas(B)))

    def test_a_restart_after_the_start_reply_was_lost_is_not_its_own_change(self):
        item = self.begin()
        REVERT.resolve(item)
        REVERT.resolve(item)
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertNotIn("changed", message)
        self.assertEqual(2, self.replicas(A))

    def test_a_new_leader_rereading_the_saved_job_is_not_its_own_change(self):
        item = self.begin()
        self.kube.fail(A, Died(), method="PATCH", applied=True)
        kept = self.saved(item)
        with self.assertRaises(Died):
            REVERT.resolve(item)
        status, _, message = self.rounds(kept)
        self.assertEqual("succeeded", status, message)
        self.assertNotIn("changed", message)

    def test_an_end_replayed_after_the_mark_came_off_is_not_said_to_be_changed(self):
        item = self.begin()
        self.fits = False
        for _ in range(4):
            REVERT.resolve(item)
        kept = self.saved(item)
        self.clock.now += REVERT.WAIT["restarting"] + 1
        REVERT.resolve(item)
        status, _, message = self.rounds(kept)
        self.assertEqual("failed", status, message)
        self.assertIn("app-a was left stopped", message)
        self.assertNotIn("changed", message)

    def test_the_same_review_sent_again_after_the_stop_starts_no_second_job(self):
        token = self.review()
        REVERT.resolve({"id": JOB, "ref": REVERT.start(VOLUME, "snap-a", self.ops, token)})
        self.ops.start.reset_mock()
        with self.assertRaisesRegex(ValueError, "changed after this rollback was reviewed"):
            REVERT.start(VOLUME, "snap-a", self.ops, token)
        self.ops.start.assert_not_called()

    def test_a_cancel_after_a_stop_is_not_its_own_change(self):
        item = self.begin()
        REVERT.resolve(item)
        message = REVERT.cancel_run(item, {})
        self.assertNotIn("changed while", message)
        self.assertEqual((2, 1), (self.replicas(A), self.replicas(B)))
        self.assertNotIn("left", message)

    def test_a_person_removing_the_mark_in_the_instant_before_the_start_reads_again(self):
        """A remove of a mark already gone answers 422 on a real cluster: read again, not a failure."""
        item = self.begin()
        REVERT.resolve(item)
        REVERT.resolve(item)
        count = []

        def gone(method, path, body):
            if not count:
                count.append(1)
                self.kube._reactions.clear()
                K.ksend("PATCH", A, {"metadata": {"annotations": {HOLD.HELD_BY: None, HOLD.HELD_AS: None}}}, ctype=MERGE)
        self.kube.react("PATCH", A, gone)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status, message)
        self.assertEqual(0, self.replicas(A), "the fenced start lost")
        self.assertIn("reads it again", message)
        self.clock.now += REVERT.WAIT["restarting"] + 1
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status, message)
        self.assertIn("app-a no longer carries Homestead's mark", message)


class WhoHoldsIt(Base):
    """Power jobs mark with a bare job id: a person should read the job's name."""

    def hold(self, mark):
        K.ksend("PATCH", A, {"metadata": {"annotations": {HOLD.HELD_BY: mark, HOLD.HELD_AS: "2"}}}, ctype=MERGE)

    def jobs(self, *rows):
        patch = mock.patch.object(REVERT.OPS, "snapshot", return_value=list(rows))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_bare_id_names_the_job(self):
        self.jobs({"id": "feedc0de", "title": "Reboot node-a"})
        self.hold("feedc0de")
        with self.assertRaisesRegex(ValueError, "app-a is held by another job \\(Reboot node-a\\). Nothing was changed"):
            REVERT.start(VOLUME, "snap-a", self.ops, self.review())

    def test_a_kind_and_id_names_the_job(self):
        self.jobs({"id": "beef", "title": "Reboot node-b"})
        item = self.begin()
        self.hold("node-power:beef")
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertIn("app-a was left as it is: it is held by another job (Reboot node-b)", message)

    def test_a_job_it_cant_find_says_the_mark(self):
        self.jobs()
        self.hold("feedc0de")
        with self.assertRaisesRegex(ValueError, "held by another job \\(feedc0de\\)"):
            REVERT.start(VOLUME, "snap-a", self.ops, self.review())

    def test_a_job_list_it_cant_read_says_the_mark(self):
        self.hold("feedc0de")
        with mock.patch.object(REVERT.OPS, "snapshot", side_effect=OSError("no volume")), \
                self.assertRaisesRegex(ValueError, "held by another job \\(feedc0de\\)"):
            REVERT.start(VOLUME, "snap-a", self.ops, self.review())

    def test_a_stopped_app_another_job_took_is_left_stopped_and_names_it(self):
        self.jobs({"id": "feedc0de", "title": "Reboot node-a"})
        item = self.begin()
        REVERT.resolve(item)
        self.hold("feedc0de")
        message = REVERT.cancel_run(item, {})
        self.assertIn("app-a was left stopped: app-a is held by another job (Reboot node-a)", message)
        self.assertEqual("feedc0de", self.notes(A)[HOLD.HELD_BY])


class ChangedOnceTheJobStarted(Base):
    def test_a_scale_before_the_first_round_stops_nothing_and_says_so(self):
        item = self.begin()
        self.person_scales(A, 3)
        before = len(self.kube.writes)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(before, len(self.kube.writes), "neither app is written to")
        self.assertEqual((3, 1), (self.replicas(A), self.replicas(B)))
        self.assertIn("app-a was left as it is: it changed after this rollback was reviewed (3 now, 2 when it was reviewed)", message)
        self.assertNotIn("left stopped", message)
        self.assertNotIn("starting again", message, "nothing had stopped")
        self.assertIn("Nothing was changed", message)
        self.assertEqual([], self.calls)

    def test_the_second_app_changing_after_the_first_stopped_starts_the_first_again(self):
        item = self.begin()
        self.kube.fail(B, 500, method="PATCH")
        REVERT.resolve(item)                            # A stopped, B's write refused by the server
        self.assertEqual(0, self.replicas(A))
        self.person_scales(B, 5)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual((2, 5), (self.replicas(A), self.replicas(B)))
        self.assertIn("app-b was left as it is", message)
        self.assertNotIn(HOLD.HELD_BY, self.notes(A))

    def test_a_controller_writing_status_before_the_stop_does_not_refuse(self):
        """Controllers rewrite the object all the time: a stop fenced on what
        was read at the start must look again, not give up."""
        item = self.begin()
        self.controller_writes_status(A)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status, message)
        self.assertEqual(0, self.replicas(A))
        self.assertEqual("2", self.notes(A)[HOLD.HELD_AS])

    def test_a_status_write_between_the_look_and_the_stop_looks_again(self):
        item = self.begin()

        def beat_the_stop(method, path, answer):
            self.kube._reactions.clear()
            current = self.kube.object(A)
            current["metadata"]["resourceVersion"] = "9000"
            self.kube.add(current)
            return None
        self.kube.react("PATCH", A, beat_the_stop)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status, message)
        self.assertEqual(2, self.replicas(A), "the fenced write failed")
        self.assertEqual("stopping", item["ref"]["phase"])
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status, message)
        self.assertEqual(0, self.replicas(A))

    def test_the_stop_is_one_fenced_write_that_marks_the_app(self):
        item = self.begin()
        REVERT.resolve(item)
        sent = self.writes(A)
        self.assertEqual(1, len(sent))
        ops = sent[0][2]
        self.assertIn({"op": "test", "path": "/metadata/uid", "value": self.kube.object(A)["metadata"]["uid"]}, ops)
        self.assertTrue(any(op["path"] == "/metadata/resourceVersion" and op["op"] == "test" for op in ops))
        self.assertEqual(MARK, self.notes(A)[HOLD.HELD_BY])
        self.assertEqual("2", self.notes(A)[HOLD.HELD_AS])

    def test_an_app_replaced_before_the_stop_is_not_stopped(self):
        item = self.begin()
        self.kube.discard(A)
        self.kube.add(deployment("app-a", 2))
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(2, self.replicas(A))
        self.assertIn("app-a was left as it is: it was replaced", message)

    def test_a_hold_another_job_took_before_the_stop_is_refused(self):
        item = self.begin()
        K.ksend("PATCH", A, {"metadata": {"annotations": {HOLD.HELD_BY: "node-power:other", HOLD.HELD_AS: "2"}}}, ctype=MERGE)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual("node-power:other", self.notes(A)[HOLD.HELD_BY], "another job's mark is never taken away")
        self.assertIn("app-a was left as it is: it is held by another job (other)", message)


class Stopping(Base):
    def test_an_app_being_removed_is_not_stopped(self):
        item = self.begin()
        current = self.kube.object(A)
        current["metadata"]["deletionTimestamp"] = "2026-11-06T00:00:00Z"
        current["metadata"]["finalizers"] = ["example.com/hold"]
        self.kube.add(current)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(2, self.replicas(A))
        self.assertIn("app-a was left as it is: it is being removed", message)

    def test_an_object_with_no_generation_that_moved_is_refused_not_trusted(self):
        current = self.kube.object(A)
        current["metadata"].pop("generation")
        self.kube.add(current)
        item = self.begin()
        self.controller_writes_status(A)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(2, self.replicas(A))

    def test_a_statefulset_that_came_to_delete_its_claims_after_the_start_is_not_stopped(self):
        """The policy is in spec: the generation moves. (Blocked at review by its own PR.)"""
        path = f"/apis/apps/v1/namespaces/{NS}/statefulsets/db"
        self.kube.add(statefulset("db", 1))
        item = self.begin()
        K.ksend("PATCH", path, {"spec": {"persistentVolumeClaimRetentionPolicy": {"whenScaled": "Delete"}}}, ctype=MERGE)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.kube.object(path)["spec"]["replicas"])
        self.assertIn("db was left as it is: it changed after this rollback was reviewed", message)

    def test_that_policy_change_in_the_instant_before_the_stop_loses_to_the_fence(self):
        path = f"/apis/apps/v1/namespaces/{NS}/statefulsets/db"
        self.kube.add(statefulset("db", 1))
        item = self.begin()

        def beat_it(method, where, body):
            self.kube._reactions.clear()
            K.ksend("PATCH", path, {"spec": {"persistentVolumeClaimRetentionPolicy": {"whenScaled": "Delete"}}}, ctype=MERGE)
        self.kube.react("PATCH", path, beat_it)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(1, self.kube.object(path)["spec"]["replicas"])


class LostReplies(Base):
    def test_a_stop_that_landed_with_its_reply_lost_is_not_sent_twice(self):
        item = self.begin()
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status, message)
        self.assertEqual(0, self.replicas(A), "the write stands")
        self.assertIn("app-a could not be stopped", message)
        REVERT.resolve(item)
        self.assertEqual(1, len(self.writes(A)))
        self.assertEqual("2", self.notes(A)[HOLD.HELD_AS], "how it ran is not overwritten by its own stop")

    def test_a_lost_stop_followed_by_a_give_up_still_starts_the_app_again(self):
        """On the old code the app stayed at 0: its stop was never flagged."""
        item = self.begin()
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        self.kube.fail(B, 500, times=1000, method="PATCH")
        REVERT.resolve(item)
        self.late()
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(2, self.replicas(A))
        self.assertNotIn(HOLD.HELD_BY, self.notes(A))

    def test_a_lost_stop_that_did_not_land_then_a_scale_is_left_alone(self):
        item = self.begin()
        self.kube.fail(A, OSError("timed out"), method="PATCH")
        REVERT.resolve(item)
        self.person_scales(A, 5)
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(5, self.replicas(A))
        self.assertIn("app-a was left as it is", message)

    def test_a_stop_that_landed_is_never_reported_as_left_alone(self):
        """The class: a "nothing was stopped" message a crash can falsify."""
        item = self.begin()
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        REVERT.resolve(item)
        self.person_scales(A, 5)                        # a person starts it by hand, mark and all
        status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertEqual(5, self.replicas(A), "not written over")
        self.assertNotIn("app-a was left as it is", message)
        self.assertIn("app-a was changed while it was stopped", message)
        self.assertNotIn(HOLD.HELD_BY, self.notes(A), "the job has ended: its mark goes")

    def test_an_app_scaled_after_its_stop_then_a_cancel_is_not_said_to_be_stopped(self):
        item = self.begin()
        self.kube.fail(B, 500, times=1000, method="PATCH")
        REVERT.resolve(item)
        self.person_scales(A, 5)
        message = REVERT.cancel_run(item, {})
        self.assertEqual(5, self.replicas(A))
        self.assertIn("app-a was changed while it was stopped, and was left as it is now", message)
        self.assertNotIn("app-a was left stopped", message)
        self.assertNotIn(HOLD.HELD_BY, self.notes(A))

    def test_a_start_that_landed_with_its_reply_lost_is_not_sent_twice(self):
        item = self.begin()
        REVERT.resolve(item)
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertEqual(2, self.replicas(A))
        self.assertEqual(2, len(self.writes(A)), "one stop, one start")
        self.assertNotIn("left stopped", message)

    def test_a_lost_start_then_a_scale_is_not_started_over_it(self):
        item = self.begin()
        REVERT.resolve(item)                            # stops, attaches
        REVERT.resolve(item)                            # reverts, lets go
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        REVERT.resolve(item)                            # starts: the reply is lost
        self.assertEqual(2, self.replicas(A))
        self.person_scales(A, 7)
        self.rounds(item)
        self.assertEqual(7, self.replicas(A))

    def test_a_refused_start_keeps_the_mark_until_the_job_ends_and_then_it_goes(self):
        item = self.begin()
        self.fits = False
        REVERT.resolve(item)
        REVERT.resolve(item)
        REVERT.resolve(item)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status, message)
        self.assertEqual(MARK, self.notes(A)[HOLD.HELD_BY], "the job is still trying")
        self.clock.now += REVERT.WAIT["restarting"] + 1
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status, message)
        self.assertIn("Containers → app-a → Start with 2 replicas", message)
        self.assertEqual(0, self.replicas(A))
        self.assertNotIn(HOLD.HELD_BY, self.notes(A))
        self.assertNotIn(HOLD.HELD_AS, self.notes(A))

    def test_a_mark_that_cannot_be_taken_off_says_so(self):
        item = self.begin()
        self.fits = False
        REVERT.resolve(item)
        REVERT.resolve(item)
        REVERT.resolve(item)
        REVERT.resolve(item)
        self.clock.now += REVERT.WAIT["restarting"] + 1
        self.kube.fail(A, OSError("timed out"), times=5, method="PATCH")
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status, message)
        self.assertIn(f"app-a may still carry {HOLD.HELD_BY}", message)
        self.assertEqual(MARK, self.notes(A)[HOLD.HELD_BY])

    def test_an_app_left_stopped_by_an_ended_job_can_be_rolled_back_again(self):
        """A mark outliving its job would refuse every later hold."""
        item = self.begin()
        self.fits = False
        for _ in range(4):
            REVERT.resolve(item)
        self.clock.now += REVERT.WAIT["restarting"] + 1
        self.assertEqual("failed", REVERT.resolve(item)[0])
        self.fits = True
        second = self.begin()
        rows = {c["name"]: c for c in second["ref"]["consumers"]}
        self.assertEqual(0, rows["app-a"]["replicas"], "stopped now, and recorded so")


class Interrupted(Base):
    def test_a_new_leader_after_a_stop_that_landed_finishes_the_stop_without_a_second_write(self):
        item = self.begin()
        self.kube.fail(A, Died(), method="PATCH", applied=True)
        kept = self.saved(item)
        with self.assertRaises(Died):
            REVERT.resolve(item)
        status, _, message = REVERT.resolve(kept)
        self.assertEqual("running", status, message)
        self.assertEqual(1, len(self.writes(A)))
        self.assertEqual((0, 0), (self.replicas(A), self.replicas(B)))
        self.assertEqual("succeeded", self.rounds(kept)[0])
        self.assertEqual((2, 1), (self.replicas(A), self.replicas(B)))

    def test_interrupted_it_starts_exactly_what_it_stopped_and_nothing_it_did_not(self):
        """Cut off with A stopped and B not reached; then cancelled. Between the
        two a person stopped B: it is not this job's to start."""
        item = self.begin()
        self.kube.fail(B, Died(), method="PATCH")
        kept = self.saved(item)
        with self.assertRaises(Died):
            REVERT.resolve(item)
        self.assertEqual(0, self.replicas(A))
        self.person_scales(B, 0)
        before = len(self.writes(B))
        REVERT.cancel_run(kept, {})
        self.assertEqual(2, self.replicas(A), "A was stopped by this job")
        self.assertEqual(0, self.replicas(B), "B was not")
        self.assertEqual(before, len(self.writes(B)))
        self.assertNotIn(HOLD.HELD_BY, self.notes(B))

    def test_a_cancel_says_what_it_left_alone(self):
        item = self.begin()
        self.kube.fail(B, Died(), method="PATCH")
        with self.assertRaises(Died):
            REVERT.resolve(item)
        self.person_scales(B, 4)
        message = REVERT.cancel_run(item, {})
        self.assertIn("app-b was left as it is", message)
        self.assertNotIn("app-b was left stopped", message)
        self.assertEqual(4, self.replicas(B))

    def test_a_cancel_cut_off_after_the_start_landed_runs_again_without_a_second_start(self):
        item = self.begin()
        self.kube.fail(B, 500, times=1000, method="PATCH")
        REVERT.resolve(item)
        self.kube.fail(A, Died(), method="PATCH", applied=True)
        kept = self.saved(item)
        with self.assertRaises(Died):
            REVERT.cancel_run(item, {})
        first = len(self.writes(A))
        message = REVERT.cancel_run(kept, {})
        self.assertEqual(2, self.replicas(A))
        self.assertEqual(first, len(self.writes(A)), "no second write")
        self.assertNotIn("app-a was left stopped", message)

    def test_a_cancel_cut_off_and_the_app_replaced_leaves_the_new_object_alone(self):
        item = self.begin()
        self.kube.fail(B, 500, times=1000, method="PATCH")
        REVERT.resolve(item)
        self.kube.fail(A, Died(), method="PATCH")
        kept = self.saved(item)
        with self.assertRaises(Died):
            REVERT.cancel_run(item, {})
        self.kube.discard(A)
        fresh = deployment("app-a", 0, annotations={HOLD.HELD_BY: MARK, HOLD.HELD_AS: "2"})
        self.kube.add(fresh)
        message = REVERT.cancel_run(kept, {})
        self.assertEqual(0, self.replicas(A))
        self.assertIn("app-a was replaced", message)
        self.assertEqual(MARK, self.notes(A)[HOLD.HELD_BY], "the mark on another object is not this job's to remove")

    def test_nothing_rolled_back_is_not_claimed_when_the_kept_snapshot_may_exist(self):
        """A snapshot request whose reply was lost: a person is told its name."""
        item = self.begin()
        REVERT.resolve(item)                            # stops, attaches

        def lost(method, path, body=None):
            if path.endswith("action=snapshotCreate"):
                raise OSError("timed out")
            return self.lh_call(method, path, body)
        REVERT.lh_call = lost
        with mock.patch.object(REVERT, "_snapshot_made", return_value=False):
            status, _, _ = REVERT.resolve(item)         # a try that may have gone out
        self.assertEqual("running", status)
        REVERT.lh_call = self.lh_call
        message = REVERT.cancel_run(item, {})
        self.assertNotIn("Nothing was changed", message)
        self.assertIn(item["ref"]["keep"], message)
        self.assertIn("may have been made", message)

    def keep_fails(self, *errors):
        """snapshotCreate answers with each of these in turn."""
        errors = list(errors)

        def call(method, path, body=None):
            if path.endswith("action=snapshotCreate"):
                self.tries.append(json.loads(json.dumps(self.stored)))
                raise errors.pop(0)
            return self.lh_call(method, path, body)
        self.tries = []
        REVERT.lh_call = call

    def to_the_snapshot(self):
        """Stopped and attached, with what the store holds at each checkpoint."""
        item = self.begin()
        self.stored = self.saved(item)

        def keep(saved):
            self.stored = json.loads(json.dumps(saved))
        patch = mock.patch.object(REVERT.OPS, "checkpoint", keep)
        patch.start()
        self.addCleanup(patch.stop)
        REVERT.resolve(item)
        return item

    def test_a_try_that_may_have_gone_out_then_a_refused_one_still_says_it_may_exist(self):
        item = self.to_the_snapshot()
        self.keep_fails(OSError("timed out"), REVERT.Refused("Longhorn refused it: busy"))
        with mock.patch.object(REVERT, "_snapshot_made", return_value=False):
            self.assertEqual("running", REVERT.resolve(item)[0])
            status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status, message)
        self.assertIn("may have been made", message)
        self.assertNotIn("not changed", message)

    def test_only_tries_all_refused_say_the_volume_was_not_changed(self):
        refused = REVERT.urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        item = self.to_the_snapshot()
        self.keep_fails(refused, refused, refused)
        rounds = [REVERT.resolve(item) for _ in range(3)]
        self.assertEqual("failed", rounds[-1][0], rounds[-1][2])
        self.assertIn("The volume was not changed", rounds[-1][2])
        self.assertNotIn("may have been made", rounds[-1][2])

    def test_each_try_is_saved_before_it_goes_so_a_crash_during_a_retry_still_says_it_may_exist(self):
        """A refused try, then a retry the pod dies in: the store must already
        show that retry, or the next leader would say nothing went out."""
        item = self.to_the_snapshot()
        self.keep_fails(REVERT.urllib.error.URLError(ConnectionRefusedError(111, "refused")), Died())
        REVERT.resolve(item)
        self.stored = self.saved(item)                  # the round's end is saved
        with self.assertRaises(Died):
            REVERT.resolve(item)
        REVERT.lh_call = self.lh_call
        message = REVERT.cancel_run(self.stored, {})
        self.assertIn("may have been made", message)
        self.assertNotIn("not changed", message)

    def test_a_crash_during_the_first_try_says_it_may_exist(self):
        item = self.to_the_snapshot()
        self.keep_fails(Died())
        with self.assertRaises(Died):
            REVERT.resolve(item)
        REVERT.lh_call = self.lh_call
        self.assertIn("may have been made", REVERT.cancel_run(self.stored, {}))

    def test_a_crash_just_after_the_mark_came_off_still_says_the_app_was_left_stopped(self):
        """The end's save is lost: the next leader runs the round again and
        finds the app at 0 with no mark. It is not "left as it is" and the job
        did not succeed."""
        item = self.begin()
        self.fits = False
        for _ in range(4):
            REVERT.resolve(item)
        kept = self.saved(item)
        self.clock.now += REVERT.WAIT["restarting"] + 1
        self.assertEqual("failed", REVERT.resolve(item)[0])     # mark off; pretend this end was never saved
        self.assertNotIn(HOLD.HELD_BY, self.notes(A))
        self.fits = True
        status, _, message = self.rounds(kept)
        self.assertEqual("failed", status, message)
        self.assertEqual(0, self.replicas(A), "with no mark it is not this job's to start")
        self.assertIn("app-a was left stopped", message)
        self.assertIn("Containers → app-a → Start with 2 replicas", message)
        self.assertNotIn("app-a was left as it is", message)

    def test_a_mark_removed_by_hand_while_stopped_is_said_to_be_left_stopped(self):
        item = self.begin()
        REVERT.resolve(item)
        K.ksend("PATCH", A, {"metadata": {"annotations": {HOLD.HELD_BY: None, HOLD.HELD_AS: None}}}, ctype=MERGE)
        message = REVERT.cancel_run(item, {})
        self.assertEqual(0, self.replicas(A))
        self.assertIn("app-a was left stopped", message)
        self.assertIn("Start with 2 replicas", message)

    def test_a_never_stopped_app_a_power_job_holds_is_not_told_to_start(self):
        item = self.begin()
        self.kube.fail(B, Died(), method="PATCH")
        with self.assertRaises(Died):
            REVERT.resolve(item)
        K.ksend("PATCH", B, {"metadata": {"annotations": {HOLD.HELD_BY: "feedc0de", HOLD.HELD_AS: "1"}}, "spec": {"replicas": 0}}, ctype=MERGE)
        message = REVERT.cancel_run(item, {})
        self.assertIn("app-b was left as it is: it is held by another job (feedc0de)", message)
        self.assertNotIn("Containers → app-b", message)
        self.assertEqual("feedc0de", self.notes(B)[HOLD.HELD_BY])

    def test_the_mark_warning_survives_the_500_characters_a_job_message_keeps(self):
        item = self.begin()
        self.fits = False
        for _ in range(4):
            REVERT.resolve(item)
        self.clock.now += REVERT.WAIT["restarting"] + 1
        self.kube.fail(A, OSError("timed out"), times=5, method="PATCH")
        with mock.patch.object(REVERT, "_how", lambda c: "x" * 400):
            _, _, message = REVERT.resolve(item)
        self.assertIn(f"may still carry {HOLD.HELD_BY}", message[:500])

    def test_a_failed_release_is_told_even_when_the_stop_was_never_flagged(self):
        """A stop whose reply was lost: not flagged, marked, and the mark can't come off."""
        item = self.begin()
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        REVERT.resolve(item)
        self.assertFalse(next(c for c in item["ref"]["consumers"] if c["name"] == "app-a").get("stopped"))
        self.kube.fail(A, OSError("timed out"), times=9, method="PATCH")
        message = REVERT.cancel_run(item, {})
        self.assertIn(f"app-a may still carry {HOLD.HELD_BY}", message)

    def test_a_failed_release_is_told_when_the_app_could_not_even_be_read(self):
        """A stop whose reply was lost, and then reads fail: nothing has flagged
        it as stopped, and the mark may be there."""
        item = self.begin()
        self.kube.fail(A, OSError("timed out"), method="PATCH", applied=True)
        REVERT.resolve(item)
        self.kube.fail(A, 500, times=100, method="GET")
        message = REVERT.cancel_run(item, {})
        self.assertIn(f"app-a may still carry {HOLD.HELD_BY}", message)

    def test_an_app_left_alone_and_never_stopped_is_not_warned_about_a_mark(self):
        item = self.begin()
        self.person_scales(A, 3)
        with mock.patch.object(HOLD, "release", return_value="HTTP 500"):
            status, _, message = self.rounds(item)
        self.assertEqual("failed", status, message)
        self.assertNotIn("may still carry", message)

    def test_a_never_stopped_app_replaced_before_a_cancel_is_not_said_to_be_stopped(self):
        item = self.begin()
        self.kube.fail(B, Died(), method="PATCH")
        with self.assertRaises(Died):
            REVERT.resolve(item)
        self.kube.discard(B)
        self.kube.add(deployment("app-b", 1))
        message = REVERT.cancel_run(item, {})
        self.assertIn("app-b was left as it is: it was replaced", message)
        self.assertNotIn("app-b was left stopped", message)

    def test_a_job_from_before_this_version_still_starts_what_it_stopped(self):
        """No record, only the flag: as the old code would."""
        ref = {"volume": VOLUME, "snapshot": "snap-a", "namespace": NS, "claim": CLAIM, "phase": "stopping",
               "since": self.clock.now,
               "consumers": [{"kind": "Deployment", "name": "app-a", "replicas": 2, "stopped": True,
                              "uid": self.kube.object(A)["metadata"]["uid"]},
                             {"kind": "Deployment", "name": "app-b", "replicas": 1,
                              "uid": self.kube.object(B)["metadata"]["uid"]}]}
        self.person_scales(A, 0)
        item = {"id": "old-job", "ref": ref}
        message = REVERT.cancel_run(item, {})
        self.assertEqual(2, self.replicas(A))
        self.assertEqual(1, self.replicas(B))
        self.assertIn("starting again", message)

    def test_a_job_from_before_this_version_whose_stop_landed_is_started_again(self):
        ref = {"volume": VOLUME, "snapshot": "snap-a", "namespace": NS, "claim": CLAIM, "phase": "stopping",
               "since": self.clock.now,
               "consumers": [{"kind": "Deployment", "name": "app-a", "replicas": 2,
                              "uid": self.kube.object(A)["metadata"]["uid"]},
                             {"kind": "Deployment", "name": "app-b", "replicas": 1,
                              "uid": self.kube.object(B)["metadata"]["uid"]}]}
        self.person_scales(A, 0)                        # its plain stop landed; the reply was lost
        item = {"id": "old-job", "ref": ref}
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertEqual(2, self.replicas(A))
        self.assertEqual(1, self.replicas(B))


class EveryKind(Base):
    apps = (lambda: (statefulset("db", 3), cronjob("nightly", False), cronjob("paused", True), vm("vm-1", "Always"),
                     deployment("idle", 0), vm("vm-off", "Halted")))

    def path(self, kind, name):
        return {"StatefulSet": f"/apis/apps/v1/namespaces/{NS}/statefulsets/{name}",
                "CronJob": f"/apis/batch/v1/namespaces/{NS}/cronjobs/{name}",
                "VirtualMachine": f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachines/{name}",
                "Deployment": f"{APPS}/{name}"}[kind]

    def test_each_kind_is_stopped_marked_and_started_as_it_ran(self):
        item = self.begin()
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status, message)
        spec = lambda kind, name: self.kube.object(self.path(kind, name))["spec"]
        self.assertEqual(0, spec("StatefulSet", "db")["replicas"])
        self.assertIs(True, spec("CronJob", "nightly")["suspend"])
        self.assertEqual("Halted", spec("VirtualMachine", "vm-1")["runStrategy"])
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertEqual(3, spec("StatefulSet", "db")["replicas"])
        self.assertIs(False, spec("CronJob", "nightly")["suspend"])
        self.assertEqual("Always", spec("VirtualMachine", "vm-1")["runStrategy"])
        for kind, name in (("StatefulSet", "db"), ("CronJob", "nightly"), ("VirtualMachine", "vm-1")):
            self.assertNotIn(HOLD.HELD_BY, self.kube.object(self.path(kind, name))["metadata"].get("annotations") or {})

    def test_what_was_stopped_before_the_job_is_never_written_to_or_started(self):
        item = self.begin()
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        for kind, name in (("CronJob", "paused"), ("Deployment", "idle"), ("VirtualMachine", "vm-off")):
            self.assertEqual([], self.writes(self.path(kind, name)), name)


class ManualVm(Base):
    apps = (lambda: (vm("vm-1", "Manual"),))

    def setUp(self):
        super().setUp()
        self.vm = f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachines/vm-1"
        self.instance = f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachineinstances/vm-1"
        self.start_path = f"/apis/subresources.kubevirt.io/v1/namespaces/{NS}/virtualmachines/vm-1/start"
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                       "metadata": {"name": "vm-1", "namespace": NS}, "status": {"phase": "Running"}})
        self.kube.react("PUT", self.start_path, self.started)
        self.starts = []

    def started(self, method, path, body):
        self.starts.append(path)
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                       "metadata": {"name": "vm-1", "namespace": NS}, "status": {"phase": "Running"}})
        return {}

    def test_an_unmarked_vm_whose_state_cant_be_read_waits_and_is_not_written_off(self):
        """restart() can't tell whether it runs: a read that fails can pass."""
        item = self.begin()
        self.kube.fail(self.vm, 500, times=1000, method="PATCH")
        REVERT.resolve(item)                            # its stop refused: not marked
        self.kube.fail(self.instance, 500, times=1, method="GET")
        message = REVERT.cancel_run(item, {})
        self.assertNotIn("vm-1 was left as it is", message)
        self.assertIn("could not tell whether vm-1 is running", message)

    def test_a_mark_gone_in_the_instant_before_its_removal_reads_again_and_finishes(self):
        """The start went out; the mark's removal answers 422 as it has gone: the VM runs, so it is done."""
        item = self.begin()
        REVERT.resolve(item)
        self.kube.discard(self.instance)
        REVERT.resolve(item)
        seen = []

        def gone(method, path, body):
            seen.append(1)
            if len(seen) == 2:                          # 1: back to Manual; 2: the marks come off
                self.kube._reactions.clear()
                self.kube.react("PUT", self.start_path, self.started)
                K.ksend("PATCH", self.vm, {"metadata": {"annotations": {HOLD.HELD_BY: None, HOLD.HELD_AS: None}}}, ctype=MERGE)
        self.kube.react("PATCH", self.vm, gone)
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertEqual(1, len(self.starts))

    def test_a_manual_vm_whose_start_failed_is_asked_again_not_taken_as_started(self):
        """Set back to Manual is not started: its mark stays until it is."""
        item = self.begin()
        REVERT.resolve(item)                            # stops, attaches
        self.kube.discard(self.instance)
        REVERT.resolve(item)                            # reverts, lets go
        self.kube.fail(self.start_path, 500, method="PUT")
        REVERT.resolve(item)                            # set back to Manual; the start fails
        self.assertEqual([], self.starts)
        self.assertEqual("Manual", self.kube.object(self.vm)["spec"]["runStrategy"])
        status, _, message = self.rounds(item)
        self.assertEqual("succeeded", status, message)
        self.assertEqual(1, len(self.starts))
        self.assertNotIn(HOLD.HELD_BY, self.notes(self.vm))


if __name__ == "__main__":
    unittest.main()
