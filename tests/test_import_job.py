import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from fakes.job_store import store_writes
import homestead_import_job as flow
import homestead_import_guard as guard
import homestead_imports as imports
import homestead_container_import as CONTAINER_IMPORT
import homestead_operations as ops
import homestead_capacity_review as review
import homestead_vm_mutation_recovery as recovery
import server


class ImportJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.objects, self.pods, self.sent = {}, [], []
        self.before_send = lambda *args: None
        self.after_send = lambda *args: None
        self.admission = mock.Mock(return_value={"blocked": False})
        self.dep_path = "/apis/apps/v1/namespaces/lab/deployments/app"
        self.job_path = "/apis/batch/v1/namespaces/lab/jobs/homestead-import-app"
        self.pvc_path = "/api/v1/namespaces/lab/persistentvolumeclaims/data"
        self.body = {"name": "app", "private": "credential-never-journalled"}
        template = {"metadata": {"labels": {"app": "app"}}, "spec": {"containers": [{"name": "app", "image": "example/app",
            "env": [{"name": "PASSWORD", "value": "credential-never-journalled"}]}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}
        self.prepared = {"volumes": [{"name": "data", "size_gb": 10, "storage_class": "storage", "access_mode": "ReadWriteOnce", "create": True}],
            "deployment": {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "app", "annotations": {guard.JOB: "homestead-import-app"}},
                           "spec": {"replicas": 0, "template": copy.deepcopy(template)}},
            "service": {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "app", "namespace": "lab"}, "spec": {"ports": [{"port": 80}]}},
            "job": {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "homestead-import-app", "namespace": "lab"}, "spec": {"template": template}},
            "result": {"ok": True, "name": "app", "job": "homestead-import-app"}}
        self.context = {"namespace": "lab", "claims": {}}
        for patch in (mock.patch.object(review, "_key", return_value=b"import-test-key"),
                      mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {flow.KIND: lambda item: flow.status(item, self.read)}),
                      mock.patch.dict(ops.CANCELLERS, {flow.KIND: (flow.cancel_plan, flow.cancel_run)}),
                      mock.patch.object(imports, "kget", side_effect=self.read), mock.patch.object(imports, "ksend", side_effect=self.send),
                      mock.patch.object(imports, "NS", "lab")):
            patch.start()
            self.addCleanup(patch.stop)
        self.approve()

    def approve(self):
        self.body.update(capacity_token=review.issue(self.body, self.context), confirm_capacity=True)

    def read(self, path):
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path in self.objects:
            found = copy.deepcopy(self.objects[path])
            getattr(self, "after_read", lambda target: None)(path)
            return found
        raise urllib.error.HTTPError(path, 404, "private API error", {}, None)

    def send(self, method, path, body, **kwargs):
        self.before_send(method, path, body)
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            # Every creation has a durable intent BEFORE transmission.
            entry = ops.stored()[0]["ref"]["writes"][-1]
            self.assertEqual("intent", entry["phase"])
            self.assertEqual(body["metadata"]["name"], entry["resource"]["name"])
            path += "/" + body["metadata"]["name"]
            self.assertNotIn(path, self.objects)
        if method == "DELETE":
            if self.objects[path]["metadata"]["uid"] != body["preconditions"]["uid"]:
                raise urllib.error.HTTPError(path, 409, "the object has a different UID", {}, None)
            return self.objects.pop(path)
        result = copy.deepcopy(body)
        result["metadata"].update(uid=result["metadata"].get("uid", result["kind"] + "-uid"), resourceVersion=str(len(self.sent)), namespace="lab")
        self.objects[path] = result
        self.after_send(method, path, result)
        return copy.deepcopy(result)

    def create_claim(self, ns, name, size, storage_class, access_mode, *, send):
        return send("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"name": name, "namespace": ns}, "spec": {"accessModes": [access_mode]}})

    def start(self):
        return flow.dispatch(self.body, self.prepared, self.context, self.read, self.send, ops, self.create_claim, self.admission)

    def item(self):
        return ops.stored()[0]

    def poll(self):
        return ops.list_operations()[0]

    def complete(self, failed=False):
        self.objects[self.job_path]["status"] = {"conditions": [{"type": "Failed" if failed else "Complete", "status": "True"}]}

    def borrowed(self):
        self.prepared["volumes"][0]["create"] = False
        self.objects[self.pvc_path] = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "data", "namespace": "lab", "uid": "borrowed", "resourceVersion": "1"}, "spec": {"volumeName": "pv-data"}}
        self.objects["/api/v1/persistentvolumes/pv-data"] = {"metadata": {"uid": "pv-original"}}
        self.context["claims"] = {"data": {"uid": "borrowed", "spec": {"volumeName": "pv-data"}}}
        self.approve()

    def recovery_body(self):
        view = recovery.preview(self.item()["id"], ops, self.read, "admin")
        return view, {"id": self.item()["id"], "capacity_token": view["capacity_token"], "confirm_capacity": True, "acknowledge_unknown": True}

    def test_success_records_every_write_and_manual_start_only(self):
        result = self.start()
        self.assertEqual("running", result["operation"]["status"])
        # The imported PASSWORD goes into the app's own Secret, before the app.
        self.assertEqual(["PersistentVolumeClaim", "Secret", "Deployment", "Service", "Job", "Deployment"], [r["resource"]["kind"] for r in self.item()["ref"]["writes"]])
        env = self.objects[self.dep_path]["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual([{"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "app-env", "key": "app.PASSWORD"}}}], env)
        self.assertNotIn("credential-never-journalled", json.dumps(self.objects[self.dep_path]))
        self.assertTrue(all(r["phase"] == "accepted" for r in self.item()["ref"]["writes"]))
        self.assertNotIn("credential-never-journalled", json.dumps(ops.stored()))
        self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", self.read))
        self.complete()
        self.assertEqual("succeeded", self.poll()["status"])
        self.assertEqual(0, self.objects[self.dep_path]["spec"]["replicas"])
        self.assertEqual("", guard.pending(self.objects[self.dep_path], "lab", self.read))
        self.assertNotIn("DELETE", [r[0] for r in self.sent])

    def test_lost_response_at_each_step_is_durable_and_never_retried(self):
        # Each subcase owns an independent journal, like separate processes.
        for fail_at in range(1, 7):
            with self.subTest(step=fail_at), tempfile.TemporaryDirectory() as directory, mock.patch.object(ops, "DATA_DIR", directory):
                self.objects.clear(); self.sent.clear()
                def lose(*args):
                    if len(self.sent) == fail_at:
                        raise TimeoutError("credential-never-journalled")
                self.after_send = lose
                with self.assertRaisesRegex(ValueError, "Recent jobs"):
                    self.start()
                self.assertEqual(fail_at, len(self.sent))
                self.assertEqual("uncertain", self.item()["ref"]["writes"][-1]["phase"])
                self.assertTrue(self.item()["ref"]["retain_resources"])
                self.assertNotIn("credential-never-journalled", json.dumps(self.item()))
                for _ in range(2):
                    self.poll()
                with self.assertRaises(ValueError):
                    self.start()
                self.assertEqual(fail_at, len(self.sent))
                if fail_at >= 3:
                    self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", self.read))

    def test_journal_failure_sends_nothing(self):
        with store_writes(ops, fail=OSError("disk full")), self.assertRaises(Exception):
            self.start()
        self.assertEqual([], self.sent)

    def test_no_forged_or_stale_approval_creates_a_journal(self):
        self.body["capacity_token"] = "1.invalid"
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual([], ops.stored())
        self.assertEqual([], self.sent)

    def test_unverified_api_receipt_cannot_advance_setup(self):
        self.after_send = lambda method, path, obj: obj["metadata"].pop("uid")
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(1, len(self.sent))
        self.assertEqual("unverified", self.item()["ref"]["writes"][-1]["phase"])

    def test_acknowledged_write_with_journal_failure_leaves_intent(self):
        def record(items):
            if items and items[0]["ref"].get("writes") and items[0]["ref"]["writes"][-1]["phase"] == "accepted":
                raise OSError("storage lost")
        with store_writes(ops, fail=record), self.assertRaises(ValueError):
            self.start()
        self.assertEqual(1, len(self.sent))
        self.assertEqual("intent", self.item()["ref"]["writes"][-1]["phase"])

    def test_approval_and_borrowed_identity_checked_before_record_or_write(self):
        self.borrowed()
        self.objects[self.pvc_path]["metadata"]["uid"] = "replacing it"
        with self.assertRaisesRegex(ValueError, "Borrowed volume"):
            self.start()
        self.assertEqual([], ops.stored())
        self.assertEqual([], self.sent)

    def test_new_and_borrowed_claim_or_pv_replacement_blocks_helper(self):
        for borrowed in (False, True):
            with self.subTest(borrowed=borrowed), tempfile.TemporaryDirectory() as directory, mock.patch.object(ops, "DATA_DIR", directory):
                self.objects.clear(); self.sent.clear()
                if borrowed:
                    self.borrowed()
                def replace(method, path, obj):
                    if path == self.dep_path:
                        if borrowed:
                            self.objects["/api/v1/persistentvolumes/pv-data"]["metadata"]["uid"] = "replacing it"
                        else:
                            self.objects[self.pvc_path]["metadata"]["uid"] = "replacing it"
                self.after_send = replace
                with self.assertRaises(ValueError):
                    self.start()
                self.assertNotIn(self.job_path, self.objects)
                self.assertNotIn("DELETE", [r[0] for r in self.sent])

    def test_fresh_capacity_and_late_volume_consumer_block_helper(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.start()
        self.assertNotIn(self.job_path, self.objects)
        self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", self.read))

    def test_consumer_appearing_during_admission_blocks_copy(self):
        def admit(_):
            self.pods = [{"metadata": {"namespace": "lab"}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}, "status": {"phase": "Running"}}]
            return {"blocked": False}
        self.admission.side_effect = admit
        with self.assertRaises(ValueError):
            self.start()
        self.assertNotIn(self.job_path, self.objects)

    def test_complete_condition_with_live_pod_does_not_allow_start_or_recovery(self):
        self.start(); self.complete()
        self.pods = [{"metadata": {"namespace": "lab", "ownerReferences": [{"kind": "Job", "uid": "Job-uid", "controller": True}]}, "status": {"phase": "Running"}}]
        self.assertEqual("running", self.poll()["status"])
        self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", self.read))
        self.assertTrue(self.recovery_body()[0]["plan"]["blocked"])

    def test_replaced_or_modified_job_is_not_trusted(self):
        self.start(); self.complete()
        self.objects[self.job_path]["metadata"]["uid"] = "replacing it"
        self.assertEqual("failed", self.poll()["status"])
        self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", self.read))
        self.assertTrue(self.recovery_body()[0]["plan"]["blocked"])

    def test_partial_setup_recovery_is_read_only_and_keeps_hold(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.start()
        before = copy.deepcopy(self.objects)
        view, body = self.recovery_body()
        self.assertFalse(view["plan"]["blocked"])
        self.assertEqual(5, len(view["plan"]["resources"]))
        self.assertIsNone(next(r for r in view["plan"]["resources"] if (r.get("kind") or r.get("resource", {}).get("kind")) == "Job")["current"])
        with self.assertRaises(ValueError):
            recovery.resolve({**body, "acknowledge_unknown": False}, ops, self.read, "admin")
        resolved = recovery.resolve(body, ops, self.read, "admin")
        self.assertEqual("failed", resolved["operation"]["status"])
        self.assertEqual(before, self.objects)
        self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", self.read))
        with self.assertRaises(ValueError):
            self.start()  # approval remains consumed

    def test_unknown_job_creation_blocks_reservation_release(self):
        def lose(method, path, obj):
            if path == self.job_path:
                raise TimeoutError("lost")
        self.after_send = lose
        with self.assertRaises(ValueError):
            self.start()
        self.complete()
        view, body = self.recovery_body()
        self.assertTrue(view["plan"]["blocked"])
        with self.assertRaises(ValueError):
            recovery.resolve(body, ops, self.read, "admin")

    def test_failed_confirmed_copy_can_resolve_without_deleting_data(self):
        self.borrowed(); self.start(); self.complete(failed=True); self.poll()
        before = copy.deepcopy(self.objects)
        view, body = self.recovery_body()
        self.assertFalse(view["plan"]["blocked"])
        recovery.resolve(body, ops, self.read, "admin")
        self.assertEqual(before, self.objects)
        self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", self.read))

    def test_shared_claim_reservation_blocks_other_copy_before_pods_exist(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "still running or needs recovery"):
            ops.start("workload-copy", "Other", {}, "/containers", {"namespace": "lab", "name": "other", "moves": [{"from": "data", "to": "other"}], "retain_resources": True})

    def test_no_copy_has_durable_record_without_helper_or_start(self):
        self.prepared["job"] = None
        self.prepared["deployment"]["metadata"]["annotations"] = {}
        result = self.start()
        self.assertEqual("succeeded", result["operation"]["status"])
        self.assertNotIn(self.job_path, self.objects)

    def test_completed_cleanup_only_deletes_uid_pinned_job_and_retains_borrowed_pvc(self):
        self.borrowed(); self.start(); self.complete(); self.poll()
        with mock.patch.object(ops.time, "time", return_value=int(self.body["capacity_token"].split(".")[0]) + 1):
            self.assertFalse(ops._public(self.item())["dismissible"])
        before = copy.deepcopy(self.objects[self.pvc_path])
        self.assertTrue(CONTAINER_IMPORT.import_cleanup_plan("homestead-import-app")["journalled"])
        result = CONTAINER_IMPORT.delete_import("homestead-import-app")
        self.assertIn("all volumes are kept", result["message"])
        self.assertEqual(before, self.objects[self.pvc_path])
        deletes = [row for row in self.sent if row[0] == "DELETE"]
        self.assertEqual(1, len(deletes))
        self.assertEqual(self.job_path, deletes[0][1])
        self.assertEqual("Job-uid", deletes[0][2]["preconditions"]["uid"])
        self.assertEqual("", guard.pending(self.objects[self.dep_path], "lab", self.read))
        with mock.patch.object(ops.time, "time", return_value=int(self.body["capacity_token"].split(".")[0]) + 1):
            self.assertTrue(ops._public(self.item())["dismissible"])

    def test_active_missing_or_replaced_job_never_falls_back_to_name_cleanup(self):
        self.start()
        for replacement in (self.objects[self.job_path], None, {"metadata": {"uid": "other"}}):
            if replacement is None:
                self.objects.pop(self.job_path, None)
            else:
                self.objects[self.job_path] = replacement
            self.assertTrue(CONTAINER_IMPORT.import_cleanup_plan("homestead-import-app")["journalled"])
            with self.assertRaises(ValueError):
                CONTAINER_IMPORT.delete_import("homestead-import-app")
        self.assertNotIn("DELETE", [row[0] for row in self.sent])

    def test_cleanup_replacement_workload_is_never_touched(self):
        self.start(); self.complete(); self.poll()
        self.objects[self.dep_path]["metadata"]["uid"] = "someone-else"
        count = len(self.sent)
        with self.assertRaises(ValueError):
            CONTAINER_IMPORT.delete_import("homestead-import-app")
        self.assertEqual(count, len(self.sent))

    def test_changed_recovery_inventory_needs_fresh_approval(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.start()
        _, body = self.recovery_body()
        self.objects[self.pvc_path]["metadata"]["uid"] = "replacing it"
        with self.assertRaises(ValueError):
            recovery.resolve(body, ops, self.read, "admin")

    def test_recovery_cannot_race_live_dispatcher(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.start()
        with mock.patch.object(recovery.RECOVERY, "_exclusive", side_effect=recovery.RECOVERY.DispatcherBusy()):
            self.assertTrue(self.recovery_body()[0]["plan"]["blocked"])

    def test_unknown_or_paginated_pods_keep_start_blocked(self):
        self.start(); self.complete()
        for inventory in ({}, {"items": [], "metadata": {"continue": "next"}}):
            def read(path):
                return inventory if path == "/api/v1/pods" else self.read(path)
            self.assertTrue(guard.pending(self.objects[self.dep_path], "lab", read))

    def cleanup_api(self, plan, result):
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/imports/delete", {}
        handler._guard = lambda path: False
        handler._body = lambda: {"name": "homestead-import-app", "remove_workload": True, "remove_volumes": ["data"]}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(CONTAINER_IMPORT, "import_cleanup_plan", return_value=plan), \
             mock.patch.object(CONTAINER_IMPORT, "delete_import", return_value=result) as cleanup, \
             mock.patch.object(server.KUBE_HELPERS, "ksend") as send, mock.patch.object(server.OVERVIEW_ROUTES, "guard_managed_smb"):
            handler.do_POST()
        send.assert_not_called()
        return handler._send.call_args.args, cleanup

    def test_a_failure_on_the_password_secret_leaves_the_copy_job_recorded(self):
        """The Job is POSTed before its Secret; the record must not wait for the Secret (#1301)."""
        self.prepared["source_secret"] = {"namespace": "lab", "name": "source-pass", "copy": "app-source-pass"}
        self.objects["/api/v1/namespaces/lab/secrets/source-pass"] = {"metadata": {"name": "source-pass"}, "data": {"password": "c2VjcmV0"}}
        def refuse(method, path, body):
            if method == "POST" and path.endswith("/secrets") and body["metadata"]["name"] == "app-source-pass":
                raise urllib.error.HTTPError(path, 500, "refused", {}, None)
        self.before_send = refuse
        with self.assertRaises(ValueError):
            self.start()
        ref = self.item()["ref"]
        self.assertEqual(("homestead-import-app", "Job-uid"), (ref.get("job"), ref.get("job_uid")))
        self.assertIn(self.job_path, self.objects, "the copy Job exists, and the record names it")

    # Remove for a failed or cancelled import (#1301): only what carries the import's stamp,
    # and volumes only when ticked.
    svc_path = "/api/v1/namespaces/lab/services/app"

    def failed_import(self, status="failed"):
        self.start(); self.complete(failed=True); self.poll()
        self.assertEqual("failed", self.item()["status"])
        if status != "failed":
            with ops.transaction() as t:
                t.items[0]["status"] = status
                t.save(t.items)
        self.sent.clear()

    def remove(self, volumes=()):
        return CONTAINER_IMPORT.delete_import("homestead-import-app", volumes=volumes)

    def deleted(self):
        return sorted(path for method, path, _ in self.sent if method == "DELETE")

    def test_removing_a_failed_import_deletes_its_stamped_app_service_and_job_and_keeps_the_volume(self):
        self.failed_import()
        plan = CONTAINER_IMPORT.import_cleanup_plan("homestead-import-app")
        self.assertEqual((True, "app", ["app"], [{"name": "data", "created": True}]),
                         (plan["removable"], plan["workload"], plan["services"], plan["volumes"]))
        result = self.remove()
        self.assertEqual(sorted([self.dep_path, self.svc_path, self.job_path]), self.deleted())
        self.assertIn(self.pvc_path, self.objects, "a volume not ticked is kept")
        self.assertIn("Kept volumes: data", result["message"])
        self.assertFalse(self.item()["ref"]["retain_resources"])

    def swap_after_first_read(self, path, replacement):
        """Deleted and recreated, unstamped, right after the stamp check read it."""
        seen = []
        def hook(target):
            if target == path and not seen and self.armed:
                seen.append(target)
                self.objects[target] = replacement
        self.after_read = hook

    def swapped_before_delete(self, path, replacement):
        self.failed_import()
        self.swap_after_first_read(path, replacement)
        real = flow.remove_failed
        def armed(*args, **kwargs):
            self.armed = True        # from here on: the reads that decide what to delete
            return real(*args, **kwargs)
        with mock.patch.object(flow, "remove_failed", armed), self.assertRaises(urllib.error.HTTPError):
            self.remove(volumes=["data"])
        self.assertEqual(replacement["metadata"]["uid"], self.objects[path]["metadata"]["uid"], "the replacement survives")

    armed = False

    def test_an_app_swapped_after_the_stamp_check_is_not_deleted(self):
        self.swapped_before_delete(self.dep_path, {"metadata": {"name": "app", "uid": "new", "annotations": {}}})

    def test_a_service_swapped_after_the_stamp_check_is_not_deleted(self):
        self.swapped_before_delete(self.svc_path, {"metadata": {"name": "app", "uid": "new", "annotations": {}}})

    def test_a_job_swapped_after_the_stamp_check_is_not_deleted(self):
        self.swapped_before_delete(self.job_path, {"metadata": {"name": "x", "uid": "new", "annotations": {}}})

    def test_a_volume_swapped_after_the_check_is_not_deleted(self):
        self.swapped_before_delete(self.pvc_path, {"metadata": {"name": "data", "uid": "new"}})

    def test_a_cancelled_import_is_removed_the_same_way(self):
        self.failed_import("cancelled")
        self.remove()
        self.assertEqual(sorted([self.dep_path, self.svc_path, self.job_path]), self.deleted())

    def test_an_object_without_the_import_s_stamp_is_never_deleted(self):
        self.failed_import()
        self.objects[self.svc_path]["metadata"]["annotations"] = {}      # made by hand, under the same name
        self.objects[self.dep_path]["metadata"]["annotations"] = {}      # replaced by an app of the same name
        self.objects[self.job_path]["metadata"]["annotations"] = {}
        plan = CONTAINER_IMPORT.import_cleanup_plan("homestead-import-app")
        self.assertEqual(("", []), (plan["workload"], plan["services"]))
        self.remove()
        self.assertEqual([], self.deleted())
        for path in (self.dep_path, self.svc_path, self.job_path, self.pvc_path):
            self.assertIn(path, self.objects)

    def test_a_ticked_volume_this_import_created_is_deleted_and_an_unticked_one_is_not(self):
        self.failed_import()
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/other"] = {"metadata": {"uid": "x", "name": "other"}}
        self.remove(volumes=["data"])
        self.assertIn(self.pvc_path, self.deleted())
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/other", self.objects)

    def test_a_volume_this_import_did_not_create_is_refused_even_when_ticked(self):
        self.borrowed(); self.start(); self.complete(failed=True); self.poll(); self.sent.clear()
        before = copy.deepcopy(self.objects[self.pvc_path])
        with self.assertRaisesRegex(ValueError, "did not create data"):
            self.remove(volumes=["data"])
        self.assertEqual([], self.deleted())
        self.assertEqual(before, self.objects[self.pvc_path])

    def test_a_volume_replaced_after_the_import_is_not_deleted(self):
        self.failed_import()
        self.objects[self.pvc_path]["metadata"]["uid"] = "someone-else"
        with self.assertRaisesRegex(ValueError, "replaced"):
            self.remove(volumes=["data"])
        self.assertIn(self.pvc_path, self.objects)

    def test_a_running_import_cannot_be_removed_this_way(self):
        self.start()
        self.assertFalse(CONTAINER_IMPORT.import_cleanup_plan("homestead-import-app")["removable"])
        with self.assertRaises(ValueError):
            self.remove()
        self.assertEqual([], [r for r in self.sent if r[0] == "DELETE"])

    def test_the_api_removes_a_failed_import_and_passes_only_the_named_volumes(self):
        self.failed_import()
        request = mock.Mock(body={"name": "homestead-import-app", "namespace": "lab", "remove_volumes": ["data"]})
        with mock.patch.object(imports, "guard_managed_smb"):
            result = imports._delete_import(request)
        self.assertIn(self.pvc_path, self.deleted())
        self.assertEqual([], result["releasing"])

    def test_cleanup_api_refuses_volume_or_workload_deletion_for_journalled_import(self):
        response, cleanup = self.cleanup_api({"journalled": True}, {})
        self.assertEqual(400, response[0])
        cleanup.assert_not_called()

    def test_cleanup_plan_race_cannot_delete_new_journalled_import_resources(self):
        response, cleanup = self.cleanup_api({"namespace": "lab", "workload": "app", "volumes": [{"name": "data", "created": True}]},
            {"ok": True, "journalled": True, "message": "Workload and all volumes are kept"})
        self.assertEqual(200, response[0])
        cleanup.assert_called_once()
        self.assertEqual([], response[1]["removed"])


if __name__ == "__main__":
    unittest.main()
