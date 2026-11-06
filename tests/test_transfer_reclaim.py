"""A transfer gives the backup store its space back: its volumes' folders, and failed copies left alone."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move_phases as PHASE
import homestead_move_state as STATE
import homestead_transfer_cleanup as CLEANUP
from test_transfer_cleanup import API, ID, OTHER, UID, artifact

BV = CLEANUP.LH + "/backupvolumes/"


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.api = API()

    def put(self, plural, name, **fields):
        path = CLEANUP.LH + "/" + plural + "/" + name
        self.api.put(path, artifact(name, **fields))
        return path

    def folder(self, volume, name=None):
        path = BV + (name or volume + "-bv")
        self.api.put(path, {"metadata": {"name": name or volume + "-bv", "uid": volume + "-bv-uid", "resourceVersion": "3",
                                         "labels": {"backup-volume": volume}}, "status": {"dataStored": "100"}})
        return path

    def backup(self, name, volume, **fields):
        path = self.put("backups", name, spec={"snapshotName": name}, **fields)
        self.api.objects[path]["metadata"]["labels"] = {"backup-volume": volume}
        return path

    def clean(self):
        return CLEANUP.source(self.api.get, self.api.send, ID, UID)

    def test_the_volume_folder_goes_with_the_transfer_s_backups(self):
        self.put("snapshots", "snap", spec={"volume": "pvc-disk"})
        self.backup("snap", "pvc-disk")
        folder = self.folder("pvc-disk")
        unrelated = self.folder("pvc-other")
        self.assertTrue(self.clean()["complete"])
        self.assertNotIn(folder, self.api.objects)
        self.assertIn(unrelated, self.api.objects)

    def test_a_failed_attempt_with_only_a_snapshot_left_still_frees_its_folder(self):
        self.put("snapshots", "snap", spec={"volume": "pvc-disk"})
        folder = self.folder("pvc-disk")
        self.assertTrue(self.clean()["complete"])
        self.assertNotIn(folder, self.api.objects)

    def test_the_folder_waits_while_the_transfer_s_backup_is_still_deleting(self):
        self.put("snapshots", "snap", spec={"volume": "pvc-disk"})
        backup = self.backup("snap", "pvc-disk")
        folder = self.folder("pvc-disk")
        self.api.delayed.add(backup)
        result = self.clean()
        self.assertFalse(result["complete"])
        self.assertIn("backup store space of pvc-disk", result["pending"])
        self.assertIn(folder, self.api.objects)
        del self.api.objects[backup]
        self.assertTrue(self.clean()["complete"])
        self.assertNotIn(folder, self.api.objects)

    def test_a_person_s_own_backups_keep_the_folder(self):
        self.put("snapshots", "snap", spec={"volume": "pvc-disk"})
        self.backup("snap", "pvc-disk")
        mine = self.backup("nightly", "pvc-disk", transfer=OTHER, status={"state": "Completed"})
        self.api.objects[mine]["metadata"]["annotations"] = {}
        folder = self.folder("pvc-disk")
        result = self.clean()
        self.assertTrue(result["complete"])
        self.assertIn(folder, self.api.objects)
        self.assertIn(mine, self.api.objects)
        self.assertTrue(any("keeps pvc-disk's folder" in note for note in result["retained"]))

    def test_another_transfer_preparing_the_same_volume_keeps_the_folder(self):
        self.put("snapshots", "snap", spec={"volume": "pvc-disk"})
        self.put("snapshots", "theirs", transfer=OTHER, spec={"volume": "pvc-disk"})
        folder = self.folder("pvc-disk")
        result = self.clean()
        self.assertIn("backup store space of pvc-disk waiting for another transfer", result["pending"])
        self.assertIn(folder, self.api.objects)


class FailedCopyTests(unittest.TestCase):
    def move(self, **fields):
        finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - PHASE.FAILED_COPY_KEEP - 60))
        return {"id": ID, "status": "failed", "transfer_mode": "copy", "finished_at": finished, "flags": {}, **fields}

    def test_a_copy_left_failed_long_enough_is_given_up(self):
        self.assertTrue(PHASE._stale_failed_copy(self.move()))
        self.assertTrue(PHASE._stale_failed_copy(self.move(live=True, flags={"quiesced": True})),
                        "a live copy never stopped its source")

    def test_what_is_kept(self):
        recent = self.move(finished_at=STATE._now())
        cases = {"recent": recent, "a move": self.move(transfer_mode="move"),
                 "stopped its source": self.move(flags={"quiesced": True}),
                 "already cancelling": self.move(flags={"cancelling": True}),
                 "succeeded": self.move(status="succeeded"), "no time": self.move(finished_at="")}
        for why, move in cases.items():
            with self.subTest(why):
                self.assertFalse(PHASE._stale_failed_copy(move))

    def test_tick_cancels_a_stale_failed_copy(self):
        move = self.move()
        with mock.patch.object(STATE, "_read", return_value=[move]), \
             mock.patch.object(STATE, "_find", return_value=move), \
             mock.patch.object(PHASE.TARGET, "_target_journal", return_value=None), \
             mock.patch.object(STATE, "_store"), mock.patch.object(PHASE.ACT, "_abandon") as abandon:
            PHASE._tick_all()
        abandon.assert_called_once_with(ID)


if __name__ == "__main__":
    unittest.main()
