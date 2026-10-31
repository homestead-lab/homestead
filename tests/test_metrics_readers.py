"""The other metrics-server readers off the request path (#928, after #906).

Hosts and Apps take homestead_kube_metrics' background reading since #906.
The Architecture page, the VM list's usage, the container balance plan and
the weekly usage record still read metrics.k8s.io themselves, with kget's
10 s timeout, so a hung metrics-server held each of them (the VM reading
holds the sampler loop that also drives the page snapshot). They take the
same reading now, with its state."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_capacity_review as CAPACITY_REVIEW
import homestead_kube as K
import homestead_kube_metrics as M
import server
import test_flow_vms as FLOW_VMS
import homestead_container_rebalance as C
from fakes.kube import Kube
import homestead_usage as usage
import homestead_vmusage as VMUSAGE

POD_METRICS = FLOW_VMS.API[M.PODS]["items"]


def never_metrics(answers):
    """A kget that answers everything but metrics-server, which a request
    must no longer ask."""
    def get(path, **kwargs):
        if path.startswith("/apis/metrics.k8s.io/"):
            raise AssertionError(f"read {path} in the request")
        return answers(path)
    return get


class Looping(unittest.TestCase):
    """As on a replica whose metrics loop runs."""

    def setUp(self):
        M.stop()
        M.reset()
        self.addCleanup(M.reset)
        self.addCleanup(M.stop)
        self.enterContext(mock.patch.object(M, "running", lambda: True))

    def round(self, nodes=(), pods=(), now=1000.0, error=None):
        def get(path, **kwargs):
            if error is not None:
                raise error
            return {"items": list(nodes if path == M.NODES else pods)}
        M.run_round(get, now=now)


class ArchitectureTests(Looping):
    def flow(self):
        with mock.patch.object(server.FLOW, "kget", side_effect=never_metrics(FLOW_VMS.API.__getitem__)), \
                mock.patch.object(server.HW, "workload_features", return_value=[]):
            return server.FLOW.get_flow2()

    def test_takes_the_loop_s_reading_and_says_its_state(self):
        self.round(pods=POD_METRICS, now=1000.0)
        with mock.patch("time.time", return_value=1001.0):
            flow = self.flow()
        ubuntu = next(w for w in flow["workloads"] if w["id"] == "w:vm-ubuntu")
        self.assertEqual((0.25, 1024.0), (ubuntu["cpu"], ubuntu["mem_mb"]))
        self.assertEqual("ok", flow["metrics"]["state"])

    def test_old_figures_are_served_and_marked_stale(self):
        self.round(pods=POD_METRICS, now=1000.0)
        self.round(error=TimeoutError("timed out"), now=1060.0)
        with mock.patch("time.time", return_value=1061.0):
            flow = self.flow()
        ubuntu = next(w for w in flow["workloads"] if w["id"] == "w:vm-ubuntu")
        self.assertEqual(0.25, ubuntu["cpu"])
        self.assertEqual(("stale", 61), (flow["metrics"]["state"], flow["metrics"]["age_s"]))


class VmUsageTests(Looping):
    def setUp(self):
        super().setUp()
        def answers(path):
            if "virt-handler" in path:
                return {"items": []}
            raise AssertionError(path)
        saved = VMUSAGE.kget
        VMUSAGE.bind(never_metrics(answers))
        self.addCleanup(VMUSAGE.bind, saved)
        VMUSAGE._readings.clear()

    def test_usage_takes_the_loop_s_reading(self):
        self.round(pods=POD_METRICS, now=1000.0)
        VMUSAGE.sample(fetch=lambda ip: "", now=1001.0)
        measured, _, metrics = VMUSAGE.measured(now=1001.0)
        self.assertEqual(0.25, measured[("lab", "ubuntu")]["cpu"])
        self.assertEqual("ok", metrics["state"])

    def test_missing_figures_are_said(self):
        self.round(error=TimeoutError("timed out"), now=1000.0)
        VMUSAGE.sample(fetch=lambda ip: "", now=1001.0)
        measured, _, metrics = VMUSAGE.measured(now=1001.0)
        self.assertEqual(({}, "unavailable"), (measured, metrics["state"]))

    def test_the_sampler_reads_only_disk_traffic(self):
        # CPU and memory come from the loop's reading when asked for, so the
        # 30 s sample no longer holds them (#928 follow-up).
        with mock.patch.object(M, "read", side_effect=AssertionError("read CPU and memory in the sample")):
            VMUSAGE.sample(fetch=lambda ip: "", now=1001.0)

    def vm_rows(self, *vms, now):
        import homestead_vm_list as VM_LIST
        import homestead_vms as VMS
        objects = []
        for name, running in vms:
            objects.append({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
                            "metadata": {"name": name, "namespace": "lab", "uid": name},
                            "spec": {"runStrategy": "Always" if running else "Halted",
                                     "template": {"spec": {"domain": {"cpu": {"cores": 2}, "memory": {"guest": "2Gi"}}}}},
                            "status": {"printableStatus": "Running" if running else "Stopped"}})
            if running:
                objects.append({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                                "metadata": {"name": name, "namespace": "lab", "uid": name + "-vmi"},
                                "status": {"phase": "Running"}})
        self.enterContext(Kube(*objects))
        saved = (VMS.kget, VMS.ksend)
        VMS.bind(K.kget, K.ksend, lambda *a, **k: [])
        self.addCleanup(lambda: VMS.bind(*saved, lambda *a, **k: []))
        with mock.patch("time.time", return_value=now):
            return VM_LIST.list_vms()

    def test_the_vm_list_rows_carry_the_state(self):
        self.round(error=TimeoutError("timed out"), now=1000.0)
        VMUSAGE.sample(fetch=lambda ip: "", now=1001.0)
        rows = self.vm_rows(("router", True), ("spare", False), now=1001.0)
        self.assertEqual(["router", "spare"], [row["name"] for row in rows])
        self.assertEqual(["unavailable"] * 2, [row["metrics"]["state"] for row in rows], "said on every row")
        self.assertFalse([row for row in rows if "usage" in row])

    def test_the_vm_list_has_figures_read_since_the_last_sample(self):
        # The sample found nothing yet; the loop has answered since.
        self.round(error=TimeoutError("timed out"), now=1000.0)
        VMUSAGE.sample(fetch=lambda ip: "", now=1001.0)
        self.round(pods=POD_METRICS, now=1010.0)
        row = self.vm_rows(("ubuntu", True), now=1011.0)[0]
        self.assertEqual("ok", row["metrics"]["state"])
        self.assertEqual(12.5, row["usage"]["cpu_pct"])

    def test_the_vm_list_says_figures_went_old_since_the_last_sample(self):
        self.round(pods=POD_METRICS, now=1000.0)
        VMUSAGE.sample(fetch=lambda ip: "", now=1001.0)
        self.round(error=TimeoutError("timed out"), now=1015.0)
        row = self.vm_rows(("ubuntu", True), now=1029.0)[0]
        self.assertEqual(("stale", 29), (row["metrics"]["state"], row["metrics"]["age_s"]))
        self.assertEqual(12.5, row["usage"]["cpu_pct"], "the last figures, marked old")


def scene_objects():
    """k2 runs the busy apps, k1 and k3 are quiet (as test_container_rebalance's
    scene), on the shared fake API; and metrics-server's lists for the loop."""
    apps = {"frigate": ("k2", "1500m", "3Gi"), "ha": ("k2", "400m", "1Gi"),
            "plex": ("k2", "900m", "2Gi"), "tiny": ("k2", "10m", "50Mi")}
    usage = {"k1": ("400m", "3Gi"), "k2": ("3200m", "11Gi"), "k3": ("500m", "4Gi")}
    objects = [{"apiVersion": "v1", "kind": "Node", "metadata": {"name": n}, "spec": {},
                "status": {"allocatable": {"cpu": "4", "memory": "16Gi"},
                           "conditions": [{"type": "Ready", "status": "True"}]}} for n in usage]
    for name, (host, _, _) in apps.items():
        selector = {"kubernetes.io/hostname": "k2"} if name == "plex" else {}
        objects += [
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": name, "generation": 1},
             "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                      "template": {"spec": {"nodeSelector": selector, "volumes": []}}},
             "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1}},
            {"apiVersion": "v1", "kind": "Pod",
             "metadata": {"namespace": "lab", "name": f"{name}-abc-0", "labels": {"app": name},
                          "ownerReferences": [{"kind": "ReplicaSet", "name": f"{name}-abc"}]},
             "spec": {"nodeName": host}, "status": {"phase": "Running"}},
            {"apiVersion": "apps/v1", "kind": "ReplicaSet",
             "metadata": {"namespace": "lab", "name": f"{name}-abc", "ownerReferences": [{"kind": "Deployment", "name": name}]}}]
    nodes = [{"metadata": {"name": n}, "usage": {"cpu": c, "memory": m}} for n, (c, m) in usage.items()]
    pods = [{"metadata": {"namespace": "lab", "name": f"{n}-abc-0"}, "containers": [{"usage": {"cpu": c, "memory": m}}]}
            for n, (_, c, m) in apps.items()]
    return objects, nodes, pods


class BalancePlanTests(Looping):
    def setUp(self):
        super().setUp()
        objects, self.nodes, self.pods = scene_objects()
        self.enterContext(Kube(*objects))
        saved = {name: getattr(C, name) for name in ("kget", "requirements", "satisfies", "apply", "own", "is_leader",
                                                      "summaries", "guard", "operations", "standby")}
        self.addCleanup(lambda: [setattr(C, name, value) for name, value in saved.items()])
        CAPACITY_REVIEW.bind(lambda: b"test-key")
        C.bind(never_metrics(lambda path: K.kget(path)), lambda dep: {}, lambda n, r: (True, []),
               lambda ns, name, to: None, ("lab", "homestead"), lambda: True)

    def test_the_plan_takes_the_loop_s_reading(self):
        self.round(self.nodes, self.pods, now=1000.0)
        with mock.patch("time.time", return_value=1001.0):
            plan = C.plan()
        self.assertEqual("lab/frigate", plan["moves"][0]["id"])
        self.assertTrue(plan["metrics"])
        self.assertEqual("ok", plan["metrics_state"]["state"])

    def test_old_figures_are_marked_stale(self):
        self.round(self.nodes, self.pods, now=1000.0)
        self.round(error=TimeoutError("timed out"), now=1100.0)
        with mock.patch("time.time", return_value=1100.0):
            plan = C.plan()
        self.assertEqual("lab/frigate", plan["moves"][0]["id"], "planned on the last figures")
        self.assertEqual(("stale", 100), (plan["metrics_state"]["state"], plan["metrics_state"]["age_s"]))

    def test_without_figures_the_plan_says_usage_is_unknown(self):
        self.round(error=TimeoutError("timed out"), now=1000.0)
        plan = C.plan()
        self.assertFalse(plan["metrics"])
        self.assertEqual("unavailable", plan["metrics_state"]["state"])

    def test_missing_pod_figures_are_said_though_the_hosts_answer(self):
        def get(path, **kwargs):
            if path == M.PODS:
                raise TimeoutError("timed out")
            return {"items": self.nodes}
        M.run_round(get, now=1000.0)
        with mock.patch("time.time", return_value=1001.0):
            plan = C.plan()
        self.assertFalse(plan["metrics"], "each app's use would read as nothing")
        self.assertEqual("unavailable", plan["metrics_state"]["state"])

    def test_the_worse_state_of_hosts_and_apps_is_said(self):
        self.round(self.nodes, self.pods, now=1000.0)
        def get(path, **kwargs):
            if path == M.PODS:
                raise TimeoutError("timed out")
            return {"items": self.nodes}
        M.run_round(get, now=1100.0)                       # hosts ok, apps stale
        with mock.patch("time.time", return_value=1101.0):
            self.assertEqual("stale", C.plan()["metrics_state"]["state"])
        M.run_round(lambda path, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")), now=1300.0)
        with mock.patch("time.time", return_value=1310.0):  # hosts stale, apps past KEEP_FOR
            self.assertEqual("unavailable", C.plan()["metrics_state"]["state"])


class RealBindingTests(unittest.TestCase):
    """server.py binds the readers' kget; the short timeout must reach it."""

    def test_the_architecture_read_passes_the_timeout_on(self):
        calls = []
        with mock.patch.object(server.KUBE_HELPERS, "kget", lambda path, **kw: calls.append(kw) or {}):
            server.FLOW.kget(M.PODS, timeout=M.TIMEOUT)
        self.assertEqual([{"timeout": M.TIMEOUT}], calls)

    def test_the_weekly_usage_read_passes_the_timeout_on(self):
        # usage's tests rebind its read, so the binding is checked where it is made.
        source = (ROOT / "server" / "server.py").read_text(encoding="utf-8")
        line = next(l for l in source.splitlines() if l.startswith("USAGE.bind("))
        self.assertIn("lambda path, **options: KUBE_HELPERS.kget(path, **options)", line)


class WeeklyUsageTests(Looping):
    """The record Edit suggests a reservation from: only current figures, each once."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        usage.bind(folder.name, server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem)
        usage._taken.update(last_ok=None)
        self.addCleanup(usage.bind, "/data", server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem)
        pod = {"metadata": {"name": "web-5c6b7-a", "namespace": "lab",
                            "ownerReferences": [{"kind": "ReplicaSet", "name": "web-5c6b7"}]},
               "status": {"phase": "Running"}}
        self.metric = {"metadata": {"name": "web-5c6b7-a", "namespace": "lab"},
                       "containers": [{"name": "web", "usage": {"cpu": "100m", "memory": "256Mi"}}]}
        self.read = never_metrics({"/api/v1/pods": {"items": [pod]}}.__getitem__)

    def sample(self, now=1001.0):
        with mock.patch("time.time", return_value=now):
            return usage.sample(self.read, server.FLOW._flow_owner, server.SYS_NS)

    def points(self):
        try:
            series = json.loads((self.folder / usage.FILE).read_text())["series"]
        except FileNotFoundError:
            return 0
        return sum(day["rounds"] for v in series.values() for day in v["days"].values())

    def test_a_round_takes_the_loop_s_reading(self):
        self.round(pods=[self.metric], now=1000.0)
        self.sample()
        self.assertEqual(1, self.points())

    def test_the_same_reading_is_not_recorded_twice(self):
        self.round(pods=[self.metric], now=1000.0)
        self.sample()
        self.sample()
        self.assertEqual(1, self.points())

    def test_old_figures_are_not_recorded_as_new_and_the_round_says_why(self):
        self.round(pods=[self.metric], now=1000.0)
        self.sample()
        self.round(error=TimeoutError("timed out"), now=1100.0)
        with self.assertRaises(RuntimeError) as said:
            self.sample(now=1100.0)
        self.assertIn("metrics-server", str(said.exception))
        self.assertEqual(1, self.points())

    def test_a_cluster_without_metrics_server_is_a_404(self):
        import io
        import urllib.error
        self.round(error=urllib.error.HTTPError(M.PODS, 404, "x", {}, io.BytesIO(b"{}")), now=1000.0)
        with self.assertRaises(Exception) as said:
            self.sample()
        self.assertEqual(404, getattr(said.exception, "code", None))

    def test_a_round_before_the_first_reading_is_tried_again_next_turn(self):
        rounds = []
        usage.bind(str(self.folder), server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem, self.read,
                   server.FLOW._flow_owner, server.SYS_NS, lambda: True, lambda *a, **k: None)
        turns = iter([None, None])

        class Stop(Exception):
            pass

        def sleep(_):
            if next(turns, Stop) is Stop:
                raise Stop
            self.round(pods=[self.metric], now=1000.0)   # the loop's first reading lands
        with mock.patch.object(M, "FIRST_WAIT", 0), mock.patch("time.time", return_value=1001.0), \
                mock.patch.object(usage, "record", side_effect=rounds.append), self.assertRaises(Stop):
            usage.run(sleep=sleep)
        self.assertEqual(1, len(rounds), "recorded on the next turn, not a whole STEP later")

    def test_nothing_is_recorded_before_the_first_reading(self):
        with mock.patch.object(M, "FIRST_WAIT", 0):
            self.sample()
        self.assertEqual(0, self.points())


if __name__ == "__main__":
    unittest.main()
