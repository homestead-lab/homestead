import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_allocation_capacity as capacity


class ProbeCapacityTests(unittest.TestCase):
    def setUp(self):
        self.obj = {"metadata": {"uid": "ds", "generation": 1}, "spec": {},
                    "status": {"desiredNumberScheduled": 1, "numberReady": 1, "observedGeneration": 1}}
        self.pod = {"metadata": {"ownerReferences": [{"uid": "ds", "kind": "DaemonSet", "controller": True}]},
                    "spec": {"nodeName": "node", "containers": [{"name": "probe", "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}}}]},
                    "status": {"phase": "Running"}}
        self.template = {"spec": copy.deepcopy(self.pod["spec"])}
        self.template["spec"]["containers"].append({"name": "allocation", "resources": {
            "requests": {"cpu": "5m", "memory": "32Mi"}, "limits": {"memory": "96Mi"}}})
        self.node = {"name": "node", "status": "Ready", "allocatable": {"cpu": "2", "memory": "2Gi"},
                     "mem_metrics_available": True, "mem_cap_gb": 2, "mem_used_gb": 1}

    def plan(self):
        return capacity.plan(self.obj, self.template, [self.node], [self.pod], 88)

    def test_available_increment_with_identity_owned_non_surging_probe(self):
        result = self.plan()
        self.assertFalse(result["blocked"], result)
        self.assertEqual([], result["warnings"])
        self.assertEqual(64, len(result["fingerprint"]))

    def test_ram_warning_is_distinct_from_hard_request_limit(self):
        self.node["mem_used_gb"] = 1.9
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertIn("projected RAM", str(result["warnings"]))
        self.node["allocatable"]["memory"] = "40Mi"
        self.assertTrue(self.plan()["blocked"])

    def test_unknown_metrics_are_warning_but_missing_inventory_is_blocked(self):
        self.node["mem_metrics_available"] = False
        self.assertIn("unavailable", str(self.plan()["warnings"]))
        self.pod["metadata"]["ownerReferences"][0]["uid"] = "other"
        self.assertTrue(self.plan()["blocked"])

    def test_rollout_surge_unready_and_changed_generation_block(self):
        for mutation in (lambda: self.obj["spec"].update(updateStrategy={"rollingUpdate": {"maxSurge": 1}}),
                         lambda: self.obj["status"].update(numberReady=0),
                         lambda: self.obj["metadata"].update(generation=2)):
            self.setUp(); mutation()
            self.assertTrue(self.plan()["blocked"])
