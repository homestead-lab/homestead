"""How hosts share the work: the setting, the standby taint Homestead keeps
in step with it, and the rebalance emptying standby hosts first."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_container_rebalance as C
import homestead_kube as K
import homestead_placement_mode as mode
import server
from test_container_rebalance import APPS, Hosts, bind


def node(name, ready=True, taints=None):
    return {"metadata": {"name": name, "resourceVersion": "7"}, "spec": {"taints": taints or []},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


OURS = dict(mode.TAINT)
THEIRS = {"key": "gpu", "value": "true", "effect": "NoSchedule"}


class SettingTests(unittest.TestCase):
    def test_a_mode_not_chosen_yet_is_allowed_and_standby_needs_a_host(self):
        self.assertEqual({"mode": "", "standby_nodes": []}, mode.validate({}))
        self.assertEqual(["pi"], mode.validate({"mode": "standby", "standby_nodes": ["pi", "pi"]})["standby_nodes"])
        with self.assertRaisesRegex(ValueError, "at least one standby host"):
            mode.validate({"mode": "standby"})
        with self.assertRaisesRegex(ValueError, "default, spread or standby"):
            mode.validate({"mode": "everywhere"})
        with self.assertRaisesRegex(ValueError, "host names"):
            mode.validate({"mode": "standby", "standby_nodes": ["Not A Host!"]})

    def test_settings_keep_the_mode_beside_the_rebalance(self):
        saved = server.APP_SETTINGS.validate({"placement": {"rebalance": "suggest", "mode": "standby", "standby_nodes": ["pi"]}})
        self.assertEqual({"rebalance": "suggest", "mode": "standby", "standby_nodes": ["pi"]}, saved["placement"])
        self.assertEqual({"rebalance": "off", "mode": "", "standby_nodes": []}, server.APP_SETTINGS.validate({})["placement"])


class TaintTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        mode.bind(None, lambda method, path, body, ctype: self.sent.append((method, path, body, ctype)))

    def test_a_standby_host_gets_the_soft_taint_and_keeps_its_own(self):
        changed, _ = mode.reconcile([node("a"), node("pi", taints=[THEIRS])], {"mode": "standby", "standby_nodes": ["pi"]})
        self.assertEqual(["pi is now standby"], changed)
        method, path, body, ctype = self.sent[0]
        self.assertEqual(("PATCH", "/api/v1/nodes/pi", "application/merge-patch+json"), (method, path, ctype))
        self.assertEqual([THEIRS, OURS], body["spec"]["taints"])
        self.assertEqual("PreferNoSchedule", OURS["effect"])
        # Refused if the node's taints changed since they were read.
        self.assertEqual({"resourceVersion": "7"}, body["metadata"])

    def test_a_refused_change_is_a_note_and_the_other_hosts_still_change(self):
        def send(method, path, body, ctype):
            if path.endswith("/pi"):
                raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)
            self.sent.append(path)
        mode.bind(None, send)
        changed, notes = mode.reconcile([node("a"), node("pi"), node("nas")],
                                        {"mode": "standby", "standby_nodes": ["pi", "nas"]})
        self.assertEqual(["nas is now standby"], changed)
        self.assertIn("pi was not changed this time", notes[-1])

    def test_our_key_with_another_effect_is_put_right(self):
        hard = {"key": "homestead.io/standby", "value": "true", "effect": "NoSchedule"}
        changed, _ = mode.reconcile([node("a"), node("pi", taints=[hard])], {"mode": "standby", "standby_nodes": ["pi"]})
        self.assertEqual(["pi is now standby"], changed)
        self.assertEqual([OURS], self.sent[0][2]["spec"]["taints"])

    def test_a_host_that_times_out_does_not_stop_the_others_and_is_said(self):
        def send(method, path, body, ctype):
            if path.endswith("/b"):
                raise urllib.error.URLError("timed out")
            self.sent.append(path)
        mode.bind(None, send)
        self.addCleanup(mode._failures.clear)
        nodes = [node("a"), node("b"), node("c")]
        changed, notes = mode.reconcile(nodes, {"mode": "standby", "standby_nodes": ["b", "c"]})
        self.assertEqual(["/api/v1/nodes/c"], self.sent)
        self.assertIn("timed out", notes[0])
        summary = mode.summary(nodes, {"mode": "standby", "standby_nodes": ["b", "c"]})
        self.assertIn("b could not be marked standby", " ".join(summary["notes"]))
        self.assertIn("timed out", {h["name"]: h for h in summary["hosts"]}["b"]["error"])

    def test_a_tiebreaker_with_a_hard_taint_does_not_count_as_taking_work(self):
        pi = node("pi", taints=[{"key": "homestead.io/role", "value": "tiebreaker", "effect": "NoSchedule"}])
        self.assertFalse(mode.takes_work(pi))
        changed, notes = mode.reconcile([node("a"), pi], {"mode": "standby", "standby_nodes": ["a"]})
        self.assertEqual([], changed)
        self.assertIn("no other host takes work", " ".join(notes))

    def test_leaving_standby_mode_removes_only_homesteads_taint(self):
        changed, _ = mode.reconcile([node("pi", taints=[THEIRS, OURS]), node("bare", taints=[OURS])], {"mode": "spread"})
        self.assertEqual({"pi takes apps again", "bare takes apps again"}, set(changed))
        bodies = {path: body for _, path, body, _ in self.sent}
        self.assertEqual([THEIRS], bodies["/api/v1/nodes/pi"]["spec"]["taints"])
        self.assertIsNone(bodies["/api/v1/nodes/bare"]["spec"]["taints"])

    def test_nothing_is_written_when_the_taints_already_match(self):
        mode.reconcile([node("a"), node("pi", taints=[OURS])], {"mode": "standby", "standby_nodes": ["pi"]})
        self.assertEqual([], self.sent)

    def test_the_last_host_taking_work_is_never_made_standby(self):
        changed, notes = mode.reconcile([node("a", ready=False), node("pi")], {"mode": "standby", "standby_nodes": ["pi"]})
        self.assertEqual([], changed)
        self.assertIn("no other host takes work", notes[0])

    def test_a_named_host_not_in_the_cluster_is_said(self):
        _, notes = mode.reconcile([node("a")], {"mode": "standby", "standby_nodes": ["gone"]})
        self.assertIn("gone is not a host", notes[0])

    def test_the_summary_counts_hosts_and_applies_with_two_taking_work(self):
        found = mode.summary([node("a"), node("pi", taints=[OURS])], {"mode": "standby", "standby_nodes": ["pi"]}, 3)
        self.assertEqual(("standby", 3, True), (found["mode"], found["unreserved"], found["applies"]))
        self.assertEqual([False, True], [h["standby"] for h in found["hosts"]])
        self.assertFalse(mode.summary([node("solo")], {}, 0)["applies"])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mode.bind, None, None, lambda: {}, ())

    def test_the_route_counts_apps_reserving_nothing_outside_the_system_namespaces(self):
        def dep(ns, requests):
            return {"metadata": {"namespace": ns}, "spec": {"template": {"spec": {"containers": [
                {"name": "app", "resources": {"requests": requests} if requests else {}}]}}}}
        listed = {"/api/v1/nodes": {"items": [node("a"), node("pi", taints=[OURS])]},
                  "/apis/apps/v1/deployments": {"items": [dep("lab", None), dep("lab", {"cpu": "50m"}), dep("kube-system", None)]}}
        mode.bind(listed.__getitem__, None, lambda: {"placement": {"mode": "standby", "standby_nodes": ["pi"]}}, {"kube-system"})
        import homestead_routes as ROUTES
        role, handler = mode.ROUTES[("GET", "/api/placement")]
        found = handler(ROUTES.Request("GET", "/api/placement", {}, None, "ada", "viewer"))
        self.assertEqual(("viewer", "standby", 1), (role, found["mode"], found["unreserved"]))
        self.assertEqual(["pi"], mode.standby_hosts())


class StandbyRebalanceTests(unittest.TestCase):
    def test_the_review_route_plans_around_the_standby_hosts(self):
        import homestead_routes as ROUTES
        c = Hosts(self)
        C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), c.apply, ("lab", "homestead"), lambda: True,
               None, None, None, lambda: ["k3"])
        found = C.ROUTES[("GET", "/api/workloads/rebalance/plan")][1](
            ROUTES.Request("GET", "/api/workloads/rebalance/plan", {}, None, "ada", "admin"))
        self.assertTrue(found["moves"])
        self.assertNotIn("k3", {m["to"] for m in found["moves"]})

    def test_apps_on_a_standby_host_move_off_it_and_nothing_moves_onto_one(self):
        c = Hosts(self)
        bind(c)
        moves = C.plan(avoid=["k3"])["moves"]
        self.assertTrue(moves)
        self.assertNotIn("k3", [m["to"] for m in moves])

    def test_a_standby_host_is_emptied_even_when_it_is_not_the_busiest(self):
        c = Hosts(self)
        # Everything runs on k1, which is quiet; k2 and k3 have room.
        for app in APPS:
            c.edit_pod(app, lambda p: p["spec"].update(nodeName="k1"))
        bind(c)
        moved = C.plan(avoid=["k1"])["moves"]
        self.assertTrue(moved)
        self.assertEqual({"k1"}, {m["from"] for m in moved})

    def test_nothing_moves_off_a_standby_host_onto_one_that_would_be_too_full(self):
        c = Hosts(self)
        # The spare k3 runs the apps; k1 and k2 are past SPARE_ROOM already.
        for app in APPS:
            c.edit_pod(app, lambda p: p["spec"].update(nodeName="k3"))
        for name, memory in {"k1": "15Gi", "k2": "15Gi", "k3": "11Gi"}.items():
            c.edit_node_metrics(name, lambda m: m["usage"].update(memory=memory))
        bind(c)
        self.assertEqual([], C.plan(avoid=["k3"])["moves"])

    def test_an_empty_spare_does_not_change_how_even_the_main_hosts_are(self):
        without = Hosts(self)
        without.kube.discard("/api/v1/nodes/k3")
        without.kube.discard("/apis/metrics.k8s.io/v1beta1/nodes/k3")
        bind(without)
        alone = C.plan()
        with_spare = Hosts(self)    # the shared fake serves one cluster at a time: this one, from here
        with_spare.edit_node_metrics("k3", lambda m: m.update(usage={"cpu": "0", "memory": "0"}))
        bind(with_spare)
        spared = C.plan(avoid=["k3"])
        self.assertEqual(alone["spread_before"], spared["spread_before"])
        self.assertEqual([(m["id"], m["to"]) for m in alone["moves"]], [(m["id"], m["to"]) for m in spared["moves"]])

    def test_an_operators_plan_leaves_admin_only_apps_on_a_standby_host(self):
        c = Hosts(self)
        for app in APPS:
            c.edit_pod(app, lambda p: p["spec"].update(nodeName="k1"))

        def guard(dep, ns, role):
            if role == "operator" and dep["metadata"]["name"] == "frigate":
                raise PermissionError("admin only")
        C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), c.apply, ("lab", "homestead"), lambda: True, None, guard)
        found = C.plan(avoid=["k1"], role="operator")
        self.assertTrue(found["moves"])
        self.assertNotIn("lab/frigate", [m["id"] for m in found["moves"]])
        self.assertIn({"id": "lab/frigate", "why": C.ADMIN_ONLY}, found["skipped"])


class TickTests(unittest.TestCase):
    def test_each_look_keeps_the_taints_in_step_and_plans_around_standby(self):
        import homestead_recovery as recovery
        settings = {"placement": {"rebalance": "suggest", "mode": "standby", "standby_nodes": ["pi"]},
                    "updates": {"maintenance": {"days": [], "start": "02:00", "duration_minutes": 60}}}
        plans = []
        jobs = mock.Mock(TERMINAL={"succeeded"}, snapshot=lambda: [])
        bound = (recovery.read, recovery.settings, recovery.operations, recovery.plan, recovery.start, recovery.WAITS_FOR)
        self.addCleanup(lambda: recovery.bind(recovery.DATA_DIR, *bound[:5], waits_for=bound[5]))
        recovery.bind(recovery.DATA_DIR, lambda path: {"items": [node("a"), node("pi")]}, lambda: settings, jobs,
                      lambda exclude=(), avoid=(): plans.append(avoid) or {"moves": [], "excluded": []}, mock.Mock())
        with mock.patch.object(recovery.PLACEMODE, "reconcile") as reconcile, \
                mock.patch.object(recovery, "observe", return_value={"a": 1}), \
                mock.patch.object(recovery, "suggest"), \
                mock.patch.object(recovery, "_planned", [0.0]):
            recovery.tick(1_760_000_000)
        reconcile.assert_called_once()
        self.assertEqual([["pi"]], plans)

    def test_a_failed_taint_change_does_not_stop_hosts_coming_back_from_being_noticed(self):
        import homestead_recovery as recovery
        settings = {"placement": {"rebalance": "off", "mode": "standby", "standby_nodes": ["pi"]}}
        bound = (recovery.read, recovery.settings, recovery.operations, recovery.plan, recovery.start, recovery.WAITS_FOR)
        self.addCleanup(lambda: recovery.bind(recovery.DATA_DIR, *bound[:5], waits_for=bound[5]))
        recovery.bind(recovery.DATA_DIR, lambda path: {"items": [node("a"), node("pi")]}, lambda: settings,
                      mock.Mock(), mock.Mock(), mock.Mock())
        with mock.patch.object(recovery.PLACEMODE, "reconcile", side_effect=RuntimeError("API down")), \
                mock.patch.object(recovery, "observe", return_value={}) as observe:
            self.assertIsNone(recovery.tick(1_760_000_000))
        observe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
