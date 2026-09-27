import copy
import hashlib
import multiprocessing
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

import test_vm_resources  # server module path
import homestead_operations as ops
import homestead_vm_mutation_job as job
import homestead_vm_mutation_recovery as recovery
import homestead_vm_power_job as power
import server
import test_vm_power_recovery as power_recovery_tests

PATH = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"
VM = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
      "metadata": {"namespace": "lab", "name": "guest", "uid": "vm-uid", "resourceVersion": "v1"},
      "spec": {"private": "test-secret-value"}}


def hold_worker(directory, ident, ready, finish):
    ops.bind(None, directory, None)
    with power.worker_lock(ident, ops):
        ready.set()
        finish.wait(10)


class VMMutationJobTests(unittest.TestCase):
    api = power_recovery_tests.VMPowerRecoveryTests.api
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patch in (mock.patch.object(ops, "DATA_DIR", temporary.name),
                      mock.patch.object(recovery.REVIEW, "_key", return_value=b"mutation-tests")):
            patch.start()
            self.addCleanup(patch.stop)
        self.vm = copy.deepcopy(VM)
        self.body = {"capacity_token": f"{int(time.time()) + 600}.mutation-test"}
        self.send = mock.Mock(side_effect=lambda *a, **kw: copy.deepcopy(self.vm))

    def dispatch(self, commit=None, kind="vm-edit", body=None):
        def save(writer):
            writer("PUT", PATH, VM)
            return {"ok": True, "vm_identity": copy.deepcopy(VM["metadata"])}
        return job.dispatch(kind, body or self.body, "lab", "guest", VM["metadata"], ops, self.send, commit or save)

    def failed(self):
        self.send.side_effect = TimeoutError("test-secret-value")
        with self.assertRaisesRegex(ValueError, "Inspect job") as error:
            self.dispatch()
        self.assertNotIn("test-secret-value", str(error.exception))
        return ops._read()[0]["id"]

    def read(self, path):
        self.assertEqual(PATH, path)
        return copy.deepcopy(self.vm)

    def approved(self, ident):
        result = recovery.preview(ident, ops, self.read, "admin")
        return {"id": ident, "capacity_token": result["capacity_token"], "confirm_capacity": True,
                "confirm": "guest", "acknowledge_unknown": True}

    def test_success_means_acknowledged_config_not_guest_health_and_cannot_replay(self):
        result = self.dispatch()
        self.assertEqual("succeeded", result["operation"]["status"])
        self.assertIn("health are not verified", result["operation"]["message"])
        item = ops._read()[0]
        self.assertEqual("accepted", item["ref"]["writes"][0]["phase"])
        self.assertFalse(item["ref"]["retain_resources"])
        self.assertNotIn("test-secret-value", str(item))
        self.assertNotIn("writes", result["operation"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        self.assertEqual(1, self.send.call_count)

    def test_clock_rollback_and_clear_history_do_not_resurrect_create_or_edit_approval(self):
        for kind in ("vm-create", "vm-edit"):
            body = {"capacity_token": f"{int(time.time()) - 1}.{kind}"}
            self.dispatch(kind=kind, body=body)
            ops.dismiss_finished()
            with mock.patch.object(time, "time", return_value=1), self.assertRaisesRegex(ValueError, "already has job"):
                self.dispatch(kind=kind, body=body)
        self.assertEqual([], ops._read())
        self.assertEqual(2, self.send.call_count)

    def test_failure_retains_receipts_and_blocks_new_create_edit_and_power(self):
        ident = self.failed()
        item = ops._read()[0]
        self.assertEqual("uncertain", item["ref"]["writes"][0]["phase"])
        self.assertTrue(ops._public(item)["mutation_recovery"])
        self.assertFalse(ops._public(item)["cancellable"])
        self.assertEqual(0, ops.dismiss_finished()["dismissed"])
        for kind in ("vm-create", "vm-edit", "vm-power"):
            with self.assertRaisesRegex(ValueError, "needs recovery"):
                ops.start(kind, "new", {}, "/vms", {"namespace": "lab", "name": "guest", "review_digest": hashlib.sha256(kind.encode()).hexdigest()})
        self.assertEqual(ident, ops._read()[0]["id"])

    def test_partial_failure_and_swallowed_warning_never_become_success(self):
        def commit(writer):
            writer("PUT", PATH, VM)
            self.send.side_effect = urllib.error.HTTPError(PATH, 422, "test-secret-value", {}, None)
            try:
                writer("PUT", PATH, VM)
            except ValueError:
                pass
            return {"ok": True, "warning": "ignored write failure"}
        with self.assertRaises(ValueError):
            self.dispatch(commit)
        item = ops._read()[0]
        self.assertEqual("failed", item["status"])
        self.assertEqual(["accepted", "refused"], [row["phase"] for row in item["ref"]["writes"]])
        self.assertTrue(item["ref"]["retain_resources"])

    def test_prewrite_admission_failure_sends_nothing_but_consumes_approval(self):
        def commit(writer):
            raise ValueError("fresh admission changed")
        with self.assertRaisesRegex(ValueError, "fresh admission"):
            self.dispatch(commit)
        self.send.assert_not_called()
        self.assertFalse(ops._read()[0]["ref"]["retain_resources"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()

    def test_history_write_failure_never_sends(self):
        with mock.patch.object(ops, "_write", side_effect=OSError("disk")), self.assertRaises(OSError):
            self.dispatch()
        self.send.assert_not_called()

    def test_unverified_completion_retains_receipts_instead_of_claiming_success(self):
        def commit(writer):
            writer("PUT", PATH, VM)
            return {"ok": True, "vm_identity": {**VM["metadata"], "uid": "wrong-vm"}}
        with self.assertRaises(ValueError):
            self.dispatch(commit)
        self.assertTrue(ops._read()[0]["ref"]["retain_resources"])
        self.assertEqual("failed", ops._read()[0]["status"])

    def test_resolution_is_read_only_audited_unknown_and_consumes_old_token(self):
        ident = self.failed()
        good = self.approved(ident)
        result = recovery.resolve(good, ops, self.read, "admin")
        self.assertEqual(1, self.send.call_count)
        self.assertEqual("failed", result["operation"]["status"])
        self.assertFalse(result["operation"]["mutation_recovery"])
        ref = ops._read()[0]["ref"]
        self.assertEqual(("admin", "unknown"), (ref["recovery"]["by"], ref["recovery"]["outcome"]))
        self.assertFalse(ref["retain_resources"])
        self.assertNotIn("test-secret-value", str(ref))
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        with self.assertRaises(ValueError):
            recovery.resolve(good, ops, self.read, "admin")

    def test_fresh_identity_actor_and_explicit_acknowledgement_required(self):
        ident = self.failed()
        good = self.approved(ident)
        for changed in ({"confirm": "wrong"}, {"acknowledge_unknown": False}, {"capacity_token": ""}):
            with self.assertRaises(ValueError):
                recovery.resolve({**good, **changed}, ops, self.read, "admin")
        with self.assertRaises(recovery.REVIEW.Rejected):
            recovery.resolve(good, ops, self.read, "other-admin")
        self.vm["metadata"]["resourceVersion"] = "v2"
        with self.assertRaises(recovery.REVIEW.Rejected):
            recovery.resolve(good, ops, self.read, "admin")
        self.assertTrue(ops._read()[0]["ref"]["retain_resources"])

    def test_missing_replacement_and_deleting_resources_are_not_adopted(self):
        ident = self.failed()
        self.vm["metadata"]["uid"] = "replacement"
        plan = recovery.preview(ident, ops, self.read, "admin")["plan"]
        self.assertEqual("replacement; not adopted", plan["resources"][0]["relationship"])
        self.vm["metadata"]["deletionTimestamp"] = "now"
        self.assertTrue(recovery.preview(ident, ops, self.read, "admin")["plan"]["blocked"])
        missing = mock.Mock(side_effect=urllib.error.HTTPError(PATH, 404, "not found", {}, None))
        plan = recovery.preview(ident, ops, missing, "admin")["plan"]
        self.assertEqual("not found", plan["resources"][0]["relationship"])
        self.assertIsNone(plan["resources"][0]["current"])

    def test_unavailable_inventory_or_store_is_never_successful_recovery(self):
        ident = self.failed()
        for value in ({}, [], TimeoutError("test-secret-value"), urllib.error.HTTPError(PATH, 403, "test-secret-value", {}, None)):
            read = mock.Mock(side_effect=value) if isinstance(value, Exception) else mock.Mock(return_value=value)
            with self.assertRaises(ValueError) as error:
                recovery.preview(ident, ops, read, "admin")
            self.assertNotIn("test-secret-value", str(error.exception))
        good = self.approved(ident)
        with mock.patch.object(ops, "_write", side_effect=OSError("disk")), self.assertRaises(OSError):
            recovery.resolve(good, ops, self.read, "admin")
        self.assertTrue(ops._read()[0]["ref"]["retain_resources"])

    def test_active_dispatcher_in_another_process_blocks_inspection_and_resolution(self):
        ident = self.failed()
        good = self.approved(ident)
        ctx = multiprocessing.get_context("spawn")
        ready, finish = ctx.Event(), ctx.Event()
        child = ctx.Process(target=hold_worker, args=(ops.DATA_DIR, ident, ready, finish))
        child.start()
        try:
            self.assertTrue(ready.wait(10))
            result = recovery.preview(ident, ops, self.read, "admin")
            self.assertTrue(result["plan"]["blocked"])
            self.assertIsNone(result["capacity_token"])
            with self.assertRaisesRegex(ValueError, "dispatcher is active"):
                recovery.resolve(good, ops, self.read, "admin")
        finally:
            finish.set()
            child.join(10)
            if child.is_alive():
                child.terminate()
                child.join(5)

    def test_orphan_prepared_job_can_be_resolved_without_replaying_and_is_fenced(self):
        ident = self.failed()
        items = ops._read()
        items[0].update(status="queued")
        items[0]["ref"].update(phase="prepared", writes=[])
        ops._write(items)
        recovery.resolve(self.approved(ident), ops, self.read, "admin")
        with self.assertRaisesRegex(ValueError, "job has ended"):
            ops.record_phase(ident, "writing", 0, "late worker")

    def test_recovery_routes_are_admin_only_and_monitor_does_not_write(self):
        for suffix in ("preview", "resolve"):
            self.assertIn("/api/operations/vm-recovery/" + suffix, server.ADMIN_ROUTES)
        item = {"progress": 20, "ref": {"phase": "writing"}}
        self.assertEqual("running", job.status(item)[0])
        self.assertFalse(job.cancel_plan(item)["can"])
        self.send.assert_not_called()

    def test_actual_recovery_routes_enforce_roles_and_never_mutate_cluster(self):
        ident = self.failed()
        for role in ("operator", "viewer"):
            for suffix in ("preview", "resolve"):
                self.assertEqual(403, self.api("/api/operations/vm-recovery/" + suffix, {"id": ident}, role)[0])
        preview = self.api("/api/operations/vm-recovery/preview", {"id": ident})
        self.assertEqual(200, preview[0], preview)
        result = self.api("/api/operations/vm-recovery/resolve", {
            "id": ident, "capacity_token": preview[1]["capacity_token"], "confirm_capacity": True,
            "confirm": "guest", "acknowledge_unknown": True})
        self.assertEqual(200, result[0], result)
        self.assertFalse(result[1]["operation"]["mutation_recovery"])
