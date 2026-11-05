"""A host's "not ready" alert waits while its maintenance is open, and is
raised at once when the maintenance overran its job's limit (#535)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as A
import homestead_history as HISTORY
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

NOT_READY = {"severity": "critical", "kind": "Node", "name": "node1", "reason": "Kubernetes reports NotReady. Review this host."}
DISK = {"severity": "degraded", "kind": "Disk", "name": "node1/sda", "reason": "SMART warning"}


class MaintenanceAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        keep_bound(self, HISTORY, A)
        HISTORY.bind(self.tmp.name)
        A.bind(self.tmp.name)
        self.now = time.time()

    def facts(self, *issues):
        return A.health_facts({"health_issues": list(issues)})

    def test_no_node_fact_while_maintenance_is_open(self):
        HISTORY.maintenance_open("node1", "OS update", "op-1", now=self.now - 60)
        keys = {f.get("key") for f in self.facts(NOT_READY, DISK)}
        self.assertNotIn("health:Node:node1", keys)
        self.assertIn("health:Disk:node1/sda", keys, "only the host's readiness waits")

    def test_the_alert_returns_the_moment_the_event_overruns(self):
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=self.now - 700)
        A.observe({"health": self.facts(NOT_READY)}, self.now - 30)
        HISTORY.maintenance_close("op-1", now=self.now, outcome="limit")
        raised = A.observe({"health": self.facts(NOT_READY)}, self.now + 1)
        self.assertEqual([("health:Node:node1", "raised")], [(e["key"], e["phase"]) for e in raised],
                         "no further hold once the job's limit has passed")

    def test_a_host_down_after_a_returned_event_waits_the_usual_hold(self):
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=self.now - 300)
        HISTORY.maintenance_close("op-1", now=self.now - 200, outcome="returned")
        self.assertEqual([], A.observe({"health": self.facts(NOT_READY)}, self.now))

    def test_an_alert_raised_before_the_event_resolves(self):
        A.observe({"health": self.facts(NOT_READY)}, self.now - 200)
        raised = A.observe({"health": self.facts(NOT_READY)}, self.now - 100)
        self.assertEqual(["raised"], [e["phase"] for e in raised])
        HISTORY.maintenance_open("node1", "host reboot", "op-1", now=self.now - 90)
        A.observe({"health": self.facts(NOT_READY)}, self.now - 80)
        resolved = A.observe({"health": self.facts(NOT_READY)}, self.now)
        self.assertEqual(["resolved"], [e["phase"] for e in resolved])


    def test_a_history_that_cannot_be_read_never_stops_alerts(self):
        from unittest import mock
        with mock.patch.object(HISTORY, "_read", side_effect=OSError("disk")):
            keys = {f.get("key") for f in self.facts(NOT_READY, DISK)}
        self.assertIn("health:Node:node1", keys, "with no way to tell, the host is reported")

    def test_one_read_of_history_per_look(self):
        from unittest import mock
        other = dict(NOT_READY, name="node2")
        real = HISTORY._read
        with mock.patch.object(HISTORY, "_read", side_effect=real) as read:
            self.facts(NOT_READY, other)
        self.assertEqual(1, read.call_count)

if __name__ == "__main__":
    unittest.main()
