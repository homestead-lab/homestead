"""Moving volume copies so hosts hold similar amounts.

k3s-1 came back from a reboot after a long while cordoned: Longhorn had
rebuilt every copy on the other two hosts, which then held 32 each to its 3."""
import copy
import sys
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_rebalance as R

LH = R.LH
GIB = R.GIB


def volume(name, size_gb, hosts, attached="", state="attached", replicas=2, app="", robustness="healthy"):
    return {"metadata": {"name": name},
            "spec": {"numberOfReplicas": replicas},
            "status": {"state": state, "robustness": robustness, "currentNodeID": attached, "actualSize": size_gb * GIB,
                       "kubernetesStatus": {"namespace": "lab", "pvcName": name,
                                            "workloadsStatus": [{"workloadName": f"{app or name}-5c9d8", "workloadType": "ReplicaSet"}]}}}


def replica(volume_name, host, n=0, state="running", healthy=True):
    return {"metadata": {"name": f"{volume_name}-r-{host}-{n}"},
            "spec": {"volumeName": volume_name, "nodeID": host, **({"healthyAt": "2026-10-04T00:00:00Z"} if healthy else {})},
            "status": {"currentState": state}}


class Cluster:
    def __init__(self, volumes, replicas, cordoned=(), hdd=("k1",)):
        self.objects = {
            f"{LH}/nodes": {"items": [{"metadata": {"name": h}, "spec": {"allowScheduling": True, "disks": {"d": {"allowScheduling": True, "tags": ["hdd"] if h != "k1" or "k1" in hdd else []}}},
                                       "status": {"diskStatus": {"d": {"storageAvailable": 900 * GIB, "storageMaximum": 1000 * GIB}}}}
                                      for h in ("k1", "k2", "k3")]},
            "/api/v1/nodes": {"items": [{"metadata": {"name": h}, "spec": {"unschedulable": h in cordoned},
                                         "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for h in ("k1", "k2", "k3")]},
            f"{LH}/volumes": {"items": volumes}, f"{LH}/replicas": {"items": replicas}, f"{LH}/engines": {"items": []},
            f"{LH}/settings/disable-scheduling-on-cordoned-node": {"value": "true"},
            f"{LH}/settings/offline-replica-rebuilding": {"value": "true"}}
        self.sent = []

    def get(self, path):
        if path.startswith(f"{LH}/volumes/"):
            name = path.rsplit("/", 1)[1]
            found = next((v for v in self.objects[f"{LH}/volumes"]["items"] if v["metadata"]["name"] == name), None)
            if found is None:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return copy.deepcopy(found)
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.objects[path])

    def send(self, method, path, body=None, ctype=None):
        self.sent.append((method, path, body))
        if method == "PATCH" and "/volumes/" in path:
            name = path.rsplit("/", 1)[1]
            v = next(v for v in self.objects[f"{LH}/volumes"]["items"] if v["metadata"]["name"] == name)
            v["spec"].update(body["spec"])
            # A merge patch: a null annotation is removed.
            notes = v.setdefault("metadata", {}).setdefault("annotations", {})
            for key, value in ((body.get("metadata") or {}).get("annotations") or {}).items():
                if value is None:
                    notes.pop(key, None)
                else:
                    notes[key] = value
        if method == "DELETE" and "/replicas/" in path:
            name = path.rsplit("/", 1)[1]
            self.objects[f"{LH}/replicas"]["items"] = [r for r in self.objects[f"{LH}/replicas"]["items"] if r["metadata"]["name"] != name]
        return {}


def lopsided():
    """k1 empty; everything on k2 and k3, each app attached on k2."""
    vols, reps = [], []
    for i, size in enumerate((100, 60, 30, 20)):
        name = f"v{i}"
        vols.append(volume(name, size, ["k2", "k3"], attached="k2", app=f"app{i}"))
        reps += [replica(name, "k2"), replica(name, "k3")]
    return Cluster(vols, reps)


class PlanTests(unittest.TestCase):
    def test_copies_move_to_the_empty_host_never_off_the_host_their_app_runs_on(self):
        c = lopsided()
        R.bind(c.get, c.send, lambda: True)
        plan = R.plan()
        self.assertTrue(plan["moves"])
        self.assertTrue(all(m["to"] == "k1" and m["from"] == "k3" for m in plan["moves"]), plan["moves"])
        hosts = {h["name"]: h for h in plan["hosts"]}
        self.assertLess(abs(hosts["k1"]["after_gb"] - hosts["k3"]["after_gb"]), abs(hosts["k1"]["before_gb"] - hosts["k3"]["before_gb"]))
        self.assertEqual("lab/app0", plan["moves"][0]["app"], "named by the Deployment, not its ReplicaSet")

    def test_an_app_left_out_keeps_its_copies_and_the_plan_works_round_it(self):
        c = lopsided()
        R.bind(c.get, c.send, lambda: True)
        first = R.plan()["moves"][0]["app"]
        again = R.plan([first])
        self.assertNotIn(first, [m["app"] for m in again["moves"]])
        self.assertIn(first, again["apps"], "still listed, unticked")

    def test_what_cannot_move_says_why(self):
        c = lopsided()
        c.objects[f"{LH}/volumes"]["items"][0]["status"]["robustness"] = "degraded"
        c.objects[f"{LH}/settings/offline-replica-rebuilding"]["value"] = "false"
        c.objects[f"{LH}/volumes"]["items"][1]["status"].update(state="detached", currentNodeID="")
        R.bind(c.get, c.send, lambda: True)
        why = {s["claim"]: s["why"] for s in R.plan()["skipped"]}
        self.assertEqual("it is not healthy now", why["lab/v0"])
        self.assertIn("offline rebuilding", why["lab/v1"])

    def test_a_volume_moves_only_to_a_disk_with_the_tags_its_storage_class_asks_for(self):
        for k1_has_hdd in (True, False):
            c = lopsided()
            c.objects[f"{LH}/nodes"]["items"][0]["spec"]["disks"]["d"]["tags"] = ["hdd"] if k1_has_hdd else ["ssd"]
            for v in c.objects[f"{LH}/volumes"]["items"]:
                v["spec"]["diskSelector"] = ["hdd"]
            R.bind(c.get, c.send, lambda: True)
            self.assertEqual(k1_has_hdd, bool(R.plan()["moves"]))

    def test_the_reviewed_copies_move_though_the_apps_wrote_since(self):
        c = lopsided()
        R.bind(c.get, c.send, lambda: True)
        plan = R.plan()
        for v in c.objects[f"{LH}/volumes"]["items"]:      # apps keep writing
            v["status"]["actualSize"] = int(v["status"]["actualSize"]) + GIB // 3
        self.assertEqual(plan["review_token"], R.plan()["review_token"])
        moves = R.reviewed(plan["moves"], [], plan["review_token"])
        self.assertEqual([(m["volume"], m["from"], m["to"]) for m in plan["moves"]], [(m["volume"], m["from"], m["to"]) for m in moves])

    def test_reviewed_copies_that_differ_or_changed_need_a_new_review(self):
        c = lopsided()
        R.bind(c.get, c.send, lambda: True)
        plan = R.plan()
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            R.reviewed([dict(plan["moves"][0], to="k2")], [], plan["review_token"])
        name = plan["moves"][0]["volume"]
        c.objects[f"{LH}/replicas"]["items"].append(replica(name, "k1"))     # someone put a copy there meanwhile
        next(v for v in c.objects[f"{LH}/volumes"]["items"] if v["metadata"]["name"] == name)["spec"]["numberOfReplicas"] = 3
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            R.reviewed(plan["moves"], [], plan["review_token"])

    def test_a_cordoned_host_takes_no_copies(self):
        c = lopsided()
        c.objects["/api/v1/nodes"]["items"][0]["spec"]["unschedulable"] = True
        R.bind(c.get, c.send, lambda: True)
        plan = R.plan()
        self.assertEqual([], plan["moves"])
        self.assertEqual(["k1"], plan["closed"], "the review can say why")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster([volume("v0", 50, ["k2", "k3"], attached="k2")], [replica("v0", "k2"), replica("v0", "k3")])
        R.bind(self.c.get, self.c.send, lambda: True)
        self.item = {"id": "job", "ref": {"moves": [{"volume": "v0", "claim": "lab/v0", "app": "lab/v0", "from": "k3", "to": "k1", "size_gb": 50}],
                                          "index": 0, "moved": 0, "stage": "add"}}

    def test_a_copy_is_built_before_the_old_one_goes(self):
        state, _, message = R.status(self.item)
        self.assertEqual(3, self.c.objects[f"{LH}/volumes"]["items"][0]["spec"]["numberOfReplicas"])
        self.assertIn("Building a copy", message)
        # Longhorn places the new copy on k1 and builds it.
        self.c.objects[f"{LH}/replicas"]["items"].append(replica("v0", "k1", n=1, healthy=False))
        self.assertIn("on k1", R.status(self.item)[2])
        self.assertFalse(any(m == "DELETE" for m, _, _ in self.c.sent), "nothing removed while the new copy is not whole")
        new = self.c.objects[f"{LH}/replicas"]["items"][-1]
        new["spec"]["healthyAt"] = "now"
        self.c.objects[f"{LH}/engines"]["items"] = [{"spec": {"volumeName": "v0"}, "status": {"replicaModeMap": {new["metadata"]["name"]: "RW"}}}]
        R.status(self.item)
        self.assertIn(("DELETE", f"{LH}/replicas/v0-r-k3-0", None), self.c.sent)
        self.assertEqual(2, self.c.objects[f"{LH}/volumes"]["items"][0]["spec"]["numberOfReplicas"])
        R.status(self.item)
        state, progress, message = R.status(self.item)
        self.assertEqual(("succeeded", 100), (state, progress))
        self.assertIn("Moved 1 copy", message)

    def test_the_volume_is_marked_as_moving_a_copy_until_it_settles(self):
        # Longhorn calls a volume asking for one more copy than it has
        # "degraded"; the mark says it keeps every copy it had meanwhile.
        notes = lambda: self.c.objects[f"{LH}/volumes"]["items"][0]["metadata"].get("annotations") or {}
        R.status(self.item)
        self.assertEqual("2", notes().get(R.MOVING))
        self.c.objects[f"{LH}/replicas"]["items"].append(replica("v0", "k1", n=1, healthy=True))
        new = self.c.objects[f"{LH}/replicas"]["items"][-1]
        new["spec"]["healthyAt"] = "now"
        self.c.objects[f"{LH}/engines"]["items"] = [{"spec": {"volumeName": "v0"}, "status": {"replicaModeMap": {new["metadata"]["name"]: "RW"}}}]
        R.status(self.item)                                  # old copy removed: still marked
        self.assertEqual("2", notes().get(R.MOVING))
        R.status(self.item)                                  # settled
        self.assertNotIn(R.MOVING, notes())

    def test_a_move_given_up_or_cancelled_leaves_no_mark(self):
        notes = lambda: self.c.objects[f"{LH}/volumes"]["items"][0]["metadata"].get("annotations") or {}
        R.status(self.item)
        R.cancel_run(self.item, {})
        self.assertNotIn(R.MOVING, notes())

    def test_a_new_copy_longhorn_drops_and_remakes_is_waited_for_not_given_up(self):
        # A long rebuild failed part-way: Longhorn dropped the new copy and was
        # about to make another. The move was an hour old - that is not a copy
        # "never placed", and giving up cut the volume's count mid-rebuild.
        replicas = self.c.objects[f"{LH}/replicas"]["items"]
        R.status(self.item)
        replicas.append(replica("v0", "k1", n=1, healthy=False))
        R.status(self.item)                                   # placed, building
        self.item["ref"]["started"] = time.time() - 5400
        replicas.pop()                                        # dropped by Longhorn
        state, _, message = R.status(self.item)
        self.assertEqual("running", state)
        self.assertIn("replacing the new copy", message)
        self.assertEqual(3, self.c.objects[f"{LH}/volumes"]["items"][0]["spec"]["numberOfReplicas"], "count kept up")
        replicas.append(replica("v0", "k1", n=2, healthy=False))  # remade
        self.assertIn("on k1", R.status(self.item)[2])
        self.assertNotIn("gone_since", self.item["ref"])

    def test_a_new_copy_dropped_and_never_remade_is_given_up_with_the_right_reason(self):
        replicas = self.c.objects[f"{LH}/replicas"]["items"]
        R.status(self.item)
        replicas.append(replica("v0", "k1", n=1, healthy=False))
        R.status(self.item)
        replicas.pop()
        R.status(self.item)
        self.item["ref"]["gone_since"] = time.time() - 700
        R.status(self.item)
        self.assertIn("dropped the new copy", " ".join(self.item["ref"]["skipped"]))
        self.assertEqual(2, self.c.objects[f"{LH}/volumes"]["items"][0]["spec"]["numberOfReplicas"])

    def test_copies_changed_since_the_review_are_left_alone(self):
        self.c.objects[f"{LH}/replicas"]["items"].append(replica("v0", "k1"))
        R.status(self.item)
        self.assertEqual([], [s for s in self.c.sent if s[0] in ("PATCH", "DELETE")])
        state, _, message = R.status(self.item)
        self.assertEqual("succeeded", state)
        self.assertIn("changed since the review", message)

    def test_cancelling_mid_build_puts_the_copy_count_back(self):
        R.status(self.item)
        message = R.cancel_run(self.item, {})
        self.assertEqual(2, self.c.objects[f"{LH}/volumes"]["items"][0]["spec"]["numberOfReplicas"])
        self.assertIn("Stopped after moving 0 copies", message)

    def test_only_the_leader_changes_anything(self):
        R.bind(self.c.get, self.c.send, lambda: False)
        state, _, _ = R.status(self.item)
        self.assertEqual("running", state)
        self.assertEqual([], self.c.sent)

    def test_a_new_copy_never_built_is_given_up_and_the_count_put_back(self):
        R.status(self.item)
        self.item["ref"]["started"] = time.time() - 700
        R.status(self.item)
        self.assertEqual(2, self.c.objects[f"{LH}/volumes"]["items"][0]["spec"]["numberOfReplicas"])
        self.assertIn("did not place", " ".join(self.item["ref"]["skipped"]))


if __name__ == "__main__":
    unittest.main()
