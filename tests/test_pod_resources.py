import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_pod_resources as resources


def container(name, request, limit=None, sidecar=False):
    row = {"name": name, "resources": {"requests": {"memory": request}}}
    if limit:
        row["resources"]["limits"] = {"memory": limit}
    if sidecar:
        row["restartPolicy"] = "Always"
    return row


class PodResourceTests(unittest.TestCase):
    def test_quantities(self):
        for value, expected in [("1Gi", 1024**3), ("1e9", 10**9), ("2.5G", 2500000000),
                                ("1k", 1000), ("400m", 1), (".5Ki", 512)]:
            self.assertEqual(expected, resources.quantity(value))
        self.assertEqual(500, resources.quantity(".5", "cpu"))
        self.assertEqual(1, resources.quantity("500u", "cpu"))
        with self.assertRaises(ValueError):
            resources.quantity("bad")

    def test_init_peak_includes_preceding_but_not_later_sidecars(self):
        spec = {"containers": [container("app", "1Gi", "2Gi")], "initContainers": [
            container("proxy", "1Gi", "2Gi", True), container("setup", "3Gi", "4Gi"),
            container("logger", "1Gi", "1Gi", True)], "overhead": {"memory": "128Mi"}}
        self.assertEqual(4 * 1024**3 + 128 * 1024**2, resources.pod_request(spec, "memory"))
        self.assertEqual((6 * 1024**3 + 128 * 1024**2, []), resources.memory_estimate(spec))

    def test_app_plus_all_sidecars_can_be_the_peak(self):
        spec = {"containers": [container("app", "4Gi")], "initContainers": [
            container("setup", "1Gi"), container("proxy", "2Gi", sidecar=True)]}
        self.assertEqual(6 * 1024**3, resources.pod_request(spec, "memory"))
        self.assertEqual(["app", "setup", "proxy"], resources.memory_estimate(spec)[1])

    def test_pod_budget_overrides_container_budget_and_bounds_unlimited_children(self):
        spec = {"containers": [container("app", "1Gi")], "resources": {
            "requests": {"memory": "3Gi"}, "limits": {"memory": "4Gi"}}, "overhead": {"memory": "1Gi"}}
        self.assertEqual(4 * 1024**3, resources.pod_request(spec, "memory"))
        self.assertEqual((5 * 1024**3, []), resources.memory_estimate(spec))

    def test_missing_request_defaults_to_limit_but_explicit_zero_is_kept(self):
        spec = {"containers": [{"resources": {"limits": {"memory": "1Gi"}}}]}
        self.assertEqual(1024**3, resources.pod_request(spec, "memory"))
        spec["containers"][0]["resources"]["requests"] = {"memory": "0"}
        self.assertEqual(0, resources.pod_request(spec, "memory"))

    def test_in_place_downsize_does_not_free_resources_before_status_catches_up(self):
        pod = {"spec": {"nodeName": "node", "containers": [container("app", "1Gi")]}, "status": {
            "containerStatuses": [{"name": "app", "allocatedResources": {"memory": "3Gi"},
                                   "resources": {"requests": {"memory": "2Gi"}}}]}}
        booked, pending, resize, dra = resources.reservations([pod])
        self.assertEqual(3 * 1024**3, booked["node"]["memory"])
        self.assertTrue(resize)
        self.assertEqual(0, pending)


if __name__ == "__main__":
    unittest.main()
