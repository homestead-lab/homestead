import copy
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

import test_deploy_capacity  # server import path
import server
import homestead_operations as ops
import homestead_storage_guard as guard


class StorageGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", directory.name); patch.start(); self.addCleanup(patch.stop)
        self.item = {"id": "move", "kind": "reclass", "status": "running", "ref": {
            "namespace": "lab", "claim": "source", "temp": "copy", "old_pv": "pv-old", "retain_resources": True}}
        with ops._lock: ops._write([self.item])
        self.pv = {"metadata": {"name": "pv-new"}, "spec": {"claimRef": {"namespace": "lab", "name": "copy"},
            "csi": {"driver": "driver.longhorn.io", "volumeHandle": "longhorn-different-name"}}}
        self.dispatch = mock.Mock(return_value={"ok": True})

    def read(self, path):
        if path == "/api/v1/persistentvolumes": return {"items": [copy.deepcopy(self.pv)]}
        if path == "/api/v1/persistentvolumes/pv-new": return copy.deepcopy(self.pv)
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, path, method="DELETE", body=None):
        return guard.send(method, path, body, self.dispatch, ops, self.read)

    def test_claims_pvs_and_unpolled_binding_cannot_be_deleted_or_rewritten(self):
        for path in ("/api/v1/namespaces/lab/persistentvolumeclaims/source", "/api/v1/namespaces/lab/persistentvolumeclaims/copy",
                     "/api/v1/persistentvolumes/pv-old", "/api/v1/persistentvolumes/pv-new",
                     "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/longhorn-different-name"):
            for method in ("DELETE", "PATCH", "PUT"):
                with self.assertRaisesRegex(ValueError, "protected"): self.send(path, method, {"spec": {"persistentVolumeReclaimPolicy": "Delete"}})
        self.dispatch.assert_not_called()

    def test_recreation_and_bulk_deletion_cannot_bypass_fence(self):
        for path in ("/api/v1/namespaces/lab/persistentvolumeclaims", "/api/v1/persistentvolumes"):
            with self.assertRaises(ValueError): self.send(path)
        with self.assertRaises(ValueError):
            self.send("/api/v1/namespaces/lab/persistentvolumeclaims", "POST", {"metadata": {"name": "source"}})
        with self.assertRaises(ValueError): self.send("/api/v1/namespaces/lab/persistentvolumeclaims/%73ource?gracePeriodSeconds=0")
        self.dispatch.assert_not_called()

    def test_other_namespace_and_nonstorage_requests_are_unaffected(self):
        self.send("/api/v1/namespaces/other/persistentvolumeclaims/source")
        self.send("/apis/apps/v1/namespaces/lab/deployments/app", "PATCH", {"spec": {"replicas": 1}})
        self.assertEqual(2, self.dispatch.call_count)

    def test_only_own_dispatcher_bypasses_own_record_and_scope_does_not_leak(self):
        path = "/api/v1/namespaces/lab/persistentvolumeclaims/source"
        with guard.dispatching(self.item): self.send(path)
        with self.assertRaises(ValueError): self.send(path)
        with ops._lock: ops._write([self.item, {**copy.deepcopy(self.item), "id": "other-move"}])
        with guard.dispatching(self.item):
            with self.assertRaises(ValueError): self.send(path)
        self.assertEqual(1, self.dispatch.call_count)

    def test_dispatcher_exemption_is_thread_local(self):
        errors = []
        def other():
            try: self.send("/api/v1/namespaces/lab/persistentvolumeclaims/source")
            except ValueError: errors.append("protected")
        with guard.dispatching(self.item):
            thread = threading.Thread(target=other); thread.start(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["protected"], errors)
        self.dispatch.assert_not_called()

    def test_pinned_longhorn_handle_survives_missing_pv_inventory(self):
        self.item["ref"]["copy_claims"] = {"source": {"pv": "removed-pv", "csi_driver": "driver.longhorn.io", "csi_handle": "pinned"}}
        with ops._lock: ops._write([self.item])
        with self.assertRaises(ValueError): self.send("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pinned")
        self.dispatch.assert_not_called()

    def test_copy_verification_job_is_protected_until_dispatcher_removes_it(self):
        self.item["ref"]["job"] = "copy-proof"
        with ops._lock: ops._write([self.item])
        path = "/apis/batch/v1/namespaces/lab/jobs/copy-proof"
        for target in (path, path + "?propagationPolicy=Background", path.rsplit("/", 1)[0]):
            with self.assertRaisesRegex(ValueError, "protected"): self.send(target)
        self.dispatch.assert_not_called()
        with guard.dispatching(self.item): self.send(path)
        self.dispatch.assert_called_once()

    def test_delete_endpoint_blocks_before_finished_job_cleanup_or_orphan_delete(self):
        handler = object.__new__(server.H)
        handler.path, handler.command = "/api/volumes/delete", "POST"
        handler.headers = {"X-Homestead-Auth": "1"}
        handler._who = lambda: {"user": "admin", "role": "admin"}
        handler._body = lambda: {"namespace": "lab", "name": "source", "uid": "uid",
                                 "action": "delete_data", "confirmation": "source"}
        handler._client_ip = lambda: "127.0.0.1"
        for orphan in (False, True):
            handler._send = mock.Mock()
            plan = {"namespace": "lab", "name": "source", "uid": "uid", "orphan": orphan,
                    "pv": {}, "longhorn": {}, "actions": {}, "blocking_reasons": [],
                    "inventory_complete": True, "removable_jobs": ["copy-proof"]}
            with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
                    mock.patch.object(server.VOLUMES, "deletion_plan", return_value=plan), \
                    mock.patch.object(server.VOLUMES, "ksend") as send, \
                    mock.patch.object(server.VOLUMES, "delete_orphan") as delete_orphan, \
                    mock.patch.object(ops, "start") as start:
                handler.do_POST()
                send.assert_not_called(); delete_orphan.assert_not_called(); start.assert_not_called()
            self.assertEqual(403, handler._send.call_args.args[0])
            self.assertIn("protected", str(handler._send.call_args.args[1]))

    def test_dispatcher_preserves_legacy_engine_and_never_downgrades_unknown_protocol(self):
        self.assertIs(server.storage_move_progress, ops.RESOLVERS["reclass"])
        with mock.patch.object(server.RECLASS, "resolve", return_value=("running", 1, "legacy")) as legacy:
            self.assertEqual("legacy", server.storage_move_progress(self.item)[2])
            legacy.assert_called_once_with(self.item)
            self.item["ref"].update(storage_protocol=999, handoff_phase="copy")
            self.assertEqual("failed", server.storage_move_progress(self.item)[0])
            self.assertEqual(1, legacy.call_count)
            self.assertTrue(self.item["ref"]["retain_resources"])

    def test_legacy_cancel_keeps_its_existing_cleanup_but_cannot_take_protocol_jobs(self):
        def cleanup(item, options):
            self.send("/api/v1/namespaces/lab/persistentvolumeclaims/copy")
            return "legacy cleanup"
        with mock.patch.object(server.CANCEL, "reclass_cancel", side_effect=cleanup) as cancel:
            self.assertEqual("legacy cleanup", ops.CANCELLERS["reclass"][1](self.item, {}))
            for protocol in (1, 999):
                self.item["ref"]["storage_protocol"] = protocol
                with self.assertRaises(ValueError): ops.CANCELLERS["reclass"][1](self.item, {})
                self.assertFalse(ops._plan_for(self.item)["can"])
            cancel.assert_called_once()
        self.dispatch.assert_called_once()

    def test_snapshot_writes_and_bulk_cleanup_respect_the_backing_volume(self):
        path = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/snapshots"
        snapshot = {"metadata": {"name": "point"}, "spec": {"volume": "longhorn-different-name"}}
        read = self.read
        with mock.patch.object(self, "read", side_effect=lambda target: snapshot if target == path + "/point" else read(target)):
            for method, target, body in (("POST", path, snapshot), ("DELETE", path + "/point", None),
                                         ("PATCH", path + "/point", {}), ("DELETE", path, None)):
                with self.assertRaisesRegex(ValueError, "protected"): self.send(target, method, body)
            with self.assertRaisesRegex(ValueError, "cannot be verified"):
                self.send(path, "POST", {"metadata": {"name": "unknown"}})
        self.dispatch.assert_not_called()

    def test_snapshot_orchestration_cannot_stop_workloads_or_call_longhorn_rest(self):
        item = {"id": "snapshot", "kind": "snapshot-revert", "ref": {"volume": "longhorn-different-name"}}
        with mock.patch.object(server, "kget", self.read), \
                mock.patch.object(server.REVERT, "resolve") as revert, \
                mock.patch.object(server.REVERT, "cancel_run") as cancel, \
                mock.patch.object(server.SNAPSHOT_DELETE, "resume_resolve") as delete:
            for dispatch in (lambda: ops.RESOLVERS["snapshot-revert"](item),
                             lambda: ops.CANCELLERS["snapshot-revert"][1](item, {}),
                             lambda: ops.RESOLVERS["snapshot-delete"](item)):
                with self.assertRaisesRegex(ValueError, "protected"): dispatch()
            revert.assert_not_called(); cancel.assert_not_called(); delete.assert_not_called()
            # Read-only previews remain available, and unrelated backing data
            # does not become globally unavailable just because a move exists.
            server.storage_volume_action("unrelated", self.dispatch)
            self.dispatch.assert_called_once()

    def test_failed_and_cancelled_records_keep_protection_even_if_tracking_hidden(self):
        for status in ("failed", "cancelled"):
            self.item.update(status=status, tracking_stopped=True)
            with ops._lock: ops._write([self.item])
            with self.assertRaises(ValueError): self.send("/api/v1/persistentvolumes/pv-old")
        self.dispatch.assert_not_called()

    def test_completed_and_verified_legacy_rollback_release_protection(self):
        for status, phase in (("succeeded", "done"), ("failed", "rolled-back")):
            self.item.update(status=status)
            self.item["ref"].update(phase=phase, retain_resources=False)
            with ops._lock: ops._write([self.item])
            self.send("/api/v1/persistentvolumes/pv-old")
        self.assertEqual(2, self.dispatch.call_count)

    def test_preview_explains_protection_and_server_transport_enforces_it(self):
        plan = {"namespace": "lab", "name": "source", "pv": {}, "longhorn": {}, "actions": {}}
        self.assertTrue(guard.review(plan, ops, self.read)["blocked"])
        self.assertFalse(plan["actions"]["delete_data"]["enabled"])
        with mock.patch.object(server, "_ksend") as raw:
            with self.assertRaises(ValueError): server.ksend("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/source")
            raw.assert_not_called()

    def test_unreadable_history_or_partial_pv_list_never_authorizes_deletion(self):
        with mock.patch.object(ops, "_read", side_effect=ValueError("unavailable")):
            with self.assertRaises(ValueError): self.send("/api/v1/namespaces/lab/persistentvolumeclaims/source")
        with mock.patch.object(self, "read", return_value={"items": [], "metadata": {"continue": "next"}}):
            with self.assertRaises(ValueError): self.send("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/unknown")
        self.dispatch.assert_not_called()
