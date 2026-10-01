import copy
import io
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_vms as VMS

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
PVCS = {"items": [{"metadata": {"name": "win11-disk-0"}, "spec": {"storageClassName": "harvester-longhorn"},
                   "status": {"capacity": {"storage": "80Gi"}}}]}


class Cluster:
    def __init__(self, running=False, refuse_start=False):
        self.vm = copy.deepcopy(VM)
        if running:
            self.vm["status"]["printableStatus"] = "Running"
        self.vmi = copy.deepcopy(VMI) if running else None
        self.refuse_start = refuse_start
        self.sent = []

    def get(self, path):
        if path.endswith("/virtualmachines"):
            return {"items": [copy.deepcopy(self.vm)]}
        if path.endswith("/virtualmachineinstances"):
            return {"items": [dict(copy.deepcopy(self.vmi), metadata={"name": "win11", "namespace": "default"})] if self.vmi else []}
        if path.endswith("/virtualmachines/win11"):
            return copy.deepcopy(self.vm)
        if path.endswith("/virtualmachineinstances/win11"):
            if not self.vmi:
                raise urllib.error.HTTPError(path, 404, "gone", None, None)
            return copy.deepcopy(self.vmi)
        if path.endswith("/persistentvolumeclaims"):
            return PVCS
        if path.endswith("/datavolumes/win11-disk-0") or path.endswith("/persistentvolumeclaims/win11-disk-0"):
            # Made from dataVolumeTemplates: owned by the VM.
            return {"metadata": {"ownerReferences": [{"kind": "VirtualMachine", "uid": "u1"}, {"kind": "Other", "uid": "x"}]}}
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if self.refuse_start and path.endswith("/start"):
            raise urllib.error.HTTPError(path, 400, "no", None, io.BytesIO(b'{"message":"Always does not support manual start requests"}'))
        if method == "PUT" and path.endswith("/virtualmachines/win11"):
            self.vm = body
        return body


class VmTests(unittest.TestCase):
    def use(self, **kw):
        self.c = Cluster(**kw)
        VMS.bind(self.c.get, self.c.send, lambda ns, name, uid="": [{"type": "Normal", "reason": "Started", "message": "", "last": ""}])
        return self.c

    def test_a_stopped_harvester_vm_offers_start(self):
        self.use()
        v = VMS.list_vms()[0]
        self.assertEqual(("Stopped", "RerunOnFailure", ["start"]), (v["status"], v["run_strategy"], v["actions"]))
        self.assertEqual(("windows", "desk", "8092Mi", 4), (v["os"], v["description"], v["memory"], v["cores"]))
        self.assertEqual([("disk-0", "disk", "win11-disk-0", "80Gi"), ("cloudinitdisk", "cloud-init", "", "")],
                         [(d["name"], d["kind"], d["claim"], d["size"]) for d in v["disks"]])
        self.assertEqual("default/vlan1", v["nics"][0]["network"])

    def crashed(self):
        c = self.use()
        c.vm["spec"]["runStrategy"] = "Halted"
        c.vm["status"] = {"printableStatus": "CrashLoopBackOff",
                          "startFailure": {"consecutiveFailCount": 3},
                          "conditions": [{"type": "Ready", "status": "False", "reason": "",
                                          "message": "Guest VM is not reported as running"}]}
        return c

    def test_a_crashed_vm_clears_backoff_when_switched_off_without_erasing_history(self):
        c = self.crashed()
        before = copy.deepcopy(c.vm)
        row, detail = VMS.list_vms()[0], VMS.detail("default", "win11")
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
                c.vm["spec"].pop("runStrategy")
                if strategy:
                    c.vm["spec"]["runStrategy"] = strategy
                else:
                    c.vm["spec"]["running"] = running
                self.assertEqual("Stopped", VMS.list_vms()[0]["status"])
                self.assertEqual(["start"], VMS.detail("default", "win11")["actions"])

    def test_completed_instances_are_off_but_live_and_unfinished_instances_keep_backoff(self):
        for phase in ("Succeeded", "Failed", "Running", "Pending", "Scheduling", "Scheduled", "Unknown", ""):
            with self.subTest(phase=phase):
                c = self.crashed()
                c.vmi = {"metadata": {"name": "win11", "namespace": "default", "deletionTimestamp": "2026-10-01T00:00:00Z"},
                         "status": {"phase": phase}}
                expected = "Stopped" if phase in ("Succeeded", "Failed") else "CrashLoopBackOff"
                self.assertEqual(expected, VMS.list_vms()[0]["status"])
                self.assertEqual(expected, VMS.detail("default", "win11")["status"])

    def test_automatic_retries_missing_policy_and_queued_power_keep_backoff(self):
        for strategy in ("Always", "RerunOnFailure", "Once", "Unknown", None):
            with self.subTest(strategy=strategy):
                c = self.crashed()
                c.vm["spec"].pop("runStrategy")
                if strategy:
                    c.vm["spec"]["runStrategy"] = strategy
                self.assertEqual("CrashLoopBackOff", VMS.list_vms()[0]["status"])
                self.assertEqual(["stop", "force-stop"], VMS.detail("default", "win11")["actions"])
        for request in ("Start", "Stop"):
            with self.subTest(request=request):
                c = self.crashed()
                c.vm["status"]["stateChangeRequests"] = [{"action": request}]
                self.assertEqual("CrashLoopBackOff", VMS.list_vms()[0]["status"])
                self.assertEqual("CrashLoopBackOff", VMS.detail("default", "win11")["status"])
        c = self.crashed()
        c.vm["metadata"]["deletionTimestamp"] = "2026-10-01T00:00:00Z"
        self.assertEqual(("Deleting", []), (VMS.list_vms()[0]["status"], VMS.detail("default", "win11")["actions"]))

    def test_failed_incomplete_or_malformed_instance_inventory_cannot_prove_off(self):
        for inventory in (None, {}, {"items": None}, {"items": {}}, {"items": [{}]},
                          {"items": [], "metadata": {"continue": "next-page"}}):
            with self.subTest(inventory=inventory):
                c = self.crashed()
                def get(path):
                    if path.endswith("/virtualmachineinstances"):
                        if inventory is None:
                            raise OSError("API unavailable")
                        return inventory
                    return c.get(path)
                VMS.bind(get, c.send, lambda *a: [])
                self.assertEqual("CrashLoopBackOff", VMS.list_vms()[0]["status"])

    def test_only_not_found_proves_an_instance_is_absent_on_the_vm_page(self):
        for code in (403, 404, 500):
            with self.subTest(code=code):
                c = self.crashed()
                def get(path):
                    if path.endswith("/virtualmachineinstances/win11"):
                        raise urllib.error.HTTPError(path, code, "unavailable", None, None)
                    return c.get(path)
                VMS.bind(get, c.send, lambda *a: [])
                self.assertEqual("Stopped" if code == 404 else "CrashLoopBackOff", VMS.detail("default", "win11")["status"])
        c = self.crashed()
        VMS.bind(lambda path: {} if path.endswith("/virtualmachineinstances/win11") else c.get(path), c.send, lambda *a: [])
        self.assertEqual("CrashLoopBackOff", VMS.detail("default", "win11")["status"])

    def test_stopping_after_a_crash_keeps_real_disk_failures_visible(self):
        c = self.crashed()
        c.vm["status"]["conditions"].append({"type": "Failure", "status": "True", "message": "DataVolume refused the disk source"})
        for row in (VMS.list_vms()[0], VMS.detail("default", "win11")):
            self.assertEqual("Stopped", row["status"])
            self.assertIn("DataVolume refused", row["problem"])
        c.vm["status"]["conditions"] = []
        dv = {"metadata": {"namespace": "default", "name": "win11-disk-0"}, "status": {"phase": "Failed"}}
        row = VMS._row(c.vm, {}, dvs={("default", "win11-disk-0"): dv})
        self.assertEqual("Stopped", row["status"])
        self.assertIn("disk win11-disk-0 has been Failed", row["problem"])

    def test_a_running_vm_shows_its_guest_and_offers_stop_restart_pause(self):
        self.use(running=True)
        v = VMS.list_vms()[0]
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

    def test_power_uses_subresources_without_fallback_policy_mutation(self):
        c = self.use()
        VMS.power("default", "win11", "start")
        self.assertTrue(c.sent[-1][1].endswith("/virtualmachines/win11/start"))
        VMS.power("default", "win11", "pause")
        self.assertTrue(c.sent[-1][1].endswith("/virtualmachineinstances/win11/pause"))
        VMS.power("default", "win11", "force-stop")
        self.assertEqual({"gracePeriod": 0}, c.sent[-1][2])
        c = self.use(refuse_start=True)
        with self.assertRaisesRegex(ValueError, "does not support manual start"):
            VMS.power("default", "win11", "start")
        self.assertEqual(1, len(c.sent), "a refused request must not trigger a different write")
        with self.assertRaises(ValueError):
            VMS.power("default", "win11", "explode")

    def test_editing_cpu_memory_and_strategy(self):
        c = self.use(running=True)
        result = VMS.edit("default", "win11", {"cores": 6, "memory": "12Gi", "run_strategy": "Always", "description": "", "restart": True})
        dom = c.vm["spec"]["template"]["spec"]["domain"]
        self.assertEqual((6, "12Gi", "6", "12Gi"), (dom["cpu"]["cores"], dom["memory"]["guest"],
                                                    dom["resources"]["limits"]["cpu"], dom["resources"]["limits"]["memory"]))
        self.assertEqual("Always", c.vm["spec"]["runStrategy"])
        self.assertNotIn("field.cattle.io/description", c.vm["metadata"]["annotations"])
        self.assertTrue(c.sent[-1][1].endswith("/restart"))
        self.assertIn("restarting now", result["detail"])
        for bad in ({"cores": 0}, {"memory": "lots"}, {"run_strategy": "Sometimes"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                VMS.edit("default", "win11", bad)

    def test_deleting_with_its_disks_spares_the_iso_library_and_other_vms_disks(self):
        c = self.use()
        vol = c.vm["spec"]["template"]["spec"]
        vol["domain"]["devices"]["disks"] += [{"name": "cd", "cdrom": {"bus": "sata"}}, {"name": "data", "disk": {"bus": "virtio"}}]
        vol["volumes"] += [{"name": "cd", "persistentVolumeClaim": {"claimName": "iso-win11-1"}},
                           {"name": "data", "persistentVolumeClaim": {"claimName": "shared-data"}}]
        other = {"metadata": {"name": "other"}, "spec": {"template": {"spec": {"volumes": [
            {"name": "d", "persistentVolumeClaim": {"claimName": "shared-data"}}]}}}}
        pvcs = {"items": PVCS["items"] + [
            {"metadata": {"name": "iso-win11-1", "labels": {"homestead.io/iso": "true"}}},
            {"metadata": {"name": "shared-data"}}]}
        get = c.get
        c.get = lambda path: ({"items": [copy.deepcopy(c.vm), other]} if path.endswith("/virtualmachines")
                              else pvcs if path.endswith("/persistentvolumeclaims")
                              else {"metadata": {}} if path.endswith(("/persistentvolumeclaims/shared-data", "/persistentvolumeclaims/iso-win11-1"))
                              else get(path))
        VMS.bind(c.get, c.send, lambda *a, **k: [])
        result = VMS.delete("default", "win11", with_disks=True)
        deleted = [p for m, p, b in c.sent if m == "DELETE"]
        self.assertFalse(any("iso-win11-1" in p or "shared-data" in p for p in deleted), deleted)
        put = next(b for m, p, b in c.sent if m == "PUT")
        self.assertEqual("win11-disk-0", put["metadata"]["annotations"]["harvesterhci.io/removedPVCs"],
                         "Harvester is not asked to remove them either")
        self.assertIn("iso-win11-1, shared-data kept", result["detail"])

    def test_deleting_with_its_disks(self):
        c = self.use()
        VMS.delete("default", "win11", with_disks=True)
        put = next(b for m, p, b in c.sent if m == "PUT")
        self.assertEqual("win11-disk-0", put["metadata"]["annotations"]["harvesterhci.io/removedPVCs"])
        self.assertEqual(["DELETE", "DELETE"], [m for m, p, b in c.sent if m == "DELETE"])
        c = self.use()
        result = VMS.delete("default", "win11")
        self.assertIn("kept", result["detail"])
        # A kept disk is let go of first, or deleting its owner takes it too.
        patches = [(p, b) for m, p, b in c.sent if m == "PATCH"]
        self.assertEqual(2, len(patches))
        self.assertTrue(all(b == {"metadata": {"ownerReferences": [{"kind": "Other", "uid": "x"}]}} for p, b in patches))
        self.assertEqual(["PATCH", "PATCH", "DELETE"], [m for m, p, b in c.sent])


if __name__ == "__main__":
    unittest.main()
