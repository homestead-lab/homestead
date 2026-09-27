import hashlib
import http.client
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_worker as W
from homestead_storage_journal import Held
from test_self_data_coordinator import Cluster


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster()
        self.now = 1000
        self.runner = self.fresh()

    def fresh(self, **overrides):
        args = dict(namespace="lab", deployment="homestead", operation=self.c.handle["operation"],
                    anchor_uid=self.c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: self.now)
        args.update(overrides)
        return W.Runner(self.c.read, self.c.send, lambda _: self.c.logs_text, self.c.admit, **args)

    def state(self):
        return self.c.fresh().load(**self.c.handle).state

    def test_full_worker_handoff_survives_new_runner_each_tick_without_data_files(self):
        c = self.c
        def tick():
            with mock.patch("builtins.open", side_effect=AssertionError("worker must not read the data volume")):
                return self.fresh().tick()
        self.assertEqual("quiesce", tick()["phase"])
        c.settle_stop()
        self.assertEqual("copy", tick()["phase"])
        self.assertEqual("copy", tick()["phase"])
        c.finish_copy()
        self.assertEqual("verify", tick()["phase"])
        tick()
        c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        self.assertEqual("switch", tick()["phase"])
        self.assertEqual("start", tick()["phase"])
        c.settle_stop(); tick(); c.settle_start()
        view = tick()
        self.assertEqual("done", view["status"])
        self.assertTrue(all(s["state"] == "complete" for s in view["stages"]))
        self.assertEqual("keep_both_volumes", view["retention_policy"])
        self.assertIsNone(view["copy_percent"])
        self.assertEqual("done", self.state()["runtime"]["state"])
        writes = len(c.sent)
        tick()
        self.assertEqual(writes, len(c.sent))

    def test_progress_polling_cannot_start_or_resume_work(self):
        before = len(self.c.sent)
        for _ in range(3): self.runner.snapshot()
        self.assertEqual(before, len(self.c.sent))
        self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])

    def test_phase_label_alone_does_not_prove_completion(self):
        anchor = self.c.fresh().load(**self.c.handle)
        anchor.state["phase"] = "done"
        view = W.progress(anchor, self.now)
        self.assertEqual("held", view["status"])
        self.assertEqual("current", view["stages"][-1]["state"])
        self.assertTrue(view["requires_review"])

    def test_preparing_worker_does_not_race_setup_checkpoints(self):
        self.c = Cluster(published=False)
        before = len(self.c.sent)
        result = self.fresh().tick()
        self.assertEqual("preparing", result["status"])
        self.assertEqual(before, len(self.c.sent))

    def test_durable_hold_prevents_restart_after_condition_clears(self):
        self.c.allow = False
        self.assertEqual("held", self.runner.tick()["status"])
        self.assertEqual("held", self.state()["runtime"]["state"])
        self.c.allow = True
        before = len(self.c.sent)
        self.assertEqual("held", self.fresh().tick()["status"])
        self.assertEqual(before, len(self.c.sent))
        with self.assertRaisesRegex(Held, "recovery review"): self.c.step()
        with self.assertRaises(Held):
            self.c.fresh().load(**self.c.handle).report("coordinator-uid", self.now, "running", "Overwrite hold")

    def test_lost_stop_reply_is_held_without_replaying_or_rolling_back(self):
        self.c.lost = lambda method, path, body: path == self.c.dep_path
        result = self.runner.tick()
        self.assertEqual("held", result["status"])
        self.assertTrue(result["requires_review"])
        self.c.lost = None
        self.c.settle_stop()
        self.fresh().tick()
        self.assertEqual(1, sum(path == self.c.dep_path for _, path, _ in self.c.sent))
        self.assertNotIn(self.c.job_path, self.c.objects)

    def test_api_outage_latches_hold_then_saves_it_without_resuming(self):
        read = self.runner.read
        self.runner.read = mock.Mock(side_effect=OSError("private URL and token"))
        view = self.runner.tick()
        self.assertEqual("unknown", view["status"])
        self.assertNotIn("private", json.dumps(view))
        self.runner.read = read
        self.assertEqual("held", self.runner.tick()["status"])
        self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])
        self.assertEqual("held", self.fresh().tick()["status"])

    def test_unexpected_error_is_not_persisted_or_exposed_raw(self):
        self.runner.admit = mock.Mock(side_effect=RuntimeError("private credentials"))
        view = self.runner.tick()
        self.assertEqual("held", view["status"])
        self.assertNotIn("private", json.dumps(view) + json.dumps(self.state()))

    def test_replaced_worker_cannot_write_status_or_mutate_workloads(self):
        before = len(self.c.sent)
        self.assertEqual("held", self.fresh(worker_uid="other-pod-uid").tick()["status"])
        self.assertEqual(before, len(self.c.sent))

    def test_lost_status_checkpoint_reply_is_not_success_or_a_retry(self):
        self.c.lost = lambda method, path, body: path == self.c.anchor.path and json.loads(body["data"]["state.json"]).get("runtime", {}).get("state") == "running"
        view = self.runner.tick()
        self.assertEqual("held", view["status"])
        self.c.lost = None
        before = len(self.c.sent)
        self.fresh().tick()
        self.assertEqual(before, len(self.c.sent))
        self.assertNotIn(self.c.job_path, self.c.objects)

    def test_stale_heartbeat_is_not_reported_as_running(self):
        self.runner.tick()
        self.now += 61
        view = self.runner.snapshot()
        self.assertEqual("unknown", view["status"])
        self.assertTrue(view["stale"])
        self.assertEqual(1000, view["checked_at"])
        self.assertEqual("quiesce", view["phase"])

    def test_hold_survives_status_failure_in_same_process(self):
        self.c.allow = False
        self.c.reject = lambda method, path, body: path == self.c.anchor.path
        self.assertEqual("held", self.runner.tick()["status"])
        self.c.reject = None; self.c.allow = True
        self.assertEqual("held", self.runner.tick()["status"])
        self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])

    def test_concurrent_tick_only_reads_status(self):
        before = len(self.c.sent)
        with self.runner.lock:
            self.runner.tick()
        self.assertEqual(before, len(self.c.sent))

    def test_loop_stop_interrupts_wait_and_does_not_start_another_tick(self):
        stop = threading.Event()
        self.runner.tick = mock.Mock(side_effect=stop.set)
        thread = threading.Thread(target=self.runner.run, args=(stop, 30), daemon=True)
        thread.start(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.runner.tick.assert_called_once()


class StatusHttpTests(unittest.TestCase):
    def setUp(self):
        self.runner = mock.Mock(operation="a" * 24)
        self.runner.snapshot.return_value = {"status": "running", "phase": "copy", "copy_percent": None}
        self.token = "b" * 64
        self.http = W.status_server(("127.0.0.1", 0), self.runner, hashlib.sha256(self.token.encode()).hexdigest())
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.path = "/api/self/data/handoff/" + self.runner.operation

    def close(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join(1)

    def request(self, method="GET", path=None, token=None):
        conn = http.client.HTTPConnection(*self.http.server_address, timeout=2)
        try:
            headers = {"Authorization": "Bearer " + token} if token else {}
            conn.request(method, path or self.path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            conn.close()

    def test_progress_requires_exact_operation_scoped_token_in_header(self):
        for path, token, code in ((self.path, None, 401), (self.path, "c" * 64, 401),
                                   (self.path + "?token=" + self.token, None, 404),
                                   (self.path.replace("a" * 24, "d" * 24), self.token, 404)):
            self.assertEqual(code, self.request(path=path, token=token)[0])
        self.runner.snapshot.assert_not_called()
        status, headers, body = self.request(token=self.token)
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("no-referrer", headers["Referrer-Policy"])
        self.assertEqual("copy", body["phase"])
        self.runner.tick.assert_not_called()

    def test_mutations_are_refused_even_with_valid_token(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(405, self.request(method=method, token=self.token)[0])
        self.runner.snapshot.assert_not_called()
        self.runner.tick.assert_not_called()

    def test_health_is_progress_server_only_not_homestead_readiness(self):
        status, _, body = self.request(path="/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"service": "data-move-progress"}, body)
        self.runner.snapshot.assert_not_called()

    def test_unknown_state_is_not_a_success_response(self):
        self.runner.snapshot.return_value = {"status": "unknown", "phase": None}
        status, _, body = self.request(token=self.token)
        self.assertEqual(503, status)
        self.assertIsNone(body["phase"])


if __name__ == "__main__":
    unittest.main()
