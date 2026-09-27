import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vm_resources as resources
import homestead_pod_resources as pods


def vm():
    return {"metadata": {"name": "guest", "namespace": "lab", "uid": "vm-uid", "resourceVersion": "1"},
            "spec": {"template": {"spec": {"domain": {"cpu": {"cores": 2}, "memory": {"guest": "4Gi"}},
                                            "volumes": []}}}}


def child(parent, kind, name, uid):
    return {"metadata": {"name": name, "namespace": "lab", "uid": uid, "resourceVersion": "2",
                         "ownerReferences": [{"apiVersion": "kubevirt.io/v1", "kind": kind,
                                              "name": parent["metadata"]["name"], "uid": parent["metadata"]["uid"], "controller": True}]},
            "spec": {"nodeName": "node1"}, "status": {"nodeName": "node1", "phase": "Running"}}


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.vm = vm()
        self.vmi = child(self.vm, "VirtualMachine", "guest", "vmi-uid")
        self.pod = child(self.vmi, "VirtualMachineInstance", "virt-launcher", "pod-uid")

    def test_uid_chain_not_names_or_labels(self):
        unrelated = copy.deepcopy(self.pod)
        unrelated["metadata"]["ownerReferences"][0]["uid"] = "old-vmi-uid"
        unrelated["metadata"]["labels"] = {"kubevirt.io/domain": "guest"}
        found, known = resources.launchers(self.vm, self.vmi, [unrelated, self.pod])
        self.assertTrue(known)
        self.assertEqual([self.pod], found)

    def test_different_vm_uid_cannot_release_resources(self):
        self.vm["metadata"]["uid"] = "replacement-vm-uid"
        self.assertEqual(([], False), resources.launchers(self.vm, self.vmi, [self.pod]))

    def test_missing_vmi_or_inventory_does_not_invent_released_capacity(self):
        self.assertEqual(([], False), resources.launchers(self.vm, None, [self.pod]))
        self.assertEqual(([], False), resources.launchers(self.vm, self.vmi, None))

    def test_namespace_and_api_group_must_match(self):
        for key, value in (("namespace", "other"), ("namespace", "")):
            changed = copy.deepcopy(self.pod)
            changed["metadata"][key] = value
            self.assertEqual([], resources.launchers(self.vm, self.vmi, [changed])[0])
        self.pod["metadata"]["ownerReferences"][0]["apiVersion"] = "impostor.io/v1"
        self.assertEqual([], resources.launchers(self.vm, self.vmi, [self.pod])[0])

    def test_ambiguous_controller_ownership_is_not_reclaimed(self):
        self.pod["metadata"]["ownerReferences"] *= 2
        self.assertEqual([], resources.launchers(self.vm, self.vmi, [self.pod])[0])

    def test_terminating_pods_still_reserve_resources(self):
        self.pod["metadata"]["deletionTimestamp"] = "now"
        self.assertEqual([self.pod], resources.launchers(self.vm, self.vmi, [self.pod])[0])
        with self.assertRaisesRegex(ValueError, "changing"):
            resources.resident(self.vm, self.vmi, [self.pod])

    def test_terminal_pods_are_not_residents(self):
        for phase in ("Succeeded", "Failed"):
            self.pod["status"]["phase"] = phase
            self.assertEqual([], resources.launchers(self.vm, self.vmi, [self.pod])[0])

    def test_migration_does_not_select_one_of_two_launchers(self):
        target = copy.deepcopy(self.pod)
        target["metadata"]["uid"] = "migration-target"
        with self.assertRaisesRegex(ValueError, "single"):
            resources.resident(self.vm, self.vmi, [self.pod, target])
        self.vmi["status"]["migrationState"] = {"migrationUid": "migration"}
        with self.assertRaisesRegex(ValueError, "migrating"):
            resources.resident(self.vm, self.vmi, [self.pod])

    def test_resident_is_detached_copy_and_requires_version(self):
        actual = resources.resident(self.vm, self.vmi, [self.pod])
        actual["metadata"]["uid"] = "changed"
        self.assertEqual("pod-uid", self.pod["metadata"]["uid"])
        del self.pod["metadata"]["resourceVersion"]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            resources.resident(self.vm, self.vmi, [self.pod])

    def test_resident_node_must_match_vmi(self):
        self.vmi["status"]["nodeName"] = "other"
        with self.assertRaisesRegex(ValueError, "changing"):
            resources.resident(self.vm, self.vmi, [self.pod])


class ResourceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.vm = vm()
        self.spec = self.vm["spec"]["template"]["spec"]
        self.domain = self.spec["domain"]

    def project(self, configuration=None):
        return resources.project(self.vm, {} if configuration is None else configuration)

    def pod(self, result):
        return result["manifest"]["spec"]["template"]["spec"]

    def test_guest_memory_is_not_called_complete_launcher_memory(self):
        result = self.project()
        self.assertGreater(result["memory_estimate_bytes"], 4 * 1024**3)
        self.assertEqual(4 * 1024**3, pods.pod_request(self.pod(result), "memory"))
        self.assertTrue(result["request_is_lower_bound"])
        self.assertNotIn("limits", self.pod(result)["containers"][0]["resources"])
        self.assertIn("not KubeVirt's exact", " ".join(result["warnings"]))

    def test_large_vm_allowance_scales_and_max_guest_counts_for_pressure(self):
        self.domain["memory"] = {"guest": "32Gi", "maxGuest": "64Gi"}
        result = self.project()
        self.assertGreater(result["planning_overhead_bytes"], 3 * 1024**3)
        self.assertGreater(result["memory_estimate_bytes"], 64 * 1024**3)
        self.assertEqual(32 * 1024**3, pods.pod_request(self.pod(result), "memory"))

    def test_cpu_allocation_ratio_and_explicit_cpu_override(self):
        self.assertEqual(200, pods.pod_request(self.pod(self.project()), "cpu"))
        self.assertEqual(500, pods.pod_request(self.pod(self.project({"developerConfiguration": {"cpuAllocationRatio": 4}})), "cpu"))
        self.domain["resources"] = {"requests": {"cpu": "700m"}}
        self.assertEqual(700, pods.pod_request(self.pod(self.project({"developerConfiguration": {"cpuAllocationRatio": 4}})), "cpu"))

    def test_cpu_limit_without_request_is_used(self):
        self.domain["resources"] = {"limits": {"cpu": "1500m"}}
        self.assertEqual(1500, pods.pod_request(self.pod(self.project()), "cpu"))

    def test_cpu_topology_multiplies_sockets_cores_threads(self):
        self.domain["cpu"] = {"sockets": 2, "cores": 3, "threads": 2}
        self.assertEqual(1200, pods.pod_request(self.pod(self.project()), "cpu"))

    def test_invalid_allocation_ratio_and_fractional_cores_reject(self):
        for ratio in (0, -1, "NaN", "Infinity", "bad"):
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                self.project({"developerConfiguration": {"cpuAllocationRatio": ratio}})
        self.domain["cpu"]["cores"] = 1.5
        with self.assertRaises(ValueError):
            self.project()

    def test_unreadable_config_does_not_claim_emulation_or_kvm_defaults(self):
        result = resources.project(self.vm)
        self.assertIn("configuration is unavailable", " ".join(result["warnings"]))
        self.assertNotIn("devices.kubevirt.io/kvm", self.pod(result)["containers"][0]["resources"]["requests"])

    def test_kvm_requirement_and_emulation(self):
        self.assertEqual(1, pods.pod_request(self.pod(self.project()), "devices.kubevirt.io/kvm"))
        result = self.project({"developerConfiguration": {"useEmulation": True}})
        self.assertEqual(0, pods.pod_request(self.pod(result), "devices.kubevirt.io/kvm"))
        self.assertIn("software emulation", " ".join(result["warnings"]))

    def test_dedicated_cpu_and_emulator_are_not_overcommitted(self):
        self.domain["cpu"].update({"dedicatedCpuPlacement": True, "isolateEmulatorThread": True})
        result = self.project()
        self.assertEqual(3000, pods.pod_request(self.pod(result), "cpu"))
        self.assertEqual("true", self.pod(result)["nodeSelector"]["cpumanager"])
        self.assertIn("SMT", " ".join(result["warnings"]))

    def test_invalid_dedicated_requests_are_blockers(self):
        self.domain["cpu"]["dedicatedCpuPlacement"] = True
        for amount in ("500m", "3", "0"):
            self.domain["resources"] = {"requests": {"cpu": amount}}
            self.assertTrue(self.project()["blockers"])

    def test_hugepages_not_double_counted_as_ordinary_memory(self):
        self.domain["memory"]["hugepages"] = {"pageSize": "2Mi"}
        result = self.project()
        self.assertEqual(4 * 1024**3, pods.pod_request(self.pod(result), "hugepages-2Mi"))
        self.assertEqual(0, pods.pod_request(self.pod(result), "memory"))
        self.assertGreater(result["memory_estimate_bytes"], 4 * 1024**3)

    def test_hugepages_require_alignment(self):
        self.domain["memory"] = {"guest": "3Mi", "hugepages": {"pageSize": "2Mi"}}
        self.assertIn("multiple", " ".join(self.project()["blockers"]))

    def test_devices_accumulate_and_architecture_model_features_constrain(self):
        self.domain["devices"] = {"gpus": [{"deviceName": "vendor/gpu"}, {"deviceName": "vendor/gpu"}],
                                  "hostDevices": [{"deviceName": "vendor/coral"}]}
        self.spec["architecture"] = "amd64"
        self.domain["cpu"].update({"model": "Skylake-Client", "features": [{"name": "vmx"}]})
        pod = self.pod(self.project())
        self.assertEqual(2, pods.pod_request(pod, "vendor/gpu"))
        self.assertEqual(1, pods.pod_request(pod, "vendor/coral"))
        self.assertEqual("amd64", pod["nodeSelector"]["kubernetes.io/arch"])
        self.assertEqual("true", pod["nodeSelector"]["cpu-model.node.kubevirt.io/Skylake-Client"])
        self.assertEqual("true", pod["nodeSelector"]["cpu-feature.node.kubevirt.io/vmx"])

    def test_conflicting_selectors_are_not_overwritten(self):
        self.spec["nodeSelector"] = {"kubevirt.io/schedulable": "false"}
        result = self.project()
        self.assertTrue(result["blockers"])
        self.assertEqual("false", self.pod(result)["nodeSelector"]["kubevirt.io/schedulable"])

    def test_missing_guest_memory_or_unexpanded_profile_is_not_small_vm(self):
        self.domain.pop("memory")
        self.assertTrue(self.project()["blockers"])
        self.vm["spec"]["instancetype"] = {"name": "large"}
        self.assertIn("expanded", " ".join(self.project()["blockers"]))
        expanded = {"domain": {"memory": {"guest": "32Gi"}}}
        result = resources.project(self.vm, {}, expanded_spec=expanded)
        self.assertFalse(result["blockers"])
        self.assertEqual(32 * 1024**3, result["guest_memory_bytes"])

    def test_projection_preserves_scheduling_and_claims_but_no_secrets(self):
        self.spec["affinity"] = {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}
        self.spec["volumes"] = [{"name": "root", "dataVolume": {"name": "root-disk"}},
                                {"name": "data", "persistentVolumeClaim": {"claimName": "data-disk"}},
                                {"name": "cloud-init", "cloudInitNoCloud": {"userData": "password: secret"}}]
        self.vm["spec"]["template"]["metadata"] = {"annotations": {"private": "secret"}, "labels": {"app": "guest"}}
        before = copy.deepcopy(self.vm)
        result = self.project()
        self.assertEqual(before, self.vm)
        self.assertNotIn("secret", str(result))
        self.assertEqual(2, len(self.pod(result)["volumes"]))
        self.assertEqual(self.spec["affinity"], self.pod(result)["affinity"])

    def test_implicit_overcommit_changes_reservation_not_physical_estimate(self):
        self.domain["memory"]["guest"] = "3Gi"
        result = self.project({"developerConfiguration": {"memoryOvercommit": 150}})
        self.assertEqual(2 * 1024**3, pods.pod_request(self.pod(result), "memory"))
        self.assertGreater(result["memory_estimate_bytes"], 3 * 1024**3)
        self.domain["resources"] = {"requests": {"memory": "1Gi"}}
        result = self.project({"developerConfiguration": {"memoryOvercommit": 150}})
        self.assertEqual(1024**3, pods.pod_request(self.pod(result), "memory"))

    def test_forbidden_cpu_feature_constrains_every_existing_or_term(self):
        self.spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": "zone", "operator": "In", "values": ["a"]}]},
                                  {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["b"]}]}]}}}
        self.domain["cpu"]["features"] = [{"name": "vmx", "policy": "forbid"}]
        terms = self.pod(self.project())["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual(2, len(terms))
        self.assertTrue(all(term["matchExpressions"][-1] == {"key": "cpu-feature.node.kubevirt.io/vmx", "operator": "DoesNotExist"} for term in terms))

    def test_same_device_in_requests_and_hardware_list_is_not_doubled(self):
        self.domain["devices"] = {"gpus": [{"deviceName": "vendor/gpu"}]}
        self.domain["resources"] = {"requests": {"vendor/gpu": "1"}}
        self.assertEqual(1, pods.pod_request(self.pod(self.project()), "vendor/gpu"))


if __name__ == "__main__":
    unittest.main()
