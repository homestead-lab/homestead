"""Transfer cleanup reclaims owned artifacts and survives interrupted deletion."""
import copy
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_csi_restore as CSI
import homestead_longhorn as LH
import homestead_longhorn_target as BACKUP_TARGET
import homestead_longhorn_backups as BACKUPS
import homestead_move_state as STATE
import homestead_move_target as TARGET
import homestead_move_plan as PLAN
import homestead_move_phases as PHASE
import homestead_move_actions as ACT
import homestead_move_source as SOURCE
import homestead_transfer_cleanup as CLEANUP
import test_move_flow as fixtures
from test_move_flow import FakeCluster
import homestead_kube as K
import homestead_operations as OPS
from fakes.kube import Kube

ID, OTHER, UID = "0123456789ab", "abcdef012345", "original-source-uid"


def artifact(name, transfer=ID, source_uid=UID, **fields):
    return {"metadata": {"name": name, "uid": name + "-uid", "resourceVersion": "1",
                         "annotations": CLEANUP.ownership(transfer, source_uid)}, **fields}


class API(FakeCluster):
    """Complete list inventories, deletion finalizers and API identity fences."""
    def __init__(self):
        super().__init__()
        self.delayed, self.denied = set(), set()

    def get(self, path):
        if path in self.denied:
            raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
        if path in ("/api/v1/persistentvolumeclaims", CSI.API + "/volumesnapshots"):
            suffix = "persistentvolumeclaims" if path.startswith("/api/") else "volumesnapshots"
            return {"items": [copy.deepcopy(v) for k, v in self.objects.items()
                              if "/" + suffix + "/" in k]}
        return super().get(path)

    def send(self, method, path, body=None, **kwargs):
        if method == "DELETE" and path in self.objects:
            meta = self.objects[path]["metadata"]
            self.assert_fence(meta, body)
            if path in self.delayed:
                self.calls.append((method, path, copy.deepcopy(body)))
                meta["deletionTimestamp"] = "2026-10-02T12:00:00Z"
                return {}
        return super().send(method, path, body, **kwargs)

    @staticmethod
    def assert_fence(meta, body):
        # The API server checks only the preconditions a DELETE gives; cleanup
        # always gives the uid (row 98).
        wanted = body.get("preconditions") or {}
        if "uid" not in wanted or any(meta.get(key) != value for key, value in wanted.items()):
            raise urllib.error.HTTPError("", 409, "resource replaced", {}, None)


class ArtifactCleanupTests(unittest.TestCase):
    def setUp(self):
        self.api = API()

    def put(self, plural, name, **fields):
        path = CLEANUP.LH + "/" + plural + "/" + name
        self.api.put(path, artifact(name, **fields))
        return path

    def clean(self):
        return CLEANUP.source(self.api.get, self.api.send, ID, UID)

    def test_current_and_superseded_backups_snapshots_and_image_are_removed(self):
        for name in ("first", "retry"):
            self.put("snapshots", name, spec={"volume": "source-disk"})
            self.put("backups", name, spec={"snapshotName": name})
        self.put("backupbackingimages", "base-image")
        foreign = self.put("backups", "manual", transfer=OTHER, status={"state": "Completed"})
        replaced = self.put("snapshots", "replacement", source_uid="replacement-uid")
        self.assertTrue(self.clean()["complete"])
        self.assertEqual({foreign, replaced}, set(self.api.objects))
        self.assertTrue(all(call[2]["propagationPolicy"] == "Background" for call in self.api.calls))

    def test_backup_finalizer_protects_snapshot_and_image_until_next_pass(self):
        backup = self.put("backups", "disk-backup", spec={"snapshotName": "disk-snapshot"})
        snapshot = self.put("snapshots", "disk-snapshot")
        image = self.put("backupbackingimages", "base-image")
        self.api.delayed.add(backup)
        self.assertFalse(self.clean()["complete"])
        self.assertIn(snapshot, self.api.objects)
        self.assertIn(image, self.api.objects)
        self.assertFalse(self.clean()["complete"])
        self.assertEqual(1, len(self.api.calls), "Do not strip finalizers or repeat accepted deletion")
        del self.api.objects[backup]
        self.assertTrue(self.clean()["complete"])
        self.assertEqual({}, self.api.objects)

    def test_shared_or_undiscovered_backing_image_dependency_is_preserved(self):
        image = self.put("backupbackingimages", "base-image")
        foreign = self.put("backups", "another-transfer", transfer=OTHER)
        self.assertFalse(self.clean()["complete"])
        self.assertIn(image, self.api.objects)
        self.api.objects[foreign]["status"] = {"state": "Completed", "volumeBackingImageName": "base-image"}
        result = self.clean()
        self.assertTrue(result["complete"])
        self.assertIn("shared image backup", result["retained"][0])
        self.assertIn(image, self.api.objects)

    def test_snapshot_used_by_another_backup_is_not_removed(self):
        snapshot = self.put("snapshots", "shared-snapshot")
        self.put("backups", "manual", transfer=OTHER, spec={"snapshotName": "shared-snapshot"})
        self.assertFalse(self.clean()["complete"])
        self.assertIn(snapshot, self.api.objects)

    def test_another_transfer_preparing_its_snapshot_protects_shared_image_backup(self):
        image = self.put("backupbackingimages", "base-image")
        snapshot = self.put("snapshots", "preparing-snapshot", transfer=OTHER, spec={"volume": "other-disk"})
        self.put("volumes", "other-disk", transfer=OTHER, spec={"backingImage": "base-image"})
        self.assertFalse(self.clean()["complete"])
        self.assertIn(image, self.api.objects)
        del self.api.objects[snapshot]
        self.assertTrue(self.clean()["complete"])
        self.assertNotIn(image, self.api.objects)

    def test_unowned_legacy_artifacts_are_never_adopted(self):
        path = self.put("backups", "old-backup")
        self.api.objects[path]["metadata"]["annotations"] = {}
        self.assertTrue(self.clean()["complete"])
        self.assertEqual([], self.api.calls)

    def test_inventory_failures_and_pagination_never_mean_no_dependencies(self):
        self.put("snapshots", "snapshot-a")
        self.api.denied.add(CLEANUP.LH + "/backups")
        with self.assertRaises(urllib.error.HTTPError):
            self.clean()
        self.assertEqual([], self.api.calls)
        with self.assertRaisesRegex(ValueError, "complete"):
            CLEANUP.items(lambda _: {"items": [], "metadata": {"continue": "next"}}, "list")
        with self.assertRaisesRegex(ValueError, "inventory"):
            CLEANUP.optional(lambda _: {}, "object")

    def test_replaced_object_is_not_deleted_after_inventory_read(self):
        path = self.put("snapshots", "snapshot-a")
        original = self.api.get(path)
        self.api.objects[path]["metadata"]["uid"] = "replacement"
        with self.assertRaises(urllib.error.HTTPError) as error:
            CLEANUP.delete(self.api.get, self.api.send, path, original)
        self.assertEqual(409, error.exception.code)
        self.assertEqual("replacement", self.api.objects[path]["metadata"]["uid"])

    def test_missing_identity_blocks_deletion(self):
        path = self.put("backups", "backup-a")
        del self.api.objects[path]["metadata"]["resourceVersion"]
        with self.assertRaisesRegex(ValueError, "identity"):
            self.clean()
        self.assertEqual([], self.api.calls)

    def test_cleanup_works_after_source_workload_is_gone(self):
        self.put("backups", "backup-a")
        with mock.patch.multiple(SOURCE, kget=self.api.get, ksend=self.api.send), \
                mock.patch.object(SOURCE, "_object", side_effect=AssertionError("Do not follow a replacement source")):
            self.assertTrue(SOURCE.cleanup(ID, UID)["complete"])

    def test_replacement_source_cannot_create_new_transfer_backups(self):
        replacement = artifact("source")
        replacement["metadata"]["uid"] = "replacement-source-uid"
        with mock.patch.object(SOURCE, "_object", return_value=replacement) as read, \
                mock.patch.object(SOURCE, "_merge") as mutate:
            with self.assertRaisesRegex(ValueError, "replaced"):
                SOURCE.backup("container", "example", expected_uid=UID, cleanup_id=ID)
        read.assert_called_once()
        mutate.assert_not_called()

    def test_ownership_is_stamped_before_longhorn_creates(self):
        with mock.patch.multiple(LH, kget=self.api.get, ksend=self.api.send), \
                mock.patch.object(BACKUP_TARGET, "backup_target", return_value={"configured": True}), \
                mock.patch.object(LH, "_bust"):
            BACKUPS.ensure_move_backup("disk-a", "snapshot-a", "backup-a", cleanup_id=ID, source_uid=UID)
            snap = CLEANUP.LH + "/snapshots/snapshot-a"
            self.api.objects[snap]["status"] = {"readyToUse": True}
            BACKUPS.ensure_move_backup("disk-a", "snapshot-a", "backup-a", cleanup_id=ID, source_uid=UID)
        with mock.patch.object(SOURCE, "ksend", self.api.send):
            SOURCE._backup_backing_image("base-image", cleanup_id=ID, source_uid=UID)
        self.assertEqual(3, len(self.api.calls))
        for _, _, body in self.api.calls:
            self.assertEqual(CLEANUP.ownership(ID, UID), body["metadata"]["annotations"])

    def test_existing_backing_image_backup_is_shared_without_adoption(self):
        with mock.patch.object(SOURCE, "ksend", side_effect=urllib.error.HTTPError("", 409, "exists", {}, None)) as send:
            SOURCE._backup_backing_image("base-image", cleanup_id=ID, source_uid=UID)
        self.assertEqual(["POST"], [c.args[0] for c in send.call_args_list])


class RestoreMetadataCleanupTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        with mock.patch.multiple(CSI, kget=self.api.get, ksend=self.api.send):
            self.name = CSI.import_backup("dest", "os-disk", "backup-a", "disk-a", "s3://example/backup-a", "Block", ID)
        self.snapshot = CSI.API + "/namespaces/dest/volumesnapshots/" + self.name
        self.content = CSI.API + "/volumesnapshotcontents/" + self.name

    def claim(self, phase="Pending", ref=False, namespace="dest"):
        path = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/os-disk"
        self.api.put(path, {"metadata": {"name": "os-disk", "namespace": namespace},
            "spec": {"dataSourceRef" if ref else "dataSource": {"name": self.name,
                "kind": "VolumeSnapshot", "apiGroup": "snapshot.storage.k8s.io", **({"namespace": "dest"} if ref else {})}},
            "status": {"phase": phase}})
        return path

    def clean(self, cancelled=False):
        return CSI.cleanup_transfer(self.api.get, self.api.send, ID, cancelled)

    def test_success_removes_only_retained_metadata_after_all_users_bind(self):
        claim = self.claim()
        self.assertTrue(self.clean())
        self.api.objects[claim]["status"]["phase"] = "Bound"
        self.assertEqual([], self.clean())
        self.assertEqual({claim}, set(self.api.objects))
        deletes = [call for call in self.api.calls if call[0] == "DELETE"]
        self.assertEqual(2, len(deletes))
        self.assertTrue(all("finalizers" not in str(call) for call in deletes))

    def test_cancel_protects_bound_pending_and_cross_namespace_users(self):
        for phase, ref, namespace in (("Pending", False, "dest"), ("Bound", False, "dest"), ("Pending", True, "other")):
            with self.subTest(phase=phase, ref=ref, namespace=namespace):
                path = self.claim(phase, ref, namespace)
                self.assertTrue(self.clean(True))
                self.assertIn(self.content, self.api.objects)
                self.assertIn(self.snapshot, self.api.objects)
                del self.api.objects[path]
        self.assertEqual([], self.clean(True))
        self.assertEqual({}, self.api.objects)

    def test_lost_snapshot_delete_reply_and_content_gone_are_reconciled(self):
        self.api.delayed.add(self.snapshot)
        self.assertTrue(self.clean(True))
        self.assertNotIn(self.content, self.api.objects)
        self.assertIn(self.snapshot, self.api.objects)
        self.assertTrue(self.clean(True))
        del self.api.objects[self.snapshot]
        self.assertEqual([], self.clean(True))

    def test_success_also_waits_for_a_snapshot_after_its_content_disappears(self):
        self.claim("Bound")
        self.api.delayed.add(self.snapshot)
        self.assertTrue(self.clean())
        self.assertNotIn(self.content, self.api.objects)
        self.assertTrue(self.clean())
        del self.api.objects[self.snapshot]
        self.assertEqual([], self.clean())

    def test_incomplete_claim_inventory_preserves_restore_metadata(self):
        self.api.denied.add("/api/v1/persistentvolumeclaims")
        with self.assertRaises(urllib.error.HTTPError):
            self.clean(True)
        self.assertIn(self.content, self.api.objects)
        self.assertIn(self.snapshot, self.api.objects)

    def test_delete_policy_mismatch_is_never_forced(self):
        self.api.objects[self.content]["spec"]["deletionPolicy"] = "Delete"
        with self.assertRaisesRegex(ValueError, "retained"):
            self.clean(True)
        self.assertEqual(2, len(self.api.calls), "Only original creates occurred")

    def test_foreign_snapshot_uid_blocks_pair_cleanup(self):
        self.api.objects[self.content]["spec"]["volumeSnapshotRef"]["uid"] = "old-uid"
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.clean(True)
        self.assertIn(self.content, self.api.objects)
        self.assertIn(self.snapshot, self.api.objects)

    def test_orphan_snapshot_is_not_removed_while_another_claim_uses_it(self):
        del self.api.objects[self.content]
        claim = self.claim("Bound", ref=True)
        self.assertTrue(self.clean(True))
        del self.api.objects[claim]
        self.assertEqual([], self.clean(True))

    def test_success_without_binding_evidence_is_pending(self):
        self.assertTrue(self.clean())
        self.assertIn(self.content, self.api.objects)

    def test_legacy_restore_metadata_is_not_deleted_by_transfer_cleanup(self):
        for obj in self.api.objects.values():
            obj["metadata"]["annotations"].pop(next(iter(CLEANUP.ownership(ID, UID))))
        self.assertEqual([], self.clean(True))
        self.assertEqual(2, len(self.api.objects))


class DestinationImageTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.image = CLEANUP.LH + "/backingimages/base-image"
        self.api.put(self.image, artifact("base-image"))
        self.move = {"id": ID, "claims": [{"claim": "os-disk", "cleanup_volume": "disk-a"}]}

    def clean(self, cancelled=True):
        return CLEANUP.destination(self.api.get, self.api.send, self.move, cancelled)

    def test_success_keeps_final_image_but_cancel_reclaims_unused_image(self):
        self.assertTrue(self.clean(False)["complete"])
        self.assertIn(self.image, self.api.objects)
        self.assertTrue(self.clean()["complete"])
        self.assertNotIn(self.image, self.api.objects)

    def test_own_disk_reclamation_is_waited_out_but_shared_and_retained_disks_are_kept(self):
        path = CLEANUP.LH + "/volumes/disk-a"
        self.api.put(path, artifact("disk-a", spec={"backingImage": "base-image"}))
        self.assertFalse(self.clean()["complete"])
        self.move["claims"][0]["cleanup_retain"] = True
        result = self.clean()
        self.assertTrue(result["complete"])
        self.assertTrue(result["retained"])
        self.assertIn(self.image, self.api.objects)
        del self.api.objects[path]
        self.api.put(CLEANUP.LH + "/volumes/other-disk", artifact("other-disk", spec={"backingImage": "base-image"}))
        self.assertTrue(self.clean()["retained"])

    def test_foreign_images_and_failed_volume_inventory_are_protected(self):
        self.api.denied.add(CLEANUP.LH + "/volumes")
        with self.assertRaises(urllib.error.HTTPError):
            self.clean()
        self.api.objects[self.image]["metadata"]["annotations"] = CLEANUP.ownership(OTHER, UID)
        self.assertTrue(self.clean()["complete"])
        self.assertIn(self.image, self.api.objects)


class CleanupRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.EngineTests.setUp(self)
        self.move = {"id": ID, "name": "example-app", "cluster": "source", "namespace": "dest",
                     "source_namespace": "lab", "source_uid": UID, "kind": "container", "claims": [],
                     "status": "running", "phase": "starting", "created_at": "2026-10-02T12:00:00Z",
                     "updated_at": "2026-10-02T12:00:00Z", "cleanup_protocol": 1,
                     "flags": {"quiesced": True}}
        self.remote = self.enterContext(mock.patch.object(STATE.CLIENT, "remote",
            return_value={"complete": True, "retained": []}))

    def test_success_is_persisted_before_remote_cleanup_and_restart_does_not_replay_transfer(self):
        def unavailable(*args):
            self.assertEqual("succeeded", STATE._find(ID)["status"])
            raise TimeoutError("source unavailable")
        self.remote.side_effect = unavailable
        PHASE._finish(self.move, "succeeded", "Finished")
        STATE._store(self.move)
        self.assertTrue(STATE._public(STATE._find(ID))["resource_cleanup_pending"])
        with self.assertRaisesRegex(ValueError, "cleanup"):
            ACT.dismiss(ID)
        self.remote.side_effect = None
        with mock.patch.object(PHASE, "_tick", side_effect=AssertionError("Do not restore again")):
            PHASE.tick_all()
        done = STATE._find(ID)
        self.assertFalse(done["flags"]["resource_cleanup_pending"])
        self.assertEqual("cleanup", self.remote.call_args.args[2]["action"])
        self.assertEqual(UID, self.remote.call_args.args[2]["expected_uid"])
        ACT.dismiss(ID)
        self.assertIsNone(STATE._find(ID))

    def test_failed_transfer_keeps_backup_for_retry_and_cannot_be_silently_dismissed(self):
        PHASE._finish(self.move, "failed", "Restore failed")
        self.remote.assert_not_called()
        self.assertFalse(STATE._public(self.move)["can_dismiss"])
        with self.assertRaisesRegex(ValueError, "failed transfer"):
            ACT.dismiss(ID)

    def test_destination_finalizers_delay_source_cleanup(self):
        with mock.patch.object(CLEANUP, "destination", return_value={"pending": ["restore"], "retained": []}):
            PHASE._finish(self.move, "succeeded", "Finished")
        self.remote.assert_not_called()
        self.assertTrue(self.move["flags"]["resource_cleanup_pending"])

    def test_restart_during_cancellation_deletes_resources_here_before_recovering_source(self):
        # Never two copies at once, nor one address on both clusters (#1064):
        # what the move made here goes first, then the source starts again.
        self.move.update(status="cancelled", flags={"quiesced": True, "cancelling": True, "resource_cleanup_pending": True})
        STATE._store(self.move)
        actions = []
        with mock.patch.object(STATE, "_source_action", side_effect=lambda *a: actions.append("release") or {"detail": "Resumed"}), \
                mock.patch.object(PHASE, "_remove_created", side_effect=lambda *a: actions.append("remove") or []):
            PHASE.tick_all()
            PHASE.tick_all()
        self.assertEqual(["remove", "release", "remove"], actions)
        done = STATE._find(ID)
        self.assertEqual("cancelled", done["status"])
        self.assertTrue(done["flags"]["source_released"])
        self.assertNotIn("cancelling", done["flags"])
        self.assertFalse(done["flags"]["resource_cleanup_pending"])

    def test_lost_source_stop_reply_preserves_recovery_intent_for_moves(self):
        self.move["flags"] = {}
        with mock.patch.object(STATE, "_source_action", side_effect=TimeoutError("reply lost")):
            with self.assertRaises(TimeoutError):
                PHASE._quiescing(self.move)
        self.assertTrue(STATE._source_held(STATE._find(ID)))

    def test_cleanup_retry_is_separate_from_restarting_a_successful_transfer(self):
        self.move.update(status="succeeded", flags={"quiesced": True, "resource_cleanup_pending": True})
        STATE._store(self.move)
        result = ACT.retry(ID)
        self.assertEqual("succeeded", result["status"])
        self.assertFalse(result["resource_cleanup_pending"])

    def test_transfer_credentials_are_tracked_until_normal_deletion_finishes(self):
        api = API()
        path = "/api/v1/namespaces/longhorn-system/secrets/transfer-token"
        api.put(path, artifact("transfer-token"))
        api.delayed.add(path)
        self.move.update(status="succeeded", transfer_target={"secret": "transfer-token"})
        with mock.patch.multiple(STATE, kget=api.get, ksend=api.send):
            TARGET._cleanup_target(self.move)
            self.assertTrue(self.move["flags"]["target_cleanup_pending"])
            self.assertIn("transfer_target", self.move)
            del api.objects[path]
            TARGET._cleanup_target(self.move)
        self.assertNotIn("target_cleanup_pending", self.move["flags"])
        self.assertNotIn("transfer_target", self.move)

    def test_unfinished_cleanup_and_retry_records_are_not_evicted_by_history_limit(self):
        rows = [{**copy.deepcopy(self.move), "id": f"{n:012x}", "created_at": str(n), "status": "failed"} for n in range(60)]
        rows += [{**copy.deepcopy(self.move), "id": f"{n:012x}", "created_at": str(n), "status": "succeeded",
                  "flags": {"resource_cleanup_pending": True}} for n in range(60, 120)]
        rows += [{**copy.deepcopy(self.move), "id": f"{n:012x}", "created_at": str(n), "status": "succeeded", "flags": {}} for n in range(120, 220)]
        STATE._write(rows)
        saved = STATE._read()
        self.assertEqual(170, len(saved))
        self.assertTrue(all(any(m["id"] == f"{n:012x}" for m in saved) for n in range(120)))


class TransferCleanupFlowTests(unittest.TestCase):
    """Real source/destination state machines with observable Longhorn CRs."""
    run_until_settled = fixtures.EngineTests.run_until_settled

    def setUp(self):
        fixtures.EngineTests.setUp(self)
        real_ensure = self.lh.ensure_move_backup

        def ensure(volume, snapshot, backup, **kwargs):
            with mock.patch.multiple(LH, kget=self.cluster.get, ksend=self.cluster.send), \
                    mock.patch.object(BACKUP_TARGET, "backup_target", return_value={"configured": True}), \
                    mock.patch.object(LH, "_bust"):
                BACKUPS.ensure_move_backup(volume, snapshot, backup, **kwargs)
                self.cluster.objects[CLEANUP.LH + "/snapshots/" + snapshot]["status"] = {"readyToUse": True}
                BACKUPS.ensure_move_backup(volume, snapshot, backup, **kwargs)
            result = real_ensure(volume, snapshot, backup, **kwargs)
            if getattr(self, "pending_backup", False) and self.lh.made[-1].get("state", "Completed") == "Completed":
                self.lh.made[-1]["state"] = "InProgress"
            return result
        self.enterContext(mock.patch.object(self.lh, "ensure_move_backup", side_effect=ensure))

    def artifact_paths(self):
        return {p for p in self.cluster.objects if p.startswith((CLEANUP.LH + "/backups/", CLEANUP.LH + "/snapshots/"))}

    def test_success_reclaims_real_backup_artifacts_but_keeps_both_final_disks(self):
        move = PLAN.start("source", "container", "frigate", "moved", "automatic")
        done = self.run_until_settled(move_id=move["id"])
        self.assertEqual("succeeded", done["status"], done["message"])
        self.assertFalse(done["resource_cleanup_pending"], done["cleanup_message"])
        self.assertEqual(set(), self.artifact_paths())
        for namespace in ("lab", "moved"):
            self.assertIn(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/frigate-config", self.cluster.objects)

    def fail_transfer(self):
        self.pending_backup = True
        move = PLAN.start("source", "container", "frigate", "moved", "automatic")
        for _ in range(15):
            self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)
            PHASE.tick_all()
            if self.lh.made:
                break
        self.lh.made[0].update(state="Error", error="backup failed")
        PHASE.tick_all()
        self.assertEqual("failed", STATE._find(move["id"])["status"])
        return move

    def test_failure_retains_real_backup_artifacts_until_cancel(self):
        move = self.fail_transfer()
        self.assertEqual(2, len(self.artifact_paths()))
        cancelled = ACT.abandon(move["id"])
        self.assertEqual("cancelled", cancelled["status"])
        self.assertFalse(cancelled["resource_cleanup_pending"], cancelled["cleanup_message"])
        self.assertEqual(set(), self.artifact_paths())
        self.assertEqual(1, self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]["spec"]["replicas"])

    def test_successful_retry_also_reclaims_the_superseded_failed_attempt(self):
        move = self.fail_transfer()
        self.assertEqual(2, len(self.artifact_paths()))
        self.pending_backup = False
        ACT.retry(move["id"])
        done = self.run_until_settled(move_id=move["id"])
        self.assertEqual("succeeded", done["status"], done["message"])
        self.assertEqual(2, len(self.lh.made))
        self.assertEqual(set(), self.artifact_paths())

    def test_vm_copy_reclaims_its_backup_artifacts_after_source_resume(self):
        import test_cluster_copy as copy_fixtures
        self.vm_path = copy_fixtures.ClusterCopyTests.vm_path
        self.vmi_path = copy_fixtures.ClusterCopyTests.vmi_path
        copy_fixtures.ClusterCopyTests.seed_vm(self)
        move = PLAN.start("source", "vm", "desktop", "copied", transfer_mode="copy")
        done = copy_fixtures.ClusterCopyTests.settle(self, move["id"])
        self.assertEqual("succeeded", done["status"], done["message"])
        self.assertFalse(done["resource_cleanup_pending"], done["cleanup_message"])
        self.assertEqual(set(), self.artifact_paths())
        self.assertEqual("Always", self.cluster.objects[self.vm_path()]["spec"]["runStrategy"])
        self.assertEqual("Halted", self.cluster.objects[self.vm_path("copied")]["spec"]["runStrategy"])


def changes_before_delete(kube, path, change, times=None):
    """A controller writes the object between Homestead's read and its DELETE
    (on every DELETE, or only the first times)."""
    left = [times]
    def answer(method, _path, body):
        if left[0] is not None:
            if left[0] <= 0:
                return
            left[0] -= 1
        kube("PATCH", path, change, ctype="application/merge-patch+json")
    kube.react("DELETE", path, answer)


class ControllerRaceTests(unittest.TestCase):
    """Row 98 A: longhorn-manager and snapshot-controller keep writing the
    artifacts a transfer stamped; that must not hold cleanup."""
    def setUp(self):
        # The fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))

    def longhorn(self, kind, name, **fields):
        return {"apiVersion": "longhorn.io/v1beta2", "kind": kind, **fields,
                "metadata": {"name": name, "namespace": "longhorn-system",
                             "annotations": CLEANUP.ownership(ID, UID)}}

    def test_status_change_before_a_backup_delete_does_not_hold_the_first_pass(self):
        with Kube(self.longhorn("Backup", "disk-backup", spec={"snapshotName": "disk-snapshot"})) as kube:
            backup = CLEANUP.LH + "/backups/disk-backup"
            changes_before_delete(kube, backup, {"status": {"state": "Completed", "progress": 100}})
            self.assertEqual([], CLEANUP.source(K.kget, K.ksend, ID, UID)["pending"])
            self.assertIsNone(kube.object(backup))
            deletes = [b["preconditions"] for m, p, b, _ in kube.writes if m == "DELETE"]
            self.assertEqual([["uid"]], [sorted(d) for d in deletes])

    def test_in_use_checked_artifacts_keep_the_version_and_retry_after_a_change(self):
        # A Snapshot or (backup) backing image is deleted only after a check
        # that nothing uses it. Its resourceVersion stays in the DELETE, and a
        # change since that check (409) leaves it pending for the next pass,
        # which checks its users again, instead of failing the cleanup.
        image = self.longhorn("BackupBackingImage", "base-image")
        with Kube(self.longhorn("Snapshot", "disk-snapshot", spec={"volume": "source-disk"}), image) as kube:
            snapshot, backup_image = CLEANUP.LH + "/snapshots/disk-snapshot", CLEANUP.LH + "/backupbackingimages/base-image"
            changes_before_delete(kube, snapshot, {"status": {"readyToUse": True}}, times=1)
            changes_before_delete(kube, backup_image, {"status": {"progress": 100}}, times=1)
            result = CLEANUP.source(K.kget, K.ksend, ID, UID)
            self.assertEqual(["snapshot disk-snapshot", "image backup base-image"], result["pending"])
            self.assertIsNotNone(kube.object(snapshot))
            self.assertIsNotNone(kube.object(backup_image))
            self.assertEqual([], CLEANUP.source(K.kget, K.ksend, ID, UID)["pending"])
            self.assertIsNone(kube.object(snapshot))
            self.assertIsNone(kube.object(backup_image))
            deletes = [sorted(b["preconditions"]) for m, p, b, _ in kube.writes if m == "DELETE"]
            self.assertEqual([["resourceVersion", "uid"]] * 4, deletes)

    def test_a_cancelled_moves_backing_image_keeps_the_version_and_retries(self):
        image = self.longhorn("BackingImage", "restored-image")
        with Kube(image) as kube:
            path = CLEANUP.LH + "/backingimages/restored-image"
            kube.add({"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
                      "metadata": {"name": "other", "namespace": "longhorn-system"}, "spec": {}})
            changes_before_delete(kube, path, {"status": {"diskFileStatusMap": {}}}, times=1)
            move = {"id": ID, "claims": []}
            self.assertEqual(["backing image restored-image"], CLEANUP.destination(K.kget, K.ksend, move, cancelled=True)["pending"])
            self.assertTrue(CLEANUP.destination(K.kget, K.ksend, move, cancelled=True)["complete"])
            deletes = [sorted(b["preconditions"]) for m, p, b, _ in kube.writes if m == "DELETE"]
            self.assertEqual([["resourceVersion", "uid"]] * 2, deletes)

    def test_terminating_artifact_is_not_deleted_again(self):
        backup = self.longhorn("Backup", "disk-backup")
        backup["metadata"]["finalizers"] = ["longhorn.io"]
        with Kube(backup) as kube:
            path = CLEANUP.LH + "/backups/disk-backup"
            changes_before_delete(kube, path, {"status": {"state": "Deleting"}})
            self.assertFalse(CLEANUP.source(K.kget, K.ksend, ID, UID)["complete"])
            kube("PATCH", path, {"status": {"progress": 50}}, ctype="application/merge-patch+json")
            self.assertFalse(CLEANUP.source(K.kget, K.ksend, ID, UID)["complete"])
            self.assertEqual(1, sum(m == "DELETE" for m, *_ in kube.writes))

    def test_same_name_replacement_with_a_new_uid_is_still_refused(self):
        with Kube(self.longhorn("Snapshot", "disk-snapshot")) as kube:
            path = CLEANUP.LH + "/snapshots/disk-snapshot"
            original = K.kget(path)
            kube("DELETE", path)
            kube.add(self.longhorn("Snapshot", "disk-snapshot"))
            with self.assertRaises(urllib.error.HTTPError) as error:
                CLEANUP.delete(K.kget, K.ksend, path, original)
            self.assertEqual(409, error.exception.code)
            self.assertIsNotNone(kube.object(path))

    def test_destination_workloads_claims_and_credentials_keep_the_full_version_check(self):
        # Only the transfer's Longhorn/CSI artifacts are delete by uid alone.
        # What a move created at the destination (workload, Service, claim)
        # and its backup credentials can be edited or adopted by a person, so
        # their DELETE still pins the resourceVersion.
        mark = {"annotations": CLEANUP.ownership(ID, UID)}
        service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": "dest", **mark}}
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "data", "namespace": "dest", **mark}}
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "transfer-creds", "namespace": "longhorn-system", **mark}}
        with Kube(service, claim, secret) as kube, \
                mock.patch.multiple(STATE, kget=K.kget, ksend=K.ksend, _store=mock.Mock(), DATA_DIR=OPS.DATA_DIR), \
                mock.patch.object(STATE, "LH") as lh, mock.patch.object(TARGET, "_target_journal", return_value={}):
            lh.backup_target_state.return_value = {"harvester": False, "value": {"credentialSecret": "other"}}
            PHASE._remove_created({"id": ID, "kind": "volume", "namespace": "dest", "name": "data",
                                   "claims": [{"claim": "data"}]})
            TARGET._release_target({"id": ID, "status": "cancelled", "transfer_target": {"secret": "transfer-creds"}})
            deletes = {p.rsplit("/", 1)[1]: sorted(b["preconditions"]) for m, p, b, _ in kube.writes if m == "DELETE"}
            self.assertEqual({"web": ["resourceVersion", "uid"], "data": ["resourceVersion", "uid"],
                              "transfer-creds": ["resourceVersion", "uid"]}, deletes)

    def test_restore_snapshot_content_is_removed_after_the_controller_annotates_it(self):
        with Kube(groups=("snapshot.storage.k8s.io",)) as kube:
            with mock.patch.multiple(CSI, kget=K.kget, ksend=K.ksend):
                name = CSI.import_backup("dest", "os-disk", "backup-a", "disk-a", "s3://example/backup-a", "Block", ID)
            kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                      "metadata": {"name": "os-disk", "namespace": "dest"},
                      "spec": {"dataSource": {"name": name, "kind": "VolumeSnapshot", "apiGroup": "snapshot.storage.k8s.io"}},
                      "status": {"phase": "Bound"}})
            snapshot = CSI.API + "/namespaces/dest/volumesnapshots/" + name
            content = CSI.API + "/volumesnapshotcontents/" + name
            def annotate(method, _path, body):
                kube("PATCH", content, {"metadata": {"annotations": {"snapshot.storage.kubernetes.io/volumesnapshot-being-deleted": "yes"}}},
                     ctype="application/merge-patch+json")
            kube.react("DELETE", snapshot, annotate)
            self.assertEqual([], CSI.cleanup_transfer(K.kget, K.ksend, ID))
            self.assertIsNone(kube.object(snapshot))
            self.assertIsNone(kube.object(content))


class SourceCleanupRouteTests(unittest.TestCase):
    def test_cleanup_uses_admin_route_and_passes_original_identity(self):
        import server
        self.assertEqual("admin", server.needed_role("/api/move/source", "POST"))
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/move/source", {}
        handler._guard = lambda _: False
        handler._body = lambda: {"action": "cleanup", "kind": "vm", "name": "example",
                                 "transfer_id": ID, "expected_uid": UID, "namespace": "dest"}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.MOVE_SOURCE, "cleanup", return_value={"ok": True, "complete": True}) as cleanup:
            handler.do_POST()
        cleanup.assert_called_once_with(ID, UID)
        self.assertEqual(200, handler._send.call_args.args[0])

    def test_cleanup_rejects_missing_or_invalid_identity_before_reading(self):
        with mock.patch.object(SOURCE, "kget", side_effect=AssertionError("No resource read")):
            for transfer_id, source_uid in (("", UID), (ID, ""), ("../unsafe", UID)):
                with self.subTest(transfer_id=transfer_id, source_uid=source_uid):
                    with self.assertRaises(ValueError):
                        SOURCE.cleanup(transfer_id, source_uid)


if __name__ == "__main__":
    unittest.main()
