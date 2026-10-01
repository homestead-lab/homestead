"""Sign-in history: what is kept, what is shown, and what is never written."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_signins as signins


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        signins.bind(self.dir.name)

    def test_newest_first_one_person_or_only_failures(self):
        signins.record("signin", "robin", "10.0.0.5")
        signins.record("signin-failed", "robin", "10.0.0.9", ok=False, detail="incorrect username or password")
        signins.record("signin", "alex", "10.0.0.6")
        self.assertEqual(["alex", "robin", "robin"], [r["user"] for r in signins.history()])
        self.assertEqual(["signin-failed", "signin"], [r["event"] for r in signins.history("Robin")])
        self.assertEqual(["10.0.0.9"], [r["ip"] for r in signins.history(failures=True)])

    def test_an_unknown_event_is_not_written(self):
        signins.record("password-dump", "robin")
        self.assertEqual([], signins.history())

    def test_the_oldest_are_dropped_past_the_limit(self):
        with mock.patch.object(signins, "KEEP", 10):
            for n in range(25):
                signins.record("signin", f"user{n}")
            signins._trim()
        rows = signins.history()
        self.assertEqual(10, len(rows))
        self.assertEqual("user24", rows[0]["user"])

    def test_a_device_is_named_not_quoted(self):
        agent = ("Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/128.0.0.0 Mobile Safari/537.36")
        self.assertEqual("Chrome on Android", signins.device(agent))
        self.assertEqual("Safari on iOS", signins.device("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile/15E148 Safari/604.1"))
        self.assertEqual("curl", signins.device("curl/8.4.0"))

    def test_a_disk_that_cannot_be_written_does_not_stop_a_sign_in(self):
        signins.bind(str(Path(self.dir.name) / "file-not-dir" / "x"))
        (Path(self.dir.name) / "file-not-dir").write_text("")
        signins.record("signin", "robin")     # no exception


class LoginRouteTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        signins.bind(self.dir.name)
        server.SIGNINS.bind(self.dir.name)

    def post(self, body, login):
        h = object.__new__(self.server.H)
        raw = json.dumps(body).encode()
        h.path, h.command = "/api/auth/login", "POST"
        h.headers = {"Content-Length": str(len(raw)), "User-Agent": "curl/8.4.0", "Content-Type": "application/json", "X-Homestead-Auth": "1"}
        h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
        h.client_address, h.connection = ("192.0.2.50", 1), None
        h._send = mock.Mock()
        with mock.patch.object(self.server.AUTH, "login", login):
            h.do_POST()
        return h

    def test_a_failed_sign_in_records_the_name_tried_not_the_password(self):
        def refuse(*a, **k):
            raise PermissionError("incorrect username or password")
        self.post({"username": "Robin", "password": "hunter2-secret"}, refuse)
        row = self.server.SIGNINS.history()[0]
        self.assertEqual(("signin-failed", "robin", False, "192.0.2.50", "curl"),
                         (row["event"], row["user"], row["ok"], row["ip"], row["device"]))
        raw = (Path(self.dir.name) / "signins.jsonl").read_text()
        self.assertNotIn("hunter2", raw)

    def test_too_many_attempts_is_its_own_entry(self):
        def block(*a, **k):
            raise PermissionError("too many attempts — wait a few minutes")
        self.post({"username": "robin", "password": "x"}, block)
        self.assertEqual("signin-blocked", self.server.SIGNINS.history()[0]["event"])

    def test_a_sign_in_is_recorded(self):
        with mock.patch.object(self.server.AUTH, "idle_ttl", return_value=60):
            self.post({"username": "robin", "password": "right", "remember": True}, lambda *a, **k: "token")
        row = self.server.SIGNINS.history()[0]
        self.assertEqual(("signin", "robin", True, "kept signed in"), (row["event"], row["user"], row["ok"], row["detail"]))


if __name__ == "__main__":
    unittest.main()
