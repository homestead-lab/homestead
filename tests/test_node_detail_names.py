"""A host's detail and impact are read only for a host in the cluster: a name
that is not one is answered "not found" at once, keeps nothing in the cache
and reads no pods."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_nodes as NODES
import homestead_place as PLACE
import homestead_routes as ROUTER
from fakes.kube import Kube


def node(name):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": {}},
            "spec": {}, "status": {}}


def pod(ns, name, on):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"namespace": ns, "name": name, "labels": {"app": name}},
            "spec": {"nodeName": on}, "status": {"phase": "Running"}}


def request(**query):
    return types.SimpleNamespace(query={k: [v] for k, v in query.items()})


class NodeDetailNamesTests(unittest.TestCase):
    def setUp(self):
        self.kube = Kube(node("k1"), node("k2"), pod("lab", "web", "k1"))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.pod_lists = 0

        def count(method, path, body):
            self.pod_lists += 1
            return None
        self.kube.react("GET", "/api/v1/pods", count)

        def get_nodes():
            return [{"name": n["metadata"]["name"], "cpu": 1} for n in K.kget("/api/v1/nodes")["items"]]
        self.store = {}

        def cached(key, seconds, fn):
            if key not in self.store:
                self.store[key] = fn()
            return self.store[key]
        for patch in (mock.patch.object(ROUTER, "_cached", [cached]),
                      mock.patch.object(NODES, "get_nodes", get_nodes),
                      mock.patch.object(PLACE, "kget", K.kget),
                      mock.patch.object(PLACE, "get_nodes", get_nodes)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_an_unknown_node_name_is_not_found_caches_nothing_and_lists_no_pods(self):
        for handler, query in ((NODES._node, "name"), (NODES._impact, "node")):
            for name in ("nope", "k1x", "x" * 2000):
                answer = handler(request(**{query: name}))
                self.assertIsInstance(answer, ROUTER.Reply, (handler.__name__, name))
                self.assertEqual(404, answer.code)
        self.assertEqual(["nodes"], sorted(self.store))
        self.assertEqual(0, self.pod_lists)

    def test_many_unknown_names_add_no_keys(self):
        for i in range(1000):
            NODES._node(request(name=f"n{i}"))
            NODES._impact(request(node=f"n{i}"))
        self.assertEqual(["nodes"], sorted(self.store))

    def test_a_real_nodes_detail_and_impact_are_unchanged(self):
        self.assertEqual({"name": "k1", "cpu": 1}, NODES._node(request(name="k1")))
        impact = NODES._impact(request(node="k1"))
        self.assertEqual(PLACE.impact("k1"), impact)
        self.assertIn("node:k1", self.store)
        self.assertIn("impact:k1", self.store)
        self.assertGreater(self.pod_lists, 0)

    def test_impact_still_needs_a_node(self):
        with self.assertRaises(ValueError):
            NODES._impact(request())


if __name__ == "__main__":
    unittest.main()
