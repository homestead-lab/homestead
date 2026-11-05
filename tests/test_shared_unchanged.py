import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import homestead_shared as SHARED  # noqa: E402


class UpdateFileUnchangedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, "x.json")
        self.lock = SHARED.SharedLock("unchanged-test")

    def test_unchanged_answers_without_writing(self):
        SHARED.write_json(self.path, {"a": 1})
        with mock.patch.object(SHARED, "write_json") as write:
            self.assertEqual(7, SHARED.update_file(self.path, lambda v: SHARED.Unchanged(7), self.lock, {}))
        write.assert_not_called()

    def test_every_other_answer_still_writes(self):
        def change(value):
            value["a"] = 2
            return 5
        self.assertEqual(5, SHARED.update_file(self.path, change, self.lock, {}))
        self.assertEqual({"a": 2}, SHARED.read_json(self.path, {}))
        self.assertIsNone(SHARED.update_file(self.path, lambda v: None, self.lock, {}))
        self.assertTrue(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
