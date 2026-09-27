"""Provisioning intent precedes writes; failed batches never destroy data."""
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_k3scluster as cluster
import homestead_operations as ops
import homestead_cancel as cancel


class BootstrapRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = {"name": "test", "network": "default/lan", "servers": 1, "agents": 1,
                    "addresses": ["192.0.2.20", "192.0.2.21"], "password": "test-private-password"}
        patch = mock.patch.object(cluster, "check", return_value="")
        patch.start()
        self.addCleanup(patch.stop)
        self.token = "test-private-join-token"
        self.prepared = cluster.prepare(self.cfg, self.token)
        self.created = []

    def create(self, cfg):
        item = ops._read()[-1]
        self.assertEqual("provisioning", item["ref"]["phase"])
        self.assertEqual(cfg["name"], item["ref"]["attempted"])
        self.created.append(copy.deepcopy(cfg))
        return {"vm_identity": {"name": cfg["name"], "namespace": "lab", "uid": cfg["name"] + "-uid", "resourceVersion": "1"}}

    def test_prepare_is_pure_deterministic_with_supplied_token_and_macs(self):
        cfg = {**self.cfg, "macs": {"test-server-1": "52:54:00:11:22:33"}}
        before = copy.deepcopy(cfg)
        with mock.patch.object(cluster, "create") as create:
            a, b = cluster.prepare(cfg, self.token), cluster.prepare(cfg, self.token)
        self.assertEqual(a, b)
        self.assertEqual(before, cfg)
        self.assertEqual("52:54:00:11:22:33", a["configs"][0]["mac"])
        self.assertIn(self.token, a["configs"][1]["cloud_init"])
        create.assert_not_called()
        self.assertEqual([], ops._read())

    def test_invalid_version_address_and_resources_fail_before_any_job_or_vm(self):
        for change in ({"k3s_version": "v1.2.3;bad"}, {"addresses": ["not-an-ip", "192.0.2.21"]},
                       {"memory": "0Gi"}, {"cores": 0}, {"disk_gb": 0}):
            with self.subTest(change=change), mock.patch.object(cluster, "create") as create:
                with self.assertRaises(ValueError):
                    cluster.start({**self.cfg, **change}, ops)
                create.assert_not_called()
        self.assertEqual([], ops._read())

    def test_job_exists_before_first_write_and_records_receipts_not_secrets(self):
        result = cluster.commit(self.prepared, ops, create_one=self.create)
        self.assertEqual("running", result["status"])
        item = ops._read()[-1]
        self.assertEqual("awaiting-ready", item["ref"]["phase"])
        self.assertEqual(2, len(item["ref"]["created"]))
        text = json.dumps(ops._read())
        for private in (self.cfg["password"], self.token, "cloud_init", "#cloud-config"):
            self.assertNotIn(private, text)

    def test_failure_on_second_node_retains_first_and_uncertain_second(self):
        def create(cfg):
            if cfg["name"].endswith("agent-1"):
                raise RuntimeError(self.cfg["password"] + " " + self.token)
            return self.create(cfg)
        with mock.patch.object(cluster, "remove") as remove:
            with self.assertRaisesRegex(ValueError, "Resources are retained") as error:
                cluster.commit(self.prepared, ops, create_one=create)
            remove.assert_not_called()
        item = ops._read()[-1]
        self.assertEqual("failed", item["status"])
        self.assertEqual("test-agent-1", item["ref"]["attempted"])
        self.assertEqual(1, len(item["ref"]["created"]))
        self.assertFalse(ops._public(item)["cleanable"])
        for private in (self.cfg["password"], self.token):
            self.assertNotIn(private, json.dumps(item) + str(error.exception))

    def test_missing_creation_identity_stops_before_next_vm(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "Stopped at test-server-1"):
            cluster.commit(self.prepared, ops, create_one=lambda cfg: calls.append(cfg["name"]) or {})
        self.assertEqual(["test-server-1"], calls)
        self.assertEqual("test-server-1", ops._read()[-1]["ref"]["attempted"])

    def test_before_node_gate_stops_remaining_writes_without_cleanup(self):
        def gate(cfg, made):
            if made:
                self.assertEqual("test-server-1-uid", made[0]["identity"]["uid"])
                raise ValueError("fresh capacity no longer fits")
        with self.assertRaisesRegex(ValueError, "Stopped at test-agent-1"):
            cluster.commit(self.prepared, ops, create_one=self.create, before_node=gate)
        self.assertEqual(1, len(self.created))
        self.assertEqual("", ops._read()[-1]["ref"]["attempted"])

    def test_job_persistence_failure_prevents_vm_creation(self):
        with mock.patch.object(ops, "_write", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                cluster.commit(self.prepared, ops, create_one=self.create)
        self.assertEqual([], self.created)

    def test_receipt_persistence_failure_never_automatically_replays_or_deletes(self):
        record = ops.record_phase
        def phase(*args, **kwargs):
            if kwargs.get("created"):
                raise OSError("full disk")
            return record(*args, **kwargs)
        with mock.patch.object(ops, "record_phase", side_effect=phase):
            with self.assertRaisesRegex(ValueError, "Resources are retained"):
                cluster.commit(self.prepared, ops, create_one=self.create)
        self.assertEqual(1, len(self.created))
        self.assertEqual("test-server-1", ops._read()[-1]["ref"]["attempted"])

    def test_active_batch_deduplicates_before_second_dispatch(self):
        cluster.commit(self.prepared, ops, create_one=self.create)
        with self.assertRaisesRegex(ValueError, "already active"):
            cluster.commit(self.prepared, ops, create_one=self.create)
        self.assertEqual(2, len(self.created))

    def test_history_limit_cannot_evict_active_intent_or_retained_failure(self):
        items = [{"id": "in-flight", "status": "running", "ref": {}},
                 {"id": "partial", "status": "failed", "ref": {"retain_resources": True}}]
        items += [{"id": str(i), "status": "succeeded", "ref": {}} for i in range(8)]
        with mock.patch.object(ops, "MAX_OPERATIONS", 3):
            ops._write(items)
        self.assertEqual(["in-flight", "partial", "5", "6", "7"], [row["id"] for row in ops._read()])

    def test_provisioning_monitor_never_declares_success_or_replays_a_write(self):
        item = {"ref": {"phase": "provisioning", "nodes": self.prepared["plan"]["nodes"], "started": time.time()}, "progress": 4}
        with mock.patch.object(cluster, "kget") as read, mock.patch.object(cluster, "create") as create:
            self.assertEqual("running", cluster.status(item)[0])
            item["ref"]["started"] -= cluster.START_LIMIT + 1
            self.assertEqual("failed", cluster.status(item)[0])
            read.assert_not_called()
            create.assert_not_called()

    def test_new_job_cancel_cannot_delete_resources_or_race_dispatch(self):
        item = {"ref": {"retain_resources": True, "phase": "provisioning"}}
        self.assertFalse(cancel.k3s_plan(item)["can"])
        with mock.patch.object(cancel.VMS, "delete") as delete, mock.patch.object(cancel, "forget_addresses") as forget:
            with self.assertRaises(ValueError):
                cancel.k3s_cancel(item, {})
            item["ref"]["phase"] = "awaiting-ready"
            self.assertEqual("forget", cancel.k3s_plan(item)["mode"])
            self.assertIn("retained", cancel.k3s_cancel(item, {}))
            delete.assert_not_called()
            forget.assert_not_called()

    def test_readiness_never_accepts_replacement_vm_by_name(self):
        cluster.commit(self.prepared, ops, create_one=self.create)
        item = ops._read()[-1]
        with mock.patch.object(cluster, "kget", return_value={"metadata": {"uid": "replacement"}}):
            status, _, message = cluster.status(item)
        self.assertEqual("failed", status)
        self.assertIn("replaced", message)


if __name__ == "__main__":
    unittest.main()
