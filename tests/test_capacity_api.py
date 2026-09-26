import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class CapacityApiTests(unittest.TestCase):
    def post(self, plan, confirmed=False, replicas=3):
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/scale", {}
        handler._guard = lambda path: False
        handler._body = lambda: {"ns": "lab", "name": "example", "replicas": replicas, "confirm_capacity": confirmed}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server, "workload_start_plan", return_value=plan) as check, \
                mock.patch.object(server, "ksend") as send, \
                mock.patch.object(server, "clear_unstarted_pods") as cleanup:
            handler.do_POST()
        return handler._send.call_args.args, check, send, cleanup

    def test_acknowledgement_cannot_override_resource_exhaustion(self):
        result, check, send, cleanup = self.post({"blocked": True, "requires_confirmation": True}, True)
        self.assertEqual(409, result[0])
        check.assert_called_once_with("lab", "example", 3)
        send.assert_not_called()
        cleanup.assert_not_called()

    def test_missing_reservations_require_acknowledgement_server_side(self):
        plan = {"blocked": False, "requires_confirmation": True}
        result, _, send, _ = self.post(plan)
        self.assertEqual(409, result[0])
        send.assert_not_called()
        result, check, send, _ = self.post(plan, True)
        self.assertEqual(200, result[0])
        check.assert_called_once()
        self.assertEqual("PATCH", send.call_args.args[0])

    def test_stop_does_not_run_capacity_checks(self):
        result, check, send, _ = self.post({"blocked": True}, replicas=0)
        self.assertEqual(200, result[0])
        check.assert_not_called()
        self.assertEqual({"spec": {"replicas": 0}}, send.call_args.args[2])

    def test_invalid_replica_count_never_deletes_pending_pods(self):
        for count in (-1, 101):
            result, check, send, cleanup = self.post({}, replicas=count)
            self.assertEqual(400, result[0])
            check.assert_not_called()
            send.assert_not_called()
            cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
