import copy
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_leader as LEADER
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound
import homestead_shared as SHARED

T0 = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


class FakeLeases:
    def __init__(self):
        self.lease, self.version = None, 0

    def get(self, path):
        if self.lease is None:
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        return copy.deepcopy(self.lease)

    def send(self, method, path, body=None, **kw):
        if method == "POST":
            if self.lease is not None:
                raise urllib.error.HTTPError(path, 409, "exists", None, None)
        elif body["metadata"].get("resourceVersion") != str(self.version):
            raise urllib.error.HTTPError(path, 409, "conflict", None, None)
        self.version += 1
        self.lease = copy.deepcopy(body)
        self.lease["metadata"]["resourceVersion"] = str(self.version)
        return self.lease


class LeaderTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeLeases()
        keep_bound(self, LEADER)
        LEADER.bind(self.store.get, self.store.send, "lab")
        LEADER._state.update(leader=False, holder="")

    def as_replica(self, name, at):
        with mock.patch.object(LEADER, "IDENTITY", name):
            return LEADER.attempt(at)

    def test_becoming_leader_tells_who_asked_once_per_term(self):
        called = []
        self.addCleanup(LEADER._listeners.__setitem__, slice(None), list(LEADER._listeners))
        LEADER.when_leading(lambda: called.append(1))
        with mock.patch.object(LEADER.time, "monotonic", return_value=100.0):
            LEADER._set(True, LEADER.IDENTITY)
            LEADER._set(True, LEADER.IDENTITY)  # a renew is not a new term
        self.assertEqual([1], called)
        LEADER._set(False, "homestead-b")
        with mock.patch.object(LEADER.time, "monotonic", return_value=200.0):
            LEADER._set(True, LEADER.IDENTITY)
        self.assertEqual([1, 1], called)

    def test_a_listener_that_fails_does_not_stop_the_lease(self):
        self.addCleanup(LEADER._listeners.__setitem__, slice(None), list(LEADER._listeners))
        LEADER.when_leading(mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch("builtins.print"):
            self.assertTrue(LEADER._set(True, LEADER.IDENTITY))

    def test_a_leader_that_cannot_renew_stops_leading_by_its_own_clock(self):
        # #621: the flag stayed True until a renew raised, and a renew can hang
        # for as long as the API write timeout - past the lease, while another
        # replica already leads. Like Kubernetes' own controllers, the leader
        # steps down at the renew deadline, before the lease can pass on.
        with mock.patch.object(LEADER.time, "monotonic", return_value=100.0):
            self.assertTrue(self.as_replica("homestead-a", T0))
            self.assertTrue(LEADER.is_leader())
        with mock.patch.object(LEADER.time, "monotonic", return_value=100.0 + LEADER.RENEW_DEADLINE - 1):
            self.assertTrue(LEADER.is_leader(), "still within the renew deadline")
            self.assertTrue(LEADER.status()["leader"])
        with mock.patch.object(LEADER.time, "monotonic", return_value=100.0 + LEADER.RENEW_DEADLINE + 1):
            self.assertFalse(LEADER.is_leader(), "no renew for too long: not leading, whatever the flag says")
            self.assertFalse(LEADER.status()["leader"])
        self.assertLess(LEADER.RENEW_DEADLINE, LEADER.DURATION, "it steps down before another replica may take over")
        # a renew that succeeds again restores it
        with mock.patch.object(LEADER.time, "monotonic", return_value=200.0):
            self.assertTrue(self.as_replica("homestead-a", T0 + datetime.timedelta(seconds=12)))
            self.assertTrue(LEADER.is_leader())

    def test_the_deadline_counts_from_when_the_renew_started(self):
        # The lease's renewTime is stamped when the attempt starts, so another
        # replica's clock runs from then. A slow renew (a GET of 8 s, a PUT of
        # 4 s, both within the API timeouts) must not buy the leader time past
        # the lease; it counts from the start, as client-go does.
        clock = [100.0]
        def slow_get(path):
            clock[0] += 8
            return self.store.get(path)
        def slow_send(method, path, body=None, **kw):
            clock[0] += 4
            return self.store.send(method, path, body, **kw)
        self.as_replica("homestead-a", T0)                      # the lease exists, a holds it
        LEADER.bind(slow_get, slow_send, "lab")
        with mock.patch.object(LEADER.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(self.as_replica("homestead-a", T0 + datetime.timedelta(seconds=2)))
            self.assertEqual(112.0, clock[0])
            clock[0] = 100.0 + LEADER.RENEW_DEADLINE + 1
            self.assertFalse(LEADER.is_leader(), "the 12 s it took to renew are not a fresh deadline")

    def test_one_slow_or_failed_renew_fits_inside_the_deadline(self):
        self.assertLessEqual(2 * LEADER.RENEW_EVERY, LEADER.RENEW_DEADLINE / 2)

    def test_stepping_down_by_the_clock_ends_the_term(self):
        # A leader that stepped down and renews again starts a new term: its
        # "leading since" is the new one, not the first.
        with mock.patch.object(LEADER.time, "monotonic", return_value=100.0), \
                mock.patch.object(LEADER.time, "time", return_value=1000.0):
            self.assertTrue(self.as_replica("homestead-a", T0))
        with mock.patch.object(LEADER.time, "monotonic", return_value=100.0 + LEADER.RENEW_DEADLINE + 1):
            self.assertFalse(LEADER.is_leader())
            self.assertFalse(LEADER._state["leader"])
        with mock.patch.object(LEADER.time, "monotonic", return_value=200.0), \
                mock.patch.object(LEADER.time, "time", return_value=2000.0):
            self.assertTrue(self.as_replica("homestead-a", T0 + datetime.timedelta(seconds=12)))
        self.assertEqual(2000.0, LEADER._state["since"])

    def test_one_replica_leads_and_another_takes_over_when_it_stops(self):
        self.assertTrue(self.as_replica("homestead-a", T0))
        self.assertFalse(self.as_replica("homestead-b", T0 + datetime.timedelta(seconds=5)))
        # a renews; b still waits
        self.assertTrue(self.as_replica("homestead-a", T0 + datetime.timedelta(seconds=10)))
        self.assertFalse(self.as_replica("homestead-b", T0 + datetime.timedelta(seconds=20)))
        # a's node dies: nothing renews for longer than the lease lasts
        self.assertTrue(self.as_replica("homestead-b", T0 + datetime.timedelta(seconds=26)))
        self.assertEqual("homestead-b", self.store.lease["spec"]["holderIdentity"])
        self.assertEqual(1, self.store.lease["spec"]["leaseTransitions"])
        # a comes back and finds it has lost the lease
        self.assertFalse(self.as_replica("homestead-a", T0 + datetime.timedelta(seconds=27)))

    def test_two_replicas_racing_for_an_expired_lease_cannot_both_win(self):
        self.as_replica("homestead-a", T0)
        stale = self.store.get("")
        later = T0 + datetime.timedelta(seconds=40)
        self.assertTrue(self.as_replica("homestead-b", later))
        # c read the lease before b wrote it, so its write is refused.
        with mock.patch.object(self.store, "get", return_value=stale), mock.patch.object(LEADER, "kget", lambda path: copy.deepcopy(stale)):
            self.assertFalse(self.as_replica("homestead-c", later))
        self.assertEqual("homestead-b", self.store.lease["spec"]["holderIdentity"])

    def test_a_released_lease_is_taken_at_once(self):
        with mock.patch.object(LEADER, "IDENTITY", "homestead-a"):
            LEADER.attempt(T0)
            LEADER.release()
        self.assertTrue(self.as_replica("homestead-b", T0 + datetime.timedelta(seconds=1)))


COUNTER = """
import json, os, sys
sys.path.insert(0, {server!r})
import homestead_shared as SHARED
SHARED.bind({data!r})
lock = SHARED.SharedLock("counter")
path = os.path.join({data!r}, "counter.json")
for _ in range(150):
    with lock:
        try:
            value = json.load(open(path))
        except (OSError, ValueError):
            value = 0
        SHARED.write_json(path, value + 1)
"""


class SharedLockTests(unittest.TestCase):
    @unittest.skipIf(SHARED.fcntl is None, "flock is a POSIX facility")
    def test_two_processes_never_lose_each_others_writes(self):
        with tempfile.TemporaryDirectory() as data:
            script = COUNTER.format(server=str(ROOT / "server"), data=data)
            runs = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(3)]
            for run in runs:
                self.assertEqual(0, run.wait(60))
            self.assertEqual(450, json.loads(Path(data, "counter.json").read_text()))

    def test_the_lock_is_reentrant_within_a_thread(self):
        with tempfile.TemporaryDirectory() as data:
            keep_bound(self, SHARED)
            SHARED.bind(data)
            lock = SHARED.SharedLock("nested")
            with lock:
                with lock:
                    SHARED.write_json(os.path.join(data, "x.json"), {"ok": True})
            self.assertEqual({"ok": True}, json.loads(Path(data, "x.json").read_text()))
            self.assertEqual([], [n for n in os.listdir(data) if n.endswith(".tmp")])


class ReplicaSettingTests(unittest.TestCase):
    def test_one_copy_on_an_unshareable_volume_is_replaced_not_rolled(self):
        # Two pods on one migratable volume is a VM migration to Longhorn, and
        # it refuses the mount: "invalid controller count 2".
        import server
        dep = {"metadata": {"name": "homestead"}, "spec": {"replicas": 1,
               "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
               "selector": {"matchLabels": {"app": "homestead"}}, "template": {"spec": {}}}}
        sent = []
        with mock.patch.object(server.KUBE_HELPERS, "kget", lambda path: copy.deepcopy(dep)),                 mock.patch.object(server.SELF_DATA_ROUTES, "homestead_data_volume", lambda *a: {"shareable": False}),                 mock.patch.object(server.KUBE_HELPERS, "ksend", lambda *a, **k: sent.append(a)):
            server.SELF_DATA_ROUTES.fit_own_strategy()
            server.SELF_HEALTH.set_replicas(1)
        self.assertEqual({"spec": {"strategy": {"type": "Recreate", "rollingUpdate": None}}}, sent[0][2])
        self.assertEqual({"type": "Recreate"}, sent[1][2]["spec"]["strategy"])
        deploy = (Path(__file__).resolve().parents[1] / "deploy" / "deploy.yaml").read_text()
        self.assertIn("type: Recreate", deploy)
        self.assertNotIn("maxSurge", deploy)

    def test_more_copies_spread_over_nodes_and_roll_one_at_a_time(self):
        import server
        dep = {"metadata": {"name": "homestead"}, "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
               "selector": {"matchLabels": {"app": "homestead"}}, "template": {"spec": {}}}}
        sent = []
        with mock.patch.object(server.KUBE_HELPERS, "kget", lambda path: copy.deepcopy(dep)), \
                mock.patch.object(server.SELF_DATA_ROUTES, "homestead_data_volume", lambda *a: {"shareable": True}), \
                mock.patch.object(server.KUBE_HELPERS, "ksend", lambda *a, **k: sent.append(a)):
            server.SELF_HEALTH.set_replicas(2)
            with self.assertRaises(ValueError):
                server.SELF_HEALTH.set_replicas(9)
        body = sent[0][2]
        self.assertEqual(2, body["spec"]["replicas"])
        self.assertEqual("RollingUpdate", body["spec"]["strategy"]["type"])
        self.assertEqual(0, body["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"])
        term = body["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual([{"app": "homestead"}], [t["podAffinityTerm"]["labelSelector"]["matchLabels"] for t in term])


if __name__ == "__main__":
    unittest.main()
