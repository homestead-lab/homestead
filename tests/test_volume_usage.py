import copy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_volume_usage as usage
import server

GIB = 1024 ** 3
STAMP = "2026-09-26T19:00:00Z"
NOW = usage.timestamp(STAMP)
KEY = ("lab", "recordings")


class Cluster:
    def __init__(self):
        self.claims = {KEY: {"metadata": {"uid": "claim-1", "creationTimestamp": "2026-09-25T00:00:00Z"},
                             "spec": {"volumeName": "pvc-1"}}}
        self.pods = {"items": [{"metadata": {"uid": "pod-1", "namespace": "lab", "creationTimestamp": STAMP},
                                "spec": {"nodeName": "node1", "volumes": [{"persistentVolumeClaim": {"claimName": "recordings"}}]},
                                "status": {"phase": "Running"}}]}
        self.sample = {"pvcRef": {"namespace": "lab", "name": "recordings"}, "time": STAMP,
                       "usedBytes": 180 * GIB, "capacityBytes": 196 * GIB}
        self.summary = {"pods": [{"podRef": {"uid": "pod-1"}, "volume": [self.sample]}]}
        self.calls = []

    def get(self, path, **kw):
        self.calls.append((path, kw))
        return self.pods if path == "/api/v1/pods" else self.summary

    def collect(self):
        return usage.collect(self.get, self.claims, NOW)


class FilesystemUsageTests(unittest.TestCase):
    def test_reports_filesystem_bytes_not_snapshot_footprint(self):
        c = Cluster()
        result = c.collect()[KEY]
        self.assertEqual((180, 196, 91.8, "kubelet"),
                         (result["used_gb"], result["capacity_gb"], result["used_pct"], result["source"]))
        self.assertEqual(("/api/v1/nodes/node1/proxy/stats/summary", {"timeout": 4}), c.calls[-1])

    def test_empty_filesystem_is_a_real_zero(self):
        c = Cluster()
        c.sample["usedBytes"] = 0
        self.assertEqual(0, c.collect()[KEY]["used_pct"])

    def test_invalid_and_missing_values_are_unknown_not_clamped(self):
        for field, value in [("usedBytes", -1), ("usedBytes", 197 * GIB), ("usedBytes", True),
                             ("usedBytes", None), ("usedBytes", float("nan")), ("usedBytes", "123"),
                             ("capacityBytes", 0), ("capacityBytes", float("inf")), ("capacityBytes", None)]:
            with self.subTest(field=field, value=value):
                c = Cluster()
                c.sample[field] = value
                self.assertEqual({}, c.collect())

    def test_stale_future_missing_and_unzoned_samples_are_unknown(self):
        for stamp in [None, "invalid", "2026-09-26T18:57:59Z", "2026-09-26T19:00:06Z", "2026-09-26T19:00:00"]:
            with self.subTest(stamp=stamp):
                c = Cluster()
                c.sample["time"] = stamp
                self.assertEqual({}, c.collect())

    def test_matches_live_pod_identity_and_claim_namespace(self):
        c = Cluster()
        c.summary["pods"][0]["podRef"]["uid"] = "old-pod"
        self.assertEqual({}, c.collect())
        c.summary["pods"][0]["podRef"]["uid"] = "pod-1"
        c.sample["pvcRef"]["namespace"] = "elsewhere"
        self.assertEqual({}, c.collect())

    def test_inactive_raw_block_and_replaced_claims_are_not_measured(self):
        for change in ("stopped", "block", "replacement", "no-pod-timestamp"):
            with self.subTest(change=change):
                c = Cluster()
                if change == "stopped":
                    c.pods["items"][0]["status"]["phase"] = "Succeeded"
                elif change == "block":
                    c.claims[KEY]["spec"]["volumeMode"] = "Block"
                elif change == "replacement":
                    c.claims[KEY]["metadata"]["creationTimestamp"] = "2026-09-26T19:00:01Z"
                else:
                    del c.pods["items"][0]["metadata"]["creationTimestamp"]
                self.assertEqual({}, c.collect())
                self.assertEqual(1, len(c.calls))

    def test_shared_mounts_are_not_summed_and_newest_wins(self):
        c = Cluster()
        second_pod = copy.deepcopy(c.pods["items"][0])
        second_pod["metadata"]["uid"] = "pod-2"
        c.pods["items"].append(second_pod)
        newer = dict(c.sample, usedBytes=175 * GIB, time="2026-09-26T19:00:01Z")
        c.summary["pods"].append({"podRef": {"uid": "pod-2"}, "volume": [newer]})
        self.assertEqual(175, c.collect()[KEY]["used_gb"])
        newer["time"] = STAMP
        self.assertEqual(180, c.collect()[KEY]["used_gb"])

    def test_unavailable_and_malformed_telemetry_stays_unknown(self):
        c = Cluster()
        for response in [None, [], {}, {"pods": [None]}, {"pods": [{"podRef": None}]},
                         {"pods": [{"podRef": {"uid": "pod-1"}, "volume": [None, {"pvcRef": None}]}]},
                         {"pods": [{"podRef": {"uid": "pod-1"}, "volume": None}]}]:
            c.summary = response
            self.assertEqual({}, c.collect())
        c.pods["metadata"] = {"continue": "more"}
        self.assertEqual({}, c.collect())
        self.assertEqual({}, usage.collect(lambda *a, **kw: (_ for _ in ()).throw(PermissionError()), c.claims, NOW))

    def test_one_unreachable_node_does_not_hide_another_nodes_usage(self):
        c = Cluster()
        other = copy.deepcopy(c.pods["items"][0])
        other["spec"]["nodeName"] = "offline"
        other["metadata"]["uid"] = "pod-2"
        c.pods["items"].append(other)

        def get(path, **kw):
            if "/offline/" in path:
                raise TimeoutError()
            return c.get(path, **kw)
        self.assertEqual(180, usage.collect(get, c.claims, NOW)[KEY]["used_gb"])


class VolumeUsageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
        self.claim = self.cluster.claims[KEY]
        self.claim["metadata"].update(namespace=KEY[0], name=KEY[1])
        self.volume = {"metadata": {"name": "pvc-1"}, "spec": {"size": str(200 * GIB)},
                       "status": {"state": "attached", "actualSize": str(292 * GIB),
                                  "kubernetesStatus": {"namespace": KEY[0], "pvcName": KEY[1]}}}
        self.metrics = {KEY: dict(used_gb=180, capacity_gb=196, used_pct=91.8, sample_at=time.time())}

    def read(self):
        def get(path):
            if path.endswith("/volumes"):
                return {"items": [self.volume]}
            if path == "/api/v1/persistentvolumeclaims":
                return {"items": [self.claim]}
            return {"items": []}
        with patch.object(server, "kget", get), patch.object(server, "claim_references", return_value={}), \
             patch.object(server, "cached", side_effect=lambda key, ttl, fn: fn()), \
             patch.object(server.VOLUME_USAGE, "collect", return_value=self.metrics) as collect:
            return server.get_volumes()[0], collect

    def test_footprint_above_capacity_is_preserved_not_used_for_bar(self):
        row, _ = self.read()
        self.assertEqual((292, 200, 91.8, 180), (row["actual_gb"], row["size_gb"], row["used_pct"], row["filesystem"]["used_gb"]))

    def test_missing_or_stale_metrics_never_fall_back_to_footprint(self):
        for data in [{}, {KEY: dict(self.metrics[KEY], sample_at=time.time() - 121)}]:
            self.metrics = data
            row, _ = self.read()
            self.assertIsNone(row["filesystem"])
            self.assertIsNone(row["used_pct"])

    def test_detached_volume_does_not_query_or_display_metrics(self):
        self.volume["status"]["state"] = "detached"
        row, collect = self.read()
        collect.assert_not_called()
        self.assertIsNone(row["filesystem"])

    def test_replaced_claim_is_not_used_for_old_retained_volume(self):
        self.claim["spec"]["volumeName"] = "pvc-replacement"
        row, collect = self.read()
        collect.assert_not_called()
        self.assertTrue(row["unclaimed"])
        self.assertIsNone(row["filesystem"])


if __name__ == "__main__":
    unittest.main()
