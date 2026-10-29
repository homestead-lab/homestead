"""What a module route may see of the connection and write back (#764).

A route gets the request's headers - without the ones that carry a secret -
and Homestead's own cookies, and who the guard found: a Cloudflare Access
sign-in, a linked Homestead, an API key. It can set Homestead's own cookies and
safe response headers through one checked writer; anything else is refused
with a 500 before any of it is written."""
import email.message
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_routes as ROUTES

C = ROUTES.Cookie


def bare_handler(sent=(), **attrs):
    """server.H as the tests build it: no socket, headers as the request sent them."""
    import server
    handler = object.__new__(server.H)
    headers = email.message.Message()
    for name, value in sent:
        headers[name] = value
    handler.headers = headers
    for name, value in attrs.items():
        setattr(handler, name, value)
    return handler


class Interface(unittest.TestCase):
    def test_old_requests_and_replies_still_build(self):
        r = ROUTES.Request("GET", "/api/x", {}, None, "ada", "viewer")
        self.assertEqual(("", None), (r.addr, r.conn))
        self.assertEqual(((), ()), (ROUTES.Reply(409, {"e": 1}).headers, ROUTES.Reply(409, {}).cookies))
        self.assertEqual("text/plain", ROUTES.Reply(200, "x", "text/plain").ctype)

    def test_cookie_header_matches_set_cookie_byte_for_byte(self):
        self.assertEqual("homestead_session=t; Path=/; HttpOnly; SameSite=Strict; Max-Age=60",
                         ROUTES.cookie_header(C("homestead_session", "t", max_age=60), tls=False))
        self.assertEqual("homestead_session=; Path=/; HttpOnly; SameSite=Strict; Secure; Max-Age=0",
                         ROUTES.cookie_header(C("homestead_session", "", max_age=0), tls=True))
        self.assertEqual("homestead_cluster=node1; Path=/; HttpOnly; SameSite=Strict",
                         ROUTES.cookie_header(C("homestead_cluster", "node1"), tls=False))

    def test_only_homesteads_cookies_with_plain_values(self):
        for bad in (C("CF_Authorization", "x"), C("homestead_session", "a;b"), C("homestead_session", "a b"),
                    C("homestead_session", "a,b"), C("homestead_session", "a\r\n"), C("homestead_session", "a\x00"),
                    C("homestead_session", "a", path="x"), C("homestead_session", "a", path="/a;b"),
                    C("homestead_session", "a", max_age="1; Domain=example.com"), C("homestead_session", "a", max_age=-1)):
            with self.subTest(cookie=bad), self.assertRaises(ROUTES.Refused):
                ROUTES.cookie_header(bad, tls=False)

    def test_each_cookie_keeps_its_own_path(self):                          # review minor 2
        for bad in (C("homestead_session", "a", path="/api/"), C("homestead_cluster", "a", path="/api/fleet/"),
                    C("homestead-data-move", "t"), C("homestead_session", "a", max_age=True)):
            with self.subTest(cookie=bad), self.assertRaises(ROUTES.Refused):
                ROUTES.cookie_header(bad, tls=False)
        self.assertEqual("homestead-data-move=t; Path=/api/self/data/handoff/; HttpOnly; SameSite=Strict; Max-Age=60",
                         ROUTES.cookie_header(C("homestead-data-move", "t", "/api/self/data/handoff/", 60), tls=False))

    def test_headers_refuse_injection_and_owned_names(self):
        for name, value in (("Content-Disposition", "a\r\nSet-Cookie: x=1"), ("Retry-After", "a\nb"),
                            ("Bad Name", "v"), ("X:A", "v"), ("Set-Cookie", "x=1"), ("content-security-policy", "x"),
                            ("Cache-Control", "max-age=9"), ("Content-Disposition", "☃"), ("Vary", "a\x00")):
            with self.subTest(name=name, value=value), self.assertRaises(ROUTES.Refused):
                ROUTES.check_header(name, value)
        self.assertEqual(("Content-Disposition", 'attachment; filename="a.zip"'),
                         ROUTES.check_header("Content-Disposition", 'attachment; filename="a.zip"'))

    def test_only_the_headers_a_route_needs_may_be_set(self):         # security review 3
        for name in ("Transfer-Encoding", "content-encoding", "Upgrade", "Refresh", "Access-Control-Allow-Origin",
                     "access-control-allow-credentials", "Set-Cookie2", "X-A", "Link", "Content-Length"):
            with self.subTest(name=name), self.assertRaises(ROUTES.Refused):
                ROUTES.check_header(name, "1")
        for name in ("Location", "Content-Disposition", "Retry-After", "ETag", "last-modified", "Vary",
                     "X-Homestead-Fleet-Missing"):
            value = "/" if name == "Location" else "1"
            self.assertEqual((name, value), ROUTES.check_header(name, value))

    def test_location_refuses_scheme_relative_and_backslash(self):      # Review Focus 1
        self.assertEqual(("Location", "/"), ROUTES.check_header("Location", "/"))
        for bad in ("//example.com", "/\\example.com", "https://example.com", "example.com", "/a b"):
            with self.subTest(location=bad), self.assertRaises(ROUTES.Refused):
                ROUTES.check_header("Location", bad)

    def test_refused_is_not_a_value_error(self):
        # do_* answers a ValueError with 400; a refused reply is Homestead's own fault: 500.
        self.assertFalse(issubclass(ROUTES.Refused, ValueError))


class PrefixRoutes(unittest.TestCase):
    def setUp(self):
        ROUTES.table()
        self.added = []
        for path, role in (("/api/t/*", "admin"), ("/api/t/exact", "viewer"), ("/api/t/u/*", "operator")):
            ROUTES._table[("GET", path)] = ROUTES.Route("GET", path, role, lambda request: {}, "test")
            self.added.append(("GET", path))

    def tearDown(self):
        for key in self.added:
            ROUTES._table.pop(key, None)

    def test_prefix_routes(self):
        self.assertEqual("/api/t/*", ROUTES.find("GET", "/api/t/a/b").path)
        self.assertEqual("/api/t/exact", ROUTES.find("GET", "/api/t/exact").path)
        self.assertIsNone(ROUTES.find("GET", "/api/t"))
        self.assertIsNone(ROUTES.find("GET", "/api/tx"))
        self.assertIsNone(ROUTES.find("POST", "/api/t/a"))
        self.assertEqual("/api/t/u/*", ROUTES.find("GET", "/api/t/u/v").path)
        self.assertEqual("admin", ROUTES.role("/api/t/a", "GET"))

    def test_a_prefix_route_answers_only_plain_paths_under_it(self):     # review minor 1
        for path in ("/api/t/", "/api/t/../auth/keys", "/api/t//a", "/api/t/a/", "/api/t/%2e%2e/x",
                     "/api/t/a\\b", "/api/t/./a", "/api/t/a/.."):
            with self.subTest(path=path):
                self.assertIsNone(ROUTES.find("GET", path))
        self.assertEqual("/api/t/*", ROUTES.find("GET", "/api/t/a.b/c-d").path)

    def declare(self, method, path):
        """table() as it reads a module that declares only this route."""
        module = type(sys)("homestead_test_star")
        module.ROUTES = {(method, path): ("viewer", lambda request: {})}
        with mock.patch.dict(sys.modules, {"homestead_test_star": module}), \
                mock.patch.object(ROUTES, "MODULES", ("homestead_test_star",)), mock.patch.object(ROUTES, "_table", {}):
            return ROUTES.table()

    def test_a_star_anywhere_but_the_end_is_refused(self):
        for path in ("/api/a*/b", "/api/a*", "/api/*/b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.declare("GET", path)

    def test_a_prefix_route_may_not_cover_another_answer(self):          # security review 2
        # a POLICY row, a public path, /api/v1, a page or the app's own routes
        for method, path in (("GET", "/api/vm/store/*"), ("POST", "/api/auth/*"), ("GET", "/api/fleet/*"),
                             ("GET", "/api/v1/*"), ("GET", "/api/v1/nodes/*"), ("POST", "/api/*"), ("GET", "/*"),
                             ("GET", "/settings/*"), ("GET", "/vms/*"), ("GET", "/js/*"),
                             # server.py's own prefix answers (review I2)
                             # (GET /api/self/data/handoff/ was lifted by #764 part 7: its module declares it)
                             # (DELETE /api/workload/ was lifted by #764 part 6: its module declares it)
                             ("GET", "/api/icons/*")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.declare(method, path)
        self.assertIn(("GET", "/api/test-free/*"), self.declare("GET", "/api/test-free/*"))

    def test_a_prefix_route_never_loosens_the_policys_role(self):         # security review 2
        import homestead_route_policy as POLICY
        ROUTES._table[("POST", "/api/t/*")] = ROUTES.Route("POST", "/api/t/*", "admin", lambda request: {}, "test")
        self.added.append(("POST", "/api/t/*"))
        with mock.patch.dict(POLICY.POLICY, {("POST", "/api/t/open"): "viewer", ("GET", "/api/t/locked"): "admin"}):
            self.assertEqual("admin", POLICY.role("/api/t/open", "POST"))
            self.assertEqual("admin", POLICY.role("/api/t/locked", "GET"))     # the policy's, stricter than viewer
        self.assertEqual("admin", POLICY.role("/api/t/other", "POST"))


class Connections(unittest.TestCase):
    def test_conn_headers_drop_secrets_in_any_case(self):               # D2 + Review Focus 4
        sent = [("Cookie", "homestead_session=s1"), ("cookie", "x=2"), ("authorization", "Bearer hs_k"),
                ("PROXY-AUTHORIZATION", "Basic eA=="), ("Cf-Access-Jwt-Assertion", "a.b.c"),
                ("X-Homestead-Fleet", "loft1"), ("X-Homestead-Fleet-Time", "1"), ("x-homestead-fleet-nonce", "n"),
                ("X-Homestead-Fleet-User", "ada"), ("X-Homestead-Fleet-Role", "admin"),
                ("X-Homestead-Fleet-Signature", "sig"), ("User-Agent", "UA"), ("Origin", "http://192.0.2.1")]
        handler = bare_handler(sent, _fleet_who=None)
        conn = ROUTES.connection(handler)
        gone = ("cookie", "authorization", "proxy-authorization", "cf-access-jwt-assertion", "x-homestead-fleet",
                "x-homestead-fleet-time", "x-homestead-fleet-nonce", "x-homestead-fleet-user",
                "x-homestead-fleet-role", "x-homestead-fleet-signature")
        for name in gone:
            for spelled in (name, name.upper(), name.title()):
                self.assertIsNone(conn.headers.get(spelled), spelled)
                self.assertNotIn(spelled, conn.headers)
                with self.assertRaises(KeyError):
                    conn.headers[spelled]
        self.assertEqual(set(), {n.lower() for n in conn.headers} & set(gone))
        self.assertFalse(any(secret in repr(conn.headers) for secret in ("s1", "hs_k", "eA==", "a.b.c", "sig")))
        self.assertEqual("UA", conn.headers.get("user-agent"))
        self.assertEqual("UA", conn.headers["USER-AGENT"])
        self.assertEqual("x", conn.headers.get("X-Missing", "x"))
        self.assertEqual(2, len(conn.headers))
        with self.assertRaises(TypeError):
            conn.headers["X-A"] = "1"

    def test_conn_headers_from_a_plain_dict(self):
        handler = bare_handler()
        handler.headers = {"Authorization": "Bearer x", "Accept": "text/html"}
        self.assertEqual(["Accept"], list(ROUTES.connection(handler).headers))

    def test_conn_cookies_are_homesteads_only(self):
        conn = ROUTES.connection(bare_handler([("Cookie", "homestead_session=s; CF_Authorization=j; other=1")]))
        self.assertEqual({"homestead_session": "s"}, dict(conn.cookies))
        with self.assertRaises(TypeError):
            conn.cookies["homestead_cluster"] = "x"

    def test_identities_come_from_the_guard(self):
        key = {"name": "ha", "kind": "key", "scopes": ["read"]}
        handler = bare_handler([("X-Forwarded-Proto", "https"), ("Cf-Ray", "1")],
                               _access={"email": "ada@example.com"}, _fleet_from="node1", api_key=key)
        conn = ROUTES.connection(handler)
        self.assertEqual(({"email": "ada@example.com"}, "node1", key), (conn.access, conn.fleet, conn.key))
        self.assertEqual((True, True), (conn.tls, conn.cloudflare))
        self.assertEqual(handler._who, conn.who)

    def test_a_signed_public_request_names_its_sender(self):
        handler = bare_handler([("X-Homestead-Fleet", "node1")], _fleet_who=False, _fleet_from=None)

        def identity():
            handler._fleet_who, handler._fleet_from = None, "node1"
        handler._fleet_identity = identity
        self.assertEqual("node1", ROUTES.connection(handler).fleet)

    def test_connection_on_a_bare_handler(self):     # Review Focus 5
        import server
        handler = object.__new__(server.H)
        handler.headers = {}
        conn = ROUTES.connection(handler)
        self.assertEqual((False, False, None, None, None, {}),
                         (conn.tls, conn.cloudflare, conn.access, conn.fleet, conn.key, dict(conn.cookies)))
        handler.headers = {"X-Forwarded-Proto": "https"}
        self.assertTrue(ROUTES.connection(handler).tls)

    def test_connection_on_a_handler_with_no_headers(self):
        import server
        conn = ROUTES.connection(object.__new__(server.H))
        self.assertEqual((0, {}, False), (len(conn.headers), dict(conn.cookies), conn.tls))

    def test_a_route_that_needs_conn_refuses_an_in_process_call(self):
        with self.assertRaises(PermissionError):
            ROUTES.connected(ROUTES.Request("GET", "/api/x", {}, None, "ada", "admin"))
        conn = ROUTES.connection(bare_handler())
        self.assertIs(conn, ROUTES.connected(ROUTES.Request("GET", "/api/x", {}, None, "ada", "admin", "", conn)))


class AccessIdentity(unittest.TestCase):                                  # security review 1
    def test_begin_forgets_the_previous_access_identity(self):
        seen = []
        handler = Answering.handler(self, "/api/test/access", {"ok": True})
        ROUTES._table[("POST", "/api/test/access")] = ROUTES.Route(
            "POST", "/api/test/access", "admin", lambda request: seen.append(request.conn.access) or {}, "test")
        handler._access = {"email": "ada@example.com"}
        handler.do_POST()
        self.assertEqual([None], seen)

    def test_a_kept_alive_request_does_not_keep_the_previous_access_identity(self):
        import threading
        from http.client import HTTPConnection
        import server
        import homestead_http as HTTP
        seen = []
        ROUTES.table()
        ROUTES._table[("GET", "/api/test/access")] = ROUTES.Route(
            "GET", "/api/test/access", "viewer", lambda request: seen.append(request.conn.access) or {}, "test")
        self.addCleanup(ROUTES._table.pop, ("GET", "/api/test/access"), None)
        self.enterContext(mock.patch.object(server.H, "_who", return_value={
            "user": "ada", "role": "viewer", "stale": False, "remember": False, "started": None}))
        self.enterContext(mock.patch.object(server.CFACCESS, "enabled", return_value=True))
        verify = self.enterContext(mock.patch.object(server.CFACCESS, "verify",
                                                     return_value={"email": "ada@example.com"}))
        listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        connection = HTTPConnection(*listener.server_address, timeout=3)
        self.addCleanup(connection.close)
        for headers in ({"Cf-Ray": "1", "Cf-Access-Jwt-Assertion": "a.b.c"}, {}):
            connection.request("GET", "/api/test/access", headers=headers)
            response = connection.getresponse()
            response.read()
            self.assertEqual(200, response.status)
        verify.assert_called_once_with("a.b.c")
        self.assertEqual([{"email": "ada@example.com"}, None], seen)


def answering(test, path, answer, guard_headers=(), module="test"):
    """server.H, with a route at path in module that returns answer."""
    import server
    handler = object.__new__(server.H)
    handler.path, handler.headers, handler.command = path, {}, "POST"
    handler._client_ip = lambda: "127.0.0.1"
    handler._send = mock.Mock()
    handler._send_stream = mock.Mock()
    handler.user, handler.role = "ada", "admin"
    handler._body = lambda: {}

    def guard(path):
        handler._extra_headers.extend(guard_headers)
        return False
    handler._guard = guard
    ROUTES.table()
    ROUTES._table[("POST", path)] = ROUTES.Route("POST", path, "admin", lambda request: answer, module)
    test.addCleanup(ROUTES._table.pop, ("POST", path), None)
    return handler


class Answering(unittest.TestCase):
    """A module's Reply is written through server.H.do_POST."""

    def handler(self, path, answer, guard_headers=()):
        return answering(self, path, answer, guard_headers)

    def test_reply_cookies_and_headers_are_written_after_the_guards(self):
        guards = ("Set-Cookie", "homestead_session=old; Path=/; HttpOnly; SameSite=Strict; Max-Age=60")
        handler = answering(self, "/api/test/reply", ROUTES.Reply(
            200, {"ok": True}, cookies=(C("homestead_cluster", "", max_age=0),), headers=(("Location", "/"),)),
            [guards], module="homestead_fleet_routes")
        handler.do_POST()
        self.assertEqual([guards, ("Set-Cookie", "homestead_cluster=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"),
                          ("Location", "/")], handler._extra_headers)
        self.assertEqual((200, {"ok": True}, "application/json"), handler._send.call_args.args)

    def test_a_refused_reply_is_a_500_and_carries_none_of_its_cookies(self):   # D4
        handler = answering(self, "/api/test/refused", ROUTES.Reply(        # its owner: the header alone is refused
            200, {}, cookies=(C("homestead_session", "ok", max_age=1),), headers=(("Set-Cookie", "x=1"),)),
            module="homestead_auth_routes")
        handler.do_POST()
        self.assertEqual(500, handler._send.call_args.args[0])
        self.assertFalse(any("homestead_session=ok" in value for _, value in handler._extra_headers))
        self.assertFalse(any(value == "x=1" for _, value in handler._extra_headers))

    def test_a_content_type_or_status_that_breaks_the_answer_is_refused(self):    # review I1
        for answer in (ROUTES.Reply(200, {}, "application/json\r\nSet-Cookie: CF_Authorization=x"),
                       ROUTES.Reply(200, {}, "text/plain\nAccess-Control-Allow-Origin: *"),
                       ROUTES.Reply(200, {}, "text/plain\x00"), ROUTES.Reply(200, {}, "json"),
                       ROUTES.Reply(200, {}, "text/plain; charset"), ROUTES.Raw(b"x", "image/png\r\nRefresh: 0"),
                       ROUTES.Stream(iter([b"a"]), "application/zip\r\nX: 1", "a.zip", 1),
                       ROUTES.Reply(101, {}), ROUTES.Reply(204, {}), ROUTES.Reply(304, {}), ROUTES.Reply(99, {}),
                       ROUTES.Reply(600, {}), ROUTES.Reply("200", {}), ROUTES.Reply(True, {})):
            with self.subTest(answer=answer):
                handler = self.handler("/api/test/broken", answer)
                handler.do_POST()
                self.assertEqual(500, handler._send.call_args.args[0])
                handler._send_stream.assert_not_called()
        for ctype in ("text/html; charset=utf-8", 'multipart/mixed; boundary="a b"', "image/svg+xml"):
            handler = self.handler("/api/test/fine", ROUTES.Reply(202, "x", ctype))
            handler.do_POST()
            self.assertEqual((202, "x", ctype), handler._send.call_args.args)

    def test_raw_stream_and_plain_answers_are_unchanged(self):
        handler = self.handler("/api/test/raw", ROUTES.Raw(b"PNG", "image/png"))
        handler.do_POST()
        self.assertEqual((200, b"PNG", "image/png"), handler._send.call_args.args)
        self.assertEqual([("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")], handler._extra_headers)
        stream = ROUTES.Stream(iter([b"a"]), "application/zip", "a.zip", 1)
        handler = self.handler("/api/test/stream", stream)
        handler.do_POST()
        handler._send_stream.assert_called_once_with(stream)
        handler._send.assert_not_called()
        handler = self.handler("/api/test/plain", {"ok": True})
        handler.do_POST()
        self.assertEqual((200, {"ok": True}), handler._send.call_args.args)
        self.assertEqual([], handler._extra_headers)

    def test_a_route_reads_its_connection(self):
        seen = []
        handler = self.handler("/api/test/conn", {"ok": True})
        ROUTES._table[("POST", "/api/test/conn")] = ROUTES.Route(
            "POST", "/api/test/conn", "admin", lambda request: seen.append(request) or {"ok": True}, "test")
        handler.headers = {"User-Agent": "UA", "Cookie": "homestead_session=s"}
        handler.do_POST()
        conn = ROUTES.connected(seen[0])
        self.assertEqual(("UA", None, {"homestead_session": "s"}),
                         (conn.headers.get("User-Agent"), conn.headers.get("Cookie"), dict(conn.cookies)))


# Each of Homestead's cookies, the one module that may set it, and one it sets (#936).
OWNED = (("homestead_session", "homestead_auth_routes", C("homestead_session", "t", max_age=60)),
         ("homestead_cluster", "homestead_fleet_routes", C("homestead_cluster", "node1", max_age=2592000)),
         ("homestead-data-move", "homestead_self_data_server",
          C("homestead-data-move", "t", "/api/self/data/handoff/", 86400)))


class CookieOwners(unittest.TestCase):
    """Each cookie is set only by the one module that owns it (#936)."""

    def send(self, cookie, module, tls=False):
        handler = answering(self, "/api/test/cookie", ROUTES.Reply(200, {"ok": True}, cookies=(cookie,)),
                            module=module)
        if tls:
            handler.headers = {"X-Forwarded-Proto": "https"}
        handler.do_POST()
        return handler

    def test_each_cookie_has_exactly_one_owner(self):
        self.assertEqual({name: module for name, module, _ in OWNED}, ROUTES.owners())
        self.assertEqual(set(ROUTES.COOKIES), set(ROUTES.owners()))

    def test_owners_are_read_once_with_the_table(self):                    # review I2
        ROUTES.table()
        with mock.patch.object(ROUTES.importlib, "import_module", side_effect=AssertionError("read again")):
            self.assertEqual({name: module for name, module, _ in OWNED}, ROUTES.owners())
            self.assertEqual(200, self.send(OWNED[0][2], OWNED[0][1])._send.call_args.args[0])

    def test_the_owner_sets_its_cookie_as_before(self):
        for name, module, cookie in OWNED:
            for tls in (False, True):
                with self.subTest(cookie=name, tls=tls):
                    handler = self.send(cookie, module, tls)
                    self.assertEqual((200, {"ok": True}, "application/json"), handler._send.call_args.args)
                    self.assertEqual([("Set-Cookie", ROUTES.cookie_header(cookie, tls))], handler._extra_headers)

    def test_a_cookie_from_another_module_is_refused(self):
        for name, owner, cookie in OWNED:
            for module in ["homestead_firewall", "test"] + [m for _, m, _ in OWNED if m != owner]:
                with self.subTest(cookie=name, module=module):
                    handler = self.send(cookie, module)
                    self.assertEqual(500, handler._send.call_args.args[0])
                    self.assertFalse(any(key == "Set-Cookie" for key, _ in handler._extra_headers))

    def test_the_refusal_names_the_cookie_its_owner_and_the_module(self):
        handler = answering(self, "/api/test/named", {})
        with self.assertRaises(ROUTES.Refused) as refused:
            ROUTES.answer(handler, ROUTES.Reply(200, {}, cookies=(C("homestead_session", "t"),)), "homestead_firewall")
        for word in ("homestead_session", "homestead_auth_routes", "homestead_firewall"):
            self.assertIn(word, str(refused.exception))
        handler._send.assert_not_called()

    def declare(self, **owned):
        """table() as it reads modules that each declare only the cookies they own."""
        modules = {}
        for index, (name, cookies) in enumerate(owned.items()):
            module = type(sys)(name)
            module.ROUTES = {("GET", f"/api/test-owner-{index}"): ("viewer", lambda request: {})}
            module.OWN_COOKIES = cookies
            modules[name] = module
        with mock.patch.dict(sys.modules, modules), mock.patch.object(ROUTES, "MODULES", tuple(modules)), \
                mock.patch.object(ROUTES, "_table", {}):
            return ROUTES.table()

    def test_two_modules_owning_one_cookie_are_refused_at_load(self):
        with self.assertRaises(ValueError) as refused:
            self.declare(homestead_test_a=("homestead_session",), homestead_test_b=("homestead_session",))
        for word in ("homestead_session", "homestead_test_a", "homestead_test_b"):
            self.assertIn(word, str(refused.exception))

    def test_a_module_may_own_only_homesteads_cookies(self):
        for cookies in (("CF_Authorization",), "homestead_session", ("homestead_session", "homestead_session"), None):
            with self.subTest(cookies=cookies), self.assertRaises(ValueError):
                self.declare(homestead_test_a=cookies)
        self.assertIn(("GET", "/api/test-owner-0"), self.declare(homestead_test_a=("homestead_cluster",)))


if __name__ == "__main__":
    unittest.main()
