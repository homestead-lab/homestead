"""The linked-clusters list and key are changed on what is stored when they are
saved, so a change another copy of Homestead made meanwhile stays, and a list
that cannot be read is never replaced by a new one (#621)."""
import base64
import importlib.util
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

SERVER = Path(os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
from fakes.kube import Kube

MAP = "/api/v1/namespaces/lab/configmaps/homestead-fleet"
SECRET = "/api/v1/namespaces/lab/secrets/homestead-fleet-key"
KEY = b"k" * 32
LOFT = {"id": "aaa", "handle": "loft", "name": "Loft", "url": "https://192.0.2.1:8088", "added": "2026-10-01 10:00"}
SHED = {"id": "bbb", "handle": "shed", "name": "Shed", "url": "https://192.0.2.2:8088", "added": "2026-10-01 10:05"}


def stored_list(members=(LOFT, SHED), revision=3, text=None):
    state = {"self": "aaa", "revision": revision, "updated": 0, "members": list(members)}
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "homestead-fleet", "namespace": "lab"},
            "data": {"fleet.json": text if text is not None else json.dumps(state, indent=2)}}


def stored_key(key=KEY):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "homestead-fleet-key", "namespace": "lab"},
            "data": {"key": base64.b64encode(key).decode()}}


def replica(name, site, read=None, send=None):
    """One copy of Homestead: its own module, so its own lock and cache."""
    spec = importlib.util.spec_from_file_location(f"fleet_writes_{name}", SERVER / "homestead_fleet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.bind(read or K.kget, send or K.send, "lab", "2.8.400", site=lambda: site,
                address=lambda: "https://192.0.2.1:8088")
    module.real_broadcast = module._broadcast
    module._broadcast = lambda *args, **kwargs: []    # the other members are not part of this test
    module.check = lambda row: {"reachable": False, "revision": 0, "error": "not asked"}
    return module


class FleetWriteTests(unittest.TestCase):
    def state(self, kube):
        return json.loads(kube.object(MAP)["data"]["fleet.json"])

    def key(self, kube):
        return base64.b64decode(kube.object(SECRET)["data"]["key"])

    def mine(self, kube):
        state = self.state(kube)
        return next(m for m in state["members"] if m["id"] == state["self"])

    def test_a_rename_made_meanwhile_on_another_copy_stays_when_the_address_is_saved(self):
        with Kube(stored_list(), stored_key()) as kube:
            other = replica("b", "Loft upstairs")
            raced = []

            def send(method, path, body=None, **options):
                if method == "PUT" and path == MAP and not raced:
                    raced.append(1)
                    other.refresh_name()            # another copy saves first
                return K.send(method, path, body, **options)
            one = replica("a", "Loft", send=send)
            one.set_address("https://192.0.2.10:8088")
            mine, state = self.mine(kube), self.state(kube)
        self.assertEqual("https://192.0.2.10:8088", mine["url"])
        self.assertEqual("Loft upstairs", mine["name"], "the rename made meanwhile was lost")
        self.assertEqual(5, state["revision"], "each change is a revision of its own")
        self.assertEqual(["aaa", "bbb"], [m["id"] for m in state["members"]])

    def test_the_key_stored_is_the_one_of_the_list_stored(self):
        # Two copies take two newer lists from the linked clusters at once, each
        # with a new key. The list saved last must keep its own key, whichever
        # copy's key write reaches the API server last.
        newer = {"revision": 5, "updated": 1, "members": [LOFT, SHED], "key": base64.b64encode(b"n" * 32).decode()}
        newest = {"revision": 6, "updated": 2, "members": [LOFT, SHED], "key": base64.b64encode(b"z" * 32).decode()}
        with Kube(stored_list(), stored_key()) as kube:
            other = replica("b", "Loft")
            raced = []

            def send(method, path, body=None, **options):
                if path.startswith(SECRET.rsplit("/", 1)[0]) and not raced:
                    raced.append(1)
                    other.adopt(newest)             # saved list and key in between
                return K.send(method, path, body, **options)
            one = replica("a", "Loft", send=send)
            one.adopt(newer)
            state, key = self.state(kube), self.key(kube)
            signs_with = one._load()[1]
        self.assertEqual(6, state["revision"])
        self.assertEqual(b"z" * 32, key, "the key of an older list was saved over the newer list's")
        self.assertEqual(b"z" * 32, signs_with, "this copy signs with a key that was never stored")

    def test_a_key_an_older_copy_saved_meanwhile_is_not_saved_over(self):
        # An older Homestead, mid-upgrade, saves its list (with no keyId) and its key whole.
        newer = {"revision": 5, "updated": 1, "members": [LOFT, SHED], "key": base64.b64encode(b"n" * 32).decode()}
        with Kube(stored_list(), stored_key()) as kube:
            raced = []

            def send(method, path, body=None, **options):
                if path.startswith(SECRET.rsplit("/", 1)[0]) and not raced:
                    raced.append(1)
                    K.send("PUT", MAP, dict(stored_list(revision=6), metadata=kube.object(MAP)["metadata"]))
                    K.send("PUT", SECRET, dict(stored_key(b"o" * 32), metadata=kube.object(SECRET)["metadata"]))
                return K.send(method, path, body, **options)
            one = replica("a", "Loft", send=send)
            one.adopt(newer)
            key = self.key(kube)
        self.assertEqual(b"o" * 32, key, "the older copy's key was saved over beside its own list")

    def test_a_member_linked_meanwhile_on_another_copy_stays_when_another_is_unlinked(self):
        barn = {"id": "ccc", "handle": "barn", "name": "Barn", "url": "https://192.0.2.3:8088", "added": "2026-10-02 09:00"}
        with Kube(stored_list(), stored_key()) as kube:
            other = replica("b", "Loft")
            raced = []

            def send(method, path, body=None, **options):
                if method == "PUT" and path == MAP and not raced:
                    raced.append(1)
                    other.adopt({"revision": 4, "updated": 1, "members": [LOFT, SHED, barn]})
                return K.send(method, path, body, **options)
            one = replica("a", "Loft", send=send)
            one.call = lambda *args, **kwargs: (_ for _ in ()).throw(one.Unreachable("not asked"))
            told = []
            one._broadcast = lambda state, skip=(), key=None, signing_key=None: told.append((state["revision"], [m["id"] for m in state["members"]], key)) or []
            one.remove("shed")
            state = self.state(kube)
            in_force = self.key(kube)
        self.assertEqual(["aaa", "ccc"], [m["id"] for m in state["members"]], "the member linked meanwhile was lost")
        self.assertEqual(5, state["revision"])
        self.assertEqual([(5, ["aaa", "ccc"], in_force)], told, "the members are told the list as saved, with its key, once")

    def test_a_new_member_hears_the_list_as_saved_when_another_copy_changed_it_meanwhile(self):
        barn = {"id": "ccc", "handle": "barn", "name": "Barn", "url": "https://192.0.2.3:8088", "added": "2026-10-02 09:00"}
        with Kube(stored_list(), stored_key()) as kube:
            other = replica("b", "Loft")
            one = replica("a", "Loft")

            def remote(url, method, path, body=None, token="", **kwargs):
                if path == "/api/fleet":
                    return {"self": "ddd", "members": [{"id": "ddd"}], "protocol": one.PROTOCOL}, None
                other.adopt({"revision": 4, "updated": 1, "members": [LOFT, SHED, barn]})   # meanwhile
                return {"id": "ddd", "handle": "yard", "name": "Yard", "added": "2026-10-03 08:00"}, None
            one._login = lambda *args: "token"
            one._json = remote
            told = []
            one._broadcast = lambda state, skip=(), key=None: told.append(([m["id"] for m in state["members"]], set(skip))) or []
            one.join("https://192.0.2.4:8088", "admin", "secret")
            state = self.state(kube)
        self.assertEqual(["aaa", "bbb", "ccc", "ddd"], [m["id"] for m in state["members"]])
        self.assertEqual(5, state["revision"])
        self.assertEqual([(["aaa", "bbb", "ccc", "ddd"], set())], told, "the new member was sent an older list")

    def test_an_address_typed_meanwhile_is_not_moved_onto_the_vip(self):
        with Kube(stored_list(members=(dict(LOFT, url=""), SHED)), stored_key()) as kube:
            other = replica("b", "Loft")
            raced = []

            def send(method, path, body=None, **options):
                if method == "PUT" and path == MAP and not raced:
                    raced.append(1)
                    other.set_address("https://192.0.2.50:8088")    # an admin, on another copy
                return K.send(method, path, body, **options)
            one = replica("a", "Loft", send=send)
            moved = one.follow_address()
            mine = self.mine(kube)
        self.assertEqual("https://192.0.2.50:8088", mine["url"], "the address the admin typed was moved")
        self.assertEqual("", moved)

    def record_calls(self, module, sent):
        """Every call a copy makes to a member: who, and the key it carries."""
        def call(ref, method, path, body=None, timeout=None, signing_key=None, **kwargs):
            sent.append({"from": module.__name__, "to": ref["id"], "key": (body or {}).get("key"),
                         "signed_with": signing_key, "revision": (body or {}).get("revision")})
            return {}
        module.call = call

    def test_no_unlinked_member_is_sent_the_key_in_force_when_two_copies_unlink_at_once(self):
        barn = {"id": "ccc", "handle": "barn", "name": "Barn", "url": "https://192.0.2.3:8088", "added": "2026-10-02 09:00"}
        for first, second in (("shed", "barn"), ("barn", "shed")):
            with self.subTest(first=first), Kube(stored_list(members=(LOFT, SHED, barn)), stored_key()) as kube:
                sent, raced = [], []
                other = replica("b", "Loft")

                def send(method, path, body=None, **options):
                    if method == "PUT" and path == MAP and not raced:
                        raced.append(1)
                        other.remove(second)        # another admin, on another copy
                    return K.send(method, path, body, **options)
                one = replica("a", "Loft", send=send)
                for module in (one, other):
                    module._broadcast = module.real_broadcast           # the real one, through the recorded calls
                    self.record_calls(module, sent)
                one.remove(first)
                state, in_force = self.state(kube), self.key(kube)
            self.assertEqual(["aaa"], [m["id"] for m in state["members"]])
            leaked = [c for c in sent if c["to"] in ("bbb", "ccc") and c["key"]
                      and base64.b64decode(c["key"]) == in_force]
            self.assertEqual([], leaked, "an unlinked member holds the key in force")

    def test_leaving_sends_the_rest_their_key_only_when_they_are_still_linked(self):
        barn = {"id": "ccc", "handle": "barn", "name": "Barn", "url": "https://192.0.2.3:8088", "added": "2026-10-02 09:00"}
        with Kube(stored_list(members=(LOFT, SHED, barn)), stored_key()):
            sent, raced = [], []
            other = replica("b", "Loft")
            other.call = lambda *args, **kwargs: {}

            def send(method, path, body=None, **options):
                if method == "PUT" and path == MAP and not raced:
                    raced.append(1)
                    other.remove("barn")            # another admin, on another copy
                return K.send(method, path, body, **options)
            one = replica("a", "Loft", send=send)
            one._broadcast = one.real_broadcast
            self.record_calls(one, sent)
            one.leave()
        keyed = [c for c in sent if c["key"]]
        self.assertEqual(["bbb"], [c["to"] for c in keyed], "a member unlinked meanwhile was sent the rest's new key")
        self.assertTrue(all(c["signed_with"] for c in keyed))

    def test_a_new_key_is_sent_signed_with_the_key_the_members_hold(self):
        barn = {"id": "ccc", "handle": "barn", "name": "Barn", "url": "https://192.0.2.3:8088", "added": "2026-10-02 09:00"}
        with Kube(stored_list(members=(LOFT, SHED, barn)), stored_key()) as kube:
            sent = []
            one = replica("a", "Loft")
            one._broadcast = one.real_broadcast
            self.record_calls(one, sent)
            one.remove("barn")
            in_force = self.key(kube)
        rotated = [c for c in sent if c["key"]]
        self.assertEqual(["bbb"], [c["to"] for c in rotated])
        self.assertEqual(in_force, base64.b64decode(rotated[0]["key"]))
        self.assertEqual(KEY, rotated[0]["signed_with"], "the members cannot check a call signed with a key they do not have yet")

    def test_a_list_that_cannot_be_read_is_not_replaced_by_a_new_identity(self):
        for broken in ("key unreadable", "list not JSON"):
            with self.subTest(broken=broken), \
                    Kube(stored_list(text="{broken" if broken == "list not JSON" else None), stored_key()) as kube:
                before = kube.object(MAP)["data"]["fleet.json"]

                def read(path, **options):
                    if path == SECRET and broken == "key unreadable":
                        raise urllib.error.HTTPError(path, 500, "the API server is busy", {}, None)
                    return K.kget(path, **options)
                one = replica("a", "Loft", read=read)
                with self.assertRaisesRegex(Exception, "busy|not valid JSON"):
                    one.summary()                   # refused, never replaced
                self.assertEqual(before, kube.object(MAP)["data"]["fleet.json"], "this cluster's list was replaced")
                self.assertEqual(KEY, self.key(kube), "the clusters' key was replaced")

    def test_a_broken_list_is_not_saved_over(self):
        with Kube(stored_list(text="{broken"), stored_key()) as kube:
            one = replica("a", "Loft")
            with self.assertRaises(ValueError):
                one.set_address("https://192.0.2.10:8088")
            self.assertEqual("{broken", kube.object(MAP)["data"]["fleet.json"])
            self.assertEqual(KEY, self.key(kube))

    def test_a_first_use_makes_one_identity_and_key(self):
        with Kube() as kube:
            one = replica("a", "Loft")
            state, key = one._ensure()
            again, same = replica("b", "Loft")._ensure()
            self.assertEqual((state["self"], key), (again["self"], same))
            self.assertEqual((state["self"], key), (self.state(kube)["self"], self.key(kube)))


if __name__ == "__main__":
    unittest.main()
