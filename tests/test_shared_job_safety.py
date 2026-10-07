"""Safety journals must never become empty/unlocked on a storage failure."""
import errno
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_shared as shared
import homestead_operations as operations


def compete_for_job(directory, ready, start, result):
    operations.bind(None, directory, None)
    ready.put(True)
    if not start.wait(10):
        result.put("timed out")
        return
    try:
        operations.start("k3s-cluster", "Test batch", {}, "/vms", {"namespace": "test", "name": "batch"})
        result.put("started")
    except ValueError as error:
        result.put("duplicate" if "already active" in str(error) else str(error))
    except Exception as error:
        result.put(type(error).__name__)


class SharedJobSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = shared.SharedLock("safety-test", strict=True, directory=lambda: self.tmp.name, timeout=.1)
        patch = mock.patch.object(operations, "DATA_DIR", self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)

    def test_strict_lock_is_reentrant_and_can_be_reused(self):
        with self.lock:
            handle = self.lock._handle
            with self.lock:
                self.assertIs(handle, self.lock._handle)
            self.assertFalse(handle.closed)
        self.assertTrue(handle.closed)
        self.assertEqual(0, self.lock._depth)
        with self.lock:
            self.assertIsNot(handle, self.lock._handle)

    def test_unavailable_backend_never_silently_uses_thread_mutex(self):
        with mock.patch.object(shared, "fcntl", None), mock.patch.object(shared, "msvcrt", None):
            with self.assertRaisesRegex(OSError, "locking is unavailable"):
                with self.lock:
                    self.fail("must not enter")
        self.assertEqual(0, self.lock._depth)
        self.assertIsNone(self.lock._handle)
        with self.lock:
            pass

    def test_flock_failure_closes_handle_and_does_not_poison_future_lock(self):
        fake = mock.Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        fake.flock.side_effect = OSError(errno.ENOLCK, "locking unavailable")
        with mock.patch.object(shared, "fcntl", fake):
            with self.assertRaises(OSError):
                with self.lock:
                    self.fail("must not enter")
        self.assertEqual(0, self.lock._depth)
        self.assertIsNone(self.lock._handle)
        with self.lock:
            pass

    def test_busy_shared_lock_times_out_without_entering(self):
        fake = mock.Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        fake.flock.side_effect = BlockingIOError(errno.EAGAIN, "busy")
        with mock.patch.object(shared, "fcntl", fake):
            with self.assertRaisesRegex(TimeoutError, "store is busy"):
                with self.lock:
                    self.fail("must not enter")
        self.assertEqual(0, self.lock._depth)

    def test_unlock_failure_still_releases_thread_and_file_handles(self):
        fake = mock.Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        fake.flock.side_effect = [None, OSError(errno.EIO, "unlock failed")]
        with mock.patch.object(shared, "fcntl", fake):
            with self.assertRaises(OSError):
                with self.lock:
                    handle = self.lock._handle
        self.assertTrue(handle.closed)
        self.assertEqual(0, self.lock._depth)
        with self.lock:
            pass

    def test_corrupt_wrong_shape_and_duplicate_history_never_get_overwritten(self):
        target = Path(self.tmp.name) / operations.STORE
        row = {"id": "same", "ref": {}, "status": "queued"}
        for value in ("{broken-private-data", "null", "{}", "[null]", '[{"id":"missing-state"}]', json.dumps([row, row])):
            target.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Job history") as caught:
                operations.start("k3s-cluster", "New", {}, "/vms", {"name": "new"})
            self.assertNotIn("private-data", str(caught.exception))
            self.assertEqual(value, target.read_text(encoding="utf-8"))

    def test_unreadable_store_is_not_empty_history(self):
        with mock.patch("builtins.open", side_effect=PermissionError("private-path")), mock.patch.object(operations, "_initialized", return_value=True):
            with self.assertRaisesRegex(ValueError, "could not be read safely") as caught:
                operations._read()
        self.assertNotIn("private-path", str(caught.exception))

    def test_bad_initialization_marker_prevents_overwriting_valid_history(self):
        operations.start("k3s-cluster", "First", {}, "/vms", {"name": "one"})
        target = Path(self.tmp.name) / operations.STORE
        before = target.read_bytes()
        for value in ("bad-private-data", "null", '{"version":99}'):
            (Path(self.tmp.name) / operations.STORE_MARKER).write_text(value)
            with self.assertRaisesRegex(ValueError, "initialization"):
                operations.start("k3s-cluster", "Second", {}, "/vms", {"name": "two"})
            self.assertEqual(before, target.read_bytes())

    def test_missing_store_is_valid_only_for_first_job(self):
        self.assertEqual([], operations._read())
        job = operations.start("k3s-cluster", "New", {}, "/vms", {"name": "new"})
        self.assertEqual(job["id"], operations._read()[0]["id"])
        (Path(self.tmp.name) / operations.STORE).unlink()
        with self.assertRaisesRegex(ValueError, "initialized job history is missing"):
            operations.start("k3s-cluster", "New", {}, "/vms", {"name": "new"})
        self.assertFalse((Path(self.tmp.name) / operations.STORE).exists())

    def test_replace_failure_preserves_last_committed_history(self):
        first = operations.start("k3s-cluster", "First", {}, "/vms", {"name": "one"})
        with mock.patch.object(shared.os, "replace", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                operations.start("k3s-cluster", "Second", {}, "/vms", {"name": "two"})
        self.assertEqual([first["id"]], [row["id"] for row in operations._read()])

    def test_operations_requests_durable_directory_sync(self):
        with mock.patch.object(shared, "write_json", wraps=shared.write_json) as write:
            operations.start("k3s-cluster", "New", {}, "/vms", {"name": "one"})
        self.assertTrue(write.call_args.kwargs["durable"])

    def test_marker_write_failure_never_acknowledges_job_and_keeps_intent(self):
        original = shared.write_json
        def write(path, value, **kwargs):
            if str(path).endswith(operations.STORE_MARKER):
                raise OSError("marker unavailable")
            return original(path, value, **kwargs)
        with mock.patch.object(shared, "write_json", side_effect=write):
            with self.assertRaisesRegex(OSError, "marker unavailable"):
                operations.start("k3s-cluster", "First", {}, "/vms", {"namespace": "test", "name": "batch"})
        self.assertEqual(1, len(operations._read()))
        with self.assertRaisesRegex(ValueError, "already active"):
            operations.start("k3s-cluster", "Retry", {}, "/vms", {"namespace": "test", "name": "batch"})

    @unittest.skipUnless(os.name == "posix", "directory fsync is a Linux deployment guarantee")
    def test_directory_sync_failure_is_reported_after_replace_not_acknowledged(self):
        target = str(Path(self.tmp.name) / "journal.json")
        original = os.fsync
        calls = []
        def sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError(errno.EIO, "directory sync failed")
            original(fd)
        with mock.patch.object(shared.os, "fsync", side_effect=sync):
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                shared.write_json(target, {"intent": "retained"}, durable=True)
        self.assertEqual({"intent": "retained"}, json.loads(Path(target).read_text()))

    def test_two_processes_cannot_both_start_same_batch(self):
        context = multiprocessing.get_context("spawn")
        ready, result, start = context.Queue(), context.Queue(), context.Event()
        children = [context.Process(target=compete_for_job, args=(self.tmp.name, ready, start, result)) for _ in range(2)]
        try:
            for child in children:
                child.start()
            for _ in children:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            self.assertEqual(["duplicate", "started"], sorted(result.get(timeout=10) for _ in children))
            for child in children:
                child.join(10)
                self.assertEqual(0, child.exitcode)
            self.assertEqual(1, len(operations._read()))
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            ready.close()
            result.close()


if __name__ == "__main__":
    unittest.main()


class UnchangedWriteTests(unittest.TestCase):
    def test_the_same_content_is_not_written_again(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state.json")
            shared.write_json(path, {"a": 1, "b": [1, 2]}, indent=1, sort_keys=True)
            with mock.patch.object(shared.os, "replace", wraps=os.replace) as replace:
                shared.write_json(path, {"b": [1, 2], "a": 1}, indent=1, sort_keys=True)
                self.assertEqual(0, replace.call_count)
                shared.write_json(path, {"a": 2, "b": [1, 2]}, indent=1, sort_keys=True)
                self.assertEqual(1, replace.call_count)
                shared.write_json(path, {"a": 2, "b": [1, 2]}, indent=1, sort_keys=True, durable=True)
                self.assertEqual(2, replace.call_count, "a durable write always lands")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual({"a": 2, "b": [1, 2]}, json.load(handle))
