"""An import's copy Job ends at a deadline, and its failure says why (on the shared fake)."""
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


if __name__ == "__main__":
    unittest.main()
