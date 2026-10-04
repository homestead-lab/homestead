import copy, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_networking as networking


DEPLOYMENT = {
    "metadata": {"name": "plex", "namespace": "lab"},
    "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "plex"}},
             "template": {"metadata": {"labels": {"app": "plex"}},
                          "spec": {"containers": [{"name": "plex", "ports": [
                              {"name": "web", "containerPort": 32400, "protocol": "TCP"}]}]}}},
}
SERVICE = {
    "metadata": {"name": "plex", "namespace": "lab",
                 "annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.245"}},
    "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.30", "selector": {"app": "plex"},
             "ports": [{"name": "web", "port": 32400, "targetPort": 32400, "protocol": "TCP"}]},
    "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.245"}]}},
}


class EditedPortsReachTheServiceTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.objects = {
            "/apis/apps/v1/namespaces/lab/deployments/plex": copy.deepcopy(DEPLOYMENT),
            "/api/v1/namespaces/lab/services": {"items": [copy.deepcopy(SERVICE)]},
            "/api/v1/services": {"items": [copy.deepcopy(SERVICE)]},
            "/apis/apps/v1/deployments": {"items": [copy.deepcopy(DEPLOYMENT)]},
            "/apis/discovery.k8s.io/v1/endpointslices": {"items": []},
            "/api/v1/nodes": {"items": []},
            "/apis/networking.k8s.io/v1/ingresses": {"items": []},
            "/apis/loadbalancer.harvesterhci.io/v1beta1/ippools": {"items": []},
            "/apis/apps/v1/namespaces/harvester-system/daemonsets/kube-vip": {
                "status": {"desiredNumberScheduled": 1, "numberReady": 1}},
        }

        def get(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return copy.deepcopy(self.objects[path])

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            if method == "DELETE":
                name = path.rsplit("/", 1)[-1]
                self.objects.pop(path, None)
                for listing in ("/api/v1/namespaces/lab/services", "/api/v1/services"):
                    self.objects[listing]["items"] = [s for s in self.objects[listing]["items"] if s["metadata"]["name"] != name]
            return body

        networking.bind(get, send, {"kube-system"}, "lab", "192.0.2.242")

    def test_changing_the_lan_port_moves_the_listener(self):
        message = networking.sync_workload_ports("lab", "plex", [
            {"name": "web", "container": 32400, "host": 32500, "protocol": "TCP", "expose": True}])

        method, path, body = self.sent[-1]
        self.assertEqual(("PUT", "/api/v1/namespaces/lab/services/plex"), (method, path))
        self.assertEqual(32500, body["spec"]["ports"][0]["port"])
        self.assertEqual(32400, body["spec"]["ports"][0]["targetPort"])
        self.assertIn("32500", message)

    def test_removing_every_port_removes_the_mapping(self):
        """An editor that submits no ports at all owns that decision."""
        message = networking.sync_workload_ports("lab", "plex", [])

        self.assertEqual(("DELETE", "/api/v1/namespaces/lab/services/plex"), self.sent[-1][:2])
        self.assertIn("released", message)

    def test_adding_a_second_port_publishes_both(self):
        networking.sync_workload_ports("lab", "plex", [
            {"name": "web", "container": 32400, "host": 32400, "protocol": "TCP", "expose": True},
            {"name": "dlna", "container": 1900, "host": 1900, "protocol": "UDP", "expose": True}])

        ports = self.sent[-1][2]["spec"]["ports"]
        self.assertEqual([(32400, "TCP"), (1900, "UDP")],
                         [(row["port"], row["protocol"]) for row in ports])


if __name__ == "__main__":
    unittest.main()


class EditAddressStepTests(EditedPortsReachTheServiceTests):
    """Edit's Address step, as Deploy's: the VIP and reachability are saved
    with the edit, and only what differs from now changes."""
    def setUp(self):
        super().setUp()
        service = copy.deepcopy(SERVICE)
        service["metadata"].update(uid="svc-uid", resourceVersion="7")
        service["metadata"]["annotations"]["homestead.io/vip-mode"] = "manual"
        for path in ("/api/v1/namespaces/lab/services", "/api/v1/services"):
            self.objects[path] = {"items": [copy.deepcopy(service)]}
        self.objects["/api/v1/namespaces/lab/services/plex"] = copy.deepcopy(service)

    def test_keeping_its_own_address_changes_nothing(self):
        self.assertEqual("", networking.set_workload_address("lab", "plex", "loadbalancer", "manual", "192.0.2.245"))
        self.assertEqual("", networking.set_workload_address("lab", "plex", "loadbalancer", "auto"),
                         "an automatic choice keeps the address it already has")
        self.assertEqual([], self.sent)

    def test_another_specific_vip_moves_the_service_in_place(self):
        networking.set_workload_address("lab", "plex", "loadbalancer", "manual", "192.0.2.250")
        method, path, body = self.sent[-1]
        self.assertEqual(("PUT", "/api/v1/namespaces/lab/services/plex"), (method, path))
        self.assertIn("192.0.2.250", str(body["metadata"]["annotations"]))
        self.assertEqual("10.43.0.30", body["spec"]["clusterIP"], "the Service is updated, not recreated")

    def test_cluster_only_replaces_the_lan_service_with_an_internal_one(self):
        networking.set_workload_address("lab", "plex", "internal")
        self.assertEqual(("DELETE", "/api/v1/namespaces/lab/services/plex"), self.sent[0][:2])
        method, path, body = self.sent[-1]
        self.assertEqual("POST", method)
        self.assertEqual("ClusterIP", body["spec"]["type"])
        self.assertEqual(32400, body["spec"]["ports"][0]["port"])

    def test_only_its_own_service_is_moved(self):
        self.objects["/api/v1/namespaces/lab/services"]["items"][0]["metadata"]["name"] = "shared-plex"
        self.assertEqual("", networking.set_workload_address("lab", "plex", "internal"))
        self.assertEqual([], self.sent)
