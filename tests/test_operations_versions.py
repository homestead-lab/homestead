"""The legacy file contract must not expose new recovery jobs to old writers."""
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_operations as ops


def legacy_writer(directory):
    # Released v2.8.183 writes operations.json, fails unknown kinds during
    # polling and retains only the last 100 records. Exercise that file contract
    # in another process, including a stale rewrite and Clear finished.
    path = Path(directory) / "operations.json"
    rows = json.loads(path.read_text()) if path.exists() else []
    for row in rows:
        row.update(status="failed", message="Unknown operation type")
    path.write_text(json.dumps(rows[-100:]))
    path.write_text("[]")


class OperationVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(ops, "DATA_DIR", self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, rows, filename=ops.LEGACY_STORE):
        (Path(self.tmp.name) / filename).write_text(json.dumps(rows), encoding="utf-8")

    def raw(self, filename):
        return (Path(self.tmp.name) / filename).read_bytes()

    def legacy(self, ident="legacy-job", status="queued"):
        row = {"id": ident, "kind": "deployment", "status": status, "progress": 0,
               "ref": {"namespace": "lab", "name": "old"}, "message": "old work",
               "started_at": "2026-09-01T00:00:00Z"}
        self.put([row])
        return row

    def start(self, kind="k3s-cluster", ref=None):
        return ops.start(kind, "New work", {}, "/vms", ref or {"namespace": "lab", "name": "new", "retain_resources": True})

    def test_new_jobs_never_enter_legacy_file_and_old_jobs_remain_visible(self):
        legacy = self.legacy()
        before = self.raw(ops.LEGACY_STORE)
        new = self.start()
        self.assertEqual(before, self.raw(ops.LEGACY_STORE))
        current = json.loads(self.raw(ops.STORE))
        self.assertEqual([new["id"]], [row["id"] for row in current])
        self.assertEqual({legacy["id"], new["id"]}, {row["id"] for row in ops._read()})
        self.assertNotIn("_legacy_store", ops.log(legacy["id"]))
        self.assertNotIn("_legacy_store", ops.log(new["id"]))

    def test_legacy_process_poll_prune_and_clear_cannot_modify_new_recovery(self):
        self.legacy()
        new = self.start("vm-power", {"namespace": "lab", "name": "guest", "phase": "uncertain",
                                     "retain_resources": True, "review_digest": "a" * 64})
        before = self.raw(ops.STORE)
        process = multiprocessing.get_context("spawn").Process(target=legacy_writer, args=(self.tmp.name,))
        try:
            process.start()
            process.join(10)
            self.assertEqual(0, process.exitcode)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(before, self.raw(ops.STORE))
        self.assertEqual([new["id"]], [row["id"] for row in ops._read()])
        with self.assertRaisesRegex(ValueError, "already has job"):
            self.start("vm-power", {"review_digest": "a" * 64})
        with self.assertRaisesRegex(ValueError, "needs recovery"):
            self.start("vm-power", {"namespace": "lab", "name": "guest", "review_digest": "b" * 64})

    def test_old_jobs_keep_original_store_when_their_progress_changes(self):
        old = self.legacy()
        new = self.start()
        ops.record_phase(old["id"], "accepted", 30, "Still following old work")
        saved = json.loads(self.raw(ops.LEGACY_STORE))
        self.assertEqual(30, saved[0]["progress"])
        self.assertNotIn("_legacy_store", saved[0])
        self.assertEqual([new["id"]], [row["id"] for row in json.loads(self.raw(ops.STORE))])

    def test_new_progress_does_not_rewrite_unchanged_legacy_store(self):
        self.legacy()
        new = self.start()
        with mock.patch.object(ops.SHARED, "write_json", wraps=ops.SHARED.write_json) as writer:
            ops.record_phase(new["id"], "provisioning", 15, "New step")
        self.assertEqual([ops.STORE], [os.path.basename(call.args[0]) for call in writer.call_args_list])

    def test_jobs_added_by_older_replica_after_initialization_are_not_hidden(self):
        new = self.start()
        old = self.legacy("added-later")
        self.assertEqual({new["id"], old["id"]}, {row["id"] for row in ops._read()})

    def test_missing_established_new_store_cannot_fall_back_to_old_history(self):
        self.legacy()
        self.start()
        (Path(self.tmp.name) / ops.STORE).unlink()
        with self.assertRaisesRegex(ValueError, "history is missing"):
            ops._read()
        with self.assertRaises(ValueError):
            self.start()

    def test_duplicate_identity_between_stores_fails_closed(self):
        old = self.legacy()
        self.put([old], ops.STORE)
        with self.assertRaisesRegex(ValueError, "duplicate identities"):
            ops._read()

    def test_current_store_cannot_redirect_record_ownership_to_legacy(self):
        old = self.legacy()
        self.put([{**old, "id": "other", "_legacy_store": True}], ops.STORE)
        with self.assertRaisesRegex(ValueError, "store ownership"):
            ops._read()

    def test_corrupt_legacy_data_never_gets_replaced_by_new_start(self):
        self.put(None)
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(b"null", self.raw(ops.LEGACY_STORE))
        self.assertFalse((Path(self.tmp.name) / ops.STORE).exists())

    def test_clear_finished_routes_each_record_to_its_original_store(self):
        self.legacy(status="succeeded")
        new = self.start()
        self.assertEqual(1, ops.dismiss_finished()["dismissed"])
        self.assertEqual([], json.loads(self.raw(ops.LEGACY_STORE)))
        self.assertEqual([new["id"]], [row["id"] for row in ops._read()])

    def test_failed_current_write_leaves_legacy_unchanged(self):
        self.legacy()
        before = self.raw(ops.LEGACY_STORE)
        with mock.patch.object(ops.SHARED, "write_json", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.start()
        self.assertEqual(before, self.raw(ops.LEGACY_STORE))


if __name__ == "__main__":
    unittest.main()
