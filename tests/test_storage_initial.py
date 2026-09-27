"""Real initial reviews and shared-store receipts through the first stop."""
import copy
import tempfile
import unittest
from unittest import mock

import test_reclass_review as fixtures
import homestead_reclass as rc
import homestead_storage_workflow as workflow
import homestead_operations as ops
import homestead_vm_power_receipts as receipts


class Crash(BaseException):
    pass


class InitialStorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", directory.name); patch.start(); self.addCleanup(patch.stop)
        self.body, self.sent, self.lose_reply = self.fixture.body, [], False
        dep = self.fixture.cluster.deps["frigate"]
        dep.update(apiVersion="apps/v1", kind="Deployment")
        dep["metadata"].update(namespace="lab")
        patch = mock.patch.object(rc, "ksend", self.send); patch.start(); self.addCleanup(patch.stop)

    def approved(self):
        result = workflow.preview(self.body, "admin", ops)
        self.assertTrue(result["ok"], result["blockers"])
        return {**self.body, "confirm_capacity": True, "capacity_token": result["capacity_token"]}

    def load(self):
        with ops._lock: return ops._read()[0]

    def send(self, method, path, body, **kwargs):
        record = self.load()["ref"]["storage_writes"][-1]
        self.assertEqual("intent", record["state"])
        self.assertEqual(path, record["target"]["path"])
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/frigate"), (method, path))
        dep = self.fixture.cluster.deps["frigate"]
        self.assertEqual(dep["metadata"]["uid"], body["metadata"]["uid"])
        self.assertEqual(dep["metadata"]["resourceVersion"], body["metadata"]["resourceVersion"])
        dep["spec"].update(copy.deepcopy(body["spec"]))
        dep["metadata"].update(copy.deepcopy(body["metadata"]))
        dep["metadata"]["resourceVersion"] = str(int(dep["metadata"]["resourceVersion"]) + 1)
        self.sent.append((method, path))
        if self.lose_reply: raise TimeoutError("lost response")
        return copy.deepcopy(dep)

    def poll(self, checkpoint=None):
        with ops._lock:
            item = self.load()
            outcome = workflow.resolve(item, checkpoint or ops.checkpoint,
                                       lambda *_: {"blocked": False}, lambda *_: {"blocked": False})
            ops._finish(item, *outcome)
            ops._write([item])
            return outcome

    def test_new_job_is_durable_before_stop_and_approval_cannot_replay_after_history_clear(self):
        body = self.approved()
        public = workflow.start_reviewed(body, "admin", ops)
        item = self.load()
        self.assertEqual("queued", public["status"])
        self.assertEqual((1, "handoff", "stop"), tuple(item["ref"][key] for key in ("storage_protocol", "phase", "handoff_phase")))
        self.assertEqual(public["id"], receipts.find(ops.DATA_DIR, item["ref"]["review_digest"]))
        self.assertEqual([], self.sent)
        self.assertNotIn(body["capacity_token"], str(item))
        # Simulate compact display history being cleared: the independent
        # consumption receipt still rejects the original approval.
        with ops._lock: ops._write([])
        with self.assertRaisesRegex(ValueError, "approval already"):
            workflow.start_reviewed(body, "admin", ops)
        self.assertEqual([], self.sent)

    def test_legacy_review_and_other_actor_do_not_authorize_new_protocol(self):
        old = rc.preview(self.body, "admin", ops)
        for body, actor in (({**self.body, "capacity_token": old["capacity_token"], "confirm_capacity": True}, "admin"),
                            (self.approved(), "other-admin")):
            with self.assertRaises(ValueError): workflow.start_reviewed(body, actor, ops)
        self.assertEqual([], ops._read())
        self.assertEqual([], self.sent)

    def test_initial_store_failure_cannot_stop_a_workload(self):
        body = self.approved()
        with mock.patch.object(ops, "_write", side_effect=OSError("store unavailable")):
            with self.assertRaises(OSError): workflow.start_reviewed(body, "admin", ops)
        self.assertEqual([], self.sent)

    def test_source_change_after_queueing_holds_before_the_first_stop(self):
        workflow.start_reviewed(self.approved(), "admin", ops)
        self.fixture.cluster.pvs["pv-old"]["metadata"]["uid"] = "replacement"
        self.assertEqual("failed", self.poll()[0])
        self.assertEqual([], self.sent)
        self.assertFalse(self.load()["ref"].get("storage_writes"))

    def test_history_failure_during_stop_checkpoint_never_sends_the_stop(self):
        workflow.start_reviewed(self.approved(), "admin", ops)
        with mock.patch.object(ops, "checkpoint", side_effect=OSError("store unavailable")):
            self.assertEqual("failed", self.poll()[0])
        self.assertEqual([], self.sent)
        self.assertEqual("intent", self.load()["ref"]["storage_writes"][0]["state"])

    def test_first_stop_is_checkpointed_and_advances_only_after_accepted_receipt(self):
        workflow.start_reviewed(self.approved(), "admin", ops)
        self.assertEqual("running", self.poll()[0])
        ref = self.load()["ref"]
        self.assertEqual("copy", ref["handoff_phase"])
        self.assertEqual(["accepted"], [row["state"] for row in ref["storage_writes"]])
        self.assertEqual(1, len(self.sent))

    def test_crash_after_intent_never_replays_an_unsent_or_uncertain_stop(self):
        workflow.start_reviewed(self.approved(), "admin", ops)
        def crash(item):
            ops.checkpoint(item)
            raise Crash()
        with self.assertRaises(Crash): self.poll(crash)
        self.assertEqual([], self.sent)
        self.assertEqual("intent", self.load()["ref"]["storage_writes"][0]["state"])
        self.assertEqual("failed", self.poll()[0])
        self.assertEqual([], self.sent)
        self.assertTrue(self.load()["ref"]["retain_resources"])

    def test_crash_after_accepted_receipt_observes_stop_without_resending(self):
        workflow.start_reviewed(self.approved(), "admin", ops)
        def crash(item):
            ops.checkpoint(item)
            if item["ref"]["storage_writes"][-1]["state"] == "accepted": raise Crash()
        with self.assertRaises(Crash): self.poll(crash)
        self.assertEqual("stop", self.load()["ref"]["handoff_phase"])
        self.assertEqual("running", self.poll()[0])
        self.assertEqual("copy", self.load()["ref"]["handoff_phase"])
        self.assertEqual(1, len(self.sent))

    def test_lost_stop_response_holds_both_resources_and_never_repeats(self):
        workflow.start_reviewed(self.approved(), "admin", ops)
        self.lose_reply = True
        self.assertEqual("failed", self.poll()[0])
        self.assertEqual("uncertain", self.load()["ref"]["storage_writes"][0]["state"])
        self.assertEqual("failed", self.poll()[0])
        self.assertEqual(1, len(self.sent))
