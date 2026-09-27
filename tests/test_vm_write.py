import copy
import io
import json
from pathlib import Path
import sys
import traceback
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vm_write as writes


class VMWriteTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.path = "/api/v1/namespaces/lab/secrets"
        self.body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "lab", "name": "guest-login"},
                     "data": {"userdata": "private-cloud-init-and-password"}}
        self.response = {**copy.deepcopy(self.body), "metadata": {**self.body["metadata"], "uid": "secret-uid", "resourceVersion": "7"}}
        self.send = mock.Mock(side_effect=self.sent)
        self.writer = writes.ResourceWriter(self.send, self.events.append)

    def sent(self, method, path, body=None, **kwargs):
        self.assertEqual("intent", self.events[-1]["phase"])
        return copy.deepcopy(self.response)

    def test_records_intent_before_send_and_only_safe_identity_after(self):
        result = self.writer("POST", self.path, self.body, ctype="application/json")
        self.assertEqual(self.response, result)
        self.assertEqual(["intent", "accepted"], [event["phase"] for event in self.events])
        self.assertEqual({"uid": "secret-uid", "resourceVersion": "7"}, self.events[-1]["identity"])
        self.assertNotIn("private-cloud-init", json.dumps(self.events))
        self.send.assert_called_once_with("POST", self.path, self.body, ctype="application/json")
        self.writer.check()

    def test_update_requires_and_records_existing_uid_version(self):
        body = {"metadata": {"uid": "secret-uid", "resourceVersion": "6"}, "data": self.body["data"]}
        self.writer("PATCH", self.path + "/guest-login", body)
        self.assertEqual(body["metadata"], self.events[0]["before"])
        self.assertEqual("7", self.events[-1]["identity"]["resourceVersion"])

    def test_unfenced_update_is_rejected_before_journal_or_send(self):
        with self.assertRaises(writes.WriteFailure):
            self.writer("PATCH", self.path + "/guest-login", {"data": self.body["data"]})
        self.send.assert_not_called()
        self.assertEqual([], self.events)

    def test_unsupported_paths_and_mismatched_targets_never_send(self):
        cases = [("DELETE", self.path, self.body), ("POST", self.path + "?token=private", self.body),
                 ("POST", "https://private.test" + self.path, self.body),
                 ("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachines/guest/restart", {}),
                 ("POST", self.path, {**self.body, "kind": "Pod"}),
                 ("POST", self.path, {**self.body, "metadata": {"name": "../escape"}}),
                 ("POST", self.path, {**self.body, "metadata": {"name": "guest-login", "namespace": "different"}})]
        for method, path, body in cases:
            with self.subTest(path=path, method=method):
                writer = writes.ResourceWriter(self.send, self.events.append)
                with self.assertRaises(writes.WriteFailure):
                    writer(method, path, body)
        self.send.assert_not_called()
        self.assertEqual([], self.events)

    def test_intent_failure_sends_nothing_and_latches_writer(self):
        record = mock.Mock(side_effect=OSError("private-storage-details"))
        writer = writes.ResourceWriter(self.send, record)
        with self.assertRaisesRegex(writes.WriteFailure, "could not be recorded") as caught:
            writer("POST", self.path, self.body)
        self.assertNotIn("private", str(caught.exception))
        with self.assertRaises(writes.WriteFailure):
            writer("POST", self.path, self.body)
        self.send.assert_not_called()
        self.assertEqual(1, record.call_count)

    def test_lost_response_never_retries_or_sends_later_steps(self):
        self.writer("POST", self.path, self.body)
        self.send.side_effect = TimeoutError("private-data")
        with self.assertRaisesRegex(writes.WriteFailure, "uncertain"):
            self.writer("POST", self.path, self.body)
        with self.assertRaises(writes.WriteFailure):
            self.writer("POST", self.path, self.body)
        self.assertEqual(2, self.send.call_count)
        self.assertEqual(["intent", "accepted", "intent", "uncertain"], [row["phase"] for row in self.events])
        self.assertEqual([1, 1, 2, 2], [row["sequence"] for row in self.events])

    def test_explicit_refusal_and_server_error_are_distinct_and_redacted(self):
        for status, phase in ((403, "refused"), (409, "refused"), (500, "uncertain"), (429, "uncertain")):
            with self.subTest(status=status):
                events = []
                error = urllib.error.HTTPError("https://private.example", status, "private-body", {}, io.BytesIO(b"private-cloud-init"))
                send = mock.Mock(side_effect=error)
                writer = writes.ResourceWriter(send, events.append)
                try:
                    writer("POST", self.path, self.body)
                except writes.WriteFailure as caught:
                    self.assertNotIn("private", "".join(traceback.format_exception(caught)))
                else:
                    self.fail("write must fail")
                self.assertEqual(phase, events[-1]["phase"])
                self.assertEqual(status, events[-1]["http_status"])
                self.assertNotIn("private", json.dumps(events))
                send.assert_called_once()

    def test_unverified_response_identity_never_becomes_a_receipt(self):
        for response in (None, {}, {"metadata": {"name": "replacement", "namespace": "lab"}},
                         {"metadata": self.response["metadata"]},
                         {**self.response, "metadata": {**self.response["metadata"], "resourceVersion": None}},
                         {**self.response, "kind": "WrongKind"}):
            with self.subTest(response=response):
                events = []
                writer = writes.ResourceWriter(mock.Mock(return_value=response), events.append)
                with self.assertRaisesRegex(writes.WriteFailure, "could not be verified"):
                    writer("POST", self.path, self.body)
                self.assertEqual("unverified", events[-1]["phase"])
                self.assertNotIn("identity", events[-1])
                self.assertNotIn("private", json.dumps(events))

    def test_replacement_uid_cannot_confirm_update(self):
        with self.assertRaises(writes.WriteFailure):
            self.writer("PATCH", self.path + "/guest-login", {"metadata": {"uid": "original-uid", "resourceVersion": "6"}})
        self.assertEqual("unverified", self.events[-1]["phase"])

    def test_resource_versions_are_opaque_and_retained_without_normalization(self):
        self.response["metadata"]["resourceVersion"] = "opaque:next/abc==+value"
        before = {"uid": "secret-uid", "resourceVersion": "opaque:prior/abc==+value"}
        self.writer("PATCH", self.path + "/guest-login", {"metadata": before})
        self.assertEqual(before, self.events[0]["before"])
        self.assertEqual(self.response["metadata"]["resourceVersion"], self.events[-1]["identity"]["resourceVersion"])

    def test_receipt_persistence_failure_keeps_intent_and_requires_inspection(self):
        def record(event):
            if event["phase"] != "intent":
                raise OSError("private-storage")
            self.events.append(event)
        writer = writes.ResourceWriter(self.send, record)
        with self.assertRaises(writes.WriteFailure):
            writer("POST", self.path, self.body)
        self.assertEqual(["intent"], [row["phase"] for row in self.events])
        self.send.assert_called_once()
        with self.assertRaises(writes.WriteFailure):
            writer.check()

    def test_callback_cannot_rewrite_the_intent_used_for_receipt_validation(self):
        events = []
        def record(event):
            events.append(copy.deepcopy(event))
            event["resource"]["name"] = "replacement"
        writer = writes.ResourceWriter(mock.Mock(return_value=self.response), record)
        writer("POST", self.path, self.body)
        self.assertEqual(["guest-login", "guest-login"], [row["resource"]["name"] for row in events])


if __name__ == "__main__":
    unittest.main()
