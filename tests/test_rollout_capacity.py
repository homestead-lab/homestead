import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_place as place
import homestead_rollout_capacity as rollout
import homestead_capacity_review as review


def owner(kind, uid):
    return {"kind": kind, "uid": uid, "controller": True}


class RolloutCapacityTests(unittest.TestCase):
    def setUp(self):
        self.current = {"metadata": {"name": "shared", "namespace": "lab", "uid": "dep-one", "resourceVersion": "10", "generation": 2},
                        "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": "shared"}},
                                 "template": {"metadata": {"labels": {"app": "shared"}}, "spec": {"containers": [
                                     {"name": "main", "image": "example/main:1", "resources": {
                                         "requests": {"memory": "5Gi"}, "limits": {"memory": "5Gi"}}}]}}},
                        "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}
        self.cfg = {"target_mode": "existing", "target_workload": "shared", "namespace": "lab", "name": "helper",
                    "image": "example/helper:1", "memory": "1Gi", "memory_limit": "1Gi", "volumes": [], "ports": []}
        self.nodes = [{"name": "a", "labels": {"kubernetes.io/hostname": "a"}, "status": "Ready", "schedulable": True,
                       "hardware": {}, "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "100"},
                       "mem_cap_gb": 8, "mem_used_gb": 5, "mem_metrics_available": True}]
        self.pods = [{"metadata": {"name": "shared-old", "namespace": "lab", "uid": "pod-old", "labels": {"app": "shared"},
                                   "ownerReferences": [owner("ReplicaSet", "rs-one")]},
                      "spec": {**copy.deepcopy(self.current["spec"]["template"]["spec"]), "nodeName": "a"},
                      "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}]
        self.sets = [{"metadata": {"name": "shared-old-rs", "namespace": "lab", "uid": "rs-one",
                                  "ownerReferences": [owner("Deployment", "dep-one")]}}]
        self.objects = {}
        for patch in (mock.patch.object(server, "kget", side_effect=self.get),
                      mock.patch.object(place, "kget", side_effect=self.get),
                      mock.patch.object(place, "get_nodes", side_effect=lambda: self.nodes),
                      mock.patch.object(place, "hardware_features", return_value=[]),
                      mock.patch.object(server.HW, "features", return_value=[]),
                      mock.patch.object(server, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS),
                      mock.patch.object(review, "_key", return_value=b"test-review-key")):
            patch.start()
            self.addCleanup(patch.stop)

    def get(self, path):
        if path in self.objects:
            obj = self.objects[path]
            if isinstance(obj, Exception):
                raise obj
            return copy.deepcopy(obj)
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path.endswith("/replicasets"):
            return {"items": copy.deepcopy(self.sets)}
        if path.endswith("/deployments/shared"):
            return copy.deepcopy(self.current)
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def plan(self):
        with mock.patch.object(server, "ksend") as send:
            result = server.deploy_capacity_plan(self.cfg)
        send.assert_not_called()
        return result

    def rolling(self, surge=1, unavailable=0):
        self.current["spec"]["strategy"] = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": surge, "maxUnavailable": unavailable}}

    def claim(self):
        volume = {"name": "data", "persistentVolumeClaim": {"claimName": "data"}}
        self.current["spec"]["template"]["spec"]["volumes"] = [volume]
        self.pods[0]["spec"]["volumes"] = [copy.deepcopy(volume)]
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"] = {
            "metadata": {"name": "data", "uid": "data-uid"},
            "spec": {"accessModes": ["ReadWriteOncePod"], "volumeName": "pv-data"}, "status": {"phase": "Bound"}}
        self.objects["/api/v1/persistentvolumes/pv-data"] = {
            "metadata": {"name": "pv-data"}, "spec": {"claimRef": {"name": "data", "namespace": "lab", "uid": "data-uid"}},
            "status": {"phase": "Bound"}}

    def reviewed(self):
        cfg = server.analyze_deploy_intent(copy.deepcopy(self.cfg))
        cfg["capacity_token"] = review.issue(cfg, server.rollout_review_context(self.current))
        cfg["confirm_capacity"] = True
        return cfg

    def test_recreate_estimates_entire_new_pod_after_conditional_release(self):
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertEqual(6, result["pod_request_gb"])
        self.assertEqual(5, result["rollout"]["release_request_gb"])
        self.assertEqual(0, result["candidates"][0]["reserved_gb"])
        self.assertEqual(11, result["candidates"][0]["projected_gb"])  # live usage is NOT assumed freed.
        self.assertTrue(result["requires_confirmation"])
        self.assertIn("unavailable during the restart", " ".join(result["warnings"]))

    def test_full_updated_pod_too_large_is_blocked(self):
        self.cfg.update(memory="4Gi", memory_limit="4Gi")
        self.assertTrue(self.plan()["blocked"])

    def test_unrelated_pod_with_same_labels_is_never_reclaimed(self):
        other = copy.deepcopy(self.pods[0])
        other["metadata"].update(name="unrelated", uid="outsider", ownerReferences=[])
        self.pods.append(other)
        result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertEqual(["shared-old"], result["rollout"]["owned_pods"])
        self.assertEqual(5, result["candidates"][0]["reserved_gb"])

    def test_replicaset_name_is_not_identity(self):
        self.sets[0]["metadata"]["ownerReferences"] = [owner("Deployment", "other-deployment")]
        result = self.plan()
        self.assertEqual([], result["rollout"]["owned_pods"])
        self.assertTrue(result["blocked"])

    def test_noncontroller_owner_reference_is_not_released(self):
        self.pods[0]["metadata"]["ownerReferences"][0]["controller"] = False
        self.assertEqual([], self.plan()["rollout"]["owned_pods"])

    def test_incomplete_ownership_is_unknown_not_fake_free_or_false_block(self):
        self.objects["/apis/apps/v1/namespaces/lab/replicasets"] = {"items": self.sets, "metadata": {"continue": "next"}}
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertFalse(result["rollout"]["ownership_known"])
        self.assertIsNone(result["candidates"][0]["reserved_gb"])
        self.assertIn("ownership is unverified", " ".join(result["warnings"]))

    def test_missing_pods_are_unknown(self):
        self.objects["/api/v1/pods"] = OSError("offline")
        result = self.plan()
        self.assertFalse(result["rollout"]["ownership_known"])
        self.assertFalse(result["blocked"])
        self.assertTrue(result["requires_confirmation"])

    def test_deleted_replicaset_race_is_unknown(self):
        self.sets = []
        self.assertFalse(self.plan()["rollout"]["ownership_known"])

    def test_terminating_old_pod_is_only_conditionally_released(self):
        self.pods[0]["metadata"]["deletionTimestamp"] = "now"
        result = self.plan()
        self.assertEqual(5, result["rollout"]["release_request_gb"])
        self.assertIn("fully terminated", " ".join(result["warnings"]))
        self.rolling()
        self.assertFalse(self.plan()["rollout"]["start_blocked"])

    def test_rolling_zero_unavailable_blocks_no_room_for_first_pod(self):
        self.rolling()
        result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertTrue(result["rollout"]["start_blocked"])
        self.assertEqual(5, result["rollout"]["first_pod"]["candidates"][0]["reserved_gb"])

    def test_allowed_unavailability_is_a_warning_not_false_deadlock(self):
        self.rolling(unavailable=1)
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertFalse(result["rollout"]["start_blocked"])
        self.assertTrue(result["rollout"]["first_pod"]["blocked"])
        self.assertIn("allowed old-pod removal", " ".join(result["warnings"]))

    def test_unknown_or_inflight_rollout_is_not_false_proven_deadlock(self):
        self.rolling()
        for key in ("observedGeneration", "readyReplicas", "updatedReplicas", "availableReplicas"):
            with self.subTest(key=key):
                original = self.current["status"].pop(key)
                self.assertFalse(self.plan()["rollout"]["start_blocked"])
                self.current["status"][key] = original

    def test_surge_capacity_does_not_claim_complete_rollout_simulation(self):
        self.rolling()
        self.nodes[0]["allocatable"]["memory"] = "20Gi"
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertFalse(result["rollout"]["intermediate_steps_verified"])
        self.assertFalse(result["rollout"]["overlap"]["blocked"])

    def test_zero_surge_warns_about_removal_and_no_overlap_estimate(self):
        self.rolling(surge=0, unavailable=1)
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertNotIn("overlap", result["rollout"])

    def test_rwop_available_after_recreate_but_not_rolling_overlap(self):
        self.claim()
        self.assertFalse(self.plan()["blocked"])
        self.rolling()
        result = self.plan()
        self.assertTrue(result["rollout"]["start_blocked"])
        self.assertIn("ReadWriteOncePod", str(result["rollout"]["first_pod"]["candidates"]))

    def test_unrelated_rwop_consumer_remains_blocking_after_recreate(self):
        self.claim()
        other = copy.deepcopy(self.pods[0])
        other["metadata"].update(name="unrelated", ownerReferences=[])
        other["spec"]["containers"] = []
        self.pods.append(other)
        self.assertTrue(self.plan()["blocked"])

    def test_old_host_port_is_only_released_in_post_stop_model(self):
        self.current["spec"]["template"]["spec"]["hostNetwork"] = True
        self.current["spec"]["template"]["spec"]["containers"][0]["ports"] = [{"containerPort": 9000}]
        self.pods[0]["spec"] = {**copy.deepcopy(self.current["spec"]["template"]["spec"]), "nodeName": "a"}
        self.assertFalse(self.plan()["blocked"])
        self.nodes[0]["allocatable"]["memory"] = "20Gi"
        self.rolling()
        result = self.plan()
        self.assertTrue(result["rollout"]["start_blocked"])
        self.assertIn("host port", str(result["rollout"]["first_pod"]["candidates"]))

    def test_stopped_workload_does_not_start_pods(self):
        self.current["spec"]["replicas"] = 0
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertEqual(0, result["additional"])
        self.assertIn("no pods are started", " ".join(result["warnings"]))

    def test_paused_template_can_be_saved_but_resume_blocker_is_not_hidden(self):
        self.rolling()
        self.current["spec"]["paused"] = True
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertTrue(result["rollout"]["resume_blocked"])
        self.assertTrue(result["requires_confirmation"])
        self.assertIn("before resuming", " ".join(result["warnings"]))

    def test_percentage_fence_rounding_and_invalid_limits(self):
        self.assertEqual(1, rollout.fence("25%", 3, True))
        self.assertEqual(0, rollout.fence("25%", 3, False))
        self.assertEqual(2, rollout.fence("25%", 5, True))
        for value in (-1, "-1%", "101%", "invalid"):
            with self.assertRaises(ValueError):
                rollout.fence(value, 3, True)
        self.rolling(surge=0, unavailable=0)
        with self.assertRaises(ValueError):
            self.plan()

    def test_stale_resource_version_requires_new_review_before_mutations(self):
        cfg = self.reviewed()
        self.current["metadata"]["resourceVersion"] = "11"
        with mock.patch.object(server, "run_deploy") as deploy:
            with self.assertRaises(review.Rejected):
                server.reviewed_deploy(cfg)
        deploy.assert_not_called()

    def test_replaced_deployment_with_same_name_invalidates_review(self):
        cfg = self.reviewed()
        self.current["metadata"]["uid"] = "replacement"
        with mock.patch.object(server, "run_deploy") as deploy:
            with self.assertRaises(review.Rejected):
                server.reviewed_deploy(cfg)
        deploy.assert_not_called()

    def test_ack_cannot_override_rolling_deadlock(self):
        self.rolling()
        with mock.patch.object(server, "run_deploy") as deploy:
            with self.assertRaises(review.Rejected):
                server.reviewed_deploy(self.reviewed())
        deploy.assert_not_called()

    def test_success_passes_exact_fresh_controller_to_mutation(self):
        with mock.patch.object(server, "run_deploy", return_value={"ok": True}) as deploy:
            self.assertTrue(server.reviewed_deploy(self.reviewed())["ok"])
        self.assertEqual(self.current, deploy.call_args.kwargs["reviewed_current"])

    def test_http_preview_and_join_share_the_same_version_bound_review(self):
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/preview", {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(self.cfg)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.NETWORK, "prepare_deploy", side_effect=lambda cfg: cfg):
            handler.do_POST()
        code, body = handler._send.call_args.args
        self.assertEqual(200, code)
        self.assertEqual("Recreate", body["capacity"]["rollout"]["strategy"])
        cfg = {**self.cfg, "confirm_capacity": True, "capacity_token": body["capacity_token"]}
        with mock.patch.object(server, "run_deploy", return_value={"ok": True}) as deploy:
            self.assertTrue(server.reviewed_deploy(cfg)["ok"])
        deploy.assert_called_once()

    def test_full_pod_preview_preserves_the_current_controller(self):
        before = copy.deepcopy(self.current)
        self.plan()
        self.assertEqual(before, self.current)

    def test_final_put_preserves_reviewed_resource_version_without_second_read(self):
        with mock.patch.object(server, "persist_icon_config"), mock.patch.object(server.NETWORK, "prepare_deploy", side_effect=lambda cfg: cfg), \
                mock.patch.object(server, "ksend") as send, mock.patch.object(server.OPS, "start", return_value={}), \
                mock.patch.object(server, "kget", side_effect=AssertionError("unreviewed second read")):
            server.run_deploy(copy.deepcopy(self.cfg), reviewed_current=copy.deepcopy(self.current))
        deployment_put = next(call for call in send.call_args_list if call.args[0] == "PUT")
        self.assertEqual("10", deployment_put.args[2]["metadata"]["resourceVersion"])
        self.assertEqual(2, len(deployment_put.args[2]["spec"]["template"]["spec"]["containers"]))


if __name__ == "__main__":
    unittest.main()
