"""App usage history: what each container uses, and the reservation Edit offers from it."""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_usage as usage
import server

DAY = 86400
NOW = 1_760_000_000


def pod(name, ns="lab", phase="Running", owner=("ReplicaSet", "web-5c6b7"), labels=None):
    meta = {"name": name, "namespace": ns, "labels": labels or {"app": "web", "pod-template-hash": "5c6b7"}}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {"metadata": meta, "status": {"phase": phase}}


def metric(name, ns="lab", **containers):
    return {"metadata": {"name": name, "namespace": ns},
            "containers": [{"name": c, "usage": {"cpu": cpu, "memory": mem}} for c, (cpu, mem) in containers.items()]}


class UsageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        usage.bind(folder.name, server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem)
        self.addCleanup(usage.bind, "/data", server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem)

    def week(self, cpu_m=100, mem_mi=400, days=7, spikes=0, container="web"):
        """A reading every five minutes for days, the last few of them spikes."""
        per_day = DAY // usage.STEP
        for i in range(days * per_day):
            spike = i >= days * per_day - spikes
            usage.record([("lab", "web", container, cpu_m * (8 if spike else 1), mem_mi * (2 if spike else 1))],
                         now=NOW - days * DAY + i * usage.STEP)

    def test_a_reading_is_kept_to_within_a_twentieth_either_way(self):
        for value in (1, 7.5, 100, 412, 4096):
            mid = usage._mid(usage._bucket(value))
            self.assertTrue(value / 1.05 - 1e-9 <= mid <= value * 1.05 + 1e-9, (value, mid))

    def test_steady_use_reads_as_itself_and_never_above_its_peak(self):
        self.week()
        found = usage.summary("lab", "web", "web", now=NOW)
        self.assertLessEqual(found["cpu_p95_m"], found["cpu_max_m"])
        self.assertTrue(95 <= found["cpu_p95_m"] <= 100, found["cpu_p95_m"])

    def test_a_day_of_readings_is_enough_for_a_suggestion(self):
        self.week(days=1, spikes=0)
        self.assertIn("suggest", usage.summary("lab", "web", "web", now=NOW - 1))

    def test_one_reading_says_how_long_until_a_suggestion(self):
        usage.record([("lab", "web", "web", 100, 400)], now=NOW)
        early = usage.summary("lab", "web", "web", now=NOW)
        self.assertNotIn("suggest", early)
        self.assertIn("0 hours so far", early["why"])

    def test_three_hours_across_midnight_are_three_hours_not_two_days(self):
        midnight = NOW - NOW % DAY
        for i in range(36):
            usage.record([("lab", "web", "web", 100, 400)], now=midnight - 18 * usage.STEP + i * usage.STEP)
        found = usage.summary("lab", "web", "web", now=midnight + DAY // 2)
        self.assertEqual((2, 3), (found["days"], found["hours"]))

    def test_each_resource_is_raised_or_lowered_on_its_own(self):
        self.week()
        found = usage.summary("lab", "web", "web", {"cpu": "300m", "memory": "128Mi"}, now=NOW)["suggest"]
        self.assertEqual(("lower", "raise", True), (found["cpu_change"], found["memory_change"], found["lowers"]))

    def test_a_resource_within_a_tenth_of_its_suggestion_is_kept(self):
        self.week()
        first = usage.summary("lab", "web", "web", now=NOW)["suggest"]
        cpu_near = f"{int(int(first['cpu'][:-1]) * 1.05)}m"
        found = usage.summary("lab", "web", "web", {"cpu": cpu_near, "memory": "128Mi"}, now=NOW)["suggest"]
        self.assertEqual(("keep", "raise", False), (found["cpu_change"], found["memory_change"], found["lowers"]))

    def test_a_cpu_limit_alone_holds_only_the_cpu(self):
        self.week()
        found = usage.summary("lab", "web", "web", {}, {"cpu": "100m"}, now=NOW)["suggest"]
        self.assertEqual(("100m", ["cpu"]), (found["cpu"], found["capped"]))

    def test_three_copies_still_need_a_whole_day(self):
        for i in range(100):
            usage.record([("lab", "web", "web", 100, 400)] * 3, now=NOW - 100 * usage.STEP + i * usage.STEP)
        found = usage.summary("lab", "web", "web", now=NOW)
        self.assertEqual((300, 100), (found["samples"], found["rounds"]))
        self.assertNotIn("suggest", found)

    def test_a_limit_without_a_request_counts_as_the_reservation(self):
        self.week()
        found = usage.summary("lab", "web", "web", {}, {"cpu": "2", "memory": "2Gi"}, now=NOW)["suggest"]
        self.assertEqual((2000, 2048, True), (found["cpu_now_m"], found["memory_now_mi"], found["lowers"]))

    def test_a_reservation_close_to_the_suggestion_is_left_alone(self):
        self.week()
        first = usage.summary("lab", "web", "web", now=NOW)["suggest"]
        near = {"cpu": f"{int(int(first['cpu'][:-1]) * 1.05)}m", "memory": f"{int(int(first['memory'][:-2]) * 0.95)}Mi"}
        self.assertFalse(usage.summary("lab", "web", "web", near, now=NOW)["suggest"]["differs"])

    def test_a_container_never_seen_says_so(self):
        self.assertEqual(0, usage.summary("lab", "web", "web", now=NOW)["samples"])
        self.assertIn("No readings yet", usage.summary("lab", "web", "web", now=NOW)["why"])

    def test_a_week_of_use_suggests_its_usual_use_with_headroom(self):
        self.week()
        found = usage.summary("lab", "web", "web", {"cpu": "50m", "memory": "128Mi"}, now=NOW)
        cpu, mem = int(found["suggest"]["cpu"][:-1]), int(found["suggest"]["memory"][:-2])
        self.assertTrue(145 <= cpu <= 160, cpu)        # 100m x 1.5, within half a bucket
        self.assertTrue(480 <= mem <= 528, mem)        # 400Mi x 1.25, within half a bucket
        self.assertEqual(0, cpu % usage.CPU_STEP_M)
        self.assertEqual(0, mem % usage.MEM_STEP_MI)
        self.assertTrue(found["suggest"]["differs"])
        self.assertEqual(7, found["days"])

    def test_a_nightly_spike_shows_as_the_peak_and_not_as_the_reservation(self):
        self.week(spikes=12)
        found = usage.summary("lab", "web", "web", now=NOW)
        self.assertEqual((800, 800), (found["cpu_max_m"], found["mem_max_mi"]))
        self.assertLess(int(found["suggest"]["cpu"][:-1]), 200)

    def test_a_suggestion_never_passes_the_limit(self):
        self.week()
        found = usage.summary("lab", "web", "web", {"memory": "128Mi"}, {"memory": "448Mi", "cpu": "120m"}, now=NOW)
        self.assertEqual(("120m", "448Mi"), (found["suggest"]["cpu"], found["suggest"]["memory"]))
        self.assertEqual(["cpu", "memory"], found["suggest"]["capped"])

    def test_a_reservation_that_matches_its_use_is_not_offered_again(self):
        self.week()
        first = usage.summary("lab", "web", "web", now=NOW)["suggest"]
        again = usage.summary("lab", "web", "web", {"cpu": first["cpu"], "memory": first["memory"]}, now=NOW)
        self.assertFalse(again["suggest"]["differs"])

    def test_days_older_than_two_weeks_are_forgotten(self):
        usage.record([("lab", "web", "web", 100, 400)], now=NOW - 20 * DAY)
        usage.record([("lab", "old", "old", 100, 400)], now=NOW - 20 * DAY)
        usage.record([("lab", "web", "web", 100, 400)], now=NOW)
        series = json.loads((self.folder / usage.FILE).read_text())["series"]
        self.assertEqual(["lab/web/web"], list(series))
        self.assertEqual(1, len(series["lab/web/web"]["days"]))

    def test_each_container_of_each_running_app_pod_is_one_reading(self):
        pods = [pod("web-5c6b7-a"), pod("web-5c6b7-b"),
                pod("backup-web-1-x", owner=("Job", "backup-web-1"), labels={}),
                pod("web-5c6b7-done", phase="Succeeded"),
                pod("coredns-1", ns="kube-system", owner=("ReplicaSet", "coredns-1"), labels={})]
        metrics = [metric("web-5c6b7-a", web=("250000000n", "512Mi"), sidecar=("5m", "32Mi")),
                   metric("web-5c6b7-b", web=("100m", "1Gi")),
                   metric("backup-web-1-x", backup=("900m", "64Mi")),
                   metric("coredns-1", ns="kube-system", coredns=("3m", "20Mi"))]
        rows = usage.readings(pods, metrics, server.FLOW._flow_owner, server.SYS_NS)
        self.assertEqual([("lab", "web", "web", 250.0, 512.0), ("lab", "web", "sidecar", 5.0, 32.0),
                          ("lab", "web", "web", 100.0, 1024.0)], rows)

    def test_a_round_reads_metrics_and_pods_and_leaves_out_helpers(self):
        listed = {"/apis/metrics.k8s.io/v1beta1/pods": {"items": [metric("web-5c6b7-a", web=("100m", "256Mi")),
                                                                   metric("files-1", files=("500m", "1Gi"))]},
                  "/api/v1/pods": {"items": [pod("web-5c6b7-a"), pod("files-1", labels={"task": "files"})]}}
        usage.sample(lambda path, **_: listed[path], server.FLOW._flow_owner, server.SYS_NS,
                     skip=lambda p: (p["metadata"].get("labels") or {}).get("task"))
        series = json.loads((self.folder / usage.FILE).read_text())["series"]
        self.assertEqual(["lab/web/web"], sorted(series))

    def test_the_edit_dialog_gets_each_containers_use(self):
        self.week(container="app")
        deployment = {"metadata": {"name": "web", "namespace": "lab"},
                      "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}},
                               "template": {"metadata": {"labels": {"app": "web"}},
                                            "spec": {"containers": [{"name": "app", "image": "nginx",
                                                                     "resources": {"requests": {"cpu": "50m"}}}]}}}}
        with mock.patch.object(server.LC, "seed_configs", return_value=[]), \
                mock.patch.object(usage.time, "time", return_value=NOW):
            payload = server.WORKLOADS.workload_edit_payload("lab", "web", deployment, hardware_definitions=[], services=[])
        self.assertIn("suggest", payload["containers"][0]["usage"])



class LoopTests(unittest.TestCase):
    def run_rounds(self, read, rounds=3):
        beats, now = [], [1000.0]
        usage.bind("/data", server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem, read, server.FLOW._flow_owner, (), lambda: True,
                   lambda *args, **kwargs: beats.append(args))
        self.addCleanup(usage.bind, "/data", server.KUBE_HELPERS.parse_cpu, server.KUBE_HELPERS.parse_mem)

        class Stop(Exception):
            pass
        ticks = iter(range(rounds))

        def sleep(_):
            now[0] += 30
            if next(ticks, None) is None:
                raise Stop
        with mock.patch.object(usage.time, "time", side_effect=lambda: now[0]), \
                mock.patch.object(usage, "record"), self.assertRaises(Stop):
            usage.run(sleep=sleep)
        return beats

    def test_a_failed_round_waits_a_whole_step_before_the_next(self):
        calls = []

        def read(path, **_):
            calls.append(path)
            raise RuntimeError("API down")
        beats = self.run_rounds(read, rounds=5)
        self.assertEqual(1, len(calls))
        self.assertEqual("usage", beats[0][0])

    def test_a_cluster_without_metrics_server_is_told_so_plainly(self):
        def read(path, **_):
            raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO())
        beats = self.run_rounds(read, rounds=1)
        self.assertIn("metrics-server is not installed", str(beats[0][2]))

if __name__ == "__main__":
    unittest.main()
