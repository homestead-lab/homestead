"""Which namespaces are yours, and making and removing them."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_namespaces as ns


def namespace(name, annotations=None):
    return {"metadata": {"name": name, "annotations": annotations or {}}, "status": {"phase": "Active"}}


class Cluster:
    def __init__(self, names, contents=None):
        self.items = [namespace(n) for n in names]
        self.contents = contents or {}
        self.sent = []

    def get(self, path):
        if path == "/api/v1/namespaces":
            return {"items": self.items}
        for (kind, name), count in self.contents.items():
            if f"/namespaces/{name}/{kind}" in path:
                return {"items": [{}] * count}
        return {"items": []}

    def send(self, method, path, body=None):
        self.sent.append((method, path, body))
        return {}


HARVESTER = ["default", "lab", "media", "kube-system", "cattle-system", "harvester-system", "longhorn-system",
             "fleet-local", "cluster-fleet-local-local-1a3d67d0a899", "p-7k2xm", "user-8fh3z", "u-b4qkz", "local",
             "cattle-impersonation-system", "harvester-public", "cdi"]


class NamespaceTests(unittest.TestCase):
    def use(self, names=HARVESTER, contents=None):
        self.cluster = Cluster(names, contents)
        ns.bind(self.cluster.get, self.cluster.send, "lab", "lab")

    def test_pickers_offer_only_places_for_apps(self):
        self.use()
        self.assertEqual(["default", "lab", "media"], ns.names())
        self.assertEqual(len(HARVESTER), len(ns.names(include_system=True)))

    def test_rancher_marks_its_own(self):
        self.assertTrue(ns.is_system(namespace("anything", {"management.cattle.io/system-namespace": "true"})))

    def test_a_namespace_is_made_and_labelled(self):
        self.use()
        ns.create("Photos")
        method, path, body = self.cluster.sent[-1]
        self.assertEqual(("POST", "/api/v1/namespaces", "photos"), (method, path, body["metadata"]["name"]))
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        for bad in ("kube-thing", "cattle-x", "p-abcde", "no_underscores", ""):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    ns.create(bad)

    def test_only_an_empty_namespace_of_yours_goes_and_only_when_named(self):
        self.use(contents={("persistentvolumeclaims", "media"): 2})
        for name, reason in (("lab", "default"), ("kube-system", "platform"), ("media", "2 volumes"),
                             ("default", "Kubernetes")):
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, reason):
                    ns.delete(name, name)
        self.use(names=["lab", "old"])
        with self.assertRaisesRegex(ValueError, "type old"):
            ns.delete("old", "")
        ns.delete("old", "old")
        self.assertEqual(("DELETE", "/api/v1/namespaces/old", None), self.cluster.sent[-1])


if __name__ == "__main__":
    unittest.main()
