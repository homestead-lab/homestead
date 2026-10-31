"""Homestead's way in to Longhorn's own API on Longhorn 1.13, whose chart
fences longhorn-manager in with NetworkPolicies by default."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_longhorn_access as ACCESS
import homestead_operations as OPS
from fakes.kube import Kube

POLICIES = "/apis/networking.k8s.io/v1/namespaces/longhorn-system/networkpolicies"
MANAGER_LABELS = {"app": "longhorn-manager", "app.kubernetes.io/name": "longhorn"}


def longhorns_policy(name="longhorn-manager", selector=None, uid="uid-1", types=("Ingress",)):
    """One of Longhorn's own, as its 1.13 chart makes it."""
    spec = {"podSelector": selector if selector is not None else {"matchLabels": {"app": "longhorn-manager"}},
            "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "longhorn-ui"}}}]}]}
    if types is not None:
        spec["policyTypes"] = list(types)
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": "longhorn-system", "uid": uid, "resourceVersion": "5"},
            "spec": spec}


class Cluster:
    """Longhorn's policies and its manager (unless longhorn is False) on the
    shared fake API (tests/fakes/kube.py). get and send are the client's."""
    get, send = staticmethod(K.kget), staticmethod(K.ksend)

    def __init__(self, case, policies=(), longhorn=True):
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        case.enterContext(mock.patch.object(OPS, "DATA_DIR", case.enterContext(tempfile.TemporaryDirectory())))
        self.kube = case.enterContext(Kube(*policies))
        if longhorn:
            self.kube.add({"apiVersion": "apps/v1", "kind": "DaemonSet",
                           "metadata": {"name": "longhorn-manager", "namespace": "longhorn-system"},
                           "spec": {"template": {"metadata": {"labels": dict(MANAGER_LABELS)}}}})

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def forget_sent(self):
        self.kube.writes.clear()


class PolicyShapeTests(unittest.TestCase):
    def test_it_lets_only_homesteads_pods_reach_the_manager_on_9500(self):
        owner = longhorns_policy()
        policy = ACCESS.policy("lab", [owner])
        self.assertEqual("homestead-backend-access", policy["metadata"]["name"])
        self.assertEqual("longhorn-system", policy["metadata"]["namespace"])
        self.assertEqual("true", policy["metadata"]["labels"]["homestead.io/managed"])
        self.assertEqual({"podSelector": {"matchLabels": {"app": "longhorn-manager"}},
                          "policyTypes": ["Ingress"],
                          "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "lab"}},
                                                 "podSelector": {"matchLabels": {"app": "homestead"}}}],
                                       "ports": [{"protocol": "TCP", "port": 9500}]}]},
                         policy["spec"])
        self.assertEqual([{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                           "name": "longhorn-manager", "uid": "uid-1"}], policy["metadata"]["ownerReferences"])

    def test_selector_matching(self):
        labels = MANAGER_LABELS
        self.assertTrue(ACCESS.fences(longhorns_policy(), labels))
        self.assertTrue(ACCESS.fences(longhorns_policy(selector={}), labels), "an empty selector selects every pod")
        self.assertTrue(ACCESS.fences(longhorns_policy(types=None), labels), "no policyTypes means Ingress")
        self.assertTrue(ACCESS.fences(longhorns_policy(selector={"matchExpressions": [
            {"key": "app", "operator": "In", "values": ["longhorn-manager", "longhorn-ui"]}]}), labels))
        self.assertTrue(ACCESS.fences(longhorns_policy(selector={"matchExpressions": [
            {"key": "app", "operator": "Exists"}, {"key": "nope", "operator": "DoesNotExist"}]}), labels))
        self.assertFalse(ACCESS.fences(longhorns_policy(selector={"matchExpressions": [
            {"key": "app", "operator": "NotIn", "values": ["longhorn-manager"]}]}), labels))
        self.assertFalse(ACCESS.fences(longhorns_policy(selector={"matchLabels": {"app": "longhorn-ui"}}), labels))
        self.assertFalse(ACCESS.fences(longhorns_policy(types=("Egress",)), labels), "egress only fences nothing in")


class EnsureTests(unittest.TestCase):
    def test_made_where_longhorn_fences_its_manager_in(self):
        cluster = Cluster(self, [longhorns_policy()])
        self.assertEqual("made", ACCESS.ensure(cluster.get, cluster.send, "lab"))
        self.assertEqual([("POST", POLICIES)], [(m, p) for m, p, _ in cluster.sent])
        self.assertEqual(ACCESS.policy("lab", [longhorns_policy()]), cluster.sent[0][2])
        self.assertTrue(cluster.kube.object(f"{POLICIES}/homestead-backend-access"))

    def test_ensuring_again_changes_nothing(self):
        cluster = Cluster(self, [longhorns_policy()])
        ACCESS.ensure(cluster.get, cluster.send, "lab")
        cluster.forget_sent()
        self.assertEqual("", ACCESS.ensure(cluster.get, cluster.send, "lab"))
        self.assertEqual([], cluster.sent)

    def test_updated_when_longhorns_policies_change(self):
        cluster = Cluster(self, [longhorns_policy()])
        ACCESS.ensure(cluster.get, cluster.send, "lab")
        version = cluster.kube.object(f"{POLICIES}/homestead-backend-access")["metadata"]["resourceVersion"]
        cluster.kube.add(longhorns_policy("longhorn-webhook", {}, uid="uid-2"))
        cluster.forget_sent()
        self.assertEqual("updated", ACCESS.ensure(cluster.get, cluster.send, "lab"))
        method, path, body = cluster.sent[0]
        self.assertEqual(("PUT", f"{POLICIES}/homestead-backend-access"), (method, path))
        self.assertEqual(version, body["metadata"]["resourceVersion"])
        self.assertEqual(["longhorn-manager", "longhorn-webhook"],
                         [o["name"] for o in body["metadata"]["ownerReferences"]])

    def test_moved_namespace_is_updated(self):
        cluster = Cluster(self, [longhorns_policy()])
        ACCESS.ensure(cluster.get, cluster.send, "lab")
        self.assertEqual("updated", ACCESS.ensure(cluster.get, cluster.send, "homes"))

    def test_no_policy_where_longhorn_fences_nothing(self):
        cluster = Cluster(self, [longhorns_policy("other", {"matchLabels": {"app": "longhorn-ui"}})])
        self.assertEqual("", ACCESS.ensure(cluster.get, cluster.send, "lab"))
        self.assertEqual([], cluster.sent, "a policy of Homestead's alone would fence the manager in")

    def test_ours_is_removed_when_longhorn_drops_its_own(self):
        cluster = Cluster(self, [longhorns_policy()])
        ACCESS.ensure(cluster.get, cluster.send, "lab")
        cluster.send("DELETE", f"{POLICIES}/longhorn-manager")
        cluster.forget_sent()
        self.assertEqual("removed", ACCESS.ensure(cluster.get, cluster.send, "lab"))
        self.assertEqual([("DELETE", f"{POLICIES}/homestead-backend-access")], [(m, p) for m, p, _ in cluster.sent])
        self.assertIsNone(cluster.kube.object(f"{POLICIES}/homestead-backend-access"))

    def test_ours_does_not_count_as_longhorns(self):
        cluster = Cluster(self, [ACCESS.policy("lab", [longhorns_policy()])])
        self.assertEqual("removed", ACCESS.ensure(cluster.get, cluster.send, "lab"))

    def test_nothing_without_longhorn(self):
        cluster = Cluster(self, [longhorns_policy()], longhorn=False)
        self.assertEqual("", ACCESS.ensure(cluster.get, cluster.send, "lab"))
        self.assertEqual([], cluster.sent)

    def test_a_conflict_on_create_is_fine(self):
        cluster = Cluster(self, [longhorns_policy()])
        taken = []

        def another_writer_first(method, path, body):
            # Another writer makes ours between the list and the create: the fake answers 409 AlreadyExists.
            if not taken:
                taken.append(cluster.kube.add(ACCESS.policy("lab", [longhorns_policy()])))
                return {"items": [longhorns_policy()]}
        cluster.kube.react("GET", POLICIES, another_writer_first)
        self.assertEqual("", ACCESS.ensure(cluster.get, cluster.send, "lab"))
        self.assertEqual("POST", cluster.sent[0][0], "the create was tried and refused")


class RequireTests(unittest.TestCase):
    def test_reachable_lets_it_through_without_touching_anything(self):
        cluster = Cluster(self, [longhorns_policy()])
        with mock.patch.object(ACCESS, "reachable", return_value=""):
            ACCESS.require(cluster.get, cluster.send, "lab", "Rolling back")
        self.assertEqual([], cluster.sent)

    def test_unreachable_refuses_and_lets_homestead_in_for_next_time(self):
        cluster = Cluster(self, [longhorns_policy()])
        with mock.patch.object(ACCESS, "reachable", return_value="Connection refused"):
            with self.assertRaises(ValueError) as caught:
                ACCESS.require(cluster.get, cluster.send, "lab", "Rolling back")
        message = str(caught.exception)
        self.assertIn("Connection refused", message)
        self.assertIn("Nothing was changed", message)
        self.assertIn("try again in a minute", message)
        self.assertEqual(["homestead-backend-access"], [b["metadata"]["name"] for m, _, b in cluster.sent if m == "POST"])

    def test_unreachable_with_nothing_to_fix_says_so(self):
        cluster = Cluster(self, [])
        with mock.patch.object(ACCESS, "reachable", return_value="timed out"):
            with self.assertRaisesRegex(ValueError, "Nothing was changed") as caught:
                ACCESS.require(cluster.get, cluster.send, "lab", "Trimming")
        self.assertNotIn("try again in a minute", str(caught.exception))

    def test_an_http_answer_counts_as_reachable(self):
        error = urllib.error.HTTPError(ACCESS.URL, 404, "missing", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual("", ACCESS.reachable())
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))):
            self.assertIn("Connection refused", ACCESS.reachable())


if __name__ == "__main__":
    unittest.main()
