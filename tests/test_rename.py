"""Rename fault injection: no duplicate writers, blind undo, or replay."""
import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_capacity_review as review
import homestead_lifecycle as lifecycle
import homestead_operations as ops
import homestead_rename as rename
import homestead_vm_power_job as locks


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": "old", "uid": "uid-old", "resourceVersion": "1", "generation": 1},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "old", "homestead.io/workload": "old"}},
                     "template": {"metadata": {"labels": {"app": "old", "homestead.io/workload": "old"}},
                                  "spec": {"containers": [{"name": "main", "image": "example/test:1",
                                                          "env": [{"name": "SECRET", "value": "do-not-journal"}]}],
                                           "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}},
            "status": self.state(1, 1)}
        self.objects = {"old": self.source}
        self.hpas, self.pods, self.sets, self.sent = [], [], [], []
        self.extra, self.after_send = {}, lambda *args: None
        self.admission = mock.Mock(return_value={"blocked": False})
        self.body = {"ns": "lab", "name": "old", "workload_name": "new"}
        for patch in (mock.patch.object(review, "_key", return_value=b"rename-tests"),
                      mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {rename.KIND: lambda item: rename.status(item, ops)}),
                      mock.patch.dict(ops.CANCELLERS, {rename.KIND: (
                          lambda item: rename.recovery_plan(item, self.read, ops),
                          lambda item, options: rename.recovery_run(item, options, self.read, ops))})):
            patch.start()
            self.addCleanup(patch.stop)
        ops.CLEANUPS.add(rename.KIND)

    @staticmethod
    def state(count, generation):
        return {**{key: count for key in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")},
                "observedGeneration": generation}

    def read(self, path):
        if path in self.extra:
            return copy.deepcopy(self.extra[path])
        if path.endswith("/horizontalpodautoscalers"):
            return {"items": copy.deepcopy(self.hpas)}
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path.endswith("/replicasets"):
            return {"items": copy.deepcopy(self.sets)}
        if "/deployments/" in path and path.rsplit("/", 1)[-1] in self.objects:
            return copy.deepcopy(self.objects[path.rsplit("/", 1)[-1]])
        raise urllib.error.HTTPError(path, 404, "not found", {}, None)

    def send(self, method, path, payload, **kwargs):
        self.sent.append((method, path, copy.deepcopy(payload)))
        if method == "POST":
            obj = copy.deepcopy(payload)
            name = obj["metadata"]["name"]
            self.assertNotIn(name, self.objects)
            obj["metadata"].update(uid="uid-" + name, resourceVersion="1", generation=1)
            obj["status"] = self.state(obj["spec"]["replicas"], 1)
            self.objects[name] = obj
        else:
            name = path.rsplit("/", 1)[-1]
            obj = self.objects[name]
            if method == "PATCH":
                self.assertEqual("application/json-patch+json", kwargs["ctype"])
                self.assertEqual({"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]}, payload[0])
                self.assertEqual({"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]}, payload[1])
                obj["spec"]["replicas"] = payload[2]["value"]
                obj["metadata"]["generation"] += 1
                obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
                obj["status"] = self.state(obj["spec"]["replicas"], obj["metadata"]["generation"])
            elif method == "DELETE":
                self.assertEqual("Orphan", payload["propagationPolicy"])
                self.assertEqual({key: obj["metadata"][key] for key in ("uid", "resourceVersion")}, payload["preconditions"])
                self.objects.pop(name)
        self.after_send(method, name, obj)
        return copy.deepcopy(obj)

    def prepare(self):
        self.proposed, self.context = rename.prepare(self.body, self.source, lifecycle._renamed_deployment, self.read)
        self.body.update(capacity_token=review.issue(self.body, self.context), confirm_capacity=True)

    def dispatch(self, **kwargs):
        if not hasattr(self, "proposed"):
            self.prepare()
        return rename.dispatch(self.body, self.context, self.proposed, self.read,
                               kwargs.pop("send", self.send), ops, self.admission, timeout=0, **kwargs)

    def test_success_rechecks_after_stop_and_keeps_service_labels_storage_and_secrets(self):
        def check(dep):
            self.assertEqual(0, self.objects["old"]["spec"]["replicas"])
            self.assertEqual(0, self.objects["new"]["spec"]["replicas"])
            self.assertEqual(1, dep["spec"]["replicas"])
            return {"blocked": False}
        self.admission.side_effect = check
        result = self.dispatch()
        self.assertTrue(result["renamed"])
        self.assertEqual("succeeded", result["operation"]["status"])
        self.assertEqual(["POST", "PATCH", "PATCH", "DELETE"], [row[0] for row in self.sent])
        new = self.objects["new"]
        self.assertEqual("old", new["spec"]["template"]["metadata"]["labels"]["homestead.io/workload"])
        self.assertIn("new", [value for key, value in new["spec"]["selector"]["matchLabels"].items() if key.startswith("homestead.io/rename-")])
        self.assertEqual("data", new["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertNotIn("do-not-journal", json.dumps(ops._read()))
        self.assertEqual(4, len(ops._read()[0]["ref"]["writes"]))

    def test_late_capacity_block_keeps_both_stopped_and_never_auto_restores(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaisesRegex(ValueError, "no longer fits"):
            self.dispatch()
        self.assertEqual(["POST", "PATCH"], [row[0] for row in self.sent])
        self.assertTrue(all(obj["spec"]["replicas"] == 0 for obj in self.objects.values()))
        self.assertTrue(ops._read()[0]["ref"]["retain_resources"])
        self.assertTrue(ops.list_operations()[0]["cleanable"])

    def test_repeated_rename_preserves_labels_a_later_service_may_select(self):
        self.dispatch()
        before = self.objects["new"]
        again = lifecycle._renamed_deployment(before, "lab", "third")
        labels = again["spec"]["template"]["metadata"]["labels"]
        for key, value in before["spec"]["selector"]["matchLabels"].items():
            self.assertEqual(value, labels[key])
            self.assertEqual(value, again["spec"]["selector"]["matchLabels"][key])
        self.assertEqual(len(before["spec"]["selector"]["matchLabels"]) + 1,
                         len(again["spec"]["selector"]["matchLabels"]))

    def test_custom_controller_hash_label_cannot_be_silently_removed(self):
        self.source["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] = "custom"
        with self.assertRaisesRegex(ValueError, "existing pod labels"):
            self.prepare()
        self.assertEqual([], self.sent)

    def test_incomplete_capacity_response_cannot_start_replacement(self):
        self.admission.return_value = {}
        with self.assertRaisesRegex(ValueError, "no longer fits"):
            self.dispatch()
        self.assertEqual(["POST", "PATCH"], [row[0] for row in self.sent])

    def test_memory_warning_is_overridable_but_actual_api_injected_requests_are_checked(self):
        def injected(method, name, obj):
            if method == "POST":
                obj["spec"]["template"]["spec"]["containers"].append({"name": "injected", "resources": {"requests": {"memory": "2Gi"}}})
        self.after_send = injected
        self.admission.return_value = {"blocked": False, "warnings": ["Memory above warning limit"], "requires_confirmation": True}
        self.dispatch()
        self.assertEqual(2, len(self.admission.call_args.args[0]["spec"]["template"]["spec"]["containers"]))

    def test_changed_review_or_same_name_replacement_never_writes(self):
        self.prepare()
        self.source["metadata"]["uid"] = "foreign"
        with self.assertRaisesRegex(ValueError, "changed after review"):
            self.dispatch()
        self.assertEqual([], self.sent)

    def test_target_automatically_started_by_external_actor_does_not_stop_source(self):
        self.after_send = lambda method, name, obj: obj["spec"].update(replicas=1) if method == "POST" else None
        with self.assertRaisesRegex(ValueError, "not confirmed stopped"):
            self.dispatch()
        self.assertEqual(["POST"], [row[0] for row in self.sent])
        self.assertEqual(1, self.source["spec"]["replicas"])

    def test_terminating_old_pods_block_even_with_zero_controller_counters(self):
        self.sets = [{"metadata": {"namespace": "lab", "uid": "rs-old", "ownerReferences": [
            {"kind": "Deployment", "uid": "uid-old", "controller": True}]}}]
        self.pods = [{"metadata": {"namespace": "lab", "uid": "pod-old", "deletionTimestamp": "now", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "rs-old", "controller": True}]}, "status": {"phase": "Running"}}]
        with self.assertRaisesRegex(ValueError, "did not finish stopping"):
            self.dispatch()
        self.admission.assert_not_called()
        self.assertEqual(["POST", "PATCH"], [row[0] for row in self.sent])

    def test_unknown_pod_ownership_fails_closed_before_stopping_source(self):
        self.pods = [{"metadata": {"namespace": "lab", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "missing", "controller": True}]}, "status": {"phase": "Running"}}]
        with self.assertRaisesRegex(ValueError, "not confirmed stopped"):
            self.dispatch()
        self.assertEqual(["POST"], [row[0] for row in self.sent])

    def test_target_replaced_or_edited_during_stop_is_never_started(self):
        def replaced(method, name, obj):
            if method == "PATCH" and name == "old":
                self.objects["new"]["metadata"]["uid"] = "foreign"
        self.after_send = replaced
        with self.assertRaisesRegex(ValueError, "was replaced"):
            self.dispatch()
        self.assertEqual(["POST", "PATCH"], [row[0] for row in self.sent])

    def test_autoscaler_on_either_name_blocks_before_any_write(self):
        for name in ("old", "new"):
            self.hpas = [{"spec": {"scaleTargetRef": {"kind": "Deployment", "name": name}}}]
            with self.assertRaisesRegex(ValueError, "autoscaling"):
                self.prepare()
        self.assertEqual([], self.sent)

    def test_late_autoscaler_does_not_scale_original(self):
        self.after_send = lambda *args: self.hpas.append({"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "old"}}})
        with self.assertRaisesRegex(ValueError, "autoscaling"):
            self.dispatch()
        self.assertEqual(["POST"], [row[0] for row in self.sent])

    def test_owner_paused_and_helm_managed_workloads_are_refused(self):
        for field, value in (("ownerReferences", [{"uid": "controller"}]), ("annotations", {"meta.helm.sh/release-name": "release"})):
            self.source["metadata"][field] = value
            with self.assertRaisesRegex(ValueError, "managed workload"):
                self.prepare()
            self.source["metadata"].pop(field)
        self.source["spec"]["paused"] = True
        with self.assertRaisesRegex(ValueError, "paused"):
            self.prepare()

    def test_unverified_import_is_refused_and_completed_import_is_not_rebound_to_wrong_uid(self):
        self.source["metadata"]["annotations"] = {rename.IMPORT.JOB: "copy"}
        with self.assertRaisesRegex(ValueError, "Import copy"):
            self.prepare()
        self.extra["/apis/batch/v1/namespaces/lab/jobs/copy"] = {
            "metadata": {"ownerReferences": [{"kind": "Deployment", "uid": "uid-old"}]},
            "status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        self.dispatch()
        self.assertNotIn(rename.IMPORT.JOB, self.objects["new"]["metadata"]["annotations"])

    def test_rename_refuses_other_edits_and_existing_target(self):
        self.body["containers"] = []
        with self.assertRaisesRegex(ValueError, "separately"):
            self.prepare()
        self.body.pop("containers")
        self.objects["new"] = copy.deepcopy(self.source)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()

    def test_lost_response_retains_intent_and_never_retries_or_exposes_api_error(self):
        def lost(method, path, payload, **kwargs):
            self.send(method, path, payload, **kwargs)
            raise TimeoutError("do-not-journal private API response")
        with self.assertRaisesRegex(ValueError, "could not be confirmed") as caught:
            self.dispatch(send=lost)
        self.assertNotIn("do-not-journal", str(caught.exception))
        self.assertEqual(1, len(self.sent))
        job = ops._read()[0]
        self.assertEqual("uncertain", job["ref"]["writes"][0]["phase"])
        self.assertEqual("failed", job["status"])
        self.assertNotIn("do-not-journal", json.dumps(job))

    def test_failed_journal_prevents_cluster_writes(self):
        with mock.patch.object(ops, "record_phase", side_effect=OSError("unwritable")):
            with self.assertRaisesRegex(ValueError, "job storage"):
                self.dispatch()
        self.assertEqual([], self.sent)
        self.assertEqual("queued", ops._read()[0]["status"])

    def test_readiness_failure_keeps_new_running_and_old_stopped(self):
        def not_ready(method, name, obj):
            if method == "PATCH" and name == "new":
                obj["status"]["availableReplicas"] = 0
        self.after_send = not_ready
        with self.assertRaisesRegex(ValueError, "did not become ready"):
            self.dispatch()
        self.assertEqual(0, self.source["spec"]["replicas"])
        self.assertEqual(1, self.objects["new"]["spec"]["replicas"])
        self.assertEqual(["POST", "PATCH", "PATCH"], [row[0] for row in self.sent])

    def test_admission_mutation_during_scale_cannot_be_reported_as_success(self):
        def mutate(method, name, obj):
            if method == "PATCH" and name == "old":
                obj["spec"]["template"]["spec"]["hostNetwork"] = True
        self.after_send = mutate
        with self.assertRaisesRegex(ValueError, "more than the replica count"):
            self.dispatch()
        self.assertEqual(["POST", "PATCH"], [row[0] for row in self.sent])
        self.admission.assert_not_called()

    def test_replay_refused_after_success_and_visible_history_pruning(self):
        self.dispatch()
        ops._write([])
        before = len(self.sent)
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        self.assertEqual(before, len(self.sent))

    def test_tracking_recovery_never_mutates_resources_or_reenables_old_approval(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.dispatch()
        job = ops._read()[0]
        before = copy.deepcopy(self.objects), copy.deepcopy(self.sent)
        with locks.worker_lock(job["id"], ops):
            self.assertFalse(rename.recovery_plan(job, self.read, ops)["can"])
        self.assertTrue(rename.recovery_plan(job, self.read, ops)["can"])
        with self.assertRaisesRegex(ValueError, "Acknowledge"):
            ops.cancel(job["id"], options={}, confirm="old")
        # A failed acknowledgement leaves the job inspectable.
        ops.cancel(job["id"], options={"ack": True}, confirm="old")
        self.assertEqual(before, (self.objects, self.sent))
        self.assertTrue(ops._read()[0]["tracking_stopped"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()

    def test_stopped_workload_stays_stopped(self):
        self.source["spec"]["replicas"] = 0
        self.source["status"] = self.state(0, 1)
        self.dispatch()
        self.assertEqual(0, self.objects["new"]["spec"]["replicas"])
        self.assertEqual(["POST", "PATCH", "DELETE"], [row[0] for row in self.sent])

    def test_interrupted_dispatch_remains_inspectable_without_replay(self):
        with mock.patch.object(ops, "record_phase", side_effect=OSError("unwritable")):
            with self.assertRaises(ValueError):
                self.dispatch()
        job = ops.list_operations()[0]
        self.assertEqual("running", job["status"])
        self.assertIn("no longer active", job["message"])
        self.assertTrue(job["rename_recovery"])
        self.assertEqual([], self.sent)

    def test_unacknowledged_failure_blocks_new_approval_touching_either_name(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.dispatch()
        self.body["workload_name"] = "another"
        self.prepare()
        before = len(self.sent)
        with self.assertRaisesRegex(ValueError, "still active or needs recovery"):
            self.dispatch()
        self.assertEqual(before, len(self.sent))


if __name__ == "__main__":
    unittest.main()
