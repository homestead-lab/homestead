import copy
import json
import tempfile
import unittest
from unittest import mock

import test_vm_write as fixtures
import homestead_vm_write as writes
import homestead_operations as ops


class VMWriteJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.fixture = fixtures.VMWriteTests()
        self.fixture.setUp()
        self.job = ops.start("vm-edit", "Save guest", {}, "/vms", {"namespace": "lab", "name": "guest", "phase": "prepared", "review_digest": "a" * 64})
        self.record = writes.operation_recorder(ops, self.job["id"])
        self.writer = writes.ResourceWriter(self.send, self.record)
        self.sent = []

    def send(self, *args, **kwargs):
        self.assertEqual("intent", ops._read()[0]["ref"]["writes"][-1]["phase"])
        self.sent.append(args)
        return copy.deepcopy(self.fixture.response)

    def write(self, writer=None):
        return (writer or self.writer)("POST", self.fixture.path, self.fixture.body)

    def test_durable_record_exists_at_send_and_contains_no_secret_values(self):
        self.write()
        job = ops._read()[0]
        self.assertEqual("accepted", job["ref"]["writes"][0]["phase"])
        self.assertEqual("secret-uid", job["ref"]["writes"][0]["identity"]["uid"])
        self.assertNotIn("private-cloud-init", json.dumps(job))
        self.assertEqual("running", job["status"])  # a receipt is not overall completion

    def test_new_writer_cannot_replay_existing_job_stream(self):
        self.write()
        another = writes.ResourceWriter(self.send, self.record)
        with self.assertRaises(writes.WriteFailure):
            self.write(another)
        self.assertEqual(1, len(self.sent))
        self.assertEqual("accepted", ops._read()[0]["ref"]["writes"][0]["phase"])

    def test_lost_response_is_retained_as_failed_recovery_and_cannot_be_cleared(self):
        self.writer.send = mock.Mock(side_effect=TimeoutError("private"))
        with self.assertRaises(writes.WriteFailure):
            self.write()
        job = ops._read()[0]
        self.assertEqual(("failed", "uncertain"), (job["status"], job["ref"]["writes"][0]["phase"]))
        self.assertEqual(0, ops.dismiss_finished()["dismissed"])
        with self.assertRaises(ValueError):
            ops.dismiss(job["id"])
        self.assertNotIn("private", json.dumps(job))

    def test_intent_disk_failure_never_sends(self):
        with mock.patch.object(ops, "_write", side_effect=OSError("private")), self.assertRaises(writes.WriteFailure):
            self.write()
        self.assertEqual([], self.sent)

    def test_failed_receipt_write_keeps_durable_intent_without_replay(self):
        original = ops._write
        def save(items):
            if items[0]["ref"]["writes"][-1]["phase"] == "accepted":
                raise OSError("private")
            return original(items)
        with mock.patch.object(ops, "_write", side_effect=save), self.assertRaises(writes.WriteFailure):
            self.write()
        self.assertEqual(1, len(self.sent))
        self.assertEqual("intent", ops._read()[0]["ref"]["writes"][-1]["phase"])
        with self.assertRaises(writes.WriteFailure):
            self.write(writes.ResourceWriter(self.send, self.record))
        self.assertEqual(1, len(self.sent))

    def test_ended_job_fences_any_further_write(self):
        items = ops._read()
        items[0]["status"] = "cancelled"
        ops._write(items)
        with self.assertRaises(writes.WriteFailure):
            self.write()
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
