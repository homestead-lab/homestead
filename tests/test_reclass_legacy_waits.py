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
import homestead_storage_server as STORAGE_ROUTES
from fakes.kube import Kube

CLAIM = "/api/v1/namespaces/lab/persistentvolumeclaims/data"
TEMP = "/api/v1/namespaces/lab/persistentvolumeclaims/data-reclass"
APP = "/apis/apps/v1/namespaces/lab/deployments/app"
LIMIT = 10 * 60


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
        self.assertIn("Nothing was stopped", job["message"])
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
        self.assertIn("Nothing was stopped", job["message"])
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
        self.assertIn("Cancel", job["message"])
        self.assertNotIn("Nothing was stopped", job["message"])
        self.assertTrue(self.raw(ident)["ref"]["retain_resources"])
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

    def test_a_held_copy_mark_counts_as_stopped(self):
        self.running()
        app = self.kube.object(APP)
        app["metadata"]["annotations"] = {"homestead.io/storage-copy-job": "copy-1"}
        self.kube.add(app)
        ident = self.start("stop", review_fences={"x": "y"}, consumers=[{"kind": "Deployment", "name": "app", "replicas": 2}])
        self.assertEqual("running", self.lost_review(ident)["status"])

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


if __name__ == "__main__":
    unittest.main()
