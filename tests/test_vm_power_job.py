import copy
import multiprocessing
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

import test_vm_resources as fixtures
import homestead_operations as ops
import homestead_vm_power_job as power


def race_dispatch(directory, body, context, ready, go, result, sent):
    ops.bind(None, directory, None)
    ready.put(True)
    if not go.wait(10):
        result.put("timeout")
        return
    def send():
        with sent.get_lock():
            sent.value += 1
        return {"ok": True}
    try:
        power.dispatch(body, context, ops, send, lambda: None)
        result.put("accepted")
    except ValueError as error:
        result.put("duplicate" if "already has job" in str(error) else str(error))


class VMPowerJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vm = fixtures.vm()
        self.vmi = fixtures.child(self.vm, "VirtualMachine", "guest", "new-vmi")
        self.vmi["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
        self.context = {"observations": {"vm": self.vm["metadata"], "vmi": {"uid": "old-vmi"}}}
        self.body = {"action": "restart", "capacity_token": f"{int(time.time()) + 600}.test-approval"}
        for patch in (mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {"vm-power": lambda item: power.status(item, self.read)}),
                      mock.patch.dict(ops.CANCELLERS, {"vm-power": (power.cancel_plan, power.cancel_run)})):
            patch.start()
            self.addCleanup(patch.stop)

    def read(self, path):
        if "/virtualmachines/" in path:
            return copy.deepcopy(self.vm)
        if self.vmi is None:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.vmi)

    def dispatch(self, **kwargs):
        send = kwargs.pop("send", mock.Mock(return_value={"ok": True}))
        result = power.dispatch(self.body, self.context, ops, send, kwargs.pop("before_send", lambda: None))
        return result, send

    def test_expected_new_instance_completes_and_receipt_survives_dismiss_and_pruning(self):
        result, send = self.dispatch()
        self.assertEqual("accepted", ops._read()[0]["ref"]["phase"])
        job = ops.list_operations()[0]
        self.assertEqual("succeeded", job["status"])
        self.assertFalse(job["dismissible"])
        self.assertIn("guest application health is not verified", job["message"])
        with self.assertRaisesRegex(ValueError, "receipt"):
            ops.dismiss(job["id"])
        self.assertEqual(0, ops.dismiss_finished()["dismissed"])
        with mock.patch.object(ops, "MAX_OPERATIONS", 0):
            ops._write(ops._read())
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        self.assertEqual(1, send.call_count)

    def test_old_instance_or_same_name_wrong_owner_cannot_complete(self):
        self.dispatch()
        self.vmi["metadata"]["uid"] = "old-vmi"
        self.assertEqual("running", ops.list_operations()[0]["status"])
        self.vmi["metadata"]["uid"] = "new-vmi"
        self.vmi["metadata"]["ownerReferences"][0]["uid"] = "other-vm"
        self.assertEqual("running", ops.list_operations()[0]["status"])
        self.vmi["metadata"]["ownerReferences"][0]["uid"] = self.vm["metadata"]["uid"]
        self.vmi["metadata"]["deletionTimestamp"] = "now"
        self.assertEqual("running", ops.list_operations()[0]["status"])

    def test_running_without_ready_or_while_paused_is_not_complete(self):
        self.dispatch()
        for conditions in ([], [{"type": "Ready", "status": "False"}],
                           [{"type": "Ready", "status": "True"}, {"type": "Paused", "status": "True"}]):
            self.vmi["status"]["conditions"] = conditions
            self.assertEqual("running", ops.list_operations()[0]["status"])

    def test_unpause_must_follow_same_instance(self):
        self.body["action"] = "unpause"
        self.dispatch()
        self.assertEqual("running", ops.list_operations()[0]["status"])
        self.vmi["metadata"]["uid"] = "old-vmi"
        self.assertEqual("succeeded", ops.list_operations()[0]["status"])

    def test_missing_or_replaced_vm_instance_stays_inspectable(self):
        self.dispatch()
        self.vmi = None
        self.assertEqual("running", ops.list_operations()[0]["status"])
        self.vm["metadata"]["uid"] = "replacement"
        self.assertIn("identity changed", ops.list_operations()[0]["message"])

    def test_lost_response_never_auto_resolves_even_if_guest_looks_ready(self):
        send = mock.Mock(side_effect=TimeoutError("private details"))
        with self.assertRaisesRegex(ValueError, "outcome|response"):
            self.dispatch(send=send)
        for _ in range(3):
            job = ops.list_operations()[0]
            self.assertEqual("running", job["status"])
            self.assertFalse(job["cancellable"])
        self.assertEqual(1, send.call_count)
        with self.assertRaisesRegex(ValueError, "cannot safely"):
            ops.cancel(job["id"], confirm="guest")

    def test_explicit_refusal_is_not_replayed_after_dismiss_attempt(self):
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            self.dispatch(send=mock.Mock(side_effect=urllib.error.HTTPError("hidden", 403, "private", {}, None)))
        job = ops.list_operations()[0]
        self.assertEqual("failed", job["status"])
        self.assertEqual(0, ops.dismiss_finished()["dismissed"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()

    def test_server_error_is_uncertain_not_definitive_refusal(self):
        with self.assertRaises(ValueError):
            self.dispatch(send=mock.Mock(side_effect=urllib.error.HTTPError("hidden", 500, "private", {}, None)))
        self.assertEqual("uncertain", ops._read()[0]["ref"]["phase"])

    def test_failed_receipt_write_preserves_dispatch_intent_and_blocks_retry(self):
        original = ops.record_phase
        def record(ident, phase, *args, **kwargs):
            if phase == "accepted":
                raise OSError("lost disk")
            return original(ident, phase, *args, **kwargs)
        send = mock.Mock(return_value={"ok": True})
        with mock.patch.object(ops, "record_phase", side_effect=record):
            with self.assertRaisesRegex(ValueError, "accepted power but its receipt"):
                self.dispatch(send=send)
        self.assertEqual("dispatching", ops._read()[0]["ref"]["phase"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch(send=send)
        send.assert_called_once()

    def test_pre_dispatch_journal_failure_does_not_send(self):
        send = mock.Mock()
        with mock.patch.object(ops, "record_phase", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.dispatch(send=send)
        send.assert_not_called()
        self.assertEqual("prepared", ops._read()[0]["ref"]["phase"])

    def test_cancel_before_dispatch_fences_worker_without_sending_power(self):
        send = mock.Mock()
        def cancel_before_send():
            ops.cancel(ops._read()[0]["id"], confirm="guest")
        with self.assertRaisesRegex(ValueError, "job has ended"):
            self.dispatch(send=send, before_send=cancel_before_send)
        send.assert_not_called()
        self.assertEqual("cancelled", ops._read()[0]["status"])

    def test_cancelling_prepared_job_cannot_advance_to_dispatching(self):
        original = power.cancel_run
        def cancel_with_race(item, options):
            with self.assertRaisesRegex(ValueError, "job has ended"):
                ops.record_phase(item["id"], "dispatching", 10, "must not advance")
            return original(item, options)
        with mock.patch.dict(ops.CANCELLERS, {"vm-power": (power.cancel_plan, cancel_with_race)}):
            def cancel():
                ops.cancel(ops._read()[0]["id"], confirm="guest")
            send = mock.Mock()
            with self.assertRaises(ValueError):
                self.dispatch(send=send, before_send=cancel)
            send.assert_not_called()

    def test_stop_tracking_known_accepted_job_keeps_one_shot_approval(self):
        result, _ = self.dispatch()
        ident = result["operation"]["id"]
        ops.cancel(ident, confirm="guest")
        self.assertEqual("cancelled", ops._read()[0]["status"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()

    def test_expired_finished_receipt_can_be_dismissed(self):
        self.dispatch()
        job = ops.list_operations()[0]
        with mock.patch.object(ops.time, "time", return_value=time.time() + 601):
            ops.dismiss(job["id"])
        self.assertEqual([], ops._read())

    def test_two_processes_with_same_approval_send_only_once(self):
        context = multiprocessing.get_context("spawn")
        ready, result, go, sent = context.Queue(), context.Queue(), context.Event(), context.Value("i", 0)
        children = [context.Process(target=race_dispatch, args=(self.tmp.name, self.body, self.context, ready, go, result, sent)) for _ in range(2)]
        try:
            for child in children:
                child.start()
            for _ in children:
                self.assertTrue(ready.get(timeout=10))
            go.set()
            self.assertEqual(["accepted", "duplicate"], sorted(result.get(timeout=10) for _ in children))
            for child in children:
                child.join(10)
                self.assertEqual(0, child.exitcode)
            self.assertEqual(1, sent.value)
            self.assertEqual(1, len(ops._read()))
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            ready.close()
            result.close()


if __name__ == "__main__":
    unittest.main()
