import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_power as power


class PowerPlanTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/api/v1/nodes": {"items": [{"metadata": {"name": "node2"}, "status": {
                "conditions": [{"type": "Ready", "status": "True"}]}}]},
            "/api/v1/nodes/node1": {"metadata": {"uid": "node-uid"}, "status": {"conditions": [{"type": "Ready", "status": "True"}],
                                                 "nodeInfo": {"bootID": "old"}}},
            "/apis/policy/v1/poddisruptionbudgets": {"items": []},
            "/api/v1/pods": {"items": [{"metadata": {"namespace": "lab", "name": "app-a",
                                         "ownerReferences": [{"kind": "ReplicaSet", "controller": True, "uid": "rs"}]},
                                         "spec": {"nodeName": "node1"}}]},
            f"{power.LH}/replicas": {"items": [
                {"spec": {"nodeID": "node1", "volumeName": "vol-a"}, "status": {"currentState": "running"}},
                {"spec": {"nodeID": "node2", "volumeName": "vol-a"}, "status": {"currentState": "running"}},
                {"spec": {"nodeID": "node1", "volumeName": "vol-b"}, "status": {"currentState": "running"}}]},
            f"{power.LH}/volumes": {"items": [
                {"metadata": {"name": "vol-a"}, "status": {"robustness": "healthy",
                    "kubernetesStatus": {"namespace": "lab", "pvcName": "appdata"}}},
                {"metadata": {"name": "vol-b"}, "status": {"robustness": "healthy",
                    "kubernetesStatus": {"namespace": "lab", "pvcName": "only-copy"}}}]},
            "/apis/kubevirt.io/v1/virtualmachineinstances": {"items": [
                {"metadata": {"namespace": "lab", "name": "vm1"}, "status": {"nodeName": "node1"}}]}}
        self.impact = {"workloads": [{"ns": "lab", "name": "app", "stranded": True, "eligible": []}],
                       "stranded": [{"ns": "lab", "name": "app", "stranded": True}]}
        power.bind(self.get, lambda node: copy.deepcopy(self.impact),
                   lambda: {"members": ["node1", "node2", "node3"], "can_lose": 1}, lambda: True)

    def get(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return copy.deepcopy(self.objects[path])

    def test_plan_names_single_copy_and_unavailable_volumes_and_vms(self):
        plan = power.plan("node1", "reboot")
        self.assertFalse(plan["ready"])
        self.assertEqual(["unavailable", "single-copy"], [row["risk"] for row in plan["volumes"]])
        self.assertEqual(["lab/vm1"], plan["vms"])
        self.assertIn("Running VMs", " ".join(plan["blockers"]))
        self.assertTrue(plan["requires_data_ack"])
        self.assertEqual(1, len(plan["stranded"]))
        self.assertEqual(plan["review_token"], power.plan("node1", "reboot")["review_token"])

    def test_a_rebuilding_copy_is_not_a_copy_and_longhorn_would_block_the_drain(self):
        # frigate-recordings2: its copy on k3s-3 was still rebuilding (WO), so
        # the only whole one was on the host; the review counted both, and the
        # drain stalled on Longhorn two minutes in.
        self.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = []
        replicas = self.objects[f"{power.LH}/replicas"]["items"]
        replicas[0]["metadata"], replicas[1]["metadata"] = {"name": "vol-a-r-here"}, {"name": "vol-a-r-there"}
        self.objects[f"{power.LH}/settings"] = {"items": [{"metadata": {"name": "node-drain-policy"}, "value": "block-if-contains-last-replica"}]}
        self.objects[f"{power.LH}/engines"] = {"items": [{"spec": {"volumeName": "vol-a"}, "status": {
            "currentState": "running", "replicaModeMap": {"vol-a-r-here": "RW", "vol-a-r-there": "WO"},
            "rebuildStatus": {"tcp://x": {"isRebuilding": True, "progress": 5}}}}]}
        plan = power.plan("node1", "reboot")
        vol = next(v for v in plan["volumes"] if v["claim"] == "lab/appdata")
        self.assertEqual(("unavailable", 0, 5), (vol["risk"], vol["healthy_elsewhere"], vol["rebuilding_pct"]))
        self.assertFalse(plan["ready"])
        self.assertTrue(any("lab/appdata" in b and "5% done" in b for b in plan["blockers"]), plan["blockers"])
        self.assertTrue(any("lab/only-copy" in b for b in plan["blockers"]), "a one-copy volume blocks the drain too")
        # Force restarts without draining, so Longhorn's drain rule does not apply.
        self.assertFalse(any("lab/appdata" in b for b in power.plan("node1", "reboot", force=True)["blockers"]))
        # Where Longhorn is set to allow it, only the data acknowledgement remains.
        self.objects[f"{power.LH}/settings"] = {"items": [{"metadata": {"name": "node-drain-policy"}, "value": "always-allow"}]}
        plan = power.plan("node1", "reboot")
        self.assertFalse(any("Longhorn will not" in b for b in plan["blockers"]))
        self.assertTrue(plan["requires_data_ack"])

    def test_unknown_replica_inventory_is_explicit(self):
        self.objects.pop(f"{power.LH}/replicas")
        plan = power.plan("node1", "poweroff")
        self.assertTrue(plan["storage_unknown"])
        self.assertTrue(plan["requires_data_ack"])
        self.assertFalse(plan["ready"])

    def test_ready_when_no_vms_and_storage_known(self):
        self.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = []
        # A host holding a volume's only copy drains only where Longhorn allows it.
        self.objects[f"{power.LH}/settings"] = {"items": [{"metadata": {"name": "node-drain-policy"}, "value": "always-allow"}]}
        self.assertTrue(power.plan("node1", "reboot")["ready"])

    def test_quorum_loss_blocks_power(self):
        power.bind(self.get, lambda node: copy.deepcopy(self.impact),
                   lambda: {"members": ["node1", "node2", "node3"], "can_lose": 0}, lambda: True)
        plan = power.plan("node1", "reboot")
        self.assertFalse(plan["ready"])
        self.assertIn("quorum", " ".join(plan["blockers"]))

    def test_reboot_waits_for_new_boot_and_volume_resync(self):
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old",
                        "volumes": ["vol-a"], "started_epoch": power.time.time()}}
        self.assertEqual(20, power.status(item)[1])
        self.objects["/api/v1/nodes/node1"]["status"]["conditions"][0]["status"] = "False"
        self.assertEqual(60, power.status(item)[1])
        self.objects["/api/v1/nodes/node1"]["status"]["conditions"][0]["status"] = "True"
        self.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new"
        self.objects[f"{power.LH}/volumes"]["items"][0]["status"]["robustness"] = "degraded"
        self.assertEqual(90, power.status(item)[1])
        self.objects[f"{power.LH}/volumes"]["items"][0]["status"]["robustness"] = "healthy"
        self.assertEqual("succeeded", power.status(item)[0])

    def test_reboot_resync_timeout_is_actionable(self):
        self.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new"
        self.objects[f"{power.LH}/volumes"]["items"][0]["status"]["robustness"] = "degraded"
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old",
                        "saw_down": True, "volumes": ["vol-a"],
                        "returned_at": power.time.time() - 1801}}
        state, _, message = power.status(item)
        self.assertEqual("failed", state)
        self.assertIn("vol-a", message)

    def test_helper_pull_backoff_keeps_monitoring_because_it_can_still_run(self):
        self.objects["/api/v1/namespaces/lab/pods/power-helper"] = {
            "metadata": {"uid": "helper-uid"},
            "status": {"phase": "Pending", "containerStatuses": [{"state": {
                "waiting": {"reason": "ImagePullBackOff"}}}]}}
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old",
                        "helper_pod": "power-helper", "helper_uid": "helper-uid", "started_epoch": power.time.time()}}
        state, _, message = power.status(item)
        self.assertEqual("running", state)
        self.assertIn("ImagePullBackOff", message)
        self.assertIn("do not send another", message)

    def test_host_replacement_changes_review_and_cannot_confirm_old_request(self):
        before = power.plan("node1", "reboot")
        self.objects["/api/v1/nodes/node1"]["metadata"]["uid"] = "replacement"
        self.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new"
        self.assertNotEqual(before["review_token"], power.plan("node1", "reboot")["review_token"])
        state, _, message = power.status({"ref": {"node": "node1", "node_uid": "node-uid", "action": "reboot", "boot_id": "old"}})
        self.assertEqual("failed", state)
        self.assertIn("identity changed", message)

    def test_missing_host_identity_blocks_new_power_review(self):
        self.objects["/api/v1/nodes/node1"].pop("metadata")
        self.assertIn("Host identity is unavailable", " ".join(power.plan("node1", "reboot")["blockers"]))

    def test_notready_does_not_mean_shutdown_succeeded(self):
        self.objects["/api/v1/nodes/node1"]["status"]["conditions"] = []
        item = {"ref": {"node": "node1", "action": "poweroff", "started_epoch": power.time.time() - 30,
                        "down_at": power.time.time() - 20}}
        self.assertEqual("running", power.status(item)[0])
        item["ref"]["started_epoch"] -= 601
        state, _, message = power.status(item)
        self.assertEqual("failed", state)
        self.assertIn("Shutdown could not be verified", message)
        self.assertIn("physical power", message)

    def test_missing_receipt_does_not_adopt_same_name_helper(self):
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old", "helper_pod": "power-helper"}}
        with mock.patch.object(power, "kget", wraps=self.get) as read:
            state, _, message = power.status(item)
        self.assertEqual("running", state)
        self.assertIn("unconfirmed", message)
        self.assertEqual([mock.call("/api/v1/nodes/node1")], read.call_args_list)

    def test_replaced_helper_and_failed_helper_are_not_success(self):
        path = "/api/v1/namespaces/lab/pods/power-helper"
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old", "helper_pod": "power-helper", "helper_uid": "original"}}
        self.objects[path] = {"metadata": {"uid": "replacement"}, "status": {"phase": "Succeeded"}}
        self.assertIn("replaced", power.status(item)[2])
        self.objects[path] = {"metadata": {"uid": "original"}, "status": {"phase": "Failed"}}
        self.assertEqual("failed", power.status(item)[0])


if __name__ == "__main__":
    unittest.main()
