import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_place as place


class StartCapacityTests(unittest.TestCase):
    def setUp(self):
        self.dep = {"spec": {"replicas": 0, "template": {"spec": {
            "containers": [{"name": "app", "resources": {
                "requests": {"memory": "1Gi"}, "limits": {"memory": "2Gi"}}}],
            "volumes": []}}}}
        self.nodes = [{"name": "node1", "status": "Ready", "schedulable": True,
                       "hardware": {}, "labels": {}, "allocatable": {"memory": "16Gi", "cpu": "8", "pods": "110"},
                       "mem_cap_gb": 16, "mem_used_gb": 13, "mem_metrics_available": True}]
        self.pods = []

    def plan(self, replicas=1):
        def get(path):
            return {"items": copy.deepcopy(self.pods)} if path == "/api/v1/pods" else copy.deepcopy(self.dep)
        with mock.patch.object(place, "kget", side_effect=get), \
                mock.patch.object(place, "get_nodes", return_value=copy.deepcopy(self.nodes)), \
                mock.patch.object(place, "hardware_features", return_value=[]):
            return place.start_plan("lab", "app", replicas, 88)

    def test_warns_before_start_near_oom(self):
        plan = self.plan()
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(93.8, plan["candidates"][0]["projected_percent"])
        self.assertIn("projected RAM", " ".join(plan["warnings"]))

    def test_unknown_usage_or_unbounded_memory_is_not_called_safe(self):
        self.nodes[0]["mem_metrics_available"] = False
        self.dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {}
        plan = self.plan()
        self.assertTrue(plan["requires_confirmation"])
        self.assertIn("live memory usage is unavailable", plan["warnings"])
        self.assertIn("memory is not limited", " ".join(plan["warnings"]))

    def test_pinned_workload_does_not_treat_other_host_as_failover(self):
        self.dep["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "node1"}
        self.nodes[0]["status"] = "NotReady"
        self.nodes.append({**self.nodes[0], "name": "node2", "status": "Ready", "mem_used_gb": 1})
        plan = self.plan()
        self.assertTrue(plan["blocked"])
        self.assertEqual([], [row for row in plan["candidates"] if row["eligible"]])

    def test_safe_start_uses_memory_limit_and_no_warning(self):
        self.nodes[0]["mem_used_gb"] = 2
        plan = self.plan()
        self.assertFalse(plan["requires_confirmation"])
        self.assertEqual(4.0, plan["candidates"][0]["projected_gb"])

    def test_unbounded_init_container_also_requires_review(self):
        self.nodes[0]["mem_used_gb"] = 2
        self.dep["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "setup", "resources": {"requests": {"memory": "1Gi"}}}]
        plan = self.plan()
        self.assertTrue(plan["requires_confirmation"])
        self.assertIn("setup", plan["unbounded"])

    def test_untolerated_taint_excludes_node(self):
        self.nodes[0]["taints"] = [{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]
        plan = self.plan()
        self.assertTrue(plan["blocked"])
        self.assertIn("untolerated dedicated taint", plan["candidates"][0]["reasons"])
        self.dep["spec"]["template"]["spec"]["tolerations"] = [
            {"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}]
        self.assertFalse(self.plan()["blocked"])

    def test_direct_node_name_bypasses_no_schedule_but_not_no_execute(self):
        self.dep["spec"]["template"]["spec"]["nodeName"] = "node1"
        self.nodes[0]["taints"] = [{"key": "dedicated", "effect": "NoSchedule"}]
        self.assertFalse(self.plan()["blocked"])
        self.nodes[0]["taints"][0]["effect"] = "NoExecute"
        self.assertTrue(self.plan()["blocked"])

    def test_required_node_affinity_excludes_other_nodes(self):
        self.nodes[0]["labels"] = {"zone": "west"}
        self.nodes.append({**self.nodes[0], "name": "node2", "labels": {"zone": "east"}})
        self.dep["spec"]["template"]["spec"]["affinity"] = {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["east"]}]}]}}}
        plan = self.plan()
        self.assertEqual(["node2"], [node["name"] for node in plan["candidates"] if node["eligible"]])

    def test_request_larger_than_allocatable_is_blocked(self):
        self.dep["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] = "20Gi"
        plan = self.plan()
        self.assertTrue(plan["blocked"])
        self.assertIn("memory request exceeds node allocatable", plan["candidates"][0]["reasons"])

    def test_unknown_pod_affinity_requires_acknowledgement(self):
        self.nodes[0]["mem_used_gb"] = 2
        self.dep["spec"]["template"]["spec"]["affinity"] = {"podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname"}]}}
        plan = self.plan()
        self.assertTrue(plan["requires_confirmation"])
        self.assertIn("pod affinity or topology spread", " ".join(plan["warnings"]))

    def reserve(self, memory="15.5Gi", cpu="0", node="node1", phase="Running", **meta):
        self.pods.append({"metadata": {"namespace": "kube-system", **meta},
                          "spec": {"nodeName": node, "containers": [{"name": "other", "resources": {
                              "requests": {"memory": memory, "cpu": cpu}}}]}, "status": {"phase": phase}})

    def test_quiet_host_with_reserved_memory_is_blocked(self):
        self.nodes[0]["mem_used_gb"] = 1
        self.reserve()
        plan = self.plan()
        self.assertTrue(plan["blocked"])
        self.assertIn("memory request exceeds remaining scheduler capacity", plan["candidates"][0]["reasons"])

    def test_cpu_and_pod_slots_are_also_reserved(self):
        self.dep["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "1"
        self.reserve(memory="1Gi", cpu="7500m")
        self.assertTrue(self.plan()["blocked"])
        self.pods.clear()
        self.nodes[0]["allocatable"]["pods"] = "1"
        self.reserve(memory="1Gi")
        self.assertTrue(self.plan()["blocked"])

    def test_multiple_replicas_need_slots_on_each_host_not_cluster_average(self):
        self.nodes[0]["mem_used_gb"] = 1
        self.reserve(memory="15Gi")
        self.nodes.append({**copy.deepcopy(self.nodes[0]), "name": "node2"})
        self.reserve(memory="15Gi", node="node2")
        plan = self.plan(replicas=3)
        self.assertTrue(plan["blocked"])
        self.assertEqual(2, plan["resource_slots"])
        self.assertFalse(self.plan(replicas=2)["blocked"])
        self.assertEqual([1, 1], [n["projected_pods"] for n in self.plan(replicas=2)["candidates"]])

    def test_terminal_pods_free_reservations_but_terminating_pods_do_not(self):
        self.reserve(phase="Succeeded")
        self.reserve(phase="Failed")
        self.assertFalse(self.plan()["blocked"])
        self.reserve(deletionTimestamp="now")
        self.assertTrue(self.plan()["blocked"])

    def test_scheduled_pending_pods_reserve_resources(self):
        self.reserve(phase="Pending")
        self.assertTrue(self.plan()["blocked"])

    def test_unscheduled_pods_are_competition_not_invented_node_reservations(self):
        self.nodes[0]["mem_used_gb"] = 1
        self.reserve(node="", phase="Pending")
        plan = self.plan()
        self.assertFalse(plan["blocked"])
        self.assertTrue(plan["requires_confirmation"])
        self.assertIn("compete for capacity", " ".join(plan["warnings"]))

    def test_limit_estimate_is_separate_from_request_and_reserved_ram(self):
        self.nodes[0]["mem_used_gb"] = 1
        self.reserve(memory="10Gi")
        plan = self.plan()
        self.assertEqual(1, plan["pod_request_gb"])
        self.assertEqual(2, plan["pod_memory_gb"])
        self.assertEqual(10, plan["candidates"][0]["reserved_gb"])
        self.assertEqual(12, plan["candidates"][0]["projected_gb"])

    def test_device_requests_sum_across_containers(self):
        spec = self.dep["spec"]["template"]["spec"]
        spec["containers"][0]["resources"]["limits"]["example.com/device"] = "1"
        spec["containers"].append(copy.deepcopy(spec["containers"][0]))
        self.nodes[0]["allocatable"]["example.com/device"] = "1"
        self.assertTrue(self.plan()["blocked"])

    def test_missing_inventory_needs_review_not_false_safe_result(self):
        self.nodes[0]["mem_used_gb"] = 1
        with mock.patch.object(place, "kget", return_value=copy.deepcopy(self.dep)), \
                mock.patch.object(place, "get_nodes", return_value=copy.deepcopy(self.nodes)), \
                mock.patch.object(place, "hardware_features", return_value=[]):
            plan = place.start_plan("lab", "app")
        self.assertFalse(plan["reservations_known"])
        self.assertIsNone(plan["resource_slots"])
        self.assertTrue(plan["requires_confirmation"])

    def test_scale_down_and_stop_do_not_require_capacity(self):
        self.dep["spec"]["replicas"] = 5
        self.reserve()
        self.assertFalse(self.plan(replicas=0)["blocked"])
        self.assertFalse(self.plan(replicas=1)["requires_confirmation"])


if __name__ == "__main__":
    unittest.main()
