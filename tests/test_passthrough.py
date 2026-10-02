import base64
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_passthrough as PASS

HOST = """CMDLINE BOOT_IMAGE=/vmlinuz root=/dev/mapper/vg-root ro intel_iommu=on iommu=pt
GROUPS 18
CPU GenuineIntel
PCI 0000:00:00.0|0x8086|0x3e30|0x060000|skl_uncore|0|0|
PCI 0000:01:00.0|0x10de|0x1e87|0x030000|nouveau|12|1|
PCI 0000:01:00.1|0x10de|0x10f8|0x040300|snd_hda_intel|12|0|
PCI 0000:00:01.0|0x8086|0x1901|0x060400|pcieport|12|0|
PCI 0000:03:00.0|0x8086|0x1533|0x020000|igb|15|0|enp3s0
PCI 0000:04:00.0|0x144d|0xa808|0x010802|nvme|16|0|
NAME 0000:01:00.0 "VGA compatible controller" "NVIDIA Corporation" "TU104 [GeForce RTX 2080]" -ra1 "" ""
USB 0bda|8153|00|Realtek|USB 10/100/1000 LAN|2-1
USB 1d6b|0003|09|Linux Foundation|3.0 root hub|usb2
USB 1a6e|089a|00||Coral|1-4
DEFAULT enp3s0
MOUNTED /sys/devices/pci0000:00/0000:00:1d.0/0000:04:00.0/nvme/nvme0/nvme0n1
END
"""


class Host:
    def __init__(self, out=HOST, action_out="OK\n"):
        self.out, self.action_out, self.scripts = out, action_out, []

    def run(self, node, script, timeout=60):
        self.scripts.append(script)
        return (self.out if script.startswith('echo "CMDLINE') else self.action_out), ""


class Cluster:
    def __init__(self, kv=None, harvester=False):
        self.kv = kv or {"metadata": {"name": "kubevirt", "namespace": "kubevirt"},
                         "spec": {"configuration": {"developerConfiguration": {"featureGates": []}}}}
        self.harvester, self.sent, self.objects = harvester, [], {}

    def kget(self, path):
        if path == PASS.KUBEVIRTS:
            return {"items": [self.kv]}
        if path == "/api/v1/nodes":
            return {"items": [{"metadata": {"name": "node-1"},
                               "status": {"allocatable": {"homestead.io/pci-10de-1e87": "1", "cpu": "8"}}}]}
        if path in self.objects:
            return self.objects[path]
        raise KeyError(path)

    def ksend(self, method, path, body=None, ctype=""):
        self.sent.append((method, path, body))
        if path.endswith("/kubevirts/kubevirt") and method == "PATCH":
            conf = self.kv["spec"]["configuration"]
            patch = body["spec"]["configuration"]
            if "permittedHostDevices" in patch:
                conf["permittedHostDevices"] = patch["permittedHostDevices"]
            if "developerConfiguration" in patch:
                conf["developerConfiguration"].update(patch["developerConfiguration"])


class PassthroughTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.cluster, self.host = Cluster(), Host()
        self.bind()

    def bind(self, harvester=False):
        PASS.bind(self.cluster.kget, self.cluster.ksend, self.host,
                  lambda force=False: {"harvester": harvester, "distribution": "rke2" if harvester else "k3s"}, self.data)

    def test_a_host_is_read_with_its_iommu_groups_and_what_it_cannot_give_away(self):
        facts = PASS.inspect("node-1")
        self.assertTrue(facts["iommu"] and facts["cmdline_iommu"])
        rows = {r["address"]: r for r in facts["pci"]}
        gpu = rows["0000:01:00.0"]
        self.assertEqual(("NVIDIA Corporation TU104 [GeForce RTX 2080]", "nouveau", True, True),
                         (gpu["name"], gpu["driver"], gpu["boot_vga"], gpu["offered"]))
        self.assertEqual(["0000:01:00.1", "0000:00:01.0"], gpu["group_members"])
        self.assertIn("carries this host's network", rows["0000:03:00.0"]["problems"][0])
        self.assertIn("in use", rows["0000:04:00.0"]["problems"][0])
        self.assertFalse(rows["0000:00:00.0"]["offered"], "the chipset is not offered")
        self.assertEqual({"0bda:8153", "1a6e:089a"}, {f"{u['vendor']}:{u['product']}" for u in facts["usb"]},
                         "root hubs are left out")

    def test_a_gpu_goes_to_vfio_with_its_group_but_not_the_bridge_and_kubevirt_is_told(self):
        result = PASS.give("node-1", "0000:01:00.0")
        self.assertEqual(["0000:01:00.0", "0000:01:00.1"], result["moved"])
        script = self.host.scripts[-1]
        for part in ("modprobe vfio-pci", "driver_override", "drivers_probe", "homestead-vfio.service",
                     "systemctl enable", "/etc/homestead/vfio.list"):
            self.assertIn(part, script)
        self.assertNotIn("0000:00:01.0", script, "the PCI bridge stays with the host")
        permitted = self.cluster.kv["spec"]["configuration"]["permittedHostDevices"]["pciHostDevices"]
        self.assertEqual({("10DE:1E87", "homestead.io/pci-10de-1e87"), ("10DE:10F8", "homestead.io/pci-10de-10f8")},
                         {(r["pciVendorSelector"], r["resourceName"]) for r in permitted})
        self.assertIn("HostDevices", self.cluster.kv["spec"]["configuration"]["developerConfiguration"]["featureGates"])

    def test_the_hosts_network_and_system_disk_are_refused(self):
        for address, words in (("0000:03:00.0", "network"), ("0000:04:00.0", "in use")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                PASS.give("node-1", address)
        self.assertFalse(any("driver_override" in s for s in self.host.scripts), "nothing was unbound")

    def test_nothing_is_handed_over_without_iommu(self):
        self.host.out = HOST.replace("GROUPS 18", "GROUPS 0")
        with self.assertRaisesRegex(ValueError, "IOMMU is off"):
            PASS.give("node-1", "0000:01:00.0")

    def test_iommu_goes_in_grub_for_the_next_restart(self):
        self.host.out = HOST.replace("GROUPS 18", "GROUPS 0")
        self.host.action_out = "OK intel_iommu=on iommu=pt\n"
        result = PASS.enable_iommu("node-1")
        self.assertTrue(result["restart_needed"])
        script = self.host.scripts[-1]
        self.assertIn("intel_iommu=on iommu=pt", script)
        self.assertIn("grub.homestead-backup", script)
        self.assertIn("update-grub", script)

    def test_a_device_given_back_is_no_longer_offered(self):
        PASS.give("node-1", "0000:01:00.0")
        PASS.take_back("node-1", "0000:01:00.0")
        script = self.host.scripts[-1]
        self.assertIn('echo > "$d/driver_override"', script)
        self.assertEqual([], self.cluster.kv["spec"]["configuration"]["permittedHostDevices"]["pciHostDevices"])

    def test_a_device_no_longer_on_the_host_is_still_taken_off_the_offer(self):
        PASS.give("node-1", "0000:01:00.0")
        # The card is pulled from the host before it is given back.
        self.host.out = "\n".join(line for line in HOST.splitlines() if "0000:01:00" not in line) + "\n"
        PASS.take_back("node-1", "0000:01:00.0")
        self.assertEqual([], self.cluster.kv["spec"]["configuration"]["permittedHostDevices"]["pciHostDevices"])

    def test_usb_is_offered_by_vendor_and_product(self):
        PASS.allow_usb("1a6e", "089a")
        usb = self.cluster.kv["spec"]["configuration"]["permittedHostDevices"]["usb"]
        self.assertEqual([{"resourceName": "homestead.io/usb-1a6e-089a", "selectors": [{"vendor": "1a6e", "product": "089a"}]}], usb)
        with self.assertRaisesRegex(ValueError, "vendor and product"):
            PASS.allow_usb("zz", "1")

    def test_the_scripts_are_valid_shell(self):
        for script in (PASS.INSPECT, PASS.vfio_script(["0000:01:00.0"], True), PASS.vfio_script(["0000:01:00.0"], False),
                       PASS.iommu_script("intel"), PASS.BOOT_SCRIPT):
            try:
                result = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
            except FileNotFoundError:
                self.skipTest("no sh here")
            self.assertEqual(0, result.returncode, result.stderr)


ROM = b"\x55\xaa" + b"\x00" * 4094


class VmTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster({"metadata": {"name": "kubevirt", "namespace": "kubevirt"},
                                "spec": {"configuration": {"developerConfiguration": {"featureGates": []},
                                                           "permittedHostDevices": {"pciHostDevices": [
                                                               {"pciVendorSelector": "10DE:1E87", "resourceName": "homestead.io/pci-10de-1e87"}]}}}})
        PASS.bind(self.cluster.kget, self.cluster.ksend, Host(), lambda force=False: {"distribution": "k3s"}, tempfile.mkdtemp())

    def vm(self):
        return {"metadata": {"name": "gaming"}, "spec": {"template": {"metadata": {}, "spec": {"domain": {"devices": {}}}}}}

    def test_a_device_vms_may_use_is_added_and_one_they_may_not_is_refused(self):
        vm, effects = self.vm(), []
        self.assertTrue(PASS.edit_vm(vm, "lab", {"add": [{"resource": "homestead.io/pci-10de-1e87"}]}, effects))
        self.assertEqual([{"name": "hostdev-0", "deviceName": "homestead.io/pci-10de-1e87"}],
                         vm["spec"]["template"]["spec"]["domain"]["devices"]["hostDevices"])
        with self.assertRaisesRegex(ValueError, "hand it over from its host"):
            PASS.edit_vm(self.vm(), "lab", {"add": [{"resource": "homestead.io/pci-dead-beef"}]}, [])

    def test_a_rom_rides_in_a_hook_script_of_the_vms_own(self):
        vm, effects = self.vm(), []
        PASS.edit_vm(vm, "lab", {"add": [{"resource": "homestead.io/pci-10de-1e87"}],
                                 "roms": {"hostdev-0": base64.b64encode(ROM).decode()}}, effects)
        annotations = vm["spec"]["template"]["metadata"]["annotations"]
        hook = json.loads(annotations[PASS.HOOK_ANNOTATION])
        self.assertEqual([{"args": ["--version", "v1alpha2"], "configMap": {"name": "gaming-vbios", "key": "vbios.py",
                                                                           "hookPath": "/usr/bin/onDefineDomain"}}], hook)
        configmap = next(e for e in effects if e["kind"] == "configmap")
        self.assertIn("gaming-vbios", configmap["path"])
        self.assertIn({"kind": "kubevirt-gates", "gates": ["Sidecar"]}, effects)
        # Saved, the ROM is read back from its ConfigMap for the next edit.
        self.cluster.objects[configmap["path"]] = configmap["body"]
        self.assertEqual({"hostdev-0": ROM}, PASS.current_roms(vm, "lab"))

    def test_the_hook_names_the_rom_on_the_right_device_and_never_breaks_the_vm(self):
        script = PASS.hook_script({"hostdev-0": ROM})
        domain = ("<domain><devices><hostdev type='pci'><alias name='ua-hostdevice-hostdev-0'/></hostdev>"
                  "<hostdev type='pci'><alias name='ua-hostdevice-hostdev-1'/></hostdev></devices></domain>")
        with tempfile.TemporaryDirectory() as hooks:
            code = script.replace('HOOKS = "/var/run/kubevirt-hooks"', f"HOOKS = {json.dumps(hooks)}")
            out = subprocess.run([sys.executable, "-c", code, "--vmi", "{}", "--domain", domain],
                                 capture_output=True, text=True)
            self.assertIn(f"<rom bar=\"on\" file=\"{hooks}", out.stdout.replace("\\\\", "\\"))
            self.assertEqual(1, out.stdout.count("<rom"), "only the device the ROM is for")
            self.assertEqual(ROM, Path(hooks, "vbios-hostdev-0.rom").read_bytes())
        broken = subprocess.run([sys.executable, "-c", script, "--vmi", "{}", "--domain", "<not xml"],
                                capture_output=True, text=True)
        self.assertEqual("<not xml", broken.stdout, "the domain goes on unchanged")

    def test_new_devices_have_stable_names_and_existing_devices_can_be_remapped(self):
        vm = self.vm()
        resource = "homestead.io/pci-10de-1e87"
        PASS.edit_vm(vm, "lab", {"add": [{"name": "gpu-main", "resource": resource}],
                                "roms": {"gpu-main": base64.b64encode(ROM).decode()}}, [])
        self.assertEqual("gpu-main", PASS.vm_devices(vm)[0]["name"])
        with mock.patch.object(PASS, "resources", return_value={"resources": [{"resource": "example.test/gpu-new"}]}):
            PASS.edit_vm(vm, "lab", {"map": {"gpu-main": "example.test/gpu-new"}}, [], {"gpu-main": ROM})
        self.assertEqual("example.test/gpu-new", PASS.vm_devices(vm)[0]["resource"])
        self.assertTrue(PASS.vm_devices(vm)[0]["rom"])
        with self.assertRaisesRegex(ValueError, "unique valid name"):
            PASS.edit_vm(vm, "lab", {"add": [{"name": "gpu-main", "resource": resource}]}, [])

    def test_missing_managed_rom_is_not_silently_lost(self):
        vm = self.vm()
        PASS.edit_vm(vm, "lab", {"add": [{"resource": "homestead.io/pci-10de-1e87"}],
                                "roms": {"hostdev-0": base64.b64encode(ROM).decode()}}, [])
        with self.assertRaisesRegex(ValueError, "vBIOS ConfigMap is missing or incomplete"):
            PASS.current_roms(vm, "lab")

    def test_combined_rom_limit_is_checked_before_a_configmap_is_written(self):
        rom = base64.b64encode(b"\x55\xaa" + b"\x00" * (400 * 1024)).decode()
        resource = "homestead.io/pci-10de-1e87"
        with self.assertRaisesRegex(ValueError, "combined vBIOS"):
            PASS.edit_vm(self.vm(), "lab", {"add": [{"resource": resource}, {"resource": resource}],
                "roms": {"hostdev-0": rom, "hostdev-1": rom}}, [])

    def test_roms_are_checked(self):
        for raw, words in ((b"MZ" + b"\x00" * 10, "55 AA"), (b"\x55\xaa" + b"\x00" * (PASS.ROM_LIMIT + 1), "fits")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                PASS.check_rom(base64.b64encode(raw).decode())

    def test_removing_the_device_removes_its_rom_and_the_hook(self):
        vm, effects = self.vm(), []
        PASS.edit_vm(vm, "lab", {"add": [{"resource": "homestead.io/pci-10de-1e87"}],
                                 "roms": {"hostdev-0": base64.b64encode(ROM).decode()}}, effects)
        effects = []
        PASS.edit_vm(vm, "lab", {"remove": ["hostdev-0"]}, effects, {"hostdev-0": ROM})
        self.assertNotIn("hostDevices", vm["spec"]["template"]["spec"]["domain"]["devices"])
        self.assertNotIn("annotations", vm["spec"]["template"]["metadata"])
        self.assertEqual({"kind": "configmap", "path": "/api/v1/namespaces/lab/configmaps/gaming-vbios", "body": None},
                         next(e for e in effects if e["kind"] == "configmap"))


if __name__ == "__main__":
    unittest.main()
