import base64
import copy
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_env_secrets as ENVSEC
import homestead_lifecycle as lifecycle
import server


def b64(text):
    return base64.b64encode(text.encode()).decode()


def deployment(env, annotations=None):
    return {"metadata": {"name": "tunnel", "namespace": "lab", "annotations": dict(annotations or {})},
            "spec": {"replicas": 1, "template": {"metadata": {}, "spec": {"containers": [
                {"name": "cloudflared", "image": "cloudflare/cloudflared:2026.1.0", "env": env}]}}}}


class Store:
    def __init__(self, objects=None):
        self.objects, self.sent = dict(objects or {}), []

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


SECRET = "/api/v1/namespaces/lab/secrets/tunnel-env"


class ExternalizeTests(unittest.TestCase):
    def test_a_token_goes_into_the_workloads_secret_and_the_rest_stays(self):
        store = Store()
        dep = deployment([{"name": "TUNNEL_TOKEN", "value": "eyJh-token"}, {"name": "TZ", "value": "UTC"},
                          {"name": "HBOX_AUTH_API_KEY_PEPPER", "value": "pepper"}])
        ENVSEC.externalize("lab", dep, store.read, store.send)
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual({"name": "TZ", "value": "UTC"}, env[1])
        self.assertEqual({"secretKeyRef": {"name": "tunnel-env", "key": "cloudflared.TUNNEL_TOKEN"}}, env[0]["valueFrom"])
        self.assertEqual("cloudflared.HBOX_AUTH_API_KEY_PEPPER", env[2]["valueFrom"]["secretKeyRef"]["key"])
        self.assertNotIn("eyJh-token", json.dumps(dep))
        self.assertEqual({"cloudflared.TUNNEL_TOKEN": b64("eyJh-token"), "cloudflared.HBOX_AUTH_API_KEY_PEPPER": b64("pepper")},
                         store.objects[SECRET]["data"])
        self.assertEqual("tunnel-env", dep["metadata"]["annotations"][ENVSEC.ANNOTATION])

    def test_a_field_masked_in_the_form_counts_whatever_its_name(self):
        store = Store()
        dep = deployment([{"name": "RTSP_LOGIN", "value": "hunter2"}])
        ENVSEC.externalize("lab", dep, store.read, store.send, masked={"RTSP_LOGIN"})
        self.assertIn("valueFrom", dep["spec"]["template"]["spec"]["containers"][0]["env"][0])

    def test_nothing_secret_writes_nothing(self):
        store = Store()
        dep = deployment([{"name": "TZ", "value": "UTC"}])
        before = copy.deepcopy(dep)
        ENVSEC.externalize("lab", dep, store.read, store.send)
        self.assertEqual(before, dep)
        self.assertEqual([], store.sent)

    def test_someone_elses_secret_of_that_name_is_never_taken_over(self):
        store = Store({SECRET: {"metadata": {"name": "tunnel-env"}, "data": {"x": "eQ=="}}})
        with self.assertRaisesRegex(ValueError, "not the one Homestead keeps"):
            ENVSEC.externalize("lab", deployment([{"name": "API_KEY", "value": "k"}]), store.read, store.send)
        self.assertEqual([], store.sent)

    def test_keys_no_longer_used_are_dropped(self):
        store = Store({SECRET: {"metadata": {"name": "tunnel-env", "labels": {ENVSEC.LABEL: "tunnel"}},
                                "data": {"cloudflared.OLD_TOKEN": b64("old"), "cloudflared.TUNNEL_TOKEN": b64("a")}}})
        dep = deployment([{"name": "TUNNEL_TOKEN", "value": "b"}], {ENVSEC.ANNOTATION: "tunnel-env"})
        ENVSEC.externalize("lab", dep, store.read, store.send)
        self.assertEqual({"cloudflared.TUNNEL_TOKEN": b64("b")}, store.objects[SECRET]["data"])

    def test_the_secret_goes_with_its_workload_and_only_one_homestead_made(self):
        dep = deployment([], {ENVSEC.ANNOTATION: "tunnel-env"})
        store = Store({SECRET: {"metadata": {"name": "tunnel-env", "labels": {ENVSEC.LABEL: "old-name"}}}})
        ENVSEC.remove("lab", dep, store.read, store.send)
        self.assertNotIn(SECRET, store.objects)
        store = Store({SECRET: {"metadata": {"name": "tunnel-env"}}})
        ENVSEC.remove("lab", dep, store.read, store.send)
        self.assertIn(SECRET, store.objects)


class EditRoundTripTests(unittest.TestCase):
    """A value kept in the Secret is edited like any other variable."""

    def setUp(self):
        self.store = Store()
        dep = deployment([{"name": "TUNNEL_TOKEN", "value": "first"}, {"name": "TZ", "value": "UTC"}])
        ENVSEC.externalize("lab", dep, self.store.read, self.store.send)
        self.store.objects["/apis/apps/v1/namespaces/lab/deployments/tunnel"] = dep
        self.store.sent.clear()
        lifecycle.bind(self.store.read, self.store.send, set(), {}, lambda: [], lambda *a, **k: None, "longhorn-r2")

    def payload(self):
        dep = self.store.objects["/apis/apps/v1/namespaces/lab/deployments/tunnel"]
        original = server.kget
        server.kget = self.store.read
        try:
            return server.workload_edit_payload("lab", "tunnel", dep, [], services=[])["containers"][0]
        finally:
            server.kget = original

    def test_the_form_shows_the_value_and_saving_a_change_updates_the_secret(self):
        container = self.payload()
        self.assertEqual({"TUNNEL_TOKEN": "first", "TZ": "UTC"}, container["env"])
        self.assertEqual([], container["env_refs"])
        self.assertEqual(["TUNNEL_TOKEN"], container["secret_env"])
        lifecycle.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "env": {"TUNNEL_TOKEN": "second", "TZ": "UTC"}}]})
        saved = self.store.sent[-1][2]
        self.assertNotIn("second", json.dumps(saved))
        self.assertEqual(b64("second"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])
        self.assertEqual({"TUNNEL_TOKEN": "second", "TZ": "UTC"}, self.payload()["env"])

    def test_removing_the_variable_in_the_form_removes_it(self):
        lifecycle.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "env": {"TZ": "UTC"}}]})
        env = self.store.sent[-1][2]["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual([{"name": "TZ", "value": "UTC"}], env)
        self.assertEqual({}, self.store.objects[SECRET]["data"])

    def test_a_plain_value_from_before_moves_over_when_edited(self):
        self.store.objects["/apis/apps/v1/namespaces/lab/deployments/tunnel"] = deployment(
            [{"name": "TUNNEL_TOKEN", "value": "legacy"}])
        del self.store.objects[SECRET]
        lifecycle.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "env": {"TUNNEL_TOKEN": "legacy"}}]})
        self.assertNotIn("legacy", json.dumps(self.store.sent[-1][2]))
        self.assertEqual(b64("legacy"), self.store.objects[SECRET]["data"]["cloudflared.TUNNEL_TOKEN"])


if __name__ == "__main__":
    unittest.main()
