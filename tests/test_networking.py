import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import harvui_networking as networking


class NetworkingTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.objects = {
            "/api/v1/services": {"items": [{
                "metadata": {"name": "homestead", "namespace": "lab",
                             "annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.242"}},
                "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.20",
                         "selector": {"app": "homestead"},
                         "ports": [{"name": "web", "port": 8088, "targetPort": 8088, "protocol": "TCP"}]},
                "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.242"}]}}
            }]},
            "/apis/discovery.k8s.io/v1/endpointslices": {"items": [{
                "metadata": {"namespace": "lab", "labels": {"kubernetes.io/service-name": "homestead"}},
                "endpoints": [{"addresses": ["10.42.0.3"], "nodeName": "node-a",
                               "targetRef": {"kind": "Pod", "name": "homestead-123"},
                               "conditions": {"ready": True}}],
                "ports": [{"name": "web", "port": 8088, "protocol": "TCP"}],
            }]},
            "/apis/apps/v1/deployments": {"items": [{
                "metadata": {"name": "homestead", "namespace": "lab"},
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "homestead"}},
                         "template": {"metadata": {"labels": {"app": "homestead"}},
                                      "spec": {"containers": [{"name": "homestead", "ports": [
                                          {"name": "web", "containerPort": 8088, "protocol": "TCP"}]}]}}}
            }, {
                "metadata": {"name": "pihole", "namespace": "lab"},
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "pihole"}},
                         "template": {"metadata": {"labels": {"app": "pihole"}},
                                      "spec": {"containers": [{"name": "pihole", "ports": [
                                          {"name": "dns", "containerPort": 53, "protocol": "UDP"}]}]}}}
            }]},
            "/api/v1/nodes": {"items": [{"status": {"addresses": [
                {"type": "InternalIP", "address": "192.0.2.210"}]}}]},
            "/apis/networking.k8s.io/v1/ingresses": {"items": []},
            "/apis/loadbalancer.harvesterhci.io/v1beta1/ippools": {"items": [{
                "metadata": {"name": "lab-pool"},
                "spec": {"ranges": [{"rangeStart": "192.0.2.242", "rangeEnd": "192.0.2.244"}]},
                "status": {"total": 3, "available": 3, "conditions": [{"type": "Ready", "status": "True"}]}
            }]},
            "/apis/apps/v1/namespaces/harvester-system/daemonsets/kube-vip": {
                "status": {"desiredNumberScheduled": 2, "numberReady": 2}},
        }

        def get(path):
            return self.objects[path]

        def send(method, path, body, **kwargs):
            self.sent.append((method, path, body))
            return body

        networking.bind(get, send, {"kube-system", "harvester-system"}, "lab", "192.0.2.242")

    def test_inventory_reconciles_pool_against_live_services(self):
        state = networking.inventory()
        self.assertEqual(["192.0.2.243", "192.0.2.244"], state["available_vips"])
        self.assertEqual(3, state["pools"][0]["reported_available"])
        self.assertTrue(state["controller"]["healthy"])
        service = state["services"][0]
        self.assertEqual("healthy", service["health"])
        self.assertEqual("homestead-123", service["endpoints"]["ready"][0]["target"])

    def test_automatic_plan_chooses_first_reconciled_free_address(self):
        plan = networking.service_plan({
            "namespace": "lab", "name": "pihole-lan", "workload": "pihole",
            "vip_mode": "auto", "ports": [{"port": 53, "target_port": 53, "protocol": "UDP"}],
        })
        self.assertEqual("automatic", plan["vip_mode"])
        self.assertEqual("192.0.2.243", plan["vip"])

    def test_shared_vip_rejects_listener_collision(self):
        with self.assertRaisesRegex(ValueError, "already used"):
            networking.service_plan({
                "namespace": "lab", "name": "other", "workload": "pihole", "vip_mode": "shared",
                "ports": [{"port": 8088, "target_port": 53, "protocol": "TCP"}],
            })

    def test_create_uses_server_side_deployment_selector(self):
        result = networking.create_service({
            "namespace": "lab", "name": "pihole-lan", "workload": "pihole", "vip_mode": "automatic",
            "ports": [{"port": 53, "target_port": 53, "protocol": "UDP"}],
        })
        self.assertTrue(result["ok"])
        body = self.sent[0][2]
        self.assertEqual({"app": "pihole"}, body["spec"]["selector"])
        self.assertEqual("192.0.2.243", body["metadata"]["annotations"][networking.VIP_ANNOTATION])

    def test_internal_deploy_is_preflighted_as_cluster_ip(self):
        prepared = networking.prepare_deploy({
            "namespace": "lab", "name": "pihole", "network_mode": "internal", "vip_mode": "auto",
            "ports": [{"container": 53, "host": 53, "protocol": "UDP", "expose": True}],
        })
        self.assertEqual("cluster", prepared["vip_mode"])
        self.assertEqual("", prepared["lb_ip"])

    def test_inventory_retries_a_single_api_throttle(self):
        calls = []

        def throttled(path):
            calls.append(path)
            if len(calls) == 1:
                raise urllib.error.HTTPError(path, 429, "throttled", {"Retry-After": "0"}, None)
            return {"items": []}

        networking.bind(throttled, lambda *args, **kwargs: None, set(), "lab", "192.0.2.242")
        self.assertEqual([], networking._items("/api/v1/services"))
        self.assertEqual(2, len(calls))


if __name__ == "__main__":
    unittest.main()
