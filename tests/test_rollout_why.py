import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_updates as UPDATES


def pod(age):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
    return {"metadata": {"name": "homestead-7cd4b47d4-hwlnr", "creationTimestamp": stamp}}


EVENTS = {"items": [
    {"reason": "Scheduled", "type": "Warning", "message": "old", "lastTimestamp": "2026-09-24T10:00:00Z"},
    {"reason": "FailedAttachVolume", "type": "Warning", "lastTimestamp": "2026-09-24T10:05:00Z",
     "message": "AttachVolume.Attach failed for volume \"pvc-1\" : rpc error: invalid controller count 2"}]}


class RolloutWhyTests(unittest.TestCase):
    def test_init_image_pull_failure_is_reported_even_when_app_is_initializing(self):
        waiting = pod(10)
        waiting["metadata"]["namespace"] = "lab"
        waiting["metadata"]["labels"] = {"app": "homestead"}
        waiting["status"] = {"initContainerStatuses": [{"name": "data-permissions", "state": {"waiting": {
            "reason": "ImagePullBackOff", "message": "failed to pull: dial tcp: lookup ghcr.io: Try again"}}}],
            "containerStatuses": [{"name": "homestead", "state": {"waiting": {"reason": "PodInitializing"}}}]}
        dep = {"metadata": {"name": "homestead", "namespace": "lab", "generation": 2}, "spec": {
            "replicas": 1, "selector": {"matchLabels": {"app": "homestead"}}}, "status": {
                "replicas": 2, "updatedReplicas": 1, "availableReplicas": 1, "readyReplicas": 1}}
        with mock.patch.object(UPDATES, "kget", return_value={"items": [waiting]}), \
                mock.patch.object(UPDATES, "pull_state", return_value={}):
            result = UPDATES.progress("lab", "homestead", dep)
        self.assertEqual("failed", result["phase"])
        self.assertTrue(result["pods"][0]["waiting"][0]["init"])
        self.assertIn("init data-permissions", result["problems"][0])
        self.assertIn("DNS", result["problems"][0])

    def test_a_young_pod_is_given_time(self):
        with mock.patch.object(UPDATES, "kget", lambda path: EVENTS):
            self.assertIsNone(UPDATES._why_waiting("lab", pod(20)))

    def test_a_pod_waiting_on_its_volume_says_so_and_is_stuck_after_three_minutes(self):
        with mock.patch.object(UPDATES, "kget", lambda path: EVENTS):
            waiting = UPDATES._why_waiting("lab", pod(90))
            self.assertIn("invalid controller count 2", waiting["message"])
            self.assertFalse(waiting["stuck"])
            self.assertIn("Scale the workload to 0", waiting["hint"])
            self.assertTrue(UPDATES._why_waiting("lab", pod(400))["stuck"])

    def test_no_warnings_no_reason(self):
        with mock.patch.object(UPDATES, "kget", lambda path: {"items": []}):
            self.assertIsNone(UPDATES._why_waiting("lab", pod(400)))


if __name__ == "__main__":
    unittest.main()
