"""Linking clusters and switching between them, pinned through the real HTTP
server: status, body and every Set-Cookie line (flags included), for each
fleet route and its refusals, signed in here and relayed from a linked
Homestead. These answers must not change when the routes move (#764)."""
import json
import sys
import threading
import time
import unittest
import urllib.parse
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_auth as AUTH
import homestead_fleet as FLEET
import homestead_http as HTTP
import homestead_move as MOVE
import server
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import no_cluster

LISTS = ("workloads", "vms", "nodes", "volumes", "flow")
SENDER = {"id": "loft1", "name": "Loft", "handle": "loft"}
CLEAR = "homestead_cluster=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
# Under TLS a cleared cluster cookie is Secure, as the one it clears was (#764, PR 4).
CLEAR_TLS = "homestead_cluster=; Path=/; HttpOnly; SameSite=Strict; Secure; Max-Age=0"
NO_POLICY = {"error": "this route has no declared authorization policy"}


def setUpModule():
    # No cluster here: a read on the way to what a test checks fails at once, as on CI,
    # instead of looking the API server's name up (tests/fakes/kube.py no_cluster).
    unittest.enterModuleContext(no_cluster())


class Served(unittest.TestCase):
    """One real server.H on 127.0.0.1, its fleet store and its sign-ins patched."""
    role = "viewer"

    @classmethod
    def setUpClass(cls):
        cls.listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=cls.listener.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()

    def setUp(self):
        self.who = {"user": "ada", "role": self.role, "remember": False, "started": 1, "expires": 2}
        self.enterContext(mock.patch.object(AUTH, "verify_token", side_effect=lambda token, force=False:
                                            dict(self.who) if token == "tok" else None))
        self.enterContext(mock.patch.object(FLEET, "self_id", return_value="me"))
        self.enterContext(mock.patch.object(FLEET, "member", side_effect=lambda ref:
                                            {"id": ref, "name": "Shed"} if ref == "shed1" else None))
        self.enterContext(mock.patch.object(FLEET, "forward", side_effect=AssertionError("fleet routes are answered here")))

    def ask(self, method, path, body=None, headers=None, session=True, tls=False, relayed=False):
        """(status, [(header, value)...], parsed body) as a browser sees them."""
        sent = {"X-Homestead-Auth": "1", "Content-Type": "application/json"}
        if session:
            sent["Cookie"] = "homestead_session=tok"
        if tls:
            sent["X-Forwarded-Proto"] = "https"
        if relayed:
            sent[FLEET.H_FROM] = SENDER["id"]
        sent.update(headers or {})
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        conn = HTTPConnection("127.0.0.1", self.listener.server_address[1], timeout=10)
        try:
            conn.request(method, path, body=data, headers=sent)
            reply = conn.getresponse()
            raw = reply.read()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw.decode()
            return reply.status, reply.getheaders(), parsed
        finally:
            conn.close()

    def relay_as(self, role):
        """A request signed by the linked Homestead loft1, for a person there."""
        return self.enterContext(mock.patch.object(FLEET, "verify", return_value={
            "sender": SENDER, "user": "robin@loft", "role": role}))

    @staticmethod
    def cookies(headers):
        return [v for k, v in headers if k.lower() == "set-cookie"]

    @staticmethod
    def header(headers, name):
        found = [v for k, v in headers if k.lower() == name.lower()]
        return found[0] if found else None


class ReadingTests(Served):
    def test_the_fleet_summary_is_for_this_browser(self):
        summary = self.enterContext(mock.patch.object(FLEET, "summary", return_value={"members": [], "self": "me"}))
        self.assertEqual((200, {"members": [], "self": "me"}), self.ask("GET", "/api/fleet")[::2])
        summary.assert_called_once_with(via=None)
        self.assertEqual([], self.cookies(self.ask("GET", "/api/fleet")[1]))

    def test_a_relayed_summary_names_who_relayed_it(self):
        self.relay_as("viewer")
        summary = self.enterContext(mock.patch.object(FLEET, "summary", return_value={"members": []}))
        self.assertEqual((200, {"members": []}), self.ask("GET", "/api/fleet", session=False, relayed=True)[::2])
        summary.assert_called_once_with(via=SENDER)

    def test_nobody_signed_in_is_401_on_every_fleet_route(self):
        for method, path in (("GET", "/api/fleet"), ("GET", "/api/fleet/state"), ("GET", "/api/fleet/all/vms"), ("POST", "/api/fleet/switch"), ("POST", "/api/fleet/join"),
                             ("POST", "/api/fleet/sync")):
            with self.subTest(path=path):
                code, headers, body = self.ask(method, path, session=False)
                self.assertEqual((401, {"error": "not signed in", "auth": False}), (code, body))
                self.assertEqual([], self.cookies(headers))

    def test_state_only_for_a_linked_homestead(self):
        shared = self.enterContext(mock.patch.object(FLEET, "shared_state", return_value={"revision": 3}))
        self.assertEqual((403, {"error": "only a linked Homestead asks for this"}),
                         self.ask("GET", "/api/fleet/state")[::2])
        shared.assert_not_called()
        self.relay_as("viewer")
        self.assertEqual((200, {"revision": 3}), self.ask("GET", "/api/fleet/state", session=False, relayed=True)[::2])

    def test_a_bad_fleet_signature_is_nobody_even_with_a_session(self):
        # The real FLEET.verify, with this Homestead linked to loft1: a request
        # naming loft1 with a signature that does not match is refused by the
        # guard, and neither linked-only route runs.
        state = {"self": "me", "revision": 1, "members": [{"id": "loft1", "handle": "loft", "name": "Loft"},
                                                           {"id": "me", "handle": "home", "name": "Home"}]}
        self.enterContext(mock.patch.object(FLEET, "_load", return_value=(state, b"k" * 32)))
        shared = self.enterContext(mock.patch.object(FLEET, "shared_state"))
        adopt = self.enterContext(mock.patch.object(FLEET, "adopt"))
        forged = {FLEET.H_TIME: str(int(time.time())), FLEET.H_NONCE: "n1", FLEET.H_USER: "fleet:loft",
                  FLEET.H_ROLE: "admin", FLEET.H_SIG: "not-the-signature"}
        for session in (False, True):
            for method, path in (("GET", "/api/fleet/state"), ("POST", "/api/fleet/sync")):
                with self.subTest(session=session, path=path):
                    code, headers, body = self.ask(method, path, {} if method == "POST" else None,
                                                   headers=forged, session=session, relayed=True)
                    self.assertEqual((401, {"error": "not signed in", "auth": False}), (code, body))
        shared.assert_not_called()
        adopt.assert_not_called()

    def test_home_clears_and_redirects(self):
        for tls in (False, True):
            with self.subTest(tls=tls):
                code, headers, body = self.ask("GET", "/api/fleet/home", tls=tls)
                self.assertEqual((302, {"ok": True}), (code, body))
                self.assertEqual("/", self.header(headers, "Location"))
                self.assertEqual([CLEAR_TLS if tls else CLEAR], self.cookies(headers))

    def test_home_is_the_way_back_even_without_a_session(self):
        # A public path: the page a cluster that does not answer shows links to it.
        code, headers, body = self.ask("GET", "/api/fleet/home", session=False)
        self.assertEqual((302, {"ok": True}), (code, body))
        self.assertEqual("/", self.header(headers, "Location"))
        self.assertEqual([CLEAR], self.cookies(headers))

    def test_home_is_get_only(self):
        for session in (True, False):
            code, headers, body = self.ask("POST", "/api/fleet/home", session=session)
            # Answered by server.py's own "no route" now that no fleet helper is left there.
            self.assertEqual((404, {"error": "no route"}), (code, body))
            self.assertEqual([], self.cookies(headers))
        code, headers, body = self.ask("POST", "/api/fleet/home", headers={"X-Homestead-Auth": "0"})
        self.assertEqual((403, {"error": "missing X-Homestead-Auth header"}), (code, body))


class AllListsTests(Served):
    def test_each_list_is_gathered_for_the_person_asking(self):
        for what in LISTS:
            with self.subTest(what=what):
                rows = {"clusters": [], "missing": []} if what == "flow" else [{"name": "a", "site": "Shed"}]
                gather = self.enterContext(mock.patch.object(FLEET, "gather", return_value=(rows, [])))
                code, headers, body = self.ask("GET", f"/api/fleet/all/{what}")
                self.assertEqual((200, rows), (code, body))
                self.assertIsNone(self.header(headers, "X-Homestead-Fleet-Missing"))
                args = gather.call_args.args
                self.assertEqual((what, "ada", "viewer"), (args[0], args[2], args[3]))
                self.assertTrue(callable(args[1]))

    def test_all_lists_name_the_missing_clusters(self):
        missing = [{"id": "shed1", "name": "Shed", "error": "no answer"}]
        self.enterContext(mock.patch.object(FLEET, "gather", return_value=([], missing)))
        code, headers, body = self.ask("GET", "/api/fleet/all/nodes")
        self.assertEqual((200, []), (code, body))
        self.assertEqual(urllib.parse.quote(json.dumps(missing)), self.header(headers, "X-Homestead-Fleet-Missing"))

    def test_an_unknown_all_list_is_still_403(self):
        gather = self.enterContext(mock.patch.object(FLEET, "gather", return_value=([], [])))
        for method, path in (("GET", "/api/fleet/all/secrets"), ("GET", "/api/fleet/all/"),
                             ("GET", "/api/fleet/all/vms/x"), ("POST", "/api/fleet/all/flow")):
            with self.subTest(method=method, path=path):
                self.assertEqual((403, NO_POLICY), self.ask(method, path)[::2])
        gather.assert_not_called()

    def test_an_unknown_fleet_route_is_403(self):
        for method, path in (("GET", "/api/fleet/nope"), ("POST", "/api/fleet/nope"), ("POST", "/api/fleet")):
            with self.subTest(method=method, path=path):
                self.assertEqual((403, NO_POLICY), self.ask(method, path)[::2])


class SwitchTests(Served):
    def reachable(self, answer=True):
        return self.enterContext(mock.patch.object(FLEET, "check", return_value={"reachable": answer, "error": "down"}))

    def test_switch_sets_and_clears_the_cluster_cookie_with_tls_and_without(self):
        self.reachable()
        for tls, secure in ((False, ""), (True, "; Secure")):
            with self.subTest(tls=tls):
                code, headers, body = self.ask("POST", "/api/fleet/switch", {"id": "shed1"}, tls=tls)
                self.assertEqual((200, {"ok": True, "id": "shed1", "name": "Shed"}), (code, body))
                self.assertEqual([f"homestead_cluster=shed1; Path=/; HttpOnly; SameSite=Strict{secure}; Max-Age=2592000"],
                                 self.cookies(headers))
                for back in ({"id": "me"}, {"id": ""}, {}):
                    code, headers, body = self.ask("POST", "/api/fleet/switch", back, tls=tls)
                    self.assertEqual((200, {"ok": True, "id": "me"}), (code, body))
                    self.assertEqual([CLEAR_TLS if tls else CLEAR], self.cookies(headers))

    def test_an_unknown_or_silent_cluster_is_not_switched_to(self):
        self.reachable(False)
        code, headers, body = self.ask("POST", "/api/fleet/switch", {"id": "nowhere1"})
        self.assertEqual((404, {"error": "no linked cluster by that name"}), (code, body))
        self.assertEqual([], self.cookies(headers))
        code, headers, body = self.ask("POST", "/api/fleet/switch", {"id": "shed1"})
        self.assertEqual((502, {"error": "Shed is not answering this Homestead: down"}), (code, body))
        self.assertEqual([], self.cookies(headers))

    def test_switch_needs_the_csrf_header(self):
        self.reachable()
        code, headers, body = self.ask("POST", "/api/fleet/switch", {"id": "shed1"}, headers={"X-Homestead-Auth": "0"})
        self.assertEqual((403, {"error": "missing X-Homestead-Auth header"}), (code, body))
        self.assertEqual([], self.cookies(headers))

    def test_a_refreshed_session_cookie_comes_before_the_switch_cookie(self):
        self.reachable()
        self.who["stale"] = True
        self.enterContext(mock.patch.object(AUTH, "issue_token", return_value="fresh"))
        self.enterContext(mock.patch.object(AUTH, "idle_ttl", return_value=3600))
        code, headers, _ = self.ask("POST", "/api/fleet/switch", {"id": "shed1"})
        self.assertEqual(200, code)
        self.assertEqual(["homestead_session=fresh; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600",
                          "homestead_cluster=shed1; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"],
                         self.cookies(headers))

    def test_a_forgotten_cluster_cookie_is_cleared_secure_under_tls(self):
        self.enterContext(mock.patch.object(FLEET, "summary", return_value={"members": []}))
        for tls in (False, True):
            with self.subTest(tls=tls):
                code, headers, _ = self.ask("GET", "/api/fleet", tls=tls,
                                            headers={"Cookie": "homestead_session=tok; homestead_cluster=gone1"})
                self.assertEqual(200, code)
                self.assertEqual([CLEAR_TLS if tls else CLEAR], self.cookies(headers))

    def test_a_browser_switched_away_still_switches_here(self):
        self.reachable()
        code, headers, body = self.ask("POST", "/api/fleet/switch", {"id": "me"},
                                       headers={"Cookie": "homestead_session=tok; homestead_cluster=shed1"})
        self.assertEqual((200, {"ok": True, "id": "me"}), (code, body))
        self.assertEqual([CLEAR], self.cookies(headers))


class LinkingTests(Served):
    role = "admin"
    CALLS = (
        ("/api/fleet/join", "join", {"url": "https://example.com", "username": "ada", "password": "pw", "own_url": "http://192.0.2.1", "plain_ok": True},
         lambda m: m.assert_called_once_with("https://example.com", "ada", "pw", "http://192.0.2.1", plain_ok=True)),
        ("/api/fleet/accept", "accept", {"invite": "x"}, lambda m: m.assert_called_once_with({"invite": "x"})),
        ("/api/fleet/remove", "remove", {"id": "shed1", "plain_ok": "yes"},
         lambda m: m.assert_called_once_with("shed1", plain_ok=False)),
        ("/api/fleet/leave", "leave", {"plain_ok": True}, lambda m: m.assert_called_once_with(plain_ok=True)),
        ("/api/fleet/address", "set_address", {"url": "http://192.0.2.1:8080"},
         lambda m: m.assert_called_once_with("http://192.0.2.1:8080")),
    )

    def test_each_link_action_answers_what_the_fleet_said(self):
        for path, name, body, called in self.CALLS:
            with self.subTest(path=path):
                fn = self.enterContext(mock.patch.object(FLEET, name, return_value={"ok": True, "did": name}))
                code, headers, answer = self.ask("POST", path, body)
                self.assertEqual((200, {"ok": True, "did": name}), (code, answer))
                self.assertEqual([], self.cookies(headers))
                called(fn)

    def test_join_defaults(self):
        join = self.enterContext(mock.patch.object(FLEET, "join", return_value={"ok": True}))
        self.ask("POST", "/api/fleet/join", {"url": "https://example.com", "plain_ok": 1})
        join.assert_called_once_with("https://example.com", None, None, "", plain_ok=False)

    def test_link_legacy(self):
        link = self.enterContext(mock.patch.object(MOVE, "link_legacy", return_value={"ok": True, "linked": "old"}))
        self.assertEqual((200, {"ok": True, "linked": "old"}),
                         self.ask("POST", "/api/fleet/link-legacy", {"name": "old", "own_url": "http://192.0.2.1", "plain_ok": True})[::2])
        link.assert_called_once_with("old", "http://192.0.2.1", plain_ok=True)

    def test_join_errors_map_to_409_and_502(self):
        for path, name in [(p, n) for p, n, _, _ in self.CALLS]:
            with self.subTest(path=path):
                with mock.patch.object(FLEET, name, side_effect=ValueError("that link is refused")):
                    self.assertEqual((409, {"error": "that link is refused"}), self.ask("POST", path, {})[::2])
                with mock.patch.object(FLEET, name, side_effect=FLEET.Unreachable("no answer from there")):
                    self.assertEqual((502, {"error": "no answer from there"}), self.ask("POST", path, {})[::2])
        with mock.patch.object(MOVE, "link_legacy", side_effect=ValueError("no such install")):
            self.assertEqual((409, {"error": "no such install"}), self.ask("POST", "/api/fleet/link-legacy", {})[::2])

    def test_sync_only_from_a_linked_homestead(self):
        adopt = self.enterContext(mock.patch.object(FLEET, "adopt", return_value={"ok": True, "revision": 4}))
        self.assertEqual((403, {"error": "only a linked Homestead sends this"}),
                         self.ask("POST", "/api/fleet/sync", {"revision": 4})[::2])
        adopt.assert_not_called()
        self.relay_as("admin")
        self.assertEqual((200, {"ok": True, "revision": 4}),
                         self.ask("POST", "/api/fleet/sync", {"revision": 4}, session=False, relayed=True)[::2])
        adopt.assert_called_once_with({"revision": 4}, SENDER)

    def test_get_on_a_link_action_is_not_found_for_an_admin(self):
        for path in ("/api/fleet/join", "/api/fleet/accept", "/api/fleet/sync", "/api/fleet/remove",
                     "/api/fleet/leave", "/api/fleet/address", "/api/fleet/link-legacy"):
            with self.subTest(path=path):
                self.assertEqual(404, self.ask("GET", path)[0])

    def test_a_relayed_switch_is_answered_here(self):
        self.relay_as("viewer")
        self.enterContext(mock.patch.object(FLEET, "check", return_value={"reachable": True}))
        code, headers, body = self.ask("POST", "/api/fleet/switch", {"id": "shed1"}, session=False, relayed=True)
        self.assertEqual((200, {"ok": True, "id": "shed1", "name": "Shed"}), (code, body))
        self.assertEqual(["homestead_cluster=shed1; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"],
                         self.cookies(headers))


class RolesTests(Served):
    role = "viewer"

    def test_a_viewer_may_not_link_or_unlink(self):
        for path in ("/api/fleet/join", "/api/fleet/accept", "/api/fleet/sync", "/api/fleet/remove",
                     "/api/fleet/leave", "/api/fleet/address", "/api/fleet/link-legacy"):
            for method in ("GET", "POST"):
                with self.subTest(method=method, path=path):
                    self.assertEqual((403, {"error": "your role (viewer) cannot do this — admin required",
                                            "role": "viewer", "needed": "admin"}), self.ask(method, path, {} if method == "POST" else None)[::2])

    def test_a_relayed_viewer_may_not_link_either(self):
        self.relay_as("viewer")
        sync = self.enterContext(mock.patch.object(FLEET, "adopt"))
        code, _, body = self.ask("POST", "/api/fleet/sync", {}, session=False, relayed=True)
        self.assertEqual(403, code)
        self.assertEqual("admin", body["needed"])
        sync.assert_not_called()


class DeclaredTests(unittest.TestCase):
    def test_fleet_routes_are_declared_in_their_module(self):
        import homestead_routes as ROUTES
        declared = [("GET", "/api/fleet"), ("GET", "/api/fleet/state"), ("GET", "/api/fleet/home")]
        declared += [("GET", f"/api/fleet/all/{what}") for what in LISTS]
        declared += [("POST", f"/api/fleet/{action}") for action in (
            "switch", "join", "accept", "sync", "remove", "leave", "address", "link-legacy")]
        for method, path in declared:
            route = ROUTES.find(method, path)
            self.assertEqual("homestead_fleet_routes", route and route.module, f"{method} {path}")
        self.assertIsNone(ROUTES.find("GET", "/api/fleet/all/secrets"))

    def test_the_combined_lists_are_routes_not_a_code_prefix(self):
        # The five lists are this module's exact routes, so server.py keeps no
        # /api/fleet/all/ branch; an unknown list still has no role (403).
        import homestead_route_policy as POLICY
        self.assertNotIn("/api/fleet/all/", POLICY.CODE_PREFIXES)
        for what in LISTS:
            self.assertEqual("viewer", POLICY.role(f"/api/fleet/all/{what}", "GET"), what)
            self.assertIsNone(POLICY.role(f"/api/fleet/all/{what}", "POST"), what)
        for path in ("/api/fleet/all/secrets", "/api/fleet/all/", "/api/fleet/all/vms/x"):
            self.assertIsNone(POLICY.role(path, "GET"), path)


if __name__ == "__main__":
    unittest.main()
