"""Log alerts: a saved log search that raises an alert when it matches too
many lines (spike 12, built). The alert carries counts, never a line and
never the query: alert text reaches lock screens and config backups."""
import calendar
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import homestead_logalerts as LA
import homestead_logsearch as LOGSEARCH

SECRET = "token=s3cr3t-value"


def workload(name, *pods, ns="lab"):
    return {"ns": ns, "name": name, "pods": [{"name": p, "phase": "Running", "containers": [{"name": name}]}
                                              for p in pods or (f"{name}-1",)]}


WL = [workload("app-x"), workload("app-y"), workload("app-z")]
STAMP = "2026-10-23T10:{:02d}:{:02d}.{:09d}Z"
T0 = calendar.timegm((2026, 10, 23, 10, 1, 2, 0, 0, 0))   # a round at 10:01:02, after the lines of 10:00
M0 = (T0 - 62) // 60                                       # the minute 10:00


def lines(n, minute=0, text="ERROR {} {}".format(SECRET, "{}")):
    return "\n".join(STAMP.format(minute, i % 60, i) + " " + text.format(i) for i in range(n))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.logs, self.calls, self.leader = {}, [], [True]
        self.workloads = list(WL)
        self.withdrawn, self.noted = [], []
        LA.bind(self.dir, workloads=lambda: self.workloads, read=self.read, is_leader=lambda: self.leader[0],
                withdraw=lambda key, title, body: self.withdrawn.append((key, title, body)) or self.raised,
                note=self.noted.append)
        self.raised = {"id": 1}   # what withdraw answers: the alert was raised
        LA.reset()

    def read(self, ns, pod, container, since, limit, previous=False):
        self.calls.append((pod, since, "previous") if previous else (pod, since))
        value = self.logs.get((pod, "previous") if previous else pod, "")
        if isinstance(value, Exception):
            raise value
        return value[:limit]

    def rule(self, **extra):
        body = {"name": "App X trouble", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 10, "window": 1}
        body.update(extra)
        return LA.save(body)

    def stored(self):
        with open(os.path.join(self.dir, "logalerts.json"), encoding="utf-8") as handle:
            return handle.read()


class Rules(Base):
    def test_ids_are_made_by_the_server(self):
        rule = self.rule()
        self.assertRegex(rule["id"], r"^la-[0-9a-f]{12}$")
        self.assertNotEqual(rule["id"], self.rule(name="Another")["id"])

    def test_client_id_must_name_an_existing_rule(self):
        for bad in ("../../etc", "la-000000000000", "la-ZZZ", 7, ""):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "no longer exists"):
                self.rule(id=bad)
        first = self.rule()
        again = self.rule(id=first["id"], threshold=20)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual([20], [r["threshold"] for r in LA.listing()["rules"]])

    def test_name_may_not_repeat_the_query(self):
        with self.assertRaisesRegex(ValueError, "may not repeat the search text"):
            self.rule(name="frigate TOKEN=abc", query="token=abc")
        self.assertEqual("Front door errors", self.rule(name="Front door errors", query="Unable to read")["name"])
        self.assertEqual("App x errors", self.rule(name="App x errors", query="x")["name"], "a query this short is no secret")

    def test_names_are_required_and_short(self):
        for name in ("", "  ", "n" * 61):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "name"):
                self.rule(name=name)

    def test_rules_must_name_one_to_five_running_apps(self):
        with self.assertRaisesRegex(ValueError, "choose the app"):
            self.rule(apps=[])
        with self.assertRaisesRegex(ValueError, "at most 5 apps"):
            self.rule(apps=[f"lab/a{i}" for i in range(6)])
        with self.assertRaisesRegex(ValueError, "not running here"):
            self.rule(apps=["lab/elsewhere"])

    def test_bad_queries_are_refused_as_search_refuses_them(self):
        with self.assertRaisesRegex(ValueError, "take ages"):
            self.rule(query="(a+)+", regex=True)
        with self.assertRaisesRegex(ValueError, "not a valid regular expression"):
            self.rule(query="(", regex=True)

    def test_thresholds_and_windows(self):
        for extra in ({"threshold": 0}, {"threshold": "x"}, {"window": 2}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.rule(**extra)

    def test_at_most_twenty_rules(self):
        for i in range(LA.MAX_RULES):
            self.rule(name=f"Rule {i}")
        with self.assertRaisesRegex(ValueError, "at most 20"):
            self.rule(name="One more")

    def test_delete_takes_only_an_existing_id(self):
        rule = self.rule()
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            LA.delete("la-../x")
        LA.delete(rule["id"])
        self.assertEqual([], LA.listing()["rules"])

    def test_edit_that_changes_what_is_counted_resets_counts(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(3)
        LA.evaluate(now=T0)
        self.rule(id=rule["id"], threshold=5)
        self.assertEqual([[M0, 3], [M0 + 1, 0]], LA.listing()["rules"][0]["counts"], "a threshold change keeps them")
        self.rule(id=rule["id"], query="WARN")
        self.assertEqual([], LA.listing()["rules"][0]["counts"], "a new query starts again")


class Rounds(Base):
    def test_over_threshold_raises_a_fact_without_lines_or_query(self):
        rule = self.rule(query="ERROR")
        self.logs["app-x-1"] = lines(12)
        facts = LA.evaluate(now=T0)
        self.assertEqual(1, len(facts))
        fact = facts[0]
        self.assertEqual((f"logs:{rule['id']}", "logs"), (fact["key"], fact["category"]))
        self.assertIn("12 matching lines in a minute", fact["title"])
        blob = json.dumps(facts) + self.stored()
        self.assertNotIn("s3cr3t", blob)
        self.assertNotIn("ERROR", json.dumps(facts), "nor the query")
        self.assertEqual("/containers?panel=logalerts", fact["href"])

    def test_under_threshold_raises_nothing(self):
        self.rule()
        self.logs["app-x-1"] = lines(10)
        self.assertEqual([], LA.evaluate(now=T0))

    def test_window_sums_recent_minutes(self):
        self.rule(threshold=10, window=5)
        for minute in range(3):
            self.logs["app-x-1"] = lines(4, minute=minute)
            facts = LA.evaluate(now=T0 + 60 * minute)
        self.assertIn("12 matching lines in 5 minutes", facts[0]["title"])

    def test_unreadable_app_is_unknown_not_zero(self):
        rule = self.rule()
        self.logs["app-x-1"] = OSError("pod is being replaced")
        self.assertEqual([{"unknown_key": f"logs:{rule['id']}"}], LA.evaluate(now=T0))
        row = LA.listing()["rules"][0]
        self.assertEqual([[M0, None], [M0 + 1, None]], row["counts"], "this minute and the one just finished")
        self.assertEqual("unread", row["status"]["state"])
        self.assertIn("could not read app-x", row["status"]["detail"])

    def test_reads_beyond_the_minute_limit_are_named(self):
        first = self.rule()
        second = self.rule(name="App Y trouble", apps=["lab/app-y"])
        self.logs["app-x-1"] = self.logs["app-y-1"] = lines(12)
        with mock.patch.object(LA, "MAX_CONTAINERS", 1):
            facts = LA.evaluate(now=T0)
        unknown = [f["unknown_key"][5:] for f in facts if "unknown_key" in f]
        raised = [f["key"][5:] for f in facts if "key" in f]
        self.assertEqual(1, len(unknown))
        self.assertEqual({first["id"], second["id"]}, set(unknown + raised))
        status = {r["id"]: r["status"] for r in LA.listing()["rules"]}[unknown[0]]
        self.assertEqual("skipped", status["state"])
        self.assertIn("containers a minute", status["detail"])

    def test_slow_expression_times_out_and_other_rules_still_count(self):
        slow = self.rule(name="Slow one", query="x", regex=True)
        plain = self.rule(name="Plain one", apps=["lab/app-y"])
        self.logs["app-x-1"] = self.logs["app-y-1"] = lines(12)
        real = LA.matching

        def matching(found, query, regex, case, limit=LA.REGEX_SECONDS):
            if regex:
                raise LA.TooSlow("the expression took more than 2 seconds")
            return real(found, query, regex, case, limit)
        with mock.patch.object(LA, "matching", matching):
            facts = LA.evaluate(now=T0)
        self.assertIn({"unknown_key": f"logs:{slow['id']}"}, facts)
        self.assertTrue(any(f.get("key") == f"logs:{plain['id']}" for f in facts))
        status = {r["id"]: r["status"] for r in LA.listing()["rules"]}[slow["id"]]
        self.assertEqual("slow", status["state"])

    def test_cursor_drops_repeats_and_forgets_gone_pods(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        self.assertEqual(1, len(LA.evaluate(now=T0)))
        self.assertEqual([], LA.evaluate(now=T0 + 60), "the same lines again are not counted twice")
        self.workloads = [workload("app-x", "app-x-2")]
        self.logs["app-x-2"] = lines(12, minute=2)
        self.assertEqual(1, len(LA.evaluate(now=T0 + 120)), "the replacement pod is read")
        self.assertNotIn(("lab", "app-x-1", "app-x"), LA._cursors)

    def test_late_round_reads_since_the_cursor(self):
        self.rule()
        self.logs["app-x-1"] = lines(1)
        LA.evaluate(now=T0)
        cursor_at = LA._cursors[("lab", "app-x-1", "app-x")][1]
        LA.evaluate(now=cursor_at + 180)
        self.assertEqual(185, self.calls[-1][1])
        LA.evaluate(now=cursor_at + 3600)
        self.assertEqual(300, self.calls[-1][1], "at most five minutes back")

    def test_round_keeps_edits_made_meanwhile(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(3)

        def read(*args):
            LA.delete(rule["id"])
            other = self.rule(name="Made meanwhile")
            self.made = other["id"]
            return lines(3)
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        LA.evaluate(now=T0)
        rows = LA.listing()["rules"]
        self.assertEqual([self.made], [r["id"] for r in rows])

    def test_standby_reads_nothing(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.evaluate(now=T0)
        self.leader[0] = False
        calls = len(self.calls)
        LA.step(now=T0 + 60)
        self.assertEqual(calls, len(self.calls))
        self.assertEqual({}, LA._cursors, "a new term starts from a fresh read")

    def test_stale_facts_are_none(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.step(now=T0)
        self.assertEqual(1, len(LA.alert_facts(now=T0 + 100)))
        self.assertIsNone(LA.alert_facts(now=T0 + LA.STALE + 1))
        LA.reset()
        self.assertIsNone(LA.alert_facts(now=T0), "nothing evaluated yet: could not look")

    def test_alert_loop_does_not_read(self):
        self.rule()
        LA.step(now=T0)
        calls = len(self.calls)
        for _ in range(5):
            LA.alert_facts(now=T0 + 10)
        self.assertEqual(calls, len(self.calls))

    def test_reads_are_bounded(self):
        self.rule(apps=["lab/app-x"])
        LA.evaluate(now=T0)
        self.assertEqual([("app-x-1", LA.EVERY)], self.calls)
        self.assertEqual(256 * 1024, LA.MAX_BYTES)


class ReviewFixes(Base):
    """The first review's findings, each pinned."""

    def stamped(self, minute, second, n, start=0):
        return "\n".join(f"2026-10-23T10:{minute:02d}:{second:02d}.{start + i:09d}Z ERROR {i}" for i in range(n))

    def test_a_one_minute_burst_is_raised_on_the_alert_loop_s_first_look(self):
        import homestead_alerts as ALERTS
        ALERTS.bind(self.dir)
        self.rule()
        self.logs["app-x-1"] = lines(12)
        facts = LA.evaluate(now=T0)
        raised = ALERTS.observe({"logs": facts}, now=T0 + 5)
        self.assertEqual(["raised"], [e["phase"] for e in raised],
                         "the counting window is the debounce: no further 60 s hold the next round would cut short")

    def test_lines_count_in_the_minute_they_were_written(self):
        self.rule(threshold=10, window=1)
        self.logs["app-x-1"] = lines(1)
        LA.evaluate(now=T0)                       # cursor at 10:00
        self.logs["app-x-1"] = "\n".join(self.stamped(m, 5, 6, start=m * 100) for m in (1, 2, 3))
        facts = LA.evaluate(now=T0 + 180)         # a late round: three minutes at once
        self.assertEqual([], facts, "6 a minute for three minutes is not 18 in a minute")
        counts = dict(map(tuple, LA.listing()["rules"][0]["counts"]))
        self.assertEqual([6, 6, 6], [counts[M0 + m] for m in (1, 2, 3)])

    def test_the_minute_just_finished_is_checked_in_full(self):
        self.rule(threshold=10, window=1)
        self.logs["app-x-1"] = self.stamped(0, 30, 6)
        self.assertEqual([], LA.evaluate(now=T0 - 30))     # 10:00:32: six so far
        self.logs["app-x-1"] = self.stamped(0, 30, 6) + "\n" + self.stamped(0, 50, 6, start=100)
        facts = LA.evaluate(now=T0)                        # 10:01:02: 12 in 10:00
        self.assertIn("12 matching lines in a minute", facts[0]["title"])

    def test_reading_the_same_lines_twice_counts_them_once(self):
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.evaluate(now=T0)
        LA.evaluate(now=T0 + 5)
        self.assertEqual(12, dict(map(tuple, LA.listing()["rules"][0]["counts"]))[M0])

    def test_an_app_with_nothing_running_is_not_zero(self):
        rule = self.rule()
        self.workloads = [workload("app-x", ns="lab")]
        self.workloads[0]["pods"] = []
        self.assertEqual([{"unknown_key": f"logs:{rule['id']}"}], LA.evaluate(now=T0))
        status = LA.listing()["rules"][0]["status"]
        self.assertEqual("absent", status["state"])
        self.assertIn("nothing of app-x is running", status["detail"])

    def test_an_edit_during_a_round_is_not_overwritten_by_old_counts(self):
        rule = self.rule()

        def read(*args):
            self.rule(id=rule["id"], query="WARN")
            return lines(12)
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        self.assertEqual([], [f for f in LA.evaluate(now=T0) if "key" in f])
        self.assertEqual([], LA.listing()["rules"][0]["counts"])

    def test_a_rule_keeps_an_app_that_is_not_running_now(self):
        rule = self.rule()
        self.workloads = [workload("app-y")]
        self.assertEqual(20, self.rule(id=rule["id"], threshold=20)["threshold"])
        with self.assertRaisesRegex(ValueError, "not running here"):
            self.rule(name="New", apps=["lab/app-x"])

    def test_delete_with_a_body_that_is_not_an_object(self):
        route = LA.ROUTES[("POST", "/api/logalerts/delete")][1]
        for body in (None, [], "x", 3):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "no longer exists"):
                route(mock.Mock(body=body))

    def test_ids_with_a_trailing_newline_are_refused(self):
        rule = self.rule()
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            LA.delete(rule["id"] + "\n")

    def test_the_list_says_when_counting_stopped(self):
        self.rule()
        self.logs["app-x-1"] = lines(3)
        LA.evaluate(now=T0)
        self.assertEqual(int(T0), LA.listing()["counted_at"])

    def test_skipped_rules_take_turns(self):
        first = self.rule()
        second = self.rule(name="App Y trouble", apps=["lab/app-y"])
        skipped = []
        with mock.patch.object(LA, "MAX_CONTAINERS", 1):
            for i in range(2):
                LA.evaluate(now=T0 + 60 * i)
                skipped += [r["id"] for r in LA.listing()["rules"] if r["status"]["state"] == "skipped"]
        self.assertEqual({first["id"], second["id"]}, set(skipped))

    def test_rounds_start_two_seconds_into_each_minute(self):
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            raise StopIteration
        for at in (T0, T0 + 28):
            with mock.patch.object(LA, "step"), self.assertRaises(StopIteration):
                LA.run(sleep=sleep, clock=lambda: at)
        self.assertEqual([60, 32], waits)


class SecondReview(Base):
    """The second review's findings, each pinned."""

    def stamped(self, minute, second, n, start=0):
        return "\n".join(f"2026-10-23T10:{minute:02d}:{second:02d}.{start + i:09d}Z ERROR {i}" for i in range(n))

    def counts(self):
        return dict(map(tuple, LA.listing()["rules"][0]["counts"]))

    def test_a_minute_not_read_stays_null_and_so_does_the_one_before(self):
        self.rule()
        self.logs["app-x-1"] = self.stamped(0, 10, 3)
        LA.evaluate(now=T0)                                   # 10:01:02
        self.logs["app-x-1"] = OSError("gone")
        LA.evaluate(now=T0 + 60)                              # 10:02:02: 10:01 was read for 2 s only
        self.logs["app-x-1"] = self.stamped(1, 30, 5) + "\n" + self.stamped(2, 30, 5, start=50)
        LA.evaluate(now=T0 + 120)                             # 10:03:02
        counts = self.counts()
        self.assertEqual(3, counts[M0])
        self.assertIsNone(counts[M0 + 1], "never a part count, never zero")
        self.assertIsNone(counts[M0 + 2])

    def test_a_cut_read_leaves_the_minutes_after_its_last_line_unread(self):
        self.rule()
        self.logs["app-x-1"] = self.stamped(0, 10, 3)
        LA.evaluate(now=T0 - 60)                             # 10:00:02, cursor set
        self.logs["app-x-1"] = self.stamped(0, 40, 30)
        with mock.patch.object(LA, "MAX_BYTES", 100):
            LA.evaluate(now=T0 + 60)                          # 10:02:02, cut inside 10:00
        counts = self.counts()
        self.assertIsNone(counts[M0 + 1])
        self.assertIsNone(counts[M0 + 2])

    def test_an_edit_mid_round_keeps_a_raised_alert_held(self):
        rule = self.rule()

        def read(*args):
            self.rule(id=rule["id"], case=True)
            return lines(12)
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        self.assertEqual([{"unknown_key": f"logs:{rule['id']}"}], LA.evaluate(now=T0))

    def test_a_late_round_checks_the_minute_it_missed(self):
        self.rule()
        self.logs["app-x-1"] = self.stamped(0, 10, 1)
        LA.evaluate(now=T0)                                   # 10:01:02
        self.logs["app-x-1"] = self.stamped(0, 10, 1) + "\n" + self.stamped(1, 20, 12, start=100)
        facts = LA.evaluate(now=T0 + 125)                     # 10:03:07: 10:02's round never ran
        self.assertIn("12 matching lines in a minute", facts[0]["title"])

    def test_preview_gives_up_on_slow_reads(self):
        import threading
        release = threading.Event()

        def read(*args):
            release.wait(5)
            return ""
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        started = time.monotonic()
        with mock.patch.object(LA, "PREVIEW_BUDGET", 0.3):
            out = LA.preview({"apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1})
        release.set()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(["app-x"], out["unread"])


class LeftoverReads(Base):
    """Security review: reads still running when a budget runs out are not
    abandoned to pile up; nothing new starts until they end."""
    BODY = {"apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1}

    def blocking(self):
        import threading
        release, calls = threading.Event(), []

        def read(*args):
            calls.append(args)
            release.wait(5)
            return ""
        self.addCleanup(release.set)
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        return release, calls

    def settle(self):
        deadline = time.monotonic() + 3
        while LA.leftover_reads() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(0, LA.leftover_reads())

    def test_a_preview_waits_for_the_last_one_s_reads_to_end(self):
        release, calls = self.blocking()
        with mock.patch.object(LA, "PREVIEW_BUDGET", 0.2):
            self.assertEqual(["app-x"], LA.preview(dict(self.BODY))["unread"])
            with self.assertRaisesRegex(ValueError, "still reading logs"):
                LA.preview(dict(self.BODY))
            self.assertEqual(1, len(calls), "no second read was started")
            release.set()
            self.settle()
            self.assertEqual([], LA.preview(dict(self.BODY))["unread"])

    def test_a_round_does_not_start_reads_beside_the_last_round_s(self):
        self.rule()
        release, calls = self.blocking()
        with mock.patch.object(LA, "READ_BUDGET", 0.2):
            self.assertEqual("unread", LA.evaluate(now=T0) and LA.listing()["rules"][0]["status"]["state"])
            LA.evaluate(now=T0 + 60)
            self.assertEqual(1, len(calls), "the first round's read still runs: no second one")
            self.assertIn("still reading", LA.listing()["rules"][0]["status"]["detail"])
            release.set()
            self.settle()
            LA.evaluate(now=T0 + 120)
            self.assertEqual(2, len(calls))


class PreviewBounds(Base):
    BODY = {"name": "", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1}

    def test_preview_reads_less_than_a_search_does(self):
        seen = []
        LA.bind(self.dir, workloads=lambda: self.workloads,
                read=lambda ns, pod, c, since, limit: seen.append(limit) or "", is_leader=lambda: True)
        LA.preview(dict(self.BODY))
        self.assertEqual([LA.PREVIEW_BYTES], seen)
        self.assertLess(LA.PREVIEW_BYTES, LOGSEARCH.MAX_BYTES)

    def test_preview_reads_containers_side_by_side(self):
        import threading
        self.workloads = [workload("app-x", "app-x-1", "app-x-2")]
        both, barrier = [], threading.Barrier(2, timeout=3)

        def read(*args):
            barrier.wait()
            both.append(1)
            return ""
        LA.bind(self.dir, workloads=lambda: self.workloads, read=read, is_leader=lambda: True)
        LA.preview(dict(self.BODY))
        self.assertEqual(2, len(both))

    def test_preview_needs_no_name(self):
        self.assertEqual(60, len(LA.preview(dict(self.BODY, name=""))["counts"]))
        self.assertEqual(60, len(LA.preview(dict(self.BODY, name="ERROR here"))["counts"]))

    def test_a_preview_out_of_time_says_why(self):
        with mock.patch.object(LA, "matching", side_effect=LA.TooSlow("x")):
            with self.assertRaisesRegex(ValueError, "too many lines, or too slow an expression"):
                LA.preview(dict(self.BODY, regex=True))


def restarted(name, restarts, pod=None):
    return {"ns": "lab", "name": name, "pods": [{"name": pod or f"{name}-1", "phase": "Running",
                                                 "containers": [{"name": name, "restarts": restarts}]}]}


class Pause(Base):
    """#929: a rule can be paused and resumed instead of deleted and made again."""

    def test_a_paused_rule_reads_and_raises_nothing_until_resumed(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(12)
        self.assertEqual({"ok": True, "paused": True}, LA.pause(rule["id"], True))
        self.assertEqual([], LA.evaluate(now=T0))
        self.assertEqual([], self.calls, "a paused rule's apps are not read")
        listed = LA.listing()["rules"][0]
        self.assertTrue(listed["paused"])
        self.assertEqual("paused", listed["status"]["state"])
        LA.pause(rule["id"], False)
        self.assertFalse(LA.listing()["rules"][0]["paused"])
        self.assertEqual(1, len(LA.evaluate(now=T0)), "counted again once resumed")

    def test_pausing_a_raised_rule_ends_its_alert_as_paused_not_back_under(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(12)
        self.assertEqual(1, len(LA.evaluate(now=T0)))
        LA.pause(rule["id"], True)
        self.assertEqual([(f"logs:{rule['id']}", "App X trouble: paused")], [w[:2] for w in self.withdrawn])
        self.assertNotIn("back under", self.withdrawn[0][2])
        self.assertEqual([], LA.alert_facts(now=T0 + 5), "the last round's fact is not raised again")
        LA.pause(rule["id"], True)
        self.assertEqual(1, len(self.withdrawn), "pausing a paused rule changes nothing")

    def test_pause_takes_only_an_existing_id(self):
        for bad in (None, "la-000000000000", "x", 5):
            with self.assertRaises(ValueError):
                LA.pause(bad, True)

    def test_an_edit_keeps_a_rule_paused(self):
        rule = self.rule()
        LA.pause(rule["id"], True)
        self.rule(id=rule["id"], threshold=20)
        self.assertTrue(LA.listing()["rules"][0]["paused"])

    def test_a_rule_paused_during_a_round_is_not_raised_by_it(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(12)
        real = LA._count

        def count(*args, **kwargs):
            LA.pause(rule["id"], True)
            return real(*args, **kwargs)
        with mock.patch.object(LA, "_count", count):
            self.assertEqual([], LA.evaluate(now=T0))
        self.assertEqual("paused", LA.listing()["rules"][0]["status"]["state"])

    def test_ten_rounds_in_a_row_out_of_time_pause_the_rule_and_say_so(self):
        self.rule(query="x", regex=True)
        self.logs["app-x-1"] = lines(12)
        with mock.patch.object(LA, "matching", side_effect=LA.TooSlow("the expression took more than 2 seconds")):
            for i in range(LA.AUTO_PAUSE - 1):
                LA.evaluate(now=T0 + 60 * i)
            self.assertFalse(LA.listing()["rules"][0].get("paused"))
            LA.evaluate(now=T0 + 60 * (LA.AUTO_PAUSE - 1))
        listed = LA.listing()["rules"][0]
        self.assertTrue(listed["paused"])
        self.assertIn(f"{LA.AUTO_PAUSE} minutes in a row", listed["status"]["detail"])
        self.assertEqual("App X trouble: paused by Homestead", self.withdrawn[0][1])
        self.assertEqual([], self.noted, "a raised alert ended says so; no second notice")

    def test_an_auto_pause_with_nothing_raised_leaves_a_notice(self):
        self.rule(query="qzqz", regex=True)
        self.raised = None
        self.logs["app-x-1"] = lines(1)
        with mock.patch.object(LA, "matching", side_effect=LA.TooSlow("x")):
            for i in range(LA.AUTO_PAUSE):
                LA.evaluate(now=T0 + 60 * i)
        self.assertEqual(1, len(self.noted))
        self.assertEqual(("logs", "info", "App X trouble: paused by Homestead"),
                         (self.noted[0]["category"], self.noted[0]["severity"], self.noted[0]["title"]))
        self.assertNotIn("qzqz", json.dumps(self.noted), "never the query")
        self.assertTrue(self.noted[0]["event"])

    def test_a_round_in_time_starts_the_count_again(self):
        rule = self.rule(query="x", regex=True)
        self.logs["app-x-1"] = lines(1)
        with mock.patch.object(LA, "matching", side_effect=LA.TooSlow("x")):
            for i in range(LA.AUTO_PAUSE - 1):
                LA.evaluate(now=T0 + 60 * i)
        LA.evaluate(now=T0 + 60 * (LA.AUTO_PAUSE - 1))
        with mock.patch.object(LA, "matching", side_effect=LA.TooSlow("x")):
            LA.evaluate(now=T0 + 60 * LA.AUTO_PAUSE)
        self.assertFalse(LA.listing()["rules"][0].get("paused"), rule["id"])


class Removed(Base):
    def test_deleting_a_raised_rule_ends_its_alert_as_removed(self):
        rule = self.rule()
        self.logs["app-x-1"] = lines(12)
        self.assertEqual(1, len(LA.evaluate(now=T0)))
        LA.delete(rule["id"])
        self.assertEqual([(f"logs:{rule['id']}", "App X trouble: removed")], [w[:2] for w in self.withdrawn])
        self.assertEqual([], LA.alert_facts(now=T0 + 5), "the last round's fact is not raised again")

    def test_facts_say_when_they_were_taken(self):
        # The alert loop takes these, then other sources, then observes: a
        # withdraw in between must win over a fact taken before it (review 1).
        self.rule()
        self.logs["app-x-1"] = lines(12)
        LA.evaluate(now=T0)
        before = time.time()
        facts = LA.alert_facts(now=T0 + 5)
        self.assertTrue(before <= facts[0]["collected"] <= time.time())

    def test_resume_leaves_the_paused_minutes_not_read(self):
        # A 5-minute rule resumed must not count the paused minutes as 0 (review 7).
        rule = self.rule(window=5)
        LA.pause(rule["id"], True, now=T0)
        LA.pause(rule["id"], False, now=T0 + 180)
        counts = dict(map(tuple, LA.listing()["rules"][0]["counts"]))
        start = int(T0 // 60)
        self.assertEqual({m: None for m in range(start, start + 4)}, counts)


class CrashLoops(Base):
    """#929: a container that restarted between rounds wrote lines before it
    died; they are in its previous log, which is read too."""

    def test_a_restart_since_the_last_round_reads_the_previous_log(self):
        rule = self.rule()
        self.workloads = [restarted("app-x", 0)]
        LA.evaluate(now=T0)
        self.workloads = [restarted("app-x", 1)]
        self.logs[("app-x-1", "previous")] = lines(12, minute=1)
        facts = LA.evaluate(now=T0 + 60)
        self.assertTrue([c for c in self.calls if c[-1] == "previous"])
        self.assertEqual([f"logs:{rule['id']}"], [f.get("key") for f in facts])

    def test_no_previous_read_without_a_restart_seen_happening(self):
        self.rule()
        self.workloads = [restarted("app-x", 3)]
        self.logs[("app-x-1", "previous")] = lines(12)
        self.assertEqual([], LA.evaluate(now=T0), "restarts from before the first round are not news")
        self.assertEqual([], LA.evaluate(now=T0 + 60))
        self.assertFalse([c for c in self.calls if c[-1] == "previous"])

    def test_lines_in_both_logs_count_once(self):
        self.rule()
        self.workloads = [restarted("app-x", 0)]
        self.logs["app-x-1"] = lines(6, minute=0)
        LA.evaluate(now=T0)
        self.workloads = [restarted("app-x", 1)]
        self.logs[("app-x-1", "previous")] = lines(6, minute=0) + "\n" + lines(6, minute=1)
        self.logs["app-x-1"] = lines(6, minute=1, text="ERROR again {}")
        facts = LA.evaluate(now=T0 + 60)
        self.assertEqual(1, len(facts))
        self.assertIn("12 matching lines", facts[0]["title"], "the 6 read last round are not counted again")

    def test_a_previous_log_that_cannot_be_read_is_not_zero(self):
        rule = self.rule()
        self.workloads = [restarted("app-x", 0)]
        LA.evaluate(now=T0)
        self.workloads = [restarted("app-x", 1)]
        self.logs[("app-x-1", "previous")] = OSError("gone")
        self.assertEqual([{"unknown_key": f"logs:{rule['id']}"}], LA.evaluate(now=T0 + 60))
        # A previous log that is gone for good (a node rebooted) must not hold
        # the rule unread forever (review 2): that round is not counted, and
        # the next one reads the current container as usual.
        self.logs["app-x-1"] = lines(12, minute=3)   # minute 2 was the failed round: not read
        self.calls.clear()
        facts = LA.evaluate(now=T0 + 120)
        self.assertFalse([c for c in self.calls if c[-1] == "previous"])
        self.assertEqual([f"logs:{rule['id']}"], [f.get("key") for f in facts])
        self.assertEqual("ok", LA.listing()["rules"][0]["status"]["state"])


class Withdraw(unittest.TestCase):
    """homestead_alerts.withdraw: a condition its source stopped watching
    ends at once, with what happened, never as "back under"."""

    def setUp(self):
        import homestead_alerts as ALERTS
        self.A, self.dir = ALERTS, tempfile.mkdtemp()
        ALERTS.bind(self.dir)
        self.fact = {"key": "logs:la-0123456789ab", "category": "logs", "severity": "degraded", "urgent": True,
                     "title": "Door: 12 matching lines in a minute", "resolved": "Door: back under 10 in a minute",
                     "body": "b", "signals": {"level": 0}}

    def test_a_raised_alert_ends_with_the_given_title(self):
        self.A.observe({"logs": [self.fact]}, now=1000)
        row = self.A.active(user="ada")[0]
        self.A.acknowledge("ada", row["key"], row["version"], now=1001)
        entry = self.A.withdraw(self.fact["key"], "Door: removed", "This log alert was removed.", now=1002)
        self.assertEqual(("resolved", "Door: removed", "This log alert was removed."),
                         (entry["phase"], entry["title"], entry["body"]))
        self.assertEqual([], self.A.active())
        self.assertEqual([], self.A.observe({"logs": []}, now=1200), "nothing left to end later as back under")
        self.assertNotIn("back under", json.dumps(self.A.log()["alerts"]))
        with open(os.path.join(self.dir, "alerts.json"), encoding="utf-8") as handle:
            self.assertNotIn(self.fact["key"], json.load(handle)["acknowledgements"].get("ada", {}))

    def test_a_fact_taken_before_the_withdraw_does_not_raise_it_again(self):
        # Review 1: the alert loop took the fact, then a person deleted the rule.
        self.A.observe({"logs": [self.fact]}, now=1000)
        self.A.withdraw(self.fact["key"], "Door: removed", "x", now=1002)
        self.assertEqual([], self.A.observe({"logs": [{**self.fact, "collected": 1001}]}, now=1003))
        self.assertEqual([], self.A.active())
        self.assertEqual([], self.A.observe({"logs": []}, now=1200), "and nothing ends later as back under")
        raised = self.A.observe({"logs": [{**self.fact, "collected": 1300}]}, now=1301)
        self.assertEqual(["raised"], [e["phase"] for e in raised], "taken after it (resumed): raised as usual")
        self.assertNotIn("collected", self.A.active()[0])

    def test_nothing_raised_is_none(self):
        self.assertIsNone(self.A.withdraw(self.fact["key"], "Door: removed", ""))
        self.A.observe({"logs": [{**self.fact, "urgent": False}]}, now=1000)
        self.assertIsNone(self.A.withdraw(self.fact["key"], "Door: removed", "", now=1001), "seen, not yet raised")
        self.assertEqual([], self.A.active())
        self.assertEqual(0, self.A.log()["latest"])


class Matching(unittest.TestCase):
    def test_plain_text_matching_spawns_nothing(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("spawned")):
            self.assertEqual([0, 2], LA.matching(["an Error", "fine", "error"], "error", False, False))
            self.assertEqual([2], LA.matching(["an Error", "fine", "error"], "error", False, True))

    def test_an_expression_is_matched_in_a_child_process(self):
        self.assertEqual([0, 2], LA.matching(["a 500 x", "a 200 x", "b 503"], r" 5\d\d", True, False))

    def test_matching_stops_a_runaway_expression(self):
        # Passes log search's nested-repeat check, yet backtracks for ages.
        LOGSEARCH.matcher(r"^(x?){40}x{40}$", True)
        started = time.monotonic()
        with self.assertRaises(LA.TooSlow):
            LA.matching(["x" * 40], r"^(x?){40}x{40}$", True, False, limit=0.5)
        self.assertLess(time.monotonic() - started, 5)


class Preview(Base):
    def test_preview_shares_the_log_search_lock(self):
        with LOGSEARCH.exclusive():
            with self.assertRaisesRegex(ValueError, "another log search is running"):
                LA.preview({"name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 1, "window": 1})

    def test_preview_counts_per_minute_and_minutes_over(self):
        now = time.time()
        minute = int(now // 60)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((minute - 2) * 60 + 5))
        self.logs["app-x-1"] = "\n".join(f"{stamp}.{i:09d}Z ERROR {SECRET}" for i in range(4))
        out = LA.preview({"name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1}, now=now)
        self.assertEqual(60, len(out["counts"]))
        self.assertEqual(4, dict(map(tuple, out["counts"]))[minute - 2])
        self.assertEqual(1, out["over_minutes"])
        self.assertNotIn("s3cr3t", json.dumps(out))
        self.assertEqual([], out["unread"])

    def test_preview_names_what_it_could_not_read(self):
        self.logs["app-x-1"] = OSError("gone")
        out = LA.preview({"name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1})
        self.assertEqual(["app-x"], out["unread"])

    def test_preview_takes_no_id(self):
        out = LA.preview({"id": "../x", "name": "x", "apps": ["lab/app-x"], "query": "ERROR", "threshold": 3, "window": 1})
        self.assertNotIn("rule", out)


class Wiring(unittest.TestCase):
    def test_routes_and_roles(self):
        import homestead_route_policy as POLICY
        import homestead_routes as ROUTES
        self.assertIn("homestead_logalerts", ROUTES.MODULES)
        self.assertEqual("viewer", POLICY.role("/api/logalerts", "GET"))
        self.assertEqual("viewer", POLICY.role("/api/logalerts/preview", "POST"))
        self.assertEqual("operator", POLICY.role("/api/logalerts", "POST"))
        self.assertEqual("operator", POLICY.role("/api/logalerts/delete", "POST"))
        self.assertEqual("operator", POLICY.role("/api/logalerts/pause", "POST"))

    def test_the_kubernetes_read_asks_for_the_previous_container_when_told(self):
        import homestead_kube as KUBE
        urls = []

        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                return b""

        def urlopen(request, **kwargs):
            urls.append(request.full_url)
            return Answer()
        with mock.patch.multiple(KUBE, API="https://k.example.com", TOKEN="t", CTX=None, create=True), \
                mock.patch.object(KUBE.urllib.request, "urlopen", urlopen):
            KUBE.logsearch_read("lab", "web-1", "web", 60, 100)
            KUBE.logsearch_read("lab", "web-1", "web", 60, 100, True)
        self.assertNotIn("previous", urls[0])
        self.assertIn("&previous=true", urls[1])

    def test_push_category_and_store_listed(self):
        import homestead_housekeeping as HOUSEKEEPING
        import homestead_push as PUSH
        self.assertIn("never the lines", PUSH.CATEGORIES["logs"])
        self.assertNotIn("logs", PUSH.DEFAULT_CATEGORIES)
        self.assertTrue(any("logalerts.json" in files for _, files, _ in HOUSEKEEPING.STORES))

    def test_the_alert_loop_takes_the_last_facts_and_starts_the_worker(self):
        path = os.path.join(os.path.dirname(__file__), "..", "server", "homestead_overview_server.py")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('take("logs", LOGALERTS.alert_facts)', text)
        loop = text[text.index("def _alerts_loop"):]
        self.assertLess(loop.index("LOGALERTS.start()"), loop.index("while True"))


class WikiTests(unittest.TestCase):
    """The wiki page for log alerts is linked, and quotes the limits the code has."""

    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def read(self, name):
        with open(os.path.join(self.ROOT, "docs", "wiki", name), encoding="utf-8") as handle:
            return " ".join(handle.read().split())

    def test_the_page_is_linked_from_the_sidebar_home_and_containers(self):
        for name in ("_Sidebar.md", "Home.md", "Containers.md"):
            self.assertIn("(Log-alerts)", self.read(name), name)

    def test_the_page_quotes_the_limits_in_the_code(self):
        page = self.read("Log-alerts.md")
        self.assertIn(f"runs out of time {LA.AUTO_PAUSE} counts in a row", page)
        self.assertIn(f"There can be {LA.MAX_RULES} alerts", page)
        self.assertIn(f"at most {LA.MAX_CONTAINERS} containers a minute", page)
        self.assertIn(f"gets {LA.REGEX_SECONDS} seconds of matching", page)
        self.assertIn("`list_log_alerts`", page)


if __name__ == "__main__":
    unittest.main()
