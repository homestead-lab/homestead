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
import homestead_lifecycle as LC


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
            obj.setdefault("spec", {}).update(body.get("spec") or {})
        return {}


DEP = "/apis/apps/v1/namespaces/lab/deployments/plex"
VM = "/apis/kubevirt.io/v1/namespaces/vms/virtualmachines/gpu"


class StopRestoreTests(unittest.TestCase):
    def setUp(self):
        self.k = Objects({DEP: {"metadata": {"name": "plex"}, "spec": {"replicas": 2}},
                          VM: {"metadata": {"name": "gpu"}, "spec": {"runStrategy": "RerunOnFailure"}}})
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

    def test_another_jobs_hold_is_not_taken_over(self):
        HOLD.stop(self.items[0], "job-1")
        with self.assertRaisesRegex(ValueError, "another host power job"):
            HOLD.stop(self.items[0], "job-2")

    def test_a_manual_vm_is_started_by_asking(self):
        self.k.objects[VM]["spec"]["runStrategy"] = "Manual"
        held = [HOLD.stop(self.items[1], "job-1")]
        HOLD.restore(held, "job-1")
        self.assertIn(("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/vms/virtualmachines/gpu/start", {}), self.k.sent)


class OrderTests(unittest.TestCase):
    """Single host: the review is rechecked before anything stops. Several
    hosts: cordon, then stop and move, then drain what is left."""
    def run_power(self, planned_outage):
        calls = []
        with mock.patch.object(LC, "NODE_POWER_ENABLED", True), \
             mock.patch.object(LC, "quorum_report", return_value={}), \
             mock.patch.object(LC, "node_action_check", return_value=(True, "", {})), \
             mock.patch.object(LC, "set_cordon", side_effect=lambda *a: calls.append("cordon")), \
             mock.patch.object(LC, "drain", side_effect=lambda *a, **k: calls.append(("drain", k["resumed"])) or {"evicted": [], "skipped": []}), \
             mock.patch.object(LC, "_send_power", side_effect=lambda *a: calls.append("send") or {}):
            LC.node_power("k1", "reboot", True, before_send=lambda: calls.append("recheck"), reviewed_pods=[],
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

    def test_back_after_a_power_off_even_with_homestead_down_meanwhile(self):
        state, _, message = POWER.status(self.job("poweroff"))
        self.assertEqual(("succeeded", ["job-1"]), (state, self.restored))
        self.assertIn("powered off and has started again", message)


if __name__ == "__main__":
    unittest.main()
