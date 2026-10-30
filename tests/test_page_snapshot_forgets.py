"""A write on one replica is told to the others (#928, after #906).

Since #906 every replica serves the leader's page parts. A write forgets (or
pops) its page's key on the replica that made it, but the other replicas, the
leader among them, kept serving the part read before it until the leader's
next cycle, up to 20 s later. Each replica now records such a forget in one
shared file; every replica's sampler loop hears it within a turn and forgets
the key too, and the leader reads that part again at once."""
import fcntl
import json
import os
import re
import sys
import threading
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


class UntoldWritesTests(Forgets):
    """Writes that dropped a page's key without telling the other replicas
    (#928 follow-up): a cache clear, and a bust that walked only the keys held
    here, so it told nothing on a replica that never read the page."""

    def test_a_cache_clear_tells_every_part_though_none_was_read_here(self):
        self.assertFalse(set(self.PAGES.PARTS) & set(CACHE._cache))
        CACHE._cache.clear()        # what a config restore does
        self.follow()
        self.assertEqual(set(self.PAGES.PARTS), set(self.told()))

    def test_a_cache_clear_drops_a_read_started_before_it(self):
        # A leader cycle running during a config restore must not keep its
        # part read before the restore.
        epoch = CACHE.epoch("ov")
        CACHE._cache.clear()
        self.assertFalse(CACHE.keep("ov", "read before", epoch, at=time.time()))
        self.assertGreater(CACHE.forgotten_at("ov"), 0)

    def test_a_bust_of_keys_never_read_here_is_told(self):
        self.assertNotIn("wl", CACHE._cache)
        CACHE.drop_where(CACHE._cache, lambda key: key.startswith(("wl", "ov", "helm")))
        self.follow()
        self.assertEqual({"wl", "ov"}, set(self.told()))

    def test_a_bust_drops_the_answers_held_here_too(self):
        CACHE._cache.update({"wl": 1, "wl:lab": 2, "helm": 3})
        CACHE.drop_where(CACHE._cache, lambda key: key.startswith("wl"))
        self.assertEqual({"helm": 3}, dict(CACHE._cache))

    def test_a_bust_of_another_dict_pops_only_its_own_keys(self):
        own = {"wl": 1, "helm": 2}
        CACHE.drop_where(own, lambda key: key.startswith("wl"))
        self.follow()
        self.assertEqual(({"helm": 2}, {}), (own, self.told()))

    def test_each_module_s_bust_is_told_though_the_page_was_never_read_here(self):
        import homestead_hardware as HW
        import homestead_imports as IMP
        import homestead_lifecycle as LC
        import homestead_longhorn as LH
        import homestead_place as PLACE
        for module, keys, told in ((HW, (), {"nodes", "ov", "wl"}), (IMP, ("vol", "stor"), {"vol", "stor"}),
                                   (LC, ("wl", "ov"), {"wl", "ov"}), (LH, ("vol",), {"vol"}),
                                   (PLACE, ("wl", "nodes"), {"wl", "nodes"})):
            with self.subTest(module=module.__name__), mock.patch.object(module, "_cache", CACHE._cache):
                self.PAGES.reset()
                if os.path.exists(self.forgets_path()):
                    os.remove(self.forgets_path())
                module._bust(*keys)
                self.follow()
                self.assertEqual(told, set(self.told()))

    def test_no_write_walks_only_the_keys_held_here(self):
        # The storage, storage-class and settings busts sit inside their
        # writes; a loop over the cache's own keys misses those not held.
        walk = re.compile(r"for \w+ in (?:\w+\()?(?:\w+\.)?_cache\b|in (?:\w+\.)?_cache(?:\.keys\(\))? if")
        found = [f"{path.name}:{number}" for path in sorted((Path(__file__).resolve().parents[1] / "server").glob("*.py"))
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if walk.search(line)]
        self.assertEqual([], found)


class SharedFileTests(Forgets):
    """page-forgets.json on NFS (#928 follow-up): a replica that merges from a
    stale copy of the file can write another's key away, and the lock must
    not hold the sampler loop."""

    def test_a_key_another_replica_merged_away_is_told_again(self):
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        when = self.told()["wl"]
        # Another replica merged from a copy read before that write.
        SHARED.write_json(self.forgets_path(), {"forgets": {"ov": time.time()}})
        self.follow()
        self.follow()
        self.assertEqual(when, self.told().get("wl"), "told again, dated as before")
        self.assertIn("ov", self.told())

    def test_a_key_to_tell_again_wakes_the_loop_but_asks_for_no_cycle(self):
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        self.PAGES._wanted.clear()
        self.PAGES.sleep(0)
        SHARED.write_json(self.forgets_path(), {"forgets": {}})
        self.follow()
        self.assertEqual((True, False), (self.PAGES._wake.is_set(), self.PAGES._wanted.is_set()))

    def test_a_key_another_replica_wrote_back_older_is_told_again(self):
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        when = self.told()["wl"]
        SHARED.write_json(self.forgets_path(), {"forgets": {"wl": when - self.PAGES.CLOCK_SKEW - 5}})
        self.follow()
        self.follow()
        self.assertEqual(when, self.told()["wl"])

    def test_a_key_held_back_by_a_slower_clock_is_not_told_again(self):
        # Another replica, its clock 2 s behind, wrote the key back as its now.
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        when = self.told()["wl"]
        SHARED.write_json(self.forgets_path(), {"forgets": {"wl": when - 2}})
        self.follow()
        self.follow()
        self.assertEqual(when - 2, self.told()["wl"], "no tug of war between the clocks")

    def test_a_write_to_tell_again_that_has_aged_out_is_not_written(self):
        self.PAGES._told["again"]["wl"] = time.time() - self.PAGES.STALE_AFTER - 1
        self.follow()
        self.assertEqual({}, self.told())

    def test_reset_leaves_the_loop_asleep(self):
        self.PAGES._wake.set()
        self.PAGES.reset()
        self.assertFalse(self.PAGES._wake.is_set())

    def test_a_write_older_than_any_part_served_is_not_told_again(self):
        with mock.patch("time.time", return_value=time.time() - self.PAGES.STALE_AFTER - 1):
            CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        SHARED.write_json(self.forgets_path(), {"forgets": {}})
        self.follow()
        self.follow()
        self.assertEqual({}, self.told())

    def test_a_key_told_and_kept_is_not_written_again(self):
        CACHE.forget_where(lambda key: key == "wl")
        self.follow()
        self.follow()
        before = os.stat(self.forgets_path())
        self.follow()
        after = os.stat(self.forgets_path())
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_a_lock_held_elsewhere_does_not_hold_the_loop(self):
        folder = os.path.join(self.tmp.name, ".locks")
        os.makedirs(folder, exist_ok=True)
        holder = open(os.path.join(folder, "page-forgets.lock"), "a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)     # another replica, stuck holding it
        CACHE.forget_where(lambda key: key == "wl")
        turn = threading.Thread(target=self.follow, daemon=True)
        wait = getattr(self.PAGES, "LOCK_WAIT", 2)
        with mock.patch("builtins.print") as said:
            started = time.monotonic()
            turn.start()
            turn.join(wait + 3)
            took = time.monotonic() - started
            if turn.is_alive():
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                turn.join(5)
                self.fail(f"the loop waited on the lock for {took:.1f} s")
        self.assertLess(took, wait + 1)
        self.assertEqual({}, self.told())
        self.assertTrue([c for c in said.call_args_list if "not told" in str(c)])
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        self.follow()
        self.assertEqual({"wl"}, set(self.told()), "told on a later turn")


if __name__ == "__main__":
    unittest.main()
