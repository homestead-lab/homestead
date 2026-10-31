"""Homestead keeps its connection to the Kubernetes API open between requests
(#705): a page that makes thirty reads opens one connection, not thirty, and
pays for one TLS handshake. A connection the API server closed while it sat
idle is replaced, and a read on it is asked once more; a write never is."""
import http.server
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server  # noqa: F401  (binds homestead_kube)
import homestead_kube as KUBE
import homestead_read_deadline as DEADLINE
import homestead_storage as STORAGE
import homestead_vm_list as VMLIST


class FakeApi(http.server.ThreadingHTTPServer):
    """An API server speaking HTTP/1.1 keep-alive that counts connections and
    requests. drop_after_reply closes a connection after one answer without
    saying so, as an idle timeout on the server does; drop_before_reply
    closes it after reading a request, without answering."""
    daemon_threads = True

    def __init__(self):
        self.connections = 0
        self.requests = []
        self.drop_after_reply = False
        self.drop_before_reply = False
        self.hang = threading.Event()      # set: read the request, never answer
        self.release = threading.Event()   # set at the end so hung handlers return
        self.gate = None                   # a Barrier: answer only once that many requests are in
        self.payload = None                # bytes: the answer to every request, in place of JSON
        self.trickle = 0                   # seconds between 100-byte pieces of the answer
        self.silent_upto = 0               # connections up to this number read requests, never answer
        self.stall_body = False            # set: send the headers, never the body
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1
            self.number = self.server.connections

    def answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with self.server.lock:
            self.server.requests.append((self.command, self.path, body))
        if self.server.drop_before_reply:
            self.close_connection = True
            return
        if self.server.hang.is_set() or self.number <= self.server.silent_upto:
            self.server.release.wait(10)
            self.close_connection = True
            return
        if self.server.gate is not None:
            self.server.gate.wait(5)
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/api/v1/secrets")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/missing"):
            payload, status = b'{"kind":"Status","code":404}', 404
        elif self.server.payload is not None:
            payload, status = self.server.payload, 200
        else:
            payload, status = json.dumps({"items": [], "path": self.path}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.server.stall_body:
            self.wfile.flush()
            self.server.release.wait(10)
            self.close_connection = True
            return
        if self.server.trickle:
            for start in range(0, len(payload), 100):
                self.wfile.write(payload[start:start + 100])
                self.wfile.flush()
                time.sleep(self.server.trickle)
        else:
            self.wfile.write(payload)
        if self.server.drop_after_reply:
            self.close_connection = True

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = answer


class ConnectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        self.addCleanup(self.api.release.set)
        host, port = self.api.server_address
        self.enterContext(mock.patch.object(KUBE, "API", f"http://{host}:{port}"))
        self.enterContext(mock.patch.object(KUBE, "TOKEN", "t0ken"))
        KUBE.close_connections()
        self.addCleanup(KUBE.close_connections)
        DEADLINE.end()   # no request's read deadline: these are background reads

    def test_reads_in_a_row_share_one_connection(self):
        for _ in range(5):
            self.assertEqual("/api/v1/pods", KUBE.transport("GET", "/api/v1/pods")["path"])
        self.assertEqual(5, len(self.api.requests))
        self.assertEqual(1, self.api.connections)

    def test_raw_get_and_table_reads_share_it_too(self):
        KUBE.transport("GET", "/api/v1/nodes")
        KUBE.raw_get("/api/v1/nodes")
        KUBE.ktable("/api/v1/nodes")
        self.assertEqual(1, self.api.connections)

    def test_the_token_is_sent_on_every_request(self):
        seen = []
        original = Handler.answer

        def answer(handler):
            seen.append(handler.headers.get("Authorization"))
            original(handler)
        self.enterContext(mock.patch.object(Handler, "answer", answer))
        self.enterContext(mock.patch.multiple(Handler, do_GET=answer))
        KUBE.transport("GET", "/api/v1/pods")
        KUBE.transport("GET", "/api/v1/pods")
        self.assertEqual(["Bearer t0ken", "Bearer t0ken"], seen)

    def test_a_connection_the_server_closed_while_idle_is_replaced_and_the_read_asked_again(self):
        self.api.drop_after_reply = True
        KUBE.transport("GET", "/api/v1/pods")
        time.sleep(0.1)
        self.assertEqual("/api/v1/nodes", KUBE.transport("GET", "/api/v1/nodes")["path"])
        self.assertEqual(2, self.api.connections)

    def test_a_write_is_never_sent_twice(self):
        KUBE.transport("GET", "/api/v1/pods")          # leaves a pooled connection
        self.api.drop_before_reply = True
        with self.assertRaises((urllib.error.URLError, OSError)):
            KUBE.transport("POST", "/api/v1/namespaces/lab/pods", {"kind": "Pod"}, "application/json")
        posts = [r for r in self.api.requests if r[0] == "POST"]
        self.assertEqual(1, len(posts))

    def test_an_error_answer_is_still_an_http_error_with_its_body(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            KUBE.transport("GET", "/missing/thing")
        self.assertEqual(404, caught.exception.code)
        self.assertIn(b'"code":404', caught.exception.read())
        # and the connection stays usable
        KUBE.transport("GET", "/api/v1/pods")
        self.assertEqual(1, self.api.connections)

    def test_threads_reading_at_once_each_get_a_connection_of_their_own(self):
        results, errors = [], []

        def read():
            try:
                results.append(KUBE.transport("GET", "/api/v1/pods")["path"])
            except Exception as error:  # pragma: no cover
                errors.append(error)
        threads = [threading.Thread(target=read) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual([], errors)
        self.assertEqual(6, len(results))

    def test_a_write_never_takes_a_pooled_connection(self):
        KUBE.transport("GET", "/api/v1/pods")
        KUBE.transport("POST", "/api/v1/namespaces/lab/pods", {"kind": "Pod"}, "application/json")
        self.assertEqual(2, self.api.connections)

    def test_a_changed_token_is_sent_at_once(self):
        seen = []
        original = Handler.answer

        def answer(handler):
            seen.append(handler.headers.get("Authorization"))
            original(handler)
        self.enterContext(mock.patch.multiple(Handler, do_GET=answer))
        KUBE.transport("GET", "/api/v1/pods")
        with mock.patch.object(KUBE, "TOKEN", "n3w"):
            KUBE.transport("GET", "/api/v1/pods")
        self.assertEqual(["Bearer t0ken", "Bearer n3w"], seen)

    def test_a_redirect_is_refused_not_followed(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            KUBE.transport("GET", "/redirect/somewhere")
        self.assertEqual(302, caught.exception.code)
        self.assertEqual(["/redirect/somewhere"], [r[1] for r in self.api.requests])

    def _full_pool(self):
        self.api.gate = threading.Barrier(16)
        threads = [threading.Thread(target=lambda: KUBE.transport("GET", "/api/v1/pods")) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.api.gate = None
        self.assertEqual(16, len(KUBE._idle))

    def test_a_pooled_read_that_times_out_is_not_retried_and_the_pool_is_dropped(self):
        # A full pool, then an API server that stops answering on every
        # connection it had, without closing them (a powered-off host, the
        # virtual IP moved): the one read that got no answer on a reused
        # connection drops them all, so the next read opens a fresh connection
        # rather than waiting a full timeout on each of the other 15.
        self._full_pool()
        self.api.silent_upto = self.api.connections
        started = time.monotonic()
        with self.assertRaises((urllib.error.URLError, OSError)):
            KUBE.transport("GET", "/api/v1/nodes", timeout=0.5)
        self.assertLess(time.monotonic() - started, 1.5, "one timeout, not one per pooled connection")
        self.assertEqual(1, len([r for r in self.api.requests if r[1] == "/api/v1/nodes"]))
        self.assertEqual([], KUBE._idle)
        started = time.monotonic()
        for _ in range(3):
            self.assertEqual("/api/v1/nodes", KUBE.transport("GET", "/api/v1/nodes", timeout=0.5)["path"])
        self.assertLess(time.monotonic() - started, 0.5, "no more waits on the silent connections")
        self.assertEqual(17, self.api.connections)

    def test_a_read_whose_body_times_out_drops_only_its_own_connection(self):
        # Headers came, the body did not in time (a slow metrics-server's big
        # answer): the API server is there, so the other idle one is kept.
        self._two_idle()
        self.api.stall_body = True
        with self.assertRaises((urllib.error.URLError, OSError)):
            KUBE.transport("GET", "/apis/metrics.k8s.io/v1beta1/nodes", timeout=0.3)
        self.assertEqual(1, len(KUBE._idle))

    def test_a_read_the_request_deadline_cuts_drops_only_its_own_connection(self):
        self._two_idle()
        self.api.silent_upto = self.api.connections
        self.addCleanup(DEADLINE.end)
        DEADLINE.begin(0.3)
        with self.assertRaises(TimeoutError):
            KUBE.raw_get("/api/v1/nodes")
        self.assertEqual(1, len(KUBE._idle))

    def test_a_connect_timeout_drops_the_pool(self):
        # Not even a connection: the API server is not there.
        self._two_idle()

        def connect(connection):
            raise TimeoutError("timed out")
        self.enterContext(mock.patch.object(KUBE, "_take", lambda timeout: None))
        self.enterContext(mock.patch.object(KUBE.http.client.HTTPConnection, "connect", connect))
        with self.assertRaises(TimeoutError):
            KUBE.transport("GET", "/api/v1/nodes", timeout=0.3)
        self.assertEqual([], KUBE._idle)

    def _two_idle(self):
        barrier = threading.Barrier(2)

        def read():
            barrier.wait(5)
            KUBE.transport("GET", "/api/v1/pods")
        threads = [threading.Thread(target=read) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(2, len(KUBE._idle))

    def test_a_connection_the_server_dropped_still_drops_the_whole_pool(self):
        # A dropped connection suggests the API server went away: every idle
        # one is then likely dead too, and is not tried in turn.
        self._two_idle()
        self.api.drop_before_reply = True
        with self.assertRaises((urllib.error.URLError, OSError)):
            KUBE.transport("GET", "/api/v1/nodes")
        self.assertEqual([], KUBE._idle)

    def test_sixteen_readers_at_once_all_keep_their_connections(self):
        # About forty background threads and 8-worker pools read at once; a
        # burst of 16 must not leave half of them to reconnect next time (#984).
        self.api.gate = threading.Barrier(16)

        def read():
            KUBE.transport("GET", "/api/v1/pods")
        for _ in range(2):
            threads = [threading.Thread(target=read) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertEqual(16, len(KUBE._idle))
        self.assertEqual(16, self.api.connections)

    def test_log_reads_share_the_pool_one_connection_per_worker(self):
        # Log search reads up to 60 containers with 8 workers (#984).
        from concurrent.futures import ThreadPoolExecutor
        self.api.payload = b"2026-01-01T00:00:00Z a line\n"
        with ThreadPoolExecutor(8) as pool:
            texts = list(pool.map(lambda n: KUBE.logsearch_read("lab", f"web-{n}", "web", 60, 1000), range(60)))
        self.assertEqual(["2026-01-01T00:00:00Z a line\n"] * 60, texts)
        self.assertEqual(60, len(self.api.requests))
        self.assertLessEqual(self.api.connections, 8)
        path = self.api.requests[0][1]
        self.assertIn("/api/v1/namespaces/lab/pods/web-", path)
        self.assertIn("sinceSeconds=60&limitBytes=1000&timestamps=true&container=web", path)

    def test_a_log_longer_than_its_limit_is_cut_and_its_connection_not_reused(self):
        self.api.payload = b"x" * 5000
        self.assertEqual("x" * 101, KUBE.logsearch_read("lab", "web-1", "web", 60, 100))
        self.assertEqual([], KUBE._idle, "the rest of the answer is unread: the connection is closed")
        self.api.payload = b"short"
        self.assertEqual("short", KUBE.logsearch_read("lab", "web-1", "web", 60, 100))
        self.assertEqual(2, self.api.connections)
        self.assertEqual(1, len(KUBE._idle))

    def test_a_log_cut_under_a_request_deadline_is_cut_the_same_way(self):
        self.addCleanup(DEADLINE.end)
        DEADLINE.begin(15)
        self.api.payload = b"x" * 200000
        self.assertEqual("x" * 1001, KUBE.logsearch_read("lab", "web-1", "web", 60, 1000))
        self.assertEqual([], KUBE._idle)

    def test_a_log_error_is_still_an_http_error(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            KUBE.raw_get("/missing/pods/web-1/log")
        self.assertEqual(404, caught.exception.code)

    def test_the_logs_view_reads_through_the_pool(self):
        import homestead_workloads as WORKLOADS
        from types import SimpleNamespace
        self.api.payload = b"2026-01-01T00:00:00Z a line\n"
        request = SimpleNamespace(query={"ns": ["lab"], "pod": ["web-1"], "container": ["web"], "tail": ["50"]},
                                  role="admin")
        for _ in range(3):
            reply = WORKLOADS.logs(request)
            self.assertEqual(200, reply.code)
        self.assertIn("a line", reply.body if isinstance(reply.body, str) else reply.body.decode())
        self.assertEqual(1, self.api.connections)    # the pod reads and the log reads alike
        self.assertEqual(f"/api/v1/namespaces/lab/pods/web-1/log?tailLines=50&timestamps=true"
                         f"&limitBytes={WORKLOADS.LOG_BYTES}&container=web",
                         [r[1] for r in self.api.requests if "/log?" in r[1]][0])

    def test_the_logs_view_refuses_a_name_that_would_change_the_request(self):
        import homestead_workloads as WORKLOADS
        from types import SimpleNamespace
        for ns, pod in (("kube-system/secrets?x=", "web-1"), ("lab", "web-1/../../secrets"),
                        ("lab", "web-1?limitBytes=1"), ("lab", "Web 1"), ("", "web-1"), ("lab/x", "")):
            reply = WORKLOADS.logs(SimpleNamespace(query={"ns": [ns], "pod": [pod], "container": ["web"]}, role="admin"))
            self.assertEqual(400, reply.code, (ns, pod))
        self.assertEqual([], self.api.requests)

    def test_the_logs_view_answers_a_read_out_of_time_as_unavailable(self):
        import homestead_workloads as WORKLOADS
        from types import SimpleNamespace
        request = SimpleNamespace(query={"ns": ["lab"], "pod": ["web-1"], "container": ["web"]}, role="admin")
        for error in (TimeoutError("timed out"), DEADLINE.Spent(DEADLINE.MESSAGE)):
            def late(*args, **kwargs):
                raise error
            with mock.patch.object(WORKLOADS, "raw_get", late):
                reply = WORKLOADS.logs(request)
            self.assertEqual((503, {"error": DEADLINE.MESSAGE, "unavailable": True}), (reply.code, reply.body))

    def test_a_plain_read_refuses_a_path_that_steps_out_of_its_object(self):
        for path in ("/api/v1/namespaces/lab/pods/../secrets/log", "/api/v1/namespaces/lab/pods/a%2fb/log"):
            with self.assertRaises(ValueError):
                KUBE.raw_get(path)
        self.assertEqual([], self.api.requests)

    def test_a_large_list_answered_slowly_but_steadily_is_not_cut(self):
        # A socket timeout bounds each read from the socket, not the whole
        # answer: a big list arriving steadily takes as long as it needs.
        self.api.payload = json.dumps({"items": [{"n": n} for n in range(150)]}).encode()
        self.api.trickle = 0.02
        started = time.monotonic()
        answer = KUBE.transport("GET", "/api/v1/pods", timeout=0.3)
        self.assertGreater(time.monotonic() - started, 0.3)
        self.assertEqual(150, len(answer["items"]))

    def test_a_connection_idle_for_over_a_minute_is_not_reused(self):
        clock = [1000.0]
        self.enterContext(mock.patch.object(KUBE.time, "monotonic", lambda: clock[0]))
        KUBE.transport("GET", "/api/v1/pods")
        clock[0] += 61
        KUBE.transport("GET", "/api/v1/pods")
        self.assertEqual(2, self.api.connections)


class TlsConnectionTests(unittest.TestCase):
    """The same over TLS, as the API server is reached: one handshake for many
    reads, and a different TLS context never shares a connection."""

    def setUp(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl is needed to make a throwaway certificate")
        folder = self.enterContext(tempfile.TemporaryDirectory())
        key, cert = os.path.join(folder, "k.pem"), os.path.join(folder, "c.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                        "-out", cert, "-days", "1", "-subj", "/CN=127.0.0.1"], check=True, capture_output=True)
        self.api = FakeApi()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.api.socket = context.wrap_socket(self.api.socket, server_side=True)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        host, port = self.api.server_address
        self.enterContext(mock.patch.object(KUBE, "API", f"https://{host}:{port}"))
        self.enterContext(mock.patch.object(KUBE, "TOKEN", "t0ken"))
        self.enterContext(mock.patch.object(KUBE, "CTX", ssl._create_unverified_context()))
        KUBE.close_connections()
        self.addCleanup(KUBE.close_connections)
        DEADLINE.end()   # no request's read deadline: these are background reads

    def test_reads_over_tls_share_one_connection(self):
        for _ in range(3):
            KUBE.transport("GET", "/api/v1/pods")
        self.assertEqual(1, self.api.connections)

    def test_a_new_tls_context_gets_a_new_connection(self):
        KUBE.transport("GET", "/api/v1/pods")
        with mock.patch.object(KUBE, "CTX", ssl._create_unverified_context()):
            KUBE.transport("GET", "/api/v1/pods")
        self.assertEqual(2, self.api.connections)



class ParallelListTests(unittest.TestCase):
    """Lists that do not depend on each other are read at the same time, and a
    list that fails still counts exactly as before."""

    def test_claim_references_reads_its_ten_lists_at_once(self):
        def slow(path, **kw):
            time.sleep(0.3)
            return {"items": [], "metadata": {}}
        self.enterContext(mock.patch.object(STORAGE, "kget", slow))
        started = time.monotonic()
        STORAGE.claim_references()
        self.assertLess(time.monotonic() - started, 1.0)  # ten reads one after another take 3 s

    def test_a_list_that_fails_is_still_unread_and_in_the_same_order(self):
        def answers(path, **kw):
            if "statefulsets" in path or "cronjobs" in path:
                raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
            return {"items": [], "metadata": {}}
        self.enterContext(mock.patch.object(STORAGE, "kget", answers))
        self.assertEqual(["StatefulSet", "CronJob"], STORAGE.claim_references()[STORAGE.UNREAD])

    def test_the_vm_list_reads_each_namespaces_claims_at_once(self):
        vms = {"items": [{"metadata": {"namespace": f"ns{i}", "name": f"vm{i}"}, "spec": {}, "status": {}}
                         for i in range(4)]}

        def slow(path, **kw):
            if path.endswith("/virtualmachines"):
                return vms
            if path.endswith("/virtualmachineinstances"):
                return {"items": [], "metadata": {}}
            time.sleep(0.3)
            return {"items": []}
        self.enterContext(mock.patch.object(VMLIST.V, "kget", slow))
        self.enterContext(mock.patch.object(VMLIST, "_row", lambda *a, **k: {"ns": a[0]["metadata"]["namespace"],
                                                                              "name": a[0]["metadata"]["name"], "running": False}))
        self.enterContext(mock.patch.object(VMLIST.VMUSAGE, "measured", lambda: ({}, "", None)))
        started = time.monotonic()
        VMLIST.list_vms()
        self.assertLess(time.monotonic() - started, 1.0)  # four namespaces and the DataVolumes in turn take 1.5 s


if __name__ == "__main__":
    unittest.main()
