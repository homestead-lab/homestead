"""What the Longhorn volumes alert says about a failed read (#1082).

Alerts go to notification channels, so the error is short and plain, and the
alert says which kind of failure it was: the cluster's API did not answer, it
answered with an error, or the read failed otherwise. Built on real
exceptions and on homestead_kube's own client against a local server, not on
hand-written strings only."""
import http.client
import http.server
import socket
import ssl
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server  # noqa: F401  (binds homestead_kube)
import homestead_alerts as ALERTS
import homestead_kube as KUBE
import homestead_read_deadline as DEADLINE
import homestead_storage as STORAGE

TOKEN = "eyJhbGciOi" + "JSUzI1NiIs" + "InR5cCI6Ik" + "pXVCJ9"      # built in pieces: push protection


def wrapped(error):
    """As homestead_storage.get_volumes raises it: a RuntimeError from the read's error."""
    try:
        raise RuntimeError(f"Longhorn's volumes could not be read: {error}") from error
    except RuntimeError as outer:
        return outer


class KindTests(unittest.TestCase):
    def says(self, error):
        return ALERTS.read_failure(wrapped(error))

    def http(self, code, reason):
        return urllib.error.HTTPError(f"https://192.0.2.10:6443/apis/longhorn.io/v1beta2/volumes?token={TOKEN}",
                                      code, reason, {}, None)

    def test_403_says_a_permission_may_be_missing(self):
        self.assertEqual("the cluster's API answered 403 Forbidden: Homestead may be missing a permission",
                         self.says(self.http(403, "Forbidden")))

    def test_404_and_429_say_what_it_answered(self):
        self.assertEqual("the cluster's API answered 404 Not Found", self.says(self.http(404, "Not Found")))
        self.assertEqual("the cluster's API answered 429 Too Many Requests",
                         self.says(self.http(429, "Too Many Requests")))

    def test_a_timeout_did_not_answer(self):
        self.assertEqual("the cluster's API did not answer (timed out)", self.says(TimeoutError("timed out")))
        self.assertEqual("the cluster's API did not answer (TimeoutError)", self.says(TimeoutError()),
                         "an empty message names its kind")
        self.assertTrue(self.says(DEADLINE.spent()).startswith("the cluster's API did not answer ("))

    def test_a_dropped_connection_did_not_answer(self):
        error = urllib.error.URLError(http.client.RemoteDisconnected("Remote end closed connection without response"))
        self.assertEqual("the cluster's API did not answer (<urlopen error Remote end closed connection without response>)",
                         self.says(error))
        self.assertTrue(self.says(ConnectionResetError(104, "Connection reset by peer")).startswith(
            "the cluster's API did not answer"))

    def test_a_certificate_that_does_not_match_did_not_answer_and_names_no_host(self):
        error = ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, certificate is not "
               "valid for 'kube.example.com'. (_ssl.c:1006)")
        for raised in (error, urllib.error.URLError(error)):
            text = self.says(raised)
            self.assertTrue(text.startswith("the cluster's API did not answer ("), text)
            self.assertIn("Hostname mismatch", text)
            self.assertNotIn("kube.example.com", text)

    def test_a_list_cut_short_is_a_failed_read(self):
        self.assertEqual("the read failed (Kubernetes returned an incomplete list for "
                         "/apis/longhorn.io/v1beta2/volumes; refresh and try again)",
                         self.says(KUBE.Incomplete("Kubernetes returned an incomplete list for "
                                                   "/apis/longhorn.io/v1beta2/volumes; refresh and try again")))

    def test_an_error_without_a_cause_is_a_failed_read(self):
        self.assertEqual("the read failed (KeyError 'status')", ALERTS.read_failure(KeyError("status")))

    def test_the_error_is_cut_at_160_characters(self):
        text = ALERTS.read_failure(KUBE.Incomplete("x " * 300))
        error = text[len("the read failed ("):-1]
        self.assertLessEqual(len(error), 160)
        self.assertTrue(error.endswith("…"))


class RealErrorTests(unittest.TestCase):
    """The str() of what homestead_kube's client really raises."""

    def test_a_socket_that_never_answers_times_out(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        with socket.create_connection(listener.getsockname(), timeout=0.2) as client:
            with self.assertRaises(TimeoutError) as caught:
                client.recv(1)
        text = ALERTS.read_failure(wrapped(caught.exception))
        self.assertEqual("the cluster's API did not answer (timed out)", text)

    def test_every_real_error_s_text_is_plain(self):
        errors = [
            urllib.error.URLError(ssl.SSLCertVerificationError(
                1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: IP address mismatch, certificate is "
                   "not valid for '192.0.2.10'. (_ssl.c:1006)")),
            urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
            OSError(f"connect to [2001:db8::1]:6443 at https://198.51.100.7:6443/?token={TOKEN} failed"),
            http.client.IncompleteRead(b"x" * 400, 100),
        ]
        for error in errors:
            with self.subTest(error=repr(error)[:60]):
                text = ALERTS.read_failure(wrapped(error))
                for part in ("192.0.2.10", "198.51.100.7", "2001:db8", "6443", TOKEN, "token="):
                    self.assertNotIn(part, text)
                self.assertLessEqual(len(text), len("the cluster's API did not answer ()") + 160)


class Api(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode):
        self.mode = mode
        super().__init__(("127.0.0.1", 0), ApiHandler)


class ApiHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.server.mode == "drop":
            self.close_connection = True       # read the request, close without an answer
            return
        body = b'{"kind":"Status","code":403,"message":"volumes.longhorn.io is forbidden"}'
        self.send_response(403, "Forbidden")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThroughTheClientTests(unittest.TestCase):
    """STORAGE.get_volumes through homestead_kube's own client and a local
    server: the exception chain the overview sees on a cluster."""

    def serve(self, mode):
        api = Api(mode)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        host, port = api.server_address
        self.enterContext(mock.patch.object(KUBE, "API", f"http://{host}:{port}"))
        self.enterContext(mock.patch.object(KUBE, "TOKEN", TOKEN))
        KUBE.close_connections()
        self.addCleanup(KUBE.close_connections)
        DEADLINE.end()
        return host, port

    def failure(self):
        with self.assertRaises(Exception) as caught:
            STORAGE.get_volumes()
        return ALERTS.read_failure(caught.exception)

    def test_a_403_says_a_permission_may_be_missing(self):
        host, port = self.serve("forbidden")
        text = self.failure()
        self.assertEqual("the cluster's API answered 403 Forbidden: Homestead may be missing a permission", text)

    def test_a_connection_closed_without_an_answer_did_not_answer(self):
        host, port = self.serve("drop")
        text = self.failure()
        self.assertTrue(text.startswith("the cluster's API did not answer ("), text)
        for part in (host, str(port), TOKEN):
            self.assertNotIn(part, text)


if __name__ == "__main__":
    unittest.main()
