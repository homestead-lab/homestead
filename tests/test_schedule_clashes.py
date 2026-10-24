"""Schedule clashes (homestead_schedule_clashes): a trim or a backup that
starts with a backup on a shared volume, in the coming week."""
import calendar
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_schedule_clashes as CLASHES

# Friday 23 October 2026, 12:00 UTC.
NOW = calendar.timegm((2026, 10, 23, 12, 0, 0))
SATURDAY_0200 = calendar.timegm((2026, 10, 24, 2, 0, 0))


def job(name, task, cron, volumes):
    return {"name": name, "task": task, "cron": cron, "volumes": volumes}


class RunsTests(unittest.TestCase):
    def test_runs_are_whole_minutes_in_the_window(self):
        runs = CLASHES.runs("0 2 * * *", NOW, NOW + 7 * 86400)
        self.assertEqual(7, len(runs))
        self.assertEqual(SATURDAY_0200, runs[0])

    def test_runs_macros_and_names(self):
        self.assertEqual(7, len(CLASHES.runs("@daily", NOW, NOW + 7 * 86400)))
        self.assertEqual([calendar.timegm((2026, 10, 24, 4, 0, 0))], CLASHES.runs("0 4 * * sat", NOW, NOW + 7 * 86400))
        self.assertEqual(1, len(CLASHES.runs("0 4 * * 7", NOW, NOW + 7 * 86400)), "7 is Sunday too")

    def test_unreadable_cron_has_no_runs(self):
        for cron in ("garbage", "", None, "61 * * * *", "* * * *", "*/0 * * * *"):
            self.assertEqual([], CLASHES.runs(cron, NOW, NOW + 7 * 86400), cron)


class ClashTests(unittest.TestCase):
    def test_trim_and_backup_same_minute_is_a_clash(self):
        found = CLASHES.clashes([job("nightly-backup", "backup", "0 2 * * *", ["pvc-a", "pvc-b"]),
                                 job("weekly-trim", "filesystem-trim", "0 2 * * 6", ["pvc-b", "pvc-c"])], NOW)
        self.assertEqual(1, len(found))
        row = found[0]
        self.assertEqual("clash", row["severity"])
        self.assertEqual({"nightly-backup", "weekly-trim"}, {j["name"] for j in row["jobs"]})
        self.assertEqual(["pvc-b"], row["volumes"])
        self.assertEqual(1, row["count"])
        self.assertEqual([[SATURDAY_0200, SATURDAY_0200]], row["times"])
        self.assertEqual(0, row["gap_min"])

    def test_snapshot_during_backup_is_not_a_clash(self):
        found = CLASHES.clashes([job("nightly-backup", "backup", "0 2 * * *", ["pvc-a"]),
                                 job("hourly-snapshot", "snapshot", "0 * * * *", ["pvc-a"]),
                                 job("cleanup", "snapshot-cleanup", "0 2 * * *", ["pvc-a"])], NOW)
        self.assertEqual([], found)

    def test_backup_and_backup_within_30_min_is_close(self):
        found = CLASHES.clashes([job("offsite", "backup-force-create", "20 2 * * *", ["pvc-a"]),
                                 job("nightly-backup", "backup", "0 2 * * *", ["pvc-a"])], NOW)
        self.assertEqual(1, len(found))
        row = found[0]
        self.assertEqual(("close", 20, 7), (row["severity"], row["gap_min"], row["count"]))
        # The earlier job first, with its own time first.
        self.assertEqual("nightly-backup", row["jobs"][0]["name"])
        self.assertEqual([SATURDAY_0200, SATURDAY_0200 + 20 * 60], row["times"][0])

    def test_more_than_30_min_apart_is_fine(self):
        self.assertEqual([], CLASHES.clashes([job("a", "backup", "0 2 * * *", ["pvc-a"]),
                                              job("b", "backup", "31 2 * * *", ["pvc-a"])], NOW))

    def test_no_shared_volume_no_clash(self):
        self.assertEqual([], CLASHES.clashes([job("a", "backup", "0 2 * * *", ["pvc-a"]),
                                              job("b", "filesystem-trim", "0 2 * * *", ["pvc-b"]),
                                              job("c", "backup", "0 2 * * *", [])], NOW))

    def test_trim_and_trim_is_not_a_clash(self):
        self.assertEqual([], CLASHES.clashes([job("a", "filesystem-trim", "0 2 * * *", ["pvc-a"]),
                                              job("b", "filesystem-trim", "0 2 * * *", ["pvc-a"])], NOW))

    def test_a_daily_pair_is_one_row(self):
        found = CLASHES.clashes([job("a", "backup", "0 2 * * *", ["pvc-a"]),
                                 job("b", "backup", "0 2 * * *", ["pvc-a"])], NOW)
        self.assertEqual(1, len(found))
        self.assertEqual(7, found[0]["count"])
        self.assertEqual(7, len(found[0]["times"]))

    def test_times_are_capped_but_counted(self):
        found = CLASHES.clashes([job("a", "backup", "*/5 * * * *", ["pvc-a"]),
                                 job("b", "backup", "*/5 * * * *", ["pvc-a"])], NOW)
        self.assertEqual("clash", found[0]["severity"])
        self.assertEqual(7 * 24 * 12, found[0]["count"])
        self.assertEqual(CLASHES.KEEP_TIMES, len(found[0]["times"]))

    def test_every_minute_backups_do_not_stall_the_overview(self):
        jobs = [job(f"b{i}", "backup", "* * * * *", ["pvc-a"]) for i in range(3)]
        started = time.perf_counter()
        found = CLASHES.clashes(jobs, NOW)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual({"clash"}, {row["severity"] for row in found})
        self.assertEqual(3, len(found), "one row per pair; close runs of jobs that already clash say nothing more")

    def test_a_pair_whose_order_flips_is_one_row(self):
        # 02:00 then 02:20 on weekdays; 02:20 then 02:30 at weekends.
        found = CLASHES.clashes([job("a", "backup", "0 2 * * 1-5", ["pvc-a"]), job("a2", "backup", "30 2 * * 0,6", ["pvc-a"]),
                                 job("b", "backup", "20 2 * * *", ["pvc-a"])], NOW)
        self.assertEqual(2, len(found))
        self.assertEqual({("a", "b"), ("b", "a2")}, {tuple(j["name"] for j in row["jobs"]) for row in found})
        flip = CLASHES.clashes([job("a", "backup", "0 2 * * 1-5", ["pvc-a"]), job("a", "backup", "40 2 * * 0,6", ["pvc-a"]),
                                job("b", "backup", "20 2 * * *", ["pvc-a"])], NOW)
        self.assertEqual(1, len(flip), "a and b, in whichever order they start, are one pair")
        self.assertEqual(7, flip[0]["count"])

    def test_clashes_come_before_close_ones(self):
        found = CLASHES.clashes([job("a", "backup", "0 1 * * *", ["pvc-a"]),
                                 job("b", "backup", "10 1 * * *", ["pvc-a"]),
                                 job("c", "backup", "0 3 * * *", ["pvc-b"]),
                                 job("d", "filesystem-trim", "0 3 * * *", ["pvc-b"])], NOW)
        self.assertEqual(["clash", "close"], [row["severity"] for row in found])

    def test_a_job_with_an_unreadable_cron_is_left_out(self):
        self.assertEqual([], CLASHES.clashes([job("a", "backup", "garbage", ["pvc-a"]),
                                              job("b", "backup", "0 2 * * *", ["pvc-a"]),
                                              {"name": "c"}], NOW))


class OverviewTests(unittest.TestCase):
    def test_the_longhorn_overview_carries_the_clashes(self):
        import homestead_longhorn as LH
        import homestead_longhorn_target as BACKUP_TARGET
        import homestead_longhorn_jobs as JOBS
        import homestead_longhorn_volumes as VOLUMES
        jobs = [{"name": "nightly-backup", "task": "backup", "cron": "0 2 * * *", "volumes": ["pvc-a"], "groups": []},
                {"name": "weekly-trim", "task": "filesystem-trim", "cron": "0 2 * * 6", "volumes": ["pvc-a"], "groups": []}]
        with mock.patch.object(JOBS, "list_jobs", return_value=jobs), \
                mock.patch.object(VOLUMES, "_volumes", return_value=[{"name": "pvc-a", "pvc": "data", "groups": []}]), \
                mock.patch.object(VOLUMES, "groups", return_value=[]), \
                mock.patch.object(BACKUP_TARGET, "backup_target", return_value={"configured": True}), \
                mock.patch.object(CLASHES.time, "time", return_value=NOW):
            overview = LH.overview()
        self.assertEqual(["clash"], [row["severity"] for row in overview["clashes"]])


if __name__ == "__main__":
    unittest.main()
