"""A reviewed reboot, shutdown or OS-update restart records the host's
maintenance: opened when power is sent, closed when the host is back, at
the job's limit, or when the job ends another way (#535)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_history as HISTORY
import homestead_node_actions as NODE_ACTIONS
import homestead_operations as OPS
import homestead_power as POWER
import homestead_power_jobs as POWER_JOBS


def plan(action="reboot", **extra):
    return {"node": "node1", "action": action, "drain_pods": [], "boot_id": "b1", **extra}


def item(phase="observing", action="reboot", started=60, **ref):
    return {"id": "op-1", "progress": 20, "message": "Observing",
            "ref": {"node": "node1", "action": action, "phase": phase, "boot_id": "b1",
                    "started_epoch": time.time() - started, **ref}}


def node(ready=True, boot="b2"):
    return {"metadata": {"uid": "u1"}, "status": {"nodeInfo": {"bootID": boot},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class MaintenanceEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = HISTORY.DATA_DIR
        HISTORY.bind(self.tmp.name)
        self.phases = []
        patcher = mock.patch.object(OPS, "record_phase", side_effect=lambda op, phase, *a, **k: self.phases.append(phase))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        HISTORY.DATA_DIR = self.saved
        self.tmp.cleanup()

    def events(self):
        return HISTORY.maintenance_events("node1")

    def run_job(self, power_plan, phases, raise_at=None):
        seen = {}

        def node_power(node, action, *args, progress, **kwargs):
            for phase in phases:
                if phase == raise_at:
                    raise RuntimeError("drain refused")
                progress(phase, 10, phase)
                seen[phase] = list(self.events())
            return {}
        with mock.patch.object(NODE_ACTIONS, "node_power", side_effect=node_power):
            try:
                POWER_JOBS.run_power_job("op-1", power_plan)
            except POWER_JOBS.PowerNotSent:
                pass
        return seen

    def test_the_event_opens_when_power_is_sent(self):
        seen = self.run_job(plan(), ["cordoning", "draining", "verifying", "sending"])
        self.assertEqual([], seen["verifying"])
        self.assertEqual([("node1", "host reboot", "op-1", None)],
                         [(e["node"], e["reason"], e["job"], e["end"]) for e in seen["sending"]])
        for power_plan, reason in ((plan("poweroff"), "host shutdown"), (plan(reason="OS update"), "OS update")):
            HISTORY.maintenance_close("op-1")
            with open(Path(self.tmp.name) / HISTORY.FILE, "w") as handle:
                handle.write("{}")
            self.run_job(power_plan, ["sending"])
            self.assertEqual(reason, self.events()[0]["reason"])

    def test_no_event_before_power_is_sent(self):
        with mock.patch.object(OPS, "stored", return_value=[]):
            self.run_job(plan(), ["cordoning", "draining", "sending"], raise_at="draining")
        self.assertEqual([], self.events())

    def status(self, job, host):
        with mock.patch.object(POWER, "kget", return_value=host), \
             mock.patch.object(POWER, "RESTORE", None), mock.patch.object(POWER, "UNCORDON", None):
            return POWER.status(job)

    def test_it_closes_when_the_host_is_back_on_a_new_boot(self):
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=time.time() - 120)
        job = item()
        self.status(job, node(ready=True, boot="b2"))
        event = self.events()[0]
        self.assertEqual(("returned", int(job["ref"]["returned_at"])), (event["outcome"], event["end"]))
        self.status(job, node(ready=True, boot="b2"))
        self.assertEqual(event, self.events()[0])

    def test_a_closed_event_is_not_closed_again_on_every_look(self):
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=time.time() - 120)
        job = item()
        with mock.patch.object(HISTORY, "maintenance_close", wraps=HISTORY.maintenance_close) as close:
            for _ in range(3):
                self.status(job, node(ready=True, boot="b2"))
        self.assertEqual(1, close.call_count)
        self.assertTrue(job["ref"]["maintenance_closed"])

    def test_the_ten_minute_limit_closes_it_as_limit(self):
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=time.time() - 700)
        state = self.status(item(started=601), node(ready=False, boot="b1"))
        self.assertEqual("failed", state[0])
        self.assertEqual("limit", self.events()[0]["outcome"])

    def test_a_failed_helper_closes_it_as_ended(self):
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=time.time() - 60)
        helper = {"metadata": {"uid": "h1"}, "status": {"phase": "Failed", "containerStatuses": []}}
        hosts = iter([node(ready=True, boot="b1"), helper])
        with mock.patch.object(POWER, "kget", side_effect=lambda path: next(hosts)), \
             mock.patch.object(POWER, "RESTORE", None), mock.patch.object(POWER, "UNCORDON", None):
            state = POWER.status(item(helper_pod="power-1", helper_uid="h1"))
        self.assertEqual("failed", state[0])
        self.assertEqual("ended", self.events()[0]["outcome"])

    def test_a_verified_shutdown_closes_it_as_ended(self):
        HISTORY.maintenance_open("node1", "host shutdown", "op-1", now=time.time() - 60)
        state = self.status(item(action="poweroff", held=["lab/app"], restored={"started": [], "left": []}),
                            node(ready=False, boot="b1"))
        self.assertEqual("succeeded", state[0])
        self.assertEqual("ended", self.events()[0]["outcome"])

    def test_nothing_is_closed_before_power_is_sent(self):
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=time.time() - 60)
        with mock.patch.object(POWER, "WORKER_GONE", None):
            self.status(item(phase="draining", phase_at=time.time() - 3000), node())
        self.assertIsNone(self.events()[0]["end"])

    def test_another_copy_closes_what_this_one_opened(self):
        with mock.patch.object(POWER_JOBS, "POD_NAME", "copy-a"):
            self.run_job(plan(), ["sending"])
        with mock.patch.object(POWER_JOBS, "POD_NAME", "copy-b"):
            self.status(item(worker="copy-a"), node(ready=True, boot="b2"))
        self.assertEqual("returned", self.events()[0]["outcome"])

    def test_a_history_that_cannot_be_written_never_stops_the_power_job(self):
        with mock.patch.object(HISTORY, "maintenance_open", side_effect=OSError("read-only")):
            seen = self.run_job(plan(), ["sending", "observing"])
        self.assertIn("observing", seen, "the job carried on past sending")
        with mock.patch.object(HISTORY, "maintenance_close", side_effect=OSError("read-only")):
            state = self.status(item(), node(ready=True, boot="b2"))
        self.assertIn(state[0], ("running", "succeeded"), "observing the job carries on too")

    def test_an_os_update_restart_says_so(self):
        ready = {"node": "node1", "action": "reboot", "ready": True, "blockers": [], "stranded": [], "hold": [],
                 "requires_data_ack": False, "drain_pods": []}
        handed = {}
        with mock.patch.object(POWER, "plan", return_value=dict(ready)), \
             mock.patch.object(POWER_JOBS, "send_reviewed_power",
                               side_effect=lambda p: handed.update(p) or {"operation": {"id": "op-2"}}):
            self.assertEqual("op-2", POWER_JOBS.rollout_reboot("node1"))
        self.assertEqual("OS update", handed.get("reason"))


if __name__ == "__main__":
    unittest.main()
