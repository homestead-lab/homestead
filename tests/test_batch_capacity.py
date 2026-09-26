import copy
import unittest
from unittest import mock

import test_deploy_capacity as fixtures
import homestead_batch_capacity as batch
import server


class BatchCapacityTests(unittest.TestCase):
    get = fixtures.DeployCapacityTests.get

    def setUp(self):
        fixtures.DeployCapacityTests.setUp(self)
        self.entries = [self.entry("one", "5Gi"), self.entry("two", "5Gi")]

    def entry(self, name, memory="1Gi", **overrides):
        cfg = {**self.cfg, "name": name, "memory": memory, "memory_limit": memory, **overrides}
        _, dep, _, _ = server.capacity_manifest(cfg)
        return {"name": name, "deployment": dep, "replicas": dep["spec"]["replicas"]}

    def plan(self, **kwargs):
        return batch.plan(self.entries, "lab", self.objects["/api/v1/pods"]["items"], self.nodes,
                          kwargs.pop("claims", {}), 88, read=self.get, **kwargs)

    def test_individually_fitting_services_cannot_double_book_memory(self):
        for entry in self.entries:
            self.assertFalse(server.PLACE.manifest_plan(entry["deployment"], "lab", entry["name"])["blocked"])
        self.assertEqual("blocked", self.plan()["status"])
        self.send.assert_not_called()

    def test_search_backtracks_instead_of_blocking_on_first_greedy_host(self):
        self.nodes.append({**copy.deepcopy(self.nodes[0]), "name": "b", "labels": {"kubernetes.io/hostname": "b"}})
        self.entries[1]["deployment"]["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "a"}
        plan = self.plan()
        self.assertEqual("fits", plan["status"])
        self.assertEqual([{"service": "one", "host": "b"}, {"service": "two", "host": "a"}], plan["example"])

    def test_multi_replica_services_share_one_capacity_budget(self):
        self.entries = [self.entry("one", "3Gi"), self.entry("two", "3Gi")]
        self.entries[0]["replicas"] = 2
        self.assertTrue(self.plan()["blocked"])

    def test_joint_host_port_conflict(self):
        self.entries = [self.entry(n, "1Gi", network_mode="host", ports=[{"container": 8123, "host": 8123}]) for n in ("one", "two")]
        self.assertTrue(self.plan()["blocked"])
        self.assertIn("host port", " ".join(self.plan()["reasons"]))

    def test_planned_rwop_claim_is_not_used_by_two_services(self):
        claims = {"data": {"name": "data", "size_gb": 1, "access_mode": "ReadWriteOncePod", "storage_class": "test-class"}}
        self.entries = [self.entry(n) for n in ("one", "two")]
        for entry in self.entries:
            entry["deployment"]["spec"]["template"]["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]
        self.assertTrue(self.plan(claims=claims)["blocked"])

    def test_shared_rwo_must_fit_on_same_node(self):
        self.nodes.append({**copy.deepcopy(self.nodes[0]), "name": "b", "labels": {"kubernetes.io/hostname": "b"}})
        claims = {"data": {"name": "data", "size_gb": 1, "access_mode": "ReadWriteOnce", "storage_class": "test-class"}}
        for entry in self.entries:
            entry["deployment"]["spec"]["template"]["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]
        self.assertTrue(self.plan(claims=claims)["blocked"])
        claims["data"]["access_mode"] = "ReadWriteMany"
        self.assertFalse(self.plan(claims=claims)["blocked"])

    def test_later_service_can_satisfy_earlier_services_required_affinity(self):
        self.entries = [self.entry(n) for n in ("one", "two")]
        self.entries[0]["deployment"]["spec"]["template"]["spec"]["affinity"] = {"podAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": {"app": "two"}},
                                                                "topologyKey": "kubernetes.io/hostname"}]}}
        plan = self.plan()
        self.assertFalse(plan["blocked"], plan)
        self.assertEqual("two", plan["example"][0]["service"])

    def test_budget_exhaustion_is_unknown_and_cannot_be_acknowledged_away(self):
        plan = self.plan(budget=0)
        self.assertEqual("unknown", plan["status"])
        self.assertTrue(plan["blocked"])

    def test_memory_upper_estimate_adds_limits_not_only_requests(self):
        self.entries = [self.entry(n, memory="1Gi", memory_limit="4Gi") for n in ("one", "two")]
        plan = self.plan()
        self.assertFalse(plan["blocked"])
        self.assertEqual(9, plan["nodes"][0]["upper_gb"])
        self.assertIn("112.5%", " ".join(plan["warnings"]))

    def test_unknown_live_memory_stays_unknown(self):
        self.entries = [self.entry("one")]
        self.nodes[0]["mem_metrics_available"] = False
        plan = self.plan()
        self.assertIsNone(plan["nodes"][0]["upper_percent"])
        self.assertIn("unavailable", " ".join(plan["warnings"]))


if __name__ == "__main__":
    unittest.main()
