"""A person relayed by a linked Homestead never shares a name with a local account.

A linked Homestead signs the name of the person it relays, and this Homestead
builds who they are from it. Built as `name@handle`, a local account named
`alice@office` (email-style names are allowed) and alice relayed by the
Homestead with handle office were one identity: one set of alert
acknowledgements, setup skips and diagnostics, and one name in the change
history. Built as `fleet:<handle>:<name>`, it is a name no account can have,
since a username cannot contain ':'. An agent request run on approval is
`approving:<key>:<name>` for the same reason.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))
import tempfile

import homestead_auth as auth
import homestead_diagnostics as diagnostics
from test_fleet import Cluster, wire
from rebind import keep_bound


class RelayedNameTests(unittest.TestCase):
    def setUp(self):
        self.a = Cluster("office", "https://192.0.2.1:8088", {})
        self.b = Cluster("shed", "https://192.0.2.2:8088", {})
        wire([self.a, self.b])
        self.a.fleet.join(self.b.url, "admin", "secret")

    def relayed(self, user="", role="admin"):
        headers = self.a.fleet.sign("GET", "/api/nodes", b"", user, role)
        return self.b.fleet.verify(headers, "GET", "/api/nodes")["user"]

    def test_a_relayed_person_is_named_with_the_homestead_that_relayed_them(self):
        self.assertEqual("fleet:office:alice", self.relayed("alice", "operator"))

    def test_a_relayed_email_style_name_is_kept_whole(self):
        self.assertEqual("fleet:office:alice@example.com", self.relayed("alice@example.com"))

    def test_a_relayed_name_holding_a_colon_cannot_pass_for_another_form(self):
        # An older Homestead's account, or a crafted request, may name someone
        # with ':'; written as the look-alike '∶', the name ends where it should.
        self.assertEqual("fleet:office:a∶b", self.relayed("a:b"))
        self.assertEqual("fleet:office:approving∶k∶ada", self.relayed("approving:k:ada"))

    def test_a_linked_homesteads_own_call_is_named_for_that_homestead(self):
        self.assertEqual("fleet:office", self.relayed())

    def test_no_local_account_can_have_a_relayed_persons_name(self):
        store = {"users": {}, "signing_key": "k" * 48}
        with mock.patch.object(auth, "_load", side_effect=lambda **kw: store), \
                mock.patch.object(auth, "_save"), mock.patch.object(auth, "_hash", return_value="hash"):
            auth.create_user("alice@office", "long-fixture-password")   # email-style names stay allowed
            for name in (self.relayed("alice", "operator"), self.relayed()):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    auth.create_user(name, "long-fixture-password")
        self.assertEqual(["alice@office"], list(store["users"]))
        self.assertNotIn(self.relayed("alice", "operator"), store["users"])


    def test_a_relayed_admin_does_not_see_a_local_accounts_diagnostics(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        keep_bound(self, diagnostics)
        diagnostics.bind(folder.name, mock.Mock(), mock.Mock(), "2.8.300", "lab", "homestead-abc")
        report = diagnostics.create("alice@office")["id"]
        relayed = self.relayed("alice")
        self.assertEqual([], diagnostics.listing(relayed))
        with self.assertRaises(PermissionError):
            diagnostics.delete(report, relayed)
        self.assertEqual([report], [row["id"] for row in diagnostics.listing("alice@office")])

    def adopt_handle(self, value):
        """Shed takes a member list in which Office's handle is value."""
        rows = [dict(m, handle=value) if m["id"] == self.a.fleet.summary()["self"] else m
                for m in self.b.fleet.members()]
        self.b.fleet.adopt({"revision": 99, "updated": 1, "members": rows})
        return next(m for m in self.b.fleet.members() if m["id"] == self.a.fleet.summary()["self"])

    def test_a_handle_that_could_split_a_relayed_name_is_made_plain(self):
        self.assertEqual("a-b", self.adopt_handle("a:b")["handle"])
        self.assertEqual("fleet:a-b:alice", self.relayed("alice"))

    def test_a_member_without_a_handle_is_named_by_its_id(self):
        self.adopt_handle("")
        self.assertEqual(f"fleet:{self.a.fleet.summary()['self']}:alice", self.relayed("alice"))


class GatherTests(unittest.TestCase):
    """The all-clusters views ask each linked Homestead as the person asking."""

    def setUp(self):
        self.a = Cluster("office", "https://192.0.2.1:8088", {})
        members = [{"id": "me", "name": "Office", "handle": "office", "self": True, "reachable": True},
                   {"id": "shed", "name": "Shed", "handle": "shed", "self": False, "reachable": True}]
        mock.patch.object(self.a.fleet, "summary", return_value={"self": "me", "members": members}).start()
        self.call = mock.patch.object(self.a.fleet, "call", return_value=[]).start()
        self.addCleanup(mock.patch.stopall)
        # Kept answers live in the module: start each test without any, and leave none.
        self.a.fleet.forget_answers()
        self.addCleanup(self.a.fleet.forget_answers)

    def asked_as(self, user):
        self.a.fleet.gather("nodes", lambda: [], user, "viewer")
        return self.call.call_args.kwargs["user"]

    def test_a_local_email_style_name_is_asked_for_whole(self):
        self.assertEqual("alice@example.com", self.asked_as("alice@example.com"))

    def test_a_relayed_person_is_asked_for_by_their_own_name(self):
        self.assertEqual("alice", self.asked_as("fleet:garage:alice"))
        self.assertEqual("alice@example.com", self.asked_as("fleet:garage:alice@example.com"))

    def test_a_linked_homesteads_own_call_is_asked_for_as_homestead_itself(self):
        self.assertEqual("", self.asked_as("fleet:garage"))


if __name__ == "__main__":
    unittest.main()
