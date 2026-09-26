import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_place as place
import homestead_dependencies as deps


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.spec = {"containers": [{"name": "app", "resources": {"requests": {"memory": "1Gi"}, "limits": {"memory": "2Gi"}}}], "volumes": []}
        self.dep = {"spec": {"replicas": 0, "template": {"spec": self.spec}}}
        self.nodes = [{"name": name, "status": "Ready", "schedulable": True, "hardware": {}, "labels": {"zone": zone},
                       "allocatable": {"memory": "4Gi", "cpu": "4", "pods": "10"}, "mem_cap_gb": 4, "mem_used_gb": 0.1,
                       "mem_metrics_available": True} for name, zone in (("a", "west"), ("b", "east"))]
        self.pods = []
        self.objects = {}
        self.sent = []

    def get(self, path):
        if "/deployments/" in path:
            return copy.deepcopy(self.dep)
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.objects[path])

    def plan(self, replicas=1):
        with mock.patch.object(place, "kget", side_effect=self.get), \
                mock.patch.object(place, "ksend") as send, \
                mock.patch.object(place, "hardware_features", return_value=[]), \
                mock.patch.object(place, "get_nodes", return_value=copy.deepcopy(self.nodes)):
            result = place.start_plan("lab", "app", replicas)
        send.assert_not_called()
        return result

    def claim(self, modes=None):
        self.spec["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]
        pvc = {"metadata": {"name": "data", "uid": "claim-uid"}, "spec": {
            "volumeName": "pv-data", "accessModes": modes or ["ReadWriteMany"]}, "status": {"phase": "Bound"}}
        pv = {"metadata": {"name": "pv-data"}, "spec": {"claimRef": {"name": "data", "namespace": "lab", "uid": "claim-uid"}}, "status": {"phase": "Bound"}}
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"] = pvc
        self.objects["/api/v1/persistentvolumes/pv-data"] = pv
        return pvc, pv

    def consumer(self, node="a", phase="Running", namespace="lab"):
        self.pods.append({"metadata": {"name": "consumer", "namespace": namespace}, "spec": {
            "nodeName": node, "containers": [], "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}, "status": {"phase": phase}})

    def port(self, number=8080, protocol="TCP", ip=""):
        self.spec["containers"][0]["ports"] = [{"containerPort": number, "hostPort": number, "protocol": protocol, "hostIP": ip}]

    def eligible(self, plan):
        return [n["name"] for n in plan["candidates"] if n["eligible"]]

    def test_host_port_conflict_excludes_only_the_occupied_host(self):
        self.port()
        self.pods = [{"metadata": {"name": "server", "namespace": "lab"}, "spec": {**copy.deepcopy(self.spec), "nodeName": "a"}}]
        plan = self.plan()
        self.assertEqual(["b"], self.eligible(plan))
        self.assertIn("host port TCP/8080", " ".join(plan["candidates"][0]["reasons"]))

    def test_host_ports_limit_new_replicas_to_one_per_host(self):
        self.port()
        self.assertFalse(self.plan(2)["blocked"])
        self.assertTrue(self.plan(3)["blocked"])

    def test_protocol_and_explicit_host_ips_are_distinct(self):
        self.assertFalse(deps.conflict(("1.2.3.4", "TCP", 53), ("1.2.3.4", "UDP", 53)))
        self.assertFalse(deps.conflict(("1.2.3.4", "TCP", 53), ("1.2.3.5", "TCP", 53)))
        self.assertTrue(deps.conflict(("0.0.0.0", "TCP", 53), ("1.2.3.5", "TCP", 53)))

    def test_only_restartable_init_ports_are_permanent_reservations(self):
        init = {"ports": [{"hostPort": 8000}]}
        self.assertEqual(set(), deps.host_ports({"initContainers": [init]}))
        init["restartPolicy"] = "Always"
        self.assertEqual({("0.0.0.0", "TCP", 8000)}, deps.host_ports({"initContainers": [init]}))

    def test_terminal_pods_do_not_hold_ports_terminating_pods_do(self):
        self.port()
        self.pods = [{"metadata": {"name": "server", "namespace": "lab", "deletionTimestamp": "now"},
                      "spec": {**copy.deepcopy(self.spec), "nodeName": "a"}, "status": {"phase": "Running"}}]
        self.assertEqual(["b"], self.eligible(self.plan()))
        self.pods[0]["status"]["phase"] = "Succeeded"
        self.assertEqual(["a", "b"], self.eligible(self.plan()))

    def test_bound_volume_affinity_rules_out_wrong_zone(self):
        _, pv = self.claim()
        pv["spec"]["nodeAffinity"] = {"required": {"nodeSelectorTerms": [{"matchExpressions": [
            {"key": "zone", "operator": "In", "values": ["east"]}]}]}}
        self.assertEqual(["b"], self.eligible(self.plan()))

    def test_bound_pv_missing_or_foreign_claim_blocks_without_mutation(self):
        _, pv = self.claim()
        pv["spec"]["claimRef"]["uid"] = "another-claim"
        self.assertTrue(self.plan()["blocked"])
        del self.objects["/api/v1/persistentvolumes/pv-data"]
        self.assertTrue(self.plan()["blocked"])

    def test_lost_deleting_and_missing_claims_block(self):
        pvc, _ = self.claim()
        pvc["status"]["phase"] = "Lost"
        self.assertTrue(self.plan()["blocked"])
        pvc["status"]["phase"] = "Bound"
        pvc["metadata"]["deletionTimestamp"] = "now"
        self.assertTrue(self.plan()["blocked"])
        del self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"]
        self.assertTrue(self.plan()["blocked"])

    def test_rwo_allows_same_host_but_not_a_second_host(self):
        self.claim(["ReadWriteOnce"])
        self.consumer()
        self.assertEqual(["a"], self.eligible(self.plan()))

    def test_rwo_replicas_cannot_pool_capacity_from_multiple_hosts(self):
        self.claim(["ReadWriteOnce"])
        plan = self.plan(5)
        self.assertEqual(4, plan["resource_slots"])
        self.assertTrue(plan["blocked"])
        self.assertIn("fit together on one host", " ".join(plan["warnings"]))

    def test_rwo_with_host_ports_cannot_place_two_replicas(self):
        self.claim(["ReadWriteOnce"])
        self.port()
        self.assertTrue(self.plan(2)["blocked"])

    def test_rwop_rejects_existing_unscheduled_consumer_and_two_new_replicas(self):
        self.claim(["ReadWriteOncePod"])
        self.assertTrue(self.plan(2)["blocked"])
        self.consumer(node="", phase="Pending")
        self.assertTrue(self.plan()["blocked"])

    def test_same_named_claim_in_other_namespace_does_not_conflict(self):
        self.claim(["ReadWriteOncePod"])
        self.consumer(namespace="other")
        self.assertFalse(self.plan()["blocked"])

    def test_wait_for_first_consumer_is_not_a_deadlock_and_checks_topology(self):
        pvc, _ = self.claim()
        pvc["spec"].pop("volumeName")
        pvc["spec"]["storageClassName"] = "local-path"
        pvc["status"]["phase"] = "Pending"
        self.objects["/apis/storage.k8s.io/v1/storageclasses/local-path"] = {"metadata": {"name": "local-path"},
            "volumeBindingMode": "WaitForFirstConsumer", "allowedTopologies": [{"matchLabelExpressions": [{"key": "zone", "values": ["west"]}]}]}
        plan = self.plan()
        self.assertFalse(plan["blocked"])
        self.assertEqual(["a"], self.eligible(plan))
        self.assertTrue(plan["requires_confirmation"])
        self.spec["nodeName"] = "a"
        self.assertTrue(self.plan()["blocked"])

    def test_api_failure_is_unknown_not_missing_and_does_not_hide_other_constraints(self):
        self.claim()
        self.port()
        original = self.get
        def failed(path):
            if "persistentvolumeclaims" in path:
                raise urllib.error.HTTPError(path, 403, "denied", {}, None)
            return original(path)
        with mock.patch.object(self, "get", side_effect=failed):
            self.assertTrue(self.plan()["requires_confirmation"])
            self.assertTrue(self.plan(3)["blocked"])


if __name__ == "__main__":
    unittest.main()
