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
        # The store is parsed with json.loads; the tiny markers with json.load.
        real, stores = json.loads, []

        def loads(text, *a, **kw):
            stores.extend([1] if '"id"' in str(text) else [])
            return real(text, *a, **kw)
        with mock.patch.object(operations.json, "loads", loads):
            value = read()
        return len(stores), value

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

    def test_a_progress_step_does_not_rewrite_the_legacy_store(self):
        operations.stored()
        written = []
        real = operations.SHARED.write_json

        def watch(path, *a, **kw):
            written.append(os.path.basename(path))
            return real(path, *a, **kw)
        with mock.patch.object(operations.SHARED, "write_json", watch):
            with operations.transaction() as tx:
                tx.items[0]["message"] = "step 2"
                tx.save()
        self.assertEqual([operations.STORE], written)

    def test_a_same_size_rewrite_in_place_is_read(self):
        # The data volume is NFS, whose attribute cache can answer stat()
        # from before another copy's write; an in-place rewrite can keep the
        # inode, size and (to the clock's tick) mtime.
        operations.stored()
        path = os.path.join(self.tmp.name, operations.STORE)
        before = os.stat(path)
        text = Path(path).read_text()
        changed = text.replace("Update web automatically", "Update web Automatically")
        self.assertEqual(len(text), len(changed))
        with open(path, "r+", encoding="utf-8") as handle:
            handle.write(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_ino, os.stat(path).st_ino)
        self.assertEqual("Update web Automatically", operations.stored()[0]["title"])

    def test_a_change_under_the_lock_always_reads_the_file(self):
        operations.stored()
        opened = []
        real = open

        def watch(path, *a, **kw):
            opened.append(os.path.basename(str(path)))
            return real(path, *a, **kw)
        with mock.patch("builtins.open", watch):
            with operations.transaction() as tx:
                tx.items
        self.assertIn(operations.STORE, opened)

    def test_a_damaged_store_still_raises(self):
        operations.stored()
        path = os.path.join(self.tmp.name, operations.STORE)
        os.replace(path, path + ".old")
        Path(path).write_text("{not json")
        with self.assertRaisesRegex(ValueError, "could not be read safely"):
            operations.stored()


if __name__ == "__main__":
    unittest.main()
