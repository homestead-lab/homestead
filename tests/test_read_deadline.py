"""A request's Kubernetes reads share one deadline (#984).

Each read was allowed its own 10 s and a cold page makes dozens to hundreds
of them in turn, so a hung API server held a page for minutes. A GET request
now has homestead_read_deadline.SECONDS for all its reads: each read is given what is
left, none starts after it, and a 429 is not retried past it. A page some of
whose reads were cut short answers 504 (or the last complete copy kept in
the cache), never a partial page as if it were complete. Background loops
set no deadline and keep their own timeouts."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import homestead_cache as CACHE
import homestead_kube as K
import homestead_read_deadline as DEADLINE
import homestead_request_scope as SCOPE
import homestead_routes as MODULE_ROUTES
from fakes.kube import Kube
import homestead_restarts as RESTARTS
from test_page_snapshot import clean_cache, clean_snapshot, objects

NODE = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node-a"}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class DeadlineCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(mock.patch.object(DEADLINE, "clock", self.clock))
        self.addCleanup(DEADLINE.end)
        self.timeouts = []

    def recording(self, kube):
        def transport(method, path, body=None, ctype=None, timeout=15):
            self.timeouts.append(timeout)
            return kube(method, path, body, ctype, timeout)
        return transport


class KgetTests(DeadlineCase):
    def test_without_a_deadline_a_read_keeps_its_own_timeout(self):
        with Kube(NODE) as kube, mock.patch.object(K, "transport", self.recording(kube)):
            K.kget("/api/v1/nodes/node-a", timeout=10)
        self.assertEqual([10], self.timeouts)

    def test_a_read_is_given_what_is_left_of_the_request(self):
        DEADLINE.begin(15)
        self.clock.advance(12)
        with Kube(NODE) as kube, mock.patch.object(K, "transport", self.recording(kube)):
            K.kget("/api/v1/nodes/node-a", timeout=10)
        self.assertEqual([3], self.timeouts)
        self.assertFalse(DEADLINE.cuts())

    def test_no_read_starts_after_the_deadline(self):
        DEADLINE.begin(15)
        self.clock.advance(15)
        with Kube(NODE) as kube, mock.patch.object(K, "transport", self.recording(kube)):
            with self.assertRaises(DEADLINE.Spent):
                K.kget("/api/v1/nodes/node-a")
        self.assertEqual([], self.timeouts)
        self.assertEqual(1, DEADLINE.cuts())

    def test_the_spent_error_is_a_timeout_so_callers_catch_it_as_before(self):
        self.assertTrue(issubclass(DEADLINE.Spent, TimeoutError))

    def test_a_read_cut_short_by_the_deadline_counts_as_cut(self):
        DEADLINE.begin(15)
        self.clock.advance(13)

        def hung(method, path, body=None, ctype=None, timeout=15):
            self.clock.advance(timeout)
            raise TimeoutError("timed out")
        with mock.patch.object(K, "transport", hung):
            with self.assertRaises(DEADLINE.Spent):
                K.kget("/api/v1/nodes/node-a")
        self.assertEqual(1, DEADLINE.cuts())
        self.assertEqual(1015.0, self.clock.now)

    def test_a_read_timing_out_on_its_own_timeout_is_not_a_cut(self):
        DEADLINE.begin(15)

        def hung(method, path, body=None, ctype=None, timeout=15):
            raise TimeoutError("timed out")
        with mock.patch.object(K, "transport", hung):
            with self.assertRaises(TimeoutError) as caught:
                K.kget("/api/v1/nodes/node-a", timeout=10)
        self.assertNotIsInstance(caught.exception, DEADLINE.Spent)
        self.assertEqual(0, DEADLINE.cuts())

    def test_a_429_is_not_retried_past_the_deadline(self):
        DEADLINE.begin(15)
        self.clock.advance(13)
        def busy(method, path, body):
            raise urllib.error.HTTPError(path, 429, "Too Many Requests", {"Retry-After": "3"}, None)
        with Kube(NODE) as kube:
            kube.react("GET", "/api/v1/nodes/node-a", busy)
            with mock.patch.object(K, "transport", self.recording(kube)), \
                 mock.patch.object(K.time, "sleep") as slept:
                with self.assertRaises(DEADLINE.Spent):
                    K.kget("/api/v1/nodes/node-a")
        self.assertEqual(1, len(self.timeouts))
        slept.assert_not_called()

    def test_a_429_is_retried_while_there_is_time(self):
        DEADLINE.begin(15)
        with Kube(NODE) as kube:
            kube.fail("/api/v1/nodes/node-a", 429, times=1)
            with mock.patch.object(K, "transport", self.recording(kube)), mock.patch.object(K.time, "sleep"):
                self.assertEqual("node-a", K.kget("/api/v1/nodes/node-a")["metadata"]["name"])
        self.assertEqual(2, len(self.timeouts))

    def test_plain_reads_are_given_what_is_left_too(self):
        DEADLINE.begin(15)
        self.clock.advance(10)
        seen = []
        with mock.patch.object(K, "_request", lambda m, p, d, h, timeout: seen.append(timeout) or b"{}"):
            K.raw_get("/api/v1/namespaces/lab/pods/web/log")
            K.ktable("/api/v1/pods")
        self.assertEqual([5, 5], seen)

    def test_a_write_keeps_its_own_timeout(self):
        DEADLINE.begin(15)
        self.clock.advance(14)
        with Kube() as kube, mock.patch.object(K, "transport", self.recording(kube)):
            K.ksend("POST", "/api/v1/namespaces/lab/configmaps",
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "lab"}})
        self.assertEqual([15], self.timeouts)


class TrickleTests(DeadlineCase):
    """A socket timeout bounds each read from the socket, not the whole
    answer: an API server sending a byte a second never times out. The body
    is read in pieces with the deadline checked between them."""

    def connection(self, pieces, seconds_each):
        clock, test = self.clock, self

        class Response:
            status, reason, headers, will_close = 200, "OK", {}, True

            def __init__(self):
                self.left = pieces

            def read1(self, n=-1):
                if self.left is None:       # hangs: the socket's timeout passes
                    clock.advance(test.opened.sock.settimeout.call_args.args[0])
                    raise TimeoutError("timed out")
                if not self.left:
                    return b""
                self.left -= 1
                clock.advance(seconds_each)
                return b" "

            def read(self, n=None):
                body = b""
                while piece := self.read1():
                    body += piece
                return body

        class Connection:
            timeout = 10

            def __init__(self):
                self.sock = mock.Mock()
                self.closed = False

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                self.closed = True
        self.opened = Connection()
        test.enterContext(mock.patch.object(K, "_take", lambda timeout: None))
        test.enterContext(mock.patch.object(K, "_open", lambda timeout: self.opened))

    def test_a_trickling_answer_is_cut_at_the_deadline(self):
        self.connection(pieces=100, seconds_each=1)    # 100 s in all, each piece well within 10 s
        DEADLINE.begin(15)
        with self.assertRaises(DEADLINE.Spent):
            K.kget("/api/v1/pods")
        self.assertLessEqual(self.clock.now - 1000, 15)
        self.assertTrue(self.opened.closed)            # half read: never back in the pool
        self.assertEqual(1, DEADLINE.cuts())

    def test_each_piece_waits_at_most_what_is_left(self):
        self.connection(pieces=3, seconds_each=1)
        DEADLINE.begin(15)
        self.clock.advance(11)
        self.assertEqual(b"   ", K._request("GET", "/api/v1/pods", None, {}, 10))
        waits = [c.args[0] for c in self.opened.sock.settimeout.call_args_list]
        self.assertEqual([4, 3, 2, 1], waits)

    def test_a_body_that_stops_coming_is_cut_at_the_deadline(self):
        self.connection(pieces=None, seconds_each=0)
        DEADLINE.begin(15)
        self.clock.advance(12)
        with self.assertRaises(DEADLINE.Spent):
            K._request("GET", "/api/v1/pods", None, {}, 10)
        self.assertEqual(1015.0, self.clock.now)

    def test_without_a_deadline_the_whole_answer_is_read(self):
        self.connection(pieces=100, seconds_each=1)
        self.assertEqual(b" " * 100, K._request("GET", "/api/v1/pods", None, {}, 10))


class CacheTests(DeadlineCase):
    def setUp(self):
        super().setUp()
        CACHE.forget_where(lambda key: key.startswith("deadline-test"))
        self.addCleanup(CACHE.forget_where, lambda key: key.startswith("deadline-test"))

    def partial(self):
        """A computation that swallows a read error, as many pages do."""
        try:
            K.kget("/api/v1/nodes")
        except Exception:
            pass
        return {"nodes": []}

    def test_an_answer_some_of_whose_reads_were_cut_is_not_kept(self):
        DEADLINE.begin(15)
        self.clock.advance(15)
        with Kube(NODE):
            with self.assertRaises(DEADLINE.Spent):
                CACHE.cached("deadline-test-a", 20, self.partial)
        self.assertIsNone(CACHE._last("deadline-test-a"))
        self.assertTrue(DEADLINE.cuts())

    def test_the_last_complete_answer_is_served_and_the_cut_forgiven(self):
        CACHE.keep("deadline-test-b", {"nodes": ["node-a"]}, CACHE.epoch("deadline-test-b"), at=0)
        DEADLINE.begin(15)
        self.clock.advance(15)
        with Kube(NODE):
            self.assertEqual({"nodes": ["node-a"]}, CACHE.cached("deadline-test-b", 20, self.partial))
        self.assertEqual(0, DEADLINE.cuts())

    def test_a_waiter_waits_only_for_what_is_left(self):
        DEADLINE.begin(15)
        self.clock.advance(11)
        flight = CACHE._Flight(0)
        flight.owner = -1
        CACHE._running["deadline-test-c"] = flight
        self.addCleanup(CACHE._running.pop, "deadline-test-c", None)
        waited = []
        with mock.patch.object(flight.done, "wait", lambda seconds: waited.append(seconds) or False):
            with self.assertRaises(DEADLINE.Spent):
                CACHE.cached("deadline-test-c", 20, dict)
        self.assertEqual([4], waited)

    def test_a_waiter_without_a_deadline_waits_as_before(self):
        flight = CACHE._Flight(0)
        flight.owner = -1
        CACHE._running["deadline-test-d"] = flight
        self.addCleanup(CACHE._running.pop, "deadline-test-d", None)
        waited = []
        with mock.patch.object(flight.done, "wait", lambda seconds: waited.append(seconds) or False):
            with self.assertRaises(TimeoutError):
                CACHE.cached("deadline-test-d", 20, dict)
        self.assertEqual([CACHE.WAIT_SECONDS], waited)


class AnswerTests(DeadlineCase):
    route = MODULE_ROUTES.Route("GET", "/api/x", "viewer", None, "test")

    def call(self, handler):
        return MODULE_ROUTES.call(self.route._replace(handler=handler), None)

    def test_a_route_whose_reads_were_cut_answers_504(self):
        DEADLINE.begin(15)
        self.clock.advance(15)

        def page(request):
            try:
                K.kget("/api/v1/nodes")
            except OSError:
                return {"nodes": []}  # looks complete, is not
        with Kube(NODE):
            answer = self.call(page)
        self.assertEqual(504, answer.code)
        self.assertIn("did not answer in time", answer.body["error"])
        self.assertTrue(answer.body["unavailable"])

    def test_a_route_that_raises_spent_answers_504(self):
        def page(request):
            raise DEADLINE.Spent("out of time")
        self.assertEqual(504, self.call(page).code)

    def test_a_route_in_time_answers_as_before(self):
        DEADLINE.begin(15)
        with Kube(NODE):
            self.assertEqual("node-a", self.call(lambda r: K.kget("/api/v1/nodes/node-a"))["metadata"]["name"])

    def test_a_download_streams_without_the_deadline(self):
        DEADLINE.begin(15)
        handler = mock.Mock(_extra_headers=[])
        left = []
        handler._send_stream = lambda stream: left.append(DEADLINE.remaining())
        MODULE_ROUTES.answer(handler, MODULE_ROUTES.Stream(iter([b"x"]), "application/octet-stream", "x", None),
                             "homestead_addons")
        self.assertEqual([None], left)


class ScopeTests(DeadlineCase):
    def handler(self, method, path):
        return mock.Mock(command=method, path=path)

    def test_a_get_request_gets_the_deadline(self):
        SCOPE.begin(self.handler("GET", "/api/overview?x=1"))
        self.assertEqual(DEADLINE.SECONDS, DEADLINE.remaining())

    def test_other_methods_and_long_connections_get_none(self):
        for method, path in (("POST", "/api/apps/restart"), ("DELETE", "/api/apps"),
                             ("GET", "/api/console"), ("GET", "/api/node/shell?node=node-a"),
                             ("GET", "/api/vm/console")):
            with self.subTest(method=method, path=path):
                SCOPE.begin(self.handler(method, path))
                self.assertIsNone(DEADLINE.remaining())

    def test_the_deadline_ends_with_the_request(self):
        SCOPE.begin(self.handler("GET", "/api/overview"))
        SCOPE.unbind()
        self.assertIsNone(DEADLINE.remaining())
        self.assertEqual(0, DEADLINE.cuts())


def handler(path, method="GET"):
    h = object.__new__(server.H)
    h.command, h.path, h.headers = method, path, {}
    h._send = mock.Mock()
    h._who = mock.Mock(return_value={"user": "viewer1", "role": "viewer", "remember": False, "started": 0})
    h.client_address = ("192.0.2.10", 40000)
    return h


class HungApiServerTests(DeadlineCase):
    """/api/overview, cold on a standby, through the real handler, against
    the shared fake with every call delayed on a fake clock."""

    def setUp(self):
        super().setUp()
        self.addCleanup(SCOPE.unbind)
        self.enterContext(mock.patch.object(server, "_self_data_boot_pending", False))
        self.enterContext(mock.patch.object(RESTARTS, "observe_later", lambda pods: True))
        self.enterContext(mock.patch.object(K.time, "sleep", lambda seconds: self.clock.advance(seconds)))
        for name, value in (("kget", K.kget), ("ksend", K.ksend),
                            ("v2_status", server.STORAGE_CLASSES.v2_engine_status)):
            self.enterContext(mock.patch.object(server.LHCAP, name, value))
        clean_snapshot(self)
        clean_cache(self)
        self.calls = 0

    def overview(self, delay):
        """(code, seconds taken, body) with every call taking delay seconds,
        or hanging until its timeout when delay is None."""
        with Kube(*objects()) as kube:
            def slow(method, path, body=None, ctype=None, timeout=15):
                self.calls += 1
                if delay is None or delay > timeout:
                    self.clock.advance(timeout)
                    raise TimeoutError("timed out")
                self.clock.advance(delay)
                return kube(method, path, body, ctype, timeout)
            with mock.patch.object(K, "transport", slow):
                h = handler("/api/overview")
                started = self.clock.now
                h.do_GET()
                return h._send.call_args.args[0], self.clock.now - started, h._send.call_args.args[1]

    def test_a_hung_api_server_answers_an_error_within_the_budget(self):
        code, took, body = self.overview(None)
        self.assertGreaterEqual(code, 500, body)
        self.assertLessEqual(took, DEADLINE.SECONDS)

    def test_a_crawling_api_server_answers_504_at_the_budget(self):
        # 2 s a call: each within its own 10 s, all of them far past 15 s.
        code, took, body = self.overview(2)
        self.assertEqual(504, code, body)
        self.assertEqual(DEADLINE.MESSAGE, body["error"])
        self.assertLessEqual(took, DEADLINE.SECONDS)

    def test_a_slow_api_server_still_answers_the_page(self):
        code, took, body = self.overview(0.03)
        self.assertEqual(200, code, body)
        self.assertLess(took, DEADLINE.SECONDS)
        self.assertGreater(self.calls, 5)


if __name__ == "__main__":
    unittest.main()
