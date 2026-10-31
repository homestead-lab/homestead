"""The Dashboard says how old its numbers are (#705 step 8): /api/overview
carries as_of, when the numbers it holds were read from the cluster, and
age_s, how long ago that was when it was served, whether it came from the
leader's snapshot, a fresh read or the last read kept after one failed."""
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


class PageDatedTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.enterContext(mock.patch.object(ROUTER, "_cached", [CACHE.cached]))
        self.enterContext(mock.patch.object(ROUTER, "_snapshot", [lambda key: None]))

    def test_a_snapshot_part_is_dated_by_its_cycle(self):
        ROUTER._snapshot[0] = lambda key: (1000.0, {"n": 1}) if key == "ov" else None
        self.assertEqual((1000.0, {"n": 1}), ROUTER.page_dated("ov", lambda: self.fail("computed")))

    def test_a_fresh_read_is_dated_when_it_was_read(self):
        with mock.patch.object(CACHE.time, "time", lambda: 2000.0):
            self.assertEqual((2000.0, {"n": 2}), ROUTER.page_dated("ov", lambda: {"n": 2}))

    def test_a_kept_read_keeps_its_date(self):
        with mock.patch.object(CACHE.time, "time", lambda: 2000.0):
            ROUTER.page_dated("ov", lambda: {"n": 2})
        with mock.patch.object(CACHE.time, "time", lambda: 2010.0):
            self.assertEqual((2000.0, {"n": 2}), ROUTER.page_dated("ov", lambda: self.fail("read again")))

    def test_the_last_read_served_after_a_failure_keeps_its_old_date(self):
        with mock.patch.object(CACHE.time, "time", lambda: 2000.0):
            ROUTER.page_dated("ov", lambda: {"n": 2})

        def broken():
            raise RuntimeError("the cluster did not answer")
        with mock.patch.object(CACHE.time, "time", lambda: 2300.0):
            self.assertEqual((2000.0, {"n": 2}), ROUTER.page_dated("ov", broken))

    def test_a_read_a_write_overtook_is_dated_now_not_by_an_older_kept_read(self):
        with mock.patch.object(CACHE.time, "time", lambda: 2000.0):
            ROUTER.page_dated("ov", lambda: {"n": 1}, seconds=0)

        def overtaken():
            CACHE.forget_where(lambda key: key == "ov")   # a write lands while it reads
            return {"n": 2}
        with mock.patch.object(CACHE.time, "time", lambda: 2100.0):
            self.assertEqual((2100.0, {"n": 2}), ROUTER.page_dated("ov", overtaken, seconds=0))

    def test_page_still_gives_only_the_value(self):
        ROUTER._snapshot[0] = lambda key: (1000.0, {"n": 1})
        self.assertEqual({"n": 1}, ROUTER.page("ov", lambda: None))


class OverviewRouteTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        # The cycle reads pods, and the restart record would look at them on its
        # own thread, writing restarts.json while the test cleans up (the reason
        # test_page_snapshot's setUpModule patches it too).
        self.enterContext(mock.patch.object(RESTARTS, "observe_later", lambda pods: True))
        self.cluster = Cluster(self)
        self.PAGES = clean_snapshot(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES

    def test_the_overview_from_the_snapshot_says_when_its_cycle_read_the_cluster(self):
        self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        as_of, value = self.PAGES.read("ov")
        with mock.patch.object(ROUTER.time, "time", lambda: as_of + 12.4):
            answer = self.cluster.route("ov")
        self.assertEqual(round(as_of, 3), answer["as_of"])
        self.assertEqual(12, answer["age_s"])
        self.assertNotIn("as_of", value, "the kept part is not changed")
        self.assertNotIn("age_s", self.PAGES.read("ov")[1])

    def test_an_overview_read_on_the_spot_is_dated_now(self):
        answer = self.cluster.route("ov")
        self.assertEqual(0, answer["age_s"])
        self.assertNotIn("as_of", CACHE._cache["ov"][1], "the cached overview is not changed")


if __name__ == "__main__":
    unittest.main()
