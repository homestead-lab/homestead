"""Priority tiers: the classes Homestead keeps, reading an app's tier, the
reviewed change for many apps, setting one in Edit, and the headroom a
critical app's reservation gets."""
import copy
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_workload_edit as WORKLOAD_EDIT
import homestead_priority as priority
import homestead_usage as usage
import server


def deployment(name, ns="lab", image="busybox", cls="", labels=None):
    spec = {"containers": [{"name": "app", "image": image}]}
    if cls:
        spec["priorityClassName"] = cls
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {}, "generation": 1},
            "spec": {"replicas": 1, "template": {"spec": spec}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1}}


def vm(name, ns="lab", cls=""):
    spec = {"domain": {"devices": {}}}
    if cls:
        spec["priorityClassName"] = cls
    return {"metadata": {"name": name, "namespace": ns}, "spec": {"runStrategy": "Always", "template": {"spec": spec}}}


def statefulset(name, ns="lab", image="busybox", cls="", labels=None, replicas=1, strategy=None):
    sts = deployment(name, ns, image, cls, labels)
    sts["spec"]["replicas"] = replicas
    sts["spec"]["updateStrategy"] = strategy or {"type": "RollingUpdate"}
    sts["status"] = {"observedGeneration": 1, "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas,
                     "currentRevision": f"{name}-1", "updateRevision": f"{name}-1"}
    return sts


def not_found(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())


def managed(dep):
    if (dep["metadata"].get("labels") or {}).get("kustomize.toolkit.fluxcd.io/name"):
        return {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}
    return {}


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.deps = {
            "lab/home-assistant": deployment("home-assistant", image="ghcr.io/home-assistant/home-assistant:stable"),
            "lab/vault": deployment("vault", image="vaultwarden/server", cls="homestead-critical"),
            "lab/paperless": deployment("paperless"),
            "ci/actions-runner": deployment("actions-runner", ns="ci"),
            "lab/unifi": deployment("unifi", cls="unifi-high"),
            "media/jellyfin": deployment("jellyfin", ns="media", labels={"kustomize.toolkit.fluxcd.io/name": "apps"}),
            "homestead/homestead": deployment("homestead", ns="homestead"),
        }
        self.classes, self.sent, self.vms, self.pods, self.stss = {}, [], {}, [], {}
        priority.bind(self.kget, self.ksend, managed, ("homestead", "homestead"), {"kube-system"}, lambda: True)

    def kget(self, path):
        if path == "/apis/apps/v1/deployments":
            return {"items": [copy.deepcopy(d) for d in self.deps.values()]}
        if path == priority.VMS:
            if self.vms is None:
                raise not_found(path)
            return {"items": [copy.deepcopy(v) for v in self.vms.values()]}
        if path == "/api/v1/pods":
            return {"items": copy.deepcopy(self.pods)}
        if path == "/apis/apps/v1/statefulsets":
            if self.stss is None:
                raise not_found(path)
            return {"items": [copy.deepcopy(s) for s in self.stss.values()]}
        if "/statefulsets/" in path:
            ns, name = path.split("/namespaces/")[1].split("/statefulsets/")
            if f"{ns}/{name}" not in self.stss:
                raise not_found(path)
            return copy.deepcopy(self.stss[f"{ns}/{name}"])
        if "/virtualmachines/" in path:
            ns, name = path.split("/namespaces/")[1].split("/virtualmachines/")
            if f"{ns}/{name}" not in self.vms:
                raise not_found(path)
            return copy.deepcopy(self.vms[f"{ns}/{name}"])
        if path == "/apis/scheduling.k8s.io/v1/priorityclasses":
            # These tests describe Homestead as it was before #429, which is what a
            # cluster gets whose permissions predate listing classes (403); a test
            # that needs the real list sets self.listable.
            if not getattr(self, "listable", False):
                raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
            return {"items": list(self.classes.values())}
        if "/priorityclasses/" in path:
            name = path.rsplit("/", 1)[1]
            if name not in self.classes:
                raise not_found(path)
            return self.classes[name]
        ns, name = path.split("/namespaces/")[1].split("/deployments/")
        if f"{ns}/{name}" not in self.deps:
            raise not_found(path)
        return copy.deepcopy(self.deps[f"{ns}/{name}"])

    def ksend(self, method, path, body=None, ctype="application/json"):
        self.sent.append((method, path, body, ctype))
        if method == "POST":
            self.classes[body["metadata"]["name"]] = body

    def test_no_class_is_normal_and_a_class_not_on_the_cluster_is_missing(self):
        # #429: a class is placed by its value; one the cluster does not list is missing, not "other".
        self.assertEqual(("normal", ""), priority.tier_of({}))
        self.assertEqual(("critical", "homestead-critical"), priority.tier_of({"priorityClassName": "homestead-critical"}))
        self.assertEqual(("missing", "unifi-high"), priority.tier_of({"priorityClassName": "unifi-high"}))

    def test_the_classes_are_made_once_with_low_never_evicting_and_none_the_default(self):
        priority.ensure()
        priority.ensure()
        made = {body["metadata"]["name"]: body for method, _, body, _ in self.sent if method == "POST"}
        self.assertEqual({"homestead-critical", "homestead-low"}, set(made))
        self.assertEqual(2, len([s for s in self.sent if s[0] == "POST"]))
        self.assertEqual("Never", made["homestead-low"]["preemptionPolicy"])
        self.assertTrue(made["homestead-critical"]["value"] > 0 > made["homestead-low"]["value"])
        self.assertFalse(any(b["globalDefault"] for b in made.values()))

    def test_only_the_class_a_tier_needs_is_made(self):
        priority.ensure(("low",))
        self.assertEqual(["homestead-low"], [b["metadata"]["name"] for m, _, b, _ in self.sent if m == "POST"])

    def test_permissions_from_before_this_version_say_so_plainly(self):
        denied = urllib.error.HTTPError("x", 403, "forbidden", {}, io.BytesIO())
        priority.bind(mock.Mock(side_effect=denied), self.ksend, managed, ("homestead", "homestead"), {"kube-system"}, lambda: True)
        with self.assertRaisesRegex(PermissionError, "permissions predate this version"):
            priority.ensure(("critical",))

    def test_a_class_made_meanwhile_by_another_save_is_taken_as_made(self):
        def ksend(method, path, body=None, ctype=None):
            raise urllib.error.HTTPError(path, 409, "exists", {}, io.BytesIO())
        priority.bind(self.kget, ksend, managed, ("homestead", "homestead"), {"kube-system"}, lambda: True)
        self.assertEqual([], priority.ensure(("critical",)))

    def test_the_job_fails_plainly_when_it_may_not_make_the_classes(self):
        plan = priority.plan()
        apps = priority.reviewed([a for a in plan["apps"] if a["tier"] != a["current"]], plan["review_token"])
        with mock.patch.object(priority, "ensure", side_effect=PermissionError(priority.NO_PERMISSION)):
            state, _, message = priority.status({"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}})
        self.assertEqual("failed", state)
        self.assertIn("permissions", message)

    def test_an_exporter_or_backup_beside_a_database_is_not_suggested_critical(self):
        for name in ("postgres-exporter", "mariadb-backup", "adguardhome-sync"):
            self.assertEqual((None, ""), priority.suggestion(deployment(name, image=f"example/{name}")), name)
        self.assertEqual("critical", priority.suggestion(deployment("db", image="postgres:16"))[0])

    def test_an_operator_is_not_offered_apps_only_an_admin_may_change(self):
        def guard(dep, ns, role):
            if role == "operator" and dep["metadata"]["name"] == "paperless":
                raise PermissionError("only an admin can operate management, system, service-account or host-privileged workloads")
        with mock.patch.object(priority, "guard", guard):
            plan = priority.plan(None, "operator")
            self.assertNotIn("lab/paperless", {a["id"] for a in plan["apps"]})
            self.assertIn("only an admin", {s["id"]: s for s in plan["skipped"]}["lab/paperless"]["why"])

    def test_a_class_someone_made_with_another_value_is_kept_and_said(self):
        self.classes["homestead-critical"] = {"value": 5}
        notes = priority.ensure()
        self.assertIn("kept", notes[0])
        self.assertNotIn("homestead-critical", [b["metadata"]["name"] for m, _, b, _ in self.sent if m == "POST"])

    def test_likely_critical_and_low_apps_start_at_their_suggestion(self):
        apps = {a["id"]: a for a in priority.plan()["apps"]}
        self.assertEqual(("normal", "critical"), (apps["lab/home-assistant"]["current"], apps["lab/home-assistant"]["tier"]))
        self.assertEqual("low", apps["ci/actions-runner"]["tier"])
        self.assertEqual("normal", apps["lab/paperless"]["tier"])
        self.assertEqual("critical", apps["lab/vault"]["tier"])                     # already set: no change

    def test_homestead_flux_apps_and_foreign_classes_are_not_changed(self):
        plan = priority.plan()
        apps = {a["id"]: a for a in plan["apps"]}
        self.assertNotIn("homestead/homestead", apps)
        self.assertIn("critical once any app", {s["id"]: s for s in plan["skipped"]}["homestead/homestead"]["why"])
        self.assertEqual("normal", apps["media/jellyfin"]["tier"])
        self.assertIn("set priorityClassName there", apps["media/jellyfin"]["note"])
        self.assertIn("unifi-high", {s["id"]: s for s in plan["skipped"]}["lab/unifi"]["why"])
        with self.assertRaisesRegex(ValueError, "not one Homestead changes"):
            priority.check("media", "jellyfin", "normal")

    def test_the_review_covers_exactly_the_changes_and_a_tier_set_since_is_kept(self):
        plan = priority.plan({"lab/paperless": "low"})
        changes = [a for a in plan["apps"] if a["tier"] != a["current"]]
        self.assertEqual({"lab/home-assistant", "ci/actions-runner", "lab/paperless"}, {c["id"] for c in changes})
        self.assertEqual(3, len(priority.reviewed(changes, plan["review_token"])))
        with self.assertRaisesRegex(ValueError, "differ from the review"):
            priority.reviewed(changes, priority.plan()["review_token"])
        self.deps["lab/paperless"]["spec"]["template"]["spec"]["priorityClassName"] = "homestead-critical"
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            priority.reviewed(changes, plan["review_token"])

    def test_starting_needs_the_restarts_confirmed_and_no_other_run(self):
        started, self.running = [], []
        jobs = mock.Mock(TERMINAL={"succeeded", "failed", "cancelled"}, snapshot=lambda: list(self.running),
                         start=lambda *a: started.append(a) or {"id": "op1"})
        plan = priority.plan()
        body = {"changes": [a for a in plan["apps"] if a["tier"] != a["current"]], "review_token": plan["review_token"],
                "restart": True}
        with mock.patch.object(priority, "operations", jobs):
            with self.assertRaisesRegex(ValueError, "confirm the restarts"):
                priority.start({**body, "restart": False}, "operator")
            self.running = [{"kind": priority.KIND, "status": "running"}]
            with self.assertRaisesRegex(ValueError, "already being set"):
                priority.start(body, "operator")
            self.running = []
            self.assertEqual({"operation": {"id": "op1"}}, priority.start(body, "operator"))
        self.assertEqual("operator", started[0][4]["role"])

    def test_the_routes_are_declared_with_their_roles_and_read_the_tiers_asked_for(self):
        import homestead_routes as ROUTES
        self.assertEqual("viewer", priority.ROUTES[("GET", "/api/workloads/priority/plan")][0])
        self.assertEqual("operator", priority.ROUTES[("POST", "/api/workloads/priority")][0])
        handler = priority.ROUTES[("GET", "/api/workloads/priority/plan")][1]
        with mock.patch.object(priority, "plan", side_effect=lambda chosen, role: (chosen, role)):
            asked = handler(ROUTES.Request("GET", "/api/workloads/priority/plan", {"chosen": ["lab/ha=critical,lab/ci=low"]},
                                           None, "op", "operator"))
        self.assertEqual(({"lab/ha": "critical", "lab/ci": "low"}, "operator"), asked)

    def test_the_job_makes_the_classes_then_sets_each_app_and_normal_removes_the_class(self):
        self.deps["lab/vault"]["spec"]["template"]["spec"]["priorityClassName"] = "homestead-critical"
        plan = priority.plan({"lab/vault": "normal", "lab/home-assistant": "normal", "ci/actions-runner": "normal"})
        apps = priority.reviewed([a for a in plan["apps"] if a["tier"] != a["current"]], plan["review_token"])
        item = {"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}}
        self.assertIn("Priority classes ready", priority.status(item)[2])
        priority.status(item)
        patch = [s for s in self.sent if s[0] == "PATCH"][0]
        self.assertEqual("/apis/apps/v1/namespaces/lab/deployments/vault", patch[1])
        self.assertEqual({"spec": {"template": {"spec": {"priorityClassName": None, "priority": None, "preemptionPolicy": None}}}},
                         patch[2])
        self.assertEqual("application/strategic-merge-patch+json", patch[3])
        priority.status(item)
        self.assertEqual(("succeeded", 100), priority.status(item)[:2])

    def test_edit_sets_and_clears_a_tier_and_leaves_a_foreign_class(self):
        spec = {}
        WORKLOAD_EDIT._apply_priority(spec, "critical")
        self.assertEqual("homestead-critical", spec["priorityClassName"])
        WORKLOAD_EDIT._apply_priority(spec, "normal")
        self.assertNotIn("priorityClassName", spec)
        foreign = {"priorityClassName": "unifi-high"}
        WORKLOAD_EDIT._apply_priority(foreign, "low")
        self.assertEqual("unifi-high", foreign["priorityClassName"])
        copied = {"priority": 0, "preemptionPolicy": "PreemptLowerPriority"}
        WORKLOAD_EDIT._apply_priority(copied, "low")
        self.assertEqual({"priorityClassName": "homestead-low"}, copied)
        with self.assertRaisesRegex(ValueError, "critical, normal or low"):
            WORKLOAD_EDIT._apply_priority({}, "urgent")

    def test_rows_stay_in_the_order_of_the_tier_each_has_now(self):
        before = [a["id"] for a in priority.plan()["apps"]]
        after = [a["id"] for a in priority.plan({"lab/paperless": "critical"})["apps"]]
        self.assertEqual(before, after)

    def test_vms_have_tiers_set_in_their_template_with_no_restart(self):
        self.vms = {"lab/windows": vm("windows"), "lab/haos": vm("haos", cls="homestead-critical")}
        plan = priority.plan({"vm:lab/windows": "low"})
        rows = {a["id"]: a for a in plan["apps"]}
        self.assertEqual(("vm", "normal", "low"), (rows["vm:lab/windows"]["kind"], rows["vm:lab/windows"]["current"],
                                                   rows["vm:lab/windows"]["tier"]))
        self.assertEqual("critical", rows["vm:lab/haos"]["current"])
        apps = priority.reviewed([rows["vm:lab/windows"]], priority.plan({"vm:lab/windows": "low", "lab/home-assistant": "normal",
                                                                            "ci/actions-runner": "normal"})["review_token"])
        self.assertEqual([("vm", "lab", "windows")], [(a["kind"], a["ns"], a["name"]) for a in apps])
        item = {"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}}
        priority.status(item)
        self.assertIn("when it next starts", priority.status(item)[2])
        patch = [s for s in self.sent if s[0] == "PATCH"][0]
        self.assertEqual(("/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/windows",
                          {"spec": {"template": {"spec": {"priorityClassName": "homestead-low"}}}}, "application/merge-patch+json"),
                         patch[1:])
        self.assertEqual(("succeeded", 100), priority.status(item)[:2])

    def test_a_cluster_without_kubevirt_plans_its_apps_only(self):
        self.vms = None
        self.assertFalse([a for a in priority.plan()["apps"] if a["kind"] == "vm"])

    # ------------------------------------------------ StatefulSets (VM-style)
    def sts_job(self, sts_id, tier):
        plan = priority.plan({sts_id: tier, "lab/home-assistant": "normal", "ci/actions-runner": "normal"})
        rows = [a for a in plan["apps"] if a["tier"] != a["current"]]
        self.assertEqual([sts_id], [a["id"] for a in rows])
        apps = priority.reviewed(rows, plan["review_token"])
        item = {"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}}
        priority.status(item)    # the classes
        return item

    def test_statefulsets_are_listed_with_sts_ids_and_system_namespaces_are_skipped(self):
        self.stss = {"lab/postgres": statefulset("postgres"), "kube-system/etcd-backup": statefulset("etcd-backup", ns="kube-system"),
                     "longhorn-system/share": statefulset("share", ns="longhorn-system")}
        rows = [a for a in priority.plan()["apps"] if a["kind"] == "sts"]
        self.assertEqual([("sts:lab/postgres", "lab", "postgres")], [(a["id"], a["ns"], a["name"]) for a in rows])
        self.assertEqual(("own", "", False, True), (rows[0]["source"], rows[0]["group"], rows[0]["follows"], rows[0]["restarts"]))

    def test_ids_and_paths_round_trip_for_every_kind(self):
        paths = {"app": "/apis/apps/v1/namespaces/lab/deployments/x-db",
                 "vm": "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/x-db",
                 "sts": "/apis/apps/v1/namespaces/lab/statefulsets/x-db"}
        for kind, path in paths.items():
            row_id = priority._row_id(kind, "lab", "x-db")
            self.assertEqual((kind, "lab", "x-db"), priority._split(row_id), row_id)
            self.assertEqual(path, priority._path(*priority._split(row_id)))
        self.assertEqual(["lab/x-db", "vm:lab/x-db", "sts:lab/x-db"], [priority._row_id(k, "lab", "x-db") for k in paths])

    def test_a_statefulsets_tier_is_patched_on_its_own_path_and_it_rolls(self):
        self.stss = {"lab/postgres": statefulset("postgres", replicas=3)}
        item = self.sts_job("sts:lab/postgres", "low")
        self.assertIn("it restarts", priority.status(item)[2])
        patch = [s for s in self.sent if s[0] == "PATCH"]
        self.assertEqual([("/apis/apps/v1/namespaces/lab/statefulsets/postgres",
                           {"spec": {"template": {"spec": {"priorityClassName": "homestead-low", "priority": None, "preemptionPolicy": None}}}},
                           "application/strategic-merge-patch+json")], [p[1:] for p in patch])
        self.assertEqual("rolling", item["ref"]["stage"])
        self.assertIn("runs as low", priority.status(item)[2])
        self.assertEqual(("succeeded", 100), priority.status(item)[:2])

    def test_a_statefulset_is_settled_only_when_its_revisions_match_and_its_replicas_are_ready(self):
        sts = statefulset("postgres", replicas=3)
        self.stss = {"lab/postgres": sts}
        self.assertTrue(priority._settled("lab", "postgres", "sts"))
        sts["status"]["updateRevision"] = "postgres-2"
        self.assertFalse(priority._settled("lab", "postgres", "sts"), "the new revision isn't out yet")
        sts["status"].update(currentRevision="postgres-2", readyReplicas=2)
        self.assertFalse(priority._settled("lab", "postgres", "sts"), "a pod is still starting")
        sts["status"]["readyReplicas"] = 3
        self.assertTrue(priority._settled("lab", "postgres", "sts"))
        sts["metadata"]["generation"] = 2
        self.assertFalse(priority._settled("lab", "postgres", "sts"), "the controller hasn't seen the change")
        one = statefulset("x-db")
        del one["spec"]["replicas"]
        self.stss["lab/x-db"] = one
        self.assertTrue(priority._settled("lab", "x-db", "sts"), "replicas default to 1")

    def test_a_statefulset_has_ten_minutes_per_pod(self):
        self.stss = {"lab/postgres": statefulset("postgres", replicas=3)}
        item = self.sts_job("sts:lab/postgres", "low")
        priority.status(item)
        self.stss["lab/postgres"]["status"]["readyReplicas"] = 2
        item["ref"]["started"] = priority.time.time() - 1700
        self.assertEqual("running", priority.status(item)[0], "three pods: thirty minutes")
        item["ref"]["started"] = priority.time.time() - 1900
        state, _, message = priority.status(item)
        self.assertEqual("failed", state)
        self.assertIn("30 minutes", message)

    def test_a_deployment_keeps_ten_minutes(self):
        self.deps["lab/paperless"]["spec"]["replicas"] = 3
        self.deps["lab/paperless"]["status"]["readyReplicas"] = 2
        plan = priority.plan({"lab/paperless": "low", "lab/home-assistant": "normal", "ci/actions-runner": "normal"})
        apps = priority.reviewed([a for a in plan["apps"] if a["tier"] != a["current"]], plan["review_token"])
        item = {"ref": {"apps": apps, "index": 0, "done": 0, "stage": "apply"}}
        priority.status(item)
        priority.status(item)
        item["ref"]["started"] = priority.time.time() - 700
        state, _, message = priority.status(item)
        self.assertEqual("failed", state)
        self.assertIn("10 minutes", message)

    def test_statefulsets_that_dont_restart_themselves_take_their_tier_at_their_next_restart(self):
        for strategy in ({"type": "OnDelete"}, {"type": "RollingUpdate", "rollingUpdate": {"partition": 3}}):
            self.sent = []
            self.stss = {"lab/postgres": statefulset("postgres", replicas=3, strategy=strategy)}
            row = next(a for a in priority.plan()["apps"] if a["id"] == "sts:lab/postgres")
            self.assertFalse(row["restarts"], strategy)
            item = self.sts_job("sts:lab/postgres", "low")
            message = priority.status(item)[2]
            self.assertEqual("postgres takes low when its pods are next restarted", message)
            self.assertEqual(("succeeded", 100), priority.status(item)[:2])
            self.assertEqual(["PATCH"], [s[0] for s in self.sent if s[0] != "POST"], "nothing is deleted or restarted")
            self.assertEqual("/apis/apps/v1/namespaces/lab/statefulsets/postgres", [s for s in self.sent if s[0] == "PATCH"][0][1])
        self.stss = {"lab/postgres": statefulset("postgres", strategy={"type": "RollingUpdate", "rollingUpdate": {"partition": 0}})}
        self.assertTrue(next(a for a in priority.plan()["apps"] if a["id"] == "sts:lab/postgres")["restarts"], "partition 0 rolls")

    def test_a_statefulsets_status_is_as_an_own_tier_deployments(self):
        # As own-tier Deployments: Flux's reads in line with the note, read-only;
        # Homestead's reads in line with its suggestion ticked.
        flux = {"kustomize.toolkit.fluxcd.io/name": "apps"}
        self.stss = {"media/postgres": statefulset("postgres", ns="media", image="postgres:16", labels=flux),
                     "lab/postgres": statefulset("postgres", image="postgres:16")}
        rows = {a["id"]: a for a in priority.plan()["apps"]}
        git, ours = rows["sts:media/postgres"], rows["sts:lab/postgres"]
        self.assertEqual(("in_line", "normal", "normal", "Flux"), (git["status"], git["wanted"], git["tier"], git["managed"]))
        self.assertIn("set priorityClassName there", git["note"])
        self.assertNotIn("line", git)
        self.assertEqual(("in_line", "normal", "critical", "critical"), (ours["status"], ours["wanted"], ours["tier"], ours["suggested"]))
        with self.assertRaisesRegex(ValueError, "not one Homestead changes"):
            priority.check("media", "postgres", "normal", kind="sts")

    def test_a_picked_tier_never_moves_a_statefulsets_row(self):
        self.stss = {"lab/x-db": statefulset("x-db")}
        before = [a["id"] for a in priority.plan()["apps"]]
        picked = priority.plan({"sts:lab/x-db": "critical"})
        self.assertEqual(before, [a["id"] for a in picked["apps"]])
        self.assertEqual("in_line", next(a for a in picked["apps"] if a["id"] == "sts:lab/x-db")["status"])

    def test_a_tier_picked_for_a_statefulset_flux_applies_is_refused(self):
        self.stss = {"media/postgres": statefulset("postgres", ns="media", labels={"kustomize.toolkit.fluxcd.io/name": "apps"})}
        row = next(a for a in priority.plan({"sts:media/postgres": "critical"})["apps"] if a["id"] == "sts:media/postgres")
        self.assertEqual(("normal", "in_line"), (row["tier"], row["status"]), "a pick never reaches it")
        change = [{"id": "sts:media/postgres", "current": "normal", "tier": "critical", "follow": False}]
        with self.assertRaisesRegex(ValueError, "not one Homestead changes"):
            priority.reviewed(change, priority.review_token(change))
        self.assertFalse([s for s in self.sent if s[0] != "POST"])

    def test_a_partition_below_the_replicas_restarts_those_pods_now_and_the_job_waits(self):
        # Kubernetes updates every pod at or above the partition at once.
        sts = statefulset("postgres", replicas=3, strategy={"type": "RollingUpdate", "rollingUpdate": {"partition": 1}})
        self.stss = {"lab/postgres": sts}
        self.assertTrue(next(a for a in priority.plan()["apps"] if a["id"] == "sts:lab/postgres")["restarts"])
        item = self.sts_job("sts:lab/postgres", "low")
        self.assertIn("it restarts", priority.status(item)[2])
        self.assertEqual("rolling", item["ref"]["stage"])
        sts = self.stss["lab/postgres"]
        sts["status"].update(updateRevision="postgres-2", updatedReplicas=1, readyReplicas=2)
        self.assertIn("Waiting for", priority.status(item)[2])
        sts["status"].update(updatedReplicas=2, readyReplicas=2)
        self.assertIn("Waiting for", priority.status(item)[2], "a pod is still starting")
        sts["status"]["readyReplicas"] = 3
        self.assertIn("runs as low", priority.status(item)[2], "pod 0 stays on the old revision while partitioned")
        self.assertEqual(["PATCH"], [s[0] for s in self.sent if s[0] != "POST"], "no pod is ever deleted")

    def test_the_job_and_its_summary_name_statefulsets(self):
        self.stss = {"lab/x-db": statefulset("x-db")}
        started, jobs = [], mock.Mock(TERMINAL={"succeeded", "failed", "cancelled"}, snapshot=lambda: [],
                                      start=lambda *a: started.append(a) or {"id": "op1"})
        plan = priority.plan({"sts:lab/x-db": "critical", "lab/home-assistant": "normal"})
        body = {"changes": [a for a in plan["apps"] if a["tier"] != a["current"]], "review_token": plan["review_token"], "restart": True}
        with mock.patch.object(priority, "operations", jobs):
            priority.start(body, "operator")
        self.assertEqual("Set the priority tier of 1 app and 1 StatefulSet", started[0][1])
        ref = started[0][4]
        item = {"ref": ref}
        while True:
            state, _, message = priority.status(item)
            if state != "running":
                break
        self.assertEqual("Set the tier of 1 app and 1 StatefulSet", message)
        self.assertEqual("Stopped after setting 1 app and 1 StatefulSet", priority.cancel_run(item, None))

    def test_a_cluster_without_statefulsets_is_unchanged(self):
        self.stss = {}
        before = priority.plan()
        self.stss = None
        after = priority.plan()
        self.assertEqual(before, after)
        self.assertFalse([a for a in after["apps"] if a["kind"] == "sts"])

    def test_homestead_joins_critical_once_anything_runs_there(self):
        jobs = mock.Mock(TERMINAL={"succeeded", "failed", "cancelled"}, snapshot=lambda: list(self.running))
        self.running = []
        # protect() acts only on the classes as listed, never on a guess (#429).
        self.listable = True
        self.classes["homestead-critical"] = pc("homestead-critical", 1000)
        priority._LADDER.update(at=0, value=None)
        with mock.patch.object(priority, "operations", jobs):
            self.assertIsNone(priority.protect(), "nothing is critical yet")
            self.pods = [{"spec": {"priorityClassName": "homestead-critical"}}]
            self.running = [{"kind": "deployment", "status": "running"}]
            self.assertIsNone(priority.protect(), "a job runs, which its restart would interrupt")
            self.running = []
            self.assertIn("no app can evict it", priority.protect())
        patch = [s for s in self.sent if s[0] == "PATCH"][0]
        self.assertEqual("/apis/apps/v1/namespaces/homestead/deployments/homestead", patch[1])
        self.assertEqual("homestead-critical", patch[2]["spec"]["template"]["spec"]["priorityClassName"])

    def test_homestead_keeps_a_class_it_already_has(self):
        self.pods = [{"spec": {"priorityClassName": "homestead-critical"}}]
        for cls in ("homestead-critical", "system-cluster-critical"):
            self.deps["homestead/homestead"]["spec"]["template"]["spec"]["priorityClassName"] = cls
            self.assertIsNone(priority.protect())
        self.assertFalse(self.sent)

    def edit(self, body, current_class=None):
        """The Edit route, with everything after the priority step stood in for."""
        dep = deployment("paperless", cls=current_class)
        made = []
        with mock.patch.object(server.WORKLOADS, "kget", return_value=dep), \
                mock.patch.object(server.WORKLOADS, "guard_managed_smb"), \
                mock.patch.object(server.WORKLOADS, "guard_self"), \
                mock.patch.object(server.WORKLOADS, "edit_capacity_plan", return_value=({"deployment": dep}, {}, {})), \
                mock.patch.object(server.WORKLOADS.CAPACITY_REVIEW, "enforce"), \
                mock.patch.object(server.WORKLOADS.RESTRUCTURE, "copies", return_value=[]), \
                mock.patch.object(server.WORKLOADS, "apply_reviewed_edit", return_value={"ok": True, "detail": "Saved"}), \
                mock.patch.object(server.WORKLOADS.PRIORITY, "ensure", side_effect=lambda tiers, lad=None: made.append(tiers) or
                                  ["homestead-critical exists with value 500 and PreemptLowerPriority; it is kept"]):
            result = server.WORKLOADS.edit(mock.Mock(body={"ns": "lab", "name": "paperless", **body}))
        return made, result

    def test_edit_makes_a_class_only_when_the_tier_changes(self):
        self.assertEqual([("critical",)], self.edit({"priority": "critical"})[0])
        self.assertEqual([], self.edit({"priority": "critical"}, current_class="homestead-critical")[0])
        self.assertEqual([], self.edit({"priority": "normal"})[0])

    def test_edit_says_when_a_class_someone_made_is_kept(self):
        self.assertIn("exists with value 500", self.edit({"priority": "critical"})[1]["detail"])

    def test_edit_never_sets_homesteads_own_tier(self):
        with mock.patch.object(server.WORKLOADS, "is_self", return_value=True):
            made, _ = self.edit({"priority": "critical"})
        self.assertEqual([], made)

    def test_the_edit_dialog_reads_the_tier_back(self):
        dep = deployment("vault", cls="homestead-critical")
        with mock.patch.object(WORKLOAD_EDIT, "seed_configs", return_value=[]):
            payload = server.WORKLOADS.workload_edit_payload("lab", "vault", dep, hardware_definitions=[], services=[])
        self.assertEqual(("critical", "homestead-critical"), (payload["priority"], payload["priority_class"]))


class HeadroomTests(unittest.TestCase):
    def test_a_critical_app_reserves_more_memory_than_a_normal_one(self):
        with tempfile.TemporaryDirectory() as folder:
            usage.bind(folder, server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem)
            try:
                for i in range(usage.MIN_ROUNDS):
                    usage.record([("lab", "ha", "ha", 100, 400)], now=1_760_000_000 + i * usage.STEP)
                now = 1_760_000_000 + usage.MIN_ROUNDS * usage.STEP
                normal = int(usage.summary("lab", "ha", "ha", now=now)["suggest"]["memory"][:-2])
                critical = int(usage.summary("lab", "ha", "ha", now=now, tier="critical")["suggest"]["memory"][:-2])
            finally:
                usage.bind("/data", server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem)
        self.assertGreater(critical, normal)
        self.assertTrue(592 <= critical <= 640, critical)          # 400Mi x 1.5, within half a bucket


if __name__ == "__main__":
    unittest.main()


P = priority


def keep_bindings(case):
    """A test that binds a fake cluster into homestead_priority puts the real
    bindings back after it, and forgets the ladder it read: other modules'
    tests read tiers through the same module."""
    saved = (P.kget, P.ksend, P.managed_by, P.own, P.system_ns)
    def restore():
        P.kget, P.ksend, P.managed_by, P.own, P.system_ns = saved
        P._LADDER.update(at=0, value=None)
    case.addCleanup(restore)


def pc(name, value, default=False, never=False):
    c = {"metadata": {"name": name}, "value": value}
    if default:
        c["globalDefault"] = True
    if never:
        c["preemptionPolicy"] = "Never"
    return c


X = [pc("x-critical", 100000), pc("x-normal", 10000, default=True), pc("x-low", 1000, never=True)]


class LadderTests(unittest.TestCase):
    def test_no_classes_means_default_zero_and_homestead_classes_to_make(self):
        lad = P.ladder([])
        self.assertEqual({"name": None, "value": 0}, lad["default"])
        self.assertEqual({"name": "homestead-critical", "value": 1000, "create": True}, lad["critical"])
        self.assertEqual({"name": "homestead-low", "value": -1000, "create": True}, lad["low"])

    def test_a_clusters_own_classes_are_used(self):
        lad = P.ladder(X)
        self.assertEqual("x-normal", lad["normal"])
        self.assertEqual({"name": "x-critical", "value": 100000, "create": False}, lad["critical"])
        self.assertEqual({"name": "x-low", "value": 1000, "create": False}, lad["low"])

    def test_a_stale_homestead_critical_gets_a_new_name_above_the_default(self):
        lad = P.ladder([pc("x-normal", 10000, default=True), pc("homestead-critical", 1000)])
        self.assertEqual({"name": "homestead-critical-11000", "value": 11000, "create": True}, lad["critical"])
        self.assertEqual("homestead-low", lad["low"]["name"], "a stale homestead-critical is never taken for low")
        self.assertEqual(9000, lad["low"]["value"])

    def test_critical_must_preempt_and_ties_prefer_homestead_then_name(self):
        lad = P.ladder([pc("b-high", 5000), pc("homestead-critical-5000", 5000), pc("a-wait", 9000, never=True)])
        self.assertEqual("homestead-critical-5000", lad["critical"]["name"])

    def test_low_prefers_never(self):
        lad = P.ladder([pc("x-normal", 10000, default=True), pc("x-batch", 9000), pc("x-low", 1000, never=True)])
        self.assertEqual("x-low", lad["low"]["name"])

    def test_a_default_at_the_cap_leaves_no_room_for_critical(self):
        lad = P.ladder([pc("top", 1_000_000_000, default=True)])
        self.assertIsNone(lad["critical"])
        self.assertTrue(any("above" in w for w in lad["warnings"]))

    def test_two_defaults_take_the_lowest_as_admission_does(self):
        lad = P.ladder([pc("a", 50, default=True), pc("b", 20, default=True)])
        self.assertEqual({"name": "b", "value": 20}, lad["default"])

    def test_system_classes_are_never_critical(self):
        lad = P.ladder([pc("system-cluster-critical", 2_000_000_000)])
        self.assertTrue(lad["critical"]["create"])

    def test_a_homestead_class_as_the_default_is_normal(self):
        lad = P.ladder([pc("homestead-critical", 1000, default=True)])
        self.assertEqual("homestead-critical", lad["normal"])
        self.assertEqual({"name": "homestead-critical-2000", "value": 2000, "create": True}, lad["critical"])

    def test_an_unreadable_class_list_behaves_as_today(self):
        saved = P.kget
        self.addCleanup(lambda: setattr(P, "kget", saved))
        def broken(path):
            raise urllib.error.HTTPError(path, 403, "no", {}, None)
        P.kget = broken
        P._LADDER.update(at=0, value=None)
        lad = P.ladder()
        self.assertEqual(0, lad["default"]["value"])
        self.assertEqual("homestead-critical", lad["critical"]["name"])
        self.assertIsNone(P._LADDER["value"], "a failed read is not kept")


class TierOfTests(unittest.TestCase):
    def setUp(self):
        self.lad = P.ladder(X)

    def test_templates_by_class_value(self):
        self.assertEqual(("critical", "x-critical"), P.tier_of({"priorityClassName": "x-critical"}, self.lad))
        self.assertEqual(("normal", "x-normal"), P.tier_of({"priorityClassName": "x-normal"}, self.lad))
        self.assertEqual(("low", "x-low"), P.tier_of({"priorityClassName": "x-low"}, self.lad))
        self.assertEqual(("normal", ""), P.tier_of({}, self.lad))

    def test_homestead_critical_at_1000_is_low_on_this_cluster(self):
        lad = P.ladder(X + [pc("homestead-critical", 1000)])
        self.assertEqual("low", P.tier_of({"priorityClassName": "homestead-critical"}, lad)[0])

    def test_pods_by_their_number(self):
        self.assertEqual("critical", P.tier_of({"priority": 100000, "priorityClassName": "x-critical"}, self.lad)[0])

    def test_old_pods_rank_by_their_own_number(self):
        self.assertEqual("low", P.tier_of({"priority": 0}, self.lad)[0])

    def test_system_and_missing(self):
        lad = P.ladder(X + [pc("system-node-critical", 2_000_001_000)])
        self.assertEqual("system", P.tier_of({"priorityClassName": "system-node-critical"}, lad)[0])
        self.assertEqual(("missing", "gone"), P.tier_of({"priorityClassName": "gone"}, lad))

    def test_with_no_classes_today_s_names_read_as_today(self):
        lad = P._fallback()
        self.assertEqual("critical", P.tier_of({"priorityClassName": "homestead-critical"}, lad)[0])
        self.assertEqual("low", P.tier_of({"priorityClassName": "homestead-low"}, lad)[0])
        self.assertEqual("normal", P.tier_of({"priorityClassName": "homestead-normal"}, lad)[0])

    def test_priority_of_gives_number_and_preemption(self):
        self.assertEqual((1000, False), P.priority_of({"priorityClassName": "x-low"}, self.lad))
        self.assertEqual((10000, True), P.priority_of({}, self.lad))


class LadderWriteTests(unittest.TestCase):
    def setUp(self):
        keep_bindings(self)
        # The ladder is cached for a few seconds; no test may leave its cluster behind.
        self.addCleanup(lambda: P._LADDER.update(at=0, value=None))
        self.posted, self.patched = [], []
        self.items = list(X)
        def kget(path):
            if path == "/apis/scheduling.k8s.io/v1/priorityclasses":
                return {"items": self.items}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        def ksend(method, path, body=None, ctype=None):
            (self.posted if method == "POST" else self.patched).append((path, body))
            return {}
        P.bind(kget, ksend, lambda dep: {}, ("lab", "homestead"), set())
        P._LADDER.update(at=0, value=None)

    def test_critical_writes_the_clusters_critical_class(self):
        app = {"kind": "app", "ns": "lab", "name": "ha", "tier": "critical"}
        P.patch(app)
        self.assertEqual("x-critical", self.patched[0][1]["spec"]["template"]["spec"]["priorityClassName"])

    def test_normal_writes_no_class(self):
        P.patch({"kind": "app", "ns": "lab", "name": "ha", "tier": "normal"})
        self.assertIsNone(self.patched[0][1]["spec"]["template"]["spec"]["priorityClassName"])

    def test_a_class_is_made_only_when_none_fits_with_labels_and_a_description(self):
        self.items = [pc("x-normal", 10000, default=True), pc("homestead-critical", 1000)]
        P._LADDER.update(at=0, value=None)
        P.ensure(("critical",))
        body = self.posted[0][1]
        self.assertEqual("homestead-critical-11000", body["metadata"]["name"])
        self.assertEqual(11000, body["value"])
        self.assertFalse(body["globalDefault"])
        self.assertEqual("priority", body["metadata"]["labels"]["homestead.io/task"])
        self.assertIn("x-normal (10000)", body["description"])

    def test_nothing_is_made_when_the_cluster_has_the_class(self):
        P.ensure(("critical", "low"))
        self.assertEqual([], self.posted)

    def test_edit_leaves_a_system_or_missing_class_alone(self):
        for cls in ("system-node-critical", "gone"):
            self.items = X + [pc("system-node-critical", 2_000_001_000)]
            P._LADDER.update(at=0, value=None)
            spec = {"priorityClassName": cls}
            WORKLOAD_EDIT._apply_priority(spec, "critical")
            self.assertEqual(cls, spec["priorityClassName"])

    def test_the_list_warns_about_a_stale_homestead_class_still_in_use(self):
        self.items = [pc("x-normal", 10000, default=True), pc("homestead-critical", 1000)]
        P._LADDER.update(at=0, value=None)
        dep = {"metadata": {"namespace": "lab", "name": "ha"},
               "spec": {"template": {"spec": {"priorityClassName": "homestead-critical", "containers": []}}}}
        with mock.patch.object(P, "_workloads", lambda: iter([("app", dep)])):
            warnings = P.plan()["warnings"]
        self.assertTrue(any("homestead-critical" in w and "lab/ha" in w for w in warnings), warnings)

    def test_the_list_warns_about_a_missing_class(self):
        dep = {"metadata": {"namespace": "lab", "name": "ha"},
               "spec": {"template": {"spec": {"priorityClassName": "gone", "containers": []}}}}
        with mock.patch.object(P, "_workloads", lambda: iter([("app", dep)])):
            out = P.plan()
        self.assertTrue(any("gone" in w for w in out["warnings"]))
        self.assertIn("lab/ha", [s["id"] for s in out["skipped"]])


class ProtectTests(unittest.TestCase):
    def setUp(self):
        keep_bindings(self)
        self.addCleanup(lambda: P._LADDER.update(at=0, value=None))
        self.items, self.patched, self.posted = list(X), [], []
        self.own = {"metadata": {"namespace": "lab", "name": "homestead", "annotations": {}},
                    "spec": {"template": {"spec": {"containers": []}}}}
        self.pods = []
        def kget(path):
            if path == "/apis/scheduling.k8s.io/v1/priorityclasses":
                return {"items": self.items}
            if path == "/apis/apps/v1/namespaces/lab/deployments/homestead":
                return self.own
            if path == "/api/v1/pods":
                return {"items": self.pods}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        def ksend(method, path, body=None, ctype=None):
            (self.posted if method == "POST" else self.patched).append(body)
            return {}
        P.bind(kget, ksend, lambda dep: {}, ("lab", "homestead"), {"kube-system"})
        P._LADDER.update(at=0, value=None)

    def app(self, priority, ns="lab"):
        self.pods.append({"metadata": {"namespace": ns}, "spec": {"priority": priority}})

    def test_protect_does_nothing_until_an_app_ranks_above_normal(self):
        self.app(10000)
        self.assertIsNone(P.protect())
        self.assertEqual([], self.patched)

    def test_protect_joins_the_critical_class_and_marks_it(self):
        self.app(100000)
        P.protect()
        spec = self.patched[0]["spec"]["template"]["spec"]
        self.assertEqual("x-critical", spec["priorityClassName"])
        self.assertEqual("x-critical", self.patched[0]["metadata"]["annotations"]["homestead.io/priority-protect"])

    def test_protect_ranks_above_any_app_class(self):
        self.items = X + [pc("x-infra", 500000)]
        self.app(500000)
        P.protect()
        self.assertEqual("x-infra", self.patched[0]["spec"]["template"]["spec"]["priorityClassName"])

    def test_a_marked_class_below_the_target_moves_up(self):
        self.own["spec"]["template"]["spec"]["priorityClassName"] = "x-low"
        self.own["metadata"]["annotations"]["homestead.io/priority-protect"] = "x-low"
        self.app(100000)
        P.protect()
        self.assertEqual("x-critical", self.patched[0]["spec"]["template"]["spec"]["priorityClassName"])

    def test_a_class_someone_set_is_left_alone(self):
        self.own["spec"]["template"]["spec"]["priorityClassName"] = "x-low"
        self.app(100000)
        self.assertIsNone(P.protect())

    def test_the_old_homestead_critical_at_1000_below_the_default_is_adopted(self):
        self.items = X + [pc("homestead-critical", 1000)]
        self.own["spec"]["template"]["spec"]["priorityClassName"] = "homestead-critical"
        self.app(100000)
        P.protect()
        self.assertEqual("x-critical", self.patched[0]["spec"]["template"]["spec"]["priorityClassName"])

    def test_it_waits_for_jobs(self):
        self.app(100000)
        ops = mock.Mock(TERMINAL={"succeeded"}, snapshot=lambda: [{"status": "running"}])
        with mock.patch.object(P, "operations", ops):
            self.assertIsNone(P.protect())

    def test_system_namespace_pods_do_not_count(self):
        self.app(100000, ns="kube-system")
        self.assertIsNone(P.protect())


class OwnershipTests(unittest.TestCase):
    """#429: apps Homestead doesn't own keep their tier where they're applied."""
    def edit(self, body, owner):
        dep = deployment("paperless")
        with mock.patch.object(server.WORKLOADS, "kget", return_value=dep), \
                mock.patch.object(server.WORKLOADS, "guard_managed_smb"), \
                mock.patch.object(server.WORKLOADS, "guard_self"), \
                mock.patch.object(server.WORKLOADS.UPDATES, "managed_by", return_value=owner), \
                mock.patch.object(server.WORKLOADS, "edit_capacity_plan", return_value=({"deployment": dep}, {}, {})), \
                mock.patch.object(server.WORKLOADS.CAPACITY_REVIEW, "enforce"), \
                mock.patch.object(server.WORKLOADS.RESTRUCTURE, "copies", return_value=[]), \
                mock.patch.object(server.WORKLOADS, "apply_reviewed_edit", return_value={"ok": True, "detail": "Saved"}), \
                mock.patch.object(server.WORKLOADS.PRIORITY, "ensure", return_value=[]):
            return server.WORKLOADS.edit(mock.Mock(body={"ns": "lab", "name": "paperless", **body}))

    def test_edit_refuses_a_tier_change_on_an_app_flux_applies(self):
        flux = {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}
        with self.assertRaisesRegex(ValueError, "Flux"):
            self.edit({"priority": "critical"}, flux)
        self.assertTrue(self.edit({"priority": "normal"}, flux)["ok"], "an unchanged tier is accepted")

    def test_edit_still_sets_a_tier_on_an_app_homestead_owns(self):
        self.assertTrue(self.edit({"priority": "critical"}, {})["ok"])

    def test_the_edit_payload_names_who_owns_the_tier(self):
        dep = deployment("paperless")
        dep["metadata"].setdefault("labels", {})["kustomize.toolkit.fluxcd.io/name"] = "apps"
        with mock.patch.object(WORKLOAD_EDIT, "seed_configs", return_value=[]):
            payload = server.WORKLOADS.workload_edit_payload("lab", "paperless", dep, hardware_definitions=[], services=[])
        self.assertEqual("Flux", payload["priority_owner"])


class ReviewFixTests(ProtectTests):
    """The final review's findings (#429)."""
    LONGHORN = [pc("longhorn-critical", 1_000_000_000), pc("system-cluster-critical", 2_000_000_000),
                pc("homestead-critical", 1000), pc("homestead-low", -1000, never=True)]

    def test_longhorns_class_is_never_critical(self):
        lad = P.ladder(self.LONGHORN)
        self.assertEqual({"name": "homestead-critical", "value": 1000, "create": False}, lad["critical"])
        self.assertEqual("system", P.tier_of({"priorityClassName": "longhorn-critical"}, lad)[0])

    def test_a_platform_named_class_is_never_offered(self):
        lad = P.ladder([pc("harvester-high", 5000)])
        self.assertEqual("system", P.tier_of({"priorityClassName": "harvester-high"}, lad)[0])
        self.assertTrue(lad["critical"]["create"])

    def broken_list(self):
        real = P.kget
        def kget(path):
            if path == "/apis/scheduling.k8s.io/v1/priorityclasses":
                raise urllib.error.HTTPError(path, 500, "busy", {}, None)
            return real(path)
        P.kget = kget
        P._LADDER.update(at=0, value=None)

    def test_protect_does_nothing_when_the_classes_cannot_be_read(self):
        self.broken_list()
        self.app(10000)
        self.assertIsNone(P.protect())
        self.assertEqual([], self.patched)
        self.assertEqual([], self.posted)

    def test_an_unreadable_class_list_marks_no_class_missing(self):
        self.broken_list()
        lad = P.ladder()
        self.assertEqual("unknown", P.tier_of({"priorityClassName": "x-normal"}, lad)[0])

    def test_the_job_makes_no_class_when_the_list_failed_for_another_reason(self):
        self.broken_list()
        with self.assertRaisesRegex(ValueError, "couldn't read"):
            P.ensure(("critical",))
        self.assertEqual([], self.posted)

    def test_the_old_class_below_the_default_is_removed_even_with_no_app_above(self):
        self.items = X + [pc("homestead-critical", 1000)]
        self.own["spec"]["template"]["spec"]["priorityClassName"] = "homestead-critical"
        self.app(10000)
        self.assertIsNotNone(P.protect())
        sent = self.patched[0]
        self.assertIsNone(sent["spec"]["template"]["spec"]["priorityClassName"])
        self.assertIsNone(sent["metadata"]["annotations"]["homestead.io/priority-protect"])

    def test_the_old_class_is_adopted_with_no_default_too(self):
        self.items = [pc("homestead-critical", 1000), pc("x-critical", 100000)]
        self.own["spec"]["template"]["spec"]["priorityClassName"] = "homestead-critical"
        self.app(100000)
        P.protect()
        self.assertEqual("x-critical", self.patched[0]["spec"]["template"]["spec"]["priorityClassName"])

    def test_protect_says_nothing_when_no_critical_class_can_be_made(self):
        # Both names Homestead could use are taken, at values it can't use.
        self.items = [pc("x-normal", 10000, default=True), pc("homestead-critical", 1000),
                      pc("homestead-critical-11000", 11000, never=True), pc("x-patient", 50000, never=True)]
        self.app(50000)
        self.assertIsNone(P.protect())
        self.assertEqual([], self.patched)

    def test_a_class_homestead_makes_stays_below_the_platforms(self):
        lad = P.ladder([pc("x-normal", 999_999_500, default=True)])
        self.assertEqual(999_999_999, lad["critical"]["value"])

    def test_the_list_warns_when_homestead_ranks_below_an_app(self):
        self.own["spec"]["template"]["spec"]["priorityClassName"] = "x-low"
        app = {"metadata": {"namespace": "lab", "name": "vault"},
               "spec": {"template": {"spec": {"priorityClassName": "x-critical", "containers": []}}}}
        with mock.patch.object(P, "_workloads", lambda: iter([("app", self.own), ("app", app)])):
            warnings = P.plan()["warnings"]
        self.assertTrue(any("Homestead" in w and "lab/vault" in w for w in warnings), warnings)

    def test_correct_setups_get_no_stale_class_warning(self):
        legacy = {"metadata": {"namespace": "lab", "name": "web"},
                  "spec": {"template": {"spec": {"priorityClassName": "homestead-normal", "containers": []}}}}
        self.items = [pc("homestead-normal", 0)]
        P._LADDER.update(at=0, value=None)
        with mock.patch.object(P, "_workloads", lambda: iter([("app", legacy)])):
            self.assertEqual([], P.plan()["warnings"])
        self.items = [pc("homestead-critical", 1000, default=True)]
        P._LADDER.update(at=0, value=None)
        dflt = {"metadata": {"namespace": "lab", "name": "db"},
                "spec": {"template": {"spec": {"priorityClassName": "homestead-critical", "containers": []}}}}
        with mock.patch.object(P, "_workloads", lambda: iter([("app", dflt)])):
            self.assertEqual([], P.plan()["warnings"])
