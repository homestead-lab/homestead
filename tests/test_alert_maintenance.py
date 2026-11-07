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

NOT_READY = {"severity": "critical", "kind": "Node", "name": "node1", "reason": "Kubernetes reports NotReady. Review this host."}
DISK = {"severity": "degraded", "kind": "Disk", "name": "node1/sda", "reason": "SMART warning"}


class MaintenanceAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = HISTORY.DATA_DIR
        self.addCleanup(lambda: setattr(HISTORY, "DATA_DIR", self.saved))
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

class DeclaredAlertTests(unittest.TestCase):
    """Maintenance a person declared keeps its host quiet until it runs out,
    and is loud then if the host is still down (#535 part 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = HISTORY.DATA_DIR
        self.addCleanup(lambda: setattr(HISTORY, "DATA_DIR", self.saved))
        HISTORY.bind(self.tmp.name)
        self.addCleanup(setattr, A, "DATA_DIR", A.DATA_DIR)
        A.bind(self.tmp.name)
        self.now = time.time()

    def facts(self, *issues):
        return A.health_facts({"health_issues": list(issues)})

    def declare(self, until, held=()):
        HISTORY.maintenance_declare("node1", "Drive swap", "op-1", until, cordoned_before=False, node_uid="u",
                                    held=held, now=self.now - 3600)

    def test_no_host_alert_while_declared_is_open(self):
        self.declare(self.now + 600)
        self.assertEqual([], [f for f in self.facts(NOT_READY) if "key" in f])

    def test_ran_out_with_the_host_down_raises_one_urgent_critical_fact(self):
        until = self.now - 1
        self.declare(until)
        facts = [f for f in self.facts(NOT_READY) if "key" in f]
        self.assertEqual(["maintenance-ran-out:node1"], [f["key"] for f in facts], "one alert, not two")
        fact = facts[0]
        self.assertEqual((True, "critical", "Maintenance on node1 ran out with the host down"),
                         (fact["urgent"], fact["severity"], fact["title"]))
        self.assertIn("Drive swap", fact["body"])
        self.assertNotIn("cordoned", fact["body"], "not so on a single host, or after a hand uncordon")
        raised = A.observe({"health": [fact]}, self.now)
        self.assertEqual([("maintenance-ran-out:node1", "raised")], [(e["key"], e["phase"]) for e in raised],
                         "raised on the first look")

    def test_it_stays_loud_after_the_recent_window(self):
        self.declare(self.now - HISTORY.OVERRAN_RECENT - 60)
        self.assertEqual(["maintenance-ran-out:node1"], [f.get("key") for f in self.facts(NOT_READY)])

    def test_ran_out_with_the_host_up_raises_nothing(self):
        self.declare(self.now - 1)
        self.assertEqual([], [f for f in self.facts(DISK) if f.get("key", "").startswith("maintenance-ran-out")])

    def test_the_fact_resolves_when_the_host_is_back(self):
        self.declare(self.now - 1)
        A.observe({"health": self.facts(NOT_READY)}, self.now)
        A.observe({"health": self.facts()}, self.now + 1)
        resolved = A.observe({"health": self.facts()}, self.now + A.HOLD + 2)
        self.assertEqual([("maintenance-ran-out:node1", "resolved")], [(e["key"], e["phase"]) for e in resolved])

    def test_the_resolved_line_never_says_the_host_is_back(self):
        self.declare(self.now - 1)
        fact = self.facts(NOT_READY)[0]
        self.assertNotIn("is back", fact["resolved"])
        self.assertIn("extended, ended or the host returned", fact["resolved"])

    def test_extending_after_it_ran_out_with_the_host_down_does_not_say_it_came_back(self):
        self.declare(self.now - 1)
        A.observe({"health": self.facts(NOT_READY)}, self.now)
        HISTORY.maintenance_extend("op-1", self.now + 3600, now=self.now + 1)
        self.assertEqual([], [f for f in self.facts(NOT_READY) if "key" in f], "quiet again")
        A.observe({"health": self.facts(NOT_READY)}, self.now + 2)
        resolved = A.observe({"health": self.facts(NOT_READY)}, self.now + A.HOLD + 3)
        self.assertEqual(1, len(resolved))
        self.assertNotIn("is back", resolved[0]["title"])

    def test_ran_out_with_the_host_up_and_apps_stopped_raises_a_plain_alert(self):
        held = [{"kind": "Deployment", "ns": "apps", "name": "web"}]
        self.declare(self.now - 1, held=held)
        facts = [f for f in self.facts() if "key" in f]
        self.assertEqual(["maintenance-held:node1"], [f["key"] for f in facts])
        self.assertEqual("degraded", facts[0]["severity"])
        self.assertNotIn("urgent", facts[0], "it holds as usual")
        self.assertIn("1 app", facts[0]["title"])
        self.assertIn("Drive swap", facts[0]["body"])

    def test_ran_out_with_the_host_down_is_one_alert_even_with_apps_stopped(self):
        self.declare(self.now - 1, held=[{"kind": "Deployment", "ns": "apps", "name": "web"}])
        self.assertEqual(["maintenance-ran-out:node1"], [f.get("key") for f in self.facts(NOT_READY)])

    def test_an_open_one_with_apps_stopped_raises_nothing(self):
        self.declare(self.now + 600, held=[{"kind": "Deployment", "ns": "apps", "name": "web"}])
        self.assertEqual([], [f for f in self.facts() if "key" in f])

    def test_an_end_that_did_not_finish_with_apps_stopped_raises_an_alert(self):
        self.declare(self.now + 600, held=[{"kind": "Deployment", "ns": "apps", "name": "web"}])
        HISTORY.maintenance_end("op-1", now=self.now - 60)
        facts = [f for f in self.facts() if "key" in f]
        self.assertEqual(["maintenance-held:node1"], [f["key"] for f in facts])
        self.assertIn("did not finish", facts[0]["title"])
        self.assertNotIn("no longer holds", facts[0]["resolved"], "replicas may still be 0")
        self.assertIn("cleared", facts[0]["resolved"])
        HISTORY.maintenance_release("op-1", now=self.now - 30)
        self.assertEqual([], [f for f in self.facts() if "key" in f])

    def test_ended_before_it_ran_out_raises_nothing_new(self):
        self.declare(self.now + 600)
        HISTORY.maintenance_end("op-1", now=self.now - 60)
        self.assertEqual(["health:Node:node1"], [f.get("key") for f in self.facts(NOT_READY)],
                         "the plain alert, and it holds as usual")
        self.assertNotIn("urgent", self.facts(NOT_READY)[0])
        self.assertEqual([], A.observe({"health": self.facts(NOT_READY)}, self.now))

    def test_ended_after_it_ran_out_is_a_plain_alert(self):
        self.declare(self.now - HISTORY.OVERRAN_RECENT - 60)
        HISTORY.maintenance_end("op-1", now=self.now - 60)
        self.assertEqual(["health:Node:node1"], [f.get("key") for f in self.facts(NOT_READY)], "before it is released")
        HISTORY.maintenance_release("op-1", now=self.now - 30)
        self.assertEqual(["health:Node:node1"], [f.get("key") for f in self.facts(NOT_READY)])

    def test_one_read_of_history_per_look(self):
        from unittest import mock
        self.declare(self.now - 1)
        real = HISTORY._read
        with mock.patch.object(HISTORY, "_read", side_effect=real) as read:
            self.facts(NOT_READY)
        self.assertEqual(1, read.call_count)


if __name__ == "__main__":
    unittest.main()
