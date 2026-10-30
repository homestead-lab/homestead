import copy
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_capacity_review as SIGN
import homestead_changes as CHANGES
import homestead_self_data_prepare as P
import homestead_updates as UPDATES
import homestead_update_registry as REGISTRY
import homestead_update_checks as CHECKS
import server  # Bind the app before test fixtures install their signing key.
from homestead_storage_journal import Held
import test_self_data_coordinator as coordinator_fixture
import test_self_data_review as review_fixture
from test_self_data_coordinator import IMAGE, OP, obj
from fakes.job_store import put


class PreparationTests(unittest.TestCase):
    def setUp(self):
        fixture = review_fixture.ReviewTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.f, self.c = fixture.f, fixture.c
        self.source = self.c.objects["/api/v1/namespaces/lab/persistentvolumeclaims/source"]
        self.source["spec"]["resources"] = {"requests": {"storage": "2Gi"}}
        self.source["status"]["capacity"] = {"storage": "3Gi"}
        self.sc = obj("StorageClass", "destination-class", ns=None)
        self.sc.pop("spec")
        self.sc.update(provisioner="csi.example.test", volumeBindingMode="WaitForFirstConsumer")
        self.c.objects["/apis/storage.k8s.io/v1/storageclasses/destination-class"] = self.sc
        self.body = {"operation": OP, "storage_class": "destination-class", "node": "node1"}
        self.previews = []
        self.saved = []
        self.mutate = lambda _: None
        self.reviewed = self.review()
        self.item = {"id": "prepare-job", "ref": {**copy.deepcopy(self.reviewed[3]), "storage_writes": [], "retain_resources": True}, "progress": 0}

    def review(self):
        return P.review(self.f.read, "lab", "homestead", self.body, actor="admin", image=IMAGE,
                        access_mode="ReadWriteOnce", threshold=88, clock=lambda: 1000)

    def checkpoint(self, item): self.saved.append(copy.deepcopy(item))

    def send(self, method, path, body):
        if "?dryRun=All" in path:
            self.previews.append((method, path, copy.deepcopy(body)))
            return json.loads(json.dumps(body))
        self.assertEqual("intent", self.saved[-1]["ref"]["storage_writes"][-1]["state"])
        result = self.c.send(method, path, body)
        self.mutate(result)
        return result

    def tick(self):
        return P.resolve(self.item, self.f.read, self.send, self.checkpoint, clock=lambda: 1000)

    def path(self, plural):
        name = self.item["ref"]["destination"] if plural == "persistentvolumeclaims" else "homestead-data-bind-" + OP
        return f"/api/v1/namespaces/lab/{plural}/{name}"

    def bind(self):
        pvc = self.c.objects[self.path("persistentvolumeclaims")]
        pvc["spec"]["volumeName"] = "prepared-pv"
        pvc["metadata"]["resourceVersion"] = "2"
        pvc["metadata"]["annotations"] = {"pv.kubernetes.io/bind-completed": "yes"}
        pvc["status"] = {"phase": "Bound"}
        pv = obj("PersistentVolume", "prepared-pv", {"claimRef": {"namespace": "lab", "name": pvc["metadata"]["name"], "uid": pvc["metadata"]["uid"]}}, ns=None)
        self.c.objects["/api/v1/persistentvolumes/prepared-pv"] = pv

    def test_review_is_read_only_and_sizes_to_larger_current_capacity(self):
        before = len(self.c.sent)
        cfg, public, context, ref = self.review()
        self.assertEqual(before, len(self.c.sent))
        self.assertEqual("3Gi", public["size"])
        self.assertNotIn("do-not-serialize-private-values", json.dumps(ref))
        pvc, pod = P.manifests(ref)
        self.assertNotIn("nodeName", pod["spec"])
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertTrue(pod["spec"]["securityContext"]["runAsNonRoot"])
        self.assertEqual([ref["destination"]], [v["persistentVolumeClaim"]["claimName"] for v in pod["spec"]["volumes"]])
        self.assertTrue(pod["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"])

    def test_wait_for_first_consumer_prepares_then_releases_only_binding_pod(self):
        self.assertEqual(15, self.tick()[1])
        self.assertEqual(40, self.tick()[1])
        self.assertEqual(1, len(self.previews))
        self.assertEqual(50, self.tick()[1])
        pod = self.c.objects[self.path("pods")]
        pod["spec"]["nodeName"] = "node1"
        self.bind()
        self.assertEqual(85, self.tick()[1])
        self.assertEqual("succeeded", self.tick()[0])
        self.assertTrue(self.item["ref"]["prepared"]["claim_uid"])
        self.assertFalse(self.item["ref"]["retain_resources"])
        deletes = [(p, b) for m, p, b in self.c.sent if m == "DELETE"]
        self.assertEqual(1, len(deletes))
        self.assertEqual(self.path("pods"), deletes[0][0])
        self.assertEqual("Foreground", deletes[0][1]["propagationPolicy"])
        self.assertNotIn("gracePeriodSeconds", deletes[0][1])
        self.assertIn(self.path("persistentvolumeclaims"), self.c.objects)
        self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])

    def test_binding_pod_is_made_on_a_cluster_with_a_default_class(self):
        # #437: the Priority plugin gives the helper the default class's name and value.
        self.c.objects["/apis/scheduling.k8s.io/v1/priorityclasses"] = {"items": [
            {"metadata": {"name": "x-normal"}, "value": 10000, "globalDefault": True}]}
        send = self.send
        def admitting(method, path, body):
            if method == "POST" and path.split("?")[0].endswith("/pods"):
                body = copy.deepcopy(body)
                body["spec"].update(priorityClassName="x-normal", priority=10000, preemptionPolicy="PreemptLowerPriority")
            return send(method, path, body)
        self.send = admitting
        self.assertEqual(15, self.tick()[1])
        self.assertEqual(40, self.tick()[1])
        self.assertEqual(50, self.tick()[1])
        self.c.objects[self.path("pods")]["spec"]["nodeName"] = "node1"
        self.bind()
        self.assertEqual(85, self.tick()[1])
        self.assertEqual("succeeded", self.tick()[0])

    def test_binding_pod_hold_says_the_classes_couldnt_be_read(self):
        # #466: the classes can't be read on a cluster whose admission adds a default class.
        read = self.f.read
        def refused(path, *args, **kwargs):
            if path.split("?")[0] == "/apis/scheduling.k8s.io/v1/priorityclasses":
                raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
            return read(path, *args, **kwargs)
        self.f.read = refused
        send = self.send
        def admitting(method, path, body):
            if method == "POST" and path.split("?")[0].endswith("/pods"):
                body = copy.deepcopy(body)
                body["spec"].update(priorityClassName="x-normal", priority=10000, preemptionPolicy="PreemptLowerPriority")
            return send(method, path, body)
        self.send = admitting
        self.assertEqual(15, self.tick()[1])
        with self.assertRaises(Held) as held:
            self.tick()
        self.assertIn("couldn't read the cluster's PriorityClasses", str(held.exception))

    def test_immediate_binding_needs_no_helper_pod(self):
        self.sc["volumeBindingMode"] = "Immediate"
        self.item["ref"] = {**self.review()[3], "storage_writes": []}
        self.tick(); self.assertEqual(30, self.tick()[1]); self.bind()
        self.assertEqual("succeeded", self.tick()[0])
        self.assertNotIn(self.path("pods"), self.c.objects)
        self.assertEqual([], self.previews)

    def test_waits_for_confirmed_deletion_before_ready(self):
        self.tick(); self.tick(); self.bind()
        original = self.c.send
        def lingering(method, path, body, **kwargs):
            pod = copy.deepcopy(self.c.objects.get(path))
            result = original(method, path, body, **kwargs)
            if method == "DELETE":
                pod["metadata"]["deletionTimestamp"] = "now"
                self.c.objects[path] = pod
            return result
        self.c.send = lingering
        self.tick()
        self.assertEqual(90, self.tick()[1])
        self.assertNotIn("prepared", self.item["ref"])
        self.c.objects.pop(self.path("pods"))
        self.assertEqual("succeeded", self.tick()[0])

    def test_lost_creation_replies_never_replay_or_adopt(self):
        for resource in ("persistentvolumeclaims", "pods"):
            self.setUp()
            if resource == "pods": self.tick()
            self.c.lost = lambda method, path, body: method == "POST" and "/" + resource + "/" in path
            with self.assertRaises(Held): self.tick()
            self.assertEqual("uncertain", self.item["ref"]["storage_writes"][-1]["state"])
            before = len(self.c.sent)
            with self.assertRaises(Held): self.tick()
            self.assertEqual(before, len(self.c.sent))

    def test_existing_destination_is_not_adopted(self):
        self.c.objects[self.path("persistentvolumeclaims")] = obj("PersistentVolumeClaim", self.item["ref"]["destination"])
        with self.assertRaises(Held): self.tick()
        self.assertEqual([], self.item["ref"]["storage_writes"])
        with self.assertRaisesRegex(Held, "already used"): self.review()

    def test_replaced_pvc_or_pv_binding_is_rejected(self):
        self.tick()
        self.c.objects[self.path("persistentvolumeclaims")]["metadata"]["uid"] = "replacement"
        with self.assertRaises(Held): self.tick()
        self.setUp(); self.tick(); self.bind()
        self.c.objects["/api/v1/persistentvolumes/prepared-pv"]["spec"]["claimRef"]["uid"] = "other-claim"
        with self.assertRaises(Held): self.tick()

    def test_storage_class_change_or_capacity_loss_blocks_dispatch(self):
        self.sc["allowedTopologies"] = [{"matchLabelExpressions": [{"key": "zone", "values": ["other"]}]}]
        before = len(self.c.sent)
        with self.assertRaises(Held): self.tick()
        self.assertEqual(before, len(self.c.sent))
        self.setUp(); self.f.nodes[0]["status"]["allocatable"]["memory"] = "1Mi"
        with self.assertRaises(Held): self.tick()
        self.assertEqual([], self.item["ref"]["storage_writes"])

    def test_binding_uses_real_capacity_and_rechecks_before_pod(self):
        self.tick()
        self.f.nodes[0]["status"]["allocatable"]["memory"] = "1Mi"
        with self.assertRaises(Held): self.tick()
        self.assertNotIn(self.path("pods"), self.c.objects)

    def test_pod_policy_denial_retains_pvc_without_live_pod(self):
        self.tick()
        def send(*args): raise urllib.error.HTTPError("private", 403, "private policy", {}, None)
        with self.assertRaises(Held) as caught: P.resolve(self.item, self.f.read, send, self.checkpoint, clock=lambda: 1000)
        self.assertNotIn("private", str(caught.exception))
        self.assertIn(self.path("persistentvolumeclaims"), self.c.objects)
        self.assertNotIn(self.path("pods"), self.c.objects)

    def write_own_deployment(self, method, path, body, ctype=None):
        """Homestead's own write to its Deployment, as it reaches the cluster:
        through the change history's write guard (homestead_kube.change_note),
        then merged onto the live object. A set-up Homestead has the key the
        mark is signed with."""
        with mock.patch.object(CHANGES, "_author_key", lambda: b"signing-key"):
            CHANGES.note_write(method, path, body)
        self.assertEqual("/apis/apps/v1/namespaces/lab/deployments/homestead", path)
        dep = self.c.objects[path]

        def merge(into, patch):
            for key, value in patch.items():
                if value is None: into.pop(key, None)
                elif isinstance(value, dict) and isinstance(into.get(key), dict): merge(into[key], value)
                else: into[key] = copy.deepcopy(value)
        merge(dep, body)
        dep["metadata"]["resourceVersion"] = str(int(dep["metadata"]["resourceVersion"]) + 1)
        return copy.deepcopy(dep)

    def scan_own_image(self, image_id=IMAGE):
        """The background image scan records the digest Homestead runs on, with
        the real update-check code and the real write guard."""
        dep = copy.deepcopy(self.c.objects["/apis/apps/v1/namespaces/lab/deployments/homestead"])
        pod = {"metadata": {"namespace": "lab", "labels": {}},
               "status": {"phase": "Running", "containerStatuses": [
                   {"name": "homestead", "state": {"running": {}}, "imageID": image_id}]}}
        with mock.patch.object(UPDATES, "ksend", self.write_own_deployment), \
             mock.patch.object(UPDATES, "PART", lambda ns, name: "self"), \
             mock.patch.object(UPDATES, "CHANNEL", lambda: "prod"), \
             mock.patch.object(UPDATES, "VERSION", lambda: ""), \
             mock.patch.object(REGISTRY, "registry_tags", lambda *a, **k: []), \
             mock.patch.object(REGISTRY, "manifest_info", lambda *a, **k: {"digest": IMAGE.split("@")[1], "children": []}), \
             mock.patch.object(REGISTRY, "_secret_credentials", lambda *a: {}):
            CHECKS._check_deployment(dep, [pod])

    def test_an_image_scan_during_preparation_is_not_a_change_to_homestead(self):
        # #680 put the change-history mark on every Deployment write, so the
        # scan's ran-digests record came with homestead.io/changedBy and the
        # preparation refused: "Homestead or its source changed during preparation".
        self.tick()
        self.scan_own_image()
        annotations = self.c.objects["/apis/apps/v1/namespaces/lab/deployments/homestead"]["metadata"]["annotations"]
        self.assertIn("homestead.io/ran-digests", annotations, "the scan recorded what runs")
        self.assertIn(CHANGES.AUTHOR, annotations, "the write guard marked the scan's write")
        self.tick(); self.bind(); self.tick(); self.tick()
        self.assertEqual("succeeded", self.tick()[0])

    def test_a_real_edit_to_homestead_during_preparation_still_refuses(self):
        path = "/apis/apps/v1/namespaces/lab/deployments/homestead"
        edits = {"image": {"spec": {"template": {"spec": {"containers": [
                     {"name": "homestead", "image": "ghcr.io/homestead-lab/homestead:other"}]}}}},
                 "env": {"spec": {"template": {"spec": {"containers": [
                     {"name": "homestead", "image": IMAGE, "env": [{"name": "EXTRA", "value": "1"}]}]}}}},
                 "annotation": {"metadata": {"annotations": {"user.example/hold": "true"}}}}
        for what, body in edits.items():
            with self.subTest(what):
                self.setUp(); self.tick()
                self.write_own_deployment("PATCH", path, body)
                with self.assertRaisesRegex(Held, "changed during preparation"): self.tick()

    def test_no_source_or_deployment_writes_and_no_claim_deletion(self):
        self.tick(); self.tick(); self.bind(); self.tick(); self.tick()
        allowed = {self.path("persistentvolumeclaims"), self.path("pods")}
        for entry in self.item["ref"]["storage_writes"]:
            self.assertIn(entry["target"]["path"], allowed)
            self.assertFalse(entry["method"] == "DELETE" and entry["target"]["kind"] == "PersistentVolumeClaim")

    def test_start_requires_signed_review_and_records_intent_before_any_cluster_write(self):
        import homestead_operations as OPS
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        cfg, public, context, ref = self.reviewed
        body = {**self.body, "confirm_capacity": True, "capacity_token": SIGN.issue(cfg, context)}
        with mock.patch.object(OPS, "DATA_DIR", temp.name):
            before = len(self.c.sent)
            with OPS.transaction():
                with self.assertRaises(Held): P.start(self.body, self.reviewed, OPS)
                job = P.start(body, self.reviewed, OPS)
                saved = OPS.stored()[0]
                self.assertEqual(P.KIND, saved["kind"])
                self.assertEqual([], saved["ref"]["storage_writes"])
                with self.assertRaises(Held): P.start(body, self.reviewed, OPS)
            self.assertEqual(before, len(self.c.sent))
            self.assertEqual("queued", job["status"])
            self.assertFalse(P.cancel_plan(saved)["can"])
            with self.assertRaises(Held): P.cancel_run(saved, {})

    def test_http_preview_start_and_standard_job_poll_complete_preparation(self):
        import server
        OPS = server.OPS
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.c.objects.pop(self.c.anchor.path)
        def send(method, path, body, **kwargs):
            if "?dryRun=All" in path: return json.loads(json.dumps(body))
            saved = OPS.stored()[0]
            self.assertEqual("intent", saved["ref"]["storage_writes"][-1]["state"])
            return self.c.send(method, path, body, **kwargs)
        def post(path, body):
            handler = object.__new__(server.H)
            handler.path = path; handler.user = "admin"; handler.headers = {}
            handler._guard = mock.Mock(return_value=False); handler._body = mock.Mock(return_value=body)
            handler._client_ip = mock.Mock(return_value="127.0.0.1"); handler._send = mock.Mock()
            handler.do_POST()
            self.assertEqual(200, handler._send.call_args.args[0], handler._send.call_args.args)
            self.assertEqual("admin", server.needed_role(path, "POST"))
            return handler._send.call_args.args[1]
        with mock.patch.object(OPS, "DATA_DIR", temp.name), mock.patch.object(server, "_self_data_fence", None), \
             mock.patch.object(server.SELF, "NS", "lab"), mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=self.f.read), \
             mock.patch.object(server.KUBE_HELPERS, "ksend", side_effect=send), \
             mock.patch.object(server.SELF_DATA_ROUTES, "_self_data_helper_image", return_value=({}, IMAGE)), \
             mock.patch.object(server.SELF_DATA_ROUTES, "homestead_data_volume", return_value={"pvc": "source", "classes": [{"name": "destination-class", "shareable": False}]}), \
             mock.patch.object(server.SETTINGS_HELPERS, "get_app_settings", return_value={"thresholds": {"memory": {"critical": 88}}}):
            preview = post("/api/self/data/prepare/preview", self.body)
            self.assertEqual([], OPS.stored())
            started = post("/api/self/data/prepare", {**self.body, "confirm_capacity": True, "capacity_token": preview["capacity_token"]})
            self.assertEqual("queued", started["operation"]["status"])
            self.assertEqual(15, OPS.list_operations()[0]["progress"])
            self.assertEqual(40, OPS.list_operations()[0]["progress"])
            self.bind()
            self.assertEqual(85, OPS.list_operations()[0]["progress"])
            self.assertEqual("succeeded", OPS.list_operations()[0]["status"])
            self.assertFalse(OPS.list_operations()[0]["dismissible"])
            self.assertFalse(OPS.list_operations()[0]["cleanable"])
            with mock.patch.object(OPS, "MAX_OPERATIONS", 0):
                put(OPS)
            self.assertEqual(1, len(OPS.stored()), "Prepared identities must survive history pruning")
            with mock.patch.object(OPS, "list_operations", side_effect=AssertionError("status must not run resolvers")):
                handler = object.__new__(server.H)
                handler.path = "/api/self/data/prepare"; handler._guard = mock.Mock(return_value=False)
                handler._send = mock.Mock(); handler.do_GET()
                self.assertEqual(200, handler._send.call_args.args[0])
                state = handler._send.call_args.args[1]
                self.assertTrue(state["preparations"][0]["prepared"])
                self.assertEqual(started["operation"]["id"], state["preparations"][0]["id"])
                for field in ("storage_writes", "capacity_token", "admission", "image", "class"):
                    self.assertNotIn(field, state["preparations"][0])
            before = len(self.c.sent)
            OPS.list_operations()
            self.assertEqual(before, len(self.c.sent))
            self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])

    def test_real_job_hold_is_failed_retained_and_not_repolled_as_work(self):
        import server
        item = {**self.item, "progress": 15}
        with mock.patch.object(server.SELF_DATA_PREPARE, "resolve", side_effect=Held("Admission changed")):
            self.assertEqual(("failed", 15, "Admission changed"), server.SELF_DATA_ROUTES.self_data_preparation_progress(item))
            self.assertTrue(item["ref"]["retain_resources"])

    def test_lost_deletion_reply_is_not_repeated_even_when_pod_disappeared(self):
        self.tick(); self.tick(); self.bind()
        self.c.lost = lambda method, *_: method == "DELETE"
        with self.assertRaises(Held): self.tick()
        self.assertNotIn(self.path("pods"), self.c.objects)
        count = len(self.c.sent)
        with self.assertRaises(Held): self.tick()
        self.assertEqual(count, len(self.c.sent))

    def churn_pod_on_read(self, change):
        """Model kubelet: the binding pod changes right after every read of it."""
        original = self.f.read
        def read(path):
            result = original(path)
            if path == self.path("pods") and path in self.c.objects:
                change(self.c.objects[path])
            return result
        self.f.read = read

    def test_status_update_while_releasing_the_binding_pod_does_not_stop_preparation(self):
        # Row 92 / CI self-data-k3s: on the tick the destination binds, kubelet
        # updates the pod's status between Homestead's read and its delete.
        self.tick(); self.tick(); self.bind()
        def status(pod):
            pod["metadata"]["resourceVersion"] = str(int(pod["metadata"]["resourceVersion"]) + 1)
            pod["status"] = {"phase": "Running"}
        self.churn_pod_on_read(status)
        self.assertEqual(85, self.tick()[1])
        self.assertEqual("succeeded", self.tick()[0])
        deletes = [b for m, p, b in self.c.sent if m == "DELETE"]
        self.assertEqual([{"uid": "homestead-data-bind-" + OP + "-created"}], [b["preconditions"] for b in deletes])

    def test_binding_pod_replaced_between_review_and_delete_is_never_deleted(self):
        self.tick(); self.tick(); self.bind()
        self.churn_pod_on_read(lambda pod: pod["metadata"].update(uid="replacement"))
        with self.assertRaises(Held): self.tick()
        self.assertFalse(any(m == "DELETE" for m, _, _ in self.c.sent))

    def test_replaced_binding_pod_is_never_deleted(self):
        self.tick(); self.tick(); self.bind()
        self.c.objects[self.path("pods")]["metadata"]["uid"] = "replacement"
        with self.assertRaises(Held): self.tick()
        self.assertFalse(any(m == "DELETE" for m, _, _ in self.c.sent))

    def test_missing_node_boot_identity_and_incompatible_class_are_blockers(self):
        self.f.nodes[0]["status"]["nodeInfo"].pop("bootID")
        with self.assertRaises(Held): self.review()
        self.setUp(); self.sc.update(provisioner="driver.longhorn.io", parameters={"migratable": "true"})
        with self.assertRaisesRegex(Held, "filesystem storage class"): self.review()

    def test_settings_status_does_not_expose_other_namespaces_or_accept_old_source(self):
        item = {**self.item, "id": "prepared", "kind": P.KIND, "status": "succeeded"}
        item["ref"]["prepared"] = {"claim_uid": "kept"}
        foreign = copy.deepcopy(item); foreign["ref"]["namespace"] = "foreign"
        with mock.patch.object(server.SELF, "NS", "lab"), mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=self.f.read), \
             mock.patch.object(server.SELF_DATA_ROUTES, "homestead_data_volume", return_value={"pvc": "different"}), \
             mock.patch.object(server.OPS, "stored", return_value=[item, foreign]):
            state = server.SELF_DATA_ROUTES.self_data_preparation_state()
        self.assertEqual(1, len(state["preparations"]))
        self.assertFalse(state["preparations"][0]["prepared"])
        self.assertTrue(state["execution_ready"])

    def test_status_error_is_safe_and_never_mutates(self):
        with mock.patch.object(server.SELF_DATA_ROUTES, "homestead_data_volume", side_effect=RuntimeError("private-secret")), \
             mock.patch.object(server.KUBE_HELPERS, "ksend") as send:
            with self.assertRaises(Held) as error: server.SELF_DATA_ROUTES.self_data_preparation_state()
            self.assertNotIn("private-secret", str(error.exception)); send.assert_not_called()


class MoveTests(unittest.TestCase):
    """The move itself, after preparation: the coordinator's phases."""
    write_own_deployment = PreparationTests.write_own_deployment
    scan_own_image = PreparationTests.scan_own_image

    def setUp(self):
        self.c = coordinator_fixture.Cluster()
        self.scans = 0

    def step(self):
        """One coordinator step, after an image scan. The scan writes whenever
        Homestead is meant to run and reports a digest it has not recorded
        (before the stop and once restarted); while it is stopped the real
        check writes nothing. A new digest each time makes every scan that
        can write do so, as a restart onto a rebuilt image would."""
        self.scans += 1
        dep_path = "/apis/apps/v1/namespaces/lab/deployments/homestead"
        before = self.c.objects[dep_path]["metadata"]["resourceVersion"]
        self.scan_own_image(IMAGE.split("@")[0] + "@sha256:" + format(self.scans, "064x"))
        dep = self.c.objects[dep_path]
        if dep["metadata"]["resourceVersion"] != before:
            # The Deployment controller copies the app's annotations onto its ReplicaSet.
            rs = self.c.objects[self.c.rs_path]
            rs["metadata"].setdefault("annotations", {}).update(
                {k: v for k, v in dep["metadata"]["annotations"].items() if k in (CHANGES.AUTHOR, UPDATES.RAN)})
            rs["metadata"]["resourceVersion"] = str(int(rs["metadata"]["resourceVersion"]) + 1)
            self.marks.append(dep["metadata"]["annotations"][CHANGES.AUTHOR])
        return self.c.step()["phase"]

    def test_image_scans_through_the_write_guard_do_not_stop_a_move(self):
        # #680 marks every Deployment write with homestead.io/changedBy. A scan
        # landing during the move read as an edit to Homestead and held the
        # move ("specification or metadata changed"); it must reach done.
        c, self.marks = self.c, []
        phases = [self.step(), self.step()]
        c.settle_stop(); phases += [self.step(), self.step()]
        c.finish_copy(); phases += [self.step(), self.step(), self.step()]
        del c.objects["/api/v1/namespaces/lab/pods/copy-pod"]
        phases += [self.step(), self.step()]
        c.settle_stop(); phases += [self.step(), self.step()]
        c.settle_start(); phases.append(self.step())
        self.assertEqual(["quiesce", "quiesce", "copy", "copy", "verify", "verify", "verify",
                          "switch", "start", "start", "start", "done"], phases)
        self.assertGreaterEqual(len(self.marks), 3, "scans before the stop and after the restart wrote, marked")


if __name__ == "__main__": unittest.main()
