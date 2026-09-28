import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_power as power
import homestead_lifecycle as lifecycle
import test_node_power_plan as fixture

ONE_NODE = lambda: {"members": ["node1"], "can_lose": 0, "ready": ["node1"], "total": 1, "quorum_needs": 1}


class ForcedPlanTests(unittest.TestCase):
    setUp = fixture.PowerPlanTests.setUp
    get = fixture.PowerPlanTests.get

    def test_a_one_node_cluster_is_blocked_but_may_be_overridden(self):
        power.quorum = ONE_NODE
        plan = power.plan("node1", "reboot")
        self.assertFalse(plan["ready"])
        self.assertTrue(any("only etcd member" in b for b in plan["overridable"]))
        self.assertTrue(any("Running VMs" in b for b in plan["overridable"]))
        self.assertEqual([], plan["hard_blockers"])
        forced = power.plan("node1", "reboot", force=True)
        self.assertTrue(forced["ready"])
        self.assertTrue(any(w.startswith("Forced: no cordon or drain") and "only etcd member" in w for w in forced["warnings"]))
        self.assertNotEqual(plan["review_token"], forced["review_token"], "a forced review is its own review")

    def test_what_cannot_be_sent_safely_is_never_overridden(self):
        power.power_enabled = lambda: False
        forced = power.plan("node1", "reboot", force=True)
        self.assertFalse(forced["ready"])
        self.assertIn("Host power control is disabled (ENABLE_NODE_POWER is off)", forced["blockers"])
        power.power_enabled = lambda: True
        self.objects["/api/v1/nodes/node1"]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        self.assertFalse(power.plan("node1", "reboot", force=True)["ready"])

    def test_a_forced_send_refuses_a_host_that_changed(self):
        power.quorum = ONE_NODE
        forced = power.plan("node1", "reboot", force=True)
        self.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new"
        with self.assertRaisesRegex(ValueError, "identity changed"):
            power.recheck_forced(forced)


class ForcedSendTests(unittest.TestCase):
    def test_forced_sends_without_cordon_or_drain(self):
        calls, sent = [], []
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                mock.patch.object(lifecycle, "set_cordon", lambda *a: calls.append(("cordon", a))), \
                mock.patch.object(lifecycle, "drain", lambda *a, **k: calls.append(("drain", a))), \
                mock.patch.object(lifecycle, "node_action_check", lambda *a: (False, "quorum", {})), \
                mock.patch.object(lifecycle, "quorum_report", lambda: {"members": ["node1"]}), \
                mock.patch.object(lifecycle, "_bust", lambda *a: None), \
                mock.patch.object(lifecycle, "ksend", lambda method, path, body=None, **k: sent.append(body) or
                                  {"metadata": {"uid": "u", "name": body["metadata"]["name"], "namespace": "lab"}}):
            result = lifecycle.node_power("node1", "reboot", True, before_send=lambda: calls.append(("checked",)),
                                          reviewed_pods=[], force=True)
        self.assertEqual([("checked",)], calls, "no cordon, no drain - only the fresh check")
        self.assertIn("systemctl reboot", sent[0]["spec"]["containers"][0]["command"][-1])
        self.assertIn("forced: no cordon or drain", result["steps"])


if __name__ == "__main__":
    unittest.main()
