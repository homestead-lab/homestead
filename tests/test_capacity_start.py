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
                       "hardware": {}, "labels": {}, "allocatable": {"memory": "16Gi", "cpu": "8"},
                       "mem_cap_gb": 16, "mem_used_gb": 13, "mem_metrics_available": True}]

    def plan(self, replicas=1):
        with mock.patch.object(place, "kget", return_value=copy.deepcopy(self.dep)), \
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


if __name__ == "__main__":
    unittest.main()
