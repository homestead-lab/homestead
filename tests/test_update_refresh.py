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
        self.assertIs(True, check.call_args.kwargs["persist"], "what now runs is recorded, as a scan does (#380)")

    def test_every_replica_notices_an_update_another_installed(self):
        UPDATES._LATEST.update(report=UPDATES._summary([
            {"ns": "lab", "name": "sonarr", "available": True, "homestead": "",
             "images": [{"container": "sonarr", "deployed": "lscr.io/linuxserver/sonarr:latest", "available": True}]},
            {"ns": "lab", "name": "radarr", "available": True, "homestead": "",
             "images": [{"container": "radarr", "deployed": "lscr.io/linuxserver/radarr:latest", "available": True}]},
            {"ns": "lab", "name": "deleted", "available": True, "homestead": "",
             "images": [{"container": "x", "deployed": "x:1", "available": True}]}],
            checked_at="2026-10-07T02:00:00Z", channel="prod"), number=3, finished=1.0)
        UPDATES._RECONCILED[0] = 0.0
        dep = lambda name, image: {"metadata": {"namespace": "lab", "name": name},
                                    "spec": {"template": {"spec": {"containers": [{"name": name, "image": image}]}}}}
        cluster = {"items": [dep("sonarr", "lscr.io/linuxserver/sonarr:latest@sha256:" + "a" * 64),
                             dep("radarr", "lscr.io/linuxserver/radarr:latest")]}
        asked = []
        with mock.patch.object(UPDATES, "kget", return_value=cluster), \
                mock.patch.object(UPDATES, "refresh_one", side_effect=lambda ns, name: asked.append(name)):
            self.assertEqual([("lab", "sonarr")], UPDATES.reconcile(now=100))
            self.assertEqual([], UPDATES.reconcile(now=105), "at most every ten seconds")
        self.assertEqual(["sonarr"], asked, "only the app whose image changed is asked again")
        names = {w["name"] for w in UPDATES._LATEST["report"]["workloads"]}
        self.assertEqual({"sonarr", "radarr"}, names, "an app that is gone is dropped")

    def test_nothing_to_refresh_without_a_report(self):
        UPDATES._LATEST.update(report=None)
        with mock.patch.object(UPDATES, "_check_deployment") as check:
            self.assertIsNone(UPDATES.refresh_one("lab", "sonarr"))
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
