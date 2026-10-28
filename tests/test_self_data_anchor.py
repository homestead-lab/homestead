"""Independent control history survives either PVC and rejects competing workers."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_kube as K
import homestead_operations as OPS
import homestead_self_data_anchor as anchor
from homestead_storage_journal import Held, Journal
from fakes.kube import Kube


ANCHOR = "/api/v1/namespaces/lab/configmaps/homestead-data-handoff"
OP = "1234567890abcdef12345678"


class Setup:
    """The claim the journal writes to, on the shared fake API
    (tests/fakes/kube.py), and where faults are put in: a request lost
    before it is done, or a reply lost after."""

    def __init__(self, test):
        self.pvc = "/api/v1/namespaces/lab/persistentvolumeclaims/data"
        # The shared fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under its DATA_DIR.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
        self.kube = test.enterContext(Kube({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"namespace": "lab", "name": "data", "uid": "pvc-old", "resourceVersion": "1"},
            "spec": {"volumeName": "old-pv"}}))
        self.read, self.send = K.kget, K.ksend

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def lose_reply(self, path, method, skip=0):
        """The request after skip of them is done, and the caller never hears."""
        self.kube.fail(path, TimeoutError("secret response diagnostic"), method=method, skip=skip, applied=True)

    def reject(self, path, method, skip=0):
        """The request after skip of them is not done: the caller gets an error."""
        self.kube.fail(path, OSError("secret API diagnostic"), method=method, skip=skip)

    def discard(self, path):
        self.kube.discard(path)

    def stored(self):
        return [self.kube.object(path) for path in (self.pvc, ANCHOR) if self.kube.object(path)] + [body for _, _, body, _ in self.kube.writes]



class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Setup(self)
        self.record = self.fresh()
        self.handle = self.create(self.record)

    def fresh(self):
        return anchor.Anchor(self.cluster.read, self.cluster.send, "lab", "homestead")

    def create(self, record):
        return record.create(operation=OP,
            deployment={"name": "homestead", "uid": "dep-1", "resourceVersion": "1"},
            source={"name": "data", "uid": "pvc-old", "resourceVersion": "1"},
            destination="data-new", replicas=2)

    def restored(self):
        return self.fresh().load(**self.handle)

    def writer(self, record):
        return Journal(record.item(), self.cluster.read, self.cluster.send, record.checkpoint)

    def patch(self, writer):
        return writer.write("retain", "PATCH", self.cluster.pvc, {"spec": {"volumeName": "retained"}},
            expected={"uid": "pvc-old", "resourceVersion": "1"}, ctype="application/merge-patch+json")

    def test_control_record_has_no_pvc_or_controller_owner_and_survives_restart(self):
        self.patch(self.writer(self.record))
        restored = self.restored()
        self.assertEqual(self.record.state, restored.state)
        self.patch(self.writer(restored))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))
        obj = self.cluster.read(self.record.path)
        self.assertNotIn("ownerReferences", obj["metadata"])
        self.assertEqual("accepted", restored.item()["ref"]["storage_writes"][0]["state"])

    def test_two_workers_cannot_dispatch_the_same_initial_write(self):
        stale = self.restored()
        self.patch(self.writer(self.record))
        # Restore original PVC shape/version to model two workers inspecting
        # before the winner wrote. The control CAS still excludes the loser.
        pvc = self.cluster.kube.object(self.cluster.pvc)
        pvc["metadata"]["resourceVersion"] = "1"
        self.cluster.kube.add(pvc)
        with self.assertRaises(Held):
            self.patch(self.writer(stale))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))
        self.assertTrue(stale.failed)

    def test_lost_intent_checkpoint_sends_no_resource_write_or_retry(self):
        self.cluster.lose_reply(self.record.path, "PUT")
        writer = self.writer(self.record)
        with self.assertRaises(Held): self.patch(writer)
        self.assertEqual("intent", self.restored().item()["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(Held): self.patch(self.writer(self.restored()))
        self.assertEqual(0, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def test_lost_resource_response_remains_uncertain_after_restart(self):
        self.cluster.lose_reply(self.cluster.pvc, "PATCH")
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        self.assertEqual("uncertain", self.restored().item()["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(Held): self.patch(self.writer(self.restored()))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def test_lost_accepted_checkpoint_can_be_observed_not_resent(self):
        self.cluster.lose_reply(self.record.path, "PUT", skip=1)
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        self.patch(self.writer(self.restored()))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def test_failed_accepted_checkpoint_leaves_intent_not_retry_permission(self):
        self.cluster.reject(self.record.path, "PUT", skip=1)
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        self.assertEqual("intent", self.restored().item()["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(Held): self.patch(self.writer(self.restored()))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def status_write_before_first_request(self):
        """A controller writes the claim's status between the journal's read and its request (#826)."""
        done = []

        def status_written(method, path, body):
            if not done:
                done.append(path)
                pvc = self.cluster.kube.object(path)
                pvc["status"] = {"phase": "Bound"}
                pvc["metadata"]["resourceVersion"] = "2"
                self.cluster.kube.add(pvc)
        self.cluster.kube.react("PATCH", self.cluster.pvc, status_written)   # answers nothing: the fake goes on

    def test_a_status_conflict_retry_is_recorded_and_survives_restart(self):
        self.status_write_before_first_request()
        self.patch(self.writer(self.record))
        entry = self.restored().item()["ref"]["storage_writes"][0]
        self.assertEqual("accepted", entry["state"])
        self.assertEqual([{"resourceVersion": "1", "code": 409}], entry["retries"])
        self.assertEqual({"uid": "pvc-old", "resourceVersion": "1"}, entry["reviewed"])
        self.assertEqual({"uid": "pvc-old", "resourceVersion": "2"}, entry["before"])
        self.patch(self.writer(self.restored()))  # observed, never resent
        self.assertEqual(2, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def test_only_a_status_retry_may_rewrite_an_open_intent(self):
        self.cluster.lose_reply(self.record.path, "PUT")  # leave the intent open, nothing sent
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        record = self.restored()
        intent = record.item()["ref"]["storage_writes"][0]
        retry = {"resourceVersion": "1", "code": 409}
        good = dict(intent, before={"uid": "pvc-old", "resourceVersion": "2"}, retries=[retry],
                    reviewed={"uid": "pvc-old", "resourceVersion": "1"})
        bad = (dict(good, before={"uid": "pvc-new", "resourceVersion": "2"}),           # a replacement
               dict(good, before={"uid": "pvc-old", "resourceVersion": "1"}),           # no new version
               dict(good, payload="0" * 64),                                             # another request
               dict(good, retries=[retry, retry]),                                       # two at once
               dict(good, retries=[dict(retry, resourceVersion="7")]),                   # not the refused one
               dict(good, retries=[dict(retry, code=422)]),                              # not a conflict
               dict(good, reviewed={"uid": "pvc-old", "resourceVersion": "9"}),          # another review
               dict(good, state="accepted"))                                             # skipped the send
        for entry in bad:
            with self.subTest(entry):
                job = record.item(); job["ref"]["storage_writes"] = [entry]
                with self.assertRaises(Held):
                    self.fresh().load(**self.handle).checkpoint(job)
        job = record.item(); job["ref"]["storage_writes"] = [good]
        record.checkpoint(job)
        self.assertEqual(good, self.restored().item()["ref"]["storage_writes"][0])
        import homestead_storage_journal as J
        entry = good
        for version in range(2, J.RETRIES + 2):  # each later retry, one at a time, up to the bound
            entry = dict(entry, before={"uid": "pvc-old", "resourceVersion": str(version + 1)},
                         retries=entry["retries"] + [{"resourceVersion": str(version), "code": 409}])
            job = record.item(); job["ref"]["storage_writes"] = [entry]
            if version <= J.RETRIES:
                record.checkpoint(job)
            else:
                with self.assertRaises(Held):
                    self.fresh().load(**self.handle).checkpoint(job)

    def test_old_local_copy_cannot_erase_current_history(self):
        old = self.record.item()
        self.patch(self.writer(self.record))
        before = self.cluster.read(self.record.path)
        with self.assertRaises(Held): self.record.checkpoint(old)
        self.assertEqual(before, self.cluster.read(self.record.path))

    def test_missing_replaced_or_foreign_anchor_is_never_adopted(self):
        original = self.cluster.read(self.record.path)
        variants = []
        for key, value in (("uid", "replacement"), ("deletionTimestamp", "now"),
                           ("ownerReferences", [{"uid": "dep-1"}]), ("finalizers", ["something"])):
            obj = copy.deepcopy(original); obj["metadata"][key] = value; variants.append(obj)
        obj = copy.deepcopy(original); obj["metadata"]["labels"][anchor.LABEL] = "another"; variants.append(obj)
        for obj in variants:
            with self.subTest(obj=obj):
                self.cluster.kube.add(obj)
                with self.assertRaises(Held): self.restored()
        self.cluster.discard(self.record.path)
        with self.assertRaises(Held): self.restored()
        self.assertEqual(1, len(self.cluster.sent))

    def test_create_collision_and_lost_create_never_adopt(self):
        duplicate = self.fresh()
        with self.assertRaises(Held): self.create(duplicate)
        with self.assertRaises(Held): duplicate.handle()
        self.cluster.discard(self.record.path)
        self.cluster.lose_reply(self.record.path.rsplit("/", 1)[0], "POST")
        lost = self.fresh()
        with self.assertRaises(Held) as caught: self.create(lost)
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(Held): lost.handle()
        with self.assertRaises(Held): self.create(lost)

    def test_phase_advances_are_conditional_ordered_and_blocked_by_uncertainty(self):
        stale = self.restored()
        with self.assertRaises(Held): self.record.advance("switch")
        self.record.advance("quiesce")
        with self.assertRaises(Held): stale.advance("quiesce")
        self.cluster.lose_reply(self.cluster.pvc, "PATCH")
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        with self.assertRaises(Held): self.restored().advance("copy")

    def test_unexpected_configuration_or_secret_fields_cannot_be_saved(self):
        for mutate in (lambda j: j.update(password="not-real"),
                       lambda j: j["ref"].update(config={"password": "not-real"})):
            job = self.record.item(); mutate(job)
            with self.assertRaises(Held): self.record.checkpoint(job)
        self.assertNotIn("not-real", json.dumps(self.cluster.stored()))

    def test_invalid_or_unknown_protocol_record_is_read_only(self):
        original = self.cluster.read(self.record.path)
        for key, value in (("protocol", 2), ("protocol", True), ("phase", "mystery"),
                           ("replicas", 0), ("password", "not-real")):
            obj = copy.deepcopy(original)
            state = json.loads(obj["data"]["state.json"]); state[key] = value
            obj["data"]["state.json"] = json.dumps(state)
            self.cluster.kube.add(obj)
            with self.assertRaises(Held): self.restored()
        self.assertEqual(1, len(self.cluster.sent))

    def test_receipt_forgery_cannot_authorize_a_following_mutation(self):
        original_send = self.record.send
        def wrong_response(method, path, body):
            response = original_send(method, path, body)
            response["metadata"]["uid"] = "wrong-control-object"
            return response
        self.record.send = wrong_response
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        self.assertEqual(0, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))
        with self.assertRaises(Held): self.record.advance("quiesce")

    def test_accepted_receipt_is_append_only_even_with_same_number_of_steps(self):
        self.patch(self.writer(self.record))
        job = self.record.item()
        job["ref"]["storage_writes"][0]["after"]["uid"] = "replacement"
        before = len(self.cluster.sent)
        with self.assertRaises(Held): self.record.checkpoint(job)
        self.assertEqual(before, len(self.cluster.sent))

    def test_full_record_or_wrong_journal_namespace_never_sends(self):
        for change in (lambda j: j["ref"].update(namespace="other"),
                       lambda j: j.update(id="f" * 24),
                       lambda j: j["ref"].update(storage_writes=[{}] * 129)):
            job = self.record.item(); change(job)
            with self.assertRaises(Held): self.record.checkpoint(job)
        self.assertEqual(1, len(self.cluster.sent))

    def test_ordered_phase_completion_survives_restarts_without_rollback(self):
        for phase in anchor.PHASES[1:]:
            restored = self.restored()
            restored.advance(phase)
            self.assertEqual(phase, self.restored().state["phase"])
        with self.assertRaises(Held): self.restored().advance("prepare")

    def test_creation_contains_no_raw_deployment_or_application_configuration(self):
        record = self.cluster.read(self.record.path)
        state = json.loads(record["data"]["state.json"])
        self.assertEqual({"name", "uid", "resourceVersion"}, set(state["deployment"]))
        self.assertEqual({"name", "uid", "resourceVersion"}, set(state["source"]))
        self.assertEqual({"state.json"}, set(record["data"]))

    def copy_phase(self):
        def fact(name):
            return {"name": name, "uid": name + "-uid", "shape": "d" * 64}
        plan = {"deployment_shape": "a" * 64, "source_pvc_shape": "b" * 64,
                "source_pv": fact("old-pv"), "destination_pvc": fact("data-new"),
                "destination_pv": fact("new-pv"), "worker": fact("coordinator"),
                "nodes": [{"name": "node1", "uid": "node-uid", "boot_id": "boot-1"}],
                "data_volume": "data", "target_shareable": False}
        self.record.configure(plan)
        self.record.advance("quiesce")
        self.record.advance("copy")
        return plan

    def test_copy_intent_survives_restart_and_prevents_second_copy(self):
        self.copy_phase()
        self.record.copy_started()
        restored = self.restored()
        self.assertEqual("intent", restored.state["copy_receipt"]["state"])
        with self.assertRaises(Held): restored.copy_started()
        with self.assertRaises(Held): restored.advance("verify")

    def test_verified_copy_receipt_allows_next_phase_but_not_recopy(self):
        self.copy_phase()
        self.record.copy_started()
        self.record.copy_finished({"manifest": "c" * 64, "files": 10, "bytes": 1024})
        self.restored().advance("verify")
        self.assertEqual("verify", self.restored().state["phase"])
        with self.assertRaises(Held): self.restored().copy_started()
        with self.assertRaises(Held): self.restored().copy_finished()

    def test_uncertain_copy_cannot_be_acknowledged_as_verified(self):
        self.copy_phase()
        self.record.copy_started()
        self.record.copy_finished()
        with self.assertRaises(Held): self.restored().copy_finished({"manifest": "c" * 64, "files": 10, "bytes": 1024})
        with self.assertRaises(Held): self.restored().advance("verify")

    def test_copy_receipt_cannot_override_worker_or_report_negative_totals(self):
        self.copy_phase()
        self.record.copy_started()
        for bad in ({"state": "verified"}, {"manifest": "c" * 64, "files": -1, "bytes": 1024},
                    {"manifest": "c" * 64, "files": 10, "bytes": 1024, "worker_uid": "different"}):
            with self.assertRaises(Held): self.record.copy_finished(bad)
        self.assertEqual("intent", self.restored().state["copy_receipt"]["state"])

    def test_plan_is_immutable_and_cannot_alias_source_and_destination(self):
        plan = self.copy_phase()
        with self.assertRaises(Held): self.record.configure(plan)
        state = copy.deepcopy(self.record.state)
        state["plan"]["destination_pv"] = state["plan"]["source_pv"]
        with self.assertRaises(Held): anchor._validate(state, "lab")


if __name__ == "__main__":
    unittest.main()
