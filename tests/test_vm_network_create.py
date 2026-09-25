import json, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_networking as NET


class VmNetworkTests(unittest.TestCase):
    """Homestead said to make a VM network in Harvester's dashboard, which
    read as its own Networking page; it makes one itself now."""

    def setUp(self):
        self.sent, self.harvester = [], True

        def get(path):
            if path == NET.CLUSTER_NETWORKS:
                if not self.harvester:
                    raise urllib.error.HTTPError(path, 404, "missing", {}, None)
                return {"items": [{"metadata": {"name": "mgmt"}}]}
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

    def test_a_bad_vlan_and_a_cluster_without_harvester_are_refused(self):
        with self.assertRaisesRegex(ValueError, "1 to 4094"):
            NET.create_vm_network({"name": "x", "vlan": "5000"})
        self.harvester = False
        with self.assertRaisesRegex(ValueError, "LAN networks are made here on Harvester"):
            NET.create_vm_network({"name": "x"})


if __name__ == "__main__":
    unittest.main()
