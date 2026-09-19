import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class ClusterHealthTests(unittest.TestCase):
    def healthy_node(self):
        return {"name": "node-1", "status": "Ready"}

    def workload(self, **changes):
        value = {"ns": "lab", "name": "demo", "desired": 1, "ready": 1,
                 "available": 1, "updated": 1, "generation": 1,
                 "observed_generation": 1, "transition_age": 30, "problems": []}
        value.update(changes)
        return value

    def test_new_workload_is_starting_not_degraded(self):
        result = server.classify_cluster_health(
            [self.healthy_node()], [self.workload(ready=0, available=0, updated=0)], [])
        self.assertEqual("healthy", result["health"])
        self.assertEqual("starting", result["health_state"])
        self.assertFalse(result["health_issues"])

    def test_rolling_update_is_updating_not_degraded(self):
        result = server.classify_cluster_health(
            [self.healthy_node()],
            [self.workload(desired=2, ready=1, available=1, updated=1,
                           generation=2, observed_generation=1)], [])
        self.assertEqual("healthy", result["health"])
        self.assertEqual("updating", result["health_state"])
        surge = server.classify_cluster_health(
            [self.healthy_node()],
            [self.workload(ready=1, available=1, updated=0,
                           generation=2, observed_generation=1)], [])
        self.assertEqual("updating", surge["health_state"])

    def test_stalled_or_broken_workload_is_degraded(self):
        stalled = server.classify_cluster_health(
            [self.healthy_node()], [self.workload(ready=0, transition_age=301)], [])
        self.assertEqual("degraded", stalled["health_state"])
        broken = server.classify_cluster_health(
            [self.healthy_node()], [self.workload(ready=0, problems=["ImagePullBackOff"])], [])
        self.assertEqual("degraded", broken["health_state"])
        self.assertIn("ImagePullBackOff", broken["health_summary"])

    def test_node_and_volume_faults_remain_critical(self):
        result = server.classify_cluster_health(
            [{"name": "node-1", "status": "NotReady"}], [],
            [{"name": "data", "robustness": "faulted"}])
        self.assertEqual("critical", result["health_state"])
        self.assertEqual(2, len(result["health_issues"]))

    def test_detached_unknown_volume_does_not_degrade_cluster(self):
        result = server.classify_cluster_health(
            [self.healthy_node()], [], [{"name": "archive", "robustness": "unknown"}])
        self.assertEqual("healthy", result["health_state"])


if __name__ == "__main__":
    unittest.main()
