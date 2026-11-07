"""A failed backup in a transfer says its cause first, and keeps Longhorn's
full error after it (design.md, Notices)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move_phases as PHASES

UNREACHABLE = ('failed to write data during saving blocks: failed to put object in single part: backupstore/volumes/b5/6f/'
               'pvc-demo/blocks/b9/83/b983.blk: AWS HTTP Error: 0 request send failed, Put '
               '"http://192.0.2.200:9050/homestead-backups/backupstore/volumes/b5/6f/pvc-demo": dial tcp 192.0.2.200:9050: '
               'connect: connection refused')


class BackupFailureWordsTests(unittest.TestCase):
    def test_a_store_it_cannot_reach_is_said_as_that_and_the_error_is_kept(self):
        summary, full = PHASES.backup_failure(UNREACHABLE)
        self.assertEqual("it could not reach the backup storage", summary)
        self.assertIn("request send failed", full)
        self.assertIn("connection refused", full)

    def test_the_usual_causes_have_their_words(self):
        for error, words in (("AccessDenied: Access Denied", "the backup storage refused its credentials"),
                             ("NoSuchBucket: The specified bucket does not exist", "the backup storage has no such bucket"),
                             ("XMinioStorageFull: Storage backend has reached its minimum free drive threshold", "the backup storage is full"),
                             ("rpc error: code = DeadlineExceeded", "it timed out")):
            self.assertEqual(words, PHASES.backup_failure(error)[0], error)

    def test_anything_else_is_its_first_clause(self):
        self.assertEqual(("the snapshot went away", "the snapshot went away; nothing to back up"),
                         PHASES.backup_failure("the snapshot went away; nothing to back up"))
        self.assertEqual("no reason given", PHASES.backup_failure("")[0])


if __name__ == "__main__":
    unittest.main()
