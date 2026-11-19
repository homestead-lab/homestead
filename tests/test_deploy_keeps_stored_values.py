"""Deploying into an existing app never deletes a stored value (#1396).

run_deploy runs for real; the cluster is an in-memory store behind kget/ksend,
and the steps around the Secret (network, claims, operations) are stubbed.
"""
import base64
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_env_secrets as ENVSEC
import homestead_deploy_manifests as MANIFESTS
import server
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import no_cluster


def setUpModule():
    unittest.enterModuleContext(no_cluster())


def b64(text):
    return base64.b64encode(text.encode()).decode()


NS = "lab"
SECRET_PATH = f"/api/v1/namespaces/{NS}/secrets/media-env"
DEPLOYMENTS = f"/apis/apps/v1/namespaces/{NS}/deployments"
# Built in pieces: token-shaped strings are never written whole.
NEW_TOKEN = "tok" + "en-" + "new-value"
OLD_VALUE = "kept" + "-old-value"
LEFTOVER = "left" + "over-value"


def labelled_secret(data):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "media-env", "namespace": NS,
                         "labels": {ENVSEC.LABEL: "media", "app.kubernetes.io/managed-by": "homestead"}},
            "data": data}


def media(annotated=True):
    """An existing app whose first container refers to one stored value."""
    env = [{"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "media-env", "key": "main.API_KEY"}}}]
    meta = {"name": "media", "namespace": NS, "resourceVersion": "7"}
    if annotated:
        meta["annotations"] = {ENVSEC.ANNOTATION: "media-env"}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
            "spec": {"selector": {"matchLabels": {"app": "media"}},
                     "template": {"metadata": {"labels": {"app": "media"}},
                                  "spec": {"containers": [{"name": "main", "image": "example/main", "env": env}]}}}}


class Cluster:
    def __init__(self, objects=None):
        self.objects, self.sent = dict(objects or {}), []

    def read(self, path, *args, **kwargs):
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        raise urllib.error.HTTPError(path, 404, "not found", {}, None)

    def send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            path += "/" + body["metadata"]["name"]
        self.objects[path] = copy.deepcopy(body)
        return body

    def secret_writes(self):
        return [s for s in self.sent if "/secrets" in s[1]]


class DeployKeepsStoredValues(unittest.TestCase):
    def deploy(self, cluster, cfg, reviewed_current=None):
        patches = [mock.patch.object(module, name, side_effect=lambda b, *a, **k: b) for module, name in (
                       (MANIFESTS, "analyze_deploy_intent"), (server.DEPLOY, "ensure_profile_compatible"),
                       (server.DEPLOY, "prepare_deploy_network"), (server.DEPLOY, "apply_deploy_bindings"),
                       (server.DEPLOY, "apply_generated_secrets"), (server.DEPLOY, "prepare_lan"))]
        patches += [mock.patch.object(server.DEPLOY, "guard_managed_smb"),
                    mock.patch.object(server.DEPLOY, "persist_icon_config"),
                    mock.patch.object(server.DEPLOY.VOLOWNER, "prepare", side_effect=lambda b: b),
                    mock.patch.object(server.DEPLOY, "new_claims", return_value=[]),
                    mock.patch.object(server.DEPLOY, "deployment_volumes", return_value=[]),
                    mock.patch.object(server.DEPLOY.OPS, "start", return_value={"id": "op"}),
                    mock.patch.object(server.DEPLOY, "kget", side_effect=cluster.read),
                    mock.patch.object(server.DEPLOY, "ksend", side_effect=cluster.send)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return server.DEPLOY.run_deploy(cfg, reviewed_current=reviewed_current)

    def join_cfg(self, **extra):
        cfg = {"name": "helper", "image": "example/helper", "namespace": NS, "target_workload": "media",
               "target_mode": "existing", "network_mode": "internal", "ports": [], "volumes": [], "hardware": [],
               "env": {"TZ": "UTC"}}
        cfg.update(extra)
        return cfg

    def new_cfg(self, **extra):
        cfg = {"name": "media", "image": "example/main", "namespace": NS, "network_mode": "internal",
               "ports": [], "volumes": [], "hardware": [], "env": {"TZ": "UTC"}}
        cfg.update(extra)
        return cfg

    def test_joining_an_app_keeps_a_stored_value_nothing_refers_to(self):
        cluster = Cluster({SECRET_PATH: labelled_secret({"main.API_KEY": b64(OLD_VALUE), "main.GONE": b64(LEFTOVER)}),
                           f"{DEPLOYMENTS}/media": media()})
        self.deploy(cluster, self.join_cfg())
        held = cluster.objects[SECRET_PATH]["data"]
        self.assertEqual(b64(LEFTOVER), held.get("main.GONE"))
        self.assertEqual(b64(OLD_VALUE), held["main.API_KEY"])
        for method, path, body in cluster.secret_writes():
            self.assertIn("main.GONE", body["data"], (method, path))

    def test_joining_still_moves_the_new_containers_secret_into_the_secret(self):
        cluster = Cluster({SECRET_PATH: labelled_secret({"main.API_KEY": b64(OLD_VALUE), "main.GONE": b64(LEFTOVER)}),
                           f"{DEPLOYMENTS}/media": media()})
        self.deploy(cluster, self.join_cfg(env={"TZ": "UTC", "ADMIN_TOKEN": NEW_TOKEN},
                                           env_meta=[{"key": "ADMIN_TOKEN", "masked": True}]))
        held = cluster.objects[SECRET_PATH]["data"]
        self.assertEqual(b64(NEW_TOKEN), held["helper.ADMIN_TOKEN"])
        self.assertEqual({"main.API_KEY", "main.GONE", "helper.ADMIN_TOKEN"}, set(held))
        put = cluster.objects[f"{DEPLOYMENTS}/media"]
        helper = [c for c in put["spec"]["template"]["spec"]["containers"] if c["name"] == "helper"][0]
        ref = [e for e in helper["env"] if e["name"] == "ADMIN_TOKEN"][0]["valueFrom"]["secretKeyRef"]
        self.assertEqual({"name": "media-env", "key": "helper.ADMIN_TOKEN"}, ref)
        self.assertNotIn(NEW_TOKEN, str(put))

    def test_a_new_app_still_drops_unused_keys_of_a_leftover_secret_of_its_name(self):
        # Scope pin: only a deploy into an existing app keeps every stored value.
        cluster = Cluster({SECRET_PATH: labelled_secret({"main.GONE": b64(LEFTOVER)})})
        self.deploy(cluster, self.new_cfg(env={"TZ": "UTC", "ADMIN_TOKEN": NEW_TOKEN},
                                          env_meta=[{"key": "ADMIN_TOKEN", "masked": True}]))
        held = cluster.objects[SECRET_PATH]["data"]
        self.assertNotIn("main.GONE", held)
        self.assertEqual(b64(NEW_TOKEN), held["media.ADMIN_TOKEN"])

    def test_the_secret_is_written_before_the_deployment_when_joining(self):
        cluster = Cluster({SECRET_PATH: labelled_secret({"main.API_KEY": b64(OLD_VALUE)}),
                           f"{DEPLOYMENTS}/media": media()})
        self.deploy(cluster, self.join_cfg(env={"ADMIN_TOKEN": NEW_TOKEN},
                                           env_meta=[{"key": "ADMIN_TOKEN", "masked": True}]))
        writes = [(m, p) for m, p, _ in cluster.sent if m in ("POST", "PUT")]
        self.assertEqual([("PUT", SECRET_PATH), ("PUT", f"{DEPLOYMENTS}/media")], writes)


if __name__ == "__main__":
    unittest.main()
