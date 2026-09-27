import copy
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_bootstrap as B
import homestead_self_data_kube as K
from homestead_storage_journal import Held, shape, identity
from test_self_data_anchor import Cluster, OP
from test_self_data_coordinator import IMAGE


def with_preview(send):
    def request(method, path, body, **kwargs):
        if method == "POST" and path.endswith("?dryRun=All&fieldValidation=Strict"):
            return copy.deepcopy(body)  # Simulated admission never persists.
        return send(method, path, body, **kwargs)
    return request


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster()
        self.c.send = with_preview(self.c.send)
        self.a = A.Anchor(self.c.read, self.c.send, "lab", "homestead")
        self.handle = self.a.create(operation=OP, deployment={"name": "homestead", "uid": "dep-1", "resourceVersion": "1"},
            source={"name": "data", "uid": "pvc-old", "resourceVersion": "1"}, destination="data-new", replicas=2)
        self.scope = K.Scope("lab", "homestead", OP, ["data", "data-new"], ["old-pv", "new-pv"], ["node1"])
        self.setup = self.fresh()

    def fresh(self, **kwargs):
        anchor = A.Anchor(self.c.read, self.c.send, "lab", "homestead").load(**self.handle)
        return B.Setup(anchor, self.scope, image=IMAGE, node="node1", status_digest=kwargs.pop("status_digest", "b" * 64),
                       admit=kwargs.pop("admit", lambda _: True), **kwargs)

    def saved(self):
        return A.Anchor(self.c.read, self.c.send, "lab", "homestead").load(**self.handle)

    def all_created(self):
        self.setup.step()
        for _ in range(9): self.setup.step()
        return self.setup.step()

    def pod_ready(self):
        path = self.setup.resources[-2]["target"]["path"]
        pod = self.c.objects[path]
        pod["spec"]["nodeName"] = "node1"
        pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "coordinator", "ready": True, "restartCount": 0,
                "containerID": "containerd://worker", "imageID": IMAGE, "state": {"running": {"startedAt": "now"}}}]}
        return pod

    def test_every_post_is_preceded_by_a_durable_intent_and_only_one_resource_per_step(self):
        self.assertEqual({"created": 0, "total": 9, "complete": False}, self.setup.step())
        original = self.setup.send
        observed = []
        def send(method, path, body):
            if method == "POST" and "?" not in path:
                state = self.saved().state["setup"]
                self.assertEqual({"state": "intent"}, state["receipts"][-1])
                self.assertEqual(state["resources"][len(state["receipts"]) - 1]["target"]["path"].rsplit("/", 1)[0], path)
                observed.append(path)
            return original(method, path, body)
        self.setup.send = send
        for index in range(9):
            result = self.setup.step()
            self.assertEqual(index + 1, len(observed))
            self.assertEqual(index == 8, result["complete"])
        self.assertTrue(B.complete(self.saved().state["setup"]))
        self.assertTrue(all(method != "DELETE" for method, _, _ in self.c.sent))

    def test_accepted_setup_survives_process_restart_without_recreating_any_resource(self):
        self.setup.step()
        for _ in range(9): self.fresh().step()
        before = len(self.c.sent)
        self.assertTrue(self.fresh().step()["complete"])
        self.assertEqual(before, len(self.c.sent))

    def test_persisted_setup_contains_only_targets_hashes_identities_and_states(self):
        self.all_created()
        encoded = json.dumps(self.saved().state["setup"])
        for private in ("containers", "rules", "subjects", "HOMESTEAD_HANDOFF_CONFIG", IMAGE):
            self.assertNotIn(private, encoded)

    def test_existing_name_is_not_adopted_even_if_its_body_matches(self):
        self.setup.step()
        target, body = self.setup.resources[0]["target"], self.setup.bodies[0]
        self.c.send("POST", target["path"].rsplit("/", 1)[0], body)
        before = len(self.c.sent)
        with self.assertRaisesRegex(Held, "already in use"): self.setup.step()
        self.assertEqual(before, len(self.c.sent))
        self.assertEqual([], self.saved().state["setup"]["receipts"])

    def test_lost_creation_response_for_each_resource_never_replays_or_adopts(self):
        for index in range(9):
            with self.subTest(resource=index):
                self.setUp(); self.setup.step()
                for _ in range(index): self.setup.step()
                self.c.lose_at = len(self.c.sent) + 2  # intent checkpoint, then POST
                with self.assertRaises(Held): self.setup.step()
                self.assertEqual("uncertain", self.saved().state["setup"]["receipts"][-1]["state"])
                before = len(self.c.sent)
                with self.assertRaises(Held): self.fresh().step()
                self.assertEqual(before, len(self.c.sent))

    def test_lost_intent_checkpoint_never_sends_resource_creation(self):
        self.setup.step()
        self.c.lose_at = len(self.c.sent) + 1
        with self.assertRaises(Held): self.setup.step()
        self.assertNotIn(self.setup.resources[0]["target"]["path"], self.c.objects)
        self.assertEqual("intent", self.saved().state["setup"]["receipts"][-1]["state"])
        with self.assertRaises(Held): self.fresh().step()

    def test_lost_completion_checkpoint_can_only_observe_the_verified_saved_receipt(self):
        self.setup.step()
        self.c.lose_at = len(self.c.sent) + 3
        with self.assertRaises(Held): self.setup.step()
        self.assertEqual("accepted", self.saved().state["setup"]["receipts"][-1]["state"])
        self.c.lose_at = None
        self.fresh().step()
        posts = [(p, b["metadata"]["name"]) for m, p, b in self.c.sent if m == "POST"]
        self.assertEqual(len(posts), len(set(posts)))

    def test_lost_completion_checkpoint_before_apply_leaves_intent_not_a_retry(self):
        self.setup.step()
        self.c.reject_at = len(self.c.sent) + 3
        with self.assertRaises(Held): self.setup.step()
        self.assertEqual("intent", self.saved().state["setup"]["receipts"][-1]["state"])
        with self.assertRaises(Held): self.fresh().step()

    def test_missing_permissions_hold_without_mutating_roles_or_exposing_errors(self):
        self.setup.step()
        def refuse(*_): raise urllib.error.HTTPError("private", 403, "credential", {}, None)
        self.setup.send = refuse
        with self.assertRaises(Held) as caught: self.setup.step()
        self.assertNotIn("credential", str(caught.exception))
        self.assertEqual("refused", self.saved().state["setup"]["receipts"][-1]["state"])
        self.assertFalse(any(m == "POST" and "/roles" in p for m, p, _ in self.c.sent))

    def test_changed_review_or_competing_setup_cannot_send(self):
        competitor = self.fresh()
        self.setup.step()
        before = len(self.c.sent)
        with self.assertRaises(Held): competitor.step()
        with self.assertRaises(Held): self.fresh(status_digest="c" * 64).step()
        self.assertEqual(before, len(self.c.sent))

    def test_mutated_manifest_cannot_be_sent_under_an_old_fingerprint(self):
        self.setup.step()
        self.setup.bodies[0]["automountServiceAccountToken"] = True
        before = len(self.c.sent)
        with self.assertRaisesRegex(Held, "manifest changed"): self.setup.step()
        self.assertEqual(before, len(self.c.sent))

    def test_changed_or_replaced_created_resource_stops_following_creations(self):
        for replacement in (True, False):
            self.setUp(); self.setup.step(); self.setup.step()
            obj = self.c.objects[self.setup.resources[0]["target"]["path"]]
            if replacement: obj["metadata"]["uid"] = "replacement"
            else: obj["automountServiceAccountToken"] = True
            before = len(self.c.sent)
            with self.assertRaises(Held): self.fresh().step()
            self.assertEqual(before, len(self.c.sent))

    def test_privilege_injection_in_creation_response_is_unverified(self):
        self.setup.step(); self.setup.step()  # next is Role
        original = self.setup.send
        def altered(method, path, body):
            result = original(method, path, body)
            result["rules"].append({"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]})
            return result
        self.setup.send = altered
        with self.assertRaises(Held): self.setup.step()
        self.assertEqual("unverified", self.saved().state["setup"]["receipts"][-1]["state"])
        with self.assertRaises(Held): self.fresh().step()

    def test_injected_sidecars_data_mounts_or_host_access_are_rejected(self):
        body, target = self.setup.bodies[-2], self.setup.resources[-2]["target"]
        for change in (lambda p: p["spec"]["containers"].append({"name": "sidecar"}),
                       lambda p: p["metadata"]["labels"].update(app="homestead"),
                       lambda p: p["spec"].update(hostNetwork=True),
                       lambda p: p["spec"]["volumes"].append({"name": "data", "persistentVolumeClaim": {"claimName": "data"}}),
                       lambda p: p["spec"]["containers"][0]["securityContext"].update(privileged=True)):
            pod = copy.deepcopy(body); pod["metadata"].update(uid="worker-uid", resourceVersion="1"); change(pod)
            with self.assertRaises(Held): B.admitted(body, pod, target)

    def test_normal_api_defaults_and_scheduling_are_accepted_but_wrong_node_is_not(self):
        self.all_created()
        pod = self.pod_ready()
        self.assertNotEqual(shape(self.setup.bodies[-2]), shape(pod))
        fact = self.setup.worker_fact()
        self.assertEqual(shape(pod), fact["shape"])
        self.assertEqual(pod["metadata"]["uid"], fact["uid"])
        body = self.setup.bodies[-2]
        defaulted = copy.deepcopy(body); defaulted["metadata"].update(uid="pod-uid", resourceVersion="1")
        defaulted["spec"].update(dnsPolicy="ClusterFirst", schedulerName="default-scheduler", priority=0,
            serviceAccount=body["spec"]["serviceAccountName"], preemptionPolicy="PreemptLowerPriority",
            tolerations=[{"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}])
        defaulted["spec"]["containers"][0].update(terminationMessagePath="/dev/termination-log", terminationMessagePolicy="File")
        B.admitted(body, defaulted, self.setup.resources[-2]["target"])
        pod["spec"]["nodeName"] = "other"
        with self.assertRaises(Held): self.setup.worker_fact()

    def test_pending_unready_or_restarted_worker_cannot_be_pinned(self):
        self.all_created()
        with self.assertRaisesRegex(Held, "Waiting"): self.setup.worker_fact()
        pod = self.pod_ready()
        pod["status"]["containerStatuses"][0]["restartCount"] = 1
        with self.assertRaises(Held): self.setup.worker_fact()
        pod["status"]["containerStatuses"][0]["restartCount"] = 0
        pod["status"]["conditions"][0]["status"] = "False"
        with self.assertRaises(Held): self.setup.worker_fact()

    def test_unfinished_setup_cannot_configure_publish_or_advance(self):
        self.setup.step()
        for action in (lambda: self.setup.anchor.configure({}), lambda: self.setup.anchor.advance("quiesce")):
            with self.assertRaisesRegex(Held, "incomplete helper setup"): action()
        self.assertEqual("prepare", self.saved().state["phase"])

    def test_anchor_receipts_are_append_only_and_target_scope_is_fixed(self):
        self.setup.step(); self.setup.step()
        anchor = self.setup.anchor
        with self.assertRaises(Held): anchor.checkpoint_setup([])
        with self.assertRaises(Held): anchor.prepare_setup(self.setup.resources)
        state = copy.deepcopy(anchor.state)
        state["setup"]["resources"][0]["target"]["path"] = "/api/v1/namespaces/lab/secrets/password"
        with self.assertRaises(Held): A._validate(state, "lab")

    def test_worker_creation_requires_fresh_explicit_admission(self):
        self.setup.step()
        for _ in range(7): self.setup.step()
        before = len(self.c.sent)
        for admit in (None, lambda _: False, lambda _: {"approved": True}):
            with self.assertRaisesRegex(Held, "capacity and placement"): self.fresh(admit=admit).step()
        self.assertEqual(before, len(self.c.sent))
        self.assertNotIn(self.setup.resources[-2]["target"]["path"], self.c.objects)

    def test_pod_policy_rejection_leaves_no_create_intent_or_live_pod(self):
        self.setup.step()
        for _ in range(7): self.setup.step()
        before = copy.deepcopy(self.saved().state)
        original = self.setup.send
        previews = []
        def send(method, path, body):
            if "?dryRun=All" in path:
                previews.append(path)
                raise urllib.error.HTTPError("private", 403, "private policy", {}, None)
            return original(method, path, body)
        self.setup.send = send
        with self.assertRaisesRegex(Held, "Pod admission preflight"):
            self.setup.step()
        self.assertEqual(1, len(previews))
        self.assertEqual(before, self.saved().state)
        self.assertNotIn(self.setup.resources[-2]["target"]["path"], self.c.objects)

    def test_prior_permissions_are_rechecked_after_pod_preview(self):
        self.setup.step()
        for _ in range(7): self.setup.step()
        original = self.setup.send
        def send(method, path, body):
            result = original(method, path, body)
            if "?dryRun=All" in path:
                self.c.objects[self.setup.resources[1]["target"]["path"]]["rules"].append(
                    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]})
            return result
        self.setup.send = send
        with self.assertRaises(Held): self.setup.step()
        self.assertEqual(7, len(self.saved().state["setup"]["receipts"]))
        self.assertNotIn(self.setup.resources[-2]["target"]["path"], self.c.objects)

    def test_admission_cannot_mutate_the_manifest_or_hide_changed_permissions(self):
        self.setup.step()
        for _ in range(7): self.setup.step()
        def admit(body):
            body["spec"]["containers"][0]["image"] = "evil:latest"
            role = self.c.objects[self.setup.resources[1]["target"]["path"]]
            role["rules"].append({"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]})
            return True
        with self.assertRaises(Held): self.fresh(admit=admit).step()
        self.assertNotIn(self.setup.resources[-2]["target"]["path"], self.c.objects)
        self.assertEqual(IMAGE, self.setup.bodies[-2]["spec"]["containers"][0]["image"])

    def test_read_errors_are_safe_and_do_not_send_a_creation(self):
        def unreadable(_): raise OSError("private upstream diagnostic")
        self.setup.read = unreadable
        before = len(self.c.sent)
        with self.assertRaises(Held) as caught: self.setup.step()
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(before, len(self.c.sent))

    def test_created_worker_identity_and_receipts_feed_a_complete_simulated_handoff(self):
        from test_self_data_admission import AdmissionTests
        import homestead_self_data_worker as W
        fixture = AdmissionTests(); fixture.setUp()
        c = fixture.cluster
        prior = copy.deepcopy(c.anchor.state)
        op = prior["operation"]
        c.objects.pop(c.anchor.path)
        anchor = c.fresh()
        handle = anchor.create(operation=op, deployment=prior["deployment"], source=prior["source"], destination="target", replicas=2)
        scope = K.Scope("lab", "homestead", op, ["source", "target"], ["old-pv", "new-pv"], ["node1"])
        def parent_read(path):
            if path == "/api/v1/pods": return {"items": [copy.deepcopy(v) for v in c.objects.values() if v.get("kind") == "Pod"]}
            return fixture.read(path)
        anchor.read = parent_read
        anchor.send = with_preview(anchor.send)
        import homestead_self_data_admission as D
        preview_pod = B.L.resources(scope, anchor_uid="pending", image=IMAGE, node="node1", status_digest="b" * 64)[-2]
        report = D.review(parent_read, "lab", "worker", preview_pod, fixture.pin, 88, clock=lambda: 1000)
        approval = {"threshold": 88, "nodes": fixture.pin, "receipt": report["receipt"]}
        setup = B.Setup(anchor, scope, image=IMAGE, node="node1", status_digest="b" * 64, approval=approval, clock=lambda: 1000)
        setup.step()
        for _ in range(9): setup.step()
        pod = c.objects[setup.resources[-2]["target"]["path"]]
        pod["spec"]["nodeName"] = "node1"
        pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "coordinator", "ready": True, "restartCount": 0, "containerID": "containerd://worker",
                "imageID": IMAGE, "state": {"running": {"startedAt": "now"}}}]}
        plan = prior["plan"]
        plan["worker"] = setup.worker_fact()
        fixture.dep = copy.deepcopy(c.dep)
        fixture.dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "target"
        plan["admission"] = fixture.policy()
        bad = copy.deepcopy(plan); bad["worker"]["uid"] = "wrong-worker"
        with self.assertRaisesRegex(Held, "not the helper created"): anchor.configure(bad)
        bad = copy.deepcopy(plan); bad["nodes"][0]["boot_id"] = "new-boot"
        with self.assertRaisesRegex(Held, "host identities differ"): anchor.configure(bad)
        with self.assertRaisesRegex(Held, "need admission preflight"): anchor.configure(plan)
        plan["copy_preflight"] = setup.preflight_copy(plan)
        anchor.configure(plan)
        anchor.pointer_published(A.pointer_digest("lab", anchor.state, handle["uid"]))
        def read(path):
            scope.check("GET", path)
            return parent_read(path)
        runner = W.Runner(read, c.send, lambda _: c.logs_text, namespace="lab", deployment="homestead",
            operation=op, anchor_uid=handle["uid"], worker_uid=identity(pod)["uid"], clock=lambda: 1000, require_setup_receipts=True)
        self.assertEqual("quiesce", runner.tick()["phase"])
        c.settle_stop(); runner.tick(); runner.tick(); c.finish_copy(); runner.tick(); runner.tick()
        c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        runner.tick(); runner.tick(); c.settle_stop(); runner.tick(); c.settle_start()
        self.assertEqual("done", runner.tick()["status"])
        state = c.fresh().load(**handle).state
        self.assertTrue(B.complete(state["setup"]))
        self.assertTrue(all(r["state"] == "accepted" for r in state["journal"]["ref"]["storage_writes"]))

    def test_launched_worker_refuses_legacy_plan_without_creation_receipts(self):
        from test_self_data_coordinator import Cluster as MoveCluster
        import homestead_self_data_worker as W
        cluster = MoveCluster()
        runner = W.Runner(cluster.read, cluster.send, lambda _: cluster.logs_text, cluster.admit,
            namespace="lab", deployment="homestead", operation=cluster.handle["operation"],
            anchor_uid=cluster.handle["uid"], worker_uid="coordinator-uid", clock=lambda: 1000, require_setup_receipts=True)
        self.assertEqual("held", runner.tick()["status"])
        self.assertEqual(2, cluster.objects[cluster.dep_path]["spec"]["replicas"])
        self.assertEqual([], cluster.admissions)


if __name__ == "__main__":
    unittest.main()
