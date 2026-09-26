"""Partial SMB service must never replace, erase or resurrect missing data."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_smb_recovery as recovery
import homestead_shares as shares
import server


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"name": name, "pvc": name, "user": "lab", "public": True}
                     for name in ("single", "replicated")]
        self.prefix = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
        self.objects = {
            "/api/v1/nodes": {"items": [self.node("a", False), self.node("b", True)]},
            "/api/v1/namespaces/lab/persistentvolumeclaims": {"items": [
                {"metadata": {"name": name}, "spec": {"volumeName": "pv-" + name},
                 "status": {"phase": "Bound"}} for name in ("single", "replicated")]},
            self.prefix + "/volumes": {"items": [
                {"metadata": {"name": "pv-" + name}, "status": {"robustness": "healthy", "state": "attached"}}
                for name in ("single", "replicated")]},
            self.prefix + "/replicas": {"items": [self.replica("single", "a"),
                                                      self.replica("replicated", "a"), self.replica("replicated", "b")]}}
        self.dep = {"metadata": {"name": shares.SAMBA_NAME, "resourceVersion": "1"},
                    "spec": {"replicas": 1, "template": {"metadata": {}, "spec": {
                        "containers": [{"name": shares.SAMBA_NAME, "image": "smb:test"}]}}}}
        self.dep = shares.configured_deployment(self.dep, self.rows, {})

    @staticmethod
    def node(name, ready):
        return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}

    @staticmethod
    def replica(volume, node):
        return {"spec": {"volumeName": "pv-" + volume, "nodeID": node, "healthyAt": "yesterday", "failedAt": ""},
                "status": {"currentState": "running"}}

    def observe(self):
        return recovery.observe(self.rows, lambda path: copy.deepcopy(self.objects[path]), "lab")

    def test_stale_running_single_copy_is_unavailable_but_two_copy_share_survives(self):
        found, warning = self.observe()
        self.assertEqual("unavailable", found["single"][0])
        self.assertEqual("available", found["replicated"][0])
        self.assertEqual("", warning)

    def test_detached_stopped_replica_is_not_lost(self):
        self.objects["/api/v1/nodes"]["items"][0] = self.node("a", True)
        volume = self.objects[self.prefix + "/volumes"]["items"][0]
        volume["status"] = {"state": "detached", "robustness": "unknown"}
        self.objects[self.prefix + "/replicas"]["items"][0]["status"]["currentState"] = "stopped"
        self.assertEqual("available", self.observe()[0]["single"][0])

    def test_faulted_volume_is_excluded_even_with_stale_healthy_replica(self):
        self.objects[self.prefix + "/volumes"]["items"][1]["status"]["robustness"] = "faulted"
        self.assertEqual("unavailable", self.observe()[0]["replicated"][0])

    def test_failed_replica_is_not_restoration_evidence(self):
        self.objects[self.prefix + "/replicas"]["items"][2]["spec"]["failedAt"] = "today"
        self.assertEqual("unavailable", self.observe()[0]["replicated"][0])

    def test_api_failure_never_means_delete_all_mounts_or_restore_exclusions(self):
        del self.objects[self.prefix + "/replicas"]
        found, warning = self.observe()
        self.assertEqual({}, found)
        self.assertIn("could not be verified", warning)
        state = recovery.plan({"suspended": {"single": "offline"}}, self.rows, found, warning, 10)
        self.assertEqual({"single": "offline"}, state["suspended"])

    def test_missing_pending_and_unknown_driver_claims_are_distinct(self):
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims"]["items"].pop(0)
        self.objects[self.prefix + "/volumes"]["items"] = []
        found, warning = self.observe()
        self.assertEqual("unavailable", found["single"][0])
        self.assertNotIn("replicated", found)
        self.assertIn("replicated", warning)
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims"]["items"][0]["status"]["phase"] = "Pending"
        self.assertNotIn("replicated", self.observe()[0])

    def test_incomplete_paginated_inventory_is_unknown_not_missing_data(self):
        self.objects["/api/v1/nodes"]["metadata"] = {"continue": "next-page"}
        found, warning = self.observe()
        self.assertEqual({}, found)
        self.assertIn("could not be verified", warning)

    def test_debounce_restore_and_unknown_interruptions(self):
        found, warning = self.observe()
        state = recovery.plan({}, self.rows, found, warning, 1000)
        self.assertEqual({}, state["suspended"])
        state = recovery.plan(state, self.rows, found, warning, 1060)
        self.assertIn("single", state["suspended"])
        found["single"] = ("available", "returned")
        state = recovery.plan(state, self.rows, found, "", 1120)
        state = recovery.plan(state, self.rows, {}, "API down", 1180)
        self.assertIn("single", state["suspended"])
        state = recovery.plan(state, self.rows, found, "", 1240)
        state = recovery.plan(state, self.rows, found, "", 1300)
        self.assertIn("single", state["suspended"])
        state = recovery.plan(state, self.rows, found, "", 1360)
        self.assertEqual({}, state["suspended"])

    def test_long_gap_in_observations_restarts_stability_timer(self):
        found, _ = self.observe()
        state = recovery.plan({}, self.rows, found, "", 1000)
        state = recovery.plan(state, self.rows, found, "", 9000)
        self.assertEqual({}, state["suspended"])

    def test_multiple_shares_on_one_claim_are_removed_without_empty_fallback(self):
        rows = self.rows + [{"name": "another-folder", "pvc": "single", "sub_path": "clips", "public": True}]
        recovery.write(self.dep, {"suspended": {"single": "node offline"}})
        dep = shares.configured_deployment(self.dep, rows, {})
        spec = dep["spec"]["template"]["spec"]
        self.assertEqual(["replicated"], [v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"]])
        self.assertEqual(["/shares/replicated"], [v["mountPath"] for v in spec["containers"][0]["volumeMounts"]])
        self.assertFalse(any("single;" in arg or "another-folder;" in arg for arg in spec["containers"][0]["args"]))
        self.assertEqual(445, spec["containers"][0]["readinessProbe"]["tcpSocket"]["port"])
        self.assertEqual(2, len(recovery.offline_shares(dep, rows)))

    def test_reconcile_only_changes_deployment_preserving_data_credentials_and_vip(self):
        sent = []
        initial_template = copy.deepcopy(self.dep["spec"]["template"])
        def send(method, path, body, **kwargs):
            sent.append((method, path, copy.deepcopy(body)))
            self.dep = copy.deepcopy(body)
            return body
        def state():
            return self.rows, {}, {"metadata": {"name": "saved"}}, {}, copy.deepcopy(self.dep)
        before = copy.deepcopy(self.rows)
        with mock.patch.object(shares, "_state", side_effect=state), \
                mock.patch.object(shares, "kget", side_effect=lambda path: copy.deepcopy(self.objects[path])), \
                mock.patch.object(shares, "ksend", side_effect=send), \
                mock.patch.object(shares, "_samba_ready", return_value=False), \
                mock.patch.object(shares.time, "time", return_value=1000):
            self.assertEqual("current", shares.reconcile_samba()["state"])
            self.assertEqual(initial_template, sent[0][2]["spec"]["template"])
            with mock.patch.object(shares.time, "time", return_value=1060):
                self.assertEqual("repaired", shares.reconcile_samba()["state"])
            self.assertEqual(["single"], [s["name"] for s in recovery.offline_shares(self.dep, self.rows)])
            self.assertEqual(before, self.rows)
            # A failed observation or normal reconciliation must not re-add it.
            del self.objects[self.prefix + "/replicas"]
            shares.reconcile_samba()
            self.assertIn("single", recovery.read(self.dep)["suspended"])
        self.assertTrue(all(method == "PUT" and path.endswith("/deployments/homestead-smb") for method, path, _ in sent))

    def test_ready_and_reconciled_server_is_not_restarted_every_poll(self):
        found = {name: ("available", "healthy") for name in ("single", "replicated")}
        recovery.write(self.dep, recovery.plan({}, self.rows, found, "", 1000))
        container = self.dep["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(1, container["startupProbe"]["timeoutSeconds"])
        self.assertEqual(1, container["readinessProbe"]["successThreshold"])
        with mock.patch.object(shares, "_state", return_value=(self.rows, {}, {}, {}, self.dep)), \
                mock.patch.object(recovery, "observe", return_value=(found, "")), \
                mock.patch.object(shares, "ksend") as send:
            self.assertEqual("current", shares.reconcile_samba()["state"])
            self.assertEqual("current", shares.reconcile_samba()["state"])
        send.assert_not_called()

    def test_failed_restore_rolls_back_and_stops_repeated_disconnects(self):
        recovery.write(self.dep, {"suspended": {"single": "offline"}, "pending": {
            "single": {"action": "restore", "since": 1000, "last_seen": 1060}}})
        self.dep = shares.configured_deployment(self.dep, self.rows, {})
        sent = []
        found = {"single": ("available", "returned"), "replicated": ("available", "ready")}
        def send(method, path, body, **kwargs):
            sent.append(copy.deepcopy(body))
            self.dep = copy.deepcopy(body)
            return body
        with mock.patch.object(shares, "_state", side_effect=lambda: (self.rows, {}, {}, {}, copy.deepcopy(self.dep))), \
                mock.patch.object(recovery, "observe", return_value=(found, "")), \
                mock.patch.object(shares, "_samba_ready", return_value=True), \
                mock.patch.object(shares, "_samba_blocker", return_value="mount failed"), \
                mock.patch.object(shares, "_get_optional", side_effect=lambda path: copy.deepcopy(self.dep)), \
                mock.patch.object(shares, "ksend", side_effect=send), \
                mock.patch.object(shares, "ROLLOUT_TIMEOUT", 0), \
                mock.patch.object(shares.time, "time", return_value=1120):
            with self.assertRaisesRegex(ValueError, "previous shares were restored"):
                shares.reconcile_samba()
            self.assertIn("single", recovery.read(self.dep)["restore_failures"])
            self.assertIn("single", recovery.read(self.dep)["suspended"])
            template = copy.deepcopy(self.dep["spec"]["template"])
            with mock.patch.object(shares.time, "time", return_value=2000):
                shares.reconcile_samba()
                self.assertEqual(template, self.dep["spec"]["template"])
                shares.reconcile_samba(retry_recovery=True)
            self.assertEqual({}, recovery.read(self.dep)["restore_failures"])
            self.assertEqual("restore", recovery.read(self.dep)["pending"]["single"]["action"])

    def test_status_reports_partial_not_drift_and_keeps_read_path_non_mutating(self):
        recovery.write(self.dep, {"suspended": {"single": "node offline"}})
        self.dep = shares.configured_deployment(self.dep, self.rows, {})
        self.dep["spec"]["template"]["spec"]["containers"][0]["image"] = server.SAMBA_IMAGE
        with mock.patch.object(server, "_optional_smb", side_effect=lambda path: self.dep if "/deployments/" in path else {}), \
                mock.patch.object(shares, "_state", return_value=(self.rows, {}, {}, {}, self.dep)), \
                mock.patch.object(server, "ksend") as send:
            result = server.samba_state()
        self.assertTrue(result["in_sync"])
        self.assertTrue(result["partial"])
        self.assertEqual(["replicated"], result["served_shares"])
        self.assertEqual("single", result["offline_shares"][0]["name"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
