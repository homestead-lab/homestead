import copy
import io
import json
import unittest
import urllib.error
from unittest import mock

import test_vms as fixtures
import homestead_vms as VMS


class CrashRetryStopTests(unittest.TestCase):
    def setUp(self):
        self.vm = copy.deepcopy(fixtures.VM)
        self.vm["status"] = {"printableStatus": "CrashLoopBackOff",
                             "startFailure": {"consecutiveFailCount": 3},
                             "conditions": [{"type": "Ready", "status": "False", "message": "VMI does not exist"}]}
        self.vmi = None
        self.writes = []
        self.before_patch = lambda: None
        self.refuse = False
        for patch in (mock.patch.object(VMS, "kget", side_effect=self.read),
                      mock.patch.object(VMS, "ksend", side_effect=self.send)):
            patch.start()
            self.addCleanup(patch.stop)

    def instance(self, phase):
        self.vmi = {"metadata": {"name": "win11", "namespace": "default", "uid": "instance-uid",
                                 "ownerReferences": [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
                                                      "name": "win11", "uid": "u1", "controller": True}]},
                    "status": {"phase": phase}}

    def read(self, path):
        if path.endswith("/virtualmachines/win11"):
            return copy.deepcopy(self.vm)
        if path.endswith("/virtualmachineinstances/win11"):
            if self.vmi is None:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return copy.deepcopy(self.vmi)
        raise AssertionError(path)

    def send(self, method, path, body, **kwargs):
        self.writes.append((method, path, copy.deepcopy(body), kwargs))
        if method == "PATCH":
            self.before_patch()
            updated = copy.deepcopy(self.vm)
            for op in body:
                _, section, key = op["path"].split("/")
                if op["op"] == "test":
                    if updated[section][key] != op["value"]:
                        raise urllib.error.HTTPError(path, 409, "changed", {}, io.BytesIO(b'{"message":"VM changed"}'))
                elif op["op"] == "replace":
                    updated[section][key] = op["value"]
                else:
                    raise AssertionError(op)
            self.vm = updated
        elif self.refuse or self.vmi is None or self.vmi["status"]["phase"] in ("Succeeded", "Failed"):
            raise urllib.error.HTTPError(path, 409, "not running", {}, io.BytesIO(b'{"message":"VM is not running"}'))
        return {}

    def stop(self, action="stop"):
        return VMS.power("default", "win11", action)

    def test_shutdown_halts_retries_with_no_instance_and_preserves_vm_data(self):
        before = copy.deepcopy(self.vm)
        result = self.stop()
        self.assertTrue(result["ok"])
        self.assertIn("Boot retries stopped", result["detail"])
        self.assertEqual("Halted", self.vm["spec"]["runStrategy"])
        before["spec"]["runStrategy"] = "Halted"
        self.assertEqual(before, self.vm, "No disk, hardware, cloud-init or retry history should change")
        self.assertEqual(1, len(self.writes))
        self.assertEqual(("PATCH", VMS.API + "/namespaces/default/virtualmachines/win11"), self.writes[0][:2])
        self.assertEqual("application/json-patch+json", self.writes[0][3]["ctype"])
        self.assertNotIn("cloudInit", json.dumps(self.writes))
        self.assertEqual("Stopped", VMS._status(self.vm, {}))
        self.assertEqual(["start"], VMS.actions_for(VMS._status(self.vm, {})))

    def test_force_stop_also_halts_missing_or_completed_instances(self):
        for phase in (None, "Succeeded", "Failed"):
            with self.subTest(phase=phase):
                self.vm["spec"]["runStrategy"] = "RerunOnFailure"
                if phase:
                    self.instance(phase)
                self.assertTrue(self.stop("force-stop")["ok"])
                self.assertEqual("Halted", self.vm["spec"]["runStrategy"])
        self.assertTrue(all(method == "PATCH" for method, _, _, _ in self.writes))

    def test_live_or_unfinished_instance_keeps_normal_shutdown_and_force_semantics(self):
        for phase in ("Running", "Pending", "Scheduling", "Scheduled", "Unknown", ""):
            for action in ("stop", "force-stop"):
                with self.subTest(phase=phase, action=action):
                    self.instance(phase)
                    self.stop(action)
                    method, path, body, _ = self.writes[-1]
                    self.assertEqual("PUT", method)
                    self.assertTrue(path.endswith("/virtualmachines/win11/stop"))
                    self.assertEqual({} if action == "stop" else {"gracePeriod": 0}, body)
                    self.assertEqual("RerunOnFailure", self.vm["spec"]["runStrategy"])

    def test_refused_live_shutdown_never_falls_back_to_policy_mutation(self):
        self.instance("Running")
        self.refuse = True
        with self.assertRaisesRegex(ValueError, "VM is not running"):
            self.stop()
        self.assertEqual(1, len(self.writes))
        self.assertEqual("PUT", self.writes[0][0])
        self.assertEqual("RerunOnFailure", self.vm["spec"]["runStrategy"])

    def test_replaced_or_changed_vm_is_not_halted_and_conflicts_are_not_retried(self):
        for field in ("uid", "resourceVersion"):
            with self.subTest(field=field):
                self.vm["metadata"][field] = "original"
                self.before_patch = lambda: self.vm["metadata"].update({field: "changed"})
                before_count = len(self.writes)
                with self.assertRaisesRegex(ValueError, "VM changed"):
                    self.stop()
                self.assertEqual(before_count + 1, len(self.writes))
                self.assertEqual("RerunOnFailure", self.vm["spec"]["runStrategy"])

    def test_queued_start_or_stop_is_not_overwritten(self):
        for action in ("Start", "Stop"):
            self.vm["status"]["stateChangeRequests"] = [{"action": action}]
            with self.assertRaisesRegex(ValueError, "already queued"):
                self.stop()
        self.assertEqual([], self.writes)

    def test_unavailable_or_malformed_instance_read_does_not_prove_absence(self):
        for failure in (403, 500, "timeout", "malformed"):
            with self.subTest(failure=failure):
                def read(path):
                    if "/virtualmachineinstances/" in path:
                        if isinstance(failure, int):
                            raise urllib.error.HTTPError(path, failure, "unavailable", {}, None)
                        if failure == "timeout":
                            raise TimeoutError("unavailable")
                        return {}
                    return self.read(path)
                with mock.patch.object(VMS, "kget", side_effect=read), self.assertRaises((ValueError, TimeoutError)):
                    self.stop()
        self.assertEqual([], self.writes)

    def test_deleting_or_incomplete_vm_identity_cannot_be_halted(self):
        for change in ({"uid": ""}, {"resourceVersion": ""}, {"name": "other"}, {"namespace": "other"},
                       {"deletionTimestamp": "2026-01-01T00:00:00Z"}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.vm["metadata"])
                self.vm["metadata"].update(change)
                with self.assertRaises(ValueError):
                    self.stop()
                self.vm["metadata"] = original
        self.assertEqual([], self.writes)

    def test_completed_instance_with_wrong_ownership_cannot_trigger_policy_change(self):
        self.instance("Failed")
        self.vmi["metadata"]["ownerReferences"][0]["uid"] = "other-vm"
        with self.assertRaisesRegex(ValueError, "different owner"):
            self.stop()
        self.assertEqual([], self.writes)

    def test_other_run_strategies_are_not_changed_by_this_recovery(self):
        self.instance("Running")
        for strategy in ("Always", "Once", "Manual", "Halted"):
            with self.subTest(strategy=strategy):
                self.vm["spec"]["runStrategy"] = strategy
                self.stop()
                self.assertEqual("PUT", self.writes[-1][0])
                self.assertEqual(strategy, self.vm["spec"]["runStrategy"])

    def test_views_offer_stop_retries_only_for_a_verified_failed_boot_without_guest(self):
        self.assertTrue(VMS._row(self.vm, {}, {}, {})["stop_retries"])
        self.assertFalse(VMS._row(self.vm, {}, {}, {}, instance_known=False)["stop_retries"])
        self.instance("Running")
        self.assertFalse(VMS._row(self.vm, self.vmi, {}, {})["stop_retries"])
        self.vm["spec"]["runStrategy"] = "Halted"
        self.assertFalse(VMS._row(self.vm, {}, {}, {})["stop_retries"])


if __name__ == "__main__":
    unittest.main()
