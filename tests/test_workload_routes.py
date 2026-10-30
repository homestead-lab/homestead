"""Workload, deploy and move routes answered by their modules (#363): the
same status codes server.py's own branches gave."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_deploy_appstore as APPSTORE  # noqa: E402


class ThroughTheServer(unittest.TestCase):
    def handler(self, method, path, body=None):
        import server
        handler = object.__new__(server.H)
        handler.path, handler.headers, handler.command = path, {}, method
        handler._guard = lambda path: False
        handler._body = lambda: body or {}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        handler.user, handler.role = "ada", "admin"
        return server, handler

    def test_a_refused_start_is_a_409_with_its_plan(self):
        server, handler = self.handler("POST", "/api/scale", {"ns": "lab", "name": "web", "replicas": 1})
        refused = server.WORKLOADS.WorkloadRefused("review node memory before starting", {"blocked": False})
        with mock.patch.object(server.OVERVIEW_ROUTES, "scale_workload", side_effect=refused):
            handler.do_POST()
        code, body = handler._send.call_args.args[:2]
        self.assertEqual(409, code)
        self.assertEqual({"blocked": False}, body["plan"])

    def test_the_app_store_without_its_feed_is_a_502(self):
        server, handler = self.handler("GET", "/api/appstore")
        with mock.patch.object(APPSTORE, "fetch_appstore", side_effect=OSError("offline")):
            handler.do_GET()
        code, body = handler._send.call_args.args[:2]
        self.assertEqual(502, code)
        self.assertIn("app feed unavailable", body["error"])

    def test_a_move_refusal_is_a_409_and_an_unreachable_cluster_a_502(self):
        server, handler = self.handler("POST", "/api/move/clusters/check", {"name": "far"})
        with mock.patch.object(server.MOVE, "check_cluster", side_effect=ValueError("still running")):
            handler.do_POST()
        self.assertEqual(409, handler._send.call_args.args[0])
        with mock.patch.object(server.MOVE, "check_cluster", side_effect=server.MOVE.Unreachable("no answer")):
            handler.do_POST()
        self.assertEqual(502, handler._send.call_args.args[0])

    def test_source_recovery_still_waiting_is_a_503(self):
        server, handler = self.handler("GET", "/api/move/target")
        with mock.patch.object(server.MOVE_SOURCE, "target", side_effect=server.MOVE_SOURCE.PendingRecovery("wait")):
            handler.do_GET()
        self.assertEqual(503, handler._send.call_args.args[0])

    def test_an_unknown_source_action_is_a_400(self):
        server, handler = self.handler("POST", "/api/move/source", {"action": "explode"})
        handler.do_POST()
        self.assertEqual((400, {"error": "unknown move action"}, "application/json"), handler._send.call_args.args)

    def test_a_rebalance_needs_its_restarts_confirmed(self):
        server, handler = self.handler("POST", "/api/workloads/rebalance", {})
        with mock.patch.object(server.OPS, "list_operations", return_value=[]):
            handler.do_POST()
        self.assertEqual(409, handler._send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
