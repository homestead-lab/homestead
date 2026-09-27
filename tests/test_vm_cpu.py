import copy
import unittest

import test_vm_resources as fixtures
import homestead_vm_cpu as cpu
import homestead_vm_resources as resources
import homestead_pod_resources as pods


class CPUProjectionTests(unittest.TestCase):
    def setUp(self):
        self.vm = fixtures.vm()
        self.spec = self.vm["spec"]["template"]["spec"]
        self.domain = self.spec["domain"]

    def project(self, version="v1.9.0", config=None):
        return cpu.project(self.vm, self.spec, config or {}, version)

    def pool(self, count=3):
        self.domain.update(ioThreadsPolicy="supplementalPool", ioThreads={"supplementalPoolThreadCount": count})

    def test_modern_shared_cpu_counts_pool_and_respects_explicit_request(self):
        self.pool()
        for version in ("v1.6.0", "1.7.2", "v1.8.1", "v1.9.0"):
            with self.subTest(version=version):
                self.assertEqual(500, self.project(version)["cpu_millis"])
        self.assertEqual(1250, self.project(config={"developerConfiguration": {"cpuAllocationRatio": 4}})["cpu_millis"])
        self.domain["resources"] = {"requests": {"cpu": "700m"}}
        self.assertEqual(700, self.project()["cpu_millis"])

    def test_legacy_unknown_custom_or_upgrading_cpu_is_conservative_and_explained(self):
        self.pool()
        for version in (None, "v1.3.1", "v1.5.0", "v1.6.0-vendor.1", "v1.10.0", "bogus"):
            with self.subTest(version=version):
                result = self.project(version)
                self.assertEqual(3200, result["cpu_millis"])
                self.assertIn("not an exact", " ".join(result["warnings"]))
        self.domain["resources"] = {"requests": {"cpu": "700m"}}
        self.assertEqual(3700, self.project("v1.5.0")["cpu_millis"])

    def test_dedicated_pool_uses_whole_cores_and_emulator_parity(self):
        self.pool(1)
        self.domain["cpu"].update(dedicatedCpuPlacement=True, isolateEmulatorThread=True)
        self.assertEqual(4000, self.project()["cpu_millis"])
        self.vm["spec"]["template"]["metadata"] = {"annotations": {cpu.PARITY: ""}}
        self.assertEqual(5000, self.project()["cpu_millis"])
        self.assertEqual(4000, self.project("v1.5.0")["cpu_millis"])
        self.assertEqual(5000, self.project(None)["cpu_millis"])
        self.domain["cpu"]["cores"] = 3
        self.assertEqual(5000, self.project()["cpu_millis"])
        self.assertEqual(6000, self.project("v1.5.0")["cpu_millis"])

    def test_dedicated_explicit_cpu_is_guest_topology_not_pool_or_emulator(self):
        self.pool(3)
        self.domain["cpu"].update(dedicatedCpuPlacement=True, isolateEmulatorThread=True)
        self.domain["resources"] = {"requests": {"cpu": "2"}, "limits": {"cpu": "2"}}
        result = self.project()
        self.assertFalse(result["blockers"])
        self.assertEqual(6000, result["cpu_millis"])
        self.domain["resources"]["requests"]["cpu"] = "5"
        self.assertTrue(self.project()["blockers"])

    def test_invalid_or_missing_pool_is_not_silently_ignored(self):
        for count in (0, -1, 1.5, "bad", "NaN", True, 4294967296):
            self.pool(count)
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.project()
        self.pool(2)
        self.domain["ioThreadsPolicy"] = "auto"
        self.assertTrue(self.project()["blockers"])
        self.domain.update(ioThreadsPolicy="supplementalPool", ioThreads={})
        self.assertTrue(self.project()["blockers"])

    def test_observed_version_does_not_use_requested_or_in_flight_target(self):
        self.assertIsNone(cpu.observed_version({"spec": {"imageTag": "v1.9.0"}}))
        self.assertIsNone(cpu.observed_version({"status": {"targetKubeVirtVersion": "v1.9.0"}}))
        self.assertIsNone(cpu.observed_version({"status": {"observedKubeVirtVersion": "v1.5.0", "targetKubeVirtVersion": "v1.9.0"}}))
        self.assertEqual("v1.9.0", cpu.observed_version({"status": {"observedKubeVirtVersion": "v1.9.0", "targetKubeVirtVersion": "v1.9.0"}}))

    def test_no_topology_uses_cpu_limit_before_request_for_memory_floor(self):
        self.domain.pop("cpu")
        self.domain["resources"] = {"requests": {"cpu": "1500m"}, "limits": {"cpu": "64"}}
        result = self.project()
        self.assertEqual(64, result["vcpus"])
        self.assertEqual(1500, result["cpu_millis"])
        self.assertGreater(result["memory_floor"], 700 * cpu.MIB)

    def test_high_vcpu_and_pool_floor_is_not_a_memory_request_or_limit(self):
        self.domain["cpu"]["cores"] = 128
        self.pool(32)
        before = copy.deepcopy(self.vm)
        result = resources.project(self.vm, {}, kubevirt_version="v1.9.0")
        self.assertGreater(result["planning_overhead_bytes"], 1500 * cpu.MIB)
        pod = result["manifest"]["spec"]["template"]["spec"]
        self.assertEqual(16005, pods.pod_request(pod, "cpu"))
        self.assertEqual(4 * 1024**3 + 35_000_000, pods.pod_request(pod, "memory"))
        self.assertNotIn("limits", pod["containers"][0]["resources"])
        self.assertEqual(before, self.vm)

    def test_memory_floor_includes_graphics_guaranteed_and_nonshrinking_scale(self):
        baseline = self.project()["memory_floor"]
        self.domain["devices"] = {"autoattachGraphicsDevice": False}
        self.assertEqual(baseline - 32 * cpu.MIB, self.project()["memory_floor"])
        self.domain["cpu"]["dedicatedCpuPlacement"] = True
        self.assertEqual(baseline + 68 * cpu.MIB, self.project()["memory_floor"])
        default = resources.project(self.vm, {})["planning_overhead_bytes"]
        for ratio, multiplier in (("0.5", 1), ("2", 2)):
            result = resources.project(self.vm, {"additionalGuestMemoryOverheadRatio": ratio})
            self.assertEqual(default * multiplier, result["planning_overhead_bytes"])


if __name__ == "__main__":
    unittest.main()
