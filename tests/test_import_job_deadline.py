"""An import's copy Job ends at a deadline, and its failure says why; it can also be cancelled (on the shared fake)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from fakes.kube import Kube
import homestead_capacity_review as review
import homestead_copy_job as COPY
import homestead_import_guard as guard
import homestead_import_job as flow
import homestead_imports as imports
import homestead_kube as K
import homestead_operations as ops

DEP = "/apis/apps/v1/namespaces/lab/deployments/app"
JOB = "/apis/batch/v1/namespaces/lab/jobs/homestead-import-app"


class ImportDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kube = Kube()
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__)
        self.admission = mock.Mock(return_value={"blocked": False})
        for patch in (mock.patch.object(review, "_key", return_value=b"import-test-key"),
                      mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {flow.KIND: lambda item: flow.status(item, K.kget)}),
                      mock.patch.dict(ops.CANCELLERS, {flow.KIND: (flow.cancel_plan, flow.cancel_run)}),
                      mock.patch.object(imports, "kget", K.kget), mock.patch.object(imports, "ksend", K.ksend),
                      mock.patch.object(imports, "NS", "lab")):
            patch.start()
            self.addCleanup(patch.stop)
        template = {"metadata": {"labels": {"app": "app"}}, "spec": {"restartPolicy": "Never", "containers": [
            {"name": "copy", "image": "example/copy"}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}
        self.prepared = {
            "volumes": [{"name": "data", "size_gb": 40, "storage_class": "storage", "access_mode": "ReadWriteOnce", "create": True}],
            "deployment": {"apiVersion": "apps/v1", "kind": "Deployment",
                           "metadata": {"namespace": "lab", "name": "app", "annotations": {guard.JOB: "homestead-import-app"}},
                           "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "app"}}, "template": {
                               "metadata": {"labels": {"app": "app"}},
                               "spec": {"containers": [{"name": "app", "image": "example/app"}]}}}},
            "service": None,
            "job": {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "homestead-import-app", "namespace": "lab"},
                    "spec": {"backoffLimit": 0, "template": template}},
            "result": {"ok": True, "name": "app", "job": "homestead-import-app"}}
        self.context = {"namespace": "lab", "claims": {}}
        self.body = {"name": "app"}
        self.body.update(capacity_token=review.issue(self.body, self.context), confirm_capacity=True)

    def create_claim(self, ns, name, size, storage_class, access_mode, *, send):
        return send("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"name": name, "namespace": ns}, "spec": {"accessModes": [access_mode],
                    "resources": {"requests": {"storage": f"{size or 1}Gi"}}}})

    def start(self):
        result = flow.dispatch(self.body, self.prepared, self.context, K.kget, K.ksend, ops, self.create_claim, self.admission)
        self.assertEqual("running", result["operation"]["status"])
        self.ident = result["operation"]["id"]
        return self.kube.object(JOB)

    def finish(self, job, condition, reason=""):
        job["status"] = {"conditions": [{"type": condition, "status": "True", "reason": reason}]}
        self.kube.add(job)

    def test_the_import_copy_job_carries_a_deadline_sized_from_its_volumes(self):
        job = self.start()
        self.assertEqual(COPY.deadline(40), job["spec"]["activeDeadlineSeconds"])

    def test_a_copy_pod_that_never_schedules_fails_the_import_after_the_deadline_with_the_reason(self):
        job = self.start()
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "copy-pod", "namespace": "lab",
            "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": "homestead-import-app",
                                 "uid": job["metadata"]["uid"], "controller": True}]},
            "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]}})
        self.assertEqual("running", ops.list_operations()[0]["status"])
        self.finish(job, "Failed", "DeadlineExceeded")
        self.kube.discard("/api/v1/namespaces/lab/pods/copy-pod")
        result = ops.list_operations()[0]
        self.assertEqual("failed", result["status"])
        self.assertIn("Copying 40 GiB did not finish within 1.8 hours", result["message"])
        self.assertIn("app was created stopped for this import and stays stopped", result["message"])
        # It never ran: stopped is its earlier count, and the guard still holds it.
        self.assertEqual(0, self.kube.object(DEP)["spec"]["replicas"])
        self.assertTrue(guard.pending(self.kube.object(DEP), "lab", K.kget))

    def test_a_failed_copy_into_volumes_only_does_not_speak_of_an_app(self):
        self.prepared["deployment"] = None
        job = self.start()
        self.finish(job, "Failed", "BackoffLimitExceeded")
        result = ops.list_operations()[0]
        self.assertEqual("failed", result["status"])
        self.assertIn("The copy failed (BackoffLimitExceeded)", result["message"])
        self.assertNotIn("created stopped", result["message"])

    def test_a_volume_of_unknown_size_gets_the_ceiling(self):
        self.prepared["volumes"][0]["size_gb"] = None
        self.assertEqual(COPY.DEADLINE_CEILING, self.start()["spec"]["activeDeadlineSeconds"])

    def test_a_normal_import_copy_still_succeeds(self):
        job = self.start()
        self.finish(job, "Complete")
        self.assertEqual("succeeded", ops.list_operations()[0]["status"])

    def item(self):
        return ops.list_operations()[0]

    def cancel(self):
        return ops.cancel(self.ident, {}, "")

    def test_a_running_import_copy_can_be_cancelled_and_says_what_it_keeps(self):
        self.start()
        plan = ops.cancel_plan(self.ident)
        self.assertTrue(plan["can"], plan.get("why_not"))
        self.assertEqual("stop", plan["mode"])
        self.assertEqual("admin", plan["needs"])
        self.assertIn("volumes", " ".join(plan["keeps"]))
        self.assertIn("Start refuses", " ".join(plan["keeps"]))
        self.assertIn("log goes with the Job", " ".join(plan["undo"]))

    def test_cancelling_removes_the_copy_job_and_keeps_volumes_and_the_stopped_app(self):
        self.start()
        self.cancel()
        result = self.item()
        self.assertEqual("cancelled", result["status"])
        self.assertIsNone(self.kube.object(JOB))
        self.assertEqual(0, self.kube.object(DEP)["spec"]["replicas"])
        self.assertTrue(guard.pending(self.kube.object(DEP), "lab", K.kget), "data is incomplete: the app stays held")
        self.assertIn("stays stopped", result["message"])
        self.assertIn("delete app and its volumes, then import again", result["message"])
        self.assertTrue(self.kube.object("/api/v1/namespaces/lab/persistentvolumeclaims/data"))

    def test_cancelling_twice_after_the_job_is_gone_still_finishes(self):
        self.start()
        self.kube.discard(JOB)
        self.cancel()
        self.assertEqual("cancelled", self.item()["status"])

    def test_a_replaced_copy_job_is_not_deleted_by_a_cancel(self):
        job = self.start()
        other = dict(job, metadata=dict(job["metadata"], uid="someone-elses"))
        self.kube.add(other)
        with self.assertRaises(ValueError):
            self.cancel()
        self.assertEqual("someone-elses", self.kube.object(JOB)["metadata"]["uid"])
        self.assertEqual("running", ops.snapshot()[0]["status"])

    def test_a_copy_that_already_finished_is_not_cancelled(self):
        job = self.start()
        self.finish(job, "Complete")
        with self.assertRaises(ValueError):
            self.cancel()
        self.assertIsNotNone(self.kube.object(JOB))
        ops.list_operations()
        self.assertEqual("succeeded", self.item()["status"])

    def bumping(self, condition=None, times=1):
        """A read that finds the Job changed by someone else just after it, as the API server does."""
        calls = []

        def read(path):
            got = K.kget(path)
            if path == JOB and len(calls) < times:
                calls.append(1)
                changed = self.kube.object(JOB)
                changed["metadata"]["resourceVersion"] = str(999990 + len(calls))
                if condition:
                    self.finish(changed, condition)
                else:
                    self.kube.add(changed)
            return got
        return read

    def run_cancel(self, read):
        stored = next(i for i in ops.stored() if i["id"] == self.ident)
        return flow.cancel_run(stored, {}, read=read)

    def test_a_copy_that_completes_between_the_read_and_the_delete_is_not_deleted(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "already finished"):
            self.run_cancel(self.bumping("Complete"))
        self.assertIsNotNone(self.kube.object(JOB))

    def test_a_status_update_between_the_read_and_the_delete_does_not_stop_the_cancel(self):
        self.start()
        self.run_cancel(self.bumping())
        self.assertIsNone(self.kube.object(JOB))

    def test_a_job_that_keeps_changing_ends_in_a_plain_sentence(self):
        self.start()
        with self.assertRaises(ValueError) as caught:
            self.run_cancel(self.bumping(times=9))
        self.assertEqual("The copy changed while it was being stopped. Try again.", str(caught.exception))
        self.assertIsNotNone(self.kube.object(JOB))

    def test_setup_before_the_copy_job_exists_still_cannot_be_cancelled(self):
        self.start()
        ident = self.ident
        with ops.transaction() as t:   # the journal as it stands mid-setup
            t.items[0]["ref"].update(phase="writing", job_uid="")
            t.save(t.items)
        self.assertFalse(ops.cancel_plan(ident)["can"])
        with self.assertRaises(ValueError):
            ops.cancel(ident, {}, "")


if __name__ == "__main__":
    unittest.main()
