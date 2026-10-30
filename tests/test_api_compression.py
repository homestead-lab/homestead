"""The page's big per-tick reads travel gzipped when the browser accepts it (#982).

Only the reads on homestead_static.COMPRESSED_ANSWERS are compressed: list and
summary answers every role may read, with no secret in them. Compressing an
answer that holds a secret next to text the requester chose is how BREACH
reads the secret back, so everything else (answers to actions, downloads,
streams, reveals, logs) is always sent as it is."""
import email.message
import gzip
import importlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server
import homestead_routes as ROUTES
import homestead_static as STATIC

BIG = {"nodes": [{"name": f"node-{n}", "days": [{"day": d, "up": 86400, "down": 0, "kind": "ok"}
                                                 for d in range(90)]} for n in range(3)]}
SMALL = {"ok": True}


def call(path, body, ctype="application/json", code=200, method="GET", gzip_ok=True, extra=()):
    """Send body through the handler's own _send, with no socket, and parse what it wrote."""
    handler = server.H.__new__(server.H)
    handler.path, handler.command = path, method
    handler.request_version, handler.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
    handler.headers = email.message.Message()
    if gzip_ok:
        handler.headers["Accept-Encoding"] = "br, gzip;q=0.9, deflate"
    handler.wfile, handler.connection = io.BytesIO(), None
    handler.close_connection, handler._extra_headers = False, list(extra)
    handler.client_address = ("192.0.2.10", 0)
    handler.log_message = lambda *args: None
    handler._send(code, body, ctype)
    head, _, payload = handler.wfile.getvalue().partition(b"\r\n\r\n")
    headers = {}
    for line in head.decode("latin-1").split("\r\n")[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return headers, payload


class AnswerCompressionTests(unittest.TestCase):
    def test_a_large_listed_read_is_gzipped_when_asked(self):
        headers, payload = call("/api/nodes/uptime", BIG)
        self.assertEqual(["gzip"], headers["content-encoding"])
        self.assertIn("Accept-Encoding", headers["vary"])
        self.assertEqual([str(len(payload))], headers["content-length"])
        self.assertEqual(BIG, json.loads(gzip.decompress(payload)))
        self.assertLess(len(payload), len(json.dumps(BIG)) / 5)

    def test_a_query_string_does_not_change_which_read_it_is(self):
        headers, _ = call("/api/history/long?range=7d", BIG)
        self.assertEqual(["gzip"], headers["content-encoding"])

    def test_the_same_read_is_plain_when_the_browser_did_not_ask(self):
        headers, payload = call("/api/nodes/uptime", BIG, gzip_ok=False)
        self.assertNotIn("content-encoding", headers)
        self.assertIn("Accept-Encoding", headers["vary"])
        self.assertEqual(BIG, json.loads(payload))

    def test_gzip_refused_with_q_zero_is_not_used(self):
        for refused in ("gzip;q=0, identity", "gzip; q=0.0", "identity"):
            with self.subTest(accept=refused):
                handler = type("H", (), {"headers": {"Accept-Encoding": refused}})()
                self.assertFalse(STATIC._wants_gzip(handler))

    def test_a_small_answer_is_never_compressed(self):
        headers, payload = call("/api/nodes/uptime", SMALL)
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(SMALL, json.loads(payload))

    def test_a_read_not_on_the_list_is_never_compressed(self):
        for path in ("/api/resources/reveal?ns=a&name=b", "/api/auth/keys", "/api/logs?q=x", "/api/setup"):
            with self.subTest(path=path):
                headers, payload = call(path, BIG)
                self.assertNotIn("content-encoding", headers)
                self.assertNotIn("vary", headers)
                self.assertEqual(BIG, json.loads(payload))

    def test_an_answer_to_an_action_is_never_compressed(self):
        headers, _ = call("/api/overview", BIG, method="POST")
        self.assertNotIn("content-encoding", headers)

    def test_an_error_is_never_compressed(self):
        headers, _ = call("/api/overview", BIG, code=500)
        self.assertNotIn("content-encoding", headers)

    def test_a_download_or_stream_is_never_compressed(self):
        text = json.dumps(BIG)
        for ctype, extra in (("application/json", [("Content-Disposition", 'attachment; filename="a.json"')]),
                             ("text/event-stream", []), ("application/octet-stream", [])):
            with self.subTest(ctype=ctype):
                headers, payload = call("/api/overview", text, ctype=ctype, extra=extra)
                self.assertNotIn("content-encoding", headers)
                self.assertEqual(text.encode(), payload)

    def test_a_body_already_encoded_is_not_encoded_twice(self):
        packed = gzip.compress(json.dumps(BIG).encode())
        headers, payload = call("/api/overview", packed, extra=[("Content-Encoding", "gzip")])
        self.assertEqual(["gzip"], headers["content-encoding"])
        self.assertEqual(packed, payload)

    def test_every_listed_read_is_a_get_route_any_role_may_read(self):
        declared = {}
        for name in ROUTES.MODULES:
            for (method, path), (role, *_) in getattr(importlib.import_module(name), "ROUTES", {}).items():
                declared[(method, path)] = role
        for path in STATIC.COMPRESSED_ANSWERS:
            with self.subTest(path=path):
                self.assertEqual("viewer", declared.get(("GET", path)), f"{path} is not a viewer GET route")


if __name__ == "__main__":
    unittest.main()
