import sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class RolloutPowerJobTests(unittest.TestCase):
    """A rollout's new leader takes up the restart its drained predecessor
    started. The job tray's public list leaves each job's ref out, so that is
    not where it can be found: the stored records are."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(server.OPS, "DATA_DIR", temporary.name)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, node, action="reboot", started=None):
        return server.OPS.start("node-power", f"{action} {node}", {"kind": "Node", "name": node}, "/nodes",
                                {"node": node, "action": action, "started_epoch": started or time.time()}, "reviewed")

    def test_the_restart_left_running_is_found(self):
        job = self.start("node-1")
        self.assertEqual(job["id"], server.POWER_JOBS.rollout_power_job("node-1", time.time() - 60))

    def test_another_host_an_older_job_a_power_off_or_a_failed_one_is_not_it(self):
        now = time.time()
        stored = [{"id": "a", "kind": "node-power", "status": "running", "ref": {"node": "node-2", "action": "reboot", "started_epoch": now}},
                  {"id": "b", "kind": "node-power", "status": "running", "ref": {"node": "node-1", "action": "reboot", "started_epoch": now - 3600}},
                  {"id": "c", "kind": "node-power", "status": "running", "ref": {"node": "node-1", "action": "poweroff", "started_epoch": now}},
                  {"id": "d", "kind": "node-power", "status": "failed", "ref": {"node": "node-1", "action": "reboot", "started_epoch": now}}]
        with mock.patch.object(server.OPS, "_read", return_value=stored):
            self.assertEqual("", server.POWER_JOBS.rollout_power_job("node-1", now - 60))


if __name__ == "__main__":
    unittest.main()
