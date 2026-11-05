"""OS updates for the hosts a person chooses (#1174), and what stops a restart
nobody is watching.

A row or a selection runs the same engine as an update of every host: only the
named hosts, in the same order, with this run's own answer on restarts, and
automatic updates held on every Ready host until it ends. A read-only preview
says, per host, what the restart checks would say today. In any unattended
restart a drain blocker refuses it and is named; the host waits for a person.

Reads go through the real kget, the real review (homestead_power.plan) and the
real job store over the shared fake API server (tests/fakes/kube.py). The host
itself is the one stand-in, as nothing here can run a package manager."""
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_drain_blockers as BLOCKERS
import homestead_kube as K
import homestead_os_rollout as ROLLOUT
import homestead_power as power
import homestead_power_jobs as POWER_JOBS
from fakes.kube import Kube
from test_drain_blockers import attachment, claim, engine, engine_orphan, instance_manager, pod
from test_os_rollout import Hosts, facts as host_facts


def host(name, ready=True):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "uid": f"uid-{name}"},
            "spec": {},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "nodeInfo": {"bootID": f"boot-{name}"}}}


def orphans_on_node_a():
    """Three engine orphans on node-a whose engines now run on node-b."""
    volumes = ["vol-a", "vol-b", "vol-c"]
    return ([engine_orphan(volume, n) for n, volume in enumerate(volumes, 1)]
            + [engine(volume, "node-b") for volume in volumes]
            + [instance_manager("node-a", engines=[f"{v}-e-0" for v in volumes])])


def stuck_pod_on_node_a():
    return [pod(deleting_at=time.time() - 3600), claim(), attachment()]


def keep_bound(test, module, *names):
    """What bind() sets goes back when the test ends: the module is shared."""
    for name in names:
        patcher = mock.patch.object(module, name, getattr(module, name))
        patcher.start()
        test.addCleanup(patcher.stop)


class FakeCluster(unittest.TestCase):
    """Three hosts on the fake, the real review, and a stand-in for sending
    power: what would be restarted is recorded, nothing else is stubbed."""
    def setUp(self):
        self.kube = Kube(host("node-a"), host("node-b"), host("node-c"))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        keep_bound(self, power, "kget", "impact", "quorum", "power_enabled")
        power.bind(K.kget, lambda node: {"workloads": [], "stranded": []},
                   lambda: {"members": ["node-a", "node-b", "node-c"], "can_lose": 1}, lambda: True)
        self.sent = []

        def send(plan, *args, **kwargs):
            self.sent.append(plan["node"])
            return {"operation": {"id": f"op-{plan['node']}"}}
        patcher = mock.patch.object(POWER_JOBS, "send_reviewed_power", side_effect=send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, *objects):
        for obj in objects:
            self.kube.add(obj)


class UnattendedRestartTests(FakeCluster):
    def test_a_host_with_nothing_in_the_way_still_restarts(self):
        self.assertEqual("op-node-a", POWER_JOBS.rollout_reboot("node-a"))
        self.assertEqual(["node-a"], self.sent)

    def test_a_drain_blocker_refuses_it_and_is_named(self):
        self.add(*orphans_on_node_a())
        with self.assertRaises(ValueError) as refused:
            POWER_JOBS.rollout_reboot("node-a")
        self.assertIn("3 Longhorn orphan records", str(refused.exception))
        self.assertEqual([], self.sent, "nothing is sent")
        self.assertEqual([], [w for w in self.kube.writes if w[1].startswith("/api/v1/nodes")], "nor cordoned")

    def test_every_blocker_is_named(self):
        self.add(*orphans_on_node_a(), *stuck_pod_on_node_a())
        with self.assertRaises(ValueError) as refused:
            POWER_JOBS.rollout_reboot("node-a")
        self.assertIn("orphan records", str(refused.exception))
        self.assertIn("apps/app-0", str(refused.exception))

    def test_a_check_that_could_not_read_refuses_too(self):
        # Not knowing is not "nothing in the way": a person looks.
        with mock.patch.object(BLOCKERS, "for_host", side_effect=KeyError("status")):
            with self.assertRaises(ValueError) as refused:
                POWER_JOBS.rollout_reboot("node-a")
        self.assertIn("couldn't check", str(refused.exception))
        self.assertEqual([], self.sent)

    def test_the_settings_accepting_an_only_copy_do_not_accept_a_blocker(self):
        self.add(*orphans_on_node_a())
        with self.assertRaises(ValueError):
            POWER_JOBS.rollout_reboot("node-a", allow_single_copy=True)
        self.assertEqual([], self.sent)

    def test_the_refusal_is_not_taken_for_copies_still_rebuilding(self):
        # The rollout waits 15 minutes on that phrase; a blocker waits for a person.
        self.add(*orphans_on_node_a())
        self.assertNotIn("only healthy copy", POWER_JOBS.rollout_refusal("node-a"))


class AskingTheChecksTests(FakeCluster):
    def test_it_answers_as_the_restart_would_and_changes_nothing(self):
        self.assertEqual("", POWER_JOBS.rollout_refusal("node-a"))
        self.add(*orphans_on_node_a())
        before = len(self.kube.writes)
        answer = POWER_JOBS.rollout_refusal("node-a")
        self.assertIn("3 Longhorn orphan records", answer)
        with self.assertRaises(ValueError) as refused:
            POWER_JOBS.rollout_reboot("node-a")
        self.assertEqual(str(refused.exception), answer, "the same words as the restart itself")
        self.assertEqual([], self.sent)
        self.assertEqual(before, len(self.kube.writes))

    def test_the_answer_does_not_change_as_a_pod_stays_stuck(self):
        self.add(*stuck_pod_on_node_a())
        first = POWER_JOBS.rollout_refusal("node-a")
        self.assertIn("apps/app-0", first)
        with mock.patch.object(BLOCKERS.time, "time", return_value=time.time() + 7200):
            self.assertEqual(first, POWER_JOBS.rollout_refusal("node-a"))

    def test_a_failed_read_is_not_an_answer(self):
        with mock.patch.object(POWER_JOBS.POWER, "plan", side_effect=OSError("no route to host")):
            with self.assertRaises(OSError):
                POWER_JOBS.rollout_refusal("node-a")


if __name__ == "__main__":
    unittest.main()
