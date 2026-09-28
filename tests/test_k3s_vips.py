import sys, unittest, urllib.error
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_addons as ADDONS
import homestead_networking as networking
import homestead_platform as PLATFORM
import test_networking

K3S_KUBE_VIP = {"load_balancer": "kube-vip", "servicelb": True, "vip_class": "kube-vip.io/kube-vip-class",
                "distribution": "k3s"}


class KubeVipAddonTests(unittest.TestCase):
    """k3s had no VIPs: every Service sat on the nodes' own addresses."""

    def setup(self, platform=None, probes=None):
        self.sent = []
        self.platform = {"distribution": "k3s", "harvester": False, "helm_controller": True, "servicelb": True,
                         "load_balancer": "servicelb", **(platform or {})}
        ADDONS.bind(self.get, lambda m, p, b=None, **k: self.sent.append((m, p, b)) or b,
                    lambda force=False: dict(self.platform),
                    lambda: probes if probes is not None else {"n1": {"default_interface": "eth0"},
                                                               "n2": {"default_interface": "eth0"}})

    def get(self, path):
        if path.endswith("/helmcharts"):
            return {"items": [b for _, _, b in self.sent]}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def test_beside_servicelb_it_takes_only_its_class_on_the_nodes_interface(self):
        self.setup()
        result = ADDONS.install_kube_vip()
        chart = self.sent[0][2]
        self.assertEqual(("kube-vip", "kube-vip", "https://kube-vip.github.io/helm-charts"),
                         (chart["metadata"]["name"], chart["spec"]["chart"], chart["spec"]["repo"]))
        values = chart["spec"]["valuesContent"]
        for line in ('vip_interface: "eth0"', 'lb_class_only: "true"', 'lb_class_name: "kube-vip.io/kube-vip-class"',
                     'svc_enable: "true"', 'cp_enable: "false"', 'vip_arp: "true"'):
            self.assertIn(line, values)
        self.assertEqual(("eth0", True), (result["interface"], result["class_only"]))
        self.assertEqual(ADDONS.KUBE_VIP_CHART, chart["spec"]["version"], "installed at the tested chart")

    def test_a_chart_version_can_be_chosen_and_must_be_one(self):
        self.setup()
        ADDONS.install_kube_vip({"version": "0.12.0"})
        self.assertEqual("0.12.0", self.sent[0][2]["spec"]["version"])
        self.setup()
        with self.assertRaisesRegex(ValueError, "not a kube-vip chart version"):
            ADDONS.install_kube_vip({"version": "latest; rm"})

    def test_rke2_has_no_servicelb_so_it_takes_every_service(self):
        self.setup({"distribution": "rke2", "servicelb": False, "load_balancer": ""})
        ADDONS.install_kube_vip()
        self.assertNotIn("lb_class_only", self.sent[0][2]["spec"]["valuesContent"])

    def test_nodes_that_disagree_leave_the_interface_to_kube_vip(self):
        self.setup(probes={"n1": {"default_interface": "eth0"}, "n2": {"default_interface": "enp1s0"}})
        ADDONS.install_kube_vip()
        self.assertNotIn("vip_interface", self.sent[0][2]["spec"]["valuesContent"])

    def test_it_is_refused_beside_another_load_balancer(self):
        self.setup({"load_balancer": "metallb"})
        with self.assertRaisesRegex(ValueError, "MetalLB already"):
            ADDONS.install_kube_vip()


class PlatformTests(unittest.TestCase):
    def test_a_class_only_kube_vip_names_its_class(self):
        ds = {"spec": {"template": {"spec": {"containers": [{"env": [
            {"name": "lb_class_only", "value": "true"}, {"name": "svc_enable", "value": "true"}]}]}}}}
        self.assertEqual("kube-vip.io/kube-vip-class", PLATFORM._vip_class(ds))
        self.assertEqual("", PLATFORM._vip_class({"spec": {}}))
        with mock.patch.object(PLATFORM, "detect", return_value=K3S_KUBE_VIP):
            self.assertEqual({"loadBalancerClass": "kube-vip.io/kube-vip-class"}, PLATFORM.vip_spec("192.168.1.80"))
            self.assertEqual({}, PLATFORM.vip_spec(""))


class K3sVipServiceTests(unittest.TestCase):
    def setUp(self):
        base = test_networking.NetworkingTests("setUp")
        base.setUp()
        self.sent, self.objects = base.sent, base.objects
        node = "192.168.1.210"
        homestead = self.objects["/api/v1/services"]["items"][0]
        homestead["status"] = {"loadBalancer": {"ingress": [{"ip": node}]}}
        homestead["metadata"]["annotations"] = {}
        # Homestead's own list of VIPs to hand out on k3s.
        self.objects["/apis/loadbalancer.harvesterhci.io/v1beta1/ippools"] = {"items": [{
            "metadata": {"name": "lab"}, "spec": {"ranges": [{"rangeStart": "192.168.1.80", "rangeEnd": "192.168.1.81"}]},
            "status": {"total": 2, "available": 2, "conditions": [{"type": "Ready", "status": "True"}]}}]}
        get = self.objects.get

        def strict(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return get(path)
        networking.bind(strict, lambda m, p, b=None, **k: self.sent.append((m, p, b)) or b, {"kube-system"}, "lab", "")
        patcher = mock.patch.object(PLATFORM, "detect", return_value=K3S_KUBE_VIP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_automatic_vip_gets_kube_vips_class_and_its_address(self):
        networking.create_service({"namespace": "lab", "name": "pihole", "workload": "pihole", "type": "LoadBalancer",
                                   "vip_mode": "automatic",
                                   "ports": [{"port": 53, "target_port": 53, "protocol": "UDP"}]})
        body = self.sent[-1][2]
        self.assertEqual("kube-vip.io/kube-vip-class", body["spec"]["loadBalancerClass"])
        self.assertEqual("192.168.1.80", body["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])

    def test_the_nodes_addresses_stay_on_offer_and_a_vip_does_not_take_a_node_port(self):
        plan = networking.service_plan({"namespace": "lab", "name": "web", "workload": "pihole", "vip_mode": "nodes",
                                        "ports": [{"port": 9000, "target_port": 80, "protocol": "TCP"}]})
        self.assertEqual(("nodes", ""), (plan["vip_mode"], plan["vip"]))
        with self.assertRaisesRegex(ValueError, "port 8088/TCP is already answered by lab/homestead"):
            networking.service_plan({"namespace": "lab", "name": "web", "workload": "pihole", "vip_mode": "shared",
                                     "ports": [{"port": 8088, "target_port": 80, "protocol": "TCP"}]})
        # 8088 on a VIP of its own is fine: only the nodes' addresses have it.
        plan = networking.service_plan({"namespace": "lab", "name": "web", "workload": "pihole", "vip_mode": "automatic",
                                        "ports": [{"port": 8088, "target_port": 80, "protocol": "TCP"}]})
        self.assertEqual("192.168.1.80", plan["vip"])


if __name__ == "__main__":
    unittest.main()
