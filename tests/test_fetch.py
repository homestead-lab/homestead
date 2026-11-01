"""Outbound answers read with a size cap and one deadline over the whole body."""
import http.server
import sys
import threading
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
