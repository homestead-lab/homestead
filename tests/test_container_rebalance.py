"""Moving containers so hosts carry similar CPU and memory - the fewest moves
worth a restart, each one vetoable, one at a time. On the shared fake API
(tests/fakes/kube.py)."""
import copy
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_container_rebalance as C
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_kube as K
import homestead_operations as OPS
from fakes.kube import Kube

GI = 1024 ** 3
METRICS = "/apis/metrics.k8s.io/v1beta1"
PDBS = "/apis/policy/v1/poddisruptionbudgets"
LONGHORN = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
APPS = {"frigate": ("k2", "1500m", "3Gi"), "ha": ("k2", "400m", "1Gi"), "plex": ("k2", "900m", "2Gi"), "tiny": ("k2", "10m", "50Mi")}
USAGE = {"k1": {"cpu": "400m", "memory": "3Gi"}, "k2": {"cpu": "3200m", "memory": "11Gi"}, "k3": {"cpu": "500m", "memory": "4Gi"}}


def node(name, cordoned=False, allocatable=None):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"allocatable": allocatable or {"cpu": "4", "memory": "16Gi"}, "conditions": [{"type": "Ready", "status": "True"}]}}


def deployment(name, replicas=1, pinned=None, host_path=False, claim=None):
    template = {"nodeSelector": {"kubernetes.io/hostname": pinned} if pinned else {},
                "volumes": ([{"name": "h", "hostPath": {"path": "/x"}}] if host_path else [])
                           + ([{"name": "d", "persistentVolumeClaim": {"claimName": claim}}] if claim else [])}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": name, "generation": 1},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}}, "template": {"spec": template}},
            "status": {"observedGeneration": 1, "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas}}


def pod(name, host, n=0):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"namespace": "lab", "name": f"{name}-abc-{n}", "labels": {"app": name},
                         "ownerReferences": [{"kind": "ReplicaSet", "name": f"{name}-abc"}]},
            "spec": {"nodeName": host}, "status": {"phase": "Running"}}


def replicaset(name):
    return {"apiVersion": "apps/v1", "kind": "ReplicaSet",
            "metadata": {"namespace": "lab", "name": f"{name}-abc", "ownerReferences": [{"kind": "Deployment", "name": name}]}}


def pod_metrics(name, cpu, memory):
    return {"apiVersion": "metrics.k8s.io/v1beta1", "kind": "PodMetrics", "metadata": {"namespace": "lab", "name": f"{name}-abc-0"},
            "containers": [{"usage": {"cpu": cpu, "memory": memory}}]}


def node_metrics(name, cpu, memory):
    return {"apiVersion": "metrics.k8s.io/v1beta1", "kind": "NodeMetrics", "metadata": {"name": name},
            "usage": {"cpu": cpu, "memory": memory}}


class Hosts:
    """k2 runs the busy apps; k1 and k3 are quiet. The objects sit on the
    shared fake API; the module under test is bound to the shared client
    and put back after (see bind below)."""

    def __init__(self, test, apps=APPS, allocatable=None, usage=USAGE):
        pinned = {"plex": "k2"}
        objects = [node(n, allocatable=(allocatable or {}).get(n)) for n in ("k1", "k2", "k3")]
        objects += [node_metrics(n, **usage[n]) for n in ("k1", "k2", "k3")]
        for name, (host, cpu, memory) in apps.items():
            objects += [deployment(name, pinned=pinned.get(name)), pod(name, host), replicaset(name), pod_metrics(name, cpu, memory)]
        self.kube = test.enterContext(Kube(*objects))
        # The shared fake runs the real write guards, and an app edited while it is
        # moved reaches one: a temporary DATA_DIR for the job store's lock.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
        saved = {name: getattr(C, name) for name in ("kget", "requirements", "satisfies", "apply", "own", "is_leader",
                                                      "summaries", "guard", "operations", "standby")}
        test.addCleanup(lambda: [setattr(C, name, value) for name, value in saved.items()])
        self.moved = []

    def apply(self, ns, name, to):
        self.moved.append((name, to))

    # What someone else does to the cluster meanwhile.
    def edit(self, path, change):
        """Change the object at path; change(obj) edits a copy."""
        obj = self.kube.object(path)
        change(obj)
        self.kube.add(obj)

    def edit_dep(self, name, change):
        self.edit(f"/apis/apps/v1/namespaces/lab/deployments/{name}", change)

    def each_dep(self, change):
        for item in K.kget("/apis/apps/v1/deployments")["items"]:
            self.edit_dep(item["metadata"]["name"], change)

    def edit_pod(self, app, change):
        self.edit(f"/api/v1/namespaces/lab/pods/{app}-abc-0", change)

    def edit_pod_metrics(self, app, change):
        self.edit(f"{METRICS}/namespaces/lab/pods/{app}-abc-0", change)

    def edit_node_metrics(self, name, change):
        self.edit(f"{METRICS}/nodes/{name}", change)

    def cordon(self, *names):
        for name in ("k1", "k2", "k3"):
            self.edit(f"/api/v1/nodes/{name}", lambda n: n["spec"].update(unschedulable=n["metadata"]["name"] in names))

    def add_app(self, name, host, cpu=None, memory=None, dep=None):
        self.kube.add(dep or deployment(name))
        self.kube.add(pod(name, host))
        self.kube.add(replicaset(name))
        if cpu:
            self.kube.add(pod_metrics(name, cpu, memory))

    def dep(self, name):
        return self.kube.object(f"/apis/apps/v1/namespaces/lab/deployments/{name}")


def bind(c, satisfies=lambda n, r: (True, [])):
    CAPACITY_REVIEW.bind(lambda: b"test-key")
    C.bind(K.kget, lambda dep: {}, satisfies, c.apply, ("lab", "homestead"), lambda: True)


class PlanTests(unittest.TestCase):
    def test_the_busiest_hosts_biggest_movable_app_goes_to_a_quiet_host(self):
        c = Hosts(self); bind(c)
        plan = C.plan()
        self.assertEqual("lab/frigate", plan["moves"][0]["id"])
        self.assertIn(plan["moves"][0]["to"], ("k1", "k3"))
        self.assertNotIn("lab/tiny", [m["id"] for m in plan["moves"]], "not worth a restart")
        k2 = next(h for h in plan["hosts"] if h["name"] == "k2")
        self.assertLess(max(k2["cpu_after"], k2["mem_after"]), max(k2["cpu_before"], k2["mem_before"]))

    def test_a_memory_gap_moves_though_another_host_leads_on_cpu(self):
        # #381: k1 (8 cores) 19% CPU 36% memory, k2 (4 cores) 37% CPU 15%,
        # k3 (8 cores) 4% CPU 34%. Only each host's higher share was compared,
        # 37/36/34, so no move could pass; the memory gap should close.
        c = Hosts(self, apps={"paperless": ("k1", "50m", "3Gi"), "web": ("k2", "1200m", "300Mi")},
                  allocatable={n: {"cpu": cores, "memory": "30Gi"} for n, cores in (("k1", "8"), ("k2", "4"), ("k3", "8"))},
                  usage={"k1": {"cpu": "1520m", "memory": "10800Mi"}, "k2": {"cpu": "1480m", "memory": "4500Mi"},
                         "k3": {"cpu": "320m", "memory": "10200Mi"}})
        bind(c)
        plan = C.plan()
        moved = {m["id"]: (m["from"], m["to"]) for m in plan["moves"]}
        self.assertEqual(("k1", "k2"), moved.get("lab/paperless"), plan)
        self.assertLess(plan["spread_after"], plan["spread_before"])

    def test_hosts_already_even_say_so(self):
        c = Hosts(self)
        for name in ("k1", "k2", "k3"):
            c.edit_node_metrics(name, lambda m: m.update(usage={"cpu": "1000m", "memory": "4Gi"}))
        bind(c)
        plan = C.plan()
        self.assertEqual([], plan["moves"])
        self.assertEqual("even", plan["quiet"])

    def test_a_pinned_app_stays_and_says_why(self):
        c = Hosts(self); bind(c)
        plan = C.plan()
        self.assertNotIn("lab/plex", [m["id"] for m in plan["moves"]])
        self.assertIn({"id": "lab/plex", "why": "it is pinned to its host"}, plan["skipped"])

    @staticmethod
    def place(c, name, kind, other, required=False):
        """Give an app a run-with (podAffinity) or keep-apart (podAntiAffinity) rule, as Edit › Where it runs does."""
        c.each_dep(lambda dep: dep["spec"]["template"].setdefault("metadata", {"labels": {"app": dep["metadata"]["name"]}}))
        term = {"labelSelector": {"matchLabels": {"app": other}}, "namespaces": ["lab"], "topologyKey": "kubernetes.io/hostname"}
        rule = {"requiredDuringSchedulingIgnoredDuringExecution": [term]} if required else \
            {"preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 100, "podAffinityTerm": term}]}
        c.edit_dep(name, lambda dep: dep["spec"]["template"]["spec"].update(affinity={kind: rule}))

    def test_an_app_that_runs_with_another_is_not_moved_away_from_it_alone(self):
        # Seen on a real cluster: Home Assistant preferred to run with
        # Mosquitto; Balance moved it alone and it came back beside Mosquitto.
        c = Hosts(self); self.place(c, "frigate", "podAffinity", "ha"); bind(c)
        plan = C.plan()
        moved = [m["id"] for m in plan["moves"]]
        self.assertNotIn("lab/frigate", moved)
        self.assertNotIn("lab/ha", moved, "nor the one it runs with")
        why = {s["id"]: s["why"] for s in plan["skipped"]}
        self.assertIn("it runs with lab/ha (its placement)", why["lab/frigate"])
        self.assertIn("lab/frigate runs with it (their placement)", why["lab/ha"])

    def test_nothing_goes_to_a_host_it_is_kept_apart_from(self):
        c = Hosts(self)
        c.add_app("dns", "k1")
        self.place(c, "frigate", "podAntiAffinity", "dns"); bind(c)
        frigate = next(m for m in C.plan()["moves"] if m["id"] == "lab/frigate")
        self.assertEqual("k3", frigate["to"], "not k1, where what it is kept apart from runs")

    def coral_only_on_k3(self, c):
        """Frigate needs hardware only k3 has besides its own host; the rest go anywhere."""
        C.bind(K.kget, lambda dep: {"app": dep["metadata"]["name"]},
               lambda n, r: (r.get("app") != "frigate" or n["metadata"]["name"] == "k3", ["no Coral"]),
               c.apply, ("lab", "homestead"), lambda: True)
        CAPACITY_REVIEW.bind(lambda: b"test-key")

    def add(self, c, name, host, cpu="100m", memory="512Mi"):
        c.add_app(name, host, cpu, memory)

    def test_what_keeps_an_app_off_its_only_other_host_moves_first(self):
        # Seen on a real cluster: Plex kept apart from Frigate, on the one
        # other host with a Coral, so Frigate never moved off the busy one.
        c = Hosts(self); self.add(c, "dns", "k3")
        self.place(c, "dns", "podAntiAffinity", "frigate"); self.coral_only_on_k3(c)
        plan = C.plan()
        order = [(m["id"], m["to"]) for m in plan["moves"]]
        self.assertIn(("lab/frigate", "k3"), order, plan)
        dns = next(m for m in plan["moves"] if m["id"] == "lab/dns")
        self.assertEqual("k1", dns["to"])
        self.assertLess(order.index(("lab/dns", "k1")), order.index(("lab/frigate", "k3")), "the one in the way first")
        self.assertIn("makes room on k3 for lab/frigate", dns["note"])
        self.assertIn("after lab/dns leaves k3", next(m for m in plan["moves"] if m["id"] == "lab/frigate")["note"])

    def test_a_chain_is_not_made_when_the_one_in_the_way_has_nowhere_to_go(self):
        c = Hosts(self); self.add(c, "dns", "k3")
        c.kube.add(deployment("dns", pinned="k3"))
        self.place(c, "dns", "podAntiAffinity", "frigate"); self.coral_only_on_k3(c)
        plan = C.plan()
        self.assertNotIn("lab/frigate", [m["id"] for m in plan["moves"]])
        why = {s["id"]: s["why"] for s in plan["skipped"]}
        self.assertIn("k3: kept apart from lab/dns there", why["lab/frigate"])
        self.assertIn("k1: no Coral", why["lab/frigate"])

    def test_every_app_that_could_move_but_does_not_says_why(self):
        c = Hosts(self); bind(c, satisfies=lambda n, r: (False, ["no iGPU for /dev/dri"]))
        plan = C.plan()
        why = {s["id"]: s["why"] for s in plan["skipped"]}
        self.assertIn("no other host it may run on (k1: no iGPU for /dev/dri; k3: no iGPU for /dev/dri)", why["lab/frigate"])
        self.assertIn("lab/ha", why)

    def test_small_apps_are_listed_together_and_near_misses_give_the_numbers(self):
        c = Hosts(self); bind(c)
        plan = C.plan(["lab/frigate", "lab/ha"])
        why = {s["id"]: s["why"] for s in plan["skipped"]}
        self.assertIn("lab/tiny", why["1 small app"])
        self.assertNotIn("lab/tiny", why)
        self.assertNotIn("lab/frigate", why, "a vetoed app is the person's choice, not a reason")

    def locality(self, mode, copies):
        c = Hosts(self)
        c.kube.add(deployment("frigate", claim="frigate-data"))
        c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": "pvc-f"},
                    "spec": {"dataLocality": mode},
                    "status": {"kubernetesStatus": {"namespace": "lab", "pvcName": "frigate-data"}}})
        for host in copies:
            c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Replica",
                        "metadata": {"namespace": "longhorn-system", "name": f"pvc-f-r-{host}"},
                        "spec": {"volumeName": "pvc-f", "nodeID": host}})
        bind(c)
        return C.plan()

    def test_strict_local_data_keeps_its_app_on_its_host(self):
        plan = self.locality("strict-local", ["k2"])
        self.assertNotIn("lab/frigate", [m["id"] for m in plan["moves"]])
        self.assertIn("strict-local", {s["id"]: s["why"] for s in plan["skipped"]}["lab/frigate"])

    def test_an_app_reading_its_data_locally_moves_only_where_a_copy_is(self):
        for _ in range(5):      # whichever quiet host would score best, never the one without a copy
            frigate = next(m for m in self.locality("disabled", ["k2", "k3"])["moves"] if m["id"] == "lab/frigate")
            self.assertEqual(("k3", True), (frigate["to"], frigate["near"]))

    def test_data_that_follows_its_app_moves_only_where_a_copy_already_is(self):
        frigate = next(m for m in self.locality("best-effort", ["k1"])["moves"] if m["id"] == "lab/frigate")
        self.assertEqual("k1", frigate["to"], "best-effort would build a whole new copy anywhere else")

    def test_data_read_over_the_network_anyway_still_lets_its_app_move_anywhere(self):
        moves = self.locality("disabled", ["k1"])["moves"]
        self.assertIn("lab/frigate", [m["id"] for m in moves])

    def test_a_vetoed_app_stays_and_the_plan_works_round_it(self):
        c = Hosts(self); bind(c)
        plan = C.plan(["lab/frigate"])
        self.assertNotIn("lab/frigate", [m["id"] for m in plan["moves"]])
        self.assertIn("lab/frigate", plan["apps"])

    def test_only_hosts_with_the_hardware_it_needs(self):
        c = Hosts(self); bind(c, satisfies=lambda n, r: (n["metadata"]["name"] == "k3", []))
        self.assertTrue(all(m["to"] == "k3" for m in C.plan()["moves"]))

    def test_hosts_are_checked_on_homesteads_summary_not_the_kubernetes_node(self):
        # PLACE.satisfies reads status == "Ready"; a Kubernetes Node's status
        # is a dict, so given one every host was refused and nothing moved.
        import homestead_place as PLACE
        c = Hosts(self)
        summary = lambda: [{"name": n, "status": "Ready", "labels": {}, "allocatable": {}, "schedulable": True, "hardware": {}}
                           for n in ("k1", "k2", "k3")]
        C.bind(K.kget, lambda dep: {"devices": [], "features": [], "resources": {}, "labels": {}},
               PLACE.satisfies, c.apply, ("lab", "homestead"), lambda: True, summary)
        self.assertEqual("lab/frigate", C.plan()["moves"][0]["id"])

    def test_when_the_busiest_host_has_nothing_to_move_the_next_one_is_balanced(self):
        c = Hosts(self)
        for name in ("frigate", "ha", "tiny"):
            c.kube.add(deployment(name, pinned="k2"))
        c.add_app("busy", "k3", "1500m", "2Gi")
        c.edit_node_metrics("k3", lambda m: m.update(usage={"cpu": "2400m", "memory": "6Gi"}))
        bind(c)
        moves = C.plan()["moves"]
        self.assertEqual([("lab/busy", "k3", "k1")], [(m["id"], m["from"], m["to"]) for m in moves])

    def test_the_reviewed_moves_start_though_the_load_moved_since(self):
        c = Hosts(self); bind(c)
        plan = C.plan()
        # Live CPU moves by the second: the same moves are the review.
        c.edit_pod_metrics("frigate", lambda m: m["containers"][0]["usage"].update(cpu="1450m"))
        moves = C.reviewed(plan["moves"], [], plan["review_token"])
        self.assertEqual([(m["id"], m["from"], m["to"]) for m in plan["moves"]], [(m["id"], m["from"], m["to"]) for m in moves])

    def test_reviewed_moves_that_differ_or_no_longer_fit_need_a_new_review(self):
        c = Hosts(self); bind(c)
        plan = C.plan()
        forged = [dict(plan["moves"][0], to="k2")]
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            C.reviewed(forged, [], plan["review_token"])
        c.edit_pod("frigate", lambda p: p["spec"].update(nodeName="k3"))   # frigate moved meanwhile
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            C.reviewed(plan["moves"], [], plan["review_token"])
        c.edit_pod("frigate", lambda p: p["spec"].update(nodeName="k2"))
        target = plan["moves"][0]["to"]
        c.cordon(target)
        with self.assertRaisesRegex(ValueError, "cannot take"):
            C.reviewed(plan["moves"], [], plan["review_token"])

    def test_a_cordoned_host_takes_none(self):
        c = Hosts(self)
        c.cordon("k1")
        bind(c)
        self.assertTrue(all(m["to"] == "k3" for m in C.plan()["moves"]))


class SignedReviewTests(unittest.TestCase):
    def setUp(self):
        self.c = Hosts(self); bind(self.c)
        self.plan = C.plan()

    def test_the_token_is_signed_and_expires(self):
        token = self.plan["review_token"]
        self.assertRegex(token, r"^\d+\.[0-9a-f]{64}$")
        with mock.patch("time.time", return_value=time.time() + C.CAPACITY_REVIEW.TTL + 5):
            with self.assertRaisesRegex(ValueError, "review again"):
                C.reviewed(self.plan["moves"], [], token)

    def test_an_edit_to_an_app_after_the_review_needs_a_new_review(self):
        app = self.plan["moves"][0]["name"]
        self.c.edit_dep(app, lambda dep: dep["metadata"].update(generation=2))
        with self.assertRaisesRegex(ValueError, "review again"):
            C.reviewed(self.plan["moves"], [], self.plan["review_token"])

    def test_reviewed_moves_carry_the_generation_reviewed(self):
        moves = C.reviewed(self.plan["moves"], [], self.plan["review_token"])
        self.assertEqual(1, moves[0]["generation"])

    def test_the_job_skips_an_app_edited_after_the_review(self):
        moves = C.reviewed(self.plan["moves"], [], self.plan["review_token"])
        item = {"id": "op1", "ref": {"moves": moves, "index": 0, "moved": 0, "stage": "move", "excluded": [], "role": None}}
        self.c.edit_dep(moves[0]["name"], lambda dep: dep["metadata"].update(generation=2))
        state, _, message = C.status(item)
        self.assertEqual("running", state)
        self.assertIn("changed since the review", message)
        self.assertEqual([], self.c.moved)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.c = Hosts(self); bind(self.c)
        self.item = {"id": "job", "ref": {"moves": [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1"}],
                                          "index": 0, "moved": 0, "stage": "move"}}

    def test_moves_then_waits_until_it_runs_on_its_new_host(self):
        state, _, message = C.status(self.item)
        self.assertEqual([("frigate", "k1")], self.c.moved)
        self.assertIn("restarts there", message)
        self.assertEqual("running", C.status(self.item)[0], "still on k2")
        self.c.edit_pod("frigate", lambda p: p["spec"].update(nodeName="k1"))
        C.status(self.item)
        state, _, message = C.status(self.item)
        self.assertEqual("succeeded", state)
        self.assertIn("Moved 1 container", message)

    def test_its_image_is_pulled_onto_the_new_host_before_it_moves(self):
        # A host already holding the image scored over the preferred one:
        # Frigate restarted where it was (#880).
        import homestead_host_move as HOST_MOVE
        pulled = [False]
        with mock.patch.object(C, "_pull_first", return_value=(["pull-1"], ["example/frigate:stable"])), \
                mock.patch.object(HOST_MOVE, "_pulled", side_effect=lambda names: pulled[0]), \
                mock.patch.object(HOST_MOVE, "_clear") as clear:
            message = C.status(self.item)[2]
            self.assertIn("Pulling example/frigate:stable onto k1 first", message)
            self.assertEqual([], self.c.moved, "not moved before its image is there")
            C.status(self.item)
            self.assertEqual([], self.c.moved)
            pulled[0] = True
            self.assertIn("Image on k1", C.status(self.item)[2])
            clear.assert_called_once_with(["pull-1"])
            C.status(self.item)
        self.assertEqual([("frigate", "k1")], self.c.moved)

    def test_cancel_while_pulling_stops_the_pulls(self):
        """The pulls are cleared when their stage ends; a cancel ends it too,
        or the pre-pull DaemonSets would pull on for ever."""
        import homestead_host_move as HOST_MOVE
        for stop_on_failure in (False, True):
            with self.subTest(stop_on_failure=stop_on_failure):
                item = copy.deepcopy(self.item)
                item["ref"]["stop_on_failure"] = stop_on_failure
                with mock.patch.object(C, "_pull_first", return_value=(["pull-1"], ["example/frigate:stable"])), \
                        mock.patch.object(HOST_MOVE, "_pulled", return_value=False), \
                        mock.patch.object(HOST_MOVE, "_clear") as clear:
                    C.status(item)
                    self.assertEqual("pulling", item["ref"]["stage"])
                    C.cancel_run(item, {})
                    clear.assert_called_once_with(["pull-1"])
                self.assertNotIn("pulls", item["ref"])
                self.assertEqual([], self.c.moved)

    def test_its_pulls_are_its_own_so_another_job_pulling_the_same_image_is_not_stopped(self):
        import homestead_host_move as HOST_MOVE
        import homestead_image_cache as IMAGES
        with mock.patch.object(HOST_MOVE, "missing", return_value=["example/frigate:stable"]), \
                mock.patch.object(IMAGES, "prepull", return_value={"daemonset": "homestead-pull-frigate-0a1b2c3d"}) as pull:
            self.assertEqual((["homestead-pull-frigate-0a1b2c3d"], ["example/frigate:stable"]), C._pull_first({"x": 1}, "k1"))
        pull.assert_called_once_with("example/frigate:stable", ["k1"], own=True)

    def test_one_that_restarts_on_its_old_host_says_so(self):
        C.status(self.item)
        self.item["ref"]["started"] -= 700
        state, _, message = C.status(self.item)
        self.assertEqual("failed", state)
        self.assertIn("lab/frigate restarted on k2, not k1", message)

    def test_a_null_budget_list_moves_nothing(self):
        """Budgets are checked again before each move: an answer of null is
        not "no budgets"."""
        real = C.kget
        with mock.patch.object(C, "kget", lambda path: None if path == PDBS else real(path)):   # the API's answer is null
            with self.assertRaises(AttributeError):
                C.status(self.item)
        self.assertEqual([], self.c.moved)

    def test_one_that_does_not_start_stops_the_rest(self):
        C.status(self.item)
        self.item["ref"]["started"] = time.time() - 700
        state, _, message = C.status(self.item)
        self.assertEqual("failed", state)
        self.assertIn("the rest were not moved", message)

    def test_one_moved_since_the_review_is_skipped(self):
        self.c.edit_pod("frigate", lambda p: p["spec"].update(nodeName="k3"))
        C.status(self.item)
        self.assertEqual([], self.c.moved)
        self.assertIn("moved or changed", " ".join(self.item["ref"]["skipped"]))

    def test_a_blocked_capacity_check_skips_it(self):
        def blocked(ns, name, to):
            raise ValueError("k1 has no room for it")
        C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), blocked, ("lab", "homestead"), lambda: True)
        C.status(self.item)
        self.assertIn("no room", " ".join(self.item["ref"]["skipped"]))

    def test_only_the_leader_moves_anything(self):
        C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), self.c.apply, ("lab", "homestead"), lambda: False)
        C.status(self.item)
        self.assertEqual([], self.c.moved)


class RoleTests(unittest.TestCase):
    """An operator may not move an app only an admin may change; the job,
    which runs with no request behind it, asks again for whoever started it."""

    def setUp(self):
        self.c = Hosts(self)
        self.asked = []

        def guard(dep, ns, role):
            self.asked.append((dep["metadata"]["name"], role))
            if role == "operator" and dep["metadata"]["name"] == "frigate":
                raise PermissionError("only an admin can operate management workloads")
        C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), self.c.apply, ("lab", "homestead"), lambda: True,
               None, guard)
        self.item = {"id": "job", "ref": {"moves": [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1"}],
                                          "index": 0, "moved": 0, "stage": "move"}}

    def test_an_operators_plan_leaves_out_what_only_an_admin_may_change(self):
        found = C.plan(role="operator")
        self.assertNotIn("lab/frigate", [m["id"] for m in found["moves"]])
        self.assertIn({"id": "lab/frigate", "why": C.ADMIN_ONLY}, found["skipped"])
        self.assertIn("lab/frigate", [m["id"] for m in C.plan(role="admin")["moves"]])

    def test_a_plan_with_no_role_asks_no_one(self):
        C.plan()
        self.assertEqual([], self.asked)

    def test_a_move_an_operator_was_never_offered_is_refused(self):
        moves = [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1"}]
        with self.assertRaisesRegex(PermissionError, "only an admin"):
            C.reviewed(moves, [], C.review_token(moves, []), role="operator")
        self.assertEqual("lab/frigate", C.reviewed(moves, [], C.review_token(moves, []), role="admin")[0]["id"])

    def test_the_job_asks_again_for_whoever_started_it(self):
        self.item["ref"]["role"] = "operator"
        C.status(self.item)
        self.assertEqual([], self.c.moved)
        self.assertIn(f"lab/frigate: {C.ADMIN_ONLY}", self.item["ref"]["skipped"])

    def test_the_review_route_plans_for_whoever_asks(self):
        import homestead_routes as ROUTES
        _, handler = C.ROUTES[("GET", "/api/workloads/rebalance/plan")]
        found = handler(ROUTES.Request("GET", "/api/workloads/rebalance/plan", {}, None, "op", "operator"))
        self.assertIn({"id": "lab/frigate", "why": C.ADMIN_ONLY}, found["skipped"])

    def test_a_job_with_no_role_moves_as_before(self):
        C.status(self.item)
        self.assertEqual([("frigate", "k1")], self.c.moved)

    def test_an_admin_run_reads_nothing_more_before_each_move(self):
        reads = []
        real = C.kget
        C.kget = lambda path: reads.append(path) or real(path)
        self.item["ref"]["role"] = "admin"
        C.status(self.item)
        self.assertEqual([("frigate", "k1")], self.c.moved)
        # Once, to see where it runs - not again for a check an admin skips.
        self.assertEqual(1, reads.count("/apis/apps/v1/namespaces/lab/deployments/frigate"))

    def test_each_move_is_written_as_whoever_started_the_job(self):
        import homestead_host_access as HOSTACCESS
        seen = []
        C.apply = lambda ns, name, to: seen.append(HOSTACCESS.role())
        self.item["ref"]["moves"][0].update(name="ha", id="lab/ha")
        self.item["ref"]["role"] = "operator"
        C.status(self.item)
        self.assertEqual(["operator"], seen)
        self.assertIsNone(HOSTACCESS.role())

    def test_a_write_the_fence_refuses_is_skipped_and_the_job_goes_on(self):
        def refuse(ns, name, to):
            raise PermissionError("only an admin can operate management workloads")
        C.apply = refuse
        self.item["ref"]["moves"][0].update(name="ha", id="lab/ha")
        self.item["ref"]["role"] = "operator"
        state, _, message = C.status(self.item)
        self.assertEqual(("running", f"Skipped lab/ha: {C.ADMIN_ONLY}"), (state, message))
        self.assertEqual(1, self.item["ref"]["index"])

    def test_an_operators_forged_move_list_is_refused_with_403(self):
        import homestead_routes as ROUTES
        moves = [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1"}]
        body = {"moves": moves, "exclude": [], "review_token": C.review_token(moves, []), "restart": True}
        with mock.patch.object(C.OPS, "list_operations", return_value=[]), \
                mock.patch.object(C.OPS, "start") as start:
            reply = C.start_run(ROUTES.Request("POST", "/api/workloads/rebalance", {}, body, "op", "operator"))
        self.assertEqual(403, reply.code)
        self.assertIn(C.ADMIN_ONLY, reply.body["error"])
        start.assert_not_called()


class RealGuardTests(unittest.TestCase):
    """The rebalance through homestead_host_access itself, not a stand-in:
    an app with its own service account is an admin's to move."""

    def setUp(self):
        import homestead_host_access as HOSTACCESS
        self.c = Hosts(self)
        self.c.edit_dep("frigate", lambda dep: dep["spec"]["template"]["spec"].update(serviceAccountName="frigate-sa"))
        C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), self.c.apply, ("lab", "homestead"), lambda: True,
               None, HOSTACCESS.require_target_as)

    def test_an_operator_is_not_offered_an_app_with_its_own_service_account(self):
        self.assertIn({"id": "lab/frigate", "why": C.ADMIN_ONLY}, C.plan(role="operator")["skipped"])
        self.assertIn("lab/frigate", [m["id"] for m in C.plan(role="admin")["moves"]])

    def test_an_app_given_a_service_account_since_the_review_is_skipped(self):
        item = {"id": "job", "ref": {"moves": [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1"}],
                                     "index": 0, "moved": 0, "stage": "move", "role": "operator"}}
        C.status(item)
        self.assertEqual([], self.c.moved)
        self.assertIn(f"lab/frigate: {C.ADMIN_ONLY}", item["ref"]["skipped"])


class StartTests(unittest.TestCase):
    def test_the_job_keeps_who_started_it_and_homesteads_own_run_has_no_role(self):
        started = []
        jobs = mock.Mock(start=lambda *a, **k: started.append(a) or {"id": "op"})
        plan = {"moves": [{"ns": "lab", "name": "web", "id": "lab/web", "from": "k2", "to": "k1"}], "excluded": []}
        with mock.patch.object(C, "operations", jobs):
            C.start(plan, role="operator")
            C.start(plan, " after k2 came back")
        self.assertEqual(["operator", None], [a[4]["role"] for a in started])
        self.assertEqual("Rebalance 1 container after k2 came back", started[1][1])


if __name__ == "__main__":
    unittest.main()


class HostPathTests(unittest.TestCase):
    def test_devices_are_hardware_not_host_storage(self):
        for path, storage in (("/dev/dri", False), ("/dev/bus/usb", False), ("/dev/net/tun", False),
                              ("/mnt/user/media", True), ("/srv/data", True), ("/devices", True)):
            self.assertEqual(storage, C._host_storage({"hostPath": {"path": path}}), path)


def requests(dep, cpu="500m", memory="1Gi"):
    dep["spec"]["template"]["spec"]["containers"] = [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": memory}}}]
    return dep


class PickedTests(unittest.TestCase):
    def setUp(self):
        self.c = Hosts(self)
        for name in ("frigate", "ha", "tiny"):
            self.c.edit_dep(name, requests)
        bind(self.c)

    def why(self, picks, app):
        return next(r["why"] for r in C.picked(picks)["rows"] if r["id"] == app)

    def test_chosen_moves_go_where_they_are_sent(self):
        plan = C.picked([{"id": "lab/ha", "to": "k1"}, {"id": "lab/tiny", "to": "k3"}])
        self.assertEqual([("lab/ha", "k2", "k1"), ("lab/tiny", "k2", "k3")],
                         [(m["id"], m["from"], m["to"]) for m in plan["moves"]])
        self.assertFalse(plan["blocked"])
        self.assertRegex(plan["review_token"], r"^\d+\.")

    def test_a_pinned_app_is_a_row_with_its_reason_not_a_failure(self):
        plan = C.picked([{"id": "lab/plex", "to": "k1"}, {"id": "lab/ha", "to": "k1"}])
        self.assertEqual(["lab/ha"], [m["id"] for m in plan["moves"]])
        self.assertIn("pinned", self.why([{"id": "lab/plex", "to": "k1"}], "lab/plex"))

    def test_each_new_reason(self):
        self.c.edit_dep("ha", lambda dep: dep["spec"].update(paused=True))
        self.assertIn("paused", self.why([{"id": "lab/ha", "to": "k1"}], "lab/ha"))
        self.c.edit_dep("ha", lambda dep: dep["spec"].update(paused=False))
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"]["spec"].update(nodeName="k2"))
        self.assertIn("nodeName", self.why([{"id": "lab/ha", "to": "k1"}], "lab/ha"))
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"]["spec"].pop("nodeName"))
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"]["spec"].update(topologySpreadConstraints=[{"maxSkew": 1}]))
        self.assertIn("spread", self.why([{"id": "lab/ha", "to": "k1"}], "lab/ha"))
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"]["spec"].pop("topologySpreadConstraints"))
        self.assertIn("already runs on k2", self.why([{"id": "lab/ha", "to": "k2"}], "lab/ha"))
        bind(self.c, satisfies=lambda n, r: (False, ["no GPU"]))
        self.assertIn("no GPU", self.why([{"id": "lab/ha", "to": "k1"}], "lab/ha"))

    def test_a_cordoned_destination_takes_none(self):
        self.c.cordon("k1")
        bind(self.c)
        self.assertIn("takes no new containers", self.why([{"id": "lab/ha", "to": "k1"}], "lab/ha"))

    def test_a_budget_allowing_no_restart_blocks_a_pick_and_balance(self):
        self.c.kube.add({
            "apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
            "metadata": {"namespace": "lab", "name": "ha-pdb"}, "spec": {"selector": {"matchLabels": {"app": "ha"}}},
            "status": {"disruptionsAllowed": 0}})
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"].update(metadata={"labels": {"app": "ha"}}))
        self.assertIn("disruption budget lab/ha-pdb", self.why([{"id": "lab/ha", "to": "k1"}], "lab/ha"))
        self.assertIn("lab/ha", [s["id"] for s in C.plan()["skipped"]])

    def test_two_picks_that_fit_apart_but_not_together(self):
        for name in ("frigate", "ha"):
            self.c.edit_dep(name, lambda dep: requests(dep, cpu="100m", memory="9Gi"))
        plan = C.picked([{"id": "lab/frigate", "to": "k1"}, {"id": "lab/ha", "to": "k1"}])
        self.assertEqual([], plan["moves"])
        self.assertTrue(all("no room on k1 with the others" in r["why"] for r in plan["rows"]))
        k1 = next(h for h in plan["hosts"] if h["name"] == "k1")
        self.assertTrue(k1["over"])
        self.assertEqual("", C.picked([{"id": "lab/frigate", "to": "k1"}])["rows"][0]["why"], "alone it fits")

    def test_picks_kept_apart_from_each_other_do_not_go_together(self):
        self.c.edit_dep("tiny", lambda dep: dep["spec"]["template"].update(metadata={"labels": {"app": "tiny"}}))
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"]["spec"].update(affinity={"podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname",
                                                                "labelSelector": {"matchLabels": {"app": "tiny"}}}]}}))
        plan = C.picked([{"id": "lab/tiny", "to": "k1"}, {"id": "lab/ha", "to": "k1"}])
        self.assertEqual(["lab/tiny"], [m["id"] for m in plan["moves"]])
        self.assertIn("kept apart", self.why([{"id": "lab/tiny", "to": "k1"}, {"id": "lab/ha", "to": "k1"}], "lab/ha"))

    def test_a_pick_twice_is_one_row(self):
        plan = C.picked([{"id": "lab/ha", "to": "k1"}, {"id": "lab/ha", "to": "k3"}])
        self.assertEqual(1, len(plan["rows"]))
        self.assertEqual("k1", plan["moves"][0]["to"])

    def test_a_missing_app_is_a_row_with_a_reason(self):
        plan = C.picked([{"id": "lab/gone", "to": "k1"}])
        self.assertTrue(plan["blocked"])
        self.assertIn("not running", plan["rows"][0]["why"])

    def test_more_than_ten_picks(self):
        for n in range(12):
            name = f"app{n}"
            self.c.add_app(name, "k2", dep=requests(deployment(name), cpu="10m", memory="10Mi"))
        plan = C.picked([{"id": f"lab/app{n}", "to": "k1"} for n in range(12)])
        self.assertEqual(10, len(plan["moves"]))
        self.assertEqual(2, sum("at most 10" in r["why"] for r in plan["rows"]))

    def test_apps_reserving_nothing_are_said_so(self):
        plan = C.picked([{"id": "lab/plex", "to": "k1"}, {"id": "lab/ha", "to": "k1"}])
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"]["spec"].pop("containers"))
        plan = C.picked([{"id": "lab/ha", "to": "k1"}])
        self.assertFalse(plan["rows"][0]["reserves"])

    def test_bad_input_is_refused(self):
        for bad in (None, [], [{"id": "lab/ha"}], ["lab/ha"]):
            with self.assertRaises(ValueError):
                C.picked(bad)

    def test_the_review_route_is_open_to_a_viewer(self):
        self.assertEqual("viewer", C.ROUTES[("POST", "/api/workloads/rebalance/review")][0])


class PickedJobTests(unittest.TestCase):
    def setUp(self):
        self.c = Hosts(self)
        for name in ("frigate", "ha", "tiny"):
            self.c.edit_dep(name, requests)
        bind(self.c)

    def started(self, picks):
        import homestead_routes as ROUTES
        plan = C.picked(picks)
        body = {"picks": picks, "review_token": plan["review_token"], "restart": True}
        # bind() passes no operations module, so the job's starter is faked here.
        start = mock.Mock(side_effect=lambda kind, title, res, href, ref, msg, unless=(): {"id": "op1", "ref": ref})
        with mock.patch.object(C.OPS, "list_operations", return_value=[]), \
                mock.patch.object(C, "operations", mock.Mock(start=start)):
            reply = C.start_run(ROUTES.Request("POST", "/api/workloads/rebalance", {}, body, "op", "admin"))
        return reply, start

    def test_picks_start_a_job_that_stops_on_failure(self):
        reply, start = self.started([{"id": "lab/ha", "to": "k1"}])
        self.assertEqual(202, reply.code)
        ref = start.call_args[0][4]
        self.assertTrue(ref["stop_on_failure"])
        self.assertEqual(["lab/ha"], [m["id"] for m in ref["moves"]])

    def test_a_stale_pick_review_is_refused_with_a_fresh_one(self):
        import homestead_routes as ROUTES
        body = {"picks": [{"id": "lab/ha", "to": "k1"}], "review_token": "1.00", "restart": True}
        with mock.patch.object(C.OPS, "list_operations", return_value=[]):
            reply = C.start_run(ROUTES.Request("POST", "/api/workloads/rebalance", {}, body, "op", "admin"))
        self.assertEqual(409, reply.code)
        self.assertIn("rows", reply.body["plan"])

    def item(self, picks):
        _, start = self.started(picks)
        return {"id": "op1", "ref": start.call_args[0][4]}

    def test_a_change_before_a_move_fails_the_job_and_leaves_the_rest(self):
        item = self.item([{"id": "lab/ha", "to": "k1"}, {"id": "lab/tiny", "to": "k3"}])
        self.c.edit_dep("ha", lambda dep: dep["metadata"].update(generation=2))
        state, _, message = C.status(item)
        self.assertEqual("failed", state)
        self.assertEqual(["failed", "not reached"], [r["result"] for r in item["ref"]["results"]])
        self.assertIn("lab/ha", message)
        self.assertIn("not reached", message)
        self.assertEqual([], self.c.moved)

    def test_balance_still_skips_and_carries_on(self):
        moves = C.reviewed(C.plan()["moves"], [], C.plan()["review_token"])
        item = {"id": "op2", "ref": {"moves": moves, "index": 0, "moved": 0, "stage": "move", "excluded": [], "role": None}}
        self.c.edit_dep(moves[0]["name"], lambda dep: dep["metadata"].update(generation=2))
        self.assertEqual("running", C.status(item)[0])
        self.assertEqual("skipped", item["ref"]["results"][0]["result"])

    def test_results_are_in_the_final_message(self):
        item = self.item([{"id": "lab/ha", "to": "k1"}])
        self.assertEqual("running", C.status(item)[0])            # moved, now starting
        self.c.edit_pod("ha", lambda p: p["spec"].update(nodeName="k1"))   # ha runs on k1 now
        C.status(item)
        state, _, message = C.status(item)
        self.assertEqual("succeeded", state)
        self.assertIn("Moved lab/ha to k1", message)
        self.assertEqual("moved", item["ref"]["results"][0]["result"])


class ReviewFixTests(PickedJobTests):
    """The final review's findings (#366)."""
    def test_a_budget_tightened_after_the_start_stops_a_chosen_move(self):
        item = self.item([{"id": "lab/ha", "to": "k1"}])
        self.c.edit_dep("ha", lambda dep: dep["spec"]["template"].update(metadata={"labels": {"app": "ha"}}))
        self.c.kube.add({
            "apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
            "metadata": {"namespace": "lab", "name": "ha-pdb"}, "spec": {"selector": {"matchLabels": {"app": "ha"}}},
            "status": {"disruptionsAllowed": 0}})
        state, _, message = C.status(item)
        self.assertEqual("failed", state)
        self.assertIn("disruption budget", message)
        self.assertEqual([], self.c.moved)

    def test_an_empty_selector_budget_covers_its_whole_namespace(self):
        budgets = [{"metadata": {"namespace": "lab", "name": "all"}, "spec": {"selector": {}}, "status": {"disruptionsAllowed": 0}}]
        self.assertIn("lab/all", C._budget_why("lab", {"app": "ha"}, budgets))
        self.assertEqual("", C._budget_why("media", {"app": "ha"}, budgets))

    def test_the_running_message_says_what_moved_so_far(self):
        item = self.item([{"id": "lab/ha", "to": "k1"}, {"id": "lab/tiny", "to": "k3"}])
        C.status(item)                                         # ha: moving
        self.c.edit_pod("ha", lambda p: p["spec"].update(nodeName="k1"))
        C.status(item)                                         # ha runs on k1
        _, _, message = C.status(item)                         # tiny: moving
        self.assertIn("2 of 2", message)
        self.assertIn("moved lab/ha", message)
        self.assertIn("moving lab/tiny", message)

    def test_cancel_records_the_rest_as_not_reached(self):
        item = self.item([{"id": "lab/ha", "to": "k1"}, {"id": "lab/tiny", "to": "k3"}])
        message = C.cancel_run(item, {})
        self.assertEqual(["not reached", "not reached"], [r["result"] for r in item["ref"]["results"]])
        self.assertIn("lab/ha not reached", message)
