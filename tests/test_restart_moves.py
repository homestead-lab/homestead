"""Moving an app back by restarting it: an app a tool applies can't take a new
placement, but the scheduler can place it again. The plan says where a restart
would land it, or why it can't; the job restarts it once and looks where it went.
On the shared fake API."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_container_rebalance as C
import homestead_kube as K
import homestead_names as NAMES
import homestead_place as PLACE
import server
from rebind import keep_bound
from test_container_rebalance import Hosts, bind, deployment, pod

FLUX = {"lab/frigate": "Flux"}
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
        keep_bound(self, C)
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

    def test_a_preference_naming_several_hosts_leaves_it_with_why(self):
        term = preference("k1")
        term["preference"]["matchExpressions"][0]["values"] = ["k1", "k2"]
        self.c.edit_dep("frigate", lambda d: prefers(d, term))
        self.assertIn("several hosts", why_of(self.plan())["lab/frigate"])

    def test_reservations_favouring_its_host_leave_it_with_why(self):
        for host in ("k1", "k3"):
            self.c.kube.add(reserving(f"full-{host}", host))
        plan = self.plan()
        self.assertNotIn("lab/frigate", moves_of(plan))
        self.assertIn("would put it back on k2", why_of(plan)["lab/frigate"])

    def test_its_own_requests_are_not_counted_against_its_host(self):
        # Each host has 1 CPU reserved by something else, so a tie: it stays. Counting
        # its own 3 CPUs against k2 as well would send it away.
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

    def test_manual_plan_output_is_unchanged_without_restart(self):
        plain = C.plan()
        self.assertTrue(plain["moves"])
        self.assertFalse([m for m in plain["moves"] if "how" in m or "by" in m])
        self.assertEqual(plain["moves"], C.plan(restart=None, back=())["moves"])
        self.assertEqual(plain["moves"], C.plan(restart={})["moves"])


class RestartJob(unittest.TestCase):
    def setUp(self):
        keep_bound(self, C)
        self.c = Hosts(self)
        bind(self.c)
        C.restart = server.OVERVIEW_ROUTES.restart_workload
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


if __name__ == "__main__":
    unittest.main()
