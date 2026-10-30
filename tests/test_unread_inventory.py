"""A list that could not be read is not an empty list.

A host's impact preview read no VMs' devices or service addresses as "none",
Network > Your VIPs read an unreadable registry as no VIPs, Containers and
Edit read unread Services as no exposure, and the VM disk and import lists
read a failed read as nothing there. Each now says what could not be read.
Everything here runs on the shared fake."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_impact as IMPACT
import homestead_kube as K
import homestead_network_inventory as INV
import homestead_vip_registry as REG
from test_page_snapshot import Cluster, clean_cache, clean_snapshot, setUpModule  # noqa: F401 (restart record off)

RAW_VMS = "/apis/kubevirt.io/v1/virtualmachines"


class PreviewTests(unittest.TestCase):
    NODES = [{"name": "node-a", "status": "Ready"}, {"name": "node-b", "status": "Ready"}]
    VM = {"name": "vm1", "ns": "lab", "node": "node-a", "running": True, "disks": []}
    ADDRESS = {"ip": "192.0.2.50", "kind": "vip", "node": "node-a", "services": ["lab/web"]}

    def preview(self, raw_vms, addresses):
        return IMPACT.preview("node-a", [], [], [self.VM], raw_vms, [], addresses, self.NODES, sets=[])

    def test_a_vm_whose_definition_was_read_without_devices_still_moves(self):
        p = self.preview([{"metadata": {"namespace": "lab", "name": "vm1"}, "spec": {}}], [])
        self.assertEqual("moves", p["apps"][0]["outcome"])
        self.assertEqual([], p["unread"])

    def test_unread_vm_definitions_leave_a_running_vm_unknown_never_moving(self):
        p = self.preview(None, [])
        self.assertEqual("unknown", p["apps"][0]["outcome"])
        self.assertIn("passed through", p["apps"][0]["why"])
        self.assertIn(IMPACT.VMS_UNREAD, p["unread"])

    def test_read_addresses_on_this_host_are_counted(self):
        p = self.preview([], [self.ADDRESS])
        self.assertEqual(1, p["counts"]["addresses"])

    def test_unread_addresses_make_the_count_unknown_never_zero(self):
        p = self.preview([], None)
        self.assertIsNone(p["counts"]["addresses"])
        self.assertEqual([], p["addresses"])
        self.assertIn(IMPACT.ADDRESSES_UNREAD, p["unread"])


class NodeImpactTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        clean_snapshot(self)
        self.cluster = Cluster(self)
        self.NODES = self.cluster.server.NODES

    def test_a_host_preview_with_vm_definitions_unread_says_so(self):
        self.cluster.kube.fail(RAW_VMS, 500, times=1)
        self.assertIsNone(self.NODES._impact_sources()["raw_vms"])

    def test_a_host_preview_with_the_network_inventory_unread_says_addresses_are_unknown(self):
        def broken():
            raise RuntimeError("network inventory unavailable")
        self.enterContext(mock.patch.object(self.NODES.INV, "inventory", broken))
        p = self.NODES.node_impact("node-a")
        self.assertIsNone(p["counts"]["addresses"])
        self.assertIn(IMPACT.ADDRESSES_UNREAD, p["unread"])



class YourVipsTests(unittest.TestCase):
    """Network > Your VIPs, the default workload VIP and what may be offered as
    free all come from the VIP registry ConfigMap."""
    def setUp(self):
        clean_cache(self)
        clean_snapshot(self)
        self.cluster = Cluster(self)
        self.NETWORK = self.cluster.server.NETWORK
        # Other test files bind the networking modules to fakes of their own
        # and leave them so: read through this cluster.
        import homestead_network_state as STATE
        for name, value in (("kget", K.kget), ("ksend", K.ksend)):
            self.enterContext(mock.patch.object(STATE, name, value))

    def break_registry(self, how="timeout"):
        """A registry read that times out (it used to read as no VIPs) or
        answers 500 (it used to fail the whole Network page and Dashboard)."""
        if how == "500":
            self.cluster.kube.fail(REG._vip_map_path(), 500, times=999)
            return

        def timeout(method, path, body):
            raise TimeoutError("timed out")
        self.cluster.kube.react("GET", REG._vip_map_path(), timeout)

    def test_no_registry_at_all_is_no_vips(self):
        self.assertEqual([], REG.registered())
        self.assertEqual([], self.NETWORK.inventory()["registered_vips"])

    def test_a_registry_that_cannot_be_read_raises_never_empty(self):
        self.break_registry()
        with self.assertRaisesRegex(RuntimeError, "Your VIPs could not be read"):
            REG.registered()

    def test_the_default_vip_is_not_the_shared_fallback_when_the_registry_is_unread(self):
        self.break_registry()
        with self.assertRaisesRegex(RuntimeError, "Your VIPs could not be read"):
            REG.shared_vip()

    def test_the_inventory_names_the_registry_unread_and_offers_no_default(self):
        for how in ("timeout", "500"):
            with self.subTest(how=how):
                clean_cache(self)
                self.cluster.kube._failures.clear()
                self.cluster.kube._reactions.clear()
                self.break_registry(how)
                inv = self.NETWORK.inventory()
                self.assertIsNone(inv["registered_vips"])
                self.assertIsNone(inv["shared_vip"]["ip"])
                self.assertIn(REG.UNREAD, inv["unread"])
                self.assertIn("Your VIPs could not be read", inv["vips_unread"])

    def test_an_address_is_not_offered_or_accepted_while_the_registry_is_unread(self):
        self.break_registry()
        inv = self.NETWORK.inventory()
        self.assertIn("could not read", INV.address_problem("192.0.2.200", inv))

    def test_the_overview_still_answers_without_a_vip_when_the_registry_is_unread(self):
        self.break_registry()
        self.assertIsNone(self.cluster.route("ov")["lb_ip"])


if __name__ == "__main__":
    unittest.main()
