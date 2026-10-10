"""Host impact: the address map (#372), taints (#373), capacity per
destination (#375) and detached volumes' copies (#379)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_place as place


def node(name, taints=None, cpu="4", memory="8Gi"):
    return {"name": name, "status": "Ready", "schedulable": True, "hardware": {}, "labels": {},
            "allocatable": {"cpu": cpu, "memory": memory}, "taints": taints or []}


def pod(ns, app, host, cpu="500m", memory="1Gi"):
    return {"metadata": {"namespace": ns, "labels": {"app": app}}, "status": {"phase": "Running"},
            "spec": {"nodeName": host, "containers": [{"name": app, "resources": {"requests": {"cpu": cpu, "memory": memory}}}]}}


def deployment(tolerations=None, cpu="500m", memory="1Gi"):
    return {"spec": {"template": {"spec": {"containers": [{"name": "app", "resources": {"requests": {"cpu": cpu, "memory": memory}}}],
                                           **({"tolerations": tolerations} if tolerations else {})}}}}


TIEBREAKER = [{"key": "node-role/tiebreaker", "value": "true", "effect": "NoSchedule"}]


class PlaceImpactTests(unittest.TestCase):
    def setUp(self):
        place.hardware_features = lambda: []
        self.nodes = [node("k3s-01"), node("k3s-02", memory="16Gi"), node("k3s-03", TIEBREAKER)]
        self.pods = [pod("lab", "web", "k3s-01"), pod("lab", "db", "k3s-01", memory="4Gi"), pod("lab", "other", "k3s-02", memory="2Gi")]
        self.deps = {"web": deployment(), "db": deployment(memory="4Gi")}
        place.get_nodes = lambda: self.nodes

        def kget(path):
            if path == "/api/v1/pods":
                return {"items": self.pods}
            return self.deps[path.rsplit("/", 1)[-1]]
        place.kget = kget

    def test_a_tainted_host_is_not_a_destination(self):
        result = place.impact("k3s-01")
        web = next(w for w in result["workloads"] if w["name"] == "web")
        self.assertEqual(["k3s-02"], web["eligible"], "the tiebreaker's taint is not tolerated")
        blocked = {b["name"]: b["why"] for b in web["blocked"]}
        self.assertIn("untolerated node-role/tiebreaker taint", blocked["k3s-03"])

    def test_a_tolerated_taint_is_a_destination(self):
        self.deps["web"] = deployment(tolerations=[{"key": "node-role/tiebreaker", "operator": "Exists"}])
        web = next(w for w in place.impact("k3s-01")["workloads"] if w["name"] == "web")
        self.assertEqual(["k3s-02", "k3s-03"], web["eligible"])

    def test_each_destination_says_what_it_holds_now_and_after(self):
        hosts = {h["name"]: h for h in place.impact("k3s-01")["hosts"]}
        two = hosts["k3s-02"]
        self.assertEqual(["lab/db", "lab/web"], sorted(two["apps"]))
        self.assertEqual(2 * 1024 ** 3, two["memory_now"])
        self.assertEqual(7 * 1024 ** 3, two["memory_after"], "both apps' requests arrive")
        self.assertEqual(1500, two["cpu_after"])
        self.assertEqual([], two["over"])
        self.assertEqual(["node-role/tiebreaker taint"], hosts["k3s-03"]["why"])
        self.assertEqual([], hosts["k3s-03"]["apps"])

    def test_a_host_that_would_be_overcommitted_says_so(self):
        self.nodes[1] = node("k3s-02", memory="4Gi")
        two = next(h for h in place.impact("k3s-01")["hosts"] if h["name"] == "k3s-02")
        self.assertEqual(["memory"], two["over"])


class NodeImpactWiringTests(unittest.TestCase):
    def test_the_address_map_is_unwrapped(self):
        import server
        captured = {}
        inventory = {"addresses": {"nodes": [{"name": "k3s-01"}],
                                   "addresses": [{"ip": "192.0.2.50", "node": "k3s-01", "kind": "vip", "services": ["lab/web"]}]}}

        def cached(key, ttl, fn):
            return {"nodes": [node("k3s-01"), node("k3s-02")], "network": inventory}.get(key, [])

        def preview(*args, **kw):
            captured["addresses"] = args[6]
            return {}
        with mock.patch.object(server, "cached", side_effect=cached), \
                mock.patch.object(server, "kget", return_value={"items": []}), \
                mock.patch.object(server.IMPACT, "preview", side_effect=preview):
            server.node_impact("k3s-01")
        self.assertEqual([{"ip": "192.0.2.50", "node": "k3s-01", "kind": "vip", "services": ["lab/web"]}], captured["addresses"])

    def test_preview_with_the_real_shape_runs(self):
        import homestead_impact as IMPACT
        result = IMPACT.preview("k3s-01", [], [], [], [], [], [{"ip": "192.0.2.50", "node": "k3s-01", "kind": "vip"}],
                                [node("k3s-01"), node("k3s-02")])
        self.assertEqual(["192.0.2.50"], [a["ip"] for a in result["addresses"]])


class DetachedCopiesTests(unittest.TestCase):
    def replica(self, volume, host, state, healthy=True, failed=False):
        return {"metadata": {"name": f"{volume}-{host}"}, "status": {"currentState": state},
                "spec": {"volumeName": volume, "nodeID": host, "diskPath": "/var/lib/longhorn",
                         **({"healthyAt": "2026-10-09T00:00:00Z"} if healthy else {}),
                         **({"failedAt": "2026-10-09T01:00:00Z"} if failed else {})}}

    def copies(self, replicas):
        import server

        def kget(path):
            return {"items": replicas if path.endswith("/replicas") else []}
        with mock.patch.object(server, "kget", side_effect=kget):
            return server.volume_copies()

    def test_a_detached_volumes_stopped_copies_are_whole(self):
        got = self.copies([self.replica("pg-dumps", "k3s-01", "stopped"), self.replica("pg-dumps", "k3s-02", "stopped")])
        self.assertEqual([True, True], [c["healthy"] for c in got["pg-dumps"]])
        self.assertTrue(all(c["detached"] for c in got["pg-dumps"]))

    def test_never_healthy_or_failed_copies_are_not(self):
        got = self.copies([self.replica("v", "k3s-01", "stopped", healthy=False),
                           self.replica("v", "k3s-02", "stopped", failed=True)])
        self.assertEqual([False, False], [c["healthy"] for c in got["v"]])

    def test_an_attached_volumes_stopped_copy_is_not(self):
        got = self.copies([self.replica("v", "k3s-01", "running"), self.replica("v", "k3s-02", "stopped")])
        self.assertEqual({"k3s-01": True, "k3s-02": False}, {c["node"]: c["healthy"] for c in got["v"]})

    def test_a_detached_volume_is_not_at_risk_when_a_host_goes_down(self):
        import homestead_impact as IMPACT
        volumes = [{"name": "pvc-1", "pvc_name": "pg-dumps", "namespace": "data", "replicas": 2,
                    "copies": [{"node": "k3s-01", "healthy": True}, {"node": "k3s-02", "healthy": True}]}]
        for host in ("k3s-01", "k3s-02"):
            result = IMPACT.preview(host, [], [], [], [], volumes, [], [node("k3s-01"), node("k3s-02")])
            self.assertEqual([], result["at_risk"])


if __name__ == "__main__":
    unittest.main()
