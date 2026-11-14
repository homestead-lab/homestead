"""Maintenance a person declares (#535 part 2): reviewed and drained like a
reboot, then an event instead of power, with Extend and End.

Through the routes and the job's own status, on the fake API server: the real
review, drain, hold, job store and history file."""
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: F401  (binds the job store's resolvers and the modules)
import homestead_history as HISTORY
import homestead_host_maintenance as HM
import homestead_lifecycle as LC
import homestead_node_actions as NODE_ACTIONS
import homestead_operations as OPS
import homestead_os_rollout as ROLLOUT
import homestead_power as POWER
import homestead_power_hold as HOLD
import homestead_power_jobs as JOBS
import homestead_routes as ROUTER
from fakes.kube import Kube

NODES = "/api/v1/nodes"
QUORUM = {"members": ["node-a", "node-b", "node-c"], "can_lose": 1, "ready": ["node-a", "node-b", "node-c"], "total": 3,
          "quorum_needs": 2}


def host(name, uid=None, boot="boot-1", ready=True, cordoned=False):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "uid": uid or f"uid-{name}"},
            "spec": {"unschedulable": cordoned},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "nodeInfo": {"bootID": boot}}}


def app_pod(name, node, owner="web-7d9f"):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"namespace": "apps", "name": name,
                         "ownerReferences": [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": owner, "controller": True, "uid": owner}]},
            "spec": {"nodeName": node, "containers": [{"name": "c", "image": "x"}]}, "status": {"phase": "Running"}}


def replica_set(name, deployment):
    return {"apiVersion": "apps/v1", "kind": "ReplicaSet",
            "metadata": {"namespace": "apps", "name": name,
                         "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": deployment, "controller": True, "uid": deployment}]},
            "spec": {}}


def deployment(name, replicas=1):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "apps", "name": name},
            "spec": {"replicas": replicas}}


def req(body=None, **query):
    return types.SimpleNamespace(body=body or {}, query={k: [v] for k, v in query.items()}, path="/api/node/power")


class Case(unittest.TestCase):
    objects = ()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = tmp.name
        self.kube = self.enterContext(Kube(host("node-a"), host("node-b"), host("node-c"), *self.objects,
                                           groups=("longhorn.io", "kubevirt.io")))
        impact = {"workloads": [{"ns": "apps", "name": "web", "eligible": ["node-b"]}], "stranded": []}
        self.leader = mock.Mock(return_value=True)
        for target, name, value in (
                (OPS, "DATA_DIR", self.data), (HISTORY, "DATA_DIR", self.data), (ROLLOUT, "DATA_DIR", self.data),
                (POWER, "impact", lambda node: impact), (POWER, "quorum", lambda: QUORUM),
                (POWER, "power_enabled", lambda: False), (LC, "NODE_POWER_ENABLED", False),
                (NODE_ACTIONS, "quorum_report", lambda: QUORUM),
                (JOBS, "POD_NAME", "homestead-a"), (JOBS.LEADER, "is_leader", self.leader),
                (POWER, "SETTLE", 0), (POWER, "WORKER_GONE", lambda pod, restarts=None: False),
                (JOBS, "_power_in_background", self.run_now)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def stops_for_real(self):
        """No controller runs here: what a held app's replicas 0 would remove, gone by hand."""
        real = HOLD.stop

        def stop(item, job):
            out = real(item, job)
            self.kube.discard("/api/v1/namespaces/apps/pods/web-1")
            return out
        patcher = mock.patch.object(HOLD, "stop", stop)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_now(self, operation_id, power_plan, force, resumed=False):
        try:
            JOBS.run_power_job(operation_id, power_plan, force, resumed)
        except (JOBS.PowerNotSent, OPS.Superseded):
            pass

    def plan(self, node="node-a", action="maintenance"):
        return JOBS._plan(req(node=node, action=action))

    def send(self, node="node-a", reason="Drive swap", hours=1, **extra):
        review = self.plan(node)
        body = {"node": node, "confirm": node, "action": "maintenance", "reason": reason, "hours": hours,
                "review_token": review["review_token"], "choices": {}, "allow_drain_blockers": [], **extra}
        return JOBS._send_power(req(body))

    def advance(self):
        """What the leader's jobs loop does every few seconds."""
        OPS.list_operations(wait=False)

    def job(self, reply):
        return next(i for i in OPS.stored() if i["id"] == reply.body["operation"]["id"])

    def edit(self, change, node="node-a"):
        found = self.kube.object(f"{NODES}/{node}")
        change(found)
        self.kube.add(found)

    def cordoned(self, node="node-a"):
        return bool(self.kube.object(f"{NODES}/{node}")["spec"].get("unschedulable"))

    def helper_pods(self):
        return [w for w in self.kube.writes if w[0] == "POST" and w[1].endswith("/pods")]


class ReviewTests(Case):
    def test_plan_reviews_like_a_reboot(self):
        with mock.patch.object(POWER, "power_enabled", lambda: True):
            maintain, reboot = self.plan(), self.plan(action="reboot")
        self.assertTrue(maintain["ready"], maintain["blockers"])
        for key in ("workloads", "stranded", "volumes", "vms", "pods", "hold", "drain_pods", "maintenance", "quorum"):
            self.assertEqual(reboot[key], maintain[key], key)
        self.assertNotEqual(reboot["review_token"], maintain["review_token"])
        self.assertEqual(maintain["review_token"], self.plan()["review_token"])

    def test_plan_needs_no_power_control(self):
        self.assertTrue(self.plan()["ready"])
        self.assertIn("ENABLE_NODE_POWER", " ".join(self.plan(action="reboot")["blockers"]))

    def test_plan_refuses_a_host_already_in_maintenance(self):
        HISTORY.maintenance_declare("node-a", "Drive swap", "op-0", time.time() + 600, cordoned_before=False, node_uid="u")
        plan = self.plan()
        self.assertFalse(plan["ready"])
        self.assertIn("This host is already in maintenance", plan["hard_blockers"])
        with mock.patch.object(POWER, "power_enabled", lambda: True):
            self.assertTrue(self.plan(action="reboot")["ready"], "Reboot stays available in maintenance")

    def test_a_host_that_ran_out_is_still_in_maintenance_for_the_review(self):
        HISTORY.maintenance_declare("node-a", "Drive swap", "op-0", time.time() - 1, cordoned_before=False, node_uid="u",
                                    now=time.time() - 3600)
        self.assertIn("This host is already in maintenance", self.plan()["hard_blockers"])

    def rollout(self, name, status="running"):
        ROLLOUT._save({"rollout": {"status": status, "phase": "installing", "node": {"name": name}, "nodes": [name]}})

    def test_a_rollout_working_on_this_host_blocks_the_review(self):
        self.rollout("node-a")
        self.assertIn("An OS update of every host is working on this host. Wait for it to move on, or stop it.",
                      self.plan()["hard_blockers"])
        with mock.patch.object(POWER, "power_enabled", lambda: True):
            self.assertTrue(self.plan(action="reboot")["ready"], "a reboot's review doesn't ask")

    def test_a_rollout_on_another_host_or_finished_does_not(self):
        self.rollout("node-b")
        self.assertTrue(self.plan()["ready"])
        self.rollout("node-a", status="succeeded")
        self.assertTrue(self.plan()["ready"])

    def test_a_rollout_that_cannot_be_read_blocks_the_review(self):
        with open(ROLLOUT._path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIn("not be read", " ".join(self.plan()["hard_blockers"]))

    def test_a_host_not_ready_is_refused_in_its_own_words(self):
        self.edit(lambda n: n["status"]["conditions"][0].update(status="False"))
        self.assertIn("The host is not ready. Maintenance must be declared before it goes down.", self.plan()["hard_blockers"])

    def test_there_is_no_force(self):
        with self.assertRaisesRegex(ValueError, "force"):
            JOBS._plan(req(node="node-a", action="maintenance", force="1"))
        with self.assertRaisesRegex(ValueError, "force"):
            self.send(force=True)

    def test_bad_reason_or_hours_are_refused(self):
        for reason in ("", "   ", "x" * 121, "new\ndisk", "tab\there", "harvester MAINTENANCE", "Host Reboot", 7, None):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.send(reason=reason)
        for hours in (3, 0, 25, "1", True, None, 1.5):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                self.send(hours=hours)
        self.assertEqual([], [i for i in OPS.stored() if i["kind"] == "node-power"])

    def test_a_reason_keeps_its_words_and_loses_its_edges(self):
        self.send(reason="  <b>Drive</b> swap ")
        self.assertEqual("<b>Drive</b> swap", HISTORY.declared_for("node-a")["reason"])

    def test_a_stale_review_is_refused_with_a_fresh_one(self):
        reply = self.send(review_token="old")
        self.assertEqual(409, reply.code)
        self.assertEqual("maintenance", reply.body["plan"]["action"])


class JobTests(Case):
    objects = (app_pod("web-1", "node-a"), replica_set("web-7d9f", "web"), deployment("web"))

    def test_the_job_cordons_drains_and_then_opens(self):
        seen = {}
        real = NODE_ACTIONS.drain

        def drain(*args, **kwargs):
            seen["cordoned"], seen["open"] = self.cordoned(), HISTORY.declared_for("node-a")
            return real(*args, **kwargs)
        with mock.patch.object(NODE_ACTIONS, "drain", drain):
            before = int(time.time())
            reply = self.send(hours=2)
        self.assertEqual(202, reply.code)
        self.assertEqual((True, None), (seen["cordoned"], seen["open"]), "cordoned first, and no event before the drain ends")
        event = HISTORY.declared_for("node-a")
        self.assertEqual(("Drive swap", False, "uid-node-a", ["apps/web"]),
                         (event["reason"], event["cordoned_before"], event["node_uid"], event["apps"]))
        self.assertGreaterEqual(event["until"], before + 2 * 3600)
        self.assertEqual(int(event["start"]) + 2 * 3600, event["until"], "counted from when it opened")
        self.assertIsNone(self.kube.object("/api/v1/namespaces/apps/pods/web-1"), "drained")
        self.assertEqual([], self.helper_pods(), "no helper pod, ever")
        item = self.job(reply)
        self.assertEqual("maintained", item["ref"]["phase"])
        self.assertEqual(("succeeded", 100), POWER.status(item)[:2])
        self.assertEqual("In maintenance: Drive swap", POWER.status(item)[2])

    def test_the_job_ends_and_the_event_goes_on(self):
        reply = self.send()
        self.advance()
        self.assertEqual("succeeded", self.job(reply)["status"])
        self.assertIsNone(JOBS.active_power_job("node-a"), "it blocks nothing during the maintenance")
        self.assertIsNone(HISTORY.declared_for("node-a")["end"], "and the job's status left the event alone")

    def test_a_failed_drain_opens_nothing_and_says_so(self):
        def refused(*args, **kwargs):
            return {"evicted": [], "skipped": ["apps/web-1 (HTTP 429)"]}
        with mock.patch.object(NODE_ACTIONS, "drain", refused):
            reply = self.send()
        item = self.job(reply)
        self.assertEqual("failed", item["status"])
        self.assertIsNone(HISTORY.declared_for("node-a"))
        self.assertNotIn("power", item["message"].lower())
        self.assertIn("aintenance was not started", item["message"])
        self.assertIn("HTTP 429", item["message"])
        self.assertFalse(self.cordoned(), "the job's own cordon comes off, as a refused reboot's does (#1289)")

    def test_a_recheck_that_fails_says_maintenance_not_power(self):
        real = POWER.plan

        def changed(node, action, **kwargs):
            out = real(node, action, **kwargs)
            return {**out, "blockers": ["Something appeared"], "ready": False} if kwargs.get("after_drain") else out
        with mock.patch.object(POWER, "plan", changed):
            item = self.job(self.send())
        self.assertEqual("failed", item["status"])
        self.assertNotIn("power", item["message"].lower())
        self.assertIn("Something appeared", item["message"])

    def test_resumed_job_opens_one_event(self):
        real = NODE_ACTIONS.drain
        calls = []

        def evicted_once(*args, **kwargs):
            calls.append(kwargs.get("resumed"))
            if len(calls) == 1:
                raise OPS.Superseded("this replica was evicted")
            return real(*args, **kwargs)
        with mock.patch.object(NODE_ACTIONS, "drain", evicted_once):
            reply = self.send()
            item = self.job(reply)
            self.assertIsNone(HISTORY.declared_for("node-a"), "the first copy never got there")
            self.assertEqual("draining", item["ref"]["phase"])
            OPS.record_phase(item["id"], "draining", 10, "carrying on", worker="homestead-b", expect_worker="homestead-a")
            with mock.patch.object(JOBS, "POD_NAME", "homestead-b"):
                JOBS.run_power_job(item["id"], item["ref"]["plan"], False, resumed=True)
        self.assertEqual([None, True], [c if c else None for c in calls])
        self.assertEqual(1, len(HISTORY.maintenance_events("node-a")))
        self.assertEqual("maintained", self.job(reply)["ref"]["phase"])

    def test_resumed_after_it_opened_does_nothing_twice(self):
        real = HISTORY.maintenance_declare

        def opened_then_evicted(*args, **kwargs):
            real(*args, **kwargs)
            raise OPS.Superseded("this replica was evicted")
        with mock.patch.object(HISTORY, "maintenance_declare", opened_then_evicted):
            reply = self.send()
        item = self.job(reply)
        self.assertNotEqual("maintained", item["ref"]["phase"])
        self.assertTrue(HISTORY.declared_for("node-a"))
        writes = len(self.kube.writes)
        JOBS.run_power_job(item["id"], item["ref"]["plan"], False, resumed=True)
        self.assertEqual(writes, len(self.kube.writes), "no second cordon or drain")
        self.assertEqual(1, len(HISTORY.maintenance_events("node-a")))
        self.assertEqual("maintained", self.job(reply)["ref"]["phase"])

    def test_held_apps_are_on_the_event(self):
        review = self.plan()
        pick = {i["id"]: "wait" for i in review["hold"]}
        self.assertEqual(["Deployment/apps/web"], list(pick))
        self.stops_for_real()
        self.send(choices=pick)
        event = HISTORY.declared_for("node-a")
        self.assertEqual([("Deployment", "apps", "web")], [(h["kind"], h["ns"], h["name"]) for h in event["held"]])
        self.assertEqual(0, self.kube.object("/apis/apps/v1/namespaces/apps/deployments/web")["spec"]["replicas"])

    def test_a_failure_starts_again_what_it_stopped(self):
        pick = {i["id"]: "wait" for i in self.plan()["hold"]}
        self.stops_for_real()
        with mock.patch.object(POWER, "recheck_after_drain", side_effect=ValueError("The host stays cordoned; power was not sent")):
            item = self.job(self.send(choices=pick))
        self.assertEqual("failed", item["status"])
        self.assertEqual(1, self.kube.object("/apis/apps/v1/namespaces/apps/deployments/web")["spec"]["replicas"])
        self.assertIn("Started again: apps/web", item["message"])

    def test_a_failure_after_it_opened_says_it_is_in_maintenance(self):
        pick = {i["id"]: "wait" for i in self.plan()["hold"]}
        self.stops_for_real()
        real = OPS.record_phase

        def lost(operation_id, phase, *args, **kwargs):
            if phase == "maintained":
                raise OSError("disk full")
            return real(operation_id, phase, *args, **kwargs)
        with mock.patch.object(OPS, "record_phase", lost):
            item = self.job(self.send(choices=pick))
        self.assertEqual("failed", item["status"])
        self.assertNotIn("not opened", item["message"])
        self.assertIn("In maintenance", item["message"])
        self.assertEqual(0, self.kube.object("/apis/apps/v1/namespaces/apps/deployments/web")["spec"]["replicas"],
                         "what waits goes on waiting: the event is open")
        self.assertIsNone(HISTORY.declared_for("node-a")["end"])

    def test_a_stalled_job_whose_event_opened_is_done_and_starts_nothing(self):
        item = self.job(self.send())
        item["ref"].update(phase="draining", phase_at=time.time() - 4000, worker="homestead-a", held=[{"kind": "Deployment", "ns": "apps", "name": "web"}])
        restore = mock.Mock()
        with mock.patch.object(POWER, "RESTORE", restore):
            state, _, message = POWER.status(item)
        self.assertEqual(("succeeded", "In maintenance: Drive swap"), (state, message))
        restore.assert_not_called()

    def stalled(self, item):
        item["ref"].update(phase="draining", phase_at=time.time() - 4000, worker="homestead-a",
                           held=[{"kind": "Deployment", "ns": "apps", "name": "web"}])
        return item

    def test_a_stalled_job_whose_event_cannot_be_read_stays_running_and_starts_nothing(self):
        item = self.stalled(self.job(self.send()))
        with open(HISTORY._path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        restore = mock.Mock()
        with mock.patch.object(POWER, "RESTORE", restore):
            state = POWER.status(item)[0]
        self.assertEqual("running", state, "a bad read isn't proof that it stopped")
        restore.assert_not_called()

    def test_a_stalled_job_is_not_saved_by_another_jobs_event(self):
        item = self.stalled(self.job(self.send()))
        HISTORY.maintenance_end(item["id"])
        HISTORY.maintenance_release(item["id"])
        HISTORY.maintenance_declare("node-a", "Another", "op-other", time.time() + 600, cordoned_before=False, node_uid="u")
        restore = mock.Mock(return_value=True)
        with mock.patch.object(POWER, "RESTORE", restore):
            state = POWER.status(item)[0]
        self.assertEqual("failed", state)
        restore.assert_called_once()

    def test_history_failure_fails_the_job(self):
        with mock.patch.object(HISTORY, "maintenance_declare", side_effect=OSError("disk full")):
            item = self.job(self.send())
        self.assertEqual("failed", item["status"])
        self.assertIn("cordoned", item["message"])
        self.assertIn("not be recorded", item["message"])
        self.assertTrue(self.cordoned())

    def test_a_stalled_job_says_maintenance(self):
        item = self.job(self.send())
        item["ref"].update(phase="draining", phase_at=time.time() - 4000, worker="homestead-a")
        with mock.patch.object(HISTORY, "declared_for", return_value=None):
            state, _, message = POWER.status(item)
        self.assertEqual("failed", state)
        self.assertNotIn("power", message.lower())

    def test_a_reboot_inside_maintenance_leaves_the_cordon(self):
        self.send()
        self.advance()
        with mock.patch.object(LC, "NODE_POWER_ENABLED", True), mock.patch.object(POWER, "power_enabled", lambda: True):
            review = self.plan(action="reboot")
            self.assertTrue(review["ready"], "not blocked by the maintenance job, which has finished")
            self.assertTrue(review["cordoned"])
            reply = JOBS._send_power(req({"node": "node-a", "confirm": "node-a", "action": "reboot", "choices": {},
                                          "review_token": review["review_token"], "allow_drain_blockers": []}))
            item = self.job(reply)
            self.assertEqual(1, len(self.helper_pods()), "the reboot's own helper")
            self.assertTrue(item["ref"]["cordoned_before"])
            self.edit(lambda n: n["status"]["nodeInfo"].update(bootID="boot-2"))
            item["ref"]["volumes"] = []
            state, _, message = POWER.status(item)
        self.assertEqual("succeeded", state)
        self.assertIn("stays cordoned", message)
        self.assertTrue(self.cordoned())
        declared = HISTORY.declared_for("node-a")
        self.assertIsNone(declared["end"], "the reboot closes its own event, not this one")
        self.assertFalse(declared["released"])

    def test_the_job_log_says_no_helper_is_involved(self):
        import homestead_joblogs as LOGS
        item = self.job(self.send())
        self.assertNotIn("power", LOGS.node_power(item)[0]["note"].lower())


class SingleHostTests(Case):
    def setUp(self):
        super().setUp()
        self.kube.discard(f"{NODES}/node-b")
        self.kube.discard(f"{NODES}/node-c")
        solo = {"members": ["node-a"], "can_lose": 0}
        for target, name in ((POWER, "quorum"), (NODE_ACTIONS, "quorum_report")):
            patcher = mock.patch.object(target, name, lambda: solo)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_single_host_opens_at_once_with_nothing_stopped(self):
        review = self.plan()
        self.assertTrue(review["planned_outage"])
        self.assertEqual([], review["hold"])
        self.assertIn("nothing is stopped", " ".join(review["warnings"]))
        self.assertIn("whole cluster", " ".join(review["warnings"]))
        self.assertEqual(409, self.send().code, "needs the acknowledgement")
        self.send(allow_cluster_outage=True)
        event = HISTORY.declared_for("node-a")
        self.assertIsNone(event["end"])
        self.assertFalse(self.cordoned(), "no cordon")
        self.assertEqual([], [w for w in self.kube.writes if "/eviction" in w[1]])
        self.assertEqual([], event["held"])

    def test_end_leaves_the_host_as_it_found_it(self):
        self.send(allow_cluster_outage=True)
        writes = len(self.kube.writes)
        self.assertTrue(HM._end(req({"node": "node-a"}))["ok"])
        self.assertEqual(writes, len(self.kube.writes), "nothing to uncordon or start")

    def test_end_says_what_is_true_of_a_host_never_cordoned(self):
        self.send(allow_cluster_outage=True)
        detail = HM._end(req({"node": "node-a"}))["detail"]
        self.assertNotIn("cordoned", detail)
        self.assertIn("already allowed", detail)


class ExtendAndEndTests(Case):
    objects = (app_pod("web-1", "node-a"), replica_set("web-7d9f", "web"), deployment("web"))

    def declared(self, **extra):
        pick = {i["id"]: "wait" for i in self.plan()["hold"]}
        self.stops_for_real()
        self.send(choices=pick, **extra)
        return HISTORY.declared_for("node-a")

    def web_replicas(self):
        return self.kube.object("/apis/apps/v1/namespaces/apps/deployments/web")["spec"]["replicas"]

    def test_end_uncordons_closes_and_starts_what_waited(self):
        self.declared()
        with mock.patch.object(ROUTER, "forget") as forget:
            out = HM._end(req({"node": "node-a"}))
        self.assertEqual((True, ["apps/web"], []), (out["ok"], out["started"], out["left"]))
        self.assertFalse(self.cordoned())
        self.assertEqual(1, self.web_replicas())
        self.assertIsNone(HISTORY.declared_for("node-a"))
        self.assertEqual("ended", HISTORY.maintenance_events("node-a")[-1]["outcome"])
        forget.assert_called_with("uptime")

    def test_it_uncordons_before_it_starts_what_waited(self):
        self.declared()
        order = []
        real_cordon, real_restore = NODE_ACTIONS.set_cordon, HOLD.restore
        with mock.patch.object(NODE_ACTIONS, "set_cordon", lambda *a: order.append("uncordon") or real_cordon(*a)), \
             mock.patch.object(HOLD, "restore", lambda *a: order.append("restore") or real_restore(*a)):
            HM._end(req({"node": "node-a"}))
        self.assertEqual(["uncordon", "restore"], order)

    def test_end_keeps_a_cordon_that_was_there_before(self):
        self.edit(lambda n: n["spec"].update(unschedulable=True))
        self.declared()
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(self.cordoned())
        self.assertIn("cordoned", out["detail"])

    def test_end_leaves_a_recreated_host_alone(self):
        self.declared()
        self.edit(lambda n: n["metadata"].update(uid="a-stranger"))
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(self.cordoned(), "not this event's host")
        self.assertIn("replaced", out["detail"])
        self.assertIsNone(HISTORY.declared_for("node-a"), "the event closes")
        self.assertEqual(1, self.web_replicas(), "what waited still starts")

    def test_end_of_a_host_that_has_gone_closes_it(self):
        self.declared()
        self.kube.discard(f"{NODES}/node-a")
        out = HM._end(req({"node": "node-a"}))
        self.assertIn("replaced", out["detail"])
        self.assertIsNone(HISTORY.declared_for("node-a"))

    def test_end_that_cannot_read_the_host_says_so_and_leaves_the_cordon(self):
        self.declared()
        self.kube.fail(f"{NODES}/node-a", 500, times=1, method="GET")
        out = HM._end(req({"node": "node-a"}))
        self.assertFalse(out["ok"])
        self.assertIn("still cordoned", out["detail"])
        self.assertTrue(self.cordoned())
        self.assertEqual(1, self.web_replicas(), "what waited still starts, on another host")
        self.assertIsNotNone(HISTORY.declared_for("node-a"), "and End stays offered")

    def test_a_failed_restore_names_what_is_left(self):
        self.declared()
        with mock.patch.object(HOLD, "restore", side_effect=OSError("api down")):
            out = HM._end(req({"node": "node-a"}))
        self.assertFalse(out["ok"])
        self.assertEqual(["apps/web"], out["left"])
        self.assertIn("apps/web", out["detail"])
        self.assertIn("again", out["detail"], "and that End can be asked again")
        self.assertNotIn("by hand", out["detail"], "its marks would stop a later hold")

    def test_end_after_it_finished_is_refused_and_touches_nothing(self):
        self.declared()
        HM._end(req({"node": "node-a"}))
        self.edit(lambda n: n["spec"].update(unschedulable=True))
        reply = HM._end(req({"node": "node-a"}))
        self.assertEqual(409, reply.code, "by then the host has no maintenance")
        self.assertTrue(self.cordoned())

    def test_two_ends_at_once_act_twice_harmlessly(self):
        event = self.declared()
        HM._end(req({"node": "node-a"}))
        self.assertEqual(1, self.web_replicas())
        # The second looked before the first finished.
        with mock.patch.object(HISTORY, "declared_for", lambda node, now=None: event):
            out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"])
        self.assertFalse(self.cordoned())
        self.assertEqual(1, self.web_replicas())
        self.assertEqual([], out["started"])
        self.assertEqual(["apps/web (already started, or changed since)"], out["left"])
        self.assertNotIn("(changed since)", out["detail"])

    def test_a_death_between_the_end_and_its_acts_can_be_ended_again(self):
        self.declared()

        class Died(BaseException):
            pass
        with mock.patch.object(HM, "_allow_scheduling", side_effect=Died()), self.assertRaises(Died):
            HM._end(req({"node": "node-a"}))
        self.assertTrue(self.cordoned())
        self.assertEqual(0, self.web_replicas())
        self.assertEqual("ending", HM._get(req(node="node-a"))["declared"]["state"], "still said")
        self.assertIn("This host is already in maintenance", self.plan()["hard_blockers"])
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"])
        self.assertFalse(self.cordoned())
        self.assertEqual(1, self.web_replicas())
        self.assertIsNone(HISTORY.declared_for("node-a"))

    def test_a_death_after_the_uncordon_can_be_ended_again(self):
        self.declared()

        class Died(BaseException):
            pass
        with mock.patch.object(HOLD, "restore", side_effect=Died()), self.assertRaises(Died):
            HM._end(req({"node": "node-a"}))
        self.assertFalse(self.cordoned())
        self.assertEqual(0, self.web_replicas())
        self.assertTrue(HM._end(req({"node": "node-a"}))["ok"])
        self.assertEqual(1, self.web_replicas())

    def test_an_uncordon_that_fails_can_be_tried_again(self):
        self.declared()
        self.kube.fail(f"{NODES}/node-a", 500, times=1, method="PATCH")
        out = HM._end(req({"node": "node-a"}))
        self.assertFalse(out["ok"])
        self.assertIn("still cordoned", out["detail"])
        self.assertTrue(self.cordoned())
        self.assertEqual("ending", HM._get(req(node="node-a"))["declared"]["state"])
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"])
        self.assertFalse(self.cordoned())
        self.assertIsNone(HISTORY.declared_for("node-a"))

    def test_a_restore_that_fails_can_be_tried_again_and_keeps_the_end_time(self):
        self.declared()
        with mock.patch.object(HOLD, "restore", side_effect=OSError("api down")):
            first = HM._end(req({"node": "node-a"}))
        self.assertFalse(first["ok"])
        ended = HISTORY.declared_for("node-a")["end"]
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"])
        self.assertEqual(["apps/web"], out["started"])
        self.assertEqual(1, self.web_replicas())
        self.assertEqual(ended, HISTORY.maintenance_events("node-a")[-1]["end"])

    def test_a_release_that_cannot_be_written_says_so(self):
        self.declared()
        with mock.patch.object(HISTORY, "maintenance_release", side_effect=OSError("disk full")):
            out = HM._end(req({"node": "node-a"}))
        self.assertFalse(out["ok"])
        self.assertIn("recorded", out["detail"])

    def test_a_host_cordoned_before_that_cannot_be_read_is_left_as_it_was(self):
        self.edit(lambda n: n["spec"].update(unschedulable=True))
        self.declared()
        self.kube.fail(f"{NODES}/node-a", 500, times=1, method="GET")
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"], "there was nothing to take off")
        self.assertIn("Its cordon was left as it was", out["detail"])
        self.assertTrue(self.cordoned())
        self.assertIsNone(HISTORY.declared_for("node-a"))

    def test_ending_a_late_extension_lets_all_of_it_go(self):
        now = int(time.time())
        HISTORY.maintenance_declare("node-a", "Drive swap", "op-1", now - 60, cordoned_before=False, node_uid="uid-node-a",
                                    now=now - 3660)
        HISTORY.maintenance_extend("op-1", now + 3600, now=now)
        self.assertEqual("open", HM._get(req(node="node-a"))["declared"]["state"])
        self.assertTrue(HM._end(req({"node": "node-a"}))["ok"])
        self.assertEqual({"declared": None}, HM._get(req(node="node-a")), "the older row doesn't come back as ran out")
        self.assertEqual({"open": {}, "ran_out": {}, "ending": {}}, HISTORY.declared_view())

    def test_end_on_a_host_someone_uncordoned_says_what_is_true(self):
        self.declared()
        self.edit(lambda n: n["spec"].update(unschedulable=False))
        writes = [w for w in self.kube.writes if w[0] == "PATCH" and w[1].endswith("/nodes/node-a")]
        out = HM._end(req({"node": "node-a"}))
        self.assertEqual(writes, [w for w in self.kube.writes if w[0] == "PATCH" and w[1].endswith("/nodes/node-a")])
        self.assertNotIn("cordoned", out["detail"])
        self.assertIn("already allowed", out["detail"])

    def test_end_after_it_ran_out_releases_the_host(self):
        event = self.declared()
        before = HISTORY.maintenance_events("node-a")
        import homestead_shared as SHARED
        data = HISTORY._read()
        data["maintenance"][0].update(until=time.time() - 60, start=time.time() - 7200)
        SHARED.write_json(HISTORY._path(), data)
        self.assertEqual("ran_out", HM._get(req(node="node-a"))["declared"]["state"])
        out = HM._end(req({"node": "node-a"}))
        self.assertTrue(out["ok"])
        self.assertFalse(self.cordoned())
        after = HISTORY.maintenance_events("node-a")
        self.assertEqual((len(before), "limit"), (len(after), after[0]["outcome"]), "no extra history change")
        self.assertEqual(event["op"], after[0]["op"])

    def test_end_on_a_non_leader_works(self):
        self.declared()
        self.leader.return_value = False
        self.assertTrue(HM._end(req({"node": "node-a"}))["ok"])
        self.assertFalse(self.cordoned())

    def test_end_on_a_host_that_is_down_is_allowed(self):
        self.declared()
        self.edit(lambda n: n["status"]["conditions"][0].update(status="False"))
        self.assertTrue(HM._end(req({"node": "node-a"}))["ok"])

    def test_end_waits_for_a_power_job_still_draining(self):
        self.declared()
        self.advance()
        OPS.start("node-power", "reboot node-a", {"kind": "Node", "name": "node-a"}, "/nodes",
                  {"node": "node-a", "action": "reboot", "phase": "draining"}, "reviewed")
        reply = HM._end(req({"node": "node-a"}))
        self.assertEqual(409, reply.code)
        self.assertTrue(self.cordoned())
        self.assertIsNotNone(HISTORY.declared_for("node-a"))

    def test_extend(self):
        event = self.declared()
        with mock.patch.object(ROUTER, "forget") as forget:
            out = HM._extend(req({"node": "node-a", "hours": 8}))
        self.assertEqual(True, out["ok"])
        self.assertGreater(out["until"], event["until"])
        self.assertEqual(out["until"], HISTORY.declared_for("node-a")["until"])
        forget.assert_called_with("uptime")

    def test_extend_never_shortens(self):
        self.send(hours=8)
        reply = HM._extend(req({"node": "node-a", "hours": 2}))
        self.assertEqual(409, reply.code)
        self.assertIn("later", reply.body["error"])

    def test_extend_refuses_hours_it_does_not_offer(self):
        self.declared()
        for hours in (3, 25, "1", True, None):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                HM._extend(req({"node": "node-a", "hours": hours}))

    def test_extend_routes_refuse_a_host_with_no_maintenance(self):
        self.assertEqual(409, HM._extend(req({"node": "node-a", "hours": 1})).code)
        self.assertEqual(409, HM._end(req({"node": "node-a"})).code)

    def test_read_is_fresh_and_says_how_long_is_left(self):
        self.assertEqual({"declared": None}, HM._get(req(node="node-a")))
        self.declared()
        row = HM._get(req(node="node-a"))["declared"]
        self.assertEqual({"reason", "until", "start", "state", "left_s"}, set(row))
        self.assertEqual("open", row["state"])
        self.assertAlmostEqual(3600, row["left_s"], delta=30)

    def test_roles_are_admin(self):
        self.assertEqual("viewer", HM.ROUTES[("GET", "/api/node/maintenance")][0])
        self.assertEqual("admin", HM.ROUTES[("POST", "/api/node/maintenance/extend")][0])
        self.assertEqual("admin", HM.ROUTES[("POST", "/api/node/maintenance/end")][0])
        self.assertIn("homestead_host_maintenance", ROUTER.MODULES)


class ExcusedAppsTests(Case):
    """Which apps are moving because of a maintenance, for app uptime."""

    def declare(self, op, apps, start, **extra):
        return HISTORY.maintenance_declare("node-a", "Drive swap", op, start + 7200, cordoned_before=False, node_uid="u",
                                           apps=apps, now=start, **extra)

    def test_excused_apps_comes_from_jobs_and_events_and_is_empty_on_a_bad_read(self):
        now = int(time.time())
        OPS.start("node-power", "maintenance node-b", {"kind": "Node", "name": "node-b"}, "/nodes",
                  {"node": "node-b", "action": "maintenance", "phase": "draining",
                   "plan": {"workloads": [{"ns": "lab", "name": "web"}], "vms": ["vms/win"]}}, "reviewed")
        self.declare("op-1", ["lab/db", "vm:vms/gpu"], now - 60)
        self.assertEqual({"lab/web", "vm:vms/win", "lab/db", "vm:vms/gpu"}, HM.excused_apps(now))
        with mock.patch.object(HISTORY, "maintenance_since", side_effect=OSError("disk")):
            self.assertEqual(set(), HM.excused_apps(now), "reported rather than hidden")
        with mock.patch.object(OPS, "stored", side_effect=OSError("disk")):
            self.assertEqual(set(), HM.excused_apps(now))

    def test_only_until_the_settle_time(self):
        now = int(time.time())
        self.declare("op-1", ["lab/db"], now - HM.EXCUSE_SETTLE)
        self.assertEqual({"lab/db"}, HM.excused_apps(now))
        self.assertEqual(set(), HM.excused_apps(now + 1))

    def test_a_reboot_job_or_a_finished_job_excuses_nothing(self):
        OPS.start("node-power", "reboot node-b", {"kind": "Node", "name": "node-b"}, "/nodes",
                  {"node": "node-b", "action": "reboot", "plan": {"workloads": [{"ns": "lab", "name": "web"}]}}, "reviewed")
        job = OPS.start("node-power", "maintenance node-c", {"kind": "Node", "name": "node-c"}, "/nodes",
                        {"node": "node-c", "action": "maintenance", "phase": "draining",
                         "plan": {"workloads": [{"ns": "lab", "name": "db"}]}}, "reviewed")
        OPS.record_phase(job["id"], "failed", 10, "stopped")
        self.assertEqual(set(), HM.excused_apps())

    def test_what_waited_is_excused_for_the_settle_time_after_end(self):
        now = int(time.time())
        held = [{"kind": "Deployment", "ns": "lab", "name": "web"}, {"kind": "VirtualMachine", "ns": "vms", "name": "win"}]
        self.declare("op-1", ["lab/other"], now - 7000, held=held)
        self.assertEqual(set(), HM.excused_apps(now), "the move's excuse is long over, and it is still stopped")
        HISTORY.maintenance_end("op-1", now=now)
        both = {"lab/web", "vm:vms/win"}
        self.assertEqual(both, HM.excused_apps(now + 1), "from End, not from when it opened")
        self.assertEqual(both, HM.excused_apps(now + HM.EXCUSE_SETTLE))
        self.assertEqual(set(), HM.excused_apps(now + HM.EXCUSE_SETTLE + 1))

    def test_an_end_that_finished_later_counts_from_when_it_finished(self):
        now = int(time.time())
        self.declare("op-1", [], now - 7000, held=[{"kind": "Deployment", "ns": "lab", "name": "web"}])
        HISTORY.maintenance_end("op-1", now=now - 3000)
        self.assertEqual(set(), HM.excused_apps(now))
        HISTORY.maintenance_release("op-1", now=now)
        self.assertEqual({"lab/web"}, HM.excused_apps(now + 1))

    def test_what_waited_through_a_ran_out_maintenance_is_excused_after_end(self):
        now = int(time.time())
        self.declare("op-1", [], now - 90000, held=[{"kind": "Deployment", "ns": "lab", "name": "web"}])
        HISTORY.maintenance_end("op-1", now=now)
        self.assertEqual({"lab/web"}, HM.excused_apps(now + 1), "its end is hours ago; End is now")

    def test_a_late_extension_moves_nothing_so_excuses_nothing(self):
        now = int(time.time())
        self.declare("op-1", ["lab/db"], now - 7300)
        HISTORY.maintenance_extend("op-1", now + 600, now=now - 10)
        self.assertEqual(set(), HM.excused_apps(now))

    def test_a_single_host_moves_nothing_so_excuses_nothing(self):
        self.assertEqual([], JOBS._moved_apps({"planned_outage": True, "workloads": [{"ns": "lab", "name": "web"}], "vms": ["vms/win"]}))


if __name__ == "__main__":
    unittest.main()
