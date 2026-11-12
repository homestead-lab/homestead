"""An Edit save that deletes values stored in the app's Secret needs the
review's acknowledgement of exactly that list, checked by the server.

The review (POST /api/edit/preview) lists the stored values the save deletes
and answers secret_removed_token, signed over the app, the list of keys and
the review's capacity token. The save (POST /api/edit) works the list out
again with the same function, before it writes anything, and refuses
without that token, with one for another app or another list (a key appeared
or went since the review), and when the Secret cannot be read. A save that
deletes nothing needs nothing new. The agent tool edit_container passes it
inside settings, as it passes the other confirmations."""
import base64
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_agent_tools as TOOLS
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_deploy_server as DEPLOY_SERVER
import homestead_env_secrets as ENVSEC
import homestead_kube as K
import homestead_lifecycle as LC
import homestead_route_policy as ROUTE_POLICY
import homestead_routes as ROUTER
import homestead_workload_edit as WEDIT
import server  # noqa: F401  (binds the modules the Edit form and save go through)
import homestead_workloads as WORKLOADS
import homestead_start_order as START
from fakes.kube import Kube
from rebind import keep_bound


def b64(text):
    return base64.b64encode(text.encode()).decode()


def app(name, extra=None):
    """An app with two stored values in cloudflared and one in worker, and its Secret."""
    secret_name = f"{name}-env"

    def ref(var, key):
        return {"name": var, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}}}
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": name, "namespace": "lab", "uid": f"uid-{name}",
                        "annotations": {ENVSEC.ANNOTATION: secret_name}},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                    "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [
                        {"name": "cloudflared", "image": "cloudflare/cloudflared:2026.1.0", "env": [
                            ref("TUNNEL_TOKEN", "cloudflared.TUNNEL_TOKEN"), ref("OLD_TOKEN", "cloudflared.OLD_TOKEN"),
                            {"name": "TZ", "value": "UTC"}]},
                        {"name": "worker", "image": "example/worker:1", "env": [
                            ref("DB_PASSWORD", "worker.DB_PASSWORD")]}]}}}}
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": secret_name, "namespace": "lab", "labels": {ENVSEC.LABEL: name}},
              "data": {"cloudflared.TUNNEL_TOKEN": b64("first"), "cloudflared.OLD_TOKEN": b64("old"),
                       "worker.DB_PASSWORD": b64("db-pass"), **(extra or {})}}
    return dep, secret


def dep_path(name):
    return f"/apis/apps/v1/namespaces/lab/deployments/{name}"


def secret_path(name):
    return f"/api/v1/namespaces/lab/secrets/{name}-env"


def request(path, body):
    return ROUTER.Request("POST", path, {}, copy.deepcopy(body), "ada", "admin")


class Fixture(unittest.TestCase):
    """POST /api/edit/preview and POST /api/edit on a fake API server, with
    the real prepare_edit, the real review tokens (a test key) and the real
    save; only the capacity plan is stubbed (no nodes here)."""

    def setUp(self):
        self.plan = {"blocked": False, "warnings": [], "requires_confirmation": False}
        self.icons = mock.Mock()
        for patch in (mock.patch.object(LC, "kget", K.kget), mock.patch.object(LC, "ksend", K.ksend),
                      mock.patch.object(WORKLOADS, "kget", K.kget, create=True),
                      mock.patch.object(WORKLOADS, "guard_managed_smb", create=True),
                      mock.patch.object(WORKLOADS, "guard_self", create=True),
                      mock.patch.object(WORKLOADS, "is_self", return_value=False, create=True),
                      mock.patch.object(WORKLOADS, "edit_capacity_plan", side_effect=self.capacity_plan),
                      mock.patch.object(DEPLOY_SERVER, "persist_icon_config", self.icons),
                      mock.patch.object(CAPACITY_REVIEW, "_key", lambda: b"a test key, not a real one")):
            patch.start()
            self.addCleanup(patch.stop)
        self.kube = self.enterContext(Kube(*app("tunnel"), *app("relay")))

    def capacity_plan(self, body):
        """edit_capacity_plan's answer: the real edit, and a context that,
        as the real one does, names the app (its identity)."""
        current = K.kget(dep_path(body["name"]))
        prepared = WEDIT.prepare_edit(dict(body), current=current)
        context = {"action": "edit", "deployment": CAPACITY_REVIEW.review_identity(current)}
        return prepared, context, self.plan

    def body(self, name="tunnel", env=None, worker=True):
        """What Edit's Save sends for app name: the form as it opened, with
        cloudflared's variables as env and the worker container kept or removed."""
        form = WORKLOADS.workload_edit_payload("lab", name, self.kube.object(dep_path(name)), [], services=[])
        containers = []
        for container in form["containers"]:
            if container["name"] == "worker" and not worker:
                continue
            containers.append({"original_name": container["name"], "name": container["name"],
                               "image": container["image"],
                               "env": env if env is not None and container["name"] == "cloudflared" else dict(container["env"]),
                               "secret_env": list(container["secret_env"])})
        return {"ns": "lab", "name": name, "containers": containers,
                **({} if worker else {"remove_containers": ["worker"]})}

    def review(self, body):
        return WORKLOADS.preview_edit(request("/api/edit/preview", body))

    def save(self, body, review=None, ack=True):
        """POST /api/edit as the page sends it after review: its capacity
        token, and the acknowledgement when ack."""
        sent = dict(body)
        if review is not None:
            sent.update(capacity_token=review["capacity_token"], confirm_capacity=True)
            if ack and review.get("secret_removed_token"):
                sent["confirm_secret_removed"] = review["secret_removed_token"]
        return WORKLOADS.edit(request("/api/edit", sent))

    def stored(self, name="tunnel"):
        return self.kube.object(secret_path(name))["data"]

    def assertRefused(self, answer, reason):
        self.assertIsInstance(answer, ROUTER.Reply, answer)
        self.assertEqual(409, answer.code)
        self.assertEqual("secret_removed", answer.body["refused"])
        self.assertEqual(reason, answer.body["reason"])
        return answer.body["error"]

    def assertNothingWritten(self):
        self.assertEqual([], self.kube.writes, "neither the Secret nor the Deployment")
        self.icons.assert_not_called()


DROP_OLD = {"TUNNEL_TOKEN": "first", "TZ": "UTC"}


class ASaveThatDeletesStoredValues(Fixture):
    def test_without_the_acknowledgement_is_refused_and_writes_nothing(self):
        body = self.body(env=DROP_OLD)
        review = self.review(body)
        message = self.assertRefused(self.save(body, review, ack=False), "unconfirmed")
        self.assertNothingWritten()
        self.assertEqual(b64("old"), self.stored()["cloudflared.OLD_TOKEN"])
        self.assertIn("Saving deletes 1 value stored in this app's Secret.", message)
        self.assertIn("OLD_TOKEN", message)
        self.assertNotIn("old", message.replace("OLD_TOKEN", ""), "names only, never a value")

    def test_with_no_review_at_all_is_refused(self):
        self.assertRefused(self.save(self.body(env=DROP_OLD)), "unconfirmed")
        self.assertNothingWritten()

    def test_a_plain_yes_is_not_the_acknowledgement(self):
        answer = self.save({**self.body(env=DROP_OLD), "confirm_secret_removed": True})
        message = self.assertRefused(answer, "unconfirmed")
        self.assertTrue(message.endswith("Review the changes and confirm deleting them."), "no field names for a person")
        self.assertIn("confirm_secret_removed", answer.body["detail"], "they are for API callers")
        self.assertIn("secret_removed_token", answer.body["detail"])
        self.assertNothingWritten()

    def test_with_the_reviews_acknowledgement_is_saved(self):
        body = self.body(env=DROP_OLD, worker=False)
        review = self.review(body)
        answer = self.save(body, review)
        self.assertEqual(True, answer["ok"], answer)
        self.assertEqual(["cloudflared.OLD_TOKEN", "worker.DB_PASSWORD"], answer["secret_removed"])
        self.assertEqual({"cloudflared.TUNNEL_TOKEN": b64("first")}, self.stored())

    def test_an_acknowledgement_for_another_app_is_refused(self):
        """relay has the same keys and the same change: its review's token
        still does not let tunnel's save delete anything."""
        theirs = self.review(self.body("relay", env=DROP_OLD))
        body = self.body(env=DROP_OLD)
        ours = self.review(body)
        sent = {**body, "capacity_token": ours["capacity_token"], "confirm_capacity": True,
                "confirm_secret_removed": theirs["secret_removed_token"]}
        self.assertRefused(WORKLOADS.edit(request("/api/edit", sent)), "changed")
        self.assertNothingWritten()

    def test_an_acknowledgement_with_another_reviews_capacity_token_is_refused(self):
        """The acknowledgement is bound to its own review: sent with the
        capacity token of a later review of the same save, it is refused."""
        body = self.body(env=DROP_OLD)
        with mock.patch.object(CAPACITY_REVIEW.time, "time", return_value=time.time() - 5):
            first = self.review(body)
        second = dict(self.review(body), secret_removed_token=first["secret_removed_token"])
        self.assertNotEqual(first["capacity_token"], second["capacity_token"])
        self.assertRefused(self.save(body, second), "changed")
        self.assertNothingWritten()

    def test_an_acknowledgement_older_than_a_review_lasts_is_refused(self):
        body = self.body(env=DROP_OLD)
        with mock.patch.object(CAPACITY_REVIEW.time, "time", return_value=time.time() - CAPACITY_REVIEW.TTL - 5):
            review = self.review(body)
        message = self.assertRefused(self.save(body, review), "changed")
        self.assertIn("more than 10 minutes old", message)
        self.assertNothingWritten()

    def test_a_key_that_appears_after_the_review_is_refused(self):
        """The save would delete a longer list than the review showed."""
        body = self.body(env=DROP_OLD)
        review = self.review(body)
        self.assertEqual(["cloudflared.OLD_TOKEN"], [entry["key"] for entry in review["secret_removed"]])
        secret = self.kube.object(secret_path("tunnel"))
        secret["data"]["cloudflared.LEFTOVER_TOKEN"] = b64("left")
        self.kube.add(secret)
        message = self.assertRefused(self.save(body, review), "changed")
        self.assertNothingWritten()
        self.assertIn("changed since the review", message)
        self.assertIn(b64("left"), self.stored().values())

    def test_a_key_that_goes_after_the_review_is_refused(self):
        """The save would delete a shorter list than the review showed."""
        self.kube.add(app("tunnel", {"cloudflared.LEFTOVER_TOKEN": b64("left")})[1])
        body = self.body(env=DROP_OLD)
        review = self.review(body)
        self.assertEqual(["cloudflared.LEFTOVER_TOKEN", "cloudflared.OLD_TOKEN"],
                         sorted(entry["key"] for entry in review["secret_removed"]))
        self.kube.add(app("tunnel")[1])
        self.assertRefused(self.save(body, review), "changed")
        self.assertNothingWritten()

    def test_an_acknowledgement_signed_for_a_shorter_list_is_refused(self):
        """A token the server signed, for this app and review, over fewer keys."""
        body = self.body(env=DROP_OLD, worker=False)
        review = self.review(body)
        shorter = WORKLOADS.secret_removal_token("lab", "tunnel", ["cloudflared.OLD_TOKEN"], review["capacity_token"])
        self.assertRefused(self.save(body, dict(review, secret_removed_token=shorter)), "changed")
        longer = WORKLOADS.secret_removal_token("lab", "tunnel", ["cloudflared.OLD_TOKEN", "worker.DB_PASSWORD",
                                                                  "worker.OTHER"], review["capacity_token"])
        self.assertRefused(self.save(body, dict(review, secret_removed_token=longer)), "changed")
        self.assertNothingWritten()

    def test_a_key_that_appears_during_the_save_is_kept(self):
        """After the save's check, before it writes, another save leaves a
        key: it was never listed, so this save does not remove it."""
        body = self.body(env=DROP_OLD)
        review = self.review(body)
        reads = []

        def another_save(method, path, sent):
            reads.append(path)
            if len(reads) == 2:     # the check read it first; this is the save's own read
                secret = self.kube.object(secret_path("tunnel"))
                secret["data"]["cloudflared.LEFTOVER_TOKEN"] = b64("left")
                self.kube.add(secret)
        self.kube.react("GET", secret_path("tunnel"), another_save)
        answer = self.save(body, review)
        self.assertEqual(2, len(reads))
        self.assertEqual(["cloudflared.OLD_TOKEN"], answer["secret_removed"])
        self.assertEqual(b64("left"), self.stored()["cloudflared.LEFTOVER_TOKEN"])
        self.assertNotIn("cloudflared.OLD_TOKEN", self.stored())


class ASecretThatCannotBeRead(Fixture):
    def test_a_review_that_could_not_read_it_gives_nothing_to_save_with(self):
        body = self.body(env=DROP_OLD)
        self.kube.fail(secret_path("tunnel"), 500, method="GET", times=1)
        review = self.review(body)
        self.assertIsNone(review["secret_removed"])
        self.assertIsNone(review.get("secret_removed_token"))
        self.assertRefused(self.save(body, review), "unconfirmed")   # readable again now: still refused
        self.assertNothingWritten()

    def test_a_save_that_cannot_read_it_is_refused_before_writing(self):
        body = self.body(env=DROP_OLD)
        review = self.review(body)
        self.kube.fail(secret_path("tunnel"), 500, method="GET", times=10)
        message = self.assertRefused(self.save(body, review), "unread")
        self.assertNothingWritten()
        self.assertIn("Homestead could not read this app's Secret. Nothing was saved.", message)


class ASaveThatDeletesNothing(Fixture):
    """Needs no acknowledgement and saves as before."""

    def test_a_changed_image(self):
        body = self.body()
        body["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        review = self.review(body)
        self.assertEqual([], review["secret_removed"])
        self.assertIsNone(review.get("secret_removed_token"))
        before = dict(self.stored())
        self.assertEqual({"ok": True, "name": "tunnel"}, self.save(body, review))
        self.assertEqual(before, self.stored())
        saved = self.kube.object(dep_path("tunnel"))["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual("cloudflare/cloudflared:2026.2.0", saved)

    def test_without_any_review_as_an_api_caller_sends_it(self):
        body = self.body()
        body["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        self.assertEqual({"ok": True, "name": "tunnel"}, self.save(body))

    def test_a_renamed_variable_whose_value_moves(self):
        """Its old key goes, but its value lives on: nothing is lost, no tick."""
        body = self.body(env={"TUNNEL_LOGIN": "first", "OLD_TOKEN": "old", "TZ": "UTC"})
        answer = self.save(body)
        self.assertEqual(["cloudflared.TUNNEL_TOKEN"], answer["secret_removed"])
        self.assertEqual(b64("first"), self.stored()["cloudflared.TUNNEL_LOGIN"])

    def test_an_app_without_a_secret_of_its_own(self):
        dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "plain", "namespace": "lab"},
               "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "plain"}},
                        "template": {"metadata": {"labels": {"app": "plain"}}, "spec": {"containers": [
                            {"name": "main", "image": "example/plain:1", "env": [{"name": "TZ", "value": "UTC"}]}]}}}}
        self.kube.add(dep)
        body = {"ns": "lab", "name": "plain", "containers": [
            {"original_name": "main", "name": "main", "image": "example/plain:2", "env": {"TZ": "UTC"}}]}
        self.assertEqual({"ok": True, "name": "plain"}, self.save(body))


class TheStartOrderSave(Fixture):
    """POST /api/workloads/starts-after saves through the same save: it never
    deletes a stored value (it changes no variable; a key nothing uses stays
    for an Edit save, whose review lists it)."""

    def test_a_key_nothing_uses_stays(self):
        self.kube.add(app("tunnel", {"cloudflared.LEFTOVER_TOKEN": b64("left")})[1])
        node = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node-a"}, "status": {"images": []}}
        service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "relay", "namespace": "lab"},
                   "spec": {"selector": {"app": "relay"}, "ports": [{"port": 80, "protocol": "TCP"}]}}
        self.kube.add(node)
        self.kube.add(service)
        with mock.patch.object(WORKLOADS, "homestead_part", create=True, new=lambda ns, name: ""), \
                mock.patch.object(WORKLOADS, "SYS_NS", {"kube-system"}), \
                mock.patch.object(START, "ensure_image", return_value=None):
            answer = START.save({"ns": "lab", "name": "tunnel", "after": [{"ns": "lab", "app": "relay"}]})
        self.assertTrue(answer["restarts"], answer)
        self.assertEqual(b64("left"), self.stored()["cloudflared.LEFTOVER_TOKEN"])


class TheAgentTool(Fixture):
    """edit_container passes confirmations inside settings, as it passes
    confirm_managed for a rename of an app git applies (docs/wiki/Containers.md):
    without the review's token the save is refused and writes nothing."""

    def setUp(self):
        super().setUp()
        keep_bound(self, TOOLS)
        TOOLS.bind(ROUTE_POLICY.role, lambda method, path, body: None, K.kget, None)
        self.ctx = {"user": "api-key:claude", "role": "operator"}

    def tool(self, name, settings, fresh=False):
        run = next(tool for tool in TOOLS.TOOLS if tool.name == name).run
        return run({"settings": settings}, {**self.ctx, **({"fresh": True} if fresh else {})})

    def test_without_the_acknowledgement_it_is_refused(self):
        body = self.body(env=DROP_OLD)
        review = self.tool("preview_edit_container", body)
        with self.assertRaises(TOOLS.ToolError) as refused:
            self.tool("edit_container", {**body, "capacity_token": review["capacity_token"], "confirm_capacity": True})
        self.assertIn("OLD_TOKEN", str(refused.exception))
        self.assertIn("confirm_secret_removed", refused.exception.data["detail"])
        self.assertNothingWritten()

    def test_with_the_previews_token_it_is_saved(self):
        body = self.body(env=DROP_OLD)
        review = self.tool("preview_edit_container", body)
        answer = self.tool("edit_container", {**body, "capacity_token": review["capacity_token"], "confirm_capacity": True,
                                              "confirm_secret_removed": review["secret_removed_token"]})
        self.assertEqual(["cloudflared.OLD_TOKEN"], answer["secret_removed"])

    def test_an_approved_run_reviews_again_and_the_old_token_no_longer_holds(self):
        body = self.body(env=DROP_OLD)
        with mock.patch.object(CAPACITY_REVIEW.time, "time", return_value=time.time() - 5):
            review = self.tool("preview_edit_container", body)
        with self.assertRaises(TOOLS.ToolError) as refused:
            self.tool("edit_container", {**body, "capacity_token": review["capacity_token"], "confirm_capacity": True,
                                         "confirm_secret_removed": review["secret_removed_token"]}, fresh=True)
        self.assertIn("changed since the review", str(refused.exception))
        self.assertNothingWritten()

    def test_a_save_that_deletes_nothing_goes_through(self):
        body = self.body()
        body["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        review = self.tool("preview_edit_container", body)
        answer = self.tool("edit_container", {**body, "capacity_token": review["capacity_token"], "confirm_capacity": True})
        self.assertEqual({"ok": True, "name": "tunnel"}, answer)

    def test_its_description_says_so(self):
        tool = next(tool for tool in TOOLS.TOOLS if tool.name == "edit_container")
        self.assertIn("confirm_secret_removed", tool.description)
        self.assertIn("secret_removed_token", tool.description)
        wiki = (Path(__file__).resolve().parents[1] / "docs" / "wiki" / "AI-agents.md").read_text(encoding="utf-8")
        row = next(line for line in wiki.splitlines() if line.startswith("| `edit_container`"))
        self.assertIn("confirm_secret_removed", row)


class TheAcknowledgementIsAControlField(unittest.TestCase):
    def test_sending_it_keeps_the_capacity_token_valid(self):
        """It is added after the review, as confirm_managed is: the capacity
        token signed over the body without it still holds."""
        self.assertIn("confirm_secret_removed", CAPACITY_REVIEW.CONTROL_FIELDS)

    def test_the_token_carries_no_value(self):
        """What is signed is the app, the key names and the capacity token:
        no value can be in it or worked out from it."""
        with mock.patch.object(CAPACITY_REVIEW, "_key", lambda: b"a test key, not a real one"), \
                mock.patch.object(CAPACITY_REVIEW, "issue", wraps=CAPACITY_REVIEW.issue) as issue:
            WORKLOADS.secret_removal_token("lab", "tunnel", ["cloudflared.OLD_TOKEN"], "1.abc")
        config, context = issue.call_args.args
        self.assertEqual({}, config)
        self.assertEqual({"secret_removed": {"namespace": "lab", "name": "tunnel", "keys": ["cloudflared.OLD_TOKEN"],
                                             "review": "1.abc"}}, json.loads(json.dumps(context)))


if __name__ == "__main__":
    unittest.main()
