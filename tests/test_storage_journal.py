"""Crash/lost-response tests for the pending storage handoff implementation."""
import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_storage_journal as journal
import homestead_operations as operations


class Store:
    def __init__(self):
        self.path = "/api/v1/namespaces/lab/persistentvolumeclaims/data"
        self.obj = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"namespace": "lab", "name": "data", "uid": "original", "resourceVersion": "1"},
                    "spec": {"volumeName": "pv-old"}}
        self.item = {"id": "move-1", "status": "running", "ref": {"namespace": "lab"}}
        self.durable = copy.deepcopy(self.item)
        self.sent, self.saved = [], []
        self.send_error = self.save_at = None
        self.response = None
        self.apply_then_lose = False

    def read(self, path):
        if self.obj is None:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.obj)

    def save(self, item):
        self.saved.append(copy.deepcopy(item))
        if self.save_at == len(self.saved):
            raise OSError("disk unavailable")
        self.durable = copy.deepcopy(item)

    def send(self, method, path, body, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body), kwargs))
        assert self.durable["ref"]["storage_writes"][-1]["state"] == "intent"
        if self.send_error:
            raise self.send_error
        if method == "DELETE":
            self.obj = None
            result = {"apiVersion": "v1", "kind": "Status", "status": "Success", "details": {"uid": "original"}}
        elif method == "POST":
            self.obj = copy.deepcopy(body)
            self.obj["metadata"].update(uid="created", resourceVersion="1")
            result = copy.deepcopy(self.obj)
        else:
            self.obj["spec"].update(body.get("spec", {}))
            self.obj["metadata"]["resourceVersion"] = str(int(self.obj["metadata"]["resourceVersion"]) + 1)
            result = copy.deepcopy(self.obj)
        if self.apply_then_lose:
            raise TimeoutError("response lost with secret in raw error")
        return self.response if self.response is not None else result

    def writer(self, *, restored=False):
        if restored:
            self.item = copy.deepcopy(self.durable)
        return journal.Journal(self.item, self.read, self.send, self.save)

    def patch(self, writer, *, step="retain", expected=None, body=None):
        return writer.write(step, "PATCH", self.path, body or {"spec": {"storageClassName": "new"}},
            expected=expected or {"uid": "original", "resourceVersion": "1"}, ctype="application/merge-patch+json")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def test_intent_precedes_write_and_receipt_survives_restart_without_replay(self):
        store = self.store
        store.patch(store.writer())
        self.assertEqual("intent", store.saved[0]["ref"]["storage_writes"][0]["state"])
        self.assertEqual("accepted", store.durable["ref"]["storage_writes"][0]["state"])
        store.obj["metadata"]["resourceVersion"] = "3"  # controller status is not a spec change
        store.obj["status"] = {"phase": "Bound"}
        self.assertEqual("3", store.patch(store.writer(restored=True))["metadata"]["resourceVersion"])
        self.assertEqual(1, len(store.sent))
        self.assertEqual({"uid": "original", "resourceVersion": "1"}, store.sent[0][2]["metadata"])

    def test_intent_save_failure_sends_nothing_and_writer_stays_stopped(self):
        store = self.store; store.save_at = 1; writer = store.writer()
        with self.assertRaises(journal.Held): store.patch(writer)
        with self.assertRaises(journal.Held): store.patch(writer, step="another")
        self.assertEqual([], store.sent)

    def test_receipt_save_failure_restores_an_intent_not_a_permission_to_resend(self):
        store = self.store; store.save_at = 2
        with self.assertRaises(journal.Held): store.patch(store.writer())
        self.assertEqual("intent", store.durable["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(journal.Held): store.patch(store.writer(restored=True))
        self.assertEqual(1, len(store.sent))

    def test_accepted_but_lost_reply_is_not_adopted_from_matching_current_state(self):
        store = self.store; store.apply_then_lose = True
        with self.assertRaises(journal.Held) as error: store.patch(store.writer())
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual("new", store.obj["spec"]["storageClassName"])
        writer = store.writer(restored=True)
        for step in ("retain", "next"):
            with self.assertRaises(journal.Held): store.patch(writer, step=step)
        self.assertEqual(1, len(store.sent))

    def test_refused_request_never_adopts_same_name_or_retries(self):
        store = self.store
        store.send_error = urllib.error.HTTPError(store.path, 409, "conflict", {}, None)
        with self.assertRaises(journal.Held): store.patch(store.writer())
        self.assertEqual("refused", store.durable["ref"]["storage_writes"][0]["state"])
        with self.assertRaises(journal.Held): store.patch(store.writer(restored=True))
        self.assertEqual(1, len(store.sent))

    def test_uid_version_and_shape_changes_fail_before_sending(self):
        store = self.store
        for expected in ({"uid": "other", "resourceVersion": "1"},
                         {"uid": "original", "resourceVersion": "old"},
                         {"uid": "original", "resourceVersion": "1", "shape": "wrong"}):
            with self.assertRaises(journal.Held): store.patch(store.writer(), expected=expected)
        self.assertEqual([], store.sent)

    def test_receipt_observation_rejects_replacement_and_manual_edit(self):
        store = self.store; store.patch(store.writer())
        store.obj["metadata"]["uid"] = "replacement"
        with self.assertRaises(journal.Held): store.patch(store.writer(restored=True))
        store.obj["metadata"]["uid"] = "original"
        store.obj["spec"]["volumeName"] = "different-pv"
        with self.assertRaises(journal.Held): store.patch(store.writer(restored=True))
        self.assertEqual(1, len(store.sent))

    def test_same_step_cannot_silently_change_payload(self):
        store = self.store; store.patch(store.writer())
        with self.assertRaises(journal.Held): store.patch(store.writer(restored=True), body={"spec": {"volumeName": "other"}})
        self.assertEqual(1, len(store.sent))

    def test_invalid_receipt_is_held_without_recording_its_contents(self):
        store = self.store; store.response = {"secret": "DO-NOT-STORE"}
        with self.assertRaises(journal.Held): store.patch(store.writer())
        entry = store.durable["ref"]["storage_writes"][0]
        self.assertEqual("unverified", entry["state"])
        self.assertNotIn("DO-NOT-STORE", json.dumps(store.durable))

    def test_secret_manifest_fields_are_hashed_never_stored(self):
        store = self.store
        store.patch(store.writer(), body={"spec": {"env": {"PASSWORD": "not-a-real-secret"}}})
        self.assertNotIn("PASSWORD", json.dumps(store.durable))
        self.assertNotIn("not-a-real-secret", json.dumps(store.durable))

    def test_delete_has_exact_preconditions_and_waits_without_repeating(self):
        store = self.store; before = journal.identity(store.obj)
        writer = store.writer()
        writer.write("delete", "DELETE", store.path, expected=before)
        self.assertEqual(before, store.sent[0][2]["preconditions"])
        self.assertIsNone(store.writer(restored=True).write("delete", "DELETE", store.path, expected=before))
        self.assertEqual(1, len(store.sent))

    def test_delete_receipt_does_not_authorize_deleting_a_replacement(self):
        store = self.store; original = copy.deepcopy(store.obj); before = journal.identity(original)
        store.writer().write("delete", "DELETE", store.path, expected=before)
        store.obj = original; store.obj["metadata"]["uid"] = "replacement"
        with self.assertRaises(journal.Held): store.writer(restored=True).write("delete", "DELETE", store.path, expected=before)
        self.assertEqual(1, len(store.sent))

    def test_post_requires_absence_and_verifies_created_identity(self):
        store = self.store; body = copy.deepcopy(store.obj)
        for key in ("uid", "resourceVersion"): body["metadata"].pop(key)
        with self.assertRaises(journal.Held): store.writer().write("create", "POST", store.path.rsplit("/", 1)[0], body)
        self.assertEqual([], store.sent)
        store.obj = None
        result = store.writer().write("create", "POST", store.path.rsplit("/", 1)[0], body)
        self.assertEqual("created", result["metadata"]["uid"])

    def test_cross_namespace_subresources_queries_and_unknown_kinds_are_rejected(self):
        store = self.store
        paths = (store.path.replace("/lab/", "/other/"), store.path + "/status", store.path + "?dryRun=All",
                 store.path.replace("persistentvolumeclaims", "secrets"), "/api/v1/persistentvolumeclaims/data")
        for path in paths:
            with self.assertRaises(journal.Held):
                store.writer().write("bad", "PATCH", path, {"spec": {}}, expected=journal.identity(store.obj), ctype="application/merge-patch+json")
        self.assertEqual([], store.sent)

    def test_cluster_scoped_pv_can_be_fenced_without_namespace(self):
        store = self.store; store.path = "/api/v1/persistentvolumes/pv-old"
        store.obj.update(kind="PersistentVolume")
        store.obj["metadata"].pop("namespace"); store.obj["metadata"]["name"] = "pv-old"
        store.patch(store.writer())
        self.assertIsNone(store.durable["ref"]["storage_writes"][0]["target"]["namespace"])

    def test_shared_operation_store_persists_receipt_for_a_fresh_writer_instance(self):
        store = self.store
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(operations, "DATA_DIR", directory):
            operation = operations.start("reclass", "Move data", {"namespace": "lab", "name": "data"}, "/volumes", {"namespace": "lab"})
            def checkpoint(item):
                operations.checkpoint(item)
                store.durable = copy.deepcopy(next(row for row in operations._read() if row["id"] == operation["id"]))
            with operations._lock:
                item = next(row for row in operations._read() if row["id"] == operation["id"])
                store.patch(journal.Journal(item, store.read, store.send, checkpoint))
            with operations._lock:
                restored = next(row for row in operations._read() if row["id"] == operation["id"])
                self.assertEqual("accepted", restored["ref"]["storage_writes"][0]["state"])
                store.patch(journal.Journal(restored, store.read, store.send, checkpoint))
            self.assertEqual(1, len(store.sent))


class ListedPodShapeTests(unittest.TestCase):
    """A pod read alone and the same pod from a list are the same pod."""

    def test_a_listed_pod_without_its_kind_still_ignores_what_the_network_plugin_wrote(self):
        meta = {"name": "homestead-1", "uid": "u", "resourceVersion": "9", "labels": {"app": "homestead"},
                "annotations": {"k8s.v1.cni.cncf.io/network-status": '[{"name":"cbr0"}]', "kept": "yes"}}
        spec = {"containers": [{"name": "homestead", "image": "x"}]}
        alone = {"kind": "Pod", "apiVersion": "v1", "metadata": meta, "spec": spec}
        listed = {"metadata": dict(meta, annotations={"kept": "yes"}), "spec": spec}
        self.assertEqual(journal.shape(alone), journal.shape(listed))
        listed_with_cni = {"metadata": meta, "spec": spec}
        self.assertEqual(journal.shape(alone), journal.shape(listed_with_cni))
        self.assertNotEqual(journal.shape(alone), journal.shape({"metadata": dict(meta, annotations={"kept": "no"}), "spec": spec}),
                            "other annotations still count")
