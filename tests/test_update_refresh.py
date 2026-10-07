import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_updates as UPDATES


class RefreshOneTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(UPDATES._LATEST)

    def tearDown(self):
        UPDATES._LATEST.clear()
        UPDATES._LATEST.update(self.saved)

    def test_one_app_is_asked_again_and_the_rest_of_the_report_stays(self):
        UPDATES._LATEST.update(report=UPDATES._summary([
            {"ns": "lab", "name": "sonarr", "available": True, "homestead": "", "images": [{"available": True}]},
            {"ns": "lab", "name": "radarr", "available": True, "homestead": "", "images": [{"available": True}]}],
            checked_at="2026-10-07T02:00:00Z", channel="prod"), number=3, finished=1.0)
        fresh = {"ns": "lab", "name": "sonarr", "available": False, "images": [{"available": False}]}
        with mock.patch.object(UPDATES, "kget", side_effect=lambda path: {"items": []} if path.endswith("/pods") else {"metadata": {}}), \
                mock.patch.object(UPDATES, "_check_deployment", return_value=dict(fresh)) as check, \
                mock.patch.object(UPDATES, "PART", lambda ns, name: ""):
            UPDATES.refresh_one("lab", "sonarr")
        report = UPDATES._LATEST["report"]
        rows = {w["name"]: w for w in report["workloads"]}
        self.assertFalse(rows["sonarr"]["available"], "the updated app no longer shows an update waiting")
        self.assertTrue(rows["radarr"]["available"], "nothing else is asked again or lost")
        self.assertEqual(1, report["updates"])
        self.assertEqual("2026-10-07T02:00:00Z", report["checked_at"])
        self.assertIs(False, check.call_args.kwargs["persist"])

    def test_nothing_to_refresh_without_a_report(self):
        UPDATES._LATEST.update(report=None)
        with mock.patch.object(UPDATES, "_check_deployment") as check:
            self.assertIsNone(UPDATES.refresh_one("lab", "sonarr"))
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
