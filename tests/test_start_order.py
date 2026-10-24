"""#746 "Starts after": an app names the apps it starts after, and each new
pod of it waits for their Services before anything of it runs."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_start_order as START
import homestead_workloads as WORKLOADS

KEY = "homestead.io/starts-after"


def deployment(ns, name, after=None, replicas=1, ready=1, host_network=False, dns_policy=None, inits=None, **meta):
    spec = {"containers": [{"name": "main", "image": f"example/{name}:1"}]}
    if host_network:
        spec["hostNetwork"] = True
    if dns_policy:
        spec["dnsPolicy"] = dns_policy
    if inits is not None:
        spec["initContainers"] = inits
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": name, "namespace": ns, **meta},
           "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                    "template": {"metadata": {"labels": {"app": name}}, "spec": spec}},
           "status": {"readyReplicas": ready}}
    if after is not None:
        dep["metadata"].setdefault("annotations", {})[KEY] = after if isinstance(after, str) else json.dumps(after)
    return dep


def service(ns, name, app, port, **spec):
    body = {"selector": {"app": app}, "ports": [{"port": port, "protocol": "TCP"}], **spec}
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns}, "spec": body}


def stored(*pairs, timeout=300, on_timeout="start"):
    """A stored setting: after (ns, app, port) for each pair."""
    return {"after": [{"ns": ns, "app": app, "service": app, "port": port} for ns, app, port in pairs],
            "timeout": timeout, "on_timeout": on_timeout}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.deps = [deployment("lab", "app-1"), deployment("lab", "app-2"), deployment("lab", "app-3"),
                     deployment("data", "db"), deployment("homestead", "homestead"),
                     deployment("kube-system", "coredns")]
        self.svcs = [service("lab", "app-1", "app-1", 80), service("lab", "app-2", "app-2", 5432),
                     service("lab", "app-3", "app-3", 80), service("data", "db", "db", 5432),
                     service("homestead", "homestead", "homestead", 8080)]
        for patch in (mock.patch.object(WORKLOADS, "is_self", create=True,
                                        new=lambda ns, name: (ns, name) == ("homestead", "homestead")),
                      mock.patch.object(WORKLOADS, "SYS_NS", {"kube-system", "longhorn-system"})):
            patch.start()
            self.addCleanup(patch.stop)

    def dep(self, ns, name):
        return next(d for d in self.deps if (d["metadata"]["namespace"], d["metadata"]["name"]) == (ns, name))

    def put(self, ns, name, setting):
        self.dep(ns, name)["metadata"].setdefault("annotations", {})[KEY] = json.dumps(setting)

    def clean(self, ns, name, value):
        return START.clean(self.dep(ns, name), value, self.deps, self.svcs)

    def refused(self, ns, name, value):
        with self.assertRaises(ValueError) as caught:
            self.clean(ns, name, value)
        return str(caught.exception)


class SettingTests(Fixture):
    def test_a_cycle_is_refused_and_named(self):
        self.put("lab", "app-2", stored(("lab", "app-1", 80)))
        self.assertEqual("a cycle: app-1 → app-2 → app-1",
                         self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))
        self.put("lab", "app-2", stored(("lab", "app-3", 80)))
        self.put("lab", "app-3", stored(("lab", "app-1", 80)))
        self.assertEqual("a cycle: app-1 → app-2 → app-3 → app-1",
                         self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))
        self.assertEqual("an app can't start after itself",
                         self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-1"}]}))

    def test_homestead_and_system_apps_are_never_gated(self):
        for ns, name in (("homestead", "homestead"), ("kube-system", "coredns")):
            with self.subTest(app=name):
                self.assertEqual("Homestead and system apps can't wait for other apps",
                                 self.refused(ns, name, {"after": [{"ns": "lab", "app": "app-2"}]}))
        got = self.clean("lab", "app-1", {"after": [{"ns": "homestead", "app": "homestead"}]})
        self.assertEqual([{"ns": "homestead", "app": "homestead", "service": "homestead", "port": 8080}], got["after"])

    def test_dependency_needs_a_service_and_its_port(self):
        self.svcs = [s for s in self.svcs if s["metadata"]["name"] != "app-2"]
        self.assertEqual("app-2 has no Service with a TCP port, so nothing can tell when it is ready",
                         self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))
        self.svcs.append(service("lab", "app-2", "app-2", 5432))
        self.assertEqual("app-2's Service has no port 5433",
                         self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2", "port": 5433}]}))
        self.assertEqual("app-2 has no Service with a TCP port, so nothing can tell when it is ready",
                         self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2", "service": "app-3"}]}))
        self.svcs.append(service("lab", "app-2-udp", "app-2", 53))
        self.svcs[-1]["spec"]["ports"][0]["protocol"] = "UDP"
        self.assertEqual([{"name": "app-2", "ports": [5432]}],
                         START.services_for("lab", "app-2", self.deps, self.svcs), "a UDP port can't be probed")
        self.assertEqual({"ns": "lab", "app": "app-2", "service": "app-2", "port": 5432},
                         self.clean("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]})["after"][0],
                         "the Service and its first TCP port are picked when none is named")

    def test_services_that_cannot_show_readiness_are_refused(self):
        for spec in ({"type": "ExternalName", "externalName": "db.example.com"},
                     {"selector": None},
                     {"publishNotReadyAddresses": True}):
            with self.subTest(spec=spec):
                self.svcs = [s for s in self.svcs if s["metadata"]["name"] != "app-2"]
                bad = service("lab", "app-2", "app-2", 5432)
                bad["spec"].update(spec)
                self.svcs.append(bad)
                self.assertEqual("app-2 has no Service with a TCP port, so nothing can tell when it is ready",
                                 self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))

    def test_host_network_without_cluster_dns_is_refused(self):
        self.deps[0] = deployment("lab", "app-1", host_network=True)
        self.assertEqual("app-1 uses the host's network without cluster DNS; set dnsPolicy to "
                         "ClusterFirstWithHostNet so it can find app-2",
                         self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))
        self.deps[0] = deployment("lab", "app-1", host_network=True, dns_policy="ClusterFirstWithHostNet")
        self.assertTrue(self.clean("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))

    def test_a_pod_without_cluster_dns_is_refused(self):
        for policy in ("Default", "None"):
            with self.subTest(policy=policy):
                self.deps[0] = deployment("lab", "app-1", dns_policy=policy)
                self.assertEqual(f"app-1 does not use cluster DNS (dnsPolicy {policy}), so it can't find app-2; "
                                 "set dnsPolicy to ClusterFirst",
                                 self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))
        self.deps[0] = deployment("lab", "app-1", dns_policy="ClusterFirst")
        self.assertTrue(self.clean("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2"}]}))

    def test_a_kept_dependency_that_is_gone_is_not_checked_again(self):
        """Only what changed is checked: a stored entry whose app has since
        been removed stays, and a newly added one must still exist."""
        gone = {"ns": "lab", "app": "gone", "service": "gone", "port": 80}
        self.put("lab", "app-1", {"after": [gone], "timeout": 300, "on_timeout": "start"})
        kept = self.clean("lab", "app-1", {"after": [gone], "timeout": 300, "on_timeout": "start"})
        self.assertEqual([gone], kept["after"])
        more = self.clean("lab", "app-1", {"after": [gone, {"ns": "lab", "app": "app-2"}]})
        self.assertEqual([gone, {"ns": "lab", "app": "app-2", "service": "app-2", "port": 5432}], more["after"])
        self.assertEqual("nope has no Service with a TCP port, so nothing can tell when it is ready",
                         self.refused("lab", "app-1", {"after": [gone, {"ns": "lab", "app": "nope"}]}))
        self.assertEqual("gone has no Service with a TCP port, so nothing can tell when it is ready",
                         self.refused("lab", "app-1", {"after": [dict(gone, port=81)]}), "a changed entry is checked")

    def test_the_unreadable_marker_is_never_saved(self):
        """{"unreadable": True} is what Edit's form shows for a hand-edited
        annotation: sent back, it must not read as "remove it"."""
        self.assertEqual("Starts after can't be read on app-1; set it again or clear it",
                         self.refused("lab", "app-1", {"unreadable": True}))

    def test_timeout_and_count_limits(self):
        one = [{"ns": "lab", "app": "app-2"}]
        for timeout in (29, 3601, 30.5, "300", True):
            with self.subTest(timeout=timeout):
                self.assertEqual("wait up to must be between 30 s and 60 min",
                                 self.refused("lab", "app-1", {"after": one, "timeout": timeout}))
        self.assertEqual("if it never comes up: start or wait",
                         self.refused("lab", "app-1", {"after": one, "on_timeout": "later"}))
        for n in range(4, 13):
            self.deps.append(deployment("lab", f"app-{n}"))
            self.svcs.append(service("lab", f"app-{n}", f"app-{n}", 80))
        eleven = [{"ns": "lab", "app": f"app-{n}"} for n in range(2, 13)]
        self.assertEqual("Starts after takes at most 10 apps", self.refused("lab", "app-1", {"after": eleven}))
        got = self.clean("lab", "app-1", {"after": one + one + [{"ns": "data", "app": "db"}], "timeout": 60,
                                          "on_timeout": "wait"})
        self.assertEqual(["app-2", "db"], [a["app"] for a in got["after"]], "duplicates are collapsed")
        self.assertEqual((60, "wait"), (got["timeout"], got["on_timeout"]))
        self.assertEqual((300, "start"), (lambda s: (s["timeout"], s["on_timeout"]))(self.clean("lab", "app-1", {"after": one})))

    def test_empty_after_means_remove(self):
        self.assertIsNone(self.clean("lab", "app-1", {"after": []}))
        self.assertIsNone(self.clean("lab", "app-1", None))
        self.assertIsNone(self.clean("homestead", "homestead", {"after": []}), "removing is never refused")

    def test_cross_namespace_dependency_is_allowed(self):
        got = self.clean("lab", "app-1", {"after": [{"ns": "data", "app": "db"}]})
        self.assertEqual([{"ns": "data", "app": "db", "service": "db", "port": 5432}], got["after"])

    def test_names_are_dns_labels(self):
        self.assertIn("lowercase", self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2; reboot"}]}))
        self.assertIn("lowercase", self.refused("lab", "app-1", {"after": [{"ns": "lab", "app": "app-2", "service": "$(id)"}]}))

    def test_unreadable_annotation_reads_as_unreadable(self):
        self.assertEqual({"unreadable": True}, START.read(deployment("lab", "x", after="{not json")))
        self.assertEqual({"unreadable": True}, START.read(deployment("lab", "x", after="[1]")))
        self.assertIsNone(START.read(deployment("lab", "x")))
        self.put("lab", "app-2", stored(("lab", "app-3", 80)))
        self.assertEqual(stored(("lab", "app-3", 80)), START.read(self.dep("lab", "app-2")))
        self.dep("lab", "app-3")["metadata"]["annotations"] = {KEY: "{broken"}
        self.assertEqual({("lab", "app-2"): [("lab", "app-3")]}, START.graph(self.deps),
                         "an unreadable setting is no edge, and no crash")


VOLOWNER = {"name": "homestead-volume-owner", "image": "docker.io/library/alpine:3.24", "command": ["chown"]}
SIDECAR = {"name": "log-shipper", "image": "example/shipper:1", "restartPolicy": "Always"}
SETTING = stored(("lab", "app-2", 5432), ("data", "db", 5432))


def inits(dep):
    return _template(dep).get("initContainers")


def _template(dep):
    return dep["spec"]["template"]["spec"]


class GateTests(unittest.TestCase):
    def test_gate_goes_first(self):
        dep = deployment("lab", "app-1", inits=[copy.deepcopy(VOLOWNER), copy.deepcopy(SIDECAR)])
        START.apply_to(dep, SETTING)
        self.assertEqual(["homestead-start-gate", "homestead-volume-owner", "log-shipper"], [c["name"] for c in inits(dep)])
        self.assertEqual("app-2.lab.svc:5432 db.data.svc:5432",
                         next(e["value"] for e in inits(dep)[0]["env"] if e["name"] == "HS_AFTER"))
        self.assertEqual(json.dumps(SETTING, sort_keys=True, separators=(",", ":")), dep["metadata"]["annotations"][KEY])

    def test_render_is_idempotent(self):
        once = deployment("lab", "app-1", inits=[copy.deepcopy(VOLOWNER)])
        START.apply_to(once, SETTING)
        twice = copy.deepcopy(once)
        START.apply_to(twice, SETTING)
        self.assertEqual(once, twice)
        self.assertEqual("rendered", START.gate_state(twice))

    def test_saving_again_keeps_what_the_api_server_added(self):
        dep = deployment("lab", "app-1")
        START.apply_to(dep, SETTING)
        # What the API server fills in when it stores the template.
        inits(dep)[0].update({"terminationMessagePath": "/dev/termination-log"})
        live = copy.deepcopy(dep)
        self.assertEqual("rendered", START.gate_state(live))
        START.apply_to(dep, SETTING)
        self.assertEqual(live, dep, "an unchanged setting must not change the pods")

    def test_removing_removes_both_and_nothing_else(self):
        for before in (deployment("lab", "app-1"), deployment("lab", "app-1", inits=[copy.deepcopy(VOLOWNER)])):
            with self.subTest(inits=bool(inits(before))):
                dep = copy.deepcopy(before)
                START.apply_to(dep, SETTING)
                START.apply_to(dep, None)
                self.assertEqual({}, dep["metadata"].get("annotations"))
                dep["metadata"].pop("annotations")
                self.assertEqual(before, dep)
        untouched = deployment("lab", "app-1")
        START.apply_to(untouched, None)
        self.assertEqual(deployment("lab", "app-1"), untouched, "removing nothing changes nothing")

    def test_names_only_through_env(self):
        for name in ("app-2", "db", "lab", "data", "svc", "5432"):
            self.assertNotIn(name, START.SCRIPT)
        for setting in (SETTING, stored(("other", "x-y", 1), timeout=30, on_timeout="wait")):
            self.assertEqual(["sh", "-c", START.SCRIPT], START.render(setting)["command"])
        self.assertEqual({"HS_AFTER", "HS_TIMEOUT", "HS_ON_TIMEOUT"}, {e["name"] for e in START.render(SETTING)["env"]})

    def test_gate_passes_restricted_and_quota(self):
        gate = START.render(SETTING)
        for side in ("requests", "limits"):
            self.assertEqual({"cpu", "memory"}, set(gate["resources"][side]), side)
        sc = gate["securityContext"]
        self.assertIs(True, sc["runAsNonRoot"])
        self.assertEqual("RuntimeDefault", sc["seccompProfile"]["type"])
        self.assertEqual(["ALL"], sc["capabilities"]["drop"])
        self.assertIs(False, sc["allowPrivilegeEscalation"])
        self.assertNotEqual(0, sc["runAsUser"])
        self.assertEqual("IfNotPresent", gate["imagePullPolicy"])

    def test_gate_image_is_pinned(self):
        self.assertRegex(START.GATE_IMAGE, r"\Adocker\.io/library/alpine:3\.24@sha256:[0-9a-f]{64}\Z")
        self.assertEqual(START.GATE_IMAGE, START.render(SETTING)["image"])

    def test_state_differs_when_moved_or_edited(self):
        self.assertEqual("none", START.gate_state(deployment("lab", "app-1")))
        moved = deployment("lab", "app-1", inits=[copy.deepcopy(VOLOWNER)])
        START.apply_to(moved, SETTING)
        inits(moved).reverse()
        self.assertEqual("differs", START.gate_state(moved))
        edited = deployment("lab", "app-1")
        START.apply_to(edited, SETTING)
        inits(edited)[0]["env"][0]["value"] = "elsewhere.lab.svc:1"
        self.assertEqual("differs", START.gate_state(edited))
        missing = deployment("lab", "app-1", after=SETTING)
        self.assertEqual("missing", START.gate_state(missing))
        orphan = deployment("lab", "app-1")
        START.apply_to(orphan, SETTING)
        orphan["metadata"]["annotations"].pop(KEY)
        self.assertEqual("differs", START.gate_state(orphan), "a gate with no setting")
        self.assertEqual("differs", START.gate_state(deployment("lab", "app-1", after="{broken")))


FLUX = {"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
NODE = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node-1"},
        "status": {"images": [{"names": ["docker.io/library/alpine:3.24"]}]}}


class SaveTests(Fixture):
    """Saving it: its own route and Edit, Flux apps, the first pre-pull."""

    def setUp(self):
        super().setUp()
        import homestead_kube as K
        import homestead_lifecycle as LC
        import homestead_image_cache as IMAGES
        from fakes.kube import Kube
        self.deps.append(deployment("lab", "flux-app", labels=FLUX))
        self.svcs.append(service("lab", "flux-app", "flux-app", 80))
        self.kube = self.enterContext(Kube(*self.deps, *self.svcs, NODE))
        self.prepull = mock.MagicMock(return_value={"ok": True})
        for patch in (mock.patch.object(LC, "kget", K.kget), mock.patch.object(LC, "ksend", K.ksend),
                      mock.patch.object(IMAGES, "prepull", self.prepull),
                      # Edit's guards, as server.py binds them; GuardTests checks they are called.
                      mock.patch.object(WORKLOADS, "guard_managed_smb", create=True, new=lambda ns, name: None),
                      mock.patch.object(WORKLOADS, "guard_self", create=True, new=lambda ns, name: None),
                      mock.patch.object(WORKLOADS, "homestead_part", create=True, new=lambda ns, name: "")):
            patch.start()
            self.addCleanup(patch.stop)

    def live(self, ns, name):
        return self.kube.object(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")

    def deployment_writes(self):
        return [w for w in self.kube.writes if "/deployments/" in w[1]]

    def test_restore_gate_puts_the_gate_back(self):
        """A gate changed outside Homestead: the same setting saved again is
        no change; with "restore_gate" the gate is rendered again, once."""
        START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-2"}]})
        live = self.live("lab", "app-1")
        live["spec"]["template"]["spec"]["initContainers"][0]["image"] = "example/other:1"
        self.kube.add(live)
        self.assertEqual("differs", START.gate_state(self.live("lab", "app-1")))
        stored = START.read(self.live("lab", "app-1"))
        same = START.save({"ns": "lab", "name": "app-1", **stored})
        self.assertFalse(same["restarts"])
        self.assertEqual("differs", START.gate_state(self.live("lab", "app-1")))
        back = START.save({"ns": "lab", "name": "app-1", **stored, "restore_gate": True})
        self.assertTrue(back["restarts"])
        self.assertEqual("rendered", START.gate_state(self.live("lab", "app-1")))
        self.assertEqual(stored, START.read(self.live("lab", "app-1")), "the flag is not stored")

    def test_save_writes_the_setting_and_says_it_restarts(self):
        got = START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-2"}]})
        self.assertEqual(True, got["ok"])
        self.assertTrue(got["restarts"])
        saved = self.live("lab", "app-1")
        self.assertEqual("rendered", START.gate_state(saved))
        self.assertIn("homestead.io/editedAt", saved["spec"]["template"]["metadata"]["annotations"])
        again = START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-2"}]})
        self.assertFalse(again["restarts"], "the same setting again restarts nothing")
        self.assertEqual(1, len(self.deployment_writes()))
        removed = START.save({"ns": "lab", "name": "app-1", "after": []})
        self.assertTrue(removed["restarts"])
        self.assertEqual("none", START.gate_state(self.live("lab", "app-1")))

    def test_a_refusal_writes_nothing(self):
        with self.assertRaises(ValueError):
            START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-1"}]})
        self.assertEqual([], self.kube.writes)

    def test_managed_app_writes_nothing_and_gets_git_lines(self):
        import homestead_routes as ROUTER
        got = START.save({"ns": "lab", "name": "flux-app", "after": [{"ns": "lab", "app": "app-2"}]})
        self.assertIsInstance(got, ROUTER.Reply)
        self.assertEqual(409, got.code)
        self.assertIn("homestead.io/starts-after:", got.body["git"])
        self.assertIn("name: homestead-start-gate", got.body["git"])
        self.assertIn(START.GATE_IMAGE, got.body["git"])
        self.assertEqual("flux-app's start order is set where Flux applies it; commit the lines Homestead shows",
                         got.body["error"])
        self.assertEqual([], self.kube.writes)
        self.prepull.assert_not_called()

    def test_lines_only_never_writes(self):
        # Edit's "Show the lines to commit" (#746 PR 2): the lines for the
        # apps chosen, and nothing written, whoever applies the app now.
        for name in ("flux-app", "app-1"):
            with self.subTest(name=name):
                got = START.save({"ns": "lab", "name": name, "after": [{"ns": "lab", "app": "app-2"}], "lines": True})
                self.assertIsInstance(got, dict)
                self.assertIn("name: homestead-start-gate", got["git"])
                self.assertNotIn("ok", got)
        self.assertEqual([], self.kube.writes)
        self.prepull.assert_not_called()
        with self.assertRaises(ValueError):
            START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-1"}], "lines": True})

    def test_git_lines_are_yaml_the_manifest_takes(self):
        lines = START.git_lines(self.dep("lab", "flux-app"), SETTING)
        self.assertRegex(lines, r"spec:\n  template:\n    spec:\n      initContainers:[^\n]*\n        - name: homestead-start-gate\n")
        annotation = next(line for line in lines.splitlines() if "homestead.io/starts-after:" in line)
        self.assertEqual(SETTING, json.loads(json.loads(annotation.split(": ", 1)[1])), "a quoted string that parses back")
        helm = deployment("lab", "chart-app", labels={"helm.toolkit.fluxcd.io/name": "chart-app"})
        chart = START.git_lines(helm, SETTING)
        self.assertTrue(chart.startswith("# chart values, if the chart takes extra initContainers and podAnnotations"))
        self.assertIn("post-renderer", chart)
        self.assertIn("remove", START.git_lines(self.dep("lab", "flux-app"), None))

    def test_apply_refuses_a_changed_setting_on_a_managed_app(self):
        dep = self.dep("lab", "flux-app")
        with self.assertRaises(ValueError) as caught:
            START.apply(dep, {"after": [{"ns": "lab", "app": "app-2"}]}, self.deps, self.svcs)
        self.assertIn("where Flux applies it", str(caught.exception))
        before = copy.deepcopy(dep)
        START.apply(dep, None, self.deps, self.svcs)
        self.assertEqual(before, dep, "an unchanged (empty) setting passes and writes nothing")

    def test_form_lists_the_setting_and_the_apps_to_choose_from(self):
        START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "data", "app": "db"}], "timeout": 120})
        form = START.form("lab", "app-1")
        self.assertEqual([{"ns": "data", "app": "db", "service": "db", "port": 5432}], form["after"])
        self.assertEqual((120, "start", None, "rendered"), (form["timeout"], form["on_timeout"], form["managed_by"], form["gate"]))
        apps = {(c["ns"], c["app"]): c["services"] for c in form["candidates"]}
        self.assertNotIn(("lab", "app-1"), apps)
        self.assertNotIn(("kube-system", "coredns"), apps)
        self.assertEqual([{"name": "app-2", "ports": [5432]}], apps[("lab", "app-2")])
        self.assertIn(("homestead", "homestead"), apps)
        empty = START.form("lab", "app-2")
        self.assertEqual(([], 300, "start", "none"), (empty["after"], empty["timeout"], empty["on_timeout"], empty["gate"]))
        self.kube.add(deployment("lab", "flux-app", after=SETTING, labels=FLUX))
        flux = START.form("lab", "flux-app")
        self.assertEqual(("Flux", "not-committed"), (flux["managed_by"], flux["gate"]))
        self.assertIn("homestead-start-gate", flux["git"])

    def test_first_gate_prepulls_the_image(self):
        START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-2"}]})
        self.prepull.assert_called_once_with(START.GATE_IMAGE)
        START.save({"ns": "lab", "name": "app-3", "after": [{"ns": "lab", "app": "app-2"}]})
        self.prepull.assert_called_once()

    def test_a_node_that_has_the_image_needs_no_prepull(self):
        self.kube.add({**NODE, "status": {"images": [{"names": [START.GATE_IMAGE.replace(":3.24@", "@")]}]}})
        START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-2"}]})
        self.prepull.assert_not_called()

    def test_a_failed_prepull_is_a_note_not_a_failure(self):
        self.prepull.side_effect = ValueError("no node is ready to take an image pull")
        got = START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-2"}]})
        self.assertTrue(got["ok"])
        self.assertEqual("the start gate's image could not be pre-pulled: no node is ready to take an image pull", got["note"])

    def test_edit_refuses_a_changed_setting_on_a_managed_app(self):
        import homestead_kube as K

        class Reached(Exception):
            pass

        def stop(body):
            raise Reached(body)
        for patch in (mock.patch.object(WORKLOADS, "kget", K.kget, create=True),
                      mock.patch.object(WORKLOADS, "guard_managed_smb", lambda ns, name: None, create=True),
                      mock.patch.object(WORKLOADS, "edit_capacity_plan", stop)):
            patch.start()
            self.addCleanup(patch.stop)
        import homestead_routes as ROUTER
        request = lambda body: ROUTER.Request("POST", "/api/edit", {}, body, "admin", "admin")
        with self.assertRaises(ValueError) as caught:
            WORKLOADS.edit(request({"ns": "lab", "name": "flux-app", "starts_after": {"after": [{"ns": "lab", "app": "app-2"}]}}))
        self.assertEqual("flux-app's start order is set where Flux applies it; commit the lines Homestead shows",
                         str(caught.exception))
        with self.assertRaises(Reached) as passed:
            WORKLOADS.edit(request({"ns": "lab", "name": "flux-app", "starts_after": {"after": []}}))
        self.assertNotIn("starts_after", passed.exception.args[0], "an unchanged setting passes through, unwritten")

    def reach_plan(self, body):
        """Edit's save up to its capacity review: the body that gets there."""
        import homestead_kube as K
        import homestead_routes as ROUTER

        class Reached(Exception):
            pass

        def stop(got):
            raise Reached(got)
        with mock.patch.object(WORKLOADS, "kget", K.kget, create=True), \
                mock.patch.object(WORKLOADS, "guard_managed_smb", lambda ns, name: None, create=True), \
                mock.patch.object(WORKLOADS, "edit_capacity_plan", stop), self.assertRaises(Reached) as reached:
            WORKLOADS.edit(ROUTER.Request("POST", "/api/edit", {}, body, "admin", "admin"))
        return reached.exception.args[0]

    def test_edit_of_a_managed_app_with_an_unreadable_setting_is_not_refused(self):
        self.kube.add(deployment("lab", "flux-app", after='{"after": "by hand"}', labels=FLUX))
        got = self.reach_plan({"ns": "lab", "name": "flux-app", "starts_after": {"unreadable": True}})
        self.assertNotIn("starts_after", got, "the marker Edit's form showed is not a change")

    def test_edit_of_a_managed_app_whose_dependency_is_gone_is_not_refused(self):
        gone = {"after": [{"ns": "lab", "app": "gone", "service": "gone", "port": 80}], "timeout": 300, "on_timeout": "start"}
        self.kube.add(deployment("lab", "flux-app", after=gone, labels=FLUX))
        got = self.reach_plan({"ns": "lab", "name": "flux-app", "starts_after": copy.deepcopy(gone)})
        self.assertNotIn("starts_after", got)

    def test_edit_sending_the_stored_setting_back_writes_nothing_of_it(self):
        gone = {"after": [{"ns": "lab", "app": "gone", "service": "gone", "port": 80}], "timeout": 300, "on_timeout": "start"}
        self.kube.add(deployment("lab", "app-1", after=gone))
        self.assertNotIn("starts_after", self.reach_plan({"ns": "lab", "name": "app-1", "starts_after": copy.deepcopy(gone)}))
        self.assertNotIn("starts_after", self.reach_plan({"ns": "lab", "name": "app-1", "starts_after": {"unreadable": True}}))
        added = dict(gone, after=gone["after"] + [{"ns": "lab", "app": "app-2"}])
        self.assertIn("starts_after", self.reach_plan({"ns": "lab", "name": "app-1", "starts_after": added}))

    def test_an_empty_setting_prepulls_nothing(self):
        import homestead_capacity_review as CAPACITY_REVIEW
        import homestead_restructure as RESTRUCTURE
        import homestead_kube as K
        import homestead_routes as ROUTER
        with mock.patch.object(WORKLOADS, "kget", K.kget, create=True), \
                mock.patch.object(WORKLOADS, "guard_managed_smb", lambda ns, name: None, create=True), \
                mock.patch.object(WORKLOADS, "edit_capacity_plan", lambda b: ({}, {}, {})), \
                mock.patch.object(CAPACITY_REVIEW, "enforce", lambda *a: None), \
                mock.patch.object(RESTRUCTURE, "copies", lambda b: []), \
                mock.patch.object(WORKLOADS, "guard_self", lambda *a, **k: None, create=True), \
                mock.patch.object(WORKLOADS, "apply_reviewed_edit", lambda b, prepared, **kw: {"ok": True}, create=True):
            WORKLOADS.edit(ROUTER.Request("POST", "/api/edit", {}, {"ns": "lab", "name": "app-1", "starts_after": {"after": []}},
                                          "admin", "admin"))
        self.prepull.assert_not_called()

    def test_routes_and_roles(self):
        import homestead_routes as ROUTER
        self.assertIn("homestead_start_order", ROUTER.MODULES)
        self.assertEqual("viewer", START.ROUTES[("GET", "/api/workloads/starts-after")][0])
        self.assertEqual("operator", START.ROUTES[("POST", "/api/workloads/starts-after")][0])
        request = ROUTER.Request("GET", "/api/workloads/starts-after", {"ns": ["lab"], "name": ["app-2"]}, {}, "u", "viewer")
        self.assertEqual([], START.ROUTES[("GET", "/api/workloads/starts-after")][1](request)["after"])



class GuardTests(SaveTests):
    """Its own route makes the checks Edit makes, and Homestead's helpers
    are never gated (security review, 2026-10-22)."""

    def setUp(self):
        super().setUp()
        self.deps += [deployment("homestead", "samba"), deployment("homestead", "homestead-nfs")]
        for dep in self.deps[-2:]:
            self.kube.add(dep)
        parts = {("homestead", "homestead"): "self", ("homestead", "samba"): "smb", ("homestead", "homestead-nfs"): "nfs"}

        def guard_managed_smb(ns, name):
            if parts.get((ns, name)) in ("smb", "nfs"):
                raise ValueError("Homestead manages SMB and its mounts from Network Shares.")
        self.guard_self = mock.MagicMock()
        for patch in (mock.patch.object(WORKLOADS, "homestead_part", create=True, new=lambda ns, name: parts.get((ns, name), "")),
                      mock.patch.object(WORKLOADS, "guard_managed_smb", create=True, new=guard_managed_smb),
                      mock.patch.object(WORKLOADS, "guard_self", create=True, new=self.guard_self)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_managed_smb_share_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            START.save({"ns": "homestead", "name": "samba", "after": [{"ns": "lab", "app": "app-2"}]})
        self.assertIn("Homestead manages SMB", str(caught.exception))
        self.assertEqual([], self.kube.writes)

    def test_homestead_itself_is_refused(self):
        with self.assertRaises(ValueError):
            START.save({"ns": "homestead", "name": "homestead", "after": [{"ns": "lab", "app": "app-2"}]})
        self.guard_self.assert_called_once_with("homestead", "homestead")
        self.assertEqual([], self.kube.writes)

    def test_a_homestead_helper_workload_is_refused(self):
        for entry in ("save", "clean"):
            with self.subTest(entry=entry), self.assertRaises(ValueError) as caught:
                if entry == "save":
                    with mock.patch.object(WORKLOADS, "guard_managed_smb", create=True, new=lambda ns, name: None):
                        START.save({"ns": "homestead", "name": "homestead-nfs", "after": [{"ns": "lab", "app": "app-2"}]})
                else:
                    self.clean("homestead", "homestead-nfs", {"after": [{"ns": "lab", "app": "app-2"}]})
            self.assertEqual("Homestead and system apps can't wait for other apps", str(caught.exception))
        self.assertEqual([], self.kube.writes)

    def test_an_ordinary_app_still_works(self):
        got = START.save({"ns": "lab", "name": "app-1", "after": [{"ns": "lab", "app": "app-2"}]})
        self.assertTrue(got["ok"] and got["restarts"])
        self.guard_self.assert_called_once_with("lab", "app-1")
        self.assertEqual("rendered", START.gate_state(self.live("lab", "app-1")))


def item(name, ns="lab", kind="app"):
    return {"kind": kind, "ns": ns, "name": name}


def names(layers):
    return [[i["name"] for i in layer] for layer in layers]


class OrderTests(Fixture):
    """Homestead's own starts of several apps go dependencies first."""

    def test_order_layers(self):
        self.put("lab", "app-1", stored(("lab", "app-2", 5432)))
        self.put("lab", "app-2", stored(("data", "db", 5432)))
        self.deps.append(deployment("lab", "web"))
        got = START.order([item("app-1"), item("app-2"), item("db", "data"), item("web")], self.deps)
        self.assertEqual([["db", "web"], ["app-2"], ["app-1"]], names(got))

    def test_outside_cycle_is_one_layer(self):
        for a, b in (("a", "b"), ("b", "a"), ("c", "a")):
            self.deps.append(deployment("lab", a, after=stored(("lab", b, 80))))
        got = names(START.order([item("c"), item("b"), item("a")], self.deps))
        self.assertEqual(2, len(got))
        self.assertEqual({"a", "b"}, set(got[0]))
        self.assertEqual(["c"], got[1])

    def test_order_ignores_edges_to_apps_not_starting(self):
        self.put("lab", "app-1", stored(("lab", "app-2", 5432)))
        self.put("lab", "app-3", stored(("data", "db", 5432)))
        got = START.order([item("app-1"), item("app-3"), item("vm-1", kind="vm")], self.deps)
        self.assertEqual([["app-1", "app-3", "vm-1"]], names(got))
        self.assertEqual([], START.order([], self.deps))

    def test_arrange_reads_through_the_bound_kget(self):
        import homestead_lifecycle as LC
        self.put("lab", "app-1", stored(("lab", "app-2", 5432)))
        with mock.patch.object(LC, "kget", lambda path: {"items": copy.deepcopy(self.deps)}):
            got = START.arrange([item("app-1"), item("app-2")])
        self.assertEqual(["app-2", "app-1"], [i["name"] for i in got])

    def test_start_plan_names_stopped_dependencies(self):
        self.put("lab", "app-1", stored(("lab", "app-2", 5432), ("data", "db", 5432), ("lab", "gone", 80)))
        self.dep("lab", "app-2")["spec"]["replicas"] = 0
        self.assertEqual(["app-2", "gone"], START.stopped_dependencies(self.dep("lab", "app-1"), self.deps))
        self.assertEqual([], START.stopped_dependencies(self.dep("lab", "app-3"), self.deps))

    def test_the_start_plan_carries_waits_for(self):
        import server
        self.put("lab", "app-1", stored(("lab", "app-2", 5432)))
        self.dep("lab", "app-2")["spec"]["replicas"] = 0
        by_path = {f"/apis/apps/v1/namespaces/lab/deployments/app-1": self.dep("lab", "app-1"),
                   "/apis/apps/v1/deployments": {"items": self.deps}}
        overview = server.OVERVIEW_ROUTES
        settings = mock.patch.object(overview, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS)
        settings.start()
        self.addCleanup(settings.stop)
        with mock.patch.object(overview.PLACE, "start_plan", return_value={"blocked": False}), \
                mock.patch.object(overview, "kget", side_effect=lambda path: copy.deepcopy(by_path[path])):
            self.assertEqual(["app-2"], overview.workload_start_plan("lab", "app-1")["waits_for"])
        with mock.patch.object(overview.PLACE, "start_plan", return_value={"blocked": False}), \
                mock.patch.object(overview, "kget", side_effect=RuntimeError("API down")):
            plan = overview.workload_start_plan("lab", "app-1")
        self.assertEqual([], plan["waits_for"], "a start never fails on it")
        # #620: Start says it could not check, rather than that nothing waits.
        self.assertIn("API down", plan["waits_for_error"])


T0 = 1_792_000_000   # 2026-10-15 (UTC), whole seconds


def stamp(epoch):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def gated_pod(name, gate, created=T0, app="app-1", ns="lab", template="t1"):
    """A pod of app with its gate's status: ("running", started) or
    ("terminated", finished, message)."""
    state = ({"running": {"startedAt": stamp(gate[1])}} if gate[0] == "running" else
             {"terminated": {"exitCode": 0, "reason": "Completed", "finishedAt": stamp(gate[1]), "message": gate[2]}})
    return {"metadata": {"name": name, "namespace": ns, "labels": {"app": app, "pod-template-hash": template},
                         "creationTimestamp": stamp(created)},
            "spec": {}, "status": {"initContainerStatuses": [{"name": "homestead-start-gate", "state": state}]}}


class StateTests(Fixture):
    """What the person is told: rows, alerts, flow."""

    def setUp(self):
        super().setUp()
        self.put("lab", "app-1", stored(("lab", "app-2", 5432), ("data", "db", 5432)))
        START.apply_to(self.dep("lab", "app-1"), START.read(self.dep("lab", "app-1")))
        self.dep("lab", "app-2")["status"]["readyReplicas"] = 0

    def rows(self, pods, now=T0 + 120):
        rows = []
        for dep in self.deps:
            ns, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
            mine = [p for p in pods if (p["metadata"]["namespace"], p["metadata"]["labels"].get("app")) == (ns, name)]
            rows.append({"ns": ns, "name": name, **START.row_fields(dep, mine, self.deps)})
        return rows

    def row(self, pods, name="app-1"):
        return next(r for r in self.rows(pods) if r["name"] == name)

    def test_running_gate_is_waiting(self):
        got = START.pod_wait(self.dep("lab", "app-1"), [gated_pod("p1", ("running", T0 + 5))], self.deps)
        self.assertEqual({"waiting_for": ["app-2"], "since": T0 + 5}, got)
        row = self.row([gated_pod("p1", ("running", T0 + 5))])
        self.assertEqual((["app-2", "db"], "rendered"), (row["starts_after"], row["start_gate"]))

    def test_timeout_message_is_started_without(self):
        pods = [gated_pod("old", ("terminated", T0 - 900, ""), created=T0 - 1000),
                gated_pod("new", ("terminated", T0 + 300, "timeout app-2.lab.svc:5432"), created=T0)]
        self.assertEqual({"started_without": ["app-2"], "at": T0 + 300},
                         START.pod_wait(self.dep("lab", "app-1"), pods, self.deps))

    def test_any_current_replica_that_gave_up_is_started_without(self):
        pods = [gated_pod("a", ("terminated", T0 + 300, "timeout app-2.lab.svc:5432"), created=T0),
                gated_pod("b", ("terminated", T0 + 20, ""), created=T0 + 10)]
        self.assertEqual({"started_without": ["app-2"], "at": T0 + 300},
                         START.pod_wait(self.dep("lab", "app-1"), pods, self.deps))
        replaced = [dict(pods[0]), gated_pod("b", ("terminated", T0 + 20, ""), created=T0 + 10, template="t2")]
        self.assertIsNone(START.pod_wait(self.dep("lab", "app-1"), replaced, self.deps),
                          "a pod of the template before is not the app as it runs now")

    def test_clean_exit_is_none(self):
        self.assertIsNone(START.pod_wait(self.dep("lab", "app-1"), [gated_pod("p1", ("terminated", T0, ""))], self.deps))
        self.assertIsNone(START.pod_wait(self.dep("lab", "app-1"), [], self.deps))
        self.assertIsNone(START.pod_wait(self.dep("lab", "app-3"), [gated_pod("p1", ("running", T0))], self.deps),
                          "no setting, no wait")

    def test_no_alert_within_timeout(self):
        pods = [gated_pod("p1", ("running", T0))]
        self.assertEqual([], START.alert_facts(self.rows(pods), now=T0 + 120))
        self.assertEqual([], START.alert_facts(self.rows(pods), now=T0 + 400), "start anyway: the gate goes on by itself")
        self.put("lab", "app-1", stored(("lab", "app-2", 5432), ("data", "db", 5432), on_timeout="wait"))
        START.apply_to(self.dep("lab", "app-1"), START.read(self.dep("lab", "app-1")))
        self.assertEqual([], START.alert_facts(self.rows(pods), now=T0 + 120))
        facts = START.alert_facts(self.rows(pods), now=T0 + 300)
        self.assertEqual(["starts-after:waiting:lab/app-1"], [f["key"] for f in facts])
        self.assertEqual("app-1 is waiting for app-2", facts[0]["title"])
        self.assertEqual("degraded", facts[0]["severity"])

    def test_started_without_resolves_on_next_clean_start(self):
        late = [gated_pod("p1", ("terminated", T0 + 300, "timeout app-2.lab.svc:5432"))]
        facts = START.alert_facts(self.rows(late), now=T0 + 400)
        self.assertEqual(["starts-after:started-without:lab/app-1"], [f["key"] for f in facts])
        self.assertEqual("app-1 started without app-2", facts[0]["title"])
        self.assertIn("app-2 was not ready after 5 min", facts[0]["body"])
        clean = late + [gated_pod("p2", ("terminated", T0 + 900, ""), created=T0 + 800, template="t2")]
        self.assertEqual([], START.alert_facts(self.rows(clean), now=T0 + 1000))

    def test_a_gone_dependency_is_named_not_fatal(self):
        self.deps = [d for d in self.deps if d["metadata"]["name"] != "app-2"]
        row = self.row([gated_pod("p1", ("running", T0))])
        self.assertEqual(["app-2"], row["start_wait"]["waiting_for"])
        facts = START.alert_facts(self.rows([gated_pod("p1", ("running", T0))]), now=T0 + 10)
        gone = next(f for f in facts if f["key"] == "starts-after:gone:lab/app-1")
        self.assertEqual("app-1 waits for app-2, which no longer exists", gone["title"])
        self.assertIn("Remove it from Starts after", gone["body"])
        # And through the Apps list itself, with nothing else bound.
        import server
        items = {"/apis/apps/v1/deployments": {"items": self.deps},
                 "/api/v1/pods": {"items": [gated_pod("p1", ("running", T0))]}}
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=lambda path, **kw: copy.deepcopy(items.get(path, {"items": []}))):
            rows = {(w["ns"], w["name"]): w for w in server.WORKLOADS.get_workloads()}
        self.assertEqual(["app-2"], rows[("lab", "app-1")]["start_wait"]["waiting_for"])
        self.assertEqual(["app-2", "db"], rows[("lab", "app-1")]["starts_after"])
        self.assertIsNone(rows[("lab", "app-3")]["start_wait"])

    def test_outside_cycle_alert(self):
        self.put("lab", "app-2", stored(("lab", "app-1", 80), on_timeout="wait"))
        facts = START.alert_facts(self.rows([]), now=T0)
        cycle = next(f for f in facts if f["key"].startswith("starts-after:cycle:"))
        self.assertEqual("starts-after:cycle:lab/app-1,lab/app-2", cycle["key"])
        self.assertEqual("app-1 and app-2 wait for each other", cycle["title"])
        self.assertIn("with Keep waiting neither starts; set one of them to Start anyway", cycle["body"])

    def test_a_gate_that_differs_or_is_not_committed_is_info(self):
        self.dep("lab", "app-1")["spec"]["template"]["spec"]["initContainers"][0]["env"][0]["value"] = "x.lab.svc:1"
        self.dep("lab", "app-3")["metadata"]["labels"] = dict(FLUX)
        self.put("lab", "app-3", stored(("data", "db", 5432)))
        facts = {f["key"]: f for f in START.alert_facts(self.rows([]), now=T0)}
        differs = facts["starts-after:differs:lab/app-1"]
        self.assertEqual(("info", "app-1's start gate does not match its setting"), (differs["severity"], differs["title"]))
        # Advice that works: the same setting saved again is no change, so it
        # names restore_gate (SaveTests.test_restore_gate_puts_the_gate_back).
        self.assertIn('save its Starts after again with "restore_gate": true to put the gate back; that restarts it once',
                      differs["body"])
        self.assertNotIn("save Starts after in Edit", differs["body"])
        flux = facts["starts-after:differs:lab/app-3"]
        self.assertIn("commit the lines /api/workloads/starts-after gives for it", flux["body"])

    def test_bad_rows_never_break_the_alerts(self):
        self.assertEqual([], START.alert_facts([{"ns": "lab", "name": "x"}, {}], now=T0))
        self.assertEqual([], START.alert_facts(None, now=T0))


GOOD = {"ns": "lab", "app": "app-2", "service": "app-2", "port": 5432}
MALFORMED = [
    {"after": [1]},
    {"after": [{"ns": "lab", "app": "app-2"}], "timeout": 300, "on_timeout": "start"},          # no service, no port
    {"after": [dict(GOOD, port="5432")], "timeout": 300, "on_timeout": "start"},
    {"after": [dict(GOOD, port=True)], "timeout": 300, "on_timeout": "start"},
    {"after": [dict(GOOD, port=70000)], "timeout": 300, "on_timeout": "start"},
    {"after": [dict(GOOD, ns=7)], "timeout": 300, "on_timeout": "start"},
    {"after": [dict(GOOD, app="app 2 reboot")], "timeout": 300, "on_timeout": "start"},
    {"after": [GOOD], "timeout": "300", "on_timeout": "start"},
    {"after": [GOOD], "timeout": 5, "on_timeout": "start"},
    {"after": [GOOD], "on_timeout": "start"},
    {"after": [GOOD], "timeout": 300, "on_timeout": "later"},
    {"after": "app-2", "timeout": 300, "on_timeout": "start"},
]


class MalformedTests(Fixture):
    """A setting edited by hand, or broken, is shown on its app and never
    breaks the Apps list (security review, 2026-10-22)."""

    def test_every_malformed_setting_reads_as_unreadable(self):
        for bad in MALFORMED:
            with self.subTest(bad=bad):
                dep = deployment("lab", "app-1", after=bad)
                self.assertEqual({"unreadable": True}, START.read(dep))
                pods = [gated_pod("p1", ("terminated", T0, "timeout app-2.lab.svc:5432"))]
                fields = START.row_fields(dep, pods, self.deps)
                self.assertEqual(([], "differs", None, None, True),
                                 (fields["starts_after"], fields["start_gate"], fields["start_wait"],
                                  fields["start_setting"], fields["start_unreadable"]))
                self.assertEqual([], START.after_of(dep))
                facts = START.alert_facts([{"ns": "lab", "name": "app-1", **fields}], now=T0)
                self.assertEqual(["starts-after:differs:lab/app-1"], [f["key"] for f in facts])
        self.assertEqual(stored(("lab", "app-2", 5432)), START.read(deployment("lab", "app-1", after=stored(("lab", "app-2", 5432)))))

    def test_the_apps_list_still_loads_with_that_app_marked(self):
        import server
        self.deps[0] = deployment("lab", "app-1", after=MALFORMED[1])
        self.deps[2] = deployment("lab", "app-3", after=MALFORMED[7])
        items = {"/apis/apps/v1/deployments": {"items": self.deps},
                 "/api/v1/pods": {"items": [gated_pod("p1", ("running", T0))]}}
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=lambda path, **kw: copy.deepcopy(items.get(path, {"items": []}))):
            rows = {(w["ns"], w["name"]): w for w in server.WORKLOADS.get_workloads()}
            facts = START.alert_facts(list(rows.values()), now=T0 + 4000)
        self.assertTrue(rows[("lab", "app-1")]["start_unreadable"])
        self.assertTrue(rows[("lab", "app-3")]["start_unreadable"])
        self.assertFalse(rows[("lab", "app-2")]["start_unreadable"])
        self.assertEqual({"starts-after:differs:lab/app-1", "starts-after:differs:lab/app-3"}, {f["key"] for f in facts})

    def test_a_row_that_fails_falls_back_and_the_rest_load(self):
        import server
        items = {"/apis/apps/v1/deployments": {"items": self.deps}, "/api/v1/pods": {"items": []}}
        real = START.row_fields

        def fails_for_app_1(dep, pods, deployments):
            if dep["metadata"]["name"] == "app-1":
                raise KeyError("after")
            return real(dep, pods, deployments)
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=lambda path, **kw: copy.deepcopy(items.get(path, {"items": []}))), \
                mock.patch.object(START, "row_fields", fails_for_app_1):
            rows = {(w["ns"], w["name"]): w for w in server.WORKLOADS.get_workloads()}
        bad = rows[("lab", "app-1")]
        self.assertEqual(([], None, None, True), (bad["starts_after"], bad["start_wait"], bad["start_setting"], bad["start_unreadable"]))
        self.assertEqual("its Starts after setting could not be read", bad["start_note"])
        self.assertIn(("lab", "app-2"), rows)
        self.assertFalse(rows[("lab", "app-2")]["start_unreadable"])


if __name__ == "__main__":
    unittest.main()
