"""A restart cannot repeat a restore into an unrelated or deleted claim."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_csi_restore as CSI


class RestoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"backup": "backup-example", "namespace": "lab", "name": "example-data",
                    "restore_id": "example-attempt", "size_gb": 8, "source_size_bytes": 5 * 1024 ** 3}
        self.item = {"status": "running", "ref": {"namespace": "lab", "name": "example-data",
                       "restore_config": self.cfg, "restore_started": False}}
        self.pvc = {"metadata": {"namespace": "lab", "name": "example-data", "resourceVersion": "7",
                                "annotations": {"homestead.io/restore-id": "example-attempt"}},
                    "spec": {"storageClassName": "example-storage", "volumeName": "example-pv",
                             "resources": {"requests": {"storage": "5Gi"}}},
                    "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}

    def test_pending_setup_is_polled_and_keeps_config_for_restart(self):
        resolver = Mock()
        with patch.object(server.LH, "_get_or_none", return_value=None), \
             patch.object(server.LH, "restore_backup", return_value={"created": False, "message": "Installing CSI snapshots"}) as restore:
            self.assertEqual(("running", 2, "Installing CSI snapshots"), server._restore_then_tidy(self.item, resolver))
        restore.assert_called_once_with(self.cfg)
        resolver.assert_not_called()
        self.assertFalse(self.item["ref"]["restore_started"])

    def test_existing_owned_claim_after_lost_reply_is_adopted_without_second_create(self):
        with patch.object(server.LH, "_get_or_none", return_value=self.pvc), \
             patch.object(server.LH, "restore_backup") as restore, \
             patch.object(server.LH, "restore_problem", return_value=""):
            result = server._restore_then_tidy(self.item, lambda item: ("running", 30, "Restoring data"))
        self.assertEqual("running", result[0])
        self.assertTrue(self.item["ref"]["restore_started"])
        restore.assert_not_called()

    def test_unrelated_claim_is_not_adopted_and_deleted_started_claim_is_not_recreated(self):
        self.pvc["metadata"]["annotations"] = {}
        with patch.object(server.LH, "_get_or_none", return_value=self.pvc), \
             patch.object(server.LH, "restore_backup") as restore:
            with self.assertRaisesRegex(ValueError, "another operation"):
                server._restore_then_tidy(self.item, Mock())
            restore.assert_not_called()
        self.item["ref"]["restore_started"] = True
        with patch.object(server.LH, "_get_or_none", return_value=None), \
             patch.object(server.LH, "restore_backup") as restore, \
             patch.object(server, "cleanup_restores"):
            result = server._restore_then_tidy(self.item, lambda item: ("failed", 0, "Destination deleted"))
        self.assertEqual("failed", result[0])
        restore.assert_not_called()

    def test_snapshot_errors_are_visible_before_claim_binds(self):
        self.item["ref"]["restore_started"] = True
        self.pvc["status"]["phase"] = "Pending"
        resolver = Mock()
        with patch.object(server.LH, "_get_or_none", return_value=self.pvc), \
             patch.object(server.LH, "restore_problem", return_value="backup is unavailable"):
            result = server._restore_then_tidy(self.item, resolver)
        self.assertEqual("failed", result[0])
        self.assertIn("backup is unavailable", result[2])
        resolver.assert_not_called()

    def test_explicit_resume_resets_dependency_wait_timeout(self):
        self.cfg["snapshot_wait_started"] = 1
        self.item["status"] = "queued"
        with patch.object(server.LH, "_get_or_none", return_value=None), \
             patch.object(server.LH, "restore_backup", return_value={"created": False, "message": "Waiting"}):
            self.assertEqual("running", server._restore_then_tidy(self.item, Mock())[0])
        self.assertGreater(self.cfg["snapshot_wait_started"], 1)

    def test_growth_waits_for_data_restore_then_checks_class_fresh(self):
        self.item["ref"]["restore_started"] = True
        with patch.object(server.LH, "_get_or_none", return_value=self.pvc), \
             patch.object(server.LH, "restore_problem", return_value=""), \
             patch.object(server.LH, "finish_restore_resize") as grow:
            server._restore_then_tidy(self.item, lambda item: ("running", 50, "Restoring"))
            grow.assert_not_called()
        with patch.object(CSI, "kget", return_value={"allowVolumeExpansion": True}), \
             patch.object(CSI, "ksend") as send:
            result = CSI.finish_resize(self.pvc, self.cfg)
        self.assertEqual("running", result[0])
        self.assertEqual(str(8 * 1024 ** 3), send.call_args.args[2]["spec"]["resources"]["requests"]["storage"])
        self.assertEqual("7", send.call_args.args[2]["metadata"]["resourceVersion"])
        with patch.object(CSI, "kget", return_value={"allowVolumeExpansion": False}), \
             patch.object(CSI, "ksend") as send:
            with self.assertRaisesRegex(ValueError, "does not allow"):
                CSI.finish_resize(self.pvc, self.cfg)
            send.assert_not_called()

    def test_growth_reports_pending_until_real_capacity_and_allows_mount_time_filesystem_resize(self):
        self.pvc["spec"]["resources"]["requests"]["storage"] = "8Gi"
        self.assertEqual("running", CSI.finish_resize(self.pvc, self.cfg)[0])
        self.pvc["status"]["conditions"] = [{"type": "FileSystemResizePending", "status": "True"}]
        with patch.object(CSI, "kget", return_value={"spec": {"capacity": {"storage": "8Gi"}}}):
            result = CSI.finish_resize(self.pvc, self.cfg)
        self.assertEqual("succeeded", result[0])
        self.assertIn("next mounted", result[2])
        self.pvc["status"]["capacity"]["storage"] = "8Gi"
        self.assertEqual("succeeded", CSI.finish_resize(self.pvc, self.cfg)[0])


if __name__ == "__main__":
    unittest.main()
