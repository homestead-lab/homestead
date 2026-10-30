"""The sidebar's health pill on every page (#940): /api/alerts, which every
page reads every 20 s, carries the cluster's verdict from the overview the
Dashboard reads. From the leader's snapshot part it asks the cluster nothing,
and the alerts are answered even when the verdict cannot be read."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_alerts as ALERTS
import homestead_cache as CACHE
import homestead_cluster as CLUSTER
import homestead_routes as ROUTER
from test_page_snapshot import Cluster, clean_cache, clean_snapshot


class HealthBriefTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.enterContext(mock.patch.object(ROUTER, "_cached", [CACHE.cached]))

    def snapshot(self, value, as_of=1000.0):
        return mock.patch.object(ROUTER, "_snapshot", [lambda key: (as_of, value) if key == "ov" else None])

    def test_the_verdict_counts_critical_and_degraded_issues(self):
        ov = {"health": "critical", "health_state": "critical", "health_summary": "Node node-1: down",
              "health_issues": [{"severity": "critical"}, {"severity": "degraded"}, {"severity": "degraded"}]}
        with self.snapshot(ov), mock.patch.object(ROUTER.time, "time", lambda: 1020.4):
            brief = CLUSTER.health_brief()
        self.assertEqual({"state": "critical", "summary": "Node node-1: down", "critical": 1,
                          "degraded": 2, "as_of": 1000.0, "age_s": 20}, brief)

    def test_an_activity_is_the_state_not_the_plain_health(self):
        ov = {"health": "healthy", "health_state": "updating", "health_summary": "Workload lab/web: 0/1 replicas ready"}
        with self.snapshot(ov):
            self.assertEqual("updating", CLUSTER.health_brief()["state"])

    def test_with_neither_a_part_nor_a_kept_read_there_is_no_verdict(self):
        with mock.patch.object(ROUTER, "_snapshot", [lambda key: None]), \
                mock.patch.object(CLUSTER, "overview", lambda: self.fail("built the overview")):
            self.assertIsNone(CLUSTER.health_brief())

    def test_an_overview_without_health_state_falls_back_to_health(self):
        with self.snapshot({"health": "degraded"}):
            brief = CLUSTER.health_brief()
        self.assertEqual(("degraded", "", 0, 0), (brief["state"], brief["summary"], brief["critical"], brief["degraded"]))


class AlertsHealthRouteTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.cluster = Cluster(self)
        self.PAGES = clean_snapshot(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(ALERTS.bind, ALERTS.DATA_DIR)
        ALERTS.bind(folder.name)

    def alerts(self):
        handler = self.cluster.server.MODULE_ROUTES.table()[("GET", "/api/alerts")].handler
        return handler(SimpleNamespace(user="alice"))

    def test_from_a_fresh_snapshot_part_the_verdict_asks_the_cluster_nothing(self):
        self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        self.cluster.calls.clear()
        ALERTS.HEALTH[0]()
        self.assertEqual([], self.cluster.calls)
        # Nor does the route: the bell's own settings read (kept a while) is
        # made once without the verdict, and then the route asks nothing.
        with mock.patch.object(ALERTS, "HEALTH", [lambda: None]):
            self.alerts()
        self.cluster.calls.clear()
        answer = self.alerts()
        self.assertEqual([], self.cluster.calls)
        ov = self.PAGES.read("ov")[1]
        self.assertEqual(ov["health_state"], answer["health"]["state"])
        self.assertEqual(ov["health_summary"], answer["health"]["summary"])

    def test_the_age_is_the_snapshot_part_s(self):
        self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        as_of = self.PAGES.read("ov")[0]
        with mock.patch.object(ROUTER.time, "time", lambda: as_of + 33.7):
            health = self.alerts()["health"]
        self.assertEqual((round(as_of, 3), 33), (health["as_of"], health["age_s"]))

    def test_with_no_part_and_no_kept_read_the_verdict_is_unknown_and_nothing_is_built(self):
        # The bell is read every 20 s on every page: it never waits on an
        # overview build, however cold the cache.
        with mock.patch.object(ALERTS, "HEALTH", [lambda: None]):
            self.alerts()
        self.cluster.calls.clear()

        builds = []
        with mock.patch.object(CLUSTER, "overview", lambda: builds.append(1) or {}):
            answer = self.alerts()
        self.assertEqual([], builds, "the alerts built the overview")
        self.assertEqual([], self.cluster.calls)
        self.assertIsNone(answer["health"])
        self.assertEqual([], answer["active"])
        self.assertIn("log", answer)

    def test_without_a_fresh_part_the_kept_read_is_served_with_its_date(self):
        with mock.patch.object(CACHE.time, "time", lambda: 5000.0):
            ROUTER.cached("ov", 20, lambda: {"health": "degraded", "health_state": "degraded",
                                             "health_summary": "Volume lab/data: rebuilding",
                                             "health_issues": [{"severity": "degraded"}]})
        with mock.patch.object(ALERTS, "HEALTH", [lambda: None]):
            self.alerts()
        self.cluster.calls.clear()
        with mock.patch.object(ROUTER.time, "time", lambda: 5045.0):
            health = self.alerts()["health"]
        self.assertEqual([], self.cluster.calls)
        self.assertEqual(("degraded", 1, 5000.0, 45),
                         (health["state"], health["degraded"], health["as_of"], health["age_s"]))

    def test_a_failing_verdict_hook_costs_the_alerts_nothing(self):
        def broken():
            raise ValueError("unexpected")
        with mock.patch.object(ALERTS, "HEALTH", [broken]):
            answer = self.alerts()
        self.assertIsNone(answer["health"])
        self.assertIn("active", answer)


if __name__ == "__main__":
    unittest.main()
