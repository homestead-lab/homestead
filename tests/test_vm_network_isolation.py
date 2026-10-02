import copy
import unittest
from unittest import mock

import test_vm_edit as edit_fixtures
import test_vm_create as create_fixtures
import homestead_vms as VMS
import homestead_imports as IMP
import homestead_vm_network as NETWORK


class VMNetworkIsolationTests(unittest.TestCase):
    def setUp(self):
        self.cluster = edit_fixtures.Cluster({"harvester": False, "cdi": False})

    def spec(self):
        return self.cluster.vm["spec"]["template"]["spec"]

    def assert_no_nics(self, spec):
        self.assertEqual([], spec["domain"]["devices"]["interfaces"])
        self.assertIs(False, spec["domain"]["devices"]["autoattachPodInterface"])
        self.assertEqual([], spec["networks"])
        self.assertEqual({}, NETWORK.evidence(spec, "lab", {})["requests"])

    def test_removing_last_nic_disables_implicit_fallback(self):
        VMS.edit("lab", "web", {"nics": [{"name": "default", "remove": True}]})
        self.assert_no_nics(self.spec())
        self.assertFalse(NETWORK.isolated(self.cluster.vm))
        self.assertFalse(any(path.endswith("/restart") for _, path, _ in self.cluster.sent))

    def test_isolation_removes_all_networks_and_preserves_disks_and_passthrough(self):
        spec = self.spec()
        devices = spec["domain"]["devices"]
        devices["hostDevices"] = [{"name": "gpu", "deviceName": "homestead.io/pci-10de-1c30"}]
        devices["interfaces"].append({"name": "lan", "bridge": {}})
        spec["networks"].append({"name": "lan", "multus": {"networkName": "lab/lan"}})
        old = copy.deepcopy(spec)
        VMS.edit("lab", "web", {"isolated": True})
        self.assert_no_nics(self.spec())
        self.assertTrue(NETWORK.isolated(self.cluster.vm))
        self.assertEqual(old["volumes"], self.spec()["volumes"])
        self.assertEqual(old["domain"]["devices"]["disks"], self.spec()["domain"]["devices"]["disks"])
        self.assertEqual(old["domain"]["devices"]["hostDevices"], self.spec()["domain"]["devices"]["hostDevices"])

    def test_saved_isolation_rejects_nic_additions_and_modifications_without_writes(self):
        VMS.edit("lab", "web", {"isolated": True})
        self.cluster.sent.clear()
        for cfg in ({"add_nics": [{"network": "pod"}]}, {"nics": [{"name": "default"}]},
                    {"isolated": True, "add_nics": [{"network": "pod"}]}):
            with self.subTest(cfg=cfg), self.assertRaisesRegex(ValueError, "Clear Isolated VM"):
                VMS.edit("lab", "web", cfg)
        self.assertEqual([], self.cluster.sent)

    def test_unrelated_edit_keeps_isolation_and_explicit_release_allows_addition(self):
        VMS.edit("lab", "web", {"isolated": True})
        VMS.edit("lab", "web", {"description": "offline guest"})
        self.assertTrue(VMS._row(self.cluster.vm, {})["isolated"])
        self.assert_no_nics(self.spec())
        VMS.edit("lab", "web", {"isolated": False})
        self.assertFalse(NETWORK.isolated(self.cluster.vm))
        self.assert_no_nics(self.spec())
        VMS.edit("lab", "web", {"add_nics": [{"network": "pod"}]})
        self.assertEqual(1, len(self.spec()["networks"]))
        self.assertIs(False, self.spec()["domain"]["devices"]["autoattachPodInterface"])

    def test_legacy_implicit_nic_is_disclosed_and_empty_form_disables_it(self):
        self.spec().pop("networks")
        self.spec()["domain"]["devices"].pop("interfaces")
        self.assertTrue(VMS._row(self.cluster.vm, {})["implicit_network"])
        VMS.edit("lab", "web", {"description": "leave networking alone"})
        self.assertTrue(VMS._row(self.cluster.vm, {})["implicit_network"])
        VMS.edit("lab", "web", {"nics": [], "isolated": False})
        self.assert_no_nics(self.spec())
        self.assertFalse(VMS._row(self.cluster.vm, {})["implicit_network"])

    def test_normal_metadata_edit_does_not_change_template_or_add_annotations(self):
        before = copy.deepcopy(self.cluster.vm)
        prepared = VMS.prepare_edit("lab", "web", {}, current=before)
        self.assertEqual(before, prepared["vm"])
        self.assertFalse(prepared["changed_hardware"])

    def test_checkboxes_are_strict_booleans(self):
        for value in ("true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "checkbox"):
                VMS.prepare_edit("lab", "web", {"isolated": value})
        self.assertEqual([], self.cluster.sent)


class VMIsolatedCreateTests(unittest.TestCase):
    setUp = create_fixtures.VmCreateTests.setUp
    create = create_fixtures.VmCreateTests.create
    vm = create_fixtures.VmCreateTests.vm

    def test_isolated_create_has_no_mac_or_automatic_interface_on_all_platforms(self):
        for platform in (create_fixtures.HARVESTER, create_fixtures.K3S_CDI, create_fixtures.K3S_BARE):
            with self.subTest(platform=platform), mock.patch.object(IMP, "_vm_mac") as mac:
                self.sent.clear()
                result = self.create(platform, "storage", isolated=True)
                spec = self.vm()["spec"]["template"]["spec"]
                self.assertEqual([], spec["networks"])
                self.assertEqual([], spec["domain"]["devices"]["interfaces"])
                self.assertIs(False, spec["domain"]["devices"]["autoattachPodInterface"])
                self.assertTrue(NETWORK.isolated(self.vm()))
                self.assertEqual("", result["mac"])
                self.assertEqual({}, NETWORK.evidence(spec, "lab", {})["requests"])
                mac.assert_not_called()

    def test_isolated_create_rejects_network_address_or_model_conflicts_before_writes(self):
        for key, value in (("network", "pod"), ("nic_model", "virtio"), ("mac", "52:54:00:11:22:33"),
                           ("static_ip", {"address": "192.0.2.1"}), ("add_nics", [{}]), ("nics", [{}])):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "isolated VM"):
                self.create(create_fixtures.K3S_BARE, isolated=True, **{key: value})
        self.assertEqual([], self.sent)
