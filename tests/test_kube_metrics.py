"""metrics-server out of the request path (#906 item 1): each replica reads
metrics-server in the background with a short timeout and keeps the last
reading, and Hosts and Apps serve it with its state, so a metrics-server that
hangs no longer holds a page back and one that fails is no longer silent."""
import io
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_kube_metrics as M

NODE_ITEMS = [{"metadata": {"name": "node-a"}, "usage": {"cpu": "500m", "memory": "2Gi"}},
              {"metadata": {"name": "node-b"}, "usage": {"cpu": "1", "memory": "1Gi"}}]
POD_ITEMS = [{"metadata": {"namespace": "lab", "name": "web-1"},
              "containers": [{"name": "c", "usage": {"cpu": "50m", "memory": "64Mi"}}]}]


def answering(calls=None):
    def get(path, **kwargs):
        if calls is not None:
            calls.append((path, kwargs))
        return {"items": NODE_ITEMS if path == M.NODES else POD_ITEMS}
    return get


def failing(error):
    def get(path, **kwargs):
        raise error
    return get


def http(code):
    return urllib.error.HTTPError(M.NODES, code, "x", {}, io.BytesIO(b"{}"))


def must_not_read(path, **kwargs):
    raise AssertionError(f"read {path} in the request")


class Clean(unittest.TestCase):
    def setUp(self):
        M.stop()
        M.reset()
        self.addCleanup(M.reset)
        self.addCleanup(M.stop)

    def looping(self):
        """As if this replica's loop were running."""
        self.enterContext(mock.patch.object(M, "running", lambda: True))


class WithoutTheLoopTests(Clean):
    """Before the loop starts, and in tests: read now, with the short timeout."""

    def test_reads_now_with_the_short_timeout(self):
        calls = []
        items, status = M.read("nodes", answering(calls), now=1000.0)
        self.assertEqual(NODE_ITEMS, items)
        self.assertEqual([(M.NODES, {"timeout": M.TIMEOUT})], calls)
        self.assertLessEqual(M.TIMEOUT, 2)
        self.assertEqual({"state": "ok", "last_ok": 1000.0, "age_s": 0, "detail": ""}, status)

    def test_a_metrics_server_that_does_not_answer_is_said_not_silent(self):
        items, status = M.read("pods", failing(TimeoutError("timed out")), now=1000.0)
        self.assertEqual([], items)
        self.assertEqual("unavailable", status["state"])
        self.assertIsNone(status["last_ok"])
        self.assertIn("no answer", status["detail"])

    def test_a_refusal_names_its_code(self):
        items, status = M.read("nodes", failing(http(503)))
        self.assertEqual(([], "unavailable"), (items, status["state"]))
        self.assertEqual("HTTP 503", status["detail"])

    def test_no_metrics_api_at_all_is_absent_not_unavailable(self):
        items, status = M.read("nodes", failing(http(404)))
        self.assertEqual(([], "absent"), (items, status["state"]))

    def test_nothing_is_kept_between_reads(self):
        M.read("nodes", answering())
        items, status = M.read("nodes", failing(http(503)))
        self.assertEqual(([], "unavailable"), (items, status["state"]))


class WithTheLoopTests(Clean):
    def test_a_request_serves_the_last_reading_and_never_reads(self):
        M.run_round(answering(), now=1000.0)
        self.looping()
        items, status = M.read("nodes", must_not_read, now=1004.0)
        self.assertEqual(NODE_ITEMS, items)
        self.assertEqual(("ok", 4), (status["state"], status["age_s"]))
        self.assertEqual(POD_ITEMS, M.read("pods", must_not_read, now=1004.0)[0])

    def test_a_round_reads_each_kind_once_with_the_short_timeout(self):
        calls = []
        M.run_round(answering(calls))
        self.assertEqual([(M.NODES, {"timeout": M.TIMEOUT}), (M.PODS, {"timeout": M.TIMEOUT})], calls)

    def test_after_it_stops_answering_the_last_reading_is_served_as_stale(self):
        M.run_round(answering(), now=1000.0)
        M.run_round(failing(TimeoutError("timed out")), now=1030.0)
        self.looping()
        items, status = M.read("nodes", must_not_read, now=1040.0)
        self.assertEqual(NODE_ITEMS, items)
        self.assertEqual({"state": "stale", "last_ok": 1000.0, "age_s": 40}, {k: status[k] for k in ("state", "last_ok", "age_s")})
        self.assertIn("no answer", status["detail"])

    def test_a_reading_older_than_keep_for_is_no_longer_served(self):
        M.run_round(answering(), now=1000.0)
        M.run_round(failing(http(503)), now=1000.0 + M.KEEP_FOR)
        self.looping()
        items, status = M.read("nodes", must_not_read, now=1001.0 + M.KEEP_FOR)
        self.assertEqual([], items)
        self.assertEqual(("unavailable", 1000.0), (status["state"], status["last_ok"]))

    def test_an_answer_after_a_failure_is_ok_again(self):
        M.run_round(failing(http(503)), now=1000.0)
        M.run_round(answering(), now=1015.0)
        self.looping()
        self.assertEqual("ok", M.read("pods", must_not_read, now=1016.0)[1]["state"])

    def test_before_the_first_round_a_request_waits_briefly_then_says_waiting(self):
        self.looping()
        self.enterContext(mock.patch.object(M, "FIRST_WAIT", 0.05))
        started = time.monotonic()
        items, status = M.read("nodes", must_not_read)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(([], "waiting"), (items, status["state"]))

    def test_the_loop_reads_in_the_background_and_starts_once(self):
        read = threading.Event()

        def get(path, **kwargs):
            read.set()
            return {"items": NODE_ITEMS if path == M.NODES else POD_ITEMS}
        M.start(get)
        first = M._loop["thread"]
        M.start(get)
        self.assertIs(first, M._loop["thread"])
        self.assertTrue(read.wait(5))
        self.assertTrue(M._loop["done"].wait(5))
        self.assertEqual(NODE_ITEMS, M.read("nodes", must_not_read)[0])
        M.stop()
        self.assertFalse(M.running())

    def test_a_read_that_raises_is_recorded_as_unavailable(self):
        M.run_round(failing(RuntimeError("boom")))
        self.looping()
        self.assertEqual("unavailable", M.read("nodes", must_not_read)[1]["state"])

    def test_a_round_that_raises_does_not_end_the_loop(self):
        rounds = []

        def round_():
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("boom")
            M._loop["stop"].set()
        with mock.patch.object(M, "run_round", round_), mock.patch.object(M, "INTERVAL", 0):
            M._loop["stop"].clear()
            M._run()
        self.assertEqual(2, len(rounds))

    def test_ok_figures_the_loop_has_not_refreshed_for_three_rounds_are_stale(self):
        # A loop stuck in a read that never returns must not leave "ok" forever.
        M.run_round(answering(), now=1000.0)
        self.looping()
        self.assertEqual("ok", M.read("nodes", must_not_read, now=1000.0 + 3 * M.INTERVAL - 1)[1]["state"])
        items, status = M.read("nodes", must_not_read, now=1000.0 + 3 * M.INTERVAL)
        self.assertEqual((NODE_ITEMS, "stale"), (items, status["state"]))
        self.assertIn("not read since", status["detail"])


class PagesTests(unittest.TestCase):
    """Hosts and Apps take their figures from the loop's reading and say its state."""

    def setUp(self):
        import test_page_snapshot as T
        M.stop()
        M.reset()
        self.addCleanup(M.reset)
        T.clean_cache(self)
        self.cluster = T.Cluster(self)
        T.clean_snapshot(self)
        import homestead_kube as K
        inner = K.transport
        self.metrics_reads = []

        def transport(method, path, *args, **kwargs):
            if "/apis/metrics.k8s.io/" in path:
                self.metrics_reads.append(path)
                raise TimeoutError("timed out")       # a hung metrics-server
            return inner(method, path, *args, **kwargs)
        self.enterContext(mock.patch.object(K, "transport", transport))
        import homestead_storage as STORAGE
        self.enterContext(mock.patch.object(STORAGE.VOLUME_USAGE, "collect", lambda get, claims: {}))

    def test_hosts_serve_the_last_reading_without_asking_metrics_server(self):
        M.run_round(answering())
        self.enterContext(mock.patch.object(M, "running", lambda: True))
        rows = {n["name"]: n for n in self.cluster.route("nodes")}
        self.assertEqual([], self.metrics_reads)
        self.assertEqual(0.5, rows["node-a"]["cpu_used"])
        self.assertEqual("ok", rows["node-a"]["metrics"]["state"])

    def test_apps_serve_the_last_reading_without_asking_metrics_server(self):
        M.run_round(answering())
        self.enterContext(mock.patch.object(M, "running", lambda: True))
        row = self.cluster.route("wl")[0]
        self.assertEqual([], self.metrics_reads)
        self.assertEqual(0.05, row["cpu"])
        self.assertEqual("ok", row["metrics"]["state"])

    def test_without_the_loop_a_hung_metrics_server_costs_the_short_timeout_and_is_said(self):
        rows = self.cluster.route("nodes")
        self.assertEqual(["/apis/metrics.k8s.io/v1beta1/nodes"], self.metrics_reads)
        self.assertEqual("unavailable", rows[0]["metrics"]["state"])
        self.assertEqual("unavailable", self.cluster.route("wl")[0]["metrics"]["state"])

    def test_the_loop_starts_with_the_node_probes(self):
        import homestead_nodes as NODES
        with mock.patch.object(NODES.PROBES, "start") as probes, mock.patch.object(M, "start") as metrics:
            NODES.start_probes()
        probes.assert_called_once()
        metrics.assert_called_once()


if __name__ == "__main__":
    unittest.main()
