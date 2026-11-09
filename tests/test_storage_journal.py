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

    def created_helper(self):
        """A helper this journal made itself, then given a new status by the cluster."""
        store = self.store; body = copy.deepcopy(store.obj); store.obj = None
        for key in ("uid", "resourceVersion"): body["metadata"].pop(key)
        writer = store.writer()
        writer.write("create", "POST", store.path.rsplit("/", 1)[0], body)
        reviewed = journal.identity(store.obj)
        store.obj["metadata"]["resourceVersion"] = "7"  # kubelet/controller status update
        store.obj["status"] = {"phase": "Running"}
        store.response = {"kind": "Status", "status": "Success", "details": {"uid": "created"}}
        return writer, reviewed

    def test_own_helper_delete_survives_a_status_update_with_a_uid_only_precondition(self):
        store = self.store; writer, reviewed = self.created_helper()
        writer.write("release", "DELETE", store.path, expected=reviewed, own=True)
        method, path, body, _ = store.sent[-1]
        self.assertEqual(("DELETE", store.path), (method, path))
        self.assertEqual({"uid": "created"}, body["preconditions"])
        self.assertEqual("accepted", store.durable["ref"]["storage_writes"][-1]["state"])

    def test_own_helper_delete_still_refuses_a_replaced_object(self):
        store = self.store; writer, reviewed = self.created_helper()
        store.obj["metadata"]["uid"] = "replacement"
        with self.assertRaises(journal.Held):
            writer.write("release", "DELETE", store.path, expected=reviewed, own=True)
        with self.assertRaises(journal.Held):
            writer.write("release2", "DELETE", store.path, expected=journal.identity(store.obj), own=True)
        self.assertEqual(["POST"], [m for m, *_ in store.sent])

    def test_own_is_refused_for_an_object_this_journal_did_not_create(self):
        store = self.store; before = journal.identity(store.obj)
        with self.assertRaises(journal.Held):
            store.writer().write("delete", "DELETE", store.path, expected=before, own=True)
        with self.assertRaises(journal.Held):
            store.writer().write("retain", "PATCH", store.path, {"spec": {}}, expected=before,
                                 ctype="application/merge-patch+json", own=True)
        self.assertEqual([], store.sent)

    def test_a_user_resource_whose_version_moved_is_still_refused(self):
        store = self.store; before = journal.identity(store.obj)
        store.obj["metadata"]["resourceVersion"] = "2"
        with self.assertRaisesRegex(journal.Held, "changed after the review"):
            store.writer().write("delete", "DELETE", store.path, expected=before)
        self.assertEqual([], store.sent)

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
                store.durable = copy.deepcopy(next(row for row in operations.stored() if row["id"] == operation["id"]))
            with operations.transaction():
                item = next(row for row in operations.stored() if row["id"] == operation["id"])
                store.patch(journal.Journal(item, store.read, store.send, checkpoint))
            with operations.transaction():
                restored = next(row for row in operations.stored() if row["id"] == operation["id"])
                self.assertEqual("accepted", restored["ref"]["storage_writes"][0]["state"])
                store.patch(journal.Journal(restored, store.read, store.send, checkpoint))
            self.assertEqual(1, len(store.sent))


class OwnBookkeepingShapeTests(unittest.TestCase):
    """What Homestead writes about its own Deployment, not to it, is not a change."""

    def test_the_change_history_mark_is_ignored_on_deployments_and_their_replicasets(self):
        # The Deployment controller copies the Deployment's annotations onto
        # its ReplicaSet, so both carry the mark the image scan's write brings.
        for kind in ("Deployment", "ReplicaSet"):
            with self.subTest(kind):
                bare = {"apiVersion": "apps/v1", "kind": kind, "metadata": {"name": "homestead", "annotations": {}},
                        "spec": {"replicas": 1}}
                marked = copy.deepcopy(bare)
                marked["metadata"]["annotations"] = {"homestead.io/ran-digests": "{}",
                                                     "homestead.io/changedBy": '{"at":1,"by":"x"}'}
                self.assertEqual(journal.shape(bare), journal.shape(marked))
                marked["metadata"]["annotations"]["user.example/hold"] = "true"
                self.assertNotEqual(journal.shape(bare), journal.shape(marked), "other annotations still count")

    def test_the_mark_still_counts_on_other_kinds(self):
        pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "source"}, "spec": {}}
        marked = copy.deepcopy(pvc)
        marked["metadata"]["annotations"] = {"homestead.io/changedBy": '{"at":1,"by":"x"}'}
        self.assertNotEqual(journal.shape(pvc), journal.shape(marked))


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


class ForeignStatusRaceTests(unittest.TestCase):
    """#826: a controller's status write is not an edit to what Homestead reviewed.

    pv-controller and the Deployment controller write status (and so a new
    resourceVersion) on objects Homestead did not create, on their own
    schedule. Between Homestead's read and its request that must not stop a
    move for good; a change to anything else still must."""

    PV = "/api/v1/persistentvolumes/pv-data"
    DEP = "/apis/apps/v1/namespaces/lab/deployments/app"

    def setUp(self):
        from fakes.kube import Kube
        import homestead_kube as K
        pv = {"apiVersion": "v1", "kind": "PersistentVolume",
              "metadata": {"name": "pv-data", "annotations": {"pv.kubernetes.io/provisioned-by": "csi.example.com"},
                           "finalizers": ["kubernetes.io/pv-protection"]},
              "spec": {"persistentVolumeReclaimPolicy": "Delete", "csi": {"driver": "csi.example.com", "volumeHandle": "vol-1"}},
              "status": {"phase": "Bound"}}
        dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "app", "namespace": "lab"},
               "spec": {"replicas": 0, "template": {"spec": {"containers": [{"name": "app", "image": "app:1"}]}}},
               "status": {"observedGeneration": 1}}
        self.kube = Kube(pv, dep)
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.K = K
        self.item = {"id": "move-1", "status": "running", "ref": {"namespace": "lab"}}
        self.saved = []
        self.requests = []
        self.before_send = None

    def send(self, method, path, body=None, ctype=None):
        # Straight to the fake API server: this tests the journal, not the
        # write guards in front of it (those take the operations store's lock).
        self.requests.append((method, path, copy.deepcopy(body)))
        if self.before_send:
            self.before_send(len(self.requests))
        return self.kube(method, path, body, ctype=ctype)

    def writer(self):
        return journal.Journal(self.item, self.K.kget, self.send, lambda item: self.saved.append(copy.deepcopy(item)))

    def status(self, path, **status):
        """What a controller does: a status-only write, which moves resourceVersion."""
        self.kube("PUT", path + "/status", {"status": status})

    def reviewed(self, path):
        obj = self.K.kget(path)
        return obj, {**journal.identity(obj), "settled": journal.settled(obj)}

    def retain(self, expected, writer=None):
        return (writer or self.writer()).write("retain-original", "PATCH", self.PV,
                                               {"spec": {"persistentVolumeReclaimPolicy": "Retain"}},
                                               expected=expected, ctype="application/merge-patch+json")

    def sent(self, method, path):
        return [body for m, p, body in self.requests if (m, p) == (method, path)]

    def test_a_status_update_after_the_review_continues_with_the_new_version(self):
        _, expected = self.reviewed(self.PV)
        self.status(self.PV, phase="Released", lastPhaseTransitionTime="2026-10-26T00:00:00Z")
        current = self.kube.object(self.PV)["metadata"]["resourceVersion"]
        self.retain(expected)
        self.assertEqual("Retain", self.kube.object(self.PV)["spec"]["persistentVolumeReclaimPolicy"])
        [body] = self.sent("PATCH", self.PV)
        self.assertEqual(current, body["metadata"]["resourceVersion"], "the request still pins the version it saw")
        entry = self.item["ref"]["storage_writes"][0]
        self.assertEqual("accepted", entry["state"])
        self.assertEqual(expected["resourceVersion"], entry["reviewed"]["resourceVersion"])
        self.assertEqual(current, entry["before"]["resourceVersion"])

    def test_a_status_update_just_before_the_request_is_retried_once_it_is_refused(self):
        _, expected = self.reviewed(self.PV)
        self.before_send = lambda n: n == 1 and self.status(self.PV, phase="Released")
        self.retain(expected)
        self.assertEqual("Retain", self.kube.object(self.PV)["spec"]["persistentVolumeReclaimPolicy"])
        first, second = self.sent("PATCH", self.PV)
        self.assertNotEqual(first["metadata"]["resourceVersion"], second["metadata"]["resourceVersion"])
        entry = self.item["ref"]["storage_writes"][0]
        self.assertEqual("accepted", entry["state"])
        self.assertEqual([{"resourceVersion": first["metadata"]["resourceVersion"], "code": 409}], entry["retries"])
        self.assertEqual(second["metadata"]["resourceVersion"], entry["before"]["resourceVersion"])
        # The retry was durable before it was sent.
        self.assertTrue(any(e["state"] == "intent" and e.get("retries") for s in self.saved for e in s["ref"]["storage_writes"]))

    def test_a_deployment_put_survives_the_controller_writing_status(self):
        dep, expected = self.reviewed(self.DEP)
        proposed = copy.deepcopy(dep); proposed["spec"]["replicas"] = 1
        self.status(self.DEP, observedGeneration=1, replicas=0)
        self.before_send = lambda n: n == 1 and self.status(self.DEP, observedGeneration=1, replicas=0, readyReplicas=0)
        self.writer().write("restart", "PUT", self.DEP, proposed, expected=expected)
        self.assertEqual(1, self.kube.object(self.DEP)["spec"]["replicas"])
        self.assertEqual(2, len(self.sent("PUT", self.DEP)))

    def test_a_spec_change_after_the_review_still_holds_and_sends_nothing(self):
        _, expected = self.reviewed(self.PV)
        self.kube("PATCH", self.PV, {"spec": {"persistentVolumeReclaimPolicy": "Recycle"}}, ctype="application/merge-patch+json")
        with self.assertRaisesRegex(journal.Held, "changed after the review"):
            self.retain(expected)
        self.assertEqual([], self.sent("PATCH", self.PV))
        self.assertEqual("Recycle", self.kube.object(self.PV)["spec"]["persistentVolumeReclaimPolicy"])

    def test_metadata_edits_after_the_review_still_hold(self):
        edits = ({"metadata": {"annotations": {"example.com/hold": "true"}}},
                 {"metadata": {"labels": {"tier": "gold"}}},
                 {"metadata": {"finalizers": ["kubernetes.io/pv-protection", "example.com/keep"]}})
        for edit in edits:
            with self.subTest(edit):
                self.setUp()
                _, expected = self.reviewed(self.PV)
                self.kube("PATCH", self.PV, edit, ctype="application/merge-patch+json")
                with self.assertRaisesRegex(journal.Held, "changed after the review"):
                    self.retain(expected)
                self.assertEqual([], self.sent("PATCH", self.PV))

    def test_a_spec_change_just_before_the_request_is_refused_and_not_retried(self):
        dep, expected = self.reviewed(self.DEP)
        proposed = copy.deepcopy(dep); proposed["spec"]["replicas"] = 1
        def edit(n):
            if n == 1:
                self.kube("PATCH", self.DEP, {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "app:2"}]}}}},
                          ctype="application/merge-patch+json")
        self.before_send = edit
        writer = self.writer()
        with self.assertRaisesRegex(journal.Held, "Nothing was retried"):
            writer.write("restart", "PUT", self.DEP, proposed, expected=expected)
        self.assertEqual(1, len(self.sent("PUT", self.DEP)))
        self.assertEqual("refused", self.item["ref"]["storage_writes"][0]["state"])
        self.assertEqual("app:2", self.kube.object(self.DEP)["spec"]["template"]["spec"]["containers"][0]["image"])
        with self.assertRaises(journal.Held):
            writer.write("other", "PUT", self.DEP, proposed, expected=expected)

    def replace(self):
        """Someone deletes the volume and creates another under the same name."""
        obj = self.kube.object(self.PV)
        self.kube("PATCH", self.PV, {"metadata": {"finalizers": None}}, ctype="application/merge-patch+json")
        self.kube("DELETE", self.PV, {"propagationPolicy": "Background"})
        self.kube("POST", "/api/v1/persistentvolumes", {"apiVersion": "v1", "kind": "PersistentVolume",
                  "metadata": {"name": "pv-data", "annotations": obj["metadata"]["annotations"]},
                  "spec": obj["spec"], "status": obj["status"]})

    def retains(self):
        return [b for b in self.sent("PATCH", self.PV) if "spec" in b]

    def test_a_replacement_still_holds(self):
        _, expected = self.reviewed(self.PV)
        self.replace()
        self.assertNotEqual(expected["uid"], self.kube.object(self.PV)["metadata"]["uid"])
        with self.assertRaisesRegex(journal.Held, "changed after the review"):
            self.retain(expected)
        self.assertEqual([], self.retains())

    def test_a_replacement_just_before_the_request_is_not_retried(self):
        _, expected = self.reviewed(self.PV)
        self.before_send = lambda n: n == 1 and self.replace()
        with self.assertRaisesRegex(journal.Held, "Nothing was retried"):
            self.retain(expected)
        self.assertEqual(1, len(self.retains()))
        self.assertEqual("refused", self.item["ref"]["storage_writes"][0]["state"])

    def test_a_controller_that_never_settles_still_surfaces(self):
        _, expected = self.reviewed(self.PV)
        self.before_send = lambda n: self.status(self.PV, phase="Released", attempt=str(n))
        with self.assertRaisesRegex(journal.Held, "Nothing was retried"):
            self.retain(expected)
        attempts = self.sent("PATCH", self.PV)
        self.assertEqual(1 + journal.RETRIES, len(attempts))
        entry = self.item["ref"]["storage_writes"][0]
        self.assertEqual("refused", entry["state"])
        self.assertEqual(journal.RETRIES, len(entry["retries"]))
        self.assertEqual("Delete", self.kube.object(self.PV)["spec"]["persistentVolumeReclaimPolicy"])

    def test_other_refusals_are_not_retried(self):
        for code in (400, 403, 422):
            with self.subTest(code):
                self.setUp()
                _, expected = self.reviewed(self.PV)
                self.kube.fail(self.PV, code, method="PATCH")
                with self.assertRaisesRegex(journal.Held, "Nothing was retried"):
                    self.retain(expected)
                self.assertEqual(1, len(self.sent("PATCH", self.PV)))
                self.assertEqual("refused", self.item["ref"]["storage_writes"][0]["state"])

    def test_a_conflict_without_a_new_version_is_not_retried(self):
        _, expected = self.reviewed(self.PV)
        self.kube.fail(self.PV, 409, method="PATCH")
        with self.assertRaisesRegex(journal.Held, "Nothing was retried"):
            self.retain(expected)
        self.assertEqual(1, len(self.sent("PATCH", self.PV)))

    def test_without_a_settled_review_a_moved_version_still_holds(self):
        obj, _ = self.reviewed(self.PV)
        self.status(self.PV, phase="Released")
        with self.assertRaisesRegex(journal.Held, "changed after the review"):
            self.retain(journal.identity(obj))
        self.assertEqual([], self.sent("PATCH", self.PV))

    def test_deletes_of_user_data_keep_the_full_check(self):
        obj, expected = self.reviewed(self.PV)
        self.status(self.PV, phase="Released")
        with self.assertRaisesRegex(journal.Held, "changed after the review"):
            self.writer().write("delete", "DELETE", self.PV, expected=expected)
        self.assertEqual([], self.sent("DELETE", self.PV))

    def test_a_rebased_step_is_observed_again_with_the_reviewed_identity(self):
        _, expected = self.reviewed(self.PV)
        self.status(self.PV, phase="Released")
        writer = self.writer()
        self.retain(expected, writer)
        self.retain(expected, writer)
        self.assertEqual(1, len(self.sent("PATCH", self.PV)))

    def test_a_step_retried_several_times_is_observed_with_either_identity(self):
        _, expected = self.reviewed(self.PV)
        self.before_send = lambda n: n <= 2 and self.status(self.PV, phase="Released", attempt=str(n))
        writer = self.writer()
        self.retain(expected, writer)
        entry = self.item["ref"]["storage_writes"][0]
        self.assertEqual(2, len(entry["retries"]))
        self.retain(expected, writer)
        self.retain({k: entry["before"][k] for k in ("uid", "resourceVersion")}, writer)
        with self.assertRaisesRegex(journal.Held, "different reviewed identity"):
            self.retain({"uid": entry["before"]["uid"], "resourceVersion": entry["retries"][1]["resourceVersion"]}, writer)
        self.assertEqual(3, len(self.sent("PATCH", self.PV)))

    def test_a_review_older_than_the_read_it_names_still_holds(self):
        # The request was built from one read and names the identity of a later
        # one: whatever changed in between counts, even with no version change since.
        old, _ = self.reviewed(self.PV)
        self.kube("PATCH", self.PV, {"spec": {"persistentVolumeReclaimPolicy": "Recycle"}}, ctype="application/merge-patch+json")
        expected = {**journal.identity(self.K.kget(self.PV)), "settled": journal.settled(old)}
        with self.assertRaisesRegex(journal.Held, "changed after the review"):
            self.retain(expected)
        self.assertEqual([], self.sent("PATCH", self.PV))

    def test_a_settled_review_without_a_version_is_refused_cleanly(self):
        obj, expected = self.reviewed(self.PV)
        self.status(self.PV, phase="Released")
        for partial in ({"uid": expected["uid"], "settled": expected["settled"]},
                        {"uid": expected["uid"], "resourceVersion": None, "settled": expected["settled"]}):
            with self.subTest(partial), self.assertRaisesRegex(journal.Held, "changed after the review"):
                self.retain(partial)
        self.assertEqual([], self.sent("PATCH", self.PV))

    def test_stale_managed_fields_are_never_written_back(self):
        dep, expected = self.reviewed(self.DEP)
        proposed = copy.deepcopy(dep); proposed["spec"]["replicas"] = 1
        proposed["metadata"]["managedFields"] = [{"manager": "kube-controller-manager", "operation": "Update",
                                                  "subresource": "status", "time": "2026-10-26T00:00:00Z"}]
        self.status(self.DEP, observedGeneration=1, replicas=0)
        self.writer().write("restart", "PUT", self.DEP, proposed, expected=expected)
        [put] = self.sent("PUT", self.DEP)
        self.assertNotIn("managedFields", put["metadata"])

    def test_a_failed_pre_write_check_before_a_retry_sends_nothing_more(self):
        _, expected = self.reviewed(self.PV)
        self.before_send = lambda n: n == 1 and self.status(self.PV, phase="Released")
        checks = []
        def before_write():
            checks.append(1)
            if len(checks) > 1:
                raise journal.Held("Homestead replica runtime identity is incomplete")
        writer = journal.Journal(self.item, self.K.kget, self.send, lambda item: self.saved.append(copy.deepcopy(item)),
                                 before_write=before_write)
        with self.assertRaisesRegex(journal.Held, "runtime identity"):
            self.retain(expected, writer)
        self.assertEqual(1, len(self.sent("PATCH", self.PV)))
        entry = self.item["ref"]["storage_writes"][0]
        self.assertEqual("refused", entry["state"])
        self.assertNotIn("retries", entry)

    def test_a_volume_being_deleted_is_not_retried(self):
        _, expected = self.reviewed(self.PV)
        self.before_send = lambda n: n == 1 and self.kube("DELETE", self.PV, {"propagationPolicy": "Background"})
        with self.assertRaisesRegex(journal.Held, "Nothing was retried"):
            self.retain(expected)
        self.assertTrue(self.kube.object(self.PV)["metadata"]["deletionTimestamp"])
        self.assertEqual(1, len(self.sent("PATCH", self.PV)))
        self.assertEqual("refused", self.item["ref"]["storage_writes"][0]["state"])

    def test_inspection_after_a_status_update_shows_the_new_version_and_writes_nothing(self):
        _, expected = self.reviewed(self.PV)
        self.status(self.PV, phase="Released")
        writer = journal.Journal(self.item, self.K.kget, None, None, preview=True)
        with self.assertRaises(journal.NextWrite) as ready:
            self.retain(expected, writer)
        self.assertEqual(self.kube.object(self.PV)["metadata"]["resourceVersion"], ready.exception.plan["before"]["resourceVersion"])
        self.assertEqual([], self.item["ref"]["storage_writes"])
        self.assertEqual([], self.sent("PATCH", self.PV))
