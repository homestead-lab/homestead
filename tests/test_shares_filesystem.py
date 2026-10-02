"""Real Linux permission checks for SMB's root-only preparation."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_shares as shares


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "requires the root Linux filesystem fixture")
class ShareFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="homestead-smb-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.base.chmod(0o755)
        self.root = self.base / "writable"
        self.root.mkdir()
        os.chown(self.root, 200, 202)
        self.root.chmod(0o750)

    def prepare(self, gid=101, uid=0, process_gid=0):
        init = shares.shared_group_init([{"name": "data", "mountPath": "/shares/media"}], gid)
        command = init["command"][2].replace("/shares/0", str(self.root))
        return subprocess.run(["sh", "-ec", command], user=uid, group=process_gid,
                              extra_groups=[], capture_output=True, text=True)

    @staticmethod
    def identity(path):
        st = path.stat()
        return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)

    def test_preparation_preserves_owners_descendants_and_other_roots(self):
        private = self.root / "private"
        private.write_text("private app data")
        os.chown(private, 201, 202)
        private.chmod(0o600)
        readonly = self.base / "readonly"
        readonly.mkdir()
        os.chown(readonly, 201, 202)
        readonly.chmod(0o555)
        private_before, readonly_before = self.identity(private), self.identity(readonly)
        result = self.prepare()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual((200, 101, 0o2770), self.identity(self.root))
        self.assertEqual(private_before, self.identity(private))
        self.assertEqual(readonly_before, self.identity(readonly))

    def test_different_app_uids_can_write_each_others_new_files_through_shared_group(self):
        for gid in (101, 100):
            with self.subTest(gid=gid):
                result = self.prepare(gid)
                self.assertEqual(0, result.returncode, result.stderr)
                for uid, source, other in ((99, "first", None), (100, "second", "first"), (99, "third", "second")):
                    script = "from pathlib import Path; import os; "
                    script += f"root=Path({str(self.root)!r}); "
                    script += f"(root/{source!r}).write_text('new data'); "
                    if other:
                        script += f"h=(root/{other!r}).open('a'); h.write(' shared'); h.close(); "
                    script += f"(root/('dir'+{source!r})).mkdir(exist_ok=True)"
                    result = subprocess.run([sys.executable, "-c", script], user=uid, group=gid,
                                            extra_groups=[], umask=0o002, capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual((uid, gid, 0o664), self.identity(self.root / source))
                    self.assertEqual((uid, gid, 0o2775), self.identity(self.root / ("dir" + source)))
                # Each group scenario starts with an empty writable directory.
                for child in self.root.iterdir():
                    child.rmdir() if child.is_dir() else child.unlink()

    def test_already_prepared_root_needs_no_privileged_metadata_writes(self):
        os.chown(self.root, 200, 101)
        self.root.chmod(0o2770)
        before = self.identity(self.root)
        result = self.prepare(uid=201, process_gid=201)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.identity(self.root))

    def test_required_change_fails_closed_when_storage_rejects_metadata_writes(self):
        before = self.identity(self.root)
        result = self.prepare(uid=201, process_gid=201)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self.identity(self.root))


if __name__ == "__main__":
    unittest.main()
