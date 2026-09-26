import copy
import unittest
import urllib.error
from unittest import mock

import test_rollout_capacity as fixtures
import server


class HostMoveCapacityTests(unittest.TestCase):
    get = fixtures.RolloutCapacityTests.get

    def setUp(self):
        fixtures.RolloutCapacityTests.setUp(self)
        self.nodes.append({**copy.deepcopy(self.nodes[0]), "name": "b", "labels": {"kubernetes.io/hostname": "b"}, "mem_used_gb": 1})
        self.config = {"ns": "lab", "name": "shared", "node": "b", "pin": False}

    def call(self, path, config):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(config)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server, "ksend") as send, mock.patch.object(server.OPS, "start", return_value={}) as job:
            handler.do_POST()
        return handler._send.call_args.args, send, job

    def reviewed(self):
        result, send, job = self.call("/api/move/preview", self.config)
        self.assertEqual(200, result[0], result)
        send.assert_not_called()
        job.assert_not_called()
        return {**copy.deepcopy(self.config), "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}

    def test_preview_is_pure_and_includes_all_containers(self):
        self.current["spec"]["strategy"] = {"type": "RollingUpdate"}
        self.current["spec"]["template"]["spec"]["containers"].append({"name": "helper", "resources": {
            "requests": {"memory": "1Gi"}, "limits": {"memory": "1Gi"}}})
        before = copy.deepcopy(self.current)
        preview = server.preview_host_move(self.config)["capacity"]
        self.assertEqual(6, preview["pod_request_gb"])
        self.assertEqual("Recreate", preview["rollout"]["strategy"])
        self.assertIn("unavailable during the restart", " ".join(preview["warnings"]))
        self.assertEqual(before, self.current)

    def test_missing_acknowledgement_never_writes_or_starts_job(self):
        result, send, job = self.call("/api/move", self.config)
        self.assertEqual(409, result[0])
        send.assert_not_called()
        job.assert_not_called()

    def test_preference_put_preserves_version_and_not_read_only_hard_pin(self):
        result, send, job = self.call("/api/move", self.reviewed())
        self.assertEqual(200, result[0], result)
        method, path, dep = send.call_args.args
        self.assertEqual(("PUT", "/apis/apps/v1/namespaces/lab/deployments/shared"), (method, path))
        self.assertEqual("10", dep["metadata"]["resourceVersion"])
        spec = dep["spec"]["template"]["spec"]
        self.assertNotIn("kubernetes.io/hostname", spec.get("nodeSelector", {}))
        self.assertEqual(["b"], spec["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0]["preference"]["matchExpressions"][0]["values"])
        self.assertEqual("Recreate", dep["spec"]["strategy"]["type"])
        job.assert_called_once()

    def test_chosen_host_shortfall_blocks_even_when_current_host_fits(self):
        self.nodes[1]["allocatable"]["memory"] = "4Gi"
        result, send, job = self.call("/api/move", self.reviewed())
        self.assertEqual(409, result[0])
        send.assert_not_called()
        job.assert_not_called()

    def test_competitor_arriving_after_review_is_counted(self):
        config = self.reviewed()
        self.pods.append({"metadata": {"namespace": "other", "name": "competitor"}, "spec": {
            "nodeName": "b", "containers": [{"resources": {"requests": {"memory": "4Gi"}}}]}, "status": {"phase": "Running"}})
        result, send, _ = self.call("/api/move", config)
        self.assertEqual(409, result[0])
        send.assert_not_called()

    def test_identity_version_and_input_changes_invalidate_token(self):
        for change in ("version", "uid", "node", "pin"):
            with self.subTest(change=change):
                original = copy.deepcopy(self.current)
                config = self.reviewed()
                if change == "version": self.current["metadata"]["resourceVersion"] = "11"
                elif change == "uid": self.current["metadata"]["uid"] = "replacement"
                elif change == "node": config["node"] = None
                else: config["pin"] = True
                result, send, _ = self.call("/api/move", config)
                self.assertIn(result[0], (400, 409))
                send.assert_not_called()
                self.current = original

    def test_incomplete_pod_or_replica_set_inventory_cannot_be_acknowledged(self):
        for path in ("/api/v1/pods", "/apis/apps/v1/namespaces/lab/replicasets"):
            self.objects[path] = {"items": [], "metadata": {"continue": "more"}}
            result, send, job = self.call("/api/move", {**self.config, "confirm_capacity": True})
            self.assertEqual(400, result[0], result)
            send.assert_not_called()
            job.assert_not_called()
            del self.objects[path]

    def test_unavailable_inventory_never_writes(self):
        self.objects["/api/v1/pods"] = urllib.error.HTTPError("pods", 503, "unavailable", {}, None)
        with mock.patch.object(server, "ksend") as send:
            with self.assertRaises(urllib.error.HTTPError):
                server.reviewed_host_move({**self.config, "confirm_capacity": True})
        send.assert_not_called()

    def test_all_replicas_must_fit_selected_host(self):
        self.current["spec"]["replicas"] = 2
        plan = server.preview_host_move(self.config)["capacity"]
        self.assertTrue(plan["blocked"])
        self.assertTrue(plan["move"]["target"]["blocked"])

    def test_unpin_retains_other_required_constraints(self):
        self.config.update(node=None)
        spec = self.current["spec"]["template"]["spec"]
        spec["nodeSelector"] = {"kubernetes.io/hostname": "a", "hardware/custom": "true"}
        proposed, capacity, _ = server.move_capacity_plan(self.config)
        self.assertEqual({"hardware/custom": "true"}, proposed["spec"]["template"]["spec"]["nodeSelector"])
        self.assertTrue(capacity["blocked"])

    def test_pv_topology_is_respected_at_destination(self):
        fixtures.RolloutCapacityTests.claim(self)
        self.objects["/api/v1/persistentvolumes/pv-data"]["spec"]["nodeAffinity"] = {"required": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["a"]}]}]}}
        self.assertTrue(server.preview_host_move(self.config)["capacity"]["blocked"])

    def test_unknown_memory_remains_warning(self):
        self.nodes[1]["mem_metrics_available"] = False
        plan = server.preview_host_move(self.config)["capacity"]
        self.assertFalse(plan["blocked"])
        self.assertIn("unavailable", " ".join(plan["move"]["target"]["warnings"]))
        self.assertTrue(plan["requires_confirmation"])

    def test_projection_over_100_percent_can_be_overridden_with_review(self):
        self.nodes[1]["mem_used_gb"] = 7
        plan = server.preview_host_move(self.config)["capacity"]
        target = next(n for n in plan["move"]["target"]["candidates"] if n["name"] == "b")
        self.assertEqual(150, target["projected_percent"])
        self.assertFalse(plan["blocked"])
        self.assertTrue(plan["requires_confirmation"])
        unconfirmed, send, job = self.call("/api/move", self.config)
        self.assertEqual(409, unconfirmed[0])
        send.assert_not_called()
        job.assert_not_called()
        result, send, job = self.call("/api/move", self.reviewed())
        self.assertEqual(200, result[0], result)
        send.assert_called_once()
        job.assert_called_once()

    def test_stopped_workload_stays_stopped(self):
        self.current["spec"]["replicas"] = 0
        self.pods = []
        result, send, _ = self.call("/api/move", self.reviewed())
        self.assertEqual(200, result[0], result)
        self.assertEqual(0, send.call_args.args[2]["spec"]["replicas"])

    def test_unsafe_or_ambiguous_legacy_inputs_are_rejected(self):
        for extra in ({"auto": True}, {"pin": "false"}, {"node": "missing"}, {"node": "../a"}):
            with self.assertRaises(ValueError):
                server.move_capacity_plan({**self.config, **extra})
        self.current["spec"]["paused"] = True
        with self.assertRaisesRegex(ValueError, "paused"):
            server.move_capacity_plan(self.config)
        self.current["spec"].pop("paused")
        self.current["spec"]["template"]["spec"]["nodeName"] = "a"
        with self.assertRaisesRegex(ValueError, "nodeName"):
            server.move_capacity_plan(self.config)

    def test_host_and_pod_local_data_warning(self):
        self.current["spec"]["template"]["spec"]["volumes"] = [
            {"name": "local", "hostPath": {"path": "/srv/data"}}, {"name": "scratch", "emptyDir": {}}]
        warnings = " ".join(server.preview_host_move(self.config)["capacity"]["warnings"])
        self.assertIn("not copied", warnings)
        self.assertIn("emptyDir data is lost", warnings)

    def test_fqdn_host_names_supported(self):
        self.nodes[1]["name"] = "b.example.test"
        self.nodes[1]["labels"]["kubernetes.io/hostname"] = "b.example.test"
        self.config["node"] = "b.example.test"
        self.assertFalse(server.preview_host_move(self.config)["capacity"]["blocked"])

    def test_target_taints_and_declared_host_port_conflicts_block(self):
        self.nodes[1]["taints"] = [{"key": "reserved", "effect": "NoSchedule"}]
        self.assertTrue(server.preview_host_move(self.config)["capacity"]["blocked"])
        self.nodes[1].pop("taints")
        self.current["spec"]["template"]["spec"]["containers"][0]["ports"] = [{"containerPort": 8123, "hostPort": 8123}]
        self.pods.append({"metadata": {"name": "other", "namespace": "lab"}, "status": {"phase": "Running"},
                          "spec": {"nodeName": "b", "containers": [{"ports": [{"containerPort": 8123, "hostPort": 8123}]}]}})
        self.assertTrue(server.preview_host_move(self.config)["capacity"]["blocked"])

    def test_same_label_unrelated_pod_is_not_reclaimed(self):
        other = copy.deepcopy(self.pods[0])
        other["metadata"].update(name="unrelated", uid="other", ownerReferences=[])
        other["spec"]["nodeName"] = "b"
        self.pods.append(other)
        self.assertTrue(server.preview_host_move(self.config)["capacity"]["blocked"])

    def test_put_conflict_propagates_without_retry_or_second_fetch(self):
        config = self.reviewed()
        conflict = urllib.error.HTTPError("deployment", 409, "changed", {}, None)
        with mock.patch.object(server, "ksend", side_effect=conflict) as send:
            with self.assertRaises(urllib.error.HTTPError):
                server.reviewed_host_move(config)
        send.assert_called_once()
        self.assertEqual("10", send.call_args.args[2]["metadata"]["resourceVersion"])

    def test_managed_share_containers_cannot_bypass_addon_management(self):
        with mock.patch.object(server, "guard_managed_smb", side_effect=ValueError("managed add-on")):
            result, send, job = self.call("/api/move", self.config)
        self.assertEqual(400, result[0])
        send.assert_not_called()
        job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
