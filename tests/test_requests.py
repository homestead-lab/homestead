"""Setting reservations from use for many apps: what is offered, what is left
out and why, the review, and the job that applies it one app at a time."""
import copy
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_requests as reserve
from rebind import keep_bound
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import no_cluster


def setUpModule():
    # No cluster here: a read on the way to what a test checks fails at once, as on CI,
    # instead of looking the API server's name up (tests/fakes/kube.py no_cluster).
    unittest.enterModuleContext(no_cluster())


def deployment(name, ns="lab", containers=(("app", {}),), labels=None, generation=1, settled=True):
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {}, "generation": generation},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": c, "resources": {"requests": dict(r)} if r else {}} for c, r in containers]}}},
            "status": {"observedGeneration": generation if settled else generation - 1,
                       "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1 if settled else 0}}


# What each container's week of use suggests: None when it has too few readings.
SUGGEST = {("lab", "paperless", "app"): ("60m", "480Mi"), ("lab", "frigate", "app"): ("180m", "528Mi"),
           ("lab", "web", "app"): ("20m", "64Mi"), ("lab", "web", "sidecar"): ("10m", "32Mi"),
           ("media", "jellyfin", "app"): ("240m", "1104Mi"), ("homestead", "homestead", "app"): ("100m", "256Mi"),
           ("lab", "unifi", "app"): None}


def usage(ns, name, container, requests=None, limits=None, tier=""):
    found = SUGGEST.get((ns, name, container))
    if not found:
        return {"samples": 96, "why": "A suggestion needs a day of readings; 96 of 288 so far."}
    cpu, memory = found
    return {"days": 7, "samples": 2016, "cpu_p95_m": 40, "mem_p95_mi": 380, "mem_max_mi": 500,
            "suggest": {"cpu": cpu, "memory": memory, "capped": [],
                        "differs": (requests or {}).get("cpu") != cpu or (requests or {}).get("memory") != memory}}


def managed(dep):
    if (dep["metadata"].get("labels") or {}).get("kustomize.toolkit.fluxcd.io/name"):
        return {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}
    return {}


class RequestsTests(unittest.TestCase):
    def setUp(self):
        self.deps = {
            "lab/paperless": deployment("paperless"),
            "lab/frigate": deployment("frigate", containers=(("app", {"cpu": "50m", "memory": "128Mi"}),)),
            "lab/web": deployment("web", containers=(("app", {}), ("sidecar", {}))),
            "lab/settled": deployment("settled", containers=(("app", {"cpu": "20m", "memory": "64Mi"}),)),
            "lab/unifi": deployment("unifi"),
            "media/jellyfin": deployment("jellyfin", ns="media", labels={"kustomize.toolkit.fluxcd.io/name": "apps"}),
            "homestead/homestead": deployment("homestead", ns="homestead"),
            "kube-system/coredns": deployment("coredns", ns="kube-system"),
        }
        SUGGEST[("lab", "settled", "app")] = ("20m", "64Mi")
        self.sent, self.node_memory = [], "16Gi"
        keep_bound(self, reserve)   # put back what reserve.bind() sets
        reserve.bind(self.kget, lambda *a, **k: self.sent.append((a, k)), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)

    def kget(self, path):
        if path == "/apis/apps/v1/deployments":
            return {"items": [copy.deepcopy(d) for d in self.deps.values()]}
        if path == "/api/v1/nodes":
            return {"items": [{"metadata": {"name": "n1"}, "spec": {},
                               "status": {"allocatable": {"cpu": "4", "memory": self.node_memory},
                                          "conditions": [{"type": "Ready", "status": "True"}]}}]}
        if path == "/api/v1/pods":
            return {"items": []}
        ns, name = path.split("/namespaces/")[1].split("/deployments/")
        if f"{ns}/{name}" not in self.deps:
            raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())
        return copy.deepcopy(self.deps[f"{ns}/{name}"])

    def test_apps_reserving_nothing_are_ticked_and_others_are_offered_unticked(self):
        plan = reserve.plan()
        chosen = {a["id"]: a["chosen"] for a in plan["apps"]}
        self.assertEqual({"lab/paperless": True, "lab/web": True, "lab/frigate": False}, chosen)
        self.assertEqual(["lab/paperless", "lab/web", "lab/frigate"], [a["id"] for a in plan["apps"]])

    def test_an_app_whose_reservation_matches_its_use_is_not_offered(self):
        self.assertNotIn("lab/settled", {a["id"] for a in reserve.plan()["apps"]})

    def test_a_flux_app_gets_a_patch_to_commit_instead(self):
        skipped = {s["id"]: s for s in reserve.plan()["skipped"]}
        self.assertIn("Flux applies it", skipped["media/jellyfin"]["why"])
        patch = skipped["media/jellyfin"]["yaml"]
        self.assertTrue(patch.startswith("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: jellyfin\n  namespace: media"))
        self.assertIn("      - name: app\n        resources:\n          requests:\n            cpu: 240m\n            memory: 1104Mi", patch)

    def test_an_app_an_admin_marked_as_updated_elsewhere_is_not_said_to_sync(self):
        with mock.patch.object(reserve, "managed_by", lambda dep: {"by": "Renovate", "source": "", "detected": False}
                               if dep["metadata"]["name"] == "paperless" else {}):
            why = {s["id"]: s for s in reserve.plan()["skipped"]}["lab/paperless"]["why"]
        self.assertNotIn("sync", why)
        self.assertIn("Renovate", why)

    def test_an_operator_is_not_offered_apps_only_an_admin_may_change(self):
        def guard(dep, ns, role):
            if role == "operator" and dep["metadata"]["name"] == "web":
                raise PermissionError("only an admin can operate management, system, service-account or host-privileged workloads")
        with mock.patch.object(reserve, "guard", guard):
            plan = reserve.plan(None, "operator")
            self.assertNotIn("lab/web", {a["id"] for a in plan["apps"]})
            self.assertIn("only an admin", {s["id"]: s for s in plan["skipped"]}["lab/web"]["why"])
            self.assertIn("lab/web", {a["id"] for a in reserve.plan(None, "admin")["apps"]})
            item = self.job(["lab/web"])
            item["ref"]["role"] = "operator"
            self.assertIn("Skipped lab/web: only an admin", reserve.status(item)[2])
        self.assertEqual([], self.sent)

    def test_the_default_never_ticks_more_than_one_run_may_change(self):
        with mock.patch.object(reserve, "MAX_APPS", 1):
            plan = reserve.plan()
        self.assertEqual(1, sum(a["chosen"] for a in plan["apps"]))

    def test_homestead_itself_and_system_namespaces_are_not_changed(self):
        plan = reserve.plan()
        ids = {a["id"] for a in plan["apps"]}
        self.assertFalse({"homestead/homestead", "kube-system/coredns"} & ids)
        self.assertIn("its manifest", {s["id"]: s for s in plan["skipped"]}["homestead/homestead"]["why"])

    def test_an_app_with_too_few_readings_says_why(self):
        self.assertIn("96 of 288", {s["id"]: s for s in reserve.plan()["skipped"]}["lab/unifi"]["why"])

    def test_the_review_token_covers_exactly_the_ticked_apps(self):
        default = reserve.plan()
        more = reserve.plan(["lab/paperless", "lab/web", "lab/frigate"])
        self.assertNotEqual(default["review_token"], more["review_token"])
        chosen = [a for a in more["apps"] if a["chosen"]]
        self.assertEqual(3, len(reserve.reviewed(chosen, more["review_token"])))
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            reserve.reviewed(chosen, default["review_token"])

    def test_starting_needs_the_restarts_confirmed_and_no_other_run(self):
        started = []
        jobs = mock.Mock(TERMINAL={"succeeded", "failed", "cancelled"}, snapshot=lambda: list(self.running),
                         start=lambda *a: started.append(a) or {"id": "op1"})
        self.running = []
        plan = reserve.plan()
        apps = [{"id": a["id"], "changes": a["changes"]} for a in plan["apps"] if a["chosen"]]
        body = {"apps": apps, "review_token": plan["review_token"], "restart": True}
        with mock.patch.object(reserve, "operations", jobs):
            with self.assertRaisesRegex(ValueError, "confirm the restarts"):
                reserve.start({**body, "restart": False}, "operator")
            self.running = [{"kind": reserve.KIND, "status": "running"}]
            with self.assertRaisesRegex(ValueError, "already being set"):
                reserve.start(body, "operator")
            self.running = []
            self.assertEqual({"operation": {"id": "op1"}}, reserve.start(body, "operator"))
        self.assertEqual("operator", started[0][4]["role"])
        self.assertEqual(["lab/paperless", "lab/web"], [a["id"] for a in started[0][4]["apps"]])

    def test_the_routes_are_declared_with_their_roles_and_plan_for_whoever_asks(self):
        import homestead_routes as ROUTES
        self.assertEqual("viewer", reserve.ROUTES[("GET", "/api/workloads/requests/plan")][0])
        self.assertEqual("operator", reserve.ROUTES[("POST", "/api/workloads/requests")][0])
        handler = reserve.ROUTES[("GET", "/api/workloads/requests/plan")][1]
        with mock.patch.object(reserve, "plan", side_effect=lambda chosen, role: (chosen, role)):
            asked = handler(ROUTES.Request("GET", "/api/workloads/requests/plan", {"chosen": ["lab/web,lab/frigate"]},
                                           None, "op", "operator"))
        self.assertEqual((["lab/web", "lab/frigate"], "operator"), asked)

    def test_a_reservation_set_by_hand_since_the_review_is_not_overwritten(self):
        plan = reserve.plan()
        self.deps["lab/paperless"] = deployment("paperless", containers=(("app", {"memory": "1Gi"}),))
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            reserve.reviewed([a for a in plan["apps"] if a["chosen"]], plan["review_token"])

    def test_an_app_handed_to_flux_since_the_review_is_refused(self):
        plan = reserve.plan()
        self.deps["lab/web"]["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps"}
        with self.assertRaisesRegex(ValueError, "not one Homestead changes"):
            reserve.reviewed([a for a in plan["apps"] if a["chosen"]], plan["review_token"])

    def job(self, ids):
        plan = reserve.plan(ids)
        apps = reserve.reviewed([a for a in plan["apps"] if a["chosen"]], plan["review_token"])
        return {"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}}

    def test_each_app_restarts_once_with_all_its_containers_and_the_next_waits(self):
        item = self.job(["lab/paperless", "lab/web"])
        self.assertEqual(["lab/paperless", "lab/web"], [a["id"] for a in item["ref"]["apps"]])
        self.assertEqual("running", reserve.status(item)[0])            # paperless changed
        self.deps["lab/paperless"]["status"]["readyReplicas"] = 0      # restarting
        reserve.status(item)
        self.assertEqual(1, len(self.sent))                      # web waits for paperless
        self.deps["lab/paperless"]["status"]["readyReplicas"] = 1
        reserve.status(item)
        reserve.status(item)
        (method, path, body), kwargs = self.sent[1]
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/web"), (method, path))
        self.assertEqual(["app", "sidecar"], [c["name"] for c in body["spec"]["template"]["spec"]["containers"]])
        self.assertEqual({"cpu": "10m", "memory": "32Mi"}, body["spec"]["template"]["spec"]["containers"][1]["resources"]["requests"])
        self.assertEqual("application/strategic-merge-patch+json", kwargs["ctype"])
        reserve.status(item)
        state, progress, message = reserve.status(item)
        self.assertEqual(("succeeded", 100), (state, progress))
        self.assertIn("2 apps", message)

    def test_a_change_already_made_is_waited_for_not_skipped(self):
        item = self.job(["lab/paperless"])
        self.deps["lab/paperless"]["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "requests": {"cpu": "60m", "memory": "480Mi"}}
        state, _, message = reserve.status(item)
        self.assertEqual(("running", "rolling"), (state, item["ref"]["stage"]))
        self.assertEqual([], self.sent)
        self.assertNotIn("skipped", item["ref"])

    def test_an_app_no_host_has_room_for_is_skipped(self):
        item = self.job(["lab/paperless"])
        self.node_memory = "256Mi"
        message = reserve.status(item)[2]
        self.assertIn("no host it may run on has room for its new reservation (480 MiB", message)
        self.assertEqual([], self.sent)

    def test_an_app_deleted_mid_job_is_skipped_and_the_rest_go_on(self):
        item = self.job(["lab/paperless", "lab/web"])
        gone = urllib.error.HTTPError("x", 404, "not found", {}, io.BytesIO())
        keep_bound(self, reserve)   # put back what reserve.bind() sets
        reserve.bind(self.kget, mock.Mock(side_effect=[gone, None]), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)
        self.assertIn("Skipped lab/paperless", reserve.status(item)[2])

    def test_a_refused_change_skips_that_app_and_the_rest_go_on(self):
        item = self.job(["lab/paperless", "lab/web"])
        refuse = urllib.error.HTTPError("x", 422, "request above limit", {}, io.BytesIO())
        keep_bound(self, reserve)   # put back what reserve.bind() sets
        reserve.bind(self.kget, mock.Mock(side_effect=[refuse, None]), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)
        state, _, message = reserve.status(item)
        self.assertEqual("running", state)
        self.assertIn("Skipped lab/paperless", message)
        self.assertEqual(1, item["ref"]["index"])

    def test_an_app_that_does_not_come_back_stops_the_job_and_says_what_it_had(self):
        item = self.job(["lab/frigate"])
        item["ref"]["apps"].append({"id": "lab/web", "ns": "lab", "name": "web", "changes": []})
        reserve.status(item)
        self.deps["lab/frigate"]["status"]["readyReplicas"] = 0
        item["ref"]["started"] -= 601
        state, _, message = reserve.status(item)
        self.assertEqual("failed", state)
        self.assertIn("the rest were not changed", message)
        self.assertIn("It reserved app: CPU 50m, memory 128Mi before", message)
        self.assertEqual(1, len(self.sent))

    def nodes(self, *nodes):
        self.node_list = list(nodes)
        get = self.kget
        self.kget = lambda path: {"items": copy.deepcopy(self.node_list)} if path == "/api/v1/nodes" else get(path)
        keep_bound(self, reserve)   # put back what reserve.bind() sets
        reserve.bind(self.kget, lambda *a, **k: self.sent.append((a, k)), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)

    @staticmethod
    def node(name, memory, labels=None, taints=None):
        return {"metadata": {"name": name, "labels": labels or {}}, "spec": {"taints": taints or []},
                "status": {"allocatable": {"cpu": "4", "memory": memory}, "conditions": [{"type": "Ready", "status": "True"}]}}

    def test_an_app_pinned_to_a_full_host_is_skipped_however_empty_the_others_are(self):
        self.deps["lab/paperless"]["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "small"}
        self.nodes(self.node("small", "256Mi", {"kubernetes.io/hostname": "small"}),
                   self.node("big", "64Gi", {"kubernetes.io/hostname": "big"}))
        message = reserve.status(self.job(["lab/paperless"]))[2]
        self.assertIn("no host it may run on has room", message)
        self.assertEqual([], self.sent)

    def test_a_host_whose_taint_the_app_does_not_tolerate_gives_it_no_room(self):
        self.nodes(self.node("pi", "64Gi", taints=[{"key": "homestead.io/role", "value": "tiebreaker", "effect": "NoSchedule"}]))
        message = reserve.status(self.job(["lab/paperless"]))[2]
        self.assertIn("no host it may run on takes apps now", message)
        self.assertEqual([], self.sent)

    def test_a_limit_lowered_below_the_new_reservation_is_refused_before_sending(self):
        item = self.job(["lab/paperless"])
        self.deps["lab/paperless"]["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"memory": "256Mi"}}
        message = reserve.status(item)[2]
        self.assertIn("memory limit is now 256Mi, below the new reservation 480Mi", message)
        self.assertEqual([], self.sent)

    def test_a_reservation_kubernetes_could_not_read_is_refused_at_the_review(self):
        plan = reserve.plan(["lab/paperless"])
        apps = [a for a in plan["apps"] if a["chosen"]]
        apps[0]["changes"][0]["memory"] = "lots"
        with self.assertRaisesRegex(ValueError, "no readable reservation"):
            reserve.reviewed(apps, reserve.review_token(apps))

    def test_a_reservation_the_api_wrote_back_another_way_counts_as_applied(self):
        SUGGEST[("lab", "paperless", "app")] = ("1000m", "1024Mi")
        self.addCleanup(SUGGEST.__setitem__, ("lab", "paperless", "app"), ("60m", "480Mi"))
        item = self.job(["lab/paperless"])
        self.deps["lab/paperless"]["spec"]["template"]["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "1", "memory": "1Gi"}}
        reserve.status(item)
        self.assertEqual(("rolling", []), (item["ref"]["stage"], self.sent))

    def test_a_paused_or_unsettled_app_is_left_alone(self):
        for change, why in ((lambda d: d["spec"].update(paused=True), "is paused"),
                            (lambda d: d["status"].update(readyReplicas=0), "not running as it should")):
            self.setUp()
            item = self.job(["lab/paperless"])
            change(self.deps["lab/paperless"])
            self.assertIn(why, reserve.status(item)[2])
            self.assertEqual([], self.sent)

    def test_a_stopped_app_needs_no_room_now(self):
        self.deps["lab/paperless"]["spec"]["replicas"] = 0
        for key in ("replicas", "updatedReplicas", "readyReplicas"):
            self.deps["lab/paperless"]["status"][key] = 0
        self.node_memory = "256Mi"
        reserve.status(self.job(["lab/paperless"]))
        self.assertEqual(1, len(self.sent))

    def test_a_run_that_skipped_every_app_says_nothing_was_changed(self):
        item = self.job(["lab/paperless"])
        self.node_memory = "256Mi"
        reserve.status(item)
        state, _, message = reserve.status(item)
        self.assertEqual("succeeded", state)
        self.assertTrue(message.startswith("Nothing was changed; left 1"), message)

    def test_a_refused_change_says_kubernetes_reason(self):
        item = self.job(["lab/paperless"])
        refuse = urllib.error.HTTPError("x", 422, "Unprocessable", {},
                                        io.BytesIO(b'{"message": "requests.memory: must be less than or equal to memory limit"}'))
        keep_bound(self, reserve)   # put back what reserve.bind() sets
        reserve.bind(self.kget, mock.Mock(side_effect=[refuse]), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)
        self.assertIn("Kubernetes refused it (422: requests.memory: must be less than", reserve.status(item)[2])

    def test_a_recreate_app_gets_back_only_its_own_pods_room(self):
        self.deps["lab/paperless"]["spec"]["strategy"] = {"type": "Recreate"}
        pods = [{"metadata": {"namespace": "lab", "name": f"{owner}-5d8f-x", "labels": {"pod-template-hash": "5d8f"},
                              "ownerReferences": [{"kind": "ReplicaSet", "name": f"{owner}-5d8f"}]},
                 "spec": {"nodeName": "n1", "containers": [{"name": "app", "resources": {"requests": {"memory": "200Mi"}}}]},
                 "status": {"phase": "Running"}} for owner in ("paperless", "paperless-worker")]
        get = self.kget
        self.kget = lambda path: {"items": copy.deepcopy(pods)} if path == "/api/v1/pods" else get(path)
        keep_bound(self, reserve)   # put back what reserve.bind() sets
        reserve.bind(self.kget, lambda *a, **k: self.sent.append((a, k)), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)
        self.node_memory = "700Mi"     # 300 free; 500 with paperless's own pod back; 700 had the worker's counted too
        reserve.status(self.job(["lab/paperless"]))
        self.assertEqual(1, len(self.sent))
        self.setUp()
        self.deps["lab/paperless"]["spec"]["strategy"] = {"type": "Recreate"}
        self.kget = lambda path: {"items": copy.deepcopy(pods)} if path == "/api/v1/pods" else RequestsTests.kget(self, path)
        reserve.bind(self.kget, lambda *a, **k: self.sent.append((a, k)), usage, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)
        self.node_memory = "600Mi"     # 200 free; 400 with its own pod back - short of 480
        self.assertIn("no host it may run on has room", reserve.status(self.job(["lab/paperless"]))[2])

    def test_a_container_reserving_through_its_limit_is_shown_with_it(self):
        found = {"days": 7, "hours": 168, "samples": 2016, "cpu_p95_m": 40, "mem_p95_mi": 380, "mem_max_mi": 500,
                 "suggest": {"cpu": "60m", "memory": "480Mi", "capped": [], "cpu_now_m": 2000, "memory_now_mi": 2048,
                             "lowers": True, "differs": True}}
        self.deps["lab/paperless"]["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"cpu": "2", "memory": "2Gi"}}
        keep_bound(self, reserve)   # put back what reserve.bind() sets
        reserve.bind(self.kget, lambda *a, **k: None, lambda *a, **k: found, managed,
                     ("homestead", "homestead"), {"kube-system"}, lambda: True)
        app = next(a for a in reserve.plan()["apps"] if a["id"] == "lab/paperless")
        change = app["changes"][0]
        self.assertEqual((2000, 2048, ["cpu", "memory"], False), (change["cpu_now_m"], change["memory_now_mi"],
                                                                 change["from_limit"], change["missing"]))


if __name__ == "__main__":
    unittest.main()
