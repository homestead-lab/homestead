"""Rename fault injection: no duplicate writers, blind undo, or replay."""
import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_capacity_review as review
import homestead_workload_edit as WORKLOAD_EDIT
import homestead_operations as ops
from fakes.job_store import put
import homestead_rename as rename
import homestead_vm_power_job as locks


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": "old", "uid": "uid-old", "resourceVersion": "1", "generation": 1,
                         "managedFields": [self.status_fields("2026-11-01T10:00:00Z", {})]},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "old", "homestead.io/workload": "old"}},
                     "template": {"metadata": {"labels": {"app": "old", "homestead.io/workload": "old"}},
                                  "spec": {"containers": [{"name": "main", "image": "example/test:1",
                                                          "env": [{"name": "SECRET", "value": "do-not-journal"}]}],
                                           "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}},
            "status": self.state(1, 1)}
        self.objects = {"old": self.source}
        self.hpas, self.pods, self.sets, self.sent = [], [], [], []
        self.extra, self.after_send, self.unreadable = {}, lambda *args: None, set()
        self.pod_reads, self.pods_gone_after, self.slept = 0, None, []
        # The Deployment controller rewrites the original's status once its last
        # old pod has gone (terminatingReplicas), a moment after: it lands just
        # after the next read of the original, so that read's resourceVersion
        # is stale. status_lags=False: the write landed before any read.
        self.status_lags, self.status_write, self.on_status_write = True, None, lambda: None
        self.admission = mock.Mock(return_value={"blocked": False})
        self.body = {"ns": "lab", "name": "old", "workload_name": "new"}
        for patch in (mock.patch.object(review, "_key", return_value=b"rename-tests"),
                      mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {rename.KIND: lambda item: rename.status(item, ops)}),
                      mock.patch.dict(ops.CANCELLERS, {rename.KIND: (
                          lambda item: rename.recovery_plan(item, self.read, ops),
                          lambda item, options: rename.recovery_run(item, options, self.read, ops))})):
            patch.start()
            self.addCleanup(patch.stop)
        ops.CLEANUPS.add(rename.KIND)

    @staticmethod
    def state(count, generation):
        return {**{key: count for key in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")},
                "observedGeneration": generation}

    def read(self, path):
        if path in self.unreadable:
            raise OSError("unreadable")
        if path in self.extra:
            return copy.deepcopy(self.extra[path])
        if path.endswith("/horizontalpodautoscalers"):
            return {"items": copy.deepcopy(self.hpas)}
        if path == "/api/v1/pods":
            self.pod_reads += 1
            if self.pods_gone_after is not None and self.pod_reads > self.pods_gone_after:
                if self.status_write is None:
                    self.status_write = "pending"
                    if not self.status_lags:
                        self.controller_status_write()
                return {"items": []}
            return {"items": copy.deepcopy(self.pods)}
        if path.endswith("/replicasets"):
            return {"items": copy.deepcopy(self.sets)}
        if "/deployments/" in path and path.rsplit("/", 1)[-1] in self.objects:
            obj = copy.deepcopy(self.objects[path.rsplit("/", 1)[-1]])
            if path.endswith("/old") and self.status_write == "pending":
                self.controller_status_write()
            return obj
        raise urllib.error.HTTPError(path, 404, "not found", {}, None)

    @staticmethod
    def status_fields(time, more):
        """The controller's managedFields entry for status, as k3s writes it."""
        return {"manager": "k3s", "operation": "Update", "apiVersion": "apps/v1", "time": time,
                "fieldsType": "FieldsV1", "subresource": "status",
                "fieldsV1": {"f:status": {"f:observedGeneration": {}, "f:replicas": {}, **more}}}

    def controller_status_write(self):
        """What the Deployment controller does as the last old pod goes: a status
        write, which bumps resourceVersion, rewrites its managedFields entry
        and leaves the spec alone."""
        self.status_write = "written"
        obj = self.objects["old"]
        obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        obj["metadata"]["managedFields"] = [row for row in obj["metadata"].get("managedFields", [])
                                            if row.get("subresource") != "status"] + [
            self.status_fields("2026-11-01T10:00:07Z", {"f:terminatingReplicas": {}})]
        obj["status"] = dict(obj.get("status") or {}, terminatingReplicas=0)
        self.on_status_write()

    def someone_labels_the_original(self):
        """Someone else's edit: a label, with the resourceVersion bump any write makes."""
        meta = self.objects["old"]["metadata"]
        meta.setdefault("labels", {})["team"] = "b"
        meta["resourceVersion"] = str(int(meta["resourceVersion"]) + 1)

    def send(self, method, path, payload, **kwargs):
        self.sent.append((method, path, copy.deepcopy(payload)))
        if method == "POST":
            obj = copy.deepcopy(payload)
            name = obj["metadata"]["name"]
            self.assertNotIn(name, self.objects)
            obj["metadata"].update(uid="uid-" + name, resourceVersion="1", generation=1)
            obj["status"] = self.state(obj["spec"]["replicas"], 1)
            self.objects[name] = obj
        else:
            name = path.rsplit("/", 1)[-1]
            obj = self.objects[name]
            if method == "PATCH":
                self.assertEqual("application/json-patch+json", kwargs["ctype"])
                self.assertEqual(["test", "/metadata/uid"], [payload[0]["op"], payload[0]["path"]])
                self.assertEqual(["test", "/metadata/resourceVersion"], [payload[1]["op"], payload[1]["path"]])
                if (payload[0]["value"], payload[1]["value"]) != (obj["metadata"]["uid"], obj["metadata"]["resourceVersion"]):
                    # The API server's answer to a JSON-patch test op that does not hold.
                    raise urllib.error.HTTPError(path, 422, "Unprocessable Entity", {}, None)
                obj["spec"]["replicas"] = payload[2]["value"]
                obj["metadata"]["generation"] += 1
                obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
                obj["status"] = self.state(obj["spec"]["replicas"], obj["metadata"]["generation"])
            elif method == "DELETE":
                self.assertEqual("Orphan", payload["propagationPolicy"])
                self.assertEqual({key: obj["metadata"][key] for key in ("uid", "resourceVersion")}, payload["preconditions"])
                self.objects.pop(name)
        self.after_send(method, name, obj)
        return copy.deepcopy(obj)

    def prepare(self):
        self.proposed, self.context = rename.prepare(self.body, self.source, WORKLOAD_EDIT._renamed_deployment, self.read)
        self.body.update(capacity_token=review.issue(self.body, self.context), confirm_capacity=True)

    def dispatch(self, **kwargs):
        if not hasattr(self, "proposed"):
            self.prepare()
        kwargs.setdefault("settle", 0)
        return rename.dispatch(self.body, self.context, self.proposed, self.read,
                               kwargs.pop("send", self.send), ops, self.admission, timeout=0, sleep=self.slept.append, **kwargs)

    def test_success_rechecks_after_stop_and_keeps_service_labels_storage_and_secrets(self):
        def check(dep):
            self.assertEqual(0, self.objects["old"]["spec"]["replicas"])
            self.assertEqual(0, self.objects["new"]["spec"]["replicas"])
            self.assertEqual(1, dep["spec"]["replicas"])
            return {"blocked": False}
        self.admission.side_effect = check
        result = self.dispatch()
        self.assertTrue(result["renamed"])
        self.assertEqual("succeeded", result["operation"]["status"])
        self.assertEqual(["POST", "PATCH", "PATCH", "DELETE"], [row[0] for row in self.sent])
        new = self.objects["new"]
        self.assertEqual("old", new["spec"]["template"]["metadata"]["labels"]["homestead.io/workload"])
        self.assertIn("new", [value for key, value in new["spec"]["selector"]["matchLabels"].items() if key.startswith("homestead.io/rename-")])
        self.assertEqual("data", new["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertNotIn("do-not-journal", json.dumps(ops.stored()))
        self.assertEqual(4, len(ops.stored()[0]["ref"]["writes"]))

    def test_the_change_mark_rides_on_both_scale_patches_and_the_receipts_still_pass(self):
        # The write guard appends the mark to each JSON patch (#621): with no
        # annotations yet as the whole map under the patch's version test, else
        # at its own key. The rename's own checks look only at spec.
        import homestead_changes as CH
        import homestead_kube as K

        def guarded(method, path, payload, **kwargs):
            CH.note_write(method, path, payload)
            result = self.send(method, path, payload, **kwargs)
            if method != "PATCH":
                return result
            self.assertEqual(4, len(payload), "one op added after the rename's own")
            obj, mark = self.objects[path.rsplit("/", 1)[-1]], payload[3]
            if mark["path"] == "/metadata/annotations":
                obj["metadata"]["annotations"] = mark["value"]
            else:
                self.assertEqual(CH._AUTHOR_POINTER, mark["path"])
                obj["metadata"]["annotations"][CH.AUTHOR] = mark["value"]
            return copy.deepcopy(obj)
        # A rename runs on the request's own thread, so its writes are the person's.
        CH.REQUEST.user = "ada"
        self.addCleanup(setattr, CH.REQUEST, "user", None)
        with mock.patch.object(CH, "_author_key", lambda: b"rename-mark"), mock.patch.object(K, "kget", self.read):
            result = self.dispatch(send=guarded)
        for name, value in (("old", self.sent[1][2][3]["value"][CH.AUTHOR]), ("new", self.sent[2][2][3]["value"])):
            mark = json.loads(value)
            self.assertEqual(CH._signed(b"rename-mark", "lab", name, mark["at"], "ada"), mark["by"], name)
        self.assertTrue(result["renamed"])
        self.assertEqual("succeeded", result["operation"]["status"])
        self.assertEqual(["POST", "PATCH", "PATCH", "DELETE"], [row[0] for row in self.sent])
        self.assertEqual("/metadata/annotations", self.sent[1][2][3]["path"], "the original had no annotations")
        self.assertEqual(CH._AUTHOR_POINTER, self.sent[2][2][3]["path"], "the new one has them from its create")
        self.assertIn(CH.AUTHOR, self.objects["new"]["metadata"]["annotations"])

    NEW_PATH = "/apis/apps/v1/namespaces/lab/deployments/new"

    def old_pods_outlast_the_wait(self, count=1, gone_by_the_check=True):
        """The stop wait (timeout=0: one look) sees a terminating old pod. With
        gone_by_the_check it has gone by the put-back's first look at the pods."""
        if gone_by_the_check:
            self.pods_gone_after = 2  # the replacement's check after its POST, then the wait
        self.source["spec"]["replicas"] = count
        self.source["status"] = self.state(count, 1)
        self.sets = [{"metadata": {"namespace": "lab", "uid": "rs-old", "ownerReferences": [
            {"kind": "Deployment", "uid": "uid-old", "controller": True}]}}]
        self.pods = [{"metadata": {"namespace": "lab", "uid": "pod-old", "deletionTimestamp": "now", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "rs-old", "controller": True}]}, "status": {"phase": "Running"}}]

    def during_the_stop(self, change):
        """Run `change` once the original has been scaled to 0."""
        def hook(method, name, obj):
            if method == "PATCH" and name == "old" and obj["spec"]["replicas"] == 0:
                change()
        self.after_send = hook

    def assert_original_back(self, count, writes=("POST", "PATCH", "PATCH", "PATCH")):
        """The original is at its earlier replica count again; the replacement was kept, stopped."""
        self.assertEqual(list(writes), [row[0] for row in self.sent])
        self.assertEqual("/apis/apps/v1/namespaces/lab/deployments/old", self.sent[-1][1])
        self.assertEqual(count, self.objects["old"]["spec"]["replicas"])
        self.assertEqual(0, self.objects["new"]["spec"]["replicas"])
        job = ops.stored()[0]
        self.assertEqual("failed", job["status"])
        self.assertEqual({"name": "old", "replicas": count}, job["ref"]["restored"])
        self.assertIn(f"old was put back to {count} replica", job["message"])
        self.assertNotIn("Nothing was rolled back", job["message"])
        self.assertTrue(job["ref"]["retain_resources"], "the stopped replacement is still there")
        self.assertTrue(ops.list_operations()[0]["cleanable"])

    def assert_not_put_back(self, why, count=1, says=(), both_stopped=False, writes=("POST", "PATCH")):
        """No put-back. The job says why, what each workload is now, and the way back."""
        self.assertEqual(list(writes), [row[0] for row in self.sent])
        job = ops.stored()[0]
        self.assertEqual("failed", job["status"])
        self.assertNotIn("restored", job["ref"])
        self.assertIn(f"old was not put back to {count} replica", job["message"])
        self.assertIn(why, job["message"])
        for words in says:
            self.assertIn(words, job["message"])
        (self.assertIn if both_stopped else self.assertNotIn)("both are stopped", job["message"])
        self.assertIn(f"Containers → old → Start with {count} replica", job["message"])
        self.assertIn("then delete new", job["message"])
        self.assertTrue(ops.list_operations()[0]["cleanable"])

    def test_old_pods_still_stopping_after_the_wait_keep_both_stopped(self):
        # New pods beside a terminating one could be two writers on one volume.
        self.old_pods_outlast_the_wait(2, gone_by_the_check=False)
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old was not put back"):
            self.dispatch(settle=6)
        self.assert_not_put_back("its old pods are still stopping", 2, ("old is still stopping", "new is stopped"))
        self.assertEqual([2, 2, 2], self.slept, "a short bounded wait: 6 s in 2 s steps")
        self.assertEqual(0, self.objects["old"]["spec"]["replicas"])

    def test_old_pods_outlasting_the_stop_wait_put_the_original_back_once_gone(self):
        self.old_pods_outlast_the_wait(2)
        self.status_lags = False  # the controller's status write landed before the put-back's reads
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old was put back to 2 replicas"):
            self.dispatch()
        self.assert_original_back(2, ("POST", "PATCH", "PATCH"))
        # Checked again at the count it goes back to, before the write.
        self.assertEqual(1, self.admission.call_count)
        self.assertEqual("old", self.admission.call_args.args[0]["metadata"]["name"])
        self.assertEqual(2, self.admission.call_args.args[0]["spec"]["replicas"])

    def test_the_put_back_waits_a_little_for_the_old_pods_to_go(self):
        self.old_pods_outlast_the_wait(2)
        self.pods_gone_after = 3  # still there at the put-back's first look, gone at its second
        with self.assertRaisesRegex(ValueError, "old was put back to 2 replicas"):
            self.dispatch(settle=60)
        self.assertEqual([2], self.slept)
        self.assert_original_back(2)

    def test_late_capacity_block_leaves_both_stopped_for_a_person(self):
        self.source["spec"]["replicas"] = 3
        self.source["status"] = self.state(3, 1)
        self.admission.return_value = {"blocked": True}
        with self.assertRaisesRegex(ValueError, "no longer fits.*old was not put back.*both are stopped"):
            self.dispatch()
        self.assert_not_put_back("no longer fits", 3, both_stopped=True)
        self.assertEqual(1, self.admission.call_count, "no second check: nothing is put back")

    def test_original_that_no_longer_fits_is_not_put_back(self):
        self.old_pods_outlast_the_wait()
        self.admission.return_value = {"blocked": True}
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old was not put back.*both are stopped"):
            self.dispatch()
        self.assert_not_put_back("old no longer fits", both_stopped=True)

    def test_original_changed_outside_the_rename_is_not_put_back(self):
        # Someone else scaled it up again while it stopped: never write over them,
        # and never say both are stopped.
        def restarted(dep):
            self.objects["old"]["spec"]["replicas"] = 4
            return {"blocked": False}
        self.admission.side_effect = restarted
        with self.assertRaisesRegex(ValueError, "changed or restarted.*old was not put back.*old is set to 4 replicas; new is stopped"):
            self.dispatch()
        self.assert_not_put_back("changed or restarted", says=("old is set to 4 replicas", "new is stopped"))
        self.assertEqual(4, self.objects["old"]["spec"]["replicas"])

    def test_replacement_scaled_up_meanwhile_keeps_the_original_stopped(self):
        self.old_pods_outlast_the_wait()
        self.during_the_stop(lambda: self.objects["new"]["spec"].update(replicas=1))
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old was not put back"):
            self.dispatch()
        self.assert_not_put_back("new changed outside this rename", says=("old is stopped", "new is set to 1 replica"))
        self.assertEqual(1, self.objects["new"]["spec"]["replicas"])
        self.assertEqual(0, self.objects["old"]["spec"]["replicas"])

    def test_replacement_that_cannot_be_read_keeps_the_original_stopped(self):
        self.old_pods_outlast_the_wait()
        self.during_the_stop(lambda: self.unreadable.add(self.NEW_PATH))
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old was not put back"):
            self.dispatch()
        self.assert_not_put_back("could not be checked", says=("could not read new, so check before starting anything",))

    def test_late_autoscaler_on_the_replacement_keeps_the_original_stopped(self):
        self.old_pods_outlast_the_wait()
        self.during_the_stop(lambda: self.hpas.append({"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "new"}}}))
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old was not put back.*both are stopped"):
            self.dispatch()
        self.assert_not_put_back("autoscaling", both_stopped=True)

    def test_a_put_back_whose_reply_is_lost_says_so_and_is_not_retried(self):
        self.old_pods_outlast_the_wait()
        def lost_restore(method, path, payload, **kwargs):
            result = self.send(method, path, payload, **kwargs)
            if method == "PATCH" and path.endswith("/old") and payload[2]["value"]:
                raise TimeoutError("do-not-journal")
            return result
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old could not be put back to 1 replica"):
            self.dispatch(send=lost_restore)
        self.assertEqual(["POST", "PATCH", "PATCH", "PATCH"], [row[0] for row in self.sent])
        job = ops.stored()[0]
        self.assertEqual("failed", job["status"])
        self.assertEqual("uncertain", job["ref"]["writes"][-1]["phase"])
        self.assertNotIn("restored", job["ref"])
        self.assertIn("old is set to 1 replica", job["message"])
        self.assertNotIn("do-not-journal", json.dumps(job))

    def test_a_put_back_accepted_with_other_changes_says_it_may_be_running(self):
        self.old_pods_outlast_the_wait()
        def mutate(method, name, obj):
            if method == "PATCH" and name == "old" and obj["spec"]["replicas"]:
                obj["spec"]["template"]["spec"]["hostNetwork"] = True
        self.after_send = mutate
        with self.assertRaisesRegex(ValueError, "old was scaled to 1 replica, but it came back with other changes: it may be running"):
            self.dispatch()
        self.assertNotIn("restored", ops.stored()[0]["ref"])
        self.assertNotIn("both are stopped", ops.stored()[0]["message"])

    def test_a_status_write_as_the_last_old_pod_goes_still_puts_the_original_back(self):
        # The controller's status write moves resourceVersion between the last
        # read and the PATCH: the test op fails (422) and nothing is written.
        # The spec is still the stopped one, so it is read again and sent once more.
        self.old_pods_outlast_the_wait(2)
        with self.assertRaisesRegex(ValueError, "did not finish stopping.*old was put back to 2 replicas"):
            self.dispatch()
        self.assert_original_back(2)
        refused, written = self.sent[2][2], self.sent[3][2]
        self.assertEqual(int(refused[1]["value"]) + 1, int(written[1]["value"]), "the second try tests the fresh resourceVersion")
        self.assertEqual("written", self.status_write, "the controller's status write landed between the read and the PATCH")
        self.assertIn("f:terminatingReplicas", json.dumps(self.objects["old"]["metadata"]["managedFields"]))
        self.assertEqual(["refused", "accepted"], [row["phase"] for row in ops.stored()[0]["ref"]["writes"][-2:]])
        self.assertNotIn("not confirmed", ops.stored()[0]["message"])
        self.assertEqual(2, self.admission.call_count, "the retry is checked again at the count it goes back to")

    def test_an_edit_while_it_was_being_put_back_is_not_written_over(self):
        # Someone changed the original's spec in that moment: the refused write
        # is not sent again over their change, and the job says what happened.
        self.old_pods_outlast_the_wait(2)
        self.on_status_write = lambda: self.objects["old"]["spec"]["template"]["spec"]["containers"][0].update(image="example/test:2")
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: old was edited by someone else while it was being put back"):
            self.dispatch()
        self.assert_not_put_back("nothing was written over it", 2, both_stopped=True, writes=("POST", "PATCH", "PATCH"))
        self.assertEqual("example/test:2", self.objects["old"]["spec"]["template"]["spec"]["containers"][0]["image"])
        self.assertEqual(0, self.objects["old"]["spec"]["replicas"])
        self.assertEqual("refused", ops.stored()[0]["ref"]["writes"][-1]["phase"])
        self.assertNotIn("not confirmed", ops.stored()[0]["message"])

    def assert_edit_in_that_moment_refused(self, edit):
        # Labels, annotations, owners and generation are not status: a change
        # to any of them in that moment is someone else's, and is refused.
        self.old_pods_outlast_the_wait(2)
        self.on_status_write = lambda: edit(self.objects["old"]["metadata"])
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: old was edited by someone else while it was being put back"):
            self.dispatch()
        self.assert_not_put_back("nothing was written over it", 2, both_stopped=True, writes=("POST", "PATCH", "PATCH"))
        self.assertEqual(0, self.objects["old"]["spec"]["replicas"])

    def test_an_annotation_added_in_that_moment_is_not_written_over(self):
        self.assert_edit_in_that_moment_refused(lambda meta: meta.setdefault("annotations", {}).update({"example.com/note": "x"}))

    def test_a_label_changed_in_that_moment_is_not_written_over(self):
        self.assert_edit_in_that_moment_refused(lambda meta: meta.setdefault("labels", {}).update(team="b"))

    def test_an_owner_added_in_that_moment_is_not_written_over(self):
        self.assert_edit_in_that_moment_refused(lambda meta: meta.update(ownerReferences=[{"kind": "Example", "uid": "o", "controller": True}]))

    def test_a_generation_bump_in_that_moment_is_not_written_over(self):
        self.assert_edit_in_that_moment_refused(lambda meta: meta.update(generation=meta["generation"] + 1))

    def edit_at_admission(self, call):
        """Someone labels the original right after admission call `call` (1: the first try's checks, 2: the retry's)."""
        calls = []
        def check(dep):
            calls.append(dep)
            if len(calls) == call:
                self.someone_labels_the_original()
            return {"blocked": False}
        self.admission.side_effect = check

    def test_an_edit_between_the_first_checks_and_the_write_is_not_written_over(self):
        self.old_pods_outlast_the_wait(2)
        self.edit_at_admission(1)
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: old was edited by someone else while it was being put back"):
            self.dispatch()
        self.assert_not_put_back("nothing was written over it", 2, both_stopped=True, writes=("POST", "PATCH"))
        self.assertEqual("b", self.objects["old"]["metadata"]["labels"]["team"])
        self.assertEqual(0, self.objects["old"]["spec"]["replicas"])

    def test_an_edit_between_the_retry_checks_and_its_write_is_not_written_over(self):
        self.old_pods_outlast_the_wait(2)
        self.edit_at_admission(2)
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: old was edited by someone else while it was being put back"):
            self.dispatch()
        self.assert_not_put_back("nothing was written over it", 2, both_stopped=True, writes=("POST", "PATCH", "PATCH"))
        self.assertEqual("b", self.objects["old"]["metadata"]["labels"]["team"])
        self.assertEqual(0, self.objects["old"]["spec"]["replicas"])

    def test_an_original_replaced_in_that_moment_is_named_as_replaced(self):
        self.old_pods_outlast_the_wait(2)
        self.on_status_write = lambda: self.objects["old"]["metadata"].update(uid="uid-other")
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: old was replaced by another object of the same name"):
            self.dispatch()
        self.assert_not_put_back("replaced by another object of the same name", 2, writes=("POST", "PATCH", "PATCH"))
        self.assertNotIn("edited by someone else", ops.stored()[0]["message"])

    def test_an_original_recreated_before_the_put_back_is_not_scaled(self):
        # Deleted and created again with the same spec (stopped) between the
        # stop wait and the put-back's first look: someone else's Deployment.
        self.old_pods_outlast_the_wait(2)
        read = self.read
        def recreated(path):
            if path == "/api/v1/pods" and self.pod_reads == 1:  # the stop wait's look at the pods is next
                self.objects["old"]["metadata"].update(uid="uid-other", resourceVersion="40")
            return read(path)
        self.read = recreated
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: old was replaced by another object of the same name"):
            self.dispatch()
        self.assert_not_put_back("replaced by another object of the same name", 2, writes=("POST", "PATCH"))
        self.assertEqual(0, self.objects["old"]["spec"]["replicas"])
        self.assertNotIn("edited by someone else", ops.stored()[0]["message"])

    def test_the_retry_runs_the_same_checks_as_the_first_try(self):
        # The original no longer fits when it is read again: no second PATCH.
        self.old_pods_outlast_the_wait(2)
        self.admission.side_effect = [{"blocked": False}, {"blocked": True}]
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: old no longer fits"):
            self.dispatch()
        self.assert_not_put_back("old no longer fits", 2, both_stopped=True, writes=("POST", "PATCH", "PATCH"))
        self.assertEqual(2, self.admission.call_count)
        self.assertEqual(2, self.admission.call_args.args[0]["spec"]["replicas"])

    def test_an_autoscaler_on_the_original_in_that_moment_stops_the_retry(self):
        self.old_pods_outlast_the_wait(2)
        self.on_status_write = lambda: self.hpas.append({"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "old"}}})
        with self.assertRaisesRegex(ValueError, "old was not put back to 2 replicas: Remove autoscaling"):
            self.dispatch()
        self.assert_not_put_back("autoscaling", 2, both_stopped=True, writes=("POST", "PATCH", "PATCH"))

    def test_a_put_back_refused_twice_is_not_tried_a_third_time(self):
        self.old_pods_outlast_the_wait()
        def refuse_restores(method, path, payload, **kwargs):
            if method == "PATCH" and path.endswith("/old") and payload[2]["value"]:
                self.sent.append((method, path, copy.deepcopy(payload)))
                raise urllib.error.HTTPError(path, 422, "Unprocessable Entity", {}, None)
            return self.send(method, path, payload, **kwargs)
        with self.assertRaisesRegex(ValueError, "old was not put back to 1 replica: the API server refused the write twice, so nothing was written"):
            self.dispatch(send=refuse_restores)
        self.assert_not_put_back("refused the write twice", both_stopped=True, writes=("POST", "PATCH", "PATCH", "PATCH"))
        self.assertEqual(["refused", "refused"], [row["phase"] for row in ops.stored()[0]["ref"]["writes"][-2:]])

    def test_the_count_before_the_rename_is_kept_for_recovery(self):
        self.source["spec"]["replicas"] = 3
        self.source["status"] = self.state(3, 1)
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.dispatch()
        job = ops.stored()[0]
        self.assertEqual({"replicas": 3, "uid": "uid-old", "new_uid": "uid-new"}, job["ref"]["before_rename"])
        self.assertIn("old had 3 replicas before the rename", rename.status(job, ops)[2])
        self.assertIn("old had 3 replicas before the rename", rename.recovery_plan(job, self.read, ops)["keeps"])

    def test_repeated_rename_preserves_labels_a_later_service_may_select(self):
        self.dispatch()
        before = self.objects["new"]
        again = WORKLOAD_EDIT._renamed_deployment(before, "lab", "third")
        labels = again["spec"]["template"]["metadata"]["labels"]
        for key, value in before["spec"]["selector"]["matchLabels"].items():
            self.assertEqual(value, labels[key])
            self.assertEqual(value, again["spec"]["selector"]["matchLabels"][key])
        self.assertEqual(len(before["spec"]["selector"]["matchLabels"]) + 1,
                         len(again["spec"]["selector"]["matchLabels"]))

    def test_custom_controller_hash_label_cannot_be_silently_removed(self):
        self.source["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] = "custom"
        with self.assertRaisesRegex(ValueError, "existing pod labels"):
            self.prepare()
        self.assertEqual([], self.sent)

    def test_incomplete_capacity_response_cannot_start_replacement(self):
        self.admission.return_value = {}
        with self.assertRaisesRegex(ValueError, "no longer fits"):
            self.dispatch()
        self.assert_not_put_back("no longer fits", both_stopped=True)

    def test_memory_warning_is_overridable_but_actual_api_injected_requests_are_checked(self):
        def injected(method, name, obj):
            if method == "POST":
                obj["spec"]["template"]["spec"]["containers"].append({"name": "injected", "resources": {"requests": {"memory": "2Gi"}}})
        self.after_send = injected
        self.admission.return_value = {"blocked": False, "warnings": ["Memory above warning limit"], "requires_confirmation": True}
        self.dispatch()
        self.assertEqual(2, len(self.admission.call_args.args[0]["spec"]["template"]["spec"]["containers"]))

    def test_changed_review_or_same_name_replacement_never_writes(self):
        self.prepare()
        self.source["metadata"]["uid"] = "foreign"
        with self.assertRaisesRegex(ValueError, "changed after the review"):
            self.dispatch()
        self.assertEqual([], self.sent)

    def test_target_automatically_started_by_external_actor_does_not_stop_source(self):
        self.after_send = lambda method, name, obj: obj["spec"].update(replicas=1) if method == "POST" else None
        with self.assertRaisesRegex(ValueError, "not confirmed as stopped"):
            self.dispatch()
        self.assertEqual(["POST"], [row[0] for row in self.sent])
        self.assertEqual(1, self.source["spec"]["replicas"])

    def test_terminating_old_pods_block_even_with_zero_controller_counters(self):
        self.sets = [{"metadata": {"namespace": "lab", "uid": "rs-old", "ownerReferences": [
            {"kind": "Deployment", "uid": "uid-old", "controller": True}]}}]
        self.pods = [{"metadata": {"namespace": "lab", "uid": "pod-old", "deletionTimestamp": "now", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "rs-old", "controller": True}]}, "status": {"phase": "Running"}}]
        with self.assertRaisesRegex(ValueError, "did not finish stopping"):
            self.dispatch()
        self.assert_not_put_back("its old pods are still stopping", says=("old is still stopping",))

    def test_unknown_pod_ownership_fails_closed_before_stopping_source(self):
        self.pods = [{"metadata": {"namespace": "lab", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "missing", "controller": True}]}, "status": {"phase": "Running"}}]
        with self.assertRaisesRegex(ValueError, "not confirmed as stopped"):
            self.dispatch()
        self.assertEqual(["POST"], [row[0] for row in self.sent])

    def test_target_replaced_or_edited_during_stop_is_never_started(self):
        def replaced(method, name, obj):
            if method == "PATCH" and name == "old":
                self.objects["new"]["metadata"]["uid"] = "foreign"
        self.after_send = replaced
        with self.assertRaisesRegex(ValueError, "was replaced.*old was not put back"):
            self.dispatch()
        self.assert_not_put_back("was replaced", says=("new was replaced by another Deployment",))
        self.assertEqual("foreign", self.objects["new"]["metadata"]["uid"], "the foreign replacement is never touched")

    def test_autoscaler_on_either_name_blocks_before_any_write(self):
        for name in ("old", "new"):
            self.hpas = [{"spec": {"scaleTargetRef": {"kind": "Deployment", "name": name}}}]
            with self.assertRaisesRegex(ValueError, "autoscaling"):
                self.prepare()
        self.assertEqual([], self.sent)

    def test_late_autoscaler_does_not_scale_original(self):
        self.after_send = lambda *args: self.hpas.append({"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "old"}}})
        with self.assertRaisesRegex(ValueError, "autoscaling"):
            self.dispatch()
        self.assertEqual(["POST"], [row[0] for row in self.sent])

    def test_owner_paused_and_helm_managed_workloads_are_refused(self):
        for field, value in (("ownerReferences", [{"uid": "controller"}]), ("annotations", {"meta.helm.sh/release-name": "release"})):
            self.source["metadata"][field] = value
            with self.assertRaisesRegex(ValueError, "managed workload"):
                self.prepare()
            self.source["metadata"].pop(field)
        self.source["spec"]["paused"] = True
        with self.assertRaisesRegex(ValueError, "paused"):
            self.prepare()

    def test_unverified_import_is_refused_and_completed_import_is_not_rebound_to_wrong_uid(self):
        self.source["metadata"]["annotations"] = {rename.IMPORT.JOB: "copy"}
        with self.assertRaisesRegex(ValueError, "Import copy"):
            self.prepare()
        self.extra["/apis/batch/v1/namespaces/lab/jobs/copy"] = {
            "metadata": {"ownerReferences": [{"kind": "Deployment", "uid": "uid-old"}]},
            "status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        self.dispatch()
        self.assertNotIn(rename.IMPORT.JOB, self.objects["new"]["metadata"]["annotations"])

    def refuse(self, nth_method, code, only=lambda payload: True):
        """A send that the API server refuses (a definite answer: nothing was written) for one kind of write."""
        def refusing(method, path, payload, **kwargs):
            if method == nth_method and only(payload):
                self.sent.append((method, path, copy.deepcopy(payload)))
                raise urllib.error.HTTPError(path, code, "refused", {}, None)
            return self.send(method, path, payload, **kwargs)
        return refusing

    def assert_said_refused(self, caught, words, phase):
        text = str(caught.exception)
        self.assertIn(words, text)
        self.assertNotIn("did not confirm", text)
        for method in ("PATCH", "DELETE", "POST", "409", "422"):
            # As a word: the job id in the text is random hex and can contain "409".
            self.assertNotRegex(text, rf"\b{method}\b")
        self.assertEqual(phase, ops.stored()[0]["ref"]["writes"][-1]["phase"])

    def test_a_refused_stop_says_it_was_refused_not_unconfirmed(self):
        with self.assertRaises(ValueError) as caught:
            self.dispatch(send=self.refuse("PATCH", 409, lambda payload: payload[2]["value"] == 0))
        self.assert_said_refused(caught, "Kubernetes refused the change to old, so old was not changed.", "refused")

    def test_a_refused_start_of_the_replacement_says_it_was_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.dispatch(send=self.refuse("PATCH", 422, lambda payload: payload[2]["value"] == 1))
        self.assert_said_refused(caught, "Kubernetes refused the change to new, so new was not changed.", "refused")

    def test_a_refused_removal_of_the_original_says_it_was_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.dispatch(send=self.refuse("DELETE", 409))
        self.assert_said_refused(caught, "Kubernetes refused the removal of old, so old was not changed.", "refused")

    def test_a_refused_write_is_not_tried_again(self):
        with self.assertRaises(ValueError):
            self.dispatch(send=self.refuse("DELETE", 409))
        self.assertEqual(["POST", "PATCH", "PATCH", "DELETE"], [row[0] for row in self.sent])

    def test_rename_refuses_other_edits_and_existing_target(self):
        self.body["containers"] = []
        with self.assertRaisesRegex(ValueError, "separately"):
            self.prepare()
        self.body.pop("containers")
        self.objects["new"] = copy.deepcopy(self.source)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()

    def test_lost_response_retains_intent_and_never_retries_or_exposes_api_error(self):
        def lost(method, path, payload, **kwargs):
            self.send(method, path, payload, **kwargs)
            raise TimeoutError("do-not-journal private API response")
        with self.assertRaisesRegex(ValueError, "Kubernetes did not confirm the creation of new, so it may have taken effect") as caught:
            self.dispatch(send=lost)
        self.assertNotIn("do-not-journal", str(caught.exception))
        self.assertEqual(1, len(self.sent))
        job = ops.stored()[0]
        self.assertEqual("uncertain", job["ref"]["writes"][0]["phase"])
        self.assertEqual("failed", job["status"])
        self.assertNotIn("do-not-journal", json.dumps(job))

    def test_failed_journal_prevents_cluster_writes(self):
        with mock.patch.object(ops, "record_phase", side_effect=OSError("unwritable")):
            with self.assertRaisesRegex(ValueError, "job storage"):
                self.dispatch()
        self.assertEqual([], self.sent)
        self.assertEqual("queued", ops.stored()[0]["status"])

    def test_readiness_failure_keeps_new_running_and_old_stopped(self):
        def not_ready(method, name, obj):
            if method == "PATCH" and name == "new":
                obj["status"]["availableReplicas"] = 0
        self.after_send = not_ready
        with self.assertRaisesRegex(ValueError, "did not become ready"):
            self.dispatch()
        self.assertEqual(0, self.source["spec"]["replicas"])
        self.assertEqual(1, self.objects["new"]["spec"]["replicas"])
        self.assertEqual(["POST", "PATCH", "PATCH"], [row[0] for row in self.sent])

    def test_admission_mutation_during_scale_cannot_be_reported_as_success(self):
        def mutate(method, name, obj):
            if method == "PATCH" and name == "old":
                obj["spec"]["template"]["spec"]["hostNetwork"] = True
        self.after_send = mutate
        with self.assertRaisesRegex(ValueError, "more than the number of replicas"):
            self.dispatch()
        self.assertEqual(["POST", "PATCH"], [row[0] for row in self.sent])
        self.admission.assert_not_called()

    def test_replay_refused_after_success_and_visible_history_pruning(self):
        self.dispatch()
        put(ops, [])
        before = len(self.sent)
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()
        self.assertEqual(before, len(self.sent))

    def test_tracking_recovery_never_mutates_resources_or_reenables_old_approval(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.dispatch()
        job = ops.stored()[0]
        before = copy.deepcopy(self.objects), copy.deepcopy(self.sent)
        with locks.worker_lock(job["id"], ops):
            self.assertFalse(rename.recovery_plan(job, self.read, ops)["can"])
        self.assertTrue(rename.recovery_plan(job, self.read, ops)["can"])
        with self.assertRaisesRegex(ValueError, "Acknowledge"):
            ops.cancel(job["id"], options={}, confirm="old")
        # A failed acknowledgement leaves the job inspectable.
        ops.cancel(job["id"], options={"ack": True}, confirm="old")
        self.assertEqual(before, (self.objects, self.sent))
        self.assertTrue(ops.stored()[0]["tracking_stopped"])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.dispatch()

    def test_stopped_workload_stays_stopped(self):
        self.source["spec"]["replicas"] = 0
        self.source["status"] = self.state(0, 1)
        self.dispatch()
        self.assertEqual(0, self.objects["new"]["spec"]["replicas"])
        self.assertEqual(["POST", "PATCH", "DELETE"], [row[0] for row in self.sent])

    def test_interrupted_dispatch_remains_inspectable_without_replay(self):
        with mock.patch.object(ops, "record_phase", side_effect=OSError("unwritable")):
            with self.assertRaises(ValueError):
                self.dispatch()
        job = ops.list_operations()[0]
        self.assertEqual("running", job["status"])
        self.assertIn("no longer running", job["message"])
        self.assertTrue(job["rename_recovery"])
        self.assertEqual([], self.sent)

    def test_unacknowledged_failure_blocks_new_approval_touching_either_name(self):
        self.admission.return_value = {"blocked": True}
        with self.assertRaises(ValueError):
            self.dispatch()
        self.body["workload_name"] = "another"
        self.prepare()
        before = len(self.sent)
        with self.assertRaisesRegex(ValueError, "still running or needs recovery"):
            self.dispatch()
        self.assertEqual(before, len(self.sent))


if __name__ == "__main__":
    unittest.main()
