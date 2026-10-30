"""Readers of what the leader's page snapshot already reads (#978, #980): a
loop or route that asks for a page's key with a shorter cache time than the
cycle that refreshes it is served the cycle's part, not a read of its own; the
alerts loop reads the Harvester version alone, not a whole Cluster inventory."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cache as CACHE
import homestead_restarts as RESTARTS
import homestead_routes as ROUTER
from test_page_snapshot import Cluster, clean_cache, clean_snapshot

# The keys the cycle (or a page) keeps PAGE_SECONDS, and the readers that ask
# for them with less (homestead_overview_server, homestead_metrics, ...).
SNAPSHOT_KEYS = ("nodes", "wl", "vol", "stor", "ov", "lhcap")


def setUpModule():
    patch = mock.patch.object(RESTARTS, "observe_later", lambda pods: True)
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class Clock:
    def __init__(self, test, start=1_800_000_000.0):
        self.now = start
        test.enterContext(mock.patch.object(CACHE.time, "time", lambda: self.now))


COMPUTED = []


def boom():
    """A read that must not happen. Recorded, since a failing read is
    answered from the last kept value."""
    COMPUTED.append(1)
    raise AssertionError("computed: the cycle's part should have been served")


class ShortReadersTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.clock = Clock(self)
        self.cluster = Cluster(self)
        self.PAGES = clean_snapshot(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES
        COMPUTED.clear()
        self.addCleanup(lambda: self.assertEqual([], COMPUTED, "nothing computed"))

    def cycle(self):
        return self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())

    def test_a_reader_asking_for_less_than_the_cycle_is_served_its_part(self):
        self.cycle()
        self.clock.now += 12      # past the 5-10 s these readers ask for
        for key in SNAPSHOT_KEYS:
            self.assertIs(self.PAGES.read(key)[1], CACHE.cached(key, 5, boom), key)

    def test_a_part_late_for_its_next_cycle_is_served_as_the_page_serves_it(self):
        self.cycle()
        self.clock.now += 25      # the next cycle is late: past PAGE_SECONDS, still fresh
        self.assertIsNotNone(self.PAGES.read("wl"))
        self.assertIs(self.PAGES.read("wl")[1], CACHE.cached("wl", 5, boom))
        self.assertIs(ROUTER.page("wl", boom), CACHE.cached("wl", 5, boom))

    def test_with_no_part_a_page_key_is_kept_as_long_as_its_page_keeps_it(self):
        # A standby (no cycle), or vms, which is no part: the VMs page keeps
        # it PAGE_SECONDS, so a 5 s reader shares the page's read.
        calls = []
        read = lambda: calls.append(1) or ["vm"]   # noqa: E731
        for key, page_keeps in (("vms", ROUTER.PAGE_SECONDS), ("wl", ROUTER.PAGE_SECONDS), ("lhcap", 15)):
            calls.clear()
            CACHE.cached(key, 5, read)
            self.clock.now += page_keeps - 1
            CACHE.cached(key, 5, read)
            self.assertEqual(1, len(calls), key)
            self.clock.now += 2
            CACHE.cached(key, 5, read)
            self.assertEqual(2, len(calls), f"{key}: no older than its page")

    def test_every_snapshot_part_is_kept_at_least_as_long_as_its_page(self):
        # A part added to the snapshot without its floor would leave its
        # short readers reading the cluster on every turn again.
        for key in self.PAGES.PARTS:
            self.assertIn(key, CACHE._at_least, key)
            self.assertGreaterEqual(CACHE._at_least[key], 15, key)

    def test_a_key_no_page_keeps_keeps_the_time_its_reader_asked_for(self):
        calls = []
        CACHE.cached("network", 5, lambda: calls.append(1))
        self.clock.now += 6
        CACHE.cached("network", 5, lambda: calls.append(1))
        self.assertEqual(2, len(calls))

    def test_a_write_still_makes_the_next_short_read_fresh(self):
        self.cycle()
        ROUTER.forget("wl")
        computed = []
        CACHE.cached("wl", 5, lambda: computed.append(1) or [])
        self.assertEqual([1], computed)

    def test_two_alert_ticks_twenty_seconds_apart_list_no_pods_or_events(self):
        alerts = []

        def tick():
            before = len(self.cluster.calls)
            self.OV._alert_sources()
            alerts.extend(p for m, p in self.cluster.calls[before:] if m == "GET")
        self.cycle()
        self.clock.now += 7
        tick()
        self.clock.now += 13
        self.cycle()
        self.clock.now += 7
        tick()
        self.assertEqual(0, alerts.count("/api/v1/pods"), "the cycle's pods serve every alert source")
        self.assertEqual(0, alerts.count("/api/v1/events"), "the Harvester version needs no events")

    def test_the_upgrade_alert_reads_the_harvester_version_without_an_inventory(self):
        import homestead_cluster as CLUSTER
        self.cluster.kube.add({"apiVersion": "harvesterhci.io/v1beta1", "kind": "Setting",
                               "metadata": {"name": "server-version"}, "value": "v1.4.1"})
        seen = []
        with mock.patch.object(CLUSTER, "inventory", boom), \
                mock.patch.object(self.OV.UPGRADES, "report", lambda current: seen.append(current) or {}):
            self.OV._alert_sources()
        self.assertEqual(["1.4.1"], seen)

    def test_the_harvester_version_is_the_one_the_cluster_page_shows(self):
        import homestead_cluster as CLUSTER
        self.assertEqual(CLUSTER.inventory()["versions"]["harvester"], CLUSTER.harvester_version())
        self.cluster.kube.add({"apiVersion": "harvesterhci.io/v1beta1", "kind": "Setting",
                               "metadata": {"name": "server-version"}, "value": "v1.4.1"})
        self.assertEqual(CLUSTER.inventory()["versions"]["harvester"], CLUSTER.harvester_version())
        self.assertEqual("1.4.1", CLUSTER.harvester_version())


if __name__ == "__main__":
    unittest.main()
