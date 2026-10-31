import calendar
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_cronjob_health as H


def ts(text):
    return calendar.timegm(__import__("time").strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


# Thursday 22 October 2026, 12:00 UTC (13:00 in London, summer time).
NOW = ts("2026-10-22T12:00:00Z")


def cronjob(name="backup", ns="media", schedule="0 3 * * *", tz="Etc/UTC", last=None, success=None,
            created="2026-01-01T00:00:00Z", suspend=False, owners=None, labels=None, deadline=None, claims=()):
    spec = {"schedule": schedule, "suspend": suspend, "jobTemplate": {"spec": {"template": {"spec": {
        "containers": [{"name": "task", "image": "example/backup:1"}],
        "volumes": [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in claims]}}}}}
    if tz:
        spec["timeZone"] = tz
    if deadline is not None:
        spec["startingDeadlineSeconds"] = deadline
    status = {}
    if last:
        status["lastScheduleTime"] = last
    if success:
        status["lastSuccessfulTime"] = success
    return {"metadata": {"name": name, "namespace": ns, "uid": f"uid-{ns}-{name}", "creationTimestamp": created,
                         "labels": labels or {}, "ownerReferences": owners or []},
            "spec": spec, "status": status}


def job(cj, started, outcome=None, finished=None, message="", active=0):
    conditions = []
    if outcome == "ok":
        conditions.append({"type": "Complete", "status": "True", "lastTransitionTime": finished})
    elif outcome == "failed":
        conditions.append({"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded",
                           "message": message, "lastTransitionTime": finished})
    status = {"startTime": started, "conditions": conditions, "active": active}
    if outcome == "ok":
        status["completionTime"] = finished
    return {"metadata": {"name": f"{cj['metadata']['name']}-{ts(started)}", "namespace": cj["metadata"]["namespace"],
                         "creationTimestamp": started,
                         "ownerReferences": [{"kind": "CronJob", "name": cj["metadata"]["name"], "uid": cj["metadata"]["uid"],
                                                              "controller": True}]},
            "status": status}


class ResultTests(unittest.TestCase):
    def test_the_newest_finished_run_gives_the_result(self):
        cj = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-21T03:00:40Z")
        jobs = [job(cj, "2026-10-21T03:00:00Z", "ok", "2026-10-21T03:00:40Z"),
                job(cj, "2026-10-22T03:00:00Z", "failed", "2026-10-22T03:04:00Z", "Job has reached the specified backoff limit")]
        got = H.health(cj, jobs, NOW)
        self.assertEqual("failed", got["result"])
        self.assertEqual("2026-10-22T03:04:00Z", got["result_at"])
        self.assertIn("backoff limit", got["result_reason"])
        self.assertEqual("2026-10-21T03:00:40Z", got["last_success"])

    def test_a_run_in_progress_does_not_hide_the_failure_before_it(self):
        cj = cronjob(last="2026-10-22T11:00:00Z")
        jobs = [job(cj, "2026-10-22T03:00:00Z", "failed", "2026-10-22T03:04:00Z"),
                job(cj, "2026-10-22T11:00:00Z", active=1)]
        self.assertEqual("failed", H.health(cj, jobs, NOW)["result"])

    def test_a_success_clears_it(self):
        cj = cronjob(last="2026-10-22T03:00:00Z")
        jobs = [job(cj, "2026-10-21T03:00:00Z", "failed", "2026-10-21T03:04:00Z"),
                job(cj, "2026-10-22T03:00:00Z", "ok", "2026-10-22T03:01:00Z")]
        self.assertEqual("ok", H.health(cj, jobs, NOW)["result"])

    def test_other_cronjobs_runs_are_not_counted(self):
        cj, other = cronjob(last="2026-10-22T03:00:00Z"), cronjob(name="other")
        jobs = [job(other, "2026-10-22T03:00:00Z", "failed", "2026-10-22T03:04:00Z")]
        self.assertNotEqual("failed", H.health(cj, jobs, NOW)["result"])

    def test_an_older_success_does_not_stand_in_for_a_newer_run_whose_job_is_gone(self):
        # failedJobsHistoryLimit 0: last night's failed Job is gone, an older success remains.
        cj = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-21T03:00:40Z")
        jobs = [job(cj, "2026-10-21T03:00:00Z", "ok", "2026-10-21T03:00:40Z")]
        self.assertEqual("unknown", H.health(cj, jobs, NOW)["result"])

    def test_never_run(self):
        self.assertEqual("never", H.health(cronjob(), [], NOW)["result"])

    def test_runs_that_kubernetes_cleaned_up_fall_back_to_the_last_success(self):
        cj = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-22T03:00:30Z")
        self.assertEqual("ok", H.health(cj, [], NOW)["result"])
        gone = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-20T03:00:30Z")
        self.assertEqual("unknown", H.health(gone, [], NOW)["result"], "a run with no record is not called fine")

    def test_another_namespaces_unread_runs_carry_no_raw_error(self):
        got = H.health(cronjob(), None, NOW, error="GET https://10.0.0.1:6443/apis/batch/v1/jobs: forbidden", detail=False)
        self.assertEqual("unknown", got["result"])
        self.assertNotIn("6443", got["result_reason"])

    def test_unread_runs_are_unknown_never_fine(self):
        cj = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-22T03:00:30Z")
        got = H.health(cj, None, NOW, error="forbidden")
        self.assertEqual("unknown", got["result"])
        self.assertIn("forbidden", got["result_reason"])

    def test_who_owns_it(self):
        self.assertEqual("longhorn", H.health(cronjob(owners=[{"kind": "RecurringJob", "name": "trim"}]), [], NOW)["owner"])
        self.assertEqual("flux", H.health(cronjob(labels={"kustomize.toolkit.fluxcd.io/name": "apps"}), [], NOW)["owner"])
        self.assertEqual("flux", H.health(cronjob(labels={"helm.toolkit.fluxcd.io/name": "x"}), [], NOW)["owner"])
        self.assertEqual("homestead", H.health(cronjob(labels={"homestead.io/managed": "true"}), [], NOW)["owner"])
        self.assertEqual("", H.health(cronjob(), [], NOW)["owner"])

    def test_the_volumes_it_mounts(self):
        self.assertEqual(["photos-data"], H.health(cronjob(claims=["photos-data"]), [], NOW)["claims"])


class MissedTests(unittest.TestCase):
    def test_a_daily_run_that_did_not_happen(self):
        cj = cronjob(last="2026-10-20T03:00:00Z")
        self.assertIs(True, H.missed(cj, NOW))

    def test_one_that_ran_on_time(self):
        self.assertIs(False, H.missed(cronjob(last="2026-10-22T03:00:00Z"), NOW))

    def test_within_the_grace_it_is_not_missed_yet(self):
        cj = cronjob(schedule="50 11 * * *", last="2026-10-21T11:50:00Z")
        self.assertIs(False, H.missed(cj, NOW), "due ten minutes ago: still within the grace")
        self.assertIs(True, H.missed(cj, NOW + 10 * 60))

    def test_a_longer_starting_deadline_is_the_grace(self):
        cj = cronjob(schedule="0 11 * * *", last="2026-10-21T11:00:00Z", deadline=7200)
        self.assertIs(False, H.missed(cj, NOW))

    def test_in_its_own_time_zone(self):
        # 03:00 in London is 02:00 UTC in October; it last ran yesterday's.
        cj = cronjob(tz="Europe/London", last="2026-10-21T02:00:00Z")
        self.assertIs(True, H.missed(cj, NOW))
        self.assertIs(False, H.missed(cronjob(tz="Europe/London", last="2026-10-22T02:00:00Z"), NOW))

    def test_new_and_not_yet_due(self):
        cj = cronjob(created="2026-10-22T08:00:00Z")
        self.assertIs(False, H.missed(cj, NOW), "created after today's run time: first due tomorrow")

    def test_created_long_ago_and_never_run(self):
        self.assertIs(True, H.missed(cronjob(created="2026-10-01T00:00:00Z"), NOW))

    def test_suspended_and_longhorns_are_not_judged(self):
        self.assertIs(False, H.missed(cronjob(last="2026-10-01T03:00:00Z", suspend=True), NOW))
        self.assertIs(False, H.missed(cronjob(last="2026-10-01T03:00:00Z", owners=[{"kind": "RecurringJob"}]), NOW))

    def test_an_unreadable_schedule_or_zone_is_unknown(self):
        self.assertIsNone(H.missed(cronjob(schedule="every night", last="2026-10-01T03:00:00Z"), NOW))
        self.assertIsNone(H.missed(cronjob(tz="Mars/Base", last="2026-10-01T03:00:00Z"), NOW))

    def test_without_a_zone_only_the_clocks_its_last_run_fits_count(self):
        # The controller's clock is unknown; its last run at 02:00 UTC fits a
        # 03:00 schedule on UTC+1 (or the hour either side of a clock change).
        self.assertIs(True, H.missed(cronjob(tz=None, last="2026-10-19T02:00:00Z"), NOW))
        self.assertIs(False, H.missed(cronjob(tz=None, last="2026-10-22T02:00:00Z"), NOW))

    def test_with_no_times_at_all_it_is_unknown(self):
        cj = cronjob()
        del cj["metadata"]["creationTimestamp"]
        self.assertIsNone(H.missed(cj, NOW))

    def test_without_a_zone_or_a_last_run_it_is_unknown(self):
        self.assertIsNone(H.missed(cronjob(tz=None), NOW))

    def test_a_frequent_job(self):
        self.assertIs(False, H.missed(cronjob(schedule="*/5 * * * *", tz=None, last="2026-10-22T11:55:00Z"), NOW))
        self.assertIs(True, H.missed(cronjob(schedule="*/5 * * * *", tz=None, last="2026-10-22T11:00:00Z"), NOW))


    def test_a_run_missed_long_ago_stays_missed(self):
        # A yearly job whose last run was over a year ago, created long before.
        cj = cronjob(schedule="0 3 1 1 *", last="2025-01-01T03:00:00Z", created="2024-06-01T00:00:00Z")
        self.assertIs(True, H.missed(cj, NOW))
        self.assertIs(False, H.missed(cronjob(schedule="0 3 1 1 *", last="2026-01-01T03:00:00Z"), NOW))

    def test_a_run_still_going_is_not_a_missed_one(self):
        # With concurrencyPolicy Forbid the controller skips a due run while one runs.
        cj = cronjob(last="2026-10-20T03:00:00Z")
        cj["status"]["active"] = [{"name": "backup-1"}]
        self.assertIs(False, H.missed(cj, NOW))

    def test_a_time_skipped_by_the_clocks_going_forward_is_not_missed(self):
        # 01:30 does not exist in London on 29 March 2026.
        cj = cronjob(schedule="30 1 * * *", tz="Europe/London", last="2026-03-28T01:30:00Z")
        self.assertIs(False, H.missed(cj, ts("2026-03-29T12:00:00Z")))


class OwnershipTests(unittest.TestCase):
    """Security review: anyone who can create a Job somewhere must not be able
    to make a failing CronJob look fine, or a fine one look failed."""
    def test_a_job_in_another_namespace_naming_the_uid_is_ignored(self):
        cj = cronjob(last="2026-10-21T03:00:00Z")
        fake = job(cj, "2026-10-22T03:00:00Z", "ok", "2026-10-22T03:01:00Z")
        fake["metadata"]["namespace"] = "elsewhere"
        real = job(cj, "2026-10-21T03:00:00Z", "failed", "2026-10-21T03:04:00Z")
        self.assertEqual("failed", H.health(cj, [real, fake], NOW)["result"])

    def test_a_job_owned_by_another_kind_or_name_is_ignored(self):
        cj = cronjob(last="2026-10-21T03:00:00Z")
        real = job(cj, "2026-10-21T03:00:00Z", "failed", "2026-10-21T03:04:00Z")
        other_kind, other_name = (job(cj, "2026-10-22T03:00:00Z", "ok", "2026-10-22T03:01:00Z") for _ in range(2))
        other_kind["metadata"]["ownerReferences"][0]["kind"] = "Deployment"
        other_name["metadata"]["ownerReferences"][0]["name"] = "not-backup"
        self.assertEqual("failed", H.health(cj, [real, other_kind, other_name], NOW)["result"])


def brute(schedule, tz, after, limit, offset=0):
    """The minute-by-minute answer the direct one must equal."""
    matches, clock = H._matcher(schedule), H._clock(tz, offset)
    start = -(-int(after) // 60) * 60
    return next((t for t in range(start, start + int(limit), 60) if matches(*clock(t))), float("inf"))


class FirstFireTests(unittest.TestCase):
    def test_equals_a_minute_by_minute_scan(self):
        cases = [("0 3 * * *", "Etc/UTC"), ("30 1 * * *", "Europe/London"), ("*/5 * * * *", ""),
                 ("15 2 1 * *", "Europe/Berlin"), ("0 4 * * 6", "America/New_York"), ("0 0 1,15 * 1", "Etc/UTC"),
                 ("@weekly", "Europe/London"), ("7 */6 * jan,oct mon-fri", "Australia/Sydney")]
        # Around both of London's clock changes in 2026, and an ordinary day.
        # 2026-10-25T00:31Z sits between London's two 01:30s (BST and GMT).
        for start in (ts("2026-03-28T22:13:00Z"), ts("2026-10-24T23:59:30Z"), ts("2026-10-25T00:31:00Z"), NOW):
            for schedule, tz in cases:
                for offset in ((0, 3600, -16200) if not tz else (0,)):
                    with self.subTest(schedule=schedule, tz=tz, start=start, offset=offset):
                        H._firsts.clear()
                        self.assertEqual(brute(schedule, tz, start, 40 * 86400, offset),
                                         H.first_fire(schedule, tz, start, 40 * 86400, offset))

    def test_a_schedule_that_fires_rarely_or_never_costs_days_not_minutes(self):
        H._firsts.clear()
        H._steps[0] = 0
        for schedule in ("0 0 29 2 *", "0 0 31 4 *", "0 0 30 2 *"):
            self.assertEqual(float("inf"), H.first_fire(schedule, "Europe/London", NOW, 43 * 86400))
        self.assertLessEqual(H._steps[0], 3 * 45)

    def test_a_thousand_cronjobs_stay_bounded(self):
        import time as clock
        H._firsts.clear()
        H._steps[0] = 0
        began = clock.monotonic()
        rows = [H.health(cronjob(name=f"j{i}", schedule="0 0 29 2 *", tz=None if i % 2 else "Europe/London",
                                 last="2024-02-29T00:00:00Z"), [], NOW) for i in range(1000)]
        self.assertLess(clock.monotonic() - began, 5)
        self.assertLessEqual(H._steps[0], 1000 * 4 * 45)
        self.assertTrue(all(r["missed"] in (False, None) for r in rows))


class CacheTests(unittest.TestCase):
    def test_next_runs_do_not_fill_the_cache(self):
        H._firsts.clear()
        for later in range(50):
            H.next_run(cronjob(tz="Europe/London"), NOW + later * 60)
        self.assertEqual(0, len(H._firsts))

    def test_a_later_start_in_the_same_minute_is_not_served_the_earlier_answer(self):
        H._firsts.clear()
        self.assertEqual(60_000 * 60, H.first_fire("* * * * *", "Etc/UTC", 60_000 * 60, 3600))
        self.assertEqual(60_000 * 60 + 60, H.first_fire("* * * * *", "Etc/UTC", 60_000 * 60 + 1, 3600))

    def test_the_missed_check_is_cached_while_its_last_run_stands(self):
        H._firsts.clear()
        cj = cronjob(schedule="0 3 * * 0", last="2026-10-18T03:00:00Z")
        H.missed(cj, NOW)
        size = len(H._firsts)
        for later in range(1, 30):
            H.missed(cj, NOW + later * 8)
        self.assertEqual(size, len(H._firsts), "the same question, not a new scan every pass")


class FrequentTests(unittest.TestCase):
    def test_more_than_once_an_hour(self):
        self.assertTrue(H.health(cronjob(schedule="*/5 * * * *"), [], NOW)["frequent"])
        self.assertFalse(H.health(cronjob(schedule="0 * * * *"), [], NOW)["frequent"])
        self.assertFalse(H.health(cronjob(schedule="0,30 2 * * *"), [], NOW)["frequent"], "twice a day")


class NextRunTests(unittest.TestCase):
    def test_in_utc_and_in_a_zone(self):
        self.assertEqual("2026-10-23T03:00:00Z", H.next_run(cronjob(), NOW))
        self.assertEqual("2026-10-23T02:00:00Z", H.next_run(cronjob(tz="Europe/London"), NOW))

    def test_none_when_unknown_suspended_or_far_off(self):
        self.assertEqual("", H.next_run(cronjob(tz=None), NOW), "the host's clock is not known")
        self.assertEqual("", H.next_run(cronjob(suspend=True), NOW))
        self.assertEqual("", H.next_run(cronjob(schedule="0 0 1 1 *"), NOW), "more than eight days away")


class AlertTests(unittest.TestCase):
    def rows(self, *pairs):
        return [{"ns": cj["metadata"]["namespace"], "name": cj["metadata"]["name"], "suspend": cj["spec"]["suspend"],
                 "schedule": cj["spec"]["schedule"], "created": cj["metadata"]["creationTimestamp"],
                 **H.health(cj, jobs, NOW)} for cj, jobs in pairs]

    def test_a_failed_run_alerts(self):
        cj = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-21T03:00:30Z")
        facts = H.alert_facts(self.rows((cj, [job(cj, "2026-10-22T03:00:00Z", "failed", "2026-10-22T03:04:00Z", "backoff")])), NOW)
        self.assertEqual(["cronjob:media/backup:failed"], [f["key"] for f in facts])
        fact = facts[0]
        self.assertEqual(("degraded", "degraded"), (fact["category"], fact["severity"]))
        self.assertIn("backup", fact["title"])
        self.assertIn("media", fact["body"])
        # Not ?job=: the alert's Review button opens ?job= in the Jobs tray.
        self.assertEqual("/schedules?find=media/backup", fact["href"])

    def test_a_missed_run_alerts(self):
        rows = self.rows((cronjob(last="2026-10-20T03:00:00Z", success="2026-10-20T03:00:30Z"), []))
        rows[0]["result"] = "ok"
        self.assertEqual(["cronjob:media/backup:missed"], [f["key"] for f in H.alert_facts(rows, NOW)])

    def test_quiet_for_suspended_longhorn_and_healthy(self):
        cj = cronjob(last="2026-10-22T03:00:00Z", suspend=True)
        lh = cronjob(name="trim", ns="longhorn-system", last="2026-10-22T03:00:00Z", owners=[{"kind": "RecurringJob"}])
        ok = cronjob(name="ok", last="2026-10-22T03:00:00Z")
        failed = lambda c: [job(c, "2026-10-22T03:00:00Z", "failed", "2026-10-22T03:04:00Z")]
        rows = self.rows((cj, failed(cj)), (lh, failed(lh)), (ok, [job(ok, "2026-10-22T03:00:00Z", "ok", "2026-10-22T03:01:00Z")]))
        self.assertEqual([], H.alert_facts(rows, NOW))

    def test_a_frequent_job_alerts_only_after_half_an_hour_without_success(self):
        cj = cronjob(schedule="*/5 * * * *", last="2026-10-22T11:55:00Z", success="2026-10-22T11:45:30Z")
        failed = [job(cj, "2026-10-22T11:55:00Z", "failed", "2026-10-22T11:56:00Z")]
        self.assertEqual([], H.alert_facts(self.rows((cj, failed)), NOW))
        cj["status"]["lastSuccessfulTime"] = "2026-10-22T11:20:00Z"
        self.assertEqual(["cronjob:media/backup:failed"], [f["key"] for f in H.alert_facts(self.rows((cj, failed)), NOW)])

    def test_unread_results_neither_raise_nor_clear_failures(self):
        cj = cronjob(last="2026-10-22T03:00:00Z")
        rows = [{"ns": "media", "name": "backup", "suspend": False, **H.health(cj, None, NOW, error="timed out")}]
        facts = H.alert_facts(rows, NOW)
        self.assertIn({"unknown_key": "cronjob:media/backup:failed"}, facts)
        self.assertFalse([f for f in facts if f.get("key", "").endswith(":failed")])
        self.assertNotIn({"unknown_prefix": "cronjob:"}, facts, "a missed run does not depend on the Jobs read")

    def test_a_failure_whose_job_is_gone_is_kept_not_cleared(self):
        # Review: ttlSecondsAfterFinished removed the failed Job; nothing says it succeeded.
        cj = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-21T03:00:30Z")
        for jobs in ([], [job(cj, "2026-10-22T11:00:00Z", active=1)]):
            rows = self.rows((cj, jobs))
            # Known (from the CronJob's status), just not a new result: kept, not held as unread.
            self.assertIn({"keep_key": "cronjob:media/backup:failed"}, H.alert_facts(rows, NOW), rows[0]["result"])

    def test_a_kept_failure_is_current_not_last_known_after_an_hour(self):
        """A run still going, or one Kubernetes no longer keeps, is read every
        cycle: its failure alert must not say it could not be checked."""
        import tempfile
        import homestead_alerts as ALERTS
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        old = ALERTS.DATA_DIR
        ALERTS.bind(directory.name)
        self.addCleanup(ALERTS.bind, old)
        cj = cronjob(last="2026-10-22T03:00:00Z", success="2026-10-21T03:00:30Z")
        failed = H.alert_facts(self.rows((cj, [job(cj, "2026-10-22T03:00:00Z", "failed", "2026-10-22T03:04:00Z")])), NOW)
        ALERTS.observe({"cronjobs": failed}, now=1000)
        ALERTS.observe({"cronjobs": failed}, now=1000 + ALERTS.HOLD)
        running = H.alert_facts(self.rows((cj, [job(cj, "2026-10-22T11:00:00Z", active=1)])), NOW)
        unread = [{"ns": "media", "name": "backup", "suspend": False, **H.health(cj, None, NOW, error="timed out")}]
        unread = H.alert_facts(unread, NOW)
        key = "cronjob:media/backup:failed"
        row = lambda at: next(r for r in ALERTS.active(now=at) if r["key"] == key)
        for at in (2000, 2000 + ALERTS.STALE, 2000 + 2 * ALERTS.STALE):
            ALERTS.observe({"cronjobs": running}, now=at)
        self.assertEqual((False, failed[0]["body"]), (row(2000 + 2 * ALERTS.STALE)["stale"], row(2000 + 2 * ALERTS.STALE)["body"]))
        # Its runs not read at all for an hour: that one is last known.
        start = 3000 + 2 * ALERTS.STALE
        for at in (start, start + ALERTS.STALE):
            ALERTS.observe({"cronjobs": unread}, now=at)
        self.assertTrue(row(start + ALERTS.STALE)["stale"])

    def test_an_unknown_missed_state_keeps_that_alert(self):
        rows = self.rows((cronjob(tz=None), []))
        self.assertIn({"unknown_key": "cronjob:media/backup:missed"}, H.alert_facts(rows, NOW))


if __name__ == "__main__":
    unittest.main()
