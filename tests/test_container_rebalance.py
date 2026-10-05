"""Moving containers so hosts carry similar CPU and memory - the fewest moves
worth a restart, each one vetoable, one at a time."""
import copy
import sys
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_container_rebalance as C

GI = 1024 ** 3


def node(name, cordoned=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"allocatable": {"cpu": "4", "memory": "16Gi"}, "conditions": [{"type": "Ready", "status": "True"}]}}


def deployment(name, replicas=1, pinned=None, host_path=False, claim=None):
    template = {"nodeSelector": {"kubernetes.io/hostname": pinned} if pinned else {},
                "volumes": ([{"name": "h", "hostPath": {"path": "/x"}}] if host_path else [])
                           + ([{"name": "d", "persistentVolumeClaim": {"claimName": claim}}] if claim else [])}
    return {"metadata": {"namespace": "lab", "name": name, "generation": 1},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}}, "template": {"spec": template}},
            "status": {"observedGeneration": 1, "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas}}


def pod(name, host, n=0):
    return {"metadata": {"namespace": "lab", "name": f"{name}-abc-{n}", "labels": {"app": name},
                         "ownerReferences": [{"kind": "ReplicaSet", "name": f"{name}-abc"}]},
            "spec": {"nodeName": host}, "status": {"phase": "Running"}}


def replicaset(name):
    return {"metadata": {"namespace": "lab", "name": f"{name}-abc", "ownerReferences": [{"kind": "Deployment", "name": name}]}}


class Cluster:
    """k2 runs the busy apps; k1 and k3 are quiet."""
    def __init__(self):
        apps = {"frigate": ("k2", "1500m", "3Gi"), "ha": ("k2", "400m", "1Gi"), "plex": ("k2", "900m", "2Gi"), "tiny": ("k2", "10m", "50Mi")}
        self.deps = {n: deployment(n) for n in apps}
        self.deps["plex"] = deployment("plex", pinned="k2")
        self.pods = [pod(n, host) for n, (host, _, _) in apps.items()]
        self.objects = {
            "/api/v1/nodes": {"items": [node("k1"), node("k2"), node("k3")]},
            "/apis/metrics.k8s.io/v1beta1/nodes": {"items": [
                {"metadata": {"name": "k1"}, "usage": {"cpu": "400m", "memory": "3Gi"}},
                {"metadata": {"name": "k2"}, "usage": {"cpu": "3200m", "memory": "11Gi"}},
                {"metadata": {"name": "k3"}, "usage": {"cpu": "500m", "memory": "4Gi"}}]},
            "/apis/metrics.k8s.io/v1beta1/pods": {"items": [
                {"metadata": {"namespace": "lab", "name": f"{n}-abc-0"}, "containers": [{"usage": {"cpu": c, "memory": m}}]}
                for n, (_, c, m) in apps.items()]},
            "/apis/apps/v1/replicasets": {"items": [replicaset(n) for n in apps]},
        }
        self.moved = []

    def get(self, path):
        if path == "/apis/apps/v1/deployments":
            return {"items": copy.deepcopy(list(self.deps.values()))}
        if path.startswith("/apis/apps/v1/namespaces/lab/deployments/"):
            return copy.deepcopy(self.deps[path.rsplit("/", 1)[1]])
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path.startswith("/api/v1/namespaces/lab/pods?labelSelector="):
            app = path.split("app%3D", 1)[1]
            return {"items": [copy.deepcopy(p) for p in self.pods if p["metadata"]["labels"]["app"] == app]}
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.objects[path])

    def apply(self, ns, name, to):
        self.moved.append((name, to))


def bind(c, satisfies=lambda n, r: (True, [])):
    C.bind(c.get, lambda dep: {}, satisfies, c.apply, ("lab", "homestead"), lambda: True)


class PlanTests(unittest.TestCase):
    def test_the_busiest_hosts_biggest_movable_app_goes_to_a_quiet_host(self):
        c = Cluster(); bind(c)
        plan = C.plan()
        self.assertEqual("lab/frigate", plan["moves"][0]["id"])
        self.assertIn(plan["moves"][0]["to"], ("k1", "k3"))
        self.assertNotIn("lab/tiny", [m["id"] for m in plan["moves"]], "not worth a restart")
        k2 = next(h for h in plan["hosts"] if h["name"] == "k2")
        self.assertLess(max(k2["cpu_after"], k2["mem_after"]), max(k2["cpu_before"], k2["mem_before"]))

    def test_a_pinned_app_stays_and_says_why(self):
        c = Cluster(); bind(c)
        plan = C.plan()
        self.assertNotIn("lab/plex", [m["id"] for m in plan["moves"]])
        self.assertIn({"id": "lab/plex", "why": "it is pinned to its host"}, plan["skipped"])

    def test_a_vetoed_app_stays_and_the_plan_works_round_it(self):
        c = Cluster(); bind(c)
        plan = C.plan(["lab/frigate"])
        self.assertNotIn("lab/frigate", [m["id"] for m in plan["moves"]])
        self.assertIn("lab/frigate", plan["apps"])

    def test_only_hosts_with_the_hardware_it_needs(self):
        c = Cluster(); bind(c, satisfies=lambda n, r: (n["metadata"]["name"] == "k3", []))
        self.assertTrue(all(m["to"] == "k3" for m in C.plan()["moves"]))

    def test_hosts_are_checked_on_homesteads_summary_not_the_kubernetes_node(self):
        # PLACE.satisfies reads status == "Ready"; a Kubernetes Node's status
        # is a dict, so given one every host was refused and nothing moved.
        import homestead_place as PLACE
        c = Cluster()
        summary = lambda: [{"name": n, "status": "Ready", "labels": {}, "allocatable": {}, "schedulable": True, "hardware": {}}
                           for n in ("k1", "k2", "k3")]
        C.bind(c.get, lambda dep: {"devices": [], "features": [], "resources": {}, "labels": {}},
               PLACE.satisfies, c.apply, ("lab", "homestead"), lambda: True, summary)
        self.assertEqual("lab/frigate", C.plan()["moves"][0]["id"])

    def test_when_the_busiest_host_has_nothing_to_move_the_next_one_is_balanced(self):
        c = Cluster()
        c.deps["frigate"] = deployment("frigate", pinned="k2")
        c.deps["ha"] = deployment("ha", pinned="k2")
        c.deps["tiny"] = deployment("tiny", pinned="k2")
        c.deps["busy"] = deployment("busy")
        c.pods.append(pod("busy", "k3"))
        c.objects["/apis/apps/v1/replicasets"]["items"].append(replicaset("busy"))
        c.objects["/apis/metrics.k8s.io/v1beta1/pods"]["items"].append(
            {"metadata": {"namespace": "lab", "name": "busy-abc-0"}, "containers": [{"usage": {"cpu": "1500m", "memory": "2Gi"}}]})
        c.objects["/apis/metrics.k8s.io/v1beta1/nodes"]["items"][2]["usage"] = {"cpu": "2400m", "memory": "6Gi"}
        bind(c)
        moves = C.plan()["moves"]
        self.assertEqual([("lab/busy", "k3", "k1")], [(m["id"], m["from"], m["to"]) for m in moves])

    def test_the_reviewed_moves_start_though_the_load_moved_since(self):
        c = Cluster(); bind(c)
        plan = C.plan()
        # Live CPU moves by the second: the same moves are the review.
        c.objects["/apis/metrics.k8s.io/v1beta1/pods"]["items"][0]["containers"][0]["usage"]["cpu"] = "1450m"
        self.assertEqual(plan["review_token"], C.plan()["review_token"])
        moves = C.reviewed(plan["moves"], [], plan["review_token"])
        self.assertEqual([(m["id"], m["from"], m["to"]) for m in plan["moves"]], [(m["id"], m["from"], m["to"]) for m in moves])

    def test_reviewed_moves_that_differ_or_no_longer_fit_need_a_new_review(self):
        c = Cluster(); bind(c)
        plan = C.plan()
        forged = [dict(plan["moves"][0], to="k2")]
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            C.reviewed(forged, [], plan["review_token"])
        c.pods[0]["spec"]["nodeName"] = "k3"                       # frigate moved meanwhile
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            C.reviewed(plan["moves"], [], plan["review_token"])
        c.pods[0]["spec"]["nodeName"] = "k2"
        target = plan["moves"][0]["to"]
        c.objects["/api/v1/nodes"]["items"] = [node(n, cordoned=(n == target)) for n in ("k1", "k2", "k3")]
        with self.assertRaisesRegex(ValueError, "cannot take"):
            C.reviewed(plan["moves"], [], plan["review_token"])

    def test_a_cordoned_host_takes_none(self):
        c = Cluster()
        c.objects["/api/v1/nodes"]["items"] = [node("k1", cordoned=True), node("k2"), node("k3")]
        bind(c)
        self.assertTrue(all(m["to"] == "k3" for m in C.plan()["moves"]))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster(); bind(self.c)
        self.item = {"id": "job", "ref": {"moves": [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1"}],
                                          "index": 0, "moved": 0, "stage": "move"}}

    def test_moves_then_waits_until_it_runs_on_its_new_host(self):
        state, _, message = C.status(self.item)
        self.assertEqual([("frigate", "k1")], self.c.moved)
        self.assertIn("restarts there", message)
        self.assertEqual("running", C.status(self.item)[0], "still on k2")
        self.c.pods[0]["spec"]["nodeName"] = "k1"
        C.status(self.item)
        state, _, message = C.status(self.item)
        self.assertEqual("succeeded", state)
        self.assertIn("Moved 1 container", message)

    def test_one_that_does_not_start_stops_the_rest(self):
        C.status(self.item)
        self.item["ref"]["started"] = time.time() - 700
        state, _, message = C.status(self.item)
        self.assertEqual("failed", state)
        self.assertIn("the rest were not moved", message)

    def test_one_moved_since_the_review_is_skipped(self):
        self.c.pods[0]["spec"]["nodeName"] = "k3"
        C.status(self.item)
        self.assertEqual([], self.c.moved)
        self.assertIn("moved or changed", " ".join(self.item["ref"]["skipped"]))

    def test_a_blocked_capacity_check_skips_it(self):
        def blocked(ns, name, to):
            raise ValueError("k1 has no room for it")
        C.bind(self.c.get, lambda dep: {}, lambda n, r: (True, []), blocked, ("lab", "homestead"), lambda: True)
        C.status(self.item)
        self.assertIn("no room", " ".join(self.item["ref"]["skipped"]))

    def test_only_the_leader_moves_anything(self):
        C.bind(self.c.get, lambda dep: {}, lambda n, r: (True, []), self.c.apply, ("lab", "homestead"), lambda: False)
        C.status(self.item)
        self.assertEqual([], self.c.moved)


if __name__ == "__main__":
    unittest.main()


class HostPathTests(unittest.TestCase):
    def test_devices_are_hardware_not_host_storage(self):
        for path, storage in (("/dev/dri", False), ("/dev/bus/usb", False), ("/dev/net/tun", False),
                              ("/mnt/user/media", True), ("/srv/data", True), ("/devices", True)):
            self.assertEqual(storage, C._host_storage({"hostPath": {"path": path}}), path)
