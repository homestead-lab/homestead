"""The leader removes Homestead's finished helpers once their result has been
read: image clean-up, host power and cluster shutdown pods, pull watchers,
storage-copy Jobs, and imports finished long ago (homestead_helper_sweep)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_helper_sweep as SWEEP
from fakes.kube import Kube

NOW = 1_800_000_000
HOUR, DAY = 3600, 86400


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def pod(name, task, phase="Succeeded", finished=NOW - 30 * DAY, ns="lab", uid=None):
    status = {"phase": phase, "startTime": iso(finished - 5)}
    if phase in ("Succeeded", "Failed"):
        status["containerStatuses"] = [{"name": "c", "state": {"terminated": {
            "exitCode": 0 if phase == "Succeeded" else 1, "finishedAt": iso(finished)}}}]
    labels = {"homestead.io/task": task} if task else {}
    meta = {"name": name, "namespace": ns, "labels": labels, "creationTimestamp": iso(finished - 10)}
    if uid:
        meta["uid"] = uid
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": {"containers": [{"name": "c"}]},
            "status": status}


def job(name, task, ns="apps", state="Complete", finished=NOW - 30 * DAY, annotations=None):
    status = {}
    if state in ("Complete", "Failed"):
        status["conditions"] = [{"type": state, "status": "True", "lastTransitionTime": iso(finished)}]
        if state == "Complete":
            status["completionTime"] = iso(finished)
    else:
        status["active"] = 1
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": ns, "labels": {"homestead.io/task": task},
                         "annotations": dict(annotations or {}), "creationTimestamp": iso(finished - 60)},
            "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}, "status": status}


class Base(unittest.TestCase):
    ops = ()

    def setUp(self):
        # A delete goes round the real write guards, and the storage guard
        # takes the job store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(SWEEP.OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))

    def run_sweep(self, *objects, ops=None):
        self.kube = Kube(*objects)
        with self.kube, mock.patch.object(SWEEP.OPS, "stored", return_value=list(self.ops if ops is None else ops)):
            SWEEP.sweep(now=NOW)
        return self.kube

    def left(self, kind, ns, name):
        plural = "pods" if kind == "Pod" else "jobs"
        base = "/api/v1" if kind == "Pod" else "/apis/batch/v1"
        return self.kube.object(f"{base}/namespaces/{ns}/{plural}/{name}") is not None


class FinishedPodTests(Base):
    def test_a_finished_image_cleaner_goes_after_a_day_and_a_recent_one_stays(self):
        self.run_sweep(pod("homestead-image-clean-0123456789-a1b2c3", "image-cleanup", finished=NOW - 2 * DAY),
                       pod("homestead-image-clean-0123456789-a1b2c4", "image-cleanup", finished=NOW - HOUR),
                       pod("homestead-image-clean-9876543210-a1b2c4", "image-cleanup", phase="Running"))
        self.assertFalse(self.left("Pod", "lab", "homestead-image-clean-0123456789-a1b2c3"))
        self.assertTrue(self.left("Pod", "lab", "homestead-image-clean-0123456789-a1b2c4"), "its log is still read")
        self.assertTrue(self.left("Pod", "lab", "homestead-image-clean-9876543210-a1b2c4"), "never a running one")

    def test_a_pod_not_known_to_have_ended_is_kept_however_old(self):
        self.run_sweep(pod("homestead-image-clean-0123456789-a1b2c3", "image-cleanup", phase="Running"),
                       pod("homestead-image-clean-0123456789-a1b2c4", "image-cleanup", phase="Unknown"),
                       pod("homestead-image-clean-0123456789-a1b2c5", "image-cleanup", phase="Pending"))
        self.assertEqual([], [w for w in self.kube.writes if w[0] == "DELETE"])

    def test_at_most_fifty_go_in_one_round(self):
        many = [pod(f"homestead-image-clean-0123456789-{i:06x}", "image-cleanup") for i in range(60)]
        self.run_sweep(*many)
        self.assertEqual(50, len([w for w in self.kube.writes if w[0] == "DELETE"]))

    def test_the_delete_names_the_pod_it_read(self):
        self.run_sweep(pod("homestead-image-clean-0123456789-a1b2c3", "image-cleanup", uid="uid-cleaner"))
        deletes = [w for w in self.kube.writes if w[0] == "DELETE"]
        self.assertEqual(1, len(deletes))
        self.assertEqual({"uid": "uid-cleaner"}, deletes[0][2]["preconditions"])

    def test_host_power_helpers_stay_a_week_for_their_log_then_go(self):
        self.run_sweep(pod("homestead-reboot-node-a-12345", "node-power", phase="Failed", finished=NOW - 8 * DAY),
                       pod("homestead-poweroff-node-a-12346", "node-power", finished=NOW - 2 * DAY),
                       pod("homestead-handoff-reboot-12347", "node-power", finished=NOW - 8 * DAY))
        self.assertFalse(self.left("Pod", "lab", "homestead-reboot-node-a-12345"))
        self.assertTrue(self.left("Pod", "lab", "homestead-poweroff-node-a-12346"))
        self.assertFalse(self.left("Pod", "lab", "homestead-handoff-reboot-12347"))

    def test_a_helper_a_job_still_follows_is_kept_however_old(self):
        running = {"id": "op-1", "kind": "node-power", "status": "running",
                   "ref": {"helper_pod": "homestead-reboot-node-a-12345", "helper_namespace": "lab"}}
        self.run_sweep(pod("homestead-reboot-node-a-12345", "node-power", finished=NOW - 30 * DAY), ops=[running])
        self.assertTrue(self.left("Pod", "lab", "homestead-reboot-node-a-12345"))

    def test_shutdown_helpers_stay_while_their_run_is_followed(self):
        run = "0123456789abcdef"
        names = [f"homestead-shutdown-{run}-coordinator", f"homestead-shutdown-{run}-0"]
        objects = [pod(name, "cluster-shutdown", finished=NOW - 30 * DAY) for name in names]
        follow = {"id": "op-2", "kind": "cluster-shutdown", "status": "running", "ref": {"run": run, "namespace": "lab"}}
        self.run_sweep(*objects, ops=[follow])
        self.assertTrue(all(self.left("Pod", "lab", name) for name in names))
        self.run_sweep(*objects, ops=[dict(follow, status="succeeded")])
        self.assertFalse(any(self.left("Pod", "lab", name) for name in names))

    def test_a_finished_pull_watcher_goes_after_an_hour_even_from_another_release(self):
        self.run_sweep(pod("homestead-pull-watch-0123456789abcd", "pull-watch", finished=NOW - 2 * HOUR),
                       pod("homestead-pull-watch-0123456789abce", "pull-watch", finished=NOW - 60))
        self.assertFalse(self.left("Pod", "lab", "homestead-pull-watch-0123456789abcd"))
        self.assertTrue(self.left("Pod", "lab", "homestead-pull-watch-0123456789abce"))

    def test_only_homestead_helpers_by_label_and_name(self):
        self.run_sweep(pod("homestead-image-clean-0123456789-a1b2c3", "", finished=NOW - 30 * DAY),
                       pod("backup-image-clean", "image-cleanup", finished=NOW - 30 * DAY),
                       pod("homestead-host-node-a-1", "something-else", finished=NOW - 30 * DAY),
                       pod("homestead-reboot-node-a-1", "node-power", ns="apps", finished=NOW - 30 * DAY))
        self.assertEqual([], [w for w in self.kube.writes if w[0] == "DELETE"])

    def test_nothing_goes_when_the_jobs_cannot_be_read(self):
        kube = Kube(pod("homestead-image-clean-0123456789-a1b2c3", "image-cleanup"))
        with kube, mock.patch.object(SWEEP.OPS, "stored", side_effect=ValueError("store unreadable")):
            self.assertEqual([], SWEEP.sweep(now=NOW))
        self.assertEqual([], [w for w in kube.writes if w[0] == "DELETE"])

    def test_a_pod_replaced_since_it_was_read_is_left(self):
        name = "homestead-image-clean-0123456789-a1b2c3"
        kube = Kube(pod(name, "image-cleanup", uid="uid-old"))

        def swap(method, path, body):
            kube.add(pod(name, "image-cleanup", phase="Running", uid="uid-new"))
            return None
        kube.react("DELETE", f"/api/v1/namespaces/lab/pods/{name}", swap)
        with kube, mock.patch.object(SWEEP.OPS, "stored", return_value=[]):
            self.assertEqual([], SWEEP.sweep(now=NOW))
        self.assertEqual("uid-new", kube.object(f"/api/v1/namespaces/lab/pods/{name}")["metadata"]["uid"])


class FinishedJobTests(Base):
    def test_a_finished_storage_copy_job_goes_after_a_week_with_its_pods(self):
        self.run_sweep(job("app-restructure-a1b2c3", "restructure", finished=NOW - 8 * DAY),
                       job("web-restructure-a1b2c4", "restructure", finished=NOW - 2 * DAY),
                       job("db-restructure-a1b2c5", "restructure", state="Running"))
        self.assertFalse(self.left("Job", "apps", "app-restructure-a1b2c3"))
        self.assertTrue(self.left("Job", "apps", "web-restructure-a1b2c4"))
        self.assertTrue(self.left("Job", "apps", "db-restructure-a1b2c5"))
        delete = next(w for w in self.kube.writes if w[0] == "DELETE")
        self.assertEqual("Background", delete[2]["propagationPolicy"])

    def test_a_job_with_a_pod_still_running_is_kept_however_old(self):
        old = job("app-restructure-a1b2c3", "restructure", finished=NOW - 30 * DAY)
        old["status"]["active"] = 1
        self.run_sweep(old)
        self.assertTrue(self.left("Job", "apps", "app-restructure-a1b2c3"))

    def test_a_copy_kept_for_inspection_keeps_its_job(self):
        failed = {"id": "op-3", "kind": "workload-copy", "status": "failed",
                  "ref": {"namespace": "apps", "job": "app-restructure-a1b2c3", "retain_resources": True}}
        self.run_sweep(job("app-restructure-a1b2c3", "restructure", state="Failed", finished=NOW - 30 * DAY),
                       ops=[failed])
        self.assertTrue(self.left("Job", "apps", "app-restructure-a1b2c3"))

    def test_a_user_job_with_a_homestead_label_is_left(self):
        self.run_sweep(job("nightly-backup", "restructure", finished=NOW - 30 * DAY))
        self.assertTrue(self.left("Job", "apps", "nightly-backup"))


class DeleteTests(unittest.TestCase):
    def test_a_refused_delete_is_said_and_gone_or_replaced_are_not(self):
        import io
        import urllib.error
        for code, said in ((403, True), (404, False), (409, False)):
            error = urllib.error.HTTPError("/x", code, "no", {}, io.BytesIO(b""))
            with self.subTest(code=code), mock.patch.object(SWEEP.KUBE, "ksend", side_effect=error), \
                    mock.patch("builtins.print") as printed:
                self.assertFalse(SWEEP._delete("/api/v1/namespaces/lab/pods/p", {"name": "p", "uid": "u"}))
            self.assertEqual(said, printed.called)

    def test_a_job_store_row_that_is_not_an_object_is_skipped(self):
        self.assertEqual({"homestead-reboot-node-a-1"},
                         SWEEP._followed(["junk", None, {"status": "running", "ref": {"helper_pod": "homestead-reboot-node-a-1"}}]))

    def test_a_finished_one_with_no_time_on_it_counts_as_ending_at_the_round_given(self):
        bare = {"metadata": {"name": "p"}, "status": {"phase": "Succeeded"}}
        self.assertEqual(NOW, SWEEP._pod_ended(bare, NOW))
        undated = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        self.assertEqual(NOW, SWEEP._job_ended(undated, NOW))


class FinishedImportTests(Base):
    def receipt(self, name, ns="apps", **ref):
        return {"id": f"op-{name}", "kind": "import-create", "status": "succeeded",
                "ref": dict({"namespace": ns, "job": name, "job_uid": f"uid-{name}"}, **ref)}

    def test_an_import_finished_a_week_ago_is_removed_the_way_remove_does_it(self):
        old = job("homestead-import-app", "import", finished=NOW - 8 * DAY, annotations={"homestead.io/dispatch": "x"})
        old["metadata"]["uid"] = "uid-homestead-import-app"
        young = job("homestead-import-web", "import", finished=NOW - DAY)
        young["metadata"]["uid"] = "uid-homestead-import-web"
        failed = job("homestead-import-db", "import", state="Failed", finished=NOW - 30 * DAY)
        failed["metadata"]["uid"] = "uid-homestead-import-db"
        ops = [self.receipt("homestead-import-app"), self.receipt("homestead-import-web"),
               dict(self.receipt("homestead-import-db"), status="failed")]
        with mock.patch.object(SWEEP.IMPORT_JOB, "remove_completed_job") as remove:
            self.run_sweep(old, young, failed, ops=ops)
        self.assertEqual([("apps", "homestead-import-app")], [c.args[:2] for c in remove.call_args_list])

    def test_an_import_with_no_receipt_or_one_already_removed_is_not_touched(self):
        old = job("homestead-import-app", "import", finished=NOW - 30 * DAY)
        old["metadata"]["uid"] = "uid-homestead-import-app"
        with mock.patch.object(SWEEP.IMPORT_JOB, "remove_completed_job") as remove:
            self.run_sweep(old, ops=[])
            self.run_sweep(old, ops=[self.receipt("homestead-import-app", helper_removed=True)])
            self.run_sweep(old, ops=[self.receipt("homestead-import-app", job_uid="uid-other")])
        remove.assert_not_called()

    def test_an_import_that_cannot_be_removed_does_not_stop_the_round(self):
        old = job("homestead-import-app", "import", finished=NOW - 30 * DAY)
        old["metadata"]["uid"] = "uid-homestead-import-app"
        with mock.patch.object(SWEEP.IMPORT_JOB, "remove_completed_job", side_effect=ValueError("busy")):
            self.run_sweep(old, pod("homestead-image-clean-0123456789-a1b2c3", "image-cleanup"),
                           ops=[self.receipt("homestead-import-app")])
        self.assertFalse(self.left("Pod", "lab", "homestead-image-clean-0123456789-a1b2c3"))


class TickTests(unittest.TestCase):
    def test_the_leader_round_also_sweeps_finished_pull_watchers(self):
        with mock.patch.object(SWEEP.PULLWATCH, "sweep") as watchers, mock.patch.object(SWEEP, "sweep") as helpers:
            SWEEP.tick(now=NOW)
        watchers.assert_called_once_with(NOW)
        helpers.assert_called_once_with(now=NOW)

    def test_a_failed_pull_watch_sweep_still_sweeps_the_rest(self):
        with mock.patch.object(SWEEP.PULLWATCH, "sweep", side_effect=RuntimeError("unbound")), \
                mock.patch.object(SWEEP, "sweep") as helpers:
            SWEEP.tick(now=NOW)
        helpers.assert_called_once_with(now=NOW)


class LeaderLoopTests(unittest.TestCase):
    """The leader's one-minute loop runs the sweep, even when the image tick
    beside it fails, and not while the cluster shuts down."""

    def run_once(self, leader=True, phase=None, images=None):
        import types
        import homestead_cluster_server as CLUSTER
        sleeps = iter([None])

        def sleep(_):
            next(sleeps)            # the second sleep ends the loop
        state = types.SimpleNamespace(state=lambda: {"phase": phase} if phase else {})
        swept = mock.Mock()
        with mock.patch.object(CLUSTER.time, "sleep", sleep), \
             mock.patch.object(CLUSTER.LEADER, "is_leader", lambda: leader), \
             mock.patch.object(CLUSTER, "cluster_shutdown", lambda: state), \
             mock.patch.object(CLUSTER.LHREBUILD, "tick", mock.Mock()), \
             mock.patch.object(CLUSTER, "_cache", {}, create=True), \
             mock.patch.object(getattr(CLUSTER, "IMAGES", None) or CLUSTER.IMPORTS, "tick", images or mock.Mock()), \
             mock.patch.object(CLUSTER.HELPER_SWEEP, "tick", swept), \
             mock.patch("builtins.print"):
            with self.assertRaises(StopIteration):
                CLUSTER._detached_copies_loop()
        return swept.call_count

    def test_the_leader_sweeps_each_minute_even_when_the_image_tick_fails(self):
        self.assertEqual(1, self.run_once())
        self.assertEqual(1, self.run_once(images=mock.Mock(side_effect=RuntimeError("down"))))

    def test_not_on_another_replica_or_during_a_shutdown(self):
        self.assertEqual(0, self.run_once(leader=False))
        self.assertEqual(0, self.run_once(phase="stopping"))


if __name__ == "__main__":
    unittest.main()
