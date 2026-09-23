"""Homestead's own permissions, kept to exactly what its release describes."""
import copy
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import homestead_self as SELF
import render_rbac

RBAC = "/apis/rbac.authorization.k8s.io/v1"


def http(code):
    return urllib.error.HTTPError("x", code, "x", {}, io.BytesIO(b""))


class Cluster:
    """Just enough of the Kubernetes API: objects by path, collections by prefix."""

    def __init__(self, objects, forbid=()):
        self.objects = {path: copy.deepcopy(body) for path, body in objects.items()}
        self.forbid = set(forbid)
        self.calls = []

    def get(self, path):
        path = path.split("?")[0]
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        # A list: every object of that kind under that API, in any namespace.
        base, plural = path.rsplit("/", 1)
        base = base.split("/namespaces/")[0]
        children = [body for key, body in self.objects.items()
                    if key.startswith(base + "/") and key.split("/")[-2] == plural
                    and (("/namespaces/" not in path) or key.startswith(path + "/"))]
        if children or plural.endswith("s"):
            return {"items": copy.deepcopy(children)}
        raise http(404)

    def send(self, method, path, body=None, ctype=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if (method, path) in self.forbid:
            raise http(403)
        if method == "PUT":
            self.objects[path] = copy.deepcopy(body)
        elif method == "PATCH":
            meta = self.objects[path].setdefault("metadata", {})
            for field, change in (body.get("metadata") or {}).items():
                values = meta.setdefault(field, {})
                for key, value in change.items():
                    if value is None:
                        values.pop(key, None)
                    else:
                        values[key] = value
        return {}


def installed(rules=None, subjects=("homestead",), account="homestead"):
    return {
        "/api/v1/namespaces/lab/pods/homestead-abc-1": {"spec": {"serviceAccountName": account}},
        f"{RBAC}/clusterroles/homestead": {"metadata": {"name": "homestead", "resourceVersion": "7"},
                                           "rules": SELF.desired_rules() if rules is None else rules},
        f"{RBAC}/clusterrolebindings/homestead": {
            "metadata": {"name": "homestead"}, "roleRef": {"kind": "ClusterRole", "name": "homestead"},
            "subjects": [{"kind": "ServiceAccount", "name": s, "namespace": "lab"} for s in subjects]},
    }


class SelfTest(unittest.TestCase):
    def use(self, cluster):
        self.cluster = cluster
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        SELF.bind(cluster.get, cluster.send, "lab", "2.8.73", self.dir.name)
        patcher = mock.patch.object(SELF, "POD", "homestead-abc-1")
        patcher.start()
        self.addCleanup(patcher.stop)
        return cluster

    def role(self):
        return self.cluster.objects[f"{RBAC}/clusterroles/homestead"]["rules"]


class ManifestTests(unittest.TestCase):
    def test_homestead_may_edit_its_own_role_and_no_other(self):
        for rule in SELF.desired_rules():
            if "rbac.authorization.k8s.io" in (rule.get("apiGroups") or []):
                self.assertEqual(["homestead"], rule.get("resourceNames"), rule)
            self.assertFalse({"bind", "impersonate", "*"} & set(rule.get("verbs") or []), rule)

    def test_the_permissions_file_is_the_manifests_own(self):
        self.assertEqual(render_rbac.render(), (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8"),
                         "run python scripts/render_rbac.py")
        rbac = (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8")
        for absent in ("kind: Deployment", "kind: Service\n", "kind: PersistentVolumeClaim", "kind: Namespace"):
            self.assertNotIn(absent, rbac)


class PermissionTests(SelfTest):
    def test_a_role_that_matches_is_left_alone(self):
        self.use(Cluster(installed()))
        self.assertEqual("current", SELF.reconcile()["state"])
        self.assertEqual([], [c for c in self.cluster.calls if c[0] == "PUT"])

    def test_a_role_behind_the_release_gains_what_it_needs(self):
        self.use(Cluster(installed(rules=SELF.desired_rules()[:-2])))
        result = SELF.reconcile()

        self.assertEqual("updated", result["state"])
        self.assertIn("added", result["detail"])
        self.assertEqual(SELF.desired_rules(), self.role())

    def test_a_permission_a_release_dropped_is_taken_away(self):
        retired = {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"], "resourceNames": ["old"]}
        self.use(Cluster(installed(rules=SELF.desired_rules() + [retired])))
        result = SELF.reconcile()

        self.assertEqual("updated", result["state"])
        self.assertIn("removed 1 permission", result["detail"])
        self.assertNotIn(retired, self.role())

    def test_the_binding_names_homestead_alone(self):
        self.use(Cluster(installed(subjects=("homestead", "old-account"))))
        SELF.reconcile()
        subjects = self.cluster.objects[f"{RBAC}/clusterrolebindings/homestead"]["subjects"]
        self.assertEqual([{"kind": "ServiceAccount", "name": "homestead", "namespace": "lab"}], subjects)

    def test_without_the_one_time_command_settings_shows_it(self):
        self.use(Cluster(installed(rules=SELF.desired_rules()[:-2]),
                         forbid={("PUT", f"{RBAC}/clusterroles/homestead")}))
        result = SELF.reconcile()

        self.assertEqual("manual", result["state"])
        self.assertEqual("kubectl apply -f https://raw.githubusercontent.com/wjcloudy/homestead/v2.8.73/deploy/rbac.yaml",
                         result["command"])

    def test_an_account_the_binding_does_not_name_is_told_what_to_run(self):
        self.use(Cluster(installed(account="someone-else")))
        self.assertEqual("manual", SELF.reconcile()["state"])

    def test_names_restrict_and_wildcards_widen(self):
        live = [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]
        self.assertEqual([], SELF.missing(live, SELF.desired_rules()))
        narrow = [{"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"], "resourceNames": ["x"]}]
        self.assertEqual([("", "serviceaccounts", "delete", None)],
                         SELF.missing(narrow, [{"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"]}]))


class OldKeyTests(SelfTest):
    def cluster_with_old_keys(self):
        return Cluster({
            "/apis/apps/v1/namespaces/lab/deployments/frigate": {
                "metadata": {"name": "frigate", "namespace": "lab",
                             "annotations": {"harvui.io/icon": "/api/icons/abc.png", "harvui.io/edited": "1",
                                             "homestead.io/edited": "2", "other": "kept"},
                             "labels": {"harvui.io/managed": "true", "app": "frigate"}},
                "spec": {"template": {"metadata": {"annotations": {"harvui.io/restarted": "t"}}}}},
            "/api/v1/namespaces/lab/secrets/homestead-src-unraid": {
                "metadata": {"name": "homestead-src-unraid", "namespace": "lab",
                             "labels": {"harvui.io/managed": "true"}}, "data": {"k": "dg=="}},
            "/api/v1/nodes/node1": {"metadata": {"name": "node1", "annotations": {"harvui.io/auto-hardware": "igpu"}}},
            "/api/v1/namespaces/lab/services/plain": {"metadata": {"name": "plain", "namespace": "lab"}},
        })

    def test_old_keys_move_to_the_homestead_domain_on_metadata_only(self):
        self.use(self.cluster_with_old_keys())
        result = SELF.adopt_old_keys()
        objects = self.cluster.objects

        self.assertEqual(3, result["changed"])
        frigate = objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]
        self.assertEqual({"homestead.io/icon": "/api/icons/abc.png", "homestead.io/edited": "2", "other": "kept"},
                         frigate["metadata"]["annotations"], "a newer value under the new key is kept")
        self.assertEqual({"homestead.io/managed": "true", "app": "frigate"}, frigate["metadata"]["labels"])
        self.assertEqual({"harvui.io/restarted": "t"}, frigate["spec"]["template"]["metadata"]["annotations"],
                         "a pod template is never touched, so nothing restarts")
        self.assertEqual({"homestead.io/auto-hardware": "igpu"}, objects["/api/v1/nodes/node1"]["metadata"]["annotations"])
        secret = objects["/api/v1/namespaces/lab/secrets/homestead-src-unraid"]
        self.assertEqual({"homestead.io/managed": "true"}, secret["metadata"]["labels"])
        self.assertEqual({"k": "dg=="}, secret["data"])
        self.assertIn(("PUT", "/api/v1/namespaces/lab/secrets/homestead-src-unraid"),
                      [(m, p) for m, p, _ in self.cluster.calls], "Secrets are updated, not patched")

    def test_it_runs_once(self):
        self.use(self.cluster_with_old_keys())
        SELF.adopt_old_keys()
        calls = len(self.cluster.calls)
        self.assertEqual({"done": True, "changed": 0}, SELF.adopt_old_keys())
        self.assertEqual(calls, len(self.cluster.calls))


if __name__ == "__main__":
    unittest.main()
