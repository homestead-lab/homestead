"""The jobs store is parsed once per version of its files (#986 §3): every
tab polls GET /api/operations every 3 s while a job runs, and about twenty
modules read the store, so an unchanged store is not parsed again."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_operations as operations
import homestead_shared as SHARED


class StoreMemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        operations.bind(lambda path: {}, self.tmp.name, lambda namespace, name: {})
        operations.start("auto-update", "Update web automatically", {"kind": "Deployment", "name": "web"},
                         "/containers", {})

    def parses(self, read):
        with mock.patch.object(operations.json, "load", wraps=json.load) as load:
            value = read()
        return load.call_count, value

    def test_reading_an_unchanged_store_twice_parses_it_once(self):
        operations.stored()
        count, items = self.parses(operations.stored)
        self.assertEqual(0, count)
        self.assertEqual(["Update web automatically"], [i["title"] for i in items])
        self.assertEqual(0, self.parses(operations.snapshot)[0])

    def test_a_write_by_another_copy_is_read(self):
        operations.stored()
        path = os.path.join(self.tmp.name, operations.STORE)
        items = json.loads(Path(path).read_text())
        items[0]["title"] = "Changed elsewhere"
        SHARED.write_json(path, items)                 # replaces the file, as every copy does
        self.assertEqual("Changed elsewhere", operations.stored()[0]["title"])

    def test_changing_what_a_read_returned_does_not_change_the_next_read(self):
        operations.stored()[0]["title"] = "scribbled"
        self.assertEqual("Update web automatically", operations.stored()[0]["title"])

    def test_a_progress_step_does_not_reread_the_legacy_store(self):
        operations.stored()
        legacy = os.path.join(self.tmp.name, operations.LEGACY_STORE)
        with operations.transaction() as tx:
            tx.items[0]["message"] = "step 2"
            opened = []
            real = open

            def watch(path, *a, **kw):
                opened.append(os.path.basename(str(path)))
                return real(path, *a, **kw)
            with mock.patch("builtins.open", watch):
                tx.save()
        self.assertNotIn(os.path.basename(legacy), opened)
        self.assertNotIn(operations.LEGACY_MARKER, opened)

    def test_a_damaged_store_still_raises(self):
        operations.stored()
        path = os.path.join(self.tmp.name, operations.STORE)
        os.replace(path, path + ".old")
        Path(path).write_text("{not json")
        with self.assertRaisesRegex(ValueError, "could not be read safely"):
            operations.stored()


if __name__ == "__main__":
    unittest.main()
