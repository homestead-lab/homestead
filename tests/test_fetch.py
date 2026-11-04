"""Outbound answers read with a size cap and one deadline over the whole body."""
import http.server
import io
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_fetch as FETCH


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Socket:
    def __init__(self):
        self.timeouts = []
        self.timeout = 12.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeouts.append(value)


class Answer:
    """A response handing out its body in pieces, each piece taking `step` seconds."""

    def __init__(self, pieces, clock=None, step=0.0, length=None):
        self.pieces, self.clock, self.step = list(pieces), clock, step
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self.reads = 0

    def read1(self, size):
        self.reads += 1
        if self.clock:
            self.clock.now += self.step
        if not self.pieces:
            return b""
        piece = self.pieces.pop(0)
        if len(piece) > size:
            self.pieces.insert(0, piece[size:])
            piece = piece[:size]
        return piece


class CappedReadTests(unittest.TestCase):
    def test_a_body_within_the_cap_is_read_whole(self):
        self.assertEqual(b"abcdef", FETCH.read(Answer([b"abc", b"def"]), 6))

    def test_a_body_over_the_cap_is_refused_however_it_arrives(self):
        with self.assertRaisesRegex(FETCH.TooLarge, "larger than"):
            FETCH.read(Answer([b"abc", b"defg"]), 6)

    def test_a_declared_length_over_the_cap_is_refused_before_reading(self):
        answer = Answer([b"x"], length=7)
        with self.assertRaises(FETCH.TooLarge):
            FETCH.read(answer, 6)
        self.assertEqual(0, answer.reads)

    def test_no_more_than_the_cap_and_one_byte_is_ever_asked_for(self):
        answer = Answer([b"a" * 100])
        with self.assertRaises(FETCH.TooLarge):
            FETCH.read(answer, 10)
        self.assertLessEqual(answer.reads, 2)

    def test_a_trickling_body_stops_at_the_deadline(self):
        clock = Clock()
        answer = Answer([b"a"] * 1000, clock, step=1.0)
        with mock.patch.object(FETCH.time, "monotonic", clock):
            with self.assertRaisesRegex(TimeoutError, "took too long"):
                FETCH.read(answer, 1 << 20, deadline=clock.now + 30, what="the feed")
        self.assertLess(answer.reads, 40, "the whole read is bounded, not each piece")

    def test_each_wait_on_the_socket_is_at_most_what_is_left(self):
        clock = Clock()
        answer, sock = Answer([b"ab", b"cd"], clock, step=4.0), Socket()
        with mock.patch.object(FETCH.time, "monotonic", clock), mock.patch.object(FETCH, "_socket", return_value=sock):
            self.assertEqual(b"abcd", FETCH.read(answer, 100, deadline=clock.now + 10))
        self.assertEqual([10.0, 6.0, 2.0], sock.timeouts)

    def test_pieces_can_be_handled_as_they_arrive(self):
        self.assertEqual([b"ab", b"cd"], list(FETCH.chunks(Answer([b"ab", b"cd"]), 10)))

    def test_a_response_without_read1_is_read_with_read(self):
        class Plain:
            headers = {}

            def __init__(self):
                self.left = [b"xy", b""]

            def read(self, size=-1):
                return self.left.pop(0)

        self.assertEqual(b"xy", FETCH.read(Plain(), 10))


class RealAnswerTests(unittest.TestCase):
    """http.client closes the socket once the body is in; the fakes above have none."""

    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true}' * (1 if self.path == "/small" else 20000)
                self.send_response(200)
                if self.path != "/chunked":
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}{path}", timeout=5) as response:
            return FETCH.read(response, 1 << 20, time.monotonic() + 5)

    def test_a_whole_answer_is_read_under_a_deadline(self):
        self.assertEqual(b'{"ok": true}', self.fetch("/small"))

    def test_an_answer_of_many_pieces_is_read_under_a_deadline(self):
        self.assertEqual(12 * 20000, len(self.fetch("/large")))

    def test_an_answer_without_a_length_is_read_under_a_deadline(self):
        self.assertEqual(12 * 20000, len(self.fetch("/chunked")))


class GetHandler(http.server.BaseHTTPRequestHandler):
    """Answers as the far ends do: whole, trickling, too large, closing, redirecting."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def answer(self, body, status=200, headers=(), length=True):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/json":
            return self.answer(b'{"ok": true}')
        if path == "/trickle":
            self.send_response(200)
            self.send_header("Content-Length", "400")
            self.end_headers()
            try:
                for _ in range(400):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            return
        if path == "/big":
            return self.answer(b"x" * 5000)
        if path == "/big-unsized":
            self.close_connection = True
            return self.answer(b"x" * 5000, length=False)
        if path == "/close":
            self.close_connection = True
            return self.answer(b"y" * 200000, headers=[("Connection", "close")])
        if path == "/headers":
            seen = {name.lower(): value for name, value in self.headers.items()}
            sent = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.update(_method=self.command, _body=sent.decode())
            return self.answer(json.dumps(seen).encode(), headers=[("Content-Type", "application/json")])
        if path == "/redirect":
            return self.answer(b"", status=302, headers=[("Location", query)])
        if path.startswith("/r3"):
            return self.answer(b"", status=int(path[2:]), headers=[("Location", query)])
        if path == "/redirect-big":
            return self.answer(b"x" * (1 << 20), status=302, headers=[("Location", query)])
        if path == "/redirect-trickle":
            self.trickle(302, [("Location", query)])
            return
        if path == "/hops":
            left = int(query or 0)
            target = f"/hops?{left - 1}" if left > 1 else "/json"
            self.trickle(302, [("Location", target)], count=8)
            return
        if path == "/error-big":
            return self.answer(b'{"error": "no"}' + b" " * (1 << 20), status=500)
        if path == "/error-bad-chunks":
            # A chunked error body that breaks off inside its second chunk.
            self.close_connection = True
            self.send_response(500)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"5\r\nfirst\r\n40\r\nshort")
            self.wfile.flush()
            return
        if path == "/error-trickle":
            self.trickle(500)
            return
        self.answer(b"missing", status=404)

    do_POST = do_GET

    def trickle(self, status, headers=(), count=400):
        """A body of `count` bytes at one byte every 0.05 s."""
        self.close_connection = True
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(count))
        self.end_headers()
        try:
            for _ in range(count):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass


class RedirectAnswer(io.BytesIO):
    """A 3xx answer with a body and a declared length, that records being closed."""

    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(length if length is not None else len(body))}
        self.closes = 0

    def close(self):
        self.closes += 1
        super().close()


class RedirectCloseTests(unittest.TestCase):
    """A redirect given up on closes its answer at once, not when it is collected."""

    class Request:
        full_url = "https://192.0.2.10/x"
        timeout = 5

    def follow(self, fp, newurl="https://192.0.2.10/y", deadline=None):
        handler = FETCH._Redirects(deadline or time.monotonic() + 5, "it")
        return handler.redirect_request(self.Request(), fp, 302, "Found", {}, newurl)

    def test_a_redirect_body_over_the_cap_closes_the_answer(self):
        fp = RedirectAnswer(b"x", length=1 << 20)
        with self.assertRaises(FETCH.TooLarge):
            self.follow(fp)
        self.assertEqual(1, fp.closes)

    def test_a_deadline_gone_by_closes_the_answer(self):
        fp = RedirectAnswer(b"")
        with self.assertRaises(TimeoutError):
            self.follow(fp, deadline=time.monotonic() - 1)
        self.assertEqual(1, fp.closes)

    def test_a_refused_redirect_closes_the_answer(self):
        fp = RedirectAnswer(b"")
        with self.assertRaises(urllib.error.HTTPError):
            self.follow(fp, newurl="http://192.0.2.10/y")
        self.assertGreaterEqual(fp.closes, 1, "closing twice is harmless; never closing is not")


class GetTests(unittest.TestCase):
    """FETCH.get opens, reads under a cap and a deadline, and keeps credentials on their host."""

    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for _ in range(2):
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GetHandler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.servers.append(server)
        cls.here, cls.other = (f"http://127.0.0.1:{s.server_port}" for s in cls.servers)

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown()
            server.server_close()

    def test_a_whole_answer_comes_back_with_its_address(self):
        answer = FETCH.get(self.here + "/json", limit=100, seconds=5)
        self.assertEqual(b'{"ok": true}', answer.body)
        self.assertEqual(self.here + "/json", answer.url)
        self.assertEqual({"ok": True}, FETCH.get_json(self.here + "/json", limit=100, seconds=5))
        self.assertEqual('{"ok": true}', FETCH.get_text(self.here + "/json", limit=100, seconds=5))

    def test_a_trickling_answer_ends_at_the_deadline(self):
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "the trickle took too long"):
            FETCH.get(self.here + "/trickle", limit=1000, seconds=0.5, timeout=5, what="the trickle")
        self.assertLess(time.monotonic() - started, 3, "the whole read is bounded, not each wait")

    def test_an_answer_over_the_cap_is_refused_with_or_without_a_length(self):
        for path in ("/big", "/big-unsized"):
            with self.subTest(path=path), self.assertRaisesRegex(FETCH.TooLarge, "the list is larger than 1000 bytes"):
                FETCH.get(self.here + path, limit=1000, seconds=5, what="the list")

    def test_an_answer_that_closes_its_connection_is_read_whole(self):
        self.assertEqual(200000, len(FETCH.get(self.here + "/close", limit=1 << 20, seconds=5).body))

    def test_a_redirect_to_another_host_drops_the_credentials(self):
        secret = "k" + "ey-" + "123"
        target = self.other + "/headers"
        seen = FETCH.get_json(f"{self.here}/redirect?{target}", limit=1 << 16, seconds=5, headers={
            "X-API-KEY": secret, "Authorization": "Bearer " + secret, "Cookie": "s=" + secret,
            "X-Homestead-Sig": secret, "Accept": "application/json", "User-Agent": "Homestead"})
        self.assertNotIn(secret, json.dumps(seen))
        self.assertEqual("application/json", seen.get("accept"))
        self.assertEqual("Homestead", seen.get("user-agent"))

    def test_a_redirect_on_the_same_host_keeps_them(self):
        secret = "k" + "ey-" + "456"
        seen = FETCH.get_json(f"{self.here}/redirect?{self.here}/headers", limit=1 << 16, seconds=5,
                              headers={"X-API-KEY": secret})
        self.assertEqual(secret, seen.get("x-api-key"))

    def test_a_redirect_off_the_web_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError):
            FETCH.get(f"{self.here}/redirect?ftp://127.0.0.1/x", limit=100, seconds=5)

    def test_credentials_do_not_come_back_after_a_hop_elsewhere(self):
        secret = "k" + "ey-" + "321"
        back = f"{self.other}/redirect?{self.here}/headers"
        seen = FETCH.get_json(f"{self.here}/redirect?{back}", limit=1 << 16, seconds=5, headers={"X-API-KEY": secret})
        self.assertEqual(self.here.split("//")[1], seen["host"])
        self.assertNotIn("x-api-key", seen)

    def test_a_redirect_from_https_to_http_is_refused(self):
        class Old:
            full_url = "https://192.0.2.10/x"
        with self.assertRaisesRegex(urllib.error.HTTPError, "https"):
            FETCH._Redirects(time.monotonic() + 5, "it").redirect_request(
                Old(), io.BytesIO(b""), 302, "Found", {}, "http://192.0.2.10/y")

    def test_a_redirect_with_a_port_that_is_not_one_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError):
            FETCH.get(f"{self.here}/redirect?http://127.0.0.1:99999999/x", limit=100, seconds=5)

    def test_a_post_redirected_by_307_or_308_is_not_sent_again(self):
        for code in (307, 308):
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError) as caught:
                FETCH.get(f"{self.here}/r{code}?{self.here}/headers", data=b'{"a": 1}', method="POST",
                          limit=1 << 16, seconds=5)
            self.assertEqual(code, caught.exception.code)

    def test_a_post_redirected_by_301_302_or_303_becomes_a_get_without_its_body(self):
        for code in (301, 302, 303):
            with self.subTest(code=code):
                seen = FETCH.get_json(f"{self.here}/r{code}?{self.here}/headers", data=b'{"a": 1}',
                                      method="POST", limit=1 << 16, seconds=5)
                self.assertEqual(("GET", ""), (seen["_method"], seen["_body"]))

    def test_a_redirect_body_over_the_cap_is_refused(self):
        with self.assertRaises(FETCH.TooLarge):
            FETCH.get(f"{self.here}/redirect-big?{self.here}/json", limit=1 << 20, seconds=5)

    def test_a_trickling_redirect_body_ends_at_the_deadline(self):
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "took too long"):
            FETCH.get(f"{self.here}/redirect-trickle?{self.here}/json", limit=100, seconds=0.5, timeout=5)
        self.assertLess(time.monotonic() - started, 3)

    def test_the_deadline_holds_across_redirects(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            FETCH.get(self.here + "/hops?8", limit=100, seconds=1, timeout=5)
        self.assertLess(time.monotonic() - started, 2.5, "eight hops of 0.4 s each are cut at one second")

    def test_an_error_body_is_cut_at_its_cap(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            FETCH.get(self.here + "/error-big", limit=1 << 22, seconds=5)
        self.assertEqual(500, caught.exception.code)
        self.assertLessEqual(len(caught.exception.read()), FETCH.ERROR_MAX_BYTES)

    def test_a_trickling_error_body_ends_at_the_deadline(self):
        started = time.monotonic()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            FETCH.get(self.here + "/error-trickle", limit=100, seconds=0.5, timeout=5)
        caught.exception.read()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(500, caught.exception.code)

    def test_a_broken_error_body_still_gives_the_http_error_and_what_came(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            FETCH.get(self.here + "/error-bad-chunks", limit=1 << 16, seconds=5)
        self.assertEqual(500, caught.exception.code)
        self.assertTrue(caught.exception.read().startswith(b"first"))

    def test_an_error_status_is_raised_as_urllib_does(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            FETCH.get(self.here + "/nothing", limit=100, seconds=5)
        self.assertEqual(404, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
