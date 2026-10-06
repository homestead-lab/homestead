"""Shutdown tests use an in-memory API; they never enter a host namespace."""
import base64
import copy
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import homestead_cluster_shutdown as S


def missing(code=404):
    return urllib.error.HTTPError("fake", code, "fake", {}, None)


def pod(name, node="a", namespace="lab", kind="ReplicaSet"):
    return {"metadata": {"name": name, "namespace": namespace, "uid": name + "-uid", "labels": {"app": name},
                         "ownerReferences": [{"kind": kind, "controller": True, "uid": "owner"}]},
            "spec": {"nodeName": node, "serviceAccountName": "homestead", "containers": []},
            "status": {"phase": "Running"}}


class Fake:
    def __init__(self):
        self.now = 1000
        self.journal = None
        self.configmap = None
        self.calls = []
        self.blocked = False
        self.budgeted = set()      # pods a disruption budget never lets go
        self.stuck = False         # evicted pods that never finish stopping
        self.detach = True
        self.auto_ready = True
        self.on_sleep = lambda: None
        self.nodes = [{"metadata": {"name": name, "uid": name + "-uid", "resourceVersion": "1"}, "spec": {},
                       "status": {"nodeInfo": {"bootID": name + "-boot"}, "conditions": [{"type": "Ready", "status": "True"}]}} for name in ("a", "b", "c")]
        self.pods = [pod("homestead"), pod("app", "b"), pod("daemon", "c", kind="DaemonSet"), pod("dns", "a", "kube-system")]
        self.volumes = [{"metadata": {"name": "data", "namespace": "longhorn-system", "uid": "data-uid"},
                         "spec": {"nodeID": "a"}, "status": {"state": "attached", "robustness": "healthy"}}]
        self.vmis = []
        self.vms = {}
        self.deployment = {"metadata": {"annotations": {}}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "homestead"}}}}
        self.attachments = []
        self.budgets = []
        self.webhooks = []
        self.s = S.Shutdown(self.get, self.send, "lab", "homestead", "repo/image@sha256:" + "a" * 64,
                            clock=lambda: self.now, sleep=self.sleep)

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()

    def get(self, path):
        if path == self.s.path:
            if not self.journal:
                raise missing()
            result = copy.deepcopy(self.journal)
            result["data"] = {k: base64.b64encode(v.encode()).decode() for k, v in result["data"].items()}
        elif path == f"/api/v1/namespaces/lab/configmaps/{S.NAME}":
            if self.configmap is None: raise missing()
            result = self.configmap
        elif path == "/api/v1/nodes": result = {"items": self.nodes}
        elif path.startswith("/api/v1/nodes/"): result = next(n for n in self.nodes if n["metadata"]["name"] == path.split("/")[-1])
        elif path == "/api/v1/pods": result = {"items": self.pods}
        elif "/pods/" in path:
            result = next((p for p in self.pods if p["metadata"]["name"] == path.split("/")[-1]), None)
            if not result: raise missing()
        elif "/deployments/" in path: result = self.deployment
        elif "/virtualmachines/" in path:
            result = self.vms.get(path.rsplit("/", 1)[1])
            if result is None: raise missing()
        elif path == "/api/v1/namespaces/lab/pods": result = {"items": [p for p in self.pods if p["metadata"]["namespace"] == "lab"]}
        elif path == S.LH: result = {"items": self.volumes}
        elif path == S.LH_ATTACHMENTS: result = {"items": self.attachments}
        elif path.startswith(S.ADMISSION): result = {"items": self.webhooks}
        elif "/services/" in path: result = {"spec":{"selector":{"app":path.rsplit("/",1)[1]}}}
        elif path in ("/api/v1/persistentvolumes", "/apis/storage.k8s.io/v1/volumeattachments"): result = {"items": []}
        elif path.endswith("virtualmachineinstances"): result = {"items": self.vmis}
        elif path.endswith("poddisruptionbudgets"): result = {"items": self.budgets}
        else: raise AssertionError(path)
        return copy.deepcopy(result)

    def send(self, method, path, body, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == self.s.path or path == self.s.path.rsplit("/", 1)[0]:
            if method == "POST" and self.journal: raise missing(409)
            if method == "PUT" and body["metadata"].get("resourceVersion") != self.journal["metadata"]["resourceVersion"]: raise missing(409)
            self.journal = copy.deepcopy(body)
            self.journal["data"] = {k: base64.b64decode(v, validate=True).decode() for k, v in body["data"].items()}
            self.journal["metadata"].update(uid="journal-uid", resourceVersion=str(int(self.journal["metadata"].get("resourceVersion", "0")) + 1))
            return copy.deepcopy(self.journal)
        if method == "POST" and path.endswith("/pods"):
            p = copy.deepcopy(body)
            p["metadata"]["uid"] = p["metadata"]["name"] + "-uid"
            self.pods.append(p)
            if self.auto_ready and "agent" in p["spec"]["containers"][0]["command"]:
                i = int(p["spec"]["containers"][0]["command"][-1])
                self.journal["data"]["ready-" + str(i)] = self.nodes[i]["status"]["nodeInfo"]["bootID"]
            return p
        if path.endswith("/eviction"):
            if self.blocked or path.split("/")[-2] in self.budgeted: raise missing(429)
            uid = body["deleteOptions"]["preconditions"]["uid"]
            if self.stuck:
                for p in self.pods:
                    if p["metadata"]["uid"] == uid: p["metadata"]["deletionTimestamp"] = "2026-10-06T00:00:00Z"
                return {}
            self.pods = [p for p in self.pods if p["metadata"]["uid"] != uid]
            if self.detach and not any(p["metadata"]["name"] in ("app", "homestead") for p in self.pods):
                self.volumes[0]["spec"]["nodeID"] = ""
                self.volumes[0]["status"]["state"] = "detached"
            return {}
        if method == "PATCH" and "/deployments/" in path:
            notes = self.deployment["metadata"]["annotations"]
            for k, v in body["metadata"]["annotations"].items():
                notes.pop(k, None) if v is None else notes.__setitem__(k, v)
            self.deployment["spec"].update(body.get("spec") or {})
            if self.deployment["spec"]["replicas"] == 1:      # the scheduler keeps the costly copy
                self.pods = [p for p in self.pods if p["metadata"]["name"] != "homestead-2"]
            return {}
        if method == "PATCH" and "/virtualmachines/" in path:
            vm = self.vms[path.rsplit("/", 1)[1]]
            notes = vm["metadata"].setdefault("annotations", {})
            for k, v in body["metadata"]["annotations"].items():
                notes.pop(k, None) if v is None else notes.__setitem__(k, v)
            vm["spec"].update(body.get("spec") or {})
            return {}
        if method == "PUT" and path.endswith("/start"):
            self.vms[path.split("/")[-2]]["started"] = True
            return {}
        if method == "DELETE" and "/pods/" in path:
            uid = body["preconditions"]["uid"]
            self.pods = [p for p in self.pods if p["metadata"]["uid"] != uid]
            if self.detach and not any(p["metadata"]["name"] in ("app", "homestead") for p in self.pods):
                self.volumes[0]["spec"]["nodeID"] = ""
                self.volumes[0]["status"]["state"] = "detached"
            return {}
        if method == "PUT" and path.endswith("/stop"):
            name = path.split("/")[-2]
            self.vmis = [v for v in self.vmis if v["metadata"]["name"] != name]
            self.pods = [p for p in self.pods if p["metadata"]["name"] != "virt-launcher-" + name]
            self.vms[name]["spec"]["runStrategy"] = "Halted"
            return {}
        if method == "PATCH" and "/pods/" in path:
            return {}
        if path.endswith("/deployments") and method == "POST":
            self.recovery = copy.deepcopy(body)
            return body
        if method == "DELETE" and "/deployments/" in path:
            if not getattr(self, "recovery", None): raise missing()
            self.recovery = None
            return {}
        if method == "PATCH" and path.startswith("/api/v1/nodes/"):
            node = next(n for n in self.nodes if n["metadata"]["name"] == path.split("/")[-1])
            for patch in body:
                keys = [k.replace("~1", "/") for k in patch["path"].split("/")[1:]]
                target = node
                for key in keys[:-1]: target = target[key]
                if patch["op"] == "test": self.assert_value(target[keys[-1]], patch["value"])
                elif patch["op"] == "remove": target.pop(keys[-1])
                else: target[keys[-1]] = patch["value"]
            return node
        raise AssertionError((method, path))

    @staticmethod
    def assert_value(a, b):
        if a != b: raise missing(409)

    def start(self):
        plan = self.s.review()
        ops = Mock()
        ops.start.return_value = {"id": "job"}
        self.s.start({"confirm": S.CONFIRM, "review_token": plan["review_token"]}, ops)
        return S.Coordinator(self.s, self.journal["metadata"]["uid"], self.s.state()["run"])


class ShutdownTests(unittest.TestCase):
    def setUp(self): self.f = Fake()

    def test_configmap_writer_cannot_replace_the_approved_image_or_release_the_fence(self):
        c = self.f.start()
        forged = self.f.s.state()
        forged["plan"]["image"] = "untrusted.example/payload@sha256:" + "b" * 64
        forged["review_token"] = S.hashlib.sha256(S.encode(forged["plan"]).encode()).hexdigest()
        forged["phase"] = "released"
        self.f.configmap = {"kind": "ConfigMap", "metadata": {"uid": "journal-uid"},
                            "data": {"state": S.encode(forged)}}
        self.assertEqual(self.f.s.state()["phase"], "preparing")
        # Match the independent worker's bootstrap: its image comes from the journal.
        self.f.s.image = self.f.s.state()["plan"]["image"]
        c.execute()
        helpers = [p for p in self.f.pods if p["spec"].get("hostPID")]
        self.assertEqual(len(helpers), 3)
        for p in helpers:
            self.assertEqual(p["spec"]["containers"][0]["image"], "repo/image@sha256:" + "a" * 64)
            self.assertEqual(p["metadata"]["ownerReferences"][0]["kind"], "Secret")
        self.assertEqual(self.f.s.state()["phase"], "handoff")
        self.assertTrue(all("/configmaps/" not in path for _, path, _ in self.f.calls))

    def test_forged_configmap_commit_cannot_power_a_host_before_drain(self):
        self.f.start()
        state = self.f.s.state()
        command = Mock(return_value=b"b-boot\n")
        def forge():
            self.f.configmap = {"data": {"state": S.encode(state),
                "commit": S.encode({"run": state["run"], "until": state["deadline"] + 30})}}
            self.f.now = state["deadline"]
        self.f.on_sleep = forge
        with self.assertRaisesRegex(ValueError, "expired"):
            S.agent(self.f.s, "journal-uid", state["run"], 1, command)
        self.assertEqual(command.call_count, 1, "only the read-only host preflight ran")
        self.assertNotIn("commit", self.f.journal["data"])
        self.assertFalse(any(path.endswith("/eviction") or method == "PATCH" for method, path, _ in self.f.calls))

    def test_legacy_configmap_is_never_migrated_into_power_authority(self):
        self.f.configmap = {"data": {"state": S.encode({"phase": "released"})}}
        with self.assertRaisesRegex(ValueError, "Legacy shutdown ConfigMap"):
            self.f.start()
        self.assertFalse(self.f.calls)
        self.assertIsNone(self.f.journal)

    def test_missing_replaced_or_mistyped_secret_disarms_existing_helpers(self):
        self.f.start()
        original = copy.deepcopy(self.f.journal)
        state = self.f.s.state()
        changed = copy.deepcopy(original)
        changed["metadata"]["uid"] = "replacement"
        mistyped = copy.deepcopy(original)
        mistyped["type"] = "Opaque"
        for journal in (None, changed, mistyped):
            self.f.journal = journal
            command = Mock()
            with self.assertRaises(ValueError):
                S.agent(self.f.s, "journal-uid", state["run"], 1, command)
            command.assert_not_called()

    def test_unreadable_secret_never_falls_back_to_a_configmap(self):
        self.f.start()
        state = self.f.s.state()
        self.f.configmap = {"data": {"state": S.encode(state)}}
        get = self.f.s.get
        def forbidden(path):
            if path == self.f.s.path: raise missing(403)
            return get(path)
        self.f.s.get = forbidden
        command = Mock()
        with self.assertRaises(urllib.error.HTTPError):
            S.agent(self.f.s, "journal-uid", state["run"], 1, command)
        command.assert_not_called()

    def test_every_host_is_drained_before_homestead_and_power_commit(self):
        c = self.f.start()
        c.execute()
        self.assertEqual(self.f.s.state()["phase"], "handoff")
        evictions = [path for _, path, _ in self.f.calls if path.endswith("eviction")]
        self.assertEqual(evictions, ["/api/v1/namespaces/lab/pods/app/eviction", "/api/v1/namespaces/lab/pods/homestead/eviction"])
        self.assertTrue(all(n["spec"]["unschedulable"] for n in self.f.nodes))
        self.assertIn("commit", self.f.journal["data"])

    def journal_states(self):
        seen = []
        self.f.on_sleep = lambda: seen.append(json.loads(self.f.journal["data"]["state"]))
        return seen

    def test_a_pod_a_disruption_budget_holds_is_stopped_directly_once_every_host_is_cordoned(self):
        # KubeVirt's virt-controller: its budget wants one copy up, and with
        # every host cordoned no copy can start anywhere, so it never would.
        self.f.budgeted = {"app"}
        seen = self.journal_states()
        c = self.f.start()
        c.execute()
        self.assertEqual("handoff", self.f.s.state()["phase"], self.f.s.state()["message"])
        deletes = [(i, path) for i, (method, path, _) in enumerate(self.f.calls) if method == "DELETE" and "/pods/" in path]
        self.assertEqual(["/api/v1/namespaces/lab/pods/app"], [path for _, path in deletes])
        self.assertEqual({"preconditions": {"uid": "app-uid"}},
                         {k: v for k, v in self.f.calls[deletes[0][0]][2].items() if k == "preconditions"})
        held = [s for s in seen if s.get("drain") and any(p["state"] == "budget" for p in s["drain"]["pods"])]
        self.assertTrue(held, "the wait is shown while the budget holds it")
        self.assertIn("Held by a disruption budget", held[0]["message"])
        waited = sum(1 for s in held) * 2
        self.assertGreaterEqual(waited, S.BUDGET_WAIT, "the budget gets its chance first")

    def test_the_drain_says_how_many_pods_have_stopped_and_where_the_rest_are(self):
        self.f.budgeted = {"app"}
        seen = self.journal_states()
        self.f.start().execute()
        draining = [s for s in seen if s["phase"] == "draining" and s.get("drain")]
        self.assertTrue(draining)
        first = draining[0]
        self.assertEqual((0, 1), (first["drain"]["done"], first["drain"]["total"]))
        self.assertEqual([{"pod": "lab/app", "node": "b", "state": "budget"}], first["drain"]["pods"])
        self.assertTrue(first["message"].startswith("Stopping applications: 0 of 1 pod stopped."))
        self.assertTrue(25 <= first["progress"] < 65)
        later = [s for s in seen if s["phase"] not in ("draining", "stopping-homestead")]
        self.assertTrue(all("drain" not in s for s in later), "gone once the drain is over")

    def test_typed_confirmation_and_changed_review_have_no_effect(self):
        for body in ({"confirm": "yes"}, {"confirm": S.CONFIRM, "review_token": "old"}):
            with self.assertRaises(ValueError): self.f.s.start(body, Mock())
        self.assertFalse(self.f.calls)

    def test_a_vm_no_virtualmachine_manages_blocks_before_any_mutation(self):
        self.f.vmis = [pod("vm")]
        self.assertIn("stop them by hand first: lab/vm", " ".join(self.f.s.review()["blockers"]))
        self.assertFalse(self.f.calls)

    def managed_vm(self):
        vmi = pod("vm", kind="VirtualMachine")
        vmi["status"]["phase"] = "Running"
        self.f.vmis = [vmi]
        self.f.vms["vm"] = {"metadata": {"name": "vm", "annotations": {}}, "spec": {"runStrategy": "RerunOnFailure"}}
        self.f.pods.append(pod("virt-launcher-vm", "b"))

    def test_vms_are_shut_down_from_inside_first_and_started_again_on_recovery(self):
        self.managed_vm()
        review = self.f.s.review()
        self.assertTrue(review["ready"], review["blockers"])
        c = self.f.start()
        c.execute()
        self.assertIn(("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachines/vm/stop", {}), self.f.calls)
        stop = next(i for i, call in enumerate(self.f.calls) if call[1].endswith("/vm/stop"))
        cordon = next(i for i, call in enumerate(self.f.calls) if call[0] == "PATCH" and call[1].startswith("/api/v1/nodes/"))
        self.assertLess(stop, cordon, "VMs stop before any host is cordoned")
        self.assertEqual(c.run, self.f.vms["vm"]["metadata"]["annotations"][S.HELD_BY])
        self.f.s.release_holds(self.f.s.state()["plan"], c.run)
        self.assertEqual("RerunOnFailure", self.f.vms["vm"]["spec"]["runStrategy"], "Harvester's restart policy is put back")
        self.assertTrue(self.f.vms["vm"].get("started"), "RerunOnFailure leaves a cleanly stopped VM stopped: it is started")
        self.assertNotIn(S.HELD_BY, self.f.vms["vm"]["metadata"]["annotations"])

    def test_several_homestead_copies_run_as_one_and_go_back_on_recovery(self):
        self.f.deployment["spec"]["replicas"] = 2
        self.f.pods.append(pod("homestead-2", "b"))
        review = self.f.s.review()
        self.assertTrue(review["ready"], review["blockers"])
        self.assertEqual(2, review["homestead_copies"])
        c = self.f.start()
        c.execute()
        self.assertIn("commit", self.f.journal["data"], self.f.s.state()["message"])
        self.assertTrue(any(call[1] == "/api/v1/namespaces/lab/pods/homestead" and call[0] == "PATCH" for call in self.f.calls),
                        "the coordinating copy is the one kept")
        self.assertEqual("2", self.f.deployment["metadata"]["annotations"][S.HELD_AS])
        self.f.s.release_holds(self.f.s.state()["plan"], c.run)
        self.assertEqual(2, self.f.deployment["spec"]["replicas"])

    def test_a_shutdown_that_fails_starts_its_vms_again(self):
        self.managed_vm()
        c = self.f.start()
        self.f.stuck = True
        c.execute()
        self.assertEqual("failed", self.f.s.state()["phase"])
        self.assertEqual("RerunOnFailure", self.f.vms["vm"]["spec"]["runStrategy"])

    def test_duplicate_and_lost_submission_do_not_create_another_shutdown(self):
        self.f.start()
        before = len(self.f.calls)
        with self.assertRaises(ValueError): self.f.start()
        self.assertEqual(len(self.f.calls), before)

    def test_a_drain_that_never_finishes_never_stops_homestead_or_commits_power(self):
        c = self.f.start()
        self.f.stuck = True
        c.execute()
        self.assertEqual(self.f.s.state()["phase"], "failed")
        self.assertNotIn("commit", self.f.journal["data"])
        self.assertTrue(any(p["metadata"]["name"] == "homestead" for p in self.f.pods))
        self.assertTrue(all(not n["spec"]["unschedulable"] for n in self.f.nodes))

    def test_attached_storage_after_homestead_stops_prevents_power(self):
        c = self.f.start()
        self.f.detach = False
        c.execute()
        self.assertNotIn("commit", self.f.journal["data"])
        self.assertIn("did not detach", self.f.s.state()["message"])

    def test_a_detached_volume_is_not_an_unknown_one(self):
        # Longhorn reports no live health for a detached volume: "unknown".
        self.f.volumes.append({"metadata": {"name": "spare", "namespace": "longhorn-system", "uid": "spare-uid"},
                               "spec": {}, "status": {"state": "detached", "robustness": "unknown"}})
        self.assertTrue(self.f.s.review()["ready"], self.f.s.review()["blockers"])
        self.f.volumes[-1]["status"]["state"] = "attached"
        self.assertIn("Repair faulted or unknown Longhorn volumes before shutting down", self.f.s.review()["blockers"])

    def test_a_volume_held_only_to_rebuild_a_copy_does_not_hold_up_power(self):
        c = self.f.start()
        self.f.detach = False
        self.f.attachments = [{"metadata": {"name": "data"}, "spec": {"attachmentTickets": {
            "volume-rebuilding-controller-data": {"type": "volume-rebuilding-controller", "parameters": {"disableFrontend": "any"}}}}}]
        c.execute()
        self.assertIn("commit", self.f.journal["data"], self.f.s.state()["message"])
        # With anything else holding it, power still waits for it.
        self.f.attachments[0]["spec"]["attachmentTickets"]["csi-x"] = {"type": "csi-attacher"}
        self.assertEqual(["data"], self.f.s.volumes_detached())

    def test_power_helpers_must_be_ready_before_any_host_is_cordoned(self):
        self.f.auto_ready = False
        self.f.start().execute()
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.f.calls))
        self.assertNotIn("commit", self.f.journal["data"])

    def test_cancel_during_drain_restores_only_originally_allowed_scheduling(self):
        self.f.nodes[2]["spec"]["unschedulable"] = True
        c = self.f.start()
        self.f.blocked = True
        self.f.on_sleep = self.f.s.cancel
        c.execute()
        self.assertEqual([n["spec"]["unschedulable"] for n in self.f.nodes], [False, False, True])
        self.assertNotIn("commit", self.f.journal["data"])

    def test_cancel_after_commit_is_refused(self):
        self.f.start().execute()
        with self.assertRaises(ValueError): self.f.s.cancel()

    def test_membership_change_blocks_before_draining(self):
        c = self.f.start()
        self.f.nodes[0]["status"]["nodeInfo"]["bootID"] = "different"
        c.execute()
        self.assertNotIn("commit", self.f.journal["data"])
        self.assertFalse(any(path.endswith("eviction") for _, path, _ in self.f.calls))

    def test_a_new_bound_pod_during_drain_stops_shutdown(self):
        c = self.f.start()
        self.f.on_sleep = lambda: self.f.pods.append(pod("surprise"))
        c.execute()
        self.assertNotIn("commit", self.f.journal["data"])

    def test_disappearing_volume_inventory_is_not_detached_storage(self):
        c = self.f.start()
        self.f.on_sleep = lambda: self.f.volumes.clear()
        self.f.detach = False
        c.execute()
        self.assertNotIn("commit", self.f.journal["data"])
        self.assertIn("inventory changed", self.f.s.state()["message"])

    def test_unrelated_cordon_is_not_removed_when_helpers_fail(self):
        self.f.auto_ready = False
        c = self.f.start()
        self.f.on_sleep = lambda: self.f.nodes[1]["spec"].update(unschedulable=True)
        c.execute()
        self.assertTrue(self.f.nodes[1]["spec"]["unschedulable"])

    def test_recovery_waits_for_expiry_and_preserves_original_cordons(self):
        self.f.nodes[2]["spec"]["unschedulable"] = True
        self.f.start().execute()
        state = self.f.s.state()
        with self.assertRaises(ValueError): self.f.s.recover(state["run"])
        self.f.now = state["deadline"] + 181
        with self.assertRaisesRegex(ValueError, "new boot ID"): self.f.s.recover(state["run"])
        for n in self.f.nodes: n["status"]["nodeInfo"]["bootID"] += "-new"
        for p in self.f.pods:
            if S.helper(p, "journal-uid"): p["status"] = {"phase":"Succeeded"}
        self.f.s.recover(state["run"])
        self.assertEqual(self.f.s.state()["phase"], "released")
        self.assertEqual([n["spec"]["unschedulable"] for n in self.f.nodes], [False, False, True])

    def test_a_recovery_deployment_is_made_just_before_power_and_tolerates_cordoned_hosts(self):
        self.f.start().execute()
        body = self.f.recovery
        posts = [path for method, path, _ in self.f.calls if method == "POST"]
        self.assertLess(posts.index("/apis/apps/v1/namespaces/lab/deployments"), len(posts))
        self.assertIn("commit", self.f.journal["data"])
        spec = body["spec"]["template"]["spec"]
        self.assertEqual([{"operator": "Exists"}], spec["tolerations"])
        self.assertEqual(self.f.s.image, spec["containers"][0]["image"])
        self.assertNotIn("volumes", spec)
        self.assertEqual("recover", spec["containers"][0]["command"][2])
        self.assertEqual("journal-uid", body["metadata"]["ownerReferences"][0]["uid"])

    def test_a_shutdown_that_fails_before_power_makes_no_recovery(self):
        self.f.detach = False
        self.f.start().execute()
        self.assertEqual("failed", self.f.s.state()["phase"])
        self.assertIsNone(getattr(self.f, "recovery", None))

    def test_after_power_on_the_cluster_recovers_itself_without_waiting_out_the_deadline(self):
        self.f.nodes[2]["spec"]["unschedulable"] = True
        self.f.start().execute()
        run = self.f.s.state()["run"]
        def hosts_back():                       # the hosts come back while it waits
            for n in self.f.nodes:
                if not n["status"]["nodeInfo"]["bootID"].endswith("-new"):
                    n["status"]["nodeInfo"]["bootID"] += "-new"
            for p in self.f.pods:
                if S.helper(p, "journal-uid"): p["status"] = {"phase": "Succeeded"}
        self.f.on_sleep = hosts_back
        S.recovery(self.f.s, "journal-uid", run)
        self.assertLess(self.f.now, self.f.s.state()["deadline"], "no waiting out the helpers' deadline")
        self.assertEqual("released", self.f.s.state()["phase"])
        self.assertEqual([False, False, True], [n["spec"]["unschedulable"] for n in self.f.nodes], "a cordon from before stays")
        self.assertIsNone(self.f.recovery, "it removes itself")

    def test_recovery_waits_while_any_host_is_on_its_old_boot(self):
        self.f.start().execute()
        run = self.f.s.state()["run"]
        self.f.nodes[0]["status"]["nodeInfo"]["bootID"] += "-new"
        sleeps = []
        def stop():
            sleeps.append(1)
            if len(sleeps) > 3: raise KeyboardInterrupt
        self.f.on_sleep = stop
        with self.assertRaises(KeyboardInterrupt):
            S.recovery(self.f.s, "journal-uid", run)
        self.assertEqual("handoff", self.f.s.state()["phase"])
        self.assertTrue(all(n["spec"]["unschedulable"] for n in self.f.nodes))

    def test_a_run_recovered_by_hand_leaves_the_recovery_to_remove_itself(self):
        self.f.start().execute()
        state = self.f.s.state()
        self.f.now = state["deadline"] + 181
        for n in self.f.nodes: n["status"]["nodeInfo"]["bootID"] += "-new"
        for p in self.f.pods:
            if S.helper(p, "journal-uid"): p["status"] = {"phase": "Succeeded"}
        self.f.s.recover(state["run"])
        S.recovery(self.f.s, "journal-uid", state["run"])
        self.assertIsNone(self.f.recovery)

    def test_agents_only_schedule_power_for_fresh_matching_commit(self):
        self.f.start()
        state = self.f.s.state()
        command = Mock(return_value=b"b-boot\n")
        def commit():
            self.f.journal["data"]["commit"] = S.encode({"run": state["run"], "until": self.f.now + 30})
        self.f.on_sleep = commit
        S.agent(self.f.s, "journal-uid", state["run"], 1, command)
        self.assertEqual(command.call_count, 2)
        self.assertIn("--on-active=30s", command.call_args.args[0][-1])

    def test_homestead_host_receives_later_timer(self):
        self.f.start()
        state = self.f.s.state()
        command = Mock(return_value=b"a-boot\n")
        self.f.on_sleep = lambda: self.f.journal["data"].update(commit=S.encode({"run":state["run"], "until":self.f.now+30}))
        S.agent(self.f.s, "journal-uid", state["run"], 0, command)
        self.assertIn("--on-active=90s", command.call_args.args[0][-1])

    def test_expired_commit_and_wrong_boot_never_send_power(self):
        self.f.start()
        state = self.f.s.state()
        command = Mock(return_value=b"wrong\n")
        with self.assertRaises(ValueError): S.agent(self.f.s, "journal-uid", state["run"], 1, command)
        self.assertEqual(command.call_count, 1)
        command = Mock(return_value=b"b-boot\n")
        self.f.on_sleep = lambda: self.f.journal["data"].update(commit=S.encode({"run":state["run"], "until":self.f.now-1}))
        S.agent(self.f.s, "journal-uid", state["run"], 1, command)
        self.assertEqual(command.call_count, 1)

    def test_missing_api_is_not_success(self):
        self.f.start().execute()
        self.f.now += 1900
        status, percent, message = self.f.s.progress({"ref":{"run":self.f.s.state()["run"]}})
        self.assertEqual(status, "failed")
        self.assertLess(percent, 100)
        self.assertIn("physical", message)

    def test_cancel_before_coordinator_starts_finishes_without_cordoning(self):
        c = self.f.start()
        self.f.s.cancel()
        c.execute()
        self.assertEqual(self.f.s.state()["phase"], "failed")
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.f.calls))

    def test_recovery_refuses_live_helpers_even_after_deadline(self):
        c = self.f.start()
        self.f.s.cancel()
        c.execute()
        self.f.now += 2000
        with self.assertRaisesRegex(ValueError, "helpers have not all terminated"):
            self.f.s.recover(self.f.s.state()["run"])

    def test_a_lost_commit_receipt_never_restores_scheduling(self):
        c = self.f.start()
        send = self.f.s.send
        def lost(method, path, body, **kwargs):
            result = send(method, path, body, **kwargs)
            if method == "PUT" and "commit" in body.get("data", {}):
                raise OSError("reply lost")
            return result
        self.f.s.send = lost
        with self.assertRaises(OSError): c.execute()
        self.assertIn("commit", self.f.journal["data"])
        self.assertTrue(all(n["spec"]["unschedulable"] for n in self.f.nodes))

    def test_still_attached_csi_volume_prevents_power(self):
        c = self.f.start()
        get = self.f.s.get
        self.f.s.get = lambda path: ({"items":[{"metadata":{"name":"external"},"status":{"attached":True}}]}
                                    if path.endswith("volumeattachments") else get(path))
        c.execute()
        self.assertNotIn("commit", self.f.journal["data"])

    def test_missing_longhorn_api_with_longhorn_pv_is_blocked(self):
        get = self.f.s.get
        self.f.volumes = []
        self.f.s.get = lambda path: ({"items":[{"spec":{"csi":{"driver":"driver.longhorn.io","volumeHandle":"missing"}}}]}
                                    if path == "/api/v1/persistentvolumes" else get(path))
        self.assertIn("missing from", " ".join(self.f.s.review()["blockers"]))

    def test_partial_inventory_never_authorizes_shutdown(self):
        get = self.f.s.get
        self.f.s.get = lambda path: ({"items":self.f.nodes,"metadata":{"continue":"more"}} if path == "/api/v1/nodes" else get(path))
        with self.assertRaisesRegex(ValueError, "Incomplete"): self.f.s.review()

    def test_admission_webhook_backends_survive_application_drain(self):
        self.f.pods.append(pod("webhook", "c", "cattle-system"))
        self.f.webhooks = [{"webhooks":[{"clientConfig":{"service":{"namespace":"cattle-system","name":"webhook"}}}]}]
        self.f.start().execute()
        self.assertEqual(self.f.s.state()["phase"], "handoff")
        self.assertTrue(any(p["metadata"]["name"] == "webhook" for p in self.f.pods))

    def test_storage_backed_admission_dependency_blocks_the_review(self):
        backend = pod("webhook", "c", "cattle-system")
        backend["spec"]["volumes"] = [{"persistentVolumeClaim":{"claimName":"data"}}]
        self.f.pods.append(backend)
        self.f.webhooks = [{"webhooks":[{"clientConfig":{"service":{"namespace":"cattle-system","name":"webhook"}}}]}]
        self.assertIn("separate shutdown plan", " ".join(self.f.s.review()["blockers"]))

    def test_read_only_routes_and_mutations_are_admin_only(self):
        import homestead_route_policy as policy
        for method, path in (("GET", "/api/cluster/shutdown"), ("GET", "/api/cluster/shutdown/plan"),
                             ("POST", "/api/cluster/shutdown"), ("POST", "/api/cluster/shutdown/cancel"),
                             ("POST", "/api/cluster/shutdown/recover")):
            self.assertEqual(policy.role(path, method), "admin")

    def test_server_transport_holds_mutations_while_shutdown_is_active(self):
        from unittest import mock
        import server
        for phase in ("preparing", "draining", "stopping-homestead", "handoff"):
            with mock.patch.object(server.CLUSTER_SHUTDOWN.Shutdown, "state", return_value={"phase":phase}), \
                 mock.patch.object(server.urllib.request, "urlopen") as send:
                with self.assertRaisesRegex(ValueError, "shutdown is active"):
                    server._ksend("PATCH", "/apis/apps/v1/namespaces/lab/deployments/app", {})
                send.assert_not_called()


if __name__ == "__main__": unittest.main()
