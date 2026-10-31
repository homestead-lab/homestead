import copy
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_kube as K
import homestead_operations as OPS
import homestead_vms as VMS
import homestead_vm_list as VM_LIST
import homestead_vm_edit as VM_EDIT
from fakes.kube import Kube

import homestead_vm_list as VM_LIST
import homestead_vm_edit as VM_EDIT

# The shape Harvester writes: runStrategy, limits, a multus network, a disk
# claim and a cloud-init volume.
VM = {"metadata": {"name": "win11", "namespace": "default", "uid": "u1", "resourceVersion": "10", "creationTimestamp": "2026-09-01T00:00:00Z",
                   "labels": {"harvesterhci.io/os": "windows"}, "annotations": {"field.cattle.io/description": "desk"}},
      "spec": {"runStrategy": "RerunOnFailure", "template": {"spec": {
          "domain": {"cpu": {"cores": 4, "sockets": 1, "threads": 1}, "memory": {"guest": "8092Mi"},
                     "resources": {"limits": {"cpu": "4", "memory": "8Gi"}},
                     "devices": {"disks": [{"name": "disk-0", "disk": {"bus": "virtio"}, "bootOrder": 1},
                                           {"name": "cloudinitdisk", "disk": {"bus": "virtio"}}],
                                 "interfaces": [{"name": "default", "model": "virtio", "bridge": {}, "macAddress": "52:54:00:aa:bb:cc"}]}},
          "networks": [{"name": "default", "multus": {"networkName": "default/vlan1"}}],
          "volumes": [{"name": "disk-0", "persistentVolumeClaim": {"claimName": "win11-disk-0"}},
                      {"name": "cloudinitdisk", "cloudInitNoCloud": {"userData": "#cloud-config"}}]}}},
      "status": {"printableStatus": "Stopped"}}
VMI = {"status": {"phase": "Running", "nodeName": "harvester-node1",
                  "interfaces": [{"name": "default", "mac": "52:54:00:aa:bb:cc", "ipAddress": "192.0.2.50",
                                  "ipAddresses": ["192.0.2.50", "fe80::1"]}],
                  "guestOSInfo": {"prettyName": "Windows 11 Pro"},
                  "conditions": [{"type": "LiveMigratable", "status": "True"}]}}


VM_PATH = "/apis/kubevirt.io/v1/namespaces/default/virtualmachines/win11"
VMI_PATH = "/apis/kubevirt.io/v1/namespaces/default/virtualmachineinstances/win11"
CLAIM = "/api/v1/namespaces/default/persistentvolumeclaims/win11-disk-0"
DATA_VOLUME = "/apis/cdi.kubevirt.io/v1beta1/namespaces/default/datavolumes/win11-disk-0"
SUB = "/apis/subresources.kubevirt.io/v1/namespaces/default"
# Made from dataVolumeTemplates: owned by the VM.
OWNERS = [{"kind": "VirtualMachine", "uid": "u1"}, {"kind": "Other", "uid": "x"}]


def vm_object(vm):
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", **copy.deepcopy(vm)}


class Cluster:
    """A VM, its instance and disks on the shared fake API (tests/fakes/kube.py).
    The fake has no KubeVirt: its power actions (the subresources) are answered here."""

    def __init__(self, case, running=False, refuse_start=False):
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        case.enterContext(mock.patch.object(OPS, "DATA_DIR", case.enterContext(tempfile.TemporaryDirectory())))
        vm = vm_object(VM)
        if running:
            vm["status"]["printableStatus"] = "Running"
        self.kube = case.enterContext(Kube(
            vm,
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": "win11-disk-0", "namespace": "default", "ownerReferences": copy.deepcopy(OWNERS)},
             "spec": {"storageClassName": "harvester-longhorn"}, "status": {"capacity": {"storage": "80Gi"}}},
            {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
             "metadata": {"name": "win11-disk-0", "namespace": "default", "ownerReferences": copy.deepcopy(OWNERS)}}))
        if running:
            self.set_instance(VMI)

        def refuse(method, path, body):
            raise urllib.error.HTTPError(path, 400, "no", None, io.BytesIO(b'{"message":"Always does not support manual start requests"}'))
        for action in ("start", "stop", "restart"):
            self.kube.react("*", f"{SUB}/virtualmachines/win11/{action}", refuse if refuse_start and action == "start" else lambda *a: {})
        for action in ("pause", "unpause"):
            self.kube.react("*", f"{SUB}/virtualmachineinstances/win11/{action}", lambda *a: {})

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    @property
    def vm(self):
        """The VM as the cluster keeps it now."""
        return self.kube.object(VM_PATH)

    def get(self, path):
        """For files that bind their own reader: reads the fake itself, whatever homestead_kube is patched to."""
        return self.kube("GET", path)

    def send(self, method, path, body=None, ctype=None, **_):
        return self.kube(method, path, body, ctype)

    def change(self, change):
        found = self.vm
        change(found)
        self.kube.add(found)

    def set_instance(self, vmi):
        self.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                       "metadata": {"name": "win11", "namespace": "default"}, **copy.deepcopy(vmi)})


class VmTests(unittest.TestCase):
    def use(self, **kw):
        self.c = Cluster(self, **kw)
        for name in ("kget", "ksend", "events_for"):
            self.addCleanup(setattr, VMS, name, getattr(VMS, name))
        VMS.bind(K.kget, K.ksend, lambda ns, name, uid="": [{"type": "Normal", "reason": "Started", "message": "", "last": ""}])
        return self.c

    def test_a_stopped_harvester_vm_offers_start(self):
        self.use()
        v = VM_LIST.list_vms()[0]
        self.assertEqual(("Stopped", "RerunOnFailure", ["start"]), (v["status"], v["run_strategy"], v["actions"]))
        self.assertEqual(("windows", "desk", "8092Mi", 4), (v["os"], v["description"], v["memory"], v["cores"]))
        self.assertEqual([("disk-0", "disk", "win11-disk-0", "80Gi"), ("cloudinitdisk", "cloud-init", "", "")],
                         [(d["name"], d["kind"], d["claim"], d["size"]) for d in v["disks"]])
        self.assertEqual("default/vlan1", v["nics"][0]["network"])

    def crashed(self):
        c = self.use()
        c = self.use()
        c.change(lambda vm: (vm["spec"].update(runStrategy="Halted"), vm.update(status={
            "printableStatus": "CrashLoopBackOff", "startFailure": {"consecutiveFailCount": 3},
            "conditions": [{"type": "Ready", "status": "False", "reason": "",
                            "message": "Guest VM is not reported as running"}]})))
        return c

    def test_a_crashed_vm_clears_backoff_when_switched_off_without_erasing_history(self):
        c = self.crashed()
        before = copy.deepcopy(c.vm)
        row, detail = VM_LIST.list_vms()[0], VMS.detail("default", "win11")
        for view in (row, detail):
            self.assertEqual(("Stopped", ["start"], False, ""),
                             (view["status"], view["actions"], view["running"], view["problem"]))
        self.assertEqual(before["status"]["conditions"], detail["conditions"])
        self.assertEqual("Started", detail["events"][0]["reason"])
        self.assertEqual(before, c.vm, "displaying the VM must not rewrite KubeVirt status or retry history")
        self.assertEqual([], c.sent)

    def test_manual_and_legacy_stopped_vms_clear_previous_backoff(self):
        for strategy, running in (("Manual", None), (None, False)):
            with self.subTest(strategy=strategy, running=running):
                c = self.crashed()
                c.change(lambda vm: vm["spec"].pop("runStrategy"))
                if strategy:
                    c.change(lambda vm: vm["spec"].update(runStrategy=strategy))
                else:
                    c.change(lambda vm: vm["spec"].update(running=running))
                self.assertEqual("Stopped", VM_LIST.list_vms()[0]["status"])
                self.assertEqual(["start"], VMS.detail("default", "win11")["actions"])

    def test_completed_instances_are_off_but_live_and_unfinished_instances_keep_backoff(self):
        for phase in ("Succeeded", "Failed", "Running", "Pending", "Scheduling", "Scheduled", "Unknown", ""):
            with self.subTest(phase=phase):
                c = self.crashed()
                c.set_instance({"metadata": {"name": "win11", "namespace": "default", "deletionTimestamp": "2026-10-01T00:00:00Z"},
                         "status": {"phase": phase}})
                expected = "Stopped" if phase in ("Succeeded", "Failed") else "CrashLoopBackOff"
                self.assertEqual(expected, VM_LIST.list_vms()[0]["status"])
                self.assertEqual(expected, VMS.detail("default", "win11")["status"])

    def test_automatic_retries_missing_policy_and_queued_power_keep_backoff(self):
        for strategy in ("Always", "RerunOnFailure", "Once", "Unknown", None):
            with self.subTest(strategy=strategy):
                c = self.crashed()
                c.change(lambda vm: vm["spec"].pop("runStrategy"))
                if strategy:
                    c.change(lambda vm: vm["spec"].update(runStrategy=strategy))
                self.assertEqual("CrashLoopBackOff", VM_LIST.list_vms()[0]["status"])
                self.assertEqual(["stop", "force-stop"], VMS.detail("default", "win11")["actions"])
        for request in ("Start", "Stop"):
            with self.subTest(request=request):
                c = self.crashed()
                c.change(lambda vm: vm["status"].update(stateChangeRequests=[{"action": request}]))
                self.assertEqual("CrashLoopBackOff", VM_LIST.list_vms()[0]["status"])
                self.assertEqual("CrashLoopBackOff", VMS.detail("default", "win11")["status"])
        c = self.crashed()
        c.change(lambda vm: vm["metadata"].update(deletionTimestamp="2026-10-01T00:00:00Z"))
        self.assertEqual(("Deleting", []), (VM_LIST.list_vms()[0]["status"], VMS.detail("default", "win11")["actions"]))

    def test_failed_incomplete_or_malformed_instance_inventory_cannot_prove_off(self):
        for inventory in (None, {}, {"items": None}, {"items": {}}, {"items": [{}]},
                          {"items": [], "metadata": {"continue": "next-page"}}):
            with self.subTest(inventory=inventory):
                self.crashed()
                def get(path):
                    if path.endswith("/virtualmachineinstances"):
                        if inventory is None:
                            raise OSError("API unavailable")
                        return inventory
                    return K.kget(path)
                VMS.bind(get, K.ksend, lambda *a: [])
                self.assertEqual("CrashLoopBackOff", VM_LIST.list_vms()[0]["status"])

    def test_only_not_found_proves_an_instance_is_absent_on_the_vm_page(self):
        for code in (403, 404, 500):
            with self.subTest(code=code):
                self.crashed()
                def get(path):
                    if path.endswith("/virtualmachineinstances/win11"):
                        raise urllib.error.HTTPError(path, code, "unavailable", None, None)
                    return K.kget(path)
                VMS.bind(get, K.ksend, lambda *a: [])
                self.assertEqual("Stopped" if code == 404 else "CrashLoopBackOff", VMS.detail("default", "win11")["status"])
        self.crashed()
        VMS.bind(lambda path: {} if path.endswith("/virtualmachineinstances/win11") else K.kget(path), K.ksend, lambda *a: [])
        self.assertEqual("CrashLoopBackOff", VMS.detail("default", "win11")["status"])

    def test_stopping_after_a_crash_keeps_real_disk_failures_visible(self):
        c = self.crashed()
        c.change(lambda vm: vm["status"]["conditions"].append({"type": "Failure", "status": "True", "message": "DataVolume refused the disk source"}))
        for row in (VM_LIST.list_vms()[0], VMS.detail("default", "win11")):
            self.assertEqual("Stopped", row["status"])
            self.assertIn("DataVolume refused", row["problem"])
        c.change(lambda vm: vm["status"].update(conditions=[]))
        dv = {"metadata": {"namespace": "default", "name": "win11-disk-0"}, "status": {"phase": "Failed"}}
        row = VM_LIST._row(c.vm, {}, dvs={("default", "win11-disk-0"): dv})
        self.assertEqual("Stopped", row["status"])
        self.assertIn("disk win11-disk-0 has been Failed", row["problem"])

    def test_a_running_vm_shows_its_guest_and_offers_stop_restart_pause(self):
        self.use(running=True)
        v = VM_LIST.list_vms()[0]
        self.assertEqual(["console", "stop", "restart", "pause", "migrate"], v["actions"])
        self.assertEqual(("192.0.2.50", "harvester-node1", "Windows 11 Pro"), (v["ip"], v["node"], v["os"]))
        d = VMS.detail("default", "win11")
        self.assertEqual("Started", d["events"][0]["reason"])

    def test_actions_follow_every_state(self):
        self.assertEqual(["unpause", "stop", "force-stop"], VMS.actions_for("Paused"))
        self.assertEqual(["force-stop"], VMS.actions_for("Stopping"))
        self.assertEqual(["stop", "force-stop"], VMS.actions_for("ErrorUnschedulable"))

    def test_normal_starting_does_not_look_like_a_failure(self):
        vm = copy.deepcopy(VM)
        vm["status"] = {"printableStatus": "Starting", "conditions": [{
            "type": "Ready", "status": "False", "message": "Guest VM is not reported as running",
        }]}
        self.assertEqual("", VMS._problem(vm, {}))

    def test_real_scheduler_reason_wins_over_generic_ready_message(self):
        vm = copy.deepcopy(VM)
        vm["status"] = {"printableStatus": "ErrorUnschedulable", "conditions": [
            {"type": "Ready", "status": "False", "message": "Guest VM is not reported as running"},
            {"type": "PodScheduled", "status": "False",
             "message": "0/2 nodes are available: 2 Insufficient memory."},
        ]}
        self.assertEqual("0/2 nodes are available: 2 Insufficient memory.", VMS._problem(vm, {}))

    def test_display_says_where_the_picture_goes(self):
        self.assertEqual("virtual", VM_LIST._display({}))
        self.assertEqual("gpu", VM_LIST._display({"autoattachGraphicsDevice": False, "gpus": [{"name": "gpu1"}]}))
        self.assertEqual("gpu", VM_LIST._display({"autoattachGraphicsDevice": False, "hostDevices": [{"name": "hd1"}]}))
        self.assertEqual("none", VM_LIST._display({"autoattachGraphicsDevice": False}))

    def test_power_uses_subresources_without_fallback_policy_mutation(self):
        c = self.use()
        VMS.power("default", "win11", "start")
        self.assertTrue(c.sent[-1][1].endswith("/virtualmachines/win11/start"))
        VMS.power("default", "win11", "pause")
        self.assertTrue(c.sent[-1][1].endswith("/virtualmachineinstances/win11/pause"))
        VMS.power("default", "win11", "force-stop")
        self.assertEqual({"gracePeriod": 0}, c.sent[-1][2])
        # Reset: a restart that does not wait for the guest.
        self.assertEqual("win11 is being reset", VMS.power("default", "win11", "reset")["detail"])
        self.assertTrue(c.sent[-1][1].endswith("/virtualmachines/win11/restart"))
        self.assertEqual({"gracePeriodSeconds": 0}, c.sent[-1][2])
        c = self.use(refuse_start=True)
        with self.assertRaisesRegex(ValueError, "does not support manual start"):
            VMS.power("default", "win11", "start")
        self.assertEqual(1, len(c.sent), "a refused request must not trigger a different write")
        with self.assertRaises(ValueError):
            VMS.power("default", "win11", "explode")

    def test_editing_cpu_memory_and_strategy(self):
        c = self.use(running=True)
        result = VM_EDIT.edit("default", "win11", {"cores": 6, "memory": "12Gi", "run_strategy": "Always", "description": "", "restart": True})
        dom = c.vm["spec"]["template"]["spec"]["domain"]
        self.assertEqual((6, "12Gi", "6", "12Gi"), (dom["cpu"]["cores"], dom["memory"]["guest"],
                                                    dom["resources"]["limits"]["cpu"], dom["resources"]["limits"]["memory"]))
        self.assertEqual("Always", c.vm["spec"]["runStrategy"])
        self.assertNotIn("field.cattle.io/description", c.vm["metadata"]["annotations"])
        self.assertTrue(c.sent[-1][1].endswith("/restart"))
        self.assertIn("restarting now", result["detail"])
        for bad in ({"cores": 0}, {"memory": "lots"}, {"run_strategy": "Sometimes"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                VM_EDIT.edit("default", "win11", bad)

    def test_deleting_with_its_disks_spares_the_iso_library_and_other_vms_disks(self):
        c = self.use()

        def add_disks(vm):
            vol = vm["spec"]["template"]["spec"]
            vol["domain"]["devices"]["disks"] += [{"name": "cd", "cdrom": {"bus": "sata"}}, {"name": "data", "disk": {"bus": "virtio"}}]
            vol["volumes"] += [{"name": "cd", "persistentVolumeClaim": {"claimName": "iso-win11-1"}},
                               {"name": "data", "persistentVolumeClaim": {"claimName": "shared-data"}}]
        c.change(add_disks)
        for found in ({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
                       "metadata": {"name": "other", "namespace": "default"},
                       "spec": {"template": {"spec": {"volumes": [
                           {"name": "d", "persistentVolumeClaim": {"claimName": "shared-data"}}]}}}},
                      {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"name": "iso-win11-1", "namespace": "default", "labels": {"homestead.io/iso": "true"}}},
                      {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"name": "shared-data", "namespace": "default"}}):
            c.kube.add(found)
        result = VMS.delete("default", "win11", with_disks=True)
        deleted = [p for m, p, b in c.sent if m == "DELETE"]
        self.assertFalse(any("iso-win11-1" in p or "shared-data" in p for p in deleted), deleted)
        put = next(b for m, p, b in c.sent if m == "PUT")
        self.assertEqual("win11-disk-0", put["metadata"]["annotations"]["harvesterhci.io/removedPVCs"],
                         "Harvester is not asked to remove them either")
        self.assertIn("iso-win11-1, shared-data kept", result["detail"])
        self.assertTrue(c.kube.object("/api/v1/namespaces/default/persistentvolumeclaims/iso-win11-1"))
        self.assertTrue(c.kube.object("/api/v1/namespaces/default/persistentvolumeclaims/shared-data"))

    def test_a_disk_already_gone_counts_as_removed_and_one_refused_does_not(self):
        for code, count in ((404, "1 disk"), (500, "0 disks")):
            with self.subTest(code=code):
                c = self.use()
                for path in (DATA_VOLUME, CLAIM):
                    c.kube.fail(path, code, times=9, method="DELETE")
                result = VMS.delete("default", "win11", with_disks=True)
                self.assertIn(f"with {count}", result["detail"])

    def test_deleting_with_its_disks(self):
        c = self.use()
        VMS.delete("default", "win11", with_disks=True)
        put = next(b for m, p, b in c.sent if m == "PUT")
        self.assertEqual("win11-disk-0", put["metadata"]["annotations"]["harvesterhci.io/removedPVCs"])
        self.assertEqual([VM_PATH, DATA_VOLUME, CLAIM], [p for m, p, b in c.sent if m == "DELETE"],
                         "the VM, then each disk's DataVolume and claim")
        c = self.use()
        result = VMS.delete("default", "win11")
        self.assertIn("kept", result["detail"])
        # A kept disk is let go of first, or deleting its owner takes it too.
        patches = [(p, b) for m, p, b in c.sent if m == "PATCH"]
        self.assertEqual(2, len(patches))
        self.assertTrue(all(b == {"metadata": {"ownerReferences": [{"kind": "Other", "uid": "x"}]}} for p, b in patches))
        self.assertEqual(["PATCH", "PATCH", "DELETE"], [m for m, p, b in c.sent])



class UnreadVmListTests(unittest.TestCase):
    """#620: a VM list that could not be read is an error, never "no VMs" -
    the power schedules, outage actions and pages built on it would act on none.
    On the shared fake API (tests/fakes/kube.py), which answers the VM list as told."""

    def bind(self, answer, groups=("kubevirt.io",)):
        kube = self.enterContext(Kube(groups=groups))
        kube.react("GET", "/apis/kubevirt.io/v1/virtualmachines", answer)
        for name in ("kget", "ksend", "events_for"):
            self.addCleanup(setattr, VMS, name, getattr(VMS, name))
        VMS.bind(K.kget, K.ksend, lambda *a, **k: [])

    def refuse(self, fail):
        def answer(method, path, body):
            raise fail
        self.bind(answer)

    def test_a_failed_read_raises(self):
        self.refuse(urllib.error.URLError("timed out"))
        with self.assertRaises(Exception):
            VM_LIST.list_vms()

    def test_a_404_with_kubevirt_installed_raises(self):
        self.refuse(urllib.error.HTTPError("/apis/kubevirt.io/v1/virtualmachines", 404, "not found", None, io.BytesIO(b"")))
        with self.assertRaises(urllib.error.HTTPError):
            VM_LIST.list_vms()

    def test_no_kubevirt_is_no_vms(self):
        def answer(method, path, body):
            raise urllib.error.HTTPError("/apis/kubevirt.io/v1/virtualmachines", 404, "not found", None, io.BytesIO(b""))
        self.bind(answer, groups=("apps",))
        self.assertEqual([], VM_LIST.list_vms())

    def test_an_incomplete_list_raises(self):
        self.bind(lambda method, path, body: {"items": [], "metadata": {"continue": "more"}})
        with self.assertRaises(ValueError):
            VM_LIST.list_vms()


if __name__ == "__main__":
    unittest.main()
