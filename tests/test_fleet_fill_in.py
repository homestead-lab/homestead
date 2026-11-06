"""The view of all clusters answers at once (#705): each linked Homestead fills in
as it answers, one that hasn't yet is named, its last answer is kept and marked
with its age while it is slow or out of reach, and after three misses in a row it
is said not to be answering. A refusal drops its rows at once."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
from test_fleet import Cluster, wire

FLEET = server.FLEET
OUT_OF_REACH = FLEET.Unreachable("Office: no answer from https://192.0.2.10:8088: timed out")


class FillInTests(unittest.TestCase):
    def setUp(self):
        self.members = [
            {"id": "home", "name": "Home", "handle": "home", "self": True, "reachable": True},
            {"id": "office", "name": "Office", "handle": "office", "self": False, "reachable": True},
            {"id": "loft", "name": "Loft", "handle": "loft", "self": False, "reachable": True},
        ]
        self.slow = {}           # member id -> threading.Event released to let it answer
        self.failing = {}           # member id -> exception it raises
        self.reply = {}          # member id -> what it answers instead of a list
        for patch in [mock.patch.object(FLEET, "summary", return_value={"self": "home", "members": self.members}),
                      mock.patch.object(FLEET, "call", side_effect=self.answer),
                      mock.patch.object(FLEET, "ANSWER_BUDGET", 0.3)]:
            patch.start()
            self.addCleanup(patch.stop)
        FLEET.forget_answers()
        self.addCleanup(self.release_all)
        self.addCleanup(FLEET.forget_answers)

    def answer(self, member, method, path, **_):
        gate = self.slow.get(member["id"])
        if gate is not None:
            gate.wait(10)
        if member["id"] in self.failing:
            raise self.failing[member["id"]]
        return self.reply.get(member["id"], [{"name": f"app-{member['id']}"}])

    def release_all(self):
        for gate in self.slow.values():
            gate.set()

    def answered(self, member_id):
        """Let a slow member answer, and wait until its answer is kept."""
        self.slow.pop(member_id).set()
        for key, entry in list(FLEET._answers.items()):
            if key[1] == member_id:
                self.assertTrue(entry["done"].wait(5))

    def gather(self, user="alice", role="viewer"):
        return FLEET.gather("workloads", lambda: [{"name": "app-home"}], user, role)

    def sites(self, rows):
        return sorted(row["site"]["id"] for row in rows)

    def test_a_slow_member_does_not_hold_up_the_others(self):
        self.slow["office"] = threading.Event()
        started = time.monotonic()
        rows, missing = self.gather()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(["home", "loft"], self.sites(rows))
        self.assertEqual([("office", "waiting")], [(m["id"], m["state"]) for m in missing])

    def test_a_member_never_heard_from_is_waiting_and_one_that_failed_is_not_answering(self):
        # As the cluster menu says it (#1083): the last answer wins.
        self.members[1].update(reachable=False, checking=True, answered=False, error="still checking")
        self.members[2].update(reachable=False, checking=True, answered=True, error="no answer from https://192.0.2.11:8088")
        rows, missing = self.gather()
        self.assertEqual(["home"], self.sites(rows))
        self.assertEqual([("office", "waiting", "has not answered yet"),
                          ("loft", "not answering", "no answer from https://192.0.2.11:8088")],
                         [(m["id"], m["state"], m["error"]) for m in missing])

    def test_a_member_that_answers_late_fills_in_on_the_next_refresh(self):
        self.slow["office"] = threading.Event()
        self.gather()
        self.answered("office")
        rows, missing = self.gather()
        self.assertEqual(["home", "loft", "office"], self.sites(rows))
        self.assertEqual([], missing)

    def test_the_last_answer_is_kept_and_its_age_given_while_a_member_is_slow(self):
        rows, _ = self.gather()
        self.assertIn("office", self.sites(rows))
        self.slow["office"] = threading.Event()
        rows, missing = self.gather()
        self.assertIn("office", self.sites(rows))
        self.assertEqual("waiting", missing[0]["state"])
        self.assertIn("age_s", missing[0])

    def test_the_last_answer_is_kept_while_a_member_is_out_of_reach(self):
        self.gather()
        self.failing["office"] = OUT_OF_REACH
        rows, missing = self.gather()
        self.assertIn("office", self.sites(rows))
        self.assertEqual(("error", True), (missing[0]["state"], "age_s" in missing[0]))

    def test_a_refusal_drops_the_last_answer_at_once(self):
        self.gather()
        self.failing["office"] = ValueError("Office: HTTP 403")
        rows, missing = self.gather()
        self.assertNotIn("office", self.sites(rows))
        self.assertEqual("error", missing[0]["state"])
        self.assertNotIn("age_s", missing[0])

    def test_an_answer_that_is_not_a_list_is_named_and_shows_no_rows(self):
        self.reply["office"] = {"error": "not found"}
        rows, missing = self.gather()
        self.assertNotIn("office", self.sites(rows))
        self.assertEqual(("office", "error"), (missing[0]["id"], missing[0]["state"]))
        self.assertIn("not a list", missing[0]["error"])

    def test_a_last_answer_older_than_five_minutes_is_not_shown(self):
        self.gather()
        for key, entry in FLEET._answers.items():
            if key[1] == "office":
                entry["at"] -= FLEET.ANSWER_KEEP + 1
        self.slow["office"] = threading.Event()
        rows, missing = self.gather()
        self.assertNotIn("office", self.sites(rows))
        self.assertNotIn("age_s", missing[0])

    def test_three_misses_in_a_row_mean_not_answering(self):
        self.failing["office"] = OUT_OF_REACH
        states = [self.gather()[1][0]["state"] for _ in range(3)]
        self.assertEqual(["error", "error", "not answering"], states)

    def test_an_answer_after_misses_resets_them(self):
        self.failing["office"] = OUT_OF_REACH
        for _ in range(3):
            self.gather()
        del self.failing["office"]
        rows, missing = self.gather()
        self.assertIn("office", self.sites(rows))
        self.assertEqual([], missing)

    def test_the_error_names_neither_the_member_again_nor_its_address(self):
        self.failing["office"] = OUT_OF_REACH
        self.assertEqual("it did not answer", self.gather()[1][0]["error"])
        self.failing["office"] = ValueError("Office: you may not see workloads here")
        self.assertEqual("you may not see workloads here", self.gather()[1][0]["error"])

    def test_a_slow_member_is_asked_once_not_on_every_refresh(self):
        self.slow["office"] = threading.Event()
        for _ in range(3):
            self.gather()
        office_calls = [c for c in FLEET.call.call_args_list if c.args[0]["id"] == "office"]
        self.assertEqual(1, len(office_calls))

    def test_answers_are_kept_per_person(self):
        self.gather("alice", "viewer")
        self.slow["office"] = threading.Event()
        rows, _ = self.gather("bob", "viewer")
        self.assertNotIn("office", self.sites(rows))

    def test_answers_are_kept_per_role(self):
        self.gather("alice", "admin")
        self.slow["office"] = threading.Event()
        rows, _ = self.gather("alice", "viewer")
        self.assertNotIn("office", self.sites(rows))

    def test_a_question_that_comes_back_wakes_only_its_own_gather(self):
        # A later gather may already have asked again and swapped in a fresh
        # Event: the old question setting that one would let the new gather
        # stop waiting early and say "waiting".
        entry = {"result": None, "at": 0, "misses": 0, "error": "", "pending": True, "done": threading.Event()}
        own = threading.Event()
        FLEET._ask(entry, own, self.members[1], "workloads", "alice", "viewer")
        self.assertTrue(own.is_set())
        self.assertFalse(entry["done"].is_set())

    def test_an_answer_is_shown_even_if_the_next_question_has_already_started(self):
        # Office answers; before this gather looks, another gather has
        # already asked Office again. This gather's question came back: its
        # answer is shown, not called "waiting".
        class AnswerThenAskAgain:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)
                entry = self.args[0]
                with FLEET._answers_lock:
                    entry["pending"], entry["done"] = True, threading.Event()

        with mock.patch.object(FLEET.threading, "Thread", AnswerThenAskAgain):
            rows, missing = self.gather()
        self.assertEqual(["home", "loft", "office"], self.sites(rows))
        self.assertEqual([], missing)

    def test_the_age_of_kept_rows_counts_the_wait(self):
        self.gather()
        self.slow["office"] = threading.Event()
        with mock.patch.object(FLEET, "ANSWER_BUDGET", 1.0):
            for key, entry in FLEET._answers.items():
                if key[1] == "office":
                    entry["at"] = time.time() - 0.5
            _, missing = self.gather()
        self.assertGreaterEqual(missing[0]["age_s"], 1)

    def test_the_age_of_kept_rows_is_never_negative(self):
        self.gather()
        self.slow["office"] = threading.Event()
        for key, entry in FLEET._answers.items():
            if key[1] == "office":
                entry["at"] = time.time() + 30   # a clock that stepped back
        rows, missing = self.gather()
        self.assertIn("office", self.sites(rows))
        self.assertEqual(0, missing[0]["age_s"])


class ForgetOnUnlinkTests(unittest.TestCase):
    def setUp(self):
        self.a = Cluster("loft", "https://10.0.0.1:8088", {})
        self.b = Cluster("shed", "https://10.0.0.2:8088", {})
        self.c = Cluster("garage", "https://10.0.0.3:8088", {})
        wire([self.a, self.b, self.c])
        self.a.fleet.join(self.b.url, "admin", "secret")
        self.a.fleet.join(self.c.url, "admin", "secret")
        for other in (self.b, self.c):
            self.a.fleet._answers[("workloads", other.fleet.self_id(), "alice", "viewer")] = {"result": []}

    def kept(self):
        return sorted(key[1] for key in self.a.fleet._answers)

    def test_unlinking_a_member_forgets_its_answers_only(self):
        self.a.fleet.remove(self.c.fleet.self_id())
        self.assertEqual([self.b.fleet.self_id()], self.kept())

    def test_leaving_forgets_every_answer(self):
        self.a.fleet.leave()
        self.assertEqual([], self.kept())


if __name__ == "__main__":
    unittest.main()
