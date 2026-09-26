import copy
import tempfile
import unittest
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
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                mock.patch.object(lifecycle, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(lifecycle, "set_cordon"), \
                mock.patch.object(lifecycle, "drain", return_value={"evicted": [], "skipped": []}), \
                mock.patch.object(lifecycle, "ksend", side_effect=lambda *a, **k: calls.append("POST")):
            lifecycle.node_power("node1", "reboot", reviewed_pods=[], before_send=lambda: calls.append("CHECK"),
                                 progress=lambda phase, *a, **k: calls.append(phase))
        self.assertLess(calls.index("CHECK"), calls.index("sending"))
        self.assertLess(calls.index("sending"), calls.index("POST"))

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
