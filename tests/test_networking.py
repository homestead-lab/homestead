import sys
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_networking as networking


class NetworkingTests(unittest.TestCase):
    def edit_service_config(self):
        service = self.objects["/api/v1/services"]["items"][0]
        service["metadata"].update(uid="service-uid", resourceVersion="7")
        self.objects["/api/v1/namespaces/lab/services/homestead"] = service
        return {"namespace": "lab", "workload": "homestead", "name": "homestead",
                "update": True, "uid": "service-uid", "resource_version": "7", "type": "LoadBalancer",
                "vip_mode": "manual", "vip": "192.168.1.243",
                "ports": [{"port": 8088, "target_port": 8088}]}

    def test_vip_edit_preserves_service_identity_and_cluster_address(self):
        cfg = self.edit_service_config()
        with patch.object(networking.PLATFORM, "vip_spec", return_value={}):
            result = networking.create_service(cfg)
        method, path, service = self.sent[-1]
        self.assertEqual("PUT", method)
        self.assertEqual("10.43.0.20", service["spec"]["clusterIP"])
        self.assertEqual("service-uid", service["metadata"]["uid"])
        self.assertEqual("7", service["metadata"]["resourceVersion"])
        self.assertEqual("192.168.1.243", service["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        self.assertIn("updated", result["message"])

    def test_vip_edit_same_address_does_not_conflict_with_itself(self):
        cfg = self.edit_service_config()
        cfg.update(vip_mode="shared", vip="")
        with patch.object(networking.PLATFORM, "vip_spec", return_value={}):
            self.assertEqual("192.168.1.242", networking.service_plan(cfg)["vip"])

    def test_changed_default_cannot_silently_replace_reviewed_address(self):
        cfg = self.edit_service_config()
        cfg.update(vip_mode="shared", reviewed_vip="192.168.1.243")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            networking.create_service(cfg)
        self.assertFalse(self.sent)

    def test_vip_edit_rejects_stale_identity_and_foreign_workload(self):
        cfg = self.edit_service_config()
        for change in ({"uid": "replacement"}, {"resource_version": "6"}, {"workload": "pihole"}):
            with self.assertRaises(ValueError):
                networking.service_plan({**cfg, **change})
        self.assertFalse(self.sent)

    def test_vip_edit_never_recreates_to_change_class(self):
        cfg = self.edit_service_config()
        with patch.object(networking.PLATFORM, "vip_spec", return_value={"loadBalancerClass": "new-class"}):
            with self.assertRaisesRegex(ValueError, "replacement"):
                networking.create_service(cfg)
        self.assertFalse(self.sent)

    def test_vip_edit_checks_other_services_ports(self):
        import copy
        cfg = self.edit_service_config()
        other = copy.deepcopy(self.objects["/api/v1/services"]["items"][0])
        other["metadata"].update(name="another", uid="another-uid")
        other["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"] = cfg["vip"]
        other["status"] = {}
        self.objects["/api/v1/services"]["items"].append(other)
        with patch.object(networking.PLATFORM, "vip_spec", return_value={}):
            with self.assertRaisesRegex(ValueError, "already used"):
                networking.create_service(cfg)
        self.assertFalse(self.sent)

    def test_named_target_ports_survive_vip_only_edit(self):
        cfg = self.edit_service_config()
        cfg["ports"][0]["target_port"] = "web"
        with patch.object(networking.PLATFORM, "vip_spec", return_value={}):
            networking.create_service(cfg)
        self.assertEqual("web", self.sent[-1][2]["spec"]["ports"][0]["targetPort"])

    def test_vip_edit_preserves_ingress_port_name_and_protocol_metadata(self):
        cfg = self.edit_service_config()
        port = self.objects["/api/v1/services"]["items"][0]["spec"]["ports"][0]
        port.update(name="application-web-listener", nodePort=31080, appProtocol="http")
        with patch.object(networking.PLATFORM, "vip_spec", return_value={}):
            networking.create_service(cfg)
        updated = self.sent[-1][2]["spec"]["ports"][0]
        self.assertEqual("application-web-listener", updated["name"])
        self.assertEqual(31080, updated["nodePort"])
        self.assertEqual("http", updated["appProtocol"])

    def test_changed_listener_keeps_its_named_ingress_target(self):
        cfg = self.edit_service_config()
        cfg["ports"][0].update(name="web", port=9090)
        with patch.object(networking.PLATFORM, "vip_spec", return_value={}):
            networking.create_service(cfg)
        self.assertEqual("web", self.sent[-1][2]["spec"]["ports"][0]["name"])

    def setUp(self):
        self.sent = []
        self.objects = {
            "/api/v1/services": {"items": [{
                "metadata": {"name": "homestead", "namespace": "lab",
                             "annotations": {"kube-vip.io/loadbalancerIPs": "192.168.1.242"}},
                "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.20",
                         "selector": {"app": "homestead"},
                         "ports": [{"name": "web", "port": 8088, "targetPort": 8088, "protocol": "TCP"}]},
                "status": {"loadBalancer": {"ingress": [{"ip": "192.168.1.242"}]}}
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
                {"type": "InternalIP", "address": "192.168.1.210"}]}}]},
            "/apis/networking.k8s.io/v1/ingresses": {"items": []},
            "/apis/loadbalancer.harvesterhci.io/v1beta1/ippools": {"items": [{
                "metadata": {"name": "lab-pool"},
                "spec": {"ranges": [{"rangeStart": "192.168.1.242", "rangeEnd": "192.168.1.244"}]},
                "status": {"total": 3, "available": 3, "conditions": [{"type": "Ready", "status": "True"}]}
            }]},
            "/apis/apps/v1/namespaces/harvester-system/daemonsets/kube-vip": {
                "status": {"desiredNumberScheduled": 2, "numberReady": 2}},
        }

        self.objects["/api/v1/namespaces/lab/services"] = self.objects["/api/v1/services"]
        for deployment in self.objects["/apis/apps/v1/deployments"]["items"]:
            meta = deployment["metadata"]
            self.objects[f"/apis/apps/v1/namespaces/lab/deployments/{meta['name']}"] = deployment

        def get(path):
            return self.objects[path]

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            return body

        networking.bind(get, send, {"kube-system", "harvester-system"}, "lab", "192.168.1.242")

    def test_edited_lan_port_is_published_on_the_existing_service(self):
        message = networking.sync_workload_ports("lab", "homestead", [
            {"name": "web", "container": 8088, "host": 9090, "protocol": "TCP", "expose": True}])

        method, path, body = self.sent[-1]
        self.assertEqual(("PUT", "/api/v1/namespaces/lab/services/homestead"), (method, path))
        self.assertEqual([{"name": "web", "port": 9090, "targetPort": 8088, "protocol": "TCP"}],
                         body["spec"]["ports"])
        self.assertEqual("10.43.0.20", body["spec"]["clusterIP"], "the VIP and cluster IP are kept")
        self.assertIn("9090", message)

    def test_unexposing_every_port_removes_the_service(self):
        message = networking.sync_workload_ports("lab", "homestead", [
            {"name": "web", "container": 8088, "host": 8088, "protocol": "TCP", "expose": False}])

        self.assertEqual([("DELETE", "/api/v1/namespaces/lab/services/homestead", None)],
                         [(m, p, b) for m, p, b in self.sent])
        self.assertIn("released", message)

    def test_unchanged_ports_do_not_touch_the_service(self):
        self.assertEqual("", networking.sync_workload_ports("lab", "homestead", [
            {"name": "web", "container": 8088, "host": 8088, "protocol": "TCP", "expose": True}]))
        self.assertEqual([], self.sent)

    def test_exposing_a_workload_without_a_service_creates_one(self):
        message = networking.sync_workload_ports("lab", "pihole", [
            {"name": "dns", "container": 53, "host": 53, "protocol": "UDP", "expose": True}],
            vip_mode="automatic")

        method, path, body = self.sent[-1]
        self.assertEqual(("POST", "/api/v1/namespaces/lab/services"), (method, path))
        self.assertEqual({"app": "pihole"}, body["spec"]["selector"])
        self.assertIn("192.168.1.243", message)

    def test_a_lan_port_another_service_already_answers_is_refused(self):
        self.objects["/api/v1/services"]["items"].append({
            "metadata": {"name": "pihole", "namespace": "lab",
                         "annotations": {"kube-vip.io/loadbalancerIPs": "192.168.1.242"}},
            "spec": {"type": "LoadBalancer", "selector": {"app": "pihole"},
                     "ports": [{"name": "dns", "port": 53, "targetPort": 53, "protocol": "UDP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": "192.168.1.242"}]}}})

        with self.assertRaisesRegex(ValueError, "already answered by"):
            networking.sync_workload_ports("lab", "homestead", [
                {"name": "dns", "container": 8088, "host": 53, "protocol": "UDP", "expose": True}])

    def test_a_service_whose_workload_is_gone_is_marked_orphaned(self):
        self.objects["/api/v1/services"]["items"].append({
            "metadata": {"name": "ghost", "namespace": "lab",
                         "annotations": {"kube-vip.io/loadbalancerIPs": "192.168.1.244"}},
            "spec": {"type": "LoadBalancer", "selector": {"app": "deleted-app"},
                     "ports": [{"name": "web", "port": 8080, "targetPort": 8080, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": "192.168.1.244"}]}}})

        rows = {row["name"]: row for row in networking.inventory()["services"]}
        self.assertTrue(rows["ghost"]["orphaned"])
        self.assertFalse(rows["homestead"]["orphaned"], "a served Service is not orphaned")

    def test_an_orphaned_listener_can_be_released(self):
        self.objects["/api/v1/services"]["items"].append({
            "metadata": {"name": "ghost", "namespace": "lab",
                         "annotations": {"kube-vip.io/loadbalancerIPs": "192.168.1.244"}},
            "spec": {"type": "LoadBalancer", "selector": {"app": "deleted-app"},
                     "ports": [{"name": "web", "port": 8080, "targetPort": 8080, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": "192.168.1.244"}]}}})

        result = networking.delete_service("lab", "ghost")

        self.assertEqual(("DELETE", "/api/v1/namespaces/lab/services/ghost"), self.sent[-1][:2])
        self.assertEqual(["192.168.1.244:8080/TCP"], result["freed"])

    def test_deleting_a_service_that_still_serves_a_workload_needs_force(self):
        with self.assertRaisesRegex(ValueError, "still serves homestead"):
            networking.delete_service("lab", "homestead")
        self.assertEqual([], self.sent)

        networking.delete_service("lab", "homestead", force=True)
        self.assertEqual(("DELETE", "/api/v1/namespaces/lab/services/homestead"), self.sent[-1][:2])

    def test_system_namespace_services_are_off_limits(self):
        with self.assertRaises(PermissionError):
            networking.delete_service("kube-system", "kube-dns")

    def test_inventory_reconciles_pool_against_live_services(self):
        state = networking.inventory()
        self.assertEqual(["192.168.1.243", "192.168.1.244"], state["available_vips"])
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
        self.assertEqual("192.168.1.243", plan["vip"])

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
        self.assertEqual("192.168.1.243", body["metadata"]["annotations"][networking.VIP_ANNOTATION])

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

        networking.bind(throttled, lambda *args, **kwargs: None, set(), "lab", "192.168.1.242")
        self.assertEqual([], networking._items("/api/v1/services"))
        self.assertEqual(2, len(calls))


if __name__ == "__main__":
    unittest.main()
