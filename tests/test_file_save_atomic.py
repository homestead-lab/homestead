"""Execute the real commit scripts on Linux, including stale/interrupted saves."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_files as files


@unittest.skipIf(os.name == "nt", "requires the Linux helper's shell commands")
class AtomicSaveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = self.root / "config.txt"
        self.target.write_bytes(b"original")
        self.target.chmod(0o640)
        self.fail_stage = False
        self.rename_failure = False
        self.commands = []
        def shell(ns, pod, script, stdin=b"", **kw):
            self.commands.append(script)
            if self.fail_stage and "head -c" in script:
                return b"", "fixture interrupted upload"
            if self.rename_failure and "printf saved" in script:
                # Fail before either rename, as a helper/disk error would.
                script = script.replace("mv -fT", "false; mv -fT", 1)
            result = subprocess.run(["sh", "-c", script], input=stdin, capture_output=True, timeout=10)
            return result.stdout, result.stderr.decode().strip()
        for patch in (mock.patch.object(files, "MOUNT", str(self.root)),
                      mock.patch.object(files, "open_session", return_value={"namespace": "lab", "pod": "fixture"}),
                      mock.patch.object(files, "_sh", side_effect=shell)):
            patch.start()
            self.addCleanup(patch.stop)
        self.revision = hashlib.sha256(b"original").hexdigest()

    def test_atomic_save_preserves_mode_and_keeps_a_complete_backup(self):
        result = files.write_file("lab", "data", "config.txt", "changed", self.revision)
        self.assertEqual(b"changed", self.target.read_bytes())
        self.assertEqual(b"original", (self.root / "config.txt.homestead-bak").read_bytes())
        self.assertEqual(0o640, self.target.stat().st_mode & 0o777)
        self.assertEqual(hashlib.sha256(b"changed").hexdigest(), result["revision"])
        self.assertFalse((self.root / "config.txt.homestead-lock").exists())

    def test_stale_editor_cannot_overwrite_equal_length_changed_content(self):
        self.target.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "file changed"):
            files.write_file("lab", "data", "config.txt", "new", self.revision)
        self.assertEqual(b"modified", self.target.read_bytes())

    def test_interrupted_upload_preserves_original_and_cleans_own_lock(self):
        self.fail_stage = True
        with self.assertRaisesRegex(ValueError, "interrupted"):
            files.write_file("lab", "data", "config.txt", "new", self.revision)
        self.assertEqual(b"original", self.target.read_bytes())
        self.assertFalse((self.root / "config.txt.homestead-lock").exists())

    def test_failed_commit_preserves_original(self):
        self.rename_failure = True
        with self.assertRaises(ValueError):
            files.write_file("lab", "data", "config.txt", "new", self.revision)
        self.assertEqual(b"original", self.target.read_bytes())

    def test_concurrent_writer_cannot_remove_someone_elses_lock(self):
        lock = self.root / "config.txt.homestead-lock"
        lock.mkdir()
        with self.assertRaisesRegex(ValueError, "another save"):
            files.write_file("lab", "data", "config.txt", "new", self.revision)
        self.assertTrue(lock.exists())
        self.assertEqual(b"original", self.target.read_bytes())


if __name__ == "__main__":
    unittest.main()
