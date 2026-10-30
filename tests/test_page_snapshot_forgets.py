"""A write on one replica is told to the others (#928, after #906).

Since #906 every replica serves the leader's page parts. A write forgets (or
pops) its page's key on the replica that made it, but the other replicas, the
leader among them, kept serving the part read before it until the leader's
next cycle, up to 20 s later. Each replica now records such a forget in one
shared file; every replica's sampler loop hears it within a turn and forgets
the key too, and the leader reads that part again at once."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_cache as CACHE
import homestead_shared as SHARED
from test_page_snapshot_shared import FOLLOWER, LEADER, Shared


class Forgets(Shared):
    def setUp(self):
        super().setUp()
        self.PAGES._register()   # as at import; the test base clears the cache

    def forgets_path(self):
        return os.path.join(self.tmp.name, self.PAGES.FORGETS)

    def told(self):
        try:
            with open(self.forgets_path(), encoding="utf-8") as handle:
                return json.load(handle)["forgets"]
        except FileNotFoundError:
            return {}

    def another_replica_forgot(self, **keys):
        """What another replica's tell() leaves in the shared file."""
        forgets = self.told()
        forgets.update(keys)
        SHARED.write_json(self.forgets_path(), {"forgets": forgets})

    def follow(self, now=None):
        return self.PAGES.tick(FOLLOWER, {}, self.builders, now=now)


class TellTests(Forgets):
    def test_a_forget_here_is_told_to_the_others(self):
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        self.assertEqual({"wl": CACHE.forgotten_at("wl")}, self.told())

    def test_a_write_through_the_routes_is_told(self):
        import homestead_routes as ROUTER
        ROUTER.forget("stor")
        self.follow()
        self.assertEqual({"stor"}, set(self.told()))

    def test_a_key_popped_here_is_told_too(self):
        before = time.time()
        CACHE._cache.pop("wl", None)
        self.follow()
        self.assertGreaterEqual(self.told()["wl"], before)
        self.assertLessEqual(self.told()["wl"], time.time())

    def test_a_pop_wakes_the_loop_as_a_forget_does(self):
        self.PAGES.sleep(0)
        CACHE._cache.pop("ov", None)
        self.assertTrue(self.PAGES._wake.is_set())

    def test_only_the_page_parts_are_told(self):
        CACHE.forget_where(lambda key: key == "network")
        CACHE._cache.pop("helm", None)
        self.follow()
        self.assertEqual({}, self.told())

    def test_the_other_replicas_forgets_are_kept(self):
        self.another_replica_forgot(ov=time.time() - 5)
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        self.assertEqual({"ov", "wl"}, set(self.told()))

    def test_a_later_forget_of_the_same_key_moves_it_on(self):
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        first = self.told()["wl"]
        time.sleep(0.01)
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        self.assertGreater(self.told()["wl"], first)

    def test_a_write_that_fails_is_tried_again_and_said_once(self):
        CACHE.forget_where(lambda key: key == "wl")
        with mock.patch.object(SHARED, "write_json", side_effect=OSError("held during a move")), \
                mock.patch("builtins.print") as said:
            self.follow()
            self.follow()
        self.assertEqual(1, len([c for c in said.call_args_list if "not told" in str(c)]))
        self.follow()
        self.assertEqual({"wl": CACHE.forgotten_at("wl")}, self.told())


class HearTests(Forgets):
    def take_in_a_leader_part(self):
        as_of = time.time() - 1
        self.write_parts({"wl": [as_of, [{"name": "web"}]]})
        self.follow()
        self.assertEqual(as_of, self.PAGES.read("wl")[0])
        return as_of

    def test_another_replica_s_write_drops_the_leader_s_part_here(self):
        as_of = self.take_in_a_leader_part()
        written = as_of + 0.5
        self.another_replica_forgot(wl=written)
        self.follow()
        self.assertIsNone(self.PAGES.read("wl"), "computed here again, not the part read before the write")
        self.assertEqual(written, CACHE.forgotten_at("wl"))

    def test_a_leader_part_read_before_it_is_not_taken_in_and_a_later_one_is(self):
        written = time.time() - 1
        self.another_replica_forgot(wl=written)
        self.follow()
        self.write_parts({"wl": [written - 0.5, [{"name": "old"}]]})
        self.follow()
        self.assertIsNone(self.PAGES.read("wl"))
        self.write_parts({"wl": [written + 0.5, [{"name": "new"}]]})
        self.follow()
        self.assertEqual([{"name": "new"}], self.PAGES.read("wl")[1])

    def test_the_leader_s_part_read_after_the_write_is_taken_in_though_heard_later_here(self):
        # The leader may hear the write and read again before this replica
        # hears it: its part, read after the write, still counts here.
        self.take_in_a_leader_part()
        written = time.time() - 0.5
        self.another_replica_forgot(wl=written)
        self.follow()
        self.write_parts({"wl": [written + 0.1, [{"name": "after"}]]})
        self.follow()
        self.assertEqual([{"name": "after"}], self.PAGES.read("wl")[1])

    def test_the_leader_hears_it_and_reads_that_part_again_soon(self):
        self.lead_one_cycle(now=100)
        self.assertIsNotNone(self.PAGES.read("ov"))
        self.another_replica_forgot(ov=time.time())
        self.assertIsNotNone(self.PAGES.tick(LEADER, {}, self.builders, now=100 + self.PAGES.MIN_GAP),
                             "a cycle well before its turn")
        self.assertEqual(2, len(self.runs))
        self.assertEqual(2, self.PAGES.read("ov")[1]["n"])

    def test_what_was_heard_is_not_told_back(self):
        self.another_replica_forgot(wl=time.time() - 1)
        self.follow()
        before = os.stat(self.forgets_path())
        self.follow()
        after = os.stat(self.forgets_path())
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_a_forget_of_its_own_is_not_heard_again(self):
        CACHE.forget_where(lambda key: key == "wl")
        epoch = CACHE.epoch("wl")
        self.follow()
        self.follow()
        self.assertEqual(epoch, CACHE.epoch("wl"))

    def test_a_forget_dated_ahead_of_this_clock_counts_from_now(self):
        self.another_replica_forgot(wl=time.time() + 3600)
        self.follow()
        self.assertLessEqual(CACHE.forgotten_at("wl"), time.time())
        self.assertGreater(CACHE.forgotten_at("wl"), 0)

    def test_a_forget_older_than_any_part_served_changes_nothing(self):
        epoch = CACHE.epoch("wl")
        self.another_replica_forgot(wl=time.time() - self.PAGES.STALE_AFTER - 1)
        self.follow()
        self.assertEqual(epoch, CACHE.epoch("wl"))

    def test_a_broken_file_or_unknown_key_is_ignored(self):
        epoch = CACHE.epoch("wl")
        SHARED.write_json(self.forgets_path(), {"forgets": {"wl": "soon", "network": time.time()}})
        self.follow()
        with open(self.forgets_path(), "w", encoding="utf-8") as handle:
            handle.write("{")
        self.follow()
        self.assertEqual((epoch, 0.0), (CACHE.epoch("wl"), CACHE.forgotten_at("network")))


if __name__ == "__main__":
    unittest.main()
