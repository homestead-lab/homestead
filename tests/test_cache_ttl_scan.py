"""No reader keeps its answer for less time than the cycle that feeds it (#978).

A page answer kept 5 s while the leader's cycle refreshes it every 20 s makes
nearly every look read the cluster again. #1039 settled the snapshot's parts
with a floor inside cached() (homestead_routes: CACHE.at_least(PAGE_SECONDS,
...)) rather than at each call. This keeps it settled: the parts keep their
floor, and any other cached() call that keeps its answer for less than a
page cycle is listed here with the reason it must stay short. The list only
shrinks: a key that no longer keeps briefly fails until its entry goes.

Only calls written with a literal key and a literal number are seen (a lint,
like the other source scans); a keep time held in a name is the author's
own, documented beside it."""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_cache as CACHE
import homestead_routes
import source_scan as SS

PARTS = ("nodes", "wl", "vol", "stor", "ov", "vms")

# Keys kept for less than a page cycle on purpose, and why.
SHORT = {
    "network": "IPAM's address picks read it and must see an address taken a moment ago",
    "settings": "Homestead's own settings: a change made on another tab shows on the next look",
    "placement": "page route left short on purpose (#978 leftovers: E skipped); read only while its page is open",
    "cron": "the Schedules page and the timeline; a run that just ended shows on the next look",
    "volother": "page route left short on purpose (#978 leftovers: E skipped)",
    "vmimages": "page route left short on purpose (#978 leftovers: E skipped)",
    "hardware:features": "what a host's hardware supports; read on its dialog only",
    "ev": "page route left short on purpose (#978 leftovers: E skipped); events are read Warning-only",
    "data-copies": "the data-protection copies list while a copy job runs",
    "cluster": "page route left short on purpose (#978 leftovers: E skipped)",
    "baseline": "the page's own comparison point for its rates",
    "lhbackupvols": "page route left short on purpose (#978 leftovers: E skipped)",
}

CALLEES = ("cached", "CACHE.cached", "homestead_routes.cached", "routes.cached", "ROUTES.cached")


def short_keeps():
    """{key: [file:line, ...]} for cached("<key>", <number below PAGE_SECONDS>, ...)."""
    found = {}
    for path in SS.server_files():
        for call in SS.calls(path, *CALLEES):
            if len(call.args) < 2:
                continue
            key, ttl = call.args[0], call.args[1]
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not (isinstance(ttl, ast.Constant) and isinstance(ttl.value, (int, float))):
                continue
            if ttl.value < homestead_routes.PAGE_SECONDS:
                found.setdefault(key.value, []).append(f"{path.name}:{call.lineno}")
    return found


class TtlScan(unittest.TestCase):
    def test_the_snapshot_parts_keep_at_least_a_page_cycle(self):
        for key in PARTS:
            with self.subTest(key=key):
                self.assertGreaterEqual(CACHE._at_least.get(key, 0), homestead_routes.PAGE_SECONDS,
                                        f"{key} is a snapshot part: keep it at_least PAGE_SECONDS (#978)")

    def test_a_short_keep_is_listed_with_its_reason(self):
        found = {key: where for key, where in short_keeps().items() if key not in PARTS}
        missing = {key: where for key, where in found.items() if key not in SHORT}
        self.assertEqual({}, missing, "these keep their answer for less than a page cycle "
                                      f"({homestead_routes.PAGE_SECONDS} s): keep them longer, or list them "
                                      "in SHORT with the reason they must stay short (#978)")

    def test_the_list_only_shrinks(self):
        found = short_keeps()
        stale = sorted(key for key in SHORT if key not in found)
        self.assertEqual([], stale, "these no longer keep briefly: remove them from SHORT")

    def test_the_scan_sees_the_calls(self):
        # A floor: the scan must find today's short keeps, or it checks nothing.
        self.assertGreaterEqual(len(short_keeps()), len(SHORT) // 2)

    def test_it_catches_a_new_short_keep(self):
        sample = SS.code('import homestead_routes\nx = homestead_routes.cached("brand-new", 5, lambda: 1)\n')
        calls = SS.calls(sample, *CALLEES)
        self.assertEqual(1, len(calls))
        self.assertEqual(("brand-new", 5), (calls[0].args[0].value, calls[0].args[1].value))


if __name__ == "__main__":
    unittest.main()
