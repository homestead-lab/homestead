import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_capacity_review as review
import homestead_place as place


class DeployCapacityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"name": "demo", "namespace": "lab", "image": "example/demo:1", "target_mode": "new",
                    "memory": "1Gi", "memory_limit": "2Gi", "ports": [], "volumes": []}
        self.nodes = [{"name": "a", "status": "Ready", "schedulable": True, "hardware": {},
                       "labels": {"zone": "west", "kubernetes.io/hostname": "a"},
                       "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "100"},
                       "mem_cap_gb": 8, "mem_used_gb": 1, "mem_metrics_available": True}]
        self.objects = {"/api/v1/pods": {"items": []},
                        "/apis/storage.k8s.io/v1/storageclasses/test-class": {
                            "metadata": {"name": "test-class"}, "volumeBindingMode": "WaitForFirstConsumer"}}
        self.send = mock.Mock()
        for patch in (mock.patch.object(review, "_key", return_value=b"unit-test-review-key"),
                      mock.patch.object(place, "kget", side_effect=self.get),
                      mock.patch.object(place, "ksend", self.send),
                      mock.patch.object(server, "ksend", self.send),
                      mock.patch.object(place, "get_nodes", side_effect=lambda: self.nodes),
                      mock.patch.object(place, "hardware_features", return_value=[]),
                      mock.patch.object(server.HW, "features", return_value=[]),
                      mock.patch.object(server, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS)):
            patch.start()
            self.addCleanup(patch.stop)

    def get(self, path):
        value = self.objects.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(value)

    def volume(self, **kwargs):
        self.cfg["volumes"] = [{"type": "pvc", "path": "/config", "source": "data", "create": True,
                                "access_mode": "ReadWriteOnce", "storage_class": "test-class", "size_gb": 10, **kwargs}]

    def plan(self):
        result = server.deploy_capacity_plan(self.cfg)
        self.send.assert_not_called()
        return result

    def test_manifest_checks_new_deployment_without_fetching_it(self):
        result = self.plan()
        self.assertEqual(1, result["additional"])
        self.assertFalse(result["blocked"])
        self.assertFalse(result["requires_confirmation"])
        self.assertEqual(2, result["pod_memory_gb"])

    def test_no_cpu_memory_or_hardware_capacity_is_a_hard_block(self):
        self.cfg["memory"] = "9Gi"
        self.cfg["memory_limit"] = "10Gi"
        self.assertTrue(self.plan()["blocked"])

    def test_new_claim_can_reach_delayed_binding_with_warning(self):
        self.volume()
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertTrue(result["requires_confirmation"])
        self.assertIn("planned, not provisioned", " ".join(result["warnings"]))

    def test_missing_existing_claim_is_not_treated_as_planned(self):
        self.volume(create=False)
        self.assertTrue(self.plan()["blocked"])

    def test_missing_class_blocks_new_claim(self):
        self.volume(storage_class="missing")
        self.assertTrue(self.plan()["blocked"])

    def test_missing_claim_api_permissions_are_unknown_not_planned(self):
        self.volume()
        path = "/api/v1/namespaces/lab/persistentvolumeclaims/data"
        self.objects[path] = urllib.error.HTTPError(path, 403, "forbidden", {}, None)
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertIn("PVC data could not be verified", result["warnings"])
        self.assertNotIn("planned, not provisioned", " ".join(result["warnings"]))

    def test_new_claim_class_topology_blocks_wrong_host(self):
        self.volume()
        self.objects["/apis/storage.k8s.io/v1/storageclasses/test-class"]["allowedTopologies"] = [
            {"matchLabelExpressions": [{"key": "zone", "values": ["east"]}]}]
        self.assertTrue(self.plan()["blocked"])

    def test_new_rwo_claim_replicas_must_fit_on_one_host(self):
        self.volume()
        self.cfg.update(replicas=2, memory="5Gi", memory_limit="5Gi")
        self.nodes.append({**copy.deepcopy(self.nodes[0]), "name": "b"})
        self.assertTrue(self.plan()["blocked"])
        self.cfg["volumes"][0]["access_mode"] = "ReadWriteMany"
        self.assertFalse(self.plan()["blocked"])

    def test_new_rwop_claim_has_one_pod_limit(self):
        self.volume(access_mode="ReadWriteOncePod")
        self.cfg["replicas"] = 2
        self.assertTrue(self.plan()["blocked"])

    def test_real_claim_overrides_proposed_mode_and_topology(self):
        self.volume(access_mode="ReadWriteMany")
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"] = {
            "metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOncePod"], "storageClassName": "test-class"}}
        self.cfg["replicas"] = 2
        result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertNotIn("planned, not provisioned", " ".join(result["warnings"]))

    def test_migratable_vm_class_is_not_for_container_claim(self):
        self.volume()
        self.objects["/apis/storage.k8s.io/v1/storageclasses/test-class"]["provisioner"] = "driver.longhorn.io"
        self.objects["/apis/storage.k8s.io/v1/storageclasses/test-class"]["parameters"] = {"migratable": "true"}
        self.assertTrue(self.plan()["blocked"])

    def test_owner_init_is_included_without_registry_or_mutations(self):
        self.volume()
        self.cfg.update(memory="1Mi", memory_limit="2Mi")
        with mock.patch.object(server.VOLOWNER, "prepare") as prepare:
            result = self.plan()
        prepare.assert_not_called()
        self.assertEqual(round(16 / 1024, 2), result["pod_request_gb"])
        self.assertIn(server.VOLOWNER.INIT, result["unbounded"])

    def test_rejected_guard_never_reaches_side_effects_even_with_ack(self):
        self.cfg["memory"] = "10Gi"
        self.cfg["memory_limit"] = "12Gi"
        cfg = server.analyze_deploy_intent(self.cfg)
        cfg.update(capacity_token=review.issue(cfg), confirm_capacity=True)
        with mock.patch.object(server, "run_deploy") as deploy:
            with self.assertRaises(review.Rejected):
                server.reviewed_deploy(cfg)
        deploy.assert_not_called()
        self.send.assert_not_called()

    def test_unknown_requires_valid_review_token_and_ack(self):
        self.nodes[0]["mem_metrics_available"] = False
        with mock.patch.object(server, "run_deploy", return_value={"ok": True}) as deploy:
            for cfg in (self.cfg, {**self.cfg, "confirm_capacity": True}):
                with self.assertRaises(review.Rejected):
                    server.reviewed_deploy(cfg)
            deploy.assert_not_called()
            cfg = server.analyze_deploy_intent(self.cfg)
            cfg.update(capacity_token=review.issue(cfg), confirm_capacity=True)
            self.assertTrue(server.reviewed_deploy(cfg)["ok"])

    def test_fresh_recheck_blocks_previously_reviewed_capacity(self):
        cfg = server.analyze_deploy_intent(self.cfg)
        cfg.update(capacity_token=review.issue(cfg), confirm_capacity=True)
        self.objects["/api/v1/pods"]["items"] = [{"metadata": {"namespace": "other"},
            "spec": {"nodeName": "a", "containers": [{"resources": {"requests": {"memory": "8Gi"}}}]}}]
        with mock.patch.object(server, "run_deploy") as deploy:
            with self.assertRaises(review.Rejected):
                server.reviewed_deploy(cfg)
        deploy.assert_not_called()

    def test_invalid_count_rejected_before_any_writes(self):
        for count in (-1, 101):
            self.cfg["replicas"] = count
            with mock.patch.object(server, "run_deploy") as deploy:
                with self.assertRaises(ValueError):
                    server.reviewed_deploy(self.cfg)
            deploy.assert_not_called()

    def test_shared_pod_requires_existing_workload_selection(self):
        self.cfg["target_mode"] = "existing"
        with self.assertRaisesRegex(ValueError, "existing workload"):
            self.plan()

    def test_api_409_for_capacity_block_precedes_all_writes(self):
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/deploy", {}
        handler._guard = lambda path: False
        handler._body = lambda: self.cfg
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        self.cfg.update(memory="10Gi", memory_limit="12Gi")
        with mock.patch.object(server, "run_deploy") as deploy:
            handler.do_POST()
        self.assertEqual(409, handler._send.call_args.args[0])
        self.assertTrue(handler._send.call_args.args[1]["review_required"])
        deploy.assert_not_called()

    def test_preview_token_accepts_the_exact_followup_request(self):
        self.nodes[0]["mem_metrics_available"] = False
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
        self.assertTrue(body["capacity"]["requires_confirmation"])
        cfg = {**self.cfg, "capacity_token": body["capacity_token"], "confirm_capacity": True}
        with mock.patch.object(server, "run_deploy", return_value={"ok": True}) as deploy:
            self.assertTrue(server.reviewed_deploy(cfg)["ok"])
        deploy.assert_called_once()


class ReviewTokenTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(review, "_key", return_value=b"unit-test-review-key")
        patch.start()
        self.addCleanup(patch.stop)

    def test_token_hides_configuration_and_is_bound_to_all_input(self):
        cfg = {"image": "private:1", "env": {"PASSWORD": "sensitive-example"}}
        token = review.issue(cfg)
        self.assertNotIn("sensitive", token)
        self.assertTrue(review.valid({**cfg, "capacity_token": token, "confirm_capacity": True}))
        self.assertFalse(review.valid({**cfg, "image": "changed", "capacity_token": token}))

    def test_token_expiry_and_tampering(self):
        with mock.patch.object(review.time, "time", return_value=1000):
            token = review.issue({"name": "demo"})
        with mock.patch.object(review.time, "time", return_value=1601):
            self.assertFalse(review.valid({"name": "demo", "capacity_token": token}))
        self.assertFalse(review.valid({"name": "demo", "capacity_token": "bad"}))
        self.assertFalse(review.valid({"name": "demo", "capacity_token": token + "x"}))

    def test_same_shared_key_survives_process_change_but_rotation_invalidates(self):
        token = review.issue({"name": "demo"})
        with mock.patch.object(review, "_key", return_value=b"unit-test-review-key"):
            self.assertTrue(review.valid({"name": "demo", "capacity_token": token}))
        with mock.patch.object(review, "_key", return_value=b"rotated-test-key"):
            self.assertFalse(review.valid({"name": "demo", "capacity_token": token}))

    def test_auth_review_key_is_read_only_and_domain_separated(self):
        with mock.patch.object(server.AUTH, "_load", return_value={"signing_key": "example-test-key"}), \
                mock.patch.object(server.AUTH, "_save") as save:
            key = server.AUTH.review_signing_key()
            self.assertNotEqual(b"example-test-key", key)
            self.assertEqual(key, server.AUTH.review_signing_key())
        save.assert_not_called()
        with mock.patch.object(server.AUTH, "_load", return_value={}), mock.patch.object(server.AUTH, "_save") as save:
            with self.assertRaises(server.AUTH.StoreUnavailable):
                server.AUTH.review_signing_key()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
