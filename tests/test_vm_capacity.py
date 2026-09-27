import copy
import unittest
import urllib.error
from unittest import mock

import test_vm_resources as fixtures
import homestead_vm_capacity as capacity
import homestead_place as place


class VMCapacityTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.nodes = [{"name": "node1", "status": "Ready", "schedulable": True,
                       "labels": {"kubevirt.io/schedulable": "true"}, "mem_cap_gb": 16,
                       "mem_used_gb": 2, "mem_metrics_available": True,
                       "allocatable": {"cpu": "8", "memory": "16Gi", "pods": "110", "devices.kubevirt.io/kvm": "100",
                                       "devices.kubevirt.io/tun": "100", "devices.kubevirt.io/vhost-net": "100"}}]
        self.config = {"metadata": {"name": "kubevirt", "namespace": "kubevirt", "uid": "kv-uid", "resourceVersion": "1"}, "spec": {"configuration": {}}}
        self.pods = []
        self.objects = {}
        self.vmi_path = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/guest"

    def read(self, path):
        if path == "/apis/kubevirt.io/v1/kubevirts":
            return {"items": [self.config]}
        if path == "/api/v1/pods":
            return {"items": self.pods}
        if path in self.objects:
            value = self.objects[path]
            if isinstance(value, Exception):
                raise value
            return value
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    def plan(self, **kwargs):
        with mock.patch.object(place, "hardware_features", return_value=[]):
            return capacity.plan(self.vm, self.read, self.nodes, **kwargs)

    def disk(self, phase="Bound", dv=False):
        self.vm["spec"]["template"]["spec"]["volumes"] = [{"name": "root", **({"dataVolume": {"name": "root"}} if dv else {"persistentVolumeClaim": {"claimName": "root"}})}]
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/root"] = {
            "metadata": {"namespace": "lab", "name": "root", "uid": "disk-uid", "resourceVersion": "1"},
            "spec": {"volumeName": "pv-root", "accessModes": ["ReadWriteOnce"], "volumeMode": "Block", "storageClassName": "longhorn"},
            "status": {"phase": phase}}
        self.objects["/api/v1/persistentvolumes/pv-root"] = {
            "metadata": {"name": "pv-root", "uid": "pv-uid", "resourceVersion": "1"},
            "spec": {"claimRef": {"name": "root", "namespace": "lab", "uid": "disk-uid"}}}

    def running(self):
        vmi = fixtures.child(self.vm, "VirtualMachine", "guest", "vmi-uid")
        vmi["spec"] = copy.deepcopy(self.vm["spec"]["template"]["spec"])
        pod = fixtures.child(vmi, "VirtualMachineInstance", "launcher", "pod-uid")
        pod["spec"].update({"containers": [{"name": "compute", "resources": {"requests": {"memory": "4608Mi", "cpu": "200m"}}}]})
        self.objects[self.vmi_path] = vmi
        self.pods = [pod]
        return vmi, pod

    def test_cold_start_projects_guest_plus_allowance_not_an_exact_limit(self):
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(6.3, result["candidates"][0]["projected_gb"])
        self.assertEqual(4.0, result["vm"]["guest_memory_gb"])
        self.assertTrue(result["vm"]["request_is_lower_bound"])

    def test_ram_warning_overridable_but_missing_kvm_is_blocked(self):
        self.nodes[0]["mem_used_gb"] = 14
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertIn("projected RAM", " ".join(result["warnings"]))
        del self.nodes[0]["allocatable"]["devices.kubevirt.io/kvm"]
        self.assertTrue(self.plan()["blocked"])

    def test_non_kubevirt_node_never_eligible(self):
        self.nodes[0]["labels"] = {}
        self.assertTrue(self.plan()["blocked"])

    def test_uid_version_context_changes_when_config_changes(self):
        before = self.plan()["vm"]["context"]
        self.config["metadata"]["resourceVersion"] = "2"
        self.assertNotEqual(before, self.plan()["vm"]["context"])

    def test_unreadable_config_blocks_hardware_assumptions(self):
        with mock.patch.object(capacity, "_items", side_effect=ValueError("incomplete")):
            result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertIn("configuration must be readable", " ".join(result["blockers"]))

    def test_block_volume_is_accepted_and_uid_bound(self):
        self.disk()
        result = self.plan()
        self.assertFalse(result["blocked"])
        self.assertEqual("disk-uid", result["vm"]["context"]["dependencies"]["/api/v1/namespaces/lab/persistentvolumeclaims/root"]["uid"])

    def test_disk_not_found_or_forbidden_is_not_overridable(self):
        self.disk()
        path = "/api/v1/namespaces/lab/persistentvolumeclaims/root"
        del self.objects[path]
        self.assertTrue(self.plan()["blocked"])
        self.objects[path] = urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
        self.assertIn("could not be checked", " ".join(self.plan()["blockers"]))

    def test_pending_import_blocks_start(self):
        self.disk(dv=True)
        self.objects["/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/root"] = {
            "metadata": {"name": "root", "namespace": "lab", "uid": "dv-uid", "resourceVersion": "1"}, "status": {"phase": "ImportInProgress"}}
        self.assertIn("has not completed", " ".join(self.plan()["blockers"]))

    def test_pending_first_consumer_claim_can_be_scheduled(self):
        self.disk(phase="Pending")
        pvc = self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/root"]
        del pvc["spec"]["volumeName"]
        self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn"] = {
            "metadata": {"name": "longhorn", "uid": "sc-uid", "resourceVersion": "1"},
            "volumeBindingMode": "WaitForFirstConsumer"}
        self.assertFalse(self.plan()["blocked"])
        self.assertIn("first-consumer", " ".join(self.plan()["warnings"]))

    def test_planned_migratable_vm_disk_not_rejected_as_container_storage(self):
        self.vm["spec"]["template"]["spec"]["volumes"] = [{"name": "root", "persistentVolumeClaim": {"claimName": "root"}}]
        self.objects["/apis/storage.k8s.io/v1/storageclasses/vm-disks"] = {
            "metadata": {"name": "vm-disks", "uid": "sc-uid", "resourceVersion": "1"}, "provisioner": "driver.longhorn.io",
            "parameters": {"migratable": "true"}, "volumeBindingMode": "WaitForFirstConsumer"}
        result = self.plan(action="create", planned_claims={"root": {"access_mode": "ReadWriteMany", "storage_class": "vm-disks"}})
        self.assertFalse(result["blocked"])
        self.assertIn("planned", " ".join(result["warnings"]))

    def test_lost_claim_cannot_be_relabelled_as_planned(self):
        self.disk(phase="Lost")
        result = self.plan(planned_claims={"root": {"access_mode": "ReadWriteMany", "storage_class": "longhorn"}})
        self.assertTrue(result["blocked"])

    def test_rwx_does_not_authorize_two_vm_disk_writers(self):
        self.disk()
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/root"]["spec"]["accessModes"] = ["ReadWriteMany"]
        self.pods.append({"metadata": {"name": "other-vm", "namespace": "lab", "uid": "other-uid", "resourceVersion": "1"},
                          "spec": {"nodeName": "node1", "containers": [], "volumes": [{"persistentVolumeClaim": {"claimName": "root"}}]},
                          "status": {"phase": "Running"}})
        self.assertIn("concurrent disk writers", " ".join(self.plan()["blockers"]))
        self.pods[0]["status"]["phase"] = "Succeeded"
        self.assertFalse(self.plan()["blocked"])

    def test_existing_owned_disk_consumer_is_reused_for_unpause(self):
        self.disk()
        _, pod = self.running()
        pod["spec"]["volumes"] = [{"persistentVolumeClaim": {"claimName": "root"}}]
        self.assertFalse(self.plan(action="unpause")["blocked"])

    def test_missing_lan_network_is_hard_block(self):
        self.vm["spec"]["template"]["spec"]["networks"] = [{"name": "lan", "multus": {"networkName": "default/lan"}}]
        self.assertIn("LAN network default/lan does not exist", self.plan()["blockers"])

    def test_start_does_not_reclaim_running_instance(self):
        self.running()
        result = self.plan()
        self.assertTrue(result["blocked"])
        self.assertEqual(4.5, result["candidates"][0]["reserved_gb"])

    def test_unpause_counts_resident_once_and_keeps_other_reservations(self):
        self.running()
        self.nodes[0]["mem_used_gb"] = 10
        self.pods.append({"metadata": {"name": "other", "namespace": "lab", "uid": "other"},
                          "spec": {"nodeName": "node1", "containers": [{"resources": {"requests": {"memory": "5Gi"}}}]}, "status": {"phase": "Running"}})
        result = self.plan(action="unpause")
        self.assertFalse(result["blocked"])
        self.assertEqual(5.0, result["candidates"][0]["reserved_gb"])
        self.assertEqual(10.0, result["candidates"][0]["projected_gb"])
        self.assertFalse(result["vm"]["request_is_lower_bound"])

    def test_restart_reclaims_only_owned_reservation_not_observed_ram(self):
        self.running()
        self.nodes[0]["mem_used_gb"] = 10
        result = self.plan(action="restart")
        self.assertFalse(result["blocked"])
        self.assertEqual(0.0, result["candidates"][0]["reserved_gb"])
        self.assertGreater(result["candidates"][0]["projected_gb"], 14)
        self.assertIn("post-stop", " ".join(result["warnings"]))

    def test_unpause_requires_provable_resident(self):
        self.assertTrue(self.plan(action="unpause")["blocked"])
        _, pod = self.running()
        pod["metadata"]["ownerReferences"][0]["uid"] = "wrong"
        self.assertTrue(self.plan(action="unpause")["blocked"])

    def test_unpause_uses_running_guest_and_disks_not_a_staged_edit(self):
        self.running()
        self.vm["spec"]["template"]["spec"]["domain"]["memory"]["guest"] = "64Gi"
        self.vm["spec"]["template"]["spec"]["volumes"] = [{"name": "later", "persistentVolumeClaim": {"claimName": "not-created"}}]
        result = self.plan(action="unpause")
        self.assertFalse(result["blocked"])
        self.assertEqual(4.0, result["vm"]["guest_memory_gb"])

    def test_restart_during_migration_is_blocked(self):
        vmi, _ = self.running()
        vmi["status"]["migrationState"] = {"migrationUid": "migrating"}
        self.assertIn("current VMI is migrating; wait for migration to finish", self.plan(action="restart")["blockers"])

    def test_deleting_vm_cannot_start(self):
        self.vm["metadata"]["deletionTimestamp"] = "now"
        self.assertIn("VM is being deleted", self.plan()["blockers"])


if __name__ == "__main__":
    unittest.main()
