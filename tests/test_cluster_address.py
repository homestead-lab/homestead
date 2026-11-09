import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_networking as NET
import homestead_network_state as STATE
import homestead_network_inventory as INV
import homestead_network_services as SVC
import homestead_vip_registry as REG
import homestead_platform as PLATFORM
import homestead_operations as OPS
from fakes.kube import Kube
from fakes.platform import plain_platform
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

NODE = "192.0.2.201"
MGMT = "192.0.2.210"


def cluster(services=(), vips=None, harvester_vip=None):
    """A Harvester cluster whose management VIP is carried by a kube-system
    Service, as Harvester's ingress-expose (or an rke2-traefik) carries it,
    on the shared fake API (tests/fakes/kube.py)."""
    objects = [{"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node1"},
                "status": {"addresses": [{"type": "InternalIP", "address": NODE}]}}]
    objects += [{"apiVersion": "v1", "kind": "Service", **row} for row in services]
    if vips is not None:
        objects.append({"apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {"name": "homestead-vips", "namespace": "lab"},
                        "data": {"vips.json": json.dumps(vips)}})
    if harvester_vip is not None:
        objects.append({"apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {"name": "vip", "namespace": "harvester-system"},
                        "data": {"ip": harvester_vip, "enabled": "true"}})
    return Kube(*objects)


def service(ip, name, namespace="lab", port=80, labels=None, selector=None):
    return {"metadata": {"namespace": namespace, "name": name, "labels": labels or {},
                         "annotations": {STATE.VIP_ANNOTATION: ip}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.9", "selector": selector or {"app": name},
                     "ports": [{"port": port, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}]}}}


INGRESS = service(MGMT, "ingress-expose", "kube-system", 443)


def plan(mode, vip="", port=8080):
    return SVC.service_plan({"namespace": "lab", "name": "app", "workload": "app", "type": "LoadBalancer",
                             "vip_mode": mode, "vip": vip, "ports": [{"port": port, "target_port": port}]},
                            require_workload=False)


class ClusterAddressTests(unittest.TestCase):
    """Homestead once put apps on Harvester's management VIP, and new hosts
    could no longer join through it on RKE2's 9345."""

    def setUp(self):
        plain_platform(self)

    def cluster(self, services=(), vips=None, shared=MGMT, harvester_vip=None):
        self.kube = self.enterContext(cluster(services, vips, harvester_vip))
        # The shared fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        keep_bound(self, NET)
        NET.bind(K.kget, K.ksend, {"kube-system", "harvester-system"}, "lab", shared)
        return self.kube

    def test_the_management_vip_is_the_platforms(self):
        self.cluster([INGRESS])
        state = INV.inventory()
        self.assertEqual({MGMT: "kube-system/ingress-expose"}, state["platform_addresses"])

    def test_harvesters_own_vip_setting_counts_whichever_service_has_it(self):
        self.cluster([], harvester_vip="192.0.2.209")
        state = INV.inventory()
        self.assertIn("192.0.2.209", state["platform_addresses"])

    def test_the_shared_address_may_not_be_the_management_vip(self):
        self.cluster([INGRESS], shared=MGMT)
        with self.assertRaisesRegex(ValueError, "LB_IP.*cluster's own address"):
            plan("shared")
        self.assertIn("cluster's own address", INV.inventory()["shared_vip"]["problem"])

    def test_a_specific_address_may_not_be_the_management_vip(self):
        self.cluster([INGRESS], shared="192.0.2.242")
        with self.assertRaisesRegex(ValueError, "cluster's own address"):
            plan("manual", MGMT)

    def test_an_address_another_program_owns_is_refused(self):
        self.cluster([service("192.0.2.230", "grafana", "monitoring", 3000)], shared="192.0.2.242")
        with self.assertRaisesRegex(ValueError, "monitoring/grafana, which Homestead did not create"):
            plan("manual", "192.0.2.230")

    def flux(self, lease=None, annotations=None):
        row = service("192.0.2.242", "n8n", "automation", 5678,
                      labels={"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"})
        if lease:
            row["metadata"]["annotations"]["kube-vip.io/leaseName"] = lease
        row["metadata"]["annotations"].update(annotations or {})
        return row

    def test_a_service_gitops_made_on_the_shared_lease_shares_the_vip(self):
        # #306: Flux's Services carried Homestead's lease and class; the VIP read Unavailable.
        from unittest.mock import patch
        self.cluster([service("192.0.2.242", "homestead", port=8088), self.flux("homestead-vip-192-0-2-242")], shared="192.0.2.242")
        # kube-vip electing once for every Service: everything on an address goes together.
        with patch.object(PLATFORM, "detect", return_value={"load_balancer": "kube-vip", "vip_shared_lease": True, "vip_service_election": False}):
            self.assertEqual("192.0.2.242", plan("shared")["vip"])
            self.assertEqual("192.0.2.242", plan("manual", "192.0.2.242", 9000)["vip"])
            with self.assertRaisesRegex(ValueError, "already used by automation/n8n"):
                plan("manual", "192.0.2.242", 5678)   # a port both claim is still a conflict

    def test_a_service_on_its_own_lease_takes_the_address_and_says_how_to_share_it(self):
        from unittest.mock import patch
        self.cluster([self.flux("kubevip-n8n")], shared="192.0.2.242")
        with patch.object(PLATFORM, "detect", return_value={"load_balancer": "kube-vip", "vip_shared_lease": True, "vip_service_election": True}):
            with self.assertRaisesRegex(ValueError, "automation/n8n.*homestead-vip-192-0-2-242.*share-vip"):
                plan("manual", "192.0.2.242")

    def test_a_service_that_says_it_shares_shares(self):
        from unittest.mock import patch
        self.cluster([self.flux("kubevip-n8n", {"homestead.io/share-vip": "true"})], shared="192.0.2.242")
        with patch.object(PLATFORM, "detect", return_value={"load_balancer": "kube-vip", "vip_shared_lease": True, "vip_service_election": False}):
            self.assertEqual("192.0.2.242", plan("manual", "192.0.2.242")["vip"])

    def test_one_election_per_service_still_needs_one_lease_in_one_namespace(self):
        # Each Service's lease lives in its own namespace: automation's and
        # lab's are two elections, which could announce the address twice.
        from unittest.mock import patch
        self.cluster([self.flux("homestead-vip-192-0-2-242")], shared="192.0.2.242")
        with patch.object(PLATFORM, "detect", return_value={"load_balancer": "kube-vip", "vip_shared_lease": True, "vip_service_election": True}):
            self.assertEqual("", INV.address_problem("192.0.2.242"), "not held against it: it shares")
            with self.assertRaisesRegex(ValueError, "shared kube-vip lease in the same namespace"):
                plan("manual", "192.0.2.242", 9000)

    def test_one_election_for_every_service_shares_every_address(self):
        from unittest.mock import patch
        self.cluster([self.flux()], shared="192.0.2.242")
        with patch.object(PLATFORM, "detect", return_value={"load_balancer": "kube-vip", "vip_shared_lease": False, "vip_service_election": False}):
            self.assertEqual("192.0.2.242", plan("manual", "192.0.2.242")["vip"])

    def test_homesteads_own_services_still_share(self):
        self.cluster([service("192.0.2.242", "homestead", port=8088),
                 service("192.0.2.242", "plex", "media", 32400,
                         labels={"homestead.io/managed": "true"})], shared="192.0.2.242")
        self.assertEqual("192.0.2.242", plan("shared")["vip"])
        self.assertEqual("192.0.2.242", plan("manual", "192.0.2.242", 9000)["vip"])

    def test_a_vip_of_your_own_on_the_management_address_is_never_handed_out(self):
        self.cluster([INGRESS], vips=[{"ip": MGMT, "label": "oops"}, {"ip": "192.0.2.231", "label": ""}],
                shared="192.0.2.242")
        state = INV.inventory()
        self.assertEqual("192.0.2.231", plan("automatic")["vip"])
        blocked = {row["ip"]: row["blocked"] for row in state["registered_vips"]}
        self.assertIn("cluster's own address", blocked[MGMT])
        self.assertEqual("", blocked["192.0.2.231"])

    def test_the_management_vip_cannot_be_kept_as_a_vip(self):
        self.cluster([INGRESS], shared="192.0.2.242")
        result = REG.add_vips({"start": "192.0.2.209", "end": MGMT})
        self.assertEqual(["192.0.2.209"], result["added"])
        self.assertTrue(any("cluster's own address" in row for row in result["skipped"]))

    def test_apps_already_on_the_management_vip_are_named(self):
        self.cluster([INGRESS, service(MGMT, "jellyfin", port=8096)], shared="192.0.2.242")
        self.assertEqual([{"namespace": "lab", "service": "jellyfin", "ip": MGMT,
                           "owner": "kube-system/ingress-expose"}],
                         INV.inventory()["platform_clashes"])

    def test_k3s_servicelb_node_addresses_stay_shareable(self):
        """ServiceLB publishes every Service - traefik's included - on the
        nodes' own addresses, and the k3s bootstrap sets LB_IP to one."""
        self.cluster([service(NODE, "traefik", "kube-system", 443)], shared=NODE)
        state = INV.inventory()
        self.assertEqual({}, state["platform_addresses"])
        from unittest.mock import patch
        with patch.object(INV, "servicelb_present", return_value=True):
            result = plan("shared")
        self.assertEqual("nodes", result["vip_mode"])
        self.assertEqual("", result["vip"])


if __name__ == "__main__":
    unittest.main()
