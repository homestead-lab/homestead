import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_housekeeping as HK

NOW = 1_800_000_000


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = Path(self.dir.name)
        HK.bind(self.dir.name)
        (self.d / "icons").mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def put(self, name, text="x", age=0):
        path = self.d / name
        path.write_text(text)
        os.utime(path, (NOW - age, NOW - age))
        return path


class ReportTests(Base):
    def test_each_store_with_its_size_and_how_long_it_keeps(self):
        self.put("uptime.json", "u" * 3000)
        self.put("change-history.json", "c" * 1000)
        self.put("icons/a.png", "p" * 500)
        self.put("setup.json", "s" * 10)
        rep = HK.report()
        stores = {s["label"]: s for s in rep["stores"]}
        self.assertEqual(3000, stores["Monitoring history"]["size"])
        self.assertIn("30 days", stores["Monitoring history"]["kept"])
        self.assertEqual(1, stores["Logos"]["files"])
        self.assertEqual(10, stores["Settings and other state"]["size"], "anything unclaimed is still counted")
        self.assertEqual("Monitoring history", rep["stores"][0]["label"], "largest first")
        self.assertEqual(4510, rep["total"])


class TidyTests(Base):
    def test_logs_keep_a_year_and_at_most_so_many_lines(self):
        old, recent = NOW - 400 * 86400, NOW - 10 * 86400
        self.put("image-update-history.jsonl", "".join(json.dumps({"at": iso(t), "n": i}) + "\n"
                                                        for i, t in enumerate([old, old, recent, recent])))
        self.put("console-audit.jsonl", "".join(json.dumps({"at": iso(recent), "n": i}) + "\n" for i in range(HK.LOG_LINES + 20)))
        result = HK.tidy([], NOW)
        updates = (self.d / "image-update-history.jsonl").read_text().splitlines()
        self.assertEqual([2, 3], [json.loads(l)["n"] for l in updates])
        audit = (self.d / "console-audit.jsonl").read_text().splitlines()
        self.assertEqual(HK.LOG_LINES, len(audit))
        self.assertEqual(20, json.loads(audit[0])["n"], "the newest are kept")
        self.assertGreater(result["freed"], 0)
        self.assertIn("trimmed image-update-history.jsonl", result["notes"])

    def test_logos_in_use_stay_and_unused_ones_go_after_a_month(self):
        self.put("icons/used.png", age=90 * 86400)
        self.put("icons/unused-old.png", age=40 * 86400)
        self.put("icons/unused-new.png", age=2 * 86400)
        HK.tidy(["/api/icons/used.png"], NOW)
        self.assertEqual({"used.png", "unused-new.png"}, {p.name for p in (self.d / "icons").iterdir()})

    def test_only_stale_leftovers_of_interrupted_writes_go(self):
        self.put("os-rollout.json.1.125057651448632.tmp", age=2 * 3600)
        self.put("alerts.json.7.99.tmp", age=60)                     # a write in progress
        self.put("icons/.icon-abc", age=2 * 3600)
        self.put("os-rollout.json", age=2 * 3600)
        self.put("notes.tmpl", age=2 * 3600)
        result = HK.tidy([], NOW)
        left = {p.name for p in self.d.iterdir()} | {p.name for p in (self.d / "icons").iterdir()}
        self.assertNotIn("os-rollout.json.1.125057651448632.tmp", left)
        self.assertNotIn(".icon-abc", left)
        self.assertIn("alerts.json.7.99.tmp", left)
        self.assertIn("os-rollout.json", left)
        self.assertIn("notes.tmpl", left)
        self.assertTrue(any("interrupted write" in n for n in result["notes"]))

    def test_under_pressure_logs_keep_half_and_unused_logos_go_at_once(self):
        self.put("icons/unused-new.png", age=86400)
        self.put("image-update-history.jsonl", "".join(json.dumps({"at": iso(NOW - d * 86400)}) + "\n" for d in (300, 100)))
        with mock.patch.object(HK, "volume", return_value={"size": 100, "used": 85, "free": 15, "pct": 85.0}):
            result = HK.tidy([], NOW)
        self.assertTrue(result["pressure"])
        self.assertEqual([], list((self.d / "icons").iterdir()))
        self.assertEqual(1, len((self.d / "image-update-history.jsonl").read_text().splitlines()), "300 days is past half a year")

    def test_nothing_to_do_writes_nothing(self):
        self.put("image-update-history.jsonl", json.dumps({"at": iso(NOW)}) + "\n")
        before = (self.d / "image-update-history.jsonl").stat().st_mtime
        result = HK.tidy([], NOW)
        self.assertEqual(([], 0), (result["notes"], result["freed"]))
        self.assertEqual(before, (self.d / "image-update-history.jsonl").stat().st_mtime)


class AlertTests(Base):
    def test_an_alert_names_the_biggest_stores(self):
        rep = {"volume": {"pct": 86.0}, "stores": [{"label": "Long-term stats", "size": 900 * 1024**2},
                                                    {"label": "Logos", "size": 50 * 1024**2}]}
        fact = HK.alert_facts(rep)[0]
        self.assertEqual(("degraded", "Homestead's data volume is 86% full"), (fact["severity"], fact["title"]))
        self.assertIn("Long-term stats (900 MB)", fact["body"])
        self.assertEqual("critical", HK.alert_facts({"volume": {"pct": 96.0}, "stores": []})[0]["severity"])
        self.assertEqual([], HK.alert_facts({"volume": {"pct": 50.0}, "stores": []}))


class ServerTests(Base):
    def test_unknown_references_keep_every_logo(self):
        import server
        self.put("icons/a.png", age=90 * 86400)
        with mock.patch.object(server, "referenced_icons", return_value=None), mock.patch.object(server, "HOUSEKEEPING", HK):
            server.housekeeping_tidy()
        self.assertTrue((self.d / "icons" / "a.png").exists(), "not knowing what is used, nothing is removed")


if __name__ == "__main__":
    unittest.main()
