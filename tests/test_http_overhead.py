"""Per-request overhead (#985 §7): request deadlines share one timer thread,
prefix routes are found without scanning every route, and an expected 404 or
409 to a request with no body keeps its connection."""
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_http as HTTP
import homestead_routes as ROUTES


class DeadlineTests(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return a, b

    def test_deadlines_start_no_thread_per_request(self):
        a, _b = self.pair()
        with HTTP.deadline(a, 30):
            pass                        # the shared timer thread may start once
        before = threading.active_count()
        with mock.patch.object(threading, "Timer", side_effect=AssertionError("a thread per deadline")):
            for _ in range(100):
                with HTTP.deadline(a, 30):
                    pass
        self.assertLessEqual(threading.active_count(), before)

    def test_a_deadline_that_passes_cuts_the_connection(self):
        a, _b = self.pair()
        a.settimeout(5)
        started = time.monotonic()
        with HTTP.deadline(a, 0.05):
            try:
                data = a.recv(10)
            except OSError:
                data = b""
        self.assertEqual(b"", data)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_deadline_left_in_time_leaves_the_connection_alone(self):
        a, b = self.pair()
        with HTTP.deadline(a, 0.05):
            pass
        time.sleep(0.15)
        b.sendall(b"x")
        a.settimeout(2)
        self.assertEqual(b"x", a.recv(1))


class PrefixRouteTests(unittest.TestCase):
    def test_a_static_path_does_not_scan_every_route(self):
        ROUTES.find("GET", "/js/app.js")
        table = ROUTES.table()
        looked = []
        class Counting(dict):
            def __iter__(self):
                looked.append(1)
                return super().__iter__()
        counting = Counting(table)
        with mock.patch.object(ROUTES, "table", lambda: counting):
            ROUTES.find("GET", "/js/app.js")        # builds the index once
            looked.clear()
            for _ in range(10):
                ROUTES.find("GET", "/js/app.js")
        self.assertEqual([], looked, "prefix routes come from an index, not a scan of the table")

    def test_the_index_finds_the_same_routes_as_the_scan(self):
        table = ROUTES.table()
        prefixes = [(m, p) for m, p in table if p.endswith("/*")]
        self.assertTrue(prefixes)
        for method, path in prefixes:
            probe = path[:-1] + "x"
            want = max((p for m, p in table if m == method and p.endswith("/*") and probe.startswith(p[:-1])
                        and ROUTES._REST.fullmatch(probe[len(p) - 1:])), key=len)
            if (method, probe) not in table:
                self.assertEqual(table[(method, want)], ROUTES.find(method, probe), probe)


class KeepAliveTests(unittest.TestCase):
    def handler(self, method="GET", headers=None):
        h = type("H", (), {})()
        h.command, h.headers = method, headers or {}
        return h

    def test_an_expected_miss_on_a_read_keeps_the_connection(self):
        for code in (404, 409):
            self.assertFalse(HTTP.closes_after(self.handler(), code), code)

    def test_other_errors_and_requests_with_a_body_still_close(self):
        self.assertTrue(HTTP.closes_after(self.handler(), 400))
        self.assertTrue(HTTP.closes_after(self.handler(), 500))
        self.assertTrue(HTTP.closes_after(self.handler(), 503))
        self.assertTrue(HTTP.closes_after(self.handler("POST", {"Content-Length": "12"}), 409))
        self.assertTrue(HTTP.closes_after(self.handler("GET", {"Transfer-Encoding": "chunked"}), 404))
        self.assertFalse(HTTP.closes_after(self.handler(), 200))


if __name__ == "__main__":
    unittest.main()
