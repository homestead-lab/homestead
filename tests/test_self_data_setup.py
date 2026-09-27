import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_setup as S
from homestead_storage_journal import Held
from test_self_data_coordinator import Cluster


class PublicationGateTests(unittest.TestCase):
    def test_coordinator_cannot_stop_without_publication_receipt(self):
        c = Cluster(published=False)
        before = len(c.sent)
        with self.assertRaisesRegex(Held, "not durably published"):
            c.step()
        self.assertEqual(before, len(c.sent))
        self.assertEqual(2, c.objects[c.dep_path]["spec"]["replicas"])

    def test_receipt_is_bound_to_exact_control_uid_and_cannot_be_replaced(self):
        c = Cluster(published=False)
        with self.assertRaises(Held):
            c.anchor.pointer_published(A.pointer_digest("lab", c.anchor.state, "different-anchor"))
        digest = A.pointer_digest("lab", c.anchor.state, c.handle["uid"])
        c.anchor.pointer_published(digest)
        with self.assertRaises(Held): c.anchor.pointer_published(digest)
        self.assertEqual("quiesce", c.step()["phase"])

    def test_copied_receipt_cannot_authorize_replacement_anchor(self):
        c = Cluster()
        c.objects[c.anchor.path]["metadata"]["uid"] = "replacement"
        with self.assertRaises(Held):
            c.fresh().load(operation=c.handle["operation"], uid="replacement")


@unittest.skipUnless(os.name == "posix", "requires the actual Linux publication/filesystem semantics")
class PointerPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = Cluster(published=False)

    def publish(self):
        return S.publish_pointer(self.temp.name, self.c.anchor)

    def assert_unarmed(self):
        with self.assertRaises(Held): self.c.step()
        self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])

    def test_file_and_directory_are_synced_before_api_checkpoint(self):
        calls = []
        sync = os.fsync
        send = self.c.anchor.send
        def fsync(fd):
            calls.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            sync(fd)
        def checkpoint(*args):
            self.assertEqual(["file", "directory"], calls)
            self.assertEqual(A.pointer("lab", self.c.anchor.state, self.c.handle["uid"]), S.read_marker(self.temp.name))
            return send(*args)
        with mock.patch.object(S.os, "fsync", side_effect=fsync), mock.patch.object(self.c.anchor, "send", side_effect=checkpoint):
            self.assertEqual(self.c.handle, self.publish())
        self.assertEqual([S.MARKER], [p.name for p in self.root.iterdir()])
        self.assertEqual(0o600, stat.S_IMODE((self.root / S.MARKER).stat().st_mode))
        self.assertEqual("quiesce", self.c.step()["phase"])

    def test_existing_identical_marker_is_not_adopted(self):
        original = A.pointer_bytes(A.pointer("lab", self.c.anchor.state, self.c.handle["uid"]))
        (self.root / S.MARKER).write_bytes(original)
        with self.assertRaisesRegex(Held, "already exists"): self.publish()
        self.assertEqual(original, (self.root / S.MARKER).read_bytes())
        self.assert_unarmed()

    def test_file_sync_failure_does_not_publish_marker(self):
        with mock.patch.object(S.os, "fsync", side_effect=OSError("private filesystem diagnostic")):
            with self.assertRaises(Held) as caught: self.publish()
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual([], list(self.root.iterdir()))
        self.assert_unarmed()

    def test_directory_sync_failure_retains_marker_but_does_not_release_coordinator(self):
        sync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("directory unavailable")
            sync(fd)
        with mock.patch.object(S.os, "fsync", side_effect=fail_directory):
            with self.assertRaises(Held): self.publish()
        self.assertIsNotNone(S.read_marker(self.temp.name))
        self.assert_unarmed()
        with self.assertRaisesRegex(Held, "already exists"): self.publish()

    def test_competing_publication_cannot_be_overwritten(self):
        link = os.link
        def race(*args, **kwargs):
            (self.root / S.MARKER).write_bytes(b"retained competing receipt")
            return link(*args, **kwargs)
        with mock.patch.object(S.os, "link", side_effect=race):
            with self.assertRaises(Held): self.publish()
        self.assertEqual(b"retained competing receipt", (self.root / S.MARKER).read_bytes())
        self.assert_unarmed()

    def test_replaced_source_directory_cannot_publish_api_receipt(self):
        sync = os.fsync
        original = self.root / "data"
        original.mkdir()
        def swap(fd):
            sync(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                original.rename(self.root / "retained")
                original.mkdir()
        with mock.patch.object(S.os, "fsync", side_effect=swap):
            with self.assertRaisesRegex(Held, "directory changed"):
                S.publish_pointer(str(original), self.c.anchor)
        self.assertTrue((self.root / "retained" / S.MARKER).exists())
        self.assert_unarmed()

    def test_stale_control_after_publication_does_not_get_acknowledged(self):
        read = S.read_marker
        def stale(directory):
            self.c.objects[self.c.anchor.path]["metadata"]["resourceVersion"] = "99"
            return read(directory)
        with mock.patch.object(S, "read_marker", side_effect=stale):
            with self.assertRaisesRegex(Held, "review changed"): self.publish()
        self.assertIsNotNone(S.read_marker(self.temp.name))
        self.assert_unarmed()

    def test_lost_checkpoint_reply_never_removes_or_republishes_marker(self):
        self.c.lost = lambda method, path, body: path == self.c.anchor.path
        with self.assertRaises(Held): self.publish()
        marker = (self.root / S.MARKER).read_bytes()
        with self.assertRaises(Held): self.publish()
        self.assertEqual(marker, (self.root / S.MARKER).read_bytes())
        # The independent reader can see an accepted checkpoint; unlike an
        # unverified file write, this API write followed both successful fsyncs.
        self.c.lost = None
        self.assertEqual("quiesce", self.c.step()["phase"])

    def test_symlink_marker_or_source_directory_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        (self.root / S.MARKER).symlink_to(other / "absent")
        with self.assertRaises(Held): self.publish()
        self.assert_unarmed()
        link = self.root / "linked-directory"
        link.symlink_to(other)
        with self.assertRaises(Held): S.publish_pointer(str(link), self.c.anchor)


if __name__ == "__main__":
    unittest.main()
