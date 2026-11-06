"""Temporary transfer targets must not become permanent backup pointers."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_longhorn_target as BACKUP_TARGET
import homestead_move as client
import homestead_move_engine as engine
import homestead_move_state as STATE
import homestead_move_target as TARGET
import homestead_move_phases as PHASE
import homestead_move_actions as ACT
from test_move_flow import FakeCluster, FakeLonghorn, FakeNetwork, FakeOps


class TransferTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = FakeCluster()
        self.lh = FakeLonghorn(self.cluster)
        self.lh.target = {"configured": True, "url": "s3://local@us-east-1/",
                          "secret": "local-keys", "interval": "10m"}
        engine.bind(self.cluster.get, self.cluster.send, self.lh, client, FakeNetwork(),
                    FakeOps(), self.tmp.name, "lab")
        self.there = {"url": "s3://source@us-east-1/", "endpoint": "http://192.0.2.211:9070",
                      "credentials": {"AWS_ACCESS_KEY_ID": "source-key", "AWS_SECRET_ACCESS_KEY": "source-secret",
                                      "AWS_ENDPOINTS": "http://192.0.2.211:9070"}}
        for patch in (mock.patch.object(client, "remote", return_value=self.there),
                      mock.patch.object(client, "answers", return_value=True),
                      mock.patch.object(STATE, "_here_endpoint", return_value="http://local:9000"),
                      mock.patch.object(engine, "after_finish", None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.original = self.lh.backup_target_state()

    def move(self, identity="0123456789ab"):
        move = {"id": identity, "cluster": "shed", "kind": "container", "name": "app",
                "namespace": "lab", "claims": [], "phase": "joining", "status": "running",
                "flags": {}, "created_at": STATE._now(), "updated_at": STATE._now()}
        STATE._store(move)
        return move

    def test_finish_restores_url_secret_interval_and_removes_transfer_secret(self):
        move = self.move()
        PHASE._joining(move)
        self.assertNotEqual(self.original, self.lh.backup_target_state())
        secret = self.lh.target["secret"]
        PHASE._finish(move, "succeeded", "Arrived")
        self.assertEqual(self.original, self.lh.backup_target_state())
        self.assertNotIn(f"/api/v1/namespaces/longhorn-system/secrets/{secret}", self.cluster.objects)
        self.assertEqual({}, TARGET._target_journal())
        self.assertNotIn("source-secret", json.dumps(STATE._public(move)))

    def test_cancel_restores_without_needing_the_linked_source(self):
        move = self.move()
        PHASE._joining(move)
        STATE._store(move)
        with mock.patch.object(client, "remote", side_effect=client.Unreachable("unlinked")):
            result = ACT.abandon(move["id"])
        self.assertEqual("cancelled", result["status"])
        self.assertEqual(self.original, self.lh.backup_target_state())

    def test_failed_transfer_restores_then_retry_reacquires_its_original_store(self):
        move = self.move()
        PHASE._joining(move)
        move["phase"] = "syncing"
        borrowed = self.lh.backup_target_state()
        with mock.patch.dict(PHASE.HANDLERS, {"syncing": mock.Mock(side_effect=ValueError("Restore refused"))}):
            PHASE._tick(move)
        STATE._store(move)
        self.assertEqual("failed", move["status"])
        self.assertEqual(self.original, self.lh.backup_target_state())
        ACT.retry(move["id"])
        with mock.patch.object(client, "remote", side_effect=AssertionError("must use saved backup location")), \
                mock.patch.dict(PHASE.HANDLERS, {"syncing": lambda m: STATE._advance(m, "restoring", 50, "Found")}):
            PHASE.tick_all()
        self.assertEqual(borrowed, self.lh.backup_target_state())
        self.assertEqual("restoring", STATE._find(move["id"])["phase"])

    def test_unconfigured_destination_is_unconfigured_again(self):
        self.lh.target = {"configured": False, "url": "", "secret": "", "interval": ""}
        previous = self.lh.backup_target_state()
        move = self.move()
        PHASE._joining(move)
        PHASE._finish(move, "succeeded", "Arrived")
        self.assertEqual(previous, self.lh.backup_target_state())

    def test_admin_change_is_kept_when_transfer_finishes(self):
        move = self.move()
        PHASE._joining(move)
        self.lh.set_backup_target("nfs://new:/backups", poll="20m")
        admin = self.lh.backup_target_state()
        PHASE._finish(move, "succeeded", "Arrived")
        self.assertEqual(admin, self.lh.backup_target_state())

    def test_admin_can_keep_the_borrowed_target_without_losing_its_secret(self):
        move = self.move()
        PHASE._joining(move)
        secret = self.lh.target["secret"]
        self.lh.target["interval"] = "20m"
        admin = self.lh.backup_target_state()
        PHASE._finish(move, "succeeded", "Arrived")
        self.assertEqual(admin, self.lh.backup_target_state())
        self.assertIn(f"/api/v1/namespaces/longhorn-system/secrets/{secret}", self.cluster.objects)

    def test_concurrent_transfer_waits_without_overwriting_keys_or_target(self):
        first, second = self.move(), self.move("abcdef012345")
        PHASE._joining(first)
        borrowed = self.lh.backup_target_state()
        PHASE._joining(second)
        self.assertEqual("joining", second["phase"])
        self.assertIn("Waiting for another transfer", second["message"])
        self.assertEqual(borrowed, self.lh.backup_target_state())
        PHASE._finish(first, "succeeded", "Arrived")
        PHASE._joining(second)
        self.assertEqual("quiescing", second["phase"])
        PHASE._finish(second, "succeeded", "Arrived")
        self.assertEqual(self.original, self.lh.backup_target_state())

    def test_restart_after_lost_apply_response_recovers_durable_original(self):
        move = self.move()
        apply = self.lh.replace_backup_target_state
        def lost_reply(expected, replacement):
            apply(expected, replacement)
            raise OSError("lost reply")
        with mock.patch.object(self.lh, "replace_backup_target_state", side_effect=lost_reply):
            with self.assertRaises(client.Unreachable):
                PHASE._joining(move)
        reloaded = STATE._find(move["id"])
        PHASE._joining(reloaded)
        PHASE._finish(reloaded, "succeeded", "Arrived")
        self.assertEqual(self.original, self.lh.backup_target_state())

    def test_cleanup_failure_is_visible_and_automatically_retried(self):
        move = self.move()
        PHASE._joining(move)
        with mock.patch.object(self.lh, "replace_backup_target_state", side_effect=OSError("API down")):
            PHASE._finish(move, "succeeded", "Arrived")
        STATE._store(move)
        self.assertTrue(STATE._public(move)["backup_target_cleanup_pending"])
        with self.assertRaisesRegex(ValueError, "cleanup"):
            ACT.dismiss(move["id"])
        PHASE.tick_all()
        self.assertEqual(self.original, self.lh.backup_target_state())
        self.assertFalse(STATE._public(STATE._find(move["id"]))["backup_target_cleanup_pending"])

    def test_lost_cleanup_reply_recovers_without_overwriting_a_later_admin_change(self):
        move = self.move()
        PHASE._joining(move)
        replace = self.lh.replace_backup_target_state
        def lost_reply(expected, replacement):
            replace(expected, replacement)
            raise OSError("lost reply")
        with mock.patch.object(self.lh, "replace_backup_target_state", side_effect=lost_reply):
            PHASE._finish(move, "succeeded", "Arrived")
        STATE._store(move)
        self.lh.set_backup_target("nfs://admin:/backups", poll="20m")
        admin = self.lh.backup_target_state()
        PHASE.tick_all()
        self.assertEqual(admin, self.lh.backup_target_state())
        self.assertEqual({}, TARGET._target_journal())

    def test_harvester_restores_exact_old_endpoint_keys_and_options(self):
        self.lh.harvester = True
        self.lh.setting = {"type": "s3", "bucketName": "local", "bucketRegion": "us-east-1",
                           "endpoint": "http://local:9000", "accessKeyId": "old-ak", "secretAccessKey": "old-sk",
                           "virtualHostedStyle": True, "refreshIntervalInSeconds": 600}
        previous = self.lh.backup_target_state()
        move = self.move()
        PHASE._joining(move)
        PHASE._finish(move, "succeeded", "Arrived")
        self.assertEqual(previous, self.lh.backup_target_state())
        self.assertNotIn("old-sk", json.dumps(STATE._public(move)))


class TargetStateTests(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        saved = (LH.kget, LH.ksend, LH._cache, LH.DEFAULT_NS)
        self.addCleanup(lambda: LH.bind(*saved))   # put back what server.py bound
        LH.bind(self.cluster.get, self.cluster.send, {})
        self.path = f"{LH.API}/namespaces/longhorn-system/backuptargets/default"
        self.cluster.put(self.path, {"metadata": {"resourceVersion": "1"}, "spec": {
            "backupTargetURL": "nfs://old:/backups", "credentialSecret": "", "pollInterval": "10m",
            "unrelatedField": "keep"}})

    def test_resource_version_conflict_preserves_concurrent_update(self):
        previous = BACKUP_TARGET.backup_target_state()
        desired = BACKUP_TARGET.transfer_target_state(previous, "nfs://source:/backups", "", {})
        send = self.cluster.send
        def change_before_patch(method, path, body, **kwargs):
            self.cluster.objects[path]["metadata"]["resourceVersion"] = "2"
            self.cluster.objects[path]["spec"]["backupTargetURL"] = "nfs://admin:/backups"
            return send(method, path, body, **kwargs)
        with mock.patch.object(LH, "ksend", side_effect=change_before_patch):
            with self.assertRaises(Exception) as caught:
                BACKUP_TARGET.replace_backup_target_state(previous, desired)
        self.assertEqual(409, caught.exception.code)
        self.assertEqual("nfs://admin:/backups", self.cluster.objects[self.path]["spec"]["backupTargetURL"])

    def test_longhorn_round_trip_preserves_extra_fields(self):
        previous = BACKUP_TARGET.backup_target_state()
        desired = BACKUP_TARGET.transfer_target_state(previous, "nfs://source:/backups", "", {})
        self.assertTrue(BACKUP_TARGET.replace_backup_target_state(previous, desired))
        self.assertTrue(BACKUP_TARGET.replace_backup_target_state(desired, previous))
        self.assertEqual(previous, BACKUP_TARGET.backup_target_state())
        self.assertEqual("keep", self.cluster.objects[self.path]["spec"]["unrelatedField"])

    def test_missing_longhorn_target_returns_to_unconfigured(self):
        del self.cluster.objects[self.path]
        previous = BACKUP_TARGET.backup_target_state()
        desired = BACKUP_TARGET.transfer_target_state(previous, "nfs://source:/backups", "", {})
        self.assertTrue(BACKUP_TARGET.replace_backup_target_state(previous, desired))
        self.assertTrue(BACKUP_TARGET.replace_backup_target_state(desired, previous))
        self.assertEqual("", self.cluster.objects[self.path]["spec"]["backupTargetURL"])
        self.assertEqual(previous, BACKUP_TARGET.backup_target_state())

    def test_target_card_selects_the_default_target_when_several_exist(self):
        self.cluster.objects[self.path]["metadata"]["name"] = "default"
        self.cluster.put(self.path.rsplit("/", 1)[0], {"items": [
            {"metadata": {"name": "other"}, "spec": {"backupTargetURL": "nfs://other:/backups"}},
            self.cluster.objects[self.path]]})
        self.assertEqual("nfs://old:/backups", BACKUP_TARGET.backup_target()["url"])

    def test_harvester_round_trip_restores_keys_and_empty_setting(self):
        for value in ("", json.dumps({"type": "s3", "endpoint": "http://old:9000", "accessKeyId": "ak",
                                      "secretAccessKey": "sk", "virtualHostedStyle": True})):
            self.cluster.put(BACKUP_TARGET.HARVESTER_TARGET, {"metadata": {"resourceVersion": "1"}, "value": value})
            previous = BACKUP_TARGET.backup_target_state()
            desired = BACKUP_TARGET.transfer_target_state(previous, "nfs://source:/backups", "", {})
            self.assertTrue(BACKUP_TARGET.replace_backup_target_state(previous, desired))
            self.assertTrue(BACKUP_TARGET.replace_backup_target_state(desired, previous))
            self.assertEqual(previous, BACKUP_TARGET.backup_target_state())
