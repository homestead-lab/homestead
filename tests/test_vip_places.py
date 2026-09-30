import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_vips as VIPS

NOW = 1_800_000_000
STAMP = "2027-01-15T08:00:00.000000Z"  # NOW, as a Kubernetes MicroTime
CLASS = "kube-vip.io/kube-vip-class"
K3S = {"load_balancer": "kube-vip", "vip_class": CLASS, "servicelb": True, "vip_service_election": True}


def service(name, ports, vip=None, lease=None, status=None, cls=CLASS, ns="lab"):
    annotations = {}
    if vip:
        annotations["kube-vip.io/loadbalancerIPs"] = vip
    if lease:
        annotations["kube-vip.io/leaseName"] = lease
    spec = {"type": "LoadBalancer", "ports": [{"port": p, "protocol": "TCP"} for p in ports]}
    if cls:
        spec["loadBalancerClass"] = cls
    return {"metadata": {"namespace": ns, "name": name, "annotations": annotations}, "spec": spec,
            "status": {"loadBalancer": {"ingress": [{"ip": ip} for ip in status or []]}}}


def lease(name, holder, ns="lab", renewed=STAMP):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"holderIdentity": holder, "renewTime": renewed, "leaseDurationSeconds": 15}}


NODE = {"metadata": {"name": "k3s", "labels": {"node-role.kubernetes.io/control-plane": "true"}},
        "status": {"addresses": [{"type": "InternalIP", "address": "192.0.2.109"}],
                   "conditions": [{"type": "Ready", "status": "True"}]}}
SHARED = "homestead-vip-192-0-2-108"


def k3s_test():
    """k3s-test as it was found: three Services share .108 under one lease
    that k3s holds, and kube-vip recorded .108 on none of them; ServiceLB
    publishes Homestead on the node's own address."""
    return [service("homestead-objectstore", [9000, 9001], "192.0.2.108", SHARED),
            service("homestead-vip", [8088], "192.0.2.108", SHARED),
            service("plex", [32400], "192.0.2.108", SHARED),
            service("homestead", [8088], cls=None, status=["192.0.2.109"])]


class Placement(unittest.TestCase):
    def test_announced_but_unrecorded_is_unrouted_with_the_node_named(self):
        view = VIPS.address_map(k3s_test(), [NODE], [lease(SHARED, "k3s")], K3S,
                                endpoints={("lab", "homestead-objectstore"): 1, ("lab", "homestead-vip"): 1,
                                           ("lab", "plex"): 0, ("lab", "homestead"): 1},
                                targets={("lab", "plex"): ["plex"]}, now=NOW)
        vip = next(row for row in view["addresses"] if row["ip"] == "192.0.2.108")
        self.assertEqual((vip["kind"], vip["node"], vip["state"]), ("vip", "k3s", "unrouted"))
        self.assertIn("k3s answers for 192.0.2.108", vip["reason"])
        self.assertEqual(len(vip["unrouted"]), 3)
        self.assertEqual([l["port"] for l in vip["listeners"]], [8088, 9000, 9001, 32400])
        self.assertEqual(next(l for l in vip["listeners"] if l["port"] == 32400)["workloads"], ["plex"])
        node = view["nodes"][0]
        self.assertEqual((node["ips"], node["vips"], node["control_plane"]), (["192.0.2.109"], ["192.0.2.108"], True))
        own = next(row for row in view["addresses"] if row["ip"] == "192.0.2.109")
        self.assertEqual((own["kind"], own["node"], own["state"], own["controller"]), ("node", "k3s", "ok", "servicelb"))
        self.assertEqual(view["problems"], 1)

    def test_recorded_is_ok(self):
        services = [service("plex", [32400], "192.0.2.108", SHARED, status=["192.0.2.108"])]
        view = VIPS.address_map(services, [NODE], [lease(SHARED, "k3s")], K3S, endpoints={("lab", "plex"): 1}, now=NOW)
        vip = next(row for row in view["addresses"] if row["ip"] == "192.0.2.108")
        self.assertEqual((vip["state"], vip["reason"]), ("ok", ""))

    def test_nothing_running_is_idle_not_broken(self):
        services = [service("plex", [32400], "192.0.2.108", SHARED)]
        view = VIPS.address_map(services, [NODE], [lease(SHARED, "")], K3S, endpoints={("lab", "plex"): 0}, now=NOW)
        vip = next(row for row in view["addresses"] if row["ip"] == "192.0.2.108")
        self.assertEqual((vip["state"], vip["node"]), ("idle", ""))
        self.assertEqual(view["problems"], 0)

    def test_running_but_no_node_answers_is_unannounced(self):
        services = [service("plex", [32400], "192.0.2.108", SHARED)]
        view = VIPS.address_map(services, [NODE], [], K3S, endpoints={("lab", "plex"): 1}, now=NOW)
        vip = next(row for row in view["addresses"] if row["ip"] == "192.0.2.108")
        self.assertEqual(vip["state"], "unannounced")

    def test_a_lease_not_renewed_does_not_count(self):
        stale = lease(SHARED, "k3s", renewed="2027-01-15T07:00:00Z")
        self.assertEqual(VIPS.live_holders([stale], now=NOW), {})
        self.assertEqual(VIPS.live_holders([lease(SHARED, "k3s")], now=NOW), {("lab", SHARED): "k3s"})

    def test_harvester_style_per_service_lease_in_kube_system(self):
        harvester = {"load_balancer": "kube-vip", "vip_class": "", "servicelb": False, "vip_service_election": True}
        services = [service("grafana", [3000], "192.0.2.120", cls=None, status=["192.0.2.120"])]
        view = VIPS.address_map(services, [NODE], [lease("kubevip-grafana", "harvester-1", ns="kube-system")],
                                harvester, endpoints={("lab", "grafana"): 1}, now=NOW)
        vip = next(row for row in view["addresses"] if row["ip"] == "192.0.2.120")
        self.assertEqual((vip["node"], vip["state"]), ("harvester-1", "ok"))

    def test_servicelb_port_clash_is_pending_on_the_node(self):
        services = [service("homestead", [8088], cls=None, status=["192.0.2.109"]),
                    service("other", [8088], cls=None)]
        view = VIPS.address_map(services, [NODE], [], K3S, now=NOW)
        own = next(row for row in view["addresses"] if row["ip"] == "192.0.2.109")
        self.assertEqual(own["state"], "pending")
        self.assertIn("other", own["reason"])


class Keeping(unittest.TestCase):
    def test_records_what_the_answering_node_serves(self):
        fixes = VIPS.repairs(k3s_test(), [lease(SHARED, "k3s")], K3S, now=NOW)
        self.assertEqual(sorted(f["name"] for f in fixes), ["homestead-objectstore", "homestead-vip", "plex"])
        store = next(f for f in fixes if f["name"] == "homestead-objectstore")
        self.assertEqual(store["node"], "k3s")
        self.assertEqual(store["status"], {"loadBalancer": {"ingress": [{"ip": "192.0.2.108", "ports": [
            {"port": 9000, "protocol": "TCP"}, {"port": 9001, "protocol": "TCP"}]}]}})

    def test_leaves_alone_what_no_node_answers_for(self):
        self.assertEqual(VIPS.repairs(k3s_test(), [lease(SHARED, "")], K3S, now=NOW), [])
        self.assertEqual(VIPS.repairs(k3s_test(), [], K3S, now=NOW), [])

    def test_leaves_alone_what_is_recorded_and_what_servicelb_serves(self):
        services = [service("plex", [32400], "192.0.2.108", SHARED, status=["192.0.2.108"]),
                    service("homestead", [8088], cls=None)]
        self.assertEqual(VIPS.repairs(services, [lease(SHARED, "k3s")], K3S, now=NOW), [])

    def test_keep_patches_status_and_remembers(self):
        sent = []
        VIPS.bind(lambda path: {"items": k3s_test() if path.endswith("/services") else [lease(SHARED, "k3s", renewed=None)]},
                  lambda method, path, body=None, ctype="": sent.append((method, path, body, ctype)) or {})
        done = VIPS.keep(K3S)
        self.assertEqual(len(done), 3)
        method, path, body, ctype = next(s for s in sent if s[1].endswith("/plex/status"))
        self.assertEqual((method, path, ctype), ("PATCH", "/api/v1/namespaces/lab/services/plex/status",
                                                 "application/merge-patch+json"))
        self.assertEqual(body["status"]["loadBalancer"]["ingress"][0]["ip"], "192.0.2.108")
        self.assertTrue(any(k["name"] == "plex" for k in VIPS.kept()))

    def test_keep_does_nothing_without_kube_vip(self):
        VIPS.bind(lambda path: (_ for _ in ()).throw(AssertionError("read")), None)
        self.assertEqual(VIPS.keep({"load_balancer": "servicelb"}), [])

    def test_alert_only_for_addresses_that_need_someone(self):
        facts = VIPS.alert_facts({"addresses": [{"ip": "a", "state": "unrouted", "reason": "r"},
                                                {"ip": "b", "state": "idle"}, {"ip": "c", "state": "ok"}]})
        self.assertEqual([f["key"] for f in facts], ["address:a"])


if __name__ == "__main__":
    unittest.main()
