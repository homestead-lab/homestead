"""A restart cannot repeat a restore into an unrelated or deleted claim."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_longhorn_restore as RESTORE
import homestead_csi_restore as CSI
import homestead_kube as KUBE
import homestead_operations as OPS
from fakes.kube import Kube


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
        with patch.object(KUBE, "get_optional", return_value=None), \
             patch.object(RESTORE, "restore_backup", return_value={"created": False, "message": "Installing CSI snapshots"}) as restore:
            self.assertEqual(("running", 2, "Installing CSI snapshots"), server.STORAGE_ROUTES._restore_then_tidy(self.item, resolver))
        restore.assert_called_once_with(self.cfg)
        resolver.assert_not_called()
        self.assertFalse(self.item["ref"]["restore_started"])

    def test_existing_owned_claim_after_lost_reply_is_adopted_without_second_create(self):
        with patch.object(KUBE, "get_optional", return_value=self.pvc) as read, \
             patch.object(RESTORE, "restore_backup") as restore, \
             patch.object(RESTORE, "restore_problem", return_value=""):
            result = server.STORAGE_ROUTES._restore_then_tidy(self.item, lambda item: ("running", 30, "Restoring data"))
        self.assertEqual("running", result[0])
        self.assertTrue(self.item["ref"]["restore_started"])
        restore.assert_not_called()
        # The claim is read through Longhorn's own reader, as it was before.
        claim = "/api/v1/namespaces/lab/persistentvolumeclaims/example-data"
        self.assertTrue(read.call_args_list)
        for call in read.call_args_list:
            self.assertEqual(((claim,), {"read": server.LH.kget}), (call.args, call.kwargs))

    def test_unrelated_claim_is_not_adopted_and_deleted_started_claim_is_not_recreated(self):
        self.pvc["metadata"]["annotations"] = {}
        with patch.object(KUBE, "get_optional", return_value=self.pvc), \
             patch.object(RESTORE, "restore_backup") as restore:
            with self.assertRaisesRegex(ValueError, "another operation"):
                server.STORAGE_ROUTES._restore_then_tidy(self.item, Mock())
            restore.assert_not_called()
        self.item["ref"]["restore_started"] = True
        with patch.object(KUBE, "get_optional", return_value=None), \
             patch.object(RESTORE, "restore_backup") as restore, \
             patch.object(server.STORAGE_ROUTES, "cleanup_restores"):
            result = server.STORAGE_ROUTES._restore_then_tidy(self.item, lambda item: ("failed", 0, "Destination deleted"))
        self.assertEqual("failed", result[0])
        restore.assert_not_called()

    def test_snapshot_errors_are_visible_before_claim_binds(self):
        self.item["ref"]["restore_started"] = True
        self.pvc["status"]["phase"] = "Pending"
        resolver = Mock()
        with patch.object(KUBE, "get_optional", return_value=self.pvc), \
             patch.object(RESTORE, "restore_problem", return_value="backup is unavailable"):
            result = server.STORAGE_ROUTES._restore_then_tidy(self.item, resolver)
        self.assertEqual("failed", result[0])
        self.assertIn("backup is unavailable", result[2])
        resolver.assert_not_called()

    def test_explicit_resume_resets_dependency_wait_timeout(self):
        self.cfg["snapshot_wait_started"] = 1
        self.item["status"] = "queued"
        with patch.object(KUBE, "get_optional", return_value=None), \
             patch.object(RESTORE, "restore_backup", return_value={"created": False, "message": "Waiting"}):
            self.assertEqual("running", server.STORAGE_ROUTES._restore_then_tidy(self.item, Mock())[0])
        self.assertGreater(self.cfg["snapshot_wait_started"], 1)

    def test_growth_waits_for_data_restore_then_checks_class_fresh(self):
        self.item["ref"]["restore_started"] = True
        with patch.object(KUBE, "get_optional", return_value=self.pvc), \
             patch.object(RESTORE, "restore_problem", return_value=""), \
             patch.object(RESTORE, "finish_restore_resize") as grow:
            server.STORAGE_ROUTES._restore_then_tidy(self.item, lambda item: ("running", 50, "Restoring"))
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


class CleanupControllerRaceTests(unittest.TestCase):
    """Row 98 B: snapshot-controller writes the restore snapshot and its
    content while Homestead removes them."""
    def setUp(self):
        self.enterContext(patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(groups=("snapshot.storage.k8s.io",)))
        self.kube.react("GET", CSI.API, lambda *_: {"kind": "APIResourceList", "groupVersion": "snapshot.storage.k8s.io/v1"})
        self.enterContext(patch.multiple(CSI, kget=KUBE.kget, ksend=KUBE.ksend))
        self.name = CSI.import_backup("lab", "example-data", "backup-a", "disk-a", "s3://example/backup-a", "Filesystem")
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"name": "example-data", "namespace": "lab"},
                       "spec": {"dataSource": {"name": self.name, "kind": "VolumeSnapshot", "apiGroup": "snapshot.storage.k8s.io"}},
                       "status": {"phase": "Bound"}})
        self.snapshot = CSI.API + "/namespaces/lab/volumesnapshots/" + self.name
        self.content = CSI.API + "/volumesnapshotcontents/" + self.name

    def change(self, path, body):
        self.kube("PATCH", path, body, ctype="application/merge-patch+json")

    def deletes(self, path):
        return [b["preconditions"] for m, p, b, _ in self.kube.writes if m == "DELETE" and p == path]

    def test_content_is_removed_after_the_controller_annotates_it_for_the_snapshot_delete(self):
        # snapshot-controller marks the content when its snapshot is deleted,
        # after cleanup read the content and before it deletes it.
        self.kube.react("DELETE", self.snapshot, lambda *_: self.change(self.content, {"metadata": {"annotations": {
            "snapshot.storage.kubernetes.io/volumesnapshot-being-deleted": "yes"}}}))
        self.assertEqual([self.name], CSI.cleanup())
        self.assertIsNone(self.kube.object(self.snapshot))
        self.assertIsNone(self.kube.object(self.content))
        self.assertEqual([{"uid": "uid-1"}], self.deletes(self.content))

    def test_terminating_snapshot_and_content_are_not_deleted_again(self):
        for path in (self.snapshot, self.content):
            self.change(path, {"metadata": {"finalizers": ["snapshot.storage.kubernetes.io/protection"]}})
        self.kube.react("DELETE", self.snapshot, lambda *_: self.change(self.snapshot, {"status": {"readyToUse": False}}))
        CSI.cleanup()
        self.change(self.snapshot, {"status": {"readyToUse": True}})
        CSI.cleanup()
        self.assertEqual(1, len(self.deletes(self.snapshot)))
        self.assertEqual(1, len(self.deletes(self.content)))
        self.assertTrue(self.kube.object(self.snapshot)["metadata"].get("deletionTimestamp"))

    def test_replaced_content_with_a_new_uid_is_still_refused(self):
        content = KUBE.kget(self.content)
        self.kube("DELETE", self.content)
        self.kube.add({**content, "metadata": {**content["metadata"], "uid": "replacement"}})
        with self.assertRaises(urllib.error.HTTPError) as error:
            CSI._delete(self.content, content)
        self.assertEqual(409, error.exception.code)
        self.assertEqual("replacement", self.kube.object(self.content)["metadata"]["uid"])


class StoppedRestoreTests(unittest.TestCase):
    """A restore's VolumeSnapshot and VolumeSnapshotContent (Retain) were
    removed only once its claim bound: a restore whose claim was never made,
    or that was cancelled and its claim deleted, kept both for good."""
    def setUp(self):
        self.enterContext(patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(groups=("snapshot.storage.k8s.io",)))
        self.kube.react("GET", CSI.API, lambda *_: {"kind": "APIResourceList", "groupVersion": "snapshot.storage.k8s.io/v1"})
        self.enterContext(patch.multiple(CSI, kget=KUBE.kget, ksend=KUBE.ksend))
        self.name = CSI.import_backup("lab", "example-data", "backup-a", "disk-a", "s3://example/backup-a", "Filesystem")
        self.snapshot = CSI.API + "/namespaces/lab/volumesnapshots/" + self.name
        self.content = CSI.API + "/volumesnapshotcontents/" + self.name
        self.claim = "/api/v1/namespaces/lab/persistentvolumeclaims/example-data"

    def add_claim(self, snapshot=None, **meta):
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"name": "example-data", "namespace": "lab",
                                    "annotations": {"homestead.io/restored-from-backup": "backup-a"}, **meta},
                       "spec": {"dataSource": {"name": snapshot or self.name, "kind": "VolumeSnapshot",
                                               "apiGroup": "snapshot.storage.k8s.io"}},
                       "status": {"phase": "Pending"}})

    def gone(self):
        return self.kube.object(self.snapshot) is None and self.kube.object(self.content) is None

    def test_a_restore_whose_claim_was_never_made_is_tidied(self):
        self.assertEqual([self.name], CSI.discard("lab", "example-data", self.name))
        self.assertTrue(self.gone())

    def test_a_live_claim_restoring_from_it_keeps_it(self):
        self.add_claim()
        self.assertEqual([], CSI.discard("lab", "example-data", self.name))
        self.assertEqual([], CSI.discard("lab", "example-data"))
        self.assertFalse(self.gone())

    def test_a_claim_restoring_from_another_snapshot_or_being_deleted_does_not(self):
        self.add_claim(snapshot="homestead-restore-other")
        self.assertEqual([self.name], CSI.discard("lab", "example-data"))
        self.assertTrue(self.gone())

    def test_other_claims_and_moves_are_left_alone(self):
        self.assertEqual([], CSI.discard("lab", "other-data"))
        self.assertEqual([], CSI.discard("other", "example-data"))
        moved = CSI.import_backup("lab", "example-data", "backup-a", "disk-a", "s3://example/backup-a", "Filesystem",
                                  "0123456789ab")
        self.assertEqual([self.name], CSI.discard("lab", "example-data"))
        self.assertIsNotNone(self.kube.object(CSI.API + "/volumesnapshotcontents/" + moved))

    def test_cancelling_a_restore_removes_its_snapshot_with_its_claim(self):
        import homestead_cancel as CANCEL
        self.add_claim()
        self.enterContext(patch.multiple(CANCEL, kget=KUBE.kget, ksend=KUBE.ksend, tidy_restore=None))
        item = {"ref": {"namespace": "lab", "name": "example-data", "backup": "backup-a", "snapshot": self.name}}
        self.assertIn("being deleted", CANCEL.restore_cancel(item, {}))
        self.assertIsNone(self.kube.object(self.claim))
        self.assertTrue(self.gone())

    def test_cancelling_one_restore_keeps_another_restore_of_the_same_claim_name(self):
        """Restore B made its pair and has not made its claim yet: cancelling
        restore A removes only A's own pair."""
        import homestead_cancel as CANCEL
        other = CSI.import_backup("lab", "example-data", "backup-b", "disk-a", "s3://example/backup-b", "Filesystem",
                                  "attempt-b")
        self.add_claim()
        self.enterContext(patch.multiple(CANCEL, kget=KUBE.kget, ksend=KUBE.ksend, tidy_restore=None))
        item = {"ref": {"namespace": "lab", "name": "example-data", "backup": "backup-a", "snapshot": self.name}}
        CANCEL.restore_cancel(item, {})
        self.assertTrue(self.gone())
        self.assertIsNotNone(self.kube.object(CSI.API + "/volumesnapshotcontents/" + other))
        self.assertIsNotNone(self.kube.object(CSI.API + "/namespaces/lab/volumesnapshots/" + other))

    def test_a_job_keeps_the_snapshot_its_restore_made(self):
        import homestead_longhorn as LONGHORN
        import homestead_longhorn_restore as RESTORE
        request = {"backup": "backup-a", "namespace": "lab", "name": "example-data"}
        result = {**request, "created": True, "snapshot": self.name, "source_size_bytes": 1, "message": "Restore started"}
        with patch.object(RESTORE, "restore_plan", return_value={}), \
                patch.object(RESTORE, "restore_backup", return_value=dict(result)), \
                patch.object(LONGHORN.OPS, "start", side_effect=lambda *args: args[4]) as start:
            LONGHORN.restore_reviewed(dict(request))
        self.assertEqual(self.name, start.call_args.args[4]["snapshot"])
        item = {"status": "running", "ref": {"namespace": "lab", "name": "example-data",
                                             "restore_config": {**request, "restore_id": "x"}, "restore_started": False}}
        # Where the job looks the restore up: its own import of the defining
        # module once it has one, else homestead_longhorn's re-export.
        lookup = getattr(server.STORAGE_ROUTES, "RESTORE", None) or server.LH
        with patch.object(lookup, "restore_backup", return_value=dict(result)), \
                patch.object(lookup, "restore_problem", return_value=""):
            server.STORAGE_ROUTES._restore_then_tidy(item, lambda work: ("running", 30, "Restoring data"))
        self.assertEqual(self.name, item["ref"]["snapshot"])

    def test_cancel_keeps_a_claim_it_did_not_make_and_its_snapshot(self):
        import homestead_cancel as CANCEL
        self.add_claim(annotations={})
        self.enterContext(patch.multiple(CANCEL, kget=KUBE.kget, ksend=KUBE.ksend, tidy_restore=None))
        item = {"ref": {"namespace": "lab", "name": "example-data", "backup": "backup-a"}}
        self.assertIn("kept", CANCEL.restore_cancel(item, {}))
        self.assertFalse(self.gone())


if __name__ == "__main__":
    unittest.main()
