import base64
import json
import sys
import contextlib
import copy
import urllib.error
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_unraid_vms as UVMS
from rebind import keep_bound

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
            if method == "POST" and body and body.get("metadata", {}).get("name"):
                obj = copy.deepcopy(body)
                obj["metadata"].update(uid="uid-" + body["metadata"]["name"], resourceVersion="1")
                self.objects[path + "/" + body["metadata"]["name"]] = obj
                return obj
            if method == "DELETE":
                self.objects.pop(path, None)
            return {}

        class Ops:
            @staticmethod
            def start(kind, title, resource, href, ref, message):
                self.started.append((kind, title, ref))
                return {"id": "op1", "ref": ref}

            @staticmethod
            def dispatch_guard():
                return contextlib.nullcontext()

            @staticmethod
            def checkpoint(item):
                pass

            @staticmethod
            def record_phase(operation_id, phase, progress, message, **updates):
                ref = self.started[-1][2]
                ref.update(updates, phase=phase)
                return {"id": operation_id, "ref": ref}

        def kget(path):
            if "services?" in path:
                return {"items": [{"metadata": {"namespace": "cdi"}}]}
            if path in self.objects:
                return self.objects[path]
            if path.endswith(("/persistentvolumeclaims", "/pods", "/persistentvolumes", "/virtualmachines", "/virtualmachineinstances")):
                return {"items": []}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        keep_bound(self, UVMS)   # put back what UVMS.bind() sets
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
        # 80 GiB and the room its filesystem takes (6%)
        self.assertEqual(("86836Mi", "longhorn-r2"), (dvs[0]["spec"]["storage"]["resources"]["requests"]["storage"],
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
        self.assertIn("pv -n -t -b -i 10", script, "progress records elapsed time and bytes, not bare percentages")
        self.assertIn('Bearer $TOKEN', script)
        self.assertNotIn("tok", script, "the token comes from a Secret, never the script")

    def test_an_unraid_name_never_becomes_part_of_a_command(self):
        script = UVMS.copy_script(self.imp._source("nas"), 'x$(touch /tmp/owned)"`id`', PATH1, 1)
        self.assertIn("vm='x$(touch /tmp/owned)\"`id`'", script)
        self.assertNotIn("HSVM-FAILED x$(", script)

    def test_progress_is_what_pv_last_said(self):
        self.assertEqual(42, UVMS.progress("7\n19\n42\n"))
        self.assertEqual(0, UVMS.progress("fetch https://dl-cdn\n"))

    def test_byte_samples_update_progress_and_keep_upload_errors(self):
        log = "10.0 100000000\n20.0 250000000\ncurl: (22) HTTP 500\nHSVM-FAILED upload failed\n"
        self.assertEqual(25, UVMS.progress(log, 1000000000))
        source = UVMS.copy_output({"text": log}, {"bytes": 1000000000})
        self.assertIn("25.0% · 0.2 / 0.9 GiB · 14.3 MiB/s · ETA 0h 0m 50s", source["text"])
        self.assertIn("curl: (22) HTTP 500", source["text"])
        self.assertEqual(50, source["progress"]["eta_seconds"])
        self.assertIn("HTTP 500", UVMS.failure_reason(log))

    def test_old_percentage_logs_do_not_invent_speed_or_eta_from_rounded_bytes(self):
        source = UVMS.copy_output({"text": "2026-01-01T12:00:10Z 10\n2026-01-01T12:00:20Z 20\n"}, {"bytes": 1000000000})
        self.assertIn("12:00:20 · ~20.0% · ~0.2 / 0.9 GiB · Speed / ETA unavailable (1% progress)", source["text"])
        self.assertIsNone(source["progress"]["bytes_per_second"])
        self.assertIsNone(source["progress"]["eta_seconds"])
        self.assertTrue(source["progress"]["coarse"])
        unstamped = UVMS.copy_output({"text": "10\n20\n"}, {"bytes": 1000000000})
        self.assertIn("~20.0% · ~0.2 / 0.9 GiB · Speed / ETA unavailable", unstamped["text"])
        self.assertIsNone(unstamped["progress"]["eta_seconds"], "missing timing cannot invent an ETA")

    def test_repeated_whole_percentages_are_not_a_measured_stall(self):
        log = "\n".join(f"2026-01-01T12:{i:02}:00Z 36" for i in range(8))
        source = UVMS.copy_output({"text": log, "note": "Running"}, {"bytes": 80000000000})
        self.assertNotIn("MiB/s", source["text"])
        self.assertIsNone(source["progress"]["bytes_per_second"])
        self.assertIsNone(source["progress"]["eta_seconds"])
        self.assertIn("Repeated values do not confirm a stalled copy", source["note"])
        self.assertTrue(source["note"].startswith("Running"))

    def test_byte_samples_measure_sub_percent_progress_even_after_legacy_samples(self):
        source = UVMS.copy_output({"text": "2026-01-01T12:00:00Z 36\n10.0 360000000\n20.0 364000000\n"}, {"bytes": 1000000000})
        self.assertEqual(400000, source["progress"]["bytes_per_second"])
        self.assertFalse(source["progress"]["coarse"])
        self.assertIn("36.4%", source["text"])
        self.assertIn("0.4 MiB/s", source["text"])

    def test_stalled_copy_has_no_countdown_and_full_stream_still_waits_for_cdi(self):
        source = UVMS.copy_output({"text": "10.0 100\n20.0 100\n"}, {"bytes": 1000})
        self.assertEqual(0, source["progress"]["bytes_per_second"])
        self.assertIsNone(source["progress"]["eta_seconds"])
        complete = UVMS.copy_output({"text": "30.0 1000\n"}, {"bytes": 1000})
        self.assertIn("100.0%", complete["text"])
        self.assertIn("waiting for CDI", complete["text"])

    def test_retained_old_logs_are_readable_without_reading_retry_pods(self):
        ref = {"disks": [{"dv": "desktop-disk", "bytes": 1000}], "diagnostics": [
            {"title": "Disk 1 · desktop-disk · copy", "text": "0\n10\nPermission denied", "note": "Error (exit 22)"}]}
        with mock.patch.object(UVMS, "kget") as get:
            source = UVMS.log_sources({"ref": ref})[0]
        get.assert_not_called()
        self.assertIn("10.0%", source["text"])
        self.assertIn("Permission denied", source["text"])

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
                self.objects.clear()
                UVMS.start({"source": "nas", "vm": "Windows 11", "storage_class": selected})
                disks = [body["spec"]["storage"] for method, path, body in self.sent if path.endswith("/datavolumes")]
                self.assertEqual(2, len(disks))
                for disk in disks:
                    self.assertEqual("Filesystem", disk["volumeMode"])
                    self.assertEqual(["ReadWriteOnce"], disk["accessModes"])
                    self.assertEqual(selected or None, disk.get("storageClassName"))

    def test_legacy_failure_keeps_diagnostics_and_never_deletes_a_retry_by_name(self):
        ref = {"namespace": "lab", "disks": [{"dv": "desktop-disk", "job": "copy", "bytes": 1}]}
        evidence = [{"title": "CDI upload", "text": "No space left on device\n" + "x" * 70000, "note": "upload failed"}]
        with mock.patch.object(UVMS, "log_sources", return_value=evidence):
            state, _, message = UVMS._fail(ref, "Upload failed")
        self.assertEqual("failed", state)
        self.assertIn("no cleanup receipt", message)
        self.assertEqual([], self.sent)
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
        with mock.patch.object(logs, "_pod_source", side_effect=lambda ns, p, title, **kw: {"title": title, "text": p["metadata"]["name"], "note": ""}) as pod_source, \
                mock.patch.object(logs, "_events", return_value="storage event") as events:
            sources = UVMS.log_sources({"ref": ref})
        self.assertEqual(["copy-current", "storage event", "upload-prime", "storage event"], [s["text"] for s in sources])
        self.assertEqual("OOMKilled (exit 137)", sources[0]["note"])
        pod_source.assert_any_call("lab", pod, "Disk 1 · desktop-disk · copy", timestamps=True)
        events.assert_any_call("lab", "desktop-disk", "pvc-uid")
        events.assert_any_call("lab", "upload-prime", "upload-uid")
        pod["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": "2026-01-01T12:00:00Z"}}
        with mock.patch.object(logs, "_pod_source", return_value={"title": "Copy", "text": "4\n4\n", "note": ""}), \
                mock.patch.object(logs, "_events", return_value=""):
            self.assertNotIn("exited", UVMS.log_sources({"ref": ref})[0]["note"], "a running copy must not be labelled exited")

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
            self.objects[f"{UVMS.CDI_API}/namespaces/lab/datavolumes/{dv}"]["status"] = {"phase": "UploadScheduled"}
        self.assertEqual(("running", 2), UVMS.status(item)[:2])
        for dv in ("win11-disk", "win11-disk-2"):
            self.objects[f"{UVMS.CDI_API}/namespaces/lab/datavolumes/{dv}"]["status"] = {"phase": "UploadReady"}
        self.objects["/api/v1/namespaces/cdi/configmaps/cdi-uploadproxy-signer-bundle"] = {"data": {"ca-bundle.crt": "CA"}}
        self.imp._get_or_none = lambda path: self.objects.get(path)
        self.assertEqual("running", UVMS.status(item)[0])
        jobs = [body for method, path, body in self.sent if path.endswith("/jobs")]
        self.assertEqual(2, len(jobs))
        self.assertEqual("copy", item["ref"]["phase"])
        secrets = [body for method, path, body in self.sent if path.endswith("/secrets")]
        self.assertEqual({"token": "tok", "ca.crt": "CA"}, secrets[0]["stringData"])

    def test_a_failed_copy_confirms_cleanup_instead_of_claiming_retained_data(self):
        op = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11"})
        item = {"ref": op["ref"]}
        for dv in ("win11-disk", "win11-disk-2"):
            self.objects[f"{UVMS.CDI_API}/namespaces/lab/datavolumes/{dv}"]["status"] = {"phase": "Failed"}
        state, _, message = UVMS.status(item)
        self.assertEqual("failed", state)
        self.assertIn("VM on Unraid is unchanged", message)
        self.assertIn("temporary import storage cleared", message)
        self.assertNotIn("disks were removed", message)
        deleted = [path for method, path, body in self.sent if method == "DELETE"]
        self.assertIn(f"{UVMS.CDI_API}/namespaces/lab/datavolumes/win11-disk", deleted)


def lh_class(name="longhorn-r2", replicas="2", provisioner="driver.longhorn.io", disk_tags=(), node_tags=()):
    return {"name": name, "provisioner": provisioner, "replicas": replicas,
            "disk_tags": list(disk_tags), "node_tags": list(node_tags)}


def lh_nodes(*rooms, tags=()):
    """capacity()'s nodes, one disk each with this much room (GiB) under both limits."""
    return {"nodes": [{"name": f"node{i + 1}", "tags": [], "disks": [
        {"id": "d", "room_gb": room, "physical_room_gb": room, "blocked": "", "tags": list(tags)}]}
        for i, room in enumerate(rooms)]}


class RoomTests(unittest.TestCase):
    """The room check before an import makes its disks: 80 GiB and 100 GiB,
    asked for as 84.8 and 106 GiB with their filesystem's room."""

    def setUp(self):
        ImportTests.setUp(self)
        self.classes = [lh_class(), lh_class("local-path", "", "rancher.io/local-path")]
        self.cap = lh_nodes(300, 300, 300)
        for name, value in (("storage_classes", lambda: self.classes), ("lh_capacity", lambda: self.cap),
                            ("default_class", lambda: "longhorn-r2")):
            patcher = mock.patch.object(UVMS, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def room(self, storage_class="longhorn-r2", skip=()):
        found = UVMS._one("nas", "Windows 11")
        return UVMS.storage_room(UVMS._chosen_disks(found, skip), storage_class)

    def test_disks_that_fit_with_room_to_spare_pass(self):
        room = self.room()
        self.assertEqual(("ok", [], []), (room["level"], room["blockers"], room["warnings"]))
        # Two copies on three nodes of 300 GiB: 450 GiB of two-copy volumes.
        self.assertEqual((190.8, 450.0, 2), (room["needed_gb"], room["free_gb"], room["copies"]))

    def test_too_little_room_blocks_and_says_what_is_needed_and_free(self):
        self.cap = lh_nodes(100, 100, 100)
        room = self.room()
        self.assertEqual("bad", room["level"])
        self.assertEqual(["Not enough room on longhorn-r2: these disks need 190.8 GiB (381.6 GiB with its 2 copies), "
                          "and it can still place 150 GiB. Free some space, add a disk, or leave a disk out"], room["blockers"])

    def test_leaving_a_disk_out_can_make_it_fit(self):
        self.cap = lh_nodes(100, 100, 100)
        self.assertEqual("ok", self.room(skip=[1])["level"])

    def test_tight_room_warns_but_does_not_block(self):
        self.cap = lh_nodes(200, 200)
        room = self.room()
        self.assertEqual(("warn", []), (room["level"], room["blockers"]))
        self.assertIn("Room is tight on longhorn-r2: these disks take 190.8 of the 200 GiB", room["warnings"][0])
        self.assertIn("leaving 9.2 GiB (5%)", room["warnings"][0])

    def test_each_copy_needs_its_own_node(self):
        self.cap = lh_nodes(1000)
        room = self.room()
        self.assertEqual("bad", room["level"])
        self.assertIn("keeps 2 copies of a disk, each on a different node, and 1 node has room", room["blockers"][0])

    def test_a_disk_too_big_for_any_one_longhorn_disk_is_blocked(self):
        # 270 GiB in all is enough by sum, but the 106 GiB disk fits on no node's disk.
        self.cap = {"nodes": [{"name": f"node{i}", "tags": [], "disks": [
            {"id": f"d{j}", "room_gb": 50, "physical_room_gb": 50, "blocked": "", "tags": []} for j in range(3)]}
            for i in range(3)]}
        room = self.room()
        self.assertEqual("bad", room["level"])
        self.assertIn("in one place: a 106 GiB disk needs that much free on one Longhorn disk on each of 2 nodes", room["blockers"][0])

    def test_the_less_of_over_provisioned_and_physical_room_counts(self):
        self.cap = {"nodes": [{"name": f"node{i}", "tags": [], "disks": [
            {"id": "d", "room_gb": 900, "physical_room_gb": 90, "blocked": "", "tags": []}]} for i in range(3)]}
        self.assertEqual(135.0, self.room()["free_gb"])

    def test_blocked_disks_and_untagged_ones_do_not_count(self):
        self.classes = [lh_class(disk_tags=["ssd"])]
        self.cap = lh_nodes(300, 300, 300)
        self.cap["nodes"][0]["disks"][0]["tags"] = ["ssd"]
        self.cap["nodes"][1]["disks"][0]["tags"] = ["ssd"]
        self.assertEqual(300.0, self.room()["free_gb"])
        self.cap["nodes"][1]["disks"][0]["blocked"] = "scheduling is off on this disk"
        self.assertEqual("bad", self.room()["level"])

    def test_the_default_class_is_checked_when_none_is_chosen(self):
        self.assertEqual(("longhorn-r2", "ok"), (self.room("")["storage_class"], self.room("")["level"]))

    def test_a_class_that_is_not_longhorn_or_unreadable_warns_without_blocking(self):
        room = self.room("local-path")
        self.assertEqual(("unknown", []), (room["level"], room["blockers"]))
        self.assertIn("local-path is not Longhorn, so Homestead cannot check it has room for 190.8 GiB", room["warnings"][0])
        with mock.patch.object(UVMS, "lh_capacity", mock.Mock(side_effect=RuntimeError("down"))):
            room = self.room()
        self.assertEqual(("unknown", []), (room["level"], room["blockers"]))
        self.assertIn("Longhorn's free space could not be read", room["warnings"][0])

    def test_longhorns_settings_decide_the_room(self):
        # Real capacity(): two 100 GiB disks, 90 GiB physically free, nothing
        # placed yet. Over-provisioning 200% books up to 200 GiB on each, but
        # minimal-available 25% keeps 25 GiB of each free: 65 GiB for one disk.
        import homestead_lhcapacity as LHCAP
        lh_node = lambda name: {"metadata": {"name": name}, "spec": {"disks": {"d": {"path": "/var/lib/longhorn"}}},
                                "status": {"diskStatus": {"d": {"storageMaximum": 100 * 1024 ** 3, "storageScheduled": 0,
                                                                "storageAvailable": 90 * 1024 ** 3}}}}
        with mock.patch.object(LHCAP, "kget", lambda path: {"items": [lh_node("node1"), lh_node("node2")]}):
            for minimal, level, free in ((25, "bad", 65.0), (5, "warn", 85.0)):
                with self.subTest(minimal=minimal), mock.patch.object(UVMS, "lh_capacity", lambda: LHCAP.capacity(
                        {"over_provisioning": 200, "minimal_available": minimal})):
                    room = self.room(skip=[1])
                    self.assertEqual((level, free), (room["level"], room["free_gb"]))

    def test_an_import_that_will_not_fit_is_refused_before_anything_is_made(self):
        self.cap = lh_nodes(100, 100, 100)
        with self.assertRaises(ValueError) as caught:
            UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11", "storage_class": "longhorn-r2"})
        self.assertIn("these disks need 190.8 GiB", str(caught.exception))
        self.assertIn("it can still place 150 GiB", str(caught.exception))
        self.assertEqual([], self.sent)
        self.assertEqual([], self.started)

    def test_a_tight_import_still_starts(self):
        self.cap = lh_nodes(200, 200)
        op = UVMS.start({"source": "nas", "vm": "Windows 11", "name": "win11", "storage_class": "longhorn-r2"})
        self.assertEqual(["win11-disk", "win11-disk-2"], [d["dv"] for d in op["ref"]["disks"]])

    def test_the_dialogs_check_takes_the_disks_it_shows(self):
        disks = [{"virtual": 80 * 1024 ** 3, "size_gb": 80}, {"virtual": 100 * 1024 ** 3, "size_gb": 100}]
        self.cap = lh_nodes(100, 100, 100)
        self.assertEqual("bad", UVMS.room({"storage_class": "longhorn-r2", "disks": disks})["level"])
        self.assertEqual("ok", UVMS.room({"storage_class": "longhorn-r2", "disks": disks[:1]})["level"])
        for bad in ([], [{"virtual": "lots"}], [{"virtual": 0, "size_gb": 0}], [{"virtual": -1}]):
            with self.subTest(disks=bad), self.assertRaises(ValueError):
                UVMS.room({"disks": bad})
        self.assertIn(("POST", "/api/vms/import-unraid/room"), UVMS.ROUTES)
        self.assertEqual("admin", UVMS.ROUTES[("POST", "/api/vms/import-unraid/room")][0])


if __name__ == "__main__":
    unittest.main()


class RoomDefaultClassTests(unittest.TestCase):
    def test_an_unreadable_default_class_leaves_the_room_unknown_instead_of_failing(self):
        def unreadable():
            raise OSError("the API server did not answer")
        saved = (UVMS.storage_classes, UVMS.lh_capacity, UVMS.default_class)
        UVMS.storage_classes, UVMS.lh_capacity, UVMS.default_class = (lambda: []), (lambda: {}), unreadable
        try:
            room = UVMS.storage_room([{"virtual": 1024 ** 3, "size_gb": 1}])
        finally:
            UVMS.storage_classes, UVMS.lh_capacity, UVMS.default_class = saved
        self.assertEqual(("unknown", []), (room["level"], room["blockers"]))
        self.assertTrue(room["warnings"])
