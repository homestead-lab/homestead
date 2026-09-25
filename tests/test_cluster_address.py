import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_networking as NET

MAP = "/api/v1/namespaces/lab/configmaps/homestead-vips"
NODE = "192.0.2.201"
MGMT = "192.0.2.210"


class Cluster:
    """A Harvester cluster whose management VIP is carried by a kube-system
    Service, as Harvester's ingress-expose (or an rke2-traefik) carries it."""

    def __init__(self, services=(), vips=None, shared=MGMT, harvester_vip=None):
        self.services = list(services)
        self.map = {"data": {"vips.json": json.dumps(vips)}} if vips is not None else None
        self.harvester_vip = harvester_vip
        self.sent = []
        NET.bind(self.get, self.send, {"kube-system", "harvester-system"}, "lab", shared)

    def get(self, path):
        if path == MAP:
            if self.map is None:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return self.map
        if path == NET.HARVESTER_VIP:
            if self.harvester_vip is None:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return {"data": {"ip": self.harvester_vip, "enabled": "true"}}
        if path == "/api/v1/nodes":
            return {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": NODE}]}}]}
        if path == "/api/v1/services":
            return {"items": self.services}
        return {"items": []}

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if path.endswith("/configmaps") or path == MAP:
            self.map = body
        return body


def service(ip, name, namespace="lab", port=80, labels=None, selector=None):
    return {"metadata": {"namespace": namespace, "name": name, "labels": labels or {},
                         "annotations": {NET.VIP_ANNOTATION: ip}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.9", "selector": selector or {"app": name},
                     "ports": [{"port": port, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}]}}}


INGRESS = service(MGMT, "ingress-expose", "kube-system", 443)


def plan(mode, vip="", port=8080):
    return NET.service_plan({"namespace": "lab", "name": "app", "workload": "app", "type": "LoadBalancer",
                             "vip_mode": mode, "vip": vip, "ports": [{"port": port, "target_port": port}]},
                            require_workload=False)


class ClusterAddressTests(unittest.TestCase):
    """Homestead once put apps on Harvester's management VIP, and new hosts
    could no longer join through it on RKE2's 9345."""

    def test_the_management_vip_is_the_platforms(self):
        Cluster([INGRESS])
        state = NET.inventory()
        self.assertEqual({MGMT: "kube-system/ingress-expose"}, state["platform_addresses"])

    def test_harvesters_own_vip_setting_counts_whichever_service_has_it(self):
        Cluster([], harvester_vip="192.0.2.209")
        state = NET.inventory()
        self.assertIn("192.0.2.209", state["platform_addresses"])

    def test_the_shared_address_may_not_be_the_management_vip(self):
        Cluster([INGRESS], shared=MGMT)
        with self.assertRaisesRegex(ValueError, "LB_IP.*cluster's own address"):
            plan("shared")
        self.assertIn("cluster's own address", NET.inventory()["shared_vip"]["problem"])

    def test_a_specific_address_may_not_be_the_management_vip(self):
        Cluster([INGRESS], shared="192.0.2.242")
        with self.assertRaisesRegex(ValueError, "cluster's own address"):
            plan("manual", MGMT)

    def test_an_address_another_program_owns_is_refused(self):
        Cluster([service("192.0.2.230", "grafana", "monitoring", 3000)], shared="192.0.2.242")
        with self.assertRaisesRegex(ValueError, "monitoring/grafana, which Homestead did not create"):
            plan("manual", "192.0.2.230")

    def test_homesteads_own_services_still_share(self):
        Cluster([service("192.0.2.242", "homestead", port=8088),
                 service("192.0.2.242", "plex", "media", 32400,
                         labels={"homestead.io/managed": "true"})], shared="192.0.2.242")
        self.assertEqual("192.0.2.242", plan("shared")["vip"])
        self.assertEqual("192.0.2.242", plan("manual", "192.0.2.242", 9000)["vip"])

    def test_a_vip_of_your_own_on_the_management_address_is_never_handed_out(self):
        Cluster([INGRESS], vips=[{"ip": MGMT, "label": "oops"}, {"ip": "192.0.2.231", "label": ""}],
                shared="192.0.2.242")
        state = NET.inventory()
        self.assertEqual("192.0.2.231", plan("automatic")["vip"])
        blocked = {row["ip"]: row["blocked"] for row in state["registered_vips"]}
        self.assertIn("cluster's own address", blocked[MGMT])
        self.assertEqual("", blocked["192.0.2.231"])

    def test_the_management_vip_cannot_be_kept_as_a_vip(self):
        Cluster([INGRESS], shared="192.0.2.242")
        result = NET.add_vips({"start": "192.0.2.209", "end": MGMT})
        self.assertEqual(["192.0.2.209"], result["added"])
        self.assertTrue(any("cluster's own address" in row for row in result["skipped"]))

    def test_apps_already_on_the_management_vip_are_named(self):
        Cluster([INGRESS, service(MGMT, "jellyfin", port=8096)], shared="192.0.2.242")
        self.assertEqual([{"namespace": "lab", "service": "jellyfin", "ip": MGMT,
                           "owner": "kube-system/ingress-expose"}],
                         NET.inventory()["platform_clashes"])

    def test_k3s_servicelb_node_addresses_stay_shareable(self):
        """ServiceLB publishes every Service - traefik's included - on the
        nodes' own addresses, and the k3s bootstrap sets LB_IP to one."""
        Cluster([service(NODE, "traefik", "kube-system", 443)], shared=NODE)
        state = NET.inventory()
        self.assertEqual({}, state["platform_addresses"])
        self.assertEqual(NODE, plan("shared")["vip"])


if __name__ == "__main__":
    unittest.main()
