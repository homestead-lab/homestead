import copy
import json
import unittest

import test_vm_resources as fixtures
import homestead_vm_resources as resources
import homestead_pod_resources as pods
import homestead_vm_network as network


class NetworkSupportTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.spec = self.vm["spec"]["template"]["spec"]
        self.devices = self.spec["domain"].setdefault("devices", {})
        self.config, self.objects, self.reads = {}, {}, []

    def read(self, path):
        self.reads.append(path)
        return copy.deepcopy(self.objects[path])

    def project(self):
        return resources.project(self.vm, self.config, read=self.read)

    def pod(self, result=None):
        return (result or self.project())["manifest"]["spec"]["template"]["spec"]

    def nad(self, name="lan", resource="vendor/nic", namespace="lab"):
        path = f"/apis/k8s.cni.cncf.io/v1/namespaces/{namespace}/network-attachment-definitions/{name}"
        value = {"metadata": {"name": name, "namespace": namespace, "uid": name + "-uid", "resourceVersion": "1",
                              "annotations": {network.RESOURCE: resource}}, "spec": {"config": "private-CNI-value"}}
        self.objects[path] = value
        return path, value

    def nic(self, name="nic1", nad="lan", binding="sriov"):
        self.spec.setdefault("networks", []).append({"name": name, "multus": {"networkName": nad}})
        self.devices.setdefault("interfaces", []).append({"name": name, binding: {} if binding != "binding" else {"name": "plugin"}})

    def test_implicit_network_and_emulation_device_requirements(self):
        self.assertEqual(1, pods.pod_request(self.pod(), "devices.kubevirt.io/tun"))
        self.assertEqual(1, pods.pod_request(self.pod(), "devices.kubevirt.io/vhost-net"))
        self.config["developerConfiguration"] = {"useEmulation": True}
        self.assertEqual(1, pods.pod_request(self.pod(), "devices.kubevirt.io/tun"))
        self.assertEqual(0, pods.pod_request(self.pod(), "devices.kubevirt.io/vhost-net"))
        self.devices["autoattachPodInterface"] = False
        self.assertEqual(0, pods.pod_request(self.pod(), "devices.kubevirt.io/tun"))

    def test_nonvirtio_interface_does_not_request_vhost_net(self):
        self.nic(binding="bridge")
        self.nad(resource="")
        self.devices["interfaces"][0]["model"] = "e1000"
        self.assertEqual(1, pods.pod_request(self.pod(), "devices.kubevirt.io/tun"))
        self.assertEqual(0, pods.pod_request(self.pod(), "devices.kubevirt.io/vhost-net"))

    def test_same_nad_used_twice_needs_two_devices_but_one_identity_read(self):
        path, _ = self.nad()
        self.nic("nic1")
        self.nic("nic2")
        result = self.project()
        self.assertFalse(result["blockers"])
        self.assertEqual(2, pods.pod_request(self.pod(result), "vendor/nic"))
        self.assertEqual([path], self.reads)
        self.assertEqual("lan-uid", result["dependencies"][path]["uid"])
        self.assertNotIn("private-CNI-value", str(result))

    def test_missing_sriov_annotation_or_unverifiable_nad_is_not_overridable(self):
        self.nic()
        path, value = self.nad(resource="")
        self.assertIn("SR-IOV", " ".join(self.project()["blockers"]))
        for change in ({"namespace": "wrong"}, {"uid": ""}, {"resourceVersion": ""}, {"deletionTimestamp": "now"}):
            _, value = self.nad()
            value["metadata"].update(change)
            self.assertTrue(self.project()["blockers"])
        self.objects.clear()
        self.assertTrue(self.project()["blockers"])

    def test_network_and_host_device_using_same_resource_add_demands(self):
        self.nad()
        self.nic()
        self.devices["hostDevices"] = [{"name": "host-nic", "deviceName": "vendor/nic"}]
        self.assertEqual(2, pods.pod_request(self.pod(), "vendor/nic"))

    def test_duplicate_network_names_unknown_binding_and_dra_are_explicit(self):
        self.nad()
        self.nic()
        self.nic()
        self.assertIn("unique", " ".join(self.project()["blockers"]))
        self.setUp()
        self.nad()
        self.nic(binding="binding")
        self.assertIn("not registered", " ".join(self.project()["blockers"]))
        self.spec["networks"][0] = {"name": "nic1", "resourceClaim": {"claimName": "dynamic"}}
        self.assertIn("dynamic", " ".join(self.project()["blockers"]))

    def test_binding_sidecar_and_compute_memory_are_unique_per_plugin(self):
        self.nad(resource="")
        path, _ = self.nad("binding", resource="")
        self.nic("nic1", binding="binding")
        self.nic("nic2", binding="binding")
        self.config = {"network": {"binding": {"plugin": {"sidecarImage": "private/image", "networkAttachmentDefinition": "binding",
                    "computeResourceOverhead": {"requests": {"memory": "64Mi"}}}}},
                    "supportContainerResources": [{"type": "sidecar", "resources": {"requests": {"cpu": "40m", "memory": "16Mi"}, "limits": {"memory": "32Mi"}}}]}
        result = self.project()
        pod = self.pod(result)
        self.assertFalse(result["blockers"])
        self.assertEqual(1, sum(row["name"].startswith("hook-sidecar") for row in pod["containers"]))
        self.assertEqual(64 * 1024**2, result["additional_overhead_bytes"])
        self.assertEqual(245, pods.pod_request(pod, "cpu"))
        self.assertIn(path, result["dependencies"])
        self.assertNotIn("private/image", str(result))

    def test_serial_console_default_override_and_guaranteed_resources(self):
        self.assertEqual(205, pods.pod_request(self.pod(), "cpu"))
        self.config["virtualMachineOptions"] = {"disableSerialConsoleLog": {}}
        self.assertEqual(200, pods.pod_request(self.pod(), "cpu"))
        self.devices["logSerialConsole"] = True
        self.config["supportContainerResources"] = [{"type": "guest-console-log", "resources": {
            "requests": {"cpu": "20m", "memory": "50Mi"}, "limits": {"cpu": "30m", "memory": "100Mi"}}}]
        self.assertEqual(220, pods.pod_request(self.pod(), "cpu"))
        self.spec["domain"]["cpu"]["dedicatedCpuPlacement"] = True
        self.assertEqual(2030, pods.pod_request(self.pod(), "cpu"))
        self.devices["autoattachSerialConsole"] = False
        self.assertEqual(2000, pods.pod_request(self.pod(), "cpu"))

    def test_disk_filesystem_and_init_peak_resources_are_not_dropped(self):
        self.devices["autoattachSerialConsole"] = False
        self.spec["volumes"] = [{"name": "disk", "containerDisk": {"image": "private/image"}},
                                 {"name": "files", "persistentVolumeClaim": {"claimName": "files"}}]
        self.devices["filesystems"] = [{"name": "files", "virtiofs": {}}]
        self.config["supportContainerResources"] = [{"type": "containerDisk", "resources": {
            "requests": {"memory": "6Gi", "cpu": "1"}, "limits": {"memory": "7Gi", "cpu": "2"}}}]
        result = self.project()
        pod = self.pod(result)
        self.assertEqual(3, len(pod["containers"]))
        self.assertEqual(2, len(pod["initContainers"]))
        self.assertEqual(1210, pods.pod_request(pod, "cpu"))
        self.assertGreater(pods.pod_request(pod, "memory"), 10 * 1024**3)
        self.assertGreater(result["memory_estimate_bytes"], 11 * 1024**3)
        self.assertEqual(100_000_000, pods.pod_request(pod, "ephemeral-storage"))
        self.assertNotIn("private/image", str(result))

    def test_probe_vfio_tpm_vsock_sev_and_reservation_demands(self):
        self.devices.update(autoattachSerialConsole=False, autoattachVSOCK=True, tpm={},
                            hostDevices=[{"name": "device", "deviceName": "vendor/pci"}],
                            disks=[{"name": "disk", "lun": {"reservation": True}}])
        self.spec["readinessProbe"] = {"exec": {"command": ["private-command"]}}
        self.spec["livenessProbe"] = {"exec": {"command": ["another-private-command"]}}
        self.spec["domain"]["launchSecurity"] = {"sev": {}}
        result = self.project()
        self.assertEqual((120 + 1024 + 53 + 256) * 1024**2, result["additional_overhead_bytes"])
        for resource in ("vhost-vsock", "sev", "pr-helper"):
            self.assertEqual(1, pods.pod_request(self.pod(result), "devices.kubevirt.io/" + resource))
        self.assertNotIn("private-command", str(result))
        self.config["permittedHostDevices"] = {"pciHostDevices": [{"resourceName": "other/pci"}]}
        self.assertIn("not permitted", " ".join(self.project()["blockers"]))

    def test_hook_dependencies_are_pinned_but_payload_is_never_exposed(self):
        self.vm["spec"]["template"]["metadata"] = {"annotations": {"hooks.kubevirt.io/hookSidecars": json.dumps([
            {"image": "private/image", "command": ["private-command"], "configMap": {"name": "hook", "key": "script"},
             "pvc": {"name": "files"}}])}}
        for kind, name in (("configmaps", "hook"), ("persistentvolumeclaims", "files")):
            self.objects[f"/api/v1/namespaces/lab/{kind}/{name}"] = {
                "metadata": {"name": name, "namespace": "lab", "uid": name + "-uid", "resourceVersion": "1"},
                "data": {"script": "private-script"}}
        result = self.project()
        self.assertFalse(result["blockers"])
        self.assertEqual(2, len(result["dependencies"]))
        self.assertIn("files", str(self.pod(result)["volumes"]))
        self.assertIn("no memory limit", " ".join(result["warnings"]))
        for private in ("private/image", "private-command", "private-script"):
            self.assertNotIn(private, str(result))
        self.objects.clear()
        self.assertTrue(self.project()["blockers"])

    def test_bad_support_configuration_is_never_ignored(self):
        self.config["supportContainerResources"] = [{"type": "guest-console-log", "resources": {
            "requests": {"memory": "100Gi"}, "limits": {"memory": "1Gi"}}}]
        self.assertIn("exceeds", " ".join(self.project()["blockers"]))
        self.config["supportContainerResources"][0]["resources"]["requests"]["memory"] = "bad"
        with self.assertRaises(ValueError):
            self.project()

    def test_operator_overhead_ratio_scales_allowance_and_probe_not_sidecar_memory(self):
        self.spec["readinessProbe"] = {"exec": {"command": ["true"]}}
        baseline = self.project()
        self.config["additionalGuestMemoryOverheadRatio"] = "2"
        scaled = self.project()
        self.assertEqual(baseline["planning_overhead_bytes"] * 2, scaled["planning_overhead_bytes"])
        self.assertEqual(baseline["additional_overhead_bytes"] * 2, scaled["additional_overhead_bytes"])
        self.assertEqual(baseline["support_memory_bytes"], scaled["support_memory_bytes"])
        self.config["additionalGuestMemoryOverheadRatio"] = "0.5"
        reduced = self.project()
        self.assertEqual(baseline["planning_overhead_bytes"], reduced["planning_overhead_bytes"])
        self.assertEqual(baseline["additional_overhead_bytes"] // 2, reduced["additional_overhead_bytes"])
        self.config["additionalGuestMemoryOverheadRatio"] = ""
        self.assertEqual(baseline["memory_estimate_bytes"], self.project()["memory_estimate_bytes"])


if __name__ == "__main__":
    unittest.main()
