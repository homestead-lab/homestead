import copy
import unittest
from unittest import mock

import test_vm_capacity as fixtures
import server
import homestead_capacity_review as review


class VMPowerAdmissionTests(unittest.TestCase):
    read = fixtures.VMCapacityTests.read
    running = fixtures.VMCapacityTests.running
    disk = fixtures.VMCapacityTests.disk

    def setUp(self):
        fixtures.VMCapacityTests.setUp(self)
        self.vm["spec"]["runStrategy"] = "Halted"
        self.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"] = self.vm
        self.body = {"ns": "lab", "name": "guest", "action": "start"}
        for patch in (mock.patch.object(server, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.PLACE, "get_nodes", side_effect=lambda: copy.deepcopy(self.nodes)),
                      mock.patch.object(server.PLACE, "hardware_features", return_value=[]),
                      mock.patch.object(server, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS),
                      mock.patch.object(review, "_key", return_value=b"vm-review-tests")):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, path, body):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.VMS, "ksend", return_value={}) as vm_writes, \
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

    def test_stop_force_stop_pause_do_not_depend_on_capacity(self):
        for action in ("stop", "force-stop", "pause"):
            with mock.patch.object(server, "vm_power_capacity_plan", side_effect=AssertionError("must not check capacity")):
                result, writes = self.call("/api/vm/power", {**self.body, "action": action})
            self.assertEqual(200, result[0], result)
            writes.assert_called_once()

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
