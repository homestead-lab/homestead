import json, sys, unittest, urllib.error
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_lan as LAN

BASE = "/apis/k8s.cni.cncf.io/v1/namespaces/default/network-attachment-definitions/vlan1"
OWN = "/apis/k8s.cni.cncf.io/v1/namespaces/lab/network-attachment-definitions/zigbee2mqtt-lan"
VLAN1 = {"metadata": {"name": "vlan1"}, "spec": {"config": json.dumps(
    {"cniVersion": "0.3.1", "name": "vlan1", "type": "bridge", "bridge": "mgmt-br", "promiscMode": True,
     "vlan": 1, "ipam": {}})}}
WANTED = {"network": "default/vlan1", "address": "192.0.2.70", "prefix": 24, "gateway": "192.0.2.1"}


class Cluster:
    def __init__(self):
        self.objects, self.sent = {BASE: VLAN1}, []
        LAN.bind(self.get, self.send)

    def get(self, path):
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        return body


class ContainerLanTests(unittest.TestCase):
    """A Harvester VM network gives no addresses, so a container on it needs
    a network of its own holding its one address."""

    def test_its_own_network_copies_the_bridge_with_a_static_address(self):
        c = Cluster()
        LAN.ensure_nad("lab", "zigbee2mqtt", LAN.clean(WANTED))
        method, path, body = c.sent[0]
        self.assertEqual(("POST", "/apis/k8s.cni.cncf.io/v1/namespaces/lab/network-attachment-definitions"), (method, path))
        config = json.loads(body["spec"]["config"])
        self.assertEqual(("bridge", "mgmt-br", 1, "zigbee2mqtt-lan"), (config["type"], config["bridge"], config["vlan"], config["name"]))
        self.assertEqual({"type": "static", "addresses": [{"address": "192.0.2.70/24", "gateway": "192.0.2.1"}]},
                         config["ipam"])

    def test_changing_the_address_updates_its_network(self):
        c = Cluster()
        c.objects[OWN] = {"metadata": {"name": "zigbee2mqtt-lan", "resourceVersion": "7"}}
        LAN.ensure_nad("lab", "zigbee2mqtt", LAN.clean(dict(WANTED, address="192.0.2.71")))
        method, path, body = c.sent[0]
        self.assertEqual(("PUT", OWN, "7"), (method, path, body["metadata"]["resourceVersion"]))

    def test_the_pod_joins_it_as_a_second_interface(self):
        dep = {"metadata": {"name": "zigbee2mqtt"}, "spec": {"template": {"metadata": {}, "spec": {}}}}
        LAN.apply_to_template(dep, "lab", "zigbee2mqtt", LAN.clean(WANTED))
        networks = json.loads(dep["spec"]["template"]["metadata"]["annotations"][LAN.NETWORKS])
        self.assertEqual([{"name": "zigbee2mqtt-lan", "namespace": "lab", "interface": "lan0"}], networks)
        self.assertEqual("192.0.2.70", LAN.read(dep)["address"])
        LAN.apply_to_template(dep, "lab", "zigbee2mqtt", None)
        self.assertNotIn(LAN.NETWORKS, dep["spec"]["template"]["metadata"]["annotations"])
        self.assertIsNone(LAN.read(dep))

    def test_only_a_bridged_network_can_give_an_address(self):
        c = Cluster()
        c.objects[BASE] = {"spec": {"config": json.dumps({"type": "macvlan"})}}
        with self.assertRaisesRegex(ValueError, "not a network bridged"):
            LAN.ensure_nad("lab", "zigbee2mqtt", LAN.clean(WANTED))

    def test_a_bad_address_is_refused(self):
        with self.assertRaisesRegex(ValueError, "cannot be a machine's"):
            LAN.clean(dict(WANTED, address="192.0.2.255"))
        with self.assertRaisesRegex(ValueError, "outside"):
            LAN.clean(dict(WANTED, gateway="10.0.0.1"))


class DeployTests(unittest.TestCase):
    def test_a_lan_container_has_no_service_and_joins_its_network(self):
        import server
        with mock.patch.object(server.HW, "features", lambda: []):
            dep, svc = server.build_deployment({"name": "zigbee2mqtt", "namespace": "lab", "image": "koenkk/zigbee2mqtt",
                                                "network_mode": "lan", "lan": LAN.clean(WANTED), "volumes": [],
                                                "ports": [{"container": 8080, "expose": True}]})
        self.assertIsNone(svc)
        self.assertIn(LAN.NETWORKS, dep["spec"]["template"]["metadata"]["annotations"])


if __name__ == "__main__":
    unittest.main()
