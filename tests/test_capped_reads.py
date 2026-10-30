"""The guard on unbounded work in server/: a ratchet that may only shrink.

Three kinds of site are counted (tests/unbounded_scan.py finds them):

- reads: an answer from the network (urlopen, getresponse, an opener's open,
  a module's _open, an HTTPError's body) read with .read() and no size, or
  handed whole to json.load(). The far end picks the size, and urllib's
  timeout is per wait on the socket, so a trickle never times out. New code
  reads with homestead_fetch.read(response, limit, deadline).
- caches: a module-level dict named *cache* with no size check (len(name)
  compared with something) anywhere in its module. A key per request or per
  image seen grows it until the restart.
- joins: a loop that joins each thread with its own fixed timeout (or none).
  N stuck threads make the loop wait N times the timeout. Join under one
  deadline instead: t.join(max(0, end - time.monotonic())).

The BASELINE dicts are the sites on dev when the guard came in. A count may go
down (and then has to be lowered here), never up, and a new site fails.

What the scan does not see, by design (it is a lint, not a proof):
- a response under another name than the ones above (passed into a helper and
  read there, or opened through a wrapper with another name);
- .read(-1) or .read(None), which are uncapped too;
- a read with a size but no deadline (a trickle under the cap);
- caches not named *cache*, caches on a class or an instance, and a cache whose
  bound is a check other than len(name) in a comparison;
- threads joined outside a loop, or one after another in a row.
What it counts that may be harmless (each stays in BASELINE until it moves to
homestead_fetch):
- reads from trusted in-cluster peers or small error bodies (HTTPError);
- caches keyed by a small fixed set (a fixed record dict with keys is never
  counted, only an empty dict that grows);
- a loop joining a fixed set of threads at shut-down;
- x.join(name) in a loop where x is a separator string, not a thread.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan as SS
import unbounded_scan as SCAN

HELPER = "homestead_fetch.read(response, limit, deadline)"

# (file under server/, function) -> uncapped reads in it, when the guard came in.
READS = {
    # Trusted public hosts (GitHub, distribution mirrors), admin or background only.
    ("homestead_addons.py", "_fetch"): 1,
    ("homestead_components.py", "_get_json"): 1,
    ("homestead_upgrades.py", "_github"): 1,
    ("homestead_vmstore.py", "_get_json"): 1,
    ("homestead_vmstore.py", "_get_text"): 1,
    ("homestead_cfaccess.py", "_fetch_keys"): 1,
    # The API server and in-cluster peers (Longhorn, virt-handler, own probes).
    # homestead_kube._request: gone with #1047 / #1050 (read under the request deadline).
    # homestead_workloads.logs: capped at LOG_BYTES on dev since #986.
    ("homestead_move.py", "_call"): 1,
    ("homestead_longhorn_jobs.py", "_backend"): 2,
    ("homestead_revert.py", "_backend"): 2,
    ("homestead_smart.py", "_request"): 2,
    ("homestead_vmusage.py", "_fetch"): 1,
    ("homestead_cluster_shutdown.py", "main.request"): 1,
    ("homestead_power_handoff.py", "main.request"): 1,
    # A linked cluster and a UniFi controller the operator set up.
    # homestead_fleet._json: gone with #1051 (homestead_fetch.read).
    ("homestead_ipam.py", "_unifi_get"): 1,
    # An error body (HTTPError), cut after reading.
    ("homestead_deploy_server.py", "compose_apply"): 1,
    ("homestead_lhcapacity.py", "_patch_setting"): 1,
    ("homestead_longhorn_target.py", "_set_harvester_target"): 1,
    ("homestead_vm_create.py", "commit_vm"): 1,
}

# (file under server/, module-level name) -> 1, when the guard came in.
CACHES = {
    # homestead_cache._cache (the shared cached()): bounded at CACHE_MAX by #1053.
    # homestead_update_registry._CACHE and homestead_volowner._cache: bounded by #1051.
    ("homestead_icons.py", "_DATA_URL_CACHE"): 1,
    ("homestead_push.py", "_key_cache"): 1,
    ("homestead_hardware.py", "_cache"): 1,
    ("homestead_imports.py", "_cache"): 1,
    ("homestead_lifecycle.py", "_cache"): 1,
    ("homestead_longhorn.py", "_cache"): 1,
    ("homestead_place.py", "_cache"): 1,
    ("homestead_share_servers.py", "_cache"): 1,
    ("homestead_shares.py", "CACHE"): 1,
    ("homestead_storage.py", "_cache"): 1,
    ("homestead_storage_classes.py", "_cache"): 1,
}

# (file under server/, function) -> join-in-a-loop sites in it, when the guard came in.
JOINS = {
    # homestead_fleet.summary and homestead_uptime.observe: one deadline since #1051.
    ("homestead_portal.py", "_probe"): 1,    # join(3) per app; each thread is a 1.5 s connect
}


class ScanFindsSites(unittest.TestCase):
    """The scan itself, on small pieces of code."""

    def reads(self, source):
        return SCAN.reads(SS.code(source))

    def test_an_uncapped_read_of_an_opened_answer_is_counted(self):
        self.assertEqual({"get": 1}, self.reads(
            "import urllib.request\n"
            "def get(url):\n"
            "    with urllib.request.urlopen(url, timeout=5) as response:\n"
            "        return response.read()\n"))

    def test_a_read_with_a_size_is_not_counted(self):
        self.assertEqual({}, self.reads(
            "def get(url):\n"
            "    with urlopen(url, timeout=5) as r:\n"
            "        return r.read(4096)\n"))

    def test_the_shared_helper_is_not_counted(self):
        self.assertEqual({}, self.reads(
            "import homestead_fetch as FETCH\n"
            "def get(url):\n"
            "    with urlopen(url, timeout=5) as r:\n"
            "        return FETCH.read(r, 1 << 20, deadline=9.0)\n"))

    def test_getresponse_opener_open_and_module_open_are_answers(self):
        self.assertEqual({"a": 1, "b": 1, "C.c": 1}, self.reads(
            "def a(conn):\n"
            "    response = conn.getresponse()\n"
            "    return response.read()\n"
            "def b(opener, url):\n"
            "    with opener.open(url) as answer:\n"
            "        return json.load(answer)\n"
            "class C:\n"
            "    def c(self, url):\n"
            "        return _open(url).read()\n"))

    def test_an_error_body_is_an_answer_too(self):
        self.assertEqual({"get": 1}, self.reads(
            "def get(url):\n"
            "    try:\n"
            "        urlopen(url, timeout=5).close()\n"
            "    except urllib.error.HTTPError as error:\n"
            "        return error.read()\n"))

    def test_a_file_read_is_not_counted(self):
        self.assertEqual({}, self.reads(
            "def get(path):\n"
            "    with open(path) as handle:\n"
            "        return handle.read()\n"))

    def test_a_name_from_another_function_is_not_an_answer(self):
        self.assertEqual({}, self.reads(
            "def a(url):\n"
            "    with urlopen(url) as response:\n"
            "        return response.read(10)\n"
            "def b(response):\n"
            "    return response.read()\n"))

    def test_a_growing_cache_without_a_size_check_is_counted(self):
        self.assertEqual({"_cache", "_DATA_CACHE"}, set(SCAN.caches(SS.code(
            "import collections\n"
            "_cache = {}\n"
            "_DATA_CACHE = collections.OrderedDict()\n"
            "_record_cache = {'at': 0, 'value': None}\n"
            "_names = {}\n"))))

    def test_a_cache_with_a_size_check_is_not_counted(self):
        self.assertEqual({}, SCAN.caches(SS.code(
            "_cache, _cache_lock, CACHE_MAX = {}, None, 64\n"
            "def put(key, value):\n"
            "    while len(_cache) >= CACHE_MAX:\n"
            "        _cache.pop(next(iter(_cache)))\n"
            "    _cache[key] = value\n")))

    def test_a_join_with_its_own_timeout_in_a_loop_is_counted(self):
        self.assertEqual({"round": 2}, SCAN.joins(SS.code(
            "def round(threads, TIMEOUT):\n"
            "    for t in threads:\n"
            "        t.join(TIMEOUT * 2 + 2)\n"
            "    for name, worker in threads.items():\n"
            "        worker.join()\n")))

    def test_a_join_under_one_deadline_is_not_counted(self):
        self.assertEqual({}, SCAN.joins(SS.code(
            "def round(threads, end):\n"
            "    for t in threads:\n"
            "        t.join(max(0, end - time.monotonic()))\n"
            "    for part in parts:\n"
            "        ', '.join(part)\n")))


class Ratchet(unittest.TestCase):
    """Today's sites may only go down; new code uses the capped helper."""

    def check(self, found, baseline, what, fix):
        grown = {key: count for key, count in found.items() if count > baseline.get(key, 0)}
        self.assertEqual({}, grown, f"new {what}: {fix}")
        stale = {key: (baseline[key], found.get(key, 0)) for key in baseline
                 if found.get(key, 0) < baseline[key]}
        self.assertEqual({}, stale, f"fewer {what} than the baseline (baseline, now): lower it")

    def test_the_helper_is_there(self):
        self.assertTrue((SS.SERVER / "homestead_fetch.py").is_file())
        self.assertIn("def read(", SS.text(SS.SERVER / "homestead_fetch.py"))

    def test_uncapped_network_reads_only_shrink(self):
        self.check(SCAN.every(SCAN.reads), READS, "uncapped network reads",
                   f"read the answer with {HELPER}")

    def test_unbounded_caches_only_shrink(self):
        self.check(SCAN.every(SCAN.caches), CACHES, "module caches with no size bound",
                   "bound it: evict when len(cache) reaches a *_MAX (see homestead_helm._cache)")

    def test_thread_join_loops_only_shrink(self):
        self.check(SCAN.every(SCAN.joins), JOINS, "loops joining each thread with its own timeout",
                   "join under one deadline: t.join(max(0, end - time.monotonic()))")


if __name__ == "__main__":
    unittest.main()
