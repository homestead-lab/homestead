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
from rebind import keep_bound


class TransferTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = FakeCluster()
        self.lh = FakeLonghorn(self.cluster)
        self.lh.target = {"configured": True, "url": "s3://local@us-east-1/",
                          "secret": "local-keys", "interval": "10m"}
        keep_bound(self, engine, engine.NAMES)   # put back what engine.bind() sets, and the names client
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


class UnreadableJournalTests(unittest.TestCase):
    """move-backup-target.json says which transfer has borrowed this cluster's
    backup storage, and what to put back. A file that is there but cannot be
    read is never taken for "nobody has it", and never written over."""

    setUp, move = TransferTargetTests.setUp, TransferTargetTests.move

    def journal_path(self):
        return str(Path(STATE.DATA_DIR) / TARGET.TARGET_STORE)

    def broken(self, text):
        Path(self.journal_path()).write_text(text, encoding="utf-8")
        return Path(self.journal_path()).read_bytes()

    def test_a_journal_that_cannot_be_read_raises_naming_it(self):
        import homestead_shared as SHARED
        for text in ('{"owner": "0123', "[]", "null"):
            with self.subTest(text):
                self.broken(text)
                with self.assertRaisesRegex(SHARED.Unreadable, TARGET.TARGET_STORE):
                    TARGET._target_journal()

    def test_a_missing_journal_is_nobody_borrowing(self):
        self.assertEqual({}, TARGET._target_journal())

    def test_borrowing_never_writes_over_an_unreadable_journal(self):
        import homestead_shared as SHARED
        before = self.broken("[]")       # today: falsy, so taken for "nobody has it"
        move = self.move()
        with self.assertRaises(SHARED.Unreadable):
            TARGET._borrow_target(move)
        self.assertEqual(before, Path(self.journal_path()).read_bytes())
        self.assertEqual(self.original, self.lh.backup_target_state(), "the backup storage is not touched")

    def test_a_running_move_waits_and_says_why_rather_than_failing(self):
        move = self.move()
        move["phase"] = "quiescing"
        STATE._store(move)
        before = self.broken('{"owner": "0123')
        PHASE.tick_all()
        stored = STATE._find(move["id"])
        self.assertEqual("running", stored["status"], stored.get("message"))
        self.assertIn(TARGET.TARGET_STORE, stored["message"])
        self.assertTrue(stored["message"].startswith("Waiting"))
        self.assertEqual(before, Path(self.journal_path()).read_bytes())

    def test_clearing_finished_moves_over_an_unreadable_journal_is_refused_in_words(self):
        move = self.move()
        move["status"] = "succeeded"
        STATE._store(move)
        before = self.broken("null")
        with self.assertRaisesRegex(ValueError, TARGET.TARGET_STORE):
            ACT.dismiss(move["id"])
        self.assertEqual("succeeded", STATE._find(move["id"])["status"])
        self.assertEqual(before, Path(self.journal_path()).read_bytes())

    def test_a_cleanup_waiting_on_an_unreadable_journal_does_not_stop_other_moves(self):
        done = self.move("aaaaaaaaaaaa")
        done.update(status="succeeded", flags={"target_cleanup_pending": True}, transfer_target={"secret": ""})
        STATE._store(done)
        running = self.move("bbbbbbbbbbbb")
        running["phase"] = "quiescing"
        STATE._store(running)
        before = self.broken("[]")
        PHASE.tick_all()
        done = STATE._find("aaaaaaaaaaaa")
        self.assertIn("Backup storage cleanup is pending", done["message"])
        self.assertTrue(done["flags"]["target_cleanup_pending"])
        self.assertIn(TARGET.TARGET_STORE, STATE._find("bbbbbbbbbbbb")["message"], "the running move was still asked")
        self.assertEqual(before, Path(self.journal_path()).read_bytes())

    def test_a_retry_that_cannot_read_the_journal_says_so_and_claims_nothing(self):
        move = self.move()
        move.update(status="failed", phase="joining")
        STATE._store(move)
        before = self.broken('{"owner": "0123')
        reply = client.answer(lambda: ACT.retry(move["id"]))
        self.assertEqual(409, reply.code)
        error = reply.body["error"]
        self.assertIn(TARGET.TARGET_STORE, error)
        self.assertIn("it is not valid JSON", error)
        self.assertNotIn("releasing", error.lower())
        self.assertNotIn("could not record", error)
        self.assertEqual("failed", STATE._find(move["id"])["status"])
        self.assertEqual(before, Path(self.journal_path()).read_bytes())

    def test_a_pending_cleanup_says_the_journal_cannot_be_read_and_why(self):
        done = self.move("aaaaaaaaaaaa")
        done.update(status="succeeded", message="Arrived", flags={"target_cleanup_pending": True})
        STATE._store(done)
        self.broken("[]")
        TARGET._cleanup_target(done)
        self.assertIn(f"{TARGET.TARGET_STORE} on the data volume cannot be read", done["message"])
        self.assertIn("does not hold", done["message"])
        self.assertEqual(1, done["message"].count("Backup storage cleanup is pending"))
        TARGET._cleanup_target(done)
        self.assertEqual(1, done["message"].count("Backup storage cleanup is pending"), "said once")
        Path(self.journal_path()).unlink()
        TARGET._cleanup_target(done)
        self.assertEqual("Arrived", done["message"], "gone once it can be read")
