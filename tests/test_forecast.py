import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_forecast as F

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


if __name__ == "__main__":
    unittest.main()
