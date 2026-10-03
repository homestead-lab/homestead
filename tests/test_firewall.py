import copy
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_firewall as F
import homestead_route_policy as POLICY


class FirewallTests(unittest.TestCase):
    def setUp(self):
        self.workload = {"metadata": {"namespace": "apps", "name": "web", "uid": "workload-1"},
                         "spec": {"selector": {"matchLabels": {"app": "web"}}, "template": {
                             "metadata": {"labels": {"app": "web"}}, "spec": {}}}}
        self.pod = {"metadata": {"namespace": "apps", "name": "web-pod", "uid": "pod-1", "labels": {"app": "web"}}, "spec": {}}
        self.policies = []
        self.sent = []
        self.cfg = {"namespace": "apps", "name": "homestead-fw-web",
                    "target": {"namespace": "apps", "name": "web", "kind": "Deployment", "uid": "workload-1"},
                    "ingress": "restricted", "egress": "unchanged", "ingress_rules": [], "egress_rules": [], "allow_dns": False}
        F.bind(self.get, lambda *args: self.sent.append(args), lambda: {"distribution": "k3s"}, "lab")

    def get(self, path):
        if path == "/apis/apps/v1/namespaces/apps/deployments/web":
            return copy.deepcopy(self.workload)
        if path == "/api/v1/namespaces/apps/pods":
            return {"items": [copy.deepcopy(self.pod)]}
        if path in (F.POLICIES, F._path("apps")):
            return {"items": copy.deepcopy(self.policies)}
        if path == "/apis/apps/v1/namespaces/kube-system/daemonsets":
            return {"items": []}
        if path.startswith(F._path("apps") + "/"):
            for policy in self.policies:
                if policy["metadata"]["name"] == path.rsplit("/", 1)[-1]:
                    return copy.deepcopy(policy)
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        if path == "/api/v1/namespaces":
            return {"items": [{"metadata": {"name": "apps"}}]}
        if path == "/apis/apps/v1/deployments":
            return {"items": [copy.deepcopy(self.workload)]}
        return {"items": []}

    def existing(self):
        obj = F.preview(self.cfg)["manifest"]
        obj["metadata"].update(uid="policy-1", resourceVersion="10", annotations={**obj["metadata"]["annotations"], "other": "keep"})
        self.policies.append(obj)
        self.cfg.update(uid="policy-1", resource_version="10")
        return obj

    def test_default_deny_ingress_does_not_isolate_egress(self):
        plan = F.preview(self.cfg)
        self.assertEqual({"podSelector": {"matchLabels": {"app": "web"}}, "policyTypes": ["Ingress"], "ingress": []}, plan["manifest"]["spec"])
        self.assertEqual(["web-pod"], plan["pods"])
        self.assertFalse(self.sent)

    def test_namespace_and_cidr_peers_and_protocols(self):
        self.cfg["ingress_rules"] = [{"peer": "namespace", "value": "trusted", "protocol": "TCP", "ports": "80, 443"},
                                     {"peer": "cidr", "value": "2001:db8::1", "protocol": "UDP", "ports": "53"},
                                     {"peer": "any", "value": "", "protocol": "SCTP", "ports": ""}]
        rules = F.preview(self.cfg)["manifest"]["spec"]["ingress"]
        self.assertEqual({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "trusted"}}}, rules[0]["from"][0])
        self.assertEqual([{"protocol": "TCP", "port": 80}, {"protocol": "TCP", "port": 443}], rules[0]["ports"])
        self.assertEqual("2001:db8::1/128", rules[1]["from"][0]["ipBlock"]["cidr"])
        self.assertNotIn("from", rules[2])
        self.assertEqual([{"protocol": "SCTP"}], rules[2]["ports"])

    def test_dns_is_narrow_and_egress_only(self):
        self.cfg.update(egress="restricted", allow_dns=True)
        spec = F.preview(self.cfg)["manifest"]["spec"]
        dns = spec["egress"][0]
        self.assertEqual(["Ingress", "Egress"], spec["policyTypes"])
        self.assertEqual({"k8s-app": "kube-dns"}, dns["to"][0]["podSelector"]["matchLabels"])
        self.assertIn("namespaceSelector", dns["to"][0])
        self.assertEqual([{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}], dns["ports"])
        self.cfg["egress"] = "unchanged"
        with self.assertRaises(ValueError): F.preview(self.cfg)

    def test_invalid_rules_never_write(self):
        for change in ({"ports": "0"}, {"ports": "65536"}, {"ports": "80-90"}, {"ports": "http"},
                       {"protocol": "Any", "ports": "80"}, {"protocol": "ICMP"},
                       {"peer": "cidr", "value": "not-an-ip"}, {"peer": "namespace", "value": "../../lab"},
                       {"peer": "unexpected"}, {"except": ["10.0.0.0/8"]}):
            self.cfg["ingress_rules"] = [{"peer": "any", "value": "", "protocol": "TCP", "ports": "80", **change}]
            with self.subTest(change=change), self.assertRaises(ValueError): F.preview(self.cfg)
        self.assertFalse(self.sent)

    def test_requires_review_and_rechecks_rules(self):
        with self.assertRaisesRegex(ValueError, "review"): F.save(self.cfg)
        self.cfg["review"] = F.preview(self.cfg)["review"]
        self.cfg["ingress_rules"] = [{"peer": "any", "protocol": "Any", "ports": ""}]
        with self.assertRaisesRegex(ValueError, "review"): F.save(self.cfg)
        self.assertFalse(self.sent)

    def test_non_text_rule_fields_cannot_widen_access(self):
        for field in ("ports", "value"):
            for value in (0, 80, False, None, [], {}):
                self.cfg["ingress_rules"] = [{"peer": "any", "protocol": "TCP", field: value}]
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "must be text"):
                    F.preview(self.cfg)
        self.assertFalse(self.sent)
        self.cfg["ingress_rules"] = [{"peer": "any", "protocol": "TCP", "ports": ""}]
        self.assertEqual([{"ports": [{"protocol": "TCP"}]}], F.preview(self.cfg)["manifest"]["spec"]["ingress"])

    def test_overlap_and_pod_changes_invalidate_review(self):
        self.cfg["review"] = F.preview(self.cfg)["review"]
        self.policies.append({"metadata": {"name": "external", "namespace": "apps", "uid": "x", "resourceVersion": "1"},
                              "spec": {"podSelector": {}, "ingress": [{}], "policyTypes": ["Ingress"]}})
        with self.assertRaisesRegex(ValueError, "review"): F.save(self.cfg)
        self.assertEqual(["external"], F.preview(self.cfg)["overlapping"])
        self.cfg["review"] = F.preview(self.cfg)["review"]
        self.pod["metadata"]["uid"] = "new-pod"
        with self.assertRaisesRegex(ValueError, "review"): F.save(self.cfg)

    def test_review_ignores_pod_status_churn(self):
        self.cfg["review"] = F.preview(self.cfg)["review"]
        self.pod["status"] = {"phase": "Running"}
        self.pod["metadata"]["resourceVersion"] = "200"
        F.save(self.cfg)
        self.assertEqual("POST", self.sent[0][0])

    def test_pod_label_change_affecting_overlap_requires_review(self):
        self.policies.append({"metadata": {"namespace": "apps", "name": "public", "uid": "other", "resourceVersion": "1"},
                              "spec": {"podSelector": {"matchLabels": {"tier": "public"}}, "policyTypes": ["Ingress"], "ingress": [{}]}})
        self.cfg["review"] = F.preview(self.cfg)["review"]
        self.pod["metadata"]["labels"]["tier"] = "public"
        self.assertEqual(["public"], F.preview(self.cfg)["overlapping"])
        with self.assertRaisesRegex(ValueError, "review"): F.save(self.cfg)

    def test_homestead_is_protected_in_its_real_install_namespace(self):
        F.bind(self.get, lambda *args: self.sent.append(args), lambda: {}, "management")
        self.assertTrue(F._protected("management", {"app": "homestead"}))

    def test_update_preserves_metadata_and_uses_kubernetes_concurrency(self):
        self.existing()
        self.cfg["review"] = F.preview(self.cfg)["review"]
        F.save(self.cfg)
        method, path, obj = self.sent[-1]
        self.assertEqual("PUT", method)
        self.assertEqual("10", obj["metadata"]["resourceVersion"])
        self.assertEqual("keep", obj["metadata"]["annotations"]["other"])
        self.assertEqual(F._path("apps", self.cfg["name"]), path)

    def test_default_deny_remains_editable_after_api_omits_empty_lists(self):
        obj = self.existing()
        del obj["spec"]["ingress"]
        self.assertTrue(F.inventory()["policies"][0]["managed"])
        self.cfg["review"] = F.preview(self.cfg)["review"]
        F.save(self.cfg)
        self.assertEqual("PUT", self.sent[0][0])

    def test_dns_only_egress_keeps_default_deny_editable_after_api_roundtrip(self):
        self.cfg.update(egress="restricted", allow_dns=True)
        obj = self.existing()
        obj["spec"].pop("ingress")
        self.assertTrue(F._editable(obj))
        F.remove(self.cfg)
        self.assertEqual("DELETE", self.sent[0][0])

    def test_external_or_changed_specs_cannot_be_edited_or_removed(self):
        obj = self.existing()
        obj["spec"]["ingress"] = [{}]
        self.assertFalse(F.inventory()["policies"][0]["managed"])
        with self.assertRaisesRegex(ValueError, "managed elsewhere"): F.preview(self.cfg)
        with self.assertRaisesRegex(ValueError, "managed elsewhere"): F.remove(self.cfg)
        self.assertFalse(self.sent)

    def test_stale_identity_and_name_collision_rejected(self):
        obj = self.existing()
        self.cfg.pop("uid")
        with self.assertRaisesRegex(ValueError, "already exists"): F.preview(self.cfg)
        self.cfg["uid"] = "policy-1"
        obj["metadata"]["resourceVersion"] = "11"
        with self.assertRaisesRegex(ValueError, "changed"): F.remove(self.cfg)
        self.cfg["resource_version"] = "11"
        self.workload["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(ValueError, "replaced"): F.preview(self.cfg)

    def test_malformed_stored_targets_are_inspect_only(self):
        obj = self.existing()
        config = json.loads(obj["metadata"]["annotations"][F.CONFIG])
        valid = config["target"]
        for target in (None, [], {}, {**valid, "kind": []}, {**valid, "kind": "Pod"},
                       {**valid, "namespace": "other"}, {**valid, "uid": ""}, {**valid, "name": "../web"}):
            config["target"] = target
            obj["metadata"]["annotations"][F.CONFIG] = json.dumps(config)
            with self.subTest(target=target):
                row = F.inventory()["policies"][0]
                self.assertFalse(row["managed"])
                self.assertIsNone(row["config"])
                self.assertEqual(obj["spec"], row["spec"])
                with self.assertRaisesRegex(ValueError, "managed elsewhere"): F.preview(self.cfg)
                with self.assertRaisesRegex(ValueError, "managed elsewhere"): F.remove(self.cfg)
        del config["target"]
        obj["metadata"]["annotations"][F.CONFIG] = json.dumps(config)
        self.assertFalse(F.inventory()["policies"][0]["managed"])
        self.assertFalse(self.sent)

    def test_delete_has_uid_and_resource_version_preconditions(self):
        self.existing()
        F.remove(self.cfg)
        self.assertEqual("DELETE", self.sent[0][0])
        self.assertEqual({"uid": "policy-1", "resourceVersion": "10"}, self.sent[0][2]["preconditions"])

    def test_host_and_platform_pods_cannot_be_selected(self):
        self.workload["spec"]["template"]["spec"]["hostNetwork"] = True
        with self.assertRaisesRegex(ValueError, "host networking"): F.preview(self.cfg)
        self.workload["spec"]["template"]["spec"] = {}
        self.pod["spec"]["hostNetwork"] = True
        with self.assertRaisesRegex(ValueError, "host-network"): F.preview(self.cfg)
        self.assertTrue(F._protected("lab", {"app": "homestead"}))
        self.assertTrue(F._protected("kube-system", {}))

    def test_deleting_workload_cannot_be_configured(self):
        self.workload["metadata"]["deletionTimestamp"] = "2026-10-03T12:00:00Z"
        with self.assertRaisesRegex(ValueError, "being deleted"): F.preview(self.cfg)

    def test_vm_scope_uses_stable_name_label_and_excludes_lan_only(self):
        vm = copy.deepcopy(self.workload)
        vm["spec"]["template"]["spec"] = {"networks": [{"name": "lan", "multus": {"networkName": "lan"}}]}
        self.assertIn("no pod-network", F._target(vm, "VirtualMachine")["blocked"])
        vm["spec"]["template"]["spec"]["networks"].append({"name": "pod", "pod": {}})
        row = F._target(vm, "VirtualMachine")
        self.assertFalse(row["blocked"])
        self.assertTrue(row["warnings"])
        self.assertEqual({"matchLabels": {"vm.kubevirt.io/name": "web"}}, row["selector"])
        self.assertEqual("web", row["labels"]["vm.kubevirt.io/name"])
        vm["spec"]["template"]["metadata"]["labels"] = {}
        self.assertFalse(F._target(vm, "VirtualMachine")["blocked"])

    def test_vm_selector_does_not_include_other_vms_with_shared_template_labels(self):
        vm = copy.deepcopy(self.workload)
        row = F._target(vm, "VirtualMachine")
        self.assertTrue(F.matches(row["selector"], {"app": "web", "vm.kubevirt.io/name": "web"}))
        self.assertFalse(F.matches(row["selector"], {"app": "web", "vm.kubevirt.io/name": "other"}))
        vm["spec"]["template"]["metadata"]["labels"] = {"app": "changed"}
        self.assertEqual(row["selector"], F._target(vm, "VirtualMachine")["selector"])
        self.assertTrue(F.matches(F._target(vm, "VirtualMachine")["selector"],
                                  {"app": "web", "vm.kubevirt.io/name": "web"}))

    def test_selectors_include_expressions(self):
        selector = {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["web"]}]}
        self.assertTrue(F.matches(selector, {"tier": "web"}))
        self.assertFalse(F.matches(selector, {}))
        self.workload["spec"]["selector"] = selector
        self.assertEqual(selector, F.preview(self.cfg)["manifest"]["spec"]["podSelector"])

    def test_failed_inventory_reads_do_not_look_like_no_policy(self):
        def denied(path): raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
        F.kget = denied
        with self.assertRaises(urllib.error.HTTPError): F.inventory()

    def test_permissions_require_admin_for_every_mutation(self):
        self.assertEqual("viewer", POLICY.role("/api/firewall", "GET"))
        for suffix in ("preview", "save", "delete"):
            self.assertEqual("admin", POLICY.role("/api/firewall/" + suffix, "POST"))

    def test_workload_template_changes_require_a_new_review(self):
        self.cfg["review"] = F.preview(self.cfg)["review"]
        self.workload["spec"]["template"]["metadata"]["labels"]["tier"] = "public"
        with self.assertRaisesRegex(ValueError, "review"): F.save(self.cfg)

    def test_same_namespace_policy_cannot_move_to_a_different_target(self):
        obj = self.existing()
        stored = json.loads(obj["metadata"]["annotations"][F.CONFIG])
        stored["target"]["uid"] = "old-workload"
        obj["metadata"]["annotations"][F.CONFIG] = json.dumps(stored)
        with self.assertRaisesRegex(ValueError, "cannot be moved"): F.preview(self.cfg)

    def test_nonmatching_policy_is_still_part_of_review_snapshot(self):
        self.policies.append({"metadata": {"namespace": "apps", "name": "another", "uid": "other", "resourceVersion": "1"},
                              "spec": {"podSelector": {"matchLabels": {"app": "other"}}, "policyTypes": ["Ingress"], "ingress": []}})
        self.cfg["review"] = F.preview(self.cfg)["review"]
        self.assertFalse(F.preview(self.cfg)["overlapping"])
        self.policies[0]["metadata"]["resourceVersion"] = "2"
        self.policies[0]["spec"]["podSelector"] = {}
        with self.assertRaisesRegex(ValueError, "review"): F.save(self.cfg)


if __name__ == "__main__":
    unittest.main()
