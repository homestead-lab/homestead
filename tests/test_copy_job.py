import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_copy_job as flow
import homestead_operations as ops
import homestead_capacity_review as review
import homestead_import_guard as guard
import homestead_restructure as restructure
import homestead_vm_power_job as locks


class CopyJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dep = {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "app", "namespace": "lab", "uid": "dep-uid", "resourceVersion": "1", "generation": 1},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "app"}}, "template": {
                "metadata": {"labels": {"app": "app"}}, "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "old"}}],
                "containers": [{"name": "main", "image": "example/app", "env": [{"name": "KEY", "value": "secret-not-in-journal"}],
                                "volumeMounts": [{"name": "data", "mountPath": "/data"}]}]}}},
            "status": self.state(1, 1)}
        self.pvcs = {name: {"metadata": {"name": name, "namespace": "lab", "uid": name + "-uid", "resourceVersion": "1"},
                           "spec": {"volumeName": "pv-" + name}} for name in ("old", "new")}
        self.pvs = {"pv-" + name: {"metadata": {"uid": "pv-" + name + "-uid"}} for name in ("old", "new")}
        self.body = {"ns": "lab", "name": "app", "containers": [{"original_name": "main", "name": "main", "volumes": [
            {"path": "/data", "kind": "existing", "source": "new", "copy_from": {"claim": "old"}}]}]}
        self.context = {"action": "edit", "uid": "dep-uid", "resourceVersion": "1"}
        self.prepared = {"deployment": copy.deepcopy(self.dep), "claims": [], "seeds": [], "name": "app"}
        self.prepared["deployment"]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "new"
        self.jobs, self.pods, self.sets, self.hpas, self.sent = {}, [], [], [], []
        self.after_send = lambda *args: None
        self.admission = mock.Mock(return_value={"blocked": False, "warnings": []})
        for patch in (mock.patch.object(review, "_key", return_value=b"copy-tests"),
                      mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {flow.KIND: lambda item: flow.resolve(item, self.read, self.send, ops, self.admission)}),
                      mock.patch.dict(ops.CANCELLERS, {flow.KIND: (lambda item: flow.recovery_plan(item, self.read, ops),
                          lambda item, options: flow.recovery_run(item, options, self.read, self.send, ops))})):
            patch.start()
            self.addCleanup(patch.stop)
        ops.CLEANUPS.add(flow.KIND)
        self.body.update(capacity_token=review.issue(self.body, self.context), confirm_capacity=True)

    @staticmethod
    def state(count, generation):
        return {**{key: count for key in ("replicas", "readyReplicas", "availableReplicas", "updatedReplicas")}, "observedGeneration": generation}

    def read(self, path):
        if path.endswith("/deployments/app"):
            return copy.deepcopy(self.dep)
        if path.endswith("/horizontalpodautoscalers"):
            return {"items": copy.deepcopy(self.hpas)}
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path.endswith("/replicasets"):
            return {"items": copy.deepcopy(self.sets)}
        name = path.rsplit("/", 1)[-1]
        if "/persistentvolumeclaims/" in path and name in self.pvcs:
            return copy.deepcopy(self.pvcs[name])
        if "/persistentvolumes/" in path and name in self.pvs:
            return copy.deepcopy(self.pvs[name])
        if "/jobs/" in path and name in self.jobs:
            return copy.deepcopy(self.jobs[name])
        raise urllib.error.HTTPError(path, 404, "private API body", {}, None)

    def apply(self):
        self.assertEqual("preparing", ops._read()[0]["ref"]["phase"])
        count = restructure.hold(self.prepared["deployment"])
        self.dep = copy.deepcopy(self.prepared["deployment"])
        self.dep["metadata"].update(resourceVersion="2", generation=2)
        self.dep["status"] = self.state(0, 2)
        self.sent.append(("PUT", "Deployment/app", None))
        return {"ok": True, "name": "app", "held_replicas": count, "_held_receipt": copy.deepcopy(self.dep)}

    def send(self, method, path, body, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            self.assertEqual("copy_intent", ops._read()[0]["ref"]["phase"])
            name = body["metadata"]["name"]
            self.assertNotIn(name, self.jobs)
            obj = copy.deepcopy(body)
            obj["metadata"].update(uid="copy-uid", resourceVersion="1")
            obj["status"] = {"active": 1}
            self.jobs[name] = obj
        else:
            self.assertEqual("application/json-patch+json", kwargs["ctype"])
            obj = self.dep
            for change in body:
                keys = [s.replace("~1", "/").replace("~0", "~") for s in change["path"].strip("/").split("/")]
                target = obj
                for key in keys[:-1]:
                    target = target[key]
                if change["op"] == "test":
                    self.assertEqual(change["value"], target[keys[-1]])
                elif change["op"] == "remove":
                    del target[keys[-1]]
                else:
                    target[keys[-1]] = change["value"]
            obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
            obj["metadata"]["generation"] += 1
            obj["status"] = self.state(obj["spec"]["replicas"], obj["metadata"]["generation"])
        self.after_send(method, obj)
        return copy.deepcopy(obj)

    def start(self, apply=None):
        return flow.start(self.body, self.context, self.prepared, restructure.copies(self.body), self.read, ops, apply or self.apply)

    def poll(self):
        return ops.list_operations()[0]

    def complete(self, failed=False):
        next(iter(self.jobs.values()))["status"] = {"conditions": [{"type": "Failed" if failed else "Complete", "status": "True"}]}

    def test_success_has_durable_intents_fresh_checks_and_waits_for_readiness(self):
        result = self.start()
        self.assertNotIn("_held_receipt", result)
        self.assertIn("storage copy", guard.pending(self.dep, "lab", self.read))
        self.assertEqual("running", self.poll()["status"])
        self.assertEqual(1, self.admission.call_count)
        self.complete()
        self.assertEqual("running", self.poll()["status"])
        self.assertEqual(2, self.admission.call_count)
        self.assertEqual(1, self.dep["spec"]["replicas"])
        self.assertNotIn(flow.MARKER, self.dep["metadata"]["annotations"])
        self.assertEqual("succeeded", self.poll()["status"])
        self.assertNotIn("secret-not-in-journal", json.dumps(ops._read()))
        self.assertEqual(["PUT", "POST", "PATCH"], [row[0] for row in self.sent])
        helper = self.admission.call_args_list[0].args[0]
        self.assertEqual("64Mi", helper["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"])
        self.assertEqual("", guard.pending(self.admission.call_args_list[1].args[0], "lab", self.read))

    def test_capacity_changed_during_copy_leaves_app_stopped_and_data_retained(self):
        self.start(); self.poll(); self.complete()
        self.admission.return_value = {"blocked": True}
        job = self.poll()
        self.assertEqual("failed", job["status"])
        self.assertIn("Data copied", job["message"])
        self.assertEqual(0, self.dep["spec"]["replicas"])
        self.assertEqual(["PUT", "POST"], [row[0] for row in self.sent])
        self.assertTrue(job["cleanable"])

    def test_helper_capacity_blocks_before_creating_job(self):
        self.start()
        self.admission.return_value = {"blocked": True}
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual({}, self.jobs)

    def test_other_workload_copy_cannot_reserve_same_claim_before_pods_exist(self):
        self.start()
        other = copy.deepcopy(ops._read()[0]["ref"])
        other.update(name="other-app", review_digest="b" * 64, moves=[{"from": "different", "to": "new"}])
        with self.assertRaisesRegex(ValueError, "still active or needs recovery"):
            ops.start(flow.KIND, "Other copy", {}, "/containers", other)
        self.assertEqual({}, self.jobs)

    def test_changed_autostart_control_does_not_get_overridden(self):
        self.start()
        self.dep["metadata"]["annotations"]["homestead.io/autostart-replicas"] = "2"
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual({}, self.jobs)

    def test_changed_copy_template_is_not_trusted(self):
        self.start(); self.poll(); self.complete()
        next(iter(self.jobs.values()))["spec"]["template"]["spec"]["containers"][0]["image"] = "another/image"
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_consumer_appearing_during_final_admission_blocks_restart(self):
        self.start(); self.poll(); self.complete()
        def admission(_):
            self.pods = [{"metadata": {"namespace": "lab"}, "status": {"phase": "Running"},
                          "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "new"}}]}}]
            return {"blocked": False}
        self.admission.side_effect = admission
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_memory_warning_acceptance_does_not_override_hard_blockers(self):
        self.admission.return_value = {"blocked": False, "requires_confirmation": True, "warnings": ["Memory pressure"]}
        self.start(); self.poll(); self.complete(); self.poll()
        self.assertEqual(1, self.dep["spec"]["replicas"])

    def test_terminating_original_pods_wait_for_real_ownership_not_zero_counters(self):
        self.start()
        self.sets = [{"metadata": {"namespace": "lab", "uid": "rs", "ownerReferences": [{"kind": "Deployment", "uid": "dep-uid", "controller": True}]}}]
        self.pods = [{"metadata": {"namespace": "lab", "deletionTimestamp": "now", "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs", "controller": True}]}, "status": {"phase": "Running"}}]
        self.assertEqual("running", self.poll()["status"])
        self.assertEqual({}, self.jobs)

    def test_copy_complete_waits_for_helper_pods_to_finish(self):
        self.start(); self.poll(); self.complete()
        self.pods = [{"metadata": {"namespace": "lab", "ownerReferences": [{"kind": "Job", "uid": "copy-uid", "controller": True}]}, "status": {"phase": "Running"}}]
        self.assertEqual("running", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_other_pvc_consumers_block_copy(self):
        self.start()
        self.pods = [{"metadata": {"namespace": "lab"}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "old"}}]}, "status": {"phase": "Running"}}]
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual({}, self.jobs)

    def test_uid_replacement_and_config_change_never_restart(self):
        self.start(); self.poll(); self.complete()
        self.dep["metadata"]["uid"] = "foreign"
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_replaced_or_rebound_claim_fails_closed(self):
        self.start(); self.poll(); self.complete()
        self.pvcs["new"]["spec"]["volumeName"] = "different-pv"
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_replaced_pv_with_same_name_is_not_original_data(self):
        self.start(); self.poll(); self.complete()
        self.pvs["pv-old"]["metadata"]["uid"] = "replacement"
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_consumed_review_cannot_be_replayed_after_history_clear(self):
        original = copy.deepcopy(self.dep)
        self.start(); self.poll(); self.complete(); self.poll(); self.poll()
        ops._write([])
        self.dep = original
        before = len(self.sent)
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.start()
        self.assertEqual(before, len(self.sent))

    def test_unreviewed_setup_never_writes_or_allocates_job(self):
        self.body["confirm_capacity"] = False
        with self.assertRaisesRegex(ValueError, "Review"):
            self.start()
        self.assertEqual([], ops._read())
        self.assertEqual([], self.sent)

    def test_hold_cannot_be_released_by_an_operator(self):
        result = self.start(); self.admission.return_value = {"blocked": True}; self.poll()
        with self.assertRaises(PermissionError):
            ops.cancel(result["operation"]["id"], {"ack": True}, "app", allowed=lambda role: role != "admin")
        self.assertIn(flow.MARKER, self.dep["metadata"]["annotations"])

    def test_late_autoscaler_blocks_helper(self):
        self.start()
        self.hpas = [{"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "app"}}}]
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual({}, self.jobs)

    def test_job_name_reuse_is_not_copy_evidence(self):
        self.start(); self.poll(); self.complete()
        next(iter(self.jobs.values()))["metadata"]["uid"] = "foreign"
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_missing_job_does_not_restart_or_recreate_copy(self):
        self.start(); self.poll(); self.jobs.clear()
        self.assertEqual("failed", self.poll()["status"])
        self.poll()
        self.assertEqual(["PUT", "POST"], [row[0] for row in self.sent])

    def test_lost_copy_response_preserves_intent_without_retry_or_name_adoption(self):
        self.start()
        self.after_send = lambda *args: (_ for _ in ()).throw(TimeoutError("secret-not-in-journal"))
        self.assertEqual("failed", self.poll()["status"])
        self.poll()
        saved = ops._read()[0]
        self.assertEqual("copy_intent", saved["ref"]["phase"])
        self.assertFalse(flow.recovery_plan(saved, self.read, ops)["can"])
        self.assertEqual(["PUT", "POST"], [row[0] for row in self.sent])
        self.assertNotIn("secret-not-in-journal", json.dumps(saved))

    def test_lost_restart_response_is_never_resent_or_rolled_back(self):
        self.start(); self.poll(); self.complete()
        self.after_send = lambda *args: (_ for _ in ()).throw(TimeoutError("private upstream body"))
        self.assertEqual("failed", self.poll()["status"])
        self.poll()
        self.assertEqual("release_intent", ops._read()[0]["ref"]["phase"])
        self.assertEqual(["PUT", "POST", "PATCH"], [row[0] for row in self.sent])
        self.assertEqual(1, self.dep["spec"]["replicas"])

    def test_failed_checkpoint_sends_no_copy_request(self):
        self.start()
        with mock.patch.object(ops, "checkpoint", side_effect=OSError("storage unavailable")):
            self.assertEqual("failed", self.poll()["status"])
        self.assertEqual({}, self.jobs)

    def test_partial_setup_is_journalled_and_never_resubmitted(self):
        def partial():
            self.apply()
            raise TimeoutError("private details")
        with self.assertRaisesRegex(ValueError, "No automatic rollback"):
            self.start(apply=partial)
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(["PUT"], [row[0] for row in self.sent])
        self.assertTrue(ops._read()[0]["ref"]["retain_resources"])

    def test_unrelated_claim_source_rejected_before_any_edit(self):
        self.body["containers"][0]["volumes"][0]["copy_from"]["claim"] = "unrelated"
        self.body["capacity_token"] = review.issue(self.body, self.context)
        with self.assertRaisesRegex(ValueError, "Copy source"):
            self.start()
        self.assertEqual([], self.sent)

    def test_active_copy_cannot_be_released_or_cancelled_into_a_restart(self):
        result = self.start(); self.poll()
        with self.assertRaisesRegex(ValueError, "still active"):
            ops.cancel(result["operation"]["id"], {"ack": True}, "app")
        self.assertEqual(0, self.dep["spec"]["replicas"])
        self.assertEqual(["PUT", "POST"], [row[0] for row in self.sent])

    def test_failed_copy_recovery_removes_hold_only_after_explicit_inspection(self):
        result = self.start(); self.poll(); self.complete(failed=True); self.poll()
        ident = result["operation"]["id"]
        with self.assertRaisesRegex(ValueError, "Acknowledge"):
            ops.cancel(ident, {}, "app")
        ops.cancel(ident, {"ack": True}, "app")
        self.assertNotIn(flow.MARKER, self.dep["metadata"]["annotations"])
        self.assertEqual(0, self.dep["spec"]["replicas"])
        self.assertEqual({"old", "new"}, set(self.pvcs))
        self.assertEqual(1, len(self.jobs))
        self.assertTrue(ops._read()[0]["tracking_stopped"])
        self.assertEqual("failed", ops._read()[0]["status"])

    def test_legacy_resolution_is_read_only(self):
        item = {"ref": {"namespace": "lab", "name": "app", "phase": "copying", "job": "old-copy"}}
        self.assertEqual("failed", flow.legacy_status(item)[0])
        self.assertTrue(item["ref"]["retain_resources"])
        self.assertEqual([], self.sent)
        self.dep["metadata"]["annotations"] = {restructure.HELD: "1"}
        self.assertIn("legacy storage-copy hold", guard.pending(self.dep, "lab", self.read))

    def test_unknown_phase_never_reports_success(self):
        self.start()
        items = ops._read(); items[0]["ref"]["phase"] = "wrong"; ops._write(items)
        self.assertEqual("failed", self.poll()["status"])

    def test_setup_in_progress_cannot_release_hold(self):
        self.start()
        item = ops._read()[0]
        with locks.worker_lock(item["id"], ops):
            self.assertFalse(flow.recovery_plan(item, self.read, ops)["can"])


if __name__ == "__main__":
    unittest.main()
