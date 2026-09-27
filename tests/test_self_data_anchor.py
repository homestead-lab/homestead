"""Independent control history survives either PVC and rejects competing workers."""
import copy
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as anchor
from homestead_storage_journal import Held, Journal


OP = "1234567890abcdef12345678"


class Cluster:
    def __init__(self):
        self.objects = {}
        self.sent = []
        self.lose_at = None
        self.reject_at = None
        self.uid = 0
        self.pvc = "/api/v1/namespaces/lab/persistentvolumeclaims/data"
        self.objects[self.pvc] = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"namespace": "lab", "name": "data", "uid": "pvc-old", "resourceVersion": "1"},
            "spec": {"volumeName": "old-pv"}}

    def read(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "absent", {}, None)
        return copy.deepcopy(self.objects[path])

    def send(self, method, path, body, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if len(self.sent) == self.reject_at:
            raise OSError("secret API diagnostic")
        if method == "POST":
            path += "/" + body["metadata"]["name"]
            if path in self.objects:
                raise urllib.error.HTTPError(path, 409, "already exists", {}, None)
            obj = copy.deepcopy(body)
            self.uid += 1
            obj["metadata"].update(uid=f"created-{self.uid}", resourceVersion="1")
        else:
            old = self.objects[path]
            for key in ("uid", "resourceVersion"):
                if old["metadata"][key] != body["metadata"][key]:
                    raise urllib.error.HTTPError(path, 409, "changed", {}, None)
            obj = copy.deepcopy(body) if method == "PUT" else copy.deepcopy(old)
            if method == "PATCH":
                obj["spec"].update(body.get("spec", {}))
            obj["metadata"]["resourceVersion"] = str(int(old["metadata"]["resourceVersion"]) + 1)
        self.objects[path] = obj
        if len(self.sent) == self.lose_at:
            raise TimeoutError("secret response diagnostic")
        return copy.deepcopy(obj)


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
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
        self.cluster.objects[self.cluster.pvc]["metadata"]["resourceVersion"] = "1"
        with self.assertRaises(Held):
            self.patch(self.writer(stale))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))
        self.assertTrue(stale.failed)

    def test_lost_intent_checkpoint_sends_no_resource_write_or_retry(self):
        self.cluster.lose_at = 2
        writer = self.writer(self.record)
        with self.assertRaises(Held): self.patch(writer)
        self.assertEqual("intent", self.restored().item()["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(Held): self.patch(self.writer(self.restored()))
        self.assertEqual(0, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def test_lost_resource_response_remains_uncertain_after_restart(self):
        self.cluster.lose_at = 3
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        self.assertEqual("uncertain", self.restored().item()["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(Held): self.patch(self.writer(self.restored()))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def test_lost_accepted_checkpoint_can_be_observed_not_resent(self):
        self.cluster.lose_at = 4
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        self.patch(self.writer(self.restored()))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

    def test_failed_accepted_checkpoint_leaves_intent_not_retry_permission(self):
        self.cluster.reject_at = 4
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        self.assertEqual("intent", self.restored().item()["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(Held): self.patch(self.writer(self.restored()))
        self.assertEqual(1, sum(p == self.cluster.pvc for _, p, _ in self.cluster.sent))

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
                self.cluster.objects[self.record.path] = obj
                with self.assertRaises(Held): self.restored()
        del self.cluster.objects[self.record.path]
        with self.assertRaises(Held): self.restored()
        self.assertEqual(1, len(self.cluster.sent))

    def test_create_collision_and_lost_create_never_adopt(self):
        duplicate = self.fresh()
        with self.assertRaises(Held): self.create(duplicate)
        with self.assertRaises(Held): duplicate.handle()
        del self.cluster.objects[self.record.path]
        self.cluster.lose_at = len(self.cluster.sent) + 1
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
        self.cluster.lose_at = len(self.cluster.sent) + 2
        with self.assertRaises(Held): self.patch(self.writer(self.record))
        with self.assertRaises(Held): self.restored().advance("copy")

    def test_unexpected_configuration_or_secret_fields_cannot_be_saved(self):
        for mutate in (lambda j: j.update(password="not-real"),
                       lambda j: j["ref"].update(config={"password": "not-real"})):
            job = self.record.item(); mutate(job)
            with self.assertRaises(Held): self.record.checkpoint(job)
        self.assertNotIn("not-real", json.dumps(self.cluster.objects))

    def test_invalid_or_unknown_protocol_record_is_read_only(self):
        original = self.cluster.read(self.record.path)
        for key, value in (("protocol", 2), ("protocol", True), ("phase", "mystery"),
                           ("replicas", 0), ("password", "not-real")):
            obj = copy.deepcopy(original)
            state = json.loads(obj["data"]["state.json"]); state[key] = value
            obj["data"]["state.json"] = json.dumps(state)
            self.cluster.objects[self.record.path] = obj
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
