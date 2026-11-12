"""An Edit save that deletes values stored in the app's Secret needs the
review's acknowledgement of exactly that list, checked by the server.

The review (POST /api/edit/preview) lists the stored values the save deletes
and, when it loses one, answers secret_removed_token, signed over the app,
the keys it deletes, those of them whose value is lost, and the review's
capacity token. The save (POST /api/edit) works the list out again with the
same function, before it writes anything, and refuses without that token,
with one for another app, another body or another list (a key appeared or
went, or a value it kept is now lost), and when the Secret cannot be read. A
save that loses no value needs nothing new, and ignores a token sent anyway.
The agent tool edit_container never sends it: such an edit becomes a request
a person approves, and the approval carries a new review's token."""
import base64
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_agent_audit as AUDIT
import homestead_agent_requests as REQUESTS
import homestead_agent_tools as TOOLS
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_deploy_server as DEPLOY_SERVER
import homestead_env_secrets as ENVSEC
import homestead_kube as K
import homestead_lifecycle as LC
import homestead_mcp as MCP
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


def lost(key):
    """An entry of the review's secret_removed whose value is lost."""
    container, name = key.split(".", 1)
    return {"key": key, "container": container, "name": name, "value_kept": False}


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
        still does not let tunnel's save delete anything. (The capacity token
        alone already ties the app, through the Deployment's identity; the
        app's names in the signed context are a second binding no route can
        reach on its own.)"""
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
        shorter = WORKLOADS.secret_removal_token("lab", "tunnel", [lost("cloudflared.OLD_TOKEN")], review["capacity_token"])
        self.assertRefused(self.save(body, dict(review, secret_removed_token=shorter)), "changed")
        longer = WORKLOADS.secret_removal_token("lab", "tunnel", [lost("cloudflared.OLD_TOKEN"), lost("worker.DB_PASSWORD"),
                                                                  lost("worker.OTHER")], review["capacity_token"])
        self.assertRefused(self.save(body, dict(review, secret_removed_token=longer)), "changed")
        self.assertNothingWritten()

    def test_an_acknowledgement_that_lists_a_lost_value_as_kept_is_refused(self):
        """The same keys, signed with one of them as kept: the tick it stands
        for did not cover that value."""
        body = self.body(env=DROP_OLD, worker=False)
        review = self.review(body)
        kept = WORKLOADS.secret_removal_token("lab", "tunnel", [lost("cloudflared.OLD_TOKEN"),
                                                                dict(lost("worker.DB_PASSWORD"), value_kept=True)],
                                              review["capacity_token"])
        self.assertRefused(self.save(body, dict(review, secret_removed_token=kept)), "changed")
        self.assertNothingWritten()

    def test_the_reviews_tokens_with_another_body_are_refused(self):
        """Review A's capacity token and acknowledgement, sent with a body that
        deletes the same value but changes something else: refused, even when
        the capacity plan asks for no confirmation (so enforce() alone would
        not look at the capacity token)."""
        body = self.body(env=DROP_OLD)
        review = self.review(body)
        other = copy.deepcopy(body)
        other["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        self.assertRefused(self.save(other, review), "changed")
        self.assertNothingWritten()

    def test_a_value_the_review_kept_and_that_is_lost_by_the_save_is_refused(self):
        """The review renames TUNNEL_TOKEN with the same value: nothing is lost,
        so there is no tick and no token. Before the save another writer
        changes the stored value: the save would now lose it, unticked."""
        body = self.body(env={"TUNNEL_LOGIN": "first", "OLD_TOKEN": "old", "TZ": "UTC"})
        review = self.review(body)
        self.assertEqual([("cloudflared.TUNNEL_TOKEN", True)],
                         [(entry["key"], entry["value_kept"]) for entry in review["secret_removed"]])
        self.assertNotIn("secret_removed_token", review)
        secret = self.kube.object(secret_path("tunnel"))
        secret["data"]["cloudflared.TUNNEL_TOKEN"] = b64("rotated")
        self.kube.add(secret)
        message = self.assertRefused(self.save(body, review), "unconfirmed")
        self.assertIn("TUNNEL_TOKEN", message)
        self.assertNothingWritten()
        self.assertEqual(b64("rotated"), self.stored()["cloudflared.TUNNEL_TOKEN"])

    def test_a_token_for_a_kept_only_list_does_not_cover_a_value_lost_since(self):
        """The same, with a token signed over that kept-only list: it names the
        same key, but not as lost, so the save is refused."""
        body = self.body(env={"TUNNEL_LOGIN": "first", "OLD_TOKEN": "old", "TZ": "UTC"})
        review = self.review(body)
        token = WORKLOADS.secret_removal_token("lab", "tunnel", review["secret_removed"], review["capacity_token"])
        secret = self.kube.object(secret_path("tunnel"))
        secret["data"]["cloudflared.TUNNEL_TOKEN"] = b64("rotated")
        self.kube.add(secret)
        self.assertRefused(self.save(body, dict(review, secret_removed_token=token)), "changed")
        self.assertNothingWritten()
        self.assertEqual(b64("rotated"), self.stored()["cloudflared.TUNNEL_TOKEN"])

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
        self.assertNotIn("secret_removed_token", self.review(body), "nothing to tick, nothing to send")
        answer = self.save(body)
        self.assertEqual(["cloudflared.TUNNEL_TOKEN"], answer["secret_removed"])
        self.assertEqual(b64("first"), self.stored()["cloudflared.TUNNEL_LOGIN"])

    def test_a_token_it_does_not_need_is_ignored(self):
        """An earlier review's token (one that deleted a value), sent with a
        save that deletes nothing, as a script or an agent that keeps the last
        token it got would: saved, not refused as changed."""
        earlier = self.review(self.body(env=DROP_OLD))
        body = self.body()
        body["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        review = dict(self.review(body), secret_removed_token=earlier["secret_removed_token"])
        self.assertEqual({"ok": True, "name": "tunnel"}, self.save(body, review))
        self.assertEqual({"ok": True, "name": "tunnel"}, self.save({**body, "confirm_secret_removed": "1.x"}))

    def test_a_token_it_does_not_need_is_ignored_for_a_renamed_variable_too(self):
        body = self.body(env={"TUNNEL_LOGIN": "first", "OLD_TOKEN": "old", "TZ": "UTC"})
        answer = self.save({**body, "confirm_secret_removed": "garbage"})
        self.assertEqual(["cloudflared.TUNNEL_TOKEN"], answer["secret_removed"])

    def test_an_app_without_a_secret_of_its_own(self):
        dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "plain", "namespace": "lab"},
               "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "plain"}},
                        "template": {"metadata": {"labels": {"app": "plain"}}, "spec": {"containers": [
                            {"name": "main", "image": "example/plain:1", "env": [{"name": "TZ", "value": "UTC"}]}]}}}}
        self.kube.add(dep)
        body = {"ns": "lab", "name": "plain", "containers": [
            {"original_name": "main", "name": "main", "image": "example/plain:2", "env": {"TZ": "UTC"}}]}
        self.assertEqual({"ok": True, "name": "plain"}, self.save(body))


class ACallerThatConfirmsNothing(Fixture):
    """edit_workload deletes only the keys its caller confirmed: one that
    passes no list (a future caller, or prepare_edit's own answer) deletes
    none, and the values stay for an Edit save to list."""

    def test_edit_workload_alone_deletes_no_stored_value(self):
        answer = WEDIT.edit_workload(self.body(env=DROP_OLD, worker=False))
        self.assertNotIn("secret_removed", answer)
        self.assertEqual({"cloudflared.TUNNEL_TOKEN", "cloudflared.OLD_TOKEN", "worker.DB_PASSWORD"}, set(self.stored()))
        saved = self.kube.object(dep_path("tunnel"))["spec"]["template"]["spec"]["containers"]
        self.assertEqual(["cloudflared"], [container["name"] for container in saved], "the rest of the save is made")

    def test_a_prepared_edit_without_a_list_deletes_none(self):
        body = self.body(env=DROP_OLD)
        self.assertNotIn("secret_removed", WEDIT.edit_workload(body, prepared=WEDIT.prepare_edit(dict(body))))
        self.assertIn("cloudflared.OLD_TOKEN", self.stored())


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


VALUES = ('"first"', '"old"', '"db-pass"', '"rotated"', b64("first"), b64("old"), b64("db-pass"))


class AnAgentProposesAPersonApproves(Fixture):
    """edit_container through the MCP server (homestead_mcp.call_tool) and an
    approval (homestead_agent_requests.approve), on the real routes: an agent
    may propose an edit that deletes stored values, but only a person's
    approval deletes them."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        keep_bound(self, MCP, AUDIT, REQUESTS, TOOLS)
        MCP.bind(folder.name, lambda: {"mcp": {"enabled": True}}, "9.9.9", ROUTE_POLICY.role,
                 lambda method, path, body: None, K.kget, None)
        MCP._calls.clear()
        self.addCleanup(MCP._calls.clear)
        self.key = {"id": "k1", "name": "claude", "owner": "ada", "scopes": ["read", "containers:manage"],
                    "expires": time.time() + 3600, "mcp": True, "role": "operator", "kind": "key"}

    def agent(self, name, settings):
        return MCP.call_tool(name, {"settings": copy.deepcopy(settings)}, self.key, "192.0.2.40")

    def propose(self, body, own_token=True):
        """The agent's preview, then its save with the preview's tokens, its
        own acknowledgement included when own_token."""
        review = self.agent("preview_edit_container", body)
        return self.agent("edit_container", {**body, "capacity_token": review["capacity_token"], "confirm_capacity": True,
                                             **({"confirm_secret_removed": review["secret_removed_token"]}
                                                if own_token else {})})

    def test_the_agents_own_valid_token_deletes_nothing(self):
        answer = self.propose(self.body(env=DROP_OLD))
        self.assertEqual("pending_approval", answer["status"])
        self.assertIn("lists them", answer["note"])
        self.assertNothingWritten()
        self.assertEqual(b64("old"), self.stored()["cloudflared.OLD_TOKEN"])
        row = REQUESTS.get(answer["request"])
        self.assertEqual("operator", row["role"])
        self.assertEqual(["OLD_TOKEN (cloudflared)"], row["summary"]["deletes_stored_values"])
        self.assertIn("cannot restore them", row["summary"]["effect"])
        self.assertEqual("lab/tunnel", row["summary"]["app"])

    def test_without_a_token_it_is_a_request_too(self):
        answer = self.propose(self.body(env=DROP_OLD, worker=False), own_token=False)
        self.assertEqual("pending_approval", answer["status"])
        self.assertEqual(["DB_PASSWORD (worker)", "OLD_TOKEN (cloudflared)"],
                         REQUESTS.get(answer["request"])["summary"]["deletes_stored_values"])
        self.assertNothingWritten()

    def test_an_approved_run_deletes_exactly_the_listed_values(self):
        rid = self.propose(self.body(env=DROP_OLD))["request"]
        done = REQUESTS.approve(rid, "ada", "operator")
        self.assertEqual("done", done["status"], done)
        self.assertEqual(["cloudflared.OLD_TOKEN"], done["result"]["secret_removed"])
        self.assertEqual({"cloudflared.TUNNEL_TOKEN", "worker.DB_PASSWORD"}, set(self.stored()))

    def test_a_list_that_changed_after_the_request_is_refused(self):
        rid = self.propose(self.body(env=DROP_OLD))["request"]
        secret = self.kube.object(secret_path("tunnel"))
        secret["data"]["cloudflared.LEFTOVER_TOKEN"] = b64("left")
        self.kube.add(secret)
        before = (self.kube.object(dep_path("tunnel")), dict(self.stored()))
        failed = REQUESTS.approve(rid, "ada", "operator")
        self.assertEqual("failed", failed["status"])
        self.assertIn("not the ones the request listed", failed["detail"])
        self.assertIn("check the new list", failed["detail"])
        self.assertEqual(before, (self.kube.object(dep_path("tunnel")), dict(self.stored())))

    def test_a_value_listed_but_kept_by_the_time_of_approval_is_refused(self):
        """The list shrank: the approval covered a different list, so nothing is saved."""
        rid = self.propose(self.body(env=DROP_OLD, worker=False))["request"]
        self.kube.add(app("tunnel", {"worker.DB_PASSWORD": b64("first")})[1])   # now the same as a value kept
        failed = REQUESTS.approve(rid, "ada", "operator")
        self.assertEqual("failed", failed["status"])
        self.assertIn("worker.DB_PASSWORD", self.stored())

    def test_an_approval_filed_for_another_reason_does_not_delete_values(self):
        """A request whose approval listed no stored values (host access, say)
        still deletes none when the edit by then would."""
        body = self.body(env=DROP_OLD)
        review = self.agent("preview_edit_container", body)
        settings = {**body, "capacity_token": review["capacity_token"], "confirm_capacity": True}
        request = REQUESTS.create(TOOLS.BY_NAME["edit_container"], {"settings": settings},
                                  {"action": "Edit a container: this needs the admin role", "settings": settings},
                                  "admin", self.key, "this needs the admin role", "192.0.2.40", escalated=True)
        self.assertEqual("failed", REQUESTS.approve(request["id"], "ada", "admin")["status"])
        self.assertEqual(b64("old"), self.stored()["cloudflared.OLD_TOKEN"])

    def test_no_value_appears_in_the_approval_the_answer_or_the_audit_log(self):
        answer = self.propose(self.body(env=DROP_OLD))
        shown = json.dumps(REQUESTS.listing("operator"))
        done = REQUESTS.approve(answer["request"], "ada", "operator")
        audit = Path(AUDIT._path()).read_text(encoding="utf-8")
        for text in (json.dumps(answer), shown, json.dumps(done), audit):
            for value in VALUES:
                self.assertNotIn(value, text)
        self.assertIn("OLD_TOKEN (cloudflared)", shown)

    def test_a_save_that_deletes_nothing_runs_at_once(self):
        body = self.body()
        body["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        self.assertEqual({"ok": True, "name": "tunnel"}, self.propose(body, own_token=False))

    def test_a_renamed_variable_runs_at_once(self):
        answer = self.propose(self.body(env={"TUNNEL_LOGIN": "first", "OLD_TOKEN": "old", "TZ": "UTC"}), own_token=False)
        self.assertEqual(["cloudflared.TUNNEL_TOKEN"], answer["secret_removed"])

    def test_its_description_says_so(self):
        tool = TOOLS.BY_NAME["edit_container"]
        self.assertIn("never runs on the agent's call", tool.description)
        self.assertIn("confirm_secret_removed is ignored", tool.description)
        wiki = (Path(__file__).resolve().parents[1] / "docs" / "wiki" / "AI-agents.md").read_text(encoding="utf-8")
        row = next(line for line in wiki.splitlines() if line.startswith("| `edit_container`"))
        self.assertIn("never runs on the agent's call", row)
        self.assertIn("An `edit_container` save that deletes values stored in the app's Secret is a\n  request too.", wiki)


class TheAcknowledgementIsAControlField(unittest.TestCase):
    def test_sending_it_keeps_the_capacity_token_valid(self):
        """It is added after the review, as confirm_managed is: the capacity
        token signed over the body without it still holds."""
        self.assertIn("confirm_secret_removed", CAPACITY_REVIEW.CONTROL_FIELDS)

    def test_the_token_carries_no_value(self):
        """What is signed is the app, the key names (which of them lose their
        value) and the capacity token:
        no value can be in it or worked out from it."""
        with mock.patch.object(CAPACITY_REVIEW, "_key", lambda: b"a test key, not a real one"), \
                mock.patch.object(CAPACITY_REVIEW, "issue", wraps=CAPACITY_REVIEW.issue) as issue:
            WORKLOADS.secret_removal_token("lab", "tunnel", [lost("cloudflared.OLD_TOKEN"),
                                                             dict(lost("cloudflared.TUNNEL_TOKEN"), value_kept=True)], "1.abc")
        config, context = issue.call_args.args
        self.assertEqual({}, config)
        self.assertEqual({"secret_removed": {"namespace": "lab", "name": "tunnel",
                                             "keys": ["cloudflared.OLD_TOKEN", "cloudflared.TUNNEL_TOKEN"],
                                             "lost": ["cloudflared.OLD_TOKEN"], "review": "1.abc"}},
                         json.loads(json.dumps(context)))


if __name__ == "__main__":
    unittest.main()
