"""Planned single-host outages never relax multi-host maintenance checks."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_power as power
import homestead_lifecycle as lifecycle
import test_node_power_plan as fixture


class SingleNodePowerTests(unittest.TestCase):
    get = fixture.PowerPlanTests.get

    def setUp(self):
        fixture.PowerPlanTests.setUp(self)
        self.objects["/api/v1/nodes"]["items"] = [copy.deepcopy(self.objects["/api/v1/nodes/node1"])]
        self.objects["/api/v1/nodes"]["items"][0]["metadata"]["name"] = "node1"
        self.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = []
        self.objects["/api/v1/pods"]["items"][0]["metadata"]["uid"] = "app-uid"
        power.quorum = lambda: {"members": ["node1"], "ready": ["node1"], "can_lose": 0, "total": 1}

    def test_reboot_and_shutdown_are_planned_outages_without_force(self):
        for action in ("reboot", "poweroff"):
            plan = power.plan("node1", action)
            self.assertTrue(plan["ready"], plan["blockers"])
            self.assertTrue(plan["planned_outage"])
            self.assertFalse(plan["force"])
            self.assertIn("all applications, storage and Homestead", " ".join(plan["warnings"]))
            self.assertTrue(plan["requires_data_ack"])

    def test_single_sqlite_control_plane_also_requires_the_outage_review(self):
        power.quorum = lambda: {"members": [], "can_lose": 0}
        self.assertTrue(power.plan("node1", "poweroff")["planned_outage"])

    def test_one_etcd_member_with_workers_still_requires_override(self):
        for ready in (True, False):
            self.objects["/api/v1/nodes"]["items"].append({"metadata": {"name": "worker", "uid": "worker-uid"},
                "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}})
            plan = power.plan("node1", "poweroff")
            self.assertFalse(plan["planned_outage"])
            self.assertFalse(plan["ready"])
            self.assertIn("only etcd member", " ".join(plan["blockers"]))
            self.objects["/api/v1/nodes"]["items"].pop()

    def test_partial_or_mismatched_node_inventory_cannot_approve_outage(self):
        self.objects["/api/v1/nodes"]["metadata"] = {"continue": "more"}
        with self.assertRaises(ValueError): power.plan("node1", "poweroff")
        self.objects["/api/v1/nodes"].pop("metadata")
        self.objects["/api/v1/nodes"]["items"][0]["metadata"]["uid"] = "replacement"
        self.assertFalse(power.plan("node1", "poweroff")["planned_outage"])

    def test_node_inventory_must_also_report_the_host_ready(self):
        self.objects["/api/v1/nodes"]["items"][0]["status"]["conditions"] = []
        plan = power.plan("node1", "poweroff")
        self.assertFalse(plan["planned_outage"])
        self.assertFalse(plan["ready"])

    def test_disruption_budgets_do_not_block_an_outage_that_evicts_no_pods(self):
        self.objects["/apis/policy/v1/poddisruptionbudgets"]["items"] = [{
            "metadata": {"namespace": "lab", "name": "keep-ready", "generation": 1},
            "spec": {"selector": {}}, "status": {"observedGeneration": 1, "disruptionsAllowed": 0}}]
        plan = power.plan("node1", "poweroff")
        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertEqual(0, plan["maintenance"]["budgets"][0]["allowed"])
        self.assertFalse(plan["maintenance"]["budgets"][0]["wait_for_drain"])

    def test_vms_busy_helpers_unknown_inventory_and_disabled_power_still_block(self):
        for case in ("vm", "helper", "unknown-storage", "unknown-pdb", "disabled", "not-ready"):
            with self.subTest(case=case):
                self.setUp()
                if case == "vm":
                    self.objects["/apis/kubevirt.io/v1/virtualmachineinstances"]["items"] = [
                        {"metadata": {"name": "vm", "namespace": "lab"}, "status": {"nodeName": "node1"}}]
                if case == "helper": self.objects["/api/v1/pods"]["items"][0]["metadata"]["labels"] = {"homestead.io/task": "reclass"}
                if case == "unknown-storage": self.objects.pop(f"{power.LH}/replicas")
                if case == "unknown-pdb": self.objects.pop("/apis/policy/v1/poddisruptionbudgets")
                if case == "disabled": power.power_enabled = lambda: False
                if case == "not-ready": self.objects["/api/v1/nodes/node1"]["status"]["conditions"] = []
                self.assertFalse(power.plan("node1", "poweroff")["ready"])

    def test_a_changed_review_cannot_send_the_planned_outage(self):
        for case in ("new-host", "new-pod", "new-boot", "new-uid", "new-volume-risk"):
            with self.subTest(case=case):
                self.setUp()
                original = power.plan("node1", "reboot")
                if case == "new-host": self.objects["/api/v1/nodes"]["items"].append({"metadata": {"name": "worker"}})
                if case == "new-pod": self.objects["/api/v1/pods"]["items"][0]["metadata"]["uid"] = "new-pod"
                if case == "new-boot": self.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new-boot"
                if case == "new-uid": self.objects["/api/v1/nodes/node1"]["metadata"]["uid"] = "new-node"
                if case == "new-volume-risk": self.objects[f"{power.LH}/volumes"]["items"][0]["status"]["robustness"] = "degraded"
                with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), mock.patch.object(lifecycle, "quorum_report", return_value=original["quorum"]), mock.patch.object(lifecycle, "ksend") as send:
                    with self.assertRaises(ValueError):
                        lifecycle.node_power("node1", "reboot", planned_outage=True,
                                             before_send=lambda: power.recheck_planned_outage(original))
                send.assert_not_called()

    def test_planned_outage_sends_systemd_power_without_cordon_or_eviction(self):
        for action in ("reboot", "poweroff"):
            original = power.plan("node1", action)
            with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), \
                    mock.patch.object(lifecycle, "set_cordon") as cordon, mock.patch.object(lifecycle, "drain") as drain, \
                    mock.patch.object(lifecycle, "quorum_report", return_value=original["quorum"]), \
                    mock.patch.object(lifecycle, "_bust"), mock.patch.object(lifecycle, "ksend", side_effect=lambda method,path,body:
                        {"metadata": {**body["metadata"], "uid": "helper-uid"}}) as send:
                result = lifecycle.node_power("node1", action, planned_outage=True,
                                              before_send=lambda: power.recheck_planned_outage(original))
            cordon.assert_not_called()
            drain.assert_not_called()
            send.assert_called_once()
            self.assertIn("systemctl " + action, send.call_args.args[2]["spec"]["containers"][0]["command"][-1])
            self.assertIn("planned whole-cluster outage: no cordon or drain", result["steps"])

    def test_api_requires_explicit_outage_ack_and_matching_review(self):
        plan = power.plan("node1", "poweroff")
        body = {"node": "node1", "action": "poweroff", "confirm": "node1", "review_token": plan["review_token"],
                "allow_data_risk": True, "allow_stranded": True}
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/node/power", {}
        handler._guard = lambda _: False
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        for ack, code in [(None,409), (False,409), ("true",409), (True,200)]:
            handler._body = lambda: {**body, "allow_cluster_outage": ack}
            with mock.patch.object(server, "send_reviewed_power", return_value={"ok":True}) as send:
                handler.do_POST()
            self.assertEqual(code, handler._send.call_args.args[0])
            self.assertEqual(code == 200, send.called)
        handler._body = lambda: {**body, "allow_cluster_outage": True, "review_token": "stale"}
        with mock.patch.object(server, "send_reviewed_power") as send:
            handler.do_POST()
        self.assertEqual(409, handler._send.call_args.args[0])
        send.assert_not_called()

    def test_outage_cannot_skip_the_fresh_check_or_also_be_forced(self):
        with mock.patch.object(lifecycle, "NODE_POWER_ENABLED", True), mock.patch.object(lifecycle, "ksend") as send:
            with self.assertRaises(ValueError):
                lifecycle.node_power("node1", "poweroff", planned_outage=True)
            with self.assertRaises(ValueError):
                lifecycle.node_power("node1", "poweroff", planned_outage=True, force=True, before_send=lambda: None)
        send.assert_not_called()

    def test_job_records_the_outage_and_uses_a_fresh_pre_power_check(self):
        plan = power.plan("node1", "poweroff")
        job = {"id":"test-job"}
        with mock.patch.object(server.OPS, "start", return_value=job) as start, \
                mock.patch.object(server.OPS, "record_phase"), \
                mock.patch.object(power, "recheck_planned_outage", return_value=None) as recheck, \
                mock.patch.object(lifecycle, "node_power", side_effect=lambda *args, **kwargs:
                    kwargs["before_send"]() or {"ok":True}) as send:
            result = server.send_reviewed_power(plan)
        self.assertTrue(start.call_args.args[4]["planned_outage"])
        self.assertEqual(job, result["operation"])
        self.assertTrue(send.call_args.kwargs["planned_outage"])
        self.assertFalse(send.call_args.kwargs["force"])
        recheck.assert_called_once_with(plan)

    def test_automatic_os_rollout_cannot_approve_a_whole_cluster_outage(self):
        with mock.patch.object(server, "send_reviewed_power") as send:
            with self.assertRaisesRegex(ValueError, "manual review"):
                server.rollout_reboot("node1", allow_single_copy=True)
        send.assert_not_called()

    def test_reboot_observation_does_not_claim_the_host_was_cordoned(self):
        self.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new"
        status, _, message = power.status({"ref": {"node": "node1", "node_uid": "node-uid", "action": "reboot",
            "boot_id": "old", "planned_outage": True, "started_epoch": power.time.time()}})
        self.assertEqual("succeeded", status)
        self.assertIn("Scheduling was left unchanged", message)
        self.assertNotIn("cordoned", message)


if __name__ == "__main__": unittest.main()
