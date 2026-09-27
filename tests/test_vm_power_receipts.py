import hashlib
import json
import os
import time
import unittest
from unittest import mock

import homestead_capacity_review as review
import homestead_operations as ops
import homestead_vm_power_receipts as receipts
import test_vm_power_job as fixtures


class PowerReceiptTests(unittest.TestCase):
    setUp = fixtures.VMPowerJobTests.setUp
    read = fixtures.VMPowerJobTests.read
    dispatch = fixtures.VMPowerJobTests.dispatch

    def path(self, name=receipts.STORE):
        return os.path.join(self.tmp.name, name)

    def write_fixture(self, value, name=receipts.STORE):
        with open(self.path(name), "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_expiry_pruning_and_backward_clock_never_resurrect_consumed_approval(self):
        now = int(time.time())
        with mock.patch.object(review, "_key", lambda: b"test-only-key"), mock.patch.object(time, "time", return_value=now):
            self.body["capacity_token"] = review.issue(self.body, self.context)
            self.assertTrue(review.valid(self.body, self.context))
            for clearing in ("dismiss", "prune"):
                with self.subTest(clearing=clearing), mock.patch.object(ops, "DATA_DIR", os.path.join(self.tmp.name, clearing)):
                    self.dispatch()
                    ops.list_operations()
                    # A fast HA replica or temporary forward clock jump clears
                    # expired display history; a slower replica sees it valid.
                    with mock.patch.object(time, "time", return_value=now + 601):
                        if clearing == "dismiss":
                            ops.dismiss_finished()
                        else:
                            with mock.patch.object(ops, "MAX_OPERATIONS", 0):
                                ops._write(ops._read())
                    self.assertEqual([], ops._read())
                    self.assertTrue(review.valid(self.body, self.context))
                    send = mock.Mock()
                    with self.assertRaisesRegex(ValueError, "already has job"):
                        self.dispatch(send=send)
                    send.assert_not_called()

    def test_ledger_contains_only_fingerprint_and_job_id_and_survives_clear(self):
        self.dispatch()
        job = ops.list_operations()[0]
        with mock.patch.object(time, "time", return_value=time.time() + 601):
            ops.dismiss(job["id"])
        with open(self.path(), encoding="utf-8") as handle:
            value = json.load(handle)
        self.assertEqual({"version": 1, "receipts": {hashlib.sha256(self.body["capacity_token"].encode()).hexdigest(): job["id"]}}, value)
        self.assertNotIn(self.body["capacity_token"], json.dumps(value))

    def test_missing_established_ledger_blocks_new_power_without_send(self):
        self.dispatch()
        os.remove(self.path())
        send = mock.Mock()
        with self.assertRaisesRegex(ValueError, "history is missing"):
            self.dispatch(send=send)
        send.assert_not_called()

    def test_malformed_and_null_ledger_or_marker_never_reinitialize(self):
        for name, values in ((receipts.STORE, [None, [], {}, {"version": 1, "receipts": {"bad": "bad"}}]),
                             (receipts.MARKER, [None, [], {}, {"version": 2}])):
            for value in values:
                with self.subTest(name=name, value=value):
                    self.write_fixture(value, name)
                    send = mock.Mock()
                    with self.assertRaises(ValueError):
                        self.dispatch(send=send)
                    send.assert_not_called()
                    with open(self.path(name), encoding="utf-8") as handle:
                        self.assertEqual(value, json.load(handle))
                    os.remove(self.path(name))

    def test_write_and_marker_failure_never_send_and_persisted_consumption_blocks_replay(self):
        original = receipts.SHARED.write_json
        for failed_name in (receipts.STORE, receipts.MARKER):
            with self.subTest(failed_name=failed_name):
                def write(path, *args, **kwargs):
                    if os.path.basename(path) == failed_name:
                        raise OSError("disk unavailable")
                    return original(path, *args, **kwargs)
                send = mock.Mock()
                with mock.patch.object(receipts.SHARED, "write_json", side_effect=write):
                    with self.assertRaises(OSError):
                        self.dispatch(send=send)
                send.assert_not_called()
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()

    def test_durable_ledger_precedes_job_and_any_dispatch(self):
        original = receipts.SHARED.write_json
        writes = []
        def write(path, *args, **kwargs):
            writes.append(os.path.basename(path))
            self.assertTrue(kwargs.get("durable"))
            return original(path, *args, **kwargs)
        def send():
            self.assertEqual([receipts.STORE, receipts.MARKER, ops.STORE, ops.STORE_MARKER], writes[:4])
            return {"ok": True}
        with mock.patch.object(receipts.SHARED, "write_json", side_effect=write):
            self.dispatch(send=mock.Mock(side_effect=send))
        self.assertEqual([receipts.STORE, receipts.MARKER, ops.STORE, ops.STORE_MARKER], writes[:4])

    def test_job_write_failure_after_consumption_never_sends_or_replays(self):
        original = receipts.SHARED.write_json
        def write(path, *args, **kwargs):
            if os.path.basename(path) == ops.STORE:
                raise OSError("disk unavailable")
            return original(path, *args, **kwargs)
        send = mock.Mock()
        with mock.patch.object(receipts.SHARED, "write_json", side_effect=write):
            with self.assertRaises(OSError):
                self.dispatch(send=send)
        self.assertEqual([], ops._read())
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch(send=send)
        send.assert_not_called()

    def test_unreadable_ledger_error_redacts_private_details(self):
        with mock.patch("builtins.open", side_effect=PermissionError("private-path")):
            with self.assertRaisesRegex(ValueError, "cannot be read") as caught:
                receipts.find(self.tmp.name, "a" * 64)
        self.assertNotIn("private-path", str(caught.exception))

    def test_existing_receipts_import_before_pruning(self):
        digest, ident = "a" * 64, "b" * 24
        item = {"id": ident, "kind": "vm-power", "status": "succeeded", "ref": {"review_digest": digest, "review_expires": 1}}
        with mock.patch.object(ops, "MAX_OPERATIONS", 0):
            ops._write([item])
        self.assertEqual([], ops._read())
        self.assertEqual(ident, receipts.find(self.tmp.name, digest))

    def test_conflicting_receipt_cannot_overwrite_first_owner(self):
        digest = "a" * 64
        receipts.remember(self.tmp.name, [{"id": "b" * 24, "kind": "vm-power", "ref": {"review_digest": digest}}])
        with self.assertRaisesRegex(ValueError, "another job"):
            receipts.remember(self.tmp.name, [{"id": "c" * 24, "kind": "vm-power", "ref": {"review_digest": digest}}])
        self.assertEqual("b" * 24, receipts.find(self.tmp.name, digest))


if __name__ == "__main__":
    unittest.main()
