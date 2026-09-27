import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("numa_probe", ROOT / "server/probe/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
BOOT = "12345678-1234-1234-1234-123456789abc"


class NUMAProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("SYS", "PROC"):
            patch = mock.patch.object(probe, name, str(self.root / name.lower()))
            patch.start()
            self.addCleanup(patch.stop)
        self.put("proc/sys/kernel/random/boot_id", BOOT)
        self.put("sys/devices/system/cpu/online", "0-3")
        self.put("sys/devices/system/node/online", "0-1")
        for node, cpus in ((0, "0-1"), (1, "2-3")):
            self.put(f"sys/devices/system/node/node{node}/cpulist", cpus)
            for cpu in probe._cpu_list(cpus):
                self.put(f"sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list", cpus)
            for key, value in (("nr_hugepages", "512"), ("free_hugepages", "384"), ("surplus_hugepages", "0")):
                self.put(f"sys/devices/system/node/node{node}/hugepages/hugepages-2048kB/{key}", value)
        self.put("sys/kernel/mm/hugepages/hugepages-2048kB/resv_hugepages", "100")

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_physical_topology_and_global_reservations_are_not_free_cpu_capacity(self):
        result = probe.numa_facts()
        self.assertTrue(result["complete"], result)
        self.assertEqual(BOOT, result["boot_id"])
        self.assertEqual([[0, 1]], result["cells"][0]["cores"])
        self.assertEqual([2, 3], result["cells"][1]["online_cpus"])
        self.assertEqual({"reserved": 100}, result["page_pools"]["2097152"])
        self.assertEqual({"total": 512, "free": 384, "surplus": 0}, result["cells"][0]["hugepages"]["2097152"])
        self.assertEqual("unverified", result["cpu_allocation"])

    def test_offline_sibling_is_not_counted(self):
        self.put("sys/devices/system/cpu/online", "0,2-3")
        self.assertEqual([[0]], probe.numa_facts()["cells"][0]["cores"])

    def test_memory_only_node_is_retained(self):
        self.put("sys/devices/system/node/node0/cpulist", "")
        self.put("sys/devices/system/node/node1/cpulist", "0-3")
        result = probe.numa_facts()
        self.assertTrue(result["complete"])
        self.assertEqual([], result["cells"][0]["cores"])

    def test_missing_counter_never_becomes_zero(self):
        (self.root / "sys/kernel/mm/hugepages/hugepages-2048kB/resv_hugepages").unlink()
        result = probe.numa_facts()
        self.assertFalse(result["complete"])
        self.assertEqual([], result["cells"])
        self.assertEqual({}, result["page_pools"])

    def test_inconsistent_cpu_membership_is_unknown(self):
        self.put("sys/devices/system/node/node1/cpulist", "1-3")
        self.assertFalse(probe.numa_facts()["complete"])
        self.put("sys/devices/system/node/node1/cpulist", "2")
        self.assertFalse(probe.numa_facts()["complete"])

    def test_asymmetric_smt_is_unknown(self):
        self.put("sys/devices/system/cpu/cpu1/topology/thread_siblings_list", "1")
        self.assertFalse(probe.numa_facts()["complete"])

    def test_counter_failure_and_impossible_counts_are_unknown(self):
        path = "sys/devices/system/node/node0/hugepages/hugepages-2048kB/free_hugepages"
        for value in ("private-error", "-1", "99999999999999999999999", "513"):
            self.put(path, value)
            result = probe.numa_facts()
            self.assertFalse(result["complete"])
            self.assertNotIn("private-error", str(result))

    def test_reboot_or_hotplug_mid_read_discards_evidence(self):
        original = probe._read
        for suffix, change in (("boot_id", "changed"), ("cpu/online", "0"), ("node0/cpulist", "0")):
            count = 0
            def read(path):
                nonlocal count
                if path.endswith(suffix):
                    count += 1
                    if count > 1:
                        return change
                return original(path)
            with mock.patch.object(probe, "_read", side_effect=read):
                self.assertFalse(probe.numa_facts()["complete"])

    def test_cpu_list_bounds_and_ambiguous_inputs(self):
        self.assertEqual({0, 1, 2, 7}, probe._cpu_list("0-2,7"))
        self.assertEqual(set(), probe._cpu_list(""))
        for value in (None, "0,0", "0-2,2", "2-0", "-1", "0-99999999", "1,", " 1", "1-2:3"):
            with self.assertRaises(ValueError):
                probe._cpu_list(value)


if __name__ == "__main__":
    unittest.main()
