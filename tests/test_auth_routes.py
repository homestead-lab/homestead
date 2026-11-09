"""Sign-in, sessions, API keys and accounts, through server.py's real handler (#764).

These pin what each route answers today - the status line, the body, every
Set-Cookie line with its flags, and the sign-in history row - through the real
guard and the real response writer, so a route can move to another module and
be shown to answer byte for byte the same. Nothing here patches the handler
itself: only the account store, the setup code and the key store underneath.
"""
import http.client
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as ALERTS
import homestead_api_keys as API_KEYS
import homestead_auth as AUTH
import homestead_fleet as FLEET
import homestead_setup_code as SETUP_CODE
import homestead_signins as SIGNINS
import server

UA = "curl/8.4.0"
ADDR = "192.0.2.50"
IDLE = AUTH.idle_ttl(False)
KEPT = AUTH.idle_ttl(True)


def session(user="ada", role="viewer", stale=False, remember=False):
    return {"user": user, "role": role, "remember": remember, "started": 1700000000,
            "expires": 1700043200, "stale": stale}


def cookie(value, age, tls=False):
    return f"homestead_session={value}; Path=/; HttpOnly; SameSite=Strict{'; Secure' if tls else ''}; Max-Age={age}"


class Answer:
    """One HTTP answer as the client reads it."""

    def __init__(self, raw):
        head, _, self.raw_body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split(" ")[1])
        self.headers = [tuple(line.split(": ", 1)) for line in lines[1:]]
        self.body = json.loads(self.raw_body) if self.header("Content-Type") == "application/json" else self.raw_body

    def header(self, name):
        found = [v for k, v in self.headers if k.lower() == name.lower()]
        return found[0] if found else None

    @property
    def cookies(self):
        return [v for k, v in self.headers if k.lower() == "set-cookie"]


class Routes(unittest.TestCase):
    """Drives one request at a time through server.H, with the account store
    underneath replaced by named sessions and recorders."""

    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.addCleanup(SIGNINS.bind, SIGNINS.DATA_DIR)
        SIGNINS.bind(self.dir)
        self.sessions = {}
        self.issued = []

        def issue(user, remember=False, started=None, identity=None):
            self.issued.append((user, remember, started))
            return f"tok{len(self.issued)}"

        self.enterContext(mock.patch.object(AUTH, "verify_token", lambda token, force=False: self.sessions.get(token)))
        self.enterContext(mock.patch.object(AUTH, "issue_token", issue))
        self.enterContext(mock.patch.object(AUTH, "needs_setup", lambda: False))
        self.enterContext(mock.patch.object(FLEET, "members", lambda: [{"id": "m1", "name": "Office", "handle": "office"}]))

    def send(self, method, path, body=None, *, token=None, tls=False, headers=None, raw=None):
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        sent = {"Host": "192.0.2.1", "User-Agent": UA, "Content-Type": "application/json",
                "X-Homestead-Auth": "1", "Content-Length": str(len(data))}
        if token:
            sent["Cookie"] = f"homestead_session={token}"
        if tls:
            sent["X-Forwarded-Proto"] = "https"
        sent.update(headers or {})
        text = "".join(f"{k}: {v}\r\n" for k, v in sent.items() if v is not None) + "\r\n"
        h = object.__new__(server.H)
        h.path, h.command = path, method
        h.request_version, h.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
        h.headers = http.client.parse_headers(io.BytesIO(text.encode("latin-1")))
        h.rfile, h.wfile = io.BytesIO(data), io.BytesIO()
        h.client_address, h.connection, h.close_connection = (ADDR, 40000), None, False
        getattr(h, f"do_{method}")()
        return Answer(h.wfile.getvalue())

    def signed_in(self, user="ada", role="viewer", **kw):
        token = f"s-{user}"
        self.sessions[token] = session(user, role, **kw)
        return token

    def relayed(self, user="fleet:office:ada", role="admin"):
        """A request a linked Homestead signed for someone signed in there."""
        found = {"user": user, "role": role, "sender": {"id": "m1", "name": "Office", "handle": "office"}}
        self.enterContext(mock.patch.object(FLEET, "verify", lambda *a, **k: found))
        return {FLEET.H_FROM: "m1"}

    def history(self):
        return [{k: v for k, v in row.items() if k != "at"} for row in SIGNINS.history()]

    def row(self, event, user, ok=True, detail="", via=""):
        return {"event": event, "user": user, "ok": ok, "ip": ADDR, "device": "curl", "via": via, "detail": detail}


class SignInAndSessions(Routes):
    """PR 2's routes: GET state and dashboard preferences; POST setup, login,
    logout, preferences, password and signout-everywhere."""

    def test_login_sets_the_session_cookie(self):
        for tls in (False, True):
            for remember, age, detail in ((False, IDLE, ""), (True, KEPT, "kept signed in")):
                with self.subTest(tls=tls, remember=remember), \
                        mock.patch.object(AUTH, "login", return_value="tok-in") as login:
                    a = self.send("POST", "/api/auth/login", {"username": " Ada ", "password": "Pw-0123456789",
                                                               "remember": remember}, tls=tls)
                    login.assert_called_once_with(" Ada ", "Pw-0123456789", ADDR, remember)
                    self.assertEqual(200, a.status)
                    self.assertEqual({"ok": True, "remember": remember, "user": "ada"}, a.body)
                    self.assertEqual([cookie("tok-in", age, tls)], a.cookies)
                    self.assertEqual("no-store", a.header("Cache-Control"))
                    self.assertEqual(self.row("signin", "ada", detail=detail), self.history()[0])

    def test_failed_and_blocked_logins_are_recorded_without_the_password(self):
        for message, event in (("incorrect username or password", "signin-failed"),
                               ("too many attempts — wait a few minutes", "signin-blocked")):
            with self.subTest(event=event), mock.patch.object(AUTH, "login", side_effect=PermissionError(message)):
                a = self.send("POST", "/api/auth/login", {"username": "Robin", "password": "hunter2-secret"})
                self.assertEqual((401, {"error": message}, []), (a.status, a.body, a.cookies))
                self.assertEqual(self.row(event, "robin", ok=False, detail=message), self.history()[0])
        self.assertNotIn("hunter2", (Path(self.dir) / SIGNINS.FILE).read_text())

    def test_a_login_through_cloudflare_says_so_in_the_history(self):
        with mock.patch.object(AUTH, "login", return_value="tok-in"):
            a = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"}, headers={"Cf-Ray": "r1"})
        self.assertEqual(200, a.status)
        self.assertEqual(self.row("signin", "ada", via="Cloudflare"), self.history()[0])

    def access(self):
        """Cloudflare Access set up, and the request signed by it: then, and only
        then, Cloudflare's Cf-Connecting-Ip is the address that signs in."""
        self.enterContext(mock.patch.object(server.CFACCESS, "enabled", lambda: True))
        self.enterContext(mock.patch.object(server.CFACCESS, "verify", lambda *a, **k: {"email": "ada@example.com"}))
        self.enterContext(mock.patch.object(AUTH, "_attempts", {}))
        return {"Cf-Ray": "r1", "Cf-Connecting-Ip": "203.0.113.9", "Cf-Access-Jwt-Assertion": "a.b.c"}

    def test_through_access_the_sign_in_is_from_cloudflares_address(self):
        headers = self.access()
        with mock.patch.object(AUTH, "login", return_value="tok-in") as login:
            a = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"}, headers=headers)
        login.assert_called_once_with("ada", "p", "203.0.113.9", False)
        self.assertEqual(200, a.status)
        self.assertEqual({**self.row("signin", "ada", via="Cloudflare"), "ip": "203.0.113.9"}, self.history()[0])
        with mock.patch.object(SETUP_CODE, "first_admin") as first:
            a = self.send("POST", "/api/auth/setup", {"username": "ada", "password": "p"}, headers=headers)
        self.assertEqual(403, a.status)
        first.assert_not_called()

    def test_the_sign_in_limit_counts_the_address_that_signs_in(self):
        """The real limiter, not a mocked message: a full count for an address
        blocks it before any password work; through Access it is Cloudflare's
        address that is counted, not the proxy's."""
        headers = self.access()
        now = time.time()
        AUTH._attempts["ip:203.0.113.9"] = [now] * AUTH.MAX_ATTEMPTS
        with mock.patch.object(AUTH, "_load", side_effect=AssertionError("no store read when blocked")):
            a = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"}, headers=headers)
        self.assertEqual((401, {"error": "too many attempts — wait a few minutes"}, []), (a.status, a.body, a.cookies))
        self.assertEqual({**self.row("signin-blocked", "ada", ok=False, detail="too many attempts — wait a few minutes",
                                     via="Cloudflare"), "ip": "203.0.113.9"}, self.history()[0])
        AUTH._attempts.clear()
        AUTH._attempts[f"ip:{ADDR}"] = [now] * AUTH.MAX_ATTEMPTS
        with mock.patch.object(AUTH, "_shared_attempt", lambda *a, **k: None), \
                mock.patch.object(AUTH, "_load", lambda *a, **k: {"users": {}}), \
                mock.patch.object(AUTH, "ITERATIONS", 1000):
            a = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"}, headers=headers)
            self.assertEqual((401, {"error": "incorrect username or password"}), (a.status, a.body))
            self.assertIn("ip:203.0.113.9", AUTH._attempts)
            a = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"})
            self.assertEqual((401, {"error": "too many attempts — wait a few minutes"}), (a.status, a.body))

    def test_the_guard_refuses_a_login_before_the_route(self):
        cases = ((403, {"error": "missing X-Homestead-Auth header"}, {"X-Homestead-Auth": None}),
                 (415, {"error": "application/json required"}, {"Content-Type": "text/plain"}),
                 (403, {"error": "request origin rejected"}, {"Origin": "http://198.51.100.9"}))
        for path in ("/api/auth/login", "/api/auth/setup"):
            for status, body, headers in cases:
                with self.subTest(path=path, status=status, headers=headers), \
                        mock.patch.object(AUTH, "login") as login, \
                        mock.patch.object(SETUP_CODE, "first_admin") as first:
                    a = self.send("POST", path, {"username": "ada", "password": "p"}, headers=headers)
                    self.assertEqual((status, body, []), (a.status, a.body, a.cookies))
                    login.assert_not_called()
                    first.assert_not_called()
        self.assertEqual([], self.history())

    def test_a_cross_origin_login_is_still_refused(self):
        with mock.patch.object(AUTH, "login") as login:
            a = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"},
                          headers={"Origin": "http://198.51.100.9"})
        self.assertEqual((403, {"error": "request origin rejected"}), (a.status, a.body))
        login.assert_not_called()
        with mock.patch.object(AUTH, "login", return_value="tok-in"):
            same = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"},
                             headers={"Origin": "http://192.0.2.1"})
        self.assertEqual(200, same.status)

    def test_a_login_body_that_is_not_an_object_is_400(self):
        with mock.patch.object(AUTH, "login") as login:
            a = self.send("POST", "/api/auth/login", raw=b"[1]")
        self.assertEqual((400, {"error": "a JSON object is required"}), (a.status, a.body))
        login.assert_not_called()

    def test_setup_makes_the_first_admin_and_signs_them_in(self):
        for tls in (False, True):
            for remember, age in ((False, IDLE), (True, KEPT)):
                self.issued.clear()
                body = {"username": "Ada", "password": "Pw-0123456789", "setup_code": "SETUP-4F7Q-9KZD",
                        "remember": remember}
                with self.subTest(tls=tls, remember=remember), \
                        mock.patch.object(SETUP_CODE, "first_admin", return_value="ada") as first:
                    a = self.send("POST", "/api/auth/setup", body, tls=tls)
                    first.assert_called_once_with(body, ADDR, UA)
                    self.assertEqual((200, {"ok": True, "user": "Ada"}), (a.status, a.body))
                    self.assertEqual([("ada", remember, None)], self.issued)
                    self.assertEqual([cookie("tok1", age, tls)], a.cookies)
                    self.assertEqual(self.row("setup", "ada", detail="the first administrator"), self.history()[0])
        self.assertNotIn("SETUP-4F7Q", (Path(self.dir) / SIGNINS.FILE).read_text())

    def test_setup_is_refused_through_the_tunnel(self):
        with mock.patch.object(SETUP_CODE, "first_admin") as first:
            a = self.send("POST", "/api/auth/setup", {"username": "ada", "password": "p"}, headers={"Cf-Ray": "r1"})
        self.assertEqual((403, {"error": "Finish setting Homestead up from the LAN. "
                                         "setup is not offered through the tunnel"}, []), (a.status, a.body, a.cookies))
        first.assert_not_called()
        self.assertEqual([], self.history())

    def test_a_refused_setup_sets_no_cookie(self):
        for error, status in ((PermissionError("the setup code is not right"), 403),
                              (ValueError("password too short"), 400)):
            with self.subTest(status=status), mock.patch.object(SETUP_CODE, "first_admin", side_effect=error):
                a = self.send("POST", "/api/auth/setup", {"username": "ada", "password": "p", "setup_code": "x"})
                self.assertEqual((status, {"error": str(error)}, []), (a.status, a.body, a.cookies))
        self.assertEqual([], self.issued)

    def test_logout_and_signout_everywhere_clear_the_cookie(self):
        for tls in (False, True):
            with self.subTest(tls=tls):
                token = self.signed_in()
                a = self.send("POST", "/api/auth/logout", {}, token=token, tls=tls)
                self.assertEqual((200, {"ok": True}, [cookie("", 0, tls)]), (a.status, a.body, a.cookies))
                self.assertEqual(self.row("signout", "ada"), self.history()[0])
                with mock.patch.object(AUTH, "logout_everywhere") as everywhere:
                    a = self.send("POST", "/api/auth/signout-everywhere", {}, token=token, tls=tls)
                everywhere.assert_called_once_with("ada")
                self.assertEqual((200, {"ok": True}, [cookie("", 0, tls)]), (a.status, a.body, a.cookies))
                self.assertEqual(self.row("signout-everywhere", "ada"), self.history()[0])

    def test_session_routes_need_someone_signed_in(self):
        for method, path in (("POST", "/api/auth/logout"), ("POST", "/api/auth/signout-everywhere"),
                             ("POST", "/api/auth/password"), ("POST", "/api/auth/preferences/dashboard"),
                             ("GET", "/api/auth/preferences/dashboard")):
            with self.subTest(path=path):
                a = self.send(method, path, {} if method == "POST" else None, token="nobody")
                self.assertEqual((401, {"error": "not signed in", "auth": False}, []), (a.status, a.body, a.cookies))
                a = self.send(method, path, {} if method == "POST" else None, token=self.signed_in(),
                              headers={"X-Homestead-Auth": None}) if method == "POST" else None
                if a:
                    self.assertEqual((403, {"error": "missing X-Homestead-Auth header"}), (a.status, a.body))
        self.assertEqual([], self.history())

    def test_password_change_reissues_for_session_ttl(self):
        for tls in (False, True):
            self.issued.clear()
            with self.subTest(tls=tls), mock.patch.object(AUTH, "change_password") as change:
                a = self.send("POST", "/api/auth/password", {"old": "Old-pw-0123456", "new": "New-pw-6543210"},
                              token=self.signed_in(), tls=tls)
                change.assert_called_once_with("ada", "Old-pw-0123456", "New-pw-6543210")
                self.assertEqual((200, {"ok": True}), (a.status, a.body))
                self.assertEqual([("ada", False, None)], self.issued)
                self.assertEqual([cookie("tok1", AUTH.SESSION_TTL, tls)], a.cookies)
                self.assertEqual(self.row("password", "ada"), self.history()[0])
        self.assertNotIn("pw-", (Path(self.dir) / SIGNINS.FILE).read_text())

    def test_a_refused_password_change_is_recorded_and_answered(self):
        for error, status in ((PermissionError("current password is wrong"), 403),
                              (ValueError("password must be at least 12 characters"), 400)):
            with self.subTest(status=status), mock.patch.object(AUTH, "change_password", side_effect=error):
                a = self.send("POST", "/api/auth/password", {"old": "a", "new": "b"}, token=self.signed_in())
                self.assertEqual((status, {"error": str(error)}, []), (a.status, a.body, a.cookies))
                self.assertEqual(self.row("password-failed", "ada", ok=False, detail=str(error)), self.history()[0])
        self.assertEqual([], self.issued)

    def test_a_refreshed_session_cookie_comes_before_the_routes_own(self):
        token = self.signed_in(stale=True, remember=True)
        with mock.patch.object(AUTH, "change_password"):
            a = self.send("POST", "/api/auth/password", {"old": "a", "new": "b"}, token=token)
        self.assertEqual(200, a.status)
        self.assertEqual([("ada", True, 1700000000), ("ada", False, None)], self.issued)
        self.assertEqual([cookie("tok1", KEPT), cookie("tok2", AUTH.SESSION_TTL)], a.cookies)
        a = self.send("POST", "/api/auth/logout", {}, token=token, tls=True)
        self.assertEqual([cookie("tok3", KEPT, True), cookie("", 0, True)], a.cookies)

    def test_state_for_nobody_a_session_and_a_relayed_person(self):
        fixed = {"session_max_days": AUTH.ABSOLUTE_TTL // 86400, "roles": list(AUTH.ROLES)}
        nobody = {"setup": False, "via": "", "user": None, "role": None, "remember": False,
                  "session_expires": None, "session_started": None, "default_namespace": None, **fixed}
        a = self.send("GET", "/api/auth/state")
        self.assertEqual((200, nobody, []), (a.status, a.body, a.cookies))
        with mock.patch.object(AUTH, "needs_setup", lambda: True):
            self.assertEqual({**nobody, "setup": True}, self.send("GET", "/api/auth/state").body)
        # A stale session is not refreshed here: the guard lets state through before it reads one.
        a = self.send("GET", "/api/auth/state", token=self.signed_in(role="operator", stale=True, remember=True))
        self.assertEqual((200, {**nobody, "user": "ada", "role": "operator", "remember": True,
                                "session_expires": 1700043200, "session_started": 1700000000,
                                "default_namespace": server.DEFAULT_NS}, []), (a.status, a.body, a.cookies))
        with mock.patch.object(AUTH, "needs_setup", lambda: True):
            a = self.send("GET", "/api/auth/state", headers=self.relayed(role="viewer"))
        self.assertEqual((200, {**nobody, "setup": False, "via": "Office", "user": "fleet:office:ada",
                                "role": "viewer", "default_namespace": server.DEFAULT_NS}),
                         (a.status, a.body))
        self.assertEqual([], self.history())

    def test_preferences_read_and_save_for_the_person_signed_in(self):
        saved = {"cards": ["nodes"], "updated": 1}
        with mock.patch.object(AUTH, "dashboard_preferences", return_value=saved) as read:
            a = self.send("GET", "/api/auth/preferences/dashboard?user=bob", token=self.signed_in())
        read.assert_called_once_with("ada")
        self.assertEqual((200, saved, []), (a.status, a.body, a.cookies))
        with mock.patch.object(AUTH, "save_dashboard_preferences", return_value=saved) as save:
            a = self.send("POST", "/api/auth/preferences/dashboard?user=bob", {"cards": ["nodes"]}, token=self.signed_in())
        save.assert_called_once_with("ada", {"cards": ["nodes"]})
        self.assertEqual((200, saved), (a.status, a.body))

    def test_preferences_conflict_is_409(self):
        with mock.patch.object(AUTH, "save_dashboard_preferences",
                               side_effect=AUTH.StoreConflict("changed elsewhere; reload")):
            a = self.send("POST", "/api/auth/preferences/dashboard", {"cards": []}, token=self.signed_in())
        self.assertEqual((409, {"error": "changed elsewhere; reload"}), (a.status, a.body))
        with mock.patch.object(AUTH, "save_dashboard_preferences", side_effect=ValueError("cards must be a list")):
            a = self.send("POST", "/api/auth/preferences/dashboard", {"cards": 1}, token=self.signed_in())
        self.assertEqual((400, {"error": "cards must be a list"}), (a.status, a.body))

    def test_an_unreadable_account_store_is_503(self):
        failure = AUTH.StoreUnavailable("the cluster did not answer")
        with mock.patch.object(AUTH, "login", side_effect=failure):
            a = self.send("POST", "/api/auth/login", {"username": "ada", "password": "p"})
        self.assertEqual(503, a.status)
        self.assertEqual({"error": "the cluster did not answer", "cause": getattr(failure, "cause", ""),
                          "unavailable": True}, a.body)

    def test_the_wrong_method_answers_as_today(self):
        token = self.signed_in(role="admin")
        none = (404, {"error": "no route"})
        undeclared = (403, {"error": "this route has no declared authorization policy"})
        for method, path, answer in (("GET", "/api/auth/login", none), ("GET", "/api/auth/setup", none),
                                     ("GET", "/api/auth/logout", undeclared), ("GET", "/api/auth/password", undeclared),
                                     ("GET", "/api/auth/signout-everywhere", undeclared),
                                     ("POST", "/api/auth/state", none)):
            with self.subTest(method=method, path=path):
                a = self.send(method, path, {} if method == "POST" else None, token=token)
                self.assertEqual((*answer, []), (a.status, a.body, a.cookies))


class KeysAndAccounts(Routes):
    """PR 3's routes: POST keys, keys/revoke, keys/edit, users, role and
    users/delete, all for an administrator."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(AUTH, "list_users", lambda: [{"username": "robin"}]))

    def test_key_routes_answer_and_record_for_an_admin(self):
        made = {"secret": "hs_live_NEWKEYSECRET", "key": {"id": "k1", "name": "ha", "scopes": ["read", "metrics"]}}
        changed = {"key": {"id": "k1", "name": "ha"}, "changed": ["scopes"]}
        with mock.patch.object(API_KEYS, "create", return_value=made) as create, \
                mock.patch.object(API_KEYS, "revoke", return_value={"ok": True, "name": "ha"}) as revoke, \
                mock.patch.object(API_KEYS, "edit", return_value=changed) as edit:
            token = self.signed_in(role="admin")
            a = self.send("POST", "/api/auth/keys", {"name": "ha", "scopes": ["read", "metrics"]}, token=token)
            create.assert_called_once_with("ha", ["read", "metrics"], None, None, "ada", False)
            self.assertEqual((200, {"ok": True, **made}, []), (a.status, a.body, a.cookies))
            self.assertEqual(self.row("key-added", "ada", detail="ha: read, metrics"), self.history()[0])
            a = self.send("POST", "/api/auth/keys/edit", {"id": "k1", "scopes": ["read"], "other": 1}, token=token)
            edit.assert_called_once_with("k1", {"scopes": ["read"]}, "ada")
            self.assertEqual((200, {"ok": True, **changed}), (a.status, a.body))
            self.assertEqual(self.row("key-changed", "ada", detail="ha: scopes changed"), self.history()[0])
            a = self.send("POST", "/api/auth/keys/revoke", {"id": "k1"}, token=token)
            revoke.assert_called_once_with("k1")
            self.assertEqual((200, {"ok": True, "name": "ha"}), (a.status, a.body))
            self.assertEqual(self.row("key-revoked", "ada", detail="ha"), self.history()[0])

    def test_making_a_key_records_its_name_and_scopes_not_its_secret(self):
        made = {"secret": "hs_live_NEWKEYSECRET", "key": {"id": "k1", "name": "ha", "scopes": ["read"]}}
        with mock.patch.object(API_KEYS, "create", return_value=made):
            self.send("POST", "/api/auth/keys", {"name": "ha", "scopes": ["read"]}, token=self.signed_in(role="admin"))
        self.assertNotIn("NEWKEYSECRET", (Path(self.dir) / SIGNINS.FILE).read_text())

    def test_a_refused_key_change_is_answered(self):
        with mock.patch.object(API_KEYS, "revoke", side_effect=ValueError("no such key")):
            a = self.send("POST", "/api/auth/keys/revoke", {"id": "k9"}, token=self.signed_in(role="admin"))
        self.assertEqual((400, {"error": "no such key"}), (a.status, a.body))
        self.assertEqual([], self.history())

    def test_keys_are_refused_when_relayed(self):
        headers = self.relayed()
        with mock.patch.object(API_KEYS, "manage") as manage:
            for path in ("/api/auth/keys", "/api/auth/keys/revoke", "/api/auth/keys/edit"):
                with self.subTest(path=path):
                    a = self.send("POST", path, {"id": "k1"}, headers=headers)
                    self.assertEqual((403, {"error": "manage API keys in this Homestead's own app"}, []),
                                     (a.status, a.body, a.cookies))
        manage.assert_not_called()
        self.assertEqual([], self.history())

    def test_account_routes_answer_and_record(self):
        token = self.signed_in(role="admin")
        with mock.patch.object(AUTH, "create_user") as create, mock.patch.object(AUTH, "set_role") as role, \
                mock.patch.object(AUTH, "delete_user") as delete, mock.patch.object(ALERTS, "forget_user") as forget:
            a = self.send("POST", "/api/auth/users", {"username": " Robin ", "password": "x" * 16}, token=token)
            create.assert_called_once_with(" Robin ", "x" * 16, role="operator")
            self.assertEqual((200, {"ok": True, "users": [{"username": "robin"}]}, []), (a.status, a.body, a.cookies))
            self.assertEqual(self.row("user-added", "robin", detail="as operator, by ada"), self.history()[0])
            a = self.send("POST", "/api/auth/role", {"username": "robin", "role": "viewer"}, token=token)
            role.assert_called_once_with("robin", "viewer", "ada")
            self.assertEqual((200, {"ok": True, "users": [{"username": "robin"}]}), (a.status, a.body))
            self.assertEqual(self.row("role", "robin", detail="now viewer, by ada"), self.history()[0])
            a = self.send("POST", "/api/auth/users/delete", {"username": " Robin "}, token=token)
            delete.assert_called_once_with(" Robin ", "ada")
            forget.assert_called_once_with("robin")
            self.assertEqual((200, {"ok": True, "users": [{"username": "robin"}]}), (a.status, a.body))
            self.assertEqual(self.row("user-removed", " Robin ", detail="by ada"), self.history()[0])

    def test_user_role_and_removal_rows_name_a_relayed_admin_in_words(self):
        headers = self.relayed()
        with mock.patch.object(AUTH, "create_user"), mock.patch.object(AUTH, "set_role"), \
                mock.patch.object(AUTH, "delete_user"), mock.patch.object(ALERTS, "forget_user"):
            for path, body, detail in (
                    ("/api/auth/users", {"username": "robin", "password": "x" * 16, "role": "viewer"},
                     "as viewer, by ada (via Office)"),
                    ("/api/auth/role", {"username": "robin", "role": "operator"}, "now operator, by ada (via Office)"),
                    ("/api/auth/users/delete", {"username": "robin"}, "by ada (via Office)")):
                with self.subTest(path=path):
                    a = self.send("POST", path, body, headers=headers)
                    self.assertEqual(200, a.status)
                    self.assertEqual(detail, self.history()[0]["detail"])

    def test_refused_account_changes_are_answered_and_not_recorded(self):
        token = self.signed_in(role="admin")
        with mock.patch.object(AUTH, "create_user", side_effect=ValueError("that username is taken")):
            a = self.send("POST", "/api/auth/users", {"username": "robin", "password": "x"}, token=token)
        self.assertEqual((400, {"error": "that username is taken"}), (a.status, a.body))
        with mock.patch.object(AUTH, "set_role", side_effect=PermissionError("you cannot demote yourself")):
            a = self.send("POST", "/api/auth/role", {"username": "ada", "role": "viewer"}, token=token)
        self.assertEqual((403, {"error": "you cannot demote yourself"}), (a.status, a.body))
        with mock.patch.object(AUTH, "set_role") as role:
            a = self.send("POST", "/api/auth/role", {"role": "viewer"}, token=token)
        self.assertEqual((500, {"error": "'username'"}), (a.status, a.body))
        role.assert_not_called()
        with mock.patch.object(AUTH, "delete_user", side_effect=ValueError("no such user")), \
                mock.patch.object(ALERTS, "forget_user") as forget:
            a = self.send("POST", "/api/auth/users/delete", {"username": "nobody"}, token=token)
        self.assertEqual((400, {"error": "no such user"}), (a.status, a.body))
        forget.assert_not_called()
        self.assertEqual([], self.history())

    def test_an_unreadable_account_store_is_503(self):
        token = self.signed_in(role="admin")
        failure = AUTH.StoreUnavailable("the cluster did not answer")
        body = {"error": "the cluster did not answer", "cause": getattr(failure, "cause", ""), "unavailable": True}
        for path, target, name in (("/api/auth/keys", API_KEYS, "create"), ("/api/auth/users", AUTH, "create_user"),
                                   ("/api/auth/role", AUTH, "set_role"), ("/api/auth/users/delete", AUTH, "delete_user")):
            with self.subTest(path=path), mock.patch.object(target, name, side_effect=failure), \
                    mock.patch.object(ALERTS, "forget_user") as forget:
                a = self.send("POST", path, {"username": "robin", "role": "viewer", "name": "ha", "scopes": ["read"]},
                              token=token)
                self.assertEqual((503, body, []), (a.status, a.body, a.cookies))
                forget.assert_not_called()
        self.assertEqual([], self.history())

    def test_only_an_admin_reaches_them(self):
        for role in ("viewer", "operator"):
            token = self.signed_in(user=role, role=role)
            for path in ("/api/auth/keys", "/api/auth/keys/revoke", "/api/auth/keys/edit", "/api/auth/users",
                         "/api/auth/role", "/api/auth/users/delete"):
                with self.subTest(role=role, path=path):
                    a = self.send("POST", path, {"username": "robin"}, token=token)
                    self.assertEqual((403, {"error": f"This needs the admin role, and the {role} role cannot do it",
                                            "role": role, "needed": "admin"}), (a.status, a.body))
        a = self.send("POST", "/api/auth/users", {"username": "robin"}, token="nobody")
        self.assertEqual((401, {"error": "not signed in", "auth": False}), (a.status, a.body))
        self.assertEqual([], self.history())

    def test_the_wrong_method_answers_as_today(self):
        token = self.signed_in(role="admin")
        none = (404, {"error": "no route"})
        undeclared = (403, {"error": "this route has no declared authorization policy"})
        for path, answer in (("/api/auth/keys/revoke", none), ("/api/auth/keys/edit", undeclared),
                             ("/api/auth/role", none), ("/api/auth/users/delete", none)):
            with self.subTest(path=path):
                a = self.send("GET", path, token=token)
                self.assertEqual((*answer, []), (a.status, a.body, a.cookies))

class Home(unittest.TestCase):
    def test_signin_routes_are_declared_in_their_module(self):
        import homestead_routes as ROUTES
        for method, path in (("GET", "/api/auth/state"), ("GET", "/api/auth/preferences/dashboard"),
                             ("POST", "/api/auth/setup"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
                             ("POST", "/api/auth/preferences/dashboard"), ("POST", "/api/auth/password"),
                             ("POST", "/api/auth/signout-everywhere")):
            with self.subTest(method=method, path=path):
                self.assertEqual("homestead_auth_routes", getattr(ROUTES.find(method, path), "module", None))

    def test_an_in_process_call_without_a_connection_changes_nothing(self):
        """homestead_agent_tools calls module routes in-process, with no conn:
        a route that needs the connection refuses before it changes anything."""
        import homestead_routes as ROUTES
        calls = []
        record = lambda name: (lambda *a, **k: calls.append(name))
        with mock.patch.object(AUTH, "change_password", record("password")), \
                mock.patch.object(AUTH, "logout_everywhere", record("everywhere")), \
                mock.patch.object(AUTH, "login", record("login")), \
                mock.patch.object(AUTH, "issue_token", record("token")), \
                mock.patch.object(SETUP_CODE, "first_admin", record("setup")), \
                mock.patch.object(SIGNINS, "record", record("history")):
            for method, path in (("POST", "/api/auth/setup"), ("POST", "/api/auth/login"),
                                 ("POST", "/api/auth/logout"), ("POST", "/api/auth/password"),
                                 ("POST", "/api/auth/signout-everywhere"), ("GET", "/api/auth/state")):
                with self.subTest(path=path):
                    request = ROUTES.Request(method, path, {}, {"username": "ada", "password": "p", "old": "a",
                                                                "new": "b"}, "ada", "admin")
                    with self.assertRaises(PermissionError):
                        ROUTES.find(method, path).handler(request)
        self.assertEqual([], calls)

    def test_key_and_user_routes_are_declared_in_their_module(self):
        import homestead_routes as ROUTES
        for path in ("/api/auth/keys", "/api/auth/keys/revoke", "/api/auth/keys/edit", "/api/auth/users",
                     "/api/auth/role", "/api/auth/users/delete"):
            with self.subTest(path=path):
                self.assertEqual("homestead_auth_routes", getattr(ROUTES.find("POST", path), "module", None))
                self.assertIn(path, server.ADMIN_ROUTES)        # listed from the module's role, not by hand

    def test_an_in_process_account_change_without_a_connection_changes_nothing(self):
        import homestead_routes as ROUTES
        calls = []
        record = lambda name: (lambda *a, **k: calls.append(name))
        with mock.patch.object(AUTH, "create_user", record("create")), mock.patch.object(AUTH, "set_role", record("role")), \
                mock.patch.object(AUTH, "delete_user", record("delete")), mock.patch.object(ALERTS, "forget_user", record("forget")), \
                mock.patch.object(API_KEYS, "manage", record("keys")), mock.patch.object(SIGNINS, "record", record("history")):
            for path in ("/api/auth/keys", "/api/auth/keys/revoke", "/api/auth/keys/edit", "/api/auth/users",
                         "/api/auth/role", "/api/auth/users/delete"):
                with self.subTest(path=path), self.assertRaises(PermissionError):
                    ROUTES.find("POST", path).handler(ROUTES.Request("POST", path, {}, {"username": "robin", "role": "viewer"},
                                                                     "ada", "admin"))
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
