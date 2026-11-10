"""A host going down: each app and VM on it moves to another host, or stops
and waits for this one and starts again when it is back.

On a single host nothing can move, so everything waits: stopped cleanly
first, so its volumes detach in order rather than with the power."""
import copy
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_power_hold as HOLD
import homestead_power as POWER
import homestead_power_jobs as JOBS
import homestead_lifecycle as LC
import homestead_node_actions as NODE_ACTIONS
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound


def pod(ns, name, node, owner_kind="ReplicaSet", owner="app-7d9f", phase="Running", labels=None):
    return {"metadata": {"namespace": ns, "name": name, "labels": labels or {},
                         "ownerReferences": [{"kind": owner_kind, "name": owner, "controller": True}]},
            "spec": {"nodeName": node}, "status": {"phase": phase}}


def rs(ns, name, deployment):
    return {"metadata": {"namespace": ns, "name": name,
                         "ownerReferences": [{"kind": "Deployment", "name": deployment, "controller": True}]}}


def vmi(ns, name, node, migratable=True, owned=True):
    return {"metadata": {"namespace": ns, "name": name,
                         "ownerReferences": [{"kind": "VirtualMachine", "name": name, "controller": True}] if owned else []},
            "status": {"nodeName": node, "phase": "Running",
                       "conditions": [{"type": "LiveMigratable", "status": "True" if migratable else "False",
                                       "message": "" if migratable else "a host device is passed through"}]}}


class CandidateTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, HOLD)
        HOLD.OWN = ("lab", "homestead")
        self.sets = {("lab", "app-7d9f"): rs("lab", "app-7d9f", "app"),
                     ("lab", "plex-55cf"): rs("lab", "plex-55cf", "plex"),
                     ("lab", "web-1a2b"): rs("lab", "web-1a2b", "web"),
                     ("lab", "homestead-9f"): rs("lab", "homestead-9f", "homestead")}
        self.pods = [pod("lab", "app-1", "k1"),
                     pod("lab", "plex-1", "k1", owner="plex-55cf"),
                     pod("lab", "web-1", "k1", owner="web-1a2b"), pod("lab", "web-2", "k2", owner="web-1a2b"),
                     pod("lab", "homestead-1", "k1", owner="homestead-9f"),
                     pod("kube-system", "coredns-1", "k1", owner="coredns-1"),
                     pod("lab", "db-0", "k1", owner_kind="StatefulSet", owner="db")]

    def items(self, **kw):
        return {i["id"]: i for i in HOLD.candidates("k1", self.pods, [vmi("vms", "win", "k1"), vmi("vms", "gpu", "k1", migratable=False),
                                                                     vmi("vms", "bare", "k1", migratable=False, owned=False)],
                                                    self.sets, {"k1", "k2"}, {("lab", "plex"): []}, **kw)}

    def test_what_each_app_and_vm_may_do(self):
        items = self.items()
        self.assertNotIn("Deployment/lab/homestead", items, "Homestead never stops itself")
        self.assertFalse(any(k.startswith("Deployment/kube-system") for k in items))
        self.assertEqual((["move", "wait"], "move"), (items["Deployment/lab/app"]["options"], items["Deployment/lab/app"]["default"]))
        self.assertEqual((["wait"], "wait"), (items["Deployment/lab/plex"]["options"], items["Deployment/lab/plex"]["default"]),
                         "pinned to this host: it waits")
        self.assertEqual(["move"], items["Deployment/lab/web"]["options"], "waiting would stop its replica on k2 too")
        self.assertEqual(["move", "wait"], items["StatefulSet/lab/db"]["options"])
        self.assertEqual("move", items["VirtualMachine/vms/win"]["default"])
        self.assertEqual((["wait"], "wait"), (items["VirtualMachine/vms/gpu"]["options"], items["VirtualMachine/vms/gpu"]["default"]))
        self.assertIn("passed through", items["VirtualMachine/vms/gpu"]["why"])
        self.assertEqual([], items["VirtualMachine/vms/bare"]["options"])

    def test_on_a_single_host_everything_waits(self):
        self.pods = [p for p in self.pods if p["spec"]["nodeName"] == "k1"]
        items = self.items(single_host=True)
        self.assertTrue(all(i["default"] == "wait" for k, i in items.items() if k != "VirtualMachine/vms/bare"))
        self.assertEqual("the only host", items["Deployment/lab/app"]["why"])

    def test_a_choice_must_be_allowed(self):
        items = list(self.items().values())
        with self.assertRaisesRegex(ValueError, "plex cannot move"):
            HOLD.choose(items, {"Deployment/lab/plex": "move"})
        picks = HOLD.choose([i for i in items if i["options"]], {"Deployment/lab/app": "wait"})
        self.assertEqual(("wait", "move"), (picks["Deployment/lab/app"], picks["VirtualMachine/vms/win"]))

    def test_a_statefulset_that_deletes_its_volumes_when_stopped_never_waits(self):
        # #1222: scaled to none with whenScaled: Delete, Kubernetes deletes its
        # claims. Moving drains it instead, and a drain keeps them.
        deletes = [statefulset("db", "Delete")]
        items = self.items(statefulsets=deletes)
        self.assertEqual(["move"], items["StatefulSet/lab/db"]["options"])
        self.assertEqual("", items["StatefulSet/lab/db"]["why"], "it simply moves")
        with self.assertRaisesRegex(ValueError, "db cannot wait here"):
            HOLD.choose([i for i in items.values() if i["options"]], {"StatefulSet/lab/db": "wait"})
        self.assertEqual(["move", "wait"], self.items(statefulsets=[statefulset("db", "Retain")])["StatefulSet/lab/db"]["options"])
        self.assertEqual(["move", "wait"], self.items(statefulsets=[statefulset("db")])["StatefulSet/lab/db"]["options"],
                         "no policy is Retain")
        self.pods = [p for p in self.pods if p["spec"]["nodeName"] == "k1"]
        alone = self.items(single_host=True, statefulsets=deletes)["StatefulSet/lab/db"]
        self.assertEqual(([], "it can delete its volumes when stopped"), (alone["options"], alone["why"]),
                         "on the only host it can neither move nor wait")


def statefulset(name, when_scaled=None, replicas=1):
    spec = {"replicas": replicas}
    if when_scaled:
        spec["persistentVolumeClaimRetentionPolicy"] = {"whenScaled": when_scaled, "whenDeleted": "Retain"}
    return {"metadata": {"namespace": "lab", "name": name}, "spec": spec}


class Objects:
    def __init__(self, objects):
        self.objects, self.sent = objects, []

    def get(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return copy.deepcopy(self.objects[path])

    def send(self, method, path, body=None, ctype=None):
        self.sent.append((method, path, body))
        if method == "PATCH" and path in self.objects:
            obj = self.objects[path]
            notes = obj.setdefault("metadata", {}).setdefault("annotations", {})
            for key, value in ((body.get("metadata") or {}).get("annotations") or {}).items():
                if value is None:
                    notes.pop(key, None)
                else:
                    notes[key] = value
            before = int(obj.get("spec", {}).get("replicas", 1))
            obj.setdefault("spec", {}).update(body.get("spec") or {})
            self._scaled(path, obj, before)
        return {}

    def _scaled(self, path, obj, before):
        """The StatefulSet controller, as #1222 saw it: scaled down with
        whenScaled: Delete, the claims of the ordinals it stops are deleted."""
        policy = (obj["spec"].get("persistentVolumeClaimRetentionPolicy") or {}).get("whenScaled", "Retain")
        if "/statefulsets/" not in path or policy != "Delete":
            return
        ns, name = path.split("/")[5], path.rsplit("/", 1)[1]
        for ordinal in range(int(obj["spec"]["replicas"]), before):
            self.objects.pop(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/data-{name}-{ordinal}", None)


DEP = "/apis/apps/v1/namespaces/lab/deployments/plex"
VM = "/apis/kubevirt.io/v1/namespaces/vms/virtualmachines/gpu"


class StopRestoreTests(unittest.TestCase):
    def setUp(self):
        self.k = Objects({DEP: {"metadata": {"name": "plex"}, "spec": {"replicas": 2}},
                          VM: {"metadata": {"name": "gpu"}, "spec": {"runStrategy": "RerunOnFailure"}}})
        keep_bound(self, HOLD)
        HOLD.bind(self.k.get, self.k.send)
        self.items = [{"kind": "Deployment", "ns": "lab", "name": "plex"}, {"kind": "VirtualMachine", "ns": "vms", "name": "gpu"}]

    def test_stopped_with_a_mark_and_started_again_as_it_was(self):
        held = [HOLD.stop(i, "job-1") for i in self.items]
        self.assertEqual(0, self.k.objects[DEP]["spec"]["replicas"])
        self.assertEqual("2", self.k.objects[DEP]["metadata"]["annotations"][HOLD.HELD_AS])
        self.assertIn(("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/vms/virtualmachines/gpu/stop", {}), self.k.sent,
                      "the guest is shut down from inside, not killed")
        # A resumed job stops them again without forgetting what they were.
        held = [HOLD.stop(i, "job-1") for i in self.items]
        self.assertEqual(["2", "RerunOnFailure"], [h["was"] for h in held])
        self.k.objects[VM]["spec"]["runStrategy"] = "Halted"
        started, left = HOLD.restore(held, "job-1")
        self.assertEqual((["lab/plex", "vms/gpu"], []), (started, left))
        self.assertEqual(2, self.k.objects[DEP]["spec"]["replicas"])
        self.assertEqual("RerunOnFailure", self.k.objects[VM]["spec"]["runStrategy"], "Harvester's restart policy is kept")
        self.assertNotIn(HOLD.HELD_BY, self.k.objects[DEP]["metadata"]["annotations"])

    def test_something_changed_since_is_left_alone(self):
        held = [HOLD.stop(self.items[0], "job-1")]
        self.k.objects[DEP]["metadata"]["annotations"].pop(HOLD.HELD_BY)
        self.k.objects[DEP]["spec"]["replicas"] = 1
        self.assertEqual(([], ["lab/plex (changed since)"]), HOLD.restore(held, "job-1"))
        self.assertEqual(1, self.k.objects[DEP]["spec"]["replicas"])

    def test_a_statefulset_that_deletes_its_volumes_when_stopped_is_left_running(self):
        # A policy changed after the review: the stop itself checks again.
        sts, claim = "/apis/apps/v1/namespaces/lab/statefulsets/db", "/api/v1/namespaces/lab/persistentvolumeclaims/data-db-0"
        self.k.objects.update({sts: statefulset("db", "Delete"), claim: {"metadata": {"name": "data-db-0"}}})
        with self.assertRaisesRegex(ValueError, r"^StatefulSet lab/db can delete its volumes when stopped\. "
                                                r"Change its scale-down retention policy \(whenScaled\) to Retain before "):
            HOLD.stop({"kind": "StatefulSet", "ns": "lab", "name": "db"}, "job-1")
        self.assertEqual([], self.k.sent, "nothing is written, not even the mark")
        self.assertIn(claim, self.k.objects, "its claim is still there")
        self.assertEqual(1, self.k.objects[sts]["spec"]["replicas"])
        # Retain: stopped and marked as any other app, and its claim stays.
        self.k.objects[sts] = statefulset("db", "Retain")
        self.k.objects[sts]["metadata"]["resourceVersion"] = "7"
        HOLD.stop({"kind": "StatefulSet", "ns": "lab", "name": "db"}, "job-1")
        self.assertEqual((0, "1"), (self.k.objects[sts]["spec"]["replicas"], self.k.objects[sts]["metadata"]["annotations"][HOLD.HELD_AS]))
        self.assertIn(claim, self.k.objects)
        self.assertEqual("7", self.k.sent[-1][2]["metadata"]["resourceVersion"],
                         "fenced on the version whose policy was read: a change in between fails the stop")
        HOLD.stop(self.items[0], "job-1")
        self.assertNotIn("resourceVersion", self.k.sent[-1][2]["metadata"], "a Deployment has no such policy")

    def test_the_simulated_controller_deletes_claims_as_kubernetes_does(self):
        # Keeps the fake honest: without the check, this is what a stop did.
        sts, claim = "/apis/apps/v1/namespaces/lab/statefulsets/db", "/api/v1/namespaces/lab/persistentvolumeclaims/data-db-0"
        self.k.objects.update({sts: statefulset("db", "Delete"), claim: {"metadata": {"name": "data-db-0"}}})
        self.k.send("PATCH", sts, {"spec": {"replicas": 0}})
        self.assertNotIn(claim, self.k.objects)

    def test_another_jobs_hold_is_not_taken_over(self):
        HOLD.stop(self.items[0], "job-1")
        with self.assertRaisesRegex(ValueError, "held by another job"):
            HOLD.stop(self.items[0], "job-2")

    def test_a_manual_vm_is_started_by_asking(self):
        self.k.objects[VM]["spec"]["runStrategy"] = "Manual"
        held = [HOLD.stop(self.items[1], "job-1")]
        HOLD.restore(held, "job-1")
        self.assertIn(("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/vms/virtualmachines/gpu/start", {}), self.k.sent)


class StoppedBeforeRecordedTests(unittest.TestCase):
    """#1321: an app stopped by a job that then failed was left at 0, because
    the job recorded what it held only after every stop had gone through."""

    def setUp(self):
        self.k = Objects({DEP: {"metadata": {"name": "plex"}, "spec": {"replicas": 2}},
                          VM: {"metadata": {"name": "gpu"}, "spec": {"runStrategy": "RerunOnFailure"}}})
        keep_bound(self, HOLD)
        HOLD.bind(self.k.get, self.k.send)
        self.recorded = []
        self.plan = {"node": "k1", "choices": {"Deployment/lab/plex": "wait", "VirtualMachine/vms/gpu": "wait"},
                     "hold": [{"id": "Deployment/lab/plex", "kind": "Deployment", "ns": "lab", "name": "plex"},
                              {"id": "VirtualMachine/vms/gpu", "kind": "VirtualMachine", "ns": "vms", "name": "gpu"}]}

    def progress(self, phase, percent, message, **details):
        if "held" in details:
            self.recorded = copy.deepcopy(details["held"])

    def hold(self):
        return JOBS.hold_for_power("job-1", self.plan, self.progress)

    def test_an_app_stopped_before_a_later_stop_failed_is_still_listed_and_started_again(self):
        send = self.k.send

        def failing(method, path, body=None, ctype=None):
            if method == "PUT":
                raise urllib.error.URLError("connection lost")
            return send(method, path, body, ctype)
        HOLD.bind(self.k.get, failing)
        with self.assertRaises(urllib.error.URLError):
            self.hold()
        self.assertEqual(0, self.k.objects[DEP]["spec"]["replicas"])
        self.assertEqual(["lab/plex", "vms/gpu"], [f"{i['ns']}/{i['name']}" for i in self.recorded],
                         "the VM got its mark before its shut down failed, so it is listed too")
        HOLD.bind(self.k.get, send)
        started, left = HOLD.restore(self.recorded, "job-1")
        self.assertEqual((["lab/plex", "vms/gpu"], []), (started, left))
        self.assertEqual(2, self.k.objects[DEP]["spec"]["replicas"], "started as it ran, from its own mark")

    def test_an_app_whose_stop_was_refused_is_left_alone_when_restored(self):
        self.k.objects[DEP]["metadata"]["annotations"] = {HOLD.HELD_BY: "job-0", HOLD.HELD_AS: "3"}
        with self.assertRaisesRegex(ValueError, "held by another job"):
            self.hold()
        started, left = HOLD.restore(self.recorded, "job-1")
        self.assertEqual(([], ["lab/plex (changed since)"]), (started, left))
        self.assertEqual({HOLD.HELD_BY: "job-0", HOLD.HELD_AS: "3"}, self.k.objects[DEP]["metadata"]["annotations"])


class OwnPodTests(unittest.TestCase):
    def test_homestead_is_never_stopped_whatever_its_deployment_is_called(self):
        keep_bound(self, HOLD)
        HOLD.OWN, HOLD.OWN_POD = ("lab", "homestead"), "harvui-5c-x"
        try:
            items = HOLD.candidates("k1", [pod("lab", "harvui-5c-x", "k1", owner="harvui-5c")],
                                    [], {("lab", "harvui-5c"): rs("lab", "harvui-5c", "harvui")}, {"k1"}, single_host=True)
            self.assertEqual([], items)
        finally:
            HOLD.OWN_POD = ""


class ForcedSendTests(unittest.TestCase):
    def test_a_forced_send_goes_through_and_stops_nothing(self):
        # A StatefulSet that would delete its volumes has no option on the only
        # host. Force stops nothing first (hold=None), so it is not asked to choose.
        import types
        import homestead_power_jobs as JOBS
        db = {"id": "StatefulSet/lab/db", "kind": "StatefulSet", "ns": "lab", "name": "db",
              "options": [], "default": "", "why": "it can delete its volumes when stopped"}
        plan = {"ready": True, "review_token": "t", "planned_outage": False, "hold": [db], "stranded": [],
                "requires_data_ack": False, "drain_blockers": []}
        sent = []
        body = {"node": "k1", "confirm": "k1", "action": "reboot", "force": True, "review_token": "t", "choices": {}}
        with mock.patch.object(JOBS, "active_power_job", return_value=None), \
                mock.patch.object(POWER, "plan", lambda *a, **k: copy.deepcopy(plan)), \
                mock.patch.object(HOLD, "stop", side_effect=AssertionError("a forced job stops nothing")), \
                mock.patch.object(JOBS, "send_reviewed_power", lambda p, force, background=False: sent.append((p, force)) or {"id": "op"}):
            reply = JOBS._send_power(types.SimpleNamespace(body=body, query={}, path="/api/node/power"))
        self.assertEqual(202, reply.code)
        self.assertEqual(True, sent[0][1])
        self.assertEqual({}, sent[0][0]["choices"])
        # Not forced, the same item is still refused when chosen.
        with self.assertRaisesRegex(ValueError, "db cannot"):
            HOLD.choose([db], {})


class OrderTests(unittest.TestCase):
    """Single host: the review is rechecked before anything stops. Several
    hosts: cordon, then stop and move, then drain what is left."""
    def run_power(self, planned_outage):
        calls = []
        with mock.patch.object(LC, "NODE_POWER_ENABLED", True), \
             mock.patch.object(NODE_ACTIONS, "quorum_report", return_value={}), \
             mock.patch.object(NODE_ACTIONS, "node_action_check", return_value=(True, "", {})), \
             mock.patch.object(NODE_ACTIONS, "set_cordon", side_effect=lambda *a: calls.append("cordon")), \
             mock.patch.object(NODE_ACTIONS, "drain", side_effect=lambda *a, **k: calls.append(("drain", k["resumed"])) or {"evicted": [], "skipped": []}), \
             mock.patch.object(NODE_ACTIONS, "_send_power", side_effect=lambda *a: calls.append("send") or {}):
            NODE_ACTIONS.node_power("k1", "reboot", True, before_send=lambda: calls.append("recheck"), reviewed_pods=[],
                          planned_outage=planned_outage, hold=lambda: calls.append("hold") or True)
        return calls

    def test_single_host(self):
        self.assertEqual(["recheck", "hold", "send"], self.run_power(True))

    def test_several_hosts(self):
        self.assertEqual(["cordon", "hold", ("drain", True), "recheck", "send"], self.run_power(False))


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.node = {"metadata": {"uid": "u1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}],
                                                           "nodeInfo": {"bootID": "new"}}}
        self.restored = []

        def restore(item):
            item["ref"]["restored"] = {"started": ["lab/plex"], "left": []}
            self.restored.append(item["id"])
            return True
        self.patches = [mock.patch.object(POWER, "kget", lambda path: copy.deepcopy(self.node)),
                        mock.patch.object(POWER, "RESTORE", restore),
                        mock.patch.object(POWER, "_items", lambda path, absent_ok=False: [])]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def job(self, action, **ref):
        return {"id": "job-1", "progress": 20, "ref": {"node": "k1", "node_uid": "u1", "action": action, "boot_id": "old",
                                                       "phase": "observing", "started_epoch": time.time() - 3600,
                                                       "held": [{"kind": "Deployment", "ns": "lab", "name": "plex"}],
                                                       "volumes": ["v1"], **ref}}

    def test_after_a_reboot_what_waited_starts_again(self):
        state, _, message = POWER.status(self.job("reboot"))
        self.assertEqual(["job-1"], self.restored)
        self.assertEqual("succeeded", state)
        self.assertIn("Started again: lab/plex", message)

    def test_a_host_powered_off_keeps_its_apps_waiting_however_long(self):
        self.node["status"]["conditions"][0]["status"] = "Unknown"
        state, _, message = POWER.status(self.job("poweroff"))
        self.assertEqual("running", state, "an hour off is not a failure while apps wait for it")
        self.assertIn("wait for it", message)
        self.assertEqual([], self.restored)

    def test_a_job_that_ends_without_its_host_starts_what_waited(self):
        calls = []
        POWER.RESTORE = lambda item, uncordon=True: calls.append(uncordon) or item["ref"].update(
            restored={"started": ["lab/plex"], "left": []}) or True
        self.node["status"]["conditions"][0]["status"] = "Unknown"
        job = self.job("reboot", started_epoch=time.time() - 3600)
        state, _, message = POWER.status(job)
        self.assertEqual(("failed", [False]), (state, calls), "started on other hosts; this one is not uncordoned")
        self.assertIn("Started again: lab/plex", message)

    def test_only_the_leader_starts_them_and_the_job_waits_for_it(self):
        POWER.RESTORE = lambda item, uncordon=True: False
        self.node["status"]["conditions"][0]["status"] = "Unknown"
        state, _, _ = POWER.status(self.job("reboot", started_epoch=time.time() - 3600))
        self.assertEqual("running", state)

    def test_back_after_a_power_off_even_with_homestead_down_meanwhile(self):
        state, _, message = POWER.status(self.job("poweroff"))
        self.assertEqual(("succeeded", ["job-1"]), (state, self.restored))
        self.assertIn("powered off and has started again", message)


if __name__ == "__main__":
    unittest.main()


class HoldingIsBeforePowerTests(unittest.TestCase):
    def test_a_job_stopping_apps_is_not_read_as_one_waiting_for_power(self):
        # The holding step flipped between its own message and "Waiting for the
        # host to leave Ready", and could have failed at ten minutes.
        job = {"id": "job-1", "progress": 8, "message": "Waiting to stop or move: lab/plex",
               "ref": {"node": "k1", "action": "reboot", "phase": "holding", "phase_at": time.time() - 900, "worker": "w"}}
        with mock.patch.object(POWER, "WORKER_GONE", lambda pod, restarts=None: False), mock.patch.object(POWER, "kget") as kget:
            state, _, message = POWER.status(job)
        self.assertEqual(("running", "Waiting to stop or move: lab/plex"), (state, message))
        kget.assert_not_called()


class UncordonTests(unittest.TestCase):
    """A host this job cordoned is allowed scheduling again when it is back;
    one cordoned before the review stays cordoned."""
    def status(self, cordoned_before, leader=True, action="reboot", forced=False):
        node = {"metadata": {"uid": "u1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"bootID": "new"}}}
        done = []
        def uncordon(item):
            if not leader:
                return False
            item["ref"]["uncordoned"] = True
            done.append(item["id"])
            return True
        item = {"id": "job-1", "progress": 60, "ref": {"node": "k1", "node_uid": "u1", "action": action, "boot_id": "old",
                "phase": "observing", "started_epoch": time.time() - 300, "volumes": [], "forced": forced,
                "cordoned_before": cordoned_before}}
        with mock.patch.object(POWER, "kget", lambda path: copy.deepcopy(node)), mock.patch.object(POWER, "UNCORDON", uncordon), \
             mock.patch.object(POWER, "_items", lambda path, absent_ok=False: []):
            return POWER.status(item), done

    def test_back_from_a_reboot_it_is_allowed_scheduling_again(self):
        (state, _, message), done = self.status(False)
        self.assertEqual(("succeeded", ["job-1"]), (state, done))
        self.assertIn("Scheduling is allowed on it again", message)

    def test_one_cordoned_before_stays_cordoned(self):
        (state, _, message), done = self.status(True)
        self.assertEqual(("succeeded", []), (state, done))
        self.assertIn("stays cordoned, as it was before", message)

    def test_the_job_stays_open_until_the_leader_has_done_it(self):
        (state, _, _), _ = self.status(False, leader=False)
        self.assertEqual("running", state)

    def test_forced_jobs_never_cordoned_so_nothing_to_undo(self):
        (state, _, _), done = self.status(False, forced=True)
        self.assertEqual(("succeeded", []), (state, done))

    def test_a_power_off_returning_is_allowed_scheduling_too(self):
        (state, _, message), done = self.status(False, action="poweroff")
        self.assertEqual(("succeeded", ["job-1"]), (state, done))
