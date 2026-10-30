"""The shared cache's bounds: an answer nobody asks for any more is dropped,
and the number of keys is capped. Before, an entry only expired when it was
read, so a key never read again (the App Store feed under an old catalogue
URL, tens of MB) stayed until the restart."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cache as CACHE


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _clear():
    for store in (CACHE._cache, CACHE._running, CACHE._epoch, CACHE._forgot, CACHE._used, CACHE._ttl):
        store.clear()
    CACHE._swept[0] = 0.0


class CacheBoundTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.addCleanup(_clear)
        self.clock = Clock()
        patcher = mock.patch("homestead_cache.time.time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def later(self, seconds):
        self.clock.now += seconds

    def test_an_answer_nobody_asks_for_again_is_dropped(self):
        CACHE.cached("appstore:https://old.example.com/feed.json", 21600, lambda: {"apps": ["x"] * 10})
        CACHE.cached("nodes", 5, lambda: ["node-a"])
        self.later(21600 + CACHE.IDLE_SECONDS + 1)
        CACHE.cached("wl", 5, lambda: [])          # any later write runs the sweep
        self.assertEqual({"wl"}, set(CACHE._cache))
        self.assertEqual({"wl"}, set(CACHE._used) | set(CACHE._ttl))

    def test_an_answer_still_within_its_time_is_kept_however_long_unread(self):
        calls = []
        CACHE.cached("appstore:feed", 21600, lambda: calls.append(1) or "feed")
        self.later(CACHE.IDLE_SECONDS * 3)
        CACHE.cached("wl", 5, lambda: [])
        self.assertEqual("feed", CACHE.cached("appstore:feed", 21600, lambda: calls.append(1) or "new"))
        self.assertEqual([1], calls)

    def test_the_last_copy_stays_while_it_is_still_asked_for(self):
        """An API server down for hours: the page keeps the last answer it had."""
        CACHE.cached("nodes", 5, lambda: ["node-a"])

        def down():
            raise OSError("the API server is down")
        for _ in range(12):                         # asked every 10 minutes for 2 h
            self.later(600)
            CACHE.cached("other", 5, lambda: [])    # writes run the sweep
            self.assertEqual(["node-a"], CACHE.cached("nodes", 5, down))

    def test_the_sweep_runs_at_most_every_few_minutes(self):
        CACHE.cached("a", 5, lambda: 1)
        self.later(CACHE.IDLE_SECONDS + 10)
        CACHE.cached("b", 5, lambda: 2)
        self.assertNotIn("a", CACHE._cache)
        CACHE.cached("c", 5, lambda: 3)
        self.later(CACHE.IDLE_SECONDS + 10)
        with mock.patch.object(CACHE, "_sweep", wraps=CACHE._sweep) as sweep:
            CACHE.cached("d", 5, lambda: 4)
            CACHE.cached("e", 5, lambda: 5)
        self.assertEqual(1, sweep.call_count)

    def test_the_number_of_keys_is_capped_dropping_the_least_recently_used(self):
        with mock.patch.object(CACHE, "CACHE_MAX", 8):
            for n in range(8):
                CACHE.cached(f"k{n}", 60, lambda n=n: n)
                self.later(1)
            CACHE.cached("k0", 60, lambda: "again")  # k0 is used again: k1 is now the oldest
            for n in range(8, 13):
                CACHE.cached(f"k{n}", 60, lambda n=n: n)
                self.later(1)
            self.assertLessEqual(len(CACHE._cache), 8)
            self.assertIn("k12", CACHE._cache)
            self.assertIn("k0", CACHE._cache)
            self.assertNotIn("k1", CACHE._cache)

    def test_a_key_being_computed_survives_the_sweep_and_the_cap(self):
        """Its last copy is what callers waiting on the flight fall back on."""
        import threading
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        CACHE.cached("slow", 5, lambda: "old")
        self.later(CACHE.IDLE_SECONDS + 10)

        def slow():
            started.set()
            release.wait(5)
            return "new"
        worker = threading.Thread(target=CACHE.cached, args=("slow", 5, slow), daemon=True)
        worker.start()
        self.assertTrue(started.wait(5))
        self.later(CACHE.IDLE_SECONDS + 10)
        with CACHE._lock:
            CACHE._sweep(self.clock.now)
            CACHE._evict(len(CACHE._cache))
        self.assertEqual("old", CACHE._cache["slow"][1])
        self.assertIn("slow", CACHE._used)
        release.set()
        worker.join(5)
        self.assertEqual("new", CACHE._cache["slow"][1])

    def test_keep_and_refresh_are_bounded_too(self):
        CACHE.keep("snapshot", {"n": 1}, CACHE.epoch("snapshot"))
        CACHE.refresh("loop", lambda: 1)
        self.later(CACHE.IDLE_SECONDS + 1)
        CACHE.refresh("loop", lambda: 2)
        self.assertEqual({"loop"}, set(CACHE._cache))
        self.assertIn("snapshot", CACHE._epoch)     # a forget is still recorded for a held epoch


if __name__ == "__main__":
    unittest.main()
