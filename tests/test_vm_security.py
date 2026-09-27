import copy
import json
import unittest

import test_vm_resources as fixtures
import homestead_vm_security as security
import homestead_vm_resources as resources


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.spec = self.vm["spec"]["template"]["spec"]
        self.spec["architecture"] = "amd64"
        self.domain = self.spec["domain"]
        self.domain.update(launchSecurity={"sev": {}}, firmware={"bootloader": {"efi": {"secureBoot": False}}})
        self.config = {}

    def evidence(self, version="v1.9.0"):
        return security.evidence(self.spec, self.config, version)

    def test_modes_have_distinct_labels_and_device_pools(self):
        for mode, options, labels, device in (
            ("sev", {}, {"kubevirt.io/sev"}, "sev"),
            ("sev", {"policy": {"encryptedState": True}}, {"kubevirt.io/sev", "kubevirt.io/sev-es"}, "sev"),
            ("snp", {}, {"kubevirt.io/sev", "kubevirt.io/sev-snp"}, "sev"),
            ("tdx", {}, {"kubevirt.io/tdx"}, "tdx"),
        ):
            with self.subTest(mode=mode, options=options):
                self.domain["launchSecurity"] = {mode: options}
                self.config = {"developerConfiguration": {"featureGates": ["WorkloadEncryptionTDX"]}}
                found = self.evidence()
                self.assertFalse(found["blockers"], found)
                self.assertEqual({**dict.fromkeys(labels, "true"), "kubernetes.io/arch": "amd64"}, found["selectors"])
                self.assertEqual({"devices.kubevirt.io/" + device: 1}, found["requests"])
                self.assertIn("do not prove", " ".join(found["warnings"]))

    def test_absent_security_does_not_require_known_version_or_gate(self):
        self.domain.pop("launchSecurity")
        self.assertFalse(self.evidence(None)["blockers"])

    def test_sev_gate_is_opt_in_before_nine_then_beta_with_disable(self):
        self.assertIn("not enabled", str(self.evidence("v1.8.0")["blockers"]))
        self.assertFalse(self.evidence()["blockers"])
        self.config["developerConfiguration"] = {"disabledFeatureGates": ["WorkloadEncryptionSEV"]}
        self.assertIn("not enabled", str(self.evidence()["blockers"]))
        self.config["developerConfiguration"]["featureGates"] = ["WorkloadEncryptionSEV"]
        # Matches upstream precedence; schema may reject conflicting lists first.
        self.assertFalse(self.evidence()["blockers"])
        self.assertFalse(self.evidence("v1.3.1")["blockers"])

    def test_tdx_remains_opt_in_and_new_modes_require_seven(self):
        for mode in ("snp", "tdx"):
            self.domain["launchSecurity"] = {mode: {}}
            self.config = {"developerConfiguration": {"featureGates": ["WorkloadEncryptionSEV", "WorkloadEncryptionTDX"]}}
            self.assertIn("1.7", str(self.evidence("v1.6.0")["blockers"]))
            self.assertFalse(self.evidence("v1.7.0")["blockers"])
        self.config = {}
        self.assertIn("WorkloadEncryptionTDX", str(self.evidence()["blockers"]))

    def test_unknown_renderer_and_architecture_are_hard_blockers(self):
        for version in (None, "v1.9.0-vendor", "v1.10.0", "v2.0.0", "v1.2.0"):
            self.assertIn("unverified", str(self.evidence(version)["blockers"]))
        for architecture in (None, "arm64", "s390x"):
            self.spec["architecture"] = architecture
            self.assertIn("amd64", str(self.evidence()["blockers"]))

    def test_invalid_modes_policy_and_shapes_are_rejected_without_values(self):
        for value in ({}, [], "private-blob", {"sev": {}, "snp": {}}, {"sev": "private-blob"},
                      {"unknown-private": {}}, {"sev": {"policy": {"encryptedState": "private-blob"}}},
                      {"sev": {"policy": {"debug": True}}}, {"sev": {"session": {"private-blob": 1}}},
                      {"sev": {"attestation": {"private-blob": 1}}}, {"snp": {"private-blob": 1}}):
            self.domain["launchSecurity"] = value
            result = self.evidence()
            self.assertTrue(result["blockers"], value)
            self.assertNotIn("private", json.dumps(result))

    def test_uefi_secure_boot_and_persistence_constraints(self):
        self.domain["firmware"] = {}
        self.assertIn("UEFI", str(self.evidence()["blockers"]))
        efi = {}
        self.domain["firmware"] = {"bootloader": {"efi": efi}}
        self.assertIn("Secure Boot", str(self.evidence()["blockers"]))
        efi.update(secureBoot=False, persistent=True)
        self.assertFalse(self.evidence()["blockers"])
        for mode in ("snp", "tdx"):
            self.domain["launchSecurity"] = {mode: {}}
            self.assertIn("persistent EFI", str(self.evidence()["blockers"]))
        self.config = {"developerConfiguration": {"featureGates": ["WorkloadEncryptionTDX"]}}
        efi.update(persistent=False, secureBoot=True)
        self.assertFalse(self.evidence()["blockers"])
        self.domain["features"] = {"smm": {}}
        self.assertIn("SMM", str(self.evidence()["blockers"]))
        self.domain["features"]["smm"]["enabled"] = False
        self.assertFalse(self.evidence()["blockers"])

    def test_attestation_is_separate_paused_flow_and_secrets_never_return(self):
        self.domain["launchSecurity"]["sev"] = {"attestation": {}, "session": "private-session", "dhCert": "private-cert"}
        self.assertIn("Paused", str(self.evidence()["blockers"]))
        self.spec["startStrategy"] = "Paused"
        result = resources.project(self.vm, self.config, kubevirt_version="v1.9.0")
        self.assertFalse(result["blockers"])
        self.assertIn("attestation must be completed separately", str(result["warnings"]))
        self.assertNotIn("private-", json.dumps(result))

    def test_network_boot_emulation_and_unverified_hypervisor_block(self):
        self.domain["devices"] = {"interfaces": [{"bootOrder": 1}]}
        self.assertIn("network interface", str(self.evidence()["blockers"]))
        self.config = {"developerConfiguration": {"useEmulation": True}, "hypervisor": {"name": "mshv"}}
        self.assertIn("emulation", str(self.evidence()["blockers"]))
        self.assertIn("KVM", str(self.evidence()["blockers"]))

    def test_malformed_gate_lists_do_not_silently_enable(self):
        for key in ("featureGates", "disabledFeatureGates"):
            for value in ("WorkloadEncryptionSEV", [True], {}):
                self.config = {"developerConfiguration": {key: value}}
                self.assertIn("configuration is invalid", str(self.evidence()["blockers"]))

    def test_snp_memory_included_once_and_selectors_cannot_be_relaxed(self):
        self.domain["launchSecurity"] = {"snp": {}}
        self.spec["nodeSelector"] = {"kubevirt.io/sev-snp": "false"}
        before = copy.deepcopy(self.vm)
        result = resources.project(self.vm, self.config, kubevirt_version="v1.9.0")
        self.assertEqual(256 * 1024**2, result["additional_overhead_bytes"])
        self.assertIn("conflicts", str(result["blockers"]))
        pod = result["manifest"]["spec"]["template"]["spec"]
        self.assertEqual("1", pod["containers"][0]["resources"]["requests"]["devices.kubevirt.io/sev"])
        self.assertEqual(before, self.vm)


if __name__ == "__main__":
    unittest.main()
