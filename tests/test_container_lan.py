"""A container's own LAN network. Runs on the shared fake API (tests/fakes/kube.py)."""
import json, sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_lan as LAN
import homestead_deploy_manifests as MANIFESTS
from fakes.kube import Kube

OWN = "/apis/k8s.cni.cncf.io/v1/namespaces/lab/network-attachment-definitions/zigbee2mqtt-lan"


def nad(name, namespace, config=None, **meta):
    body = {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
            "metadata": dict(meta, name=name, namespace=namespace)}
    if config is not None:
        body["spec"] = {"config": json.dumps(config)}
    return body


VLAN1 = nad("vlan1", "default",
    {"cniVersion": "0.3.1", "name": "vlan1", "type": "bridge", "bridge": "mgmt-br", "promiscMode": True,
     "vlan": 1, "ipam": {}})
WANTED = {"network": "default/vlan1", "address": "192.0.2.70", "prefix": 24, "gateway": "192.0.2.1"}


class ContainerLanTests(unittest.TestCase):
    """A Harvester VM network gives no addresses, so a container on it needs
    a network of its own holding its one address."""

    def cluster(self):
        kube = self.enterContext(Kube(VLAN1))
        self.addCleanup(LAN.bind, getattr(LAN, "kget", None), getattr(LAN, "ksend", None))
        LAN.bind(K.kget, K.ksend)
        return kube

    @staticmethod
    def sent(kube):
        return [write[:3] for write in kube.writes]

    def test_its_own_network_copies_the_bridge_with_a_static_address(self):
        kube = self.cluster()
        LAN.ensure_nad("lab", "zigbee2mqtt", LAN.clean(WANTED))
        method, path, body = self.sent(kube)[0]
        self.assertEqual(("POST", "/apis/k8s.cni.cncf.io/v1/namespaces/lab/network-attachment-definitions"), (method, path))
        config = json.loads(body["spec"]["config"])
        self.assertEqual(("bridge", "mgmt-br", 1, "zigbee2mqtt-lan"), (config["type"], config["bridge"], config["vlan"], config["name"]))
        self.assertEqual({"type": "static", "addresses": [{"address": "192.0.2.70/24", "gateway": "192.0.2.1"}]},
                         config["ipam"])

    def test_changing_the_address_updates_its_network(self):
        kube = self.cluster()
        kube.add(nad("zigbee2mqtt-lan", "lab", resourceVersion="7", labels={"app": "zigbee2mqtt"}))
        LAN.ensure_nad("lab", "zigbee2mqtt", LAN.clean(dict(WANTED, address="192.0.2.71")))
        method, path, body = self.sent(kube)[0]
        self.assertEqual(("PUT", OWN, "7"), (method, path, body["metadata"]["resourceVersion"]))
        self.assertEqual("192.0.2.71/24", json.loads(kube.object(OWN)["spec"]["config"])["ipam"]["addresses"][0]["address"])

    def test_the_pod_joins_it_as_a_second_interface(self):
        dep = {"metadata": {"name": "zigbee2mqtt"}, "spec": {"template": {"metadata": {}, "spec": {}}}}
        LAN.apply_to_template(dep, "lab", "zigbee2mqtt", LAN.clean(WANTED))
        networks = json.loads(dep["spec"]["template"]["metadata"]["annotations"][LAN.NETWORKS])
        self.assertEqual([{"name": "zigbee2mqtt-lan", "namespace": "lab", "interface": "lan0"}], networks)
        self.assertEqual("192.0.2.70", LAN.read(dep)["address"])
        LAN.apply_to_template(dep, "lab", "zigbee2mqtt", None)
        self.assertNotIn(LAN.NETWORKS, dep["spec"]["template"]["metadata"]["annotations"])
        self.assertIsNone(LAN.read(dep))

    def test_only_a_network_on_the_lan_can_give_an_address(self):
        kube = self.cluster()
        kube.add(nad("vlan1", "default", {"type": "host-device", "device": "eth1"}))
        with self.assertRaisesRegex(ValueError, "not a network bridged"):
            LAN.ensure_nad("lab", "zigbee2mqtt", LAN.clean(WANTED))

    def test_a_macvlan_network_carries_its_nic_into_the_copy(self):
        """k3s LAN networks on a plain NIC are macvlan."""
        body_config = {"cniVersion": "0.3.1", "type": "macvlan", "master": "eth0", "mode": "bridge", "ipam": {}}
        kube = self.cluster()
        kube.add(nad("vlan1", "default", body_config))
        own = json.loads(LAN.nad_body("lab", "zigbee2mqtt", LAN.clean(WANTED))["spec"]["config"])
        self.assertEqual(("macvlan", "eth0", "bridge", "static"), (own["type"], own["master"], own["mode"], own["ipam"]["type"]))

    def test_a_bad_address_is_refused(self):
        with self.assertRaisesRegex(ValueError, "cannot be a machine's"):
            LAN.clean(dict(WANTED, address="192.0.2.255"))
        with self.assertRaisesRegex(ValueError, "outside"):
            LAN.clean(dict(WANTED, gateway="10.0.0.1"))


class DeployTests(unittest.TestCase):
    def test_a_lan_container_has_no_service_and_joins_its_network(self):
        import server
        with mock.patch.object(server.HW, "features", lambda: []):
            dep, svc = MANIFESTS.build_deployment({"name": "zigbee2mqtt", "namespace": "lab", "image": "koenkk/zigbee2mqtt",
                                                "network_mode": "lan", "lan": LAN.clean(WANTED), "volumes": [],
                                                "ports": [{"container": 8080, "expose": True}]})
        self.assertIsNone(svc)
        self.assertIn(LAN.NETWORKS, dep["spec"]["template"]["metadata"]["annotations"])


if __name__ == "__main__":
    unittest.main()
