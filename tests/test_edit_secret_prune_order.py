"""An Edit save removes kept values from the app's Secret only once the app is saved.

An app keeps some variables in its own Secret, and its Deployment refers to
them. A save that leaves a kept variable out deletes its value from the
Secret. That delete used to happen before the Deployment was written: when
that write then failed (a conflict, a server error), the app still referred
to a value that was gone, and its next restart failed on the missing key.

Now a save writes new values first, then the Deployment, and removes the
values it no longer uses only after the Deployment was saved. A failure at
any point leaves every value the app refers to in place."""
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
TOKEN, OLD = "cloudflared.TUNNEL_TOKEN", "cloudflared.OLD_TOKEN"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def ref(var, key):
    return {"name": var, "valueFrom": {"secretKeyRef": {"name": "tunnel-env", "key": key}}}


def objects():
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": "tunnel", "namespace": "lab", "annotations": {ENVSEC.ANNOTATION: "tunnel-env"}},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "tunnel"}},
                    "template": {"metadata": {"labels": {"app": "tunnel"}}, "spec": {"containers": [
                        {"name": "cloudflared", "image": "cloudflare/cloudflared:2026.1.0", "env": [
                            ref("TUNNEL_TOKEN", TOKEN), ref("OLD_TOKEN", OLD), {"name": "TZ", "value": "UTC"}]}]}}}}
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": "tunnel-env", "namespace": "lab", "labels": {ENVSEC.LABEL: "tunnel"}},
              "data": {TOKEN: b64("first"), OLD: b64("old")}}
    return dep, secret


class Fixture(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(*objects()))
        for patch in (mock.patch.object(LC, "kget", K.kget), mock.patch.object(LC, "ksend", K.ksend),
                      mock.patch.object(WORKLOADS, "kget", K.kget, create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def save(self, env):
        """Save Edit's form for the app with env as its variables, the way
        the page sends it: the kept ones it showed go back as secret_env."""
        form = WORKLOADS.workload_edit_payload("lab", "tunnel", self.kube.object(DEP), [], services=[])
        container = form["containers"][0]
        self.assertEqual({"TUNNEL_TOKEN": "first", "OLD_TOKEN": "old", "TZ": "UTC"}, container["env"])
        return WEDIT.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "image": container["image"],
             "env": env, "secret_env": list(container["secret_env"])}]})

    def missing(self):
        """Keys the app's Deployment refers to that its Secret does not hold."""
        dep, data = self.kube.object(DEP), self.kube.object(SECRET).get("data") or {}
        return sorted(key for key in ENVSEC._refs(dep, "tunnel-env") if key not in data)

    def secret_writes(self):
        return [(method, path) for method, path, *_ in self.kube.writes if path.startswith(SECRET)]


class TheAppKeepsItsValuesWhenASaveFails(Fixture):
    def test_a_failed_deployment_write_leaves_the_removed_value_in_place(self):
        self.kube.fail(DEP, 500, method="PUT", times=10)
        with self.assertRaises(Exception):
            self.save({"TUNNEL_TOKEN": "first", "TZ": "UTC"})
        self.assertEqual([], self.missing(), "the app still refers to every value it had")
        self.assertEqual(b64("old"), self.kube.object(SECRET)["data"][OLD])

    def test_a_conflict_leaves_the_removed_value_in_place_and_adds_the_new_one(self):
        self.kube.fail(DEP, 409, method="PUT", times=10)
        with self.assertRaises(Exception):
            self.save({"TUNNEL_TOKEN": "first", "NEW_TOKEN": "new", "TZ": "UTC"})
        data = self.kube.object(SECRET)["data"]
        self.assertEqual([], self.missing())
        self.assertEqual(b64("old"), data[OLD])
        # The new value was written first; nothing refers to it yet, which is harmless.
        self.assertEqual(b64("new"), data["cloudflared.NEW_TOKEN"])

    def test_a_failed_secret_write_writes_nothing_else(self):
        self.kube.fail(SECRET, 500, method="PUT", times=10)
        before = (self.kube.object(DEP), self.kube.object(SECRET))
        with self.assertRaises(Exception):
            self.save({"TUNNEL_TOKEN": "first", "NEW_TOKEN": "new", "TZ": "UTC"})
        self.assertEqual(before, (self.kube.object(DEP), self.kube.object(SECRET)))
        self.assertFalse([w for w in self.kube.writes if w[1] == DEP], "the app is not written")

    def test_a_secret_write_that_landed_but_lost_its_reply_keeps_every_old_value(self):
        self.kube.fail(SECRET, 500, method="PUT", applied=True)
        with self.assertRaises(Exception):
            self.save({"TUNNEL_TOKEN": "first", "NEW_TOKEN": "new", "TZ": "UTC"})
        self.assertFalse([w for w in self.kube.writes if w[1] == DEP], "the app is not written")
        self.assertEqual([], self.missing())
        self.assertEqual(b64("old"), self.kube.object(SECRET)["data"][OLD])

    def test_a_deployment_write_that_landed_but_lost_its_reply_leaves_every_value_it_needs(self):
        self.kube.fail(DEP, 500, method="PUT", applied=True)
        with self.assertRaises(Exception):
            self.save({"TUNNEL_TOKEN": "first", "NEW_TOKEN": "new", "TZ": "UTC"})
        self.assertEqual([], self.missing(), "the saved app finds every value it refers to")
        self.assertIn("cloudflared.NEW_TOKEN", self.kube.object(SECRET)["data"])


class ASaveThatSucceeds(Fixture):
    def test_the_removed_value_goes_after_the_app_is_saved(self):
        answer = self.save({"TUNNEL_TOKEN": "first", "TZ": "UTC"})
        self.assertTrue(answer["ok"])
        self.assertEqual({TOKEN: b64("first")}, self.kube.object(SECRET)["data"])
        order = [path for _, path, *_ in self.kube.writes if path in (DEP, SECRET)]
        self.assertEqual([DEP, SECRET], order, "the app is written before the value is removed")

    def test_new_values_are_written_before_the_app(self):
        self.save({"TUNNEL_TOKEN": "second", "NEW_TOKEN": "new", "TZ": "UTC"})
        order = [path for _, path, *_ in self.kube.writes if path in (DEP, SECRET)]
        self.assertEqual(SECRET, order[0])
        self.assertEqual(DEP, order[1])
        self.assertEqual({TOKEN: b64("second"), "cloudflared.NEW_TOKEN": b64("new")},
                         self.kube.object(SECRET)["data"])
        self.assertEqual([], self.missing())

    def test_a_failed_removal_after_the_save_still_answers_saved(self):
        # The app is saved and works; the value it no longer uses stays in the
        # Secret until a later save, which is harmless: nothing to do for anyone.
        self.kube.fail(SECRET, 500, method="PUT", times=10)
        answer = self.save({"TUNNEL_TOKEN": "first", "TZ": "UTC"})
        self.assertEqual({"ok": True, "name": "tunnel"}, answer)
        self.assertNotIn(OLD, ENVSEC._refs(self.kube.object(DEP), "tunnel-env"))
        self.assertEqual(b64("old"), self.kube.object(SECRET)["data"][OLD])

    def test_the_next_save_removes_a_value_left_behind(self):
        self.kube.fail(SECRET, 500, method="PUT", times=10)
        self.save({"TUNNEL_TOKEN": "first", "TZ": "UTC"})
        self.kube.stop_failing()
        WEDIT.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "image": "cloudflare/cloudflared:2026.2.0",
             "env": {"TUNNEL_TOKEN": "first", "TZ": "UTC"}, "secret_env": ["TUNNEL_TOKEN"]}]})
        self.assertEqual({TOKEN: b64("first")}, self.kube.object(SECRET)["data"])

    def test_a_secret_changed_by_another_save_in_between_is_not_pruned(self):
        # Another save writes a value its own app will refer to, between this
        # save's Deployment write and the removal: the removal leaves it alone.
        def another_save(method, path, body):
            secret = self.kube.object(SECRET)
            secret["data"]["cloudflared.THEIRS_TOKEN"] = b64("theirs")
            secret["metadata"].pop("resourceVersion", None)
            self.kube.discard(SECRET)
            self.kube.add(secret)
            return None
        self.kube.react("PUT", DEP, another_save)
        self.assertTrue(self.save({"TUNNEL_TOKEN": "first", "TZ": "UTC"})["ok"])
        self.assertIn("cloudflared.THEIRS_TOKEN", self.kube.object(SECRET)["data"])


if __name__ == "__main__":
    unittest.main()
