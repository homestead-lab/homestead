"""#931: an outage webhook's address, tokens and all, is kept in a Secret per
app or VM, not on the Deployment or VM, where anyone who may read those - a
viewer on the Resources page too - could read it. The annotation keeps only
the scheme, host and port, and the Secret's name. Annotations saved before
are moved over by the leader, and on the next save, without losing them."""
import base64
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_names as NAMES
import homestead_outage as O
import homestead_outage_secret as S
from fakes.kube import Kube
from test_outage_formats import FakeCase, SECRET

ITEM = {"kind": "app", "ns": "lab", "name": "frigate"}
VM = {"kind": "vm", "ns": "lab", "name": "frigate"}
REAL = f"https://gotify.example.com/message?token={SECRET}"
SECRETS = "/api/v1/namespaces/lab/secrets"
KEY = NAMES.key(O.KEY)


class Cluster:
    """Deployments, VMs and Secrets on the shared fake API server (tests/fakes/kube.py), which refuses a create
    of one that exists and a patch or delete naming an old resourceVersion."""

    def __init__(self):
        self.kube = Kube()

    @property
    def writes(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def kget(self, path):
        return self.kube("GET", path)

    def ksend(self, method, path, body=None, ctype="application/json", **options):
        return self.kube(method, path, body, ctype)

    def object(self, path):
        return self.kube.object(path)

    def change(self, path, mutate):
        obj = self.kube.object(path)
        mutate(obj)
        self.kube.add(obj)

    def discard(self, path):
        """Gone as if something outside the app deleted it: the delete is not counted among the app's writes."""
        self.kube("DELETE", path)
        self.kube.writes.pop()

    def add(self, item, actions=None):
        annotations = {KEY: json.dumps(actions)} if actions else {}
        kind = ({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine"} if item["kind"] == "vm"
                else {"apiVersion": "apps/v1", "kind": "Deployment"})
        return self.kube.add({**kind, "metadata": {"name": item["name"], "namespace": item["ns"],
                                                   "annotations": annotations}})

    def annotation(self, item=ITEM):
        raw = self.object(O._path_of(item["kind"], item["ns"], item["name"]))["metadata"]["annotations"].get(KEY)
        return json.loads(raw) if raw else None

    def secret(self, name):
        return self.object(f"{SECRETS}/{name}")

    def kept(self, name):
        found = self.secret(name)
        return base64.b64decode(found["data"]["webhook"]).decode() if found else None

    def foreign(self, name, url=REAL, labels=None):
        self.kube.add({"apiVersion": "v1", "kind": "Secret",
                       "metadata": {"name": name, "namespace": "lab", "labels": labels or {}},
                       "data": {"webhook": base64.b64encode(url.encode()).decode()}})


class Base(FakeCase):
    def setUp(self):
        super().setUp()
        self.cluster = Cluster()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        import server  # noqa: F401  (binds this module to the real client once; put back after each test)
        saved = dict(O._bound)
        self.addCleanup(lambda: (O._bound.clear(), O._bound.update(saved)))
        self.bind()
        O._tests.clear()
        O._unmovable.clear()
        self.addCleanup(O._unmovable.clear)
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def bind(self):
        O.bind(self.dir.name, kget=self.cluster.kget, ksend=self.cluster.ksend,
               dns_name=lambda value, what: value, guard=lambda ns, name: None)

    def server(self):
        import server
        return server

    def save(self, actions, item=ITEM, role="admin"):
        return O.set_actions({**item, "actions": actions}, role)

    def shown(self, item=ITEM):
        path = O._path_of(item["kind"], item["ns"], item["name"])
        return O.read(self.cluster.object(path)["metadata"]["annotations"], NAMES)

    def send_later(self, item=ITEM):
        shown = self.shown(item)
        done = threading.Event()
        with mock.patch.object(O.LEADER_STATE, "put", side_effect=lambda *a: done.set()):
            O._send_later(shown["webhook"], O.payload(dict(item), "down"), shown)
            self.assertTrue(done.wait(10))
        return O._last_webhook[f"{item['kind']}:lab/{item['name']}"]


class SaveTests(Base):
    def test_a_token_goes_into_the_apps_secret_and_never_onto_the_deployment(self):
        self.cluster.add(ITEM)
        answer = self.save({"webhook": REAL, "format": "gotify", "restart": {"after_min": 5, "max": 2}})
        deployment = json.dumps(self.cluster.object(O._path_of("app", "lab", "frigate")))
        self.assertNotIn(SECRET, deployment)
        self.assertNotIn(SECRET, json.dumps(answer))
        self.assertEqual({"restart": {"after_min": 5, "max": 2}, "webhook": "https://gotify.example.com/(hidden)",
                          "format": "gotify", "secret": "homestead-outage-app-frigate"}, self.cluster.annotation())
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        labels = self.cluster.secret("homestead-outage-app-frigate")["metadata"]["labels"]
        self.assertEqual("app", labels[S.LABEL])
        self.assertEqual("homestead", labels["app.kubernetes.io/managed-by"])

    def test_the_secret_is_written_before_the_annotation_names_it(self):
        self.cluster.add(ITEM)
        self.save({"webhook": REAL, "format": "gotify"})
        order = [(method, path.startswith(SECRETS)) for method, path, _ in self.cluster.writes]
        self.assertEqual([("POST", True), ("PATCH", False)], order)

    def test_a_vm_has_its_own_secret_beside_an_app_of_the_same_name(self):
        self.cluster.add(ITEM)
        self.cluster.add(VM, {"restart": None})
        self.cluster.change(O._path_of("vm", "lab", "frigate"),
                            lambda vm: vm["metadata"]["annotations"].update({NAMES.key("uptime"): "tcp:22"}))
        self.save({"webhook": REAL, "format": "gotify"})
        self.save({"webhook": REAL.replace("gotify.", "vm."), "format": "gotify"}, VM)
        self.assertEqual("homestead-outage-vm-frigate", self.cluster.annotation(VM)["secret"])
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertEqual(REAL.replace("gotify.", "vm."), self.cluster.kept("homestead-outage-vm-frigate"))

    def test_an_address_with_nothing_after_its_host_needs_no_secret(self):
        self.cluster.add(ITEM)
        self.save({"webhook": "https://hooks.example.com", "format": "generic"})
        self.assertEqual({"webhook": "https://hooks.example.com", "format": "generic"}, self.cluster.annotation())
        self.assertIsNone(self.cluster.secret("homestead-outage-app-frigate"))

    def test_saving_the_hidden_address_keeps_the_token_in_the_secret(self):
        self.cluster.add(ITEM)
        self.save({"webhook": REAL, "format": "gotify"})
        shown = self.shown()
        self.assertNotIn("token", json.dumps(shown))
        self.save({**shown, "format": "ntfy"}, role="operator")
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertEqual("ntfy", self.cluster.annotation()["format"])

    def test_a_new_token_replaces_the_old_one(self):
        self.cluster.add(ITEM)
        self.save({"webhook": REAL, "format": "gotify"})
        self.save({"webhook": REAL.replace(SECRET, "Other123Token"), "format": "gotify"})
        self.assertEqual(REAL.replace(SECRET, "Other123Token"), self.cluster.kept("homestead-outage-app-frigate"))

    def test_clearing_the_webhook_deletes_its_secret(self):
        for after in (None, {"restart": {"after_min": 5, "max": 1}}, {"webhook": "https://hooks.example.com"}):
            with self.subTest(after=after):
                self.cluster.add(ITEM)
                self.save({"webhook": REAL, "format": "gotify"})
                self.save(after)
                self.assertIsNone(self.cluster.secret("homestead-outage-app-frigate"))
                self.assertNotIn("secret", self.cluster.annotation() or {})

    def test_a_secret_of_that_name_homestead_did_not_make_is_never_overwritten(self):
        self.cluster.add(ITEM)
        self.cluster.foreign("homestead-outage-app-frigate", "https://keep.example.com/mine")
        with self.assertRaises(ValueError) as caught:
            self.save({"webhook": REAL, "format": "gotify"})
        self.assertIn("not the one Homestead keeps", str(caught.exception))
        self.assertEqual("https://keep.example.com/mine", self.cluster.kept("homestead-outage-app-frigate"))
        self.assertIsNone(self.cluster.annotation())

    def test_an_app_and_a_vm_never_share_a_secret_name(self):
        self.assertNotEqual(S.secret_name("app", "vm-x"), S.secret_name("vm", "x"))
        self.assertNotEqual(S.secret_name("app", "x"), S.secret_name("vm", "x"))

    def test_a_save_whose_annotation_fails_leaves_the_secret_as_it_was(self):
        self.cluster.add(ITEM)
        refused = urllib.error.HTTPError("x", 422, "refused", {}, None)
        real_send = self.cluster.ksend

        def send(method, path, body=None, **k):
            if method == "PATCH":
                raise refused
            return real_send(method, path, body, **k)
        O._bound["ksend"] = send
        with self.assertRaises(urllib.error.HTTPError):
            self.save({"webhook": REAL, "format": "gotify"})
        self.assertIsNone(self.cluster.secret("homestead-outage-app-frigate"), "a Secret it made goes")
        self.bind()
        self.save({"webhook": REAL, "format": "gotify"})
        O._bound["ksend"] = send
        with self.assertRaises(urllib.error.HTTPError):
            self.save({"webhook": REAL.replace(SECRET, "Other123Token"), "format": "gotify"})
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"), "one it replaced gets its address back")

    def test_another_apps_secret_named_on_the_annotation_is_never_deleted(self):
        other = {**ITEM, "name": "other"}
        self.cluster.add(other)
        self.save({"webhook": REAL, "format": "gotify"}, other)
        # A whole address typed in the YAML, with another app's Secret named beside it.
        self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify", "secret": "homestead-outage-app-other"})
        self.save(None)
        O.forget("app", "lab", self.cluster.object(O._path_of("app", "lab", "frigate")))
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-other"))

    def test_a_save_after_a_rename_moves_the_address_to_the_new_name(self):
        # A rename clones the annotation: the old Secret is still read, and the
        # next save keeps the address under the app's own name.
        self.cluster.add({**ITEM, "name": "old"})
        self.save({"webhook": REAL, "format": "gotify"}, {**ITEM, "name": "old"})
        self.cluster.add(ITEM, self.cluster.annotation({**ITEM, "name": "old"}))
        self.cluster.discard(O._path_of("app", "lab", "old"))
        self.assertEqual(REAL, O._stored_actions("app", "lab", "frigate")["webhook"])
        self.save({**self.shown(), "format": "gotify"})
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertIsNone(self.cluster.secret("homestead-outage-app-old"))


class UseTests(Base):
    def test_the_leader_calls_the_address_kept_in_the_secret(self):
        fake = self.fake(status=500)
        self.cluster.add(ITEM)
        self.save({"webhook": self.named(fake, f"/message?token={SECRET}"), "format": "ntfy"})
        record = self.send_later()
        self.assertIn(SECRET, fake.got[0]["path"])
        self.assertNotIn(SECRET, json.dumps(record))

    def test_a_test_of_the_hidden_address_uses_the_secret(self):
        fake = self.fake()
        self.cluster.add(ITEM)
        self.save({"webhook": self.named(fake, f"/message?token={SECRET}"), "format": "gotify"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            answer = O.test_webhook({**ITEM, "webhook": self.shown()["webhook"], "format": "gotify"})
        self.assertTrue(answer["ok"])
        self.assertIn(SECRET, fake.got[0]["path"])

    def test_a_secret_homestead_does_not_keep_for_it_is_never_read(self):
        fake = self.fake()
        hidden = f"http://hooks.example.com:{fake.port}/(hidden)"
        for labels in ({}, {S.LABEL: "vm"}, {"homestead.io/env-for": "frigate"}):
            with self.subTest(labels=labels):
                self.cluster.foreign("someone-else", self.named(fake, f"/x?token={SECRET}"), labels)
                self.cluster.add(ITEM, {"webhook": hidden, "format": "generic", "secret": "someone-else"})
                record = self.send_later()
                self.assertFalse(record["ok"])
                self.assertIn("save the webhook again", record["error"])
                self.assertEqual([], fake.got)
                with self.assertRaises(ValueError):
                    O.test_webhook({**ITEM, "webhook": hidden, "format": "generic"})

    def test_a_secret_for_another_host_is_never_used(self):
        fake = self.fake()
        self.cluster.add(ITEM)
        self.save({"webhook": self.named(fake, f"/x?token={SECRET}"), "format": "generic"})
        annotation = self.cluster.annotation()
        annotation["webhook"] = "http://evil.example.com/(hidden)"
        self.cluster.add(ITEM, annotation)
        self.assertNotIn("webhook", O._stored_actions("app", "lab", "frigate"))

    def test_a_missing_secret_is_said_and_never_sends_the_mask(self):
        fake = self.fake()
        self.cluster.add(ITEM)
        self.save({"webhook": self.named(fake, f"/x?token={SECRET}"), "format": "generic"})
        self.cluster.discard(f"{SECRETS}/homestead-outage-app-frigate")
        record = self.send_later()
        self.assertFalse(record["ok"])
        self.assertEqual([], fake.got)


class MoveTests(Base):
    """Annotations saved before #931 are moved into a Secret by the leader."""

    def found(self, *items):
        return O.items([{"ns": i["ns"], "name": i["name"], "outage_actions": self.shown(i)} for i in items if i["kind"] == "app"],
                       [{"ns": i["ns"], "name": i["name"], "outage_actions": self.shown(i)} for i in items if i["kind"] == "vm"])

    def test_an_old_annotation_is_moved_and_keeps_everything_else(self):
        old = {"restart": {"after_min": 5, "max": 2}, "webhook": REAL}       # before #741: no format, no lan
        self.cluster.add(ITEM, old)
        self.cluster.add(VM, {"webhook": REAL.replace("gotify.", "vm."), "format": "gotify", "lan": True})
        moved = O.migrate(self.found(ITEM, VM))
        self.assertEqual(["app:lab/frigate", "vm:lab/frigate"], sorted(moved))
        self.assertEqual({"restart": {"after_min": 5, "max": 2}, "webhook": "https://gotify.example.com/(hidden)",
                          "secret": "homestead-outage-app-frigate"}, self.cluster.annotation())
        self.assertEqual({"webhook": "https://vm.example.com/(hidden)", "format": "gotify", "lan": True,
                          "secret": "homestead-outage-vm-frigate"}, self.cluster.annotation(VM))
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertEqual(REAL, O._stored_actions("app", "lab", "frigate")["webhook"], "nothing lost")
        self.assertNotIn(SECRET, json.dumps(self.cluster.object(O._path_of("app", "lab", "frigate"))))

    def test_once_moved_nothing_is_read_or_written_again(self):
        self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify"})
        O.migrate(self.found(ITEM))
        self.cluster.kube.writes.clear()
        with mock.patch.object(self.cluster, "kget", side_effect=AssertionError("read again")):
            O._bound["kget"] = self.cluster.kget
            self.assertEqual([], O.migrate(self.found(ITEM)))
        self.assertEqual([], self.cluster.writes)

    def test_an_address_with_nothing_to_hide_stays(self):
        self.cluster.add(ITEM, {"webhook": "https://hooks.example.com", "format": "generic"})
        self.assertEqual([], O.migrate(self.found(ITEM)))
        self.assertEqual([], self.cluster.writes)

    def test_the_move_names_the_resource_version_it_read(self):
        self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify"})
        O.migrate(self.found(ITEM))
        patch = [body for method, path, body in self.cluster.writes if method == "PATCH"][0]
        self.assertTrue(patch["metadata"]["resourceVersion"])

    def meanwhile(self, when, address):
        """A save of address (None: the webhook cleared), made the first time the leader reads path `when`."""
        original, done = O._bound["kget"], []

        def kget(path):
            try:
                got, error = original(path), None     # read as it was before the save
            except urllib.error.HTTPError as missing_now:
                got, error = None, missing_now
            if path.endswith(when) and not done:
                done.append(path)
                O._bound["kget"] = original
                self.save({"webhook": address, "format": "gotify"} if address else None)
                O._bound["kget"] = kget
            if error:
                raise error
            return got
        O._bound["kget"] = kget

    def test_a_clear_meanwhile_leaves_no_token_behind(self):
        # The webhook is cleared after the leader read the app: its move made a
        # Secret, the annotation it then writes is refused, and the Secret goes.
        for when in ("/deployments/frigate", "/secrets/homestead-outage-app-frigate"):
            with self.subTest(when=when):
                self.cluster = Cluster()
                self.bind()
                self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify"})
                found = self.found(ITEM)
                self.meanwhile(when, None)
                self.assertEqual([], O.migrate(found))
                self.bind()
                self.assertIsNone(self.cluster.secret("homestead-outage-app-frigate"))
                self.assertIsNone(self.cluster.annotation())

    def test_a_rename_is_moved_to_the_new_name_and_never_shared(self):
        old = {**ITEM, "name": "old"}
        self.cluster.add(old)
        self.save({"webhook": REAL, "format": "gotify"}, old)
        # The rename: a clone with the same annotation, the old app deleted as it is.
        self.cluster.add(ITEM, self.cluster.annotation(old))
        self.cluster.discard(O._path_of("app", "lab", "old"))
        self.assertEqual(REAL, O._stored_actions("app", "lab", "frigate")["webhook"], "still called after the rename")
        self.assertEqual(["app:lab/frigate"], O.migrate(self.found(ITEM)))
        self.assertEqual("homestead-outage-app-frigate", self.cluster.annotation()["secret"])
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertIsNone(self.cluster.secret("homestead-outage-app-old"), "the old name's Secret went")
        # A new app under the old name gets a Secret of its own.
        self.cluster.add(old)
        self.save({"webhook": REAL.replace(SECRET, "Other123Token"), "format": "gotify"}, old)
        self.assertEqual(REAL, O._stored_actions("app", "lab", "frigate")["webhook"])

    def test_a_rename_whose_old_name_is_in_use_again_keeps_that_secret(self):
        old = {**ITEM, "name": "old"}
        self.cluster.add(old)
        self.save({"webhook": REAL, "format": "gotify"}, old)
        self.cluster.add(ITEM, self.cluster.annotation(old))     # the old app is still there (or there again)
        self.assertEqual(["app:lab/frigate"], O.migrate(self.found(ITEM)))
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-old"), "old's own Secret is left to it")

    def test_a_save_meanwhile_wins(self):
        # A save lands between each read the leader makes and its writes: the
        # move never puts the old address back, in the Secret or the annotation.
        for when in ("/deployments/frigate", "/secrets/homestead-outage-app-frigate"):
            for before in (None, REAL.replace(SECRET, "Older123Token")):
                with self.subTest(when=when, before=before):
                    self.cluster = Cluster()
                    self.bind()
                    self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify"})
                    if before:      # a Secret from an earlier save, then a whole address typed in the YAML
                        self.cluster.foreign("homestead-outage-app-frigate", before, {S.LABEL: "app"})
                    found = self.found(ITEM)
                    newer = REAL.replace(SECRET, "Newer123Token")
                    self.meanwhile(when, newer)
                    O.migrate(found)
                    self.bind()
                    self.assertEqual(newer, self.cluster.kept("homestead-outage-app-frigate"))
                    self.assertEqual(newer, O._stored_actions("app", "lab", "frigate")["webhook"])

    def test_a_failed_move_is_tried_again_and_says_no_token(self):
        self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify"})
        out = io.StringIO()
        with mock.patch.object(self.cluster, "ksend", side_effect=urllib.error.HTTPError("x", 503, f"down {SECRET}", {}, None)), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.write(" ".join(map(str, a)))):
            O._bound["ksend"] = self.cluster.ksend
            self.assertEqual([], O.migrate(self.found(ITEM)))
        self.assertNotIn(SECRET, out.getvalue())
        O._bound["ksend"] = Cluster.ksend.__get__(self.cluster)
        self.assertEqual(["app:lab/frigate"], O.migrate(self.found(ITEM)))

    def test_a_secret_homestead_did_not_make_blocks_the_move_and_is_kept(self):
        self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify"})
        self.cluster.foreign("homestead-outage-app-frigate", "https://keep.example.com/mine")
        self.assertEqual([], O.migrate(self.found(ITEM)))
        self.assertEqual("https://keep.example.com/mine", self.cluster.kept("homestead-outage-app-frigate"))
        self.assertEqual(REAL, self.cluster.annotation()["webhook"], "left as it was: it still works")

    def tick(self, **migrate):
        server = self.server()
        self.cluster.add(ITEM, {"webhook": REAL, "format": "gotify"})
        listed = [{"ns": "lab", "name": "frigate", "outage_actions": self.shown()}]
        with mock.patch.object(server.CACHE, "cached", side_effect=lambda key, ttl, fn: listed if key == "wl" else []), \
                mock.patch.object(server.OPS, "list_operations", return_value=[]), \
                mock.patch.object(server.UPTIME, "report", return_value={}), \
                mock.patch.object(server.OVERVIEW_ROUTES.OUTAGE_CARRY, "carry_lan") as carry, \
                mock.patch.object(O, "migrate", **migrate) as move:
            server.OVERVIEW_ROUTES.outage_tick()
        return move, carry

    def test_the_leaders_round_moves_them(self):
        move, _ = self.tick(wraps=O.migrate)
        move.assert_called_once()
        self.assertEqual(REAL, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertEqual("homestead-outage-app-frigate", self.cluster.annotation()["secret"])

    def test_a_move_that_raises_never_stops_the_round(self):
        _, carry = self.tick(side_effect=RuntimeError("bug"))
        carry.assert_called_once()


class ResourcesTests(Base):
    """The Resources page: the annotation has no token to hide now, and its
    YAML cannot point the annotation at another Secret."""

    def setUp(self):
        super().setUp()
        import homestead_resources as R
        self.R = R
        kind = {"kind": "Deployment", "group": "apps", "version": "v1", "resource": "deployments", "namespaced": True}
        patch = mock.patch.object(R, "_kind", return_value=kind)
        patch.start()
        self.addCleanup(patch.stop)
        old = (R.kget, R.ksend)
        self.addCleanup(lambda: setattr(R, "kget", old[0]) or setattr(R, "ksend", old[1]))
        R.kget, R.ksend = self.cluster.kget, self.cluster.ksend
        self.cluster.add(ITEM)
        self.save({"webhook": REAL, "format": "gotify"})

    def yaml(self, reveal=False):
        return self.R.get_object("apps", "v1", "deployments", "lab", "frigate", reveal=reveal)["yaml"]

    def test_even_the_reveal_shows_no_token_on_the_deployment(self):
        self.assertNotIn(SECRET, self.yaml(reveal=True))

    def test_saving_the_yaml_keeps_the_reference(self):
        self.R.save_object("apps", "v1", "deployments", "lab", "frigate", self.yaml())
        self.assertEqual("homestead-outage-app-frigate", self.cluster.annotation()["secret"])
        self.assertEqual(REAL, O._stored_actions("app", "lab", "frigate")["webhook"])

    def test_the_yaml_cannot_point_it_at_another_secret(self):
        self.cluster.foreign("homestead-outage-app-other", "https://gotify.example.com/stolen", {S.LABEL: "app"})
        self.R.save_object("apps", "v1", "deployments", "lab", "frigate",
                           self.yaml().replace("homestead-outage-app-frigate", "homestead-outage-app-other"))
        self.assertEqual("homestead-outage-app-frigate", self.cluster.annotation()["secret"])

    def test_a_whole_address_typed_in_the_yaml_is_used_and_moved(self):
        typed = REAL.replace(SECRET, "Typed123Token")
        text = self.yaml().replace("https://gotify.example.com/(hidden)", typed)
        self.R.save_object("apps", "v1", "deployments", "lab", "frigate", text)
        self.assertEqual(typed, O._stored_actions("app", "lab", "frigate")["webhook"])
        self.assertEqual(["app:lab/frigate"], O.migrate(O.items([{"ns": "lab", "name": "frigate",
                                                                  "outage_actions": self.shown()}], [])))
        self.assertEqual(typed, self.cluster.kept("homestead-outage-app-frigate"))
        self.assertNotIn("Typed123Token", json.dumps(self.cluster.object(O._path_of("app", "lab", "frigate"))))


def patched(stack, modules, name, **kw):
    """Patch name on each module that has it: where a helper is defined, and
    any module that still re-exports it, so the test holds either way."""
    for module in modules:
        if module is not None and hasattr(module, name):
            stack.enter_context(mock.patch.object(module, name, **kw))


class DeleteTests(Base):
    def test_the_secret_goes_with_its_app(self):
        import homestead_deploy_server as D     # its kget and ksend are bound by server.py, patched below
        import homestead_network_services as SVC
        self.cluster.add(ITEM)
        self.save({"webhook": REAL, "format": "gotify"})
        with contextlib.ExitStack() as stack:
            for name in ("require_workload_target", "guard_self", "guard_managed_smb"):
                patched(stack, [D], name)
            patched(stack, [D], "kget", new=self.cluster.kget)
            patched(stack, [D], "ksend", new=self.cluster.ksend)
            patched(stack, [D.LAN], "remove_nad")
            patched(stack, [SVC, getattr(D, "NETWORK", None), getattr(D, "SVC", None)], "workload_service_names", return_value=[])
            D.delete_workload("lab", "frigate")
        self.assertIsNone(self.cluster.secret("homestead-outage-app-frigate"))

    def test_only_a_secret_homestead_keeps_for_it_is_deleted(self):
        self.cluster.foreign("not-ours", labels={})
        S.remove("app", "lab", "not-ours", self.cluster.kget, self.cluster.ksend, "app/frigate")
        self.assertIsNotNone(self.cluster.secret("not-ours"))
        O.forget("app", "lab", {"metadata": {"name": "frigate", "annotations": {KEY: json.dumps(
            {"webhook": "https://h.example.com/(hidden)", "secret": "not-ours"})}}})
        self.assertIsNotNone(self.cluster.secret("not-ours"))

    def test_the_secret_goes_with_its_vm(self):
        import homestead_vms as V
        self.cluster.add(VM)
        self.save({"webhook": REAL, "format": "gotify"}, VM)
        vm = self.cluster.kget(O._path_of("vm", "lab", "frigate"))
        import homestead_vm_list as VM_LIST
        with contextlib.ExitStack() as stack:
            patched(stack, [V], "ksend", new=self.cluster.ksend)
            patched(stack, [V], "_get", return_value=vm)
            patched(stack, [V, VM_LIST], "_row", return_value={"disks": []})
            patched(stack, [V], "_datavolumes", return_value={})
            V.delete("lab", "frigate")
        self.assertIsNone(self.cluster.secret("homestead-outage-vm-frigate"))
        self.assertIsNone(self.cluster.object(O._path_of("vm", "lab", "frigate")))


if __name__ == "__main__":
    unittest.main()
