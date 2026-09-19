import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class WorkloadHierarchyTests(unittest.TestCase):
    def test_pod_containers_keep_init_and_app_runtime_state_separate(self):
        pod = {
            "spec": {
                "initContainers": [{"name": "seed-config", "image": "busybox:1.36"}],
                "containers": [{"name": "frigate", "image": "ghcr.io/example/frigate:1"}],
            },
            "status": {
                "initContainerStatuses": [{
                    "name": "seed-config", "ready": False, "restartCount": 0,
                    "state": {"terminated": {"reason": "Completed"}},
                }],
                "containerStatuses": [{
                    "name": "frigate", "ready": True, "restartCount": 2,
                    "state": {"running": {"startedAt": "2026-09-19T12:00:00Z"}},
                }],
            },
        }

        rows = server.pod_container_rows(pod)

        self.assertEqual(["init", "app"], [row["kind"] for row in rows])
        self.assertEqual("Completed", rows[0]["state"])
        self.assertEqual("running", rows[1]["state"])
        self.assertTrue(rows[1]["ready"])
        self.assertEqual(2, rows[1]["restarts"])

    def test_waiting_reason_and_message_are_exposed_without_full_event_dump(self):
        message = "pull failed " * 40
        rows = server.pod_container_rows({
            "spec": {"containers": [{"name": "app", "image": "example/app:latest"}]},
            "status": {"containerStatuses": [{
                "name": "app", "ready": False, "restartCount": 0,
                "state": {"waiting": {"reason": "ImagePullBackOff", "message": message}},
            }]},
        })

        self.assertEqual("ImagePullBackOff", rows[0]["state"])
        self.assertLessEqual(len(rows[0]["message"]), 220)


if __name__ == "__main__":
    unittest.main()
