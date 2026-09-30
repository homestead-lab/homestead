"""Removing a failed k3s host, and leaving a clean cluster behind."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_onboard as onboard


def node(name, ready, roles=()):
    return {"metadata": {"name": name, "labels": {f"node-role.kubernetes.io/{r}": "true" for r in roles}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown",
                                       "lastTransitionTime": "2026-09-20T10:00:00Z"}],
                       "nodeInfo": {"kubeletVersion": "v1.31.4+k3s1"}}}


def deployment(name, claim=None, pin=None):
    pod = {"containers": [{"name": name}]}
    if claim:
        pod["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]
    if pin:
        pod["nodeSelector"] = {"kubernetes.io/hostname": pin}
    return {"metadata": {"name": name, "namespace": "lab"}, "spec": {"template": {"spec": pod}}}


class K3sCluster:
    def __init__(self):
        self.nodes = [node("node-1", True, ("control-plane", "etcd")), node("node-2", False)]
        self.deployments = [deployment("paperless", claim="paperless-data"), deployment("zigbee2mqtt", pin="node-2")]
        self.pvs = [{"metadata": {"name": "pvc-local-1"},
                     "spec": {"storageClassName": "local-path", "capacity": {"storage": "5Gi"},
                              "claimRef": {"namespace": "lab", "name": "paperless-data"},
                              "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                                  {"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-2"]}]}]}}}}]
        self.claims = {"paperless-data": {"metadata": {"name": "paperless-data", "namespace": "lab",
                                                        "annotations": {"volume.kubernetes.io/selected-node": "node-2",
                                                                        "pv.kubernetes.io/bind-completed": "yes"}},
                                           "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "local-path",
                                                    "resources": {"requests": {"storage": "5Gi"}},
                                                    "volumeName": "pvc-local-1"}}}
        self.secrets = {"node-2.node-password.k3s": {"metadata": {"name": "node-2.node-password.k3s"}}}
        self.sent = []
        onboard.bind(self.get, self.send, "lab")

    def get(self, path):
        names = [n["metadata"]["name"] for n in self.nodes]
        if path == "/api/v1/nodes":
            return {"items": self.nodes}
        if path.startswith("/api/v1/nodes/"):
            found = [n for n in self.nodes if n["metadata"]["name"] == path.rsplit("/", 1)[1]]
            if found:
                return found[0]
        if path == "/apis":
            return {"groups": [{"name": "apps"}]}
        if path == "/apis/apps/v1/deployments":
            return {"items": self.deployments}
        if path.startswith("/apis/apps/v1/namespaces/lab/deployments/"):
            return next(d for d in self.deployments if d["metadata"]["name"] == path.rsplit("/", 1)[1])
        if path == "/api/v1/persistentvolumes":
            return {"items": self.pvs}
        if path.startswith("/api/v1/persistentvolumes/"):
            found = [p for p in self.pvs if p["metadata"]["name"] == path.rsplit("/", 1)[1]]
            if found:
                return found[0]
        if path.startswith("/api/v1/namespaces/lab/persistentvolumeclaims/"):
            name = path.rsplit("/", 1)[1]
            if name in self.claims:
                return self.claims[name]
        if path == "/api/v1/namespaces/kube-system/secrets":
            return {"items": list(self.secrets.values())}
        if path.startswith("/api/v1/namespaces/kube-system/secrets/"):
            name = path.rsplit("/", 1)[1]
            if name in self.secrets:
                return self.secrets[name]
        if path.endswith("/statefulsets") or path.startswith("/api/v1/pods") or "volumeattachments" in path \
                or "virtualmachineinstances" in path or "longhorn" in path or "cluster.x-k8s.io" in path \
                or path.startswith("/api/v1/namespaces/lab/pods"):
            return {"items": []}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, ctype="application/json", **kw):
        self.sent.append((method, path, body))
        name = path.rsplit("/", 1)[1]
        if method == "DELETE":
            self.nodes = [n for n in self.nodes if n["metadata"]["name"] != name]
            self.secrets.pop(name, None)
            self.claims.pop(name, None)
            self.pvs = [p for p in self.pvs if p["metadata"]["name"] != name]
        if method == "POST" and path.endswith("/persistentvolumeclaims"):
            self.claims[body["metadata"]["name"]] = body
        return body


class K3sRemovalTests(unittest.TestCase):
    def setUp(self):
        self.c = K3sCluster()

    def test_the_plan_names_what_is_lost_and_what_is_tied_to_the_host(self):
        plan = onboard.removal_plan("node-2")
        self.assertTrue(plan["ok"])
        self.assertEqual("k3s", plan["distribution"])
        self.assertEqual([("lab", "paperless-data", ["paperless"])],
                         [(v["namespace"], v["claim"], v["users"]) for v in plan["pinned_volumes"]])
        self.assertEqual(["zigbee2mqtt"], [w["name"] for w in plan["pinned_workloads"]])
        self.assertTrue(any("node-password Secret" in step for step in plan["steps"]))
        self.assertFalse(any("Cluster API" in step for step in plan["steps"]), "k3s has no Cluster API machines")

    def test_gone_for_good_needs_the_loss_accepted(self):
        with self.assertRaisesRegex(ValueError, "made again empty; confirm"):
            onboard.remove_node("node-2", gone=True)
        self.assertEqual([], self.c.sent)

    @mock.patch.object(onboard.time, "sleep", lambda s: None)
    def test_gone_for_good_leaves_a_clean_cluster(self):
        result = onboard.remove_node("node-2", accept_loss=True, gone=True)
        log = " | ".join(result["log"])
        self.assertIn("Deleted node node-2", log)
        self.assertNotIn("node-2.node-password.k3s", self.c.secrets)
        self.assertIn("zigbee2mqtt may run on any host", log)
        self.assertNotIn("nodeSelector", self.c.deployments[1]["spec"]["template"]["spec"])
        remade = self.c.claims["paperless-data"]
        self.assertNotIn("volumeName", remade["spec"])
        self.assertNotIn("volume.kubernetes.io/selected-node", remade["metadata"]["annotations"],
                         "the new claim is not tied to the dead host")
        report = onboard.cleanup_report()
        self.assertEqual(([], [], [], []), (report["passwords"], report["pinned_volumes"],
                                            report["pinned_workloads"], report["attachments"]))

    def test_the_cleanup_card_finds_what_an_old_removal_left(self):
        self.c.nodes = [n for n in self.c.nodes if n["metadata"]["name"] != "node-2"]
        report = onboard.cleanup_report()
        self.assertEqual(["node-2.node-password.k3s"], [s["name"] for s in report["passwords"]])
        self.assertEqual(["lab/paperless-data"], [f"{v['namespace']}/{v['claim']}" for v in report["pinned_volumes"]])
        self.assertEqual(["zigbee2mqtt"], [w["name"] for w in report["pinned_workloads"]])
        with self.assertRaisesRegex(ValueError, "confirm making it again empty"):
            onboard.cleanup("pinned-volume", "lab/paperless-data")
        with mock.patch.object(onboard.time, "sleep", lambda s: None):
            onboard.cleanup("pinned-volume", "lab/paperless-data", force=True)
        self.assertNotIn("volumeName", self.c.claims["paperless-data"]["spec"])
        onboard.cleanup("password", "node-2.node-password.k3s")
        onboard.cleanup("pin", "lab/zigbee2mqtt")
        self.assertEqual([], onboard.cleanup_report()["pinned_workloads"])


if __name__ == "__main__":
    unittest.main()
