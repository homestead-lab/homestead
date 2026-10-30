"""A forced refresh (?force=1) on a viewer read runs once at a time per key
and at most once per FORCED.MIN_INTERVAL: a forced call inside that window
gets the last forced answer instead of running the expensive work again."""
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_cache as CACHE
import homestead_forced as FORCED

WAIT = 5    # a guard against a hung test, never part of what is checked


class Clock:
    """A fake clock that starts at the real time, so forgets recorded by
    homestead_cache (real time) come after runs that started at 'now'."""

    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class ForcedTestCase(unittest.TestCase):
    def setUp(self):
        FORCED.reset()
        self.clock = Clock()
        patcher = mock.patch.object(FORCED, "clock", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(FORCED.reset)


class HelperTests(ForcedTestCase):
    def counter(self, value="answer"):
        runs = []

        def work():
            runs.append(1)
            return f"{value}-{len(runs)}"
        return runs, work

    def test_a_second_forced_call_inside_the_window_reuses_the_first_answer(self):
        runs, work = self.counter()
        self.assertEqual("answer-1", FORCED.run("k", work))
        self.clock.now += FORCED.MIN_INTERVAL - 1
        self.assertEqual("answer-1", FORCED.run("k", work))
        self.assertEqual(1, len(runs))

    def test_a_forced_call_after_the_window_runs_again(self):
        runs, work = self.counter()
        FORCED.run("k", work)
        self.clock.now += FORCED.MIN_INTERVAL
        self.assertEqual("answer-2", FORCED.run("k", work))
        self.assertEqual(2, len(runs))

    def test_the_window_is_kept_per_key(self):
        runs, work = self.counter()
        FORCED.run("a", work)
        FORCED.run("b", work)
        self.assertEqual(2, len(runs))

    def test_concurrent_forced_calls_share_one_run(self):
        release, started, runs = threading.Event(), threading.Event(), []

        def work():
            runs.append(1)
            started.set()
            release.wait(WAIT)
            return "shared"

        results = []
        first = threading.Thread(target=lambda: results.append(FORCED.run("k", work)))
        first.start()
        self.assertTrue(started.wait(WAIT))
        others = [threading.Thread(target=lambda: results.append(FORCED.run("k", work))) for _ in range(3)]
        for thread in others:
            thread.start()
        flight = FORCED._running["k"]
        deadline = time.monotonic() + WAIT
        while flight.waiters < 3 and time.monotonic() < deadline:
            flight.done.wait(0.01)
        self.assertEqual(3, flight.waiters)
        release.set()
        for thread in [first, *others]:
            thread.join(WAIT)
        self.assertEqual(["shared"] * 4, results)
        self.assertEqual(1, len(runs))

    def test_a_failed_run_is_shared_with_its_waiters_but_not_kept(self):
        release, started, calls = threading.Event(), threading.Event(), []

        def failing():
            calls.append(1)
            started.set()
            release.wait(WAIT)
            raise RuntimeError("registry down")

        errors = []

        def call():
            try:
                FORCED.run("k", failing)
            except RuntimeError as error:
                errors.append(str(error))

        first = threading.Thread(target=call)
        first.start()
        self.assertTrue(started.wait(WAIT))
        second = threading.Thread(target=call)
        second.start()
        flight = FORCED._running["k"]
        deadline = time.monotonic() + WAIT
        while flight.waiters < 1 and time.monotonic() < deadline:
            flight.done.wait(0.01)
        release.set()
        first.join(WAIT)
        second.join(WAIT)
        self.assertEqual(["registry down"] * 2, errors)
        self.assertEqual(1, len(calls))
        self.assertEqual("fine", FORCED.run("k", lambda: "fine"), "an error is not the answer of the window")

    def test_forget_lets_the_next_forced_call_run(self):
        runs, work = self.counter()
        FORCED.run("k", work)
        FORCED.forget("k")
        self.assertEqual("answer-2", FORCED.run("k", work))

    def test_a_cache_forget_of_the_same_key_lets_the_next_forced_call_run(self):
        runs, work = self.counter()
        FORCED.run("k", work)
        CACHE.forget_where(lambda key: key == "k")
        self.assertEqual("answer-2", FORCED.run("k", work))


def request(**query):
    return types.SimpleNamespace(query={k: [v] for k, v in query.items()}, body={}, user="viewer")


class RouteTests(ForcedTestCase):
    def test_portal_status_probes_each_link_once_per_window(self):
        import homestead_portal as PORTAL
        probes = []
        rows = [{"id": "aaaaaaaa", "url": "http://192.0.2.1"}, {"id": "bbbbbbbb", "url": "http://192.0.2.2"}]
        with mock.patch.object(PORTAL, "stored", lambda: rows), \
                mock.patch.object(PORTAL, "_reachable", lambda url: probes.append(url) or {"up": True, "ms": 1}):
            first = PORTAL.status(force=True)
            second = PORTAL.status(force=True)
            self.assertEqual(first, second)
            self.assertEqual(2, len(probes), "one probe per link, not one per forced call")
            self.clock.now += FORCED.MIN_INTERVAL
            PORTAL.status(force=True)
            self.assertEqual(4, len(probes))

    def test_resource_kinds_discovery_runs_once_per_window(self):
        import homestead_resources as RES
        reads = []

        def kget(path):
            reads.append(path)
            return {"groups": []} if path == "/apis" else {"resources": [{"name": "pods", "kind": "Pod", "verbs": ["list"]}]}

        saved = (RES.kget, RES.ksend, RES.table_get)
        RES.bind(kget, None, None)
        self.addCleanup(lambda: RES.bind(*saved))
        RES.discover(force=True)
        RES.discover(force=True)
        self.assertEqual(2, len(reads), "/api/v1 and /apis once")

    def test_components_force_refetches_releases_once_per_window(self):
        import homestead_components as C
        reports = []
        self.addCleanup(CACHE.forget_where, lambda key: key == "components")
        with mock.patch.object(C, "report", lambda force=False: reports.append(force) or {"rows": len(reports)}):
            first = C._report_route(request(force="1"))
            self.assertEqual(first, C._report_route(request(force="1")))
            self.assertEqual([True], reports)
            self.assertEqual(first, CACHE.cached("components", 60, lambda: "quiet read"),
                             "quiet reads get the forced answer")
            CACHE.forget_where(lambda key: key == "components")    # what an upgrade does
            C._report_route(request(force="1"))
        self.assertEqual([True, True], reports)

    def test_image_updates_force_scans_once_per_window(self):
        import homestead_updates as U
        scans, homestead = [], []
        with mock.patch.object(U, "report", lambda force=False: scans.append(force) or {"workloads": []}), \
                mock.patch.object(U, "homestead_report", lambda: homestead.append(1) or {"workloads": []}), \
                mock.patch.object(U, "update_policy_status", lambda: {}, create=True):
            U._report(request(force="1"))
            U._report(request(force="1"))
            U._report(request(force="1", only="homestead"))
            U._report(request(force="1", only="homestead"))
            U._report(request())
        self.assertEqual([True, False], scans, "one forced scan; the quiet read is unchanged")
        self.assertEqual([1], homestead)

    def test_portal_status_window_is_shorter(self):
        import homestead_portal as PORTAL
        probes = []
        rows = [{"id": "aaaaaaaa", "url": "http://192.0.2.1"}]
        with mock.patch.object(PORTAL, "stored", lambda: rows), \
                mock.patch.object(PORTAL, "_reachable", lambda url: probes.append(url) or {"up": True, "ms": 1}):
            PORTAL.status(force=True)
            self.clock.now += PORTAL.FORCED_INTERVAL - 1
            PORTAL.status(force=True)
            self.assertEqual(1, len(probes))
            self.clock.now += 1
            PORTAL.status(force=True)
        self.assertEqual(10, PORTAL.FORCED_INTERVAL)
        self.assertEqual(2, len(probes), "a forced check 10 s later probes again")

    def test_platform_force_detects_once_per_window(self):
        import homestead_platform as PLATFORM
        detects = []
        route = PLATFORM.ROUTES[("GET", "/api/platform")][1]
        with mock.patch.object(PLATFORM, "detect", lambda force=False: detects.append(force) or {"n": len(detects)}):
            first = route(request(force="1"))
            self.assertEqual(first, route(request(force="1")))
            route(request())
        self.assertEqual([True, False], detects, "one forced detect; the quiet read is unchanged")

    def image_updates(self):
        import homestead_updates as U
        scans = []
        for name, fake in (("report", lambda force=False: scans.append(force) or {"workloads": []}),
                           ("homestead_report", lambda: scans.append("homestead") or {"workloads": []}),
                           ("update_policy_status", lambda: {})):
            patcher = mock.patch.object(U, name, fake, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        U._report(request(force="1"))
        U._report(request(force="1", only="homestead"))
        self.assertEqual([True, "homestead"], scans)
        return U, scans

    def assert_forced_checks_run_again(self, U, scans):
        U._report(request(force="1"))
        U._report(request(force="1", only="homestead"))
        self.assertEqual([True, "homestead", True, "homestead"], scans)

    def test_an_applied_update_ends_the_forced_image_update_window(self):
        U, scans = self.image_updates()
        fakes = {"guard_managed_smb": lambda ns, name: None, "enforce_update_policy": lambda b: None,
                 "reviewed_image_update": lambda b, action: {}, "refresh_soon": lambda ns, name: None}
        for name, fake in fakes.items():
            patcher = mock.patch.object(U, name, fake, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(U.OPS, "start", lambda *a, **k: "op"), mock.patch.object(U.ROUTER, "forget", lambda *k: None):
            U._apply(types.SimpleNamespace(body={"ns": "lab", "name": "web"}, query={}), "update")
        self.assert_forced_checks_run_again(U, scans)

    def test_a_channel_change_ends_the_forced_image_update_window(self):
        U, scans = self.image_updates()
        with mock.patch.object(U, "get_app_settings", lambda: {"updates": {}}, create=True), \
                mock.patch.object(U, "save_app_settings", lambda settings: {"ok": True}, create=True):
            U._set_channel(types.SimpleNamespace(body={"channel": "beta"}, query={}))
        self.assert_forced_checks_run_again(U, scans)

    def test_invalidate_ends_the_forced_image_update_window(self):
        import homestead_update_checks as CHECKS
        U, scans = self.image_updates()
        CHECKS.invalidate()
        self.assert_forced_checks_run_again(U, scans)

    def test_refresh_one_ends_the_forced_image_update_window(self):
        import homestead_update_checks as CHECKS
        U, scans = self.image_updates()
        with mock.patch.dict(U._LATEST, report=None):
            CHECKS.refresh_one("lab", "web")       # nothing to refresh, but the app changed
        self.assert_forced_checks_run_again(U, scans)


if __name__ == "__main__":
    unittest.main()
