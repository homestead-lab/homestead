import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_copy as copier
from homestead_storage_journal import Held


OP = "a" * 24
IMAGE = "ghcr.io/homestead-lab/homestead@sha256:" + "b" * 64
RECEIPT = {"manifest": "c" * 64, "files": 3, "bytes": 1024}


class CopyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.dest = self.root / "source", self.root / "destination"
        self.source.mkdir(); self.dest.mkdir()

    def copy_with_mocks(self, snapshots=None, *, free=1000000, run=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(copier, "_roots", return_value=(str(self.source), str(self.dest))))
            stack.enter_context(mock.patch.object(copier, "inventory", side_effect=snapshots or [RECEIPT] * 3))
            stack.enter_context(mock.patch.object(copier.os, "statvfs", create=True,
                return_value=SimpleNamespace(f_bavail=free, f_frsize=1)))
            stack.enter_context(mock.patch.object(copier, "_sync_tree"))
            command = stack.enter_context(mock.patch.object(copier.subprocess, "run", side_effect=run))
            receipt = copier.copy_verified(self.source, self.dest)
            return receipt, command

    def test_one_shot_copy_verifies_and_uses_no_destructive_or_link_following_options(self):
        receipt, command = self.copy_with_mocks()
        self.assertEqual(RECEIPT, receipt)
        args = command.call_args.args[0]
        self.assertEqual(1, command.call_count)
        self.assertIn("-aHAX", args)
        self.assertIn("--fsync", args)
        self.assertIn("--modify-window=-1", args)
        self.assertNotIn("--delete", args)
        self.assertNotIn("--copy-links", args)
        self.assertEqual([str(self.source) + "/", str(self.dest) + "/"], args[-2:])
        self.assertEqual("--", args[-3])

    def test_nonempty_destination_is_never_overwritten(self):
        (self.dest / "existing-secret").write_text("keep", encoding="utf-8")
        with mock.patch.object(copier.subprocess, "run") as command:
            with self.assertRaisesRegex(Held, "already contains data"):
                self.copy_with_mocks()
            command.assert_not_called()
        self.assertEqual("keep", (self.dest / "existing-secret").read_text(encoding="utf-8"))

    def test_insufficient_free_space_blocks_before_copy(self):
        with self.assertRaisesRegex(Held, "insufficient free space"):
            self.copy_with_mocks(free=1023)

    def test_source_changes_and_destination_mismatch_are_not_success(self):
        other = {**RECEIPT, "manifest": "d" * 64}
        for snapshots in ([RECEIPT, other, RECEIPT], [RECEIPT, RECEIPT, other]):
            with self.assertRaisesRegex(Held, "did not match"):
                self.copy_with_mocks(snapshots)

    def test_command_failure_does_not_leak_filenames_or_retry(self):
        error = subprocess.CalledProcessError(23, "rsync", stderr=b"secret filename /password")
        with self.assertRaises(Held) as caught:
            self.copy_with_mocks(run=error)
        self.assertNotIn("password", str(caught.exception))
        self.assertNotIn("filename", str(caught.exception))
        self.assertIn("Nothing was retried", str(caught.exception))

    def test_root_directories_must_be_separate_mounts(self):
        with self.assertRaisesRegex(Held, "mounted volume roots"):
            copier._roots(str(self.source), str(self.dest))
        with mock.patch.object(copier.os.path, "ismount", return_value=True):
            with self.assertRaisesRegex(Held, "overlap"):
                copier._roots(str(self.source), str(self.source))
            child = self.source / "child"; child.mkdir()
            with self.assertRaisesRegex(Held, "overlap"):
                copier._roots(str(self.source), str(child))

    def test_inventory_detects_content_and_metadata_changes(self):
        file = self.source / "settings.json"; file.write_bytes(b"one")
        first = copier.inventory(self.source)
        file.write_bytes(b"two")
        second = copier.inventory(self.source)
        self.assertNotEqual(first["manifest"], second["manifest"])
        self.assertEqual((1, 3), (second["files"], second["bytes"]))
        os.utime(file, ns=(file.stat().st_atime_ns, file.stat().st_mtime_ns + 1000000000))
        self.assertNotEqual(second["manifest"], copier.inventory(self.source)["manifest"])

    def test_nonempty_lost_found_is_not_silently_skipped(self):
        lost = self.source / "lost+found"; lost.mkdir()
        (lost / "recovered").write_bytes(b"data")
        with self.assertRaisesRegex(Held, "recovered filesystem data"):
            copier.inventory(self.source)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX special files")
    def test_special_files_refuse_copy(self):
        os.mkfifo(self.source / "pipe")
        with self.assertRaisesRegex(Held, "special file"):
            copier.inventory(self.source)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink metadata")
    def test_inventory_never_follows_symlinks_outside_volume(self):
        outside = self.root / "outside"; outside.write_bytes(b"secret")
        (self.source / "link").symlink_to(outside)
        first = copier.inventory(self.source)
        outside.write_bytes(b"changed secret")
        self.assertEqual(first, copier.inventory(self.source))
        self.assertEqual(0, first["files"])

    @unittest.skipUnless(os.name == "posix" and shutil.which("rsync"), "POSIX rsync integration")
    def test_real_rsync_preserves_tree_hardlinks_symlinks_and_metadata(self):
        data = self.source / "settings"; data.write_bytes(b"private-config")
        os.link(data, self.source / "settings-link")
        (self.source / "symlink").symlink_to("settings")
        folder = self.source / "folder"; folder.mkdir()
        (folder / "receipt").write_bytes(b"receipt")
        os.chmod(data, 0o640)
        if hasattr(os, "setxattr"):
            os.setxattr(data, "user.homestead-test", b"private-attribute")
        with mock.patch.object(copier, "_roots", return_value=(str(self.source), str(self.dest))):
            try:
                result = copier.copy_verified(self.source, self.dest)
            except Held:
                # Fixture-only diagnostics: no real configuration or filenames.
                facts = {}
                for base in (self.source, self.dest):
                    facts[base.name] = {str(p.relative_to(base)): {
                        k: getattr(p.lstat(), k) for k in ("st_mode", "st_uid", "st_gid", "st_mtime_ns", "st_nlink")}
                        for p in [base, *base.rglob("*")]}
                self.fail(str(facts))
        self.assertEqual(3, result["files"])
        self.assertEqual(copier.inventory(self.source), copier.inventory(self.dest))
        self.assertEqual((self.dest / "settings").stat().st_ino, (self.dest / "settings-link").stat().st_ino)

    def test_copy_job_has_no_api_token_or_automatic_retry_and_keeps_scheduler(self):
        job = copier.job("lab", "copy", "source", "dest", IMAGE, OP, "node1")
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(0, job["spec"]["backoffLimit"])
        self.assertEqual("Failed", job["spec"]["podReplacementPolicy"])
        self.assertNotIn("ttlSecondsAfterFinished", job["spec"])
        self.assertNotIn("nodeName", pod)
        self.assertEqual("Never", pod["restartPolicy"])
        self.assertEqual(1, len(pod["containers"]))
        self.assertTrue(pod["volumes"][0]["persistentVolumeClaim"]["readOnly"])
        self.assertTrue(pod["containers"][0]["volumeMounts"][0]["readOnly"])
        self.assertEqual(IMAGE, pod["containers"][0]["image"])

    def test_mutable_image_or_invalid_names_cannot_build_job(self):
        for image in ("homestead:latest", "homestead:2.8.195", IMAGE + "; shell"):
            with self.assertRaises(Held): copier.job("lab", "copy", "a", "b", image, OP, "node1")
        with self.assertRaises(Held): copier.job("lab", "copy", "a", "a", IMAGE, OP, "node1")
        with self.assertRaises(Held): copier.job("../lab", "copy", "a", "b", IMAGE, OP, "node1")

    def test_cli_prints_only_verified_receipt_or_safe_error(self):
        out = io.StringIO()
        with mock.patch.object(copier, "copy_verified", return_value=RECEIPT), contextlib.redirect_stdout(out):
            self.assertEqual(0, copier.main([OP]))
        self.assertTrue(out.getvalue().startswith("HOMESTEAD_SELF_DATA_COPY " + OP + " "))
        with mock.patch.object(copier, "copy_verified", side_effect=Held("Copy held")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, copier.main([OP]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, copier.main(["bad-op"]))


if __name__ == "__main__":
    unittest.main()
