"""Homestead's data, on local-path on a one-host k3s install, moves to Longhorn
by itself once a second host can keep a copy, through the same reviewed move
a person makes (design 3.1, decision 7)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_self_data_grow as G
from rebind import keep_bound


class Cluster:
    def __init__(self):
        self.hosts, self.platform = 1, {"distribution": "k3s", "longhorn": True}
        self.claim, self.on_longhorn, self.copies = "homestead-data", False, 1
        self.blockers, self.rolling, self.jobs, self.calls = [], False, [], []
        self.r2 = {"exists": False}

    def own(self):
        return [{"claim": self.claim, "pv": "pv-1", "storage_class": "longhorn-r2" if self.on_longhorn else "local-path",
                 "size_gb": 2.0, "volume": {"spec": {}} if self.on_longhorn else None}]

    def kget(self, path):
        if "/storageclasses/" in path:
            return {"provisioner": "driver.longhorn.io" if path.endswith("longhorn-r2") else "rancher.io/local-path"}
        if "/deployments/" in path:
            return {"spec": {"replicas": self.copies}}
        raise AssertionError(path)

    def prepare(self, body, actor, start=False):
        self.calls.append(("prepare-start" if start else "prepare-review", body, actor))
        if not start:
            return {"capacity_token": "t1"}
        self.jobs.append({"ref": {"operation": body["operation"]}, "status": "running"})
        return {"ok": True, "operation": "job-1", "destination": "homestead-data-" + body["operation"]}

    def preview(self, body, actor):
        self.calls.append(("move-review", body, actor))
        return {"capacity_token": "t2"}

    def start(self, body, actor):
        self.calls.append(("move-start", body, actor))
        return {"ok": True}, "cookie"


class GrowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.c = c = Cluster()
        keep_bound(self, G)
        G.bind(tmp.name, prepare_fn=c.prepare, preview_fn=c.preview, start_fn=c.start, jobs_fn=lambda: c.jobs,
               blockers_fn=lambda: c.blockers, rollout_fn=lambda: c.rolling, node_fn=lambda: "node1")
        for target, name, value in (
                (G.NODE_PARITY, "platform", lambda: c.platform), (G.NODE_PARITY, "kget", c.kget),
                (G.NODE_PARITY, "OWN", ("lab", "homestead")),
                (G.NODE_PARITY, "_ready_nodes", lambda: []),
                (G.NODE_PARITY, "_storage_hosts", lambda nodes: [f"node{i + 1}" for i in range(c.hosts)]),
                (G.LHCAP, "capacity", lambda: {"nodes": [{"name": f"node{i + 1}", "room_gb": 50} for i in range(c.hosts)]}),
                (G.DATA_COPIES, "own_data_volumes", c.own), (G.DATA_COPIES, "_one_host", lambda: c.hosts == 1),
                (G.DATA_COPIES, "two_copy_class", lambda: c.r2),
                (G.STORAGE_CLASSES, "create_storage_class", lambda cfg: c.calls.append(("class", cfg, None)))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def phase(self):
        return G._load().get("phase", "idle")

    def kinds(self):
        return [kind for kind, _, _ in self.c.calls]

    def test_the_whole_move_once_the_cluster_grows(self):
        self.assertIn("moves to Longhorn by itself", G.tick(0))
        self.assertTrue(G._load()["armed"])
        self.assertEqual("", G.tick(60), "one host: nothing to do yet")
        self.c.hosts = 2
        self.assertIn("preparing its volume on Longhorn", G.tick(120))
        self.assertEqual(["class", "prepare-review", "prepare-start"], self.kinds())
        review = self.c.calls[1][1]
        self.assertEqual(("longhorn-r2", "node1", True), (review["storage_class"], review["node"], review["one_host"]))
        self.assertEqual(G.ACTOR, self.c.calls[1][2])
        self.assertEqual({"capacity_token": "t1", "confirm_capacity": True},
                         {k: self.c.calls[2][1][k] for k in ("capacity_token", "confirm_capacity")})
        self.assertEqual("preparing", self.phase())
        self.assertEqual("", G.tick(180), "the preparation is still running")
        self.c.jobs[0].update(status="succeeded", ref={**self.c.jobs[0]["ref"], "prepared": True})
        self.assertIn("Homestead restarts on homestead-data-", G.tick(240))
        move = self.c.calls[-1][1]
        self.assertEqual(("move-start", True, True, "t2", "node1", "node1"),
                         (self.c.calls[-1][0], move["confirm_move"], move["confirm_capacity"], move["capacity_token"],
                          move["worker_node"], move["copy_node"]))
        self.assertEqual("moving", self.phase())
        (fact,) = G.alert_facts(300)
        self.assertEqual(("self-data-grow:moving", "info"), (fact["key"], fact["severity"]))
        self.assertIn("unavailable for about a minute", fact["body"])
        # The Homestead that starts on the new volume finds the phase there.
        self.c.claim, self.c.on_longhorn = G._load()["destination"], True
        self.assertIn("Homestead's data moved to longhorn-r2", G.tick(400))
        self.assertEqual(("done", False), (self.phase(), G._load()["armed"]))
        (fact,) = G.alert_facts(500)
        self.assertEqual("Homestead moved its data to Longhorn", fact["title"])
        self.assertIn("homestead-data is kept", fact["body"])
        self.assertEqual([], G.alert_facts(500 + G.SHOW_DONE + 1))
        # Never again: not even after a person puts it back on local-path.
        self.c.claim, self.c.on_longhorn = "homestead-data", False
        calls = len(self.c.calls)
        G.tick(10_000)
        self.assertEqual(calls, len(self.c.calls))

    def test_it_waits_for_a_quiet_moment_and_says_what_for(self):
        G.tick(0)
        self.c.hosts, self.c.blockers = 2, [{"id": "j"}]
        self.assertIn("waits for a quiet moment", G.tick(60))
        self.assertEqual("waiting", self.phase())
        self.assertIn("It waits for the jobs that are running or keep resources to finish.", G.alert_facts(60)[0]["body"])
        self.c.blockers, self.c.rolling = [], True
        G.tick(120)
        self.assertEqual([], [k for k in self.kinds() if k.startswith("prepare")])
        self.c.rolling = False
        G.tick(180)
        self.assertEqual("preparing", self.phase())

    def test_data_a_person_put_on_local_path_on_a_bigger_cluster_stays(self):
        self.c.hosts = 2
        for at in (0, 60, 120):
            G.tick(at)
        self.assertEqual([], self.c.calls)
        self.assertFalse(G._load().get("armed"))

    def test_a_failed_move_is_said_and_tried_again_three_times_at_most(self):
        G.tick(0)
        self.c.hosts = 2
        self.c.prepare = lambda body, actor, start=False: (_ for _ in ()).throw(ValueError("no room on node2"))
        G.prepare = self.c.prepare
        self.assertIn("no room on node2", G.tick(60))
        (fact,) = G.alert_facts(60)
        self.assertEqual(("self-data-grow:failed", "degraded"), (fact["key"], fact["severity"]))
        self.assertIn("The reason: no room on node2. It tries again in six hours.", fact["body"])
        self.assertEqual("", G.tick(120), "not before the six hours")
        G.tick(60 + G.RETRY)
        G.tick(60 + 2 * G.RETRY + 60)
        state = G._load()
        self.assertEqual((3, None), (state["tries"], state["next_try"]))
        self.assertIn("Move it in Settings, About, Redundancy.", G.alert_facts(0)[0]["body"])
        G.tick(60 + 10 * G.RETRY)
        self.assertEqual(3, G._load()["tries"])

    def test_a_read_that_fails_is_not_a_try(self):
        G.tick(0)
        self.c.hosts = 2
        with mock.patch.object(G.LHCAP, "capacity", lambda: (_ for _ in ()).throw(OSError("Longhorn did not answer"))):
            self.assertIn("not checked: Longhorn did not answer", G.tick(60))
        self.assertNotEqual("failed", self.phase())

    def test_nothing_on_harvester_rke2_without_longhorn_or_two_copies_of_homestead(self):
        self.c.platform = {"distribution": "k3s", "longhorn": False}
        self.assertEqual("", G.tick(0))
        self.assertFalse(G._load().get("armed"))
        self.c.platform = {"distribution": "k3s", "longhorn": True}
        G.tick(1)
        self.c.hosts, self.c.copies = 2, 2
        G.tick(2)
        self.assertEqual([], [k for k in self.kinds() if k.startswith("prepare")])

    def test_a_move_that_did_not_finish_is_a_failure_after_an_hour(self):
        G._save({"phase": "moving", "armed": True, "operation": "a" * 24, "destination": "homestead-data-x",
                 "source": "homestead-data", "storage_class": "longhorn-r2", "node": "node1", "moved_at": 0})
        self.assertEqual("", G.tick(600))
        self.assertIn("back on its original data", G.tick(3700))
        self.assertEqual("failed", self.phase())


if __name__ == "__main__":
    unittest.main()
