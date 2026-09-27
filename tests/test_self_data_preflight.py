"""Admission previews must never turn into a real create or an identity receipt."""
import copy
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_copy as C
import homestead_self_data_kube as K
import homestead_self_data_launch as L
import homestead_self_data_preflight as P
from homestead_storage_journal import Held, digest
from test_self_data_copy import IMAGE, OP


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.scope = K.Scope("lab", "homestead", OP, ["source", "target"], ["pv-old", "pv-new"], ["node1"])
        self.preview = K.PreviewScope(self.scope)
        self.pod = L.resources(self.scope, anchor_uid="anchor-uid", image=IMAGE, node="node1", status_digest="d" * 64)[-2]
        self.job = C.job("lab", self.scope.copy_name, "source", "target", IMAGE, OP, "node1")
        self.calls = []
        self.mutate = lambda obj: None
        self.now = 1000
        self.check = P.Preflight(self.scope, self.send, clock=lambda: self.now)

    def send(self, method, path, body):
        self.preview.check(method, path, body)
        self.calls.append((method, path, copy.deepcopy(body)))
        result = json.loads(json.dumps(body))  # API serialization breaks shared Python references.
        self.mutate(result)
        return result

    def test_copy_checks_both_job_and_pod_without_persisted_identity(self):
        receipt = self.check.copy(self.job)
        self.assertEqual(["Job", "Pod"], [b["kind"] for _, _, b in self.calls])
        self.assertTrue(all(p.endswith("?dryRun=All&fieldValidation=Strict") for _, p, _ in self.calls))
        self.assertEqual(digest(self.job), receipt["request"])
        self.assertEqual(digest(self.calls[1][2]), receipt["pod_request"])
        self.assertEqual(1000, receipt["checked_at"])
        self.assertEqual(P.LIMITATION, receipt["limitation"])
        self.assertEqual({"checked_at", "request", "pod_request", "admitted", "limitation"}, set(receipt))
        self.assertFalse(self.calls[1][2]["spec"]["enableServiceLinks"])

    def test_worker_preview_does_not_need_or_retain_generated_identity(self):
        first = self.check.worker(self.pod)
        self.mutate = lambda obj: obj["metadata"].update(uid="dry-run-only", resourceVersion="999", creationTimestamp="now")
        self.assertEqual(first, self.check.worker(self.pod))
        self.assertNotIn("dry-run-only", json.dumps(first))

    def test_normal_job_selector_and_pod_defaults_are_accepted(self):
        def defaults(obj):
            if obj["kind"] == "Job":
                obj["metadata"]["uid"] = "simulated-uid"
                labels = {"controller-uid": "simulated-uid", "batch.kubernetes.io/controller-uid": "simulated-uid",
                          "job-name": self.scope.copy_name, "batch.kubernetes.io/job-name": self.scope.copy_name}
                obj["spec"]["template"]["metadata"]["labels"].update(labels)
                obj["spec"].update(selector={"matchLabels": {"batch.kubernetes.io/controller-uid": "simulated-uid"}},
                                   manualSelector=False, completionMode="NonIndexed", suspend=False)
                spec = obj["spec"]["template"]["spec"]
            else:
                spec = obj["spec"]
            spec.update(serviceAccountName="default", serviceAccount="default", terminationGracePeriodSeconds=30,
                        schedulerName="default-scheduler", dnsPolicy="ClusterFirst")
            spec["containers"][0].update(imagePullPolicy="IfNotPresent", terminationMessagePath="/dev/termination-log", terminationMessagePolicy="File")
        self.mutate = defaults
        self.check.copy(self.job)
        # The standalone Pod is checked from the reviewed template, not a
        # controller UID that exists only in a dry-run response.
        self.assertNotIn("controller-uid", self.calls[1][2]["metadata"]["labels"])

    def test_job_success_followed_by_pod_policy_denial_never_falls_back(self):
        def policy(obj):
            if obj["kind"] == "Pod":
                raise urllib.error.HTTPError("private", 403, "private policy body", {}, None)
        self.mutate = policy
        with self.assertRaisesRegex(Held, "Pod.*HTTP 403") as caught:
            self.check.copy(self.job)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(2, len(self.calls))

    def test_job_replacement_policy_must_survive_strict_admission(self):
        for change in (lambda s: s.pop("podReplacementPolicy"), lambda s: s.update(podReplacementPolicy="TerminatingOrFailed"),
                       lambda s: s.update(suspend=True), lambda s: s.update(ttlSecondsAfterFinished=0),
                       lambda s: s.update(manualSelector=True), lambda s: s.update(completionMode="Indexed")):
            with self.subTest(change=change):
                self.calls.clear()
                self.mutate = lambda obj: change(obj["spec"])
                with self.assertRaises(Held): self.check.copy(self.job)
                self.assertEqual(1, len(self.calls))

    def test_injected_job_template_is_rejected_before_pod_preview(self):
        changes = [lambda s: s["containers"].append({"name": "sidecar"}),
                   lambda s: s.update(initContainers=[{"name": "init"}]),
                   lambda s: s["volumes"].append({"name": "host", "hostPath": {"path": "/"}}),
                   lambda s: s["containers"][0]["resources"]["requests"].update(memory="4Gi"),
                   lambda s: s["containers"][0]["securityContext"].update(privileged=True),
                   lambda s: s.update(nodeName="other"),
                   lambda s: s.update(serviceAccountName="privileged-account")]
        for change in changes:
            self.calls.clear()
            self.mutate = lambda obj: change(obj["spec"]["template"]["spec"])
            with self.assertRaises(Held): self.check.copy(self.job)
            self.assertEqual(1, len(self.calls))

    def test_pod_only_mutations_are_not_hidden_by_clean_job_admission(self):
        def inject(obj):
            if obj["kind"] == "Pod": obj["spec"]["containers"][0]["volumeMounts"].append({"name": "source", "mountPath": "/extra"})
        self.mutate = inject
        with self.assertRaises(Held): self.check.copy(self.job)
        self.assertEqual(2, len(self.calls))

    def test_unknown_labels_annotations_and_selector_are_rejected(self):
        for change in (lambda o: o["metadata"].update(annotations={"inject": "yes"}),
                       lambda o: o["spec"]["template"]["metadata"]["labels"].update(app="homestead"),
                       lambda o: o["spec"].update(selector={"matchLabels": {"app": "homestead"}}),
                       lambda o: o["spec"]["template"]["metadata"].update(annotations={"inject": "yes"})):
            self.mutate = change
            with self.assertRaises(Held): self.check.copy(self.job)

    def test_malformed_responses_hold_without_leaking_details(self):
        for value in (None, [], {}, {"metadata": "private"}, {"metadata": {"name": "private"}}):
            for action, body in (("worker", self.pod), ("copy", self.job)):
                preview = P.Preflight(self.scope, lambda *_: value)
                with self.assertRaises(Held) as caught: getattr(preview, action)(body)
                self.assertNotIn("private", str(caught.exception))

    def test_failure_never_retries_or_drops_strict_dry_run_flags(self):
        for error in (TimeoutError("private"), urllib.error.HTTPError("private", 422, "unknown field private", {}, None)):
            send = mock.Mock(side_effect=error)
            with self.assertRaises(Held) as caught: P.Preflight(self.scope, send).copy(self.job)
            self.assertNotIn("private", str(caught.exception))
            send.assert_called_once()
            self.assertTrue(send.call_args.args[1].endswith("?dryRun=All&fieldValidation=Strict"))

    def test_slow_or_reversed_clock_rejects_receipt(self):
        for change in (61, -1):
            self.mutate = lambda _: setattr(self, "now", self.now + change)
            with self.assertRaisesRegex(Held, "fresh check"): self.check.worker(self.pod)

    def test_scope_rejects_missing_dry_run_or_strict_flag_and_all_other_actions(self):
        path = "/api/v1/namespaces/lab/pods?dryRun=All&fieldValidation=Strict"
        for bad in (path.split("?")[0], path.replace("&fieldValidation=Strict", ""), path.replace("dryRun=All", "dryRun="),
                    path + "&other=1", path.replace("lab", "other"), "https://elsewhere/" + path):
            with self.assertRaises(Held): self.preview.check("POST", bad, self.pod)
        for method in ("GET", "PUT", "DELETE", "PATCH"):
            with self.assertRaises(Held): self.preview.check(method, path, self.pod)
        for body in (None, [], {}, {**self.pod, "metadata": None}):
            with self.assertRaises(Held): self.preview.check("POST", path, body)
        with self.assertRaises(Held): self.preview.check("POST", path, self.pod, logs=True)

    def test_scope_refuses_identity_or_owner_adoption_and_wrong_resource(self):
        path = "/api/v1/namespaces/lab/pods?dryRun=All&fieldValidation=Strict"
        for field, value in (("uid", "old"), ("resourceVersion", "1"), ("ownerReferences", []), ("generateName", "prefix"), ("name", "other")):
            pod = copy.deepcopy(self.pod); pod["metadata"][field] = value
            with self.assertRaises(Held): self.preview.check("POST", path, pod)
        with self.assertRaises(Held): self.preview.check("POST", path, self.job)

    def test_previews_do_not_expand_worker_pod_permissions(self):
        for resource in K.access_resources(self.scope):
            for rule in resource.get("rules", []):
                if "pods" in rule.get("resources", []):
                    self.assertNotIn("create", rule["verbs"])
        with self.assertRaises(Held): self.scope.check("POST", "/api/v1/namespaces/lab/pods?dryRun=All&fieldValidation=Strict", self.pod)

    def test_transport_checks_preview_scope_before_opening_token(self):
        client = object.__new__(K.Client); client.scope = self.preview
        with mock.patch("builtins.open", side_effect=AssertionError("must not read token")):
            with self.assertRaises(Held): client.send("POST", "/api/v1/namespaces/lab/pods", self.pod)

    def test_receipt_is_bound_to_exact_job_pod_policy_and_timestamp(self):
        receipt = self.check.copy(self.job)
        P.validate_receipt(receipt, self.job, now=1000)
        for field, value in (("request", "e" * 64), ("pod_request", "e" * 64), ("admitted", "bad"),
                             ("checked_at", True), ("checked_at", -1), ("limitation", "guaranteed safe")):
            with self.subTest(field=field), self.assertRaises(Held):
                P.validate_receipt({**receipt, field: value}, self.job, now=1000)
        for age in (-1, 61):
            with self.assertRaises(Held): P.validate_receipt(receipt, self.job, now=1000 + age)
        changed = copy.deepcopy(self.job); changed["spec"]["template"]["spec"]["containers"][0]["image"] = "changed"
        with self.assertRaises(Held): P.validate_receipt(receipt, changed)

    def prepared_cluster(self, checked_at):
        from test_self_data_coordinator import Cluster
        import homestead_self_data_anchor as A
        cluster = Cluster(published=False)
        prior = copy.deepcopy(cluster.anchor.state)
        cluster.objects.pop(cluster.anchor.path)
        cluster.anchor = cluster.fresh()
        cluster.handle = cluster.anchor.create(operation=OP, deployment=prior["deployment"], source=prior["source"],
                                               destination=prior["destination"], replicas=prior["replicas"])
        self.now = checked_at
        prior["plan"]["copy_preflight"] = self.check.copy(P.copy_job("lab", prior))
        cluster.anchor.configure(prior["plan"])
        cluster.anchor.pointer_published(A.pointer_digest("lab", cluster.anchor.state, cluster.handle["uid"]))
        return cluster

    def test_expired_preview_blocks_before_any_downtime(self):
        import homestead_self_data_coordinator as Coordinator
        cluster = self.prepared_cluster(939)
        engine = Coordinator.Coordinator(cluster.anchor, cluster.read, cluster.send, lambda _: "", cluster.admit,
                                          worker_uid="coordinator-uid", clock=lambda: 1000)
        before = len(cluster.sent)
        with self.assertRaisesRegex(Held, "expired before Homestead stopped"): engine.step()
        self.assertEqual(before, len(cluster.sent))
        self.assertEqual(2, cluster.objects[cluster.dep_path]["spec"]["replicas"])

    def test_expiry_is_checked_after_slow_capacity_recheck(self):
        import homestead_self_data_coordinator as Coordinator
        cluster = self.prepared_cluster(950)
        self.now = 1000
        def admit(*_):
            self.now += 11
            return True
        engine = Coordinator.Coordinator(cluster.anchor, cluster.read, cluster.send, lambda _: "", admit,
                                          worker_uid="coordinator-uid", clock=lambda: self.now)
        with self.assertRaisesRegex(Held, "expired"): engine.step()
        self.assertEqual(2, cluster.objects[cluster.dep_path]["spec"]["replicas"])

    def test_expiry_does_not_replay_an_already_accepted_stop(self):
        import homestead_self_data_coordinator as Coordinator
        cluster = self.prepared_cluster(950)
        engine = Coordinator.Coordinator(cluster.anchor, cluster.read, cluster.send, lambda _: "", cluster.admit,
                                          worker_uid="coordinator-uid", clock=lambda: 1000)
        with mock.patch.object(cluster.anchor, "advance", side_effect=Held("simulated process interruption")):
            with self.assertRaises(Held): engine.step()
        self.assertEqual(0, cluster.objects[cluster.dep_path]["spec"]["replicas"])
        anchor = cluster.fresh().load(**cluster.handle)
        engine = Coordinator.Coordinator(anchor, cluster.read, cluster.send, lambda _: "", cluster.admit,
                                          worker_uid="coordinator-uid", clock=lambda: 1011)
        self.assertEqual("quiesce", engine.step()["phase"])
        self.assertEqual(1, len([1 for method, path, _ in cluster.sent if method == "PUT" and path == cluster.dep_path]))


if __name__ == "__main__":
    unittest.main()
