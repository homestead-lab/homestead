"""The after-request hook point (#764): every answered request reaches it, with
its status and how long it took, after its slot is given back. A hook never
changes the answer, and never sees a body, a query, a cookie, a session or
what was answered unless it asks, by name, for the few fields it may have."""
import json
import queue
import socket
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_http as HTTP


def until(check, seconds=3):
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        time.sleep(0.01)
    return check()


class Recording(HTTP.LimitedHandler):
    """A LimitedHandler with no server.py: what its hook saw goes in seen."""
    protocol_version = "HTTP/1.1"
    seen = None
    hook = None
    release = None

    def log_message(self, *args):
        pass

    def after_request(self):
        type(self).hook(self)

    def do_GET(self):
        if self.path == "/slow":
            self.release.wait(5)
        if self.path == "/upgrade":
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.close_connection = True
            return
        if self.path == "/private":
            self.user, self.role = "ada", "admin"
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HookPoint(unittest.TestCase):
    def serve(self, handler=Recording, hook=None, **limits):
        Recording.seen = []
        Recording.release = threading.Event()
        self.addCleanup(Recording.release.set)
        Recording.hook = staticmethod(hook or (lambda h: Recording.seen.append(
            (h.path, h._status, h._started is not None, getattr(h, "user", None)))))
        listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), handler, **limits)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        return listener

    def client(self, listener):
        connection = HTTPConnection(*listener.server_address, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def raw(self, listener, request):
        with socket.create_connection(listener.server_address, timeout=3) as s:
            s.sendall(request)
            out = b""
            while chunk := s.recv(4096):
                out += chunk
        return out

    def test_status_and_time_are_recorded_for_every_answer(self):
        listener = self.serve(max_connections=1)
        connection = self.client(listener)
        connection.request("GET", "/")
        connection.getresponse().read()
        self.raw(listener, b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\nab")       # send_error 400
        self.raw(listener, b"GET /upgrade HTTP/1.1\r\nHost: x\r\n\r\n")                       # 101
        busy = threading.Thread(target=lambda: self.raw(listener, b"GET /slow HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"), daemon=True)
        busy.start()
        self.assertTrue(until(lambda: listener.active == 1))
        self.assertIn(b" 503 ", self.raw(listener, b"GET /refused HTTP/1.1\r\nHost: x\r\n\r\n"))   # the slot refusal
        Recording.release.set()
        busy.join(3)
        self.assertTrue(until(lambda: len(Recording.seen) == 5))
        self.assertEqual({("/", 200), ("/", 400), ("/upgrade", 101), ("/refused", 503), ("/slow", 200)},
                         {(path, status) for path, status, _, _ in Recording.seen})
        self.assertTrue(all(started for _, _, started, _ in Recording.seen))

    def test_no_hook_by_default(self):
        self.assertIsNone(HTTP.LimitedHandler.after_request)

        class Plain(Recording):
            after_request = None
        listener = self.serve(Plain)
        connection = self.client(listener)
        connection.request("GET", "/")
        self.assertEqual((200, b"ok"), (lambda r: (r.status, r.read()))(connection.getresponse()))
        self.assertEqual([], Recording.seen)

    def test_a_hook_that_raises_changes_nothing(self):
        def answered(hook):
            listener = self.serve(hook=hook)
            first = self.raw(listener, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            return [line for line in first.split(b"\r\n") if not line.startswith(b"Date:")]

        def boom(handler):
            raise RuntimeError("hook failed")
        self.assertEqual(answered(lambda h: None), answered(boom))
        listener = self.serve(hook=boom)
        connection = self.client(listener)
        for _ in range(2):      # the connection is kept, and its next request is answered
            connection.request("GET", "/")
            self.assertEqual(b"ok", connection.getresponse().read())

    def test_the_hook_runs_after_the_slot_is_given_back(self):
        listener = None
        active = []
        listener = self.serve(hook=lambda h: active.append(listener.active))
        connection = self.client(listener)
        connection.request("GET", "/")
        connection.getresponse().read()
        self.assertTrue(until(lambda: active))
        self.assertEqual([0], active)

    def test_a_kept_alive_public_request_has_no_previous_user(self):   # D6 + Review Focus 3
        listener = self.serve()
        connection = self.client(listener)
        for path in ("/private", "/public"):
            connection.request("GET", path)
            connection.getresponse().read()
        self.assertTrue(until(lambda: len(Recording.seen) == 2))
        self.assertEqual([("/private", "ada"), ("/public", None)], [(p, u) for p, _, _, u in Recording.seen])


class ThroughServer(unittest.TestCase):
    """server.H on a real socket: what a hook sees of sign-in, setup, a password
    change and a new API key."""

    @classmethod
    def setUpClass(cls):
        import homestead_audit as AUDIT_LOG
        import homestead_routes as ROUTES
        # Its own after-request queue, so its own hook worker: a hook another file left
        # running cannot hold this class's lines up (the audit's settings read waits
        # 5 s for DNS on CI). And audit.file as a failed read leaves it, on, without
        # the read: the hook path itself is the real one.
        cls.enterClassContext(mock.patch.object(ROUTES, "_queue", queue.Queue(ROUTES.HOOK_QUEUE)))
        cls.enterClassContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))

    def setUp(self):
        import server
        import homestead_routes as ROUTES
        self.server, self.ROUTES = server, ROUTES
        ROUTES.table()
        # Hooks the modules registered (the audit log) stay; this test's own go.
        self.addCleanup(ROUTES._hooks.__setitem__, slice(None), list(ROUTES._hooks))
        self.registered = list(ROUTES._hooks)
        self.seen = []
        listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        self.listener = listener
        self.who = self.enterContext(mock.patch.object(server.H, "_who", return_value={
            "user": "ada", "role": "admin", "stale": False, "remember": False, "started": None}))
        self.enterContext(mock.patch.object(server.H, "_signin", lambda *a, **k: None))
        self.enterContext(mock.patch.object(server.SIGNINS, "record", lambda *a, **k: None))
        self.enterContext(mock.patch.object(server.FLEET, "self_id", return_value="home"))

    def route(self, method, path, role, handler):
        self.ROUTES._table[(method, path)] = self.ROUTES.Route(method, path, role, handler, "test")
        self.addCleanup(self.ROUTES._table.pop, (method, path), None)

    def request(self, method, path, body=None, headers=()):
        client = HTTPConnection(*self.listener.server_address, timeout=3)
        try:
            sent = {"Content-Type": "application/json", "X-Homestead-Auth": "1", **dict(headers)}
            client.request(method, path, json.dumps(body) if body is not None else None, sent)
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def hook(self, fields=()):
        self.ROUTES.after(lambda request, status, ms: self.seen.append((request, status, ms)), fields=fields)

    def test_no_secret_reaches_a_hook_by_default(self):                 # addition 1
        S, AUTH = self.server, self.server.AUTH
        if hasattr(S, "SETUP_CODE"):     # with #765, setup checks its code first: the code is right here
            self.enterContext(mock.patch.object(S.SETUP_CODE, "first_admin", return_value="ada"))
        self.hook()
        secrets = ("Pw-0123456789", "SETUP-4F7Q-9KZD", "Old-pw-0123456", "New-pw-6543210", "hs_live_NEWKEYSECRET",
                   "sessiontoken123")
        with mock.patch.object(AUTH, "login", return_value="sessiontoken123"), \
                mock.patch.object(AUTH, "create_user"), mock.patch.object(AUTH, "change_password"), \
                mock.patch.object(AUTH, "issue_token", return_value="sessiontoken123"), \
                mock.patch.object(S.API_KEYS, "manage", return_value=(
                    {"ok": True, "secret": "hs_live_NEWKEYSECRET", "name": "ha"}, "key-made", "ha")):
            answers = [
                self.request("POST", "/api/auth/login?password=Pw-0123456789",
                             {"username": "ada", "password": "Pw-0123456789"}),
                self.request("POST", "/api/auth/setup", {"username": "ada", "password": "Pw-0123456789",
                                                         "setup_code": "SETUP-4F7Q-9KZD"}),
                self.request("POST", "/api/auth/password", {"old": "Old-pw-0123456", "new": "New-pw-6543210"},
                             {"Cookie": "homestead_session=sessiontoken123"}),
                self.request("POST", "/api/auth/keys", {"name": "ha", "scopes": ["read"]},
                             {"Cookie": "homestead_session=sessiontoken123",
                              "Proxy-Authorization": "Basic Pw-0123456789"}),
            ]
        self.assertEqual([200, 200, 200, 200], [code for code, _ in answers])
        self.assertIn(b"hs_live_NEWKEYSECRET", answers[3][1])      # the key was answered, to the client only
        self.assertTrue(until(lambda: len(self.seen) == 4))
        for request, status, ms in self.seen:
            self.assertIsNone(request.body)
            self.assertIsNone(request.query)
            self.assertEqual({}, dict(request.conn.cookies))
            self.assertIsNone(request.conn.who)
            self.assertIsInstance(ms, int)
            self.assertFalse([s for s in secrets if s in repr((request, status, ms))], request.path)
        self.assertEqual(["/api/auth/login", "/api/auth/setup", "/api/auth/password", "/api/auth/keys"],
                         [request.path for request, _, _ in self.seen])
        self.assertEqual([None, None, "ada", "ada"], [request.user for request, _, _ in self.seen])

    def test_a_hook_asks_for_named_fields_only(self):
        self.route("POST", "/api/test/hooked", "admin", lambda request: {"ok": True})
        self.hook(fields=("ns", "name"))
        self.assertEqual(200, self.request("POST", "/api/test/hooked", {
            "ns": "lab", "name": "web", "kind": "Deployment", "password": "x", "token": "y"})[0])
        self.assertTrue(until(lambda: self.seen))
        request, status, _ = self.seen[0]
        self.assertEqual(({"ns": "lab", "name": "web"}, 200, "POST"), (request.body, status, request.method))

    def test_asking_for_a_field_outside_the_allow_list_is_refused(self):
        for f in ("password", "setup_code", "old", "new", "token", "env", "secret"):
            with self.subTest(field=f), self.assertRaises(ValueError):
                self.ROUTES.after(lambda *a: None, fields=(f,))
        with self.assertRaises(ValueError):
            self.ROUTES.after(lambda *a: None, fields="name")
        self.assertEqual(self.registered, self.ROUTES._hooks)

    def test_a_hook_must_be_callable(self):                                  # review minor 5
        with self.assertRaises(TypeError):
            self.ROUTES.after("audit")
        self.assertEqual(self.registered, self.ROUTES._hooks)

    def test_a_hook_cannot_change_the_handlers_identities(self):              # review minor 9
        import time as clock
        handler = object.__new__(self.server.H)
        handler.headers, handler.command, handler.path = {}, "GET", "/api/x"
        handler._status, handler._started = 200, clock.monotonic()
        handler._access, handler.api_key = {"email": "ada@example.com"}, {"name": "ha", "kind": "key"}

        def meddle(request, status, ms):
            for identity in (request.conn.access, request.conn.key):
                try:
                    identity["name"] = "changed"
                except TypeError:
                    self.seen.append("refused")
        self.ROUTES.after(meddle)
        self.ROUTES.after_request(handler)
        self.assertTrue(until(lambda: len(self.seen) == 2))
        self.assertEqual(({"email": "ada@example.com"}, {"name": "ha", "kind": "key"}), (handler._access, handler.api_key))

    def test_a_field_with_an_unsafe_value_is_dropped(self):
        self.route("POST", "/api/test/hooked", "admin", lambda request: {"ok": True})
        self.hook(fields=("ns", "name", "kind"))
        for body in ({"name": "a b"}, {"name": {"x": 1}}, {"name": "-a"}, {"name": "a" * 254}, {"ns": 3}):
            self.request("POST", "/api/test/hooked", body)
        self.request("POST", "/api/test/hooked", ["not", "an", "object"])
        self.assertTrue(until(lambda: len(self.seen) == 6))
        self.assertEqual([{}] * 6, [request.body for request, _, _ in self.seen])

    def test_a_hook_never_reads_an_unread_body(self):
        import time as clock
        S = self.server
        handler = object.__new__(S.H)
        handler.headers, handler.command, handler.path = {"Content-Length": "40"}, "POST", "/api/test/hooked"
        handler._raw, handler._status, handler._started = None, 401, clock.monotonic()
        handler.rfile = mock.Mock()
        self.hook(fields=("name",))
        self.ROUTES.after_request(handler)
        self.assertTrue(until(lambda: self.seen))
        handler.rfile.read.assert_not_called()
        self.assertEqual(({}, 401), (self.seen[0][0].body, self.seen[0][1]))
        # and through the server: a request refused before its body is read
        self.seen.clear()
        self.who.return_value = None
        self.assertEqual(401, self.request("POST", "/api/test/hooked", {"name": "web"})[0])
        self.assertTrue(until(lambda: self.seen))
        self.assertEqual(({}, 401), (self.seen[0][0].body, self.seen[0][1]))

    def test_the_hook_sees_guard_refusals_and_server_py_branches(self):
        self.route("POST", "/api/test/admin-only", "admin", lambda request: {"ok": True})
        self.hook()
        self.who.return_value = None
        self.assertEqual(401, self.request("POST", "/api/test/admin-only", {})[0])
        self.who.return_value = {"user": "bo", "role": "viewer", "stale": False, "remember": False, "started": None}
        self.assertEqual(403, self.request("POST", "/api/test/admin-only", {})[0])
        self.assertEqual(200, self.request("GET", "/healthz")[0])
        self.assertTrue(until(lambda: len(self.seen) == 3))
        self.assertEqual([("POST", "/api/test/admin-only", 401, None), ("POST", "/api/test/admin-only", 403, "bo"),
                          ("GET", "/healthz", 200, None)],
                         [(r.method, r.path, status, r.user) for r, status, _ in self.seen])

    def test_a_hung_hook_holds_neither_the_answer_nor_the_connection(self):    # review I3
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        self.ROUTES.after(lambda request, status, ms: stuck.wait(10))
        self.route("GET", "/api/test/hooked", "viewer", lambda request: {"ok": True})
        started = time.monotonic()
        with socket.create_connection(self.listener.server_address, timeout=5) as s:
            s.sendall(b"GET /api/test/hooked HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            answer = b""
            while chunk := s.recv(4096):
                answer += chunk
        self.assertIn(b" 200 ", answer)
        self.assertLess(time.monotonic() - started, 2, "the client waited for the hook to see the end of the answer")
        self.assertTrue(until(lambda: self.listener.open == 0), "the connection is still counted while the hook runs")
        self.assertFalse(stuck.is_set())

    def test_hooks_that_fall_behind_are_dropped_and_counted(self):            # review I3
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        self.ROUTES.after(lambda request, status, ms: stuck.wait(10))
        before = self.ROUTES.hooks_dropped()
        with mock.patch.object(self.ROUTES, "_queue", __import__("queue").Queue(1)):
            self.ROUTES._ensure_worker()
            for _ in range(4):
                self.request("GET", "/healthz")
            self.assertTrue(until(lambda: self.ROUTES.hooks_dropped() > before))

    def test_a_malformed_request_still_reaches_the_hook(self):
        self.hook()
        with socket.create_connection(self.listener.server_address, timeout=3) as s:
            s.sendall(b"GET /\x00 nonsense HTTP/1.1 extra\r\n\r\n")
            self.assertIn(b" 400 ", s.recv(4096))
        self.assertTrue(until(lambda: self.seen))
        self.assertEqual(400, self.seen[0][1])

    def test_a_hook_sees_who_the_guard_found_without_the_secrets(self):
        self.route("GET", "/api/test/seen", "viewer", lambda request: {"ok": True})
        self.hook()
        self.request("GET", "/api/test/seen?q=1", headers={"User-Agent": "UA", "X-Forwarded-Proto": "https",
                                                            "Cookie": "homestead_session=sessiontoken123"})
        self.assertTrue(until(lambda: self.seen))
        request, _, _ = self.seen[0]
        self.assertEqual(("UA", None, True, "127.0.0.1", "/api/test/seen"),
                         (request.conn.headers.get("User-Agent"), request.conn.headers.get("Cookie"),
                          request.conn.tls, request.addr, request.path))



class DeclaredNotes(unittest.TestCase):
    """What a request notes for its hooks (#738): only keys a hook declared,
    short names or numbers, cleared at every request's start."""

    def setUp(self):
        import homestead_routes as ROUTES
        self.ROUTES = ROUTES
        ROUTES.table()
        self.addCleanup(ROUTES._hooks.__setitem__, slice(None), list(ROUTES._hooks))
        self.addCleanup(ROUTES._note_keys.update, dict(ROUTES._note_keys))
        self.addCleanup(ROUTES._clear_notes)
        ROUTES._clear_notes()
        self.fn = lambda request, status, ms, notes=None: None
        ROUTES.after(self.fn, notes=("to", "count"))

    def test_only_declared_keys_may_be_noted(self):
        self.ROUTES.note("to", "member-2")
        for key in ("password", "body", "setup_code", ""):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.ROUTES.note(key, "x")
        self.assertEqual({"to": "member-2"}, self.ROUTES._take_notes())

    def test_values_are_short_names_or_numbers(self):
        self.ROUTES.note("to", "a" * 500)
        self.assertEqual("a" * self.ROUTES.NOTE_CAP, self.ROUTES._take_notes()["to"])
        self.ROUTES.note("count", 3)
        self.assertEqual({"count": 3}, self.ROUTES._take_notes())
        for value in ("a b", "Bearer hsk_x", "-x", "", None, True, 1.5, {"a": 1}, ["x"], b"x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ROUTES.note("to", value)

    def test_declared_keys_are_names(self):
        for notes in ("to", ("bad key",), ("",), (3,)):
            with self.subTest(notes=notes), self.assertRaises(ValueError):
                self.ROUTES.after(lambda *a, **k: None, notes=notes)

    def test_a_request_start_clears_what_the_last_one_noted(self):
        self.ROUTES.note("to", "member-2")
        for reset in HTTP.REQUEST_STARTED:
            reset()
        self.assertEqual({}, self.ROUTES._take_notes())


class NotesThroughServer(ThroughServer):
    """Notes reach the hooks' thread, only the declared ones, and never the next request."""

    def run(self, result=None):         # only this class's tests, not ThroughServer's again
        if not self._testMethodName.startswith("test_notes"):
            return result
        return super().run(result)

    def noting(self, declared=("to",)):
        self.ROUTES.after(lambda request, status, ms, notes=None: self.seen.append((request.path, notes)),
                          notes=declared)
        self.ROUTES.after(lambda request, status, ms: self.seen.append((request.path, "undeclared")))
        self.route("POST", "/api/test/noting", "admin", lambda request: self.ROUTES.note("to", "member-2") or {})
        self.route("POST", "/api/test/plain", "admin", lambda request: {"ok": True})

    def kept_alive(self, *paths):
        client = HTTPConnection(*self.listener.server_address, timeout=3)
        self.addCleanup(client.close)
        for path in paths:
            client.request("POST", path, "{}", {"Content-Type": "application/json", "X-Homestead-Auth": "1"})
            client.getresponse().read()

    def test_notes_reach_only_the_hooks_that_declared_them(self):
        self.noting()
        self.kept_alive("/api/test/noting")
        self.assertTrue(until(lambda: len(self.seen) == 2))
        self.assertEqual({("/api/test/noting", "undeclared")}, {s for s in self.seen if s[1] == "undeclared"})
        self.assertIn(("/api/test/noting", {"to": "member-2"}), self.seen)

    def test_notes_never_carry_over_on_a_kept_alive_connection(self):
        """Even when the hook point failed for the request that noted them."""
        self.noting()
        real, failed = self.ROUTES._read_only, []

        def once(value):
            if not failed:
                failed.append(1)
                raise RuntimeError("the hook point failed for this request")
            return real(value)
        self.enterContext(mock.patch.object(self.ROUTES, "_read_only", side_effect=once))
        self.kept_alive("/api/test/noting", "/api/test/plain")
        self.assertTrue(until(lambda: len(self.seen) == 2))
        self.assertEqual([("/api/test/plain", {})], [s for s in self.seen if s[1] != "undeclared"])


if __name__ == "__main__":
    unittest.main()
