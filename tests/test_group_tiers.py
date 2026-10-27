"""#430: a tier for a group of apps, and where each app's tier comes from."""
import copy
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_group_tiers as GROUPS
import homestead_priority as P
import homestead_route_policy as ROUTE_POLICY
import homestead_routes as ROUTER
import homestead_updates as UPDATES
# Binds every module as the app does, before any test runs, so use() always
# puts the app's own bindings back - never a module's unbound defaults.
import server  # noqa: F401

NS = "homestead"
MAP = f"/api/v1/namespaces/{NS}/configmaps/homestead-group-tiers"
FLUX = {"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
HELM = {"helm.toolkit.fluxcd.io/name": "ha", "helm.toolkit.fluxcd.io/namespace": "flux-system"}
ARGO = {"argocd.argoproj.io/tracking-id": "home:apps/Deployment:lab/db"}


def http(code, path="x"):
    return urllib.error.HTTPError(path, code, "refused", {}, io.BytesIO())


def pc(name, value, default=False, never=False):
    c = {"metadata": {"name": name}, "value": value}
    if default:
        c["globalDefault"] = True
    if never:
        c["preemptionPolicy"] = "Never"
    return c


CLASSES = [pc("x-critical", 100000), pc("x-normal", 10000, default=True), pc("x-low", 1000, never=True)]
LAD = P.ladder(CLASSES)


def app(name, ns="lab", group="", cls="", follows=False, wrote="", labels=None, ann=None):
    annotations = dict(ann or {})
    if group:
        annotations["homestead.io/group"] = group
    if follows:
        annotations["homestead.io/tier"] = "group"
    if wrote:
        annotations["homestead.io/tier-class"] = wrote
    spec = {"containers": [{"name": "app", "image": "example.com/app:1"}]}
    if cls:
        spec["priorityClassName"] = cls
    return {"metadata": {"name": name, "namespace": ns, "annotations": annotations, "labels": labels or {}, "generation": 1},
            "spec": {"replicas": 1, "template": {"spec": spec}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1}}


class Cluster:
    """Deployments, VMs, priority classes and the group-tiers ConfigMap,
    answered and changed as the API server does: a merge patch answers with
    the object, and a PUT over a stale resourceVersion is a 409."""

    def __init__(self, deps, tiers=None, raw=None):
        self.deps = {f"{d['metadata']['namespace']}/{d['metadata']['name']}": d for d in deps}
        self.vms, self.sent, self.unreadable, self.race, self.other = {}, [], None, False, None
        self.stss = {}
        self.cm = None
        if tiers is not None or raw is not None:
            self.cm = {"metadata": {"name": "homestead-group-tiers", "namespace": NS, "resourceVersion": "1"},
                       "data": {"group-tiers.json": raw if raw is not None else json.dumps(tiers)}}

    def kget(self, path, **_):
        if path == "/apis/scheduling.k8s.io/v1/priorityclasses":
            return {"items": copy.deepcopy(CLASSES)}
        if path == "/apis/apps/v1/deployments":
            return {"items": [copy.deepcopy(d) for d in self.deps.values()]}
        if path == P.VMS:
            return {"items": [copy.deepcopy(v) for v in self.vms.values()]}
        if path == "/apis/apps/v1/statefulsets":
            return {"items": [copy.deepcopy(s) for s in self.stss.values()]}
        if path == MAP:
            if self.unreadable:
                raise self.unreadable
            if self.cm is None:
                raise http(404, path)
            return copy.deepcopy(self.cm)
        if "/deployments/" in path:
            ns, name = path.split("/namespaces/")[1].split("/deployments/")
            if f"{ns}/{name}" not in self.deps:
                raise http(404, path)
            return copy.deepcopy(self.deps[f"{ns}/{name}"])
        if self.other is not None:
            return copy.deepcopy(self.other)
        raise http(404, path)

    def ksend(self, method, path, body=None, ctype="application/json", **_):
        self.sent.append((method, path, copy.deepcopy(body), ctype))
        if "/deployments/" in path and method == "PATCH":
            ns, name = path.split("/namespaces/")[1].split("/deployments/")
            dep = self.deps[f"{ns}/{name}"]
            annotations = dep["metadata"].setdefault("annotations", {})
            for key, value in ((body.get("metadata") or {}).get("annotations") or {}).items():
                if value is None:
                    annotations.pop(key, None)
                else:
                    annotations[key] = value
            for key, value in (((body.get("spec") or {}).get("template") or {}).get("spec") or {}).items():
                pod = dep["spec"]["template"]["spec"]
                if value is None:
                    pod.pop(key, None)
                else:
                    pod[key] = value
            return copy.deepcopy(dep)
        if method == "POST" and path == f"/api/v1/namespaces/{NS}/configmaps":
            if self.cm is not None:
                raise http(409, path)
            self.cm = copy.deepcopy(body)
            self.cm["metadata"]["resourceVersion"] = "1"
            return copy.deepcopy(self.cm)
        if method == "PUT" and path == MAP:
            if self.race or body["metadata"].get("resourceVersion") != self.cm["metadata"]["resourceVersion"]:
                raise http(409, path)
            version = str(int(self.cm["metadata"]["resourceVersion"]) + 1)
            self.cm = copy.deepcopy(body)
            self.cm["metadata"]["resourceVersion"] = version
            return copy.deepcopy(self.cm)
        raise AssertionError(f"unexpected {method} {path}")

    def tiers(self):
        return None if self.cm is None else json.loads(self.cm["data"]["group-tiers.json"])

    def annotations(self, key):
        return self.deps[key]["metadata"].get("annotations") or {}

    def deployment_writes(self):
        return [s for s in self.sent if "/deployments/" in s[1]]


def use(case, cluster):
    """Binds a fake cluster into homestead_group_tiers and homestead_priority,
    and puts both modules' real bindings back after the test: other modules'
    tests read tiers through them."""
    saved_groups = {name: getattr(GROUPS, name) for name in GROUPS.NEEDS + ("NAMESPACE", "OWN_GROUP", "managed_by")}
    saved_priority = (P.kget, P.ksend, P.managed_by, P.own, P.system_ns)

    def restore():
        for name, value in saved_groups.items():
            setattr(GROUPS, name, value)
        P.kget, P.ksend, P.managed_by, P.own, P.system_ns = saved_priority
        P._LADDER.update(at=0, value=None)
    case.addCleanup(restore)
    GROUPS.bind({"kget": cluster.kget, "ksend": cluster.ksend, "guard_managed_smb": lambda ns, name: None,
                 "_dns_name": lambda value, label="name": value,
                 "is_self": lambda ns, name: (ns, name) == ("homestead", "homestead")},
                namespace=NS, own_group="Homestead", managed=UPDATES.managed_by)
    P.bind(cluster.kget, cluster.ksend, UPDATES.managed_by, ("homestead", "homestead"), {"kube-system"}, lambda: True)
    P._LADDER.update(at=0, value=None)


class MapTests(unittest.TestCase):
    def test_no_map_yet_is_no_group_tiers(self):
        use(self, Cluster([]))
        self.assertEqual({}, GROUPS.stored())
        self.assertEqual(({}, None), GROUPS.read())

    def test_the_map_is_read_from_homesteads_namespace(self):
        use(self, Cluster([], {"Home": "critical"}))
        self.assertEqual(({"Home": "critical"}, "1"), GROUPS.read())

    def test_entries_that_arent_tiers_are_dropped(self):
        use(self, Cluster([], raw='{"Home": "critical", "Media": "urgent", "Lab": 3}'))
        self.assertEqual({"Home": "critical"}, GROUPS.stored())

    def test_a_map_that_cant_be_read_is_unknown_not_empty(self):
        cluster = Cluster([], {"Home": "critical"})
        cluster.unreadable = http(403, MAP)
        use(self, cluster)
        self.assertIsNone(GROUPS.stored())

    def test_a_map_that_isnt_a_map_of_tiers_is_unknown(self):
        for raw in ("not json", '["Home"]'):
            with self.subTest(raw=raw):
                use(self, Cluster([], raw=raw))
                self.assertIsNone(GROUPS.stored())
                self.assertEqual((None, "1"), GROUPS.read())

    def test_stored_reads_through_the_reader_it_is_given(self):
        # The plan and Edit read through their own module's kget.
        use(self, Cluster([], {"Home": "low"}))
        other = Cluster([], {"Home": "critical"})
        self.assertEqual({"Home": "critical"}, GROUPS.stored(other.kget))


class ResolveTests(unittest.TestCase):
    def test_no_marker_is_a_tier_of_its_own_even_in_a_group_with_one(self):
        self.assertEqual({"tier": "low", "class": "x-low", "source": "own", "runs": "low"},
                         GROUPS.resolve(app("db", group="Home", cls="x-low"), {"Home": "critical"}, LAD))

    def test_no_class_and_no_marker_is_normal_of_its_own(self):
        self.assertEqual({"tier": "normal", "class": None, "source": "own", "runs": "normal"},
                         GROUPS.resolve(app("web"), {}, LAD))

    def test_the_marker_follows_the_groups_tier(self):
        self.assertEqual({"tier": "critical", "class": "x-critical", "source": "group", "runs": "normal"},
                         GROUPS.resolve(app("ha", group="Home", follows=True), {"Home": "critical"}, LAD))

    def test_the_marker_in_a_group_with_no_tier_is_the_default(self):
        self.assertEqual({"tier": "normal", "class": None, "source": "default", "runs": "critical"},
                         GROUPS.resolve(app("ha", group="Home", follows=True, cls="x-critical"), {"Media": "low"}, LAD))

    def test_the_marker_with_no_group_is_the_default(self):
        self.assertEqual("default", GROUPS.resolve(app("ha", follows=True), {"Home": "critical"}, LAD)["source"])

    def test_a_class_alone_never_means_from_the_group(self):
        # x-critical is what Home's tier gives; without the marker it is still the app's own.
        self.assertEqual("own", GROUPS.resolve(app("ha", group="Home", cls="x-critical"), {"Home": "critical"}, LAD)["source"])

    def test_an_unreadable_map_leaves_a_follower_unknown_and_an_own_tier_known(self):
        self.assertEqual({"tier": None, "class": None, "source": "group", "runs": "normal"},
                         GROUPS.resolve(app("ha", group="Home", follows=True), None, LAD))
        self.assertEqual("low", GROUPS.resolve(app("db", group="Home", cls="x-low"), None, LAD)["tier"])
        self.assertEqual("default", GROUPS.resolve(app("ha", follows=True), None, LAD)["source"],
                         "no group: the map isn't needed")

    def test_vms_are_always_their_own(self):
        vm = app("win", group="Home", follows=True)   # annotations a person put on a VM by hand
        self.assertEqual("own", GROUPS.resolve(vm, {"Home": "critical"}, LAD, kind="vm")["source"])

    def test_statefulsets_are_always_their_own(self):
        sts = app("postgres", group="Home", follows=True)   # annotations a person put on a StatefulSet by hand
        self.assertEqual({"tier": "normal", "class": None, "source": "own", "runs": "normal"},
                         GROUPS.resolve(sts, {"Home": "critical"}, LAD, kind="sts"))

    def test_an_app_flux_applies_is_compared_with_its_groups_tier(self):
        dep = app("web", group="Home", labels=FLUX)
        self.assertEqual({"tier": "critical", "class": "x-critical", "source": "group", "runs": "normal"},
                         GROUPS.resolve(dep, {"Home": "critical"}, LAD, managed=True))
        self.assertEqual("own", GROUPS.resolve(dep, {}, LAD, managed=True)["source"])

    def test_a_marker_on_an_app_flux_applies_is_not_followed(self):
        dep = app("web", group="Home", follows=True, cls="x-low", labels=FLUX)
        self.assertEqual({"tier": "low", "class": "x-low", "source": "own", "runs": "low"},
                         GROUPS.resolve(dep, {}, LAD, managed=True))


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster([app("ha"), app("db", cls="x-critical"), app("web", labels=FLUX),
                                app("homestead", ns="homestead"),
                                app("frigate", group="Home", follows=True),
                                app("plex", group="Media", follows=True, cls="x-low", wrote="x-low")],
                               {"Home": "critical", "Media": "low"})
        use(self, self.cluster)

    def move(self, group, *names, **extra):
        items = [{"ns": "homestead" if n == "homestead" else "lab", "name": n} for n in names]
        return GROUPS.set_workload_groups({"group": group, "items": items, **extra})

    def test_joining_a_group_follows_it(self):
        result = self.move("Home", "ha")
        self.assertEqual({"homestead.io/group": "Home", "homestead.io/tier": "group"}, self.cluster.annotations("lab/ha"))
        self.assertEqual(1, result["following"])

    def test_an_app_with_a_class_of_its_own_joins_without_following(self):
        self.move("Home", "db")
        self.assertEqual({"homestead.io/group": "Home"}, self.cluster.annotations("lab/db"))

    def test_an_app_flux_applies_joins_without_following(self):
        self.move("Home", "web")
        self.assertEqual({"homestead.io/group": "Home"}, self.cluster.annotations("lab/web"))

    def test_homestead_itself_joins_without_following(self):
        self.move("Home", "homestead")
        self.assertEqual({"homestead.io/group": "Home"}, self.cluster.annotations("homestead/homestead"))

    def test_joining_touches_no_template(self):
        before = copy.deepcopy(self.cluster.deps["lab/ha"]["spec"])
        self.move("Home", "ha", "db")
        self.assertEqual(before, self.cluster.deps["lab/ha"]["spec"])
        self.assertTrue(all(set(body) == {"metadata"} for _, _, body, _ in self.cluster.deployment_writes()))

    def test_ungrouping_removes_the_marker_and_leaves_the_class(self):
        self.move("", "plex")
        self.assertEqual({}, self.cluster.annotations("lab/plex"))
        self.assertEqual("x-low", self.cluster.deps["lab/plex"]["spec"]["template"]["spec"]["priorityClassName"])

    def test_moving_to_another_group_keeps_the_marker(self):
        self.move("Media", "frigate")
        self.assertEqual({"homestead.io/group": "Media", "homestead.io/tier": "group"}, self.cluster.annotations("lab/frigate"))

    def test_a_group_that_ends_loses_its_tier(self):
        self.move("Media", "frigate")          # Home's only app
        self.assertEqual({"Media": "low"}, self.cluster.tiers())

    def test_a_group_that_still_has_apps_keeps_its_tier(self):
        self.move("Home", "ha")
        self.move("Media", "frigate")
        self.assertEqual({"Home": "critical", "Media": "low"}, self.cluster.tiers())

    def test_renaming_moves_the_tier_to_the_new_name(self):
        self.move("House", "frigate", renamed_from="Home")
        self.assertEqual({"House": "critical", "Media": "low"}, self.cluster.tiers())
        self.assertEqual("group", self.cluster.annotations("lab/frigate")["homestead.io/tier"])

    def test_a_rename_makes_no_member_a_follower(self):
        self.cluster = Cluster([app("frigate", group="Home", follows=True), app("ha", group="Home")], {"Home": "critical"})
        use(self, self.cluster)
        result = self.move("House", "frigate", "ha", renamed_from="Home")
        self.assertEqual({"homestead.io/group": "House", "homestead.io/tier": "group"}, self.cluster.annotations("lab/frigate"))
        self.assertEqual({"homestead.io/group": "House"}, self.cluster.annotations("lab/ha"), "it kept its own tier")
        self.assertEqual([{"metadata": {"annotations": {"homestead.io/group": "House"}}}] * 2,
                         [body for _, _, body, _ in self.cluster.deployment_writes()])
        self.assertEqual(0, result["following"])

    def test_renaming_into_a_group_with_a_tier_keeps_that_tier(self):
        self.move("Media", "frigate", renamed_from="Home")
        self.assertEqual({"Media": "low"}, self.cluster.tiers())

    def test_a_change_of_groups_still_works_when_the_tiers_cant_be_read(self):
        self.cluster.unreadable = http(403, MAP)
        result = self.move("Home", "ha")
        self.assertEqual("group", self.cluster.annotations("lab/ha")["homestead.io/tier"])
        self.assertIn("couldn't", result["detail"])

    def test_a_managed_add_on_is_refused_and_nothing_is_patched(self):
        def refuse(ns, name):
            raise ValueError("managed by Homestead")
        GROUPS.guard_managed_smb = refuse     # restored by use()
        with self.assertRaisesRegex(ValueError, "managed by Homestead"):
            self.move("Home", "ha")
        self.assertEqual([], self.cluster.sent)

    def test_names_are_checked_before_anything_is_written(self):
        with self.assertRaises(ValueError):
            self.move("Home", "ha", renamed_from="x" * 41)
        self.assertEqual([], self.cluster.sent)


class GroupTierRouteTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster([app("ha", group="Home", follows=True),
                                app("frigate", group="Home", follows=True, cls="x-critical", wrote="x-critical"),
                                app("db", group="Home", cls="x-low"), app("web", group="Home", labels=FLUX)], {})
        use(self, self.cluster)

    def test_setting_a_tier_writes_only_the_map(self):
        result = GROUPS.set_group_tier({"group": "Home", "tier": "critical"})
        self.assertEqual({"Home": "critical"}, self.cluster.tiers())
        self.assertEqual([], self.cluster.deployment_writes(), "nothing restarts")
        # ha and frigate follow; ha runs normal, so it needs a restart; web is Flux's, at normal.
        self.assertEqual((2, 1, 1), (result["follow"], result["restart"], result["git"]))
        self.assertEqual("critical", result["tier"])

    def test_none_removes_the_groups_tier(self):
        self.cluster.cm["data"]["group-tiers.json"] = json.dumps({"Home": "critical", "Media": "low"})
        result = GROUPS.set_group_tier({"group": "Home", "tier": "none"})
        self.assertEqual({"Media": "low"}, self.cluster.tiers())
        self.assertEqual(("", 1), (result["tier"], result["restart"]), "frigate runs critical; the default is normal")

    def test_the_first_save_makes_the_map(self):
        self.cluster.cm = None
        GROUPS.set_group_tier({"group": "Home", "tier": "low"})
        method, path, body, _ = self.cluster.sent[-1]
        self.assertEqual(("POST", f"/api/v1/namespaces/{NS}/configmaps"), (method, path))
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        self.assertEqual({"Home": "low"}, self.cluster.tiers())

    def test_a_group_name_is_saved_as_groups_are_named(self):
        GROUPS.set_group_tier({"group": "  Home  ", "tier": "low"})
        self.assertEqual({"Home": "low"}, self.cluster.tiers())

    def test_homesteads_group_is_refused(self):
        with self.assertRaisesRegex(ValueError, "its own tier"):
            GROUPS.set_group_tier({"group": "Homestead", "tier": "critical"})
        self.assertEqual([], self.cluster.sent)

    def test_a_group_with_no_apps_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no apps"):
            GROUPS.set_group_tier({"group": "Garage", "tier": "critical"})
        self.assertEqual([], self.cluster.sent)

    def test_only_the_three_tiers_or_none(self):
        with self.assertRaises(ValueError):
            GROUPS.set_group_tier({"group": "Home", "tier": "urgent"})

    def test_a_save_that_crosses_another_is_a_409_and_merges_nothing(self):
        self.cluster.cm["data"]["group-tiers.json"] = json.dumps({"Home": "low"})
        self.cluster.race = True
        result = GROUPS.set_group_tier({"group": "Home", "tier": "critical"})
        self.assertIsInstance(result, ROUTER.Reply)
        self.assertEqual(409, result.code)
        self.assertIn("changed elsewhere", result.body["error"])
        self.assertEqual({"Home": "low"}, self.cluster.tiers())

    def test_a_map_that_isnt_json_is_replaced_by_a_save(self):
        self.cluster.cm["data"]["group-tiers.json"] = "not json"
        GROUPS.set_group_tier({"group": "Home", "tier": "critical"})
        self.assertEqual({"Home": "critical"}, self.cluster.tiers())

    def test_setting_a_groups_tier_takes_the_role_setting_an_apps_tier_does(self):
        self.assertEqual("operator", ROUTE_POLICY.role("/api/workloads/group-tier", "POST"))
        for route in (("POST", "/api/workloads/group"), ("POST", "/api/workloads/group-tier")):
            self.assertEqual("operator", GROUPS.ROUTES[route][0])
        self.assertEqual("operator", ROUTE_POLICY.role("/api/workloads/group", "POST"))
        self.assertEqual(ROUTE_POLICY.role("/api/workloads/priority", "POST"),
                         ROUTE_POLICY.role("/api/workloads/group-tier", "POST"))


class PlanTests(unittest.TestCase):
    def plan(self, deps, tiers, chosen=None, vms=None, unreadable=None):
        self.cluster = Cluster(deps, tiers)
        self.cluster.vms = vms or {}
        self.cluster.unreadable = unreadable
        use(self, self.cluster)
        return P.plan(chosen)

    def row(self, out, app_id):
        return next(a for a in out["apps"] if a["id"] == app_id)

    def test_following_a_group_set_to_critical_needs_a_restart_and_is_ticked(self):
        out = self.plan([app("ha", group="Home", follows=True)], {"Home": "critical"})
        row = self.row(out, "lab/ha")
        self.assertEqual(("normal", "critical", "group", "restart", "critical", "Home", True),
                         (row["current"], row["wanted"], row["source"], row["status"], row["tier"], row["group"], row["follows"]))
        self.assertEqual(1, out["changes"])

    def test_an_app_that_runs_with_its_groups_tier_is_in_line(self):
        out = self.plan([app("ha", group="Home", follows=True, cls="x-critical", wrote="x-critical")], {"Home": "critical"})
        self.assertEqual(("in_line", "critical"), (self.row(out, "lab/ha")["status"], self.row(out, "lab/ha")["tier"]))

    def test_a_class_changed_outside_homestead_is_shown_not_ticked(self):
        out = self.plan([app("ha", group="Home", follows=True, cls="x-low", wrote="x-critical")], {"Home": "critical"})
        row = self.row(out, "lab/ha")
        self.assertEqual(("outside", "low"), (row["status"], row["tier"]))
        self.assertEqual(0, out["changes"])

    def test_a_class_homestead_wrote_before_the_group_changed_needs_a_restart(self):
        out = self.plan([app("ha", group="Home", follows=True, cls="x-critical", wrote="x-critical")], {"Home": "low"})
        self.assertEqual(("restart", "low"), (self.row(out, "lab/ha")["status"], self.row(out, "lab/ha")["tier"]))

    def test_apps_flux_or_argo_apply_get_the_line_to_commit(self):
        out = self.plan([app("web", group="Home", labels=FLUX), app("ha", group="Home", labels=HELM),
                         app("db", group="Home", ann=ARGO)], {"Home": "critical"})
        self.assertEqual("spec.template.spec.priorityClassName: x-critical", self.row(out, "lab/web")["line"])
        self.assertEqual("priorityClassName: x-critical", self.row(out, "lab/ha")["line"])
        self.assertEqual("spec.template.spec.priorityClassName: x-critical", self.row(out, "lab/db")["line"])
        self.assertEqual({"git"}, {a["status"] for a in out["apps"]})
        self.assertEqual(0, out["changes"], "none is ticked")

    def test_an_app_flux_applies_moved_to_normal_is_told_to_remove_its_class(self):
        out = self.plan([app("web", group="Home", cls="x-critical", labels=FLUX)], {"Home": "normal"})
        self.assertEqual("remove spec.template.spec.priorityClassName", self.row(out, "lab/web")["line"])

    def test_an_unreadable_map_leaves_followers_unknown_and_acts_on_none(self):
        out = self.plan([app("ha", group="Home", follows=True), app("db", group="Home", cls="x-low")],
                        {"Home": "critical"}, unreadable=http(500, MAP))
        self.assertEqual(("unknown_group", "normal"), (self.row(out, "lab/ha")["status"], self.row(out, "lab/ha")["tier"]))
        self.assertEqual("in_line", self.row(out, "lab/db")["status"])
        self.assertEqual([{"name": "Home", "tier": None, "apps": 2}], out["groups"])
        self.assertEqual(0, out["changes"])

    def test_a_follower_with_a_class_that_isnt_on_the_cluster_cant_be_set(self):
        out = self.plan([app("ha", group="Home", follows=True, cls="gone")], {"Home": "critical"})
        self.assertEqual(["lab/ha"], [s["id"] for s in out["skipped"]], "cant_set: the skipped list, as today")
        self.assertEqual([], out["apps"])

    def test_vms_are_always_their_own(self):
        win = {"metadata": {"name": "win", "namespace": "lab",
                            "annotations": {"homestead.io/group": "Home", "homestead.io/tier": "group"}},
               "spec": {"template": {"spec": {}}}}
        out = self.plan([], {"Home": "critical"}, vms={"lab/win": win})
        row = self.row(out, "vm:lab/win")
        self.assertEqual(("own", "in_line", "", False), (row["source"], row["status"], row["group"], row["follows"]))

    def test_a_statefulset_never_follows_a_group(self):
        self.cluster = Cluster([], {"Home": "critical"})
        self.cluster.stss = {"lab/x-db": app("x-db", group="Home", follows=True)}
        use(self, self.cluster)
        out = P.plan()
        row = self.row(out, "sts:lab/x-db")
        self.assertEqual(("own", "in_line", "", False, False), (row["source"], row["status"], row["group"], row["follows"], row["follow"]))
        self.assertEqual([], out["groups"], "a StatefulSet is counted in no group")

    def test_groups_are_listed_with_their_tier(self):
        out = self.plan([app("ha", group="Home", follows=True), app("frigate", group="Home"), app("plex", group="Media"),
                         app("homestead", ns="homestead", group="Homestead")], {"Home": "critical"})
        self.assertEqual([{"name": "Home", "tier": "critical", "apps": 2}, {"name": "Media", "tier": "", "apps": 1}], out["groups"])

    def test_rows_out_of_line_come_first(self):
        out = self.plan([app("a-web"), app("z-ha", group="Home", follows=True)], {"Home": "critical"})
        self.assertEqual(["lab/z-ha", "lab/a-web"], [a["id"] for a in out["apps"]])

    def test_hints_tick_only_apps_with_a_tier_of_their_own(self):
        out = self.plan([app("home-assistant", group="Home", follows=True), app("vaultwarden")], {})
        self.assertEqual("normal", self.row(out, "lab/home-assistant")["tier"], "it follows Home, which has no tier")
        self.assertEqual("critical", self.row(out, "lab/vaultwarden")["tier"])

    def test_a_pick_for_an_unknown_group_row_is_ignored_by_the_plan(self):
        out = self.plan([app("ha", group="Home", follows=True)], {"Home": "critical"}, chosen={"lab/ha": "low"}, unreadable=http(500, MAP))
        self.assertEqual(("unknown_group", "normal"), (self.row(out, "lab/ha")["status"], self.row(out, "lab/ha")["tier"]))
        self.assertEqual(0, out["changes"])


class JobTests(unittest.TestCase):
    def review(self, deps, tiers, chosen=None):
        self.cluster = Cluster(deps, tiers)
        use(self, self.cluster)
        out = P.plan(chosen)
        return P.reviewed([a for a in out["apps"] if a["tier"] != a["current"]], out["review_token"])

    def last_patch(self):
        return next(body for method, path, body, _ in reversed(self.cluster.sent) if method == "PATCH")

    def test_the_job_writes_the_marker_and_the_class_it_wrote(self):
        apps = self.review([app("ha", group="Home", follows=True)], {"Home": "critical"})
        P.patch(apps[0])
        self.assertEqual({"homestead.io/group": "Home", "homestead.io/tier": "group", "homestead.io/tier-class": "x-critical"},
                         self.cluster.annotations("lab/ha"))
        self.assertEqual("x-critical", self.cluster.deps["lab/ha"]["spec"]["template"]["spec"]["priorityClassName"])

    def test_a_tier_picked_against_the_group_becomes_the_apps_own(self):
        apps = self.review([app("ha", group="Home", follows=True)], {"Home": "critical"}, chosen={"lab/ha": "low"})
        P.patch(apps[0])
        self.assertEqual({"homestead.io/group": "Home"}, self.cluster.annotations("lab/ha"))
        self.assertEqual("x-low", self.cluster.deps["lab/ha"]["spec"]["template"]["spec"]["priorityClassName"])

    def test_an_own_tier_change_writes_no_annotations(self):
        apps = self.review([app("db")], {}, chosen={"lab/db": "critical"})
        P.patch(apps[0])
        self.assertNotIn("metadata", self.last_patch())

    def test_a_job_reviewed_before_this_version_writes_no_marker(self):
        self.cluster = Cluster([app("ha", group="Home", follows=True)], {"Home": "critical"})
        use(self, self.cluster)
        P.patch({"kind": "app", "ns": "lab", "name": "ha", "tier": "critical"})   # a ref stored before #430
        self.assertNotIn("metadata", self.last_patch())

    def test_plan_rows_say_whether_the_job_keeps_an_app_following(self):
        self.cluster = Cluster([app("ha", group="Home", follows=True), app("fr", group="Home", follows=True), app("db")],
                               {"Home": "critical"})
        use(self, self.cluster)
        out = P.plan({"lab/fr": "low", "lab/db": "low"})
        rows = {a["id"]: a for a in out["apps"]}
        self.assertEqual((True, False, False), (rows["lab/ha"]["follow"], rows["lab/fr"]["follow"], rows["lab/db"]["follow"]))
        flipped = [dict(a, follow=not a["follow"]) if a["id"] == "lab/ha" else a for a in out["apps"] if a["tier"] != a["current"]]
        self.assertNotEqual(out["review_token"], P.review_token(flipped), "the token binds follow")

    def test_a_group_tier_changed_between_review_and_start_is_refused(self):
        self.cluster = Cluster([app("ha", group="Home", follows=True)], {"Home": "critical"})
        use(self, self.cluster)
        out = P.plan()
        changes = [a for a in out["apps"] if a["tier"] != a["current"]]
        self.cluster.cm["data"]["group-tiers.json"] = json.dumps({"Home": "low"})
        with self.assertRaisesRegex(ValueError, "review again"):
            P.reviewed(changes, out["review_token"])

    def test_a_follow_the_server_doesnt_see_is_refused(self):
        self.cluster = Cluster([app("ha", group="Home", follows=True)], {"Home": "critical"})
        use(self, self.cluster)
        changes = [dict(a, follow=False) for a in P.plan()["apps"] if a["tier"] != a["current"]]
        with self.assertRaisesRegex(ValueError, "review again"):
            P.reviewed(changes, P.review_token(changes))

    def turn(self, apps):
        """The job's turn for its first app, the classes already made."""
        ref = {"apps": apps, "index": 0, "done": 0, "stage": "apply", "classes": True}
        return P.status({"ref": ref}), ref

    def test_an_app_ungrouped_before_its_turn_is_skipped_and_gets_no_marker(self):
        apps = self.review([app("ha", group="Home", follows=True)], {"Home": "critical"})
        GROUPS.set_workload_groups({"group": "", "items": [{"ns": "lab", "name": "ha"}]})
        sent = len(self.cluster.sent)
        _, ref = self.turn(apps)
        self.assertEqual(["lab/ha: it changed since the review, so it was left alone"], ref["skipped"])
        self.assertEqual([], self.cluster.sent[sent:], "nothing written")
        self.assertEqual({}, self.cluster.annotations("lab/ha"))

    def test_an_app_moved_to_another_group_before_its_turn_is_skipped(self):
        apps = self.review([app("ha", group="Home", follows=True), app("fr", group="Media")], {"Home": "critical"})
        GROUPS.set_workload_groups({"group": "Media", "items": [{"ns": "lab", "name": "ha"}]})
        _, ref = self.turn(apps)
        self.assertEqual(["lab/ha: it changed since the review, so it was left alone"], ref["skipped"])

    def test_an_app_unchanged_since_the_review_is_written_at_its_turn(self):
        apps = self.review([app("ha", group="Home", follows=True)], {"Home": "critical"})
        (state, _, _), ref = self.turn(apps)
        self.assertEqual(("running", "rolling", None), (state, ref["stage"], ref.get("skipped")))
        self.assertEqual({"homestead.io/group": "Home", "homestead.io/tier": "group", "homestead.io/tier-class": "x-critical"},
                         self.cluster.annotations("lab/ha"))

    def started(self, deps, tiers, change):
        """P.start() with the group map unreadable once the review is done."""
        self.cluster = Cluster(deps, tiers)
        use(self, self.cluster)
        out = P.plan()
        picked = [a for a in out["apps"] if a["id"] in change]
        self.cluster.unreadable = http(500, MAP)
        began = []
        ops = mock.Mock(TERMINAL=("succeeded", "failed"), RESOLVERS={}, CANCELLERS={})
        ops.snapshot.return_value = []
        ops.start.side_effect = lambda *args: began.append(args) or {"id": "op"}
        P.operations = ops
        self.addCleanup(setattr, P, "operations", None)
        body = {"changes": [{"id": a["id"], "current": a["current"], "tier": "critical"} for a in picked],
                "review_token": P.review_token([{"id": a["id"], "current": a["current"], "tier": "critical"} for a in picked]),
                "restart": True}
        return P.start(body), began

    def test_a_follower_whose_group_tier_cant_be_read_is_left_out_and_the_rest_goes_ahead(self):
        _, began = self.started([app("ha", group="Home", follows=True), app("db")], {"Home": "critical"}, {"lab/ha", "lab/db"})
        ref = began[0][4]
        self.assertEqual(["lab/db"], [a["id"] for a in ref["apps"]])
        self.assertEqual(["lab/ha: its group's tier couldn't be read, so it was left alone"], ref["skipped"])

    def test_a_follower_whose_group_tier_cant_be_read_is_not_patched(self):
        _, began = self.started([app("ha", group="Home", follows=True), app("db")], {"Home": "critical"}, {"lab/ha", "lab/db"})
        self.assertNotIn("lab/ha", [a["id"] for a in began[0][4]["apps"]])
        self.assertEqual([], [m for m, *_ in self.cluster.sent if m == "PATCH"])

    def test_a_run_of_only_unreadable_followers_is_refused_plainly(self):
        with self.assertRaisesRegex(ValueError, "couldn't be read"):
            self.started([app("ha", group="Home", follows=True)], {"Home": "critical"}, {"lab/ha"})


import homestead_agent_tools as TOOLS


class AgentToolTests(unittest.TestCase):
    def test_an_agent_may_send_from_its_group_back(self):
        self.assertEqual({"priority": "group"}, TOOLS._clean({"priority": "group"}))
        self.assertEqual({}, TOOLS._clean({"priority": "self"}), "a class it doesn't set is still left out")



import homestead_lifecycle as LC
import homestead_workload_edit as WORKLOAD_EDIT


class EditTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster([], {"Home": "critical", "Media": "low"})
        use(self, self.cluster)
        patcher = mock.patch.object(LC, "kget", side_effect=self.cluster.kget)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spec(self, dep):
        return dep["spec"]["template"]["spec"]

    def test_from_its_group_sets_the_marker_and_the_groups_class(self):
        dep = app("ha", group="Home")
        WORKLOAD_EDIT._apply_tier(dep, "group")
        self.assertEqual("x-critical", self.spec(dep)["priorityClassName"])
        self.assertEqual({"homestead.io/group": "Home", "homestead.io/tier": "group", "homestead.io/tier-class": "x-critical"},
                         dep["metadata"]["annotations"])

    def test_from_a_group_with_no_tier_is_normal_with_no_class(self):
        dep = app("ha", group="Garage", cls="x-low")
        WORKLOAD_EDIT._apply_tier(dep, "group")
        self.assertNotIn("priorityClassName", self.spec(dep))
        self.assertEqual({"homestead.io/group": "Garage", "homestead.io/tier": "group"}, dep["metadata"]["annotations"])

    def test_choosing_a_tier_makes_it_the_apps_own(self):
        dep = app("ha", group="Home", follows=True, cls="x-critical", wrote="x-critical")
        WORKLOAD_EDIT._apply_tier(dep, "low")
        self.assertEqual("x-low", self.spec(dep)["priorityClassName"])
        self.assertEqual({"homestead.io/group": "Home"}, dep["metadata"]["annotations"])

    def test_from_its_group_is_refused_for_an_app_in_no_group(self):
        with self.assertRaisesRegex(ValueError, "isn't in a group"):
            WORKLOAD_EDIT._apply_tier(app("ha"), "group")

    def test_from_its_group_waits_while_the_tiers_cant_be_read(self):
        self.cluster.unreadable = http(500, MAP)
        with self.assertRaisesRegex(ValueError, "couldn't read the group tiers"):
            WORKLOAD_EDIT._apply_tier(app("ha", group="Home"), "group")

    def test_an_app_already_following_is_left_as_it_is_while_the_tiers_cant_be_read(self):
        self.cluster.unreadable = http(500, MAP)
        dep = app("ha", group="Home", follows=True, cls="x-critical", wrote="x-critical")
        before = copy.deepcopy(dep)
        WORKLOAD_EDIT._apply_tier(dep, "group")
        self.assertEqual(before, dep)

    def test_a_class_that_isnt_on_the_cluster_is_left_alone(self):
        dep = app("ha", group="Home", cls="gone")
        WORKLOAD_EDIT._apply_tier(dep, "group")
        self.assertEqual("gone", self.spec(dep)["priorityClassName"])
        self.assertNotIn("homestead.io/tier", dep["metadata"]["annotations"])

    def test_a_save_from_its_group_applies_the_groups_tier_now(self):
        before = app("ha", group="Media", follows=True, cls="x-critical", wrote="x-critical")
        after = copy.deepcopy(before)
        WORKLOAD_EDIT._apply_tier(after, "group")
        self.assertEqual("x-low", self.spec(after)["priorityClassName"])
        self.assertTrue(WORKLOAD_EDIT.restarts(before, after), "the edit's review says it restarts")

    def test_the_edit_form_offers_from_its_group(self):
        dep = app("ha", group="Home", follows=True, cls="x-critical")
        self.assertEqual({"priority": "group", "priority_group": "Home", "priority_group_tier": "critical"},
                         GROUPS.edit_fields(dep, "critical", self.cluster.kget))

    def test_the_edit_form_of_an_app_with_a_tier_of_its_own_shows_that_tier(self):
        self.assertEqual({"priority": "low", "priority_group": "Garage", "priority_group_tier": ""},
                         GROUPS.edit_fields(app("db", group="Garage", cls="x-low"), "low", self.cluster.kget))

    def test_homesteads_own_group_offers_no_group(self):
        self.assertEqual("", GROUPS.edit_fields(app("smb", group="Homestead"), "normal", self.cluster.kget)["priority_group"])

    def test_a_system_or_missing_class_is_never_shown_as_from_its_group(self):
        self.assertEqual("missing", GROUPS.edit_fields(app("ha", group="Home", follows=True, cls="gone"), "missing",
                                                       self.cluster.kget)["priority"])

    def test_an_app_flux_applies_shows_its_own_tier_even_with_a_marker(self):
        dep = app("web", group="Home", follows=True, cls="x-low", labels=FLUX)
        self.assertEqual("low", GROUPS.edit_fields(dep, "low", self.cluster.kget)["priority"])

    def test_settings_sent_back_unchanged_keep_a_follower_following(self):
        dep = app("ha", group="Home", follows=True, cls="x-critical", wrote="x-critical")
        WORKLOAD_EDIT._apply_tier(dep, GROUPS.edit_fields(dep, "critical", self.cluster.kget)["priority"])
        self.assertEqual("group", dep["metadata"]["annotations"]["homestead.io/tier"])

    def test_the_tier_an_edit_choice_means(self):
        self.assertEqual("critical", GROUPS.edit_tier(app("ha", group="Home"), "group", self.cluster.kget))
        self.assertEqual("normal", GROUPS.edit_tier(app("ha", group="Garage"), "group", self.cluster.kget))
        self.assertEqual("low", GROUPS.edit_tier(app("ha"), "low", self.cluster.kget))


class EditRouteTests(unittest.TestCase):
    """workloads.edit() with "From its group": the class is made before the
    Deployment names it, and an unreadable map never blocks a follower."""

    def setUp(self):
        self.events, self.written = [], []

    def run_edit(self, dep, body, unreadable=False):
        import homestead_workloads as WL
        cluster = Cluster([dep], {"Home": "critical"})
        use(self, cluster)
        if unreadable:
            cluster.unreadable = http(500, MAP)
        events, written = self.events, self.written

        def apply(*args, **kwargs):
            events.append("write")
            written.append(copy.deepcopy(dep_after[0]))
            return {"ok": True, "detail": "Saved"}

        dep_after = [None]

        def fake_plan(b):
            prepared = WORKLOAD_EDIT.prepare_edit(b)
            dep_after[0] = prepared["deployment"]
            return {"deployment": dep_after[0]}, {}, {}

        def ensure(tiers, lad=None):
            events.append(("ensure", tiers))
            return []

        with mock.patch.object(WL, "kget", side_effect=cluster.kget), \
                mock.patch.object(LC, "kget", side_effect=cluster.kget), \
                mock.patch.object(WL, "guard_managed_smb"), mock.patch.object(WL, "guard_self"), \
                mock.patch.object(WL, "edit_capacity_plan", side_effect=fake_plan), \
                mock.patch.object(WL.CAPACITY_REVIEW, "enforce"), \
                mock.patch.object(WL.RESTRUCTURE, "copies", return_value=[]), \
                mock.patch.object(WL, "apply_reviewed_edit", side_effect=apply), \
                mock.patch.object(WL.PRIORITY, "ensure", side_effect=ensure):
            return WL.edit(mock.Mock(body={"ns": "lab", "name": dep["metadata"]["name"], **body}))

    def test_from_its_group_makes_the_class_before_the_deployment_names_it(self):
        self.run_edit(app("ha", group="Home"), {"priority": "group"})
        self.assertEqual([("ensure", ("critical",)), "write"], self.events)
        template = self.written[0]
        self.assertEqual("x-critical", template["spec"]["template"]["spec"]["priorityClassName"])
        self.assertEqual("group", template["metadata"]["annotations"]["homestead.io/tier"])
        self.assertEqual("x-critical", template["metadata"]["annotations"]["homestead.io/tier-class"])

    def test_a_follower_saved_while_the_tiers_cant_be_read_keeps_its_tier(self):
        dep = app("ha", group="Home", follows=True, cls="x-critical", wrote="x-critical")
        self.run_edit(dep, {"priority": "group"}, unreadable=True)
        self.assertEqual(["write"], self.events)
        saved = self.written[0]
        self.assertEqual("x-critical", saved["spec"]["template"]["spec"]["priorityClassName"])
        self.assertEqual(dep["metadata"]["annotations"], saved["metadata"]["annotations"])

    def test_a_non_follower_choosing_its_group_is_told_why_and_nothing_is_written(self):
        with self.assertRaisesRegex(ValueError, "couldn't read the group tiers"):
            self.run_edit(app("ha", group="Home"), {"priority": "group"}, unreadable=True)
        self.assertEqual([], self.events)


class WorkloadListTests(unittest.TestCase):
    def test_each_row_carries_its_groups_tier(self):
        cluster = Cluster([app("ha", group="Home"), app("plex", group="Media"), app("web")], {"Home": "critical"})
        cluster.other = {"items": []}       # pods, metrics, services: none
        use(self, cluster)
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=cluster.kget):
            rows = {w["name"]: w for w in server.WORKLOADS.get_workloads()}
        self.assertEqual(("critical", "", ""), (rows["ha"]["group_tier"], rows["plex"]["group_tier"], rows["web"]["group_tier"]))


if __name__ == "__main__":
    unittest.main()
