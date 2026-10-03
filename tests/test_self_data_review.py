import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_capacity_review as SIGN
import homestead_self_data_admission as D
import homestead_self_data_review as R
from homestead_storage_journal import Held
import test_self_data_admission as admission_fixture
from test_self_data_coordinator import OP, IMAGE, obj


class ReviewTests(unittest.TestCase):
    def test_replicaset_observational_annotations_do_not_change_review(self):
        rs = {"apiVersion": "apps/v1", "kind": "ReplicaSet", "metadata": {"name": "app", "uid": "rs", "resourceVersion": "1"}, "spec": {"replicas": 1}}
        before = R._controller_fact(rs)
        rs.pop("apiVersion"); rs.pop("kind")
        rs["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "1", "homestead.io/ran-digests": "observed"}
        self.assertEqual(before, R._controller_fact(rs))
        rs["metadata"]["annotations"]["user-setting"] = "changed"
        self.assertNotEqual(before, R._controller_fact(rs))
        rs["metadata"]["annotations"].pop("user-setting")
        rs["spec"]["replicas"] = 2
        self.assertNotEqual(before, R._controller_fact(rs))
        rs["kind"] = "Deployment"
        with self.assertRaises(Held): R._controller_fact(rs)

    def test_final_recheck_allows_resolved_warnings_but_not_new_risks(self):
        receipt = {"proposal": "work", "warnings": ["missing-metrics"]}
        original = {"deployment": {"uid": "same"}, "approvals": {
            "worker": {"threshold": 88, "nodes": [], "receipt": copy.deepcopy(receipt)},
            "policy": {"threshold": 88, "reviews": {s: copy.deepcopy(receipt) for s in ("copy", "restart")}}}}
        current = copy.deepcopy(original)
        current["approvals"]["worker"]["receipt"]["warnings"] = []
        current["approvals"]["policy"]["reviews"]["copy"]["warnings"] = []
        self.assertTrue(R.recheck_binding(original, current))
        for mutate in (lambda c: c["deployment"].update(uid="replacement"),
                       lambda c: c["approvals"]["policy"]["reviews"]["copy"].update(proposal="other"),
                       lambda c: c["approvals"]["policy"]["reviews"]["restart"]["warnings"].append("more-pressure"),
                       lambda c: c["approvals"]["worker"].update(threshold=100)):
            changed = copy.deepcopy(current); mutate(changed)
            with self.assertRaises(Held): R.recheck_binding(original, changed)

    def setUp(self):
        self.f = admission_fixture.AdmissionTests(); self.f.setUp()
        self.c = self.f.cluster
        for name in ("lab", "kube-system"):
            self.c.objects["/api/v1/namespaces/" + name] = obj("Namespace", name, ns=None)
        self.dep = self.c.objects[self.c.dep_path]
        self.dep["status"]["updatedReplicas"] = 2
        container = self.dep["spec"]["template"]["spec"]["containers"][0]
        container["resources"] = {"requests": {"cpu": "100m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}}
        container["env"] = [{"name": "PRIVATE_FIXTURE", "value": "do-not-serialize-private-values"}]
        container["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
        self.f.pods = [copy.deepcopy(self.c.objects["/api/v1/namespaces/lab/pods/old-" + str(i)]) for i in range(2)]
        for pod in self.f.pods:
            pod["spec"]["containers"] = [copy.deepcopy(container)]
            pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
        patch = mock.patch.object(SIGN, "_key", lambda: b"fixture-signing-key"); patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(SIGN.time, "time", return_value=1000); patch.start(); self.addCleanup(patch.stop)
        self.body = {"operation": OP, "destination": "target", "worker_node": "node1", "copy_node": "node1"}
        self.review = R.Review(self.f.read, "lab", "homestead", actor="admin", image=IMAGE, threshold=88, clock=lambda: 1000)

    def approved(self):
        preview = self.review.preview(self.body)
        return {**self.body, "capacity_token": preview["capacity_token"], "confirm_capacity": True, "confirm_move": True}

    def test_preview_is_read_only_and_has_simple_stages_without_private_values(self):
        before = copy.deepcopy(self.c.sent)
        response = self.review.preview(self.body)
        self.assertEqual(before, self.c.sent)
        self.assertEqual(["worker", "copy", "restart"], [s["id"] for s in response["stages"]])
        self.assertEqual([False, True, True], [s["conditional"] for s in response["stages"]])
        encoded = json.dumps(response)
        for private in ("do-not-serialize-private-values", "HOMESTEAD_HANDOFF_CONFIG", "status_token", "receipt"):
            self.assertNotIn(private, encoded)

    def test_signed_confirmation_returns_only_recomputed_execution_facts(self):
        body = self.approved()
        result = self.review.approve(body)
        self.assertEqual("source", result["source"]["metadata"]["name"])
        self.assertEqual("target", result["destination"]["metadata"]["name"])
        D.validate_worker_approval(result["approval"])
        D.validate_policy(result["policy"])
        self.assertEqual(result["policy"], self.review.approve(body)["policy"])

    def test_no_app_probe_cannot_offer_a_move_whose_restart_is_unverifiable(self):
        self.dep["spec"]["template"]["spec"]["containers"][0].pop("readinessProbe")
        with self.assertRaisesRegex(Held, "readiness probe"): self.review.preview(self.body)

    def test_confirmation_cannot_accept_unsigned_browser_receipts(self):
        for body in ({**self.body, "confirm_move": True, "confirm_capacity": True},
                     {**self.approved(), "policy": {"approve": "everything"}},
                     {**self.approved(), "confirm_move": False}, {**self.approved(), "confirm_capacity": False}):
            with self.assertRaises(Held): self.review.approve(body)

    def test_token_is_bound_to_actor_cluster_destination_and_image(self):
        body = self.approved()
        original_actor, original_image = self.review.actor, self.review.image
        for field, value in (("actor", "different-admin"), ("image", IMAGE[:-1] + "c"), ("threshold", 90)):
            old = getattr(self.review, field); setattr(self.review, field, value)
            with self.assertRaises(Held): self.review.approve(body)
            setattr(self.review, field, old)
        self.c.objects["/api/v1/namespaces/kube-system"]["metadata"]["uid"] = "another-cluster"
        with self.assertRaises(Held): self.review.approve(body)
        self.assertEqual(original_actor, self.review.actor); self.assertEqual(original_image, self.review.image)

    def test_resource_changes_and_new_pressure_require_another_confirmation(self):
        for change in (lambda: self.dep["spec"]["template"]["spec"]["containers"][0].update(image=IMAGE[:-1] + "e"),
                       lambda: self.f.pods[0]["metadata"].update(uid="replacement-pod"),
                       lambda: self.f.metrics[0]["usage"].update(memory="7.8Gi"),
                       lambda: self.c.objects["/api/v1/persistentvolumes/new-pv"]["metadata"].update(uid="replacement-volume")):
            with self.subTest(change=change):
                self.setUp()
                body = self.approved(); change()
                with self.assertRaises(Held): self.review.approve(body)

    def test_expired_signature_is_not_accepted(self):
        body = self.approved()
        with mock.patch.object(SIGN.time, "time", return_value=1601):
            with self.assertRaises(Held): self.review.approve(body)

    def test_conditional_inventory_releases_only_owned_pods_and_keeps_worker_and_live_ram(self):
        other = obj("Pod", "unrelated", {"nodeName": "node1", "containers": [{"name": "app", "resources": {"requests": {"memory": "1Gi"}}}]})
        other["status"] = {"phase": "Running"}
        other["metadata"]["deletionTimestamp"] = "now"
        self.f.pods.append(other)
        actual = D.review; snapshots = {}
        def observed(read, ns, purpose, proposed, pins, threshold, **kwargs):
            snapshots[purpose] = (read("/api/v1/pods"), read("/apis/metrics.k8s.io/v1beta1/nodes"))
            return actual(read, ns, purpose, proposed, pins, threshold, **kwargs)
        with mock.patch.object(D, "review", side_effect=observed): self.review.preview(self.body)
        self.assertEqual({"old-0", "old-1", "unrelated"}, {p["metadata"]["name"] for p in snapshots["worker"][0]["items"]})
        for purpose in ("copy", "restart"):
            pods, metrics = snapshots[purpose]
            self.assertEqual(2, len(pods["items"]))
            self.assertIn("unrelated", [p["metadata"]["name"] for p in pods["items"]])
            helper = next(p for p in pods["items"] if p["metadata"]["name"] != "unrelated")
            self.assertEqual("50m", helper["spec"]["containers"][0]["resources"]["requests"]["cpu"])
            self.assertEqual(snapshots["worker"][1], metrics)
        self.assertEqual(3, len(self.f.pods))  # original inventory untouched

    def test_coordinator_cannot_use_projected_freed_space(self):
        self.f.nodes[0]["status"]["allocatable"]["memory"] = "1Gi"
        with self.assertRaises(Held): self.review.preview(self.body)

    def test_other_claim_dependencies_are_included_in_worker_read_scope(self):
        self.dep["spec"]["template"]["spec"]["volumes"].append({"name": "other", "persistentVolumeClaim": {"claimName": "other"}})
        pvc = copy.deepcopy(self.c.source)
        pvc["metadata"].update(name="other", uid="other-claim")
        pvc["spec"]["volumeName"] = "other-pv"
        pv = copy.deepcopy(self.c.oldpv)
        pv["metadata"].update(name="other-pv", uid="other-volume")
        pv["spec"]["claimRef"].update(name="other", uid="other-claim")
        self.c.objects["/api/v1/namespaces/lab/persistentvolumeclaims/other"] = pvc
        self.c.objects["/api/v1/persistentvolumes/other-pv"] = pv
        result = self.review.approve(self.approved())
        self.assertEqual(("source", "target", "other"), result["scope"].claims)
        result["scope"].check("GET", "/api/v1/persistentvolumes/other-pv")

    def test_foreign_volume_consumers_and_unverified_ownership_block(self):
        for transform in (lambda: self.f.pods[0]["metadata"].pop("ownerReferences"),
                          lambda: self.f.pods[0]["metadata"]["ownerReferences"][0].update(uid="foreign-rs"),
                          lambda: self.f.pods[0]["spec"]["volumes"][0]["persistentVolumeClaim"].update(claimName="target")):
            self.setUp(); transform()
            with self.assertRaises(Held): self.review.preview(self.body)

    def test_rollout_terminating_replicas_or_pending_destination_cannot_be_reviewed(self):
        for transform in (lambda: self.dep["status"].update(updatedReplicas=1),
                          lambda: self.f.pods[0]["metadata"].update(deletionTimestamp="now"),
                          lambda: self.c.objects["/api/v1/namespaces/lab/persistentvolumeclaims/target"]["status"].update(phase="Pending")):
            self.setUp(); transform()
            with self.assertRaises(Held): self.review.preview(self.body)

    def test_copy_and_restart_approvals_match_the_runtime_templates(self):
        result = self.review.approve(self.approved())
        state = {"source": {"name": "source"}, "destination": "target", "replicas": 2, "operation": OP,
                 "plan": {"data_volume": "data", "copy_image": IMAGE, "copy_node": "node1"}}
        admit = D.Admitter(self.f.read, "lab", result["nodes"], result["policy"], handoff=state, clock=lambda: 1000)
        self.assertTrue(admit("stop", result["deployment"]))

    def test_mutable_source_tag_becomes_pinned_restart_approval(self):
        self.dep["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/homestead-lab/homestead:latest"
        result = self.review.approve(self.approved())
        state = {"source": {"name": "source"}, "destination": "target", "replicas": 2, "operation": OP,
                 "plan": {"data_volume": "data", "copy_image": IMAGE, "copy_node": "node1"}}
        admit = D.Admitter(self.f.read, "lab", result["nodes"], result["policy"], handoff=state, clock=lambda: 1000)
        self.assertTrue(admit("stop", result["deployment"]))
        restarted = copy.deepcopy(result["deployment"])
        restarted["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "target"
        self.assertNotEqual(result["policy"]["reviews"]["restart"]["proposal"],
                            D.review(self.f.read, "lab", "restart", restarted, result["nodes"], 88, clock=lambda: 1000)["receipt"]["proposal"])

    def test_source_process_identity_and_whole_volume_mount_must_match(self):
        self.review.source_pod = copy.deepcopy(self.f.pods[0])
        self.review.preview(self.body)
        self.review.source_pod["metadata"]["uid"] = "foreign-pod"
        with self.assertRaises(Held): self.review.preview(self.body)
        for key, value in (("subPath", "config"), ("subPathExpr", "$(POD)"), ("readOnly", True), ("mountPath", "/different")):
            self.setUp()
            self.f.pods[0]["spec"]["containers"][0]["volumeMounts"][0][key] = value
            self.review.source_pod = copy.deepcopy(self.f.pods[0])
            with self.assertRaises(Held): self.review.preview(self.body)

    def test_real_post_route_is_admin_scoped_and_read_only(self):
        import server
        import urllib.error
        current = self.f.pods[0]
        current["status"]["containerStatuses"] = [{"name": "homestead", "ready": True,
            "state": {"running": {"startedAt": "now"}}, "imageID": IMAGE}]
        def read(path):
            if path.endswith("/configmaps/homestead-data-handoff"):
                raise urllib.error.HTTPError(path, 404, "absent", {}, None)
            if path.endswith("/pods/old-0"): return copy.deepcopy(current)
            if path.endswith("/services"):
                return {"items": [obj("Service", "homestead", {"selector": {"app": "homestead"}, "ports": [{"port": 8080}]})]}
            return self.f.read(path)
        self.dep["spec"]["template"]["metadata"] = {"labels": {"app": "homestead"}}
        handler = object.__new__(server.H)
        handler.path = "/api/self/data/move/preview"; handler.user = "admin"
        handler.headers = {}
        handler._client_ip = mock.Mock(return_value="127.0.0.1")
        handler._guard = mock.Mock(return_value=False); handler._body = mock.Mock(return_value=self.body)
        handler._send = mock.Mock()
        with mock.patch.object(server.SELF, "NS", "lab"), mock.patch.object(server.SELF, "POD", "old-0"), \
             mock.patch.object(server, "DATA_DIR", "/data"), mock.patch.object(server, "kget", side_effect=read), \
             mock.patch.object(server.OPS, "_read", return_value=[]), \
             mock.patch.object(server.OPS, "list_operations", side_effect=AssertionError("must not poll resolvers")), \
             mock.patch.object(server.STORAGE_RUNTIME, "require_self_data", return_value=[{"protocol": 1}]) as runtime, \
             mock.patch.object(server, "ksend", side_effect=AssertionError("review must not mutate")), \
             mock.patch.object(server, "get_app_settings", return_value={"thresholds": {"memory": {"critical": 88}}}):
            handler.do_POST()
        self.assertEqual(200, handler._send.call_args.args[0], handler._send.call_args.args)
        self.assertIn("capacity_token", handler._send.call_args.args[1])
        self.assertEqual("admin", server.needed_role(handler.path, "POST"))
        handler._guard.assert_called_once_with(handler.path)
        runtime.assert_called_once()

    def test_replica_runtime_proof_is_bound_to_the_confirmation(self):
        proof = [{"pod_uid": "one", "container_id": "original", "mount": {"inode": 1}}]
        self.review.runtime_check = mock.Mock(side_effect=lambda *_: copy.deepcopy(proof))
        body = self.approved()
        proof[0]["container_id"] = "restarted"
        with self.assertRaisesRegex(Held, "Review the current move"):
            self.review.approve(body)
        self.review.runtime_check.side_effect = Held("Unverified replica")
        with self.assertRaisesRegex(Held, "Unverified replica"):
            self.review.preview(self.body)

    def test_api_holds_busy_or_recovery_jobs_without_reading_cluster(self):
        import server
        for job in ({"id": "busy-job", "title": "Busy job", "status": "running"}, {"id": "recovery-job", "title": "Recovery job", "status": "failed", "ref": {"retain_resources": True}}):
            with mock.patch.object(server.OPS, "_read", return_value=[job]), mock.patch.object(server, "kget") as read:
                with self.assertRaisesRegex(Held, job["id"]): server.preview_self_data_move(self.body, "admin")
                read.assert_not_called()

    def test_api_sanitizes_unavailable_inventory_without_starting_anything(self):
        import server
        with mock.patch.object(server.OPS, "_read", side_effect=OSError("private upstream detail")), \
             mock.patch.object(server, "ksend") as send:
            with self.assertRaises(Held) as caught: server.preview_self_data_move(self.body, "admin")
            self.assertNotIn("private", str(caught.exception)); send.assert_not_called()


if __name__ == "__main__": unittest.main()
