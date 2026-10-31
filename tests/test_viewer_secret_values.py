"""Values an app keeps in its own Secret reach only someone who can save them.

GET /api/workload (the Edit form) is open to viewers, and it used to hand them
every variable the app keeps in its Secret - passwords and tokens - in plain
text. Revealing a Secret anywhere else is admin-only. The form now shows those
values only to a role that can save it (POST /api/edit), on a workload that
role may edit; everyone else, and an AI agent whose key cannot save the
settings, gets the names with the values masked. A save that sends the mask
back keeps what the Secret holds."""
import base64
import copy
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_agent_tools as TOOLS
import homestead_auth as AUTH
import homestead_env_secrets as ENVSEC
import homestead_gitops as GITOPS
import homestead_workload_edit as WEDIT
import homestead_lifecycle as lifecycle
import homestead_route_policy as ROUTE_POLICY
import homestead_routes as ROUTER
import server

DEP = "/apis/apps/v1/namespaces/lab/deployments/tunnel"
SECRET = "/api/v1/namespaces/lab/secrets/tunnel-env"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def deployment(env, service_account=None):
    spec = {"containers": [{"name": "cloudflared", "image": "cloudflare/cloudflared:2026.1.0", "env": env}]}
    if service_account:
        spec["serviceAccountName"] = service_account
    return {"metadata": {"name": "tunnel", "namespace": "lab", "annotations": {}, "uid": "u-1", "resourceVersion": "1"},
            "spec": {"replicas": 1, "template": {"metadata": {}, "spec": spec}}}


class Store:
    def __init__(self):
        self.objects, self.sent = {}, []

    def read(self, path):
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        raise urllib.error.HTTPError(path, 404, "not found", {}, None)

    def send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            path += "/" + body["metadata"]["name"]
        if method == "DELETE":
            self.objects.pop(path, None)
        else:
            self.objects[path] = copy.deepcopy(body)
        return body


class Base(unittest.TestCase):
    def setUp(self, service_account=None):
        self.store = Store()
        dep = deployment([{"name": "TUNNEL_TOKEN", "value": "s3cret-first"}, {"name": "TZ", "value": "UTC"}],
                         service_account)
        ENVSEC.externalize("lab", dep, self.store.read, self.store.send)
        self.store.objects[DEP] = dep
        self.store.sent.clear()
        lifecycle.bind(self.store.read, self.store.send, set(), {}, lambda: [], lambda *a, **k: None, "longhorn-r2")
        self.kget = server.KUBE_HELPERS.kget
        server.KUBE_HELPERS.kget = self.store.read
        self.features = server.WORKLOADS.HW.features
        server.WORKLOADS.HW.features = lambda: []

    def tearDown(self):
        server.KUBE_HELPERS.kget = self.kget
        server.WORKLOADS.HW.features = self.features

    def form(self, role):
        handler = server.WORKLOADS.ROUTES[("GET", "/api/workload")][1]
        return handler(ROUTER.Request("GET", "/api/workload", {"ns": ["lab"], "name": ["tunnel"]}, {}, "someone", role))


class TheEditForm(Base):
    def test_a_viewer_gets_the_names_with_the_values_masked(self):
        payload = self.form("viewer")
        self.assertNotIn("s3cret-first", json.dumps(payload))
        container = payload["containers"][0]
        self.assertEqual({"TUNNEL_TOKEN": ENVSEC.MASK, "TZ": "UTC"}, container["env"])
        self.assertEqual(["TUNNEL_TOKEN"], container["secret_env"])
        self.assertTrue(container["secret_env_hidden"])
        self.assertEqual({"TUNNEL_TOKEN": ENVSEC.MASK, "TZ": "UTC"}, payload["env"])

    def test_an_operator_and_an_admin_get_the_values(self):
        for role in ("operator", "admin"):
            payload = self.form(role)
            self.assertEqual({"TUNNEL_TOKEN": "s3cret-first", "TZ": "UTC"}, payload["containers"][0]["env"], role)
            self.assertEqual({"TUNNEL_TOKEN": "s3cret-first", "TZ": "UTC"}, payload["env"], role)
            self.assertFalse(payload["containers"][0].get("secret_env_hidden"), role)

    def test_no_role_at_all_gets_them_masked(self):
        self.assertNotIn("s3cret-first", json.dumps(self.form(None)))

    def test_the_role_is_the_one_saving_the_form_needs(self):
        need = ROUTE_POLICY.role("/api/edit", "POST")
        self.assertEqual(need, server.WORKLOADS.ROUTES[("POST", "/api/edit")][0])
        self.assertFalse(AUTH.allows("viewer", need))
        with self.assertRaises(TOOLS.NeedsApproval):
            TOOLS.call("POST", "/api/edit", body={"ns": "lab", "name": "tunnel"}, user="someone", role="viewer")


class AWorkloadOnlyAnAdminMayEdit(Base):
    def setUp(self):
        super().setUp(service_account="robot")

    def test_an_operator_gets_them_masked_and_an_admin_gets_them(self):
        self.assertNotIn("s3cret-first", json.dumps(self.form("operator")))
        self.assertEqual("s3cret-first", self.form("admin")["containers"][0]["env"]["TUNNEL_TOKEN"])


class SavingTheMask(Base):
    def save(self, env):
        WEDIT.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "env": env}]})

    def test_an_unchanged_masked_value_keeps_what_the_secret_holds(self):
        self.save({"TUNNEL_TOKEN": ENVSEC.MASK, "TZ": "Europe/Paris"})
        self.assertEqual(b64("s3cret-first"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])
        saved = json.dumps(self.store.objects[DEP]) + json.dumps(self.store.objects[SECRET])
        self.assertNotIn(ENVSEC.MASK, saved)
        self.assertNotIn(b64(ENVSEC.MASK), saved)
        env = {e["name"]: e for e in self.store.objects[DEP]["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual("tunnel-env", env["TUNNEL_TOKEN"]["valueFrom"]["secretKeyRef"]["name"])
        self.assertEqual("Europe/Paris", env["TZ"]["value"])

    def test_an_operator_who_got_the_real_value_saves_a_change(self):
        env = self.form("operator")["containers"][0]["env"]
        self.save({**env, "TUNNEL_TOKEN": "s3cret-second"})
        self.assertEqual(b64("s3cret-second"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])
        self.assertEqual("s3cret-second", self.form("operator")["containers"][0]["env"]["TUNNEL_TOKEN"])

    def test_an_operator_who_sends_the_form_back_unchanged_keeps_the_value(self):
        self.save(self.form("operator")["containers"][0]["env"])
        self.assertEqual(b64("s3cret-first"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])

    def test_the_mask_under_a_name_the_secret_does_not_hold_is_refused(self):
        with self.assertRaisesRegex(ValueError, "hidden"):
            self.save({"NEW_TOKEN": ENVSEC.MASK, "TZ": "UTC"})
        self.assertEqual(b64("s3cret-first"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])

    def test_the_yaml_for_git_shows_the_kept_reference(self):
        # preview_edit's steps after the capacity review (which needs a cluster).
        current = self.store.objects[DEP]
        proposed = copy.deepcopy(current)
        WEDIT._apply_container_edit(proposed["spec"]["template"]["spec"]["containers"][0],
                                    {"env": {"TUNNEL_TOKEN": ENVSEC.MASK, "TZ": "Europe/Paris"}},
                                    "tunnel", ENVSEC.own(current))
        secret = ENVSEC.as_saved(proposed, current, None, ENVSEC.held("lab", current, self.store.read))
        document = GITOPS.document(current, GITOPS.patch(current, proposed))
        self.assertEqual({"name": "tunnel-env", "keys": ["cloudflared.TUNNEL_TOKEN"]}, secret)
        self.assertNotIn(ENVSEC.MASK, document)
        self.assertNotIn("s3cret-first", document)
        self.assertIn("Europe/Paris", document)   # the token is unchanged, so not in the patch


class AnAgentsKey(Base):
    """get_container_settings runs the same route as the key's role, and the
    key must also be able to save the settings (containers:manage)."""

    def setUp(self):
        super().setUp()
        self.bound = dict(TOOLS._bound)
        TOOLS.bind(ROUTE_POLICY.role, lambda m, p, b: None, self.store.read, lambda *a: None)

    def tearDown(self):
        TOOLS._bound.clear()
        TOOLS._bound.update(self.bound)
        super().tearDown()

    def settings(self, role, scopes):
        ctx = {"user": "api-key:agent", "role": role, "auth": {"name": "agent", "scopes": scopes}}
        return TOOLS.BY_NAME["get_container_settings"].run({"namespace": "lab", "name": "tunnel"}, ctx)

    def test_a_read_key_gets_them_masked(self):
        # server.py gives a key with no :control or :manage scope the viewer role.
        answer = self.settings("viewer", ["read"])
        self.assertNotIn("s3cret-first", json.dumps(answer))
        self.assertEqual(ENVSEC.MASK, answer["containers"][0]["env"]["TUNNEL_TOKEN"])

    def test_a_key_that_cannot_save_the_settings_gets_them_masked(self):
        for scopes in (["containers:control", "read"], ["vms:manage", "vms:control", "read"]):
            self.assertNotIn("s3cret-first", json.dumps(self.settings("operator", scopes)), scopes)

    def test_a_key_that_can_save_them_gets_them(self):
        answer = self.settings("operator", ["containers:manage", "containers:control", "read"])
        self.assertEqual("s3cret-first", answer["containers"][0]["env"]["TUNNEL_TOKEN"])


class AnAgentsKeyOnAWorkloadOnlyAnAdminMayEdit(AnAgentsKey):
    def setUp(self):
        Base.setUp(self, service_account="robot")
        self.bound = dict(TOOLS._bound)
        TOOLS.bind(ROUTE_POLICY.role, lambda m, p, b: None, self.store.read, lambda *a: None)

    def test_a_key_that_can_save_them_gets_them(self):
        answer = self.settings("operator", ["containers:manage", "containers:control", "read"])
        self.assertNotIn("s3cret-first", json.dumps(answer))


class MoreThanOneContainer(Base):
    """The mask keeps only what that container's own variable holds."""

    def setUp(self):
        super().setUp()
        self.store.objects[DEP]["spec"]["template"]["spec"]["containers"].append(
            {"name": "sidecar", "image": "busybox:1.36", "env": [{"name": "TZ", "value": "UTC"}]})

    def test_the_mask_in_another_container_is_refused(self):
        with self.assertRaisesRegex(ValueError, "hidden"):
            WEDIT.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
                {"original_name": "cloudflared", "name": "cloudflared", "env": {"TUNNEL_TOKEN": ENVSEC.MASK}},
                {"original_name": "sidecar", "name": "sidecar", "env": {"TUNNEL_TOKEN": ENVSEC.MASK}}]})

    def test_a_renamed_container_keeps_its_masked_value(self):
        WEDIT.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "tunnel", "env": {"TUNNEL_TOKEN": ENVSEC.MASK}},
            {"original_name": "sidecar", "name": "sidecar", "env": {"TZ": "UTC"}}]})
        self.assertEqual(b64("s3cret-first"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])
        self.assertNotIn(ENVSEC.MASK, json.dumps(self.store.objects[DEP]))


class TheOlderSingleContainerBody(Base):
    def test_a_masked_value_keeps_what_the_secret_holds(self):
        WEDIT.edit_workload({"ns": "lab", "name": "tunnel", "env": {"TUNNEL_TOKEN": ENVSEC.MASK, "TZ": "UTC"}})
        self.assertEqual(b64("s3cret-first"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])
        self.assertNotIn(ENVSEC.MASK, json.dumps(self.store.objects[DEP]))


class ASecretHomesteadDidNotMake(Base):
    """values() shows only the Secret Homestead keeps for the app (its label),
    as held() and externalize() do - not one an annotation points it at."""

    def test_its_values_are_not_shown(self):
        self.store.objects[SECRET]["metadata"]["labels"] = {}
        self.assertNotIn("s3cret-first", json.dumps(self.form("admin")))


if __name__ == "__main__":
    unittest.main()
