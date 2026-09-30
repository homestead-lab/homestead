import sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_networking as networking
import test_networking


class ServiceLbTests(unittest.TestCase):
    """On k3s, exposing a port failed: the edit saved the container, then the
    Service wanted the shared Homestead VIP, which a k3s install has none of -
    so the port never stayed exposed. ServiceLB puts every Service on every
    node's own address, so there is no VIP to find, only a free port."""

    def setUp(self):
        base = test_networking.NetworkingTests("setUp")
        base.setUp()
        self.sent = base.sent
        # A k3s cluster: Traefik and Homestead published on the node's address.
        node = "192.0.2.210"
        base.objects["/api/v1/services"]["items"][0]["status"] = {"loadBalancer": {"ingress": [{"ip": node}]}}
        base.objects["/api/v1/services"]["items"][0]["metadata"]["annotations"] = {}
        base.objects["/api/v1/services"]["items"].append({
            "metadata": {"name": "traefik", "namespace": "kube-system"},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.5", "selector": {"app": "traefik"},
                     "ports": [{"name": "web", "port": 80, "targetPort": 8000, "protocol": "TCP"},
                               {"name": "websecure", "port": 443, "targetPort": 8443, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": node}]}}})
        del base.objects["/apis/apps/v1/namespaces/harvester-system/daemonsets/kube-vip"]
        base.objects["/apis/loadbalancer.harvesterhci.io/v1beta1/ippools"] = {"items": []}
        get = base.objects.get

        def strict(path):
            if path not in base.objects:
                raise networking.urllib.error.HTTPError(path, 404, "missing", {}, None)
            return get(path)
        networking.bind(strict, lambda m, p, b=None, **k: self.sent.append((m, p, b)) or b,
                        {"kube-system"}, "lab", "")  # no LB_IP, as a k3s install has
        patcher = mock.patch.object(networking.PLATFORM, "detect", return_value={"load_balancer": "servicelb"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exposing_a_port_needs_no_shared_vip(self):
        message = networking.sync_workload_ports("lab", "pihole", [
            {"name": "dns", "container": 53, "host": 53, "protocol": "UDP", "expose": True}])
        method, path, body = self.sent[-1]
        self.assertEqual(("POST", "/api/v1/namespaces/lab/services"), (method, path))
        self.assertEqual("LoadBalancer", body["spec"]["type"])
        self.assertNotIn("kube-vip.io/loadbalancerIPs", body["metadata"]["annotations"])
        self.assertIn("created", message)

    def test_a_port_another_service_has_is_refused_before_anything_changes(self):
        with self.assertRaisesRegex(ValueError, r"port 80/TCP is already answered by kube-system/traefik.*another LAN port"):
            networking.prepare_deploy({"name": "web", "namespace": "lab", "network_mode": "loadbalancer",
                                       "ports": [{"container": 8080, "host": 80, "protocol": "TCP", "expose": True}]})
        self.assertEqual([], self.sent)

    def test_every_mode_becomes_the_nodes_addresses(self):
        for mode in ("shared", "auto", "manual"):
            with self.subTest(mode=mode):
                cfg = networking.prepare_deploy({"name": "web", "namespace": "lab", "network_mode": "loadbalancer",
                                                 "vip_mode": mode, "lb_ip": "192.0.2.99",
                                                 "ports": [{"container": 8080, "host": 8080, "protocol": "TCP",
                                                            "expose": True}]})
                self.assertEqual(("nodes", ""), (cfg["vip_mode"], cfg["lb_ip"]))


if __name__ == "__main__":
    unittest.main()
