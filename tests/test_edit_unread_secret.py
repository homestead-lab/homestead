"""#1001: a Secret that could not be read never looks like "no secret values".

An app keeps some variables in its own Secret. When that Secret cannot be read
as Edit opens, the form used to get no values for them and showed them as
read-only references; any save (an image bump) then took them out of the
Deployment and deleted their values from the Secret. Now the form says the
app's secret settings could not be read, and the server refuses a save from
that form - whatever the browser does - with nothing written."""
import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_env_secrets as ENVSEC
import homestead_kube as K
import homestead_lifecycle as LC
import homestead_workload_edit as WEDIT
import server  # noqa: F401  (binds the modules the Edit form reads through)
import homestead_workloads as WORKLOADS
from fakes.kube import Kube

DEP = "/apis/apps/v1/namespaces/lab/deployments/tunnel"
SECRET = "/api/v1/namespaces/lab/secrets/tunnel-env"
KEY = "cloudflared.TUNNEL_TOKEN"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def objects():
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": "tunnel", "namespace": "lab", "annotations": {ENVSEC.ANNOTATION: "tunnel-env"}},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "tunnel"}},
                    "template": {"metadata": {"labels": {"app": "tunnel"}}, "spec": {"containers": [
                        {"name": "cloudflared", "image": "cloudflare/cloudflared:2026.1.0", "env": [
                            {"name": "TUNNEL_TOKEN", "valueFrom": {"secretKeyRef": {"name": "tunnel-env", "key": KEY}}},
                            {"name": "TZ", "value": "UTC"}]}]}}}}
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": "tunnel-env", "namespace": "lab", "labels": {ENVSEC.LABEL: "tunnel"}},
              "data": {KEY: b64("first")}}
    return dep, secret


class Fixture(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(*objects()))
        for patch in (mock.patch.object(LC, "kget", K.kget), mock.patch.object(LC, "ksend", K.ksend),
                      mock.patch.object(WORKLOADS, "kget", K.kget, create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def open_form(self):
        return WORKLOADS.workload_edit_payload("lab", "tunnel", self.kube.object(DEP), [], services=[])

    def save(self, form, **change):
        """What Edit's Save sends for this form, with change applied: the
        variables it showed from the Secret (secret_env) go back with it."""
        container = form["containers"][0]
        cfg = {"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "image": change.get("image", container["image"]),
             "env": change.get("env", dict(container["env"])), "secret_env": list(container["secret_env"])}]}
        if form.get("secret_env_unread"):
            cfg["secret_env_unread"] = True
        return cfg


class UnreadSecretTests(Fixture):
    def test_opening_edit_says_the_secret_settings_could_not_be_read(self):
        self.kube.fail(SECRET, 500, method="GET")
        form = self.open_form()
        self.assertIn("could not be read", form["secret_env_unread"])
        container = form["containers"][0]
        self.assertNotIn("TUNNEL_TOKEN", container["env"])
        self.assertEqual([], container["secret_env"])
        self.assertIn("TUNNEL_TOKEN", [ref["name"] for ref in container["env_refs"]])

    def test_a_save_from_that_form_is_refused_and_writes_nothing(self):
        self.kube.fail(SECRET, 500, method="GET")
        cfg = self.save(self.open_form(), image="cloudflare/cloudflared:2026.2.0")
        # The Secret reads again by the time Save is pressed: the form is still
        # missing its values, so the save must not go ahead.
        before = (self.kube.object(DEP), self.kube.object(SECRET))
        for attempt in (lambda: WEDIT.prepare_edit(dict(cfg)), lambda: LC.edit_workload(dict(cfg))):
            with self.assertRaises(ValueError) as caught:
                attempt()
            self.assertIn("could not be read", str(caught.exception))
        self.assertEqual([], self.kube.writes)
        self.assertEqual(before, (self.kube.object(DEP), self.kube.object(SECRET)))

    def test_values_says_unknown_for_a_failed_read_and_none_for_a_missing_secret(self):
        dep = self.kube.object(DEP)
        self.kube.fail(SECRET, 503, method="GET")
        self.assertIsNone(ENVSEC.values("lab", dep, K.kget))
        self.assertEqual({"cloudflared": {"TUNNEL_TOKEN": "first"}}, ENVSEC.values("lab", dep, K.kget))
        self.kube("DELETE", SECRET)
        self.assertEqual({}, ENVSEC.values("lab", dep, K.kget), "no Secret at all is known: nothing held")


class WithoutTheFormTests(Fixture):
    """The server does not rely on the browser: a save that does not name a
    variable as shown from the Secret (secret_env) keeps it - an MCP or API
    caller rebuilding env from get_container_settings, or a stale form."""

    def saved(self):
        container = self.kube.object(DEP)["spec"]["template"]["spec"]["containers"][0]
        return {item["name"]: item for item in container.get("env") or []}, self.kube.object(SECRET)["data"]

    def agent_save(self, env):
        LC.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "image": "cloudflare/cloudflared:2026.2.0",
             "env": env}]})

    def test_a_save_without_secret_env_keeps_the_secret_variable(self):
        self.agent_save({"TZ": "UTC"})
        env, data = self.saved()
        self.assertEqual({"secretKeyRef": {"name": "tunnel-env", "key": KEY}}, env["TUNNEL_TOKEN"]["valueFrom"])
        self.assertEqual({KEY: b64("first")}, data)

    def test_an_unread_form_saved_without_its_marker_keeps_it_too(self):
        self.kube.fail(SECRET, 500, method="GET")
        cfg = self.save(self.open_form(), image="cloudflare/cloudflared:2026.2.0")
        cfg.pop("secret_env_unread")
        LC.edit_workload(cfg)
        env, data = self.saved()
        self.assertIn("TUNNEL_TOKEN", env)
        self.assertEqual({KEY: b64("first")}, data)

    def test_a_new_value_without_secret_env_still_replaces_it(self):
        self.agent_save({"TUNNEL_TOKEN": "second", "TZ": "UTC"})
        env, data = self.saved()
        self.assertEqual(KEY, env["TUNNEL_TOKEN"]["valueFrom"]["secretKeyRef"]["key"])
        self.assertEqual({KEY: b64("second")}, data)

    def test_a_form_save_that_deletes_the_row_removes_it(self):
        LC.edit_workload(self.save(self.open_form(), env={"TZ": "UTC"}))
        env, data = self.saved()
        self.assertNotIn("TUNNEL_TOKEN", env)
        self.assertEqual({}, data)

    def test_a_rename_in_the_form_moves_the_value(self):
        LC.edit_workload(self.save(self.open_form(), env={"TUNNEL_TOKEN_V2": "first", "TZ": "UTC"}))
        env, data = self.saved()
        self.assertNotIn("TUNNEL_TOKEN", env)
        self.assertEqual("cloudflared.TUNNEL_TOKEN_V2", env["TUNNEL_TOKEN_V2"]["valueFrom"]["secretKeyRef"]["key"])
        self.assertEqual({"cloudflared.TUNNEL_TOKEN_V2": b64("first")}, data)


class ReadSecretTests(Fixture):
    def test_a_secret_read_normally_edits_as_before(self):
        form = self.open_form()
        self.assertNotIn("secret_env_unread", form)
        container = form["containers"][0]
        self.assertEqual({"TUNNEL_TOKEN": "first", "TZ": "UTC"}, container["env"])
        self.assertEqual(["TUNNEL_TOKEN"], container["secret_env"])
        LC.edit_workload(self.save(form, image="cloudflare/cloudflared:2026.2.0"))
        saved = self.kube.object(DEP)["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual("cloudflare/cloudflared:2026.2.0", saved["image"])
        self.assertIn({"name": "TUNNEL_TOKEN", "valueFrom": {"secretKeyRef": {"name": "tunnel-env", "key": KEY}}},
                      saved["env"])
        self.assertEqual({KEY: b64("first")}, self.kube.object(SECRET)["data"])


if __name__ == "__main__":
    unittest.main()
