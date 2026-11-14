"""A restart cannot repeat a restore into an unrelated or deleted claim."""
import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_longhorn_restore as RESTORE
import homestead_csi_restore as CSI
import homestead_kube as KUBE
import homestead_operations as OPS
from fakes.kube import Kube, no_cluster


def setUpModule():
    # No cluster here: a read on the way to what a test checks fails at once, as on CI,
    # instead of looking the API server's name up (tests/fakes/kube.py no_cluster).
    unittest.enterModuleContext(no_cluster())


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
            result = server.STORAGE_ROUTES._restore_then_tidy(self.item, Mock())
            self.assertEqual("failed", result[0])
            self.assertIn("another operation", result[2])
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


class RefusedRestoreTests(unittest.TestCase):
    """A plain restore that gets refused ends as failed, in the refusal's own
    words, and keeps whichever claim is there. It used to raise, so the job
    stayed running ("Needs attention") for good and only a cancel ended it,
    which deletes the claim."""
    CLAIM = "/api/v1/namespaces/lab/persistentvolumeclaims/example-data"

    def setUp(self):
        self.enterContext(patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(groups=("longhorn.io", "snapshot.storage.k8s.io")))
        self.enterContext(patch.multiple(OPS, kget=KUBE.kget))
        self.enterContext(patch.multiple(server.LH, kget=KUBE.kget, ksend=KUBE.ksend))
        self.enterContext(patch.dict(OPS.RESOLVERS, {"volume-restore": server.STORAGE_ROUTES._restore_then_tidy}))

    def start(self, started=False, **cfg):
        ref = {"namespace": "lab", "name": "example-data", "backup": "backup-a", "restore_started": started,
               "restore_config": {"backup": "backup-a", "namespace": "lab", "name": "example-data",
                                  "restore_id": "this-restore", **cfg}}
        return OPS.start("volume-restore", "Restore example-data",
                         {"kind": "PersistentVolumeClaim", "name": "example-data", "namespace": "lab"}, "/volumes", ref)["id"]

    def job(self, ident):
        return next(job for job in OPS.list_operations() if job["id"] == ident)

    def claim(self, restore_id=None):
        annotations = {"homestead.io/restore-id": restore_id} if restore_id else {}
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "status": {"phase": "Pending"},
                       "metadata": {"name": "example-data", "namespace": "lab", "annotations": annotations}})

    def assert_failed_keeping_the_claim(self, job, words):
        self.assertEqual("failed", job["status"])
        self.assertIn(words, job["message"])
        self.assertNotIn("Needs attention", job["message"])
        self.assertNotIn("temporarily unavailable", job["message"])
        self.assertEqual([], [write for write in self.kube.writes if write[0] == "DELETE"])

    def test_a_claim_another_operation_made_fails_the_job_and_stays(self):
        self.claim()
        self.assert_failed_keeping_the_claim(self.job(self.start()), "created by another operation")
        self.assertIsNotNone(self.kube.object(self.CLAIM))

    def test_a_claim_replaced_after_the_restore_started_fails_the_job_and_stays(self):
        self.claim("someone-else")
        self.assert_failed_keeping_the_claim(self.job(self.start(started=True)), "replaced by another operation")
        self.assertIsNotNone(self.kube.object(self.CLAIM))

    def waiting_for_csi_setup(self):
        """A job past its first look, still waiting for snapshot setup, its 15 minutes up."""
        waiting = {"created": False, "message": "Installing CSI snapshots"}
        with patch.object(RESTORE, "restore_backup", return_value=waiting):
            ident = self.start()
            self.assertEqual("running", self.job(ident)["status"])
        now = time.time()
        self.enterContext(patch.object(server.STORAGE_ROUTES, "time", SimpleNamespace(time=lambda: now + 16 * 60)))
        return ident

    def test_csi_setup_that_never_gets_ready_fails_and_says_what_to_do(self):
        job = self.job(self.waiting_for_csi_setup())
        self.assert_failed_keeping_the_claim(job, "CSI snapshot setup did not become ready")
        self.assertIn("resume this restore", job["message"])

    def fifteen_minutes_on(self):
        now = time.time()
        self.enterContext(patch.object(server.STORAGE_ROUTES, "time", SimpleNamespace(time=lambda: now + 16 * 60)))

    def test_fifteen_minutes_waiting_on_something_else_names_it_not_csi(self):
        # Through the real restore_backup: its reason was the job's last answer.
        self.add_namespace(), self.add_backup(url=""), self.add_class()
        ident = self.start(storage_class="example-class")
        self.assertIn("not available yet", self.job(ident)["message"])
        self.fifteen_minutes_on()
        job = self.job(ident)
        self.assertEqual("failed", job["status"])
        self.assertEqual("The restore could not start within 15 minutes (backup URL is not available yet). "
                         "Resume it once that has cleared.", job["message"])

    def test_fifteen_minutes_of_kubernetes_errors_names_them_not_csi(self):
        self.kube.fail(self.CLAIM, 503, times=3)
        ident = self.start()
        self.assertIn("temporarily unavailable", self.job(ident)["message"])
        self.fifteen_minutes_on()
        job = self.job(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("could not start within 15 minutes (HTTP 503)", job["message"])
        self.assertNotIn("CSI", job["message"])

    def test_a_snapshot_set_up_differently_is_named_at_fifteen_minutes(self):
        self.add_namespace(), self.add_backup(), self.add_class()
        refusal = "Restore snapshot x already exists with different settings or is being deleted"
        with patch.object(CSI, "ensure_support", return_value={"ready": True}), \
                patch.object(CSI, "import_backup", side_effect=ValueError(refusal)):
            ident = self.start(storage_class="example-class")
            self.assertEqual("running", self.job(ident)["status"])
        self.fifteen_minutes_on()
        self.assertIn(f"could not start within 15 minutes ({refusal})", self.job(ident)["message"])

    def test_after_a_passing_error_clears_it_is_csi_again(self):
        # The last answer counts: an error that cleared is not why it timed out.
        self.kube.fail(self.CLAIM, 503, times=1)
        ident = self.start()
        self.job(ident)
        with patch.object(RESTORE, "restore_backup", return_value={"created": False, "message": "Installing CSI snapshots"}):
            self.assertEqual("Installing CSI snapshots", self.job(ident)["message"])
        self.fifteen_minutes_on()
        self.assertIn("CSI snapshot setup did not become ready", self.job(ident)["message"])

    def test_resuming_after_the_csi_setup_is_repaired_waits_afresh(self):
        # The message says to resume: the old start time would refuse it again at once.
        ident = self.waiting_for_csi_setup()
        job = self.job(ident)
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["resumable"])
        OPS.resume(ident)
        with patch.object(RESTORE, "restore_backup", return_value={"created": False, "message": "Installing CSI snapshots"}):
            job = self.job(ident)
        self.assertEqual(("running", "Installing CSI snapshots"), (job["status"], job["message"]))

    def test_a_refusal_from_the_restore_itself_fails_in_its_words(self):
        # The backup was deleted while the job waited for CSI snapshot setup.
        self.assert_failed_keeping_the_claim(self.job(self.start()), "backup was not found")

    def test_a_passing_kubernetes_error_is_still_waited_out(self):
        self.kube.fail(self.CLAIM, 500, times=1)
        job = self.job(self.start())
        self.assertEqual("running", job["status"])
        self.assertIn("temporarily unavailable", job["message"])

    def test_only_a_marked_refusal_ends_the_job(self):
        # Anything else restore_backup raises may pass: it waits, as it did.
        for error in (KUBE.Incomplete("the list came back incomplete"), json.JSONDecodeError("cut short", "{", 1),
                      ValueError("backup URL is not available yet"),
                      ValueError("Cluster shutdown is active; open Cluster for progress or recovery")):
            with self.subTest(error=error):
                with patch.object(RESTORE, "restore_backup", side_effect=error):
                    job = self.job(self.start())
                self.assertEqual("running", job["status"])
                self.assertIn("temporarily unavailable", job["message"])
                OPS.cancel(job["id"], {})

    def add_backup(self, **status):
        self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
                       "metadata": {"name": "backup-a", "namespace": "longhorn-system"},
                       "status": {"state": "Completed", "url": "s3://example/backup-a", "volumeName": "disk-a",
                                  "volumeSize": str(5 * 1024 ** 3), **status}})

    def add_class(self, **fields):
        self.kube.add({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "example-class"},
                       "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "2"}, **fields})

    def add_namespace(self):
        self.kube.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "lab"}})

    def test_an_incomplete_inventory_during_the_real_restore_waits(self):
        # CSI._items turns KUBE.Incomplete into a plain ValueError: "could not be checked" is not a refusal.
        self.add_namespace(), self.add_backup(), self.add_class()
        self.enterContext(patch.multiple(CSI, kget=KUBE.kget, ksend=KUBE.ksend))
        read = KUBE.items

        def items(path, **kwargs):
            if "/deployments" in path:
                raise KUBE.Incomplete("the list came back incomplete")
            return read(path, **kwargs)
        with patch.object(KUBE, "items", items):
            job = self.job(self.start(storage_class="example-class"))
        self.assertEqual("running", job["status"], job["message"])
        self.assertIn("could not be checked", job["message"])
        self.assertEqual([], self.kube.writes)

    def test_a_backup_url_not_there_yet_waits(self):
        self.add_namespace(), self.add_backup(url=""), self.add_class()
        job = self.job(self.start(storage_class="example-class"))
        self.assertEqual("running", job["status"], job["message"])
        self.assertIn("not available yet", job["message"])

    def test_a_class_a_restore_cannot_use_is_a_refusal(self):
        self.add_namespace(), self.add_backup(), self.add_class(parameters={"migratable": "true"})
        self.assert_failed_keeping_the_claim(self.job(self.start(storage_class="example-class")), "migratable disabled")

    def test_a_class_longhorn_does_not_manage_is_a_refusal(self):
        self.add_namespace(), self.add_backup(), self.add_class(provisioner="rancher.io/local-path")
        self.assert_failed_keeping_the_claim(self.job(self.start(storage_class="example-class")), "not managed by Longhorn")

    def test_a_namespace_that_is_gone_is_a_refusal(self):
        self.add_backup(), self.add_class()
        self.assert_failed_keeping_the_claim(self.job(self.start(storage_class="example-class")), "does not exist")

    def test_a_class_that_is_not_a_regular_one_is_a_refusal(self):
        self.add_namespace(), self.add_backup(), self.add_class(parameters={"fromBackup": "backup-a"})
        self.assert_failed_keeping_the_claim(self.job(self.start(storage_class="example-class")), "regular Longhorn storage class")

    def test_a_size_above_the_backup_on_a_class_that_cannot_grow_is_a_refusal(self):
        self.add_namespace(), self.add_backup(), self.add_class()
        self.assert_failed_keeping_the_claim(self.job(self.start(storage_class="example-class", size_gb=8)), "allows volume expansion")

    def test_a_size_below_the_backup_is_a_refusal(self):
        self.add_namespace(), self.add_backup(), self.add_class()
        self.assert_failed_keeping_the_claim(self.job(self.start(storage_class="example-class", size_gb=1)), "cannot be smaller")

    def test_a_snapshot_being_deleted_waits(self):
        self.add_namespace(), self.add_backup(), self.add_class()
        self.enterContext(patch.multiple(CSI, kget=KUBE.kget, ksend=KUBE.ksend))
        with patch.object(CSI, "ensure_support", return_value={"ready": True}), \
                patch.object(CSI, "import_backup", side_effect=ValueError("Restore snapshot x already exists with different settings or is being deleted")):
            job = self.job(self.start(storage_class="example-class"))
        self.assertEqual("running", job["status"], job["message"])


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


class EndedRestorePairTests(unittest.TestCase):
    """A plain restore that lost the race for its claim name failed and kept
    its snapshot pair: cleanup() waits for a claim restoring from it, a failed
    job has no Cancel, so it was removed by hand (#1303). The leader's sweep
    now removes the pair an ended restore job made, by the restore id the job
    stamped on it, and nothing else."""
    CLAIMS = "/api/v1/namespaces/lab/persistentvolumeclaims"
    RESTORE_ID = "0123456789abcdef01234567"

    def setUp(self):
        import homestead_helper_sweep as SWEEP
        self.sweeper = SWEEP
        self.enterContext(patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(groups=("longhorn.io", "snapshot.storage.k8s.io")))
        self.kube.react("GET", CSI.API, lambda *_: {"kind": "APIResourceList", "groupVersion": "snapshot.storage.k8s.io/v1"})
        self.enterContext(patch.multiple(OPS, kget=KUBE.kget))
        self.enterContext(patch.multiple(server.LH, kget=KUBE.kget, ksend=KUBE.ksend))
        self.enterContext(patch.multiple(CSI, kget=KUBE.kget, ksend=KUBE.ksend))
        self.enterContext(patch.object(CSI, "ensure_support", return_value={"ready": True}))
        self.enterContext(patch.dict(OPS.RESOLVERS, {"volume-restore": server.STORAGE_ROUTES._restore_then_tidy}))
        self.kube.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "lab"}})
        self.kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
                       "metadata": {"name": "backup-a", "namespace": "longhorn-system"},
                       "status": {"state": "Completed", "url": "s3://example/backup-a", "volumeName": "disk-a",
                                  "volumeSize": str(5 * 1024 ** 3)}})
        self.kube.add({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "example-class"},
                       "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "2"}})

    def start(self, restore_id=RESTORE_ID):
        ref = {"namespace": "lab", "name": "example-data", "backup": "backup-a", "restore_started": False,
               "restore_config": {"backup": "backup-a", "namespace": "lab", "name": "example-data",
                                  "storage_class": "example-class", "restore_id": restore_id,
                                  "annotations": {"homestead.io/restore-id": restore_id}}}
        return OPS.start("volume-restore", "Restore example-data",
                         {"kind": "PersistentVolumeClaim", "name": "example-data", "namespace": "lab"}, "/volumes", ref)["id"]

    def job(self, ident):
        """The job, after one more round of the jobs' poll."""
        return next(job for job in OPS.list_operations() if job["id"] == ident)

    def someone_takes_the_name_first(self):
        """Another operation makes the claim between the restore's look and its create."""
        def first(*_):
            if not self.kube.object(self.CLAIMS + "/example-data"):
                self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "status": {"phase": "Pending"},
                               "metadata": {"name": "example-data", "namespace": "lab"}})
            return None    # the fake answers: 409, the name is taken
        self.kube.react("POST", self.CLAIMS, first)

    def pairs(self):
        return sorted(row["metadata"]["name"] for row in KUBE.kget(CSI.API + "/volumesnapshotcontents")["items"]) + \
            sorted(row["metadata"]["name"] for row in KUBE.kget(CSI.API + "/namespaces/lab/volumesnapshots")["items"])

    def sweep(self):
        self.sweeper.sweep(now=time.time())

    def lost_race(self):
        self.someone_takes_the_name_first()
        ident = self.start()
        self.assertEqual("running", self.job(ident)["status"], "the create's 409 is waited out once")
        job = self.job(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("created by another operation", job["message"])
        self.assertEqual(2, len(self.pairs()), "the pair it made is still there")
        return ident

    def test_a_restore_that_lost_its_claim_name_has_its_pair_removed_once_it_failed(self):
        self.lost_race()
        self.sweep()
        self.assertEqual([], self.pairs())
        claim = self.kube.object(self.CLAIMS + "/example-data")
        self.assertIsNotNone(claim, "the other operation's claim is not touched")
        self.assertIsNone(claim["metadata"].get("deletionTimestamp"))

    def test_the_pair_is_looked_for_once_not_every_minute(self):
        self.lost_race()
        self.sweep()
        def listed():
            return len([path for path, _ in self.kube.reads if "volumesnapshotcontents?" in path])
        before = listed()
        self.assertGreater(before, 0)
        self.sweep()
        self.assertEqual(before, listed())

    def test_a_restore_resumed_and_ended_again_has_its_new_pair_removed_too(self):
        ident = self.lost_race()
        self.sweep()
        # Resumed, it made its pair again, then ended again.
        CSI.import_backup("lab", "example-data", "backup-a", "disk-a", "s3://example/backup-a", "Filesystem", self.RESTORE_ID)
        with OPS.transaction() as t:
            items = t.items
            item = next(i for i in items if i["id"] == ident)
            item.update(status="running", finished_at="")
            OPS._finish(item, "failed", 8, "The destination PVC was created by another operation")
            item["finished_at"] = "2099-01-01T00:00:00Z"
            t.save(items)
        self.assertEqual(2, len(self.pairs()))
        self.sweep()
        self.assertEqual([], self.pairs())

    def test_a_restore_resumed_while_the_sweep_runs_keeps_its_pair(self):
        """The sweep read the job as failed; a person resumes it before the
        pair is deleted. The resumed restore reuses that pair (its name comes
        from the restore id), so deleting it would leave its claim on a gone
        snapshot."""
        ident = self.lost_race()
        self.kube.discard(self.CLAIMS + "/example-data")    # the other claim went: a resume can carry on
        resumed = []

        def resume_first(*_):
            if not resumed:
                resumed.append(OPS.resume(ident))
            return None
        self.kube.react("GET", CSI.API + "/volumesnapshotcontents", resume_first)
        self.sweep()
        self.assertTrue(resumed)
        self.assertEqual("running", OPS.stored()[0]["status"])
        self.assertEqual(2, len(self.pairs()), "the resumed restore's pair is kept")
        self.assertFalse(any(m == "DELETE" and "volumesnapshot" in p for m, p, *_ in self.kube.writes))

    def test_a_pair_that_cannot_be_removed_is_not_looked_for_every_minute(self):
        """Not this job's pair after all (its snapshot pairs with other
        content): left, and not listed again each minute."""
        self.lost_race()
        snapshot = CSI.API + "/namespaces/lab/volumesnapshots/" + self.pairs()[0]
        obj = self.kube.object(snapshot)
        obj["spec"]["source"]["volumeSnapshotContentName"] = "homestead-restore-other"
        self.kube.discard(snapshot)
        self.kube.add(obj)
        self.sweep()
        self.assertEqual(2, len(self.pairs()))

        def listed():
            return len([path for path, _ in self.kube.reads if "volumesnapshotcontents?" in path])
        before = listed()
        self.sweep()
        self.assertEqual(before, listed())

    def test_a_running_restore_keeps_its_pair(self):
        """It made its pair and has not made its claim yet."""
        self.kube.fail(self.CLAIMS, 500, method="POST")
        ident = self.start()
        self.assertEqual("running", self.job(ident)["status"])
        self.sweep()
        self.assertEqual(2, len(self.pairs()))

    def test_another_restores_pair_and_one_from_before_the_stamp_are_kept(self):
        mine = self.lost_race()
        other = CSI.import_backup("lab", "example-data", "backup-b", "disk-a", "s3://example/backup-b", "Filesystem",
                                  "fedcba9876543210fedcba98")
        before = CSI.import_backup("lab", "example-data", "backup-c", "disk-a", "s3://example/backup-c", "Filesystem")
        self.sweep()
        self.assertEqual(sorted([other, before]) * 2, self.pairs())
        self.assertIsNotNone(mine)

    def test_a_failed_restore_whose_own_claim_restores_from_the_pair_keeps_it(self):
        self.kube.fail(self.CLAIMS, 500, method="POST", applied=True)
        ident = self.start()
        self.assertEqual("running", self.job(ident)["status"], "its claim was made, the answer lost")
        self.assertIsNotNone(self.kube.object(self.CLAIMS + "/example-data"))
        with OPS.transaction() as t:
            items = t.items
            OPS._finish(next(i for i in items if i["id"] == ident), "failed", 8, "CSI restore snapshot failed: example")
            t.save(items)
        self.sweep()
        self.assertEqual(2, len(self.pairs()), "cleanup() removes it once the claim binds")
        self.kube("DELETE", self.CLAIMS + "/example-data")
        self.sweep()
        self.assertEqual([], self.pairs(), "the claim is gone, so nothing restores from it")

    def test_a_pair_made_before_the_stamp_still_matches_on_a_resume(self):
        name = CSI.import_backup("lab", "example-data", "backup-a", "disk-a", "s3://example/backup-a", "Filesystem")
        content = self.kube.object(CSI.API + "/volumesnapshotcontents/" + name)
        self.assertNotIn("homestead.io/restore-id", content["metadata"].get("annotations") or {})
        stamped = CSI.import_backup("lab", "other-data", "backup-a", "disk-a", "s3://example/backup-a", "Filesystem",
                                    self.RESTORE_ID)
        content = self.kube.object(CSI.API + "/volumesnapshotcontents/" + stamped)
        self.assertEqual(self.RESTORE_ID, content["metadata"]["annotations"]["homestead.io/restore-id"])
        # A pair made by an older Homestead under the stamped name, without the stamp, is adopted as before.
        for path in (CSI.API + "/volumesnapshotcontents/" + stamped, CSI.API + "/namespaces/lab/volumesnapshots/" + stamped):
            obj = self.kube.object(path)
            obj["metadata"]["annotations"].pop("homestead.io/restore-id")
            self.kube.discard(path)
            self.kube.add(obj)
        self.assertEqual(stamped, CSI.import_backup("lab", "other-data", "backup-a", "disk-a", "s3://example/backup-a",
                                                    "Filesystem", self.RESTORE_ID))


if __name__ == "__main__":
    unittest.main()
