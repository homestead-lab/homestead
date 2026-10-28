"""The pages' short-lived answers (#705): one computation however many callers
miss at once, kept longer than the UI's 15 s refresh, and dropped when
Homestead itself changes what they show."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cache as CACHE
import homestead_kube as K


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class SingleFlightTests(unittest.TestCase):
    def setUp(self):
        CACHE._cache.clear()
        self.addCleanup(CACHE._cache.clear)

    def test_callers_that_miss_together_compute_once(self):
        started, release, calls = threading.Event(), threading.Event(), []

        def slow():
            calls.append(1)
            started.set()
            release.wait(5)
            return {"answer": 42}

        results = []
        first = threading.Thread(target=lambda: results.append(CACHE.cached("k", 20, slow)))
        first.start()
        started.wait(5)
        others = [threading.Thread(target=lambda: results.append(CACHE.cached("k", 20, slow))) for _ in range(4)]
        for thread in others:
            thread.start()
        time.sleep(0.2)
        release.set()
        for thread in [first, *others]:
            thread.join(5)
        self.assertEqual(1, len(calls), "one computation for five callers")
        self.assertEqual([{"answer": 42}] * 5, results)

    def test_a_failure_reaches_every_waiting_caller_and_is_not_kept(self):
        started, release, calls, errors = threading.Event(), threading.Event(), [], []

        def failing():
            calls.append(1)
            started.set()
            release.wait(5)
            raise RuntimeError("the API server timed out")

        def call():
            try:
                CACHE.cached("k", 20, failing)
            except RuntimeError as error:
                errors.append(str(error))

        first = threading.Thread(target=call)
        first.start()
        started.wait(5)
        second = threading.Thread(target=call)
        second.start()
        time.sleep(0.2)
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(1, len(calls))
        self.assertEqual(["the API server timed out"] * 2, errors)
        self.assertNotIn("k", CACHE._cache)
        self.assertEqual("fresh", CACHE.cached("k", 20, lambda: "fresh"), "the next caller asks again")

    def test_a_failure_still_falls_back_to_the_last_answer(self):
        clock = Clock()
        with mock.patch.object(CACHE.time, "time", clock):
            CACHE.cached("k", 20, lambda: "old")
            clock.now += 30
            self.assertEqual("old", CACHE.cached("k", 20, lambda: (_ for _ in ()).throw(RuntimeError("down"))))

    def test_a_computation_that_asks_for_its_own_key_does_not_wait_on_itself(self):
        inner = lambda: CACHE.cached("k", 20, lambda: "inner")
        done = []
        thread = threading.Thread(target=lambda: done.append(CACHE.cached("k", 20, inner)), daemon=True)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive(), "it did not wait on itself")
        self.assertEqual(["inner"], done)

    def test_a_write_during_a_read_is_not_overwritten_by_that_read(self):
        started, release, answers = threading.Event(), threading.Event(), iter(["before the write", "after"])

        def read():
            value = next(answers)
            if value == "before the write":
                started.set()
                release.wait(5)
            return value

        old = threading.Thread(target=lambda: CACHE.cached("wl", 20, read), daemon=True)
        old.start()
        started.wait(5)
        with mock.patch.object(K, "WRITE_GUARDS", [K.fresh_reads]):
            K.run_write_guards("PATCH", "/apis/apps/v1/namespaces/lab/deployments/web", {}, lambda: {})
        self.assertEqual("after", CACHE.cached("wl", 20, read), "a read after the write does not join the old one")
        release.set()
        old.join(5)
        self.assertEqual("after", CACHE.cached("wl", 20, read), "the older read was not kept over it")

    def test_a_forgotten_key_is_not_kept_by_the_read_that_was_running(self):
        import homestead_routes as ROUTES
        started, release = threading.Event(), threading.Event()

        def read():
            started.set()
            release.wait(5)
            return "stale"

        old = threading.Thread(target=lambda: CACHE.cached("vol", 20, read), daemon=True)
        old.start()
        started.wait(5)
        ROUTES.forget("vol")
        release.set()
        old.join(5)
        self.assertNotIn("vol", CACHE._cache)

    def test_a_caller_waits_a_bounded_time_then_takes_the_last_answer(self):
        clock, started, release = Clock(), threading.Event(), threading.Event()
        CACHE._cache["k"] = (0.0, "last")

        def stuck():
            started.set()
            release.wait(5)
            return "late"

        with mock.patch.object(CACHE, "WAIT_SECONDS", 0.2):
            owner = threading.Thread(target=lambda: CACHE.cached("k", 20, stuck), daemon=True)
            owner.start()
            started.wait(5)
            self.assertEqual("last", CACHE.cached("k", 20, stuck))
            CACHE._cache.pop("k")
            with self.assertRaises(TimeoutError):
                CACHE.cached("k", 20, stuck)
        release.set()
        owner.join(5)

    def test_waiting_callers_are_released_when_the_reader_stops_with_a_base_exception(self):
        started, release, seen = threading.Event(), threading.Event(), []

        class Stopped(BaseException):
            pass

        def stopping():
            started.set()
            release.wait(5)
            raise Stopped()

        def call():
            try:
                seen.append(CACHE.cached("k", 20, stopping))
            except Stopped:
                seen.append("stopped")

        owner, waiter = threading.Thread(target=call, daemon=True), threading.Thread(target=call, daemon=True)
        owner.start()
        started.wait(5)
        waiter.start()
        time.sleep(0.2)
        release.set()
        owner.join(5)
        waiter.join(5)
        self.assertEqual(["stopped", "stopped"], seen, "not a None answer")

    def test_a_loop_refresh_is_kept_for_the_pages_but_never_falls_back(self):
        self.assertEqual("fresh", CACHE.refresh("ov", lambda: "fresh"))
        self.assertEqual("fresh", CACHE.cached("ov", 20, lambda: "recomputed"))
        with self.assertRaises(RuntimeError):
            CACHE.refresh("ov", lambda: (_ for _ in ()).throw(RuntimeError("down")))


class RefreshTests(unittest.TestCase):
    """The Dashboard refreshes every 15 s. With the overview kept 5 s, every
    refresh recomputed it; kept 20 s, every other one does."""

    def setUp(self):
        import server  # binds homestead_routes to the shared cache
        self.server = server
        CACHE._cache.clear()
        self.addCleanup(CACHE._cache.clear)

    def route(self, path):
        return self.server.MODULE_ROUTES.table()[("GET", path)].handler

    def test_the_overview_is_computed_on_every_other_refresh(self):
        import homestead_cluster as CLUSTER
        clock, calls = Clock(), []
        with mock.patch.object(CACHE.time, "time", clock), \
                mock.patch.object(CLUSTER, "overview", lambda: calls.append(1) or {"nodes": []}):
            for tick in range(8):            # two minutes on the Dashboard
                self.route("/api/overview")(None)
                clock.now += 15
        self.assertEqual(4, len(calls))

    def test_two_reads_ten_seconds_apart_compute_once(self):
        import homestead_cluster as CLUSTER
        clock, calls = Clock(), []
        with mock.patch.object(CACHE.time, "time", clock), \
                mock.patch.object(CLUSTER, "overview", lambda: calls.append(1) or {"nodes": []}):
            self.route("/api/overview")(None)
            clock.now += 10
            self.route("/api/overview")(None)
        self.assertEqual(1, len(calls))

    def test_hosts_apps_vms_and_storage_are_kept_past_one_refresh(self):
        import homestead_nodes as NODES
        import homestead_storage as STORAGE
        import homestead_vm_list as VM_LIST
        import homestead_workloads as WORKLOADS
        clock, calls = Clock(), []
        count = lambda name, value: (lambda *a, **k: calls.append(name) or value)
        with mock.patch.object(CACHE.time, "time", clock), \
                mock.patch.object(NODES, "get_nodes", count("nodes", [])), \
                mock.patch.object(WORKLOADS, "get_workloads", count("wl", [])), \
                mock.patch.object(VM_LIST, "list_vms", count("vms", [])), \
                mock.patch.object(STORAGE, "get_volumes", count("vol", [])), \
                mock.patch.object(STORAGE, "get_storage", count("stor", {})):
            paths = ["/api/nodes", "/api/workloads", "/api/vms", "/api/volumes", "/api/storage"]
            for path in paths:
                self.route(path)(None)
            clock.now += 15
            for path in paths:
                self.route(path)(None)
        self.assertEqual(["nodes", "wl", "vms", "vol", "stor"], calls, "the second refresh is served from the cache")


class WriteDropsTests(unittest.TestCase):
    """A change Homestead makes to what those pages show is on the next refresh."""

    def setUp(self):
        CACHE._cache.clear()
        self.addCleanup(CACHE._cache.clear)
        for key in ("ov", "nodes", "wl", "vms", "vol", "stor", "node:a", "impact:a", "appstore:x"):
            CACHE._cache[key] = (time.time(), "kept")

    def write(self, method, path):
        with mock.patch.object(K, "WRITE_GUARDS", [K.fresh_reads]):
            return K.run_write_guards(method, path, {}, lambda: {"written": True})

    def test_a_write_to_an_app_drops_the_pages_answers(self):
        self.assertEqual({"written": True}, self.write("PATCH", "/apis/apps/v1/namespaces/lab/deployments/web"))
        self.assertEqual({"appstore:x"}, set(CACHE._cache))

    def test_a_write_to_a_vm_or_a_claim_drops_them_too(self):
        self.write("PUT", "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/vm-a")
        self.assertNotIn("vms", CACHE._cache)
        CACHE._cache["vms"] = (time.time(), "kept")
        self.write("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/data")
        self.assertNotIn("vol", CACHE._cache)

    def test_writes_to_longhorn_vm_actions_nodes_and_evictions_drop_them(self):
        for path in ("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-1",
                     "/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachines/vm-a/start",
                     "/api/v1/nodes/node1",
                     "/api/v1/namespaces/lab/pods/web-1/eviction",
                     "/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstancemigrations/m1",
                     "/api/v1/namespaces/lab"):
            CACHE._cache["ov"] = (time.time(), "kept")
            self.write("POST" if path.endswith(("start", "eviction")) else "PATCH", path)
            self.assertNotIn("ov", CACHE._cache, path)

    def test_a_lease_or_a_settings_write_keeps_them(self):
        self.write("PUT", "/apis/coordination.k8s.io/v1/namespaces/homestead/leases/homestead-leader")
        self.write("PUT", "/api/v1/namespaces/homestead/configmaps/homestead-settings")
        self.assertIn("ov", CACHE._cache)
        self.assertIn("wl", CACHE._cache)

    def test_a_refused_write_keeps_them(self):
        def refused():
            raise RuntimeError("409")
        with mock.patch.object(K, "WRITE_GUARDS", [K.fresh_reads]), self.assertRaises(RuntimeError):
            K.run_write_guards("PATCH", "/apis/apps/v1/namespaces/lab/deployments/web", {}, refused)
        self.assertIn("wl", CACHE._cache)


class HostOsTests(unittest.TestCase):
    def test_the_host_report_reads_the_platform_from_its_cache(self):
        import homestead_host_os as HOST_OS
        asked = []
        old = (HOST_OS.kget, HOST_OS.hostrun, HOST_OS.platform, HOST_OS.DATA_DIR)
        self.addCleanup(lambda: HOST_OS.bind(*old))
        HOST_OS.bind(lambda path: {"items": []}, None,
                     lambda force=False: asked.append(force) or {"distribution": "k3s"}, "/nonexistent")
        with mock.patch.object(HOST_OS, "_load", lambda: {}):
            HOST_OS.report()
        self.assertEqual([False], asked, "no forced re-detect on every host read")


if __name__ == "__main__":
    unittest.main()
