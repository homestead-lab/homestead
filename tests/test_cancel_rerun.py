"""A Homestead restart during a Cancel: the leader runs the canceller again with
what the person chose, or the job says why a person has to. Driven through the
job store's own entry points: OPS.cancel and the jobs loop's round."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
import homestead_changes as CHANGES
import homestead_leader as LEADER
import homestead_operations as OPS
from fakes.job_store import put, store_writes
from fakes.kube import Kube


def rerun():
    """The audited list. Imported here so that on code without it, the behaviour
    tests in this file still run and fail on behaviour, not on this import."""
    import homestead_cancel_rerun
    return homestead_cancel_rerun


SAFE_KIND = "set-requests"       # any kind the audit calls safe; the tests give it their own canceller
PERSON_KIND = "host-promote"
OPTIONS = [{"id": "wipe", "label": "wipe it", "default": True}]


class Died(BaseException):
    """The process going away mid-canceller: nothing after it runs, as with an
    OOM, and no handler of the job store's sees it."""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = OPS.DATA_DIR, dict(OPS.RESOLVERS), dict(OPS.CANCELLERS), set(OPS.CLEANUPS)
        OPS.DATA_DIR = self.tmp.name

        def restore():
            OPS.DATA_DIR = saved[0]
            OPS.RESOLVERS.clear(); OPS.RESOLVERS.update(saved[1])
            OPS.CANCELLERS.clear(); OPS.CANCELLERS.update(saved[2])
            OPS.CLEANUPS.clear(); OPS.CLEANUPS.update(saved[3])
        self.addCleanup(restore)
        self.kube = self.enterContext(Kube())
        self.runs = []
        self.die = 0        # how many runs die before one gets through
        self.plan = {"mode": "rollback", "options": OPTIONS}
        for kind in (SAFE_KIND, PERSON_KIND, "not-audited"):
            OPS.CANCELLERS[kind] = (lambda item: dict(self.plan), self.canceller)
            OPS.RESOLVERS[kind] = lambda item: ("running", 10, "Still going")

    def canceller(self, item, options):
        self.runs.append((json.loads(json.dumps(item["ref"])), dict(options), getattr(CHANGES.REQUEST, "user", None)))
        if self.die:
            self.die -= 1
            raise Died()
        return "Put back"

    def job(self, kind=SAFE_KIND, ref=None):
        return OPS.start(kind, "A job", {"kind": "Thing", "name": "thing", "namespace": "lab"}, "/", ref or {"n": 1})["id"]

    def stored(self, op):
        return next(item for item in OPS.stored() if item["id"] == op)

    def cut_off(self, op, options=None, by="alice"):
        """A person's cancel, and the process going away inside the canceller."""
        self.die = 1
        with self.assertRaises(Died):
            OPS.cancel(op, options, by=by)
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])

    def leader_round(self, leading=True):
        OPS.tick(lambda: leading)
        OPS.STEPS.wait(5)

    def public(self, op):
        return next(row for row in OPS.snapshot() if row["id"] == op)


class RunsAgain(Base):
    def test_the_next_leader_round_finishes_a_cancel_cut_off(self):
        op = self.job()
        self.cut_off(op)
        self.leader_round()
        item = self.stored(op)
        self.assertEqual("cancelled", item["status"])
        self.assertIn("Put back", item["message"])
        self.assertIn("restarted during this cancel", item["message"])
        self.assertEqual("alice", item["cancelled_by"])
        self.assertTrue(item["finished_at"])
        self.assertEqual(2, len(self.runs))

    def test_it_runs_with_what_the_person_chose_not_the_defaults(self):
        op = self.job()
        self.cut_off(op, {"wipe": False})
        self.leader_round()
        self.assertEqual(2, len(self.runs))
        self.assertEqual({"wipe": False}, self.runs[-1][1])

    def test_its_writes_are_for_whoever_cancelled_not_the_loop(self):
        op = self.job()
        self.cut_off(op)
        was = getattr(CHANGES.REQUEST, "user", None)
        self.leader_round()
        self.assertEqual("alice", self.runs[-1][2])
        self.assertEqual(was, getattr(CHANGES.REQUEST, "user", None))

    def test_only_the_leader_runs_it(self):
        op = self.job()
        self.cut_off(op)
        self.leader_round(leading=False)
        self.assertEqual(1, len(self.runs))
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])

    def test_a_cancel_that_ended_is_not_run_again(self):
        op = self.job()
        OPS.cancel(op)
        self.leader_round()
        self.assertEqual(1, len(self.runs))

    def test_a_failed_job_being_cleaned_up_ends_failed_and_cleaned(self):
        OPS.CLEANUPS.add(SAFE_KIND)
        op = self.job()
        items = OPS.stored()
        items[-1].update(status="failed", finished_at="2026-11-06T00:00:00Z")
        put(OPS, items)
        self.cut_off(op)
        self.leader_round()
        item = self.stored(op)
        self.assertEqual("failed", item["status"])
        self.assertTrue(item["cleaned"])

    def test_a_cancel_that_only_stops_tracking_says_so_when_it_runs_again(self):
        self.plan = {"mode": "forget"}
        op = self.job()
        self.cut_off(op)
        self.leader_round()
        item = self.stored(op)
        self.assertEqual("cancelled", item["status"])
        self.assertTrue(item["tracking_stopped"])

    def test_one_that_fails_goes_back_to_running_with_the_reason_and_is_not_retried(self):
        op = self.job()
        self.cut_off(op)
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: dict(self.plan),
                                     lambda item, options: self.runs.append("second") or (_ for _ in ()).throw(RuntimeError("API gone")))
        self.leader_round()
        item = self.stored(op)
        # Its resolver may have moved it on by now, so the reason is read from its history.
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertIn("Cancelling did not finish: API gone", [row.get("m") for row in item["history"]])
        self.assertNotIn("cancel_started", item)
        self.assertNotIn("cancel_record", item)
        self.leader_round()
        self.leader_round()
        self.assertEqual(1, self.runs.count("second"))
        self.assertIn("Cancelling did not finish: API gone", [row.get("m") for row in self.stored(op)["history"]])

    def test_what_a_canceller_saved_as_it_went_is_there_when_it_runs_again(self):
        def saves(item, options):
            first = "found" not in item["ref"]
            self.runs.append(item["ref"].get("found"))
            if first:
                item["ref"]["found"] = "kept"
                OPS.keep_cancel_ref(item)
                item["ref"]["lost"] = "not saved"
                raise Died()
            return "Done"
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: {"mode": "rollback"}, saves)
        op = self.job()
        with self.assertRaises(Died):
            OPS.cancel(op)
        self.leader_round()
        self.assertEqual([None, "kept"], self.runs)
        self.assertNotIn("lost", self.stored(op)["ref"])

    def test_a_job_kind_stepped_outside_the_store_lock_is_finished_too(self):
        OPS.CANCELLERS["host-os"] = (lambda item: dict(self.plan), self.canceller)
        OPS.RESOLVERS["host-os"] = lambda item: ("running", 10, "Still going")
        op = self.job("host-os", {"node": "node-a"})
        OPS.cancel(op, {"wipe": False}, by="alice")
        self.die = 1
        self.leader_round()     # applies the recorded cancel; the process dies in it
        OPS.STEPS.take_errors()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        self.leader_round()
        self.assertEqual("cancelled", self.stored(op)["status"])
        self.assertEqual([{"wipe": False}, {"wipe": False}], [row[1] for row in self.runs])


class NeverBeside(Base):
    """Every replica answers the browser, so a cancel may run on a replica that
    is not the leader, for longer than any timer would guess."""
    def slow_cancel(self, op):
        started, release = threading.Event(), threading.Event()

        def slow(item, options):
            self.runs.append("running")
            started.set()
            release.wait(10)
            return "Put back"
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: dict(self.plan), slow)
        thread = threading.Thread(target=OPS.cancel, args=(op,))
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(5))
        return thread, release

    def test_a_canceller_still_running_elsewhere_is_left_to_finish(self):
        op = self.job()
        thread, release = self.slow_cancel(op)
        self.leader_round()
        self.assertEqual(["running"], self.runs)
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        release.set()
        thread.join(5)
        self.leader_round()
        self.assertEqual(["running"], self.runs)
        self.assertEqual("cancelled", self.stored(op)["status"])

    def test_a_person_asking_again_while_it_runs_is_refused(self):
        op = self.job()
        self.slow_cancel(op)
        items = OPS.stored()
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_RETRY_AFTER - 1
        put(OPS, items)
        with self.assertRaisesRegex(ValueError, "being cancelled already"):
            OPS.cancel(op)
        self.assertEqual(["running"], self.runs)

    def make_old(self, op, seconds):
        items = OPS.stored()
        next(row for row in items if row["id"] == op)["cancel_started"] = time.time() - seconds
        put(OPS, items)

    def test_a_person_refused_is_told_where_it_runs_and_what_a_restart_does(self):
        op = self.job()
        self.slow_cancel(op)
        self.make_old(op, OPS.CANCEL_RETRY_AFTER + 1)
        with self.assertRaises(ValueError) as refused:
            OPS.cancel(op)
        words = str(refused.exception)
        self.assertIn("being cancelled already", words)
        self.assertIn(LEADER.IDENTITY, words)
        self.assertIn("restart", words.lower())

    def test_a_cancel_running_long_says_so_on_the_job_once(self):
        op = self.job()
        thread, release = self.slow_cancel(op)
        self.make_old(op, OPS.CANCEL_SLOW_AFTER + 1)
        self.leader_round()
        said = self.stored(op)["message"]
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        self.assertIn(LEADER.IDENTITY, said)
        self.assertIn("restart", said.lower())
        with store_writes(OPS) as writes:
            self.leader_round()
        self.assertEqual([], list(writes))
        release.set()
        thread.join(5)
        self.assertEqual(("cancelled", "Put back"), (self.stored(op)["status"], self.stored(op)["message"]))

    def test_a_hung_rerun_names_the_pod_running_it_not_the_first(self):
        with mock.patch.object(LEADER, "IDENTITY", "pod-a"):
            op = self.job()
            self.cut_off(op)
        entered, release = threading.Event(), threading.Event()

        def hang(item, options):
            entered.set()
            release.wait(10)
            return "done"
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: dict(self.plan), hang)
        self.addCleanup(lambda: OPS.STEPS.wait(5))
        self.addCleanup(release.set)
        with mock.patch.object(LEADER, "IDENTITY", "pod-b"):
            OPS.tick(lambda: True)
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, "pod-b"):
                OPS.cancel(op, by="bob")

    def test_a_rerun_running_long_says_so_even_if_the_first_run_did(self):
        op = self.job()
        self.cut_off(op)
        items = OPS.stored()
        items[-1]["cancel_record"]["said_slow"] = True
        put(OPS, items)
        entered, release = threading.Event(), threading.Event()

        def hang(item, options):
            entered.set()
            release.wait(10)
            return "done"
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: dict(self.plan), hang)
        self.addCleanup(lambda: OPS.STEPS.wait(5))
        self.addCleanup(release.set)
        OPS.tick(lambda: True)
        self.assertTrue(entered.wait(5))
        self.make_old(op, OPS.CANCEL_SLOW_AFTER + 1)
        OPS.tick(lambda: True)
        deadline = time.time() + 5
        while "Still cancelling" not in self.stored(op)["message"] and time.time() < deadline:
            time.sleep(.02)
        self.assertIn("Still cancelling", self.stored(op)["message"])

    def test_what_a_restart_does_is_promised_only_where_it_is_true(self):
        for kind, promise in ((SAFE_KIND, "the leader runs the cancel again"), (PERSON_KIND, "waits for you")):
            with self.subTest(kind):
                op = self.job(kind, {"node": f"node-{kind}"})
                release = self.slow_cancel_of(op, kind)
                self.make_old(op, OPS.CANCEL_RETRY_AFTER + 1)
                with self.assertRaises(ValueError) as refused:
                    OPS.cancel(op)
                self.assertIn(promise, str(refused.exception))
                release()

    def test_a_review_over_the_limit_is_not_promised_a_rerun(self):
        op = self.job()
        release = self.slow_cancel_of(op, SAFE_KIND)
        self.addCleanup(release)
        items = OPS.stored()
        items[-1]["cancel_record"]["at"] = time.time() - OPS.CANCEL_REVIEW_KEEPS - 1
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_RETRY_AFTER - 1
        put(OPS, items)
        with self.assertRaises(ValueError) as refused:
            OPS.cancel(op)
        self.assertIn("waits for you", str(refused.exception))

    def slow_cancel_of(self, op, kind):
        started, gate = threading.Event(), threading.Event()

        def slow(item, options):
            started.set()
            gate.wait(10)
            return "Put back"
        OPS.CANCELLERS[kind] = (lambda item: dict(self.plan), slow)
        thread = threading.Thread(target=OPS.cancel, args=(op,))
        thread.start()
        self.assertTrue(started.wait(5))

        def release():
            gate.set()
            thread.join(5)
        self.addCleanup(release)
        return release

    def test_a_cancel_running_a_short_while_says_nothing_new(self):
        op = self.job()
        self.slow_cancel(op)
        before = self.stored(op)["message"]
        self.leader_round()
        self.assertEqual(before, self.stored(op)["message"])

    def test_a_person_asking_again_for_a_kind_the_loop_applies_is_refused_while_it_runs(self):
        started, release = threading.Event(), threading.Event()

        def slow(item, options):
            started.set()
            release.wait(10)
            return "Put back"
        OPS.CANCELLERS["host-os"] = (lambda item: dict(self.plan), slow)
        OPS.RESOLVERS["host-os"] = lambda item: ("running", 10, "Still going")
        op = self.job("host-os", {"node": "node-a"})
        OPS.cancel(op)
        OPS.tick(lambda: True)
        self.addCleanup(lambda: OPS.STEPS.wait(5))
        self.addCleanup(release.set)
        self.assertTrue(started.wait(5))
        self.make_old(op, OPS.CANCEL_RETRY_AFTER + 1)
        with self.assertRaisesRegex(ValueError, "being cancelled already"):
            OPS.cancel(op)
        self.assertNotIn("cancel_request", self.stored(op))

    def test_a_process_killed_while_it_held_the_job_lets_the_leader_go_on(self):
        """The real thing: another process, its lock on a real file, and SIGKILL."""
        op = self.job()
        self.cut_off(op)
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             f"sys.path.insert(0, {str(SERVER)!r})\n"
             "import homestead_operations as OPS\n"
             f"OPS.DATA_DIR = {self.tmp.name!r}\n"
             f"lock = OPS._job_lock({op!r})\n"
             "lock.__enter__()\n"
             "print('held', flush=True)\n"
             "time.sleep(60)\n"],
            stdout=subprocess.PIPE, text=True, env={**os.environ, "HOMESTEAD_DATA_DIR": self.tmp.name})
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual("held", child.stdout.readline().strip())
        self.leader_round()
        self.assertEqual(1, len(self.runs), "another process holds the job: it is not run beside it")
        child.send_signal(signal.SIGKILL)
        child.wait(5)
        self.leader_round()
        self.assertEqual(2, len(self.runs))
        self.assertEqual("cancelled", self.stored(op)["status"])


class StaysWithAPerson(Base):
    def test_a_kind_the_audit_could_not_clear_is_not_run_and_says_why(self):
        op = self.job(PERSON_KIND, {"node": "node-a"})
        self.cut_off(op)
        self.leader_round()
        self.assertEqual(1, len(self.runs))
        item = self.stored(op)
        self.assertEqual(OPS.CANCELLING, item["status"])
        self.assertIn("was not run again", item["message"])
        self.assertIn(rerun().PERSON[PERSON_KIND], item["message"])
        self.assertIn("Cancel again", item["message"])

    def test_cancel_is_open_to_them_at_once_and_finishes_the_job(self):
        op = self.job(PERSON_KIND, {"node": "node-a"})
        self.cut_off(op)
        self.leader_round()
        self.assertTrue(self.public(op)["cancellable"])
        OPS.cancel(op, {"wipe": False}, by="bob")
        item = self.stored(op)
        self.assertEqual(("cancelled", "bob"), (item["status"], item["cancelled_by"]))
        self.assertEqual({"wipe": False}, self.runs[-1][1])

    def test_a_job_waiting_for_a_person_is_not_written_every_round(self):
        op = self.job(PERSON_KIND, {"node": "node-a"})
        self.cut_off(op)
        self.leader_round()
        with store_writes(OPS) as writes:
            self.leader_round()
            self.leader_round()
        self.assertEqual([], [row for row in writes if any(i["id"] == op for i in row)])

    def test_a_kind_nobody_has_audited_stays_with_a_person(self):
        op = self.job("not-audited")
        self.cut_off(op)
        self.leader_round()
        self.assertEqual(1, len(self.runs))
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_a_cancel_that_kept_killing_homestead_stops_being_tried(self):
        op = self.job()
        self.cut_off(op)
        for _ in range(OPS.MAX_CANCEL_RERUNS):
            self.die = 1
            OPS.tick(lambda: True)
            OPS.STEPS.wait(5)
            OPS.STEPS.take_errors()
        self.assertEqual(1 + OPS.MAX_CANCEL_RERUNS, len(self.runs))
        self.leader_round()
        self.assertEqual(1 + OPS.MAX_CANCEL_RERUNS, len(self.runs))
        item = self.stored(op)
        self.assertEqual(OPS.CANCELLING, item["status"])
        self.assertIn("was not run again", item["message"])
        self.assertTrue(self.public(op)["cancellable"])

    def test_a_cancel_from_before_this_version_kept_no_choices_so_none_are_guessed(self):
        op = self.job()
        items = OPS.stored()
        items[-1].update(status=OPS.CANCELLING, previous_status="queued",
                         cancel_started=time.time() - OPS.CANCEL_RETRY_AFTER - 1)
        put(OPS, items)
        self.leader_round()
        self.assertEqual([], self.runs)
        item = self.stored(op)
        self.assertEqual(OPS.CANCELLING, item["status"])
        self.assertIn("was not run again", item["message"])
        self.assertIn("what you chose", item["message"])

    def test_one_from_before_this_version_may_still_be_running_on_an_old_replica(self):
        op = self.job()
        items = OPS.stored()
        items[-1].update(status=OPS.CANCELLING, previous_status="queued", cancel_started=time.time())
        put(OPS, items)
        before = self.stored(op)["message"]
        self.leader_round()
        self.assertEqual(before, self.stored(op)["message"])


class TheReviewIsCheckedAgain(Base):
    """What the person reviewed may no longer be what cancelling would do."""
    def test_a_step_that_cannot_be_cancelled_now_goes_back_to_what_it_was_with_the_reason(self):
        op = self.job()
        self.cut_off(op)
        self.plan = {"can": False, "why_not": "the volume is being swapped"}
        self.leader_round()
        self.assertEqual(1, len(self.runs))
        item = self.stored(op)
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertIn("Cancelling did not finish: the volume is being swapped", [row.get("m") for row in item["history"]])
        self.assertNotIn("cancel_record", item)

    def test_a_person_pressing_cancel_on_a_job_that_cannot_be_cancelled_now_frees_it(self):
        """A job parked for a person, whose plan has since come to refuse: pressing Cancel
        is refused, so it must not stay cancelling for good."""
        op = self.job(PERSON_KIND, {"node": "node-a"})
        self.cut_off(op)
        self.leader_round()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        self.plan = {"can": False, "why_not": "the host is being reinstalled"}
        with self.assertRaisesRegex(ValueError, "being reinstalled"):
            OPS.cancel(op)
        item = self.stored(op)
        self.assertNotEqual(OPS.CANCELLING, item["status"])
        self.assertIn("Cancelling did not finish: the host is being reinstalled", [row.get("m") for row in item["history"]])

    def test_a_person_pressing_cancel_on_a_kind_the_loop_applies_frees_it_too(self):
        OPS.CANCELLERS["host-os"] = (lambda item: dict(self.plan), self.canceller)
        OPS.RESOLVERS["host-os"] = lambda item: ("running", 10, "Still going")
        op = self.job("host-os", {"node": "node-a"})
        OPS.cancel(op)
        self.die = 1
        self.leader_round()
        OPS.STEPS.take_errors()
        self.assertEqual(OPS.CANCELLING, self.stored(op)["status"])
        self.plan = {"can": False, "why_not": "the host is busy"}
        items = OPS.stored()
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_RETRY_AFTER - 1
        put(OPS, items)
        with self.assertRaisesRegex(ValueError, "the host is busy"):
            OPS.cancel(op)
        self.assertNotEqual(OPS.CANCELLING, self.stored(op)["status"])

    def test_what_a_reviewed_plan_named_is_checked_again(self):
        """The plan names what it would act on: the reviewed version, not only a mode."""
        op = self.job()
        self.plan = {"mode": "rollback", "reviewed_target": {"revision": 1}}
        self.cut_off(op)
        self.plan = {"mode": "rollback", "reviewed_target": {"revision": 2}}
        self.leader_round()
        self.assertEqual(1, len(self.runs))
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_the_same_target_runs_again(self):
        op = self.job()
        self.plan = {"mode": "rollback", "reviewed_target": {"revision": 1}}
        self.cut_off(op)
        self.leader_round()
        self.assertEqual("cancelled", self.stored(op)["status"])

    def test_a_plan_that_always_raises_says_so_once_it_has_waited_long(self):
        op = self.job()
        self.cut_off(op)
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: (_ for _ in ()).throw(KeyError("namespace")), self.canceller)
        self.leader_round()
        self.assertNotIn("cannot tell", self.stored(op)["message"])
        items = OPS.stored()
        items[-1]["cancel_started"] = time.time() - OPS.CANCEL_SLOW_AFTER - 1
        put(OPS, items)
        self.leader_round()
        self.assertIn("cannot tell", self.stored(op)["message"])
        self.assertEqual(1, len(self.runs))
        with store_writes(OPS) as writes:
            self.leader_round()
        self.assertEqual([], list(writes))

    def test_a_cancel_that_would_now_do_something_else_parks(self):
        op = self.job()
        self.cut_off(op)
        self.plan = {"mode": "forget"}
        self.leader_round()
        self.assertEqual(1, len(self.runs))
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_a_choice_nobody_was_offered_parks(self):
        op = self.job()
        self.cut_off(op)
        self.plan = {"mode": "rollback", "options": OPTIONS + [{"id": "more", "label": "more", "default": True}]}
        self.leader_round()
        self.assertEqual(1, len(self.runs))
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_a_plan_that_cannot_be_read_waits_and_costs_no_attempt(self):
        op = self.job()
        self.cut_off(op)
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: (_ for _ in ()).throw(OSError("API gone")), self.canceller)
        before = self.stored(op)
        self.leader_round()
        OPS.STEPS.take_errors()
        self.assertEqual(before["cancel_record"], self.stored(op)["cancel_record"])
        self.assertEqual(1, len(self.runs))
        OPS.CANCELLERS[SAFE_KIND] = (lambda item: dict(self.plan), self.canceller)
        self.leader_round()
        self.assertEqual("cancelled", self.stored(op)["status"])

    def test_a_review_older_than_the_limit_is_not_acted_on(self):
        op = self.job()
        self.cut_off(op)
        items = OPS.stored()
        items[-1]["cancel_record"]["at"] = time.time() - OPS.CANCEL_REVIEW_KEEPS - 1
        put(OPS, items)
        self.leader_round()
        self.assertEqual(1, len(self.runs))
        self.assertIn("was not run again", self.stored(op)["message"])

    def test_a_review_inside_the_limit_is_acted_on(self):
        op = self.job()
        self.cut_off(op)
        items = OPS.stored()
        items[-1]["cancel_record"]["at"] = time.time() - OPS.CANCEL_REVIEW_KEEPS + 60
        put(OPS, items)
        self.leader_round()
        self.assertEqual("cancelled", self.stored(op)["status"])


class TheWords(Base):
    def test_a_reason_with_no_full_stop_still_reads_as_two_sentences(self):
        op = self.job(PERSON_KIND, {"node": "node-a"})
        self.cut_off(op)
        with mock.patch.dict(rerun().PERSON, {PERSON_KIND: "it would not be safe"}):
            self.leader_round()
        self.assertIn("it would not be safe. Review it", self.stored(op)["message"])

    def test_a_reason_with_one_keeps_only_one(self):
        op = self.job(PERSON_KIND, {"node": "node-a"})
        self.cut_off(op)
        with mock.patch.dict(rerun().PERSON, {PERSON_KIND: "it would not be safe."}):
            self.leader_round()
        self.assertIn("it would not be safe. Review it", self.stored(op)["message"])


class TheRecord(Base):
    def test_the_choices_never_reach_the_job_tray(self):
        op = self.job()
        self.cut_off(op)
        self.assertIn("cancel_record", self.stored(op))
        self.assertNotIn("cancel_record", self.public(op))


class EveryKindIsClassified(unittest.TestCase):
    """A new canceller is not run again until someone has audited it."""
    def registered(self):
        with tempfile.TemporaryDirectory() as data:
            out = subprocess.run(
                [sys.executable, "-c",
                 "import json, server, homestead_operations as OPS\n"
                 "print('KINDS' + json.dumps(sorted(OPS.CANCELLERS)))"],
                cwd=str(SERVER), capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONPATH": str(SERVER), "DATA_DIR": data, "HOMESTEAD_DATA_DIR": data})
        line = next((row for row in out.stdout.splitlines() if row.startswith("KINDS")), "")
        self.assertTrue(line, out.stderr[-800:])
        return set(json.loads(line[len("KINDS"):]))

    def test_each_registered_kind_is_safe_or_stays_with_a_person(self):
        kinds = self.registered()
        self.assertGreater(len(kinds), 30)
        self.assertEqual(set(), kinds - set(rerun().SAFE) - set(rerun().PERSON))

    def test_the_lists_name_only_real_kinds_and_none_twice(self):
        kinds = self.registered()
        # A kind that comes with an open PR (#1217's removal job) is listed before it lands.
        coming = {"vm-disk-remove"}
        self.assertEqual(set(), (set(rerun().SAFE) | set(rerun().PERSON)) - kinds - coming)
        self.assertEqual(set(), set(rerun().SAFE) & set(rerun().PERSON))

    def test_each_entry_says_why_in_words(self):
        for kind, why in {**rerun().SAFE, **rerun().PERSON}.items():
            self.assertGreater(len(why), 15, kind)

    def test_a_kind_with_no_canceller_only_stops_tracking_so_it_may_run(self):
        self.assertEqual("", rerun().why_not("anything", {}))

    def test_an_unlisted_canceller_is_refused(self):
        self.assertTrue(rerun().why_not("anything", {"anything": (None, None)}))


if __name__ == "__main__":
    unittest.main()
