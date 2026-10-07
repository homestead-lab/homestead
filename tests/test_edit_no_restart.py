import copy
import unittest
from unittest import mock

import test_rollout_capacity as fixtures
import homestead_lifecycle as lifecycle


class EditRestartTests(unittest.TestCase):
    """Edit replaces the pods only when what they run with changes."""
    get = fixtures.RolloutCapacityTests.get

    def setUp(self):
        fixtures.RolloutCapacityTests.setUp(self)
        for patch in (mock.patch.object(lifecycle, "kget", side_effect=self.get),
                      mock.patch.object(lifecycle, "hardware_features", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        main = self.current["spec"]["template"]["spec"]["containers"][0]
        self.same = {"original_name": "main", "name": "main",
                     "memory": main["resources"]["requests"]["memory"], "memory_limit": main["resources"]["limits"]["memory"]}

    def edit(self, **extra):
        cfg = {"ns": "lab", "name": "shared", "containers": [dict(self.same, **extra.pop("container", {}))], **extra}
        return lifecycle.prepare_edit(cfg, copy.deepcopy(self.current))["deployment"]

    def test_a_logo_or_monitoring_change_keeps_the_pods(self):
        for extra in ({"icon": "/api/icons/" + "a" * 64 + ".png", "icon_source": "https://example.com/a.png"},
                      {"monitoring": {"mode": "http", "path": "/health"}},
                      {"monitoring": {"mode": "off"}}):
            with self.subTest(extra=list(extra)):
                saved = self.edit(**copy.deepcopy(extra))
                self.assertFalse(lifecycle.restarts(self.current, saved))
                self.assertNotIn("homestead.io/editedAt", (saved["spec"]["template"].get("metadata") or {}).get("annotations") or {})

    def test_monitoring_is_saved_on_the_deployment(self):
        saved = self.edit(monitoring={"mode": "http", "path": "/health"})
        self.assertEqual("/health", saved["metadata"]["annotations"]["homestead.io/uptime"])
        self.current = saved
        cleared = self.edit(monitoring={"mode": "auto"})
        self.assertNotIn("homestead.io/uptime", cleared["metadata"].get("annotations") or {})

    def test_a_bad_path_is_refused_before_anything_is_written(self):
        with self.assertRaises(ValueError):
            self.edit(monitoring={"mode": "http", "path": "no slash"})

    def test_a_change_the_pods_run_with_still_restarts_them(self):
        saved = self.edit(container={"memory": "1Gi", "memory_limit": "1Gi"})
        self.assertTrue(lifecycle.restarts(self.current, saved))
        self.assertIn("homestead.io/editedAt", saved["spec"]["template"]["metadata"]["annotations"])


if __name__ == "__main__":
    unittest.main()
