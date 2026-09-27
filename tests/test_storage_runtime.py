import copy
import os
import tempfile
import unittest
from unittest import mock

import test_deploy_capacity
import server
import homestead_operations as ops
import homestead_shared as shared
import homestead_storage_runtime as runtime
import homestead_storage_guard as guard
from homestead_storage_journal import Held


def pod(name, *, image_id="sha256:amd64", container_id=None):
    return {"metadata": {"name": name, "namespace": "lab", "uid": name + "-uid"},
            "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data"}}],
                     "containers": [{"name": "homestead", "image": "homestead:release",
                                     "volumeMounts": [{"name": "data", "mountPath": "/data"}]}]},
            "status": {"phase": "Running", "containerStatuses": [{"name": "homestead",
                "containerID": container_id or "containerd://" + name, "imageID": image_id,
                "state": {"running": {"startedAt": "now"}}}]}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", directory.name); patch.start(); self.addCleanup(patch.stop)
        self.pods = {"one": pod("one"), "two": pod("two", image_id="sha256:arm64")}

    def read(self, path):
        if path.endswith("/pods"): return {"items": copy.deepcopy(list(self.pods.values()))}
        return copy.deepcopy(self.pods[path.rsplit("/", 1)[1]])

    def report(self, name, version="new"):
        return runtime.report(ops, self.read, "lab", name, "homestead", version)

    def check(self):
        return runtime.require(ops, self.read, "lab", "one", "homestead", "new")

    def test_mixed_architecture_replicas_need_their_own_current_process_reports(self):
        self.report("one")
        with self.assertRaisesRegex(Held, "Finish upgrading"): self.check()
        self.report("two")
        self.assertEqual(2, self.check()["replicas"])

    def test_different_version_or_unknown_protocol_cannot_authorize_a_move(self):
        self.report("one"); self.report("two", "old")
        with self.assertRaises(Held): self.check()
        self.report("two")
        records = runtime._records(ops.DATA_DIR)
        records["two-uid/homestead"]["protocol"] = 999
        shared.write_json(os.path.join(ops.DATA_DIR, runtime.FILE), {"format": 1, "members": records})
        with self.assertRaises(Held): self.check()

    def test_same_name_replacement_and_container_restart_invalidate_old_reports(self):
        self.report("one"); self.report("two")
        self.pods["two"]["metadata"]["uid"] = "replacement-uid"
        with self.assertRaises(Held): self.check()
        self.report("two")
        self.pods["two"]["status"]["containerStatuses"][0]["containerID"] = "containerd://restarted"
        with self.assertRaises(Held): self.check()
        self.report("two")
        self.assertEqual(2, self.check()["replicas"])

    def test_pending_and_unverified_writers_block_but_finished_pods_do_not(self):
        self.report("one")
        self.pods["two"]["status"]["containerStatuses"] = []
        with self.assertRaises(Held): self.check()
        self.pods["two"]["status"]["phase"] = "Succeeded"
        self.assertEqual(1, self.check()["replicas"])
        self.pods["browser"] = pod("browser")
        self.pods["browser"]["spec"]["containers"][0]["name"] = "files"
        with self.assertRaises(Held): self.check()

    def test_partial_inventory_and_missing_own_pod_never_pass(self):
        self.report("one"); self.report("two")
        read = self.read
        for listing in ({"items": [], "metadata": {"continue": "next"}}, {"items": [self.pods["two"]]}, {"items": [None]}):
            with mock.patch.object(self, "read", side_effect=lambda path: listing if path.endswith("/pods") else read(path)):
                with self.assertRaises(Held): self.check()

    def test_corrupt_capabilities_are_not_replaced_by_registration(self):
        path = os.path.join(ops.DATA_DIR, runtime.FILE)
        shared.write_json(path, {"format": 999})
        with mock.patch.object(shared, "write_json") as write:
            with self.assertRaises(Held): self.report("one")
            with self.assertRaises(Held): self.check()
            write.assert_not_called()

    def test_read_only_gate_never_registers_for_a_missing_peer(self):
        self.report("one")
        with mock.patch.object(shared, "write_json") as write:
            with self.assertRaises(Held): self.check()
            write.assert_not_called()

    def test_own_update_restart_scale_and_bulk_delete_interlocked_until_move_finishes(self):
        item = {"id": "move", "kind": "reclass", "status": "running",
                "ref": {"namespace": "lab", "claim": "data", "retain_resources": True}}
        with ops._lock: ops._write([item])
        base = "/apis/apps/v1/namespaces/lab/deployments"
        dispatch = mock.Mock()
        for method, path in (("PUT", base + "/homestead"), ("PATCH", base + "/homestead"),
                             ("PUT", base + "/homestead/scale"), ("DELETE", base + "/homestead"), ("DELETE", base)):
            with self.assertRaisesRegex(ValueError, "before updating"):
                guard.send(method, path, {}, dispatch, ops, self.read, own_controller=("lab", "homestead"))
        dispatch.assert_not_called()
        # No exemption for the move's dispatcher: a stop must never target its
        # own UI/controller, even with its own storage ownership identity.
        with guard.dispatching(item):
            with self.assertRaises(ValueError):
                guard.send("PATCH", base + "/homestead", {}, dispatch, ops, self.read, own_controller=("lab", "homestead"))
        item.update(status="succeeded"); item["ref"]["retain_resources"] = False
        with ops._lock: ops._write([item])
        guard.send("PATCH", base + "/homestead", {}, dispatch, ops, self.read, own_controller=("lab", "homestead"))
        dispatch.assert_called_once()

    def test_real_server_transport_enforces_self_update_interlock(self):
        with ops._lock:
            ops._write([{"id": "move", "kind": "reclass", "status": "running", "ref": {"namespace": "lab", "claim": "data"}}])
        with mock.patch.object(server.SELF, "NS", "lab"), mock.patch.object(server, "_ksend") as raw:
            with self.assertRaises(ValueError): server.ksend("PUT", "/apis/apps/v1/namespaces/lab/deployments/homestead", {})
            raw.assert_not_called()
