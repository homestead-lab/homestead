"""The node probe says which sensor its hottest reading came from, and the
host's CPU model, so pages can show "max 71° (NVMe nvme0)" and the CPU."""
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "probe"))
import probe


class SensorTests(unittest.TestCase):
    def test_sensors_are_named_as_people_would(self):
        cases = {("coretemp", "Package id 0", ""): "CPU package", ("coretemp", "Core 3", ""): "CPU Core 3",
                 ("k10temp", "Tctl", ""): "CPU package", ("nvme", "Composite", "nvme0"): "NVMe nvme0",
                 ("nvme", "Sensor 1", "nvme1"): "NVMe nvme1", ("acpitz", "", ""): "Motherboard",
                 ("amdgpu", "edge", ""): "GPU edge", ("mystery", "temp1", ""): "mystery"}
        for args, label in cases.items():
            self.assertEqual(label, probe.sensor_label(*args), args)

    def test_the_hottest_reading_names_its_sensor(self):
        quiet = {name: mock.patch.object(probe, name, return_value={} if name in ("devices", "numa_facts", "nfs_facts") else [])
                 for name in ("devices", "disk_activity", "mounts", "interfaces", "numa_facts", "nfs_facts")}
        with mock.patch.object(probe, "thermal", return_value=[{"name": "acpitz", "zone": "thermal_zone0", "celsius": 40.0}]),              mock.patch.object(probe, "hwmon", return_value=[
                 {"chip": "coretemp", "name": "Package id 0", "celsius": 55.0, "device": ""},
                 {"chip": "nvme", "name": "Composite", "celsius": 71.5, "device": "nvme0"}]),              mock.patch.object(probe, "cpu_model", return_value="Intel(R) Core(TM) i5-12500"),              mock.patch.object(probe, "uptime", return_value=1),              mock.patch.object(probe, "default_interface", return_value=""):
            for patch in quiet.values():
                patch.start()
            try:
                payload = probe.payload()
            finally:
                for patch in quiet.values():
                    patch.stop()
        self.assertEqual((71.5, "NVMe nvme0", "Intel(R) Core(TM) i5-12500"),
                         (payload["max_c"], payload["max_source"], payload["cpu_model"]))
        self.assertEqual(55.0, payload["cpu_c"], "the headline CPU number is unchanged")

    def test_cpu_model_on_x86_and_arm(self):
        for text, model in (("processor\t: 0\nmodel name\t: Intel(R) Core(TM)  i5-12500\n", "Intel(R) Core(TM) i5-12500"),
                            ("processor\t: 0\nBogoMIPS\t: 108.00\nModel\t\t: Raspberry Pi 5 Model B Rev 1.0\n", "Raspberry Pi 5 Model B Rev 1.0"),
                            ("", "")):
            with mock.patch.object(probe, "_read", return_value=text):
                self.assertEqual(model, probe.cpu_model())


if __name__ == "__main__":
    unittest.main()
