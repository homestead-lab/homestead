import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_update_checks as UPDATE_CHECKS
import homestead_updates as UPDATES
import homestead_update_checks as CHECKS


class RefreshOneTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(CHECKS._LATEST)

    def tearDown(self):
        CHECKS._LATEST.clear()
        CHECKS._LATEST.update(self.saved)

    def test_one_app_is_asked_again_and_the_rest_of_the_report_stays(self):
        CHECKS._LATEST.update(report=CHECKS._summary([
            {"ns": "lab", "name": "sonarr", "available": True, "homestead": "", "images": [{"available": True}]},
            {"ns": "lab", "name": "radarr", "available": True, "homestead": "", "images": [{"available": True}]}],
            checked_at="2026-10-07T02:00:00Z", channel="prod"), number=3, finished=1.0)
        fresh = {"ns": "lab", "name": "sonarr", "available": False, "images": [{"available": False}]}
        with mock.patch.object(UPDATES, "kget", side_effect=lambda path: {"items": []} if path.endswith("/pods") else {"metadata": {}}), \
                mock.patch.object(CHECKS, "_check_deployment", return_value=dict(fresh)) as check, \
                mock.patch.object(UPDATES, "PART", lambda ns, name: ""):
            CHECKS.refresh_one("lab", "sonarr")
        report = CHECKS._LATEST["report"]
        rows = {w["name"]: w for w in report["workloads"]}
        self.assertFalse(rows["sonarr"]["available"], "the updated app no longer shows an update waiting")
        self.assertTrue(rows["radarr"]["available"], "nothing else is asked again or lost")
        self.assertEqual(1, report["updates"])
        self.assertEqual("2026-10-07T02:00:00Z", report["checked_at"])
        self.assertIs(True, check.call_args.kwargs["persist"], "what now runs is recorded, as a scan does (#380)")

    def test_every_replica_notices_an_update_another_installed(self):
        CHECKS._LATEST.update(report=CHECKS._summary([
            {"ns": "lab", "name": "sonarr", "available": True, "homestead": "",
             "images": [{"container": "sonarr", "deployed": "lscr.io/linuxserver/sonarr:latest", "available": True}]},
            {"ns": "lab", "name": "radarr", "available": True, "homestead": "",
             "images": [{"container": "radarr", "deployed": "lscr.io/linuxserver/radarr:latest", "available": True}]},
            {"ns": "lab", "name": "deleted", "available": True, "homestead": "",
             "images": [{"container": "x", "deployed": "x:1", "available": True}]}],
            checked_at="2026-10-07T02:00:00Z", channel="prod"), number=3, finished=1.0)
        CHECKS._RECONCILED[0] = 0.0
        dep = lambda name, image: {"metadata": {"namespace": "lab", "name": name},
                                    "spec": {"template": {"spec": {"containers": [{"name": name, "image": image}]}}}}
        cluster = {"items": [dep("sonarr", "lscr.io/linuxserver/sonarr:latest@sha256:" + "a" * 64),
                             dep("radarr", "lscr.io/linuxserver/radarr:latest")]}
        asked = []
        with mock.patch.object(UPDATES, "kget", return_value=cluster), \
                mock.patch.object(CHECKS, "refresh_one", side_effect=lambda ns, name: asked.append(name)):
            self.assertEqual([("lab", "sonarr")], CHECKS.reconcile(now=100))
            self.assertEqual([], CHECKS.reconcile(now=105), "at most every ten seconds")
        self.assertEqual(["sonarr"], asked, "only the app whose image changed is asked again")
        names = {w["name"] for w in CHECKS._LATEST["report"]["workloads"]}
        self.assertEqual({"sonarr", "radarr"}, names, "an app that is gone is dropped")

    def test_nothing_to_refresh_without_a_report(self):
        CHECKS._LATEST.update(report=None)
        with mock.patch.object(CHECKS, "_check_deployment") as check:
            self.assertIsNone(CHECKS.refresh_one("lab", "sonarr"))
        check.assert_not_called()


class ReconcileOffRequestTests(unittest.TestCase):
    """A quiet /api/image-updates answers from the last report; the check of
    what the cluster runs now happens on its own thread (#985 §1)."""
    def setUp(self):
        # A reconcile another test's report() started finishes first.
        self.assertTrue(UPDATE_CHECKS._RECONCILING.acquire(timeout=10))
        UPDATE_CHECKS._RECONCILING.release()
        self.saved = dict(UPDATES._LATEST)
        self.addCleanup(lambda: (UPDATES._LATEST.clear(), UPDATES._LATEST.update(self.saved)))
        UPDATES._RECONCILED[0] = 0.0
        self.addCleanup(UPDATES._RECONCILED.__setitem__, 0, 0.0)
        UPDATES._LATEST.update(report=UPDATES._summary([
            {"ns": "lab", "name": "sonarr", "available": True, "homestead": "",
             "images": [{"container": "sonarr", "deployed": "lscr.io/linuxserver/sonarr:latest", "available": True}]}],
            checked_at="2026-10-07T02:00:00Z", channel="prod"), number=3, finished=time.time())

    def test_a_quiet_request_reads_no_pods_and_asks_no_registry_on_its_thread(self):
        caller, calls, release = threading.current_thread(), [], threading.Event()
        dep = {"metadata": {"namespace": "lab", "name": "sonarr"},
               "spec": {"template": {"spec": {"containers": [{"name": "sonarr", "image": "lscr.io/linuxserver/sonarr:2"}]}}}}
        def kget(path):
            calls.append((threading.current_thread() is caller, path))
            release.wait(5)
            return {"items": [dep]} if "deployments" in path else {"items": []}
        refreshed = []
        with mock.patch.object(UPDATES, "kget", side_effect=kget), \
                mock.patch.object(UPDATES, "CHANNEL", lambda: "prod"), \
                mock.patch.object(UPDATES, "refresh_one", side_effect=lambda ns, name: refreshed.append(name)):
            answer = UPDATES.report()
            self.assertEqual("sonarr", answer["workloads"][0]["name"], "the last report, at once")
            release.set()
            UPDATE_CHECKS._RECONCILE_THREAD[0].join(5)
        self.assertEqual([], [path for mine, path in calls if mine], "nothing read on the request's thread")
        self.assertIn("/api/v1/pods", [path for _mine, path in calls])
        self.assertEqual(["sonarr"], refreshed, "the changed app is refreshed in the background")

    def test_requests_while_one_runs_start_no_other(self):
        started, release = [], threading.Event()
        with mock.patch.object(UPDATES, "reconcile", side_effect=lambda: (started.append(1), release.wait(5))), \
                mock.patch.object(UPDATES, "CHANNEL", lambda: "prod"):
            for _ in range(20):
                UPDATES.report()
            release.set()
            UPDATE_CHECKS._RECONCILE_THREAD[0].join(5)
        self.assertEqual(1, len(started), "one reconcile at a time")

    def test_pods_are_matched_within_their_namespace(self):
        dep = lambda ns: {"metadata": {"namespace": ns, "name": "app"},
                          "spec": {"selector": {"matchLabels": {"app": "app"}},
                                   "template": {"spec": {"containers": [{"name": "app", "image": "x:1"}]}}}}
        UPDATES._LATEST["report"] = UPDATES._summary(
            [{"ns": ns, "name": "app", "available": False, "homestead": "", "images": [
                {"container": "app", "deployed": "x:1", "current_digest": "sha256:" + "a" * 64}]}
             for ns in ("one", "two")], checked_at="2026-10-07T02:00:00Z", channel="prod")
        pod = lambda ns, digest: {"metadata": {"namespace": ns, "labels": {"app": "app"}}, "status": {
            "phase": "Running", "containerStatuses": [{"name": "app", "imageID": "x@sha256:" + digest * 64,
                                                       "state": {"running": {}}}]}}
        listed = {"/apis/apps/v1/deployments": {"items": [dep("one"), dep("two")]},
                  "/api/v1/pods": {"items": [pod("one", "a"), pod("two", "b")]}}
        with mock.patch.object(UPDATES, "kget", side_effect=lambda path: listed[path]), \
                mock.patch.object(UPDATES, "refresh_one"):
            self.assertEqual([("two", "app")], UPDATES.reconcile(now=100),
                             "only the app whose own pod runs another build")


if __name__ == "__main__":
    unittest.main()
