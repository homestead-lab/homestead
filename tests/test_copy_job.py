import base64
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
from fakes.job_store import put
from fakes.kube import Kube
import homestead_kube as K
import homestead_capacity_review as review
import homestead_import_guard as guard
import homestead_resources as RES
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
        self.secrets = {}  # the copy's record of before; its writes are not in self.sent
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
        if "/secrets/" in path and name in self.secrets:
            return copy.deepcopy(self.secrets[name])
        if "/jobs/" in path and name in self.jobs:
            return copy.deepcopy(self.jobs[name])
        raise urllib.error.HTTPError(path, 404, "private API body", {}, None)

    def apply(self):
        self.assertEqual("preparing", ops.stored()[0]["ref"]["phase"])
        count = restructure.hold(self.prepared["deployment"])
        self.dep = copy.deepcopy(self.prepared["deployment"])
        self.dep["metadata"].update(resourceVersion="2", generation=2)
        self.dep["status"] = self.state(0, 2)
        self.sent.append(("PUT", "Deployment/app", None))
        return {"ok": True, "name": "app", "held_replicas": count, "_held_receipt": copy.deepcopy(self.dep)}

    def send(self, method, path, body, **kwargs):
        if "/secrets" in path:
            if method == "POST":
                self.secrets[body["metadata"]["name"]] = obj = {**copy.deepcopy(body), "metadata": {**body["metadata"], "uid": "record-uid", "resourceVersion": "1"}}
                return copy.deepcopy(obj)
            return self.secrets.pop(path.rsplit("/", 1)[-1])
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            self.assertEqual("copy_intent", ops.stored()[0]["ref"]["phase"])
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
        return flow.start(self.body, self.context, self.prepared, restructure.copies(self.body), self.read, ops, apply or self.apply,
                          send=self.send)

    def poll(self):
        return ops.list_operations()[0]

    def complete(self, failed=False):
        next(iter(self.jobs.values()))["status"] = {"conditions": [{"type": "Failed" if failed else "Complete", "status": "True"}]}

    def fail_kept_stopped(self):
        """A failed copy whose edit can't be undone (no record of before): the hold stays for recovery."""
        self.secrets.clear()
        self.complete(failed=True)

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
        self.assertNotIn("secret-not-in-journal", json.dumps(ops.stored()))
        self.assertEqual(["PUT", "POST", "PATCH"], [row[0] for row in self.sent])
        helper = self.admission.call_args_list[0].args[0]
        self.assertEqual("64Mi", helper["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"])
        self.assertEqual("", guard.pending(self.admission.call_args_list[1].args[0], "lab", self.read))

    def guarded(self):
        """The write guard in front of this send, as in the app (#621)."""
        import homestead_changes as CH
        import homestead_kube as K
        send = self.send

        def guarded(method, path, body, **kwargs):
            CH.note_write(method, path, body)
            return send(method, path, body, **kwargs)
        self.send = guarded
        for patch in (mock.patch.object(CH, "_author_key", lambda: b"copy-mark"), mock.patch.object(K, "kget", self.read)):
            patch.start()
            self.addCleanup(patch.stop)
        return CH

    def test_the_restart_carries_the_change_mark_and_its_receipt_still_passes(self):
        CH = self.guarded()
        self.start(); self.poll(); self.complete()
        self.assertEqual("running", self.poll()["status"])
        self.assertEqual("succeeded", self.poll()["status"])
        self.assertEqual(1, self.dep["spec"]["replicas"])
        self.assertNotIn(flow.MARKER, self.dep["metadata"]["annotations"])
        self.assertIn(CH.AUTHOR, self.dep["metadata"]["annotations"])
        self.assertEqual(CH._AUTHOR_POINTER, self.sent[-1][2][-1]["path"])

    def test_releasing_the_hold_carries_the_change_mark(self):
        CH = self.guarded()
        result = self.start(); self.poll(); self.fail_kept_stopped(); self.poll()
        ops.cancel(result["operation"]["id"], {"ack": True}, "app", by="ada")
        CH.REQUEST.user = None   # the jobs loop's thread serves no request
        self.addCleanup(setattr, CH.REQUEST, "user", None)
        self.poll()  # the jobs loop applies a copy job's cancel (#622)
        self.assertNotIn(flow.MARKER, self.dep["metadata"]["annotations"])
        self.assertEqual(0, self.dep["spec"]["replicas"])
        mark = json.loads(self.dep["metadata"]["annotations"][CH.AUTHOR])
        meta = self.dep["metadata"]
        self.assertEqual(CH._signed(b"copy-mark", meta["namespace"], meta["name"], mark["at"], "ada"), mark["by"],
                         "the release is credited to whoever cancelled, not to the job")
        self.assertIsNone(CH.REQUEST.user, "the loop thread is left serving no one")

    def restart_mark(self, CH):
        self.poll(); self.complete()
        CH.REQUEST.user = None   # the jobs loop's thread serves no request
        self.assertEqual("running", self.poll()["status"])
        mark = json.loads(self.dep["metadata"]["annotations"][CH.AUTHOR])
        meta = self.dep["metadata"]
        self.assertIsNone(CH.REQUEST.user, "the loop thread is left serving no one")
        return lambda who: CH._signed(b"copy-mark", meta["namespace"], meta["name"], mark["at"], who) == mark["by"]

    def test_the_restart_is_credited_to_whoever_started_the_copy(self):
        CH = self.guarded()
        self.addCleanup(setattr, CH.REQUEST, "user", None)
        CH.REQUEST.user = "ada"   # the request that started the copy
        self.start()
        self.assertTrue(self.restart_mark(CH)("ada"), "the restart is ada's, not the job's")

    def test_a_copy_journalled_before_this_restarts_as_the_job(self):
        CH = self.guarded()
        self.addCleanup(setattr, CH.REQUEST, "user", None)
        CH.REQUEST.user = "ada"
        self.start()
        with ops.transaction() as tx:   # an older Homestead recorded no starter
            tx.items[0]["ref"].pop("started_by", None)
            tx.save()
        self.assertTrue(self.restart_mark(CH)(""), "no starter on record: credited to the job")

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
        other = copy.deepcopy(ops.stored()[0]["ref"])
        other.update(name="other-app", review_digest="b" * 64, moves=[{"from": "different", "to": "new"}])
        with self.assertRaisesRegex(ValueError, "still running or needs recovery"):
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
        self.pvs["pv-old"]["metadata"]["uid"] = "replacing it"
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(0, self.dep["spec"]["replicas"])

    def test_consumed_review_cannot_be_replayed_after_history_clear(self):
        original = copy.deepcopy(self.dep)
        self.start(); self.poll(); self.complete(); self.poll(); self.poll()
        put(ops, [])
        self.dep = original
        before = len(self.sent)
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.start()
        self.assertEqual(before, len(self.sent))

    def test_unreviewed_setup_never_writes_or_allocates_job(self):
        self.body["confirm_capacity"] = False
        with self.assertRaisesRegex(ValueError, "Review"):
            self.start()
        self.assertEqual([], ops.stored())
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
        # A gone Job is never created again or read as done: the edit is undone (#1523).
        self.start(); self.poll(); self.jobs.clear()
        self.assertEqual("failed", self.poll()["status"])
        self.poll()
        self.assertEqual(["PUT", "POST", "PATCH"], [row[0] for row in self.sent])
        self.assertEqual("old", self.dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_lost_copy_response_preserves_intent_without_retry_or_name_adoption(self):
        self.start()
        self.after_send = lambda *args: (_ for _ in ()).throw(TimeoutError("secret-not-in-journal"))
        self.assertEqual("running", self.poll()["status"])  # not known yet, so not failed (#1523)
        self.assertEqual("copy_intent", ops.stored()[0]["ref"]["phase"])
        with mock.patch.object(flow, "CREATE_SETTLE", 0):
            self.poll()
        saved = ops.stored()[0]
        self.assertEqual("copy_unowned", saved["ref"]["phase"])  # waited for, never taken over
        self.assertNotIn("job_uid", saved["ref"])
        self.assertFalse(flow.recovery_plan(saved, self.read, ops)["can"])
        self.assertEqual(["PUT", "POST"], [row[0] for row in self.sent])
        self.assertNotIn("secret-not-in-journal", json.dumps(saved))

    def test_lost_restart_response_is_never_resent_or_rolled_back(self):
        self.start(); self.poll(); self.complete()
        self.after_send = lambda *args: (_ for _ in ()).throw(TimeoutError("private upstream body"))
        self.assertEqual("failed", self.poll()["status"])
        self.poll()
        self.assertEqual("release_intent", ops.stored()[0]["ref"]["phase"])
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
        with self.assertRaisesRegex(ValueError, "Nothing was rolled back"):
            self.start(apply=partial)
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(["PUT"], [row[0] for row in self.sent])
        self.assertTrue(ops.stored()[0]["ref"]["retain_resources"])

    def test_unrelated_claim_source_rejected_before_any_edit(self):
        self.body["containers"][0]["volumes"][0]["copy_from"]["claim"] = "unrelated"
        self.body["capacity_token"] = review.issue(self.body, self.context)
        with self.assertRaisesRegex(ValueError, "copy source"):
            self.start()
        self.assertEqual([], self.sent)

    def test_active_copy_cannot_be_released_or_cancelled_into_a_restart(self):
        result = self.start(); self.poll()
        with self.assertRaisesRegex(ValueError, "still active"):
            ops.cancel(result["operation"]["id"], {"ack": True}, "app")
        self.assertEqual(0, self.dep["spec"]["replicas"])
        self.assertEqual(["PUT", "POST"], [row[0] for row in self.sent])

    def test_failed_copy_recovery_removes_hold_only_after_explicit_inspection(self):
        result = self.start(); self.poll(); self.fail_kept_stopped(); self.poll()
        ident = result["operation"]["id"]
        # Cancels of copy jobs are applied by the jobs loop (#622): a refusal
        # shows on the job, which is left as it was.
        self.assertTrue(ops.cancel(ident, {}, "app")["accepted"])
        self.poll()
        self.assertIn("Acknowledge", ops.stored()[0]["message"])
        self.assertIn(flow.MARKER, self.dep["metadata"]["annotations"])
        ops.cancel(ident, {"ack": True}, "app")
        self.poll()
        self.assertNotIn(flow.MARKER, self.dep["metadata"]["annotations"])
        self.assertEqual(0, self.dep["spec"]["replicas"])
        self.assertEqual({"old", "new"}, set(self.pvcs))
        self.assertEqual(1, len(self.jobs))
        self.assertTrue(ops.stored()[0]["tracking_stopped"])
        self.assertEqual("failed", ops.stored()[0]["status"])

    def test_legacy_resolution_is_read_only(self):
        item = {"ref": {"namespace": "lab", "name": "app", "phase": "copying", "job": "old-copy"}}
        self.assertEqual("failed", flow.legacy_status(item)[0])
        self.assertTrue(item["ref"]["retain_resources"])
        self.assertEqual([], self.sent)
        self.dep["metadata"]["annotations"] = {restructure.HELD: "1"}
        self.assertIn("older storage copy hold", guard.pending(self.dep, "lab", self.read))

    def test_unknown_phase_never_reports_success(self):
        self.start()
        items = ops.stored(); items[0]["ref"]["phase"] = "wrong"; put(ops, items)
        self.assertEqual("failed", self.poll()["status"])

    def test_setup_in_progress_cannot_release_hold(self):
        self.start()
        item = ops.stored()[0]
        with locks.worker_lock(item["id"], ops):
            self.assertFalse(flow.recovery_plan(item, self.read, ops)["can"])


class Crash(BaseException):
    """Homestead stopping mid-step: nothing after it runs, the journal stays as it was."""


class OnTheSharedFake(unittest.TestCase):
    """A reviewed storage copy of app (2 copies, /data on volume old) on the
    shared fake, and what the tests below check it with."""
    DEP = "/apis/apps/v1/namespaces/lab/deployments/app"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        claim = lambda name, size: {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": "lab"},
            "spec": {"volumeName": "pv-" + name, **({"resources": {"requests": {"storage": size}}} if size else {})},
            **({"status": {"capacity": {"storage": size}}} if size else {})}
        volume = lambda name: {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "pv-" + name}}
        dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "app", "namespace": "lab", "generation": 1},
               "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "app"}}, "template": {
                   "metadata": {"labels": {"app": "app"}}, "spec": {
                       "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "old"}}],
                       "containers": [{"name": "main", "image": "example/app", "env": [{"name": "DB_PASSWORD", "value": "secret-not-in-journal"}],
                                       "volumeMounts": [{"name": "data", "mountPath": "/data", "subPath": "app"}]}]}}},
               "status": CopyJobTests.state(2, 1)}
        self.kube = Kube(dep, claim("old", "20Gi"), claim("new", "50Gi"), claim("unsized", ""),
                         volume("old"), volume("new"), volume("unsized"))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__)
        self.admission = mock.Mock(return_value={"blocked": False, "warnings": []})
        for patch in (mock.patch.object(review, "_key", return_value=b"copy-tests"),
                      mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {flow.KIND: lambda item: flow.resolve(item, K.kget, K.ksend, ops, self.admission)}),
                      mock.patch.dict(ops.CANCELLERS, {flow.KIND: (lambda item: flow.recovery_plan(item, K.kget, ops),
                          lambda item, options: flow.recovery_run(item, options, K.kget, K.ksend, ops))})):
            patch.start()
            self.addCleanup(patch.stop)
        ops.CLEANUPS.add(flow.KIND)
        self.edit("new", "")

    def edit(self, target, folder, source="old"):
        """The reviewed edit: /data moves to target/folder, with a new image and 3 replicas."""
        current = K.kget(self.DEP)
        self.body = {"ns": "lab", "name": "app", "containers": [{"original_name": "main", "name": "main", "volumes": [
            {"path": "/data", "kind": "existing", "source": target, "sub_path": folder, "copy_from": {"claim": source, "sub_path": "app"}}]}]}
        self.context = {"action": "edit", "uid": current["metadata"]["uid"], "resourceVersion": current["metadata"]["resourceVersion"]}
        self.body.update(capacity_token=review.issue(self.body, self.context), confirm_capacity=True)
        edited = copy.deepcopy(current)
        edited.pop("status")
        spec = edited["spec"]["template"]["spec"]
        spec["volumes"][0]["persistentVolumeClaim"]["claimName"] = target
        spec["containers"][0]["volumeMounts"][0]["subPath"] = folder
        spec["containers"][0]["image"] = "example/app:2"
        # Saving moved the password into the app's Secret, so the edited spec holds none.
        spec["containers"][0]["env"] = [{"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "app-env", "key": "app.DB_PASSWORD"}}}]
        edited["spec"]["replicas"] = 3
        self.prepared = {"deployment": edited, "claims": [], "seeds": [], "name": "app"}

    def controller(self):
        """What the Deployment controller would report once it caught up."""
        dep = self.kube.object(self.DEP)
        dep["status"] = CopyJobTests.state(dep["spec"]["replicas"], dep["metadata"]["generation"])
        self.kube.add(dep)

    def apply(self):
        count = restructure.hold(self.prepared["deployment"])
        K.ksend("PUT", self.DEP, self.prepared["deployment"])
        self.controller()
        return {"ok": True, "name": "app", "held_replicas": count, "_held_receipt": K.kget(self.DEP)}

    def poll(self):
        self.controller()
        return ops.list_operations()[0]

    def job(self):
        jobs = K.kget("/apis/batch/v1/namespaces/lab/jobs")["items"]
        self.assertEqual(1, len(jobs))
        return jobs[0]

    def started(self):
        flow.start(self.body, self.context, self.prepared, restructure.copies(self.body), K.kget, ops, self.apply, send=K.ksend)
        self.assertEqual("running", self.poll()["status"])  # stopping -> copy Job submitted
        return self.job()

    def finish(self, job, condition, reason=""):
        job["status"] = {"conditions": [{"type": condition, "status": "True", "reason": reason}]}
        self.kube.add(job)

    def assert_undone(self, replicas):
        dep = self.kube.object(self.DEP)
        self.assertEqual(replicas, dep["spec"]["replicas"])
        spec = dep["spec"]["template"]["spec"]
        self.assertEqual("old", spec["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual("app", spec["containers"][0]["volumeMounts"][0]["subPath"])
        self.assertEqual("example/app", spec["containers"][0]["image"])  # the whole edit is undone
        for key in (flow.MARKER, restructure.HELD):
            self.assertNotIn(key, dep["metadata"].get("annotations", {}))
        self.assertIsNone(self.kube.object(self.record_path()))  # the record goes once the undo is done
        self.assertEqual("", guard.pending(dep, "lab", K.kget))

    def assert_kept_stopped(self, result, *words):
        self.assertEqual("failed", result["status"])
        self.assertIn("app stays stopped", result["message"])
        self.assertNotIn("no rollback was attempted", result["message"])
        for word in words:
            self.assertIn(word, result["message"])
        self.assertEqual(0, self.kube.object(self.DEP)["spec"]["replicas"])

    def record_path(self):
        uid = self.kube.object(self.DEP)["metadata"]["uid"]
        return f"/api/v1/namespaces/lab/secrets/{flow.earlier_name(uid)}"


class CopyDeadlineTests(OnTheSharedFake):
    """A copy that never runs or never ends: the Job has a deadline, and its
    failure undoes the edit and starts the app again (on the shared fake)."""

    def test_deadline_grows_with_the_data_between_a_floor_and_a_ceiling(self):
        self.assertEqual(flow.DEADLINE_FLOOR, flow.deadline(0))
        self.assertEqual(flow.DEADLINE_FLOOR + 20 * flow.SECONDS_PER_GIB, flow.deadline(20))
        self.assertEqual(48 * 3600, flow.DEADLINE_CEILING)
        self.assertEqual(flow.DEADLINE_CEILING, flow.deadline(100000))
        self.assertEqual(flow.DEADLINE_CEILING, flow.deadline(None))  # size unknown

    def test_the_copy_job_carries_a_deadline_sized_from_the_source_data(self):
        job = self.started()
        self.assertEqual(flow.deadline(20), job["spec"]["activeDeadlineSeconds"])
        self.assertNotIn("secret-not-in-journal", json.dumps(ops.stored()))

    def test_a_source_of_unknown_size_gets_the_ceiling_and_says_so(self):
        dep = self.kube.object(self.DEP)
        dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "unsized"
        self.kube.add(dep)
        self.edit("new", "", source="unsized")
        job = self.started()
        self.assertEqual(flow.DEADLINE_CEILING, job["spec"]["activeDeadlineSeconds"])
        self.finish(job, "Failed", "DeadlineExceeded")
        self.assertIn("Copying data of unknown size did not finish within 48 hours", self.poll()["message"])

    def test_a_copy_pod_that_never_schedules_fails_after_the_deadline_and_the_edit_is_undone(self):
        job = self.started()
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "copy-pod", "namespace": "lab",
            "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": job["metadata"]["name"],
                                 "uid": job["metadata"]["uid"], "controller": True}]},
            "spec": {"volumes": [{"name": "v0", "persistentVolumeClaim": {"claimName": "new"}}]},
            "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]}})
        self.assertEqual("running", self.poll()["status"])
        self.assertEqual(0, self.kube.object(self.DEP)["spec"]["replicas"])
        # The deadline passes: the Job controller fails the Job and removes its pod.
        self.finish(job, "Failed", "DeadlineExceeded")
        self.kube.discard("/api/v1/namespaces/lab/pods/copy-pod")
        result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("Copying 20 GiB did not finish within 1.2 hours", result["message"])
        self.assertIn("started again with 2 replicas", result["message"])
        self.assert_undone(2)
        # Nothing is replayed on a later poll, and the new volume is kept.
        writes = len(self.kube.writes)
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(writes, len(self.kube.writes))
        self.assertIsNotNone(self.kube.object("/api/v1/namespaces/lab/persistentvolumeclaims/new"))

    def test_a_failed_copy_puts_a_stopped_app_back_stopped_and_says_so(self):
        dep = self.kube.object(self.DEP)
        dep["spec"]["replicas"] = 0
        self.kube.add(dep)
        self.edit("new", "")
        job = self.started()
        self.finish(job, "Failed", "BackoffLimitExceeded")
        result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("stays stopped, as it was before", result["message"])
        self.assert_undone(0)

    def test_no_restart_on_a_volume_the_copy_also_wrote_to(self):
        self.edit("old", "moved")  # a folder move inside the same volume
        job = self.started()
        self.finish(job, "Failed", "BackoffLimitExceeded")
        self.assert_kept_stopped(self.poll(), "old", "also wrote")

    def test_no_restart_when_the_recorded_configuration_was_changed(self):
        job = self.started()
        record = self.kube.object(self.record_path())
        text = base64.b64decode(record["data"]["earlier"]).decode().replace("example/app", "example/other")
        record["data"]["earlier"] = base64.b64encode(text.encode()).decode()
        self.kube.add(record)
        self.finish(job, "Failed", "BackoffLimitExceeded")
        self.assert_kept_stopped(self.poll(), "Edit it back")

    def resume_after_crash(self, applied):
        job = self.started()
        self.finish(job, "Failed", "BackoffLimitExceeded")

        def crash(who, write):
            if applied:
                write()
            raise Crash()
        with mock.patch.object(flow, "_as", crash), self.assertRaises(Crash):
            self.poll()
        self.assertEqual("restore_intent", ops.stored()[0]["ref"]["phase"])
        patches = lambda: [w for w in self.kube.writes if w[0] == "PATCH" and w[1] == self.DEP]
        sent = len(patches())
        result = self.poll()
        self.assertEqual(sent, len(patches()))  # the undo is never sent again
        return result

    def test_a_crash_after_the_undo_was_applied_finishes_as_undone(self):
        result = self.resume_after_crash(applied=True)
        self.assertEqual("failed", result["status"])
        self.assertIn("started again with 2 replicas", result["message"])
        self.assert_undone(2)

    def test_a_crash_before_the_undo_was_applied_fails_and_names_what_to_check(self):
        result = self.resume_after_crash(applied=False)
        self.assertEqual("failed", result["status"])
        self.assertIn("may not have been applied", result["message"])
        self.assertIn("pod template", result["message"])
        self.assertEqual(0, self.kube.object(self.DEP)["spec"]["replicas"])

    def test_a_normal_copy_still_starts_the_app_on_its_new_storage(self):
        job = self.started()
        self.finish(job, "Complete")
        self.assertEqual("running", self.poll()["status"])
        self.assertEqual("succeeded", self.poll()["status"])
        dep = self.kube.object(self.DEP)
        self.assertEqual(3, dep["spec"]["replicas"])
        self.assertEqual("new", dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual("example/app:2", dep["spec"]["template"]["spec"]["containers"][0]["image"])
        self.assertIsNone(self.kube.object(self.record_path()))  # the record goes once the app is back

    def test_the_record_of_before_is_a_secret_owned_by_the_app_and_never_in_its_view(self):
        self.started()
        record = self.kube.object(self.record_path())
        dep = self.kube.object(self.DEP)
        self.assertEqual([dep["metadata"]["uid"]], [o["uid"] for o in record["metadata"]["ownerReferences"]])
        self.assertEqual("app", record["metadata"]["labels"][flow.EARLIER_LABEL])
        self.assertIn("secret-not-in-journal", base64.b64decode(record["data"]["earlier"]).decode())
        self.assertNotIn("secret-not-in-journal", json.dumps(ops.stored()))
        RES.hide_sensitive(dep, "apps")
        self.assertNotIn("secret-not-in-journal", json.dumps(dep))  # what a viewer of the Deployment sees

    def test_no_copy_starts_when_the_record_of_before_cannot_be_written(self):
        self.kube.fail("/api/v1/namespaces/lab/secrets", 403, method="POST")
        with self.assertRaises(ValueError) as caught:
            flow.start(self.body, self.context, self.prepared, restructure.copies(self.body), K.kget, ops, self.apply, send=K.ksend)
        self.assertIn("still running", str(caught.exception))
        dep = self.kube.object(self.DEP)
        self.assertEqual(2, dep["spec"]["replicas"])
        self.assertEqual("example/app", dep["spec"]["template"]["spec"]["containers"][0]["image"])
        self.assertNotIn(flow.MARKER, dep["metadata"].get("annotations", {}))
        self.assertEqual("failed", ops.stored()[0]["status"])


JOBS = "/apis/batch/v1/namespaces/lab/jobs"


class WithoutTheCopyJob(OnTheSharedFake):
    """What the classes below use: a copy Job taken away from outside, its pods, the Check outcome plan."""

    def begin(self):
        """The edit saved and the app held stopped; the copy Job is not sent yet."""
        flow.start(self.body, self.context, self.prepared, restructure.copies(self.body), K.kget, ops, self.apply, send=K.ksend)

    def gone(self, job):
        """kubectl delete job (background): the Job goes, and the pods it owns with it."""
        self.kube.discard(f"{JOBS}/{job['metadata']['name']}")

    def copy_pod(self, job, owned=True):
        """A running pod of the copy, mounting both volumes. owned=False is a pod
        whose Job was deleted with --cascade=orphan: it keeps its labels only."""
        meta = {"name": "copy-pod", "namespace": "lab", "labels": job["spec"]["template"]["metadata"]["labels"]}
        if owned:
            meta["ownerReferences"] = [{"apiVersion": "batch/v1", "kind": "Job", "name": job["metadata"]["name"],
                                        "uid": job["metadata"]["uid"], "controller": True}]
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": meta,
                       "spec": {"volumes": [{"name": "v0", "persistentVolumeClaim": {"claimName": "new"}},
                                            {"name": "v1", "persistentVolumeClaim": {"claimName": "old"}}]},
                       "status": {"phase": "Running"}})

    def plan(self):
        return ops.cancel_plan(ops.stored()[0]["id"])

    def sent(self, method, path):
        return len([w for w in self.kube.writes if w[0] == method and w[1] == path])

    def assert_still_held(self):
        dep = self.kube.object(self.DEP)
        self.assertEqual(0, dep["spec"]["replicas"])
        self.assertEqual("new", dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual(0, self.sent("PATCH", self.DEP))


class CopyJobGoneTests(WithoutTheCopyJob):
    """The copy Job deleted while it copies, or after it ended and before
    Homestead read it (#1523): the edit is undone as for a failed copy, once no
    pod of the copy runs. What the Job did is never used."""

    def test_a_job_deleted_while_it_copies_undoes_the_edit(self):
        job = self.started()
        self.copy_pod(job)
        self.assertEqual("running", self.poll()["status"])
        self.gone(job)
        result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("The copy Job is gone", result["message"])
        self.assertIn(f"Job: lab/{job['metadata']['name']}", result["message"])
        self.assertIn("started again with 2 replicas", result["message"])
        self.assertIn("may hold only part of the data", result["message"])
        self.assertNotIn("copy Job were kept", result["message"])  # there is no Job to keep
        self.assert_undone(2)
        self.assertEqual("restored", ops.stored()[0]["ref"]["phase"])
        self.assertEqual(1, self.sent("POST", JOBS))  # never created again

    def test_a_complete_job_deleted_before_it_was_read_is_not_used(self):
        job = self.started()
        self.finish(job, "Complete")
        self.gone(job)
        result = self.poll()
        self.assertIn("is gone", result["message"])
        self.assert_undone(2)  # on the old volume: the new one is never started on

    def test_a_failed_job_deleted_before_it_was_read_undoes_the_edit(self):
        job = self.started()
        self.finish(job, "Failed", "BackoffLimitExceeded")
        self.gone(job)
        self.assertIn("is gone", self.poll()["message"])
        self.assert_undone(2)

    def test_an_orphaned_copy_pod_keeps_the_app_stopped_until_it_ends(self):
        job = self.started()
        self.gone(job)
        self.copy_pod(job, owned=False)
        result = self.poll()
        self.assertEqual("running", result["status"])
        self.assertIn("1 copy pod is still running: copy-pod", result["message"])
        self.assert_still_held()
        self.assertFalse(self.plan()["can"])
        self.kube.discard("/api/v1/namespaces/lab/pods/copy-pod")
        self.assertEqual("failed", self.poll()["status"])
        self.assert_undone(2)

    def test_a_copy_pod_still_ending_after_a_background_delete_keeps_the_app_stopped(self):
        """kubectl delete job (background): the Job answers 404 at once, and its
        pod runs on for the grace period, still owned by the gone Job's uid."""
        job = self.started()
        self.gone(job)
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": "copy-pod", "namespace": "lab", "labels": {"other": "labels"},  # only the uid finds it
            "deletionTimestamp": "2026-11-22T00:00:00Z",
            "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": job["metadata"]["name"],
                                 "uid": job["metadata"]["uid"], "controller": True}]},
            "spec": {"volumes": [{"name": "v0", "persistentVolumeClaim": {"claimName": "new"}}]},
            "status": {"phase": "Running"}})
        result = self.poll()
        self.assertEqual("running", result["status"], result["message"])
        self.assertIn("1 copy pod is still running: copy-pod", result["message"])
        self.assert_still_held()
        self.kube.discard("/api/v1/namespaces/lab/pods/copy-pod")
        self.assertEqual("failed", self.poll()["status"])
        self.assert_undone(2)

    def test_another_pod_on_the_volumes_keeps_it_stopped_with_words(self):
        job = self.started()
        self.gone(job)
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "other", "namespace": "lab"},
                       "spec": {"volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "old"}}]},
                       "status": {"phase": "Running"}})
        self.assert_kept_stopped(self.poll(), "is gone", "another running pod uses a copy volume")
        self.assert_still_held()

    def test_a_deleted_source_volume_keeps_it_stopped_with_words(self):
        job = self.started()
        self.kube.discard("/api/v1/namespaces/lab/persistentvolumeclaims/old")
        self.gone(job)
        self.assert_kept_stopped(self.poll(), "is gone", "the app or a volume of the copy is gone")
        self.assert_still_held()

    def undo_cut_off(self, applied):
        """Homestead stops while it sends the undo; the next poll looks and never sends it again."""
        job = self.started()
        self.gone(job)

        def crash(who, write):
            if applied:
                write()
            raise Crash()
        with mock.patch.object(flow, "_as", crash), self.assertRaises(Crash):
            self.poll()
        self.assertEqual("restore_intent", ops.stored()[0]["ref"]["phase"])
        sent = self.sent("PATCH", self.DEP)
        result = self.poll()
        self.assertEqual(sent, self.sent("PATCH", self.DEP))
        self.assertEqual("failed", self.poll()["status"])
        self.assertEqual(sent, self.sent("PATCH", self.DEP))
        return result

    def test_a_restart_after_the_undo_was_sent_finishes_as_undone(self):
        result = self.undo_cut_off(applied=True)
        self.assertIn("is gone", result["message"])
        self.assertIn("started again with 2 replicas", result["message"])
        self.assertIn("may hold only part of the data", result["message"])
        self.assert_undone(2)

    def test_a_restart_before_the_undo_was_sent_never_sends_it(self):
        result = self.undo_cut_off(applied=False)
        self.assertIn("may not have been applied", result["message"])
        self.assert_still_held()


class CopyReceiptLostTests(WithoutTheCopyJob):
    """Homestead restarted, or lost the answer, between recording the copy Job
    and saving its uid (#1523). The create is never sent again and a Job of the
    same name is never taken over: it is waited for, and its result is not used."""

    def cut_off_create(self, applied):
        self.begin()
        self.kube.fail(JOBS, Crash(), method="POST", applied=applied)
        with self.assertRaises(Crash):
            self.poll()
        self.assertEqual("copy_intent", ops.stored()[0]["ref"]["phase"])

    def test_a_restart_with_no_record_and_no_job_waits_then_undoes_the_edit(self):
        self.cut_off_create(applied=False)
        result = self.poll()  # a create sent just before the restart may still be done
        self.assertEqual("running", result["status"])
        self.assertIn("is checking whether the copy Job", result["message"])
        self.assert_still_held()
        with mock.patch.object(flow, "CREATE_SETTLE", 0):
            result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("no Job of its name exists", result["message"])
        self.assert_undone(2)
        self.assertEqual(1, self.sent("POST", JOBS))

    def test_a_restart_with_no_record_and_a_running_job_waits_and_never_adopts_it(self):
        self.cut_off_create(applied=True)
        self.assertIn("is checking whether the copy Job", self.poll()["message"])
        with mock.patch.object(flow, "CREATE_SETTLE", 0):
            result = self.poll()
            self.assertEqual("running", result["status"])
            self.assertIn("still active", result["message"])
            ref = ops.stored()[0]["ref"]
            self.assertEqual("copy_unowned", ref["phase"])
            self.assertNotIn("job_uid", ref)
            self.assert_still_held()
            self.assertFalse(self.plan()["can"])
            self.finish(self.job(), "Complete")
            result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("its result is not used", result["message"])
        self.assert_undone(2)
        self.job()  # kept, not deleted
        self.assertEqual(1, self.sent("POST", JOBS))

    def test_a_complete_job_of_the_same_name_without_a_record_is_never_used(self):
        self.cut_off_create(applied=True)
        self.finish(self.job(), "Complete")  # someone else's, or ours with its answer lost
        self.assertIn("is checking whether the copy Job", self.poll()["message"])
        self.assert_still_held()
        with mock.patch.object(flow, "CREATE_SETTLE", 0):
            result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("its result is not used", result["message"])
        self.assert_undone(2)  # never started on the new volume
        self.assertEqual("old", self.kube.object(self.DEP)["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_a_lost_answer_is_never_sent_again(self):
        self.begin()
        self.kube.fail(JOBS, TimeoutError("lost"), method="POST", applied=True)
        self.assertEqual("running", self.poll()["status"])
        with mock.patch.object(flow, "CREATE_SETTLE", 0):
            self.assertEqual("running", self.poll()["status"])
        self.assertEqual("copy_unowned", ops.stored()[0]["ref"]["phase"])
        self.assertEqual(1, self.sent("POST", JOBS))


class CopyCreateRefusedTests(WithoutTheCopyJob):
    """The copy Job's create answered with an error (#1523). An answer that says
    it was not created undoes the edit at once; a 409 is checked by name first."""

    def test_a_create_refused_with_403_undoes_the_edit_at_once(self):
        self.begin()
        self.kube.fail(JOBS, 403, method="POST")
        result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("Kubernetes refused to create the copy Job (HTTP 403)", result["message"])
        self.assertNotIn("part of the data", result["message"])  # nothing was copied
        self.assert_undone(2)
        self.assertEqual([], K.kget(JOBS)["items"])

    def test_a_create_answered_409_is_checked_by_name_before_the_undo(self):
        self.begin()
        self.kube.fail(JOBS, 409, method="POST")
        self.assertEqual("running", self.poll()["status"])
        self.assert_still_held()
        with mock.patch.object(flow, "CREATE_SETTLE", 0):
            result = self.poll()
        self.assertIn("no Job of its name exists", result["message"])
        self.assert_undone(2)


class CopyGoneReleaseTests(WithoutTheCopyJob):
    """When the undo is refused, Release hold stays the way out, but only once no pod of the copy runs."""

    def refused_undo(self):
        job = self.started()
        self.kube.discard(self.record_path())  # nothing to undo the edit with
        self.gone(job)
        return job

    def test_release_hold_is_offered_when_the_undo_refused_and_no_copy_pod_runs(self):
        self.refused_undo()
        self.assert_kept_stopped(self.poll(), "is gone", "Edit it back")
        self.assertTrue(self.plan()["can"])
        ops.cancel(ops.stored()[0]["id"], {"ack": True}, "app")
        self.poll()  # the jobs loop applies a copy job's cancel
        dep = self.kube.object(self.DEP)
        self.assertNotIn(flow.MARKER, dep["metadata"].get("annotations", {}))
        self.assertEqual(0, dep["spec"]["replicas"])  # released, not started

    def test_release_hold_waits_while_a_copy_pod_runs(self):
        job = self.refused_undo()
        self.copy_pod(job, owned=False)
        self.assertEqual("running", self.poll()["status"])
        plan = self.plan()
        self.assertFalse(plan["can"])
        self.assertIn("1 copy pod is still running: copy-pod", plan["why_not"])
        self.kube.discard("/api/v1/namespaces/lab/pods/copy-pod")
        self.assert_kept_stopped(self.poll(), "Edit it back")
        self.assertTrue(self.plan()["can"])


class CopyGoneBlipTests(WithoutTheCopyJob):
    """A read that fails in passing on the new paths is asked again on the next
    poll (#1523), so the undo still comes; a refused pod list keeps the app
    stopped with words, and Release hold is not offered."""

    def test_a_passing_error_on_the_job_read_still_ends_in_the_undo(self):
        job = self.started()
        self.gone(job)
        self.kube.fail(f"{JOBS}/{job['metadata']['name']}", 500, method="GET")
        result = self.poll()
        self.assertEqual("running", result["status"])
        self.assert_still_held()
        self.assertEqual("failed", self.poll()["status"])
        self.assert_undone(2)

    def test_a_passing_error_on_the_name_check_still_ends_in_the_undo(self):
        self.begin()
        self.kube.fail(JOBS, Crash(), method="POST")
        with self.assertRaises(Crash):
            self.poll()
        with ops.transaction() as tx:  # the wait for a late create has passed
            tx.items[0]["ref"]["intent_at"] = 0
            tx.save()
        self.kube.fail(f"{JOBS}/{ops.stored()[0]['ref']['job']}", TimeoutError("timed out"), method="GET")
        self.assertEqual("running", self.poll()["status"])  # asked again, not failed for good
        self.assert_still_held()
        self.poll()
        self.assert_undone(2)

    def refused_then_passing(self):
        """The create refused (403), then the record of before cannot be read for a moment."""
        self.begin()
        self.kube.fail(JOBS, 403, method="POST")
        self.kube.fail(self.record_path(), 503, method="GET")
        self.assertEqual("running", self.poll()["status"])
        self.assert_still_held()

    def test_a_passing_error_after_a_refused_create_keeps_the_refusal(self):
        self.refused_then_passing()
        result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("Kubernetes refused to create the copy Job (HTTP 403)", result["message"])
        self.assertNotIn("part of the data", result["message"])
        self.assert_undone(2)

    def test_a_same_name_job_after_a_refused_create_is_waited_for_and_warned_of(self):
        self.refused_then_passing()
        name = ops.stored()[0]["ref"]["job"]
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "lab"},
                       "spec": {"template": {"metadata": {"labels": {}}, "spec": {"containers": []}}}, "status": {"active": 1}})
        result = self.poll()
        self.assertEqual("running", result["status"])
        self.assertIn("its result is not used", result["message"])
        ref = ops.stored()[0]["ref"]
        self.assertEqual("copy_unowned", ref["phase"])
        self.assertNotIn("copy_refused", ref)
        self.assertIn("The end of the copy was not confirmed", self.plan()["lead"])
        self.assert_still_held()

    def test_a_passing_error_on_the_pod_list_still_ends_in_the_undo(self):
        job = self.started()
        self.gone(job)
        self.kube.fail("/api/v1/pods", 503, method="GET")
        self.assertEqual("running", self.poll()["status"])
        self.assert_still_held()
        self.assertEqual("failed", self.poll()["status"])
        self.assert_undone(2)

    def test_a_refused_pod_list_keeps_it_stopped_and_release_is_not_offered(self):
        job = self.started()
        self.gone(job)
        self.kube.fail("/api/v1/pods", 403, method="GET")
        self.assert_kept_stopped(self.poll(), "pods could not be read (HTTP 403)", "Let Homestead read pods")
        self.assert_still_held()
        self.kube.fail("/api/v1/pods", 403, method="GET")
        plan = self.plan()
        self.assertFalse(plan["can"])
        self.assertIn("pods could not be read (HTTP 403)", plan["why_not"])
        self.assertTrue(self.plan()["can"])  # once pods can be read again, and none of the copy runs


class CopyReleaseReviewTests(WithoutTheCopyJob):
    refused_undo = CopyGoneReleaseTests.refused_undo
    """Release hold says the end of the copy was not confirmed when the Job is
    gone, replaced or has no record (#1523), and only then."""

    def test_the_review_says_so_for_a_gone_job(self):
        self.refused_undo()
        self.poll()
        self.assertIn("The end of the copy was not confirmed", self.plan()["lead"])

    def test_release_hold_for_a_replaced_job_says_so_and_is_offered(self):
        job = self.started()
        self.gone(job)
        self.kube.add({**job, "metadata": {**job["metadata"], "uid": "someone-else"},
                       "status": {"conditions": [{"type": "Complete", "status": "True"}]}})
        result = self.poll()
        self.assertEqual("failed", result["status"])
        self.assertIn("replaced", result["message"])
        self.assert_still_held()  # never started on the new volume
        plan = self.plan()
        self.assertTrue(plan["can"])
        self.assertIn("The end of the copy was not confirmed", plan["lead"])

    def test_the_review_says_so_for_a_job_without_a_record(self):
        self.begin()
        self.kube.fail(JOBS, Crash(), method="POST", applied=True)
        with self.assertRaises(Crash):
            self.poll()
        self.assertIn("is checking whether the copy Job", self.poll()["message"])
        with mock.patch.object(flow, "CREATE_SETTLE", 0):
            self.poll()
            self.assertIn("The end of the copy was not confirmed", self.plan()["lead"])

    def test_the_review_does_not_say_so_for_the_recorded_job(self):
        job = self.started()
        self.kube.discard(self.record_path())
        self.finish(job, "Failed", "BackoffLimitExceeded")
        self.assert_kept_stopped(self.poll(), "Edit it back")
        plan = self.plan()
        self.assertTrue(plan["can"])
        self.assertNotIn("not confirmed", plan["lead"])

    def test_a_job_deleted_in_the_foreground_is_waited_for(self):
        """The Job stays, with a deletionTimestamp, while its pods go."""
        job = self.started()
        self.copy_pod(job)
        self.kube.add({**self.job(), "metadata": {**self.job()["metadata"], "deletionTimestamp": "2026-11-22T00:00:00Z",
                                                  "finalizers": ["foregroundDeletion"]}})
        result = self.poll()
        self.assertEqual("running", result["status"], result["message"])
        self.assertIn("Kubernetes has not finished", result["message"])
        self.assertIn("Its finalizers: foregroundDeletion", result["message"])
        self.assertIn("undoes the edit once the Job and its pods are gone", result["message"])
        self.assert_still_held()
        self.assertFalse(self.plan()["can"])
        self.gone(job)
        self.kube.discard("/api/v1/namespaces/lab/pods/copy-pod")
        self.assertEqual("failed", self.poll()["status"])
        self.assert_undone(2)


if __name__ == "__main__":
    unittest.main()
