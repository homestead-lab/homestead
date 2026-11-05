import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_forecast as F
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

GB = 1024**3
NOW = 1_800_000_000          # a fixed "now", in October 2027


def days_of(values, end=NOW):
    """{day: used} for consecutive days ending at end."""
    return {F._day(end - (len(values) - 1 - i) * 86400): v for i, v in enumerate(values)}


class FitTests(unittest.TestCase):
    def test_steady_growth_gives_the_day_it_is_full(self):
        # 50 GB, then 2 GB a day, on a 100 GB volume: 50 + 2*9 = 68 now, 16 days left.
        f = F.fit(days_of([(50 + 2 * i) * GB for i in range(10)]), 100 * GB, NOW)
        self.assertAlmostEqual(16.0, f["days_left"], delta=0.2)
        self.assertAlmostEqual(2 * GB, f["per_day"], delta=GB * 0.01)

    def test_nothing_is_said_without_enough_days(self):
        f = F.fit(days_of([(50 + i) * GB for i in range(5)]), 100 * GB, NOW)
        self.assertIsNone(f["days_left"])
        self.assertIn("7 days", f["why"])

    def test_flat_or_shrinking_is_not_filling(self):
        for values in ([60 * GB] * 10, [(60 - i) * GB for i in range(10)]):
            with self.subTest(values=values[:2]):
                self.assertEqual("not growing", F.fit(days_of(values), 100 * GB, NOW)["why"])

    def test_wild_swings_are_too_irregular(self):
        values = [GB * v for v in (10, 80, 15, 70, 20, 90, 12, 85, 30, 60)]
        self.assertEqual("too irregular to forecast", F.fit(days_of(values), 100 * GB, NOW)["why"])

    def test_only_the_last_thirty_days_count(self):
        old = days_of([GB * 1] * 30, end=NOW - 40 * 86400)
        recent = days_of([(50 + 2 * i) * GB for i in range(10)])
        self.assertAlmostEqual(16.0, F.fit({**old, **recent}, 100 * GB, NOW)["days_left"], delta=0.2)

    def test_already_full_is_zero_days(self):
        f = F.fit(days_of([(90 + 2 * i) * GB for i in range(10)]), 100 * GB, NOW)
        self.assertEqual(0.0, f["days_left"])


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        keep_bound(self, F)
        F.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def vols(self, used):
        return [{"name": "pvc-1", "pvc_name": "frigate-media", "namespace": "lab",
                 "filesystem": {"used_bytes": used, "capacity_bytes": 100 * GB}},
                {"name": "pvc-2", "pvc_name": "idle"}]          # no filesystem reading: not sampled

    def cap(self):
        return {"nodes": [{"name": "node1", "disks": [{"id": "disk-a", "path": "/var/lib/longhorn", "used_gb": 300, "free_gb": 700}]},
                          {"name": "node2", "disks": [{"id": "disk-b", "path": "/mnt/ssd", "used_gb": 100, "free_gb": 900}]}]}

    def test_volumes_disks_and_the_pool(self):
        series = F.observe(self.vols(40 * GB), self.cap(), NOW)
        self.assertEqual({"volume:pvc-1", "disk:node1:disk-a", "disk:node2:disk-b", "pool"}, set(series))
        self.assertEqual(400 * GB, series["pool"]["days"][F._day(NOW)])
        self.assertEqual(2000 * GB, series["pool"]["capacity"])
        self.assertEqual("frigate-media", series["volume:pvc-1"]["label"])

    def test_a_quiet_hour_writes_nothing_and_a_new_day_does(self):
        with mock.patch.object(F.SHARED, "write_json", wraps=F.SHARED.write_json) as write:
            F.observe(self.vols(40 * GB), self.cap(), NOW)
            F.observe(self.vols(40 * GB + 1024), self.cap(), NOW + 3600)
            self.assertEqual(1, write.call_count, "a few KB more is not worth a write")
            F.observe(self.vols(45 * GB), self.cap(), NOW + 7200)
            self.assertEqual(2, write.call_count, "5% more is")
            F.observe(self.vols(45 * GB), self.cap(), NOW + 86400)
            self.assertEqual(3, write.call_count, "a new day gets its own sample")

    def test_old_days_and_gone_volumes_are_forgotten(self):
        F.observe(self.vols(40 * GB), self.cap(), NOW)
        series = F.observe([], self.cap(), NOW + (F.KEEP_DAYS + 1) * 86400)
        self.assertNotIn("volume:pvc-1", series)

    def test_report_and_alerts(self):
        for i in range(10):
            F.observe(self.vols((50 + 2 * i) * GB), {}, NOW - (9 - i) * 86400)
        rows = F.report(NOW)
        self.assertEqual("volume:pvc-1", rows[0]["key"])
        self.assertAlmostEqual(16, rows[0]["days_left"], delta=0.3)
        self.assertEqual([], F.alert_facts(rows), "16 days is beyond the 14-day warning")
        rows[0]["days_left"] = 10
        fact = F.alert_facts(rows)[0]
        self.assertEqual(("forecast:volume:pvc-1", "degraded"), (fact["key"], fact["severity"]))
        self.assertIn("Volume frigate-media fills up in about 10 days", fact["title"])
        rows[0]["days_left"] = 2
        self.assertEqual("critical", F.alert_facts(rows)[0]["severity"])



class ReportOnceTests(unittest.TestCase):
    """The page and the alerts loop ask every 20 s; the file changes hourly."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        F.bind(self.dir.name)
        for i in range(10):
            F.observe([{"name": "pvc-1", "pvc_name": "media", "filesystem": {"used_bytes": (50 + 2 * i) * GB, "capacity_bytes": 100 * GB}}],
                      {}, NOW - (9 - i) * 86400)

    def test_repeated_reports_read_and_fit_once(self):
        with mock.patch.object(F.SHARED, "read_json", wraps=F.SHARED.read_json) as read, \
                mock.patch.object(F, "fit", wraps=F.fit) as fit:
            first = [F.report(NOW + n * 20) for n in range(6)]
        self.assertEqual(1, read.call_count)
        self.assertEqual(1, fit.call_count)
        self.assertEqual(first[0], first[5])

    def test_answers_match_an_uncached_report(self):
        cached = F.report(NOW)
        F._kept[0] = None
        self.assertEqual(cached, F.report(NOW))

    def test_full_on_follows_the_clock_within_a_day(self):
        early = F.report(NOW)[0]
        late = F.report(NOW + 20 * 3600)[0]
        self.assertEqual(F._day(NOW + early["days_left"] * 86400), early["full_on"])
        self.assertEqual(F._day(NOW + 20 * 3600 + late["days_left"] * 86400), late["full_on"])

    def test_a_caller_changing_its_rows_does_not_change_the_next_answer(self):
        F.report(NOW)[0]["days_left"] = 1
        self.assertAlmostEqual(16, F.report(NOW)[0]["days_left"], delta=0.3)

    def test_a_new_sample_or_a_new_day_is_fitted_again(self):
        F.report(NOW)
        F.observe([{"name": "pvc-1", "pvc_name": "media", "filesystem": {"used_bytes": 90 * GB, "capacity_bytes": 100 * GB}}], {}, NOW)
        self.assertEqual(90 * GB, F.report(NOW)[0]["used"])
        with mock.patch.object(F, "fit", wraps=F.fit) as fit:
            F.report(NOW + 86400)
        self.assertEqual(1, fit.call_count, "the fitted window moves with the day")

    def test_another_copy_writing_the_file_is_noticed(self):
        """A follower never calls observe(): the leader replaces the file on the shared volume."""
        F.report(NOW)
        path = F._path()
        data = F.SHARED.read_json(path, {})
        data["series"]["volume:pvc-1"]["days"] = {F._day(NOW): GB}
        with open(path + ".new", "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(path + ".new", path)
        self.assertEqual(GB, F.report(NOW)[0]["used"])


class LoopTests(unittest.TestCase):
    """The hourly look runs from here, so server.py only starts it."""

    def run_once(self, read, leader=True):
        beats = []
        saved = dict(F._bound)
        self.addCleanup(F._bound.update, saved)
        F.bind(F.DATA_DIR, read, lambda: leader, lambda *args, **kwargs: beats.append(args))

        class Stop(Exception):
            pass
        with mock.patch.object(F, "observe") as observe, self.assertRaises(Stop):
            F.run(sleep=mock.Mock(side_effect=Stop))
        return observe, beats

    def test_the_leader_records_todays_figures_and_beats(self):
        observe, beats = self.run_once(lambda: (["volumes"], {"pool": 1}))
        observe.assert_called_once_with(["volumes"], {"pool": 1})
        self.assertEqual([("forecast", 3600)], beats)

    def test_a_failed_read_is_beaten_as_an_error_and_the_loop_goes_on(self):
        def read():
            raise RuntimeError("longhorn unreachable")
        observe, beats = self.run_once(read)
        observe.assert_not_called()
        self.assertEqual("forecast", beats[0][0])
        self.assertIn("unreachable", str(beats[0][2]))

    def test_another_replica_leaves_it_alone(self):
        observe, beats = self.run_once(lambda: ([], {}), leader=False)
        observe.assert_not_called()
        self.assertEqual([], beats)

if __name__ == "__main__":
    unittest.main()
