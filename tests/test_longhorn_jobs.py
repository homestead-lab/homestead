import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_longhorn as LH
import homestead_longhorn_jobs as JOBS


class RecurringJobRunTests(unittest.TestCase):
    def setUp(self):
        # Put back what server.py bound: a later test reading Longhorn through
        # the server would otherwise ask this file's fake for its recurring jobs.
        saved = (LH.kget, LH.ksend, LH._cache, LH.DEFAULT_NS)
        self.addCleanup(lambda: LH.bind(*saved))
        self.sent = []
        self.crons = [
            {"metadata": {"name": "daily", "uid": "u1"},
             "spec": {"jobTemplate": {"metadata": {"labels": {"recurring-job.longhorn.io": "daily"}},
                                      "spec": {"template": {"spec": {"containers": [{"name": "run"}]}}}}},
             "status": {"lastScheduleTime": "2026-09-23T02:00:00Z", "lastSuccessfulTime": "2026-09-23T02:03:00Z"}},
            {"metadata": {"name": "broken", "uid": "u2"}, "spec": {},
             "status": {"lastScheduleTime": "2026-09-23T02:00:00Z", "lastSuccessfulTime": "2026-09-22T02:03:00Z"}},
            {"metadata": {"name": "busy", "uid": "u3"}, "spec": {},
             "status": {"lastScheduleTime": "2026-09-23T02:00:00Z", "active": [{"name": "busy-1"}]}},
        ]

        def get(path):
            if path.endswith("/cronjobs"):
                return {"items": self.crons}
            if "/cronjobs/" in path:
                name = path.rsplit("/", 1)[-1]
                for cron in self.crons:
                    if cron["metadata"]["name"] == name:
                        return cron
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            raise AssertionError(path)

        LH.bind(get, lambda method, path, body=None, **kw: self.sent.append((method, path, body)) or body, {})

    def test_last_runs_say_whether_they_worked(self):
        runs = JOBS._cron_runs()
        self.assertFalse(runs["daily"]["last_failed"])
        self.assertTrue(runs["broken"]["last_failed"])
        self.assertFalse(runs["busy"]["last_failed"])
        self.assertEqual(1, runs["busy"]["running"])

    def test_run_now_starts_a_job_from_the_schedule(self):
        result = JOBS.run_job("daily")
        method, path, body = self.sent[0]
        self.assertEqual(("POST", "/apis/batch/v1/namespaces/longhorn-system/jobs"), (method, path))
        self.assertEqual(result["job"], body["metadata"]["name"])
        self.assertLessEqual(len(body["metadata"]["name"]), 63)
        self.assertEqual("manual", body["metadata"]["annotations"]["cronjob.kubernetes.io/instantiate"])
        self.assertEqual("u1", body["metadata"]["ownerReferences"][0]["uid"])
        self.assertEqual([{"name": "run"}], body["spec"]["template"]["spec"]["containers"])

    def test_run_now_refuses_what_it_cannot_run(self):
        with self.assertRaises(ValueError):
            JOBS.run_job("Not A Name")
        with self.assertRaisesRegex(ValueError, "not set up"):
            JOBS.run_job("missing")
        self.assertEqual([], self.sent)


class CronRunStateTests(unittest.TestCase):
    """A CronJob's last run, as Data protection and Architecture show it."""

    def test_a_run_after_the_last_success_that_is_no_longer_active_failed(self):
        state = JOBS.cron_run_state({"lastScheduleTime": "2026-09-23T03:00:00Z", "lastSuccessfulTime": "2026-09-22T03:01:00Z"})
        self.assertEqual((True, 0), (state["last_failed"], state["running"]))

    def test_a_run_still_going_has_not_failed(self):
        state = JOBS.cron_run_state({"lastScheduleTime": "2026-09-23T03:00:00Z", "active": [{"name": "job-1"}]})
        self.assertEqual((False, 1), (state["last_failed"], state["running"]))

    def test_a_run_that_succeeded_and_one_that_never_ran_have_not_failed(self):
        self.assertFalse(JOBS.cron_run_state({"lastScheduleTime": "2026-09-23T03:00:00Z",
                                            "lastSuccessfulTime": "2026-09-23T03:02:00Z"})["last_failed"])
        self.assertEqual({"last_run": "", "last_success": "", "running": 0, "last_failed": False}, JOBS.cron_run_state(None))


if __name__ == "__main__":
    unittest.main()
