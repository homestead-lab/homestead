"""Homestead's own objects: keeping its permissions current, and leaving the harvUI names."""
import copy
import io
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import homestead_names as NAMES
import homestead_probe as PROBE
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
        children = [body for key, body in self.objects.items()
                    if key.startswith(path + "/") and "/" not in key[len(path) + 1:]]
        if children:
            return {"items": copy.deepcopy(children)}
        raise http(404)

    def send(self, method, path, body=None, ctype=None):
        path = path.split("?")[0]
        self.calls.append((method, path))
        if (method, path) in self.forbid:
            raise http(403)
        if method == "POST":
            target = f"{path}/{body['metadata']['name']}"
            if target in self.objects:
                raise http(409)
            self.objects[target] = copy.deepcopy(body)
        elif method == "PUT":
            if path not in self.objects:
                raise http(404)
            self.objects[path] = copy.deepcopy(body)
        elif method == "DELETE":
            if path not in self.objects:
                raise http(404)
            del self.objects[path]
        return {}


def cm(name, data=None):
    return {f"/api/v1/namespaces/lab/configmaps/{name}": {
        "metadata": {"name": name, "namespace": "lab"}, "data": data or {"k": name}}}


def secret(name):
    return {f"/api/v1/namespaces/lab/secrets/{name}": {
        "metadata": {"name": name, "namespace": "lab"}, "type": "Opaque", "data": {"k": "dmFsdWU="}}}


def binding(name, role, *accounts):
    return {f"{RBAC}/clusterrolebindings/{name}": {
        "metadata": {"name": name}, "roleRef": {"kind": "ClusterRole", "name": role},
        "subjects": [{"kind": "ServiceAccount", "name": a, "namespace": "lab"} for a in accounts]}}


def role(name, rules):
    return {f"{RBAC}/clusterroles/{name}": {"metadata": {"name": name, "resourceVersion": "7"}, "rules": rules}}


APP_IMAGE = "ghcr.io/wjcloudy/homestead:2.8.70"


def harvui_install(account="harvui", one_time_command=True):
    """The cluster the rename was written for: made as harvUI, Deployment already called homestead."""
    objects = {
        "/api/v1/namespaces/lab/pods/homestead-abc-1": {
            "metadata": {"name": "homestead-abc-1", "ownerReferences": [{"kind": "ReplicaSet", "name": "homestead-abc"}]},
            "spec": {"serviceAccountName": account}},
        "/apis/apps/v1/namespaces/lab/replicasets/homestead-abc": {
            "metadata": {"name": "homestead-abc", "ownerReferences": [{"kind": "Deployment", "name": "homestead"}]}},
        "/apis/apps/v1/namespaces/lab/deployments/homestead": {
            "metadata": {"name": "homestead", "namespace": "lab", "resourceVersion": "42"},
            "spec": {"template": {"spec": {
                "serviceAccountName": account, "serviceAccount": account,
                "initContainers": [{"name": "data-permissions", "image": APP_IMAGE,
                                    "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                "containers": [{"name": "homestead", "image": APP_IMAGE,
                                "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "harvui-data"}}]}}},
            "status": {"readyReplicas": 1}},
        "/api/v1/namespaces/lab/persistentvolumeclaims/harvui-data": {
            "metadata": {"name": "harvui-data"}, "spec": {"accessModes": ["ReadWriteMany"],
                                                          "storageClassName": "longhorn-r2",
                                                          "resources": {"requests": {"storage": "2Gi"}}}},
        "/api/v1/namespaces/lab/services/harvui": {
            "metadata": {"name": "harvui", "labels": {"app": "homestead"},
                         "annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.242",
                                         "kubectl.kubernetes.io/last-applied-configuration": "{}"}},
            "spec": {"type": "LoadBalancer", "selector": {"app": "homestead"}, "clusterIP": "10.53.1.9",
                     "loadBalancerIP": "192.0.2.242",
                     "ports": [{"port": 8088, "targetPort": 8080, "protocol": "TCP", "nodePort": 31234}]}},
        "/apis/apps/v1/namespaces/lab/daemonsets/harvui-nodeprobe": {"metadata": {"name": "harvui-nodeprobe"}},
        "/api/v1/namespaces/lab/serviceaccounts/harvui": {"metadata": {"name": "harvui"}},
        f"{RBAC}/namespaces/lab/roles/harvui-console": {"metadata": {"name": "harvui-console"}},
        f"{RBAC}/namespaces/lab/rolebindings/harvui-console": {"metadata": {"name": "harvui-console"}},
    }
    for name in ("harvui-nodeprobe", "harvui-server", "harvui-settings", "harvui-shares", "harvui-sources",
                 "harvui-web", "homestead-clusters"):
        objects.update(cm(name))
    for name in ("harvui-auth", "harvui-share-credentials", "harvui-src-unraid", "homestead-cluster-loopback"):
        objects.update(secret(name))
    objects.update(role("harvui", [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]))
    objects.update(binding("harvui", "harvui", "harvui"))
    if one_time_command:
        objects["/api/v1/namespaces/lab/serviceaccounts/homestead"] = {"metadata": {"name": "homestead"}}
        objects.update(role("homestead", SELF.desired_rules()))
        objects.update(binding("homestead", "homestead", "homestead", "harvui"))
    return objects


class SelfTest(unittest.TestCase):
    def use(self, cluster):
        self.cluster = cluster
        SELF.bind(cluster.get, cluster.send, "lab", "lab", "lab", "2.8.71")
        PROBE.bind(cluster.get, cluster.send, "lab")
        NAMES.bind(cluster.get)
        patcher = mock.patch.object(SELF, "POD", "homestead-abc-1")
        patcher.start()
        self.addCleanup(patcher.stop)
        return cluster

    def restart_as(self, account):
        """What Kubernetes does after the Deployment changes: a new pod, as the new account."""
        pod = self.cluster.objects["/api/v1/namespaces/lab/pods/homestead-abc-1"]
        pod["spec"]["serviceAccountName"] = account


class PermissionTests(SelfTest):
    def test_the_manifest_lets_homestead_edit_its_own_role_and_no_other(self):
        rules = SELF.desired_rules()
        for rule in rules:
            if {"escalate", "bind", "impersonate"} & set(rule.get("verbs") or []) or "*" in (rule.get("verbs") or []):
                self.assertEqual({"homestead", "harvui"}, set(rule.get("resourceNames") or []), rule)
            if "rbac.authorization.k8s.io" in (rule.get("apiGroups") or []):
                self.assertTrue(rule.get("resourceNames"), f"RBAC access must name its objects: {rule}")

    def test_the_permissions_file_is_the_manifests_own(self):
        self.assertEqual(render_rbac.render(), (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8"),
                         "run python scripts/render_rbac.py")
        rbac = (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8")
        for absent in ("kind: Deployment", "kind: Service\n", "kind: PersistentVolumeClaim", "kind: Namespace"):
            self.assertNotIn(absent, rbac)

    def test_a_role_that_has_everything_is_left_alone(self):
        self.use(Cluster(harvui_install()))
        self.assertEqual("current", SELF.reconcile()["state"])
        self.assertNotIn(("PUT", f"{RBAC}/clusterroles/homestead"), self.cluster.calls)

    def test_a_role_behind_the_release_is_brought_up_to_it_keeping_additions(self):
        objects = harvui_install()
        mine = {"apiGroups": ["example.com"], "resources": ["widgets"], "verbs": ["get"]}
        objects[f"{RBAC}/clusterroles/homestead"]["rules"] = SELF.desired_rules()[:-4] + [mine]
        self.use(Cluster(objects))

        result = SELF.reconcile()

        self.assertEqual("updated", result["state"])
        rules = self.cluster.objects[f"{RBAC}/clusterroles/homestead"]["rules"]
        self.assertEqual([], SELF.missing(rules, SELF.desired_rules()))
        self.assertIn(mine, rules)

    def test_without_the_one_time_command_settings_shows_it(self):
        self.use(Cluster(harvui_install(one_time_command=False),
                         forbid={("PUT", f"{RBAC}/clusterroles/harvui")}))
        result = SELF.reconcile()

        self.assertEqual("manual", result["state"])
        self.assertEqual("kubectl apply -f https://raw.githubusercontent.com/wjcloudy/homestead/v2.8.71/deploy/rbac.yaml",
                         result["command"])

    def test_names_restrict_and_wildcards_widen(self):
        live = [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]
        self.assertEqual([], SELF.missing(live, SELF.desired_rules()))
        narrow = [{"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"], "resourceNames": ["harvui"]}]
        self.assertEqual([("", "serviceaccounts", "delete", None)],
                         SELF.missing(narrow, [{"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["delete"]}]))


class RenameTests(SelfTest):
    def test_the_plan_lists_every_old_name_and_nothing_else(self):
        self.use(Cluster(harvui_install()))
        plan = SELF.plan()

        self.assertEqual([], plan["blockers"])
        whats = [s["what"] for s in plan["steps"] if not s["done"]]
        for expected in ("ConfigMap harvui-settings → homestead-settings", "ConfigMap harvui-sources → homestead-sources",
                         "Secret harvui-auth → homestead-auth", "ConfigMap harvui-shares → homestead-shares",
                         "Secret harvui-share-credentials → homestead-share-credentials",
                         "Secret harvui-src-unraid → homestead-src-unraid", "Service harvui → homestead",
                         "Service account harvui → homestead"):
            self.assertIn(expected, whats)
        self.assertEqual(10, plan["pending"])
        self.assertFalse(any("harvui-server" in w or "harvui-web" in w for w in whats), "relics are not copied")

    def test_without_the_homestead_account_nothing_moves(self):
        self.use(Cluster(harvui_install(one_time_command=False)))
        plan = SELF.plan()
        self.assertTrue(plan["blockers"])
        with self.assertRaises(ValueError):
            SELF.move(after=lambda fn: fn())

    def test_moving_keeps_every_value_address_and_file(self):
        self.use(Cluster(harvui_install()))
        with mock.patch.object(SELF.time, "sleep"):
            result = SELF.move(after=lambda fn: fn())
        objects = self.cluster.objects

        self.assertTrue(result["restarting"])
        self.assertEqual("", SELF.MOVE["error"])
        for kind, name in (("configmaps", "settings"), ("configmaps", "shares"), ("configmaps", "sources"),
                           ("secrets", "auth"), ("secrets", "share-credentials"), ("secrets", "src-unraid")):
            old = objects[f"/api/v1/namespaces/lab/{kind}/harvui-{name}"]
            new = objects[f"/api/v1/namespaces/lab/{kind}/homestead-{name}"]
            self.assertEqual(old["data"], new["data"], name)
        # The node probe, under its new name, reading the new Secret.
        self.assertIn("/apis/apps/v1/namespaces/lab/daemonsets/homestead-nodeprobe", objects)
        self.assertNotIn("/apis/apps/v1/namespaces/lab/daemonsets/harvui-nodeprobe", objects)
        probe = objects["/apis/apps/v1/namespaces/lab/daemonsets/homestead-nodeprobe"]
        self.assertEqual("homestead-auth", probe["spec"]["template"]["spec"]["volumes"][-1]["secret"]["secretName"])
        # The Service, on the same address and port.
        self.assertNotIn("/api/v1/namespaces/lab/services/harvui", objects)
        service = objects["/api/v1/namespaces/lab/services/homestead"]
        self.assertEqual("192.0.2.242", service["spec"]["loadBalancerIP"])
        self.assertEqual("192.0.2.242", service["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        self.assertEqual([{"port": 8088, "targetPort": 8080, "protocol": "TCP"}], service["spec"]["ports"])
        self.assertNotIn("clusterIP", service["spec"])
        # The data: a new claim like the old one, filled from it before Homestead starts.
        claim = objects["/api/v1/namespaces/lab/persistentvolumeclaims/homestead-data"]
        self.assertEqual(["ReadWriteMany"], claim["spec"]["accessModes"])
        self.assertEqual("2Gi", claim["spec"]["resources"]["requests"]["storage"])
        spec = objects["/apis/apps/v1/namespaces/lab/deployments/homestead"]["spec"]["template"]["spec"]
        self.assertEqual("homestead", spec["serviceAccountName"])
        self.assertNotIn("serviceAccount", spec)
        volumes = {v["name"]: v["persistentVolumeClaim"] for v in spec["volumes"]}
        self.assertEqual("homestead-data", volumes["data"]["claimName"])
        self.assertEqual({"claimName": "harvui-data", "readOnly": True}, volumes["previous-data"])
        adopt = spec["initContainers"][0]
        self.assertEqual(("adopt-data", APP_IMAGE), (adopt["name"], adopt["image"]))
        self.assertEqual("data-permissions", spec["initContainers"][1]["name"], "ownership is fixed after the copy")
        self.assertIn({"name": "previous-data", "mountPath": "/previous", "readOnly": True}, adopt["volumeMounts"])
        self.assertIn("harvui-data", self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/harvui-data"]["metadata"]["name"],
                      "the old volume is untouched")

    def test_after_the_restart_only_leftovers_remain_and_go_on_request(self):
        self.use(Cluster(harvui_install()))
        with mock.patch.object(SELF.time, "sleep"):
            SELF.move(after=lambda fn: fn())
        self.restart_as("homestead")
        plan = SELF.plan()

        self.assertEqual(0, plan["pending"])
        names = {row["name"] for row in plan["leftovers"]}
        self.assertEqual({"harvui-settings", "harvui-sources", "harvui-auth", "harvui-shares", "harvui-share-credentials",
                          "harvui-src-unraid", "harvui-server", "harvui-web", "harvui-data", "harvui-console",
                          "harvui"}, names)
        with self.assertRaises(ValueError):
            SELF.clean()

        result = SELF.clean("harvui-data")

        self.assertEqual([], result["failed"])
        self.assertTrue(result["restarting"])
        left = [path for path in self.cluster.objects if "/harvui" in path]
        self.assertEqual([], left)
        self.assertIn("homestead-clusters", str(self.cluster.objects.keys()), "objects already named homestead stay")
        subjects = self.cluster.objects[f"{RBAC}/clusterrolebindings/homestead"]["subjects"]
        self.assertEqual(["homestead"], [s["name"] for s in subjects])
        spec = self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/homestead"]["spec"]["template"]["spec"]
        self.assertEqual(["data-permissions"], [c["name"] for c in spec["initContainers"]])
        self.assertEqual(["data"], [v["name"] for v in spec["volumes"]])

    def test_leftovers_wait_for_homestead_to_run_as_its_new_account(self):
        self.use(Cluster(harvui_install()))
        with mock.patch.object(SELF.time, "sleep"):
            SELF.move(after=lambda fn: fn())
        with self.assertRaises(ValueError):
            SELF.clean("harvui-data")
        self.assertIn(f"{RBAC}/clusterroles/harvui", self.cluster.objects)

    def test_a_service_that_cannot_be_recreated_is_put_back(self):
        self.use(Cluster(harvui_install()))
        original = self.cluster.send

        def send(method, path, body=None, ctype=None):
            if method == "POST" and path.endswith("/services") and body["metadata"]["name"] == "homestead":
                raise http(422)
            return original(method, path, body, ctype)

        self.cluster.send = send
        SELF.bind(self.cluster.get, send, "lab", "lab", "lab", "2.8.71")
        with self.assertRaises(urllib.error.HTTPError):
            SELF.swap_service()
        self.assertEqual("192.0.2.242", self.cluster.objects["/api/v1/namespaces/lab/services/harvui"]["spec"]["loadBalancerIP"])

    def test_an_install_made_as_homestead_has_nothing_to_do(self):
        fresh = {path: body for path, body in harvui_install(account="homestead").items() if "harvui" not in path}
        fresh["/apis/apps/v1/namespaces/lab/deployments/homestead"]["spec"]["template"]["spec"]["volumes"][0][
            "persistentVolumeClaim"]["claimName"] = "homestead-data"
        fresh["/api/v1/namespaces/lab/services/homestead"] = {"metadata": {"name": "homestead"}, "spec": {}}
        self.use(Cluster(fresh))
        plan = SELF.plan()
        self.assertEqual((0, []), (plan["pending"], plan["leftovers"]))


class AdoptScriptTests(unittest.TestCase):
    def test_the_copy_runs_once_and_keeps_what_it_copied(self):
        import shutil
        import tempfile
        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh")
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "previous" / "moves").mkdir(parents=True)
        (root / "previous" / "vapid.json").write_text("key")
        (root / "previous" / "moves" / "m1.json").write_text("{}")
        (root / "data").mkdir()
        script = SELF.ADOPT_SCRIPT.replace("/data", str(root / "data").replace("\\", "/")).replace(
            "/previous", str(root / "previous").replace("\\", "/"))
        subprocess.run([sh, "-c", script], check=True)
        self.assertEqual("key", (root / "data" / "vapid.json").read_text())
        self.assertTrue((root / "data" / "moves" / "m1.json").exists())
        (root / "data" / "vapid.json").write_text("newer")
        subprocess.run([sh, "-c", script], check=True)
        self.assertEqual("newer", (root / "data" / "vapid.json").read_text(), "a second start copies nothing")


if __name__ == "__main__":
    unittest.main()
