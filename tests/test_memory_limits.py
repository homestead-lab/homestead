import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_compose as compose
import homestead_lifecycle as lifecycle
import homestead_memory as memory
import server


class MemoryLimitTests(unittest.TestCase):
    def test_quantities_and_request_ceiling(self):
        self.assertEqual(memory.bytes_for("1Gi"), memory.bytes_for("1024Mi"))
        self.assertEqual(memory.bytes_for("1.5Gi"), memory.bytes_for("1536Mi"))
        memory.validate("128Mi", "512Mi", "app")
        with self.assertRaisesRegex(ValueError, "at least"):
            memory.validate("1Gi", "512Mi", "app")
        with self.assertRaisesRegex(ValueError, "positive size"):
            memory.validate("128Mi", "not-a-size", "app")

    def test_new_deployment_has_optional_memory_limit(self):
        cfg = {"name": "app", "image": "nginx:alpine", "memory": "128Mi", "memory_limit": "512Mi"}
        with mock.patch.object(server.HW, "features", return_value=[]):
            deployment, _ = server.build_deployment(cfg)
            unbounded, _ = server.build_deployment({**cfg, "memory_limit": ""})
        self.assertEqual("512Mi", deployment["spec"]["template"]["spec"]["containers"][0]
                         ["resources"]["limits"]["memory"])
        self.assertNotIn("limits", unbounded["spec"]["template"]["spec"]["containers"][0]["resources"])
        with mock.patch.object(server.HW, "features", return_value=[]):
            with self.assertRaisesRegex(ValueError, "at least"):
                server.build_deployment({**cfg, "memory_limit": "64Mi"})
        with mock.patch.object(server.LC, "seed_configs", return_value=[]):
            reopened = server.workload_edit_payload("lab", "app", deployment, hardware_definitions=[], services=[])
        self.assertEqual("512Mi", reopened["containers"][0]["memory_limit"])

    def test_sidecar_limit_is_per_container(self):
        current = {"metadata": {"name": "app"}, "spec": {"selector": {"matchLabels": {"app": "app"}},
            "template": {"spec": {"containers": [{"name": "main", "image": "nginx",
                "resources": {"requests": {"memory": "256Mi"}}}]}}}}
        cfg = {"name": "helper", "container_name": "helper", "target_workload": "app",
               "image": "busybox", "memory": "64Mi", "memory_limit": "128Mi"}
        with mock.patch.object(server.HW, "features", return_value=[]):
            updated, _ = server.build_sidecar_deployment(cfg, current)
        containers = updated["spec"]["template"]["spec"]["containers"]
        self.assertNotIn("limits", containers[0]["resources"])
        self.assertEqual("128Mi", containers[1]["resources"]["limits"]["memory"])

    def test_edit_updates_or_removes_only_memory_limit(self):
        container = {"name": "app", "image": "nginx", "resources": {
            "requests": {"memory": "128Mi", "cpu": "50m"},
            "limits": {"memory": "1Gi", "cpu": "1"}}}
        lifecycle._apply_container_edit(container, {"memory": "256Mi", "memory_limit": "2Gi"}, "app")
        self.assertEqual("2Gi", container["resources"]["limits"]["memory"])
        with self.assertRaisesRegex(ValueError, "at least"):
            lifecycle._apply_container_edit(copy.deepcopy(container), {"memory_limit": "64Mi"}, "app")
        lifecycle._apply_container_edit(container, {"memory_limit": ""}, "app")
        self.assertEqual({"cpu": "1"}, container["resources"]["limits"])

    def test_compose_memory_limit_maps_to_container_ceiling(self):
        report = compose.convert("""services:
  app:
    image: nginx
    mem_limit: 64m
""", features=[])
        cfg = report["services"][0]["config"]
        self.assertEqual("64Mi", cfg["memory_limit"])
        self.assertEqual("64Mi", cfg["memory"], "the default request must not exceed a smaller limit")
        deploy_limit = compose.convert("""services:
  app:
    image: nginx
    deploy:
      resources:
        reservations:
          memory: 256m
        limits:
          memory: 1g
""", features=[])["services"][0]["config"]
        self.assertEqual(("256Mi", "1024Mi"), (deploy_limit["memory"], deploy_limit["memory_limit"]))


if __name__ == "__main__":
    unittest.main()
