"""Moving an app back by restarting it: an app a tool applies can't take a new
placement, but the scheduler can place it again. The plan says where a restart
would land it, or why it can't; the job restarts it once and looks where it went.
On the shared fake API."""
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_container_rebalance as C
import homestead_kube as K
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_priority as PRIORITY
import homestead_recovery as RECOVERY
import server
from rebind import keep_bound, rebind
from test_container_rebalance import Hosts, bind, deployment, pod, replicaset

FLUX = {"lab/frigate": "Flux"}
APPS_HERE = ("frigate", "ha", "plex", "tiny")
LADDER = PRIORITY.ladder([{"metadata": {"name": "everyday"}, "value": 0, "globalDefault": True}])
PATH = "/apis/apps/v1/namespaces/lab/deployments/frigate"
MARK = NAMES.key("balance-preference")
RESTARTED = NAMES.key("restartedAt")


def preference(host, weight=100):
    return {"weight": weight, "preference": {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [host]}]}}


def prefers(dep, *terms):
    dep["spec"]["template"]["spec"]["affinity"] = {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": list(terms)}}


def mark(dep, host):
    dep["metadata"].setdefault("annotations", {})[MARK] = host


def reserving(name, host, cpu="3", memory="12Gi"):
    """Something else on host, taking its room in requests, using nothing."""
    made = pod(name, host)
    made["spec"]["containers"] = [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": memory}}}]
    return made


def moves_of(plan):
    return {m["id"]: m for m in plan["moves"]}


def why_of(plan):
    return {s["id"]: s["why"] for s in plan["skipped"]}


class Landing(unittest.TestCase):
    def setUp(self):
        keep_bound(self, C, CAPACITY_REVIEW)
        self.c = Hosts(self)
        bind(self.c)
        self.c.kube.add(reserving("busy", "k2", cpu="1", memory="4Gi"))

    def plan(self, back=("k1",)):
        return C.plan(restart=FLUX, back=back)

    def test_a_restart_app_goes_where_the_scheduler_would_put_it(self):
        self.c.kube.add(reserving("full", "k1"))
        move = moves_of(self.plan(("k1", "k3")))["lab/frigate"]
        self.assertEqual(("k3", "restart", "Flux"), (move["to"], move["how"], move["by"]))

    def test_a_preference_for_the_returned_host_moves_it_there(self):
        self.c.kube.add(reserving("full", "k1"))          # the scheduler's room would say k3
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k1")))
        self.assertEqual("k1", moves_of(self.plan())["lab/frigate"]["to"])

    def test_a_preference_for_its_own_host_leaves_it_with_why(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        plan = self.plan()
        self.assertNotIn("lab/frigate", moves_of(plan))
        self.assertIn("preference keeps it on k2", why_of(plan)["lab/frigate"])

    def test_a_preference_for_another_host_leaves_it_with_why(self):
        # Andreas, 7 Nov: not a host coming back, so it is a person's and stays.
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k3")))
        plan = self.plan(("k1",))
        self.assertNotIn("lab/frigate", moves_of(plan))
        self.assertIn("preference for k3", why_of(plan)["lab/frigate"])

    def test_balances_own_preference_does_not_count(self):
        self.c.kube.add(reserving("full", "k1"))
        def own(dep):
            prefers(dep, preference("k2"))
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        self.assertEqual("k3", moves_of(self.plan(("k1", "k3")))["lab/frigate"]["to"])

    def test_a_mark_that_does_not_match_counts_as_a_persons(self):
        def stale(dep):
            prefers(dep, preference("k2"))
            mark(dep, "k3")
        self.c.edit_dep("frigate", stale)
        self.assertIn("preference keeps it on k2", why_of(self.plan())["lab/frigate"])

    def test_a_preference_not_for_one_host_leaves_it_with_why(self):
        term = preference("k1")
        term["preference"]["matchExpressions"][0]["values"] = ["k1", "k2"]
        self.c.edit_dep("frigate", lambda d: prefers(d, term))
        self.assertIn("isn't for one host", why_of(self.plan())["lab/frigate"])

    def test_reservations_favouring_its_host_leave_it_with_why(self):
        for host in ("k1", "k3"):
            self.c.kube.add(reserving(f"full-{host}", host))
        plan = self.plan()
        self.assertNotIn("lab/frigate", moves_of(plan))
        self.assertIn("would put it back on k2", why_of(plan)["lab/frigate"])

    def test_its_own_requests_are_not_counted_against_its_host(self):
        # Each host has 1 CPU reserved by something else, so a tie: it stays. Counting
        # its own 3 CPUs against k2 as well would send it away.
        self.c.edit_dep("frigate", lambda d: d["spec"].update(strategy={"type": "Recreate"}))
        for host in ("k1", "k3"):
            self.c.kube.add(reserving(f"same-{host}", host, cpu="1", memory="4Gi"))
        own = [{"name": "c", "resources": {"requests": {"cpu": "3"}}}]
        self.c.edit_pod("frigate", lambda p: p["spec"].update(containers=own))
        self.c.edit_dep("frigate", lambda d: d["spec"]["template"]["spec"].update(containers=own))
        self.assertIn("would put it back on k2", why_of(self.plan())["lab/frigate"], "a tie stays where it is")

    def test_a_host_that_takes_no_work_is_not_a_landing(self):
        self.c.kube.add(reserving("full", "k1"))
        self.c.cordon("k3")
        self.assertNotEqual("k3", moves_of(self.plan()).get("lab/frigate", {}).get("to"))

    def test_a_restart_app_stays_when_the_likely_host_has_no_copy(self):
        self.c.kube.add(reserving("full", "k1"))
        self.c.kube.add(deployment("frigate", claim="data"))
        self.c.edit_dep("frigate", lambda d: d["spec"].update(strategy={"type": "Recreate"}))
        self.c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": "v1"},
                         "spec": {"dataLocality": "best-effort"}, "status": {"kubernetesStatus": {"namespace": "lab", "pvcName": "data"}}})
        self.c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Replica", "metadata": {"namespace": "longhorn-system", "name": "r1"},
                         "spec": {"volumeName": "v1", "nodeID": "k1"}})
        plan = self.plan()
        self.assertNotIn("lab/frigate", moves_of(plan))
        self.assertIn("no copy of its volumes", why_of(plan)["lab/frigate"])

    def test_restart_apps_take_no_part_in_a_chained_move(self):
        # As PlanTests' chain, which moves dns off k3 so frigate can go there.
        self.c.add_app("dns", "k3", "100m", "512Mi")
        self.c.each_dep(lambda dep: dep["spec"]["template"].setdefault("metadata", {"labels": {"app": dep["metadata"]["name"]}}))
        self.c.edit_dep("dns", lambda dep: dep["spec"]["template"]["spec"].update(affinity={"podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {"labelSelector": {"matchLabels": {"app": "frigate"}}, "namespaces": ["lab"], "topologyKey": "kubernetes.io/hostname"}]}}))
        C.bind(K.kget, lambda dep: dep["metadata"]["name"], lambda n, r: (r != "frigate" or n["metadata"]["name"] == "k3", ["no Coral"]),
               self.c.apply, ("lab", "homestead"), lambda: True)
        self.assertIn("makes room", " ".join(m.get("note", "") for m in C.plan()["moves"]), "the chain without a restart")
        for restart in ({"lab/dns": "Flux"}, {"lab/frigate": "Flux"}):
            plan = C.plan(restart=restart, back=("k1", "k3"))
            self.assertFalse([m for m in plan["moves"] if "makes room" in m.get("note", "") or "leaves" in m.get("note", "")], restart)

    def test_a_paused_or_mid_rollout_app_is_not_restarted(self):
        changes = {"its rollouts are paused": lambda d: d["spec"].update(paused=True),
                   "still rolling": lambda d: d["status"].update(updatedReplicas=0),
                   "still rolling ": lambda d: (d["metadata"].update(generation=2)),
                   "nodeName": lambda d: d["spec"]["template"]["spec"].update(nodeName="k2"),
                   "spread rules": lambda d: d["spec"]["template"]["spec"].update(topologySpreadConstraints=[{"maxSkew": 1}])}
        for word, change in changes.items():
            self.c.kube.add(deployment("frigate"))
            self.c.edit_dep("frigate", change)
            plan = self.plan()
            self.assertNotIn("lab/frigate", moves_of(plan), word)
            self.assertIn(word.strip(), why_of(plan)["lab/frigate"])

    def stranded(self, plan, app="lab/frigate"):
        return {k["id"]: k["why"] for k in plan["kept"]}.get(app, "")

    def test_a_persons_preference_on_an_app_no_tool_applies_is_left_out(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        plan = C.plan(restart={}, back=("k1",), may_restart=True)
        self.assertNotIn("lab/frigate", moves_of(plan))
        self.assertIn("preference keeps it on k2", self.stranded(plan))

    def test_a_persons_preference_for_the_returned_host_uses_a_restart_never_a_placement(self):
        self.c.kube.add(reserving("full", "k3"))
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k1")))
        move = moves_of(C.plan(restart={}, back=("k1",), may_restart=True))["lab/frigate"]
        self.assertEqual(("k1", "restart", ""), (move["to"], move["how"], move["by"]))

    def test_without_restarts_a_persons_preference_is_left_out_with_why(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k1")))
        plan = C.plan(restart={}, back=("k1",), may_restart=False)
        self.assertNotIn("lab/frigate", moves_of(plan))
        self.assertIn("Spread by capacity", self.stranded(plan))

    def test_balances_own_preference_on_an_app_no_tool_applies_moves_by_placement(self):
        def own(dep):
            prefers(dep, preference("k2"))
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        move = moves_of(C.plan(restart={}, back=("k1",)))["lab/frigate"]
        self.assertNotIn("how", move)

    def test_a_person_reviewing_balance_hosts_still_sees_it_as_movable(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        self.assertIn("lab/frigate", moves_of(C.plan()))
        self.assertNotIn("kept", C.plan())

    def test_every_move_of_an_unattended_run_carries_its_generation(self):
        for move in C.plan(restart={}, back=("k1",))["moves"]:
            self.assertEqual(1, move["generation"], move["id"])
        self.assertFalse([m for m in C.plan()["moves"] if "generation" in m], "a person's plan binds it at review")

    def rolling_with_a_volume(self, access):
        self.c.kube.add(reserving("full", "k1"))
        self.c.kube.add(deployment("frigate", claim="data"))
        self.c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": "v1"},
                         "spec": {"dataLocality": "disabled", **({"accessMode": access} if access else {})},
                         "status": {"kubernetesStatus": {"namespace": "lab", "pvcName": "data"}}})

    def test_a_persons_term_beside_balances_own_still_counts_as_theirs(self):
        def both(dep):
            prefers(dep, preference("k2"), preference("k3", weight=10))
            mark(dep, "k2")
        self.c.edit_dep("frigate", both)
        self.assertTrue(PLACE.person_preference(self.c.dep("frigate")))
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        self.assertFalse(PLACE.person_preference(self.c.dep("frigate")))

    def test_a_refused_landing_keeps_its_reason_in_the_list(self):
        self.c.kube.add(reserving("full", "k1"))
        self.c.kube.add(deployment("frigate", claim="data"))
        self.c.edit_dep("frigate", lambda d: d["spec"].update(strategy={"type": "Recreate"}))
        self.c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": "v1"},
                         "spec": {"dataLocality": "best-effort"}, "status": {"kubernetesStatus": {"namespace": "lab", "pvcName": "data"}}})
        self.c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Replica", "metadata": {"namespace": "longhorn-system", "name": "r1"},
                         "spec": {"volumeName": "v1", "nodeID": "k1"}})
        self.assertIn("no copy of its volumes", self.stranded(self.plan()))

    def test_a_manual_plan_names_a_persons_preference_it_replaces(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        self.assertEqual("k2", moves_of(C.plan())["lab/frigate"]["replaces"])

    def test_a_manual_plan_names_nothing_for_balances_own(self):
        def own(dep):
            prefers(dep, preference("k2"))
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        self.assertNotIn("replaces", moves_of(C.plan())["lab/frigate"])

    def test_a_rolling_update_on_a_one_host_volume_is_not_restarted(self):
        # The new pod waits for a volume the old one holds: Multi-Attach, then ten minutes lost.
        for access in ("rwo", None):
            self.rolling_with_a_volume(access)
            plan = self.plan(("k1", "k3"))
            self.assertNotIn("lab/frigate", moves_of(plan), access)
            self.assertIn("attaches to one host", self.stranded(plan))

    def test_a_rolling_update_on_a_shared_volume_or_a_recreate_is_fine(self):
        self.rolling_with_a_volume("rwx")
        self.assertIn("lab/frigate", moves_of(self.plan(("k1", "k3"))))
        self.rolling_with_a_volume("rwo")
        self.c.edit_dep("frigate", lambda d: d["spec"].update(strategy={"type": "Recreate"}))
        self.assertIn("lab/frigate", moves_of(self.plan(("k1", "k3"))))

    def test_an_app_whose_volume_has_a_copy_being_rebuilt_anywhere_stays(self):
        self.rolling_with_a_volume("rwx")
        for mode in ("WO", "ERR"):
            self.c.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Engine", "metadata": {"namespace": "longhorn-system", "name": "v1-e-0"},
                             "spec": {"volumeName": "v1"}, "status": {"replicaModeMap": {"r1": "RW", "r3": mode}}})
            plan = self.plan(("k1", "k3"))
            self.assertNotIn("lab/frigate", moves_of(plan), mode)
            self.assertIn("still being rebuilt", self.stranded(plan))

    def test_flux_system_never_moves(self):
        controller = deployment("controller")
        controller["metadata"]["namespace"] = "flux-system"
        running = pod("controller", "k2")
        running["metadata"]["namespace"] = "flux-system"
        made = replicaset("controller")
        made["metadata"]["namespace"] = "flux-system"
        for obj in (controller, running, made):
            self.c.kube.add(obj)
        self.assertNotIn("flux-system/controller", {a["id"] for a in C.inventory()[1]})

    def test_a_rolling_app_keeps_its_own_requests_against_its_host_while_it_rolls(self):
        for host in ("k1", "k3"):
            self.c.kube.add(reserving(f"same-{host}", host, cpu="2", memory="4Gi"))
        own = [{"name": "c", "resources": {"requests": {"cpu": "2"}}}]
        self.c.edit_pod("frigate", lambda p: p["spec"].update(containers=own))
        self.c.edit_dep("frigate", lambda d: d["spec"]["template"]["spec"].update(containers=own))
        self.assertIn("lab/frigate", moves_of(self.plan(("k1", "k3"))), "the old pod is still there when the new one is placed")
        self.c.edit_dep("frigate", lambda d: d["spec"].update(strategy={"type": "Recreate"}))
        self.assertNotIn("lab/frigate", moves_of(self.plan(("k1", "k3"))))

    def test_manual_plan_output_is_unchanged_without_restart(self):
        plain = C.plan()
        self.assertTrue(plain["moves"])
        self.assertFalse([m for m in plain["moves"] if "how" in m or "by" in m])
        self.assertEqual(plain["moves"], C.plan(restart=None, back=())["moves"])


class RestartJob(unittest.TestCase):
    def setUp(self):
        keep_bound(self, C, CAPACITY_REVIEW)
        self.c = Hosts(self)
        bind(self.c)
        C.restart = server.OVERVIEW_ROUTES.restart_workload
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = {"placement": {"mode": "spread", "rebalance": "window"}}
        self.tool = {"by": "Flux", "detected": True}
        rebind(self, RECOVERY, folder.name, K.kget, lambda: self.settings, OPS, None, None, (), _managed_by=lambda dep: self.tool)
        self.item = {"id": "job", "ref": {"moves": [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1",
                                                     "how": "restart", "by": "Flux"}],
                                          "index": 0, "moved": 0, "stage": "move", "results": []}}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(C, "_pull_first", return_value=([], [])).start()

    def writes(self):
        return [w for w in self.c.kube.writes if w[1] == PATH]

    def restarted(self, host):
        """The rollout the restart caused, and where it put the pod."""
        def roll(dep):
            dep["metadata"]["generation"] = 2
            dep["status"]["observedGeneration"] = 2
        self.c.edit_dep("frigate", roll)
        self.c.edit_pod("frigate", lambda p: p["spec"].update(nodeName=host))

    def test_a_restart_move_never_writes_placement(self):
        C.status(self.item)
        self.assertEqual([], self.c.moved, "the placement move")
        (method, _, body, ctype), = self.writes()
        self.assertEqual(("PATCH", "application/merge-patch+json"), (method, ctype))
        self.assertEqual({"template"}, set(body["spec"]), "no replicas, strategy or placement")
        self.assertEqual({"metadata"}, set(body["spec"]["template"]))
        self.assertEqual([RESTARTED], list(body["spec"]["template"]["metadata"]["annotations"]))
        self.assertNotIn(MARK, (body.get("metadata") or {}).get("annotations") or {})

    def test_a_marked_preference_goes_in_the_same_patch_as_the_restart(self):
        def own(dep):
            prefers(dep, preference("k2"))
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        C.status(self.item)
        (_, _, body, _), = self.writes()
        self.assertIn(RESTARTED, body["spec"]["template"]["metadata"]["annotations"])
        dep = self.c.kube.object(PATH)
        self.assertNotIn("affinity", dep["spec"]["template"]["spec"])
        self.assertNotIn(MARK, dep["metadata"].get("annotations") or {})

    def test_clearing_keeps_every_other_rule(self):
        hardware = {"weight": 50, "preference": {"matchExpressions": [{"key": "hardware/igpu", "operator": "In", "values": ["true"]}]}}
        required = {"nodeSelectorTerms": [{"matchExpressions": [{"key": "disk", "operator": "Exists"}]}]}
        apart = {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": {"app": "x"}}}]}
        def own(dep):
            dep["spec"]["template"]["spec"]["affinity"] = {
                "podAntiAffinity": apart,
                "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": required,
                                 "preferredDuringSchedulingIgnoredDuringExecution": [preference("k2"), hardware]}}
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        C.status(self.item)
        affinity = self.c.kube.object(PATH)["spec"]["template"]["spec"]["affinity"]
        self.assertEqual(apart, affinity["podAntiAffinity"])
        self.assertEqual(required, affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"])
        self.assertEqual([hardware], affinity["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"])

    def test_clearing_keeps_the_rest_of_the_affinity_when_no_node_rule_is_left(self):
        apart = {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": {"app": "x"}}}]}
        def own(dep):
            prefers(dep, preference("k2"))
            dep["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"] = apart
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        C.status(self.item)
        self.assertEqual({"podAntiAffinity": apart}, self.c.kube.object(PATH)["spec"]["template"]["spec"]["affinity"])

    def test_clearing_is_refused_after_someone_changes_the_app(self):
        def own(dep):
            prefers(dep, preference("k2"))
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        also = PLACE.balance_clear_patch(self.c.kube.object(PATH))
        self.c.edit_dep("frigate", lambda d: d["metadata"].update(resourceVersion="999"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            K.ksend("PATCH", PATH, also, ctype="application/merge-patch+json")
        self.assertEqual(409, caught.exception.code)

    def test_clearing_refuses_a_change_made_after_it_read_the_app(self):
        def own(dep):
            prefers(dep, preference("k2"))
            mark(dep, "k2")
        self.c.edit_dep("frigate", own)
        self.c.kube.fail(PATH, 409, method="PATCH")
        C.status(self.item)
        self.assertEqual("skipped", self.item["ref"]["results"][0]["result"])
        self.assertEqual(1, self.item["ref"]["index"])

    def test_a_persons_preference_is_never_touched(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        C.status(self.item)
        (_, _, body, _), = self.writes()
        self.assertNotIn("affinity", body["spec"]["template"].get("spec", {}))
        self.assertTrue(PLACE.preferred_host(self.c.kube.object(PATH)["spec"]["template"]["spec"]))

    def test_a_restart_landing_back_stops_the_run_at_once(self):
        C.status(self.item)
        self.restarted("k2")
        state, _, message = C.status(self.item)
        self.assertEqual("failed", state)
        self.assertIn("restarted on k2, not k1", message)
        self.assertIn("the rest were not moved", message)

    def test_a_restart_still_rolling_is_waited_for(self):
        C.status(self.item)
        self.c.edit_dep("frigate", lambda d: d["metadata"].update(generation=2))
        self.assertEqual("running", C.status(self.item)[0])

    def test_a_restart_landing_on_a_third_host_counts_with_a_note(self):
        C.status(self.item)
        self.restarted("k3")
        C.status(self.item)
        self.assertEqual(1, self.item["ref"]["moved"])
        result = self.item["ref"]["results"][0]
        self.assertEqual("moved", result["result"])
        self.assertIn("landed on k3", result["detail"])

    def watched(self):
        return RECOVERY._load()["marks"].get("lab/frigate")

    def mark_now(self):
        return self.c.kube.object(PATH)["spec"]["template"]["metadata"]["annotations"][RESTARTED]

    def test_a_restart_is_watched_from_the_moment_it_is_sent(self):
        C.status(self.item)
        self.assertEqual((self.mark_now(), "Flux"), (self.watched()["value"], self.watched()["by"]))

    def test_a_restart_landing_back_or_timing_out_is_still_watched(self):
        C.status(self.item)
        self.restarted("k2")
        self.assertEqual("failed", C.status(self.item)[0])
        self.assertTrue(self.watched())

    def test_a_run_cancelled_during_a_restart_is_still_watched(self):
        C.status(self.item)
        C.cancel_run(self.item, {})
        self.assertTrue(self.watched())

    def test_a_sync_taking_the_mark_off_before_it_lands_is_noticed(self):
        C.status(self.item)
        self.restarted("k1")
        self.c.edit_dep("frigate", lambda d: d["spec"]["template"]["metadata"]["annotations"].pop(RESTARTED))
        C.status(self.item)
        self.assertEqual(1, self.item["ref"]["moved"])
        RECOVERY._watch_marks(time.time())
        self.assertEqual("lab/frigate", RECOVERY._load()["undone"]["app"])

    def test_a_lost_reply_is_watched_once_the_mark_shows(self):
        self.c.kube.fail(PATH, TimeoutError("reply lost"), method="PATCH", applied=True)
        C.status(self.item)
        self.assertIsNone(self.watched())
        C.status(self.item)
        self.assertEqual(self.mark_now(), self.watched()["value"])

    def test_a_lost_reply_whose_mark_has_gone_counts_as_undone(self):
        self.c.kube.fail(PATH, TimeoutError("reply lost"), method="PATCH", applied=True)
        C.status(self.item)
        self.restarted("k1")
        self.c.edit_dep("frigate", lambda d: d["spec"]["template"]["metadata"]["annotations"].pop(RESTARTED))
        C.status(self.item)
        self.assertEqual("lab/frigate", RECOVERY._load()["undone"]["app"])

    def test_a_send_that_never_landed_is_not_watched(self):
        self.c.kube.fail(PATH, TimeoutError("no answer"), method="PATCH")
        C.status(self.item)
        C.status(self.item)
        self.assertIsNone(self.watched())
        self.assertEqual({}, RECOVERY._load()["undone"])

    def test_an_old_mark_is_not_taken_for_a_restart_that_never_landed(self):
        self.c.edit_dep("frigate", lambda d: d["spec"]["template"].setdefault("metadata", {}).update(annotations={RESTARTED: "2026-01-01T00:00:00Z"}))
        self.c.kube.fail(PATH, TimeoutError("no answer"), method="PATCH")
        C.status(self.item)
        C.status(self.item)
        self.assertIsNone(self.watched())

    def test_a_no_tool_app_lost_reply_with_no_mark_is_not_undone(self):
        self.item["ref"]["moves"][0]["by"] = ""
        self.tool = {}
        self.c.kube.fail(PATH, TimeoutError("reply lost"), method="PATCH", applied=True)
        C.status(self.item)
        self.restarted("k1")
        self.c.edit_dep("frigate", lambda d: d["spec"]["template"]["metadata"]["annotations"].pop(RESTARTED))
        C.status(self.item)
        self.assertEqual({}, RECOVERY._load()["undone"])

    def test_a_no_tool_restart_is_not_blocked_by_a_tools_removed_mark(self):
        self.item["ref"]["moves"][0]["by"] = ""
        self.tool = {}
        RECOVERY.undone_by("lab/other", "Flux", time.time())
        C.status(self.item)
        self.assertEqual(1, len(self.writes()))

    def test_a_no_tool_restart_stops_if_the_placement_mode_changes_mid_run(self):
        self.item["ref"]["moves"][0]["by"] = ""
        self.tool = {}
        self.settings["placement"]["mode"] = "default"
        self.skipped_without_a_write("no longer moves by restarting")

    def skipped_without_a_write(self, word):
        C.status(self.item)
        self.assertEqual([], self.writes())
        result = self.item["ref"]["results"][0]
        self.assertEqual("skipped", result["result"])
        self.assertIn(word, result["detail"])

    def test_a_mark_removed_earlier_in_the_run_skips_the_later_restarts(self):
        RECOVERY.marked("lab/other", "v", "Flux", time.time())
        RECOVERY.undone_by("lab/other", "Flux", time.time())
        self.skipped_without_a_write("restart mark")

    def test_a_person_marking_it_updated_elsewhere_mid_run_skips_it(self):
        self.tool = {"by": "my script", "detected": False}
        self.skipped_without_a_write("no longer moves by restarting")

    def test_a_restart_for_no_tool_goes_on_only_while_no_tool_applies_it(self):
        self.item["ref"]["moves"][0]["by"] = ""
        self.tool = {"by": "Flux", "detected": True}
        self.skipped_without_a_write("no longer moves by restarting")

    def test_a_restart_for_no_tool_is_not_watched_for_a_tool_removing_its_mark(self):
        self.item["ref"]["moves"][0]["by"] = ""
        self.tool = {}
        C.status(self.item)
        self.assertEqual(1, len(self.writes()))
        self.assertIsNone(self.watched())

    def test_a_tool_going_away_mid_run_skips_it(self):
        self.tool = {}
        self.skipped_without_a_write("no longer moves by restarting")

    def test_leaving_spread_mid_run_skips_it(self):
        self.settings["placement"]["mode"] = "default"
        self.skipped_without_a_write("no longer moves by restarting")

    def test_paused_mid_run_skips_it(self):
        self.c.edit_dep("frigate", lambda d: d["spec"].update(paused=True))
        self.skipped_without_a_write("paused")

    def test_a_pin_set_after_the_plan_is_not_removed_by_a_placement_move(self):
        self.item["ref"]["moves"][0].update(how="", generation=1)
        self.item["ref"]["moves"][0].pop("how")
        def pinned(dep):
            dep["metadata"]["generation"] = 2
            dep["status"]["observedGeneration"] = 2
            dep["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "k2"}
        self.c.edit_dep("frigate", pinned)
        C.status(self.item)
        self.assertEqual([], self.c.moved)
        self.assertEqual("skipped", self.item["ref"]["results"][0]["result"])

    def test_an_edit_since_the_plan_skips_it(self):
        self.c.kube.add(reserving("busy", "k2", cpu="1", memory="4Gi"))
        move = moves_of(C.plan(restart=FLUX, back=("k1", "k3")))["lab/frigate"]
        self.assertEqual(1, move["generation"])
        self.item["ref"]["moves"][0]["generation"] = 1
        def edited(dep):
            dep["metadata"]["generation"] = 2
            dep["status"]["observedGeneration"] = 2
        self.c.edit_dep("frigate", edited)
        self.skipped_without_a_write("changed since")

    def test_a_lost_reply_is_not_sent_twice(self):
        self.c.kube.fail(PATH, TimeoutError("reply lost"), method="PATCH", applied=True)
        C.status(self.item)
        self.assertEqual("starting", self.item["ref"]["stage"])
        self.assertIn(RESTARTED, (self.c.kube.object(PATH)["spec"]["template"]["metadata"] or {}).get("annotations", {}))
        self.restarted("k1")
        C.status(self.item)
        self.assertEqual(1, len(self.writes()), "one restart")
        self.assertEqual(1, self.item["ref"]["moved"])

    def test_a_send_that_never_landed_says_so_and_is_not_sent_again(self):
        self.c.kube.fail(PATH, TimeoutError("no answer"), method="PATCH")
        C.status(self.item)
        state, _, message = C.status(self.item)
        self.assertEqual("failed", state)
        self.assertIn("could not be restarted", message)
        self.assertNotIn("restarted on", message)
        self.assertEqual(1, len(self.writes()))

    def test_an_app_changed_before_the_send_is_skipped_not_restarted_on_top(self):
        # Review Focus 4: a Flux image bump in the middle of a run.
        self.c.edit_dep("frigate", lambda d: d["metadata"].update(generation=5))
        C.status(self.item)
        self.assertEqual([], self.writes())
        self.assertEqual("skipped", self.item["ref"]["results"][0]["result"])

    def test_cancel_after_a_restart_lets_it_finish(self):
        C.status(self.item)
        self.assertIn("Stopped after moving 0", C.cancel_run(self.item, {}))
        self.assertEqual(1, len(self.writes()))
        self.assertIn("finishes its restart", " ".join(C.cancel_plan(self.item)["keeps"]))


class AutomaticJob(unittest.TestCase):
    """A run Homestead started by itself asks before each move whether it may go on."""
    def setUp(self):
        keep_bound(self, C, CAPACITY_REVIEW)
        self.c = Hosts(self)
        bind(self.c)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        rebind(self, RECOVERY, folder.name)
        self.ref = {"moves": [{"ns": "lab", "name": "frigate", "id": "lab/frigate", "from": "k2", "to": "k1"},
                              {"ns": "lab", "name": "ha", "id": "lab/ha", "from": "k2", "to": "k3"}],
                    "index": 0, "moved": 0, "stage": "move", "results": [], "automatic": True}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(C, "_pull_first", return_value=([], [])).start()

    def test_it_stops_as_succeeded_with_why_and_moves_nothing_more(self):
        self.ref.update(moved=1)
        with mock.patch.object(RECOVERY, "may_go_on", return_value="b no longer takes work"):
            state, _, message = C.status({"id": "job", "ref": self.ref})
        self.assertEqual("succeeded", state, "the condition is gone once the host settles: no pause")
        self.assertEqual("Stopped: b no longer takes work. Moved 1", message)
        self.assertEqual([], self.c.moved)

    def test_it_goes_on_when_nothing_says_not_to(self):
        with mock.patch.object(RECOVERY, "may_go_on", return_value=""):
            C.status({"id": "job", "ref": self.ref})
        self.assertEqual([("frigate", "k1")], self.c.moved)

    def test_a_restart_finishing_is_not_stopped(self):
        self.ref.update(stage="starting", started=time.time())
        self.c.edit_pod("frigate", lambda p: p["spec"].update(nodeName="k1"))
        with mock.patch.object(RECOVERY, "may_go_on", return_value="the choice changed") as asked:
            state = C.status({"id": "job", "ref": self.ref})[0]
        self.assertEqual("running", state)
        asked.assert_not_called()
        self.assertEqual(1, self.ref["moved"])

    def test_a_move_starts_its_cooldown_when_sent_so_a_cancel_counts(self):
        with mock.patch.object(RECOVERY, "may_go_on", return_value=""):
            C.status({"id": "job", "ref": self.ref})
        self.assertIn("lab/frigate", RECOVERY._load()["auto"]["moved"])

    def test_a_skipped_move_starts_its_cooldown_so_empty_runs_do_not_loop(self):
        def refused(ns, name, to):
            raise ValueError("k1 cannot take it now")
        self.c.apply = refused
        C.bind(K.kget, lambda dep: {}, lambda n, r: (True, []), refused, ("lab", "homestead"), lambda: True)
        with mock.patch.object(RECOVERY, "may_go_on", return_value=""):
            C.status({"id": "job", "ref": self.ref})
        self.assertEqual("skipped", self.ref["results"][0]["result"])
        self.assertIn("lab/frigate", RECOVERY._load()["auto"]["moved"])

    def test_a_restart_starts_its_cooldown_when_sent(self):
        self.ref["moves"][0].update(how="restart", by="")
        C.restart = lambda ns, name, also=None: {"restartedAt": "2026-11-07T00:00:00Z"}
        rebind(self, RECOVERY, RECOVERY.DATA_DIR, K.kget, lambda: {"placement": {"mode": "spread", "rebalance": "auto"}}, OPS, None, None, (), _managed_by=lambda dep: {})
        C.status({"id": "job", "ref": self.ref})
        self.assertEqual("starting", self.ref["stage"])
        self.assertIn("lab/frigate", RECOVERY._load()["auto"]["moved"])

    def test_a_cooldown_that_cannot_be_written_does_not_lose_the_send(self):
        with mock.patch.object(RECOVERY, "may_go_on", return_value=""), \
                mock.patch.object(RECOVERY, "moved_back", side_effect=OSError("disk")):
            C.status({"id": "job", "ref": self.ref})
        self.assertEqual("starting", self.ref["stage"])

    def test_a_restart_in_a_window_run_asks_too(self):
        # No automatic mark: the window's own run, whose placement moves go on as they did.
        self.ref.pop("automatic")
        self.ref["moves"][0].update(how="restart", by="Flux")
        with mock.patch.object(RECOVERY, "may_go_on", return_value="b no longer takes work"):
            state = C.status({"id": "job", "ref": self.ref})[0]
        self.assertEqual("succeeded", state)

    def test_manual_balance_hosts_ignores_all_of_this(self):
        self.ref.pop("automatic")
        with mock.patch.object(RECOVERY, "may_go_on", return_value="the choice changed") as asked:
            C.status({"id": "job", "ref": self.ref})
        asked.assert_not_called()
        self.assertEqual([("frigate", "k1")], self.c.moved)
        self.ref.update(stage="starting", started=time.time())
        self.c.edit_pod("frigate", lambda p: p["spec"].update(nodeName="k1"))
        C.status({"id": "job", "ref": self.ref})
        self.assertEqual({}, RECOVERY._load()["auto"].get("moved", {}))

    def test_start_keeps_the_automatic_mark(self):
        started = []
        with mock.patch.object(C, "operations", mock.Mock(start=lambda *a, **k: started.append(a))):
            C.start({"moves": [], "excluded": [], "automatic": True})
            C.start({"moves": [], "excluded": []})
        self.assertTrue(started[0][4]["automatic"])
        self.assertFalse(started[1][4].get("automatic"))


class RealTickAndPlan(unittest.TestCase):
    """The real _auto_tick on the real plan: k1 away and back, against the shared fake.
    AutoCase fakes both plan and start, which hides how they join."""
    def setUp(self):
        keep_bound(self, C, CAPACITY_REVIEW)
        self.c = Hosts(self)
        bind(self.c)
        C.restart = server.OVERVIEW_ROUTES.restart_workload
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = {"placement": {"mode": "spread", "rebalance": "auto"},
                         "updates": {"maintenance": {"days": [], "start": "02:00", "duration_minutes": 120}}}
        self.tools = {}
        self.chosen = []

        def start(chosen, why="", unless=()):
            self.chosen.append(chosen)
            return {"id": "job"}
        rebind(self, RECOVERY, folder.name, K.kget, lambda: self.settings, OPS,
               lambda exclude=(), avoid=(), **k: C.plan(exclude, avoid=avoid, **k), start, (),
               _managed_by=lambda dep: self.tools.get(dep["metadata"]["name"], {}))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(PRIORITY, "ladder", lambda *a, **k: LADDER).start()
        mock.patch.object(RECOVERY, "_planned", [0.0]).start()
        mock.patch.object(C, "_pull_first", return_value=([], [])).start()
        self.c.kube.add(reserving("busy", "k2", cpu="1", memory="4Gi"))
        self.now = time.time()

    def ready(self, name, ready):
        self.c.edit(f"/api/v1/nodes/{name}", lambda n: n["status"].update(conditions=[{"type": "Ready", "status": "True" if ready else "False"}]))

    def back(self):
        RECOVERY.tick(self.now - 3600)
        self.ready("k1", False)
        RECOVERY.tick(self.now - 2400)
        self.ready("k1", True)
        RECOVERY.tick(self.now - 1200)
        RECOVERY._planned[0] = 0.0
        RECOVERY.tick(self.now)

    def everything_flux_preferring_k2(self):
        for name in APPS_HERE:
            self.tools[name] = {"by": "Flux", "detected": True}
            self.c.edit_dep(name, lambda d: prefers(d, preference("k2")))

    def test_apps_left_out_are_listed_and_the_host_settles(self):
        self.everything_flux_preferring_k2()
        self.back()
        self.assertEqual([], self.chosen, "no job")
        self.assertIn("k1", RECOVERY._load()["returned"])
        whys = {k["id"]: k["why"] for k in RECOVERY.status()["kept"]}
        self.assertIn("preference keeps it on k2", whys["lab/frigate"])
        self.assertEqual([], [f for f in RECOVERY.alert_facts("auto", self.settings["updates"]["maintenance"]) if f["key"] == "recovery:rebalance"],
                         "a reboot doesn't mean a week-long alert")

    def test_a_settled_host_never_starts_a_run_when_the_load_changes(self):
        self.c.edit_dep("tiny", lambda d: prefers(d, preference("k2")))
        self.c.edit_node_metrics("k2", lambda m: m["usage"].update(cpu="0m", memory="0Gi"))
        self.c.edit_node_metrics("k1", lambda m: m["usage"].update(cpu="0m", memory="0Gi"))
        self.c.edit_node_metrics("k3", lambda m: m["usage"].update(cpu="0m", memory="0Gi"))
        for name in APPS_HERE:
            self.c.edit_pod_metrics(name, lambda m: m["containers"][0]["usage"].update(cpu="1m", memory="1Mi"))
        self.back()
        self.assertEqual([], self.chosen, "even hosts: nothing worth moving")
        for name, (cpu, memory) in {"k2": ("3200m", "11Gi")}.items():
            self.c.edit_node_metrics(name, lambda m: m["usage"].update(cpu=cpu, memory=memory))
        self.c.edit_pod_metrics("frigate", lambda m: m["containers"][0]["usage"].update(cpu="1500m", memory="3Gi"))
        self.assertIn("lab/frigate", moves_of(C.plan(restart={}, back=("k1",))), "it would, if this still ran")
        RECOVERY._planned[0] = 0.0
        RECOVERY.tick(self.now + 5 * 86400)
        self.assertEqual([], self.chosen)

    def test_a_persons_preference_is_not_rewritten_by_an_automatic_run(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        before = self.c.dep("frigate")["spec"]["template"]["spec"]["affinity"]
        self.back()
        for chosen in self.chosen:
            self.assertNotIn("lab/frigate", [m["id"] for m in chosen["moves"]])
        self.assertEqual(before, self.c.dep("frigate")["spec"]["template"]["spec"]["affinity"])
        self.assertIn("lab/frigate", {k["id"] for k in RECOVERY.status()["kept"]})

    def test_an_automatic_run_of_a_preference_for_the_returned_host_keeps_the_preference(self):
        self.c.kube.add(reserving("full", "k3"))
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k1")))
        self.back()
        move = next(m for m in self.chosen[0]["moves"] if m["id"] == "lab/frigate")
        self.assertEqual(("restart", "k1"), (move["how"], move["to"]))
        item = {"id": "job", "ref": {"moves": [move], "index": 0, "moved": 0, "stage": "move", "results": [], "automatic": True}}
        C.status(item)
        self.assertEqual("starting", item["ref"]["stage"])
        self.assertEqual("k1", PLACE.preferred_host(self.c.dep("frigate")["spec"]["template"]["spec"]))
        self.assertEqual([], self.c.moved)
        self.assertEqual("", PLACE.balance_preference(self.c.dep("frigate")))

    def test_every_move_chosen_carries_its_generation(self):
        self.back()
        self.assertTrue(self.chosen[0]["moves"])
        self.assertTrue(all("generation" in m for m in self.chosen[0]["moves"]))

    def test_a_person_can_still_move_it_from_balance_hosts(self):
        self.c.edit_dep("frigate", lambda d: prefers(d, preference("k2")))
        self.assertIn("lab/frigate", moves_of(C.plan()))


if __name__ == "__main__":
    unittest.main()
