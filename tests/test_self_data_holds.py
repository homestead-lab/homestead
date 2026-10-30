"""Homestead's own data move: holds that had no way out (stuck-jobs sweep, 30 Oct).

Each test drives the real coordinator, worker, recovery, fence or cleanup code
on the self-data Cluster fake from test_self_data_coordinator (or the shared
fake API in tests/fakes/kube.py) with a fake clock. Nothing sleeps."""
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_worker as W
from homestead_storage_journal import Held
from test_self_data_coordinator import Cluster


def refused(*_args, **_kwargs):
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


class TransientApiErrorTests(unittest.TestCase):
    """C3: an API blip while Homestead is stopped is not a durable hold."""

    def setUp(self):
        self.c = Cluster()
        self.now = 1000
        self.runner = W.Runner(self.c.read, self.c.send, lambda _: self.c.logs_text, self.c.admit,
                               namespace="lab", deployment="homestead", operation=self.c.handle["operation"],
                               anchor_uid=self.c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: self.now)

    def state(self):
        return self.c.fresh().load(**self.c.handle).state

    def test_unreachable_api_is_retried_not_latched(self):
        read = self.runner.read
        self.runner.read = refused
        view = self.runner.tick()
        self.assertNotEqual("held", view["status"])
        self.assertIsNone(self.runner.hold)
        self.runner.read = read
        self.assertEqual("quiesce", self.runner.tick()["phase"])
        self.assertNotEqual("held", self.state()["runtime"]["state"])

    def test_lost_heartbeat_reply_is_retried_from_the_durable_record(self):
        c = self.c
        c.lost = lambda m, p, b: p == c.anchor.path and json.loads(b["data"]["state.json"]).get("runtime", {}).get("state") == "running"
        self.assertNotEqual("held", self.runner.tick()["status"])
        c.lost = None
        stops = sum(p == c.dep_path for _, p, _ in c.sent)
        self.assertEqual("quiesce", self.runner.tick()["phase"])
        self.assertEqual(stops, sum(p == c.dep_path for _, p, _ in c.sent))
        self.assertEqual("running", self.state()["runtime"]["state"])

    def test_outage_longer_than_the_bound_holds_with_its_reason(self):
        self.runner.read = refused
        self.runner.tick()
        self.now += W.OUTAGE_LIMIT - 1
        self.assertIsNone(self.runner.hold)
        self.runner.tick()
        self.now += 1
        self.runner.tick()
        self.assertIn("did not answer", self.runner.hold)
        self.runner.read = self.c.read
        self.assertEqual("held", self.runner.tick()["status"])
        self.assertIn("did not answer", self.state()["runtime"]["message"])

    def test_uncertain_workload_write_still_holds_at_once(self):
        c = self.c
        c.lost = lambda m, p, b: p == c.dep_path
        self.assertEqual("held", self.runner.tick()["status"])
        c.lost = None
        self.assertEqual("held", self.runner.tick()["status"])
        self.assertEqual(1, sum(p == c.dep_path for _, p, _ in c.sent))

    def test_http_server_errors_are_transient_but_refusals_are_not(self):
        def error(code):
            raise urllib.error.HTTPError("/x", code, "x", {}, None)
        for code, transient in ((503, True), (429, True), (500, True), (404, False), (409, False), (403, False)):
            try:
                try: error(code)
                except Exception: raise Held("wrapped") from None
            except Held as held:
                self.assertEqual(transient, W.transient(held), code)
        self.assertFalse(W.transient(Held("A data volume has an unreviewed consumer")))
        self.assertFalse(W.transient(ValueError("bad")))

    def test_the_move_client_marks_an_unanswered_request_and_nothing_else(self):
        from test_self_data_kube import ClientTests
        case = ClientTests("test_response_from_another_origin_is_never_trusted"); case.setUp()
        self.addCleanup(case.doCleanups)
        for failure, transient in ((TimeoutError("private endpoint"), True),
                                   (urllib.error.URLError(ConnectionResetError()), True),
                                   (ValueError("private JSON"), False)):
            case.open.side_effect = failure
            with self.assertRaises(Held) as caught: case.client.read("/api/v1/nodes")
            self.assertEqual(transient, W.transient(caught.exception), failure)
            self.assertNotIn("private", str(caught.exception))


class HelperCleanupTests(unittest.TestCase):
    """C8: a helper DELETE that never reached the API is sent again, not a hold."""

    def setUp(self):
        from test_self_data_finish import CleanupTests
        self.fixture = CleanupTests("test_replaced_helper_is_retained"); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c = self.fixture.c

    def test_delete_that_never_took_effect_is_sent_again_pinned_to_its_uid(self):
        start = len(self.c.sent)
        self.c.reject = lambda method, path, body: method == "DELETE"
        with self.assertRaises(TimeoutError): self.fixture.finish()
        first = [(p, b["preconditions"]) for m, p, b in self.c.sent[start:] if m == "DELETE"]
        self.c.reject = None
        self.assertTrue(self.fixture.finish()["done"])
        again = [(p, b["preconditions"]) for m, p, b in self.c.sent[start:] if m == "DELETE" and p == first[0][0]]
        self.assertEqual([first[0], first[0]], again)
        self.assertNotIn(self.fixture.anchor.path, self.c.objects)

    def test_a_delete_already_under_way_is_waited_for_not_repeated(self):
        self.c.reject = lambda method, path, body: method == "DELETE"
        with self.assertRaises(TimeoutError): self.fixture.finish()
        self.c.reject = None
        path = [p for m, p, _ in self.c.sent if m == "DELETE"][-1]
        self.c.objects[path]["metadata"]["deletionTimestamp"] = "1970-01-01T00:16:40Z"
        count = len(self.c.sent)
        self.assertFalse(self.fixture.finish()["done"])
        self.assertEqual(count, len(self.c.sent))

    def test_the_cleanup_loop_retries_an_api_error_a_bounded_number_of_times(self):
        import server
        calls = []
        def finish(*_):
            calls.append(1)
            if len(calls) < 3: raise urllib.error.URLError("refused")
            return {"done": True}
        with mock.patch.object(server, "_self_data_fence", object()), \
             mock.patch.object(server.SELF_DATA_FINISH, "finish", side_effect=finish), \
             mock.patch.object(server.SELF_DATA_EXECUTE, "reconcile_completed") as reconcile, \
             mock.patch.object(server.time, "sleep") as sleep:
            server.finish_self_data_helpers()
        self.assertEqual(3, len(calls)); reconcile.assert_called_once()
        calls.clear()
        with mock.patch.object(server, "_self_data_fence", object()), \
             mock.patch.object(server.SELF_DATA_FINISH, "finish", side_effect=urllib.error.URLError("down")) as down, \
             mock.patch.object(server.time, "sleep"):
            server.finish_self_data_helpers()
        self.assertEqual(server.SELF_DATA_FINISH.ATTEMPTS, down.call_count)


if __name__ == "__main__":
    unittest.main()
