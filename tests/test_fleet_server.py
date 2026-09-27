"""How the HTTP server uses linked clusters: what it relays, and to whom."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def handler(path="/api/nodes", method="GET", headers=None, body=b""):
    h = object.__new__(server.H)
    h.path, h.command = path, method
    h.headers = {**(headers or {}), "Content-Length": str(len(body))}
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    h.client_address, h.connection = ("127.0.0.1", 1), None
    h._begin()
    h._send = mock.Mock()
    return h


class TargetTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(server.FLEET, "self_id", return_value="me")
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_browser_switched_to_another_cluster_is_relayed(self):
        h = handler(headers={"Cookie": "homestead_cluster=shed1"})
        self.assertEqual("shed1", h._fleet_target("/api/nodes"))

    def test_switched_to_this_one_stays_here(self):
        h = handler(headers={"Cookie": "homestead_cluster=me"})
        self.assertEqual("", h._fleet_target("/api/nodes"))

    def test_signing_in_and_switching_are_always_answered_here(self):
        h = handler(headers={"Cookie": "homestead_cluster=shed1"})
        for path in ("/api/auth/login", "/api/auth/logout", "/api/fleet/switch", "/api/fleet/home", "/api/push/subscribe"):
            self.assertEqual("", h._fleet_target(path), path)

    def test_a_relayed_request_is_never_relayed_again(self):
        h = handler(headers={"Cookie": "homestead_cluster=shed1", server.FLEET.H_FROM: "loft1"})
        self.assertEqual("", h._fleet_target("/api/nodes"))

    def test_a_row_in_the_all_clusters_view_names_its_own_cluster(self):
        h = handler(headers={"X-Homestead-Cluster": "garage1", "Cookie": "homestead_cluster=shed1"})
        self.assertEqual("garage1", h._fleet_target("/api/vm/start"))

    def test_a_console_names_its_cluster_in_the_address(self):
        h = handler(path="/api/console?ns=lab&pod=x&hs_cluster=garage1")
        self.assertEqual("garage1", h._fleet_target("/api/console"))


class RelayTests(unittest.TestCase):
    def test_the_person_is_signed_in_here_and_their_role_goes_with_them(self):
        h = handler(headers={"Cookie": "homestead_cluster=shed1; homestead_session=tok"})
        with mock.patch.object(server.FLEET, "self_id", return_value="me"), \
                mock.patch.object(server.AUTH, "verify_token", return_value={"user": "robin", "role": "operator"}), \
                mock.patch.object(server.FLEET, "forward") as forward:
            h.do_GET()
        args = forward.call_args.args
        self.assertEqual(("shed1", b"", "robin", "operator"), (args[1], args[2], args[3], args[4]))

    def test_nobody_signed_in_gets_no_further_than_here(self):
        h = handler(headers={"Cookie": "homestead_cluster=shed1"})
        with mock.patch.object(server.FLEET, "self_id", return_value="me"), \
                mock.patch.object(server.AUTH, "verify_token", return_value=None), \
                mock.patch.object(server.FLEET, "forward") as forward:
            h.do_GET()
        forward.assert_not_called()
        self.assertEqual(401, h._send.call_args.args[0])

    def test_the_app_itself_is_relayed_without_a_session(self):
        h = handler(path="/js/core.js?v=1", headers={"Cookie": "homestead_cluster=shed1"})
        with mock.patch.object(server.FLEET, "self_id", return_value="me"), \
                mock.patch.object(server.AUTH, "verify_token", return_value=None), \
                mock.patch.object(server.FLEET, "forward") as forward:
            h.do_GET()
        self.assertEqual("", forward.call_args.args[3])

    def test_a_cluster_that_does_not_answer_gets_a_way_back(self):
        h = handler(path="/containers", headers={"Cookie": "homestead_cluster=shed1", "Accept": "text/html"})
        with mock.patch.object(server.FLEET, "self_id", return_value="me"), \
                mock.patch.object(server.AUTH, "verify_token", return_value={"user": "robin", "role": "admin"}), \
                mock.patch.object(server.FLEET, "member", return_value={"name": "Shed"}), \
                mock.patch.object(server.FLEET, "forward", side_effect=server.FLEET.Unreachable("no answer")):
            h.do_GET()
        code, page, kind = h._send.call_args.args
        self.assertEqual(502, code)
        self.assertIn("/api/fleet/home", page)
        self.assertIn("text/html", kind)


class SignedRequestTests(unittest.TestCase):
    def test_a_signed_request_acts_as_the_person_it_carries(self):
        body = json.dumps({"ns": "lab"}).encode()
        h = handler("/api/scale", "POST", {server.FLEET.H_FROM: "loft1"}, body)
        found = {"sender": {"id": "loft1", "name": "Loft", "handle": "loft"}, "user": "robin@loft", "role": "operator"}
        with mock.patch.object(server.FLEET, "verify", return_value=found) as verify:
            who = h._who()
            again = h._who()
        verify.assert_called_once_with(h.headers, "POST", "/api/scale", body)
        self.assertEqual(("robin@loft", "operator"), (who["user"], who["role"]))
        self.assertIs(who, again)
        self.assertEqual({"ns": "lab"}, h._body())

    def test_a_bad_signature_is_nobody(self):
        h = handler(headers={server.FLEET.H_FROM: "loft1"})
        with mock.patch.object(server.FLEET, "verify", side_effect=PermissionError("no")):
            self.assertIsNone(h._who())

    def test_a_relayed_person_is_never_asked_to_set_up(self):
        h = handler("/api/auth/state", headers={server.FLEET.H_FROM: "loft1"})
        found = {"sender": {"id": "loft1", "name": "Loft", "handle": "loft"}, "user": "robin@loft", "role": "viewer"}
        with mock.patch.object(server.FLEET, "verify", return_value=found), \
                mock.patch.object(server.AUTH, "needs_setup", return_value=True):
            h.do_GET()
        state = h._send.call_args.args[1]
        self.assertFalse(state["setup"])
        self.assertEqual("robin@loft", state["user"])
        self.assertEqual("Loft", state["via"])


class SwitchTests(unittest.TestCase):
    def post(self, wanted, reachable=True):
        h = handler("/api/fleet/switch", "POST", {"X-Homestead-Auth": "1", "Cookie": "homestead_session=tok"},
                    json.dumps({"id": wanted}).encode())
        with mock.patch.object(server.FLEET, "self_id", return_value="me"), \
                mock.patch.object(server.AUTH, "verify_token", return_value={"user": "robin", "role": "viewer"}), \
                mock.patch.object(server.FLEET, "member", return_value={"id": "shed1", "name": "Shed"}), \
                mock.patch.object(server.FLEET, "check", return_value={"reachable": reachable, "error": "down"}):
            h.do_POST()
        return h

    def test_switching_sets_the_cluster_for_this_browser(self):
        h = self.post("shed1")
        self.assertEqual(200, h._send.call_args.args[0])
        self.assertIn(("Set-Cookie", "homestead_cluster=shed1; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"),
                      h._extra_headers)

    def test_switching_back_here_forgets_it(self):
        h = self.post("me")
        self.assertTrue(any(v.startswith("homestead_cluster=;") and "Max-Age=0" in v for _, v in h._extra_headers))

    def test_a_cluster_that_does_not_answer_is_not_switched_to(self):
        h = self.post("shed1", reachable=False)
        self.assertEqual(502, h._send.call_args.args[0])
        self.assertFalse(any(k == "Set-Cookie" for k, _ in h._extra_headers))


if __name__ == "__main__":
    unittest.main()
