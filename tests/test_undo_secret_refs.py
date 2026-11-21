"""Undo checks the stored values the settings from before refer to.

Homestead keeps an app's secret-looking variables in its own Secret. An Edit
save that leaves one out deletes its value there (#1395). Undoing that save
used to put the old settings back with their reference to the deleted value,
and the app's new pods could not start. Undo now reads the app's Secret and
refuses, naming each value it no longer holds, as the PVC check beside it
does for a deleted volume."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_changes as CH
import homestead_env_secrets as ENVSEC
import homestead_update_checks as CHECKS
import server  # noqa: F401  (binds the change history's review of an undo)
from fakes.kube import Kube

SECRET = "/api/v1/namespaces/lab/secrets/tunnel-env"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def ref(var, key, optional=None):
    source = {"name": "tunnel-env", "key": key, **({"optional": optional} if optional is not None else {})}
    return {"name": var, "valueFrom": {"secretKeyRef": source}}


def app(env, image="example/tunnel:1", init=None):
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "tunnel", "namespace": "lab", "uid": "u1",
                         "annotations": {ENVSEC.ANNOTATION: "tunnel-env"}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "tunnel"}},
                     "template": {"metadata": {"labels": {"app": "tunnel"}}, "spec": {"containers": [
                         {"name": "main", "image": image, "env": env}]}}}}
    if init:
        dep["spec"]["template"]["spec"]["initContainers"] = init
    return dep


def secret(**data):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "tunnel-env", "namespace": "lab", "labels": {ENVSEC.LABEL: "tunnel"}},
            "data": {key: b64(value) for key, value in data.items()}}


BEFORE = [ref("TUNNEL_TOKEN", "main.TUNNEL_TOKEN"), ref("OLD_TOKEN", "main.OLD_TOKEN"), {"name": "TZ", "value": "UTC"}]
AFTER = [ref("TUNNEL_TOKEN", "main.TUNNEL_TOKEN"), {"name": "TZ", "value": "UTC"}]


class UndoSecretRefs(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(CH.bind, CH.DATA_DIR)
        CH.bind(folder.name)
        for patch in (mock.patch.object(server.OVERVIEW_ROUTES, "guard_managed_smb"),
                      mock.patch.object(server, "CHANGES", CH)):
            patch.start()
            self.addCleanup(patch.stop)
        # The Edit save that took OLD_TOKEN out, as the history saw it.
        CH.observe([app(BEFORE)], now=1000)
        CH.observe([app(AFTER)], now=1060)
        self.entry = CH.history("lab", "tunnel")[0]["id"]

    def plan(self, kube):
        with kube:
            return server.OVERVIEW_ROUTES.change_undo_plan({"ns": "lab", "name": "tunnel", "id": self.entry})

    def test_undo_refuses_settings_whose_stored_value_was_deleted(self):
        kube = Kube(app(AFTER), secret(**{"main.TUNNEL_TOKEN": "first"}))
        with self.assertRaises(ValueError) as refused:
            self.plan(kube)
        message = str(refused.exception)
        self.assertIn("1 variable whose value this app's Secret no longer holds", message)
        self.assertIn("Homestead cannot restore it: OLD_TOKEN.", message)
        self.assertNotIn("TUNNEL_TOKEN", message, "a value the Secret still holds is not named")
        self.assertNotIn("first", message, "never a value")

    def test_the_undo_itself_is_refused_and_sends_nothing(self):
        kube = Kube(app(AFTER), secret(**{"main.TUNNEL_TOKEN": "first"}))
        refused = None
        with kube, mock.patch.object(CAPACITY_REVIEW, "enforce"), mock.patch.object(CHECKS, "refresh_soon"):
            try:
                CH.undo({"ns": "lab", "name": "tunnel", "id": self.entry, "confirm_capacity": True})
            except ValueError as error:
                refused = error
        puts = [path for method, path, *_ in kube.writes if method == "PUT" and "/deployments/" in path]
        self.assertEqual([], puts, "the old settings are not written back")
        self.assertIn("OLD_TOKEN", str(refused))

    def test_undo_refuses_when_the_secret_cannot_be_read(self):
        kube = Kube(app(AFTER), secret(**{"main.TUNNEL_TOKEN": "first"}))
        kube.fail(SECRET, 500, times=5)
        with self.assertRaisesRegex(ValueError, "could not read this app's Secret"):
            self.plan(kube)

    def test_an_unread_secret_does_not_stop_an_undo_that_uses_none_of_its_values(self):
        CH.observe([app([{"name": "TZ", "value": "UTC"}])], now=1120)
        CH.observe([app([{"name": "TZ", "value": "UTC"}], image="example/tunnel:2")], now=1180)
        self.entry = CH.history("lab", "tunnel")[0]["id"]
        kube = Kube(app([{"name": "TZ", "value": "UTC"}], image="example/tunnel:2"), secret(**{"main.TUNNEL_TOKEN": "first"}))
        kube.fail(SECRET, 500, times=5)
        current, proposed, rows, capacity, context = self.plan(kube)
        self.assertEqual("example/tunnel:1", proposed["spec"]["template"]["spec"]["containers"][0]["image"])

    def test_undo_goes_ahead_when_the_secret_still_holds_every_value(self):
        kube = Kube(app(AFTER), secret(**{"main.TUNNEL_TOKEN": "first", "main.OLD_TOKEN": "old"}))
        current, proposed, rows, capacity, context = self.plan(kube)
        names = [e["name"] for e in proposed["spec"]["template"]["spec"]["containers"][0]["env"]]
        self.assertIn("OLD_TOKEN", names)

    def test_a_reference_marked_optional_does_not_stop_the_undo(self):
        CH.observe([app([ref("TUNNEL_TOKEN", "main.TUNNEL_TOKEN"), ref("OLD_TOKEN", "main.OLD_TOKEN", optional=True)])],
                   now=1120)
        CH.observe([app(AFTER)], now=1180)
        self.entry = CH.history("lab", "tunnel")[0]["id"]
        kube = Kube(app(AFTER), secret(**{"main.TUNNEL_TOKEN": "first"}))
        self.plan(kube)

    def test_each_missing_variable_is_named_with_its_container(self):
        init = [{"name": "setup", "image": "example/setup:1", "env": [ref("B_TOKEN", "setup.B_TOKEN")]}]
        CH.observe([app(AFTER + [ref("A_TOKEN", "main.A_TOKEN")], init=init)], now=1120)
        CH.observe([app(AFTER, image="example/tunnel:2")], now=1180)
        self.entry = CH.history("lab", "tunnel")[0]["id"]
        kube = Kube(app(AFTER, image="example/tunnel:2"), secret(**{"main.TUNNEL_TOKEN": "first"}))
        with self.assertRaises(ValueError) as refused:
            self.plan(kube)
        self.assertIn("2 variables whose values this app's Secret no longer holds", str(refused.exception))
        self.assertIn("Homestead cannot restore them: A_TOKEN (main), B_TOKEN (setup).", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
