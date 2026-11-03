"""No password, setup code, API key or cookie value ever reaches the audit log
(#738): not on stdout, not in audit.jsonl - through a real server.H, with the
requests that carry them."""
import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_audit as AUDIT_LOG
import homestead_routes as ROUTES
import homestead_shared as SHARED

SECRETS = ("Pw-0123456789", "SETUP-4F7Q-9KZD", "Old-pw-0123456", "New-pw-6543210", "hs_live_NEWKEYSECRET",
           "sessiontoken123", "cfjwt.a.b", "hsk_abc", "Basic-Pw-0123456789")


class NothingSecret(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Its own after-request queue, so its own hook worker: a hook another file left
        # running cannot hold this class's lines up (the audit's settings read waits
        # 5 s for DNS on CI). And audit.file as a failed read leaves it, on, without
        # the read: the hook path itself is the real one.
        cls.enterClassContext(mock.patch.object(ROUTES, "_queue", queue.Queue(ROUTES.HOOK_QUEUE)))
        cls.enterClassContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))

    def setUp(self):
        import homestead_http as HTTP
        import server
        self.server = server
        ROUTES.table()
        self.assertTrue(any(fn is AUDIT_LOG.record for fn, _ in ROUTES._hooks), "the audit hook is registered")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.enterContext(mock.patch.object(SHARED, "DIR", self.dir))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))
        AUDIT_LOG._buckets.clear()
        self.addCleanup(AUDIT_LOG._buckets.clear)
        self.printed, self.arrived = [], threading.Condition()

        def out(text):
            with self.arrived:
                self.printed.append(text)
                self.arrived.notify_all()
        self.enterContext(mock.patch.object(AUDIT_LOG, "_stdout", side_effect=out))
        listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        self.listener = listener
        self.enterContext(mock.patch.object(server.H, "_who", return_value={
            "user": "ada", "role": "admin", "stale": False, "remember": False, "started": None}))
        self.enterContext(mock.patch.object(server.H, "_signin", lambda *a, **k: None))
        self.enterContext(mock.patch.object(server.FLEET, "self_id", return_value="home"))

    def request(self, method, path, body=None, headers=()):
        client = HTTPConnection(*self.listener.server_address, timeout=5)
        try:
            sent = {"Content-Type": "application/json", "X-Homestead-Auth": "1", **dict(headers)}
            client.request(method, path, json.dumps(body) if body is not None else None, sent)
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def test_no_password_setup_code_key_or_cookie_is_ever_written(self):
        S, AUTH = self.server, self.server.AUTH
        cookie = {"Cookie": "homestead_session=sessiontoken123; CF_Authorization=cfjwt.a.b",
                  "Cf-Access-Jwt-Assertion": "cfjwt.a.b", "Proxy-Authorization": "Basic-Pw-0123456789"}
        with mock.patch.object(AUTH, "login", return_value="sessiontoken123"), \
                mock.patch.object(AUTH, "create_user"), mock.patch.object(AUTH, "change_password"), \
                mock.patch.object(AUTH, "list_users", return_value=[]), \
                mock.patch.object(AUTH, "issue_token", return_value="sessiontoken123"), \
                mock.patch.object(S.API_KEYS, "verify", return_value={
                    "id": "k1", "name": "ha", "owner": "ada", "scopes": ["apps:control"], "expires": None}), \
                mock.patch.object(S.API_KEYS, "manage", return_value=(
                    {"ok": True, "secret": "hs_live_NEWKEYSECRET", "name": "ha"}, "key-made", "ha")):
            answers = [
                self.request("POST", "/api/auth/login?password=Pw-0123456789",
                             {"username": "ada", "password": "Pw-0123456789"}),
                self.request("POST", "/api/auth/setup?code=SETUP-4F7Q-9KZD",
                             {"username": "ada", "password": "Pw-0123456789", "setup_code": "SETUP-4F7Q-9KZD"}),
                self.request("POST", "/api/auth/password", {"old": "Old-pw-0123456", "new": "New-pw-6543210"}, cookie),
                self.request("POST", "/api/auth/users",
                             {"username": "node1", "password": "Pw-0123456789", "role": "viewer"}, cookie),
                self.request("POST", "/api/auth/keys", {"name": "ha", "scopes": ["read"]}, cookie),
                self.request("POST", "/api/v1/apps/restart?token=hsk_abc", {"name": "app-1"},
                             {"Authorization": "Bearer hsk_abc"}),
            ]
        self.assertIn(b"hs_live_NEWKEYSECRET", answers[4][1], "the key was answered, to the client only")
        with self.arrived:
            self.arrived.wait_for(lambda: len(self.printed) >= 5, timeout=5)
        self.assertEqual(["/api/auth/setup", "/api/auth/password", "/api/auth/users", "/api/auth/keys",
                          "/api/v1/apps/restart"], [json.loads(t)["path"] for t in self.printed])
        audit = Path(self.dir) / "audit.jsonl"

        def kept_lines():           # stdout is written first, then the file, on the hooks' thread
            return audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
        deadline = time.monotonic() + 5
        while len(kept_lines()) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        kept = "\n".join(kept_lines())
        self.assertEqual(5, len(kept_lines()))
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, "\n".join(self.printed))
                self.assertNotIn(secret, kept)


if __name__ == "__main__":
    unittest.main()
