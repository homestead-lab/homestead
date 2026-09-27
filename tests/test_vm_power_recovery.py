import copy
import multiprocessing
import unittest
import urllib.error
from unittest import mock

import test_vm_power_job as fixtures
import homestead_operations as ops
import homestead_vm_power_job as power
import homestead_vm_power_recovery as recovery
import server


def hold_dispatch(directory, body, context, sending, finish):
    ops.bind(None, directory, None)
    def send():
        sending.set()
        finish.wait(10)
        raise TimeoutError("simulated uncertain response")
    try:
        power.dispatch(body, context, ops, send, lambda: None)
    except ValueError:
        pass


class VMPowerRecoveryTests(unittest.TestCase):
    read = fixtures.VMPowerJobTests.read
    dispatch = fixtures.VMPowerJobTests.dispatch

    def setUp(self):
        fixtures.VMPowerJobTests.setUp(self)
        key = mock.patch.object(recovery.REVIEW, "_key", return_value=b"recovery-tests-only")
        key.start()
        self.addCleanup(key.stop)
        with self.assertRaises(ValueError):
            self.dispatch(send=mock.Mock(side_effect=TimeoutError("unavailable")))
        self.ident = ops._read()[0]["id"]

    def approved(self):
        review = recovery.preview(self.ident, ops, self.read, "admin")
        self.assertFalse(review["plan"]["blocked"], review)
        return {"id": self.ident, "capacity_token": review["capacity_token"], "confirm_capacity": True,
                "confirm": "guest", "acknowledge_unknown": True}

    def test_preview_only_reports_safe_identity_and_status_fields(self):
        self.vm["spec"]["template"]["spec"]["volumes"] = [{"cloudInitNoCloud": {"userData": "private-cloud-config"}}]
        before = copy.deepcopy(ops._read())
        with mock.patch.object(ops, "_write") as write:
            result = recovery.preview(self.ident, ops, self.read, "admin")
        write.assert_not_called()
        self.assertEqual(before, ops._read())
        self.assertNotIn("private-cloud-config", str(result))
        self.assertNotIn("review_digest", str(result))
        self.assertEqual("vm-uid", result["plan"]["resource"]["original_uid"])
        self.assertEqual("new-vmi", result["plan"]["observed"]["instance"]["uid"])
        self.assertIn("apply the earlier request late", " ".join(result["plan"]["warnings"]))

    def test_resolve_keeps_unknown_outcome_and_original_approval_consumed(self):
        result = recovery.resolve(self.approved(), ops, self.read, "admin")
        self.assertTrue(result["ok"])
        item = ops._read()[0]
        self.assertEqual("failed", item["status"])
        self.assertEqual("resolved-unknown", item["ref"]["phase"])
        self.assertFalse(item["ref"]["retain_resources"])
        self.assertEqual("unknown", item["ref"]["recovery"]["outcome"])
        self.assertEqual("admin", item["ref"]["recovery"]["by"])
        self.assertTrue(item["ref"]["recovery"]["late_effect_acknowledged"])
        self.assertFalse(ops._public(item)["power_recovery"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        with self.assertRaisesRegex(ValueError, "job has ended"):
            ops.record_phase(self.ident, "accepted", 25, "late outcome must not overwrite audit")

    def test_unreviewed_or_changed_confirmation_cannot_resolve(self):
        good = self.approved()
        for change in ({"capacity_token": ""}, {"confirm_capacity": False}, {"acknowledge_unknown": False}, {"confirm": "wrong"}):
            with self.assertRaises(ValueError):
                recovery.resolve({**good, **change}, ops, self.read, "admin")
            self.assertEqual("uncertain", ops._read()[0]["ref"]["phase"])

    def test_changed_vm_instance_or_actor_requires_another_review(self):
        good = self.approved()
        with self.assertRaises(recovery.REVIEW.Rejected):
            recovery.resolve(good, ops, self.read, "another-admin")
        for resource in (self.vm, self.vmi):
            good = self.approved()
            resource["metadata"]["resourceVersion"] += "-changed"
            with self.assertRaises(recovery.REVIEW.Rejected):
                recovery.resolve(good, ops, self.read, "admin")

    def test_phase_change_or_repeated_resolve_cannot_reuse_review(self):
        good = self.approved()
        recovery.resolve(good, ops, self.read, "admin")
        with self.assertRaisesRegex(ValueError, "does not have an unresolved"):
            recovery.resolve(good, ops, self.read, "admin")

    def test_active_dispatcher_blocks_preview_and_resolve(self):
        good = self.approved()
        with power.worker_lock(self.ident, ops):
            review = recovery.preview(self.ident, ops, self.read, "admin")
            self.assertTrue(review["plan"]["blocked"])
            self.assertIsNone(review["capacity_token"])
            with self.assertRaisesRegex(ValueError, "dispatcher is active"):
                recovery.resolve(good, ops, self.read, "admin")
        self.assertEqual("uncertain", ops._read()[0]["ref"]["phase"])

    def test_dispatcher_in_another_process_blocks_recovery_until_it_exits(self):
        recovery.resolve(self.approved(), ops, self.read, "admin")
        context = multiprocessing.get_context("spawn")
        sending, finish = context.Event(), context.Event()
        body = {**self.body, "capacity_token": self.body["capacity_token"] + "-another-review"}
        child = context.Process(target=hold_dispatch, args=(self.tmp.name, body, self.context, sending, finish))
        try:
            child.start()
            self.assertTrue(sending.wait(10))
            ident = ops._read()[-1]["id"]
            self.assertTrue(recovery.preview(ident, ops, self.read, "admin")["plan"]["blocked"])
            finish.set()
            child.join(10)
            self.assertEqual(0, child.exitcode)
            self.assertFalse(recovery.preview(ident, ops, self.read, "admin")["plan"]["blocked"])
        finally:
            if child.is_alive():
                child.terminate()
                child.join(5)

    def test_orphan_dispatch_phase_can_be_inspected_only_with_fenced_protocol(self):
        ops.record_phase(self.ident, "dispatching", 10, "lost worker")
        items = ops._read()
        items[0]["ref"]["phase_at"] = 99999999999
        ops._write(items)
        self.approved()  # ownership is proven by the lock, not elapsed time
        ops.record_phase(self.ident, "dispatching", 10, "old worker", dispatch_protocol=0)
        self.assertTrue(recovery.preview(self.ident, ops, self.read, "admin")["plan"]["blocked"])

    def test_queued_changes_deletion_and_wrong_ownership_block_recovery(self):
        self.vm["status"] = {"stateChangeRequests": [{"action": "Start"}]}
        self.assertTrue(recovery.preview(self.ident, ops, self.read, "admin")["plan"]["blocked"])
        self.vm["status"] = {}
        self.vmi["metadata"]["deletionTimestamp"] = "now"
        self.assertTrue(recovery.preview(self.ident, ops, self.read, "admin")["plan"]["blocked"])
        del self.vmi["metadata"]["deletionTimestamp"]
        self.vmi["metadata"]["ownerReferences"][0]["uid"] = "unrelated"
        self.assertTrue(recovery.preview(self.ident, ops, self.read, "admin")["plan"]["blocked"])

    def test_missing_vm_is_not_an_invented_halted_guest(self):
        def missing(path):
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        plan = recovery.preview(self.ident, ops, missing, "admin")["plan"]
        self.assertIsNone(plan["observed"]["vm"])
        self.assertEqual("Not applicable", plan["observed"]["run_strategy"])
        self.assertIn("missing or has been replaced", " ".join(plan["warnings"]))

    def test_inventory_forbidden_or_malformed_never_becomes_missing(self):
        for value in (urllib.error.HTTPError("private-url", 403, "private-error", {}, None), TimeoutError("private endpoint"), {}, []):
            read = mock.Mock(side_effect=value) if isinstance(value, Exception) else mock.Mock(return_value=value)
            with self.assertRaises(ValueError) as caught:
                recovery.preview(self.ident, ops, read, "admin")
            self.assertNotIn("private", str(caught.exception))
            self.assertNotIn("dispatcher", str(caught.exception))
        self.vm["status"] = {"stateChangeRequests": {}}
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.approved()

    def test_unavailable_journal_does_not_report_resolved(self):
        good = self.approved()
        with mock.patch.object(ops, "_write", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                recovery.resolve(good, ops, self.read, "admin")
        self.assertEqual("uncertain", ops._read()[0]["ref"]["phase"])

    def test_invalid_id_cannot_address_arbitrary_lock_paths(self):
        for ident in ("../../outside", "", "A" * 24, "0" * 23):
            with self.assertRaisesRegex(ValueError, "identifier"):
                recovery.preview(ident, ops, self.read, "admin")

    def api(self, path, body, role="admin"):
        handler = object.__new__(server.H)
        handler.path, handler.command = path, "POST"
        handler.headers = {"X-Homestead-Auth": "1"}
        handler._who = lambda: {"user": "admin", "role": role}
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
                mock.patch.object(server, "kget", side_effect=self.read), \
                mock.patch.object(server, "ksend") as writes, mock.patch.object(server.VMS, "ksend") as power_writes:
            handler.do_POST()
        writes.assert_not_called()
        power_writes.assert_not_called()
        return handler._send.call_args.args

    def test_admin_routes_enforced_and_success_never_mutates_cluster(self):
        for role in ("viewer", "operator"):
            for endpoint in ("preview", "resolve"):
                self.assertEqual(403, self.api("/api/operations/power-recovery/" + endpoint, {"id": self.ident}, role)[0])
        preview = self.api("/api/operations/power-recovery/preview", {"id": self.ident})
        self.assertEqual(200, preview[0], preview)
        body = {"id": self.ident, "capacity_token": preview[1]["capacity_token"], "confirm_capacity": True,
                "confirm": "guest", "acknowledge_unknown": True}
        self.assertEqual(200, self.api("/api/operations/power-recovery/resolve", body)[0])


if __name__ == "__main__":
    unittest.main()
