"""Run the generated production scripts against disposable Linux folders."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports as imports
import homestead_restructure as restructure
from fixtures_source import source


class PathValidationTests(unittest.TestCase):
    def test_aliases_and_overlapping_import_destinations_are_rejected(self):
        for folder in (".", "x/../y", "x/./y"):
            with self.assertRaises(ValueError):
                imports.import_mappings({"mappings": [{"remote_path": "/source", "mount_path": "/config", "folder": folder}]})
            with self.assertRaises(ValueError):
                restructure._folder(folder, "/config")
        for folder in ("data", "data/sub"):
            with self.assertRaisesRegex(ValueError, "overlap"):
                imports.import_mappings({"mappings": [
                    {"remote_path": "/a", "mount_path": "/a", "folder": "data"},
                    {"remote_path": "/b", "mount_path": "/b", "folder": folder}]})

    def test_source_traversal_and_control_characters_are_refused(self):
        for path in ("/a/../b", "/a/./b", "/a\nb"):
            with self.assertRaises(ValueError):
                imports.import_mappings({"remote_path": path})

    def test_import_checks_total_per_volume_before_first_write(self):
        cfg = {"source": "fixture", "name": "test", "create_workload": False,
               "source_consistency": "snapshot", "mappings": [
                   {"remote_path": "/a", "mount_path": "/a", "bytes": 1024},
                   {"remote_path": "/b", "mount_path": "/b", "bytes": 2048}]}
        with mock.patch.object(imports, "_source", return_value=source()):
            script = imports.prepare_import(cfg)["job"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        self.assertIn("copy_space /appdata 3", script)
        self.assertLess(script.index("copy_space /appdata 3"), script.index("mkdir -p /appdata"))
        self.assertLess(script.index("sync; du"), script.index("echo '==> done'"))

    def test_preflight_failure_is_visible_in_import_progress(self):
        result = imports.import_progress("==> error: insufficient destination free space. Grow the volume.")
        self.assertEqual("copy preflight failed", result["error"])
        self.assertIn("Grow the volume", result["error_detail"])


@unittest.skipIf(os.name == "nt", "production copy shell runs on Linux; covered by source-copy fixture")
class CopyShellTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.old, self.new = self.root / "old", self.root / "new"
        self.old.mkdir(); self.new.mkdir()
        (self.old / "keep").write_text("original")
        (self.new / "borrowed").write_text("retain me")
        self.move = {"from": "old", "to": "new", "from_folder": "", "to_folder": "", "data": True}

    def run_copy(self, prefix="", move=None):
        script = restructure.script([move or self.move], {"old": str(self.old), "new": str(self.new)})
        return subprocess.run(["sh", "-c", prefix + script], capture_output=True, text=True, timeout=30)

    def assert_retained(self):
        self.assertEqual("original", (self.old / "keep").read_text())
        self.assertEqual("retain me", (self.new / "borrowed").read_text())

    def test_low_free_space_refuses_before_copy_without_deleting_either_side(self):
        result = self.run_copy("df() { printf 'Filesystem blocks used available capacity mounted\\nfixture 100 100 0 100%% /\\n'; };\n")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("insufficient destination free space", result.stdout)
        self.assertFalse((self.new / "keep").exists())
        self.assert_retained()

    def test_unavailable_measurements_are_explicit_not_zero(self):
        for prefix, message in (("df() { return 1; };\n", "free space is unavailable"),
                                ("timeout() { case \"$2\" in du) return 1;; *) command timeout \"$@\";; esac; };\n", "source size is unavailable")):
            result = self.run_copy(prefix)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(message, result.stdout)
            self.assert_retained()

    def test_symlink_destination_or_parent_cannot_escape_volume(self):
        outside = self.root / "outside"
        outside.mkdir()
        for folder in ("alias", "alias/sub"):
            link = self.new / "alias"
            if not link.exists():
                link.symlink_to(outside, target_is_directory=True)
            result = self.run_copy(move={**self.move, "to_folder": folder})
            self.assertNotEqual(0, result.returncode)
            self.assertIn("symbolic link", result.stdout)
            self.assertEqual([], list(outside.iterdir()))
            self.assert_retained()

    def test_existing_child_symlink_cannot_overwrite_unrelated_file(self):
        outside = self.root / "unrelated"
        outside.write_text("not copy data")
        (self.new / "keep").symlink_to(outside)
        result = self.run_copy()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("not copy data", outside.read_text())
        self.assert_retained()

    def test_normal_copy_preserves_original_and_borrowed_files(self):
        result = self.run_copy()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("original", (self.new / "keep").read_text())
        self.assertTrue(result.stdout.rstrip().endswith("done"))
        self.assert_retained()

    def test_copy_failure_does_not_emit_completion(self):
        result = self.run_copy("cp() { return 1; };\n")
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("\ndone\n", result.stdout)
        self.assert_retained()
