import copy
from pathlib import Path
import unittest
import urllib.error
from unittest import mock

import test_vm_resources as fixtures
import test_vm_edit as edit_fixtures
import homestead_vm_profiles as profiles
import homestead_vms as vms


class VMProfileTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.vm["spec"]["instancetype"] = {"name": "medium"}
        self.expanded = copy.deepcopy(self.vm)
        self.expanded["spec"]["template"]["spec"]["domain"]["memory"]["guest"] = "6Gi"
        self.read = mock.Mock(return_value=self.expanded)
        self.send = mock.Mock(return_value=self.expanded)

    def test_proposed_expansion_uses_only_non_persisting_endpoint_and_copies_input(self):
        result = profiles.expand(self.vm, self.read, self.send)
        method, path, payload = self.send.call_args.args
        self.assertEqual("PUT", method)
        self.assertEqual("/apis/subresources.kubevirt.io/v1/namespaces/lab/expand-vm-spec", path)
        self.read.assert_not_called()
        payload["spec"]["instancetype"]["name"] = "changed"
        result["domain"]["memory"]["guest"] = "99Gi"
        self.assertEqual("medium", self.vm["spec"]["instancetype"]["name"])
        self.assertEqual("6Gi", self.expanded["spec"]["template"]["spec"]["domain"]["memory"]["guest"])

    def test_non_profile_vm_needs_no_api_call(self):
        del self.vm["spec"]["instancetype"]
        self.assertIsNone(profiles.expand(self.vm, self.read, self.send))
        self.read.assert_not_called()
        self.send.assert_not_called()

    def test_preference_only_vm_still_requires_expansion(self):
        self.vm["spec"]["preference"] = self.vm["spec"].pop("instancetype")
        profiles.expand(self.vm, self.read)
        self.read.assert_called_once_with("/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachines/guest/expand-spec")

    def test_wrong_identity_or_missing_domain_is_rejected(self):
        for key in ("name", "namespace", "uid", "resourceVersion"):
            value = copy.deepcopy(self.expanded)
            value["metadata"][key] = "different"
            self.send.return_value = value
            with self.assertRaisesRegex(ValueError, "VM changed"):
                profiles.expand(self.vm, self.read, self.send)
        for value in (None, [], {}, {"metadata": self.vm["metadata"], "spec": {"template": {"spec": {}}}},
                      {"metadata": self.vm["metadata"], "spec": {"template": {"spec": {"domain": {"devices": {}}}}}}):
            self.send.return_value = value
            with self.assertRaises(ValueError):
                profiles.expand(self.vm, self.read, self.send)

    def test_unsupported_or_forbidden_does_not_fallback_to_saved_vm_or_leak_body(self):
        for code in (403, 404, 422, 500):
            self.send.side_effect = urllib.error.HTTPError("test", code, "private-cloud-init", {}, None)
            with self.assertRaises(ValueError) as error:
                profiles.expand(self.vm, self.read, self.send)
            self.assertNotIn("private-cloud-init", str(error.exception))
            self.assertIn(f"HTTP {code}", str(error.exception))
        self.read.assert_not_called()

    def test_detail_shows_profile_values_or_explicit_unknown(self):
        cluster = edit_fixtures.Cluster({"harvester": False, "cdi": True})
        cluster.vm["spec"]["instancetype"] = {"name": "medium"}
        with mock.patch.object(vms.PROFILES, "expand", return_value={"domain": {"cpu": {"cores": 3}, "memory": {"guest": "6Gi"}}}):
            detail = vms.detail("lab", "web")
        self.assertEqual((3, "6Gi", "medium", ""), (detail["cores"], detail["memory"], detail["resource_profile"]["name"], detail["profile_error"]))
        with mock.patch.object(vms.PROFILES, "expand", side_effect=ValueError("private-error")):
            detail = vms.detail("lab", "web")
        self.assertIsNone(detail["cores"])
        self.assertEqual("", detail["memory"])
        self.assertIn("could not be resolved", detail["profile_error"])
        self.assertNotIn("private-error", detail["profile_error"])
        self.assertEqual([], cluster.sent)

    def test_all_install_paths_include_non_persisting_expansion_permission(self):
        root = Path(__file__).resolve().parents[1]
        for file in ("deploy/rbac.yaml", "deploy/deploy.yaml", "charts/homestead/templates/rbac.yaml"):
            self.assertIn("resources: [expand-vm-spec]\n    verbs: [update]", (root / file).read_text())


if __name__ == "__main__":
    unittest.main()
