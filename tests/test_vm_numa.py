import unittest

import test_vm_resources as fixtures
import homestead_vm_numa as numa
import homestead_vm_resources as resources


class NUMAPolicyTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.spec = self.vm["spec"]["template"]["spec"]
        self.cpu = self.spec["domain"]["cpu"]
        self.cpu.update(numa={"guestMappingPassthrough": {}}, dedicatedCpuPlacement=True)
        self.spec["domain"]["memory"]["hugepages"] = {"pageSize": "2Mi"}

    def policy(self, version="v1.9.0", config=None):
        return numa.policy(self.spec, config or {}, version)

    def test_valid_mapping_is_not_claimed_as_allocated_capacity(self):
        result = self.policy()
        self.assertFalse(result["blockers"])
        self.assertIn("do not prove", " ".join(result["warnings"]))

    def test_dedicated_cpu_and_hugepages_are_both_required(self):
        self.cpu["dedicatedCpuPlacement"] = False
        self.spec["domain"]["memory"].pop("hugepages")
        result = resources.project(self.vm, {}, kubevirt_version="v1.9.0")
        self.assertIn("requires dedicated", str(result["blockers"]))
        self.assertIn("requires hugepage", str(result["blockers"]))

    def test_feature_gate_before_ga_and_unknown_versions(self):
        self.assertIn("feature gate", str(self.policy("v1.3.1")["blockers"]))
        self.assertFalse(self.policy("v1.3.1", {"developerConfiguration": {"featureGates": ["NUMA"]}})["blockers"])
        self.assertFalse(self.policy("v1.4.0")["blockers"])
        self.assertFalse(self.policy("v1.9.0", {"developerConfiguration": {"disabledFeatureGates": ["NUMA"]}})["blockers"])
        for version in (None, "v1.9.0-vendor", "v1.10.0"):
            self.assertIn("unverified", str(self.policy(version)["blockers"]))

    def test_invalid_policy_never_echoes_unknown_fields(self):
        for value in ([], "private-policy", {"private-policy": {}}, {"guestMappingPassthrough": "private-policy"},
                      {"guestMappingPassthrough": {"private-policy": True}}):
            self.cpu["numa"] = value
            result = self.policy()
            self.assertTrue(result["blockers"])
            self.assertNotIn("private-policy", str(result))

    def test_missing_policy_does_not_require_feature_gate(self):
        for value in (None, {}, {"guestMappingPassthrough": None}):
            self.cpu["numa"] = value
            self.assertFalse(self.policy(None)["blockers"])

    def test_emulation_is_not_dedicated_hardware(self):
        self.assertIn("emulation", str(self.policy(config={"developerConfiguration": {"useEmulation": True}})["blockers"]))

    def test_invalid_feature_gate_shapes_are_not_treated_as_defaults(self):
        for value in ({}, "NUMA", [True]):
            self.assertIn("configuration is invalid", str(self.policy(config={"developerConfiguration": {"featureGates": value}})["blockers"]))


if __name__ == "__main__":
    unittest.main()
