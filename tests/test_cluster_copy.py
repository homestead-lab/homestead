"""A stopped destination copy, a retained source, and restartable disk transfer."""
import copy
import base64
import json
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock

import test_move_flow as fixtures
from test_move_flow import FakeNetwork, engine, source, client
import homestead_passthrough as passthrough


class ClusterCopyTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp

    def seed_vm(self, strategy="Always", running=True):
        self.cluster.put("/apis/kubevirt.io/v1", {"groupVersion": "kubevirt.io/v1"})
        vm = {"metadata": {"name": "desktop", "namespace": "lab", "uid": "source-vm-uid"},
              "spec": {"runStrategy": strategy, "dataVolumeTemplates": [{"metadata": {"name": "os-disk"}}],
                       "template": {"spec": {"domain": {"cpu": {"cores": 4}, "memory": {"guest": "8Gi"},
                           "firmware": {"uuid": "00000000-0000-0000-0000-000000000001", "bootloader": {"efi": {}}},
                           "devices": {"interfaces": [{"name": "lan", "bridge": {}, "macAddress": "02:00:00:00:00:01"}]}},
                           "networks": [{"name": "lan", "pod": {}}],
                           "volumes": [{"name": "root", "dataVolume": {"name": "os-disk"}},
                                       {"name": "data", "persistentVolumeClaim": {"claimName": "data-disk"}},
                                       {"name": "cloudinit", "cloudInitNoCloud": {"userDataSecretRef": {"name": "desktop-init"}}}]}}}}
        self.cluster.put(self.vm_path(), vm)
        if running:
            self.cluster.put(self.vmi_path(), {"metadata": {"name": "desktop"}, "status": {"phase": "Running"}})
        for name in ("os-disk", "data-disk"):
            self.cluster.put(f"/api/v1/namespaces/lab/persistentvolumeclaims/{name}", {
                "metadata": {"name": name}, "spec": {"volumeName": "pv-" + name, "volumeMode": "Block",
                "storageClassName": "longhorn-r2", "accessModes": ["ReadWriteMany"],
                "resources": {"requests": {"storage": "20Gi"}}}, "status": {"phase": "Bound"}})
            self.cluster.put("/api/v1/persistentvolumes/pv-" + name, {"spec": {
                "csi": {"driver": "driver.longhorn.io", "volumeHandle": "pv-" + name}}})
        self.cluster.put("/api/v1/namespaces/lab/secrets/desktop-init", {
            "metadata": {"name": "desktop-init"}, "data": {"userdata": "dGVzdA=="}})
        return vm

    @staticmethod
    def vm_path(ns="lab"):
        return f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/desktop"

    @staticmethod
    def vmi_path():
        return "/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/desktop"

    def settle(self, move_id, until=None):
        for _ in range(40):
            current = engine._find(move_id)
            if current["status"] != "running" or until == current["phase"]:
                return engine._public(current)
            if current["phase"] == "quiescing" and current.get("flags", {}).get("quiesced"):
                ns = current.get("source_namespace") or "lab"
                self.cluster.objects.pop(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/desktop", None)
                self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)
            engine.tick_all()
        self.fail("Copy did not settle")

    def test_multidisk_vm_copy_keeps_source_and_arrives_halted_with_new_identity(self):
        original = self.seed_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        done = self.settle(move["id"])
        self.assertEqual("succeeded", done["status"], done["message"])
        self.assertEqual("copy", done["transfer_mode"])
        self.assertFalse(done["source_stopped"])
        self.assertEqual("Always", self.cluster.get(self.vm_path())["spec"]["runStrategy"])
        copied = self.cluster.get(self.vm_path("copied"))
        self.assertEqual("Halted", copied["spec"]["runStrategy"])
        self.assertNotIn("dataVolumeTemplates", copied["spec"])
        domain = copied["spec"]["template"]["spec"]["domain"]
        self.assertEqual((4, "8Gi"), (domain["cpu"]["cores"], domain["memory"]["guest"]))
        self.assertNotEqual(original["spec"]["template"]["spec"]["domain"]["firmware"]["uuid"], domain["firmware"]["uuid"])
        self.assertNotEqual("02:00:00:00:00:01", domain["devices"]["interfaces"][0]["macAddress"])
        self.assertEqual({"os-disk", "data-disk"}, {cfg["name"] for cfg in self.lh.restored})
        self.assertTrue(all(cfg["storage_class"] == "longhorn-r2" for cfg in self.lh.restored))
        self.assertEqual({"userdata": "dGVzdA=="}, self.cluster.get("/api/v1/namespaces/copied/secrets/desktop-init")["data"])
        self.assertNotIn("definition", done)
        self.assertNotIn("secrets", done)
        with self.assertRaisesRegex(ValueError, "keeps its source"):
            engine.finish(move["id"], True)

    def hardware_vm(self):
        vm = self.seed_vm()
        spec = vm["spec"]["template"]["spec"]
        spec["domain"]["devices"]["gpus"] = [{"name": "display", "deviceName": "example.test/source-gpu"}]
        spec["nodeSelector"] = {"kubernetes.io/hostname": "source-node"}
        rom = b"\x55\xaa" + b"\x00" * 1022
        vm["spec"]["template"]["metadata"] = {"annotations": {
            "homestead.io/vbios": '["display"]',
            passthrough.HOOK_ANNOTATION: json.dumps([{"configMap": {"name": "desktop-vbios"}}])}}
        self.cluster.put(self.vm_path(), vm)
        self.cluster.put("/api/v1/namespaces/lab/configmaps/desktop-vbios", {
            "metadata": {"name": "desktop-vbios"}, "data": {passthrough.HOOK_KEY: passthrough.hook_script({"display": rom})}})
        # The simple fixture infers plurals from an 's' suffix. A ROM name also
        # ends in s, so make absent object GETs behave like Kubernetes here.
        original_get = engine.kget
        def get(path):
            if "/configmaps/" in path and path not in self.cluster.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return original_get(path)
        patch = mock.patch.object(engine, "kget", side_effect=get)
        patch.start(); self.addCleanup(patch.stop)
        resources = {"resources": [{"resource": "example.test/destination-gpu", "kind": "pci",
                                    "label": "GPU", "nodes": ["destination-node"]}]}
        for patch in (mock.patch.object(passthrough, "resources", return_value=resources),
                      mock.patch.object(passthrough, "ensure_gates")):
            patch.start(); self.addCleanup(patch.stop)
        return vm, rom

    def test_copy_maps_destination_gpu_and_carries_rom_without_changing_source(self):
        original, rom = self.hardware_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy",
                            host_devices={"display": {"resource": "example.test/destination-gpu"}})
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        copied = self.cluster.get(self.vm_path("copied"))
        self.assertEqual("example.test/destination-gpu", passthrough.vm_devices(copied)[0]["resource"])
        self.assertNotIn("kubernetes.io/hostname", copied["spec"]["template"]["spec"]["nodeSelector"])
        cm = self.cluster.get("/api/v1/namespaces/copied/configmaps/desktop-vbios")
        self.assertEqual({"display": rom}, passthrough.roms_from_configmap(copied, cm))
        self.assertTrue(engine._ours(cm, move["id"]))
        self.assertEqual(original["spec"]["template"], self.cluster.get(self.vm_path())["spec"]["template"])
        engine.abandon(move["id"])
        self.assertNotIn("/api/v1/namespaces/copied/configmaps/desktop-vbios", self.cluster.objects)
        self.assertIn("/api/v1/namespaces/lab/configmaps/desktop-vbios", self.cluster.objects)

    def test_leave_out_device_removes_its_rom_and_hook_on_copy(self):
        self.hardware_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy",
                            host_devices={"display": {"resource": ""}})
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        copied = self.cluster.get(self.vm_path("copied"))
        self.assertEqual([], passthrough.vm_devices(copied))
        self.assertNotIn(passthrough.HOOK_ANNOTATION, copied["spec"]["template"]["metadata"].get("annotations", {}))
        self.assertNotIn("/api/v1/namespaces/copied/configmaps/desktop-vbios", self.cluster.objects)

    def test_copy_can_replace_or_clear_source_vbios(self):
        self.hardware_vm()
        replacement = b"\x55\xaa" + b"\x11" * 1022
        for namespace, rom in (("replacement", base64.b64encode(replacement).decode()), ("default-rom", "")):
            move = engine.start("shed", "vm", "desktop", namespace, transfer_mode="copy",
                                host_devices={"display": {"resource": "example.test/destination-gpu", "rom": rom}})
            self.assertEqual("succeeded", self.settle(move["id"])["status"])
            copied = self.cluster.get(self.vm_path(namespace))
            self.assertEqual(bool(rom), passthrough.vm_devices(copied)[0]["rom"])
            if rom:
                cm = self.cluster.get(f"/api/v1/namespaces/{namespace}/configmaps/desktop-vbios")
                self.assertEqual({"display": replacement}, passthrough.roms_from_configmap(copied, cm))

    def test_devices_on_different_hosts_and_custom_hooks_block_before_quiesce(self):
        vm, _ = self.hardware_vm()
        vm["spec"]["template"]["spec"]["domain"]["devices"]["hostDevices"] = [{"name": "storage", "deviceName": "example.test/source-hba"}]
        self.cluster.put(self.vm_path(), vm)
        resources = {"resources": [{"resource": "example.test/destination-gpu", "kind": "pci", "nodes": ["node1"]},
                                    {"resource": "example.test/destination-hba", "kind": "pci", "nodes": ["node2"]}]}
        choices = {"display": {"resource": "example.test/destination-gpu"}, "storage": {"resource": "example.test/destination-hba"}}
        with mock.patch.object(passthrough, "resources", return_value=resources), self.assertRaisesRegex(ValueError, "No destination host"):
            engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy", host_devices=choices)
        vm["spec"]["template"]["metadata"]["annotations"][passthrough.HOOK_ANNOTATION] = '[{"image":"example.test/custom-hook"}]'
        self.cluster.put(self.vm_path(), vm)
        with self.assertRaisesRegex(ValueError, "custom hook sidecar"):
            engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy", host_devices=choices)
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_device_and_rom_problems_are_refused_before_stopping_source(self):
        self.hardware_vm()
        for choices, message in ((None, "Choose a destination device"),
                                 ({"display": {"resource": "example.test/missing"}}, "not offered"),
                                 ({"display": {"resource": "example.test/destination-gpu", "rom": "invalid"}}, "ROM file could not")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy", host_devices=choices)
        self.cluster.objects.pop("/api/v1/namespaces/lab/configmaps/desktop-vbios")
        with self.assertRaisesRegex(ValueError, "vBIOS ConfigMap is missing"):
            engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy",
                         host_devices={"display": {"resource": ""}})
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_device_mapping_changes_during_copy_queue_do_not_stop_source(self):
        self.hardware_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy",
                            host_devices={"display": {"resource": "example.test/destination-gpu"}})
        self.cluster.objects[self.vm_path()]["spec"]["template"]["spec"]["domain"]["devices"]["gpus"][0]["deviceName"] = "example.test/changed"
        result = self.settle(move["id"])
        self.assertEqual("failed", result["status"])
        self.assertIn("source passthrough devices or vBIOS changed", result["message"])
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_homestead_cloud_init_secret_ref_is_copied(self):
        vm = self.seed_vm()
        vm["spec"]["template"]["spec"]["volumes"][-1]["cloudInitNoCloud"] = {"secretRef": {"name": "desktop-init"}}
        self.cluster.put(self.vm_path(), vm)
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        self.assertEqual({"userdata": "dGVzdA=="}, self.cluster.get("/api/v1/namespaces/copied/secrets/desktop-init")["data"])

    def test_source_is_released_after_backups_before_restore_and_snapshot_survives_restart(self):
        self.seed_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.settle(move["id"], "releasing-source")
        engine.tick_all()
        self.assertEqual("Always", self.cluster.get(self.vm_path())["spec"]["runStrategy"])
        self.assertEqual([], self.lh.restored)
        self.cluster.objects[self.vm_path()]["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] = 12
        engine.bind(self.cluster.get, self.cluster.send, self.lh, client, FakeNetwork(), self.ops, self.tmp.name, "lab")
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        copied = self.cluster.get(self.vm_path("copied"))
        self.assertEqual(4, copied["spec"]["template"]["spec"]["domain"]["cpu"]["cores"])

    def test_lost_release_reply_is_idempotent_and_does_not_take_another_backup(self):
        self.seed_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.settle(move["id"], "releasing-source")
        release = source.release
        def lost(*args, **kwargs):
            release(*args, **kwargs)
            raise client.Unreachable("reply lost")
        with mock.patch.object(source, "release", side_effect=lost):
            engine.tick_all()
        self.assertEqual("releasing-source", engine._find(move["id"])["phase"])
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        self.assertEqual(2, len(self.lh.made))

    def test_container_copy_arrives_scaled_to_zero_and_source_replicas_return(self):
        move = engine.start("shed", "container", "frigate", "copied", transfer_mode="copy")
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        self.assertEqual(1, self.cluster.get("/apis/apps/v1/namespaces/lab/deployments/frigate")["spec"]["replicas"])
        self.assertEqual(0, self.cluster.get("/apis/apps/v1/namespaces/copied/deployments/frigate")["spec"]["replicas"])
        self.assertEqual("Copy frigate from shed", self.ops.started[0][1])
        self.assertNotIn("stopped original", engine.dismiss(move["id"])["detail"])

    def test_manual_source_running_and_stopped_states_are_restored(self):
        for running in (True, False):
            with self.subTest(running=running):
                self.seed_vm("Manual", running)
                move = engine.start("shed", "vm", "desktop", "copied-" + str(running).lower(), transfer_mode="copy")
                before = len(self.cluster.calls)
                self.assertEqual("succeeded", self.settle(move["id"])["status"])
                self.assertEqual("Manual", self.cluster.get(self.vm_path())["spec"]["runStrategy"])
                starts = [call for call in self.cluster.calls[before:] if call[1].endswith("/desktop/start")]
                self.assertEqual(int(running), len(starts))

    def test_source_without_copy_capability_is_refused_before_quiesce(self):
        with mock.patch.object(client, "check_cluster", return_value={"compatible": True, "capabilities": []}):
            with self.assertRaisesRegex(ValueError, "Update Homestead on the source"):
                engine.start("shed", "container", "frigate", "copied", transfer_mode="copy")
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_missing_cloud_init_secret_is_refused_before_source_stops(self):
        self.seed_vm()
        self.cluster.objects.pop("/api/v1/namespaces/lab/secrets/desktop-init")
        checked = engine.plan("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertEqual("copy", checked["transfer_mode"])
        self.assertIn("Cloud-init Secret desktop-init is missing", checked["blockers"][0])
        with self.assertRaisesRegex(ValueError, "Cloud-init Secret desktop-init is missing"):
            engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_changed_disk_size_cannot_bypass_validation_on_retry(self):
        self.seed_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.settle(move["id"], "quiescing")
        self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/os-disk"]["spec"]["resources"]["requests"]["storage"] = "30Gi"
        self.assertEqual("failed", self.settle(move["id"])["status"])
        self.assertNotIn("definition", engine._find(move["id"]))
        engine.retry(move["id"])
        self.assertEqual("failed", self.settle(move["id"])["status"])
        self.assertEqual([], self.lh.made)
        engine.abandon(move["id"])
        self.assertEqual("Always", self.cluster.get(self.vm_path())["spec"]["runStrategy"])

    def test_completed_rerun_source_keeps_its_completed_vmi(self):
        self.seed_vm("RerunOnFailure")
        self.cluster.objects[self.vmi_path()]["status"]["phase"] = "Succeeded"
        source.quiesce("vm", "desktop", "012345abcdef", "source-vm-uid")
        self.assertEqual("RerunOnFailure", self.cluster.get(self.vm_path())["spec"]["runStrategy"])
        self.assertFalse(source.status("vm", "desktop")["running"])
        source.release("vm", "desktop", "012345abcdef", "source-vm-uid")
        self.assertEqual("Succeeded", self.cluster.get(self.vmi_path())["status"]["phase"])
        self.assertFalse(any(call[1].endswith("/desktop/start") for call in self.cluster.calls))

    def test_vm_move_still_starts_destination_and_keeps_source_halted(self):
        self.seed_vm()
        move = engine.start("shed", "vm", "desktop", "moved")
        self.settle(move["id"], "starting")
        self.cluster.put("/apis/kubevirt.io/v1/namespaces/moved/virtualmachineinstances/desktop", {"status": {"phase": "Running"}})
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        self.assertEqual("Halted", self.cluster.get(self.vm_path())["spec"]["runStrategy"])
        self.assertEqual("Always", self.cluster.get(self.vm_path("moved"))["spec"]["runStrategy"])

    def test_transfers_select_source_namespace_without_touching_same_named_vm(self):
        self.seed_vm()
        paths = [self.vm_path(), self.vmi_path(), "/api/v1/namespaces/lab/secrets/desktop-init"]
        paths += [f"/api/v1/namespaces/lab/persistentvolumeclaims/{name}" for name in ("os-disk", "data-disk")]
        for path in paths:
            body = copy.deepcopy(self.cluster.objects[path])
            body.setdefault("metadata", {})["namespace"] = "guests"
            if path == self.vm_path():
                body["metadata"]["uid"] = "guest-vm-uid"
                body["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] = 6
            if "/persistentvolumeclaims/" in path:
                pv = "guest-" + body["spec"]["volumeName"]
                body["spec"]["volumeName"] = pv
                self.cluster.put("/api/v1/persistentvolumes/" + pv, {"spec": {
                    "csi": {"driver": "driver.longhorn.io", "volumeHandle": pv}}})
            self.cluster.put(path.replace("/lab/", "/guests/"), body)
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy", source_namespace="guests")
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        self.assertEqual("guests", engine._find(move["id"])["source_namespace"])
        self.assertEqual("Running", self.cluster.get(self.vmi_path())["status"]["phase"])
        self.assertEqual(4, self.cluster.get(self.vm_path())["spec"]["template"]["spec"]["domain"]["cpu"]["cores"])
        self.assertEqual(6, self.cluster.get(self.vm_path("copied"))["spec"]["template"]["spec"]["domain"]["cpu"]["cores"])
        self.assertEqual("Always", self.cluster.get(self.vm_path("guests"))["spec"]["runStrategy"])
        self.assertEqual({"guest-pv-os-disk", "guest-pv-data-disk"}, {backup["volume"] for backup in self.lh.made})
        moved = engine.start("shed", "vm", "desktop", "moved", source_namespace="guests")
        self.settle(moved["id"], "starting")
        self.cluster.put("/apis/kubevirt.io/v1/namespaces/moved/virtualmachineinstances/desktop", {"status": {"phase": "Running"}})
        self.assertEqual("succeeded", self.settle(moved["id"])["status"])
        engine.finish(moved["id"], True)
        self.assertNotIn(self.vm_path("guests"), self.cluster.objects)
        self.assertIn(self.vm_path(), self.cluster.objects)
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/os-disk", self.cluster.objects)
        self.assertNotIn("/api/v1/namespaces/guests/persistentvolumeclaims/os-disk", self.cluster.objects)

    def test_namespace_scope_is_isolated_and_resets_after_failure(self):
        barrier = Barrier(2)
        def read():
            barrier.wait(timeout=5)
            return source._namespace()
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(source.in_namespace, ns, read) for ns in ("guests", "apps")]
            self.assertEqual(["guests", "apps"], [call.result() for call in calls])
        with self.assertRaisesRegex(ValueError, "failed"):
            source.in_namespace("guests", lambda: (_ for _ in ()).throw(ValueError("failed")))
        self.assertEqual("lab", source._namespace())
        with self.assertRaisesRegex(ValueError, "valid source namespace"):
            source.in_namespace("../other", source.definition, "vm", "desktop")

    def test_old_source_ignoring_requested_namespace_is_blocked_before_quiesce(self):
        self.seed_vm()
        with mock.patch.object(source, "in_namespace", side_effect=lambda ns, call, *args, **kwargs: call(*args, **kwargs)):
            with self.assertRaisesRegex(ValueError, "selected source namespace"):
                engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy", source_namespace="guests")
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_vm_api_and_external_state_are_checked_before_stopping(self):
        self.seed_vm()
        for change, expected in [
            (lambda: self.cluster.objects.pop("/apis/kubevirt.io/v1"), "Install KubeVirt"),
            (lambda: self.cluster.objects[self.vm_path()]["spec"].update(runStrategy="Once"), "Once run strategy"),
            (lambda: self.cluster.objects[self.vm_path()]["spec"].update(instancetype={"name": "external-size"}), "external instance type or preference"),
            (lambda: self.cluster.objects[self.vm_path()]["spec"].update(preference={"name": "external-preference"}), "external instance type or preference"),
            (lambda: self.cluster.objects[self.vm_path()]["spec"]["template"]["spec"]["domain"]["devices"].update(tpm={"persistent": True}), "persistent firmware or TPM"),
            (lambda: self.cluster.objects[self.vm_path()]["spec"]["template"]["spec"]["volumes"].append({"name": "host", "hostDisk": {"path": "/external/disk"}}), "external source"),
        ]:
            original = copy.deepcopy(self.cluster.objects)
            change()
            with self.assertRaisesRegex(ValueError, expected):
                engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
            self.cluster.objects = original
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_failed_copy_cancellation_releases_lease_and_restores_source(self):
        move = engine.start("shed", "container", "frigate", "copied", transfer_mode="copy")
        self.settle(move["id"], "backing-up")
        with mock.patch.object(source, "backup", side_effect=ValueError("backup failed")):
            engine.tick_all()
        self.assertEqual("failed", engine._find(move["id"])["status"])
        with self.assertRaisesRegex(ValueError, "Cancel the failed copy"):
            engine.dismiss(move["id"])
        result = engine.abandon(move["id"])
        self.assertEqual("cancelled", result["status"])
        self.assertFalse(result["source_stopped"])
        self.assertEqual(1, self.cluster.get("/apis/apps/v1/namespaces/lab/deployments/frigate")["spec"]["replicas"])

    def test_finished_copy_can_be_removed_when_source_is_unreachable(self):
        move = engine.start("shed", "container", "frigate", "copied", transfer_mode="copy")
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        self.unreachable = True
        result = engine.abandon(move["id"])
        self.assertEqual("cancelled", result["status"])
        self.assertIn("/apis/apps/v1/namespaces/lab/deployments/frigate", self.cluster.objects)
        self.assertNotIn("/apis/apps/v1/namespaces/copied/deployments/frigate", self.cluster.objects)

    def test_lease_rejects_other_transfers_and_replaced_source(self):
        self.seed_vm()
        source.quiesce("vm", "desktop", "012345abcdef", "source-vm-uid")
        for action in (source.quiesce, source.release):
            with self.assertRaisesRegex(ValueError, "another transfer"):
                action("vm", "desktop", "abcdef012345", "source-vm-uid")
        with self.assertRaisesRegex(ValueError, "another transfer"):
            source.remove("vm", "desktop", True)
        with self.assertRaisesRegex(ValueError, "was replaced"):
            source.release("vm", "desktop", "012345abcdef", "old-uid")

    def test_terminating_vm_launcher_is_waited_for_before_backup(self):
        self.seed_vm()
        source.quiesce("vm", "desktop", "012345abcdef", "source-vm-uid")
        self.cluster.objects.pop(self.vmi_path())
        self.cluster.put("/api/v1/namespaces/lab/pods/launcher", {
            "metadata": {"name": "launcher", "deletionTimestamp": "2026-01-01T00:00:00Z"},
            "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "os-disk"}}]},
            "status": {"phase": "Running"}})
        with self.assertRaisesRegex(ValueError, "still running"):
            source.backup("vm", "desktop", transfer_id="012345abcdef", expected_uid="source-vm-uid")
        self.assertEqual([], self.lh.made)

    def test_lost_quiesce_reply_still_allows_source_recovery(self):
        self.seed_vm()
        move = engine.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.settle(move["id"], "quiescing")
        quiesce = source.quiesce
        def lost(*args, **kwargs):
            quiesce(*args, **kwargs)
            raise client.Unreachable("reply lost")
        with mock.patch.object(source, "quiesce", side_effect=lost):
            engine.tick_all()
        self.assertTrue(engine.moves()[0]["source_stopped"])
        self.assertEqual("cancelled", engine.abandon(move["id"])["status"])
        self.assertEqual("Always", self.cluster.get(self.vm_path())["spec"]["runStrategy"])

    def test_cleanup_failure_retries_cleanup_without_restarting_source_again(self):
        move = engine.start("shed", "container", "frigate", "copied", transfer_mode="copy")
        self.assertEqual("succeeded", self.settle(move["id"])["status"])
        path = "/api/v1/namespaces/copied/persistentvolumeclaims/frigate-config"
        original = self.cluster.send
        def refuse(method, target, *args, **kwargs):
            if method == "DELETE" and target == path:
                raise urllib.error.HTTPError(target, 503, "try later", {}, None)
            return original(method, target, *args, **kwargs)
        with mock.patch.object(engine, "ksend", side_effect=refuse):
            with self.assertRaises(urllib.error.HTTPError):
                engine.abandon(move["id"])
        failed = engine.moves()[0]
        self.assertEqual("failed", failed["status"])
        self.assertTrue(failed["cleanup_pending"])
        # The source can change independently after it was released.
        self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]["spec"]["replicas"] = 2
        done = engine.retry(move["id"])
        self.assertEqual("cancelled", done["status"])
        self.assertNotIn(path, self.cluster.objects)
        self.assertEqual(2, self.cluster.get("/apis/apps/v1/namespaces/lab/deployments/frigate")["spec"]["replicas"])


if __name__ == "__main__":
    unittest.main()
