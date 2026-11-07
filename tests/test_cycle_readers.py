"""Readers of what the leader's page snapshot already reads (#978, #980): a
loop or route that asks for a page's key with a shorter cache time than the
cycle that refreshes it is served the cycle's part, not a read of its own; the
alerts loop reads the Harvester version alone, not a whole Cluster inventory;
the restart record lists ReplicaSets only when a pod names one it has not
seen; and the Cluster page asks the API server for Warnings only."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cache as CACHE
import homestead_kube as K
import homestead_restarts as RESTARTS
import homestead_routes as ROUTER
import homestead_shared as SHARED
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
        # The alert loop's twice-a-day update scan, as just made on this test's clock:
        # these tests are about the alert reads, and the loop writes the time of its
        # scan into _last_update_scan. Left at this clock's 1.8e9 it stopped every
        # later alert loop from scanning (test_alerts_unread_guard, reversed order).
        self.enterContext(mock.patch.object(self.OV, "_last_update_scan", [self.clock.now]))
        COMPUTED.clear()
        self.addCleanup(lambda: self.assertEqual([], COMPUTED, "nothing computed"))

    def cycle(self):
        return self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())

    def test_a_reader_asking_for_less_than_the_cycle_is_served_its_part(self):
        self.cycle()
        self.clock.now += 12      # past the 5-10 s these readers ask for
        for key in SNAPSHOT_KEYS:
            self.assertIs(self.PAGES.read(key)[1], CACHE.cached(key, 5, boom), key)

    def test_a_part_is_served_to_a_reader_only_within_its_own_bound(self):
        self.cycle()
        part = self.PAGES.read("wl")[1]
        self.clock.now += 15
        self.assertIs(part, CACHE.cached("wl", 5, boom), "within PAGE_SECONDS")
        # Three missed cycles' worth late: the page still shows the part (it
        # says how old), a reader that keeps 20 s reads for itself.
        self.clock.now += 10
        self.assertIsNotNone(self.PAGES.read("wl"))
        self.assertIs(part, ROUTER.page("wl", boom))
        computed = []
        CACHE.cached("wl", 5, lambda: computed.append(1) or [])
        self.assertEqual([1], computed)

    def test_a_capacity_check_sees_a_host_that_went_not_ready_since_the_cycle(self):
        # A start, scale or maintenance preflight (homestead_place) decides
        # on the hosts as they are, not the snapshot's: its own 5 s.
        self.cycle()
        shown = {row["name"]: row["status"] for row in self.PAGES.read("nodes")[1]}
        node = copy.deepcopy(self.cluster.kube.object("/api/v1/nodes/node-a"))
        node["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        self.cluster.kube.add(node)       # changed outside Homestead: nothing forgets
        self.clock.now += 6
        status = {row["name"]: row["status"] for row in self.cluster.server.PLACE.get_nodes()}
        self.assertNotEqual("Ready", status["node-a"])
        self.assertEqual("Ready", shown["node-a"], "the snapshot had it Ready")

    def test_a_host_s_impact_preview_reads_the_hosts_fresh(self):
        NODES = self.cluster.server.NODES
        self.cycle()
        self.clock.now += 6
        built = []
        real = NODES.get_nodes
        # The placement half reads its hosts through PLACE (fresh too): left
        # out, so this counts the preview's own read.
        with mock.patch.object(NODES, "get_nodes", lambda *a, **k: built.append(1) or real(*a, **k)), \
                mock.patch.object(NODES.PLACE, "impact", lambda node: None):
            try:
                NODES.node_impact("node-a")
            except Exception:
                pass                      # only what it read matters here
        self.assertTrue(built, "the hosts are built for the preview, not taken from the snapshot")

    def test_within_fresh_a_page_key_keeps_only_the_reader_s_time(self):
        self.cycle()
        self.clock.now += 6
        computed = []
        CACHE.fresh(lambda: CACHE.cached("wl", 5, lambda: computed.append(1) or []))()
        self.assertEqual([1], computed)
        # Its read is newer than the part, so it is what the next readers get.
        self.assertEqual([], CACHE.cached("wl", 5, boom))

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


class ClusterPageReadsTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.clock = Clock(self)
        self.cluster = Cluster(self)
        self.PAGES = clean_snapshot(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES
        import homestead_cluster as CLUSTER
        self.CLUSTER = CLUSTER
        stamp = "2026-10-29T10:00:00Z"
        self.cluster.kube.add({"apiVersion": "v1", "kind": "Pod",
                               "metadata": {"namespace": "kube-system", "name": "coredns-1", "labels": {"k8s-app": "coredns"}},
                               "spec": {"nodeName": "node-b", "containers": [{"name": "c", "image": "example.com/dns:1"}]},
                               "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                                          "containerStatuses": [{"name": "c", "ready": True, "restartCount": 0}]}})
        self.enterContext(mock.patch.object(CLUSTER, "_iso_age", lambda value, now=None: 60))
        for i, (kind, ns, typ) in enumerate((("Node", "default", "Warning"), ("Pod", "kube-system", "Warning"),
                                             ("Pod", "kube-system", "Normal"), ("Pod", "lab", "Warning"))):
            self.cluster.kube.add({"apiVersion": "v1", "kind": "Event",
                                   "metadata": {"namespace": ns, "name": f"ev-{i}", "creationTimestamp": stamp},
                                   "involvedObject": {"kind": kind, "name": f"x{i}", "namespace": ns},
                                   "reason": "BackOff", "message": "m", "type": typ, "lastTimestamp": stamp})

    def test_the_cluster_page_asks_for_warnings_only_and_shows_the_same_rows(self):
        every = self.CLUSTER._warning_rows(K.kget("/api/v1/events").get("items", []))
        self.cluster.calls.clear()
        queries = []
        inner = K.transport

        def seen(method, path, *args, **kwargs):
            queries.append(path)
            return inner(method, path, *args, **kwargs)
        with mock.patch.object(K, "transport", seen):
            report = self.CLUSTER.inventory()
        events = [q for q in queries if q.split("?", 1)[0] == "/api/v1/events"]
        self.assertEqual(["/api/v1/events?fieldSelector=type%3DWarning"], events)
        self.assertEqual(every, report["warnings"])
        self.assertEqual(2, len(report["warnings"]))

    def test_after_a_cycle_the_cluster_page_lists_no_pods_and_shows_the_same_services(self):
        before = self.CLUSTER.inventory()["services"]
        clean_cache(self)
        self.cycle = self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        self.cluster.calls.clear()
        self.clock.now += 10
        self.assertEqual(before, self.CLUSTER.inventory()["services"])
        self.assertEqual([], self.cluster.gets("/api/v1/pods"))

    def test_an_unread_pod_list_says_so_and_shows_no_old_services(self):
        self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        self.clock.now += self.CLUSTER.PODS_SECONDS + 1
        inner = K.transport

        def no_pods(method, path, *args, **kwargs):
            if path.split("?", 1)[0] == "/api/v1/pods":
                raise OSError("connection refused")
            return inner(method, path, *args, **kwargs)
        with mock.patch.object(K, "transport", no_pods):
            report = self.CLUSTER.inventory()
        self.assertIn("System services", [row["section"] for row in report["unavailable"]])
        self.assertEqual([0], [row["pods"] for row in report["services"] if row["id"] == "dns"])

    def test_a_pod_write_makes_the_cluster_page_list_pods_again(self):
        self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())
        dns = lambda: [row["pods"] for row in self.CLUSTER.inventory()["services"] if row["id"] == "dns"]  # noqa: E731
        self.assertEqual([1], dns(), "the system pod is shown")
        K.ksend("DELETE", "/api/v1/namespaces/kube-system/pods/coredns-1")
        self.assertIsNone(CACHE.kept_at("pods"), "the write forgets the cycle's pod list")
        self.assertEqual([0], dns(), "the next Cluster page lists pods again and shows it gone")


class RestartOwnersTests(unittest.TestCase):
    """The restart record maps a pod's ReplicaSet to its Deployment. The
    ReplicaSet list is the largest list Homestead reads (#980), and a
    ReplicaSet's controller is set when it is made."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(mock.patch.object(SHARED, "DIR", temp.name))
        self.clock = [1_800_000_000.0]
        self.enterContext(mock.patch.object(RESTARTS.time, "time", lambda: self.clock[0]))
        self.reads = []
        self.sets = {}

    def read(self, path):
        self.reads.append(path)
        if path == "/apis/apps/v1/replicasets":
            return {"items": [{"metadata": {"name": name, "namespace": "lab", "uid": f"rs-{name}", "ownerReferences": [
                {"kind": "Deployment", "name": dep, "controller": True, "uid": f"dep-{dep}"}]}}
                for name, dep in self.sets.items()]}
        if path == "/api/v1/nodes":
            return {"items": []}
        return {"items": []}

    def pod(self, name, rs, restarts=0):
        self.sets.setdefault(rs, rs.rsplit("-", 1)[0])
        return {"metadata": {"name": name, "uid": f"pod-{name}", "namespace": "lab",
                             "creationTimestamp": "2026-10-22T08:00:00Z", "labels": {},
                             "ownerReferences": [{"kind": "ReplicaSet", "name": rs, "controller": True,
                                                  "uid": f"rs-{rs}"}]},
                "spec": {"nodeName": "node-a", "containers": [{"name": "c", "image": "example.com/web:1"}]},
                "status": {"phase": "Running", "containerStatuses": [
                    {"name": "c", "restartCount": restarts, "image": "example.com/web:1", "state": {"running": {}}}]}}

    def look(self, *pods, after=20):
        self.clock[0] += after
        return RESTARTS.observe({"items": [copy.deepcopy(p) for p in pods]}, read=self.read,
                                tail=lambda ns, pod, c: {"text": ""}, outage=lambda: {}, sys_ns=frozenset())

    def lists(self):
        return self.reads.count("/apis/apps/v1/replicasets")

    def test_a_minute_of_looks_at_the_same_pods_lists_replicasets_once(self):
        web = self.pod("web-1", "web-5d9")
        for _ in range(4):
            self.look(web)
        self.assertEqual(1, self.lists())

    def test_a_pod_of_a_new_replicaset_lists_them_again(self):
        self.look(self.pod("web-1", "web-5d9"))
        self.look(self.pod("web-1", "web-5d9"), self.pod("web-2", "web-7c1"))
        self.assertEqual(2, self.lists())

    def test_whose_each_replicaset_is_is_read_again_now_and_then(self):
        web = self.pod("web-1", "web-5d9")
        self.look(web)
        self.look(web, after=RESTARTS.OWNERS_FOR + 1)
        self.assertEqual(2, self.lists())

    def test_restarts_are_still_credited_to_the_deployment(self):
        self.look(self.pod("web-1", "web-5d9"))
        self.look(self.pod("web-1", "web-5d9", restarts=1))
        self.assertEqual(1, self.lists())
        apps = SHARED.read_json(RESTARTS._path(), {}).get("apps") or {}
        self.assertTrue(any("/Deployment/web/" in key for key in apps), apps)

    def test_a_failed_list_still_skips_the_look(self):
        def failing(path):
            raise OSError("connection refused")
        self.clock[0] += 20
        self.assertEqual(0, RESTARTS.observe({"items": [self.pod("web-1", "web-5d9")]}, read=failing,
                                             tail=lambda *a: {}, outage=lambda: {}, sys_ns=frozenset()))


# The alerts loop sleeps 20 s after each look, so a look comes every 20 s plus
# what the look itself took.
LOOK = 20


class AlertReadingsTests(unittest.TestCase):
    """The alert sources that keep a reading of their own (addresses, disks,
    ports, CronJobs) take one made since the previous look began: each serves
    its own look and at most the next one (#978)."""

    def setUp(self):
        clean_cache(self)
        self.clock = Clock(self)
        self.cluster = Cluster(self)
        clean_snapshot(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(mock.patch.object(self.OV.ALERTS, "DATA_DIR", folder.name))
        self.enterContext(mock.patch.object(self.OV.CHECKS, "report", lambda: None))
        self.enterContext(mock.patch.object(self.OV, "_looked", [0.0]))
        # The Dashboard's build reads addresses and disks for the host cards
        # with times of its own: not these readings.
        self.enterContext(mock.patch.object(self.OV, "get_overview", lambda: {}))
        self.reads = {}
        self.disk_ready = True

        def counted(name, value):
            def read(*args, **kwargs):
                self.reads[name] = self.reads.get(name, 0) + 1
                return value()
            return read
        disks = lambda: {"nodes": {"node-a": [{"longhorn": [  # noqa: E731
            {"id": "disk-1", "path": "/var/lib/longhorn", "ready": self.disk_ready}]}]}}
        for module, name, value in ((self.OV.INV, "inventory", lambda: {"addresses": {"addresses": []}}),
                                    (self.OV.DISKS, "inventory", disks),
                                    (self.OV.NODES, "ports_report", lambda: {}),
                                    (self.OV.CRONJOBS, "list_jobs", lambda: [])):
            self.enterContext(mock.patch.object(module, name, counted(f"{module.__name__}.{name}", value)))

    def look(self, took=1):
        self.clock.now += LOOK + took
        return self.OV._alert_sources()

    def test_four_looks_read_each_source_twice(self):
        for _ in range(4):
            self.look()
        self.assertEqual({"homestead_network_inventory.inventory": 2, "homestead_disks.inventory": 2,
                          "homestead_nodes.ports_report": 2, "homestead_scheduled_jobs.list_jobs": 2}, self.reads)

    def test_a_reading_never_serves_a_third_look_however_long_a_look_takes(self):
        for took in range(0, 10):
            clean_cache(self)
            self.reads.clear()
            for _ in range(3):
                self.look(took)
            self.assertEqual({2}, set(self.reads.values()), f"a look taking {took} s")

    def raised_and_resolved(self, fresh, page_first=False):
        """The looks at which a disk going not ready is raised and, once it is
        ready again, resolved. fresh reads again on every look, as each of these
        sources did while it kept its reading for less than a look. page_first:
        the Disks page reads them a second before the look, and the disk
        changes between the two."""
        def look(change=None):
            if page_first and change is not None:
                self.clock.now += LOOK           # the look comes a second later
                self.cluster.server.MODULE_ROUTES.table()[("GET", "/api/disks")].handler(None)
                self.clock.now -= LOOK
            if fresh:
                ROUTER.forget("network", "disks", "ports", "cron")
            if change is not None:
                self.disk_ready = change
            return [e["phase"] for e in self.OV.ALERTS.observe(self.look(), now=self.clock.now)
                    if e["key"] == "disks:node-a:disk-1"]
        self.disk_ready = True
        look()
        look()
        raised = next(n for n in range(1, 20) if "raised" in look(False if n == 1 else None))
        resolved = next(n for n in range(1, 20) if "resolved" in look(True if n == 1 else None))
        return raised, resolved

    def assert_at_most_one_look_later(self, page_first=False):
        fresh = self.raised_and_resolved(fresh=True, page_first=page_first)
        clean_cache(self)
        Path(self.OV.ALERTS._path()).unlink()
        kept = self.raised_and_resolved(fresh=False, page_first=page_first)
        for phase, now, then in zip(("raised", "resolved"), kept, fresh):
            self.assertLessEqual(now - then, 1, f"{phase}: after {now} looks, against {then}")
        # The minute's hold takes three or four looks.
        self.assertGreaterEqual(fresh[0], self.OV.ALERTS.HOLD // (LOOK + 1))

    def test_a_disk_alert_is_raised_and_resolved_at_most_one_look_later(self):
        self.assert_at_most_one_look_later()

    def test_also_when_a_page_read_the_disks_just_before_the_look(self):
        # A reading kept a fixed 30 s would serve this look and the next:
        # two looks late.
        self.assert_at_most_one_look_later(page_first=True)


class PageRouteKeepTests(unittest.TestCase):
    """Data protection, Helm, Flow and Disks refresh every 15 s: a reading is
    kept PAGE_SECONDS, so every other refresh is served from the last one, and
    a write Homestead makes to what the page shows drops it at once (#978)."""

    ROUTES = (("/api/lh/overview", "homestead_longhorn", "overview", "lhov"),
              ("/api/helm", "homestead_helm", "releases", "helm"),
              ("/api/flow", "homestead_flow", "get_flow2", "flow2"),
              ("/api/disks", "homestead_disks", "inventory", "disks"))

    def setUp(self):
        clean_cache(self)
        self.clock = Clock(self)
        self.cluster = Cluster(self)
        clean_snapshot(self)
        self.table = self.cluster.server.MODULE_ROUTES.table()

    def get(self, path, module, name, read=lambda: {}):
        with mock.patch.object(sys.modules[module], name, read):
            return self.table[("GET", path)].handler(None)

    def test_a_page_refresh_is_served_the_last_reading_and_the_next_one_reads(self):
        for path, module, name, _key in self.ROUTES:
            reads = []
            for _ in range(3):
                self.get(path, module, name, lambda: reads.append(1) or {})
                self.clock.now += 15
            self.assertEqual(2, len(reads), path)

    def test_none_is_kept_longer_than_the_main_pages(self):
        for path, module, name, key in self.ROUTES:
            self.get(path, module, name)
            self.clock.now += ROUTER.PAGE_SECONDS
            self.get(path, module, name)
            self.assertEqual(self.clock.now, CACHE.kept_at(key), path)

    def test_a_write_to_an_app_service_or_volume_drops_the_flow_and_data_protection_pictures(self):
        for path in ("/apis/apps/v1/namespaces/lab/deployments/web",
                     "/api/v1/namespaces/lab/services/web",
                     "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-1"):
            self.get("/api/flow", "homestead_flow", "get_flow2")
            self.get("/api/lh/overview", "homestead_longhorn", "overview")
            K.fresh_reads("PATCH", path, {}, lambda: None)
            self.assertEqual((None, None), (CACHE.kept_at("flow2"), CACHE.kept_at("lhov")), path)

    def test_a_new_backup_target_drops_data_protection(self):
        import homestead_longhorn as LH
        self.get("/api/lh/overview", "homestead_longhorn", "overview")
        LH._bust("lhtarget")
        self.assertIsNone(CACHE.kept_at("lhov"))

    def helm_job(self, status):
        self.cluster.kube.add({"apiVersion": "batch/v1", "kind": "Job", "status": status,
                               "metadata": {"namespace": "kube-system", "name": "helm-install-web"}})
        self.get("/api/helm", "homestead_helm", "releases")
        return self.cluster.server.OPS.RESOLVERS["helm"]({"ref": {"namespace": "kube-system", "name": "helm-install-web"}})

    def test_a_helm_job_that_ends_drops_the_helm_page(self):
        failed = {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}
        for status in ({"succeeded": 1}, failed):
            self.helm_job(status)
            self.assertIsNone(CACHE.kept_at("helm"), status)

    def test_a_helm_job_still_running_keeps_the_helm_page(self):
        self.helm_job({"active": 1})
        self.assertIsNotNone(CACHE.kept_at("helm"))


class WhatDropsTheKeptPagesTests(unittest.TestCase):
    """Each change Homestead makes to what Data protection, Flow, Helm or
    Disks show drops the reading those pages keep PAGE_SECONDS (#978)."""

    def setUp(self):
        clean_cache(self)
        self.clock = Clock(self)
        self.cluster = Cluster(self)
        clean_snapshot(self)
        self.table = self.cluster.server.MODULE_ROUTES.table()
        self.OPS = self.cluster.server.OPS

    def keep(self, *keys):
        for key in keys:
            CACHE.cached(key, ROUTER.PAGE_SECONDS, lambda: {})

    def dropped(self, *keys):
        return all(CACHE.kept_at(key) is None for key in keys)

    def test_moving_or_switching_off_the_weekly_trim_drops_data_protection(self):
        import homestead_weekly_trim as WT
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(mock.patch.object(WT, "DATA_DIR", folder.name))
        for body in ({"weekday": 2, "hour": 22}, {"enabled": False}):
            self.keep("lhov")
            WT.settings(body)
            self.assertTrue(self.dropped("lhov"), body)

    def test_leaving_a_volume_out_of_the_weekly_trim_drops_data_protection(self):
        import homestead_weekly_trim as WT
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(mock.patch.object(WT, "DATA_DIR", folder.name))
        self.keep("lhov")
        WT.leave_out({"volume": "pvc-1", "left_out": False})
        self.assertTrue(self.dropped("lhov"))

    def test_a_write_to_a_longhorn_node_drops_the_disks_page(self):
        self.keep("disks")
        K.fresh_reads("PATCH", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/node-a", {}, lambda: None)
        self.assertTrue(self.dropped("disks"))

    def test_starting_a_disk_job_drops_the_disks_page(self):
        import homestead_disk_allocation as ALLOCATION
        import homestead_disk_v2 as DISK_V2
        for module, name, path in ((DISK_V2, "start", "/api/disks/v2/start"),
                                   (DISK_V2, "prepare", "/api/disks/v2/prepare"),
                                   (ALLOCATION, "start", "/api/disks/allocation/start"),
                                   (ALLOCATION, "finish", "/api/disks/allocation/finish")):
            self.keep("disks")
            with mock.patch.object(module, name, lambda body: {"ok": True}):
                self.table[("POST", path)].handler(mock.Mock(body={}))
            self.assertTrue(self.dropped("disks"), path)

    def ended(self, kind, keys, answer=("succeeded", 100, "Done")):
        resolver = self.OPS.RESOLVERS[kind]
        inner = getattr(resolver, "__wrapped__", None)
        self.assertIsNotNone(inner, f"{kind}: its resolver drops nothing when its job ends")
        with mock.patch.object(resolver, "__wrapped__", lambda item: answer):
            self.keep(*keys)
            resolver({"id": "j1", "kind": kind, "ref": {}})
        return self.dropped(*keys)

    def test_a_disk_job_that_ends_drops_the_disks_page(self):
        for kind in ("disk-retire", "disk-v2-convert", "disk-allocation", "longhorn-v2-prepare"):
            self.assertTrue(self.ended(kind, ("disks",)), kind)
            self.assertFalse(self.ended(kind, ("disks",), ("running", 50, "Working")), kind)

    def test_a_job_that_changes_helm_releases_drops_the_helm_page_when_it_ends(self):
        for kind in ("helm", "multus", "platform-upgrade"):
            for status in ("succeeded", "failed"):
                self.assertTrue(self.ended(kind, ("helm",), (status, 100, "Done")), (kind, status))
            self.assertFalse(self.ended(kind, ("helm",), ("running", 40, "Helm is running")), kind)

    def test_a_macvtap_chart_change_drops_the_helm_page(self):
        import homestead_macvtap as MACVTAP
        sent = []
        chart = {"metadata": {"name": MACVTAP.CHART}, "spec": {}}
        with mock.patch.object(MACVTAP, "kget", lambda path: copy.deepcopy(chart)), \
                mock.patch.object(MACVTAP, "ksend", lambda *a, **k: sent.append(a)), \
                mock.patch.object(MACVTAP, "platform", lambda *a: {}), \
                mock.patch.object(MACVTAP, "addons", mock.Mock(chart_archive=lambda *a: "")), \
                mock.patch.object(MACVTAP, "manifests", lambda *a: ""):
            self.keep("helm")
            MACVTAP.upgrade("v1.2.3")
        self.assertTrue(sent)
        self.assertTrue(self.dropped("helm"))

    def test_a_pop_of_the_addresses_under_the_cache_s_lock_does_not_hang(self):
        # The VIP loop pops "network" holding the cache's lock (a plain Lock):
        # a pop listener must not take it again. A lock of the test's own, so
        # a hang here leaves the real one free.
        import threading
        self.enterContext(mock.patch.object(CACHE, "_lock", threading.Lock()))
        self.keep("network", "flow2")

        def vip_loop():
            with CACHE._lock:
                CACHE._cache.pop("network", None)
        thread = threading.Thread(target=vip_loop, daemon=True)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive(), "the pop waited on the lock its own thread holds")
        self.assertIsNone(CACHE.kept_at("flow2"))

    def test_a_change_of_addresses_drops_the_flow_picture_built_from_them(self):
        self.keep("network", "flow2")
        ROUTER.forget("network")
        self.assertTrue(self.dropped("flow2"), "a forget")
        self.keep("network", "flow2")
        CACHE._cache.pop("network", None)
        self.assertTrue(self.dropped("flow2"), "a pop")


if __name__ == "__main__":
    unittest.main()
