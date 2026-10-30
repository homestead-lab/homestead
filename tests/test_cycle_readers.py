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


if __name__ == "__main__":
    unittest.main()
