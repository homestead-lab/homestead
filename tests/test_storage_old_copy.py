import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_operations as ops
import homestead_reclass as rc


class RetainedCopyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.volume = {"metadata": {"name": "original", "uid": "original-uid", "resourceVersion": "5",
                                     "annotations": {rc.OLD_COPY: "lab/data"}},
                       "spec": {"persistentVolumeReclaimPolicy": "Retain"}, "status": {"phase": "Released"}}
        self.send = mock.Mock()
        for patch in (mock.patch.object(ops, "DATA_DIR", directory.name),
                      mock.patch.object(rc, "kget", side_effect=lambda _: self.volume),
                      mock.patch.object(rc, "ksend", self.send)):
            patch.start(); self.addCleanup(patch.stop)

    def record(self, status, **ref):
        with ops._lock:
            ops._write([{"id": "move", "kind": "reclass", "status": status, "ref": {"old_pv": "original", **ref}}])

    def test_active_failed_and_cancelled_moves_protect_original(self):
        for status in ("queued", "running", "cancelling", "failed", "cancelled"):
            self.record(status)
            with self.assertRaisesRegex(ValueError, "protected"): rc.remove_old_copy("original", ops)
        self.send.assert_not_called()

    def test_succeeded_move_with_recovery_hold_remains_protected(self):
        self.record("succeeded", retain_resources=True)
        with self.assertRaisesRegex(ValueError, "protected"): rc.remove_old_copy("original", ops)
        self.send.assert_not_called()

    def test_completed_move_releases_only_exact_reviewed_volume_version(self):
        self.record("succeeded")
        self.assertTrue(rc.remove_old_copy("original", ops)["ok"])
        self.assertEqual({"uid": "original-uid", "resourceVersion": "5"}, self.send.call_args.args[2]["metadata"])
        self.assertEqual("Delete", self.send.call_args.args[2]["spec"]["persistentVolumeReclaimPolicy"])

    def test_unreadable_history_never_means_no_active_moves(self):
        with mock.patch.object(ops, "_read", side_effect=ValueError("history unavailable")):
            with self.assertRaises(ValueError): rc.remove_old_copy("original", ops)
        self.send.assert_not_called()

    def test_missing_identity_and_new_binding_never_authorize_removal(self):
        self.volume["status"]["phase"] = "Bound"
        with self.assertRaises(ValueError): rc.remove_old_copy("original", ops)
        self.volume["status"]["phase"] = "Released"
        self.volume["metadata"].pop("uid")
        with self.assertRaises(ValueError): rc.remove_old_copy("original", ops)
        self.send.assert_not_called()
