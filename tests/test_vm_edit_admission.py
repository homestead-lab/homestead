import copy
import base64
import tempfile
import unittest
import urllib.error
from unittest import mock

import test_vm_capacity as fixtures
import server
import homestead_capacity_review as review
import homestead_passthrough as passthrough


class VMEditAdmissionTests(unittest.TestCase):
    read = fixtures.VMCapacityTests.read
    disk = fixtures.VMCapacityTests.disk
    running = fixtures.VMCapacityTests.running
    fresh_state = fixtures.VMCapacityTests.fresh_state

    def setUp(self):
        fixtures.VMCapacityTests.setUp(self)
        self.vm.update(apiVersion="kubevirt.io/v1", kind="VirtualMachine")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(server.OPS, "DATA_DIR", temporary.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.vm_path = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"
        self.objects[self.vm_path] = self.vm
        self.body = {"ns": "lab", "name": "guest", "memory": "6Gi", "restart": False}
        for patch in (mock.patch.object(server, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.VMS, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.PLACE, "get_nodes", side_effect=lambda: copy.deepcopy(self.nodes)),
                      mock.patch.object(server.PLACE, "hardware_features", return_value=[]),
                      mock.patch.object(server, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS),
                      mock.patch.object(review, "_key", return_value=b"edit-review-tests")):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, path, body):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        def expand(method, path, body):
            self.assertEqual(("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/lab/expand-vm-spec"), (method, path))
            self.assertTrue(hasattr(self, "profile_memory"), "unexpected expansion")
            result = copy.deepcopy(body)
            result["spec"]["template"]["spec"]["domain"].update(cpu={"cores": 3}, memory={"guest": self.profile_memory})
            return result
        def send(method, path, body=None, **kw):
            value = copy.deepcopy(body)
            meta = value.setdefault("metadata", {})
            meta.setdefault("namespace", "lab")
            meta.setdefault("uid", "created-uid")
            meta["resourceVersion"] = "written-version"
            if method == "PATCH":
                value.update(apiVersion="v1", kind="Secret" if "/secrets/" in path else "PersistentVolumeClaim")
                meta.setdefault("name", path.rsplit("/", 1)[-1])
            return value
        with mock.patch.object(server.VMS, "ksend", side_effect=send) as writes, \
                mock.patch.object(server, "ksend", side_effect=expand) as other_writes:
            handler.do_POST()
        if not hasattr(self, "profile_memory"):
            other_writes.assert_not_called()
        self.expansions = other_writes.call_args_list
        return handler._send.call_args.args, writes

    def reviewed(self):
        result, writes = self.call("/api/vm/edit/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertFalse(result[1]["capacity"]["blocked"], result)
        writes.assert_not_called()
        return {**self.body, "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}

    def test_unsigned_edit_writes_nothing(self):
        for body in (self.body, {**self.body, "confirm_capacity": True}):
            result, writes = self.call("/api/vm/edit", body)
            self.assertEqual(409, result[0], result)
            writes.assert_not_called()

    def running_passthrough(self):
        gpu, usb = "example.test/gpu", "example.test/usb"
        self.vm["spec"]["runStrategy"] = "Always"
        self.vm["spec"]["template"]["spec"]["domain"]["devices"] = {"hostDevices": [
            {"name": "gpu", "deviceName": gpu}, {"name": "keyboard", "deviceName": usb}]}
        self.nodes[0]["allocatable"].update({gpu: "1", usb: "1"})
        self.config["spec"]["configuration"]["permittedHostDevices"] = {
            "pciHostDevices": [{"resourceName": gpu}], "usb": [{"resourceName": usb}]}
        instance, launcher = self.running()
        launcher["spec"]["containers"][0]["resources"]["requests"].update({gpu: "1", usb: "1"})
        return instance, launcher

    def test_reviewed_running_edit_keeps_its_exclusive_gpu_and_usb(self):
        instance, _ = self.running_passthrough()
        before = copy.deepcopy(instance)
        result, writes = self.call("/api/vm/edit", self.reviewed())
        self.assertEqual(200, result[0], result)
        self.assertEqual(1, writes.call_count)
        self.assertIn("/virtualmachines/guest", writes.call_args.args[1])
        saved = writes.call_args.args[2]
        self.assertEqual("6Gi", saved["spec"]["template"]["spec"]["domain"]["memory"]["guest"])
        self.assertEqual(self.vm["spec"]["template"]["spec"]["domain"]["devices"],
                         saved["spec"]["template"]["spec"]["domain"]["devices"])
        self.assertEqual(before, instance, "Save does not write or restart the running instance")

    def test_changed_resident_allocation_invalidates_running_edit_review(self):
        _, launcher = self.running_passthrough()
        body = self.reviewed()
        launcher["metadata"]["resourceVersion"] = "3"
        del launcher["spec"]["containers"][0]["resources"]["requests"]["example.test/gpu"]
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_resident_device_credit_is_rechecked_immediately_before_save(self):
        _, launcher = self.running_passthrough()
        body = self.reviewed()
        original = server.VMS.commit_edit
        def commit(prepared, before_save=None, send=None):
            del launcher["spec"]["containers"][0]["resources"]["requests"]["example.test/gpu"]
            return original(prepared, before_save=before_save, send=send)
        with mock.patch.object(server.VMS, "commit_edit", side_effect=commit):
            result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def passthrough_setup(self):
        resource = "example.test/gpu"
        self.nodes[0]["allocatable"][resource] = "1"
        self.config["spec"]["configuration"].update(
            permittedHostDevices={"pciHostDevices": [{"resourceName": resource}]},
            developerConfiguration={"featureGates": ["Sidecar"]})
        self.body["host_devices"] = {"add": [{"name": "gpu", "resource": resource}],
            "roms": {"gpu": base64.b64encode(b"\x55\xaa" + b"\x00" * 1022).decode()}}
        for patch in (mock.patch.object(passthrough, "resources", return_value={"resources": [{"resource": resource}]}),
                      mock.patch.object(passthrough, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(passthrough, "_kubevirt", side_effect=lambda: copy.deepcopy(self.config))):
            patch.start(); self.addCleanup(patch.stop)

    def test_add_device_and_rom_in_one_reviewed_edit(self):
        self.passthrough_setup()
        result, writes = self.call("/api/vm/edit", self.reviewed())
        self.assertEqual(200, result[0], result)
        vm = next(c.args[2] for c in writes.call_args_list if "/virtualmachines/" in c.args[1])
        cm = next(c.args[2] for c in writes.call_args_list if c.args[1].endswith("/configmaps"))
        self.assertEqual({"gpu"}, set(passthrough.roms_from_configmap(vm, cm)))

    def test_vbios_collision_after_edit_review_does_not_write(self):
        self.passthrough_setup()
        body = self.reviewed()
        self.objects["/api/v1/namespaces/lab/configmaps/guest-vbios"] = {
            "metadata": {"name": "guest-vbios", "namespace": "lab", "uid": "other", "resourceVersion": "1"}}
        result, writes = self.call("/api/vm/edit", body)
        self.assertIn(result[0], (400, 409), result)
        writes.assert_not_called()

    def test_missing_rom_can_be_replaced_explicitly_but_not_silently_dropped(self):
        self.passthrough_setup()
        self.vm["spec"]["template"]["spec"]["domain"].setdefault("devices", {})["hostDevices"] = [{"name": "gpu", "deviceName": "example.test/gpu"}]
        self.vm["spec"]["template"].setdefault("metadata", {})["annotations"] = {"homestead.io/vbios": '["gpu"]'}
        self.body["host_devices"].pop("add")
        roms = self.body["host_devices"].pop("roms")
        self.body["host_devices"]["map"] = {"gpu": "example.test/gpu"}
        refused, writes = self.call("/api/vm/edit/preview", self.body)
        self.assertEqual(400, refused[0], refused)
        writes.assert_not_called()
        self.body["host_devices"]["roms"] = roms
        result, writes = self.call("/api/vm/edit", self.reviewed())
        self.assertEqual(200, result[0], result)
        self.assertTrue(any(c.args[1].endswith("/configmaps") for c in writes.call_args_list))

    def test_clearing_rom_empties_owned_configmap_with_version_check(self):
        self.passthrough_setup()
        effects = []
        passthrough.edit_vm(self.vm, "lab", self.body["host_devices"], effects)
        cm = next(e["body"] for e in effects if e["kind"] == "configmap")
        cm["metadata"].update(uid="rom-cm-uid", resourceVersion="old-version")
        self.objects["/api/v1/namespaces/lab/configmaps/guest-vbios"] = cm
        self.body["host_devices"] = {"roms": {"gpu": ""}}
        result, writes = self.call("/api/vm/edit", self.reviewed())
        self.assertEqual(200, result[0], result)
        saved = next(c.args[2] for c in writes.call_args_list if "/configmaps/" in c.args[1])
        self.assertEqual({}, saved["data"])
        self.assertEqual("old-version", saved["metadata"]["resourceVersion"])
        self.assertNotIn("DELETE", [c.args[0] for c in writes.call_args_list])

    def test_missing_state_edit_requires_typed_initialization_consent(self):
        self.fresh_state()
        signed = self.reviewed()
        result, writes = self.call("/api/vm/edit", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        self.assertFalse(server.OPS._read())
        result, writes = self.call("/api/vm/edit", {**signed, "ack_state_initialization": True, "confirm_state_name": "guest"})
        self.assertEqual(200, result[0], result)
        self.assertEqual(1, writes.call_count)
        self.assertTrue(server.OPS._read()[0]["ref"]["state_initialization_acknowledged"])

    def test_outer_label_change_cannot_skip_admission_as_metadata_only(self):
        prepared = server.prepare_vm_edit({"ns": "lab", "name": "guest", "description": "edited"})
        plan, _, _ = server.vm_edit_capacity(prepared)
        self.assertFalse(plan["vm"]["admission_needed"])
        prepared["vm"]["metadata"]["labels"] = {"backup": "yes"}
        plan, _, _ = server.vm_edit_capacity(prepared)
        self.assertTrue(plan["vm"]["admission_needed"])

    def disk_setup(self, platform):
        self.body = {"ns": "lab", "name": "guest", "add_disks": [{"size": "2Gi", "storage_class": "storage"}]}
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims"] = {"items": []}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/storage"] = {
            "metadata": {"name": "storage", "uid": "sc-uid", "resourceVersion": "1"},
            "provisioner": "example.test/storage", "volumeBindingMode": "WaitForFirstConsumer"}
        self.objects["/apis/cdi.kubevirt.io/v1beta1/storageprofiles/storage"] = {
            "metadata": {"name": "storage", "uid": "profile-uid", "resourceVersion": "1"},
            "status": {"claimPropertySets": [{"accessModes": ["ReadWriteOnce"], "volumeMode": "Block"}]}}
        patch = mock.patch.object(server.VMS, "platform", return_value=platform)
        patch.start()
        self.addCleanup(patch.stop)

    def test_cdi_edit_pins_new_disk_modes_and_preserves_existing_templates(self):
        self.disk_setup({"harvester": False, "cdi": True})
        self.disk()
        old = {"metadata": {"name": "root"}, "spec": {"storage": {"storageClassName": "old", "resources": {"requests": {"storage": "20Gi"}}}}}
        self.vm["spec"]["dataVolumeTemplates"] = [copy.deepcopy(old)]
        body = self.reviewed()
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(200, result[0], result)
        templates = writes.call_args.args[2]["spec"]["dataVolumeTemplates"]
        self.assertEqual(old, templates[0])
        storage = templates[1]["spec"]["storage"]
        self.assertEqual(["ReadWriteOnce"], storage["accessModes"])
        self.assertEqual("Block", storage["volumeMode"])
        self.assertEqual("storage", storage["storageClassName"])

    def test_plain_edit_pins_the_posted_claim_not_only_the_vm_model(self):
        self.disk_setup({"harvester": False, "cdi": False})
        body = self.reviewed()
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(200, result[0], result)
        claim = next(c.args[2] for c in writes.call_args_list if c.args[0] == "POST")
        self.assertEqual("Filesystem", claim["spec"]["volumeMode"])
        self.assertEqual(["ReadWriteOnce"], claim["spec"]["accessModes"])

    def test_profile_mode_change_before_edit_writes_requires_new_review(self):
        self.disk_setup({"harvester": False, "cdi": True})
        body = self.reviewed()
        original = server.VMS.commit_edit
        def commit(prepared, before_save=None, send=None):
            profile = self.objects["/apis/cdi.kubevirt.io/v1beta1/storageprofiles/storage"]
            profile["metadata"]["resourceVersion"] = "2"
            profile["status"]["claimPropertySets"][0]["volumeMode"] = "Filesystem"
            return original(prepared, before_save=before_save, send=send)
        with mock.patch.object(server.VMS, "commit_edit", side_effect=commit):
            result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_edit_context_keeps_reviewed_manifest_snapshot(self):
        self.disk_setup({"harvester": False, "cdi": True})
        prepared = server.prepare_vm_edit(self.body)
        _, _, context = server.vm_edit_capacity(prepared)
        prepared["vm"]["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]["volumeMode"] = "Filesystem"
        self.assertEqual("Block", context["prepared"]["vm"]["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]["volumeMode"])

    def profile_setup(self):
        self.vm["spec"]["instancetype"] = {"name": "guest-size", "kind": "VirtualMachineClusterInstancetype"}
        self.vm["spec"]["template"]["spec"]["domain"] = {"devices": {"interfaces": []}}
        self.profile_memory = "6Gi"
        self.body = {"ns": "lab", "name": "guest", "node": "node1"}

    def test_profile_edit_expands_proposed_vm_not_current_saved_spec(self):
        self.profile_setup()
        result, writes = self.call("/api/vm/edit/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertEqual(6, result[1]["capacity"]["vm"]["guest_memory_gb"])
        submitted = self.expansions[0].args[2]
        self.assertEqual({"kubernetes.io/hostname": "node1"}, submitted["spec"]["template"]["spec"]["nodeSelector"])
        self.assertNotIn("nodeSelector", self.vm["spec"]["template"]["spec"])
        self.assertNotIn("memory", self.vm["spec"]["template"]["spec"]["domain"])
        writes.assert_not_called()
        result, writes = self.call("/api/vm/edit", self.reviewed())
        self.assertEqual(200, result[0], result)
        self.assertEqual(2, len(self.expansions))
        self.assertNotIn("memory", writes.call_args.args[2]["spec"]["template"]["spec"]["domain"])
        self.assertEqual("guest-size", writes.call_args.args[2]["spec"]["instancetype"]["name"])

    def test_profile_changes_invalidate_review_and_post_preparation_gate(self):
        self.profile_setup()
        body = self.reviewed()
        self.profile_memory = "8Gi"
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        body = self.reviewed()
        original = server.VMS.commit_edit
        def commit(prepared, before_save=None, send=None):
            self.profile_memory = "7Gi"
            return original(prepared, before_save=before_save, send=send)
        with mock.patch.object(server.VMS, "commit_edit", side_effect=commit):
            result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        self.assertIn("profile expansion changed", result[1]["error"])
        writes.assert_not_called()

    def test_instance_type_cpu_memory_overrides_are_rejected_without_writes(self):
        self.profile_setup()
        for changes in ({"cores": 3}, {"memory": "6Gi"}):
            result, writes = self.call("/api/vm/edit/preview", {**self.body, **changes})
            self.assertEqual(400, result[0], result)
            self.assertIn("controlled by", result[1]["error"])
            self.assertEqual([], self.expansions)
            writes.assert_not_called()

    def test_profile_vm_metadata_and_stop_changes_do_not_need_expansion(self):
        self.profile_setup()
        self.nodes = []
        for changes in ({"description": "kept"}, {"run_strategy": "Halted"}):
            self.body = {"ns": "lab", "name": "guest", **changes}
            result, writes = self.call("/api/vm/edit", self.reviewed())
            self.assertEqual(200, result[0], result)
            self.assertEqual([], self.expansions)
            self.assertEqual(1, writes.call_count)

    def test_memory_edit_reviews_proposed_resources_and_never_restarts(self):
        body = self.reviewed()
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(200, result[0], result)
        self.assertEqual(1, writes.call_count)
        self.assertEqual(self.vm_path, writes.call_args.args[1])
        vm = writes.call_args.args[2]
        self.assertEqual("6Gi", vm["spec"]["template"]["spec"]["domain"]["memory"]["guest"])
        self.assertEqual("1", vm["metadata"]["resourceVersion"])

    def test_halted_to_always_needs_full_capacity_even_without_resource_change(self):
        self.vm["spec"]["runStrategy"] = "Halted"
        self.body = {"ns": "lab", "name": "guest", "run_strategy": "Always"}
        body = self.reviewed()
        self.nodes[0]["allocatable"]["memory"] = "1Gi"
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_metadata_and_stop_policy_do_not_require_working_capacity_inventory(self):
        self.nodes = []
        self.config = {}
        self.vm["spec"]["runStrategy"] = "Always"
        for changes in ({"description": "new description"}, {"run_strategy": "Halted"}, {"run_strategy": "Manual"}):
            self.body = {"ns": "lab", "name": "guest", **changes}
            body = self.reviewed()
            result, writes = self.call("/api/vm/edit", body)
            self.assertEqual(200, result[0], result)
            self.assertEqual(1, writes.call_count)

    def test_edited_input_vm_identity_and_policy_invalidate_approval(self):
        body = self.reviewed()
        for change in ({"memory": "8Gi"}, {"run_strategy": "Always"}, {"description": "changed"}):
            result, writes = self.call("/api/vm/edit", {**body, **change})
            self.assertEqual(409, result[0], result)
            writes.assert_not_called()
        self.vm["metadata"]["resourceVersion"] = "2"
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_restart_flag_cannot_bypass_separate_power_review(self):
        body = self.reviewed()
        result, writes = self.call("/api/vm/edit", {**body, "restart": True})
        self.assertEqual(400, result[0], result)
        self.assertIn("review Restart separately", result[1]["error"])
        writes.assert_not_called()

    def test_fresh_capacity_change_before_dependency_writes_blocks(self):
        body = self.reviewed()
        original = server.VMS.commit_edit
        def commit(prepared, before_save=None, send=None):
            self.nodes[0]["allocatable"]["memory"] = "1Gi"
            return original(prepared, before_save=before_save, send=send)
        with mock.patch.object(server.VMS, "commit_edit", side_effect=commit):
            result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_live_update_still_needs_capacity_without_restart(self):
        self.running()
        body = self.reviewed()
        self.nodes[0]["allocatable"]["memory"] = "1Gi"
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_live_edit_cannot_use_another_hosts_capacity_or_credit_old_launcher(self):
        _, pod = self.running()
        second = copy.deepcopy(self.nodes[0])
        second["name"] = "node2"
        self.nodes.append(second)
        self.nodes[0]["allocatable"]["memory"] = "8Gi"
        # Old launcher requests 4.5 GiB. The proposed 6 GiB must not be
        # admitted using released capacity or the spare second host.
        result, writes = self.call("/api/vm/edit/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertTrue(result[1]["capacity"]["blocked"])
        writes.assert_not_called()

    def test_live_host_change_requires_stop_instead_of_claiming_save_moves_vm(self):
        self.running()
        self.body["node"] = "node2"
        result, writes = self.call("/api/vm/edit/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertTrue(result[1]["capacity"]["blocked"])
        self.assertIn("Stop the running VM", " ".join(result[1]["capacity"]["blockers"]))
        writes.assert_not_called()

    def test_high_ram_warning_can_be_acknowledged_but_not_missing_hardware(self):
        self.nodes[0]["mem_used_gb"] = 15
        body = self.reviewed()
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(200, result[0], result)
        self.assertEqual(1, writes.call_count)
        del self.nodes[0]["allocatable"]["devices.kubevirt.io/kvm"]
        result, writes = self.call("/api/vm/edit", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_secret_payload_not_returned_and_failure_never_restarted_or_deleted(self):
        self.vm["spec"]["template"]["spec"]["volumes"] = [{"name": "ci", "cloudInitNoCloud": {"secretRef": {"name": "ci"}}}]
        self.objects["/api/v1/namespaces/lab/secrets/ci"] = {"metadata": {"name": "ci", "uid": "secret-uid", "resourceVersion": "5"}, "data": {}}
        self.body["cloud_init"] = {"user_data": "test-only-private-data"}
        result, writes = self.call("/api/vm/edit/preview", self.body)
        self.assertNotIn("test-only-private-data", str(result))
        body = self.reviewed()
        original = server.VMS.commit_edit
        sent = []
        def commit(prepared, before_save=None, send=None):
            def transport(method, path, body=None, **kw):
                sent.append((method, path))
                if method == "PUT":
                    raise urllib.error.HTTPError(path, 500, "uncertain", {}, None)
                return {**body, "apiVersion": "v1", "kind": "Secret", "metadata": {**body["metadata"], "namespace": "lab", "name": "ci", "resourceVersion": "6"}}
            send.send = transport
            return original(prepared, before_save=before_save, send=send)
        with mock.patch.object(server.VMS, "commit_edit", side_effect=commit):
            result, _ = self.call("/api/vm/edit", body)
        self.assertNotEqual(200, result[0])
        self.assertEqual(["PATCH", "PUT"], [method for method, _ in sent])
        self.assertFalse(any("/restart" in path for _, path in sent))


if __name__ == "__main__":
    unittest.main()
