import json, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_networking as NET


class VmNetworkTests(unittest.TestCase):
    """Homestead said to make a VM network in Harvester's dashboard, which
    read as its own Networking page; it makes one itself now."""

    def setUp(self):
        self.sent, self.harvester, self.multus = [], True, True

        def get(path):
            if path == NET.CLUSTER_NETWORKS:
                if not self.harvester:
                    raise urllib.error.HTTPError(path, 404, "missing", {}, None)
                return {"items": [{"metadata": {"name": "mgmt"}}]}
            if path == NET.NAD_API and self.multus:
                return {"resources": []}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        NET.bind(get, lambda m, p, b=None, **k: self.sent.append((m, p, b)), {"kube-system"}, "lab", "")

    def test_an_untagged_network_on_the_hosts_lan(self):
        NET.create_vm_network({"name": "lan"})
        method, path, body = self.sent[0]
        self.assertEqual("/apis/k8s.cni.cncf.io/v1/namespaces/default/network-attachment-definitions", path)
        config = json.loads(body["spec"]["config"])
        self.assertEqual(("bridge", "mgmt-br", {}), (config["type"], config["bridge"], config["ipam"]))
        self.assertNotIn("vlan", config)
        self.assertEqual("UntaggedNetwork", body["metadata"]["labels"]["network.harvesterhci.io/type"])

    def test_a_vlan_network(self):
        NET.create_vm_network({"name": "iot", "vlan": "20"})
        body = self.sent[0][2]
        self.assertEqual(20, json.loads(body["spec"]["config"])["vlan"])
        self.assertEqual(("L2VlanNetwork", "20"), (body["metadata"]["labels"]["network.harvesterhci.io/type"],
                                                   body["metadata"]["labels"]["network.harvesterhci.io/vlan-id"]))

    def test_a_bad_vlan_is_refused(self):
        with self.assertRaisesRegex(ValueError, "1 to 4094"):
            NET.create_vm_network({"name": "x", "vlan": "5000"})


class HostInterfaceNetworkTests(VmNetworkTests):
    """On k3s the form listed Harvester's cluster networks - none - so its
    dropdown was blank. Off Harvester a LAN network rides a host interface."""
    PROBES = {"n1": {"interfaces": [{"name": "eth0", "kind": "nic", "master": ""},
                                    {"name": "enp2s0", "kind": "nic", "master": "br0"},
                                    {"name": "br0", "kind": "bridge", "master": ""}]},
              "n2": {"interfaces": [{"name": "eth0", "kind": "nic", "master": ""}]}}

    def setUp(self):
        super().setUp()
        self.harvester = False

    def make(self, **cfg):
        NET.create_vm_network(dict({"name": "lan", "_probes": self.PROBES}, **cfg))
        return json.loads(self.sent[-1][2]["spec"]["config"])

    def test_options_list_the_hosts_interfaces_those_on_every_host_first(self):
        options = NET.vm_network_options(self.PROBES)
        self.assertFalse(options["harvester"])
        self.assertTrue(options["multus"])
        self.assertEqual(["eth0", "br0", "enp2s0"], [row["name"] for row in options["interfaces"]])
        self.assertEqual(["n1", "n2"], options["interfaces"][0]["nodes"])

    def test_a_host_bridge_carries_vms_and_containers(self):
        config = self.make(interface="br0", vlan="30")
        self.assertEqual(("bridge", "br0", 30), (config["type"], config["bridge"], config["vlan"]))

    def test_a_nic_already_in_a_bridge_rides_the_bridge(self):
        self.assertEqual(("bridge", "br0"), (self.make(interface="enp2s0")["type"], self.make(interface="enp2s0")["bridge"]))

    def test_a_plain_nic_gives_containers_macvlan(self):
        config = self.make(interface="eth0")
        self.assertEqual(("macvlan", "eth0", "bridge"), (config["type"], config["master"], config["mode"]))
        with self.assertRaisesRegex(ValueError, "no eth0.20 interface"):
            self.make(interface="eth0", vlan="20")
        self.PROBES["n1"]["interfaces"].append({"name": "eth0.20", "kind": "vlan", "master": ""})
        try:
            self.assertEqual("eth0.20", self.make(interface="eth0", vlan="20")["master"])
        finally:
            self.PROBES["n1"]["interfaces"].pop()

    def test_without_multus_it_says_how_to_install_it(self):
        self.multus = False
        with self.assertRaisesRegex(ValueError, "Settings > Cluster > Add-ons"):
            self.make(interface="eth0")
        self.assertEqual([], self.sent)

    def test_an_odd_interface_name_is_refused(self):
        with self.assertRaisesRegex(ValueError, "host interface"):
            self.make(interface="eth0; reboot")

    def test_a_bad_vlan_is_refused(self):
        with self.assertRaisesRegex(ValueError, "1 to 4094"):
            self.make(interface="br0", vlan="5000")

    # Harvester-only cases do not apply here.
    test_an_untagged_network_on_the_hosts_lan = test_a_vlan_network = None


if __name__ == "__main__":
    unittest.main()
