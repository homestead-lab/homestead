"""The Edit review names the stored values a save deletes.

Homestead keeps an app's secret-looking variables in its own Secret. A save
that leaves one out - the variable removed, or its whole container - deletes
its value, and nothing else holds it. The review (POST /api/edit/preview)
now lists those variables, says when it could not check, and the save's
answer lists the keys it removed."""
import base64
import copy
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
from fakes import confirmed_edit as CONFIRMED

DEP = "/apis/apps/v1/namespaces/lab/deployments/tunnel"
SECRET = "/api/v1/namespaces/lab/secrets/tunnel-env"


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
                            ref("TUNNEL_TOKEN", "cloudflared.TUNNEL_TOKEN"), ref("OLD_TOKEN", "cloudflared.OLD_TOKEN"),
                            {"name": "TZ", "value": "UTC"}]},
                        {"name": "worker", "image": "example/worker:1", "env": [
                            ref("DB_PASSWORD", "worker.DB_PASSWORD")]}]}}}}
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": "tunnel-env", "namespace": "lab", "labels": {ENVSEC.LABEL: "tunnel"}},
              "data": {"cloudflared.TUNNEL_TOKEN": b64("first"), "cloudflared.OLD_TOKEN": b64("old"),
                       "worker.DB_PASSWORD": b64("db-pass")}}
    return dep, secret


class Request:
    def __init__(self, body):
        self.body = body


class Fixture(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(LC, "kget", K.kget), mock.patch.object(LC, "ksend", K.ksend),
                      mock.patch.object(WORKLOADS, "kget", K.kget, create=True),
                      mock.patch.object(WORKLOADS, "guard_managed_smb", create=True),
                      mock.patch.object(WORKLOADS.CAPACITY_REVIEW, "issue", return_value="1.token")):
            patch.start()
            self.addCleanup(patch.stop)
        self.open(self.enterContext(Kube(*objects())))

    def open(self, kube):
        """Open Edit for the app on kube, as the page does."""
        self.kube = kube
        form = WORKLOADS.workload_edit_payload("lab", "tunnel", self.kube.object(DEP), [], services=[])
        self.form = {c["name"]: c for c in form["containers"]}

    def body(self, env=None, worker=True):
        """The body Edit's Save sends, with cloudflared's variables as env and
        the worker container kept or removed."""
        containers = []
        for name, container in self.form.items():
            if name == "worker" and not worker:
                continue
            containers.append({"original_name": name, "name": name, "image": container["image"],
                               "env": env if env is not None and name == "cloudflared" else dict(container["env"]),
                               "secret_env": list(container["secret_env"])})
        return {"ns": "lab", "name": "tunnel", "containers": containers,
                **({} if worker else {"remove_containers": ["worker"]})}

    def review(self, body):
        """POST /api/edit/preview, with the form's real prepare_edit and no capacity reads."""
        plan = {"blocked": False, "warnings": [], "requires_confirmation": True}
        prepare = lambda b: ({"deployment": WEDIT.prepare_edit(dict(b))["deployment"]}, {}, plan)
        with mock.patch.object(WORKLOADS, "edit_capacity_plan", side_effect=prepare):
            return WORKLOADS.preview_edit(Request(body))


def lost(review):
    """(container, variable) of each value the review says is lost."""
    return [(entry["container"], entry["name"]) for entry in review["secret_removed"] if not entry["value_kept"]]


class TheReviewNamesWhatASaveDeletes(Fixture):
    def test_a_removed_variable_is_named(self):
        review = self.review(self.body({"TUNNEL_TOKEN": "first", "TZ": "UTC"}))
        self.assertEqual([{"key": "cloudflared.OLD_TOKEN", "container": "cloudflared", "name": "OLD_TOKEN",
                           "value_kept": False}], review["secret_removed"])

    def test_a_removed_container_names_its_stored_values(self):
        self.assertEqual([("worker", "DB_PASSWORD")], lost(self.review(self.body(worker=False))))

    def test_a_save_that_keeps_every_value_names_none(self):
        body = self.body()
        body["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        self.assertEqual([], self.review(body)["secret_removed"])

    def test_a_renamed_variable_is_listed_with_its_value_kept(self):
        review = self.review(self.body({"TUNNEL_LOGIN": "first", "OLD_TOKEN": "old", "TZ": "UTC"}))
        self.assertEqual(["cloudflared.TUNNEL_TOKEN"], [entry["key"] for entry in review["secret_removed"]])
        self.assertEqual([], lost(review))

    def test_a_secret_that_cannot_be_read_is_unknown_never_none(self):
        body = self.body({"TUNNEL_TOKEN": "first", "TZ": "UTC"})
        self.kube.fail(SECRET, 500, method="GET", times=10)
        self.assertIsNone(self.review(body)["secret_removed"])

    def test_no_secret_at_all_deletes_nothing(self):
        body = self.body({"TUNNEL_TOKEN": "first", "TZ": "UTC"})
        self.kube.discard(SECRET)
        self.assertEqual([], self.review(body)["secret_removed"])


class TheReviewListsExactlyWhatTheSaveDeletes(Fixture):
    """One calculation (ENVSEC.unused) decides both: for each app shape the
    review's keys are the keys the save then removes."""

    def shapes(self):
        binary = base64.b64encode(bytes([0xff, 0xfe, 0x81])).decode()

        def init_container(dep, secret):
            dep["spec"]["template"]["spec"]["initContainers"] = [
                {"name": "migrate", "image": "example/migrate:1", "env": [ref("MIGRATE_TOKEN", "migrate.MIGRATE_TOKEN")]}]
            secret["data"]["migrate.MIGRATE_TOKEN"] = b64("migrate")

        def env_from(dep, secret):
            dep["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": "tunnel-env"}}]
            secret["data"]["EXTRA_TOKEN"] = b64("extra")

        def leftover(dep, secret):
            secret["data"]["cloudflared.LEFTOVER_TOKEN"] = b64("left")

        def not_text(dep, secret):
            secret["data"]["cloudflared.OLD_TOKEN"] = binary
        drop_old = {"TUNNEL_TOKEN": "first", "TZ": "UTC"}
        return [
            ("a removed variable", None, drop_old, True, ["cloudflared.OLD_TOKEN"]),
            ("a removed container", None, None, False, ["worker.DB_PASSWORD"]),
            ("several containers", None, drop_old, False, ["cloudflared.OLD_TOKEN", "worker.DB_PASSWORD"]),
            ("a renamed variable", None, {"TUNNEL_LOGIN": "first", "OLD_TOKEN": "old", "TZ": "UTC"}, True,
             ["cloudflared.TUNNEL_TOKEN"]),
            ("an init container's value", init_container, drop_old, True, ["cloudflared.OLD_TOKEN"]),
            ("a Secret taken whole", env_from, drop_old, True, []),
            ("a value a failed prune left", leftover, None, True, ["cloudflared.LEFTOVER_TOKEN"]),
            ("a removed value that is not text", not_text, drop_old, True, ["cloudflared.OLD_TOKEN"]),
        ]

    def test_each_shape(self):
        for label, shape, env, worker, expected in self.shapes():
            with self.subTest(label):
                dep, secret = objects()
                if shape:
                    shape(dep, secret)
                with Kube(dep, secret) as kube:
                    self.open(kube)
                    body = self.body(env, worker)
                    listed = sorted({entry["key"] for entry in self.review(copy.deepcopy(body))["secret_removed"]})
                    # Every key the Secret holds confirmed: the save deletes what unused() says.
                    answer = CONFIRMED.save(copy.deepcopy(body), secret["data"])
                    self.assertEqual(expected, listed)
                    self.assertEqual(listed, answer.get("secret_removed", []))
                    self.assertEqual(expected, sorted(set(secret["data"]) - set(kube.object(SECRET)["data"])))


class TheSaveAnswerListsWhatItDeleted(Fixture):
    def test_the_answer_lists_the_removed_keys(self):
        answer = CONFIRMED.save(self.body({"TUNNEL_TOKEN": "first", "TZ": "UTC"}, worker=False),
                                ["cloudflared.OLD_TOKEN", "worker.DB_PASSWORD"])
        self.assertEqual(["cloudflared.OLD_TOKEN", "worker.DB_PASSWORD"], answer["secret_removed"])
        self.assertEqual({"cloudflared.TUNNEL_TOKEN": b64("first")}, self.kube.object(SECRET)["data"])

    def test_a_save_that_deletes_nothing_answers_as_before(self):
        body = self.body()
        body["containers"][0]["image"] = "cloudflare/cloudflared:2026.2.0"
        self.assertEqual({"ok": True, "name": "tunnel"}, WEDIT.edit_workload(body))


if __name__ == "__main__":
    unittest.main()
