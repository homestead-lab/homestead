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
        LEADER.bind(self.store.get, self.store.send, "lab")
        LEADER._state.update(leader=False, holder="")

    def as_replica(self, name, at):
        with mock.patch.object(LEADER, "IDENTITY", name):
            return LEADER.attempt(at)

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
            SHARED.bind(data)
            lock = SHARED.SharedLock("nested")
            with lock:
                with lock:
                    SHARED.write_json(os.path.join(data, "x.json"), {"ok": True})
            self.assertEqual({"ok": True}, json.loads(Path(data, "x.json").read_text()))
            self.assertEqual([], [n for n in os.listdir(data) if n.endswith(".tmp")])


class ReplicaSettingTests(unittest.TestCase):
    def test_more_copies_spread_over_nodes_and_roll_one_at_a_time(self):
        import server
        dep = {"metadata": {"name": "homestead"}, "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
               "selector": {"matchLabels": {"app": "homestead"}}, "template": {"spec": {}}}}
        sent = []
        with mock.patch.object(server, "kget", lambda path: copy.deepcopy(dep)), \
                mock.patch.object(server, "homestead_data_volume", lambda *a: {"shareable": True}), \
                mock.patch.object(server, "ksend", lambda *a, **k: sent.append(a)):
            server.set_homestead_replicas(2)
            with self.assertRaises(ValueError):
                server.set_homestead_replicas(9)
        body = sent[0][2]
        self.assertEqual(2, body["spec"]["replicas"])
        self.assertEqual("RollingUpdate", body["spec"]["strategy"]["type"])
        self.assertEqual(0, body["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"])
        term = body["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual([{"app": "homestead"}], [t["podAffinityTerm"]["labelSelector"]["matchLabels"] for t in term])


if __name__ == "__main__":
    unittest.main()
