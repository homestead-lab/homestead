import base64
import json
import sys
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_proxmox_vms as PVE
import homestead_unraid_vms as UVMS
from rebind import keep_bound

# A Windows 11 VM as Proxmox's `qm config` prints it: OVMF with enrolled keys,
# a TPM, two disks on LVM-thin, the VirtIO driver ISO, a GPU and a detached disk.
WIN11 = """bios: ovmf
boot: order=scsi0;ide2;net0
cores: 4
cpu: host
efidisk0: local-lvm:vm-100-disk-0,efitype=4m,pre-enrolled-keys=1,size=4M
hostpci0: 0000:01:00,pcie=1,x-vga=1
ide2: local:iso/virtio-win-0.1.262.iso,media=cdrom,size=708140K
machine: pc-q35-8.1
memory: 8192
meta: creation-qemu=8.1.5,ctime=1712345678
name: win11-gaming
net0: virtio=BC:24:11:3A:1C:07,bridge=vmbr0,firewall=1,tag=20
numa: 0
ostype: win11
scsi0: local-lvm:vm-100-disk-1,iothread=1,size=128G,ssd=1
scsi1: local-lvm:vm-100-disk-3,size=500G
scsihw: virtio-scsi-single
smbios1: uuid=0f5c2a3e-1d4b-4c8e-9a7f-2b6d8e1f3c5a
sockets: 1
tpmstate0: local-lvm:vm-100-disk-2,size=4M,version=v2.0
unused0: local-lvm:vm-100-disk-4
vmgenid: 6a1c2d3e-0000-4000-8000-1234567890ab"""

# A Linux VM on directory storage (qcow2), with a second disk on Ceph, two
# cards and the older boot form. No name, no machine: Proxmox's defaults.
LINUX = """#Home automation
#second line of the description
bootdisk: virtio0
cores: 2
ide2: none,media=cdrom
memory: current=4096
net0: e1000=BC:24:11:AA:BB:01,bridge=vmbr0
net1: virtio=BC:24:11:AA:BB:02,bridge=vmbr1
ostype: l26
sockets: 2
virtio0: local:101/vm-101-disk-0.qcow2,size=32G
virtio1: ceph-pool:vm-101-disk-1,size=64G

[before-upgrade]
cores: 1
memory: 2048"""

GIB = 1024 ** 3
FACTS = {
    "local-lvm:vm-100-disk-1": {"path": "/dev/pve/vm-100-disk-1", "format": "raw", "virtual": 128 * GIB, "size": 128 * GIB, "used": 128 * GIB},
    "local-lvm:vm-100-disk-3": {"path": "/dev/pve/vm-100-disk-3", "format": "raw", "virtual": 500 * GIB, "size": 500 * GIB, "used": 500 * GIB},
    "local:101/vm-101-disk-0.qcow2": {"path": "/var/lib/vz/images/101/vm-101-disk-0.qcow2", "format": "qcow2",
                                      "virtual": 32 * GIB, "size": 9 * GIB, "used": 9 * GIB},
    "ceph-pool:vm-101-disk-1": {"path": "", "format": "", "virtual": 0, "size": 0, "used": 0},
}


def b64(text):
    return base64.b64encode(str(text).encode()).decode()


def listing_lines(vms, disks):
    """What LIST_SCRIPT prints, for these VMs and disks."""
    lines = [f"VM {vmid} {b64(state)} {b64(config)}" for vmid, state, config in vms]
    for volid, f in disks.items():
        info = json.dumps({"format": f["format"], "virtual-size": f["virtual"]}) if f["path"] else ""
        lines.append(f"DISK 100 {b64(volid)} {b64(f['path'])} {f['size']} {f['used']} {b64(info)}")
    return lines + ["END"]


class ConfigTests(unittest.TestCase):
    def test_config_is_read_as_keys_with_the_description_and_without_snapshots(self):
        cfg = PVE.parse_config(LINUX)
        self.assertEqual("Home automation second line of the description", cfg["description"])
        self.assertEqual("current=4096", cfg["memory"])
        self.assertEqual("2", cfg["cores"], "a snapshot's settings are not the VM's own")

    def test_a_disk_value_splits_into_its_volume_and_options(self):
        self.assertEqual(("local-lvm:vm-100-disk-1", {"iothread": "1", "size": "128G", "ssd": "1"}),
                         PVE.options("local-lvm:vm-100-disk-1,iothread=1,size=128G,ssd=1"))


class PlanTests(unittest.TestCase):
    def win(self, state="stopped"):
        return PVE.plan("100", state, PVE.parse_config(WIN11), FACTS)

    def test_a_windows_vm_maps_across_with_what_it_needs_to_boot(self):
        vm = self.win()
        self.assertEqual(("100", 100, "win11-gaming", "win11-gaming"), (vm["id"], vm["vmid"], vm["name"], vm["slug"]))
        self.assertEqual((4, "8Gi", "uefi", True, True, True), (vm["cores"], vm["memory"], vm["firmware"], vm["secure_boot"], vm["tpm"], vm["hyperv"]))
        self.assertEqual("host-passthrough", vm["cpu_model"])
        self.assertEqual("Windows 11", vm["os"])
        self.assertTrue(vm["shut_off"] and vm["ready"], vm["problem"])
        self.assertEqual(["scsi0", "scsi1"], [d["key"] for d in vm["disks"]], "the boot disk first, as the boot order says")
        self.assertEqual([0, 1], [d["index"] for d in vm["disks"]])
        self.assertEqual(("/dev/pve/vm-100-disk-1", "scsi", "raw", 128), (vm["disks"][0]["path"], vm["disks"][0]["bus"],
                                                                         vm["disks"][0]["format"], vm["disks"][0]["size_gb"]))
        self.assertEqual({"mac": "bc:24:11:3a:1c:07", "bridge": "vmbr0", "model": "virtio", "nic_model": "virtio"}, vm["nic"])

    def test_what_cannot_come_across_says_why(self):
        vm = self.win()
        whats = {d["what"]: d for d in vm["dropped"]}
        self.assertIn("virtio-win-0.1.262.iso", whats["CD-ROM"]["detail"])
        self.assertTrue(whats["GPU or PCI device"]["hardware"])
        self.assertEqual("local-lvm:vm-100-disk-4", whats["Unused disk"]["detail"])
        notes = " ".join(vm["notes"])
        self.assertIn("EFI variables", notes)
        self.assertIn("TPM", notes)
        self.assertIn("VLAN 20", notes)
        self.assertNotIn("q35, the only", notes, "already q35")

    def test_a_running_vm_is_listed_but_not_shut_off(self):
        vm = self.win("running")
        self.assertFalse(vm["shut_off"])
        self.assertTrue(vm["ready"], "ready to copy once it is shut down")

    def test_a_linux_vm_on_qcow2_with_a_ceph_disk_comes_without_that_disk(self):
        vm = PVE.plan("101", "stopped", PVE.parse_config(LINUX), FACTS)
        self.assertEqual(("vm-101", 4, "4Gi", "bios", False), (vm["name"], vm["cores"], vm["memory"], vm["firmware"], vm["hyperv"]))
        self.assertEqual("Linux", vm["os"])
        self.assertTrue(vm["ready"], vm["problem"])
        self.assertEqual(["virtio0"], [d["key"] for d in vm["disks"]])
        self.assertEqual(("qcow2", "virtio", 9 * GIB), (vm["disks"][0]["format"], vm["disks"][0]["bus"], vm["disks"][0]["size"]))
        ceph = next(d for d in vm["dropped"] if d["detail"] == "ceph-pool:vm-101-disk-1")
        self.assertIn("Ceph", ceph["reason"])
        self.assertTrue(any(d["what"] == "Second network card" for d in vm["dropped"]))
        self.assertFalse(any(d["what"] == "CD-ROM" for d in vm["dropped"]), "an empty drive is nothing to leave behind")
        self.assertEqual(("e1000", "e1000"), (vm["nic"]["model"], vm["nic"]["nic_model"]))
        self.assertIn("i440fx, Proxmox's default, becomes q35", " ".join(vm["notes"]))

    def test_a_vm_whose_boot_disk_cannot_be_read_is_not_ready_and_says_why(self):
        cfg = PVE.parse_config("scsi0: ceph-pool:vm-102-disk-0,size=32G\nmemory: 2048\nostype: l26")
        vm = PVE.plan("102", "stopped", cfg, FACTS)
        self.assertFalse(vm["ready"])
        self.assertIn("its boot disk", vm["problem"])

    def test_a_vmdk_disk_is_refused_with_how_to_convert_it(self):
        facts = {"local:103/vm-103-disk-0.vmdk": {"path": "/var/lib/vz/images/103/vm-103-disk-0.vmdk", "format": "vmdk",
                                                 "virtual": 20 * GIB, "size": GIB, "used": GIB}}
        vm = PVE.plan("103", "stopped", PVE.parse_config("sata0: local:103/vm-103-disk-0.vmdk,size=20G"), facts)
        self.assertFalse(vm["ready"])
        self.assertIn("qm disk move", vm["problem"])

    def test_a_physical_disk_passed_through_is_left_behind(self):
        cfg = PVE.parse_config("scsi0: local-lvm:vm-100-disk-1,size=128G\nsata1: /dev/disk/by-id/ata-WDC_WD40EFRX,size=4T")
        vm = PVE.plan("100", "stopped", cfg, FACTS)
        self.assertEqual(["scsi0"], [d["key"] for d in vm["disks"]])
        self.assertTrue(any("physical disk" in d["reason"] for d in vm["dropped"]))


class ListingTests(unittest.TestCase):
    def test_the_listing_is_parsed_into_mapped_vms(self):
        lines = listing_lines([("100", "stopped", WIN11), ("101", "running", LINUX)], FACTS)
        found = PVE.listing(lines)
        self.assertTrue(found["hypervisor"])
        self.assertEqual(["vm-101", "win11-gaming"], [v["name"] for v in found["vms"]])

    def test_a_host_without_qm_runs_no_vms(self):
        self.assertEqual({"hypervisor": False, "vms": []}, PVE.listing(["NOQM"]))

    def test_a_listing_cut_short_is_not_taken_as_the_whole(self):
        with self.assertRaises(ValueError):
            PVE.listing(listing_lines([("100", "stopped", WIN11)], FACTS)[:-1])

    def test_the_listing_script_reads_paths_and_never_the_disks_themselves(self):
        self.assertIn("pvesm path", PVE.LIST_SCRIPT)
        self.assertIn("qemu-img info -U", PVE.LIST_SCRIPT)
        self.assertNotIn("dd ", PVE.LIST_SCRIPT)


class ImportTests(unittest.TestCase):
    """The shared import (homestead_unraid_vms) with a Proxmox source."""
    SRC = {"name": "pve", "kind": "proxmox", "host": "192.0.2.10", "user": "root", "port": 22, "ssh_trust": {"key": "x"}}

    def setUp(self):
        self.imp = mock.Mock()
        self.imp._source.return_value = dict(self.SRC)
        self.imp._ssh_script.side_effect = lambda src, cmd: f"ssh {src['host']} {cmd!r}"
        self.imp.run_probe.return_value = listing_lines([("100", "stopped", WIN11)], FACTS)
        self.imp.SOURCE_SSH.setup.return_value = "# ssh setup\n"
        self.imp.SOURCE_SSH.command.side_effect = lambda src, cmd, compress=False: f"ssh {src['host']} {cmd!r}"
        keep_bound(self, UVMS)   # put back what UVMS.bind() sets
        UVMS.bind(self.imp, mock.Mock(), mock.Mock(), mock.Mock())

    def test_a_proxmox_source_lists_its_vms(self):
        found = UVMS.listing("pve")
        self.assertEqual(("proxmox", True), (found["kind"], found["virsh"]))
        self.assertEqual(["100"], [v["id"] for v in found["vms"]])
        self.assertIn("qm config", self.imp._ssh_script.call_args[0][1])

    def test_a_vm_is_picked_by_its_vmid_not_its_name(self):
        self.assertEqual("win11-gaming", UVMS._one("pve", "100")["name"])
        with self.assertRaises(ValueError):
            UVMS._one("pve", "win11-gaming")

    def test_shut_down_presses_the_power_button_by_vmid(self):
        self.imp.run_probe.side_effect = [listing_lines([("100", "running", WIN11)], FACTS), ["ok"]]
        message = UVMS.shutdown("pve", "100")["message"]
        self.assertIn("qm shutdown 100", self.imp._ssh_script.call_args[0][1])
        self.assertIn("win11-gaming", message)

    def test_the_copy_checks_qm_status_and_reads_the_volume(self):
        script = UVMS.copy_script(self.SRC, "100", "/dev/pve/vm-100-disk-1", 128 * GIB)
        self.assertIn("qm status 100", script)
        self.assertIn("'stopped'", script)
        self.assertIn("on Proxmox again", script)
        self.assertIn("cat -- /dev/pve/vm-100-disk-1", script)
        self.assertNotIn("virsh", script)


if __name__ == "__main__":
    unittest.main()


class WrongTypeTests(unittest.TestCase):
    """A Proxmox host added as Unraid: libvirt isn't there, but qm is."""
    def test_the_unraid_listing_notices_qm_and_says_proxmox(self):
        imp = mock.Mock()
        imp._source.return_value = {"name": "pve", "kind": "unraid", "host": "192.0.2.10"}
        imp._ssh_script.side_effect = lambda src, cmd: cmd
        imp.run_probe.return_value = ["NOVIRSH", "HASQM"]
        keep_bound(self, UVMS)   # put back what UVMS.bind() sets
        UVMS.bind(imp, mock.Mock(), mock.Mock(), mock.Mock())
        found = UVMS.listing("pve")
        self.assertEqual((False, True), (found["virsh"], found["proxmox"]))
        self.assertIn("command -v qm", imp._ssh_script.call_args[0][1])

    def test_a_server_with_neither_is_no_proxmox(self):
        self.assertFalse(UVMS.parse_listing(["NOVIRSH"])["qm"])


class VolumeSizeTests(unittest.TestCase):
    def test_a_volume_has_room_for_its_filesystem_as_well_as_the_disk(self):
        import homestead_unraid_vms as UVMS
        # The import that failed: a 1 GiB disk on a volume of exactly 1 GiB.
        self.assertEqual("1280Mi", UVMS.volume_request({"virtual": 1024 ** 3, "size_gb": 1}))
        self.assertEqual("108544Mi", UVMS.volume_request({"virtual": 100 * 1024 ** 3, "size_gb": 100}))
        self.assertEqual("1280Mi", UVMS.volume_request({"size_gb": 1}))


class RoomTests(unittest.TestCase):
    """The room check with a Proxmox source: 128 GiB and 500 GiB disks, asked
    for as 135.7 and 530 GiB with their filesystem's room."""

    def setUp(self):
        self.imp = mock.Mock()
        self.imp.NS = "lab"
        self.imp._source.return_value = dict(ImportTests.SRC)
        self.imp._ssh_script.side_effect = lambda src, cmd: f"ssh {src['host']} {cmd!r}"
        self.imp.run_probe.return_value = listing_lines([("100", "stopped", WIN11)], FACTS)
        self.imp._required_name.side_effect = lambda value, label="name": str(value)
        self.imp._get_or_none.return_value = None
        self.imp.vm_disk_import_plan.return_value = {"ready": True, "message": ""}
        self.kget, self.ksend, self.ops = mock.Mock(), mock.Mock(), mock.Mock()
        self.kget.return_value = {"items": [{"metadata": {"namespace": "cdi"}}]}
        self.rooms = [400, 400, 400]
        cap = lambda: {"nodes": [{"name": f"node{i + 1}", "tags": [], "disks": [
            {"id": "d", "room_gb": room, "physical_room_gb": room, "blocked": "", "tags": []}]}
            for i, room in enumerate(self.rooms)]}
        classes = lambda: [{"name": "longhorn-r3", "provisioner": "driver.longhorn.io", "replicas": "3",
                            "disk_tags": [], "node_tags": []}]
        keep_bound(self, UVMS)   # put back what UVMS.bind() sets
        UVMS.bind(self.imp, self.kget, self.ksend, self.ops, storage=(classes, cap, lambda: "longhorn-r3"))
        self.addCleanup(UVMS.bind, self.imp, self.kget, self.ksend, self.ops, storage=(None, None, lambda: ""))

    def test_both_disks_need_more_than_three_copies_leave_room_for(self):
        found = UVMS._one("pve", "100")
        room = UVMS.storage_room(UVMS._chosen_disks(found, []), "longhorn-r3")
        self.assertEqual("bad", room["level"])
        self.assertEqual(["Not enough room on longhorn-r3: these disks need 665.7 GiB (1997 GiB with its 3 copies), "
                          "and it can still place 400 GiB. Free some space, add a disk, or leave a disk out"], room["blockers"])

    def test_the_import_is_refused_before_any_disk_is_made(self):
        with self.assertRaises(ValueError) as caught:
            UVMS.start({"source": "pve", "vm": "100", "name": "win11"})
        self.assertIn("these disks need 665.7 GiB", str(caught.exception))
        self.ksend.assert_not_called()
        self.ops.start.assert_not_called()

    def test_the_boot_disk_alone_fits_and_a_tight_fit_warns(self):
        found = UVMS._one("pve", "100")
        alone = UVMS._chosen_disks(found, [1])
        self.assertEqual(("ok", 135.7), (UVMS.storage_room(alone, "")["level"], UVMS.storage_room(alone, "")["needed_gb"]))
        self.rooms = [140, 140, 140]
        room = UVMS.storage_room(alone, "")
        self.assertEqual(("warn", 140.0), (room["level"], room["free_gb"]))
        self.assertIn("leaving 4.3 GiB (3%)", room["warnings"][0])


if __name__ == "__main__":
    unittest.main()


class StuckImportTests(unittest.TestCase):
    """An import paused by a safety check can still be cancelled."""
    def setUp(self):
        import homestead_operations as OPS
        import homestead_unraid_vms as UVMS
        self.OPS, self.UVMS = OPS, UVMS
        self.objects = {
            "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/test-disk": {"metadata": {"name": "test-disk", "uid": "u1", "resourceVersion": "1", "annotations": {"homestead.io/cdi-import-id": "w1"}}},
            "/apis/batch/v1/namespaces/lab/jobs/homestead-vmimport-test-w1-1": {"metadata": {"name": "j", "uid": "u2", "resourceVersion": "1", "annotations": {"homestead.io/cdi-import-id": "w1"}}},
            "/api/v1/namespaces/lab/secrets/homestead-vmimport-test-w1-1-upload": {"metadata": {"name": "s", "uid": "u3", "resourceVersion": "1", "annotations": {"homestead.io/cdi-import-id": "other"}}},
            # Cancel reads who uses the disks first (#1312); the namespace has no pods.
            "/api/v1/namespaces/lab/pods": {"metadata": {}, "items": []},
        }
        self.deleted = []

        def kget(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "gone", {}, None)
            return self.objects[path]

        def ksend(method, path, body=None, **_):
            self.deleted.append(path)
            self.objects.pop(path, None)
            return {}
        keep_bound(self, UVMS)   # put back what UVMS.bind() sets
        UVMS.bind(mock.Mock(), kget, ksend, OPS)
        UVMS.register(OPS)
        self.item = {"id": "op1", "kind": UVMS.KIND, "status": "running", "message": "Import paused: Import claim was replaced; its resources were preserved",
                     "ref": {"namespace": "lab", "kind": "proxmox", "source": "pve", "vm": "100", "vm_name": "test", "phase": "copy",
                             "cdi_cleanup": {"id": "w1", "disks": {}},
                             "disks": [{"dv": "test-disk", "job": "homestead-vmimport-test-w1-1"}]}}

    def test_it_is_cancellable_and_removes_only_what_carries_its_stamp(self):
        self.assertTrue(self.OPS._public(self.item)["cancellable"])
        plan = self.OPS._plan_for(self.item)
        self.assertTrue(plan["can"])
        self.assertIn("test-disk", " ".join(plan["undo"]))
        message = self.UVMS.cancel_run(self.item, {})
        self.assertEqual(["/apis/batch/v1/namespaces/lab/jobs/homestead-vmimport-test-w1-1",
                          "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/test-disk"], self.deleted)
        self.assertIn("belong to another import", message)
        self.assertEqual("failed", self.item["ref"]["phase"])

    def test_a_disk_a_vm_uses_is_kept(self):
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines"] = {"metadata": {}, "items": [
            {"metadata": {"name": "win11"}, "spec": {"template": {"spec": {"volumes": [{"dataVolume": {"name": "test-disk"}}]}}}}]}
        message = self.UVMS.cancel_run(self.item, {})
        self.assertEqual(["/apis/batch/v1/namespaces/lab/jobs/homestead-vmimport-test-w1-1"], self.deleted)
        self.assertIn("VM win11 uses test-disk, so it is kept.", message)

    def test_a_url_disk_import_is_cancellable_too(self):
        # #1338: a URL disk import is cancellable now; its own plan says what it removes.
        other = {**self.item, "kind": "vm-disk-import"}
        self.assertTrue(self.OPS._public(other)["cancellable"])


if __name__ == "__main__":
    unittest.main()
