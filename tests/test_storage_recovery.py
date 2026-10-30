import copy
import json
import tempfile
import unittest
import urllib.error
from unittest import mock

from test_reclass_handoff import CopyCluster
import homestead_reclass as rc
import homestead_operations as ops
from fakes.job_store import put, store_writes
import homestead_capacity_review as review
import homestead_storage_workflow as workflow
import homestead_storage_recovery as recovery
import homestead_reclass_handoff as handoff
import homestead_storage_conflicts as conflicts
import server
import homestead_storage_admission as admission
import homestead_place as place
import homestead_storage_journal as journal
import homestead_storage_runtime as runtime
from test_storage_runtime import pod as runtime_pod


class RecoveryFixture(unittest.TestCase):
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
        put(ops, [self.cluster.item])

    def load(self):
        self.cluster.item = ops.stored()[0]
        return self.cluster.item

    def checkpoint(self, item):
        self.cluster.checkpoint(item)
        ops.checkpoint(item)

    def poll(self):
        with ops.transaction() as tx:
            item = self.load()
            outcome = workflow.resolve(item, self.checkpoint, self.helper, self.restart, runtime_check=self.runtime_check)
            ops._finish(item, *outcome)
            tx.save([item])
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



class StorageRecoveryTests(RecoveryFixture):
    def test_inspection_never_creates_volume_job_or_durable_intent(self):
        before, sent = copy.deepcopy(ops.stored()), len(self.cluster.sent)
        with store_writes(ops) as written, mock.patch.object(rc, "ksend") as send:
            result = self.preview()
        self.assertEqual([], written); send.assert_not_called()
        self.assertEqual(before, ops.stored())
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
        self.assertNotIn("private-test-value", json.dumps(ops.stored()))

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
                    mock.patch.object(server.KUBE_HELPERS, "kget", self.cluster.read), \
                    mock.patch.object(server.STORAGE_ROUTES, "storage_helper_admission", self.helper), \
                    mock.patch.object(server.STORAGE_ROUTES, "storage_restart_admission", self.restart), \
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


DEPLOYMENT = "/apis/apps/v1/namespaces/lab/deployments/app"


class HeldMoveRecoveryTests(RecoveryFixture):
    """A held move has a way out: every wait ends, and retry, finish or roll back."""
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        patch = mock.patch.object(handoff, "CLOCK", lambda: self.now)
        patch.start(); self.addCleanup(patch.stop)

    def writes(self):
        return [(method, path) for method, path, _ in self.cluster.sent]

    def ready(self):
        dep = self.cluster.objects[DEPLOYMENT]
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 1,
                         "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}

    def running_copy(self):
        self.warnings = []
        self.assertEqual(12, self.poll()[1])
        self.assertEqual(20, self.poll()[1])
        job = self.cluster.objects["/apis/batch/v1/namespaces/lab/jobs/data-copy-job"]
        job["status"] = {"active": 1}
        self.cluster.add("v1", "pods", "copy-pod", "Pod", copy.deepcopy(job["spec"]["template"]["spec"]))
        pod = self.cluster.objects["/api/v1/namespaces/lab/pods/copy-pod"]
        pod["metadata"]["ownerReferences"] = [{"kind": "Job", "controller": True, "uid": job["metadata"]["uid"]}]
        pod["status"] = {"phase": "Running"}

    def to_cutover(self, until):
        self.warnings = []
        path = "/apis/storage.k8s.io/v1/storageclasses/new"
        self.cluster.objects[path] = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": "new", "uid": "class-new", "resourceVersion": "1"}, "reclaimPolicy": "Delete"}
        self.cluster.item["ref"]["review_fences"][path] = journal.identity(self.cluster.objects[path])
        self.save()
        self.poll(); self.poll(); self.cluster.complete()
        for _ in range(20):
            self.poll()
            if handoff._receipt(self.load(), until): return
        self.fail(f"{until} was never reached: {self.load()['message']}")

    # (a) every wait ends in a named hold

    def test_quiesce_wait_holds_after_its_deadline_and_names_what_it_waited_for(self):
        dep = self.cluster.objects[DEPLOYMENT]
        dep["status"]["observedGeneration"] = 0
        before = len(self.cluster.sent)
        self.assertEqual(("running", 8), self.poll()[:2])
        self.now += 599
        self.assertEqual("running", self.poll()[0])
        self.now += 2
        status, _, message = self.poll()
        self.assertEqual("failed", status)
        self.assertIn("workload controllers to finish stopping", message)
        self.assertIn("10 min", message)
        self.assertEqual(message, self.load()["ref"]["storage_hold"])
        self.assertEqual(before, len(self.cluster.sent))
        view = self.preview()
        self.assertTrue(view["plan"]["can_retry"])
        self.assertIn("retry", view["tokens"])
        self.act("retry", view)
        self.assertEqual(("running", 8), self.poll()[:2])  # the deadline starts again

    def test_copy_pod_that_never_starts_holds_instead_of_waiting_for_ever(self):
        self.warnings = []
        self.assertEqual(12, self.poll()[1])
        self.assertEqual(20, self.poll()[1])
        self.assertEqual("running", self.poll()[0])
        self.now += 601
        status, _, message = self.poll()
        self.assertEqual("failed", status)
        self.assertIn("copy pod to start", message)

    def test_a_running_copy_gets_a_size_based_deadline(self):
        self.running_copy()
        self.assertEqual("running", self.poll()[0])
        self.now += 3600 + 120 * 10 - 1   # 1 h + 2 min per GiB of a 10 GiB volume
        self.assertEqual("running", self.poll()[0])
        self.now += 2
        self.assertIn("copy to finish", self.poll()[2])
        self.assertEqual(1, sum(path.endswith("/jobs") for _, path in self.writes()))  # nothing deleted on its own

    def test_the_wait_clock_restarts_when_the_move_makes_progress(self):
        dep = self.cluster.objects[DEPLOYMENT]
        dep["status"]["observedGeneration"] = 0
        self.warnings = []
        self.assertEqual(8, self.poll()[1])
        self.now += 500
        dep["status"]["observedGeneration"] = dep["metadata"]["generation"]
        self.assertEqual(12, self.poll()[1])
        self.assertNotIn("storage_wait", self.load()["ref"])
        self.assertEqual(20, self.poll()[1])
        self.now += 200
        self.assertEqual("running", self.poll()[0])   # copy pod pending: a new 10 min wait
        self.now += 590
        self.assertEqual("running", self.poll()[0])

    def test_inspection_never_holds_on_an_overdue_wait(self):
        self.cluster.objects[DEPLOYMENT]["status"]["observedGeneration"] = 0
        self.poll()
        self.now += 601
        view = self.preview()
        self.assertEqual([], view["plan"]["blockers"])
        self.assertEqual("running", self.load()["status"])

    # (b) retry a rejected request; finish forward once the copy is verified

    def test_a_rejected_request_can_be_retried_after_review(self):
        self.warnings = []
        self.assertEqual(12, self.poll()[1])
        real = self.cluster.send
        def refuse(method, path, body, **kw):
            if method == "POST" and path.endswith("/jobs"):
                raise urllib.error.HTTPError(path, 422, "Unprocessable", {}, None)
            return real(method, path, body, **kw)
        with mock.patch.object(rc, "ksend", refuse):
            self.assertEqual("failed", self.poll()[0])
        view = self.preview()
        self.assertTrue(view["plan"]["can_retry"], view["plan"]["blockers"])
        self.assertTrue(view["plan"]["can_rollback"])
        self.act("retry", view)
        ref = self.load()["ref"]
        self.assertEqual(["refused"], [e["state"] for e in ref["storage_refused"]])
        self.assertFalse(handoff._receipt(self.load(), "copy-job"))
        self.assertEqual(20, self.poll()[1])
        self.assertEqual("accepted", handoff._receipt(self.load(), "copy-job")["state"])

    def test_a_request_with_no_confirmed_outcome_offers_neither_retry_nor_rollback(self):
        self.warnings = []
        self.poll()
        self.cluster.lose_reply = len(self.cluster.sent) + 1
        self.assertEqual("failed", self.poll()[0])
        plan = self.preview()["plan"]
        self.assertFalse(plan["can_retry"] or plan["can_finish"] or plan["can_rollback"])

    def test_finish_forward_is_offered_once_the_copy_is_verified(self):
        self.to_cutover("retain-original")
        self.act("pause")
        view = self.preview()
        self.assertTrue(view["plan"]["can_finish"])
        self.assertFalse(view["plan"]["can_retry"])
        self.assertTrue(view["plan"]["can_rollback"])   # the original claim was never released
        self.act("finish", view)
        self.assertEqual("running", self.load()["status"])

    # (b, c) roll back to the original volume and bring the app back up there

    def test_rollback_restarts_the_app_on_the_original_and_frees_the_volume(self):
        self.awaiting_helper()
        original = copy.deepcopy(self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"])
        view = self.preview()
        self.assertTrue(view["plan"]["can_rollback"])
        self.assertEqual("PUT", view["plan"]["rollback"]["next_write"]["method"])
        self.act("rollback", view)
        self.assertEqual("rollback", self.load()["ref"]["handoff_phase"])
        self.assertEqual("running", self.poll()[0])
        dep = self.cluster.objects[DEPLOYMENT]
        self.assertEqual(1, dep["spec"]["replicas"])
        self.assertNotIn("homestead.io/storage-copy-job", dep["metadata"].get("annotations", {}))
        self.assertEqual("running", self.poll()[0])   # waiting for readiness
        self.ready()
        status, _, message = self.poll()
        self.assertEqual("running", status)   # the app is up; now the copy is set aside
        self.assertEqual(("PATCH", "/api/v1/persistentvolumes/pv-copy"), self.writes()[-1])
        for _ in range(4):
            status, _, message = self.poll()
            if status != "running": break
        self.assertEqual("cancelled", status)
        self.assertIn("original volume", message)
        self.assertIn("data-copy", message)
        self.assertIn("free for a later move", message)
        self.assertIn("Retain", message)
        self.assertIn("old copy", message)
        item = self.load()
        self.assertFalse(item["ref"]["retain_resources"])
        self.assertFalse(conflicts.unresolved(item))
        self.assertEqual(original, self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"])
        # The copy's name is free, but its data is kept as an old copy, removable
        # only by the separate, confirmed old-copy action.
        self.assertNotIn("/api/v1/namespaces/lab/persistentvolumeclaims/data-copy", self.cluster.objects)
        pv = self.cluster.objects["/api/v1/persistentvolumes/pv-copy"]
        self.assertEqual("Retain", pv["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual("lab/data-copy", pv["metadata"]["annotations"][rc.OLD_COPY])
        self.assertEqual([("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/data-copy")],
                         [w for w in self.writes() if w[0] == "DELETE"])
        self.assertFalse(ops._public(item)["storage_recovery"])
        pv["status"] = {"phase": "Released"}
        with mock.patch.object(rc, "_items", return_value=[copy.deepcopy(pv)]):
            self.assertEqual(["pv-copy"], [row["pv"] for row in rc.old_copies()])
        with mock.patch.object(rc, "ksend") as send:
            self.assertTrue(rc.remove_old_copy("pv-copy", ops)["ok"])
        self.assertEqual("Delete", send.call_args.args[2]["spec"]["persistentVolumeReclaimPolicy"])

    def test_rollback_stops_a_running_copy_before_restarting_the_app(self):
        self.running_copy()
        self.assertEqual("running", self.poll()[0])
        self.act("pause")
        self.act("rollback")
        self.poll()
        self.assertEqual(("DELETE", "/apis/batch/v1/namespaces/lab/jobs/data-copy-job"), self.writes()[-1])
        self.assertEqual(0, self.cluster.objects[DEPLOYMENT]["spec"]["replicas"])
        self.poll()
        self.assertEqual(("PUT", DEPLOYMENT), self.writes()[-1])

    def test_rollback_is_refused_once_the_original_claim_was_released(self):
        self.to_cutover("delete-source")
        self.act("pause")
        view = self.preview()
        self.assertFalse(view["plan"]["can_rollback"])
        self.assertTrue(view["plan"]["can_finish"])
        self.assertNotIn("rollback", view["tokens"])
        with self.assertRaises(ValueError):
            self.act("rollback", view, capacity_token=view["tokens"]["finish"])

    def test_rollback_never_overwrites_a_workload_edited_while_it_was_held(self):
        self.awaiting_helper(); self.act("rollback")
        self.cluster.objects[DEPLOYMENT]["spec"]["template"]["spec"]["containers"][0]["image"] = "example/app:2"
        before = len(self.cluster.sent)
        self.assertEqual("failed", self.poll()[0])
        self.assertEqual(before, len(self.cluster.sent))
        self.assertEqual("example/app:2", self.cluster.objects[DEPLOYMENT]["spec"]["template"]["spec"]["containers"][0]["image"])

    def test_rollback_readiness_has_a_deadline_and_retry_keeps_rolling_back(self):
        self.awaiting_helper(); self.act("rollback")
        self.poll(); self.poll()
        self.now += 1799
        self.assertEqual("running", self.poll()[0])
        self.now += 2
        status, _, message = self.poll()
        self.assertEqual("failed", status)
        self.assertIn("app", message)
        self.assertIn("30 min", message)
        self.assertIn("already started again on the original volume", message)
        plan = self.preview()["plan"]
        self.assertTrue(plan["can_retry"])
        self.assertFalse(plan["can_finish"] or plan["can_rollback"])
        self.act("retry")
        self.ready()
        self.assertEqual("cancelled", self.until_done())

    def until_done(self):
        for _ in range(6):
            status = self.poll()[0]
            if status != "running": return status
        return status

    def test_rollback_counts_a_copy_job_removed_by_hand_once_its_pods_are_gone(self):
        self.running_copy()
        self.assertEqual("running", self.poll()[0])
        self.act("pause"); self.act("rollback")
        del self.cluster.objects["/apis/batch/v1/namespaces/lab/jobs/data-copy-job"]   # by hand; its pod lingers
        before = len(self.cluster.sent)
        self.assertEqual("running", self.poll()[0])
        self.assertEqual(before, len(self.cluster.sent))
        self.assertNotIn("copy_job_gone", self.load()["ref"])
        del self.cluster.objects["/api/v1/namespaces/lab/pods/copy-pod"]
        self.poll()
        self.assertIn("copy_job_gone", self.load()["ref"])
        self.assertEqual(("PUT", DEPLOYMENT), self.writes()[-1])
        self.assertFalse(any(path.endswith("/jobs/data-copy-job") for _, path in self.writes()))
        self.ready()
        self.assertEqual("cancelled", self.until_done())

    def test_retry_after_a_copy_run_hold_keeps_watching_the_same_job(self):
        self.running_copy()
        self.poll()
        self.now += 4801
        self.assertEqual("failed", self.poll()[0])
        self.act("retry")
        self.assertEqual("running", self.poll()[0])
        self.cluster.complete()
        self.assertEqual(75, self.poll()[1])
        self.assertEqual(1, sum(method == "POST" and path.endswith("/jobs") for method, path in self.writes()))
        self.assertEqual("data-copy-job-uid", self.load()["ref"]["copy_verified"]["job_uid"])

    def test_a_rejected_original_marker_still_allows_rollback(self):
        self.to_cutover("retain-copy")
        real = self.cluster.send
        def refuse(method, path, body, **kw):
            if method == "PATCH" and rc.OLD_COPY in json.dumps(body):
                raise urllib.error.HTTPError(path, 422, "Unprocessable", {}, None)
            return real(method, path, body, **kw)
        with mock.patch.object(rc, "ksend", refuse):
            self.assertEqual("failed", self.poll()[0])
        self.assertEqual("refused", handoff._receipt(self.load(), "mark-original")["state"])
        view = self.preview()
        self.assertTrue(view["plan"]["can_rollback"], view["plan"]["rollback"])
        self.act("rollback", view)
        self.poll(); self.poll()   # copy Job removed, then the app restarted
        self.ready()
        status = self.until_done()
        self.assertEqual("cancelled", status)
        self.assertIn("pv-data keeps the Retain reclaim policy", self.load()["message"])
        self.assertNotIn(rc.OLD_COPY, self.cluster.objects["/api/v1/persistentvolumes/pv-data"]["metadata"].get("annotations", {}))

    def test_rollback_from_a_partial_stop_restarts_only_what_was_stopped(self):
        ref = self.cluster.item["ref"]
        worker = self.cluster.add("apps/v1", "deployments", "worker", "Deployment", {"replicas": 1,
            "template": {"spec": {"containers": [{"name": "w", "image": "example/w:1"}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}})
        self.cluster.consumer("Deployment", "worker", replicas=1)
        self.cluster.objects[worker]["metadata"]["resourceVersion"] = "2"   # changed after review
        ref["handoff_phase"] = "stop"
        self.save()
        self.assertEqual("failed", self.poll()[0])
        self.assertIn("changed after review", self.load()["ref"]["storage_hold"])
        self.assertFalse(self.preview()["plan"]["can_retry"])
        self.act("rollback")
        self.poll()
        self.assertEqual(1, self.cluster.objects[DEPLOYMENT]["spec"]["replicas"])
        self.assertFalse(any(path == worker for _, path in self.writes()))
        self.ready()
        self.assertEqual("cancelled", self.poll()[0])
