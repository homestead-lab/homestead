import copy
import unittest
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

import test_vm_capacity as fixtures
import server
import homestead_capacity_review as review


class VMPowerAdmissionTests(unittest.TestCase):
    read = fixtures.VMCapacityTests.read
    running = fixtures.VMCapacityTests.running
    disk = fixtures.VMCapacityTests.disk
    persistent_state = fixtures.VMCapacityTests.persistent_state
    fresh_state = fixtures.VMCapacityTests.fresh_state
    encrypted = fixtures.VMCapacityTests.encrypted
    numa_host = fixtures.VMCapacityTests.numa_host

    def setUp(self):
        fixtures.VMCapacityTests.setUp(self)
        self.vm["spec"]["runStrategy"] = "Halted"
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"] = self.vm
        self.body = {"ns": "lab", "name": "guest", "action": "start"}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (mock.patch.object(server, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.VMS, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.OPS, "DATA_DIR", self.tmp.name),
                      mock.patch.object(server.PLACE, "get_nodes", side_effect=lambda: copy.deepcopy(self.nodes)),
                      mock.patch.object(server.PLACE, "hardware_features", return_value=[]),
                      mock.patch.object(server, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS),
                      mock.patch.object(review, "_key", return_value=b"vm-review-tests")):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, path, body, error=None):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.VMS, "ksend", return_value={}, side_effect=error) as vm_writes, \
                mock.patch.object(server, "ksend") as other_writes:
            handler.do_POST()
        other_writes.assert_not_called()
        return handler._send.call_args.args, vm_writes

    def reviewed(self):
        result, writes = self.call("/api/vm/power/preview", self.body)
        self.assertEqual(200, result[0], result)
        writes.assert_not_called()
        return {**self.body, "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}

    def test_preview_is_read_only_and_explains_start_policy_change(self):
        result, writes = self.call("/api/vm/power/preview", self.body)
        self.assertEqual(200, result[0])
        self.assertEqual("Halted", result[1]["capacity"]["vm"]["policy_before"])
        self.assertEqual("Always", result[1]["capacity"]["vm"]["policy_after"])
        writes.assert_not_called()

    def gpu(self):
        import test_vm_device_usage as devices
        devices.DeviceAdmissionTests.setUp(self)
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"] = self.vm

    def test_device_acquired_after_review_returns_holder_and_sends_no_start(self):
        import test_vm_device_usage as devices
        self.gpu()
        signed = self.reviewed()
        devices.DeviceAdmissionTests.holder(self)
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        self.assertIn("VM lab/other", result[1]["error"])
        writes.assert_not_called()

    def test_second_start_is_blocked_by_first_durable_intent_before_instance_exists(self):
        self.gpu()
        second = copy.deepcopy(self.vm)
        second["metadata"].update(name="second", uid="vm-second")
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/second"] = second
        second_body = {**self.body, "name": "second"}
        preview, _ = self.call("/api/vm/power/preview", second_body)
        signed_second = {**second_body, "capacity_token": preview[1]["capacity_token"], "confirm_capacity": True}
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(200, result[0], result)
        writes.assert_called_once()
        result, writes = self.call("/api/vm/power", signed_second)
        self.assertEqual(409, result[0], result)
        self.assertIn("VM lab/guest (start pending)", result[1]["error"])
        writes.assert_not_called()

    def test_device_acquired_during_final_admission_sends_no_start(self):
        import test_vm_device_usage as devices
        self.gpu()
        signed = self.reviewed()
        start = server.OPS.start
        def begin(*args, **kwargs):
            result = start(*args, **kwargs)
            devices.DeviceAdmissionTests.holder(self)
            return result
        with mock.patch.object(server.OPS, "start", side_effect=begin):
            result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        self.assertIn("VM lab/other", result[1]["error"])
        writes.assert_not_called()
        self.assertFalse(server.OPS._read()[0]["ref"]["retain_resources"])

    def test_concurrent_approved_starts_send_only_one_request_for_one_device(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.gpu()
        signed = [self.reviewed()]
        second = copy.deepcopy(self.vm)
        second["metadata"].update(name="second", uid="vm-second")
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/second"] = second
        self.body["name"] = "second"
        signed.append(self.reviewed())
        gate = threading.Barrier(2)
        def attempt(body):
            gate.wait(timeout=5)
            try:
                return server.VM_REVIEW.reviewed_vm_power(body)
            except review.Rejected as error:
                return str(error)
        with mock.patch.object(server.VMS, "ksend", return_value={}) as writes, ThreadPoolExecutor(2) as executor:
            outcomes = list(executor.map(attempt, signed))
        writes.assert_called_once()
        self.assertEqual(1, sum(isinstance(result, dict) for result in outcomes), outcomes)
        self.assertIn("start pending", str(outcomes))
        self.assertEqual(1, len(server.OPS._read()))

    def test_start_requires_signed_review_not_just_checkbox(self):
        for body in (self.body, {**self.body, "confirm_capacity": True}):
            result, writes = self.call("/api/vm/power", body)
            self.assertEqual(409, result[0])
            writes.assert_not_called()

    def test_valid_review_sends_exactly_one_start(self):
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(200, result[0], result)
        writes.assert_called_once_with("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachines/guest/start", {})

    def test_changed_action_or_expired_token_never_dispatches(self):
        signed = self.reviewed()
        for body in ({**signed, "action": "restart"}, {**signed, "capacity_token": "1.old"}):
            result, writes = self.call("/api/vm/power", body)
            self.assertEqual(409, result[0])
            writes.assert_not_called()

    def test_changed_vm_version_or_replacement_invalidates_review(self):
        for key in ("uid", "resourceVersion"):
            signed = self.reviewed()
            self.vm["metadata"][key] += "-changed"
            result, writes = self.call("/api/vm/power", signed)
            self.assertEqual(409, result[0])
            writes.assert_not_called()

    def test_changed_config_invalidates_review(self):
        signed = self.reviewed()
        self.config["metadata"]["resourceVersion"] = "2"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0])
        writes.assert_not_called()

    def test_encryption_hardware_cannot_be_overridden_by_capacity_ack(self):
        self.encrypted()
        self.nodes[0]["labels"].pop("kubevirt.io/sev-snp")
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        self.assertFalse(server.OPS._read())

    def test_encryption_gate_change_after_review_does_not_send_power(self):
        self.encrypted()
        signed = self.reviewed()
        self.config["spec"]["configuration"]["developerConfiguration"] = {"disabledFeatureGates": ["WorkloadEncryptionSEV"]}
        self.config["metadata"]["resourceVersion"] = "2"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        self.assertFalse(server.OPS._read())

    def test_encryption_device_disappearing_after_review_never_dispatches(self):
        self.encrypted()
        signed = self.reviewed()
        self.nodes[0]["allocatable"]["devices.kubevirt.io/sev"] = "0"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        self.assertFalse(server.OPS._read())

    def test_supported_encrypted_guest_uses_normal_one_shot_power_journal(self):
        self.encrypted()
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(200, result[0], result)
        writes.assert_called_once_with("PUT", "/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachines/guest/start", {})
        self.assertEqual(1, len(server.OPS._read()))

    def test_invalid_numa_policy_cannot_be_overridden_by_capacity_ack(self):
        self.vm["spec"]["template"]["spec"]["domain"]["cpu"]["numa"] = {"guestMappingPassthrough": {}}
        self.config["status"] = {"observedKubeVirtVersion": "v1.9.0"}
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        self.assertFalse(server.OPS._read())

    def test_physical_numa_sample_is_not_capacity_override_authority(self):
        self.numa_host()
        with mock.patch.object(server.VM_CAPACITY.NUMA_EVIDENCE.time, "time", return_value=1000):
            result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        self.assertFalse(server.OPS._read())

    def test_persistent_state_replacement_after_review_never_sends_power(self):
        pvc = self.persistent_state()
        signed = self.reviewed()
        pvc["metadata"]["uid"] = "replacement-state"
        self.objects["/api/v1/persistentvolumes/state-pv"]["spec"]["claimRef"]["uid"] = "replacement-state"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_stale_vm_state_owner_cannot_be_overridden_by_memory_confirmation(self):
        pvc = self.persistent_state()
        pvc["metadata"]["ownerReferences"][0]["uid"] = "old-vm"
        signed = self.reviewed()
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_fresh_state_requires_separate_typed_consent_and_journals_it(self):
        self.fresh_state()
        signed = self.reviewed()
        for controls in ({}, {"ack_state_initialization": True}, {"ack_state_initialization": True, "confirm_state_name": "other"},
                         {"ack_state_initialization": "true", "confirm_state_name": "guest"}):
            result, writes = self.call("/api/vm/power", {**signed, **controls})
            self.assertEqual(409, result[0], result)
            writes.assert_not_called()
            self.assertFalse(server.OPS._read())
        result, writes = self.call("/api/vm/power", {**signed, "ack_state_initialization": True, "confirm_state_name": "guest"})
        self.assertEqual(200, result[0], result)
        writes.assert_called_once()
        self.assertTrue(server.OPS._read()[0]["ref"]["state_initialization_acknowledged"])

    def test_namespace_backup_selector_change_invalidates_review(self):
        self.fresh_state()
        self.vm["spec"]["template"]["spec"]["domain"].pop("devices")
        self.config["spec"]["configuration"].update(developerConfiguration={"featureGates": ["IncrementalBackup"]},
            changedBlockTrackingLabelSelectors={"namespaceLabelSelector": {"matchLabels": {"backup": "yes"}}})
        namespace = {"metadata": {"name": "lab", "uid": "namespace-uid", "resourceVersion": "1", "labels": {"backup": "yes"}}}
        self.objects["/api/v1/namespaces/lab"] = namespace
        signed = {**self.reviewed(), "ack_state_initialization": True, "confirm_state_name": "guest"}
        namespace["metadata"].update(resourceVersion="2", labels={"backup": "no"})
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_observed_renderer_change_after_thread_review_never_dispatches(self):
        self.config["status"] = {"observedKubeVirtVersion": "v1.9.0", "targetKubeVirtVersion": "v1.9.0"}
        self.vm["spec"]["template"]["spec"]["domain"].update(
            ioThreadsPolicy="supplementalPool", ioThreads={"supplementalPoolThreadCount": 3})
        signed = self.reviewed()
        self.config["status"]["targetKubeVirtVersion"] = "v1.10.0"
        self.config["metadata"]["resourceVersion"] = "2"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_dedicated_io_threads_exhaust_cpu_even_with_capacity_override(self):
        self.nodes[0]["labels"]["cpumanager"] = "true"
        self.config["status"] = {"observedKubeVirtVersion": "v1.9.0"}
        domain = self.vm["spec"]["template"]["spec"]["domain"]
        domain["cpu"]["dedicatedCpuPlacement"] = True
        domain.update(ioThreadsPolicy="supplementalPool", ioThreads={"supplementalPoolThreadCount": 7})
        signed = self.reviewed()
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_current_capacity_is_checked_again_and_cannot_override_hardware(self):
        signed = self.reviewed()
        self.nodes[0]["allocatable"]["devices.kubevirt.io/kvm"] = "0"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0])
        writes.assert_not_called()

    def test_high_physical_ram_is_explicitly_overridable(self):
        self.nodes[0]["mem_used_gb"] = 15
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(200, result[0], result)
        writes.assert_called_once()

    def test_numa_policy_change_or_exhausted_cpu_rejects_approved_start(self):
        import test_vm_numa_fit
        import test_numa_evidence
        self.numa_host()
        evidence = test_vm_numa_fit.snapshot()
        with mock.patch.object(server.VM_CAPACITY.ALLOCATION, "inspect", return_value=evidence), mock.patch.object(server.VM_CAPACITY.NUMA_EVIDENCE.time, "time", return_value=test_numa_evidence.NOW):
            signed = self.reviewed()
            evidence["policy"]["fingerprint"] = "changed"
            result, writes = self.call("/api/vm/power", signed)
            self.assertEqual(409, result[0], result)
            writes.assert_not_called()
            signed = self.reviewed()
            evidence["allocation"]["unallocated_cpu_ids"] = []
            result, writes = self.call("/api/vm/power", signed)
            self.assertEqual(409, result[0], result)
            writes.assert_not_called()

    def test_numa_current_verified_fit_allows_one_shot_start(self):
        import test_vm_numa_fit
        import test_numa_evidence
        self.numa_host()
        with mock.patch.object(server.VM_CAPACITY.ALLOCATION, "inspect", return_value=test_vm_numa_fit.snapshot()), mock.patch.object(server.VM_CAPACITY.NUMA_EVIDENCE.time, "time", return_value=test_numa_evidence.NOW):
            result, writes = self.call("/api/vm/power", self.reviewed())
            self.assertEqual(200, result[0], result)
            writes.assert_called_once()

    def test_dynamic_claim_start_is_explicitly_unsupported_and_cannot_be_overridden(self):
        self.vm["spec"]["template"]["spec"]["resourceClaims"] = [{"name": "accelerator", "resourceClaimName": "device"}]
        preview, writes = self.call("/api/vm/power/preview", self.body)
        self.assertTrue(preview[1]["capacity"]["blocked"])
        self.assertIn("Dynamic resource claims", str(preview[1]["capacity"]["blockers"]))
        result, writes = self.call("/api/vm/power", {**self.body, "confirm_capacity": True, "capacity_token": preview[1]["capacity_token"]})
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_missing_network_device_cannot_be_overridden_by_capacity_checkbox(self):
        for device in ("tun", "vhost-net"):
            signed = self.reviewed()
            self.nodes[0]["allocatable"]["devices.kubevirt.io/" + device] = "0"
            result, writes = self.call("/api/vm/power", signed)
            self.assertEqual(409, result[0], result)
            writes.assert_not_called()
            self.nodes[0]["allocatable"]["devices.kubevirt.io/" + device] = "100"

    def test_network_resource_version_and_free_devices_are_rechecked(self):
        spec = self.vm["spec"]["template"]["spec"]
        spec["networks"] = [{"name": "lan", "multus": {"networkName": "lab/lan"}}]
        spec["domain"]["devices"] = {"interfaces": [{"name": "lan", "sriov": {}}]}
        self.nodes[0]["allocatable"]["vendor/nic"] = "1"
        path = "/apis/k8s.cni.cncf.io/v1/namespaces/lab/network-attachment-definitions/lan"
        nad = {"metadata": {"name": "lan", "namespace": "lab", "uid": "nad-uid", "resourceVersion": "1",
                            "annotations": {"k8s.v1.cni.cncf.io/resourceName": "vendor/nic"}}}
        self.objects[path] = nad
        signed = self.reviewed()
        nad["metadata"]["resourceVersion"] = "2"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        signed = self.reviewed()
        self.pods.append({"metadata": {"name": "consumer", "namespace": "lab"}, "status": {"phase": "Running"},
                          "spec": {"nodeName": "node1", "containers": [{"name": "app", "resources": {"requests": {"vendor/nic": "1"}}}]}})
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_changed_hook_configmap_invalidates_review_without_exposing_script(self):
        import json
        self.vm["spec"]["template"]["metadata"] = {"annotations": {"hooks.kubevirt.io/hookSidecars": json.dumps([
            {"configMap": {"name": "hook", "key": "script"}}])}}
        path = "/api/v1/namespaces/lab/configmaps/hook"
        value = {"metadata": {"name": "hook", "namespace": "lab", "uid": "hook-uid", "resourceVersion": "1"},
                 "data": {"script": "private-script-value"}}
        self.objects[path] = value
        preview, _ = self.call("/api/vm/power/preview", self.body)
        self.assertNotIn("private-script-value", str(preview))
        signed = self.reviewed()
        value["metadata"]["resourceVersion"] = "2"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_stop_force_stop_pause_do_not_depend_on_capacity(self):
        for action in ("stop", "force-stop", "pause"):
            with mock.patch.object(server.VM_REVIEW, "vm_power_capacity_plan", side_effect=AssertionError("must not check capacity")):
                result, writes = self.call("/api/vm/power", {**self.body, "action": action})
            self.assertEqual(200, result[0], result)
            writes.assert_called_once()

    def test_stop_crash_retries_uses_same_power_route_without_capacity_admission(self):
        self.vm["spec"]["runStrategy"] = "RerunOnFailure"
        self.vm["status"] = {"printableStatus": "CrashLoopBackOff"}
        with mock.patch.object(server.VM_REVIEW, "vm_power_capacity_plan", side_effect=AssertionError("must not check capacity")):
            result, writes = self.call("/api/vm/power", {**self.body, "action": "stop"})
        self.assertEqual(200, result[0], result)
        self.assertIn("Boot retries stopped", result[1]["detail"])
        writes.assert_called_once()
        self.assertEqual("PATCH", writes.call_args.args[0])
        self.assertEqual("Halted", writes.call_args.args[2][-1]["value"])

    def test_same_approved_power_is_consumed_across_http_requests(self):
        body = self.reviewed()
        result, writes = self.call("/api/vm/power", body)
        self.assertEqual(200, result[0], result)
        ident = result[1]["operation"]["id"]
        self.assertEqual("vm-power", result[1]["operation"]["kind"])
        result, writes = self.call("/api/vm/power", body)
        self.assertEqual(400, result[0], result)
        self.assertIn(ident, result[1]["error"])
        writes.assert_not_called()
        stored = (Path(self.tmp.name) / server.OPS.STORE).read_text()
        self.assertNotIn(body["capacity_token"], stored)
        self.assertNotIn("capacity_token", stored)

    def test_uncertain_response_is_durable_blocks_other_power_but_not_stop(self):
        body = self.reviewed()
        result, writes = self.call("/api/vm/power", body, error=TimeoutError("private-endpoint"))
        self.assertEqual(400, result[0], result)
        writes.assert_called_once()
        self.assertNotIn("private-endpoint", str(result))
        record = server.OPS._read()[0]
        self.assertEqual("uncertain", record["ref"]["phase"])
        self.assertFalse(server.OPS._public(record)["cancellable"])
        result, writes = self.call("/api/vm/power", body)
        self.assertEqual(400, result[0], result)
        writes.assert_not_called()
        result, writes = self.call("/api/vm/power", {**self.body, "action": "force-stop"})
        self.assertEqual(200, result[0], result)
        writes.assert_called_once()

    def test_explicit_refusal_has_receipt_and_does_not_rewrite_policy(self):
        result, writes = self.call("/api/vm/power", self.reviewed(), error=urllib.error.HTTPError("private-url", 409, "private-payload", {}, None))
        self.assertEqual(400, result[0], result)
        self.assertIn("HTTP 409", result[1]["error"])
        self.assertNotIn("private", str(result))
        writes.assert_called_once()
        record = server.OPS._read()[0]
        self.assertEqual("failed", record["status"])
        self.assertFalse(record["ref"]["retain_resources"])

    def test_journal_failure_never_sends_power(self):
        body = self.reviewed()
        with mock.patch.object(server.OPS, "_write", side_effect=OSError("journal unavailable")):
            result, writes = self.call("/api/vm/power", body)
        self.assertNotEqual(200, result[0])
        writes.assert_not_called()

    def test_admission_is_rechecked_after_obtaining_durable_intent(self):
        body = self.reviewed()
        start = server.OPS.start
        def begin(*args, **kwargs):
            result = start(*args, **kwargs)
            self.nodes[0]["allocatable"]["memory"] = "1Gi"
            return result
        with mock.patch.object(server.OPS, "start", side_effect=begin):
            result, writes = self.call("/api/vm/power", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()
        record = server.OPS._read()[0]
        self.assertEqual("failed", record["status"])
        self.assertIn("no power request was sent", record["message"])

    def test_restart_and_unpause_both_require_fresh_review(self):
        self.running()
        self.vm["spec"]["runStrategy"] = "Manual"
        for action in ("restart", "unpause"):
            self.body["action"] = action
            result, writes = self.call("/api/vm/power", self.body)
            self.assertEqual(409, result[0])
            writes.assert_not_called()
            result, writes = self.call("/api/vm/power", self.reviewed())
            self.assertEqual(200, result[0], result)
            writes.assert_called_once()
            server.OPS.cancel(result[1]["operation"]["id"], confirm="guest")

    def test_identity_change_during_final_check_stops_dispatch(self):
        signed = self.reviewed()
        get = server.kget
        reads = 0
        def racing_get(path):
            nonlocal reads
            value = get(path)
            if path.endswith("/virtualmachines/guest"):
                reads += 1
                if reads == 2:
                    value["metadata"]["uid"] = "replacement"
            return value
        with mock.patch.object(server, "kget", side_effect=racing_get):
            result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_once_policy_is_not_silently_rewritten(self):
        self.vm["spec"]["runStrategy"] = "Once"
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(409, result[0])
        writes.assert_not_called()

    def profile(self):
        self.vm["spec"]["instancetype"] = {"name": "large"}
        expanded = copy.deepcopy(self.vm)
        del expanded["spec"]["instancetype"]
        expanded["spec"]["template"]["spec"]["domain"]["memory"]["guest"] = "8Gi"
        self.objects["/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachines/guest/expand-spec"] = expanded
        return expanded

    def test_instance_type_expansion_is_read_only_and_reviewed(self):
        self.profile()
        result, writes = self.call("/api/vm/power/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertEqual(8, result[1]["capacity"]["vm"]["guest_memory_gb"])
        writes.assert_not_called()
        result, writes = self.call("/api/vm/power", self.reviewed())
        self.assertEqual(200, result[0], result)
        writes.assert_called_once()

    def test_profile_change_invalidates_review_even_without_vm_version_change(self):
        expanded = self.profile()
        signed = self.reviewed()
        expanded["spec"]["template"]["spec"]["domain"]["memory"]["guest"] = "10Gi"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0])
        writes.assert_not_called()

    def test_expansion_for_another_vm_cannot_be_used(self):
        self.profile()["metadata"]["uid"] = "replacement"
        result, writes = self.call("/api/vm/power/preview", self.body)
        self.assertEqual(400, result[0])
        writes.assert_not_called()

    def test_pv_change_invalidates_review_even_if_claim_is_unchanged(self):
        self.disk()
        signed = self.reviewed()
        self.objects["/api/v1/persistentvolumes/pv-root"]["metadata"]["resourceVersion"] = "2"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0])
        writes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
