import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_move_source as SOURCE


class SnapshotNameTests(unittest.TestCase):
    def test_two_volumes_snapshotted_in_the_same_second_get_different_names(self):
        sent = []
        with mock.patch.object(LH, "ksend", lambda m, p, b=None, **k: sent.append(b) or b), \
                mock.patch.object(LH.time, "time", lambda: 1790287418):
            first = LH.create_snapshot("pvc-a")["snapshot"]
            second = LH.create_snapshot("pvc-b")["snapshot"]
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("homestead-1790287418-"))


class BackupProgressTests(unittest.TestCase):
    def test_unavailable_inventory_cannot_be_mistaken_for_missing_backups(self):
        row = {"claim": "data", "backup": "backup-done", "volume": "volume-a"}
        with mock.patch.object(SOURCE, "_object", return_value={}), \
             mock.patch.object(SOURCE, "_origin", return_value={"replicas": 1}), \
             mock.patch.object(SOURCE, "_remaining", return_value=0), \
             mock.patch.object(SOURCE, "_recorded_backups", return_value=[row]), \
             mock.patch.object(SOURCE.LH, "backups", side_effect=TimeoutError("inventory unavailable")), \
             mock.patch.object(SOURCE, "_merge") as saved:
            with self.assertRaises(TimeoutError):
                SOURCE.backup("container", "camera-app", retry_failed=True)
        saved.assert_not_called()

    def test_a_backup_failing_part_way_keeps_what_it_made(self):
        merged = []
        made = iter([{"backup": "b1"}, RuntimeError("longhorn refused")])

        def create_backup(volume, snapshot, backup):
            result = next(made)
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(SOURCE, "_kind", lambda k: k), \
                mock.patch.object(SOURCE, "_object", lambda kind, name: {"metadata": {"annotations": {}}}), \
                mock.patch.object(SOURCE, "_origin", lambda obj: {"replicas": 1}), \
                mock.patch.object(SOURCE, "_remaining", lambda kind, obj: 0), \
                mock.patch.object(SOURCE, "_recorded_backups", lambda obj: []), \
                mock.patch.object(SOURCE, "_claims_of", lambda kind, obj: ["data", "data2"]), \
                mock.patch.object(SOURCE, "_claim_row", lambda c: {"volume": f"pvc-{c}", "backing_image": ""}), \
                mock.patch.object(SOURCE, "_merge", lambda kind, name, patch: merged.append(patch)), \
                mock.patch.object(SOURCE.LH, "ensure_move_backup", create_backup):
            with self.assertRaises(RuntimeError):
                SOURCE.backup("container", "birdnet-go")
        recorded = json.loads(next(iter(merged[-1]["metadata"]["annotations"].values())))
        self.assertEqual(["data", "data2"], [r["claim"] for r in recorded])
        self.assertTrue(all(r["snapshot"] and r["backup"] for r in recorded), "save intent before every create")

    def test_retry_keeps_completed_and_in_progress_backups_but_replaces_failed(self):
        rows = [{"claim": c, "volume": "pvc-" + c, "backup": "backup-" + c, "backing_image": ""}
                for c in ("done", "busy", "failed")]
        states = [{**r, "name": r["backup"], "state": "Completed" if r["claim"] == "done" else
                   "InProgress" if r["claim"] == "busy" else "Error",
                   "error": "cannot find matched snapshot" if r["claim"] == "failed" else ""} for r in rows]
        saved, created = [], []
        obj = {"metadata": {"annotations": {}}}
        with mock.patch.object(SOURCE, "_object", return_value=obj), \
             mock.patch.object(SOURCE, "_origin", return_value={"replicas": 1}), \
             mock.patch.object(SOURCE, "_remaining", return_value=0), \
             mock.patch.object(SOURCE, "_recorded_backups", return_value=rows), \
             mock.patch.object(SOURCE, "_claims_of", return_value=["done", "busy", "failed"]), \
             mock.patch.object(SOURCE, "_claim_row", side_effect=lambda c: {"volume": "pvc-" + c, "backing_image": ""}), \
             mock.patch.object(SOURCE, "_merge", side_effect=lambda *a: saved.append(a[2])), \
             mock.patch.object(SOURCE.LH, "backups", return_value=states), \
             mock.patch.object(SOURCE.LH, "ensure_move_backup", side_effect=lambda *a: created.append(a)):
            result = SOURCE.backup("container", "camera-app", retry_failed=True)
        self.assertEqual(["backup-done", "backup-busy"], [r["backup"] for r in result["backups"][:2]])
        self.assertNotEqual("backup-failed", result["backups"][2]["backup"])
        self.assertEqual(["backup-failed"], result["backups"][2]["previous_backups"])
        self.assertEqual(1, len(saved))
        self.assertEqual(1, len(created))


class ReadySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.objects, self.sent = {}, []
        self.base = f"{LH.API}/namespaces/{LH.LHNS}"
        def get(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return self.objects[path]
        def send(method, path, body, **kwargs):
            self.sent.append((method, path, body))
            self.objects[path + "/" + body["metadata"]["name"]] = body
            return body
        self.enterContext(mock.patch.object(LH, "kget", side_effect=get))
        self.enterContext(mock.patch.object(LH, "ksend", side_effect=send))
        self.enterContext(mock.patch.object(LH, "backup_target", return_value={"configured": True}))

    def test_waits_for_ready_snapshot_then_resumes_without_duplicate_creates(self):
        LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        self.assertEqual(1, len(self.sent))
        self.assertTrue(self.sent[0][1].endswith("/snapshots"))
        self.objects[self.base + "/snapshots/snapshot-a"]["status"] = {"readyToUse": True}
        LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        self.assertEqual(2, len(self.sent))
        self.assertEqual("backup-a", self.sent[1][2]["metadata"]["name"])

    def test_detached_volume_attachment_wait_is_pending_not_a_failed_snapshot(self):
        LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        snap = self.objects[self.base + "/snapshots/snapshot-a"]
        snap["status"] = {"readyToUse": False, "error":
            "failed to take snapshot because the volume engine engine-a is not running. Waiting for the volume to be attached"}
        result = LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        self.assertTrue(result["pending"])
        self.assertEqual("", LH.move_snapshot_error(snap))
        self.assertEqual(1, len(self.sent))
        snap["status"].update(readyToUse=True, creationTime="2026-09-29T12:00:00Z", error="")
        LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        self.assertEqual(2, len(self.sent))

    def test_lost_reply_after_backup_create_reuses_the_persisted_name(self):
        self.objects[self.base + "/snapshots/snapshot-a"] = {"spec": {"volume": "volume-a"}, "status": {"readyToUse": True}}
        real_send = LH.ksend
        def uncertain(*args, **kwargs):
            real_send(*args, **kwargs)
            raise TimeoutError("reply lost after create")
        with mock.patch.object(LH, "ksend", side_effect=uncertain):
            with self.assertRaises(TimeoutError):
                LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        self.assertEqual(1, len(self.sent))

    def test_removed_or_foreign_snapshot_cannot_be_backed_up(self):
        path = self.base + "/snapshots/snapshot-a"
        for snap in ({"spec": {"volume": "other"}, "status": {"readyToUse": True}},
                     {"spec": {"volume": "volume-a"}, "status": {"readyToUse": True, "markRemoved": True}}):
            self.objects[path] = snap
            with self.assertRaises(ValueError):
                LH.ensure_move_backup("volume-a", "snapshot-a", "backup-a")
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
