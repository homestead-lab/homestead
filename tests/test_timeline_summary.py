"""The daily or weekly summary push (#921): when it is due, what it says,
and that it is sent once."""
import datetime
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as ALERTS
import homestead_push as PUSH
import homestead_shared as SHARED
import homestead_timeline_summary as S

UTC = datetime.timezone.utc


def at(*parts):
    return int(datetime.datetime(*parts, tzinfo=UTC).timestamp())


def row(every="daily", hour=8, tz="Europe/London", day=6, **extra):
    return {"every": every, "hour": hour, "tz": tz, "day": day, **extra}


class ScheduleTests(unittest.TestCase):
    def test_daily_slot_is_today_once_its_hour_has_come(self):
        r = row()
        self.assertEqual(at(2026, 1, 15, 8), S.last_slot(r, at(2026, 1, 15, 10)))
        self.assertEqual(at(2026, 1, 14, 8), S.last_slot(r, at(2026, 1, 15, 7, 59)))
        self.assertEqual(at(2026, 1, 15, 8), S.last_slot(r, at(2026, 1, 15, 8)))

    def test_the_hour_is_the_persons_own_in_summer_time(self):
        # 08:00 in London in July is 07:00 UTC.
        self.assertEqual(at(2026, 7, 15, 7), S.last_slot(row(), at(2026, 7, 15, 9)))

    def test_a_zone_ahead_of_utc_counts_its_own_date(self):
        # 09:00 on 15 January in Auckland is still the 14th in UTC.
        r = row(tz="Pacific/Auckland")
        self.assertEqual(at(2026, 1, 14, 19), S.last_slot(r, at(2026, 1, 14, 20)))

    def test_weekly_is_the_chosen_day(self):
        r = row(every="weekly", day=6, hour=18, tz="America/New_York")    # Sunday 18:00
        # Wednesday 21 January 2026: the last Sunday was the 18th, 18:00 EST = 23:00 UTC.
        self.assertEqual(at(2026, 1, 18, 23), S.last_slot(r, at(2026, 1, 21, 12)))
        self.assertEqual(at(2026, 1, 18, 23) - 7 * 86400, S.previous_slot(r, at(2026, 1, 18, 23)))

    def test_the_day_before_the_clocks_change_is_23_hours_long(self):
        r = row()
        slot = S.last_slot(r, at(2026, 3, 29, 7, 30))           # 08:30 BST
        self.assertEqual(at(2026, 3, 29, 7), slot)
        self.assertEqual(at(2026, 3, 28, 8), S.previous_slot(r, slot))

    def test_next_slot(self):
        self.assertEqual(at(2026, 1, 16, 8), S.next_slot(row(), at(2026, 1, 15, 8)))
        self.assertEqual(at(2026, 1, 25, 23), S.next_slot(row(every="weekly", day=6, hour=18, tz="America/New_York"),
                                                         at(2026, 1, 21, 12)))

    def test_an_hour_the_clocks_skip_or_repeat_is_still_one_slot_a_day(self):
        # 01:00 in London: skipped on 29 March, repeated on 25 October.
        for start in (at(2026, 3, 27), at(2026, 10, 22, 23)):
            with self.subTest(start=start):
                r = row(hour=1)
                slots = {S.last_slot(r, start + minute * 60) for minute in range(0, 5 * 1440, 10)}
                slots.discard(S.last_slot(r, start))
                self.assertEqual(5, len(slots))
                gaps = [b - a for a, b in zip(sorted(slots), sorted(slots)[1:])]
                self.assertTrue(all(23 * 3600 <= g <= 25 * 3600 for g in gaps), gaps)
        # The skipped 01:00 is read with the offset before the change (02:00 BST);
        # the repeated one is its first time (01:00 BST).
        self.assertEqual(at(2026, 3, 29, 1), S.last_slot(row(hour=1), at(2026, 3, 29, 3)))
        self.assertEqual(at(2026, 10, 25, 0), S.last_slot(row(hour=1), at(2026, 10, 25, 3)))

    def test_midnight_in_a_zone_whose_clocks_change_at_midnight(self):
        # Havana skips 00:00-01:00 on 8 March 2026: still one slot that day.
        r = row(hour=0, tz="America/Havana")
        slots = {S.last_slot(r, at(2026, 3, 6) + m * 600) for m in range(0, 4 * 144)}
        self.assertEqual(4, len(slots - {S.last_slot(r, at(2026, 3, 6))}))


class DataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, name, value in ((SHARED, "DIR", self.tmp.name), (ALERTS, "DATA_DIR", self.tmp.name),
                                    (PUSH, "DATA_DIR", self.tmp.name)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def file(self):
        with open(os.path.join(self.tmp.name, S.FILE), encoding="utf-8") as handle:
            return json.load(handle)


class SettingTests(DataDir):
    NOW = at(2026, 1, 15, 10)

    def test_off_by_default(self):
        self.assertEqual("off", S.view("alice", self.NOW)["every"])

    def test_saving_arms_the_current_slot_so_nothing_is_sent_at_once(self):
        saved = S.save("alice", {"every": "daily", "hour": 8, "tz": "Europe/London"}, self.NOW)
        self.assertEqual(at(2026, 1, 16, 8), saved["next"])
        self.assertEqual(at(2026, 1, 15, 8), self.file()["users"]["alice"]["slot"])
        self.assertIsNone(S.due(self.file()["users"]["alice"], self.NOW))

    def test_off_removes_the_row(self):
        S.save("alice", {"every": "weekly", "day": 6, "hour": 8, "tz": "UTC"}, self.NOW)
        self.assertEqual("off", S.save("alice", {"every": "off"}, self.NOW)["every"])
        self.assertNotIn("alice", self.file()["users"])

    def test_bad_settings_are_refused(self):
        for bad in ({"every": "hourly"}, {"every": "daily", "hour": 24, "tz": "UTC"},
                    {"every": "daily", "hour": "8", "tz": "UTC"}, {"every": "weekly", "day": 7, "hour": 8, "tz": "UTC"},
                    {"every": "daily", "hour": 8, "tz": "Mars/Olympus"}, {"every": "daily", "hour": 8, "tz": "../../etc"}):
            with self.subTest(bad), self.assertRaises(ValueError):
                S.save("alice", bad, self.NOW)

    def test_no_account_no_setting(self):
        with self.assertRaises(ValueError):
            S.save("", {"every": "daily", "hour": 8, "tz": "UTC"}, self.NOW)

    def test_routes(self):
        import homestead_audit as AUDIT
        import homestead_routes as ROUTES
        self.assertIn("homestead_timeline_summary", ROUTES.MODULES)
        self.assertEqual("viewer", S.ROUTES[("GET", "/api/timeline/summary")][0])
        self.assertEqual("viewer", S.ROUTES[("POST", "/api/timeline/summary")][0])
        self.assertIn(("POST", "/api/timeline/summary"), AUDIT.NOT_AUDITED)
        post = S.ROUTES[("POST", "/api/timeline/summary")][1]
        with mock.patch.object(S.time, "time", lambda: self.NOW):
            post(types.SimpleNamespace(user="bob", body={"every": "daily", "hour": 7, "tz": "UTC"}))
        self.assertEqual(["bob"], list(self.file()["users"]))


def feed(counts=None, unread=None, bad=0):
    entries = [{"at": 1, "kind": "schedule", "tone": "bad", "text": "x failed"} for _ in range(bad)]
    return {"counts": counts or {}, "entries": entries, "unread": unread or []}


class ComposeTests(unittest.TestCase):
    R = row()
    SLOT = at(2026, 1, 18, 8)
    PREV = at(2026, 1, 17, 8)

    def test_counts_per_kind_and_nothing_failed(self):
        words = S.compose(feed({"change": 3, "restart": 1}), self.R, self.PREV, self.SLOT)
        self.assertEqual("Past day: 3 changes, 1 restart, nothing failed", words["title"])
        self.assertEqual(f"/timeline?since={self.PREV}", words["href"])

    def test_failures_are_counted(self):
        words = S.compose(feed({"schedule": 2}, bad=1), self.R, self.PREV, self.SLOT)
        self.assertEqual("Past day: 2 scheduled job runs, 1 failed", words["title"])

    def test_a_quiet_day(self):
        self.assertEqual("Past day: nothing happened", S.compose(feed(), self.R, self.PREV, self.SLOT)["title"])

    def test_weekly_and_late_ranges(self):
        weekly = row(every="weekly")
        self.assertTrue(S.compose(feed(), weekly, self.SLOT - 7 * 86400, self.SLOT)["title"].startswith("Past week: "))
        late = S.compose(feed(), self.R, at(2026, 1, 14, 8), self.SLOT)["title"]
        self.assertTrue(late.startswith("Since Wed 08:00: "), late)

    def test_an_unreadable_source_is_named_never_counted_as_nothing(self):
        words = S.compose(feed({"change": 1}, unread=[{"source": "restarts", "label": "App restarts", "reason": "x"}]),
                          self.R, self.PREV, self.SLOT)
        self.assertEqual("Past day: 1 change", words["title"])
        self.assertNotIn("restart", words["title"].lower())
        self.assertEqual("App restarts could not be read.", words["body"])
        quiet = S.compose(feed(unread=[{"source": "restarts", "label": "App restarts", "reason": "x"},
                                       {"source": "alerts", "label": "Alerts", "reason": "x"}]), self.R, self.PREV, self.SLOT)
        self.assertEqual("Past day: nothing recorded", quiet["title"])
        self.assertEqual("App restarts and alerts could not be read.", quiet["body"])

    def test_admin_only_and_last_here_are_not_unread(self):
        words = S.compose(feed(unread=[{"source": "audit", "label": "Audit log", "reason": "shown to admins only", "admin_only": True},
                                       {"source": "last-here", "label": "When you were last here", "reason": "x"}]),
                          self.R, self.PREV, self.SLOT)
        self.assertEqual("Past day: nothing happened", words["title"])
        self.assertEqual("", words["body"])

    def test_failures_are_counted_over_the_whole_window(self):
        # The entries are capped at 300; the feed's own count is of all of them.
        words = S.compose({**feed({"change": 400}), "failed": 2, "more": 100}, self.R, self.PREV, self.SLOT)
        self.assertEqual("Past day: 400 changes, 2 failed", words["title"])
        unknown = S.compose({**feed({"change": 400}), "more": 100}, self.R, self.PREV, self.SLOT)
        self.assertEqual("Past day: 400 changes", unknown["title"])

    def test_a_critical_alert_is_an_alert_not_a_failure(self):
        f = feed({"alert": 1})
        f["entries"] = [{"at": 1, "kind": "alert", "tone": "bad", "text": "node down"}]
        self.assertEqual("Past day: 1 alert, nothing failed", S.compose(f, self.R, self.PREV, self.SLOT)["title"])

    def test_title_is_bounded(self):
        many = {k: 12345 for k in ("change", "restart", "host", "schedule", "alert", "action")}
        self.assertLessEqual(len(S.compose(feed(many, bad=3), self.R, at(2026, 1, 1, 8), self.SLOT)["title"]), 100)


class SendTests(DataDir):
    """step() on the leader: due rows get one summary, once."""
    ARMED = at(2026, 1, 14, 8)

    def setUp(self):
        super().setUp()
        self.pushed, self.feeds = [], []
        self.users = [{"name": "alice", "role": "viewer"}, {"name": "root", "role": "admin"}]

        def timeline(user, role, since=None, now=None):
            self.feeds.append((user, role, since))
            return feed({"change": 2})
        for target, name, value in ((S.TIMELINE, "timeline", timeline),
                                    (S.PUSH, "send", lambda wants, urgency="normal": self.push(wants))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for user in ("alice", "root"):
            PUSH.subscribe(user, {"endpoint": f"https://fcm.googleapis.com/fcm/send/{user}"})

    def push(self, wants):
        rows = [r for r in PUSH._read() if wants(r)]
        self.pushed.append(sorted(r["user"] for r in rows))
        return {"sent": len(rows), "removed": 0, "statuses": [201] * len(rows)}

    def arm(self, user="alice", **kw):
        S.save(user, {"every": "daily", "hour": 8, "tz": "Europe/London", **kw}, self.ARMED + 3600)

    def test_off_sends_nothing(self):
        self.assertEqual([], S.step(at(2026, 1, 15, 8), users=self.users))
        self.assertEqual([], self.pushed)

    def test_due_sends_one_summary_to_that_persons_devices_only(self):
        self.arm()
        S.step(at(2026, 1, 15, 8, 0, 30), users=self.users)
        self.assertEqual([["alice"]], self.pushed)
        self.assertEqual([("alice", "viewer", at(2026, 1, 14, 8))], self.feeds)
        mine = PUSH.pending("alice", "https://fcm.googleapis.com/fcm/send/alice")["alerts"]
        self.assertEqual(["Past day: 2 changes, nothing failed"], [a["title"] for a in mine if a["category"] == "summary"])
        theirs = PUSH.pending("root", "https://fcm.googleapis.com/fcm/send/root")["alerts"]
        self.assertEqual([], [a for a in theirs if a["category"] == "summary"])

    def test_the_role_is_the_persons_own(self):
        self.arm("root")
        S.step(at(2026, 1, 15, 9), users=self.users)
        self.assertEqual([("root", "admin", at(2026, 1, 14, 8))], self.feeds)

    def test_once_across_a_restart_and_a_leader_change(self):
        self.arm()
        now = at(2026, 1, 15, 8, 1)
        S.step(now, users=self.users)
        S.step(now + 60, users=self.users)
        # A restart, or another copy taking the lead: a fresh module, the same data volume.
        fresh = importlib.reload(S)
        self.addCleanup(importlib.reload, S)
        with mock.patch.object(fresh.TIMELINE, "timeline", lambda *a, **k: feed()), \
                mock.patch.object(fresh.PUSH, "send", lambda wants, urgency="normal": self.push(wants)):
            fresh.step(now + 120, users=self.users)
        self.assertEqual([["alice"]], self.pushed)

    def test_a_failed_send_is_not_sent_again(self):
        self.arm()
        with mock.patch.object(S.PUSH, "send", mock.Mock(side_effect=OSError("push service down"))):
            S.step(at(2026, 1, 15, 8, 1), users=self.users)
        S.step(at(2026, 1, 15, 8, 2), users=self.users)
        self.assertEqual([], self.pushed)
        self.assertIn("push service down", S.view("alice", at(2026, 1, 15, 9))["result"])

    def test_no_claim_no_send(self):
        self.arm()
        with mock.patch.object(SHARED, "WRITE_GUARD", mock.Mock(side_effect=ValueError("Homestead's data is being moved"))):
            S.step(at(2026, 1, 15, 8, 1), users=self.users)
        self.assertEqual([], self.pushed)
        S.step(at(2026, 1, 15, 8, 2), users=self.users)
        self.assertEqual([["alice"]], self.pushed)

    def test_missed_slots_send_one_late_summary_not_a_backlog(self):
        self.arm()
        S.step(at(2026, 1, 18, 15), users=self.users)          # down from the 14th to the 18th
        S.step(at(2026, 1, 18, 15, 1), users=self.users)
        self.assertEqual([["alice"]], self.pushed)
        self.assertEqual([("alice", "viewer", self.ARMED)], self.feeds)
        # The next starts where the late one ended: nothing is counted twice.
        S.step(at(2026, 1, 19, 8), users=self.users)
        self.assertEqual(at(2026, 1, 18, 15), self.feeds[-1][2])

    def test_each_summary_starts_where_the_last_ended_and_still_reads_past_day(self):
        self.arm()
        S.step(at(2026, 1, 15, 8, 0, 40), users=self.users)
        S.step(at(2026, 1, 16, 8, 0, 20), users=self.users)
        self.assertEqual(at(2026, 1, 15, 8, 0, 40), self.feeds[-1][2])
        mine = PUSH.pending("alice", "https://fcm.googleapis.com/fcm/send/alice")["alerts"]
        self.assertEqual(["Past day: 2 changes, nothing failed"], [a["title"] for a in mine if a["category"] == "summary"])

    def test_two_copies_at_once_send_once(self):
        self.arm()
        import threading
        gate = threading.Barrier(2)

        def slow(user, role, since=None, now=None):
            gate.wait(5)          # both have built their summary before either claims
            return feed({"change": 1})
        with mock.patch.object(S.TIMELINE, "timeline", slow):
            threads = [threading.Thread(target=S.step, args=(at(2026, 1, 15, 8, 1),), kwargs={"users": self.users})
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual([["alice"]], self.pushed)

    def test_saving_the_same_setting_again_keeps_a_due_summary(self):
        self.arm()
        self.arm()                       # saved again, unchanged
        S.save("alice", {"every": "daily", "hour": 8, "tz": "Europe/London"}, at(2026, 1, 15, 8, 0, 10))
        S.step(at(2026, 1, 15, 8, 1), users=self.users)
        self.assertEqual([["alice"]], self.pushed)

    def test_nothing_set_reads_no_accounts(self):
        import homestead_auth as AUTH
        with mock.patch.object(AUTH, "list_users", mock.Mock(side_effect=AssertionError("read"))):
            self.assertEqual([], S.step(at(2026, 1, 15, 8)))

    def test_a_removed_account_loses_its_setting(self):
        self.arm()
        S.forget_user("alice")
        self.assertEqual("off", S.view("alice", at(2026, 1, 15, 9))["every"])
        import homestead_auth_routes as AUTH_ROUTES
        request = types.SimpleNamespace(user="root", body={"username": "Alice"})
        with mock.patch.object(AUTH_ROUTES, "connected", lambda r: None), \
                mock.patch.object(AUTH_ROUTES.AUTH, "delete_user", lambda *a: None), \
                mock.patch.object(AUTH_ROUTES.AUTH, "list_users", lambda: []), \
                mock.patch.object(AUTH_ROUTES, "signin", lambda *a, **k: None), \
                mock.patch.object(AUTH_ROUTES.SUMMARY, "forget_user") as forget:
            AUTH_ROUTES.remove_user(request)
        forget.assert_called_once_with("alice")

    def test_a_removed_account_is_not_sent_to(self):
        self.arm()
        S.step(at(2026, 1, 15, 9), users=[{"name": "root", "role": "admin"}])
        self.assertEqual([], self.pushed)

    def test_no_device_sends_nothing_and_builds_nothing(self):
        self.arm()
        PUSH.unsubscribe("alice", "https://fcm.googleapis.com/fcm/send/alice")
        S.step(at(2026, 1, 15, 9), users=self.users)
        self.assertEqual([], self.pushed)
        self.assertEqual([], self.feeds)


class PrivateEntriesTests(DataDir):
    def test_summaries_stay_out_of_the_shared_log(self):
        ALERTS.note({"key": "summary:alice:1", "category": "summary", "severity": "info", "event": True,
                     "title": "Past day: 1 change", "for": "alice"})
        with mock.patch.object(ALERTS, "AGENTS", [lambda: {}]), mock.patch.object(ALERTS, "DEVICES", [lambda user: []]):
            shared = ALERTS.ROUTES[("GET", "/api/alerts")][1](types.SimpleNamespace(user="bob"))["log"]
        self.assertEqual([], shared)
        import homestead_alert_webhook as HOOK
        self.assertFalse(HOOK._wanted({"category": "summary", "severity": "critical"}, "info"))
        import homestead_timeline as T
        self.assertEqual([], T._alerts(0, 10 ** 10, "admin", []))

    def test_a_person_keeps_one_summary_line_in_the_shared_log(self):
        for n, user in ((1, "alice"), (2, "bob"), (3, "alice")):
            ALERTS.note({"key": f"summary:{user}:{n}", "category": "summary", "severity": "info", "event": True,
                         "title": f"summary {n}", "for": user})
        self.assertEqual(["summary 2", "summary 3"], [a["title"] for a in ALERTS.log(limit=0 or 50)["alerts"]])


if __name__ == "__main__":
    unittest.main()
