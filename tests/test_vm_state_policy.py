import copy
import unittest

import test_vm_resources as fixtures
import homestead_vm_state_policy as policy


class StatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.spec = self.vm["spec"]["template"]["spec"]
        self.config = {"developerConfiguration": {"featureGates": ["IncrementalBackup"]},
                       "changedBlockTrackingLabelSelectors": {"virtualMachineLabelSelector": {"matchLabels": {"backup": "yes"}}}}
        self.namespace = {"metadata": {"name": "lab", "uid": "ns-uid", "resourceVersion": "1", "labels": {"backup": "yes"}}}
        self.paths = []

    def read(self, path):
        self.paths.append(path)
        return copy.deepcopy(self.namespace)

    def select(self, **kwargs):
        return policy.selection(self.vm, self.spec, self.config, self.read, version=kwargs.pop("version", "v1.9.0"), **kwargs)

    def test_vm_outer_labels_not_launcher_labels_and_or_namespace_selection(self):
        self.vm["spec"]["template"]["metadata"] = {"labels": {"backup": "yes"}}
        self.assertFalse(self.select()["automatic"])
        self.config["changedBlockTrackingLabelSelectors"]["namespaceLabelSelector"] = {"matchLabels": {"backup": "yes"}}
        result = self.select()
        self.assertTrue(result["automatic"])
        self.assertEqual(["/api/v1/namespaces/lab"], self.paths)
        self.assertEqual("ns-uid", result["dependencies"][self.paths[0]]["uid"])
        self.paths.clear()
        self.vm["metadata"]["labels"] = {"backup": "yes"}
        self.assertTrue(self.select()["automatic"])
        self.assertFalse(self.paths, "OR match does not depend on namespace labels")

    def test_empty_selector_matches_all_but_absent_selector_does_not(self):
        self.config["changedBlockTrackingLabelSelectors"] = {}
        self.assertFalse(self.select()["automatic"])
        self.config["changedBlockTrackingLabelSelectors"]["virtualMachineLabelSelector"] = {}
        self.assertTrue(self.select()["automatic"])

    def test_gate_disabled_on_known_version_does_not_create_backup_state(self):
        self.config["developerConfiguration"]["featureGates"] = []
        self.config["changedBlockTrackingLabelSelectors"] = {"virtualMachineLabelSelector": {}}
        self.assertFalse(self.select()["automatic"])
        result = self.select(version=None)
        self.assertTrue(result["automatic"])
        self.assertIn("unverified", " ".join(result["warnings"]))

    def test_resume_does_not_apply_future_instance_selectors(self):
        self.config["changedBlockTrackingLabelSelectors"] = {"virtualMachineLabelSelector": {}}
        self.assertFalse(self.select(cold=False)["automatic"])

    def test_namespace_read_failure_identity_or_deletion_cannot_be_assumed_no_match(self):
        self.config["changedBlockTrackingLabelSelectors"] = {"namespaceLabelSelector": {}}
        for key, value in (("uid", ""), ("resourceVersion", ""), ("name", "wrong"), ("deletionTimestamp", "now")):
            before = copy.deepcopy(self.namespace)
            self.namespace["metadata"][key] = value
            self.assertTrue(self.select()["blockers"])
            self.namespace = before
        def fail(path):
            raise ValueError("private-token")
        result = policy.selection(self.vm, self.spec, self.config, fail, version="v1.9.0")
        self.assertTrue(result["blockers"])
        self.assertNotIn("private-token", str(result))

    def test_selector_operators_and_missing_key_semantics(self):
        for operator, values, present, absent in (("In", ["yes"], True, False), ("NotIn", ["yes"], False, True),
                                                 ("Exists", [], True, False), ("DoesNotExist", [], False, True)):
            selector = {"matchExpressions": [{"key": "backup", "operator": operator, "values": values}]}
            self.assertEqual(present, policy.matches(selector, {"backup": "yes"}))
            self.assertEqual(absent, policy.matches(selector, {}))
        self.assertFalse(policy.matches({"matchLabels": {"x": "a"}, "matchExpressions": [{"key": "x", "operator": "DoesNotExist"}]}, {"x": "a"}))

    def test_malformed_selector_does_not_silently_disable_dependencies(self):
        for selector in ("all", {"other": "field"}, {"matchLabels": {"x": None}},
                         {"matchExpressions": [{"key": "x", "operator": "In", "values": []}]},
                         {"matchExpressions": [{"key": "x", "operator": "Exists", "values": ["yes"]}]}):
            self.config["changedBlockTrackingLabelSelectors"] = {"virtualMachineLabelSelector": selector}
            self.assertTrue(self.select()["blockers"])

    def test_persistent_state_feature_gate_is_version_aware(self):
        self.config.pop("changedBlockTrackingLabelSelectors")
        self.spec["domain"]["devices"] = {"tpm": {"persistent": True}}
        for version in ("v1.3.1", "v1.4.0", "v1.5.0"):
            self.assertTrue(self.select(version=version)["blockers"])
        self.assertFalse(self.select(version="v1.6.0")["blockers"])
        self.assertIn("unverified", " ".join(self.select(version=None)["warnings"]))
        self.config["developerConfiguration"]["featureGates"].append("VMPersistentState")
        self.assertFalse(self.select(version="v1.3.1")["blockers"])

    def test_incremental_backup_gate_on_unsupported_version_is_explicit(self):
        self.assertIn("1.6", " ".join(self.select(version="v1.5.0")["blockers"]))


if __name__ == "__main__":
    unittest.main()
