import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_topology as topology
import homestead_place as place


def pod(name="peer", host="a", labels=None, namespace="lab"):
    return {"metadata": {"name": name, "namespace": namespace, "labels": labels if labels is not None else {"app": "demo"}},
            "spec": {"nodeName": host, "containers": []}, "status": {"phase": "Running"}}


def rule(key="zone", **kwargs):
    return {"topologyKey": key, "labelSelector": {"matchLabels": {"app": "demo"}}, **kwargs}


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [{"name": name, "labels": {"zone": name, "kubernetes.io/hostname": name},
                       "status": "Ready", "schedulable": True, "hardware": {},
                       "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "100"},
                       "mem_cap_gb": 8, "mem_used_gb": 1, "mem_metrics_available": True} for name in ("a", "b")]
        self.template = {"metadata": {"labels": {"app": "demo"}}, "spec": {"containers": [
            {"name": "demo", "resources": {"requests": {"memory": "1Gi"}, "limits": {"memory": "1Gi"}}}]}}
        self.spec = self.template["spec"]
        self.pods = []
        self.get = mock.Mock(return_value={"metadata": {"name": "peer", "labels": {"team": "blue"}}})

    def affinity(self, kind, *rules):
        self.spec.setdefault("affinity", {})[kind] = {topology.REQUIRED: list(rules)}

    def spread(self, **kwargs):
        self.spec["topologySpreadConstraints"] = [rule(maxSkew=1, whenUnsatisfiable="DoNotSchedule", **kwargs)]

    def snapshot(self):
        return topology.Snapshot(self.template, "lab", self.nodes, self.pods, self.get,
                                 place._required_affinity_matches, place._tolerates)

    def check(self, host=0):
        return self.snapshot().check(self.nodes[host])

    def plan(self, count=1):
        dep = {"spec": {"replicas": 0, "template": self.template}}
        def get(path):
            return {"items": self.pods} if path == "/api/v1/pods" else dep
        with mock.patch.object(place, "kget", side_effect=get), mock.patch.object(place, "get_nodes", return_value=self.nodes), \
                mock.patch.object(place, "hardware_features", return_value=[]):
            return place.start_plan("lab", "demo", count)

    def test_required_affinity_rejects_other_domain(self):
        self.affinity("podAffinity", rule())
        self.pods = [pod()]
        self.assertFalse(self.check()[0])
        self.assertIn("no matching peer", self.check(1)[0][0])

    def test_first_self_affinity_pod_bootstraps(self):
        self.affinity("podAffinity", rule())
        self.assertEqual(([], []), self.check())

    def test_missing_affinity_topology_label_blocks_even_bootstrap(self):
        self.affinity("podAffinity", rule())
        self.nodes[0]["labels"] = {}
        self.assertTrue(self.check()[0])

    def test_nonself_affinity_needs_peer(self):
        self.affinity("podAffinity", rule(labelSelector={"matchLabels": {"app": "database"}}))
        self.assertTrue(self.check()[0])

    def test_all_affinity_terms_must_match_one_peer(self):
        self.affinity("podAffinity", rule(), rule(labelSelector={"matchLabels": {"role": "db"}}))
        self.pods = [pod(), pod(labels={"role": "db"})]
        self.assertTrue(self.check()[0])
        self.pods[0]["metadata"]["labels"]["role"] = "db"
        self.assertFalse(self.check()[0])

    def test_anti_affinity_defaults_to_same_namespace(self):
        self.affinity("podAntiAffinity", rule())
        self.pods = [pod(namespace="peer")]
        self.assertFalse(self.check()[0])
        self.pods[0]["metadata"]["namespace"] = "lab"
        self.assertTrue(self.check()[0])

    def test_explicit_namespace_union_with_selector(self):
        self.affinity("podAntiAffinity", rule(namespaces=["explicit"], namespaceSelector={"matchLabels": {"team": "blue"}}))
        self.pods = [pod(namespace="explicit")]
        self.assertTrue(self.check()[0])
        self.get.assert_not_called()
        self.pods = [pod(namespace="peer")]
        self.assertTrue(self.check()[0])
        self.get.assert_called_once()

    def test_empty_namespace_selector_includes_all_without_api(self):
        self.affinity("podAntiAffinity", rule(namespaceSelector={}))
        self.pods = [pod(namespace="elsewhere")]
        self.assertTrue(self.check()[0])
        self.get.assert_not_called()

    def test_namespace_failure_is_unknown_not_blocked(self):
        self.affinity("podAntiAffinity", rule(namespaceSelector={"matchLabels": {"team": "blue"}}))
        self.pods = [pod(namespace="peer")]
        self.get.side_effect = OSError("offline")
        reasons, warnings = self.check()
        self.assertFalse(reasons)
        self.assertTrue(warnings)

    def test_existing_pod_anti_affinity_protects_it(self):
        peer = pod()
        peer["spec"]["affinity"] = {"podAntiAffinity": {topology.REQUIRED: [rule()]}}
        self.pods = [peer]
        self.assertIn("existing pod lab/peer", self.check()[0][0])
        self.assertFalse(self.check(1)[0])

    def test_match_label_keys(self):
        self.affinity("podAntiAffinity", rule(matchLabelKeys=["revision"]))
        self.template["metadata"]["labels"]["revision"] = "new"
        self.pods = [pod(labels={"app": "demo", "revision": "old"})]
        self.assertFalse(self.check()[0])
        self.pods[0]["metadata"]["labels"]["revision"] = "new"
        self.assertTrue(self.check()[0])

    def test_generated_revision_is_unknown_before_controller_creates_pod(self):
        self.affinity("podAntiAffinity", rule(matchLabelKeys=["pod-template-hash"]))
        self.pods = [pod()]
        self.assertTrue(self.check()[1])
        self.assertFalse(self.check()[0])

    def test_mismatch_label_keys(self):
        self.affinity("podAntiAffinity", rule(mismatchLabelKeys=["tenant"]))
        self.template["metadata"]["labels"]["tenant"] = "mine"
        self.pods = [pod(labels={"app": "demo", "tenant": "yours"})]
        self.assertTrue(self.check()[0])
        self.pods[0]["metadata"]["labels"]["tenant"] = "mine"
        self.assertFalse(self.check()[0])

    def test_null_selector_matches_none_empty_matches_all(self):
        self.affinity("podAntiAffinity", rule(labelSelector=None))
        self.pods = [pod()]
        self.assertFalse(self.check()[0])
        self.affinity("podAntiAffinity", rule(labelSelector={}))
        self.assertTrue(self.check()[0])

    def test_completed_ignored_terminating_retained_for_affinity(self):
        self.affinity("podAntiAffinity", rule())
        self.pods = [pod()]
        self.pods[0]["status"]["phase"] = "Succeeded"
        self.assertFalse(self.check()[0])
        self.pods[0]["status"]["phase"] = "Running"
        self.pods[0]["metadata"]["deletionTimestamp"] = "now"
        self.assertTrue(self.check()[0])

    def test_missing_inventory_or_host_is_unknown(self):
        self.affinity("podAffinity", rule())
        for pods in (None, [pod(host="gone")]):
            self.pods = pods
            self.assertFalse(self.check()[0])
            self.assertTrue(self.check()[1])

    def test_direct_assignment_and_custom_scheduler_not_false_blocks(self):
        self.affinity("podAffinity", rule(labelSelector={"matchLabels": {"app": "db"}}))
        self.spec["nodeName"] = "a"
        self.assertFalse(self.check()[0])
        self.assertTrue(self.check()[1])
        del self.spec["nodeName"]
        self.spec["schedulerName"] = "custom"
        self.assertFalse(self.check()[0])
        self.assertTrue(self.check()[1])

    def test_soft_rules_do_not_block(self):
        self.spec["affinity"] = {"podAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
            {"weight": 100, "podAffinityTerm": rule()}]}}
        self.spread()
        self.spec["topologySpreadConstraints"][0]["whenUnsatisfiable"] = "ScheduleAnyway"
        self.assertEqual(([], []), self.check())

    def test_spread_rejects_crowded_domain(self):
        self.spread()
        self.pods = [pod()]
        self.assertIn("skew 2", self.check()[0][0])
        self.assertFalse(self.check(1)[0])

    def test_spread_only_same_namespace_and_nondeleting_pods(self):
        self.spread()
        self.pods = [pod(namespace="other"), pod()]
        self.pods[1]["metadata"]["deletionTimestamp"] = "now"
        self.assertFalse(self.check()[0])

    def test_spread_min_domains_uses_zero(self):
        self.spread(minDomains=3)
        self.pods = [pod(), pod(host="b")]
        self.assertTrue(self.check()[0])
        self.spec["topologySpreadConstraints"][0]["minDomains"] = 2
        self.assertFalse(self.check()[0])

    def test_spread_missing_topology_label(self):
        self.spread()
        del self.nodes[0]["labels"]["zone"]
        self.assertTrue(self.check()[0])

    def test_spread_honors_node_selector_by_default(self):
        self.spread()
        self.spec["nodeSelector"] = {"zone": "a"}
        self.pods = [pod()]
        self.assertFalse(self.check()[0])
        self.spec["topologySpreadConstraints"][0]["nodeAffinityPolicy"] = "Ignore"
        self.assertTrue(self.check()[0])

    def test_spread_taints_ignored_by_default_but_honor_supported(self):
        self.spread()
        self.nodes[1]["taints"] = [{"key": "dedicated", "effect": "NoSchedule"}]
        self.pods = [pod()]
        self.assertTrue(self.check()[0])
        self.spec["topologySpreadConstraints"][0]["nodeTaintsPolicy"] = "Honor"
        self.assertFalse(self.check()[0])

    def test_batch_rechecks_spread_after_each_replica(self):
        self.spread()
        self.pods = [pod()]
        plan = self.plan(3)
        self.assertFalse(plan["blocked"])
        self.assertEqual("fits", plan["topology_status"])
        self.assertFalse(plan["candidates"][0]["eligible"])
        self.assertTrue(plan["candidates"][1]["eligible"])

    def test_batch_anti_affinity_limits_one_per_domain(self):
        self.affinity("podAntiAffinity", rule())
        plan = self.plan(3)
        self.assertTrue(plan["blocked"])
        self.assertEqual(2, plan["resource_slots"])
        self.assertFalse(self.plan(2)["blocked"])

    def test_later_spread_host_keeps_its_memory_warning(self):
        self.spread()
        self.pods = [pod()]
        self.nodes[0]["mem_used_gb"] = 7
        result = self.plan(3)
        self.assertFalse(result["candidates"][0]["eligible"])
        self.assertFalse(result["blocked"])
        self.assertTrue(result["requires_confirmation"])
        self.assertIn("projected RAM", " ".join(result["warnings"]))

    def test_batch_same_node_volume_conflicts_with_spread(self):
        self.spread()
        result = self.snapshot().batch({"a": 10, "b": 10}, 3, same_node=True)
        self.assertEqual("blocked", result["status"])
        self.assertEqual(1, result["slots"])

    def test_batch_search_budget_is_unknown_never_blocked(self):
        self.spread()
        self.assertEqual("unknown", self.snapshot().batch({"a": 10, "b": 10}, 3, budget=1)["status"])

    def test_domain_without_resource_slots_still_affects_spread(self):
        self.spread()
        # Three domains, a already ahead. Starting b is a dead end because c
        # has no resource capacity; removing minDomains would change the bound.
        self.nodes.append({**copy.deepcopy(self.nodes[1]), "name": "c", "labels": {"zone": "c"}})
        self.pods = [pod()]
        self.assertEqual("blocked", self.snapshot().batch({"a": 2, "b": 2, "c": 0}, 3)["status"])

    def test_batch_backtracks_from_bad_first_choice(self):
        self.spread()
        self.spec["topologySpreadConstraints"][0]["maxSkew"] = 2
        self.pods = [pod()]
        self.assertEqual("fits", self.snapshot().batch({"a": 2, "b": 2}, 2, same_node=True)["status"])

    def test_two_hosts_in_one_zone_are_not_two_anti_affinity_slots(self):
        self.affinity("podAntiAffinity", rule())
        self.nodes[1]["labels"]["zone"] = "a"
        self.assertTrue(self.plan(2)["blocked"])
        self.assertEqual(1, self.plan(2)["resource_slots"])

    def test_unknown_namespace_in_batch_is_not_reported_as_verified(self):
        self.affinity("podAntiAffinity", rule(namespaceSelector={"matchLabels": {"team": "blue"}}))
        self.pods = [pod(namespace="peer")]
        self.get.side_effect = OSError("offline")
        self.assertEqual("unknown", self.snapshot().batch({"a": 2, "b": 2}, 2)["status"])

    def test_partial_topology_search_needs_confirmation_but_is_not_a_hard_block(self):
        self.spread()
        with mock.patch.object(topology.Snapshot, "batch", return_value={"status": "unknown", "slots": 1, "search_exhausted": True}):
            result = self.plan(3)
        self.assertFalse(result["blocked"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual("unknown", result["topology_status"])

    def test_selectors_support_set_and_existence_operators(self):
        for operator, labels, expected in [("In", {"tier": "db"}, True), ("NotIn", {}, True),
                                           ("Exists", {}, False), ("DoesNotExist", {}, True)]:
            selector = {"matchExpressions": [{"key": "tier", "operator": operator, "values": ["db"]}]}
            self.assertEqual(expected, topology.selected(selector, labels))

    def test_multiple_spread_constraints_require_all_topology_keys(self):
        self.spread()
        self.spec["topologySpreadConstraints"].append(rule(key="rack", maxSkew=1, whenUnsatisfiable="DoNotSchedule"))
        self.nodes[0]["labels"]["rack"] = "r1"
        self.pods = [pod()]
        self.assertFalse(self.check()[0])  # b lacks rack, so does not add a zone domain.
        self.assertTrue(self.check(1)[0])


if __name__ == "__main__":
    unittest.main()
