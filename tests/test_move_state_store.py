"""The move store on Homestead's data volume fails closed.

moves.json holds every cross-cluster move this cluster runs, with the flags
that say what is still to be cleaned up on both clusters. A read that fails -
an NFS hiccup, a half-written file, a file that cannot be opened - must never
be taken for "no moves" and written back: that would forget every other move.
A missing file is a fresh install and reads as no moves.
"""
import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_shared as SHARED
import homestead_move as MOVE
import homestead_move_state as STATE
import homestead_move_actions as ACT
import homestead_move_phases as PHASES
import homestead_move_plan as PLAN
import homestead_cancel as CANCEL


def move(id, status="running", **extra):
    return {"id": id, "cluster": "oldcluster", "name": id, "kind": "container", "status": status,
            "phase": "syncing", "created_at": id, "namespace": "lab", **extra}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.enterContext(mock.patch.object(STATE, "DATA_DIR", self.dir.name))
        self.enterContext(mock.patch.object(STATE, "CLIENT", MOVE))
        # Cancelling reads what the move made here before the source starts again: nothing is here.
        self.enterContext(mock.patch.object(STATE, "kget", self.nothing_here))
        self.path = os.path.join(self.dir.name, STATE.STORE)
        self.rows = [move("aaaaaaaaaaaa", flags={"target_cleanup_pending": True}),
                     move("bbbbbbbbbbbb", "succeeded", flags={"resource_cleanup_pending": True}),
                     move("cccccccccccc", "succeeded")]

    def save(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        with open(self.path, "rb") as handle:
            return handle.read()

    def stored_bytes(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def broken_stores(self):
        """Files that are there but hold no readable list of moves."""
        whole = json.dumps(self.rows)
        yield "half-written", whole[: len(whole) // 2]
        yield "not JSON", "\0\0\0\0"
        yield "not a list", json.dumps({"moves": self.rows})

    def assert_refused_and_kept(self, call, before):
        with self.assertRaises(SHARED.Unreadable) as caught:
            call()
        self.assertIn(STATE.STORE, str(caught.exception))
        self.assertEqual(before, self.stored_bytes(), "the stored moves must not be written over")

    # A failed read is never "no moves", and is never written back.

    def test_storing_a_move_over_a_broken_store_raises_and_writes_nothing(self):
        for what, text in self.broken_stores():
            with self.subTest(what):
                before = self.save(text)
                self.assert_refused_and_kept(lambda: STATE._store(move("dddddddddddd")), before)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a mode-000 file")
    def test_storing_a_move_over_a_store_that_cannot_be_opened_raises_and_writes_nothing(self):
        before = self.save(json.dumps(self.rows))
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        with self.assertRaises(SHARED.Unreadable):
            STATE._store(move("dddddddddddd"))
        os.chmod(self.path, 0o600)
        self.assertEqual(before, self.stored_bytes())

    def test_updating_a_move_over_a_broken_store_raises_and_writes_nothing(self):
        before = self.save("[{\"id\": \"aaaaaaaaaaaa\"")
        self.assert_refused_and_kept(lambda: STATE._update(move("aaaaaaaaaaaa"), flags={"x": True}), before)

    def test_the_moves_list_says_the_store_cannot_be_read_rather_than_showing_none(self):
        self.save("not json")
        with self.assertRaises(SHARED.Unreadable):
            STATE.moves()
        with self.assertRaises(SHARED.Unreadable):
            STATE._find("aaaaaaaaaaaa")

    def test_clearing_finished_moves_over_a_broken_store_raises_and_writes_nothing(self):
        before = self.save("[{\"id\":")
        self.assert_refused_and_kept(ACT.dismiss, before)

    def test_starting_a_move_over_a_broken_store_is_refused(self):
        before = self.save("[{\"id\":")
        self.assert_refused_and_kept(lambda: PLAN._refuse_second("oldcluster", "container", "x"), before)

    def test_the_activity_tray_does_not_call_a_move_gone_when_its_store_cannot_be_read(self):
        # "This move's record is gone" would end the move's Activity item as
        # failed, and offer to forget it, while the move is still running.
        self.save("[{\"id\":")
        item = {"kind": "move", "ref": {"move": "aaaaaaaaaaaa"}}
        with self.assertRaises(SHARED.Unreadable):
            PLAN.op_state(item)
        with self.assertRaises(SHARED.Unreadable):
            CANCEL.move_plan(item)

    def test_a_person_gets_a_refusal_naming_the_store_not_a_server_error(self):
        before = self.save("[{\"id\":")
        reply = MOVE.answer(lambda: ACT.retry("aaaaaaaaaaaa"))
        self.assertEqual(409, reply.code)
        self.assertIn(STATE.STORE, reply.body["error"])
        self.assertEqual(before, self.stored_bytes())

    # The engine's background pass.

    def test_the_engine_pass_over_a_broken_store_stops_and_writes_nothing(self):
        before = self.save("[{\"id\":")
        self.assert_refused_and_kept(PHASES.tick_all, before)

    def test_a_store_that_breaks_mid_step_leaves_the_move_running_rather_than_failed(self):
        # Unreadable is a ValueError, and _tick ends a move as failed on a
        # ValueError. A storage hiccup is not the move's fault: the step stops,
        # nothing is written, and the next pass asks again.
        running = move("aaaaaaaaaaaa", phase="syncing")
        self.save(json.dumps([running]))
        broken = "[{\"id\":"

        def step(current):
            self.save(broken)
            current["message"] = "this step's write"
            STATE._store(current)

        with mock.patch.dict(PHASES.HANDLERS, {"syncing": step}), \
                mock.patch.object(PHASES.TARGET, "_borrow_target", return_value=True):
            with self.assertRaises(SHARED.Unreadable):
                PHASES._tick(dict(running))
        self.assertEqual(broken.encode(), self.stored_bytes())
        self.save(json.dumps([running]))
        self.assertEqual("running", STATE._find("aaaaaaaaaaaa")["status"])

    # A missing file is a fresh install.

    def test_a_fresh_install_has_no_moves_and_can_store_the_first(self):
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual([], STATE.moves())
        self.assertIsNone(STATE._find("aaaaaaaaaaaa"))
        STATE._store(move("aaaaaaaaaaaa"))
        self.assertEqual(["aaaaaaaaaaaa"], [m["id"] for m in json.loads(self.stored_bytes())])

    def test_a_readable_store_keeps_every_other_move_when_one_is_stored(self):
        self.save(json.dumps(self.rows))
        STATE._store(move("dddddddddddd"))
        self.assertEqual(["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd"],
                         [m["id"] for m in json.loads(self.stored_bytes())])

    # The lock every Homestead replica takes.

    def test_the_lock_lives_beside_the_moves_it_guards(self):
        STATE._store(move("aaaaaaaaaaaa"))
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, ".locks", "moves.lock")))

    def test_without_the_shared_lock_nothing_is_stored(self):
        # Another replica cannot be kept out, so two could each write back
        # their own copy and lose the other's move. Refuse instead of falling
        # back to a lock only this process honours.
        before = self.save(json.dumps(self.rows))
        with open(os.path.join(self.dir.name, ".locks"), "w", encoding="utf-8"):
            pass  # a file where the lock folder belongs: the lock cannot be made
        with self.assertRaises(STATE.StoreUnavailable) as caught:
            STATE._store(move("dddddddddddd"))
        self.assertIn(STATE.STORE, str(caught.exception))
        self.assertEqual(before, self.stored_bytes())

    def test_a_busy_moves_lock_is_a_refusal_that_names_the_moves_store(self):
        # Another replica holds it past the timeout: a 409 about moves, not a
        # server error about the operations store.
        before = self.save(json.dumps(self.rows))
        os.makedirs(os.path.join(self.dir.name, ".locks"))
        # A second open file description: flock treats it as another replica.
        other = open(os.path.join(self.dir.name, ".locks", "moves.lock"), "a+b")
        self.addCleanup(other.close)
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(STATE._lock, "timeout", 0.2):
            reply = MOVE.answer(lambda: ACT.retry("aaaaaaaaaaaa"))
        self.assertEqual(409, reply.code)
        self.assertIn("moves", reply.body["error"])
        self.assertNotIn("job store", reply.body["error"])
        self.assertEqual(before, self.stored_bytes())

    # One failed read, as a single EIO or ESTALE: the next read works again.

    def test_one_failed_read_in_a_step_leaves_the_move_running_through_the_engine_pass(self):
        running = move("aaaaaaaaaaaa", phase="syncing", op="op-1", flags={})
        for what, error in (("unreadable", SHARED.Unreadable("could not read moves.json")),
                            ("lock busy", STATE.StoreUnavailable("the moves store is busy"))):
            with self.subTest(what):
                self.save(json.dumps([running]))
                with mock.patch.dict(PHASES.HANDLERS, {"syncing": mock.Mock(side_effect=[error])}), \
                        mock.patch.object(PHASES.TARGET, "_borrow_target", return_value=True), \
                        mock.patch.object(PHASES.ENGINE, "after_finish", None):
                    with self.assertRaises(type(error)):
                        PHASES.tick_all()
                stored = STATE._find("aaaaaaaaaaaa")
                self.assertEqual("running", stored["status"], stored.get("message"))

    # After a change on a cluster, a failed write says what was done.

    @staticmethod
    def nothing_here(path, **_options):
        if path.rsplit("/", 1)[-1].split("?")[0].endswith("aaaaaaaaaaaa"):
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO(b""))
        return {"items": []}

    def assert_says_what_was_done(self, reply, *done):
        self.assertEqual(409, reply.code)
        error = reply.body["error"]
        for words in done:
            self.assertIn(words, error)
        self.assertIn("could not record", error)
        self.assertNotIn("changed nothing", error)
        self.assertNotIn("no operation was started", error)

    def test_removing_the_original_that_cannot_be_recorded_says_it_was_removed(self):
        self.save(json.dumps([move("aaaaaaaaaaaa", "succeeded", claims=[])]))

        def removed(*_args, **_kwargs):
            self.save("[{\"id\":")  # the volume stops answering right after
            return {"detail": "removed workload aaaaaaaaaaaa"}

        with mock.patch.object(STATE.CLIENT, "remote", side_effect=removed):
            reply = MOVE.answer(lambda: ACT.finish("aaaaaaaaaaaa"))
        self.assert_says_what_was_done(reply, "Removed aaaaaaaaaaaa from oldcluster")

    def test_putting_back_that_cannot_be_recorded_says_the_source_runs_again(self):
        self.save(json.dumps([move("aaaaaaaaaaaa", flags={"quiesced": True}, claims=[])]))

        def released(*_args, **_kwargs):
            self.save("[{\"id\":")
            return {"detail": "aaaaaaaaaaaa runs on oldcluster again"}

        with mock.patch.object(STATE, "_source_action", side_effect=released):
            reply = MOVE.answer(lambda: ACT.abandon("aaaaaaaaaaaa"))
        self.assert_says_what_was_done(reply, "aaaaaaaaaaaa runs on oldcluster again", "next pass")

    def test_a_retry_whose_activity_item_cannot_be_recorded_says_one_was_added(self):
        self.save(json.dumps([move("aaaaaaaaaaaa", "failed", phase="syncing", flags={})]))

        def operation(_current):
            self.save("[{\"id\":")
            return "op-1"

        with mock.patch.object(PLAN, "_operation", side_effect=operation):
            reply = MOVE.answer(lambda: ACT.retry("aaaaaaaaaaaa"))
        self.assert_says_what_was_done(reply, "Activity item", "stays failed")

    def test_a_cleanup_retry_that_cannot_be_recorded_says_cleanup_ran(self):
        self.save(json.dumps([move("aaaaaaaaaaaa", "succeeded", flags={"resource_cleanup_pending": True})]))

        def cleanup(_current):
            self.save("[{\"id\":")

        with mock.patch.object(PHASES, "_cleanup_resources", side_effect=cleanup):
            reply = MOVE.answer(lambda: ACT.retry("aaaaaaaaaaaa"))
        self.assert_says_what_was_done(reply, "cleaning up", "next pass")

    def test_a_joining_retry_that_cannot_be_recorded_says_backup_storage_was_released(self):
        self.save(json.dumps([move("aaaaaaaaaaaa", "failed", phase="joining", flags={})]))

        def operation(_current):
            self.save("[{\"id\":")
            return "op-1"

        with mock.patch.object(ACT.TARGET, "_release_target"), \
                mock.patch.object(PLAN, "_operation", side_effect=operation):
            reply = MOVE.answer(lambda: ACT.retry("aaaaaaaaaaaa"))
        self.assert_says_what_was_done(reply, "backup storage", "Activity item")
        self.assertEqual("Released the backup storage this move had borrowed; added an Activity item to retry "
                         "aaaaaaaaaaaa, which ends as failed, but Homestead could not record the retry on its data "
                         "volume (it is not valid JSON). The move stays failed. Retry once the data volume answers.",
                         reply.body["error"])

    def test_a_refused_removal_is_named_when_its_failure_cannot_be_recorded(self):
        self.save(json.dumps([move("aaaaaaaaaaaa", flags={"quiesced": True}, claims=[])]))

        def refused(_current):
            self.save("[{\"id\":")
            raise RuntimeError("Kubernetes refused it (HTTP 403): forbidden")

        # What this move made here goes before the source starts again (dev's order):
        # a refused removal leaves the source stopped, and the reply says how far it got.
        with mock.patch.object(STATE, "_source_action", return_value={"detail": "aaaaaaaaaaaa runs on oldcluster again"}) as release, \
                mock.patch.object(PHASES, "_remove_created", side_effect=refused):
            reply = MOVE.answer(lambda: ACT.abandon("aaaaaaaaaaaa"))
        self.assert_says_what_was_done(reply, "Began removing what this move made here", "HTTP 403",
                                       "aaaaaaaaaaaa stays stopped on oldcluster")
        release.assert_not_called()

    def test_a_started_move_whose_activity_item_cannot_be_recorded_says_it_will_run(self):
        def operation(current):
            current["op"] = "op-1"
            self.save("[{\"id\":")

        with mock.patch.object(PLAN, "plan", return_value={"ok": True, "storage_class": ""}), \
                mock.patch.object(STATE, "_definition", return_value={"claims": [], "namespace": "lab"}), \
                mock.patch.object(PLAN, "_add_operation", side_effect=operation):
            reply = MOVE.answer(lambda: PLAN.start("oldcluster", "container", "aaaaaaaaaaaa"))
        self.assert_says_what_was_done(reply, "recorded and will run")


if __name__ == "__main__":
    unittest.main()
