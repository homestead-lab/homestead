"""The first host of an OS update as a canary (issue #1348, section 5.3)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_os_canary as CANARY
import homestead_os_rollout as ROLLOUT
from rebind import keep_bound

T = 1_800_000_000


def ready(name):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def pod(name, ok=True, looping=False):
    return {"metadata": {"name": name},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ok else "False"}],
                       "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}] if looping else []}}


class Cluster:
    def __init__(self):
        self.nodes = {"a": ready("a"), "b": ready("b"), "c": ready("c")}
        self.pods = {"a": [pod("x")] * 5, "b": [pod("x")] * 2, "c": [pod("x")]}

    def get(self, path):
        if path.startswith("/api/v1/nodes/"):
            return self.nodes[path.rsplit("/", 1)[1]]
        return {"items": self.pods[path.rsplit("%3D", 1)[1]]}


class CanaryTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, CANARY)
        self.cluster = Cluster()
        self.alerts = []
        self.held = []
        CANARY.bind(self.cluster.get, lambda: self.alerts, lambda: self.held)

    def test_the_least_loaded_host_that_is_not_the_leader_s(self):
        self.assertEqual("b", CANARY.pick(["a", "b", "c"], "c"))
        self.assertEqual("c", CANARY.pick(["a", "b", "c"], "a"))
        self.assertIsNone(CANARY.pick(["a"], "b"), "one host: no canary")

    def test_the_checks(self):
        self.assertEqual("", CANARY.checks("b"))
        self.cluster.pods["b"] = [pod("virt-handler-x", ok=False)]
        self.assertEqual("KubeVirt's handler on b is not Ready", CANARY.checks("b"))
        self.cluster.pods["b"] = [pod("virt-handler-x"), pod("web-1", looping=True)]
        self.assertEqual("web-1 on b keeps crashing", CANARY.checks("b"))
        self.cluster.nodes["b"] = {"metadata": {"name": "b"}, "status": {"conditions": []}}
        self.assertEqual("b is not Ready", CANARY.checks("b"))

    def test_a_day_of_passing_checks_and_no_new_alert_lets_the_others_follow(self):
        soak = {"name": "b", "since": T}
        verdict, words = CANARY.watch(soak, now=T + 3600)
        self.assertEqual("wait", verdict)
        self.assertIn("follow in 23 h", words)
        self.assertEqual("go", CANARY.watch(soak, now=T + CANARY.SOAK)[0])
        soak = {"name": "b", "since": T}          # a new day, first read with these entries in the log
        self.alerts = [{"id": 1, "phase": "raised", "severity": "degraded", "at": T - 100, "title": "from before"},
                       {"id": 2, "phase": "raised", "severity": "info", "at": T + 10, "title": "info"},
                       {"id": 3, "phase": "resolved", "severity": "degraded", "at": T + 20, "title": "Cleared: from before"}]
        self.assertEqual("go", CANARY.watch(soak, now=T + CANARY.SOAK)[0], "info, or raised before the soak")
        self.alerts.append({"id": 4, "phase": "raised", "severity": "degraded", "at": T + 50, "title": "Volume web-data is degraded"})
        verdict, words = CANARY.watch(soak, now=T + 60)
        self.assertEqual(("stop", True), (verdict, "Volume web-data is degraded" in words))

    def test_at_the_end_of_the_day_a_held_condition_makes_the_others_wait(self):
        soak = {"name": "b", "since": T}
        self.held = [{"key": "address:192.0.2.50", "severity": "degraded", "announced": 0, "since": T + 100,
                      "title": "Service address 192.0.2.50 has no route"},
                     {"key": "x", "severity": "degraded", "announced": 0, "since": T - 100, "title": "from before"},
                     {"key": "y", "severity": "info", "announced": 0, "since": T + 100, "title": "info"}]
        verdict, words = CANARY.watch(soak, now=T + 3600)
        self.assertEqual(("wait", True), (verdict, "follow in" in words), "during the day: held is not news")
        verdict, words = CANARY.watch(soak, now=T + CANARY.SOAK)
        self.assertEqual(("wait", True), (verdict, "Service address 192.0.2.50 has no route" in words))
        verdict, words = CANARY.watch(soak, now=T + CANARY.SOAK + CANARY.SETTLE)
        self.assertEqual(("stop", True), (verdict, "neither cleared nor was raised" in words))
        self.held = self.held[1:]
        self.assertEqual("go", CANARY.watch(soak, now=T + CANARY.SOAK + 60)[0], "from before the day, or info")


class RolloutTests(unittest.TestCase):
    def rollout(self, **extra):
        return {"rollout": {"id": "os-1", "status": "running", "phase": "next", "index": 1, "nodes": ["b", "a"],
                            "canary": "b", "results": [{"node": "b", "ok": True, "note": "updated", "restarted": True}],
                            "node": {}, "stop": False, **extra}}

    def test_after_the_canary_the_run_soaks_then_goes_on(self):
        state = self.rollout()
        with mock.patch.object(ROLLOUT, "settings", lambda state=None: {**ROLLOUT.DEFAULTS, "manage": "homestead"}):
            self.assertTrue(ROLLOUT.step(state, now=T))
            self.assertEqual(("canary", {"name": "b", "since": T}), (state["rollout"]["phase"], state["rollout"]["soak"]))
            with mock.patch.object(CANARY, "watch", lambda soak, now: ("wait", "waiting")):
                ROLLOUT.step(state, now=T + 60)
            self.assertEqual("canary", state["rollout"]["phase"])
            with mock.patch.object(CANARY, "watch", lambda soak, now: ("go", "passed")):
                ROLLOUT.step(state, now=T + CANARY.SOAK)
            self.assertEqual(("next", True), (state["rollout"]["phase"], state["rollout"]["canary_passed"]))

    def test_a_failing_canary_stops_the_run_and_says_why(self):
        state = self.rollout()
        with mock.patch.object(ROLLOUT, "settings", lambda state=None: {**ROLLOUT.DEFAULTS, "manage": "homestead"}):
            ROLLOUT.step(state, now=T)
            with mock.patch.object(CANARY, "watch", lambda soak, now: ("stop", "the canary failed its checks: b is not Ready")):
                ROLLOUT.step(state, now=T + 60)
            self.assertTrue(state["rollout"]["failed"])
            ROLLOUT.step(state, now=T + 120)
        self.assertEqual("failed", state["rollout"]["status"])
        self.assertIn("stopped: the canary failed its checks: b is not Ready", state["rollout"]["message"])

    def test_no_canary_on_one_host_or_when_the_first_host_failed(self):
        for extra in ({"canary": ""}, {"results": [{"node": "b", "ok": False, "note": "x"}]}):
            state = self.rollout(**extra)
            with mock.patch.object(ROLLOUT, "settings", lambda state=None: {**ROLLOUT.DEFAULTS, "manage": "homestead"}), \
                    mock.patch.object(ROLLOUT, "_busy_host", lambda name: "busy"),                     mock.patch.object(ROLLOUT, "_nodes", lambda: {}):
                ROLLOUT.step(state, now=T)
            self.assertNotEqual("canary", state["rollout"]["phase"])


if __name__ == "__main__":
    unittest.main()
