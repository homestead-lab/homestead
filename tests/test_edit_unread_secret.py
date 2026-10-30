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
        """What Edit's Save sends for this form, with change applied."""
        container = form["containers"][0]
        cfg = {"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "image": change.get("image", container["image"]),
             "env": dict(container["env"])}]}
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
