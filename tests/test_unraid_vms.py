import base64
import json
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_unraid_vms as UVMS

# A Windows 11 VM as Unraid's VM manager writes it.
WIN11 = """<domain type='kvm'>
  <name>Windows 11</name>
  <uuid>0f5c2a3e-1d4b-4c8e-9a7f-2b6d8e1f3c5a</uuid>
  <metadata><vmtemplate xmlns="unraid" name="Windows 11" icon="windows11.png" os="windows11"/></metadata>
  <memory unit='KiB'>8388608</memory>
  <currentMemory unit='KiB'>8388608</currentMemory>
  <vcpu placement='static'>4</vcpu>
  <cputune><vcpupin vcpu='0' cpuset='2'/></cputune>
  <os>
    <type arch='x86_64' machine='pc-q35-9.2'>hvm</type>
    <loader readonly='yes' type='pflash'>/usr/share/qemu/ovmf-x64/OVMF_CODE-pure-efi-tpm.fd</loader>
    <nvram>/etc/libvirt/qemu/nvram/0f5c2a3e_VARS-pure-efi-tpm.fd</nvram>
  </os>
  <features><acpi/><apic/><hyperv mode='custom'><relaxed state='on'/></hyperv></features>
  <cpu mode='host-passthrough' check='none' migratable='on'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='raw' cache='writeback'/>
      <source file='/mnt/user/domains/Windows 11/vdisk1.img'/>
      <target dev='hdc' bus='sata'/>
      <boot order='1'/>
    </disk>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/mnt/user/domains/Windows 11/vdisk2.img'/>
      <target dev='hdd' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <source file='/mnt/user/isos/virtio-win-0.1.262-2.iso'/>
      <target dev='hdb' bus='sata'/>
    </disk>
    <interface type='bridge'>
      <mac address='52:54:00:3A:1C:07'/>
      <source bridge='br0'/>
      <model type='virtio-net'/>
    </interface>
    <tpm model='tpm-tis'><backend type='emulator' version='2.0' persistent_state='yes'/></tpm>
    <graphics type='vnc' port='-1' autoport='yes'/>
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source><address domain='0x0000' bus='0x01' slot='0x00' function='0x0'/></source>
    </hostdev>
  </devices>
</domain>"""

PATH1, PATH2 = "/mnt/user/domains/Windows 11/vdisk1.img", "/mnt/user/domains/Windows 11/vdisk2.img"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def listing_lines(state="shut off"):
    return [f"VM {b64('Windows 11')} {b64(state)} {b64(WIN11)}",
            f"DISK {b64(PATH1)} 85899345920 19327352832 {b64(json.dumps({'format': 'raw', 'virtual-size': 85899345920}))}",
            f"DISK {b64(PATH2)} 2147483648 2147483648 {b64(json.dumps({'format': 'qcow2', 'virtual-size': 107374182400}))}",
            "END"]


class ReadTests(unittest.TestCase):
    def mapped(self, state="shut off"):
        found = UVMS.parse_listing(listing_lines(state))
        name, state, xml = found["domains"][0]
        dom = UVMS.parse_domain(xml)
        dom["state"] = state
        return UVMS.plan(dom, found["disks"])

    def test_a_windows_vm_maps_across_with_what_it_needs_to_boot(self):
        vm = self.mapped()
        self.assertEqual(("windows-11", 4, "8Gi", "uefi", True, True), (vm["slug"], vm["cores"], vm["memory"],
                                                                        vm["firmware"], vm["tpm"], vm["hyperv"]))
        self.assertEqual("windows11", vm["os"])
        self.assertEqual("host-passthrough", vm["cpu_model"])
        boot, data = vm["disks"]
        self.assertEqual((PATH1, "sata", "raw", 80), (boot["path"], boot["bus"], boot["format"], boot["size_gb"]),
                         "Windows installed on SATA keeps SATA, or it cannot find its disk")
        self.assertEqual(("qcow2", 100), (data["format"], data["size_gb"]), "qcow2 is sized by its virtual size")
        self.assertEqual({"mac": "52:54:00:3a:1c:07", "bridge": "br0", "model": "virtio-net", "nic_model": "virtio"}, vm["nic"])
        self.assertTrue(vm["ready"] and vm["shut_off"])

    def test_what_cannot_come_across_says_why(self):
        dropped = {row["what"]: row for row in self.mapped()["dropped"]}
        self.assertEqual({"CD-ROM", "GPU or PCI device", "CPU pinning"}, set(dropped))
        self.assertEqual("0000:01:00.0", dropped["GPU or PCI device"]["detail"])
        self.assertTrue(dropped["GPU or PCI device"]["hardware"])
        self.assertTrue(all(row["reason"] for row in dropped.values()))
        self.assertTrue(any("BitLocker" in note for note in self.mapped()["notes"]), "a new TPM is said, not hidden")

    def test_a_running_vm_is_listed_but_not_ready_to_copy(self):
        self.assertFalse(self.mapped("running")["shut_off"])

    def test_a_listing_cut_short_is_not_taken_as_the_whole(self):
        self.assertFalse(UVMS.parse_listing(listing_lines()[:-1])["complete"])
        self.assertFalse(UVMS.parse_listing(["NOVIRSH"])["virsh"])

    def test_an_old_machine_type_and_odd_memory_are_said_plainly(self):
        dom = UVMS.parse_domain(WIN11.replace("pc-q35-9.2", "pc-i440fx-7.2").replace("8388608", "1572864"))
        dom["state"] = "shut off"
        vm = UVMS.plan(dom, UVMS.parse_listing(listing_lines())["disks"])
        self.assertEqual("1536Mi", vm["memory"])
        self.assertTrue(any("q35" in note for note in vm["notes"]))

    def test_names_become_homestead_names(self):
        self.assertEqual(("windows-11", "ha-os", "imported-vm"),
                         (UVMS.slug("Windows 11"), UVMS.slug("  HA_OS!! "), UVMS.slug("!!!")))
        self.assertEqual(["web-disk", "web-disk-2"], UVMS.disk_names("web", 2))


class FakeImports:
    NS = "lab"
    VM_DISK_LABEL = "homestead.io/vm-disk-import"

    class SOURCE_SSH:
        @staticmethod
        def command(src, remote, compress=False):
            return "ssh " + ("-o Compression=yes " if compress else "") + "root@192.0.2.10 " + repr(remote)

        @staticmethod
        def setup(src):
            return "printf known > /tmp/k\n"

    def __init__(self, lines):
        self.lines, self.created = lines, []

    def _source(self, name):
        return {"name": name, "kind": "unraid", "host": "192.0.2.10", "user": "root"}

    def _ssh_script(self, src, remote):
        return "ssh root@192.0.2.10 " + repr(remote)

    def run_probe(self, tag, script, src, timeout=70):
        self.probed = script
        return self.lines

    def source_secret(self, name, src=None):
        return f"source-{name}"

    def _required_name(self, value, label="name"):
        return str(value)

    def _get_or_none(self, path):
        return None

    def vm_disk_import_plan(self, ns, name):
        return {"ready": True, "message": ""}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.sent, self.objects, self.started = [], {}, []
        self.imp = FakeImports(listing_lines())

        def ksend(method, path, body=None):
            self.sent.append((method, path, body))
            if path.endswith("/uploadtokenrequests"):
                return {"status": {"token": "tok"}}
            return {}

        class Ops:
            @staticmethod
            def start(kind, title, resource, href, ref, message):
                self.started.append((kind, title, ref))
                return {"id": "op1", "ref": ref}

        def kget(path):
            if "services?" in path:
                return {"items": [{"metadata": {"namespace": "cdi"}}]}
            return self.objects.get(path, {})

        UVMS.bind(self.imp, kget, ksend, Ops)

    def test_a_running_vm_is_refused_until_it_is_shut_down(self):
        self.imp.lines = listing_lines("running")
        with self.assertRaises(ValueError) as caught:
            UVMS.start({"source": "nas", "vm": "Windows 11"})
        self.assertIn("shut it down first", str(caught.exception))
        self.assertEqual([], self.sent)

    def test_import_makes_upload_disks_and_a_stopped_vm_plan(self):
        op = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11", "storage_class": "longhorn-r2"})
        dvs = [body for method, path, body in self.sent if path.endswith("/datavolumes")]
        self.assertEqual(["win11-disk", "win11-disk-2"], [dv["metadata"]["name"] for dv in dvs])
        self.assertEqual({"upload": {}}, dvs[0]["spec"]["source"])
        self.assertEqual(("80Gi", "longhorn-r2"), (dvs[0]["spec"]["storage"]["resources"]["requests"]["storage"],
                                                   dvs[0]["spec"]["storage"]["storageClassName"]))
        for dv in dvs:
            self.assertEqual("Filesystem", dv["spec"]["storage"]["volumeMode"],
                             "CDI must not inherit Block mode and require access to a root-owned device")
            self.assertEqual(["ReadWriteOnce"], dv["spec"]["storage"]["accessModes"])
        cfg = op["ref"]["vm_cfg"]
        self.assertFalse(cfg["start"])
        self.assertEqual(("win11-disk", "sata", [{"name": "win11-disk-2", "bus": "virtio"}], "52:54:00:3a:1c:07"),
                         (cfg["disk_import"], cfg["disk_bus"], cfg["extra_disks"], cfg["mac"]))
        self.assertEqual({"firmware": "uefi", "machine": "q35", "hyperv": True, "secure_boot": False, "tpm": "on"}, cfg["hardware"])
        self.assertEqual("https://cdi-uploadproxy.cdi.svc/v1beta1/upload", op["ref"]["url"])

    def test_a_skipped_second_disk_is_left_behind(self):
        op = UVMS.start({"source": "nas", "vm": "Windows 11", "skip_disks": [1]})
        self.assertEqual(["windows-11-disk"], [d["dv"] for d in op["ref"]["disks"]])
        self.assertEqual([], op["ref"]["vm_cfg"]["extra_disks"])

    def test_the_copy_checks_the_vm_is_still_off_and_streams_into_the_upload(self):
        script = UVMS.copy_script(self.imp._source("nas"), "Windows 11", PATH1, 85899345920)
        self.assertIn("virsh domstate --domain 'Windows 11'", script)
        self.assertIn("exit 5", script)
        self.assertIn("cat -- '/mnt/user/domains/Windows 11/vdisk1.img'", script)
        cat = next(line for line in script.splitlines() if "cat -- " in line)
        self.assertIn("Compression=yes", cat, "a raw disk's empty space crosses as almost nothing")
        self.assertIn("--cacert /ca/ca.crt", script)
        self.assertIn('Bearer $TOKEN', script)
        self.assertNotIn("tok", script, "the token comes from a Secret, never the script")

    def test_an_unraid_name_never_becomes_part_of_a_command(self):
        script = UVMS.copy_script(self.imp._source("nas"), 'x$(touch /tmp/owned)"`id`', PATH1, 1)
        self.assertIn("vm='x$(touch /tmp/owned)\"`id`'", script)
        self.assertNotIn("HSVM-FAILED x$(", script)

    def test_progress_is_what_pv_last_said(self):
        self.assertEqual(42, UVMS.progress("7\n19\n42\n"))
        self.assertEqual(0, UVMS.progress("fetch https://dl-cdn\n"))

    def test_a_meter_line_does_not_hide_the_transport_or_upload_error(self):
        for error in ("curl: (22) The requested URL returned error: 413", "Permission denied", "No space left on device"):
            with self.subTest(error=error):
                self.assertEqual(error, UVMS.failure_reason(error + "\n0\n"))
        self.assertIn("copy stopped", UVMS.failure_reason("0\n"))
        self.assertEqual("VM started again", UVMS.failure_reason("HSVM-FAILED VM started again\n0\n"))

    def test_upload_body_explains_the_http_failure(self):
        cause = "Saving stream failed: blockdev: cannot open /dev/cdi-block-volume: Permission denied"
        log = cause + "\ncurl: (22) The requested URL returned error: 500\n0\nHSVM-FAILED Disk stream or CDI upload exited with status 22\n"
        self.assertEqual(cause, UVMS.failure_reason(log))

    def test_filesystem_upload_keeps_the_selected_class_and_default_choice(self):
        for selected in ("", "longhorn-ssd", "longhorn-hdd"):
            with self.subTest(selected=selected):
                self.sent.clear()
                UVMS.start({"source": "nas", "vm": "Windows 11", "storage_class": selected})
                disks = [body["spec"]["storage"] for method, path, body in self.sent if path.endswith("/datavolumes")]
                self.assertEqual(2, len(disks))
                for disk in disks:
                    self.assertEqual("Filesystem", disk["volumeMode"])
                    self.assertEqual(["ReadWriteOnce"], disk["accessModes"])
                    self.assertEqual(selected or None, disk.get("storageClassName"))

    def test_failure_keeps_bounded_evidence_before_removing_resources(self):
        ref = {"namespace": "lab", "disks": [{"dv": "desktop-disk", "job": "copy", "bytes": 1}]}
        evidence = [{"title": "CDI upload", "text": "No space left on device\n" + "x" * 70000, "note": "upload failed"}]
        with mock.patch.object(UVMS, "log_sources", return_value=evidence), mock.patch.object(UVMS, "_delete") as delete:
            delete.side_effect = lambda path: self.assertIn("diagnostics", ref)
            state, _, _ = UVMS._fail(ref, "Upload failed")
        self.assertEqual("failed", state)
        self.assertLessEqual(len(ref["diagnostics"][0]["text"]), 20000)
        self.assertEqual(ref["diagnostics"], UVMS.log_sources({"ref": ref}))

    def test_a_failed_copy_reports_an_error_instead_of_the_last_percentage(self):
        ref = {"namespace": "lab", "vm": "Desktop VM", "phase": "copy", "disks": [{"dv": "desktop-disk", "job": "copy", "bytes": 1}]}
        self.objects[f"{UVMS.CDI_API}/namespaces/lab/datavolumes/desktop-disk"] = {"status": {"phase": "UploadReady"}}
        self.imp._get_or_none = lambda path: {"status": {"failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}}
        with mock.patch.object(UVMS, "_job_logs", return_value="curl: (22) The requested URL returned error: 500\n0\n"):
            state, _, message = UVMS.status({"ref": ref})
        self.assertEqual("failed", state)
        self.assertIn("curl: (22)", message)
        self.assertNotIn("failed: 0", message)

    def test_logs_follow_the_confirmed_job_and_prime_upload_pvc(self):
        ref = {"namespace": "lab", "phase": "copy", "disks": [{"dv": "desktop-disk", "job": "copy", "job_uid": "job-current"}]}
        pod = {"metadata": {"name": "copy-current", "ownerReferences": [{"kind": "Job", "uid": "job-current", "controller": True}]},
               "status": {"containerStatuses": [{"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
        stale = {"metadata": {"name": "copy-previous", "ownerReferences": [{"kind": "Job", "uid": "job-previous", "controller": True}]}}
        upload = {"metadata": {"name": "upload-prime", "uid": "upload-uid"},
                  "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "prime-pvc"}}]}}
        unrelated = {"metadata": {"name": "upload-other"}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "other-pvc"}}]}}
        self.objects["/api/v1/namespaces/lab/pods?labelSelector=job-name%3Dcopy"] = {"items": [stale, pod]}
        self.objects["/api/v1/namespaces/lab/pods?labelSelector=cdi.kubevirt.io%3Dcdi-upload-server"] = {"items": [unrelated, upload]}
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/desktop-disk"] = {
            "metadata": {"uid": "pvc-uid", "annotations": {"cdi.kubevirt.io/storage.populator.pvcPrime": "prime-pvc"}},
            "spec": {"resources": {"requests": {"storage": "200Gi"}}}, "status": {"phase": "Bound", "capacity": {"storage": "200Gi"}}}
        import homestead_joblogs as logs
        with mock.patch.object(logs, "_pod_source", side_effect=lambda ns, p, title: {"title": title, "text": p["metadata"]["name"], "note": ""}), \
                mock.patch.object(logs, "_events", return_value="storage event") as events:
            sources = UVMS.log_sources({"ref": ref})
        self.assertEqual(["copy-current", "storage event", "upload-prime", "storage event"], [s["text"] for s in sources])
        self.assertEqual("OOMKilled (exit 137)", sources[0]["note"])
        events.assert_any_call("lab", "desktop-disk", "pvc-uid")
        events.assert_any_call("lab", "upload-prime", "upload-uid")
        pod["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": "2026-01-01T12:00:00Z"}}
        with mock.patch.object(logs, "_pod_source", return_value={"title": "Copy", "text": "4\n4\n", "note": ""}), \
                mock.patch.object(logs, "_events", return_value=""):
            self.assertEqual("", UVMS.log_sources({"ref": ref})[0]["note"], "a running copy must not be labelled exited")

    def test_an_old_failed_import_never_reads_output_from_a_retry(self):
        ref = {"namespace": "lab", "phase": "failed", "disks": [{"dv": "desktop-disk", "job": "copy"}]}
        with mock.patch.object(UVMS, "kget") as get:
            sources = UVMS.log_sources({"ref": ref})
        get.assert_not_called()
        self.assertIn("earlier error cannot be recovered", sources[0]["note"])

    def test_the_job_waits_for_cdi_then_starts_one_copy_per_disk(self):
        op = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        item = {"ref": op["ref"]}
        for dv in ("win11-disk", "win11-disk-2"):
            self.objects[f"{UVMS.CDI_API}/namespaces/lab/datavolumes/{dv}"] = {"status": {"phase": "UploadScheduled"}}
        self.assertEqual(("running", 2), UVMS.status(item)[:2])
        for dv in ("win11-disk", "win11-disk-2"):
            self.objects[f"{UVMS.CDI_API}/namespaces/lab/datavolumes/{dv}"] = {"status": {"phase": "UploadReady"}}
        self.objects["/api/v1/namespaces/cdi/configmaps/cdi-uploadproxy-signer-bundle"] = {"data": {"ca-bundle.crt": "CA"}}
        self.imp._get_or_none = lambda path: self.objects.get(path)
        self.assertEqual("running", UVMS.status(item)[0])
        jobs = [body for method, path, body in self.sent if path.endswith("/jobs")]
        self.assertEqual(2, len(jobs))
        self.assertEqual("copy", item["ref"]["phase"])
        secrets = [body for method, path, body in self.sent if path.endswith("/secrets")]
        self.assertEqual({"token": "tok", "ca.crt": "CA"}, secrets[0]["stringData"])

    def test_a_failed_copy_requests_cleanup_and_explains_retained_data(self):
        op = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        item = {"ref": op["ref"]}
        for dv in ("win11-disk", "win11-disk-2"):
            self.objects[f"{UVMS.CDI_API}/namespaces/lab/datavolumes/{dv}"] = {"status": {"phase": "Failed"}}
        state, _, message = UVMS.status(item)
        self.assertEqual("failed", state)
        self.assertIn("VM on Unraid is unchanged", message)
        self.assertIn("Retain storage policy keeps backing data", message)
        self.assertNotIn("disks were removed", message)
        deleted = [path for method, path, body in self.sent if method == "DELETE"]
        self.assertIn(f"{UVMS.CDI_API}/namespaces/lab/datavolumes/win11-disk", deleted)


if __name__ == "__main__":
    unittest.main()
