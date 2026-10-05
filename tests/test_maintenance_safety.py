import copy
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import test_node_power_plan as fixtures
import homestead_power as power
import homestead_maintenance as maintenance
import homestead_lifecycle as lifecycle
import homestead_operations as ops


class MaintenanceSafetyTests(unittest.TestCase):
    get = fixtures.PowerPlanTests.get

    def setUp(self):
        fixtures.PowerPlanTests.setUp(self)
        self.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = []
        self.pod = self.objects["/api/v1/pods"]["items"][0]
        self.pod["metadata"].update(uid="pod1", labels={"app": "a"})
        self.pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
        self.budget = {"metadata": {"namespace": "lab", "name": "keep-ready", "generation": 2},
                       "spec": {"selector": {"matchLabels": {"app": "a"}}},
                       "status": {"observedGeneration": 2, "disruptionsAllowed": 1}}

    def with_budget(self):
        self.objects["/apis/policy/v1/poddisruptionbudgets"]["items"] = [self.budget]

    def with_longhorn_manager(self):
        manager = copy.deepcopy(self.pod)
        manager["metadata"].update(namespace="longhorn-system", name="instance-manager-test", uid="im-pod",
            labels={"longhorn.io/component": "instance-manager", "longhorn.io/managed-by": "longhorn-manager",
                    "longhorn.io/node": "node1"},
            ownerReferences=[{"apiVersion": "longhorn.io/v1beta2", "kind": "InstanceManager",
                              "name": "instance-manager-test", "uid": "im-owner", "controller": True}])
        self.objects["/api/v1/pods"]["items"].insert(0, manager)
        self.budget["metadata"].update(namespace="longhorn-system", name="instance-manager-test")
        self.budget["spec"]["selector"] = {"matchLabels": {"longhorn.io/component": "instance-manager"}}
        self.budget["status"]["disruptionsAllowed"] = 0
        self.with_budget()
        return manager

    def test_longhorn_zero_budget_can_wait_during_reviewed_drain(self):
        self.with_longhorn_manager()
        for action in ("reboot", "poweroff"):
            plan = power.plan("node1", action)
            self.assertTrue(plan["ready"])
            self.assertTrue(plan["maintenance"]["budgets"][0]["wait_for_drain"])
            self.assertIn("power will not be sent", " ".join(plan["warnings"]))
        original = power.plan("node1", "reboot")
        with self.assertRaisesRegex(ValueError, "pods remain"):
            power.recheck_after_drain(original)

    def test_longhorn_exception_requires_owned_pod_and_its_fresh_single_budget(self):
        self.with_longhorn_manager()
        original = copy.deepcopy(self.objects)
        mutations = [
            lambda p: p["metadata"]["ownerReferences"][0].update(kind="ReplicaSet"),
            lambda p: p["metadata"]["ownerReferences"][0].update(apiVersion="apps/v1"),
            lambda p: p["metadata"]["ownerReferences"][0].update(controller=False),
            lambda p: p["metadata"]["labels"].update({"longhorn.io/node": "node2"}),
            lambda p: p["metadata"]["labels"].update({"longhorn.io/managed-by": "other"}),
            lambda p: p["metadata"].update(uid=""),
        ]
        for mutate in mutations:
            self.objects = copy.deepcopy(original)
            mutate(self.objects["/api/v1/pods"]["items"][0])
            self.assertFalse(power.plan("node1", "reboot")["ready"])
        for field, value in (("observedGeneration", 1), ("disruptionsAllowed", None)):
            self.objects = copy.deepcopy(original)
            self.objects["/apis/policy/v1/poddisruptionbudgets"]["items"][0]["status"][field] = value
            self.assertFalse(power.plan("node1", "reboot")["ready"])
        self.objects = copy.deepcopy(original)
        budgets = self.objects["/apis/policy/v1/poddisruptionbudgets"]["items"]
        budgets[0]["metadata"]["name"] = "other-budget"
        self.assertFalse(power.plan("node1", "reboot")["ready"])
        self.objects = copy.deepcopy(original)
        budgets = self.objects["/apis/policy/v1/poddisruptionbudgets"]["items"]
        budgets.append(copy.deepcopy(budgets[0]))
        self.assertFalse(power.plan("node1", "reboot")["ready"])

    def test_power_drains_workloads_before_retrying_longhorn_and_final_checks(self):
        self.with_longhorn_manager()
        original = power.plan("node1", "poweroff")
        calls, phases = [], []
        def send(method, path, body):
            name = body["metadata"]["name"]
            calls.append(name)
            if name == "instance-manager-test" and calls.count(name) == 1:
                raise urllib.error.HTTPError(path, 429, "budget prevents eviction", {}, None)
            self.objects["/api/v1/pods"]["items"] = [p for p in self.objects["/api/v1/pods"]["items"]
                                                       if p["metadata"]["name"] != name]
        def checked():
            calls.append("final-check")
            power.recheck_after_drain(original)
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                mock.patch.object(lifecycle, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(lifecycle, "set_cordon", side_effect=lambda *a: calls.append("cordon")), \
                mock.patch.object(lifecycle, "kget", side_effect=self.get), \
                mock.patch.object(lifecycle, "ksend", side_effect=send) as evict, \
                mock.patch.object(lifecycle.time, "sleep"), \
                mock.patch.object(lifecycle, "_send_power", side_effect=lambda *a: calls.append("power") or {}) as helper:
            lifecycle.node_power("node1", "poweroff", reviewed_pods=original["drain_pods"],
                                 before_send=checked, progress=lambda *a: phases.append(a))
        self.assertEqual(["cordon", "app-a", "instance-manager-test", "instance-manager-test", "final-check", "power"], calls)
        self.assertTrue(all(c.args[1].endswith("/eviction") for c in evict.call_args_list))
        self.assertEqual({"uid": "im-pod"}, evict.call_args.args[2]["deleteOptions"]["preconditions"])
        self.assertTrue(any("Waiting for Longhorn" in row[2] for row in phases))
        helper.assert_called_once()

    def test_longhorn_timeout_keeps_host_cordoned_without_power(self):
        self.with_longhorn_manager()
        original = power.plan("node1", "reboot")
        def send(method, path, body):
            if body["metadata"]["name"] == "instance-manager-test":
                raise urllib.error.HTTPError(path, 429, "last replica", {}, None)
            self.objects["/api/v1/pods"]["items"] = [self.objects["/api/v1/pods"]["items"][0]]
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                mock.patch.object(lifecycle, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(lifecycle, "set_cordon") as cordon, \
                mock.patch.object(lifecycle, "kget", side_effect=self.get), \
                mock.patch.object(lifecycle, "ksend", side_effect=send), \
                mock.patch.object(lifecycle.time, "monotonic", side_effect=[0, 121]), \
                mock.patch.object(lifecycle, "_send_power") as helper:
            with self.assertRaisesRegex(ValueError, "Longhorn still prevents eviction.*Power was not sent"):
                lifecycle.node_power("node1", "reboot", reviewed_pods=original["drain_pods"], before_send=lambda: None)
        cordon.assert_called_once_with("node1", True)
        helper.assert_not_called()

    def test_retry_never_evicts_replacement_or_new_unreviewed_pod(self):
        self.with_longhorn_manager()
        original = copy.deepcopy(self.objects)
        for replacement in (False, True):
            self.objects = copy.deepcopy(original)
            def send(method, path, body):
                if body["metadata"]["name"] == "instance-manager-test":
                    if replacement:
                        self.objects["/api/v1/pods"]["items"][0]["metadata"]["uid"] = "replacement"
                    else:
                        new = copy.deepcopy(self.pod)
                        new["metadata"].update(name="new-pod", uid="new-uid")
                        self.objects["/api/v1/pods"]["items"].append(new)
                    raise urllib.error.HTTPError(path, 429, "budget", {}, None)
            snapshot = power.plan("node1", "reboot")["drain_pods"]
            with mock.patch.object(lifecycle, "kget", side_effect=self.get), \
                    mock.patch.object(lifecycle, "ksend", side_effect=send) as evict:
                # The pod that changed is named, so the next look starts there.
                with self.assertRaisesRegex(ValueError, r"pods changed during drain \([\w.-]+/[\w.-]+ (arrived|changed)"):
                    lifecycle.drain("node1", include_system=True, reviewed_pods=snapshot, wait=True)
                self.assertEqual(2, evict.call_count)

    def test_non_budget_refusal_is_not_retried_or_followed_by_power(self):
        self.with_longhorn_manager()
        snapshot = power.plan("node1", "reboot")["drain_pods"]
        for code in (403, 409, 500):
            with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                    mock.patch.object(lifecycle, "node_action_check", return_value=(True, "", {})), \
                    mock.patch.object(lifecycle, "set_cordon"), \
                    mock.patch.object(lifecycle, "kget", side_effect=self.get), \
                    mock.patch.object(lifecycle, "ksend", side_effect=urllib.error.HTTPError("eviction", code, "refused", {}, None)) as evict, \
                    mock.patch.object(lifecycle.time, "sleep") as sleep, \
                    mock.patch.object(lifecycle, "_send_power") as helper:
                with self.assertRaisesRegex(ValueError, "drain was refused"):
                    lifecycle.node_power("node1", "reboot", reviewed_pods=snapshot, before_send=lambda: None)
                self.assertEqual(2, evict.call_count)
                sleep.assert_not_called()
                helper.assert_not_called()

    def test_generic_budget_refusal_is_not_retried_during_drain(self):
        with mock.patch.object(lifecycle, "kget", side_effect=self.get), \
                mock.patch.object(lifecycle, "ksend", side_effect=urllib.error.HTTPError("eviction", 429, "budget", {}, None)) as send, \
                mock.patch.object(lifecycle.time, "sleep") as sleep:
            result = lifecycle.drain("node1", wait=True)
        self.assertEqual(["lab/app-a (HTTP 429)"], result["skipped"])
        send.assert_called_once()
        sleep.assert_not_called()

    def test_accepted_eviction_is_not_repeated_while_pod_terminates(self):
        self.with_longhorn_manager()
        snapshot = power.plan("node1", "reboot")["drain_pods"]
        def send(method, path, body):
            self.objects["/api/v1/pods"]["items"] = [p for p in self.objects["/api/v1/pods"]["items"]
                                                       if p["metadata"]["name"] == "app-a"]
        with mock.patch.object(lifecycle, "kget", side_effect=self.get), \
                mock.patch.object(lifecycle, "ksend", side_effect=send) as evict, \
                mock.patch.object(lifecycle.time, "sleep", side_effect=lambda _: self.objects["/api/v1/pods"].update(items=[])):
            result = lifecycle.drain("node1", include_system=True, reviewed_pods=snapshot, wait=True)
        self.assertEqual(2, evict.call_count)
        self.assertEqual(["lab/app-a", "longhorn-system/instance-manager-test"], result["evicted"])

    def test_pod_not_found_only_finishes_drain_when_inventory_confirms_departure(self):
        def send(method, path, body):
            self.objects["/api/v1/pods"]["items"] = []
            raise urllib.error.HTTPError(path, 404, "gone", {}, None)
        snapshot = power.plan("node1", "reboot")["drain_pods"]
        with mock.patch.object(lifecycle, "kget", side_effect=self.get), mock.patch.object(lifecycle, "ksend", side_effect=send):
            self.assertEqual([], lifecycle.drain("node1", reviewed_pods=snapshot, wait=True)["skipped"])

    def test_zero_budget_blocks_before_drain(self):
        self.with_budget()
        self.budget["status"]["disruptionsAllowed"] = 0
        plan = power.plan("node1", "reboot")
        self.assertFalse(plan["ready"])
        self.assertIn("permits no verified eviction", " ".join(plan["blockers"]))

    def test_offline_survivors_and_duplicate_replica_hosts_are_not_protection(self):
        self.objects[f"{power.LH}/replicas"]["items"].append(copy.deepcopy(self.objects[f"{power.LH}/replicas"]["items"][1]))
        rows = power.plan("node1", "reboot")["volumes"]
        self.assertEqual(1, next(v for v in rows if v["name"] == "vol-a")["healthy_elsewhere"])
        self.objects["/api/v1/nodes"]["items"][0]["status"]["conditions"] = []
        rows = power.plan("node1", "reboot")["volumes"]
        self.assertEqual(0, next(v for v in rows if v["name"] == "vol-a")["healthy_elsewhere"])

    def test_pending_old_helper_prevents_duplicate_power_submission(self):
        self.pod["metadata"]["labels"]["homestead.io/task"] = "node-power"
        self.assertIn("earlier power helper", " ".join(power.plan("node1", "reboot")["blockers"]))

    def test_stale_budget_blocks(self):
        self.with_budget()
        self.budget["status"]["observedGeneration"] = 1
        self.assertFalse(power.plan("node1", "reboot")["ready"])

    def test_policy_v1_null_empty_and_namespace_selectors(self):
        self.with_budget()
        self.budget["status"]["disruptionsAllowed"] = 0
        self.budget["spec"]["selector"] = None
        self.assertTrue(power.plan("node1", "reboot")["ready"])
        self.budget["spec"]["selector"] = {}
        self.assertFalse(power.plan("node1", "reboot")["ready"])
        self.budget["metadata"]["namespace"] = "elsewhere"
        self.assertTrue(power.plan("node1", "reboot")["ready"])

    def test_always_allow_only_applies_to_unhealthy_running_pod(self):
        self.with_budget()
        self.budget["status"]["disruptionsAllowed"] = 0
        self.budget["spec"]["unhealthyPodEvictionPolicy"] = "AlwaysAllow"
        self.assertFalse(power.plan("node1", "reboot")["ready"])
        self.pod["status"]["conditions"] = []
        self.assertTrue(power.plan("node1", "reboot")["ready"])

    def test_multiple_budgets_fail_closed(self):
        self.with_budget()
        self.objects["/apis/policy/v1/poddisruptionbudgets"]["items"].append(copy.deepcopy(self.budget))
        self.assertFalse(power.plan("node1", "reboot")["ready"])

    def test_missing_or_paginated_inventory_is_not_safe(self):
        self.objects["/apis/policy/v1/poddisruptionbudgets"]["metadata"] = {"continue": "next"}
        self.assertFalse(power.plan("node1", "reboot")["ready"])
        self.objects.pop("/apis/policy/v1/poddisruptionbudgets")
        self.assertFalse(power.plan("node1", "reboot")["ready"])

    def test_unmanaged_pods_block_but_daemonsets_and_static_pods_are_skipped(self):
        self.pod["metadata"].pop("ownerReferences")
        self.assertFalse(power.plan("node1", "reboot")["ready"])
        self.pod["metadata"]["annotations"] = {"kubernetes.io/config.mirror": "mirror"}
        self.assertTrue(power.plan("node1", "reboot")["ready"])

    def test_local_and_external_storage_requires_ack_and_is_named(self):
        self.pod["spec"]["volumes"] = [{"name": "cache", "emptyDir": {}},
                                          {"name": "files", "hostPath": {"path": "/files"}},
                                          {"name": "nfs", "nfs": {"server": "fileserver"}}]
        plan = power.plan("node1", "reboot")
        self.assertEqual(3, len(plan["maintenance"]["local_storage"]))
        self.assertTrue(plan["requires_data_ack"])
        self.assertIn("/files", str(plan["maintenance"]["local_storage"]))

    def test_local_pvc_is_not_assumed_replicated(self):
        self.pod["spec"]["volumes"] = [{"name": "files", "persistentVolumeClaim": {"claimName": "data"}}]
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"] = {"spec": {"volumeName": "data-pv"}}
        self.objects["/api/v1/persistentvolumes/data-pv"] = {"spec": {"local": {"path": "/data"}}}
        self.assertEqual("host-local PVC", power.plan("node1", "reboot")["maintenance"]["local_storage"][0]["kind"])
        self.objects.pop("/api/v1/persistentvolumes/data-pv")
        self.assertFalse(power.plan("node1", "reboot")["ready"])

    def test_after_drain_rechecks_replica_loss(self):
        original = power.plan("node1", "reboot")
        self.objects["/api/v1/pods"]["items"] = []
        self.objects[f"{power.LH}/replicas"]["items"][1]["status"]["currentState"] = "stopped"
        with self.assertRaisesRegex(ValueError, "volume impact changed"):
            power.recheck_after_drain(original)

    def test_expected_degradation_after_local_replica_stops_allows_final_check(self):
        original = power.plan("node1", "reboot")
        self.objects["/api/v1/pods"]["items"] = []
        self.objects[f"{power.LH}/replicas"]["items"][0]["status"]["currentState"] = "stopped"
        volume = self.objects[f"{power.LH}/volumes"]["items"][0]
        volume["status"]["robustness"] = "degraded"
        power.recheck_after_drain(original)
        volume["status"]["robustness"] = "faulted"
        with self.assertRaisesRegex(ValueError, "volume impact changed"):
            power.recheck_after_drain(original)

    def test_a_volume_still_attaching_with_its_moved_app_is_given_time_to_settle(self):
        original = power.plan("node1", "reboot")
        self.objects["/api/v1/pods"]["items"] = []
        volume = self.objects[f"{power.LH}/volumes"]["items"][0]
        volume["status"].update(state="attaching", robustness="unknown")
        now = [0]
        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 20:                   # attached on its new host
                volume["status"].update(state="attached", robustness="healthy")
        power.recheck_after_drain(original, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(20, now[0])

    def test_a_volume_that_never_settles_still_stops_power(self):
        original = power.plan("node1", "reboot")
        self.objects["/api/v1/pods"]["items"] = []
        self.objects[f"{power.LH}/volumes"]["items"][0]["status"].update(state="attaching", robustness="unknown")
        now = [0]
        def sleep(seconds): now[0] += seconds
        with self.assertRaisesRegex(ValueError, "volume impact changed"):
            power.recheck_after_drain(original, clock=lambda: now[0], sleep=sleep)
        self.assertGreaterEqual(now[0], power.SETTLE)

    def test_evicted_local_replica_does_not_hide_survivor_loss_or_missing_volume(self):
        original = power.plan("node1", "reboot")
        self.objects["/api/v1/pods"]["items"] = []
        replicas = self.objects[f"{power.LH}/replicas"]["items"]
        replicas.pop(0)
        self.objects[f"{power.LH}/volumes"]["items"][0]["status"]["robustness"] = "degraded"
        power.recheck_after_drain(original)
        replicas[0]["status"]["currentState"] = "stopped"
        with self.assertRaisesRegex(ValueError, "volume impact changed"):
            power.recheck_after_drain(original)
        replicas[0]["status"]["currentState"] = "running"
        self.objects[f"{power.LH}/volumes"]["items"].pop(0)
        with self.assertRaisesRegex(ValueError, "volume impact changed"):
            power.recheck_after_drain(original)

    def test_after_drain_new_pod_blocks_power(self):
        original = power.plan("node1", "reboot")
        with self.assertRaisesRegex(ValueError, "pods remain"):
            power.recheck_after_drain(original)
        self.objects["/api/v1/pods"]["items"] = []
        power.recheck_after_drain(original)

    def test_eviction_uid_precondition_and_inventory_race(self):
        snapshot = maintenance.pod_snapshot([self.pod])
        with mock.patch.object(lifecycle, "kget", side_effect=self.get), mock.patch.object(lifecycle, "ksend") as send:
            lifecycle.drain("node1", include_system=True, reviewed_pods=snapshot)
            self.assertEqual({"uid": "pod1"}, send.call_args.args[2]["deleteOptions"]["preconditions"])
            send.reset_mock()
            self.pod["metadata"]["uid"] = "replacement"
            with self.assertRaisesRegex(ValueError, "changed"):
                lifecycle.drain("node1", include_system=True, reviewed_pods=snapshot)
            send.assert_not_called()

    def test_power_requires_review_before_cordon(self):
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), mock.patch.object(lifecycle, "set_cordon") as cordon:
            with self.assertRaisesRegex(ValueError, "reviewed drain"):
                lifecycle.node_power("node1", "reboot")
            cordon.assert_not_called()

    def test_failed_post_drain_check_never_creates_power_helper(self):
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                mock.patch.object(lifecycle, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(lifecycle, "set_cordon"), \
                mock.patch.object(lifecycle, "drain", return_value={"evicted": [], "skipped": []}) as drain, \
                mock.patch.object(lifecycle, "ksend") as send:
            with self.assertRaisesRegex(ValueError, "changed"):
                lifecycle.node_power("node1", "reboot", reviewed_pods=[],
                                     before_send=mock.Mock(side_effect=ValueError("changed")))
            self.assertTrue(drain.call_args.kwargs["include_system"])
            send.assert_not_called()

    def test_power_helper_intent_is_recorded_before_submission(self):
        calls = []
        receipts = []
        def send(method, path, body):
            calls.append("POST")
            return {**body, "metadata": {**body["metadata"], "uid": "helper-uid"}}
        def progress(phase, *args, **details):
            calls.append(phase)
            receipts.append(details)
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                mock.patch.object(lifecycle, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(lifecycle, "set_cordon"), \
                mock.patch.object(lifecycle, "drain", return_value={"evicted": [], "skipped": []}), \
                mock.patch.object(lifecycle, "ksend", side_effect=send):
            lifecycle.node_power("node1", "reboot", reviewed_pods=[], before_send=lambda: calls.append("CHECK"),
                                 progress=progress)
        self.assertLess(calls.index("CHECK"), calls.index("sending"))
        self.assertLess(calls.index("sending"), calls.index("POST"))
        self.assertLess(calls.index("POST"), calls.index("observing"))
        self.assertEqual("helper-uid", receipts[-1]["helper_uid"])

    def test_missing_creation_receipt_leaves_sending_intent_and_does_not_retry(self):
        phases = []
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                mock.patch.object(lifecycle, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(lifecycle, "set_cordon"), \
                mock.patch.object(lifecycle, "drain", return_value={"evicted": [], "skipped": []}), \
                mock.patch.object(lifecycle, "ksend", return_value={}) as send:
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                lifecycle.node_power("node1", "reboot", reviewed_pods=[], before_send=lambda: None,
                                     progress=lambda phase, *a, **k: phases.append(phase))
        self.assertEqual("sending", phases[-1])
        send.assert_called_once()

    def test_post_drain_replacement_with_same_boot_id_blocks_power(self):
        original = power.plan("node1", "reboot")
        self.objects["/api/v1/pods"]["items"] = []
        self.objects["/api/v1/nodes/node1"]["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(ValueError, "identity changed"):
            power.recheck_after_drain(original)

    def test_reboot_observer_accepts_missed_notready_but_needs_known_old_boot(self):
        self.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new"
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old", "started_epoch": power.time.time()}}
        self.assertEqual("succeeded", power.status(item)[0])
        item["ref"]["boot_id"] = ""
        self.assertEqual("running", power.status(item)[0])

    def test_down_host_does_not_wait_forever(self):
        self.objects["/api/v1/nodes/node1"]["status"]["conditions"] = []
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old", "started_epoch": power.time.time() - 601}}
        self.assertEqual("failed", power.status(item)[0])

    def test_interrupted_pre_power_phase_expires_without_power(self):
        item = {"ref": {"phase": "draining", "phase_at": power.time.time() - 241}}
        self.assertEqual("failed", power.status(item)[0])


class MaintenanceJobTests(unittest.TestCase):
    def test_all_install_manifests_include_read_only_pdb_permission(self):
        root = Path(__file__).resolve().parents[1]
        for filename in ("deploy/deploy.yaml", "deploy/rbac.yaml", "charts/homestead/templates/rbac.yaml"):
            text = (root / filename).read_text(encoding="utf-8")
            self.assertIn('apiGroups: ["policy"]\n    resources: [poddisruptionbudgets]\n    verbs: [get, list]', text)

    def test_persisted_phases_and_duplicate_host_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(ops, "DATA_DIR", directory):
                job = ops.start("node-power", "Reboot", {}, "/nodes", {"node": "one", "phase": "reviewed"})
                with self.assertRaisesRegex(ValueError, "already active"):
                    ops.start("node-power", "Reboot", {}, "/nodes", {"node": "one"})
                ops.record_phase(job["id"], "sending", 20, "Sending", helper_pod="helper")
                record = ops._read()[0]
                self.assertEqual("helper", record["ref"]["helper_pod"])
                self.assertEqual("sending", record["ref"]["phase"])
                self.assertEqual("Sending", record["history"][-1]["m"])
                ops.record_phase(job["id"], "failed", 20, "Stopped")
                with self.assertRaisesRegex(ValueError, "ended"):
                    ops.record_phase(job["id"], "observing", 25, "Must not resume")


if __name__ == "__main__":
    unittest.main()
