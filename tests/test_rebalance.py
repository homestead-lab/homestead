"""Moving volume copies so hosts hold similar amounts.

k3s-1 came back from a reboot after a long while cordoned: Longhorn had
rebuilt every copy on the other two hosts, which then held 32 each to its 3.
On the shared fake API (tests/fakes/kube.py)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_operations as OPS
import homestead_rebalance as R
from fakes.kube import Kube

LH = R.LH
GIB = R.GIB
LONGHORN = "longhorn.io/v1beta2"


def volume(name, size_gb, hosts, attached="", state="attached", replicas=2, app="", robustness="healthy"):
    return {"apiVersion": LONGHORN, "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": name},
            "spec": {"numberOfReplicas": replicas},
            "status": {"state": state, "robustness": robustness, "currentNodeID": attached, "actualSize": size_gb * GIB,
                       "kubernetesStatus": {"namespace": "lab", "pvcName": name,
                                            "workloadsStatus": [{"workloadName": f"{app or name}-5c9d8", "workloadType": "ReplicaSet"}]}}}


def replica(volume_name, host, n=0, state="running", healthy=True):
    return {"apiVersion": LONGHORN, "kind": "Replica", "metadata": {"namespace": "longhorn-system", "name": f"{volume_name}-r-{host}-{n}"},
            "spec": {"volumeName": volume_name, "nodeID": host, **({"healthyAt": "2026-10-04T00:00:00Z"} if healthy else {})},
            "status": {"currentState": state}}


def engine(volume_name, modes):
    return {"apiVersion": LONGHORN, "kind": "Engine", "metadata": {"namespace": "longhorn-system", "name": f"{volume_name}-e-0"},
            "spec": {"volumeName": volume_name}, "status": {"replicaModeMap": modes}}


def setting(name, value):
    return {"apiVersion": LONGHORN, "kind": "Setting", "metadata": {"namespace": "longhorn-system", "name": name}, "value": value}


class World:
    """Three hosts, their volumes and copies, on the shared fake API; the
    module under test is bound to the shared client, then put back."""

    def __init__(self, test, volumes, replicas, cordoned=(), hdd=("k1",), leader=True):
        hosts = ("k1", "k2", "k3")
        lh_nodes = [{"apiVersion": LONGHORN, "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": h},
                     "spec": {"allowScheduling": True, "disks": {"d": {"allowScheduling": True, "tags": ["hdd"] if h != "k1" or "k1" in hdd else []}}},
                     "status": {"diskStatus": {"d": {"storageAvailable": 900 * GIB, "storageMaximum": 1000 * GIB}}}}
                    for h in hosts]
        nodes = [{"apiVersion": "v1", "kind": "Node", "metadata": {"name": h}, "spec": {"unschedulable": h in cordoned},
                  "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for h in hosts]
        self.kube = test.enterContext(Kube(*lh_nodes, *nodes, *volumes, *replicas,
                                           setting("disable-scheduling-on-cordoned-node", "true"),
                                           setting("offline-replica-rebuilding", "true")))
        # The shared fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under its DATA_DIR.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
        saved = (R.kget, R.ksend, R.is_leader)
        test.addCleanup(lambda: setattr(R, "kget", saved[0]) or setattr(R, "ksend", saved[1]) or setattr(R, "is_leader", saved[2]))
        R.bind(K.kget, K.ksend, lambda: leader)

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def edit(self, path, change):
        """Someone else changes the object at path; change(obj) edits a copy."""
        obj = self.kube.object(path)
        change(obj)
        self.kube.add(obj)

    def volume(self, name):
        return self.kube.object(f"{LH}/volumes/{name}")

    def edit_volume(self, name, change):
        self.edit(f"{LH}/volumes/{name}", change)

    def each_volume(self, change):
        for item in K.kget(f"{LH}/volumes")["items"]:
            self.edit_volume(item["metadata"]["name"], change)

    def replicas(self):
        return K.kget(f"{LH}/replicas")["items"]

    def edit_replica(self, name, change):
        self.edit(f"{LH}/replicas/{name}", change)

    def drop_replica(self, name):
        self.kube.discard(f"{LH}/replicas/{name}")

    def set_engines(self, *engines):
        for old in K.kget(f"{LH}/engines")["items"]:
            self.kube.discard(f"{LH}/engines/{old['metadata']['name']}")
        for new in engines:
            self.kube.add(new)

    def notes(self, name):
        return self.volume(name)["metadata"].get("annotations") or {}


def lopsided(test):
    """k1 empty; everything on k2 and k3, each app attached on k2."""
    vols, reps = [], []
    for i, size in enumerate((100, 60, 30, 20)):
        name = f"v{i}"
        vols.append(volume(name, size, ["k2", "k3"], attached="k2", app=f"app{i}"))
        reps += [replica(name, "k2"), replica(name, "k3")]
    return World(test, vols, reps)


class PlanTests(unittest.TestCase):
    def test_copies_move_to_the_empty_host_never_off_the_host_their_app_runs_on(self):
        lopsided(self)
        plan = R.plan()
        self.assertTrue(plan["moves"])
        self.assertTrue(all(m["to"] == "k1" and m["from"] == "k3" for m in plan["moves"]), plan["moves"])
        hosts = {h["name"]: h for h in plan["hosts"]}
        self.assertLess(abs(hosts["k1"]["after_gb"] - hosts["k3"]["after_gb"]), abs(hosts["k1"]["before_gb"] - hosts["k3"]["before_gb"]))
        self.assertEqual("lab/app0", plan["moves"][0]["app"], "named by the Deployment, not its ReplicaSet")

    def test_an_app_left_out_keeps_its_copies_and_the_plan_works_round_it(self):
        lopsided(self)
        first = R.plan()["moves"][0]["app"]
        again = R.plan([first])
        self.assertNotIn(first, [m["app"] for m in again["moves"]])
        self.assertIn(first, again["apps"], "still listed, unticked")

    def test_a_container_moving_to_a_host_with_no_copy_takes_one_with_it(self):
        # Balance hosts moves app3 to k1: a copy of its volume follows, from
        # k3 - not k2, where it is attached until it restarts.
        lopsided(self)
        plan = R.plan(follow=[("lab/app3", "k1")])
        first = plan["moves"][0]
        self.assertEqual(("v3", "k3", "k1", "lab/app3"), (first["volume"], first["from"], first["to"], first.get("follows")))
        self.assertEqual(1, sum(m["volume"] == "v3" for m in plan["moves"]))
        self.assertIn("lab/app3", plan["apps"])
        R.reviewed(plan["moves"], [], plan["review_token"])

    def test_a_copy_that_cannot_follow_says_why(self):
        c = lopsided(self)
        for r in c.replicas():
            if r["spec"]["volumeName"] == "v3" and r["spec"]["nodeID"] != "k2":
                c.drop_replica(r["metadata"]["name"])
        c.edit_volume("v3", lambda v: v["spec"].update(numberOfReplicas=1))
        plan = R.plan(follow=[("lab/app3", "k1"), ("lab/app2", "k2")])
        why = {s["claim"]: s["why"] for s in plan["skipped"]}
        self.assertIn("no copy follows lab/app3 to k1: its only copies are where it is attached now", why["lab/v3"])
        self.assertNotIn("lab/v2", why, "it has a copy on k2 already")

    def test_what_cannot_move_says_why(self):
        c = lopsided(self)
        c.edit_volume("v0", lambda v: v["status"].update(robustness="degraded"))
        c.edit(f"{LH}/settings/offline-replica-rebuilding", lambda s: s.update(value="false"))
        c.edit_volume("v1", lambda v: v["status"].update(state="detached", currentNodeID=""))
        why = {s["claim"]: s["why"] for s in R.plan()["skipped"]}
        self.assertEqual("it is not healthy now", why["lab/v0"])
        self.assertIn("offline rebuilding", why["lab/v1"])

    def test_a_volume_moves_only_to_a_disk_with_the_tags_its_storage_class_asks_for(self):
        for k1_has_hdd in (True, False):
            c = lopsided(self)
            c.edit(f"{LH}/nodes/k1", lambda n: n["spec"]["disks"]["d"].update(tags=["hdd"] if k1_has_hdd else ["ssd"]))
            c.each_volume(lambda v: v["spec"].update(diskSelector=["hdd"]))
            self.assertEqual(k1_has_hdd, bool(R.plan()["moves"]))

    def test_the_reviewed_copies_move_though_the_apps_wrote_since(self):
        c = lopsided(self)
        plan = R.plan()
        c.each_volume(lambda v: v["status"].update(actualSize=int(v["status"]["actualSize"]) + GIB // 3))   # apps keep writing
        self.assertEqual(plan["review_token"], R.plan()["review_token"])
        moves = R.reviewed(plan["moves"], [], plan["review_token"])
        self.assertEqual([(m["volume"], m["from"], m["to"]) for m in plan["moves"]], [(m["volume"], m["from"], m["to"]) for m in moves])

    def test_reviewed_copies_that_differ_or_changed_need_a_new_review(self):
        c = lopsided(self)
        plan = R.plan()
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            R.reviewed([dict(plan["moves"][0], to="k2")], [], plan["review_token"])
        name = plan["moves"][0]["volume"]
        c.kube.add(replica(name, "k1"))     # someone put a copy there meanwhile
        c.edit_volume(name, lambda v: v["spec"].update(numberOfReplicas=3))
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            R.reviewed(plan["moves"], [], plan["review_token"])

    def test_a_cordoned_host_takes_no_copies(self):
        c = lopsided(self)
        c.edit("/api/v1/nodes/k1", lambda n: n["spec"].update(unschedulable=True))
        plan = R.plan()
        self.assertEqual([], plan["moves"])
        self.assertEqual(["k1"], plan["closed"], "the review can say why")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.c = World(self, [volume("v0", 50, ["k2", "k3"], attached="k2")], [replica("v0", "k2"), replica("v0", "k3")])
        self.item = {"id": "job", "ref": {"moves": [{"volume": "v0", "claim": "lab/v0", "app": "lab/v0", "from": "k3", "to": "k1", "size_gb": 50}],
                                          "index": 0, "moved": 0, "stage": "add"}}

    def deleted(self):
        return any(m == "DELETE" for m, _, _ in self.c.sent)

    def test_a_copy_is_built_before_the_old_one_goes(self):
        state, _, message = R.status(self.item)
        self.assertEqual(3, self.c.volume("v0")["spec"]["numberOfReplicas"])
        self.assertIn("Building a copy", message)
        # Longhorn places the new copy on k1 and builds it.
        self.c.kube.add(replica("v0", "k1", n=1, healthy=False))
        self.assertIn("on k1", R.status(self.item)[2])
        self.assertFalse(self.deleted(), "nothing removed while the new copy is not whole")
        new = "v0-r-k1-1"
        self.c.edit_replica(new, lambda r: r["spec"].update(healthyAt="now"))
        self.c.set_engines(engine("v0", {"v0-r-k2-0": "RW", new: "RW"}))
        R.status(self.item)
        self.assertIn(("DELETE", f"{LH}/replicas/v0-r-k3-0", None), self.c.sent)
        self.assertEqual(2, self.c.volume("v0")["spec"]["numberOfReplicas"])
        R.status(self.item)
        state, progress, message = R.status(self.item)
        self.assertEqual(("succeeded", 100), (state, progress))
        self.assertIn("Moved 1 copy", message)

    def _build_new_copy_while_k2_fails(self):
        """The new copy on k1 builds; meanwhile the copy on k2 fails."""
        R.status(self.item)
        self.c.kube.add(replica("v0", "k1", n=1))
        def failed(r):
            r["spec"]["failedAt"] = "now"
            r["status"]["currentState"] = "error"
        self.c.edit_replica("v0-r-k2-0", failed)
        self.c.set_engines(engine("v0", {"v0-r-k3-0": "RW", "v0-r-k1-1": "RW"}))

    def test_a_copy_that_fails_while_the_new_one_builds_keeps_the_old_copy_until_it_is_rebuilt(self):
        # Removing the old copy now would leave one whole copy of a volume
        # that asks for two. Longhorn rebuilds the failed one (the count is
        # still one over), then the move goes ahead.
        self._build_new_copy_while_k2_fails()
        state, _, message = R.status(self.item)
        self.assertEqual("running", state)
        self.assertIn("rebuild", message)
        self.assertFalse(self.deleted(), "the old copy is kept")
        self.assertEqual(3, self.c.volume("v0")["spec"]["numberOfReplicas"])
        self.c.kube.add(replica("v0", "k2", n=2, healthy=False))   # Longhorn rebuilds on k2
        R.status(self.item)
        self.assertFalse(self.deleted(), "a copy still rebuilding is not whole")
        self.c.edit_replica("v0-r-k2-2", lambda r: r["spec"].update(healthyAt="now"))
        self.c.set_engines(engine("v0", {"v0-r-k3-0": "RW", "v0-r-k1-1": "RW", "v0-r-k2-2": "RW"}))
        R.status(self.item)
        self.assertIn(("DELETE", f"{LH}/replicas/v0-r-k3-0", None), self.c.sent)

    def test_a_failed_copy_not_rebuilt_in_time_skips_the_move_and_keeps_the_old_copy(self):
        self._build_new_copy_while_k2_fails()
        R.status(self.item)
        self.item["ref"]["started"] = time.time() - 3600 - 120 * 50 - 60
        R.status(self.item)
        self.assertFalse(self.deleted())
        self.assertIn("another copy failed during the move; the old copy is kept", " ".join(self.item["ref"]["skipped"]))
        v = self.c.volume("v0")
        self.assertEqual(2, v["spec"]["numberOfReplicas"])
        self.assertNotIn(R.MOVING, v["metadata"].get("annotations") or {})

    def test_cancelling_while_a_failed_copy_rebuilds_puts_the_copy_count_back(self):
        self._build_new_copy_while_k2_fails()
        R.status(self.item)
        R.cancel_run(self.item, {})
        v = self.c.volume("v0")
        self.assertEqual(2, v["spec"]["numberOfReplicas"])
        self.assertNotIn(R.MOVING, v["metadata"].get("annotations") or {})

    def test_the_volume_is_marked_as_moving_a_copy_until_it_settles(self):
        # Longhorn calls a volume asking for one more copy than it has
        # "degraded"; the mark says it keeps every copy it had meanwhile.
        notes = lambda: self.c.notes("v0")
        R.status(self.item)
        self.assertEqual("2", notes().get(R.MOVING))
        self.c.kube.add(replica("v0", "k1", n=1, healthy=True))
        new = "v0-r-k1-1"
        self.c.edit_replica(new, lambda r: r["spec"].update(healthyAt="now"))
        self.c.set_engines(engine("v0", {"v0-r-k2-0": "RW", new: "RW"}))
        R.status(self.item)                                  # old copy removed: still marked
        self.assertEqual("2", notes().get(R.MOVING))
        R.status(self.item)                                  # settled
        self.assertNotIn(R.MOVING, notes())

    def test_a_move_given_up_or_cancelled_leaves_no_mark(self):
        R.status(self.item)
        R.cancel_run(self.item, {})
        self.assertNotIn(R.MOVING, self.c.notes("v0"))

    def test_a_new_copy_longhorn_drops_and_remakes_is_waited_for_not_given_up(self):
        # A long rebuild failed part-way: Longhorn dropped the new copy and was
        # about to make another. The move was an hour old - that is not a copy
        # "never placed", and giving up cut the volume's count mid-rebuild.
        R.status(self.item)
        self.c.kube.add(replica("v0", "k1", n=1, healthy=False))
        R.status(self.item)                                   # placed, building
        self.item["ref"]["started"] = time.time() - 5400
        self.c.drop_replica("v0-r-k1-1")                      # dropped by Longhorn
        state, _, message = R.status(self.item)
        self.assertEqual("running", state)
        self.assertIn("replacing the new copy", message)
        self.assertEqual(3, self.c.volume("v0")["spec"]["numberOfReplicas"], "count kept up")
        self.c.kube.add(replica("v0", "k1", n=2, healthy=False))  # remade
        self.assertIn("on k1", R.status(self.item)[2])
        self.assertNotIn("gone_since", self.item["ref"])

    def test_a_new_copy_dropped_and_never_remade_is_given_up_with_the_right_reason(self):
        R.status(self.item)
        self.c.kube.add(replica("v0", "k1", n=1, healthy=False))
        R.status(self.item)
        self.c.drop_replica("v0-r-k1-1")
        R.status(self.item)
        self.item["ref"]["gone_since"] = time.time() - 700
        R.status(self.item)
        self.assertIn("dropped the new copy", " ".join(self.item["ref"]["skipped"]))
        self.assertEqual(2, self.c.volume("v0")["spec"]["numberOfReplicas"])

    def test_copies_changed_since_the_review_are_left_alone(self):
        self.c.kube.add(replica("v0", "k1"))
        R.status(self.item)
        self.assertEqual([], [s for s in self.c.sent if s[0] in ("PATCH", "DELETE")])
        state, _, message = R.status(self.item)
        self.assertEqual("succeeded", state)
        self.assertIn("changed since the review", message)

    def test_cancelling_mid_build_puts_the_copy_count_back(self):
        R.status(self.item)
        message = R.cancel_run(self.item, {})
        self.assertEqual(2, self.c.volume("v0")["spec"]["numberOfReplicas"])
        self.assertIn("Stopped after moving 0 copies", message)

    def test_only_the_leader_changes_anything(self):
        R.bind(K.kget, K.ksend, lambda: False)
        state, _, _ = R.status(self.item)
        self.assertEqual("running", state)
        self.assertEqual([], self.c.sent)

    def test_a_new_copy_never_built_is_given_up_and_the_count_put_back(self):
        R.status(self.item)
        self.item["ref"]["started"] = time.time() - 700
        R.status(self.item)
        self.assertEqual(2, self.c.volume("v0")["spec"]["numberOfReplicas"])
        self.assertIn("did not place", " ".join(self.item["ref"]["skipped"]))


if __name__ == "__main__":
    unittest.main()
