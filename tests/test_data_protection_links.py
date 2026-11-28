"""Links the server makes into Data protection open the section they mean.

Data protection is split into sections, each a page of its own
(/data-protection/restore-tests, /data-protection/settings). "Open" on a job in
the tray, and "Review" on an alert, go to the page the link names and bring the
card after # into view on it. A link to the bare page with a section's old
fragment (/data-protection#restore-tests) opened Volumes instead, so each link
names its section's page."""
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_alerts as ALERTS
import homestead_restore_test as RT
import homestead_web_paths as PATHS
import homestead_weekly_trim as WT
from rebind import keep_bound

NO_MAINTENANCE = (set(), set(), {"open": {}, "ran_out": {}, "ending": {}})


class DataProtectionLinks(unittest.TestCase):
    def opens(self, href, page, card=""):
        link = urllib.parse.urlsplit(href)
        self.assertTrue(PATHS.is_spa_route(link.path), f"{href} is not a page")
        self.assertEqual((page, card), (link.path, link.fragment))

    def test_a_restore_test_job_opens_restore_tests(self):
        with mock.patch.object(RT.OPS, "start", return_value={"id": "j1"}) as start:
            RT.start_test("lab", "app-a")
        self.opens(start.call_args.args[3], "/data-protection/restore-tests")

    def test_a_weekly_trim_job_opens_housekeeping_in_settings(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        keep_bound(self, WT)
        WT.bind(temp.name)
        with mock.patch.object(WT.OPS, "start", return_value={"id": "j1"}) as start:
            WT.start("weekly")
        self.opens(start.call_args.args[3], "/data-protection/settings", "housekeeping")

    def test_a_failed_trim_alert_opens_housekeeping_in_settings(self):
        with mock.patch.object(ALERTS.HISTORY, "maintenance_view", return_value=NO_MAINTENANCE):
            facts = ALERTS.health_facts({"health_issues": [{"kind": "Trim", "name": "data-a", "severity": "degraded",
                                                            "reason": "x"}]})
        self.opens(facts[0]["href"], "/data-protection/settings", "housekeeping")


if __name__ == "__main__":
    unittest.main()
