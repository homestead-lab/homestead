import copy
import tempfile
import threading
import unittest
from unittest import mock

import test_deploy_capacity  # server import path
import homestead_operations as ops
import homestead_storage_conflicts as conflicts
import server


class StorageConflictsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", directory.name); patch.start(); self.addCleanup(patch.stop)
        self.move = {"namespace": "lab", "claim": "data", "temp": "data-reclass", "old_pv": "pv-old",
                     "copy_claims": {"data": {"pv": "pv-old", "csi_driver": "driver.longhorn.io", "csi_handle": "lh-data"}}}

    def test_every_storage_conflict_is_symmetric_and_preserves_history(self):
        others = [("snapshot-delete", {"volume": "lh-data"}), ("snapshot-revert", {"volume": "lh-data"}),
                  ("volume-delete", {"namespace": "lab", "name": "data"}),
                  ("volume-delete", {"pv": "pv-old"}), ("volume-delete", {"volume": "lh-data"}),
                  ("volume-restore", {"namespace": "lab", "name": "data-reclass"})]
        for kind, ref in others:
            for first, second in (((kind, ref), ("reclass", self.move)), (("reclass", self.move), (kind, ref))):
                with self.subTest(first=first[0], second=second[0], ref=ref):
                    with ops._lock: ops._write([])
                    ops.start(first[0], "First", {}, "/", first[1])
                    before = copy.deepcopy(ops._read())
                    with self.assertRaisesRegex(ValueError, "storage job"):
                        ops.start(second[0], "Second", {}, "/", second[1])
                    self.assertEqual(before, ops._read())

    def test_namespace_and_pv_names_are_not_confused_with_longhorn_handles(self):
        item = {"id": "move", "kind": "reclass", "status": "running", "ref": self.move}
        for kind, ref in (("volume-delete", {"namespace": "other", "name": "data"}),
                          ("snapshot-delete", {"volume": "pv-old"}),
                          ("volume-delete", {"pv": "lh-data"}),
                          ("volume-restore", {"namespace": "lab", "name": "unrelated"})):
            self.assertEqual([], conflicts.conflicts([item], kind, ref))

    def test_timeouts_and_tracking_only_cancellation_do_not_release_storage(self):
        for kind in ("snapshot-delete", "snapshot-revert", "volume-delete", "volume-restore"):
            for status in ("failed", "cancelled"):
                item = {"id": "held", "kind": kind, "status": status, "tracking_stopped": True,
                        "ref": {"namespace": "lab", "name": "data", "volume": "lh-data"}}
                self.assertTrue(conflicts.unresolved(item))
                self.assertTrue(ops._recovery_needed(item))
                self.assertFalse(ops._public(item)["dismissible"])
                with mock.patch.object(ops, "MAX_OPERATIONS", 0):
                    with ops._lock: ops._write([item])
                    self.assertEqual([item], ops._read())
                with self.assertRaisesRegex(ValueError, "unresolved"):
                    ops.start("reclass", "Move", {}, "/", self.move)

    def test_success_or_recorded_cleanup_releases_hold_but_retention_wins(self):
        for status, cleaned in (("succeeded", False), ("failed", True), ("cancelled", True)):
            item = {"id": "old", "kind": "snapshot-delete", "status": status,
                    "cleaned": cleaned, "ref": {"volume": "lh-data"}}
            self.assertEqual([], conflicts.conflicts([item], "reclass", self.move))
            item["ref"]["retain_resources"] = True
            self.assertEqual([item], conflicts.conflicts([item], "reclass", self.move))

    def test_protocol_hold_is_not_replaceable_even_when_generic_resume_is_disabled(self):
        item = {"id": "old", "kind": "reclass", "status": "failed", "resource": {},
                "ref": {**self.move, "storage_protocol": 1, "retain_resources": True}}
        with ops._lock: ops._write([item])
        with self.assertRaises(ValueError): ops.start("reclass", "Move", {}, "/", self.move)

    def test_restore_creation_and_recording_exclude_a_concurrent_storage_move(self):
        attempting, finished = threading.Event(), threading.Event()
        outcomes, workers = [], []
        def move():
            attempting.set()
            try:
                ops.start("reclass", "Move", {}, "/", self.move)
                outcomes.append("started")
            except ValueError:
                outcomes.append("conflict")
            finally:
                finished.set()
        def restore(body):
            thread = threading.Thread(target=move); workers.append(thread); thread.start()
            self.assertTrue(attempting.wait(2))
            self.assertFalse(finished.wait(.1), "move must wait until restore creation AND its record are complete")
            return {"name": "data", "namespace": "lab", "backup": "backup-1",
                    "source_size_bytes": 1073741824, "created": True, "message": "Restore started"}
        handler = object.__new__(server.H)
        handler.path, handler.command = "/api/lh/restore", "POST"
        handler.headers = {"X-Homestead-Auth": "1"}
        handler._who = lambda: {"user": "admin", "role": "admin"}
        handler._body = lambda: {"namespace": "lab", "name": "data", "backup": "backup-1"}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        try:
            with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
                    mock.patch.object(server.LH, "restore_plan", return_value={}), \
                    mock.patch.object(server.LH, "restore_backup", side_effect=restore):
                handler.do_POST()
        finally:
            for thread in workers: thread.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(["conflict"], outcomes)
        self.assertEqual(200, handler._send.call_args.args[0])
        self.assertEqual(["volume-restore"], [i["kind"] for i in ops._read()])
