import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_vm_hardware as HW
import homestead_isos as ISOS
import homestead_vms as vms
import homestead_imports as imports
import test_vm_edit as edit_fixtures


def vm(domain=None, tspec=None):
    return {"metadata": {"name": "win", "namespace": "lab"},
            "spec": {"template": {"spec": {"domain": domain or {"cpu": {"cores": 2}, "devices": {}}, **(tspec or {})}}}}


class HardwareTests(unittest.TestCase):
    def test_defaults_read_as_kubevirt_applies_them(self):
        h = HW.read(vm())
        self.assertEqual(({"sockets": 1, "cores": 2, "threads": 1, "model": "", "dedicated": False, "isolate_emulator": False},
                          "bios", "off", True, True, True, False),
                         (h["cpu"], h["firmware"], h["tpm"], h["graphics"], h["serial"], h["balloon"], h["tablet"]))

    def test_windows_11_settings_are_written_as_kubevirt_wants(self):
        target = vm()
        changed = HW.apply(target, {"firmware": "uefi", "secure_boot": True, "efi_persistent": True, "tpm": "persistent",
                                    "hyperv": True, "tablet": True, "timezone": "Europe/London", "machine": "q35"})
        dom = target["spec"]["template"]["spec"]["domain"]
        self.assertTrue(changed)
        self.assertEqual({"efi": {"secureBoot": True, "persistent": True}}, dom["firmware"]["bootloader"])
        self.assertEqual({"enabled": True}, dom["features"]["smm"], "Secure Boot needs SMM")
        self.assertEqual({"persistent": True}, dom["devices"]["tpm"])
        self.assertIn("spinlocks", dom["features"]["hyperv"])
        self.assertIn("hyperv", dom["clock"]["timer"])
        self.assertEqual("Europe/London", dom["clock"]["timezone"])
        self.assertEqual([{"type": "tablet", "bus": "usb", "name": "tablet"}], dom["devices"]["inputs"])
        self.assertEqual({"type": "q35"}, dom["machine"])
        back = HW.read(target)
        self.assertEqual(("uefi", True, True, "persistent", True, True), (back["firmware"], back["secure_boot"],
                         back["efi_persistent"], back["tpm"], back["hyperv"], back["tablet"]))

    def test_only_what_is_asked_changes(self):
        target = vm({"cpu": {"cores": 4, "model": "host-model"}, "devices": {"rng": {}}})
        before = copy.deepcopy(target)
        self.assertFalse(HW.apply(target, {"rng": True}))
        self.assertEqual(before, target)
        HW.apply(target, {"graphics": False, "rng": False})
        devices = target["spec"]["template"]["spec"]["domain"]["devices"]
        self.assertEqual((False, None), (devices["autoattachGraphicsDevice"], devices.get("rng")))
        self.assertEqual("host-model", target["spec"]["template"]["spec"]["domain"]["cpu"]["model"])

    def test_topology_sets_the_count_and_the_limit(self):
        target = vm({"cpu": {"cores": 2}, "resources": {"limits": {"cpu": "2"}}, "devices": {}})
        HW.apply(target, {"cpu": {"sockets": 2, "cores": 4, "threads": 2}})
        dom = target["spec"]["template"]["spec"]["domain"]
        self.assertEqual((2, 4, 2, "16"), (dom["cpu"]["sockets"], dom["cpu"]["cores"], dom["cpu"]["threads"], dom["resources"]["limits"]["cpu"]))

    def test_what_kubevirt_would_refuse_is_refused_first(self):
        for cfg, words in (({"firmware": "bios", "secure_boot": True}, "UEFI"),
                           ({"cpu": {"isolate_emulator": True}}, "dedicated"),
                           ({"cpu": {"model": "bad model!"}}, "CPU model"),
                           ({"timezone": "Europe/../etc"}, "time zone"),
                           ({"hugepages": "4Ki"}, "hugepages"),
                           ({"tpm": "maybe"}, "TPM"),
                           ({"cpu": {"cores": 0}}, "between")):
            with self.subTest(cfg=cfg), self.assertRaisesRegex(ValueError, words):
                HW.apply(vm(), cfg)
        with self.assertRaisesRegex(ValueError, "instance type"):
            HW.apply(vm(), {"cpu": {"sockets": 2}}, locked_cpu=True)

    def test_requirements_are_said(self):
        notes = " ".join(HW.requirements({"cpu": {"dedicated": True, "model": "host-passthrough"}, "hugepages": "1Gi",
                                          "tpm": "persistent", "eviction": "LiveMigrate"}, {}, nodes=1))
        for words in ("static CPU manager", "1Gi", "VMPersistentState", "second node", "same CPU"):
            self.assertIn(words, notes)

    def test_cpu_models_every_node_has(self):
        node = lambda *models: {"metadata": {"labels": {f"cpu-model.node.kubevirt.io/{m}": "true" for m in models}}}
        self.assertEqual(["Skylake"], HW.cpu_models([node("Skylake", "Haswell"), node("Skylake")]))


SHARE = {"name": "media", "pvc": "share-media", "sub_path": "", "access_modes": ["ReadWriteOnce"]}


class IsoTests(unittest.TestCase):
    def setUp(self):
        self.objects, self.sent = {}, []
        self.listing = {"isos": [{"name": "debian.iso", "kind": "file", "size": 800 * 1024 ** 2},
                                 {"name": "notes.txt", "kind": "file", "size": 10}, {"name": "old", "kind": "dir", "size": 0}]}

        def get(path):
            base = path.split("?")[0]
            if base in self.objects:
                return self.objects[base]
            if base.endswith("/persistentvolumeclaims") or base.endswith("/jobs") or base.endswith("/virtualmachines"):
                return {"items": [o for p, o in self.objects.items() if p.startswith(base + "/")]}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, **kw):
            self.sent.append((method, path, body))
            if method in ("POST", "PUT") and body:
                self.objects[path.rstrip("/") + ("" if method == "PUT" else f"/{body['metadata']['name']}")] = body
            if method == "DELETE":
                self.objects.pop(path.split("?")[0], None)
            return body

        ISOS.bind(get, send, lambda: [dict(SHARE)], lambda ns, pvc, path: {"entries": self.listing.get(path, [])},
                  lambda: ("longhorn-r2", True), lambda: "node-2", "lab", "lab", "alpine:3.20")
        ISOS.set_folders([{"share": "media", "path": "isos"}])

    def test_only_isos_in_the_chosen_folders_are_listed(self):
        files, problems = ISOS.iso_files()
        self.assertEqual(([("debian.iso", "isos/debian.iso")], []), ([(f["name"], f["path"]) for f in files], problems))
        with self.assertRaisesRegex(ValueError, "no share"):
            ISOS.set_folders([{"share": "nope", "path": ""}])
        with self.assertRaisesRegex(ValueError, "climb"):
            ISOS.set_folders([{"share": "media", "path": "../etc"}])

    def test_making_one_ready_copies_it_once_beside_the_share(self):
        result = ISOS.prepare("media", "isos/debian.iso")
        pvc = next(b for m, p, b in self.sent if m == "POST" and p.endswith("/persistentvolumeclaims"))
        job = next(b for m, p, b in self.sent if m == "POST" and p.endswith("/jobs"))
        self.assertEqual(["ReadWriteMany"], pvc["spec"]["accessModes"])
        self.assertEqual("2Gi", pvc["spec"]["resources"]["requests"]["storage"], "800 MiB and room, rounded up")
        self.assertEqual("media/isos/debian.iso", pvc["metadata"]["annotations"][ISOS.SOURCE])
        spec = job["spec"]["template"]["spec"]
        self.assertEqual({"kubernetes.io/hostname": "node-2"}, spec["nodeSelector"], "a ReadWriteOnce share copies on its node")
        self.assertTrue(spec["volumes"][0]["persistentVolumeClaim"]["readOnly"])
        self.assertEqual("/share/isos/debian.iso", spec["containers"][0]["env"][0]["value"])
        self.assertIn("disk.img", spec["containers"][0]["command"][2])
        self.sent.clear()
        self.assertEqual(result["name"], ISOS.prepare("media", "isos/debian.iso")["name"])
        self.assertEqual([], [s for s in self.sent if s[0] == "POST"], "not copied twice")

    def test_a_file_outside_the_folders_or_not_an_iso_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not in one of the ISO folders"):
            ISOS.prepare("media", "other/debian.iso")
        with self.assertRaisesRegex(ValueError, ".iso"):
            ISOS.prepare("media", "isos/notes.txt")

    def test_a_volume_is_attachable_only_once_copied_and_kept_while_used(self):
        name = ISOS.prepare("media", "isos/debian.iso")["name"]
        with self.assertRaisesRegex(ValueError, "still being copied"):
            ISOS.ready_volume("lab", name)
        self.objects[f"/apis/batch/v1/namespaces/lab/jobs/{name}"]["status"] = {"succeeded": 1}
        ISOS.ready_volume("lab", name)
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/win"] = {"metadata": {"name": "win"}, "spec": {"template": {"spec": {
            "volumes": [{"name": "cdrom-0", "persistentVolumeClaim": {"claimName": name, "readOnly": True}}]}}}}
        with self.assertRaisesRegex(ValueError, "win still has it"):
            ISOS.delete(name)

    def test_a_stopped_vm_holding_its_iso_read_only_is_unlocked(self):
        name = ISOS.prepare("media", "isos/debian.iso")["name"]
        vm = lambda status: {"metadata": {"name": "win", "resourceVersion": "7"}, "status": {"printableStatus": status},
                             "spec": {"template": {"spec": {"volumes": [
                                 {"name": "root", "persistentVolumeClaim": {"claimName": "win-root", "readOnly": True}},
                                 {"name": "install", "persistentVolumeClaim": {"claimName": name, "readOnly": True}}]}}}}
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/win"] = vm("Running")
        self.assertEqual([], ISOS.unlock())
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/win"] = vm("Stopped")
        self.sent.clear()
        self.assertEqual(["win"], ISOS.unlock())
        patch = next(b for m, p, b in self.sent if m == "PATCH")
        self.assertEqual([{"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
                          {"op": "remove", "path": "/spec/template/spec/volumes/1/persistentVolumeClaim/readOnly"}], patch,
                         "only the ISO's drive; other claims are left as they are")

    def test_a_changed_file_gets_a_new_volume(self):
        self.assertNotEqual(ISOS.volume_name("media", "isos/a.iso", 1), ISOS.volume_name("media", "isos/a.iso", 2))
        self.assertTrue(ISOS.volume_name("media", "isos/Windows 11 (x64).iso", 5).startswith("iso-windows-11-x64-"))


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.cluster = edit_fixtures.Cluster({"harvester": True, "cdi": True})

    def test_an_iso_goes_in_a_cd_rom(self):
        with mock.patch.object(vms, "iso_ready", lambda ns, name: {"metadata": {"name": name}}):
            prepared = vms.prepare_edit("lab", "web", {"add_disks": [{"kind": "cd-rom", "iso": "iso-debian-1", "boot": "1"}]})
        spec = prepared["vm"]["spec"]["template"]["spec"]
        drive = next(d for d in spec["domain"]["devices"]["disks"] if "cdrom" in d)
        volume = next(v for v in spec["volumes"] if v["name"] == drive["name"])
        self.assertEqual(({"bus": "sata"}, 1), (drive["cdrom"], drive["bootOrder"]))
        # Not readOnly: KubeVirt cannot start a filesystem volume mounted read-only.
        self.assertEqual({"claimName": "iso-debian-1"}, volume["persistentVolumeClaim"])
        self.assertEqual([], prepared["to_create"], "the ISO's volume is shared, not made again")

    def test_a_library_iso_is_attached_not_refused_as_an_existing_new_disk(self):
        # "New disk iso-virtio-win-0-1-285-bf0e959b already exists; it cannot be adopted by this edit"
        vm = lambda claims: {"metadata": {"uid": "u", "resourceVersion": "1"}, "spec": {"template": {"spec": {
            "volumes": [{"name": f"v{i}", "persistentVolumeClaim": {"claimName": c}} for i, c in enumerate(claims)]}}}}
        prepared = {"namespace": "lab", "name": "w11", "identity": {"uid": "u", "resourceVersion": "1"}, "effects": [], "resize": [],
                    "claims": {}, "current": vm(["w11-disk"]), "vm": vm(["w11-disk", "iso-virtio"])}
        found = {"iso-virtio": {"metadata": {"labels": {"homestead.io/iso": "true"}}}}
        with mock.patch.object(vms, "_get", lambda ns, name: prepared["current"]), \
                mock.patch.object(vms, "_identity", lambda obj: {"uid": "u", "resourceVersion": "1"}), \
                mock.patch.object(vms, "_optional", lambda path: found.get(path.rsplit("/", 1)[-1])):
            vms._recheck_edit(prepared)
            found["iso-virtio"] = {"metadata": {"labels": {}}}
            with self.assertRaisesRegex(ValueError, "cannot be adopted"):
                vms._recheck_edit(prepared)

    def test_an_iso_not_ready_is_refused(self):
        def refuse(ns, name):
            raise ValueError("iso-debian-1 is still being copied")
        with mock.patch.object(vms, "iso_ready", refuse), self.assertRaisesRegex(ValueError, "still being copied"):
            vms.prepare_edit("lab", "web", {"add_disks": [{"kind": "cd-rom", "iso": "iso-debian-1"}]})

    def test_hardware_travels_with_an_edit(self):
        prepared = vms.prepare_edit("lab", "web", {"hardware": {"firmware": "uefi", "secure_boot": False, "efi_persistent": False, "tpm": "on"}})
        dom = prepared["vm"]["spec"]["template"]["spec"]["domain"]
        self.assertEqual(({"efi": {"secureBoot": False}}, {}), (dom["firmware"]["bootloader"], dom["devices"]["tpm"]))
        self.assertTrue(prepared["changed_hardware"])
        with self.assertRaisesRegex(ValueError, "count or its topology"):
            vms.prepare_edit("lab", "web", {"cores": 4, "hardware": {"cpu": {"sockets": 2}}})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.sent = []

        def get(path):
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        imports.kget, imports.ksend, imports.NS, imports._cache = get, lambda *a, **k: None, "lab", {}

    def test_windows_gets_its_virtio_drivers_in_a_second_drive(self):
        with mock.patch.object(imports, "iso_ready", lambda ns, name: {}):
            plan = imports.prepare_vm({"name": "w11", "install_iso": "iso-win11-1", "drivers_iso": "iso-virtio-win-1", "disk_gb": 64},
                                      {"harvester": False, "cdi": True}, "longhorn")
            spec = plan["vm"]["spec"]["template"]["spec"]
            drivers = next(d for d in spec["domain"]["devices"]["disks"] if d["name"] == "drivers")
            self.assertEqual({"bus": "sata"}, drivers["cdrom"])
            self.assertNotIn("bootOrder", drivers, "the installer boots, not the drivers")
            self.assertIn({"name": "drivers", "persistentVolumeClaim": {"claimName": "iso-virtio-win-1"}}, spec["volumes"])
            with self.assertRaisesRegex(ValueError, "installer itself"):
                imports.prepare_vm({"name": "w11", "install_iso": "iso-win11-1", "drivers_iso": "iso-win11-1"},
                                   {"harvester": False, "cdi": True}, "longhorn")

    def test_a_vm_installs_from_an_iso_onto_a_blank_disk_with_its_preset(self):
        with mock.patch.object(imports, "iso_ready", lambda ns, name: {}):
            plan = imports.prepare_vm({"name": "win", "install_iso": "iso-win11-1", "disk_gb": 64,
                                       "hardware": {"firmware": "uefi", "secure_boot": True, "tpm": "persistent"}},
                                      {"harvester": False, "cdi": True}, "longhorn")
        spec = plan["vm"]["spec"]["template"]["spec"]
        disks = {d["name"]: d for d in spec["domain"]["devices"]["disks"]}
        self.assertEqual((1, 2), (disks["install"]["bootOrder"], disks["root"]["bootOrder"]))
        self.assertIn({"name": "install", "persistentVolumeClaim": {"claimName": "iso-win11-1"}}, spec["volumes"])
        self.assertFalse(any("cloudInitNoCloud" in v for v in spec["volumes"]), "no password: the installer asks")
        self.assertEqual({"persistent": True}, spec["domain"]["devices"]["tpm"])
        with self.assertRaisesRegex(ValueError, "blank disk"):
            imports.prepare_vm({"name": "win", "install_iso": "iso-win11-1", "image_url": "https://x.test/a.img"},
                               {"harvester": False, "cdi": True}, "longhorn")


if __name__ == "__main__":
    unittest.main()
