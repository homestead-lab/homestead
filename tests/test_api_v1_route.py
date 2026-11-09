"""/api/v1 pinned through the real HTTP server: for an API key and for
someone signed in, what reaches homestead_api_v1, what the change history
records, and what comes back. These answers must not change when the route
moves (#764)."""
import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_api_keys as API_KEYS
import homestead_api_v1 as API_V1
import homestead_auth as AUTH
import homestead_fleet as FLEET
import homestead_http as HTTP
import homestead_mcp as MCP
import server
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import no_cluster

KEY = {"id": "1", "name": "ha", "owner": "ada", "scopes": ["read", "containers:control"], "expires": 9}
SENDER = {"id": "loft1", "name": "Loft", "handle": "loft"}


def setUpModule():
    # No cluster here: a read on the way to what a test checks fails at once, as on CI,
    # instead of looking the API server's name up (tests/fakes/kube.py no_cluster).
    unittest.enterModuleContext(no_cluster())


class ApiV1RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=cls.listener.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()

    def setUp(self):
        self.role = "viewer"
        self.enterContext(mock.patch.object(AUTH, "verify_token", side_effect=lambda token, force=False:
                                            {"user": "ada", "role": self.role, "remember": False,
                                             "started": 1, "expires": 2} if token == "tok" else None))
        self.enterContext(mock.patch.object(API_KEYS, "verify", side_effect=lambda token, addr, force=False:
                                            dict(KEY) if token == "hsk_good" else
                                            {**KEY, "name": "dash", "scopes": ["read"]} if token == "hsk_read" else None))
        self.enterContext(mock.patch.object(FLEET, "self_id", return_value="me"))
        self.enterContext(mock.patch.object(FLEET, "member", return_value={"id": "shed1", "name": "Shed"}))
        self.enterContext(mock.patch.object(FLEET, "forward", side_effect=AssertionError("/api/v1 never travels")))
        self.handle = self.enterContext(mock.patch.object(API_V1, "handle", return_value=(200, [{"name": "node1"}])))
        self.audit = self.enterContext(mock.patch.object(MCP, "audit_api"))

    def ask(self, method, path, body=None, key=False, session=True, csrf=True, headers=None):
        sent = {"Content-Type": "application/json"}
        if csrf:
            sent["X-Homestead-Auth"] = "1"
        if session:
            sent["Cookie"] = "homestead_session=tok"
        if key:
            sent["Authorization"] = "Bearer hsk_good"
        sent.update(headers or {})
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        conn = HTTPConnection("127.0.0.1", self.listener.server_address[1], timeout=10)
        try:
            conn.request(method, path, body=data, headers=sent)
            reply = conn.getresponse()
            raw = reply.read()
            cookies = [v for k, v in reply.getheaders() if k.lower() == "set-cookie"]
            return reply.status, json.loads(raw) if raw else None, cookies
        finally:
            conn.close()

    def session_auth(self, role):
        return {"name": "ada", "kind": "session", "expires": None, "scopes": API_KEYS.scopes_for_role(role)}

    def test_a_session_reads_with_its_roles_scopes(self):
        self.assertEqual((200, [{"name": "node1"}], []), self.ask("GET", "/api/v1/nodes?limit=2"))
        self.handle.assert_called_once_with("GET", "/api/v1/nodes", {"limit": ["2"]}, None, self.session_auth("viewer"))
        self.audit.assert_called_once_with(None, "GET", "/api/v1/nodes", None, 200, "127.0.0.1")

    def test_a_key_reads_with_its_own_scopes_and_no_session(self):
        self.assertEqual((200, [{"name": "node1"}], []), self.ask("GET", "/api/v1/nodes", key=True, session=False))
        auth = {**KEY, "kind": "key"}
        self.handle.assert_called_once_with("GET", "/api/v1/nodes", {}, None, auth)
        self.audit.assert_called_once_with(auth, "GET", "/api/v1/nodes", None, 200, "127.0.0.1")

    def test_a_key_and_a_session_get_the_same_answers(self):
        self.role = "operator"
        for code, answer in ((200, {"warnings": [], "job": None}), (409, {"error": "not enough room"}),
                             (404, {"error": "no such endpoint; GET /api/v1/openapi.json lists them"})):
            self.handle.return_value = (code, answer)
            for key in (False, True):
                with self.subTest(code=code, key=key):
                    self.assertEqual((code, answer, []),
                                     self.ask("POST", "/api/v1/containers/lab/web/stop", {"replicas": 2},
                                              key=key, session=not key))

    def test_post_passes_the_query_and_body_through(self):
        self.role = "operator"
        self.ask("POST", "/api/v1/containers/lab/web/stop?dry=1", {"replicas": 2})
        self.handle.assert_called_once_with("POST", "/api/v1/containers/lab/web/stop", {"dry": ["1"]},
                                            {"replicas": 2}, self.session_auth("operator"))
        self.audit.assert_called_once_with(None, "POST", "/api/v1/containers/lab/web/stop", {"replicas": 2},
                                           200, "127.0.0.1")

    def test_a_key_posts_without_the_csrf_header_and_a_session_does_not(self):
        self.role = "operator"
        self.assertEqual(200, self.ask("POST", "/api/v1/containers/lab/web/restart", key=True, session=False,
                                       csrf=False)[0])
        self.assertEqual((403, {"error": "missing X-Homestead-Auth header"}, []),
                         self.ask("POST", "/api/v1/containers/lab/web/restart", csrf=False))
        self.assertEqual(1, self.handle.call_count)

    def test_a_viewer_session_may_read_but_not_act(self):
        self.assertEqual((403, {"error": "This needs the operator role, and the viewer role cannot do it",
                                "role": "viewer", "needed": "operator"}, []),
                         self.ask("POST", "/api/v1/containers/lab/web/restart"))
        self.handle.assert_not_called()
        self.audit.assert_not_called()

    def test_nobody_signed_in_and_no_key_is_401(self):
        self.assertEqual((401, {"error": "not signed in", "auth": False}, []),
                         self.ask("GET", "/api/v1/nodes", session=False))
        self.handle.assert_not_called()

    def test_a_bad_key_is_401(self):
        with mock.patch.object(API_KEYS, "verify", side_effect=PermissionError("that API key is not valid")), \
                mock.patch.object(server.SIGNINS, "record"):
            self.assertEqual((401, {"error": "that API key is not valid"}, []),
                             self.ask("GET", "/api/v1/nodes", key=True, session=False))
        self.handle.assert_not_called()

    def test_openapi_names_this_version(self):
        for key in (False, True):
            with self.subTest(key=key):
                code, body, _ = self.ask("GET", "/api/v1/openapi.json", key=key, session=not key)
                self.assertEqual(200, code)
                self.assertEqual(server.HOMESTEAD_VERSION, body["info"]["x-homestead-version"])
                expected = API_V1.openapi(server.HOMESTEAD_VERSION,
                                          {s: text for s, (_, text) in API_KEYS.SCOPES.items()})
                self.assertEqual(json.loads(json.dumps(expected)), body)
        self.handle.assert_not_called()
        self.audit.assert_not_called()

    def test_posting_to_openapi_is_handled_as_any_other_path(self):
        self.role = "operator"
        self.handle.return_value = (405, {"error": "use GET here"})
        self.assertEqual((405, {"error": "use GET here"}, []), self.ask("POST", "/api/v1/openapi.json"))

    def test_a_browser_switched_away_is_still_answered_here(self):
        self.assertEqual(200, self.ask("GET", "/api/v1/nodes",
                                       headers={"Cookie": "homestead_session=tok; homestead_cluster=shed1"})[0])
        self.assertEqual(200, self.ask("GET", "/api/v1/nodes", key=True, session=False,
                                       headers={"X-Homestead-Cluster": "shed1"})[0])
        self.assertEqual(2, self.handle.call_count)

    def test_a_relayed_person_is_answered_as_a_session(self):
        found = {"sender": SENDER, "user": "robin@loft", "role": "viewer"}
        self.enterContext(mock.patch.object(FLEET, "verify", return_value=found))
        self.assertEqual(200, self.ask("GET", "/api/v1/nodes", session=False, headers={FLEET.H_FROM: "loft1"})[0])
        self.handle.assert_called_once_with("GET", "/api/v1/nodes", {}, None,
                                            {"name": "robin@loft", "kind": "session", "expires": None,
                                             "scopes": API_KEYS.scopes_for_role("viewer")})

    def test_post_reads_the_query_with_get_only(self):
        # The handlers read the query with .get, so the Query a module route is
        # given (a 400 on a missing key) answers as the plain dict did.
        calls = []
        stubs = {"workloads": lambda: [{"ns": "lab", "name": "web", "problems": []}],
                 "vms": lambda: [{"ns": "lab", "name": "win", "running": False}],
                 "scale": lambda ns, name, n: calls.append((ns, name, n)) or {"warnings": [], "job": None},
                 "restart": lambda ns, name: calls.append((ns, name)),
                 "vm_power": lambda ns, name, action: calls.append((ns, name, action)) or {"warnings": [], "job": "j1"}}
        with mock.patch.dict(API_V1.ctx, stubs):
            for path, body in (("/api/v1/containers/lab/web/start", {"replicas": 1}),
                               ("/api/v1/containers/lab/web/restart", {}),
                               ("/api/v1/vms/lab/win/start", {})):
                for query in ({}, server.Query({})):
                    with self.subTest(path=path, query=type(query).__name__):
                        code, _ = REAL_HANDLE("POST", path, query, body,
                                              {**KEY, "scopes": KEY["scopes"] + ["vms:control"], "kind": "key"})
                        self.assertEqual(200, code)
        self.assertEqual(6, len(calls))

    def test_a_key_acts_only_with_its_scopes(self):
        # The real handle(): the key's scopes decide, and both answers are recorded.
        self.handle.side_effect = REAL_HANDLE
        restarted = []
        with mock.patch.dict(API_V1.ctx, {"workloads": lambda: [{"ns": "lab", "name": "web", "problems": []}],
                                          "restart": lambda ns, name: restarted.append((ns, name))}):
            self.assertEqual((403, {"error": "this needs the containers:control scope"}, []),
                             self.ask("POST", "/api/v1/containers/lab/web/restart", key=True, session=False,
                                      csrf=False, headers={"Authorization": "Bearer hsk_read"}))
            self.assertEqual([], restarted)
            self.assertEqual((200, {"ok": True, "action": "restart", "warnings": []}, []),
                             self.ask("POST", "/api/v1/containers/lab/web/restart", key=True, session=False, csrf=False))
        self.assertEqual([("lab", "web")], restarted)
        self.assertEqual([403, 200], [c.args[4] for c in self.audit.call_args_list])
        self.assertEqual(["dash", "ha"], [c.args[0]["name"] for c in self.audit.call_args_list])

    def test_answers_are_json(self):
        for path, key in (("/api/v1/nodes", False), ("/api/v1/nodes", True), ("/api/v1/openapi.json", True)):
            with self.subTest(path=path, key=key):
                conn = HTTPConnection("127.0.0.1", self.listener.server_address[1], timeout=10)
                try:
                    conn.request("GET", path, headers={"Authorization": "Bearer hsk_good"} if key
                                 else {"Cookie": "homestead_session=tok"})
                    reply = conn.getresponse()
                    reply.read()
                    self.assertEqual((200, "application/json"), (reply.status, reply.getheader("Content-Type")))
                finally:
                    conn.close()

    def test_a_key_with_a_fleet_header_is_never_checked_as_a_linked_homestead(self):
        # A key's request is the key's alone: a sender header on it is not read.
        verify = self.enterContext(mock.patch.object(FLEET, "verify", side_effect=AssertionError("not for a key")))
        self.assertEqual(200, self.ask("GET", "/api/v1/nodes", key=True, session=False,
                                       headers={FLEET.H_FROM: "loft1"})[0])
        verify.assert_not_called()
        self.handle.assert_called_once_with("GET", "/api/v1/nodes", {}, None, {**KEY, "kind": "key"})

    def test_delete_on_api_v1(self):
        # Today: a session passes the guard as an operator, then no route answers.
        self.role = "operator"
        code, body, _ = self.ask("DELETE", "/api/v1/nodes")
        self.assertEqual(404, code)
        code, body, _ = self.ask("DELETE", "/api/v1/nodes", key=True, session=False)
        self.assertEqual(404, code)
        self.handle.assert_not_called()


class DeclaredTests(unittest.TestCase):
    def test_api_v1_is_declared_by_its_module(self):
        import homestead_routes as ROUTES
        for method, role in (("GET", "viewer"), ("POST", "operator"), ("DELETE", "operator")):
            for path in ("/api/v1/openapi.json", "/api/v1/nodes", "/api/v1/containers/lab/web/stop"):
                route = ROUTES.find(method, path)
                self.assertEqual(("homestead_api_v1", "/api/v1/*", role),
                                 route and (route.module, route.path, route.role), f"{method} {path}")
        self.assertIsNone(ROUTES.find("PUT", "/api/v1/nodes"))

    def test_no_other_module_may_declare_a_path_under_api_v1(self):
        # A key passes the guard for any /api/v1/ path before its role is
        # checked, so a route there must be homestead_api_v1's, scopes and all.
        import types
        import homestead_routes as ROUTES
        for method, path in (("GET", "/api/v1/whoami"), ("POST", "/api/v1/containers/lab/web/stop"),
                             ("GET", "/api/v1/nodes/*"), ("GET", "/api/v1/")):
            module = types.ModuleType("homestead_test_v1")
            module.ROUTES = {(method, path): ("viewer", lambda request: {})}
            with self.subTest(method=method, path=path), \
                    mock.patch.dict(sys.modules, {"homestead_test_v1": module}), \
                    mock.patch.object(ROUTES, "MODULES", ("homestead_test_v1",)), \
                    mock.patch.object(ROUTES, "_table", {}), self.assertRaises(ValueError):
                ROUTES.table()

    def test_only_homestead_api_v1_may_declare_the_api_v1_prefix(self):
        import homestead_route_policy as POLICY
        import homestead_routes as ROUTES
        self.assertNotIn("/api/v1/", POLICY.CODE_PREFIXES)
        self.assertEqual("homestead_api_v1", ROUTES.table()[("GET", "/api/v1/*")].module)
        self.assertEqual([("DELETE", "/api/v1/*"), ("GET", "/api/v1/*"), ("POST", "/api/v1/*")],
                         sorted(k for k in ROUTES.table() if k[1].startswith("/api/v1/")))


REAL_HANDLE = API_V1.handle


if __name__ == "__main__":
    unittest.main()
