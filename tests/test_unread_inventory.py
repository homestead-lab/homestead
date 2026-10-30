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


if __name__ == "__main__":
    unittest.main()
