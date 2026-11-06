"""A storage-class move started before the journalled engine still runs on its
old steps. It ends, or says it has stopped: its waits had no deadline and raise
nothing, and a refusal that can never clear kept it on "Needs attention".
On the shared fake API and the real job list."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_cancel as CANCEL
import homestead_kube as K
import homestead_operations as OPS
import homestead_reclass as RC
import homestead_storage_conflicts as CONFLICTS
import homestead_storage_guard as GUARD
import homestead_storage_server as STORAGE_ROUTES
from fakes.kube import Kube

CLAIM = "/api/v1/namespaces/lab/persistentvolumeclaims/data"
TEMP = "/api/v1/namespaces/lab/persistentvolumeclaims/data-reclass"
APP = "/apis/apps/v1/namespaces/lab/deployments/app"
JOB = "/apis/batch/v1/namespaces/lab/jobs/copy"
VM = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/vm1"
LIMIT = 10 * 60
RECHECK = 60
REVIEWED = [{"kind": "Deployment", "name": "app", "replicas": 2}]


class Crash(BaseException):
    """The server going away: nothing in the jobs loop catches this."""


def claim(name, volume, finalizers=()):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"namespace": "lab", "name": name, "finalizers": list(finalizers)},
            "spec": {"volumeName": volume, "storageClassName": "fast"}, "status": {"phase": "Bound"}}


def pod(name):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"namespace": "lab", "name": name},
            "spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "data"}}]},
            "status": {"phase": "Running"}}


def volume(name):
    return {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": name},
            "spec": {"persistentVolumeReclaimPolicy": "Delete"}}


class LegacyMoveTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(
            claim("data", "old-pv"), volume("old-pv"), volume("new-pv"),
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "app"},
             "spec": {"replicas": 0}, "status": {}}))
        self.enterContext(mock.patch.multiple(RC, kget=K.kget, ksend=K.ksend, ktext=lambda path: "",
                                              storage_classes=lambda: []))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"reclass": STORAGE_ROUTES.storage_move_progress}))
        self.enterContext(mock.patch.dict(OPS.RESUMABLE, {"reclass": RC.resumable}))
        self.now = 5000.0
        self.enterContext(mock.patch.object(RC, "_clock", lambda: self.now, create=True))

    def start(self, phase, **ref):
        return OPS.start("reclass", "Move data to fast", {"kind": "PersistentVolumeClaim", "name": "data", "namespace": "lab"},
                         "/volumes", {"namespace": "lab", "claim": "data", "target": "fast", "phase": phase,
                                      "consumers": [{"kind": "Deployment", "name": "app", "replicas": 2, "stopped": True}],
                                      "old_pv": "old-pv", "temp": "data-reclass", "job": "copy", "size": "1Gi",
                                      "claim_spec": {"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem"}, **ref})["id"]

    def look(self, ident, times=1):
        for _ in range(times):
            job = next(job for job in OPS.list_operations() if job["id"] == ident)
        return job

    def replicas(self):
        return self.kube.object(APP)["spec"]["replicas"]

    def test_a_pod_that_never_lets_go_puts_everything_back(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.assertIn("to let go of data", self.look(ident)["message"])
        self.now += LIMIT - 1
        self.assertEqual("running", self.look(ident)["status"])
        self.now += 1
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("10 minutes", job["message"])
        self.assertEqual(2, self.replicas(), "the app is started again on its original")
        self.assertFalse(job["resumable"], "it is over: nothing is left to carry on")
        OPS.dismiss(ident)

    def test_a_pod_that_lets_go_in_time_carries_on(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT - 1
        self.kube.discard("/api/v1/namespaces/lab/pods/app-1")
        self.assertEqual("data is free", self.look(ident)["message"])

    def test_a_failed_pod_read_is_not_a_pod_that_never_lets_go(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        self.kube.fail("/api/v1/namespaces/lab/pods", 500, times=9)
        self.assertEqual("running", self.look(ident)["status"])
        self.assertEqual(0, self.replicas())

    def test_a_new_volume_that_never_binds_puts_everything_back(self):
        ident = self.start("swap")
        self.assertEqual("Waiting for the new volume to be bound", self.look(ident)["message"])
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("10 minutes", job["message"])
        self.assertEqual(2, self.replicas())
        self.assertEqual("old-pv", self.kube.object(CLAIM)["spec"]["volumeName"], "nothing was swapped")

    def test_an_original_that_is_never_released_stops_and_carries_on_when_asked(self):
        self.kube.discard(CLAIM)
        self.kube.add(claim("data", "old-pv", finalizers=("example.com/hold",)))
        self.kube.add(claim("data-reclass", "new-pv"))
        ident = self.start("swap")
        self.assertIn("data", self.look(ident, 3)["message"])
        self.assertTrue(self.kube.object(CLAIM)["metadata"].get("deletionTimestamp"), "it is being deleted, and held")
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("Carry on", job["message"])
        self.assertIn("old copy", job["message"])
        self.assertTrue(job["resumable"])
        self.assertEqual(0, self.replicas(), "half-way through a swap nothing is started on either volume")
        OPS.resume(ident)
        self.assertEqual("running", self.look(ident)["status"], "it is not stopped again at once")

    def running(self):
        self.kube("PATCH", APP, {"spec": {"replicas": 2}}, ctype="application/merge-patch+json")

    def test_a_review_that_no_longer_holds_ends_with_nothing_stopped(self):
        self.running()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        with mock.patch.object(RC, "plan", return_value={"ok": False, "_fences": {}}):
            job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("Every app is as reviewed", job["message"])
        self.assertEqual(2, self.replicas(), "nothing was touched")
        self.assertFalse(job["resumable"])
        OPS.dismiss(ident)

    def test_a_review_that_cannot_be_read_gets_its_time_then_ends(self):
        self.running()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        with mock.patch.object(RC, "plan", side_effect=ValueError("Volume inventory is incomplete")):
            self.assertEqual("running", self.look(ident)["status"], "this can pass")
            self.now += LIMIT
            job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("Every app is as reviewed", job["message"])
        OPS.dismiss(ident)

    def lost_review(self, ident):
        with mock.patch.object(RC, "plan", return_value={"ok": False, "_fences": {}}):
            return self.look(ident)

    def raw(self, ident):
        return next(item for item in OPS._read() if item["id"] == ident)

    def test_an_app_that_looks_stopped_keeps_the_job_for_a_person(self):
        # A restart after the first stop write can lose the note that it was tried,
        # so a changed review is no proof that nothing stopped.
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        job = self.lost_review(ident)
        self.assertEqual("running", job["status"], "nothing holds that app if this job ends")
        self.assertIn("app looks stopped", job["message"])
        self.assertIn("This move waits for you. Nothing was started or retried.", job["message"])
        self.assertIn("Cancel", job["message"])
        self.assertNotIn("Every app is as reviewed", job["message"])
        self.assertEqual(0, self.replicas(), "and nothing was started by itself")

    def test_and_cancel_starts_that_app_again(self):
        ident = self.start("stop", review_fences={"x": "y"})
        self.lost_review(ident)
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {}))
        CANCEL.register(OPS)
        OPS.cancel(ident)
        self.assertEqual(2, self.replicas())

    def test_an_app_that_cannot_be_read_keeps_the_job_too(self):
        self.running()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        self.kube.fail(APP, 500, times=99, method="GET")
        job = self.lost_review(ident)
        self.assertEqual("running", job["status"])
        self.assertIn("could not be checked", job["message"])
        self.assertIn("waiting to check", job["message"])

    def test_a_held_copy_mark_counts_as_stopped(self):
        self.running()
        app = self.kube.object(APP)
        app["metadata"]["annotations"] = {"homestead.io/storage-copy-job": "copy-1"}
        self.kube.add(app)
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        job = self.lost_review(ident)
        self.assertEqual("running", job["status"])
        self.assertIn("copy-1", job["message"], "a job no longer on the list is named by its id")

    def test_the_attempt_is_saved_before_anything_is_stopped(self):
        self.running()
        fences = {"x": "y"}
        ident = self.start("stop", review_fences=fences, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        seen = []
        self.kube.react("PATCH", APP, lambda *a: seen.append(self.raw(ident)["ref"].get("stop_attempted")) and None)
        with mock.patch.object(RC, "plan", return_value={"ok": True, "_fences": fences}):
            self.look(ident)
        self.assertEqual([True], seen, "a restart here must not forget that it tried")

    def test_nothing_is_stopped_if_the_attempt_cannot_be_saved(self):
        self.running()
        fences = {"x": "y"}
        ident = self.start("stop", review_fences=fences, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        with mock.patch.object(RC, "plan", return_value={"ok": True, "_fences": fences}), \
                mock.patch.object(RC.OPS, "checkpoint", side_effect=ValueError("store is full")):
            self.look(ident)
        self.assertEqual(2, self.replicas())

    def foreign_temp(self):
        self.kube.add(claim("data-reclass", "someone-elses-pv"))

    def test_rolling_back_before_making_anything_deletes_nothing(self):
        self.foreign_temp()
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertTrue(self.kube.object(TEMP), "this move never made it")
        self.assertEqual([], [w for w in self.kube.writes if w[0] == "DELETE"])
        self.assertNotIn("new volume was removed", job["message"])

    def test_rolling_back_later_leaves_a_claim_this_move_did_not_make(self):
        self.foreign_temp()
        ident = self.start("swap", job="", temp_made=False)
        self.kube.discard(TEMP)
        self.kube.add({**claim("data-reclass", ""), "spec": {}})    # not bound, and no label of ours
        self.look(ident)
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertTrue(self.kube.object(TEMP))
        self.assertIn("did not make", job["message"])

    def test_rolling_back_removes_the_claim_it_made(self):
        made = {**claim("data-reclass", ""), "spec": {}}
        made["metadata"]["labels"] = RC.NAMES.labels("reclass", "data")
        self.kube.add(made)
        ident = self.start("swap", job="", temp_uid=self.kube.object(TEMP)["metadata"]["uid"])
        self.look(ident)
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIsNone(self.kube.object(TEMP))
        self.assertIn("new volume was removed", job["message"])

    def test_a_same_named_claim_with_another_uid_is_left(self):
        made = {**claim("data-reclass", ""), "spec": {}}
        made["metadata"]["labels"] = RC.NAMES.labels("reclass", "data")
        self.kube.add(made)
        ident = self.start("swap", job="", temp_uid="the-one-we-made")
        self.look(ident)
        self.now += LIMIT
        self.look(ident)
        self.assertTrue(self.kube.object(TEMP))

    def create(self):
        ident = self.start("create")
        with mock.patch.object(RC, "job_body", return_value=("copy", {"apiVersion": "batch/v1", "kind": "Job", "metadata": {
                "name": "copy", "namespace": "lab"}})):
            self.look(ident)
        return self.raw(ident)["ref"]

    def test_the_claim_made_here_has_its_uid_kept(self):
        ref = self.create()
        self.assertEqual(self.kube.object(TEMP)["metadata"]["uid"], ref["temp_uid"])

    def test_but_not_one_it_lost_a_race_for(self):
        self.foreign_temp()
        self.assertNotIn("temp_uid", self.create())

    def test_a_rollback_that_cannot_finish_can_only_carry_on_rolling_back(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        self.kube.fail(APP, 500, method="PATCH")
        self.look(ident)
        self.assertEqual(0, self.replicas(), "the start failed")
        self.kube.discard("/api/v1/namespaces/lab/pods/app-1")   # the pod goes: it must not carry on to the copy
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertEqual(2, self.replicas())
        self.assertEqual([], [w for w in self.kube.writes if w[0] == "POST"], "no new volume, no copy")

    def test_a_stop_that_was_interrupted_still_waits_for_a_person(self):
        # Its apps may be half stopped, and Cancel is what puts them back: a failed
        # job has no Cancel, so this one does not end by itself.
        ident = self.start("stop", review_fences={"x": "y"}, stop_attempted=True)
        self.now += LIMIT * 10
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertIn("interrupted", job["message"])


    # ---- what a person is left with afterwards ------------------------------

    def wired(self):
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {"reclass": (CANCEL.reclass_plan, STORAGE_ROUTES.storage_legacy_cancel)}))

    def assert_nothing_locked(self, ident):
        self.assertEqual([], GUARD.active_moves(OPS.stored()), "Homestead's own updates and shutdown wait on this")
        self.assertEqual([], GUARD.reasons(OPS.stored(), namespace="lab", claim="data"))
        OPS.dismiss(ident)

    def test_cancel_from_a_hold_leaves_nothing_locked(self):
        self.wired()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        self.lost_review(ident)
        OPS.cancel(ident)
        self.assertEqual(2, self.replicas())
        self.assert_nothing_locked(ident)

    def test_cancel_after_an_interrupted_stop_leaves_nothing_locked(self):
        self.wired()
        ident = self.start("stop", review_fences={"x": "y"}, stop_attempted=True, retain_resources=True)
        self.look(ident)
        OPS.cancel(ident)
        self.assertEqual(2, self.replicas())
        self.assert_nothing_locked(ident)

    def test_an_app_started_by_hand_ends_the_hold_and_locks_nothing(self):
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        self.lost_review(ident)
        self.running()
        self.now += RECHECK
        job = self.lost_review(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("Every app is as reviewed", job["message"])
        self.assertNotIn("othing was stopped", job["message"])
        self.assert_nothing_locked(ident)

    def test_a_review_that_holds_again_carries_on_with_no_hold_left(self):
        self.running()
        fences = {"x": "y"}
        ident = self.start("stop", review_fences=fences, consumers=REVIEWED)
        with mock.patch.object(RC, "plan", side_effect=ValueError("inventory")):
            self.look(ident)
            self.now += LIMIT
            self.kube.fail(APP, 500, method="GET")
            job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertIn("waiting to check", job["message"])
        self.assertNotIn("waits for you", job["message"])
        self.now += RECHECK
        with mock.patch.object(RC, "plan", return_value={"ok": True, "_fences": fences}):
            job = self.look(ident)
        self.assertEqual("Stopping app", job["message"])
        self.assertEqual(0, self.replicas())
        self.assertFalse(CONFLICTS.unresolved({**self.raw(ident), "status": "succeeded"}),
                         "a move that then succeeds must hold nothing")

    # ---- a rollback that cannot do everything --------------------------------

    def made_temp(self):
        made = {**claim("data-reclass", ""), "spec": {}}
        made["metadata"]["labels"] = RC.NAMES.labels("reclass", "data")
        self.kube.add(made)

    def test_a_refused_delete_still_starts_the_apps_and_says_so(self):
        self.made_temp()
        ident = self.start("swap", job="")
        self.look(ident)
        self.now += LIMIT
        self.kube.fail(TEMP, 403, times=99, method="DELETE")
        job = self.look(ident)
        self.assertEqual(2, self.replicas())
        self.assertEqual("failed", job["status"])
        self.assertIn("could not be removed", job["message"])
        self.assertNotIn("was removed", job["message"])
        self.assertTrue(self.kube.object(TEMP))

    def test_cancel_with_a_refused_delete_still_starts_the_apps(self):
        self.wired()
        self.made_temp()
        ident = self.start("copy", job="", copy_since=self.now)
        self.kube.fail(TEMP, 403, times=99, method="DELETE")
        out = OPS.cancel(ident)
        self.assertEqual(2, self.replicas())
        self.assertIn("could not be removed", out["detail"])

    def crash_after(self, method, fragment):
        real = K.ksend

        def send(verb, path, *args, **options):
            answer = real(verb, path, *args, **options)
            if verb == method and fragment in path:
                raise Crash(path)
            return answer
        return mock.patch.object(RC, "ksend", send)

    def crashes(self, ident, method, fragment):
        with self.crash_after(method, fragment), self.assertRaises(Crash):
            self.look(ident)

    def test_a_crash_after_starting_the_apps_cannot_go_on_to_the_copy_from_stopping(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        self.crashes(ident, "PATCH", "deployments/app")
        self.assertEqual(2, self.replicas())
        self.kube.discard("/api/v1/namespaces/lab/pods/app-1")
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertEqual([], [w for w in self.kube.writes if w[0] == "POST"], "no new volume, no copy")

    def test_a_crash_after_starting_the_apps_cannot_go_on_to_the_copy_from_create(self):
        ident = self.start("create", job="")
        self.kube.fail("/api/v1/namespaces/lab/persistentvolumeclaims", 500, method="POST")
        self.crashes(ident, "PATCH", "deployments/app")
        self.assertEqual(2, self.replicas())
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIsNone(self.kube.object("/apis/batch/v1/namespaces/lab/jobs/copy"), "no copy with the apps up")

    def test_a_rollback_that_cannot_be_saved_starts_nothing_yet(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        with mock.patch.object(RC.OPS, "checkpoint", side_effect=OSError("store is full")):
            self.look(ident)
        self.assertEqual(0, self.replicas(), "unsaved, a restart could take the started apps on to the copy")
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertEqual(2, self.replicas())

    def test_a_crash_mid_swap_keeps_the_new_volume_and_carries_on(self):
        bound = claim("data-reclass", "new-pv")
        self.kube.add(bound)
        ident = self.start("swap", job="")
        self.crashes(ident, "DELETE", "persistentvolumeclaims/data-reclass")
        job = self.look(ident)
        self.assertNotIn("to be bound", job["message"])
        self.assertIn("Letting go", job["message"])
        self.assertEqual(0, self.replicas())

    def test_a_save_that_failed_is_not_taken_for_an_interrupted_stop(self):
        self.running()
        fences = {"x": "y"}
        ident = self.start("stop", review_fences=fences, consumers=REVIEWED)
        with mock.patch.object(RC, "plan", return_value={"ok": True, "_fences": fences}):
            with mock.patch.object(RC.OPS, "checkpoint", side_effect=OSError("store is full")):
                self.look(ident)
            self.assertEqual(2, self.replicas())
            self.assertEqual("Stopping app", self.look(ident)["message"])
        self.assertEqual(0, self.replicas())

    # ---- an app another job holds --------------------------------------------

    def held_by_another_job(self):
        other = OPS.start("weekly-trim", "Copy app elsewhere", {"kind": "Deployment", "name": "app", "namespace": "lab"}, "/", {})["id"]
        app = self.kube.object(APP)
        app["metadata"]["annotations"] = {"homestead.io/storage-copy-job": other}
        self.kube.add(app)

    def test_the_hold_names_the_job_holding_the_app(self):
        self.held_by_another_job()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        message = self.lost_review(ident)["message"]
        self.assertIn("Copy app elsewhere", message)
        self.assertNotIn("earlier try", message)
        self.assertNotIn("starts app again", message)

    def test_cancel_leaves_an_app_another_job_holds_stopped_and_names_it(self):
        self.wired()
        self.held_by_another_job()
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "web"},
                       "spec": {"replicas": 0}, "status": {}})
        ident = self.start("stop", review_fences={"x": "y"},
                           consumers=REVIEWED + [{"kind": "Deployment", "name": "web", "replicas": 1}])
        self.lost_review(ident)
        out = OPS.cancel(ident)
        self.assertEqual(0, self.replicas())
        self.assertEqual(1, self.kube.object("/apis/apps/v1/namespaces/lab/deployments/web")["spec"]["replicas"])
        self.assertIn("Copy app elsewhere", out["detail"])
        self.assertIn("app (Copy app elsewhere) stays stopped", out["detail"])
        self.assertIn("Everything else started again", out["detail"])

    def test_the_cancel_review_says_it_leaves_that_app_stopped(self):
        self.wired()
        self.held_by_another_job()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        undo = " ".join(OPS.cancel_plan(ident)["undo"])
        self.assertNotIn("app at 2 replicas", undo)
        self.assertIn("Leaves app stopped", undo)
        self.assertIn("Copy app elsewhere", undo)

    def test_a_rollback_that_timed_out_leaves_that_app_stopped_too(self):
        self.held_by_another_job()
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertEqual(0, self.replicas())
        self.assertIn("Copy app elsewhere", job["message"])

    def test_an_app_that_cannot_be_read_is_not_started_by_a_rollback(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        self.kube.fail(APP, 500, method="GET")
        self.assertEqual("running", self.look(ident)["status"])
        self.assertEqual(0, self.replicas(), "it might be another job's")
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertEqual(2, self.replicas())

    # ---- what the words say ---------------------------------------------------

    def test_the_hold_stays_said_while_the_review_stays_unreadable(self):
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        with mock.patch.object(RC, "plan", side_effect=ValueError("inventory")):
            first = self.look(ident)["message"]
            self.assertIn("waiting to check", first)
            self.assertNotIn("othing was stopped", first)
            self.now += LIMIT
            for wait in (0, RECHECK, RECHECK):
                self.now += wait
                self.assertIn("app looks stopped", self.look(ident)["message"])

    def test_the_hold_reads_as_a_hold_not_as_a_lost_status(self):
        name = "an-app-with-a-long-name-" * 4 + "x"
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": name},
                       "spec": {"replicas": 0}, "status": {}})
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": name, "replicas": 2}])
        message = self.lost_review(ident)["message"]
        self.assertTrue(message.startswith("Volume use or identity changed"), message)
        self.assertTrue(message.endswith("on the original."), message)

    def test_a_held_review_is_asked_again_once_a_minute(self):
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        with mock.patch.object(RC, "plan", return_value={"ok": False, "_fences": {}}) as plan:
            said = self.look(ident)["message"]
            self.assertEqual(said, self.look(ident)["message"])
            self.assertEqual(1, plan.call_count)
            self.now += RECHECK
            self.look(ident)
            self.assertEqual(2, plan.call_count)

    def test_a_pod_that_never_lets_go_names_a_long_grace_period(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        self.assertIn("long shutdown grace period", self.look(ident)["message"])

    def test_cancel_from_stopping_says_nothing_of_a_new_volume(self):
        self.wired()
        ident = self.start("stopping")
        self.assertNotIn("new volume", " ".join(OPS.cancel_plan(ident)["undo"]))
        self.assertEqual("Cancelled. Nothing changed: everything started again on the original.", OPS.cancel(ident)["detail"])

    def test_cancel_names_a_claim_it_left(self):
        self.wired()
        self.foreign_temp()
        ident = self.start("copy", job="", copy_since=self.now)
        out = OPS.cancel(ident)
        self.assertTrue(self.kube.object(TEMP))
        self.assertIn("did not make", out["detail"])
        self.assertNotIn("was removed", out["detail"])

    def test_cancel_from_the_copy_says_it_removed_the_new_volume(self):
        self.wired()
        self.made_temp()
        ident = self.start("copy", job="", copy_since=self.now)
        self.assertIn("deletes the new volume", " ".join(OPS.cancel_plan(ident)["undo"]))
        self.assertIn("new volume was removed", OPS.cancel(ident)["detail"])
        self.assertIsNone(self.kube.object(TEMP))

    def test_the_cancel_review_says_the_count_it_starts_at(self):
        self.wired()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        self.assertIn("app at 2 replicas", " ".join(OPS.cancel_plan(ident)["undo"]))

    # ---- each kind of workload, seen stopped ----------------------------------

    def held_with(self, obj, consumer):
        self.kube.add(obj)
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[consumer])
        return ident, self.lost_review(ident)

    def vm(self, strategy):
        return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"namespace": "lab", "name": "vm1"},
                "spec": {"runStrategy": strategy, "template": {"spec": {"volumes": []}}}}

    def cron(self, suspend):
        return {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"namespace": "lab", "name": "nightly"},
                "spec": {"suspend": suspend, "schedule": "0 1 * * *"}}

    def stateful(self, replicas):
        return {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"namespace": "lab", "name": "db"},
                "spec": {"replicas": replicas}, "status": {}}

    def test_a_halted_vm_looks_stopped_and_cancel_runs_it_again(self):
        self.wired()
        ident, job = self.held_with(self.vm("Halted"), {"kind": "VirtualMachine", "name": "vm1", "run_strategy": "Always"})
        self.assertEqual("running", job["status"])
        self.assertIn("vm1", job["message"])
        OPS.cancel(ident)
        self.assertEqual("Always", self.kube.object("/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/vm1")["spec"]["runStrategy"])

    def test_a_vm_halted_at_the_review_is_as_reviewed(self):
        _, job = self.held_with(self.vm("Halted"), {"kind": "VirtualMachine", "name": "vm1", "run_strategy": "Halted"})
        self.assertEqual("failed", job["status"])

    def test_a_suspended_cronjob_looks_stopped_and_cancel_resumes_it(self):
        self.wired()
        ident, job = self.held_with(self.cron(True), {"kind": "CronJob", "name": "nightly", "suspend": False})
        self.assertEqual("running", job["status"])
        self.assertIn("nightly", job["message"])
        OPS.cancel(ident)
        self.assertFalse(self.kube.object("/apis/batch/v1/namespaces/lab/cronjobs/nightly")["spec"]["suspend"])

    def test_a_cronjob_suspended_at_the_review_is_as_reviewed(self):
        _, job = self.held_with(self.cron(True), {"kind": "CronJob", "name": "nightly", "suspend": True})
        self.assertEqual("failed", job["status"])

    def test_a_statefulset_at_zero_looks_stopped_and_cancel_starts_it(self):
        self.wired()
        ident, job = self.held_with(self.stateful(0), {"kind": "StatefulSet", "name": "db", "replicas": 3})
        self.assertEqual("running", job["status"])
        self.assertIn("db", job["message"])
        OPS.cancel(ident)
        self.assertEqual(3, self.kube.object("/apis/apps/v1/namespaces/lab/statefulsets/db")["spec"]["replicas"])

    def test_a_deployment_at_zero_at_the_review_is_as_reviewed(self):
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": "app", "replicas": 0}])
        self.assertEqual("failed", self.lost_review(ident)["status"])

    def test_a_saved_stopped_note_counts_as_stopped(self):
        self.running()
        ident = self.start("stop", review_fences={"x": "y"},
                           consumers=[{"kind": "Deployment", "name": "app", "replicas": 2, "stopped": True}])
        self.assertEqual("running", self.lost_review(ident)["status"])

    # ---- the delete racing with the real thing --------------------------------

    def swap_timed_out(self):
        ident = self.start("swap", job="")
        self.look(ident)
        self.now += LIMIT
        return ident

    def test_a_claim_replaced_just_before_the_delete_is_left(self):
        self.made_temp()
        mine = self.kube.object(TEMP)["metadata"]["uid"]

        def replaced(*_):
            self.kube.discard(TEMP)
            self.kube.add(claim("data-reclass", "someone-elses-pv"))

        self.kube.react("DELETE", TEMP, replaced)
        job = self.look(self.swap_timed_out())
        self.assertEqual("failed", job["status"])
        kept = self.kube.object(TEMP)
        self.assertNotEqual(mine, kept["metadata"]["uid"])
        self.assertEqual("someone-elses-pv", kept["spec"]["volumeName"])
        self.assertIn("did not make", job["message"])
        self.assertEqual(2, self.replicas())

    def test_a_claim_gone_just_before_the_delete_counts_as_removed(self):
        self.made_temp()
        self.kube.react("DELETE", TEMP, lambda *_: self.kube.discard(TEMP))
        job = self.look(self.swap_timed_out())
        self.assertIn("new volume was removed", job["message"])

    # ---- Cancel while it is putting things back ---------------------------------

    def test_cancel_while_rolling_back_starts_the_apps_and_keeps_the_reason(self):
        self.wired()
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        self.kube.fail(APP, 500, method="PATCH")
        self.look(ident)
        self.assertEqual(0, self.replicas())
        out = OPS.cancel(ident)
        self.assertEqual(2, self.replicas(), "a cancel here must put things back, not just stop tracking")
        self.assertIn("did not let go", out["detail"])
        self.assert_nothing_locked(ident)


    # ---- every way into a rollback saves first --------------------------------

    def rolled_back_saved_first(self, ident, *steps):
        seen = []
        self.kube.react("PATCH", APP, lambda *_: seen.append(self.raw(ident)["ref"]["phase"]) and None)
        for step in steps:
            step()
            self.look(ident)
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertEqual("rolling-back", seen[0], "a restart must find this, once the apps are up")

    def test_a_copy_job_gone_saves_the_rollback_before_starting_apps(self):
        self.rolled_back_saved_first(self.start("copy", copy_since=self.now))

    def test_a_volume_never_bound_saves_the_rollback_before_starting_apps(self):
        ident = self.start("swap", job="")
        self.look(ident)
        self.now += LIMIT
        self.rolled_back_saved_first(ident)

    def test_a_lasting_not_found_saves_the_rollback_before_starting_apps(self):
        ident = self.start("stopping")
        self.kube.fail("/api/v1/namespaces/lab/pods", 404, times=99)
        self.rolled_back_saved_first(ident, *[lambda: None] * RC.MISS_LIMIT)

    def test_a_refused_new_volume_saves_the_rollback_before_starting_apps(self):
        ident = self.start("create", job="")
        self.kube.fail("/api/v1/namespaces/lab/persistentvolumeclaims", 500, method="POST")
        self.rolled_back_saved_first(ident)

    def test_cancel_from_the_stop_step_touches_no_claim_of_that_name(self):
        self.wired()
        self.foreign_temp()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        self.lost_review(ident)
        detail = OPS.cancel(ident)["detail"]
        self.assertTrue(self.kube.object(TEMP))
        self.assertEqual([], [w for w in self.kube.writes if w[0] == "DELETE"])
        self.assertNotIn("new volume", detail)

    def test_a_swap_that_finishes_after_the_limit_is_not_failed(self):
        self.kube.discard(CLAIM)
        waiting = claim("data", "new-pv")
        waiting["status"]["phase"] = "Pending"
        self.kube.add(waiting)
        ident = self.start("swap", job="", new_pv="new-pv")
        self.assertIn("to bind", self.look(ident)["message"])
        self.kube.add(claim("data", "new-pv"))
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertIn("is on fast", job["message"])

    def test_the_cancel_review_does_not_start_an_app_reviewed_at_zero(self):
        self.wired()
        ident = self.start("stop", review_fences={"x": "y"},
                           consumers=[{"kind": "Deployment", "name": "app", "replicas": 0}])
        self.assertNotIn("app", " ".join(OPS.cancel_plan(ident)["undo"]))


    # ---- a Cancel that takes more than one try --------------------------------

    def test_a_cancel_whose_start_failed_once_finishes_and_locks_nothing(self):
        self.wired()
        ident = self.start("stop", review_fences={"x": "y"}, stop_attempted=True, retain_resources=True)
        self.look(ident)
        self.kube.fail(APP, 500, method="PATCH")
        with self.assertRaises(ValueError):
            OPS.cancel(ident)
        self.assertEqual(0, self.replicas())
        job = self.look(ident)
        self.assertEqual(2, self.replicas())
        self.assertEqual("cancelled", job["status"], "a person asked for Cancel")
        self.assertEqual("Cancelled. Nothing changed: everything started again on the original.", job["message"])
        self.assertFalse(self.raw(ident)["ref"].get("retain_resources"), "old copies and the helper sweep read it too")
        self.assert_nothing_locked(ident)

    def test_a_cancel_whose_mark_read_failed_once_finishes_and_locks_nothing(self):
        self.wired()
        ident = self.start("stop", review_fences={"x": "y"}, stop_attempted=True, retain_resources=True)
        self.look(ident)
        self.kube.fail(APP, 500, method="GET", skip=1)   # the first read is the cancel review's
        with self.assertRaises(ValueError):
            OPS.cancel(ident)
        job = self.look(ident)
        self.assertEqual("cancelled", job["status"])
        self.assert_nothing_locked(ident)

    def test_a_rollback_that_was_not_a_cancel_stays_a_failure(self):
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        self.kube.fail(APP, 500, method="PATCH")
        self.look(ident)
        self.assertEqual("failed", self.look(ident)["status"])

    # ---- the copy job ----------------------------------------------------------

    def copy_job(self):
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": "lab", "name": "copy"},
                       "spec": {}, "status": {}})

    def test_a_rollback_deletes_the_copy_job(self):
        self.made_temp()
        self.copy_job()
        ident = self.start("swap", job="copy")
        self.look(ident)
        self.now += LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIsNone(self.kube.object(JOB))

    def test_a_copy_job_that_cannot_be_deleted_is_named_and_the_apps_still_start(self):
        self.wired()
        self.made_temp()
        self.copy_job()
        ident = self.start("copy", copy_since=self.now)
        self.kube.fail(JOB, 403, times=99, method="DELETE")
        detail = OPS.cancel(ident)["detail"]
        self.assertEqual(2, self.replicas())
        self.assertIn("The copy job copy could not be stopped", detail)
        self.assertIn("may still be using the volumes", detail)
        self.assertNotIn("Nothing changed", detail)
        self.assertNotIn("was removed", detail)

    def test_a_copy_job_whose_post_lost_its_reply_is_deleted_too(self):
        ident = self.start("create", job="", job_name="copy", mode="Filesystem")
        self.kube.fail("/apis/batch/v1/namespaces/lab/jobs", 500, method="POST", applied=True)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIsNone(self.kube.object(JOB), "its reply was lost, not its write")

    def test_a_copy_job_already_gone_is_not_a_problem(self):
        self.made_temp()
        ident = self.start("swap", job="copy")
        self.look(ident)
        self.now += LIMIT
        job = self.look(ident)
        self.assertIn("Nothing changed", job["message"])
        self.assertNotIn("could not be stopped", job["message"])

    def test_a_job_delete_that_timed_out_is_tried_again_before_the_apps_start(self):
        self.made_temp()
        self.copy_job()
        ident = self.start("swap", job="copy")
        self.look(ident)
        self.now += LIMIT
        self.kube.fail(JOB, TimeoutError("timed out"), method="DELETE")
        self.assertEqual("running", self.look(ident)["status"])
        self.assertEqual(0, self.replicas())
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertIsNone(self.kube.object(JOB))

    # ---- installs an older Cancel already locked ---------------------------------

    def older_cancel(self, **ref):
        ident = self.start("rolled-back", retain_resources=True, **ref)
        items = OPS._read()
        for item in items:
            if item["id"] == ident:
                item.update(status="cancelled", finished_at="2026-01-01T00:00:00Z")
        OPS._write(items)
        return ident

    def test_a_move_an_older_cancel_locked_is_free_again(self):
        self.assert_nothing_locked(self.older_cancel())

    def test_a_journalled_move_with_retained_resources_stays_protected(self):
        ident = self.older_cancel(storage_protocol=1)
        self.assertEqual([ident], [item["id"] for item in GUARD.active_moves(OPS.stored())])
        with self.assertRaises(ValueError):
            OPS.dismiss(ident)

    def test_a_failed_move_with_retained_resources_stays_protected(self):
        ident = self.older_cancel()
        items = OPS._read()
        for item in items:
            if item["id"] == ident:
                item["status"] = "failed"
        OPS._write(items)
        self.assertEqual([ident], [item["id"] for item in GUARD.active_moves(OPS.stored())])
        with self.assertRaises(ValueError):
            OPS.dismiss(ident)

    def test_a_cancelled_move_not_yet_put_back_stays_protected(self):
        ident = self.older_cancel()
        items = OPS._read()
        for item in items:
            if item["id"] == ident:
                item["ref"]["phase"] = "swap"
        OPS._write(items)
        self.assertEqual([ident], [item["id"] for item in GUARD.active_moves(OPS.stored())])
        with self.assertRaises(ValueError):
            OPS.dismiss(ident)

    # ---- one app that can't be started ---------------------------------------------

    def app_and_vm(self):
        self.kube.add(self.vm("Halted"))
        return REVIEWED + [{"kind": "VirtualMachine", "name": "vm1", "run_strategy": "Always"}]

    def app_starts(self):
        return [w for w in self.kube.writes if w[0] == "PATCH" and w[1] == APP]

    def test_one_unreadable_app_does_not_keep_the_others_down(self):
        ident = self.start("rolling-back", rollback_from="stopping", rollback_why="It waited.", consumers=self.app_and_vm())
        self.kube.fail(VM, 403, method="GET")
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertEqual(2, self.replicas())
        self.assertIn("vm1", job["message"])
        self.assertTrue(job["message"].startswith("Putting things back: vm1 still down"), job["message"])
        self.assertEqual("Halted", self.kube.object(VM)["spec"]["runStrategy"])
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertEqual("Always", self.kube.object(VM)["spec"]["runStrategy"])

    def test_an_app_already_started_is_not_started_again_by_a_retry(self):
        ident = self.start("rolling-back", rollback_from="stopping", rollback_why="It waited.", consumers=self.app_and_vm())
        self.kube.fail(VM, 403, method="GET")
        self.look(ident)
        self.kube("PATCH", APP, {"spec": {"replicas": 5}}, ctype="application/merge-patch+json")   # someone changed it since
        self.look(ident)
        self.assertEqual(5, self.replicas())

    def test_one_app_refusing_its_start_does_not_keep_the_others_down(self):
        ident = self.start("rolling-back", rollback_from="stopping", rollback_why="It waited.", consumers=self.app_and_vm())
        self.kube.fail(VM, 403, method="PATCH")
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertEqual(2, self.replicas())
        self.assertIn("vm1", job["message"])
        self.assertEqual("failed", self.look(ident)["status"])

    def test_cancel_starts_what_it_can_and_names_the_app_still_down(self):
        self.wired()
        ident = self.start("stopping", consumers=self.app_and_vm())
        self.kube.fail(VM, 403, method="GET", skip=1)   # the first read is the cancel review's
        with self.assertRaisesRegex(ValueError, "vm1"):
            OPS.cancel(ident)
        self.assertEqual(2, self.replicas())
        job = self.look(ident)
        self.assertEqual("cancelled", job["status"])
        self.assertEqual(1, len(self.app_starts()), "the retry starts only what is still down")
        self.assertEqual("Always", self.kube.object(VM)["spec"]["runStrategy"])
        self.assert_nothing_locked(ident)

    # ---- the words, once more ---------------------------------------------------------

    def test_a_deleted_app_is_named_not_counted_as_reviewed(self):
        self.kube.discard(APP)
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        job = self.lost_review(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("app is gone", job["message"])
        self.assertNotIn("Every app", job["message"])

    def test_a_deleted_app_with_a_stopped_note_is_not_held_for(self):
        self.kube.discard(APP)
        ident = self.start("stop", review_fences={"x": "y"})
        self.assertEqual("failed", self.lost_review(ident)["status"])

    def old_style_vm(self):
        vm = self.vm("Halted")
        del vm["spec"]["runStrategy"]
        vm["spec"]["running"] = False
        return vm

    def test_a_vm_stopped_the_old_way_looks_stopped_and_cancel_runs_it(self):
        self.wired()
        ident, job = self.held_with(self.old_style_vm(), {"kind": "VirtualMachine", "name": "vm1", "run_strategy": "Always"})
        self.assertEqual("running", job["status"])
        self.assertIn("vm1", job["message"])
        OPS.cancel(ident)
        spec = self.kube.object(VM)["spec"]
        self.assertEqual("Always", spec["runStrategy"])
        self.assertNotIn("running", spec, "the API refuses a VM with both")

    def test_a_vm_stopped_the_old_way_at_the_review_is_as_reviewed(self):
        _, job = self.held_with(self.old_style_vm(), {"kind": "VirtualMachine", "name": "vm1", "run_strategy": "Halted"})
        self.assertEqual("failed", job["status"])

    def test_an_app_left_stopped_gets_its_count_and_no_nothing_changed(self):
        self.held_by_another_job()
        self.kube.add(pod("app-1"))
        ident = self.start("stopping")
        self.look(ident)
        self.now += LIMIT
        message = self.look(ident)["message"]
        self.assertIn("it was at 2 replicas", message)
        self.assertNotIn("Nothing changed", message)

    def test_the_cancel_review_gives_the_count_of_an_app_it_leaves(self):
        self.wired()
        self.held_by_another_job()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=REVIEWED)
        self.assertIn("it was at 2 replicas", " ".join(OPS.cancel_plan(ident)["undo"]))

    def test_the_cancel_review_does_not_start_what_was_off_at_the_review(self):
        self.wired()
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[
            {"kind": "CronJob", "name": "nightly", "suspend": True},
            {"kind": "VirtualMachine", "name": "vm1", "run_strategy": "Halted"},
            {"kind": "Deployment", "name": "app", "replicas": 2}])
        undo = " ".join(OPS.cancel_plan(ident)["undo"])
        self.assertIn("app at 2 replicas", undo)
        self.assertNotIn("nightly", undo)
        self.assertNotIn("vm1", undo)


if __name__ == "__main__":
    unittest.main()
