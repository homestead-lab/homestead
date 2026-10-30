"""The node probes are read in the background, so the pages never wait on
one (#705)."""
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_kube as K
import homestead_kubelet_stats as KSTATS
import homestead_names as NAMES
import homestead_node_probes as PROBES
import homestead_nodes as NODES
import homestead_ports as PORTS
from fakes.kube import Kube


def pod(node, ip, phase="Running"):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"namespace": "lab", "name": f"probe-{node}", "uid": f"uid-{node}",
                         "labels": {"app": NAMES.NODEPROBE}},
            "spec": {"nodeName": node}, "status": {"phase": phase, "podIP": ip}}


PODS = [pod("node1", "192.0.2.1"), pod("node2", "192.0.2.2"), pod("node3", "192.0.2.3")]
PROBE_PODS = "/api/v1/namespaces/lab/pods"


class Cluster:
    """The probe pods on the shared fake API (tests/fakes/kube.py), and every
    host's probe."""

    def __init__(self, test, delay=0.0):
        self.kube = test.enterContext(Kube(*PODS))
        self.list_error = None
        self.kube.react("GET", PROBE_PODS, self.listing)
        self.delay, self.smart_delay, self.down, self.smart_down, self.calls = delay, 0.0, set(), set(), []
        self.smart_rows = [{"name": "sda", "passed": True}]

    def listing(self, method, path, body):
        if self.list_error:
            raise self.list_error

    def use_pods(self, *pods):
        """These are the probe pods from now on."""
        for row in K.kget(PROBE_PODS)["items"]:
            self.kube.discard(f"{PROBE_PODS}/{row['metadata']['name']}")
        for item in pods:
            self.kube.add(item)

    def urlopen(self, url, timeout=None):
        url = getattr(url, "full_url", url)      # the SMART read is a signed Request
        self.calls.append(url)
        ip, port = url.split("//")[1].rstrip("/").split(":")
        if port == "9100":
            time.sleep(self.smart_delay)
            if ip in self.smart_down:
                raise OSError("timed out")
            return io.BytesIO(json.dumps({"disks": self.smart_rows}).encode())
        time.sleep(self.delay)
        if ip in self.down:
            raise OSError("timed out")
        return io.BytesIO(json.dumps({"cpu_c": 41, "disks": [{"name": "sda"}], "devices": {"igpu": True}}).encode())


class Base(unittest.TestCase):
    def setUp(self):
        PROBES.stop()
        PROBES.reset()
        KSTATS.reset()
        self.addCleanup(PROBES.stop)
        self.addCleanup(PROBES.reset)
        self.addCleanup(KSTATS.reset)      # start_probes() starts the kubelet loop too (#976)
        # start_probes() starts the metrics-server loop too (#906).
        import homestead_kube_metrics as KUBE_METRICS
        self.addCleanup(KUBE_METRICS.reset)
        self.addCleanup(KUBE_METRICS.stop)
        self.cluster = Cluster(self)
        store = tempfile.TemporaryDirectory()          # the SMART readings kept on the data volume
        self.addCleanup(store.cleanup)
        for patch in (mock.patch.object(PROBES.NAMES, "kget", K.kget, create=True),
                      mock.patch.object(PROBES.urllib.request, "urlopen", self.cluster.urlopen),
                      mock.patch.object(PROBES.SMART.SHARED, "DIR", store.name)):
            patch.start()
            self.addCleanup(patch.stop)

    def misses(self, node):
        return PROBES.status()[node]["misses"]

    def unreachable(self, node_ip):
        """node1..3 read at 1000; then node_ip misses three rounds."""
        PROBES.run_round(now=1000)
        self.cluster.down = {node_ip}
        for now in (1015, 1030, 1045):
            PROBES.run_round(now=now)


class ProbeTests(Base):
    def test_hosts_are_read_at_the_same_time(self):
        self.cluster.delay = self.cluster.smart_delay = 0.3
        started = time.monotonic()
        PROBES.run_round()
        self.assertLess(time.monotonic() - started, 1.2, "three hosts at 0.3 s each for both reads, in turn, take 1.8 s")
        self.assertEqual({"node1", "node2", "node3"}, set(PROBES.node_temps(wait=False)))

    def test_with_the_loop_running_asking_never_waits_on_a_slow_probe(self):
        PROBES.run_round()
        self.cluster.delay = 1.0
        PROBES.start("lab")
        time.sleep(0.05)
        started = time.monotonic()
        temps = PROBES.node_temps(wait=False)
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(41, temps["node1"]["cpu_c"])

    def test_temperatures_are_recorded_before_smart_is_read(self):
        self.cluster.smart_delay = 1.0
        worker = threading.Thread(target=PROBES.run_round)
        worker.start()
        self.addCleanup(worker.join)
        self.assertTrue(PROBES._round["done"].wait(0.6), "the round's readings must not wait on SMART")
        self.assertEqual({"node1", "node2", "node3"}, set(PROBES.node_temps(wait=False)))

    def test_a_host_not_read_yet_is_waiting_and_left_out_of_the_readings(self):
        self.cluster.down = {"192.0.2.2"}
        PROBES.run_round()
        self.assertNotIn("node2", PROBES.node_temps(wait=False))
        self.assertEqual("waiting", PROBES.status()["node2"]["state"])
        self.assertEqual("ok", PROBES.status()["node1"]["state"])

    def test_misses_keep_the_last_reading_as_stale_and_three_make_it_unreachable(self):
        PROBES.run_round(now=1000)
        self.cluster.down = {"192.0.2.2"}
        PROBES.run_round(now=1015)
        state = PROBES.status(now=1015)["node2"]
        self.assertEqual(("stale", 1, 15), (state["state"], state["misses"], state["age_s"]))
        self.assertEqual(41, PROBES.node_temps(wait=False)["node2"]["cpu_c"], "a stale reading is still used")
        PROBES.run_round(now=1030)
        PROBES.run_round(now=1045)
        state = PROBES.status(now=1045)["node2"]
        self.assertEqual(("unreachable", 3, 45), (state["state"], state["misses"], state["age_s"]))

    def test_an_unreachable_hosts_reading_is_left_out_unless_asked_for(self):
        self.unreachable("192.0.2.2")
        self.assertNotIn("node2", PROBES.node_temps(wait=False), "too old to act on")
        self.assertEqual(41, PROBES.node_temps(wait=False, unreachable=True)["node2"]["cpu_c"], "shown, with its age")

    def test_an_answer_after_misses_resets_the_count(self):
        self.unreachable("192.0.2.2")
        self.cluster.down = set()
        PROBES.run_round(now=1060)
        self.assertEqual(("ok", 0), (PROBES.status()["node2"]["state"], self.misses("node2")))

    def test_a_listing_that_fails_keeps_every_reading_and_counts_no_miss(self):
        PROBES.run_round(now=1000)
        self.cluster.list_error = OSError("the API server did not answer")
        with self.assertRaises(OSError):
            PROBES.run_round(now=1015)
        self.assertEqual({"node1", "node2", "node3"}, set(PROBES.node_temps(wait=False)))
        self.assertEqual(0, self.misses("node1"))

    def test_a_probe_pod_being_replaced_keeps_its_hosts_reading(self):
        PROBES.run_round(now=1000)
        self.cluster.use_pods(PODS[0], pod("node2", "", phase="Pending"), PODS[2])
        PROBES.run_round(now=1015)
        self.assertEqual(("ok", 0), (PROBES.status()["node2"]["state"], self.misses("node2")))
        self.cluster.use_pods(PODS[0], PODS[2])
        for now in (1030, 1045, 1060):
            PROBES.run_round(now=now)
        self.assertIn("node2", PROBES.status(), "kept while its pod is replaced")
        PROBES.run_round(now=1075)
        self.assertNotIn("node2", PROBES.status(), "gone after four listings without it: no probe there")

    def test_smart_is_read_in_the_same_loop_and_kept_between_its_reads(self):
        PROBES.run_round(now=1000)
        PROBES.run_round(now=1015)
        self.assertEqual(3, len([url for url in self.cluster.calls if ":9100/" in url]),
                         "SMART is read once per host, not every round")
        disk = PROBES.node_temps(wait=False)["node1"]["disks"][0]
        self.assertTrue(disk["smart"]["passed"])
        self.assertTrue(PROBES.node_temps(wait=False)["node1"]["smart_helper"]["available"])

    def test_a_smart_helper_with_no_drives_is_available(self):
        self.cluster.smart_rows = []
        PROBES.run_round(now=1000)
        self.assertEqual({"available": True, "disks": 0}, PROBES.node_temps(wait=False)["node1"]["smart_helper"])

    def test_a_smart_read_that_fails_keeps_the_last_rows_marked_stale_and_retries_sooner(self):
        PROBES.run_round(now=1000)
        self.cluster.smart_down = {"192.0.2.1"}
        PROBES.run_round(now=1000 + PROBES.SMART_EVERY)
        self.assertEqual({"available": True, "disks": 1, "stale": True},
                         PROBES.node_temps(wait=False)["node1"]["smart_helper"])
        self.assertTrue(PROBES.node_temps(wait=False)["node1"]["disks"][0]["smart"]["passed"])
        reads = len([url for url in self.cluster.calls if "192.0.2.1:9100" in url])
        PROBES.run_round(now=1000 + PROBES.SMART_EVERY + PROBES.SMART_RETRY)
        self.assertEqual(reads + 1, len([url for url in self.cluster.calls if "192.0.2.1:9100" in url]),
                         "tried again after a minute, not five")

    def test_one_round_at_a_time(self):
        self.cluster.down = {"192.0.2.2"}
        self.cluster.delay = 0.3
        callers = [threading.Thread(target=PROBES.run_round) for _ in range(2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join()
        self.assertEqual(1, self.misses("node2"), "the second caller waited for the round, not counted a miss again")

    def test_the_time_an_answer_arrived_is_when_it_was_read(self):
        with mock.patch.object(PROBES.time, "time", return_value=5000):
            PROBES.run_round(now=1000)
        self.assertEqual(5000, PROBES.node_temps(wait=False)["node1"]["numa_source"]["received_at"])

    def test_without_the_loop_a_caller_reads_at_most_every_interval(self):
        PROBES.node_temps()
        PROBES.node_temps()
        self.assertEqual(3, len([url for url in self.cluster.calls if ":9099/" in url]))

    def test_the_loop_keeps_going_after_a_round_fails(self):
        self.cluster.list_error = OSError("the API server did not answer")
        with mock.patch.object(PROBES, "INTERVAL", 0.05):
            PROBES.start("lab")
            time.sleep(0.15)
            self.cluster.list_error = None
            deadline = time.monotonic() + 2
            while not PROBES.node_temps(wait=False) and time.monotonic() < deadline:
                time.sleep(0.05)
            PROBES.stop()
        self.assertEqual({"node1", "node2", "node3"}, set(PROBES.node_temps(wait=False)))


class NodesTests(Base):
    """The nodes module answers from the background state."""

    def test_the_metrics_loop_start_probes_starts_is_stopped_after_the_test(self):
        # start_probes() also starts the metrics-server loop (#906); left
        # running, it would answer get_nodes() in every later test of the shard.
        import homestead_kube_metrics as KUBE_METRICS
        inner = NodesTests("test_node_temps_with_three_slow_probes_answers_at_once_once_the_loop_runs")
        self.assertTrue(inner.run().wasSuccessful())
        self.assertFalse(KUBE_METRICS.running())

    def test_node_temps_with_three_slow_probes_answers_at_once_once_the_loop_runs(self):
        self.cluster.delay = 1.0
        NODES.start_probes()
        NODES.node_temps()                       # the first round
        started = time.monotonic()
        NODES.node_temps(wait=False)
        took = time.monotonic() - started
        PROBES.stop()
        self.assertLess(took, 0.05, "three hosts whose probe takes 1 s each must not hold the page up")

    def test_ports_report_says_an_unreachable_hosts_ports_are_unknown(self):
        self.unreachable("192.0.2.2")
        PROBES._round["read"] = time.monotonic()           # answer from the state, as with the loop
        seen = {}
        for name in ("node1", "node2", "node3"):    # on the same fake, so the probe pods stay readable
            self.cluster.kube.add({"apiVersion": "v1", "kind": "Node", "metadata": {"name": name}})
        with mock.patch.object(NODES, "kget", K.kget), \
                mock.patch.object(PORTS, "report", lambda probes, contexts: seen.update(probes) or {}):
            NODES.ports_report()
        self.assertIsNone(seen["node2"], "not answering is unknown, not quietly well")
        self.assertIsNotNone(seen["node1"])


def node(name):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": {}, "annotations": {}}, "spec": {},
            "status": {"capacity": {"cpu": "4", "memory": "8Gi"}, "conditions": [{"type": "Ready", "status": "True"}],
                       "nodeInfo": {}, "addresses": [], "allocatable": {}}}


class GetNodesTests(Base):
    """Each host in /api/nodes says what its probe last did."""

    def answer(self, names):
        for name in names:    # on the same fake, so the probe pods stay readable
            self.cluster.kube.add(node(name))
        with mock.patch.multiple(NODES, kget=K.kget, get_app_settings=lambda: {}, DEFAULT_APP_SETTINGS={"smart": {}},
                                 parse_cpu=lambda value: 0.0, parse_mem=lambda value: 1.0, SYS_NS=set(),
                                 node_duties=lambda pods: {}, node_stats=lambda name: {},
                                 workload_names=lambda pods, system: ([], [])), \
                mock.patch.object(NODES.DISKS, "summary", lambda: {}), \
                mock.patch.object(NODES.HOST_OS, "report", lambda: {"hosts": {}}), \
                mock.patch.object(NODES.HW, "reconcile_node", lambda name, labels, notes, found, definitions=None: (labels, set())), \
                mock.patch.object(NODES.HW, "inventory",
                                  lambda labels, found, auto, definitions=None: [{"id": "igpu", "available": False, "detected": False}]):
            PROBES._round["read"] = time.monotonic()       # answer from the state, as with the loop
            return {n["name"]: n for n in NODES.get_nodes()}

    def test_each_state_and_a_host_without_a_probe(self):
        now = time.time()
        PROBES.run_round(now=now - 60)
        self.cluster.down = {"192.0.2.2"}
        for ago in (45, 30, 15):
            PROBES.run_round(now=now - ago)
        self.cluster.down = {"192.0.2.2", "192.0.2.3"}
        PROBES.run_round(now=now)
        nodes = self.answer(("node1", "node2", "node3", "node4"))
        self.assertEqual("ok", nodes["node1"]["probe"]["state"])
        self.assertEqual(("unreachable", 4), (nodes["node2"]["probe"]["state"], nodes["node2"]["probe"]["misses"]))
        self.assertEqual(41, nodes["node2"]["temps"]["cpu_c"], "the last reading is shown, with its age")
        self.assertEqual("stale", nodes["node3"]["probe"]["state"])
        self.assertEqual({"state": "absent"}, nodes["node4"]["probe"], "no probe pod on this host")
        self.assertIsNone(nodes["node4"]["temps"])

    def test_no_probe_pods_at_all(self):
        self.cluster.use_pods()
        PROBES.run_round()
        nodes = self.answer(("node1",))
        self.assertEqual({"state": "absent"}, nodes["node1"]["probe"])
        self.assertIsNone(nodes["node1"]["temps"])


class FirstReadTests(unittest.TestCase):
    """The first read of three slow probes: on dev it read one host after
    another, both reads each (this test fails there)."""

    def test_the_first_read_of_three_slow_probes_reads_them_at_the_same_time(self):
        PROBES.stop()
        PROBES.reset()
        self.addCleanup(PROBES.reset)
        cluster = Cluster(self, delay=0.4)
        cluster.smart_delay = 0.4
        with mock.patch.object(NODES.NAMES, "kget", K.kget, create=True), \
                mock.patch("urllib.request.urlopen", cluster.urlopen):
            started = time.monotonic()
            NODES.node_temps()
            took = time.monotonic() - started
        self.assertLess(took, 1.5, f"took {took:.1f} s; one host after another, both reads, is 2.4 s")
        self.assertEqual(3, len([url for url in cluster.calls if ":9099/" in url]), "all three probe pods were found and read")


if __name__ == "__main__":
    unittest.main()
