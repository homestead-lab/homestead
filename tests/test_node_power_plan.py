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
            "/api/v1/nodes/node1": {"status": {"conditions": [{"type": "Ready", "status": "True"}],
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

    def test_unknown_replica_inventory_is_explicit(self):
        self.objects.pop(f"{power.LH}/replicas")
        plan = power.plan("node1", "poweroff")
        self.assertTrue(plan["storage_unknown"])
        self.assertTrue(plan["requires_data_ack"])
        self.assertFalse(plan["ready"])

    def test_ready_when_no_vms_and_storage_known(self):
        self.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = []
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

    def test_helper_pull_failure_stops_monitoring(self):
        self.objects["/api/v1/namespaces/lab/pods/power-helper"] = {
            "status": {"phase": "Pending", "containerStatuses": [{"state": {
                "waiting": {"reason": "ImagePullBackOff"}}}]}}
        item = {"ref": {"node": "node1", "action": "reboot", "boot_id": "old",
                        "helper_pod": "power-helper", "started_epoch": power.time.time()}}
        state, _, message = power.status(item)
        self.assertEqual("failed", state)
        self.assertIn("cordoned", message)


if __name__ == "__main__":
    unittest.main()
