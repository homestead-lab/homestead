import copy
import json
import tempfile
import unittest
from unittest import mock

from test_reclass_handoff import CopyCluster
import homestead_reclass as rc
import homestead_operations as ops
import homestead_capacity_review as review
import homestead_storage_workflow as workflow
import homestead_storage_recovery as recovery
import server
import homestead_storage_admission as admission
import homestead_place as place
import homestead_storage_journal as journal
import homestead_storage_runtime as runtime
from test_storage_runtime import pod as runtime_pod


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cluster = CopyCluster()
        self.cluster.item.update(progress=5, resource={"name": "data", "namespace": "lab"}, title="Move data", href="/volumes")
        self.cluster.item["ref"].update(storage_protocol=1, handoff_phase="copy")
        self.warnings = ["Measured RAM is above the configured warning limit"]
        self.blocked = False
        self.runtime_check = None
        self.helper = lambda item, obj: {"blocked": self.blocked, "warnings": self.warnings}
        self.restart = lambda item, proposals: {"blocked": self.blocked, "warnings": self.warnings}
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(ops, "DATA_DIR", directory.name),
                      mock.patch.object(rc, "kget", self.cluster.read),
                      mock.patch.object(rc, "ksend", self.cluster.send),
                      mock.patch.object(rc, "ktext", return_value="==> verified\n"),
                      mock.patch.object(review, "_key", return_value=b"storage-recovery-fixture")):
            patch.start(); self.addCleanup(patch.stop)
        self.save()

    def save(self):
        with ops._lock: ops._write([self.cluster.item])

    def load(self):
        with ops._lock: self.cluster.item = ops._read()[0]
        return self.cluster.item

    def checkpoint(self, item):
        self.cluster.checkpoint(item)
        ops.checkpoint(item)

    def poll(self):
        with ops._lock:
            item = self.load()
            outcome = workflow.resolve(item, self.checkpoint, self.helper, self.restart, runtime_check=self.runtime_check)
            ops._finish(item, *outcome)
            self.save()
            return outcome

    def preview(self, actor="admin"):
        return recovery.preview("handoff", ops, self.cluster.read, actor, self.helper, self.restart, runtime_check=self.runtime_check)

    def act(self, action, preview=None, actor="admin", **overrides):
        value = preview or self.preview(actor)
        body = {"id": "handoff", "action": action, "capacity_token": value["tokens"].get(action), "confirm_capacity": True, **overrides}
        result = recovery.act(body, ops, self.cluster.read, actor, self.helper, self.restart, runtime_check=self.runtime_check)
        self.load()
        return result

    def awaiting_helper(self):
        self.assertEqual(12, self.poll()[1])
        self.assertEqual("failed", self.poll()[0])
        self.assertIn("capacity warnings", self.load()["ref"]["storage_hold"])

    def test_inspection_never_creates_volume_job_or_durable_intent(self):
        before, sent = copy.deepcopy(ops._read()), len(self.cluster.sent)
        with mock.patch.object(ops, "_write") as write, mock.patch.object(rc, "ksend") as send:
            result = self.preview()
        write.assert_not_called(); send.assert_not_called()
        self.assertEqual(before, ops._read())
        self.assertEqual(sent, len(self.cluster.sent))
        self.assertEqual("copy-volume", result["plan"]["next_write"]["step"])
        self.assertIn("pause", result["tokens"])
        self.assertNotIn("continue", result["tokens"])

    def test_steps_stay_on_stop_until_workload_controllers_are_quiescent(self):
        self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]["status"]["observedGeneration"] = 0
        self.assertEqual(8, self.poll()[1])
        steps = self.load()["steps"]
        self.assertEqual("stop", next(row["id"] for row in steps if row["state"] == "active"))

    def test_fresh_warning_free_admission_does_not_require_an_extra_confirmation(self):
        self.warnings = []
        self.assertEqual(12, self.poll()[1])
        self.assertEqual(20, self.poll()[1])
        self.assertNotIn("storage_hold", self.load()["ref"])
        self.assertNotIn("storage_approvals", self.load()["ref"])

    def test_pause_keeps_resources_and_continue_only_changes_history(self):
        before = len(self.cluster.sent)
        self.act("pause")
        item = self.load()
        self.assertEqual("failed", item["status"])
        self.assertTrue(item["ref"]["retain_resources"])
        self.assertIn("copy jobs may continue", item["message"])
        self.act("continue")
        self.assertEqual("running", self.load()["status"])
        self.assertEqual(before, len(self.cluster.sent))
        self.assertEqual(12, self.poll()[1])

    def test_review_explicitly_overrides_warning_but_not_hard_capacity_block(self):
        self.awaiting_helper()
        view = self.preview()
        self.assertTrue(view["plan"]["can_continue"])
        self.assertIn(self.warnings[0], view["plan"]["warnings"])
        self.blocked = True
        with self.assertRaises(ValueError): self.act("continue", view)
        self.assertFalse(self.preview()["plan"]["can_continue"])
        self.blocked = False
        with self.assertRaises(ValueError): self.act("continue", confirm_capacity=False)
        self.act("continue")
        self.assertEqual(20, self.poll()[1])

    def test_changed_actor_resource_or_new_warning_requires_new_review(self):
        self.awaiting_helper()
        view = self.preview()
        with self.assertRaises(ValueError): self.act("continue", view, actor="different-admin")
        self.warnings.append("Hardware inventory could not be checked")
        with self.assertRaises(ValueError): self.act("continue", view)
        view = self.preview()
        self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data-copy"]["metadata"]["resourceVersion"] = "2"
        with self.assertRaises(ValueError): self.act("continue", view)

    def test_new_warning_after_approval_holds_before_dispatch(self):
        self.awaiting_helper(); self.act("continue")
        before = len(self.cluster.sent)
        self.warnings.append("Node memory is unknown")
        self.assertEqual("failed", self.poll()[0])
        self.assertEqual(before, len(self.cluster.sent))

    def test_lost_response_is_inspectable_but_never_adopted_or_retried(self):
        self.awaiting_helper(); self.act("continue")
        self.cluster.lose_reply = len(self.cluster.sent) + 1
        self.assertEqual("failed", self.poll()[0])
        self.cluster.complete()
        before = len(self.cluster.sent)
        view = self.preview()
        self.assertFalse(view["plan"]["can_continue"])
        self.assertIn("no confirmed outcome", " ".join(view["plan"]["blockers"]))
        self.assertTrue(any(row["receipt"] == "uncertain" for row in view["plan"]["resources"]))
        with self.assertRaises(ValueError): self.act("continue", view)
        self.assertEqual(before, len(self.cluster.sent))

    def test_generic_resume_cancel_and_dismiss_cannot_bypass_review(self):
        self.awaiting_helper()
        item = self.load()
        public = ops._public(item)
        self.assertTrue(public["storage_recovery"])
        self.assertFalse(public["resumable"])
        self.assertFalse(public["cancellable"])
        with self.assertRaises(ValueError): ops.resume(item["id"])
        with self.assertRaises(ValueError): ops.dismiss(item["id"])
        self.act("continue")
        with self.assertRaises(ValueError): ops.cancel(item["id"])

    def test_replayed_or_expired_continue_cannot_authorize_another_attempt(self):
        self.awaiting_helper()
        view = self.preview()
        expiry = int(view["tokens"]["continue"].split(".")[0])
        with mock.patch.object(review.time, "time", return_value=expiry + 1):
            with self.assertRaises(ValueError): self.act("continue", view)
        self.act("continue", view)
        with self.assertRaises(ValueError): self.act("continue", view)
        self.act("pause")
        with self.assertRaises(ValueError): self.act("continue", view)

    def test_public_inspection_and_approvals_never_store_manifest_secrets(self):
        # Secrets in the copy command are represented only by a body hash in a
        # next-write preview, never persisted into approvals or public facts.
        self.awaiting_helper()
        original = rc.job_body
        def secret_body(*args):
            name, obj = original(*args)
            obj["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "PRIVATE", "value": "private-test-value"}]
            return name, obj
        with mock.patch.object(rc, "job_body", side_effect=secret_body):
            view = self.preview()
            self.assertNotIn("private-test-value", json.dumps(view))
            self.act("continue", view)
        self.assertNotIn("private-test-value", json.dumps(ops._read()))

    def test_legacy_job_is_not_silently_upgraded(self):
        self.cluster.item["ref"].pop("storage_protocol"); self.save()
        before = len(self.cluster.sent)
        with self.assertRaises(ValueError): self.preview()
        self.assertEqual(before, len(self.cluster.sent))

    def test_admin_routes_enforced_before_inspection_and_never_dispatch_cluster_writes(self):
        def call(endpoint, body, role):
            handler = object.__new__(server.H)
            handler.path, handler.command = "/api/operations/storage-recovery/" + endpoint, "POST"
            handler.headers = {"X-Homestead-Auth": "1"}
            handler._who = lambda: {"user": "admin", "role": role}
            handler._body = lambda: copy.deepcopy(body)
            handler._client_ip = lambda: "127.0.0.1"
            handler._send = mock.Mock()
            with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
                    mock.patch.object(server, "kget", self.cluster.read), \
                    mock.patch.object(server, "storage_helper_admission", self.helper), \
                    mock.patch.object(server, "storage_restart_admission", self.restart), \
                    mock.patch.object(rc, "ksend") as send:
                handler.do_POST()
                send.assert_not_called()
            return handler._send.call_args.args
        for role in ("viewer", "operator"):
            for endpoint in ("preview", "act"):
                self.assertEqual(403, call(endpoint, {"id": "handoff"}, role)[0])
        result = call("preview", {"id": "handoff"}, "admin")
        self.assertEqual(200, result[0], result)
        body = {"id": "handoff", "action": "pause", "capacity_token": result[1]["tokens"]["pause"], "confirm_capacity": True}
        self.assertEqual(200, call("act", body, "admin")[0])

    def test_full_orchestrator_uses_real_admission_and_separate_cutover_restart_reviews(self):
        own = runtime_pod("homestead")
        own["spec"]["nodeName"] = "a"
        own["spec"]["containers"][0]["resources"] = {"requests": {"memory": "64Mi", "cpu": "10m"}}
        base_read = self.cluster.read
        def read(path):
            if path == "/api/v1/namespaces/lab/pods/homestead": return copy.deepcopy(own)
            data = base_read(path)
            if path in ("/api/v1/pods", "/api/v1/namespaces/lab/pods"):
                data["items"].append(copy.deepcopy(own))
            return data
        self.cluster.read = read
        patch = mock.patch.object(rc, "kget", read); patch.start(); self.addCleanup(patch.stop)
        runtime.report(ops, read, "lab", "homestead", "homestead", "test-release")
        self.runtime_check = lambda: runtime.require(ops, read, "lab", "homestead", "homestead", "test-release")
        nodes = [{"name": "a", "status": "Ready", "schedulable": True, "hardware": {},
            "labels": {"kubernetes.io/hostname": "a"}, "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "100"},
            "mem_cap_gb": 8, "mem_used_gb": 1, "mem_metrics_available": True}]
        for name in ("old", "new"):
            path = "/apis/storage.k8s.io/v1/storageclasses/" + name
            obj = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
                "metadata": {"name": name, "uid": "class-" + name, "resourceVersion": "1"},
                "provisioner": "test.storage", "reclaimPolicy": "Delete", "volumeBindingMode": "WaitForFirstConsumer"}
            self.cluster.objects[path] = obj
            self.cluster.item["ref"]["review_fences"][path] = journal.identity(obj)
        self.save()
        self.helper = lambda item, manifest: place.manifest_plan(manifest, "lab", manifest["metadata"]["name"],
            pod_snapshot=self.cluster.read("/api/v1/pods")["items"], nodes_snapshot=nodes, read=self.cluster.read)
        self.restart = lambda item, proposals: admission.plan(item, proposals, self.cluster.read, nodes, 88)
        with mock.patch.object(place, "hardware_features", return_value=[]):
            self.awaiting_helper(); self.act("continue")
            self.assertEqual(20, self.poll()[1])
            self.cluster.complete()
            self.assertEqual(75, self.poll()[1])
            self.assertEqual("failed", self.poll()[0])
            self.assertEqual("cutover", self.preview()["plan"]["phase"])
            self.act("continue")
            for _ in range(15):
                self.poll()
                if self.load()["ref"].get("cutover_complete"): break
            self.assertTrue(self.load()["ref"].get("cutover_complete"))
            self.assertEqual("failed", self.poll()[0])
            self.assertEqual("restart", self.preview()["plan"]["phase"])
            nodes[0]["allocatable"]["memory"] = "512Mi"
            blocked = self.preview()
            self.assertFalse(blocked["plan"]["can_continue"])
            self.assertTrue(any("memory" in reason.lower() for reason in blocked["plan"]["blockers"]))
            nodes[0]["allocatable"]["memory"] = "8Gi"
            self.act("continue")
            self.assertEqual(93, self.poll()[1])
            self.assertEqual(96, self.poll()[1])
            dep = self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]
            dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 1,
                "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}
            self.assertEqual("succeeded", self.poll()[0])
            self.assertFalse(self.load()["ref"]["retain_resources"])
            self.assertEqual("Retain", self.cluster.objects["/api/v1/persistentvolumes/pv-data"]["spec"]["persistentVolumeReclaimPolicy"])
