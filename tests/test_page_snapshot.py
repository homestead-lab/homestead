"""The leader's page snapshot (#705 step 4b): one cycle on the leader reads
the cluster once for the Dashboard, Hosts, Apps, Volumes and storage pages,
and their routes and the display loops serve that instead of computing."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cache as CACHE
import homestead_kube as K
from fakes.kube import Kube
import homestead_restarts as RESTARTS


def setUpModule():
    # The restart record's own thread would write restarts.json beside these
    # tests; the two tests about it patch it themselves.
    patch = mock.patch.object(RESTARTS, "observe_later", lambda pods: True)
    patch.start()
    unittest.addModuleCleanup(patch.stop)

GiB = 1024 ** 3
LH = "longhorn.io/v1beta2"
ROUTES = {"ov": "/api/overview", "nodes": "/api/nodes", "wl": "/api/workloads",
          "vol": "/api/volumes", "stor": "/api/storage", "lhcap": "/api/longhorn/capacity"}


def objects():
    """A small cluster: two hosts, an app with a claim on a Longhorn volume,
    a system pod, and Longhorn's disks and settings."""
    node = lambda name, ip: {
        "apiVersion": "v1", "kind": "Node",
        "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name},
                     "creationTimestamp": "2020-01-01T00:00:00Z"},
        "status": {"capacity": {"cpu": "4", "memory": "8Gi", "pods": "110"},
                   "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"},
                   "conditions": [{"type": "Ready", "status": "True"}],
                   "addresses": [{"type": "InternalIP", "address": ip}],
                   "nodeInfo": {"kubeletVersion": "v1.30.0", "osImage": "Example OS"}}}
    pod = lambda ns, name, host, owner=None, claim=None: {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"namespace": ns, "name": name, "labels": {"app": "web"},
                     "creationTimestamp": "2020-01-01T00:00:00Z",
                     "ownerReferences": [{"kind": "ReplicaSet", "name": owner, "uid": "rs1"}] if owner else []},
        "spec": {"nodeName": host,
                 "containers": [{"name": "c", "image": "example.com/web:1",
                                 "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}],
                 "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}] if claim else []},
        "status": {"phase": "Running", "containerStatuses": [{"name": "c", "ready": True, "restartCount": 0}]}}
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"namespace": "lab", "name": "web", "creationTimestamp": "2020-01-01T00:00:00Z"},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}},
                 "template": {"metadata": {"labels": {"app": "web"}},
                              "spec": {"containers": [{"name": "c", "image": "example.com/web:1"}]}}},
        "status": {"replicas": 1, "readyReplicas": 1, "availableReplicas": 1}}
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"namespace": "lab", "name": "data"},
             "spec": {"volumeName": "pvc-1", "storageClassName": "longhorn",
                      "resources": {"requests": {"storage": "5Gi"}}},
             "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}
    volume = {"apiVersion": LH, "kind": "Volume",
              "metadata": {"namespace": "longhorn-system", "name": "pvc-1"},
              "spec": {"size": str(5 * GiB), "numberOfReplicas": 2},
              "status": {"state": "attached", "robustness": "healthy", "actualSize": GiB,
                         "currentNodeID": "node-a",
                         "kubernetesStatus": {"namespace": "lab", "pvcName": "data", "pvName": "pvc-1"}}}
    lh_node = lambda name: {
        "apiVersion": LH, "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": name},
        "spec": {"allowScheduling": True, "disks": {"d1": {"path": "/var/lib/longhorn", "allowScheduling": True,
                                                            "storageReserved": 10 * GiB}}},
        "status": {"diskStatus": {"d1": {"storageMaximum": 100 * GiB, "storageAvailable": 60 * GiB,
                                         "storageScheduled": 20 * GiB,
                                         "conditions": [{"type": "Schedulable", "status": "True"}]}}}}
    setting = lambda name, value: {"apiVersion": LH, "kind": "Setting",
                                   "metadata": {"namespace": "longhorn-system", "name": name}, "value": value}
    return [node("node-a", "192.0.2.10"), node("node-b", "192.0.2.11"),
            pod("lab", "web-1", "node-a", owner="web-5d9", claim="data"),
            pod("kube-system", "dns-1", "node-b"), deployment, claim, volume,
            lh_node("node-a"), lh_node("node-b"),
            setting("storage-over-provisioning-percentage", "100"),
            setting("storage-minimal-available-percentage", "25")]


class Cluster:
    """The fake cluster behind every module's kget, counting each request."""

    def __init__(self, test):
        import server   # binds every module to homestead_kube's kget
        self.server = server
        self.calls = []
        self.kube = test.enterContext(Kube(*objects()))
        # Other test files bind homestead_lhcapacity to their own fakes and
        # leave it so: read through this cluster whatever ran before.
        LHCAP = server.LHCAP
        for name, value in (("kget", K.kget), ("ksend", K.ksend),
                            ("v2_status", server.STORAGE_CLASSES.v2_engine_status)):
            test.enterContext(mock.patch.object(LHCAP, name, value))
        inner = K.transport

        def counting(method, path, *args, **kwargs):
            self.calls.append((method, path.split("?", 1)[0]))
            return inner(method, path, *args, **kwargs)
        test.enterContext(mock.patch.object(K, "transport", counting))

    def gets(self, path=None):
        return [p for m, p in self.calls if m == "GET" and (path is None or p == path)]

    def route(self, key):
        return self.server.MODULE_ROUTES.table()[("GET", ROUTES[key])].handler(None)


def clean_snapshot(test):
    import homestead_page_snapshot as PAGES
    PAGES.reset()
    test.addCleanup(PAGES.reset)
    return PAGES


def clean_cache(test):
    for store in (CACHE._cache, CACHE._running, CACHE._epoch, CACHE._forgot):
        store.clear()
    test.addCleanup(lambda: [store.clear() for store in (CACHE._cache, CACHE._running, CACHE._epoch, CACHE._forgot)])


class CacheBookkeepingTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)

    def test_a_value_kept_with_the_current_epoch_is_served(self):
        epoch = CACHE.epoch("ov")
        self.assertTrue(CACHE.keep("ov", "cycle", epoch, at=time.time()))
        self.assertEqual("cycle", CACHE.cached("ov", 20, lambda: "computed"))

    def test_a_value_read_before_a_forget_is_not_kept(self):
        epoch = CACHE.epoch("ov")
        CACHE.forget_where(lambda key: key == "ov")
        self.assertFalse(CACHE.keep("ov", "old", epoch))
        self.assertNotIn("ov", CACHE._cache)

    def test_the_time_of_each_forget_is_recorded_even_when_nothing_is_kept(self):
        self.assertEqual(0.0, CACHE.forgotten_at("wl"))
        CACHE.epoch("wl")                      # known to the snapshot, not cached
        with mock.patch.object(CACHE.time, "time", lambda: 1234.5):
            CACHE.forget_where(lambda key: key == "wl")
        self.assertEqual(1234.5, CACHE.forgotten_at("wl"))
        with mock.patch.object(CACHE.time, "time", lambda: 1240.0):
            CACHE.forget_where(lambda key: key == "wl")   # a second write, still not cached
        self.assertEqual(1240.0, CACHE.forgotten_at("wl"))

    def test_listeners_hear_which_keys_were_forgotten(self):
        heard = []
        CACHE.on_forget(heard.append)
        self.addCleanup(lambda: CACHE._listeners.remove(heard.append))
        CACHE.epoch("vol")
        CACHE.epoch("network")
        CACHE.forget_where(lambda key: key == "vol")
        self.assertEqual([{"vol"}], heard)

    def test_a_listener_that_fails_does_not_fail_the_write(self):
        def broken(keys):
            raise RuntimeError("listener bug")
        CACHE.on_forget(broken)
        self.addCleanup(lambda: CACHE._listeners.remove(broken))
        CACHE.epoch("ov")
        CACHE.forget_where(lambda key: key == "ov")     # no exception
        self.assertGreater(CACHE.forgotten_at("ov"), 0)


class LeaderCycleTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.cluster = Cluster(self)
        self.PAGES = clean_snapshot(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES

    def cycle(self):
        return self.PAGES.cycle(self.OV.page_inputs(), self.OV.page_builders())

    def test_one_cycle_lists_pods_and_longhorn_volumes_once_for_all_six_parts(self):
        import homestead_storage as STORAGE
        # The volumes' filesystem usage keeps its own 20 s telemetry read
        # (pods, then each host's kubelet): not a page read, left as it was.
        with mock.patch.object(STORAGE.VOLUME_USAGE, "collect", lambda get, claims: {}):
            done = self.cycle()
        self.assertEqual(dict.fromkeys(ROUTES, True), done)
        self.assertEqual(1, len(self.cluster.gets("/api/v1/pods")))
        self.assertEqual(1, len(self.cluster.gets("/apis/longhorn.io/v1beta2/volumes")))

    def test_the_volumes_part_is_handed_the_cycle_s_pod_list(self):
        import homestead_storage as STORAGE
        pods = {"items": [], "metadata": {}}
        builders = dict(self.OV.page_builders())
        with mock.patch.object(STORAGE, "get_volumes", return_value=[]) as volumes:
            builders["vol"]({"pods": pods})
            builders["vol"]({})
        self.assertEqual([mock.call(pods=pods), mock.call(pods=None)], volumes.call_args_list)
    def test_the_cycle_hands_its_pod_list_to_the_restart_record(self):
        import homestead_storage as STORAGE
        seen = []
        with mock.patch.object(self.OV.RESTARTS, "observe_later", lambda pods: seen.append(pods)), \
                mock.patch.object(STORAGE.VOLUME_USAGE, "collect", lambda get, claims: {}):
            self.cycle()
        self.assertEqual(1, len(seen), "#740: the leader's one pod read, no second one")
        self.assertEqual(1, len(self.cluster.gets("/api/v1/pods")))
        self.assertIn("items", seen[0])

    def test_a_restart_record_that_fails_does_not_fail_the_pages(self):
        def broken(pods):
            raise RuntimeError("data volume full")
        with mock.patch.object(self.OV.RESTARTS, "observe_later", broken):
            done = self.cycle()
        self.assertEqual(dict.fromkeys(ROUTES, True), done)

    def test_after_a_cycle_the_six_routes_ask_the_cluster_nothing(self):
        self.cycle()
        self.cluster.calls.clear()
        for key in ROUTES:
            self.cluster.route(key)
        self.assertEqual([], self.cluster.calls)

    def test_each_part_equals_what_the_route_computed_before(self):
        # Ages (a workload's transition_age, from K.age_secs) are whole seconds
        # since a 2020 timestamp: one clock for both reads, or a second
        # boundary between them makes them differ by 1.
        now = time.time()
        with mock.patch.object(K.time, "time", lambda: now):
            self._each_part_equals_what_the_route_computed_before()

    def _each_part_equals_what_the_route_computed_before(self):
        self.cycle()
        from_cycle = {key: self.PAGES.read(key)[1] for key in ROUTES}
        self.PAGES.reset()
        CACHE._cache.clear()
        for key in ROUTES:
            answer = self.cluster.route(key)
            if key == "ov":     # the overview also says when it was read (#705 step 8)
                answer = {k: v for k, v in answer.items() if k not in ("as_of", "age_s")}
            self.assertEqual(answer, from_cycle[key], key)

    def test_a_standby_runs_no_cycle_and_its_routes_compute_as_today(self):
        self.assertIsNone(self.PAGES.tick(lambda: False, self.OV.page_inputs(), self.OV.page_builders()))
        self.assertEqual([], self.cluster.calls)
        self.cluster.route("wl")
        self.assertIn("/apis/apps/v1/deployments", self.cluster.gets())

    def test_a_write_after_a_cycle_is_seen_on_the_next_read_and_wakes_the_cycle(self):
        self.cycle()
        self.PAGES._wanted.clear()
        K.ksend("PATCH", "/apis/apps/v1/namespaces/lab/deployments/web", {"spec": {"replicas": 0}},
                ctype="application/merge-patch+json")
        self.assertTrue(self.PAGES._wanted.is_set(), "the cycle is woken")
        self.cluster.calls.clear()
        self.cluster.route("wl")
        self.assertIn("/apis/apps/v1/deployments", self.cluster.gets(), "computed, not the old part")

    def test_a_cycle_that_started_before_a_write_is_not_kept_after_it(self):
        builders = self.OV.page_builders()
        def write_mid_cycle(got):
            K.ksend("PATCH", "/apis/apps/v1/namespaces/lab/deployments/web", {"spec": {"replicas": 0}},
                    ctype="application/merge-patch+json")
            return {"from": "before the write"}
        builders = [(key, write_mid_cycle if key == "wl" else fn) for key, fn in builders]
        self.PAGES.cycle(self.OV.page_inputs(), builders)
        self.assertIsNone(self.PAGES.read("wl"))
        self.assertNotIn("wl", CACHE._cache)
        self.assertNotEqual({"from": "before the write"}, self.cluster.route("wl"))

    def test_a_failing_part_keeps_its_last_value_and_age_while_the_others_update(self):
        self.cycle()
        first = {key: self.PAGES.read(key) for key in ROUTES}
        self.cluster.kube.fail("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes", 500, times=99)
        with mock.patch.object(self.PAGES.time, "time", lambda: first["ov"][0] + 25):
            done = self.cycle()
        self.assertFalse(done["lhcap"])
        self.assertEqual(first["lhcap"], self.PAGES.read("lhcap"))
        self.assertTrue(done["nodes"])
        self.assertEqual(first["ov"][0] + 25, self.PAGES.read("nodes")[0])

    def test_a_failing_pod_list_fails_only_the_parts_that_need_it(self):
        self.cluster.kube.fail("/api/v1/pods", 500, times=99)
        done = self.cycle()
        self.assertEqual({"nodes": False, "wl": False, "ov": False, "vol": True, "stor": True, "lhcap": True}, done)

    def test_a_part_dropped_from_the_cache_directly_is_not_served(self):
        # Several modules pop their keys after a write that fresh_reads does
        # not see (a hardware or storage-class save): the next read computes.
        self.cycle()
        CACHE._cache.pop("nodes")
        self.assertIsNone(self.PAGES.read("nodes"))
        self.cluster.calls.clear()
        self.cluster.route("nodes")
        self.assertIn("/api/v1/nodes", self.cluster.gets())

    def test_a_cycle_whose_part_a_write_overtook_reports_it_as_not_failed(self):
        builders = [(key, (lambda got: CACHE.forget_where(lambda k: k == "ov") or {"stale": True})
                     if key == "ov" else fn) for key, fn in self.OV.page_builders()]
        done = self.PAGES.cycle(self.OV.page_inputs(), builders)
        self.assertIsNone(done["ov"], "overtaken by a write, not failed")
        self.assertTrue(done["nodes"])

    def test_a_part_older_than_stale_after_is_not_served(self):
        self.cycle()
        as_of = self.PAGES.read("ov")[0]
        self.assertIsNone(self.PAGES.read("ov", now=as_of + self.PAGES.STALE_AFTER + 1))
        self.assertIsNotNone(self.PAGES.read("ov", now=as_of + self.PAGES.STALE_AFTER - 1))
        CACHE._cache.clear()
        self.cluster.calls.clear()
        with mock.patch.object(self.PAGES.time, "time", lambda: as_of + 61):
            self.cluster.route("wl")
        self.assertIn("/apis/apps/v1/deployments", self.cluster.gets(), "computed locally")


class LoopTests(unittest.TestCase):
    """When cycles run, and what the leader's loops read."""

    def setUp(self):
        clean_cache(self)
        self.PAGES = clean_snapshot(self)
        self.runs = []
        self.inputs = {}
        self.builders = [("ov", lambda got: self.runs.append(1) or {"n": len(self.runs)})]

    def tick(self, now, leader=True):
        return self.PAGES.tick(lambda: leader, self.inputs, self.builders, now=now)

    def test_cycles_run_on_boot_then_every_twenty_seconds(self):
        self.assertIsNotNone(self.tick(100))
        self.assertIsNone(self.tick(119))
        self.assertIsNotNone(self.tick(120))
        self.assertEqual(2, len(self.runs))

    def test_a_wake_runs_a_cycle_soon_but_not_back_to_back(self):
        self.tick(100)
        self.PAGES.wake()
        self.assertIsNone(self.tick(101), "within MIN_GAP of the last cycle")
        self.assertIsNotNone(self.tick(102))
        self.PAGES.wake()
        self.PAGES.wake()
        self.assertIsNotNone(self.tick(104))
        self.assertIsNone(self.tick(106), "two wakes, one cycle")
        self.assertEqual(3, len(self.runs))

    def test_a_replica_that_becomes_the_leader_runs_a_cycle_at_once(self):
        self.tick(100)
        self.assertIsNone(self.tick(102, leader=False))
        self.assertIsNotNone(self.tick(104, leader=True))
        self.assertEqual(2, len(self.runs))

    def test_a_replica_that_loses_the_lease_stops_serving_its_parts(self):
        self.tick(100)
        self.assertIsNotNone(self.PAGES.read("ov"))
        self.tick(102, leader=False)
        self.assertIsNone(self.PAGES.read("ov"), "it computes as a standby does, until PR 2's file")

    def test_a_sampler_round_survives_a_failing_leader_check(self):
        OV, samples, live, beats = self.sampler()
        def broken():
            raise RuntimeError("lease read failed")
        with mock.patch.object(OV.LEADER, "is_leader", broken):
            self.assertEqual(self.PAGES.LEADER_CHECK, OV._sampler_round({}, now=0))
        self.assertEqual(1, len(samples))

    def test_a_cycle_overtaken_by_a_write_is_no_sampler_error(self):
        OV, samples, live, beats = self.sampler()
        self.builders = [("ov", lambda got: CACHE.forget_where(lambda k: k == "ov") or {"n": 1})]
        with mock.patch.object(OV.LEADER, "is_leader", lambda: True):
            OV._sampler_round({}, now=0)
        self.assertEqual([], live)
        self.assertEqual([("sampler", 30)], beats, "a beat with no error")

    def sampler(self):
        import server
        OV = server.OVERVIEW_ROUTES
        samples, live, beats = [], [], []
        self.enterContext(mock.patch.object(OV.VMUSAGE, "sample", lambda: samples.append(1)))
        self.enterContext(mock.patch.object(OV.HISTORY, "record_live", lambda overview: live.append(overview)))
        self.enterContext(mock.patch.object(OV, "beat", lambda *a, **k: beats.append(a)))
        self.enterContext(mock.patch.object(OV, "page_builders", lambda: self.builders))
        self.enterContext(mock.patch.object(OV, "page_inputs", lambda: {}))
        return OV, samples, live, beats

    def test_vm_disk_samples_stay_every_thirty_seconds_on_every_replica(self):
        OV, samples, live, beats = self.sampler()
        for leader in (False, True):
            samples.clear()
            self.PAGES.reset()
            state = {}
            with mock.patch.object(OV.LEADER, "is_leader", lambda: leader):
                for now in range(0, 62, 2):
                    OV._sampler_round(state, now=now)
            self.assertEqual(3, len(samples), f"leader={leader}")

    def test_the_live_history_is_recorded_once_per_thirty_second_step(self):
        OV, samples, live, beats = self.sampler()
        clock = [3000.0]
        state = {}
        with mock.patch.object(OV.LEADER, "is_leader", lambda: True), \
                mock.patch.object(self.PAGES.time, "time", lambda: clock[0]):
            for now in range(0, 120, 2):     # cycles at 0, 20, 40, 60, 80, 100
                clock[0] = 3000.0 + now
                OV._sampler_round(state, now=now)
        self.assertEqual(6, len(self.runs))
        self.assertEqual(4, len(live), "one per 30 s step, not one per cycle")
        self.assertEqual(6, len([b for b in beats if b[0] == "sampler"]))

    def test_alerts_and_history_read_the_snapshot_without_computing(self):
        cluster = Cluster(self)
        OV = cluster.server.OVERVIEW_ROUTES
        self.PAGES.cycle(OV.page_inputs(), OV.page_builders())
        computed = []
        self.enterContext(mock.patch.object(OV, "get_overview", lambda *a, **k: computed.append("ov")))
        self.enterContext(mock.patch.object(OV.LHCAP, "status", lambda: computed.append("lhcap")))
        # 21 s on: past the old 15 s and 20 s cache times, the cycle's next
        # turn is late, and the part is still fresh.
        late = self.PAGES.read("ov")[0] + 21
        self.enterContext(mock.patch.object(self.PAGES.time, "time", lambda: late))
        sources = OV._alert_sources()
        self.assertIsNotNone(sources["health"])
        self.assertIsNotNone(sources["capacity"])
        self.assertIn("cronjobs", sources, "scheduled jobs that failed or did not run alert (#739)")
        recorded = []

        class Stop(Exception):
            pass

        def stop(seconds):
            raise Stop()
        with mock.patch.object(OV.LEADER, "is_leader", lambda: True), \
                mock.patch.object(OV.HISTORY, "record", recorded.append), \
                mock.patch.object(OV, "beat", lambda *a, **k: None), \
                mock.patch.object(OV.time, "sleep", stop), self.assertRaises(Stop):
            OV._history_loop()
        self.assertEqual([self.PAGES.read("ov")[1]], recorded)
        self.assertEqual([], computed)


if __name__ == "__main__":
    unittest.main()
