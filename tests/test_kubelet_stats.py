"""The Hosts page and the volume list read each kubelet's summary from one
background reading, all hosts at once, never one after another (#976)."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_kubelet_stats as KSTATS
import homestead_nodes as NODES
import homestead_node_probes as PROBES
import homestead_volume_usage as USAGE

NAMES = ("node-a", "node-b", "node-c")
GIB = 1024 ** 3


def node(name):
    return {"metadata": {"name": name, "labels": {}, "annotations": {}},
            "status": {"capacity": {"cpu": "4", "memory": "8Gi"}, "conditions": [{"type": "Ready", "status": "True"}],
                       "nodeInfo": {}, "addresses": [], "allocatable": {}}}


def summary(rx=0):
    return {"node": {"network": {"interfaces": [{"name": "eth0", "rxBytes": rx, "txBytes": rx}]},
                     "fs": {"usedBytes": 10 * GIB, "capacityBytes": 100 * GIB}},
            "pods": []}


class Cluster:
    """Nodes, and a kubelet summary per node that takes `delay` seconds."""

    def __init__(self, delay=0.0):
        self.delay, self.down, self.rx, self.calls = delay, set(), 1000, []
        self.release = threading.Event()
        self._lock = threading.Lock()

    def kget(self, path, timeout=None):
        if path == "/api/v1/nodes":
            return {"items": [node(name) for name in NAMES]}
        if path.endswith("/proxy/stats/summary"):
            name = path.split("/")[4]
            with self._lock:
                self.calls.append((name, timeout))
            if self.delay:
                self.release.wait(self.delay)
            if name in self.down:
                raise OSError("kubelet did not answer")
            return summary(self.rx)
        if path == "/api/v1/pods":
            return {"items": []}
        return {"items": []}


class Base(unittest.TestCase):
    def setUp(self):
        KSTATS.reset()
        PROBES.reset()
        self.addCleanup(KSTATS.reset)
        self.addCleanup(PROBES.reset)

    def get_nodes(self, cluster):
        with mock.patch.multiple(NODES, kget=cluster.kget, get_app_settings=lambda: {},
                                 DEFAULT_APP_SETTINGS={"smart": {}}, parse_cpu=lambda value: 0.0,
                                 parse_mem=lambda value: 1.0, SYS_NS=set(), node_duties=lambda pods, vols=None: {},
                                 workload_names=lambda pods, system: ([], [])), \
                mock.patch.object(NODES.DISKS, "summary", lambda: {}), \
                mock.patch.object(NODES.HOST_OS, "report", lambda: {"hosts": {}}), \
                mock.patch.object(NODES, "node_temps", lambda **kw: {}), \
                mock.patch.object(NODES.PROBES, "status", lambda: {}), \
                mock.patch.object(NODES.HW, "reconcile_node", lambda name, labels, notes, found: (labels, set())), \
                mock.patch.object(NODES.HW, "inventory",
                                  lambda labels, found, auto: [{"id": "igpu", "available": False, "detected": False}]):
            return {n["name"]: n for n in NODES.get_nodes()}

    def loop_running(self):
        """As with the loop started, without its thread."""
        return mock.patch.object(KSTATS, "running", lambda: True)


class InPlaceTests(Base):
    """Before the loop runs, a build reads every host at once."""

    def test_hosts_are_read_at_the_same_time_with_a_short_timeout(self):
        cluster = Cluster(delay=0.4)
        started = time.monotonic()
        nodes = self.get_nodes(cluster)
        took = time.monotonic() - started
        self.assertLess(took, 1.0, "three hosts at 0.4 s each were read one after another")
        self.assertEqual(sorted(NAMES), sorted(name for name, _ in cluster.calls))
        self.assertEqual({KSTATS.TIMEOUT}, {timeout for _, timeout in cluster.calls})
        self.assertLessEqual(KSTATS.TIMEOUT, 2)
        self.assertEqual(10.0, nodes["node-a"]["fs_pct"])
        self.assertEqual("eth0", nodes["node-a"]["net_iface"])


class BackgroundTests(Base):
    """With the loop running, a build never waits on a kubelet."""

    def test_a_build_does_not_wait_on_slow_kubelets(self):
        cluster = Cluster(delay=5.0)
        self.addCleanup(cluster.release.set)
        with self.loop_running():
            started = time.monotonic()
            nodes = self.get_nodes(cluster)
            took = time.monotonic() - started
        self.assertLess(took, 1.0, "the build waited on a kubelet")
        self.assertEqual([], cluster.calls, "nothing is read on the build's thread")
        self.assertNotIn("fs_pct", nodes["node-a"], "no reading yet: no figures")
        self.assertEqual(set(NAMES), set(KSTATS._wanted), "the loop reads the hosts asked about")

    def test_the_loop_reading_is_used_and_kept_stale_when_a_kubelet_stops(self):
        cluster = Cluster()
        with self.loop_running():
            self.get_nodes(cluster)
            KSTATS.run_round(cluster.kget, now=time.time() - 20)
            cluster.rx = 1000 + 20 * 1_000_000 // 8          # 1 Mbit/s for 20 s
            KSTATS.run_round(cluster.kget)
            cluster.calls.clear()
            nodes = self.get_nodes(cluster)
            self.assertEqual([], cluster.calls)
            self.assertEqual(1.0, nodes["node-a"]["rx_mbps"])
            self.assertFalse(nodes["node-a"]["stats_stale"])
            again = self.get_nodes(cluster)
            self.assertEqual(1.0, again["node-a"]["rx_mbps"], "asking twice for one reading keeps its rate")
            cluster.down = {"node-b"}
            KSTATS.run_round(cluster.kget)
            nodes = self.get_nodes(cluster)
        self.assertTrue(nodes["node-b"]["stats_stale"])
        self.assertEqual(10.0, nodes["node-b"]["fs_pct"], "the last reading is kept")
        self.assertFalse(nodes["node-a"]["stats_stale"])

    def test_a_reading_too_old_is_dropped(self):
        cluster = Cluster()
        with self.loop_running():
            self.get_nodes(cluster)
            KSTATS.run_round(cluster.kget, now=time.time() - KSTATS.DROP_AFTER - 1)
            nodes = self.get_nodes(cluster)
        self.assertNotIn("fs_pct", nodes["node-a"])

    def test_hosts_nobody_asks_about_are_not_read(self):
        cluster = Cluster()
        with self.loop_running():
            self.get_nodes(cluster)
        cluster.calls.clear()
        KSTATS.run_round(cluster.kget, now=time.time() + KSTATS.WANTED_FOR + 1)
        self.assertEqual([], cluster.calls)


class SharedReadingTests(Base):
    """The volume list uses the Hosts page's reading instead of its own."""

    def test_one_summary_read_per_host_for_both(self):
        cluster = Cluster()
        claims = {}
        with self.loop_running():
            self.get_nodes(cluster)
            KSTATS.run_round(cluster.kget)
            pods = {"items": [{"metadata": {"uid": f"pod-{n}", "namespace": "lab"},
                               "spec": {"nodeName": n, "volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]},
                               "status": {"phase": "Running"}} for n in NAMES]}
            claims = {("lab", "data"): {"metadata": {}, "spec": {}}}
            get = lambda path, **kw: pods if path == "/api/v1/pods" else cluster.kget(path, **kw)
            USAGE.collect(get, claims)
            self.get_nodes(cluster)
        self.assertEqual(len(NAMES), len(cluster.calls), "one summary read per host for the page and the volumes")


    def test_a_kubelet_that_missed_its_last_read_says_nothing_about_volumes(self):
        cluster = Cluster()
        pods = {"items": [{"metadata": {"uid": "pod-a", "namespace": "lab"},
                           "spec": {"nodeName": "node-a", "volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]},
                           "status": {"phase": "Running"}}]}
        claims = {("lab", "data"): {"metadata": {}, "spec": {}}}
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sample = {"pvcRef": {"namespace": "lab", "name": "data"}, "time": stamp, "usedBytes": GIB, "capacityBytes": 2 * GIB}
        plain = cluster.kget
        cluster.kget = lambda path, **kw: ({**plain(path, **kw), "pods": [{"podRef": {"uid": "pod-a"}, "volume": [sample]}]}
                                           if path.endswith("/stats/summary") else plain(path, **kw))
        get = lambda path, **kw: pods if path == "/api/v1/pods" else cluster.kget(path, **kw)
        self.assertIn(("lab", "data"), USAGE.collect(get, claims))
        cluster.down = {"node-a"}
        self.assertEqual({}, USAGE.collect(get, claims), "unknown, not the last reading")

if __name__ == "__main__":
    unittest.main()
