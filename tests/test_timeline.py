"""The "since you were last here" timeline (#748)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_shared as SHARED
import homestead_timeline as T

T0 = 1_790_000_000


class DataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(SHARED, "DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seen_file(self):
        return os.path.join(self.tmp.name, T.SEEN_FILE)

    def seen(self):
        with open(self.seen_file(), encoding="utf-8") as handle:
            return json.load(handle)


class LastHereTests(DataDir):
    def test_first_visit_looks_back_one_day(self):
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("alice", T0))
        self.assertEqual(T0 - T.FIRST_LOOK, T.mark_seen("alice", T0))
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("alice", T0 + 60))

    def test_refresh_within_a_visit_keeps_since(self):
        T.mark_seen("alice", T0)
        T.mark_seen("alice", T0 + 600)
        # Still the same visit: the card does not empty on a refresh.
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("alice", T0 + 900))
        self.assertEqual(T0 - T.FIRST_LOOK, T.mark_seen("alice", T0 + 1200))

    def test_new_visit_starts_where_the_last_ended(self):
        T.mark_seen("alice", T0)
        T.mark_seen("alice", T0 + 600)
        back = T0 + 600 + T.VISIT_GAP + 1
        self.assertEqual(T0 + 600, T.last_here("alice", back))
        self.assertEqual(T0 + 600, T.mark_seen("alice", back))
        self.assertEqual(T0 + 600, T.last_here("alice", back + 60))

    def test_rows_are_bounded_and_expire(self):
        T.mark_seen("old", T0)
        for i in range(T.SEEN_USERS + 5):
            T.mark_seen(f"user{i}", T0 + T.SEEN_KEEP + 10 + i)
        rows = self.seen()["users"]
        self.assertNotIn("old", rows)
        self.assertEqual(T.SEEN_USERS, len(rows))
        self.assertNotIn("user0", rows)          # the least recently here goes first
        self.assertIn(f"user{T.SEEN_USERS + 4}", rows)

    def test_each_person_has_their_own_marker(self):
        T.mark_seen("alice", T0)
        T.mark_seen("alice", T0 + T.VISIT_GAP + 100)
        self.assertEqual(T0, T.last_here("alice", T0 + T.VISIT_GAP + 200))
        self.assertEqual(T0 + T.VISIT_GAP + 200 - T.FIRST_LOOK, T.last_here("bob", T0 + T.VISIT_GAP + 200))

    def test_no_user_has_no_marker(self):
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("", T0))
        self.assertIsNone(T.mark_seen("", T0))
        self.assertFalse(os.path.exists(self.seen_file()))

    def test_unreadable_marker_file_raises_and_is_not_overwritten(self):
        with open(self.seen_file(), "w", encoding="utf-8") as handle:
            handle.write("{half")
        with self.assertRaises(SHARED.Unreadable):
            T.last_here("alice", T0)
        with self.assertRaises(SHARED.Unreadable):
            T.mark_seen("alice", T0)
        with open(self.seen_file(), encoding="utf-8") as handle:
            self.assertEqual("{half", handle.read())


if __name__ == "__main__":
    unittest.main()
