"""POST /api/auth/setup asks for the setup code (#737), through server.py's handler."""
import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_auth as AUTH
import homestead_kube as K
import homestead_self as SELF
import homestead_setup_code as CODE
import homestead_shared as SHARED
import server
import source_scan as SS
from fakes.kube import Kube


def handler(body):
    raw = json.dumps(body).encode()
    h = object.__new__(server.H)
    h.path, h.command = "/api/auth/setup", "POST"
    h.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json", "X-Homestead-Auth": "1"}
    h.client_address = ("192.0.2.50", 40000)
    h.connection = None
    h.rfile = io.BytesIO(raw)
    sent, cookies = [], []
    def send(code, payload, ctype="application/json"):
        sent.append((code, payload))
        cookies.extend(v.split(";")[0].split("=", 1)[1] for k, v in h._extra_headers if k == "Set-Cookie")
    h._send = send
    return h, sent, cookies


class SetupRouteTests(unittest.TestCase):
    def setUp(self):
        AUTH._store_cache.update(at=0, data=None)
        AUTH._attempts.clear()
        data = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(SHARED, "DIR", data))
        self.enterContext(mock.patch.object(AUTH, "ITERATIONS", 1000))
        self.enterContext(mock.patch.object(server.SIGNINS, "record", lambda *a, **k: None))
        # Whatever earlier tests left bound: this file's own cluster and names.
        for target, name, value in ((AUTH, "kget", K.kget), (AUTH, "ksend", K.ksend), (AUTH, "NS", "lab"),
                                    (AUTH, "SECRET_NAME", lambda: "homestead-auth"), (SELF, "NS", "lab")):
            self.enterContext(mock.patch.object(target, name, value))
        self.kube = self.enterContext(Kube())
        self.addCleanup(AUTH._store_cache.update, at=0, data=None)
        self.addCleanup(AUTH._attempts.clear)

    def code(self):
        CODE.ensure()
        found = self.kube.object(f"/api/v1/namespaces/{SELF.NS}/secrets/{CODE.SECRET}")
        return base64.b64decode(found["data"]["code"]).decode()

    def test_setup_without_the_code_is_refused(self):
        self.code()
        h, sent, cookies = handler({"username": "robin", "password": "a-long-password"})
        h.do_POST()
        self.assertEqual(403, sent[0][0])
        self.assertIn("setup code", sent[0][1]["error"])
        self.assertEqual([], cookies)
        self.assertTrue(AUTH.needs_setup())

    def test_the_right_code_makes_the_admin_signs_them_in_and_spends_the_code(self):
        code = self.code()
        h, sent, cookies = handler({"username": "Robin", "password": "a-long-password", "setup_code": code})
        h.do_POST()
        self.assertEqual((200, {"ok": True, "user": "Robin"}), sent[0])
        self.assertEqual("robin", AUTH.verify_token(cookies[0])["user"])
        self.assertIsNone(self.kube.object(f"/api/v1/namespaces/{SELF.NS}/secrets/{CODE.SECRET}"))

    def test_a_failed_tidy_still_signs_the_new_admin_in_and_records_it(self):
        code = self.code()
        self.kube.fail(f"/api/v1/namespaces/{SELF.NS}/secrets/{CODE.SECRET}", 503, times=5, method="DELETE")
        records = []
        h, sent, cookies = handler({"username": "robin", "password": "a-long-password", "setup_code": code})
        with mock.patch.object(server.SIGNINS, "record", lambda *a, **k: records.append(a)), \
                mock.patch.object(CODE, "_tidy_later", lambda: None):
            h.do_POST()
        self.assertEqual(200, sent[0][0])
        self.assertEqual("robin", AUTH.verify_token(cookies[0])["user"])
        self.assertEqual("setup", records[0][0])

    def test_a_wrong_code_is_in_the_sign_in_history_with_its_device(self):
        self.code()
        records = []
        h, sent, cookies = handler({"username": "mallory", "password": "a-long-password", "setup_code": "WRONG-CODE-00"})
        h.headers["User-Agent"] = "curl/8.0"
        with mock.patch.object(server.SIGNINS, "record", lambda *a, **k: records.append((a, k))):
            h.do_POST()
        self.assertEqual(403, sent[0][0])
        self.assertEqual(("signin-failed", "mallory", "192.0.2.50", False), records[0][0][:4])
        self.assertEqual("curl/8.0", records[0][1]["agent"])

    def test_every_replica_runs_the_setup_code_start(self):
        source = SS.text(SS.SERVER_PY)
        body = source.split("def start_background_tasks", 1)[1].split("\ndef ", 1)[0]
        self.assertRegex(body, r"threading\.Thread\(target=SETUP_CODE\.run\b")
        self.assertNotIn("first_only=True", source)


if __name__ == "__main__":
    unittest.main()
