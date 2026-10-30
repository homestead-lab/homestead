"""The leader's page snapshot shared with the other replicas (#906 item 2).

The leader writes its fresh parts to one file on the shared data volume after
each cycle, as it shares its other state (homestead_leader_state). Each other
replica's sampler loop takes in a newer file, so their page reads serve the
leader's parts under the same rules as the leader's own: STALE_AFTER, a write
that forgets a key after the part was read, a key popped from the cache, and
a newer read of the replica's own all win over it."""
import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_cache as CACHE
import homestead_shared as SHARED
import test_page_snapshot as T

LEADER, FOLLOWER = (lambda: True), (lambda: False)


class Shared(unittest.TestCase):
    def setUp(self):
        T.clean_cache(self)
        self.PAGES = T.clean_snapshot(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = SHARED.DIR
        SHARED.bind(self.tmp.name)
        self.addCleanup(SHARED.bind, saved)
        self.runs = []
        self.builders = [("ov", lambda got: {"n": len(self.runs) + 1, "seen": self.runs.append(1)}),
                         ("wl", lambda got: [{"name": "web"}])]
        self.someone_follows()

    def someone_follows(self, ago=0):
        """Another replica has asked for the parts (it does on every look)."""
        wanted = os.path.join(self.tmp.name, self.PAGES.WANTED)
        open(wanted, "a").close()
        when = time.time() - ago
        os.utime(wanted, (when, when))

    def path(self):
        return os.path.join(self.tmp.name, self.PAGES.FILE)

    def written(self):
        with open(self.path(), encoding="utf-8") as handle:
            return json.load(handle)

    def lead_one_cycle(self, now=100):
        return self.PAGES.tick(LEADER, {}, self.builders, now=now)

    def become_another_replica(self):
        """What a second process sees: no parts, no cache, the same volume."""
        self.PAGES.reset()
        for store in (CACHE._cache, CACHE._running, CACHE._epoch, CACHE._forgot):
            store.clear()

    def write_parts(self, parts, at=None):
        SHARED.write_json(self.path(), {"written": at or time.time(), "parts": parts})


class LeaderWritesTests(Shared):
    def test_the_leader_writes_its_fresh_parts_after_a_cycle(self):
        self.lead_one_cycle()
        parts = self.written()["parts"]
        self.assertEqual({"ov", "wl"}, set(parts))
        as_of, value = parts["wl"]
        self.assertEqual([{"name": "web"}], value)
        self.assertEqual(self.PAGES.read("wl")[0], as_of)

    def test_a_part_a_write_overtook_is_not_written(self):
        self.builders[1] = ("wl", lambda got: CACHE.forget_where(lambda k: k == "wl") or [{"name": "old"}])
        self.lead_one_cycle()
        self.assertNotIn("wl", self.written()["parts"])

    def test_no_cycle_writes_nothing(self):
        self.lead_one_cycle(100)
        os.remove(self.path())
        self.assertIsNone(self.lead_one_cycle(101))
        self.assertFalse(os.path.exists(self.path()))

    def test_a_write_that_fails_is_said_once_and_never_stops_the_loop(self):
        with mock.patch.object(SHARED, "write_json", side_effect=OSError("held during a move")), \
                mock.patch("builtins.print") as said:
            self.assertIsNotNone(self.lead_one_cycle(100))
            self.assertIsNotNone(self.lead_one_cycle(200))
        self.assertEqual(1, len([c for c in said.call_args_list if "not shared" in str(c)]))


class WhoIsWrittenForTests(Shared):
    """The file is written only while another replica reads it, and not again
    for parts that did not change until they would go stale (#914 review:
    page-snapshot.json written every cycle for nobody with replicas: 1)."""
    def test_with_no_other_replica_nothing_is_written(self):
        os.remove(os.path.join(self.tmp.name, self.PAGES.WANTED))
        self.lead_one_cycle()
        self.assertFalse(os.path.exists(self.path()))

    def asked_at(self, mtime_ns, ino_from_new_file=False):
        """A follower's touch, as its own clock (or its NFS server's) dates it."""
        wanted = os.path.join(self.tmp.name, self.PAGES.WANTED)
        if ino_from_new_file:
            os.replace(wanted, wanted + ".old")    # still held: the new file has a new inode
        open(wanted, "a").close()
        os.utime(wanted, ns=(mtime_ns, mtime_ns))

    def lead_and_say_whether_written(self, now):
        if os.path.exists(self.path()):
            os.remove(self.path())
        self.lead_one_cycle(now)
        return os.path.exists(self.path())

    def test_an_ask_that_has_not_changed_for_ask_fresh_writes_nothing(self):
        self.assertTrue(self.lead_and_say_whether_written(100), "an ask seen first counts once")
        self.assertFalse(self.lead_and_say_whether_written(100 + self.PAGES.ASK_FRESH))

    def test_a_follower_whose_clock_is_behind_still_gets_the_parts(self):
        """#914 review: the leader's own clock never meets the follower's mtime."""
        behind = int((time.time() - 3600) * 1e9)
        self.asked_at(behind)
        self.assertTrue(self.lead_and_say_whether_written(100))
        self.asked_at(behind + 30 * 10**9)          # its next touch, ASK_EVERY later
        self.assertTrue(self.lead_and_say_whether_written(100 + self.PAGES.cadence()))
        self.assertTrue(self.lead_and_say_whether_written(100 + self.PAGES.cadence() + self.PAGES.ASK_FRESH - 1))
        self.assertFalse(self.lead_and_say_whether_written(100 + self.PAGES.cadence() + self.PAGES.ASK_FRESH))

    def test_a_follower_whose_clock_is_ahead_is_not_written_for_after_it_stops_asking(self):
        self.asked_at(int((time.time() + 3600) * 1e9))
        self.assertTrue(self.lead_and_say_whether_written(100))
        self.assertFalse(self.lead_and_say_whether_written(100 + self.PAGES.ASK_FRESH + 1))

    def test_a_new_wanted_file_with_the_same_time_is_a_new_ask(self):
        self.asked_at(5 * 10**18)
        self.assertTrue(self.lead_and_say_whether_written(100))
        self.asked_at(5 * 10**18, ino_from_new_file=True)
        self.assertTrue(self.lead_and_say_whether_written(100 + self.PAGES.ASK_FRESH + 1))

    def test_asking_goes_through_the_self_data_write_fence_and_says_once_when_it_fails(self):
        wanted = os.path.join(self.tmp.name, self.PAGES.WANTED)
        os.remove(wanted)
        scoped = []

        @contextlib.contextmanager
        def scope(path):
            scoped.append(path)
            yield

        def held(path):
            raise RuntimeError("Homestead's own data is moving")

        every = self.PAGES.ASK_EVERY
        with mock.patch.object(SHARED, "WRITE_SCOPE", scope), mock.patch("builtins.print") as said:
            with mock.patch.object(SHARED, "WRITE_GUARD", held):
                self.PAGES.tick(FOLLOWER, {}, self.builders, now=100)
                self.PAGES.tick(FOLLOWER, {}, self.builders, now=100 + every)
            self.assertFalse(os.path.exists(wanted), "nothing written past the fence")
            self.assertEqual([wanted, wanted], scoped)
            self.assertEqual(1, len([c for c in said.call_args_list if "not asked" in str(c)]))
            self.PAGES.tick(FOLLOWER, {}, self.builders, now=100 + 2 * every)
            self.assertTrue(os.path.exists(wanted))
            with mock.patch.object(SHARED, "WRITE_GUARD", held):
                self.PAGES.tick(FOLLOWER, {}, self.builders, now=100 + 3 * every)
            self.assertEqual(2, len([c for c in said.call_args_list if "not asked" in str(c)]),
                             "said again once it failed after working")

    def test_a_follower_asks_when_it_looks(self):
        os.remove(os.path.join(self.tmp.name, self.PAGES.WANTED))
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, self.PAGES.WANTED)))
        self.become_another_replica()
        self.lead_one_cycle()
        self.assertEqual({"ov", "wl"}, set(self.written()["parts"]))

    def test_a_follower_asks_at_most_every_ask_every(self):
        self.PAGES.tick(FOLLOWER, {}, self.builders, now=100)
        wanted = os.path.join(self.tmp.name, self.PAGES.WANTED)
        os.utime(wanted, (1000, 1000))
        self.PAGES.tick(FOLLOWER, {}, self.builders, now=100 + self.PAGES.ASK_EVERY - 1)
        self.assertEqual(1000, os.stat(wanted).st_mtime)
        self.PAGES.tick(FOLLOWER, {}, self.builders, now=100 + self.PAGES.ASK_EVERY)
        self.assertNotEqual(1000, os.stat(wanted).st_mtime, "asked again on the loop's clock")

    def test_unchanged_parts_are_not_rewritten_until_they_would_go_stale(self):
        with mock.patch.object(self.PAGES.time, "time", lambda: 1000):
            self.lead_one_cycle(100)
        first = os.stat(self.path()).st_ino
        self.PAGES.share(now=1001, mono=101)
        self.assertEqual(first, os.stat(self.path()).st_ino)

    def test_unchanged_parts_are_rewritten_a_cycle_before_refresh(self):
        """share() runs only after a cycle: rewrite on the last one under REFRESH,
        not the first one past it, or a follower's part ages a whole cycle more."""
        with mock.patch.object(self.PAGES.time, "time", lambda: 1000):
            self.lead_one_cycle(100)
        first = os.stat(self.path()).st_ino
        self.PAGES.share(now=1000 + self.PAGES.REFRESH - self.PAGES.cadence(), mono=101)
        self.assertNotEqual(first, os.stat(self.path()).st_ino)

    def test_a_changed_part_is_written_at_once(self):
        with mock.patch.object(self.PAGES.time, "time", lambda: 1000):
            self.lead_one_cycle(100)
        self.builders[1] = ("wl", lambda got: [{"name": "web"}, {"name": "db"}])
        with mock.patch.object(self.PAGES.time, "time", lambda: 1005):
            self.lead_one_cycle(100 + self.PAGES.cadence())
        self.assertEqual([{"name": "web"}, {"name": "db"}], self.written()["parts"]["wl"][1])

    def test_a_part_that_cannot_be_hashed_never_raises(self):
        self.builders[1] = ("wl", lambda got: {1: "one", "b": 2})   # mixed keys: no sort_keys
        self.lead_one_cycle()

    def test_unchanged_parts_are_rewritten_before_they_go_stale(self):
        with mock.patch.object(self.PAGES.time, "time", lambda: 1000):
            self.lead_one_cycle(100)
        before = self.written()["written"]
        self.PAGES.share(now=1000 + self.PAGES.REFRESH, mono=101)
        self.assertGreater(self.written()["written"], before)
        self.assertLess(self.PAGES.REFRESH, self.PAGES.STALE_AFTER)


class FollowerReadsTests(Shared):
    def test_another_replica_serves_the_leader_s_parts(self):
        self.lead_one_cycle()
        as_of = self.PAGES.read("wl")[0]
        self.become_another_replica()
        self.assertIsNone(self.PAGES.tick(FOLLOWER, {}, self.builders, now=101), "no cycle of its own")
        self.assertEqual((as_of, [{"name": "web"}]), self.PAGES.read("wl"))
        self.assertEqual([1], self.runs, "built once, by the leader")

    def test_a_part_older_than_stale_after_is_not_taken_in_or_served(self):
        old = time.time() - self.PAGES.STALE_AFTER - 1
        self.write_parts({"wl": [old, [{"name": "web"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertIsNone(self.PAGES.read("wl"))
        fresh = time.time()
        self.write_parts({"wl": [fresh, [{"name": "web"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertIsNone(self.PAGES.read("wl", now=fresh + self.PAGES.STALE_AFTER), "ages out as the leader's do")

    def test_a_write_on_this_replica_after_the_leader_read_wins(self):
        self.write_parts({"wl": [time.time(), [{"name": "web"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertIsNotNone(self.PAGES.read("wl"))
        time.sleep(0.01)
        CACHE.forget_where(lambda k: k == "wl")
        self.assertIsNone(self.PAGES.read("wl"))

    def test_after_a_pop_a_leader_part_read_before_it_is_not_taken_in(self):
        first = time.time()
        self.write_parts({"wl": [first, [{"name": "web"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        time.sleep(0.01)
        CACHE._cache.pop("wl", None)          # a write here, as the modules that pop their keys do
        self.PAGES.tick(FOLLOWER, {}, self.builders)   # the loop's next look, file unchanged
        time.sleep(0.01)
        # The leader's next file lands, but its cycle read the cluster before that write.
        self.write_parts({"wl": [first + 0.005, [{"name": "before the write"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertIsNone(self.PAGES.read("wl"))
        self.assertIsNone(CACHE.kept("wl"))
        time.sleep(0.01)
        self.write_parts({"wl": [time.time(), [{"name": "after"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertEqual([{"name": "after"}], self.PAGES.read("wl")[1], "a cycle after the write is taken in")

    def test_a_read_of_its_own_in_between_does_not_hold_off_the_leader_s_next_part(self):
        self.write_parts({"wl": [time.time() - 2, [{"name": "web"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        CACHE.keep("wl", ["mine"], CACHE.epoch("wl"), at=time.time() - 1)   # e.g. cached("wl", 5, ...)
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.write_parts({"wl": [time.time(), [{"name": "newer"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertEqual([{"name": "newer"}], self.PAGES.read("wl")[1])

    def test_a_forget_between_the_checks_and_the_keep_keeps_nothing(self):
        self.write_parts({"wl": [time.time() - 1, [{"name": "web"}]]})
        real = CACHE.forgotten_at

        def forgotten_at(key):
            seen = real(key)
            CACHE.forget_where(lambda k: k == key)      # a write lands just after the check
            return seen
        with mock.patch.object(CACHE, "forgotten_at", forgotten_at):
            self.PAGES.take_in()
        self.assertIsNone(CACHE.kept("wl"))
        self.assertIsNone(self.PAGES.read("wl"))

    def test_a_key_popped_after_a_write_wins(self):
        self.write_parts({"wl": [time.time(), [{"name": "web"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        CACHE._cache.pop("wl", None)          # as the modules that pop their keys do
        self.assertIsNone(self.PAGES.read("wl"))

    def test_a_newer_read_of_its_own_is_not_replaced(self):
        CACHE.keep("wl", ["mine"], CACHE.epoch("wl"), at=time.time())
        self.write_parts({"wl": [time.time() - 5, [{"name": "web"}]]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertIsNone(self.PAGES.read("wl"))
        self.assertEqual(["mine"], CACHE.kept("wl")[1])

    def test_a_file_already_taken_in_is_not_read_again(self):
        self.write_parts({"wl": [time.time(), [{"name": "web"}]]})
        with mock.patch.object(self.PAGES, "_load", wraps=self.PAGES._load) as load:
            self.PAGES.tick(FOLLOWER, {}, self.builders)
            self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertEqual(1, load.call_count)

    def test_a_missing_or_broken_file_is_no_part(self):
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        with open(self.path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.write_parts({"wl": "not a pair", "other": [time.time(), 1]})
        self.PAGES.tick(FOLLOWER, {}, self.builders)
        self.assertIsNone(self.PAGES.read("wl"))
        self.assertIsNone(self.PAGES.read("other"))


class FollowerPagesTests(unittest.TestCase):
    """End to end on the fake cluster: a replica that is not the leader
    answers Hosts, Apps, Volumes, storage and the Dashboard from the leader's
    file without asking the cluster."""

    def setUp(self):
        T.clean_cache(self)
        self.cluster = T.Cluster(self)
        self.PAGES = T.clean_snapshot(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = SHARED.DIR
        SHARED.bind(self.tmp.name)
        self.addCleanup(SHARED.bind, saved)
        import homestead_storage as STORAGE
        self.enterContext(mock.patch.object(STORAGE.VOLUME_USAGE, "collect", lambda get, claims: {}))
        self.OV = self.cluster.server.OVERVIEW_ROUTES
        # A follower has asked for the parts (its first look does), so the
        # leader writes them; with nobody asking it writes nothing.
        open(os.path.join(self.tmp.name, self.PAGES.WANTED), "a").close()

    def test_a_follower_answers_every_page_without_asking_the_cluster(self):
        now = time.time()
        with mock.patch.object(T.K.time, "time", lambda: now):
            self.PAGES.tick(LEADER, self.OV.page_inputs(), self.OV.page_builders(), now=100)
            on_leader = {key: self.cluster.route(key) for key in T.ROUTES}
            self.PAGES.reset()
            for store in (CACHE._cache, CACHE._running, CACHE._epoch, CACHE._forgot):
                store.clear()
            self.cluster.calls.clear()
            self.PAGES.tick(FOLLOWER, self.OV.page_inputs(), self.OV.page_builders(), now=101)
            on_follower = {key: self.cluster.route(key) for key in T.ROUTES}
        self.assertEqual([], self.cluster.calls)
        self.assertEqual(json.loads(json.dumps(on_leader)), on_follower)


if __name__ == "__main__":
    unittest.main()
