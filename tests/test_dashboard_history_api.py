"""Exercise the collector and dashboard endpoint, including slow-client isolation."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
from test_mqtt_history import overview


class StopLoop(BaseException):
    pass


class DashboardHistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_dir = server.HISTORY.DATA_DIR
        server.HISTORY.bind(self.tmp.name)
        self.addCleanup(server.HISTORY.bind, self.previous_dir)

    def test_live_api_reads_persisted_metrics_without_holding_cache_lock_during_send(self):
        server.HISTORY.record_live(overview(12))
        handler = object.__new__(server.H)
        handler.path = "/api/history"
        handler._begin = lambda: None
        handler._fleet_target = lambda path: None
        handler._guard = lambda path: False
        sent = []
        def send(code, body):
            self.assertTrue(server._lock.acquire(blocking=False), "a slow client must not block collectors or cache reads")
            server._lock.release()
            sent.append((code, body))
        handler._send = send
        handler.do_GET()
        self.assertEqual(200, sent[0][0])
        self.assertEqual([12], sent[0][1]["cpu"])

    def test_sampler_persists_only_on_the_leader_and_keeps_vm_sampling(self):
        for leading in (False, True):
            with self.subTest(leading=leading), \
                    mock.patch.object(server.LEADER, "is_leader", return_value=leading), \
                    mock.patch.object(server, "get_overview", return_value=overview(17)), \
                    mock.patch.object(server.HISTORY, "record_live", wraps=server.HISTORY.record_live) as record, \
                    mock.patch.object(server.VMUSAGE, "sample") as vm_sample, \
                    mock.patch.object(server, "beat"), \
                    mock.patch.object(server.time, "sleep", side_effect=StopLoop) as sleep:
                with self.assertRaises(StopLoop):
                    server._sampler()
                self.assertEqual(int(leading), record.call_count)
                vm_sample.assert_called_once()
                self.assertGreaterEqual(sleep.call_args.args[0], 1)
                self.assertLessEqual(sleep.call_args.args[0], 30)
        self.assertEqual([17], server.HISTORY.live_series()["cpu"])
