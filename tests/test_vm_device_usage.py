import copy
import unittest
from unittest import mock

import test_vm_capacity as fixtures
import test_vm_resources as objects
import homestead_vm_device_usage as usage

RESOURCE = "example/gpu"


class DeviceAdmissionTests(unittest.TestCase):
    read = fixtures.VMCapacityTests.read
    plan = fixtures.VMCapacityTests.plan

    def setUp(self):
        fixtures.VMCapacityTests.setUp(self)
        self.vm["spec"]["runStrategy"] = "Halted"
        self.vm["spec"]["template"]["spec"]["domain"]["devices"] = {
            "hostDevices": [{"name": "gpu", "deviceName": RESOURCE}]}
        self.config["spec"]["configuration"]["permittedHostDevices"] = {"pciHostDevices": [{"resourceName": RESOURCE}]}
        self.nodes[0]["allocatable"][RESOURCE] = "1"

    def holder(self, name="other", node="node1", phase="Running", pod=True):
        owner = objects.vm()
        owner["metadata"].update(name=name, uid="vm-" + name)
        instance = objects.child(owner, "VirtualMachine", name, "vmi-" + name)
        instance["spec"] = copy.deepcopy(self.vm["spec"]["template"]["spec"])
        instance["status"].update(nodeName=node, phase=phase)
        self.objects[f"/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/{name}"] = instance
        if pod:
            launcher = objects.child(instance, "VirtualMachineInstance", "launcher-" + name, "pod-" + name)
            launcher["spec"] = {"nodeName": node, "containers": [{"resources": {"requests": {RESOURCE: "1"}}}]}
            self.pods.append(launcher)
        return instance

    def intent(self, name="other", phase="accepted"):
        return {"kind": "vm-power", "status": "running", "ref": {"namespace": "lab", "name": name,
            "uid": "vm-" + name, "version": "1", "phase": phase, "retain_resources": True,
            "device_requests": {RESOURCE: 1}, "device_nodes": ["node1"]}}

    def running(self):
        self.vm["spec"]["runStrategy"] = "Always"
        instance, launcher = fixtures.VMCapacityTests.running(self)
        launcher["spec"]["containers"][0]["resources"]["requests"][RESOURCE] = "1"
        return instance, launcher

    def test_running_edit_reuses_own_gpu_and_usb_without_releasing_cpu_or_ram(self):
        usb = "example/usb"
        self.vm["spec"]["template"]["spec"]["domain"]["devices"]["hostDevices"].append(
            {"name": "keyboard", "deviceName": usb})
        self.nodes[0]["allocatable"][usb] = "1"
        self.config["spec"]["configuration"]["permittedHostDevices"]["usb"] = [{"resourceName": usb}]
        _, launcher = self.running()
        launcher["spec"]["containers"][0]["resources"]["requests"][usb] = "1"
        before = copy.deepcopy(self.pods)
        result = self.plan(action="edit", current=copy.deepcopy(self.vm))
        self.assertFalse(result["blocked"], result)
        self.assertEqual([], result["device_conflicts"])
        self.assertEqual({RESOURCE: 1, usb: 1}, result["vm"]["context"]["reused_device_requests"])
        self.assertEqual({RESOURCE: 1, usb: 1}, result["vm"]["context"]["device_requests"])
        self.assertEqual(4.5, result["candidates"][0]["reserved_gb"])
        self.assertEqual(before, self.pods, "observation and the resident's reservations remain intact")

    def test_running_edit_requires_capacity_for_additional_devices(self):
        self.running()
        self.vm["spec"]["template"]["spec"]["domain"]["devices"]["hostDevices"].append(
            {"name": "gpu2", "deviceName": RESOURCE})
        self.assertTrue(self.plan(action="edit")["blocked"])
        self.nodes[0]["allocatable"][RESOURCE] = "2"
        self.assertFalse(self.plan(action="edit")["blocked"])
        self.holder()
        result = self.plan(action="edit")
        self.assertTrue(result["blocked"])
        self.assertIn("lab/other", str(result["device_conflicts"]))

    def test_another_pending_start_does_not_block_edit_of_current_device_holder(self):
        self.running()
        result = self.plan(action="edit", power_intents=[self.intent()])
        self.assertFalse(result["blocked"], result)
        self.assertEqual([], result["device_conflicts"])
        self.assertTrue(self.plan(power_intents=[self.intent()])["blocked"], "Start still cannot acquire the held device")

    def test_running_edit_does_not_credit_staged_devices_or_unverified_launchers(self):
        instance, launcher = self.running()
        instance["spec"]["domain"]["devices"] = {}
        self.assertTrue(self.plan(action="edit")["blocked"], "a saved device is not proof of live allocation")
        instance["spec"] = copy.deepcopy(self.vm["spec"]["template"]["spec"])
        for mutation in ("owner", "phase", "migration", "reservation"):
            with self.subTest(mutation=mutation):
                original = copy.deepcopy((instance, launcher))
                if mutation == "owner":
                    launcher["metadata"]["ownerReferences"][0]["uid"] = "other-vmi"
                elif mutation == "phase":
                    launcher["status"]["phase"] = "Pending"
                elif mutation == "migration":
                    instance["status"]["migrationState"] = {"migrationUid": "moving"}
                else:
                    del launcher["spec"]["containers"][0]["resources"]["requests"][RESOURCE]
                self.assertTrue(self.plan(action="edit")["blocked"])
                instance.clear(); instance.update(original[0])
                launcher.clear(); launcher.update(original[1])

    def test_busy_device_names_its_holder_but_stopped_configuration_is_allowed(self):
        self.holder()
        start = self.plan()
        self.assertTrue(start["blocked"])
        self.assertIn("VM lab/other", " ".join(start["device_conflicts"]))
        for action in ("edit", "create"):
            with self.subTest(action=action):
                configuration = self.plan(action=action)
                self.assertFalse(configuration["blocked"], configuration)
                self.assertIn("stopped configuration can be saved", " ".join(configuration["warnings"]))
        self.vm["spec"]["runStrategy"] = "Always"
        self.assertTrue(self.plan(action="edit")["blocked"])

    def test_identical_devices_share_a_pool_without_false_conflict_or_double_counting(self):
        self.holder()
        self.nodes[0]["allocatable"][RESOURCE] = "2"
        self.assertFalse(self.plan()["blocked"])
        self.holder("third")
        blocked = self.plan()
        self.assertTrue(blocked["blocked"])
        self.assertIn("lab/third", str(blocked["device_conflicts"]))

    def test_another_host_with_a_free_identical_device_allows_start(self):
        self.holder()
        node = copy.deepcopy(self.nodes[0]); node["name"] = "node2"
        self.nodes.append(node)
        self.assertFalse(self.plan()["blocked"])

    def test_pending_instance_reserves_the_device_before_a_launcher_appears(self):
        for node in ("node1", None):
            self.objects.clear()
            self.holder(node=node, phase="Pending", pod=False)
            with self.subTest(node=node):
                plan = self.plan()
                self.assertTrue(plan["blocked"])
                self.assertIn("lab/other", str(plan["device_conflicts"]))

    def test_terminating_launcher_keeps_the_device_after_instance_completion(self):
        instance = self.holder(phase="Failed")
        self.pods[0]["metadata"]["deletionTimestamp"] = "now"
        self.assertTrue(self.plan()["blocked"])
        self.pods[0]["status"]["phase"] = "Succeeded"
        self.assertFalse(self.plan()["blocked"])

    def test_pending_instance_on_another_selected_host_does_not_block_this_host(self):
        instance = self.holder(node=None, phase="Pending", pod=False)
        instance["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "node2"}
        self.assertFalse(self.plan()["blocked"])
        instance["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "node1"}
        self.assertTrue(self.plan()["blocked"])

    def test_unknown_instance_use_blocks_start_but_not_configuration(self):
        self.objects["/apis/kubevirt.io/v1/virtualmachineinstances"] = {"items": [], "metadata": {"continue": "more"}}
        self.assertTrue(self.plan()["blocked"])
        self.assertFalse(self.plan(action="edit")["blocked"])

    def test_durable_pending_start_is_counted_until_instance_or_explicit_stop_is_observed(self):
        intent = self.intent()
        self.assertTrue(self.plan(power_intents=[intent])["blocked"])
        self.holder()
        self.nodes[0]["allocatable"][RESOURCE] = "2"
        self.assertFalse(self.plan(power_intents=[intent])["blocked"], "intent and its instance must not count twice")
        self.objects.clear(); self.pods.clear(); self.nodes[0]["allocatable"][RESOURCE] = "1"
        stopped = objects.vm(); stopped["metadata"].update(name="other", uid="vm-other", resourceVersion="2")
        stopped["spec"]["runStrategy"] = "Halted"
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/other"] = stopped
        self.assertFalse(self.plan(power_intents=[intent])["blocked"])
        intent["ref"]["phase"] = "uncertain"
        self.assertTrue(self.plan(power_intents=[intent])["blocked"], "an uncertain dispatch still requires inspection")


if __name__ == "__main__":
    unittest.main()
