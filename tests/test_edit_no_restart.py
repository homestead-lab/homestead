import copy
import unittest
from unittest import mock

import test_rollout_capacity as fixtures
import homestead_lifecycle as lifecycle
import homestead_workload_edit as WORKLOAD_EDIT


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
        return WORKLOAD_EDIT.prepare_edit(cfg, copy.deepcopy(self.current))["deployment"]

    def test_a_logo_or_monitoring_change_keeps_the_pods(self):
        for extra in ({"icon": "/api/icons/" + "a" * 64 + ".png", "icon_source": "https://example.com/a.png"},
                      {"monitoring": {"mode": "http", "path": "/health"}},
                      {"monitoring": {"mode": "off"}}):
            with self.subTest(extra=list(extra)):
                saved = self.edit(**copy.deepcopy(extra))
                self.assertFalse(WORKLOAD_EDIT.restarts(self.current, saved))
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

    def test_edit_saves_the_setting_and_restarts(self):
        import homestead_start_order as START
        db = {"metadata": {"name": "db", "namespace": "lab"},
              "spec": {"selector": {"matchLabels": {"app": "db"}},
                       "template": {"metadata": {"labels": {"app": "db"}}, "spec": {"containers": [{"name": "db"}]}}}}
        self.objects["/apis/apps/v1/deployments"] = {"items": [copy.deepcopy(self.current), db]}
        self.objects["/api/v1/services"] = {"items": [{"metadata": {"name": "db", "namespace": "lab"},
                                                       "spec": {"selector": {"app": "db"}, "ports": [{"port": 5432}]}}]}
        wanted = {"after": [{"ns": "lab", "app": "db"}], "timeout": 120, "on_timeout": "wait"}
        saved = self.edit(starts_after=wanted)
        self.assertEqual("homestead-start-gate", saved["spec"]["template"]["spec"]["initContainers"][0]["name"])
        self.assertEqual("rendered", START.gate_state(saved))
        self.assertTrue(WORKLOAD_EDIT.restarts(self.current, saved))
        self.current = saved
        self.objects["/apis/apps/v1/deployments"]["items"][0] = copy.deepcopy(saved)
        again = self.edit(starts_after=wanted)
        self.assertFalse(WORKLOAD_EDIT.restarts(saved, again), "the same setting saved again")
        self.assertEqual(saved, again)
        untouched = self.edit()
        self.assertEqual(saved, untouched, "an edit without it keeps the gate and the setting")

    def test_an_unrelated_edit_keeps_an_unreadable_setting_and_its_gate(self):
        """Edit's form shows {"unreadable": True} for a hand-edited annotation
        and sends it back with the rest: that must not remove it."""
        import homestead_start_order as START
        self.objects["/apis/apps/v1/deployments"] = {"items": [copy.deepcopy(self.current)]}
        self.objects["/api/v1/services"] = {"items": []}
        self.current["metadata"].setdefault("annotations", {})[START.KEY] = '{"after": "by hand"}'
        gate = {"name": START.GATE, "image": "example/gate:1"}
        self.current["spec"]["template"]["spec"]["initContainers"] = [gate]
        saved = self.edit(starts_after={"unreadable": True}, container={"memory": "1Gi", "memory_limit": "1Gi"})
        self.assertEqual('{"after": "by hand"}', saved["metadata"]["annotations"][START.KEY])
        self.assertEqual([gate], saved["spec"]["template"]["spec"]["initContainers"])

    def test_an_unrelated_edit_succeeds_when_a_dependency_is_gone(self):
        import homestead_start_order as START
        self.objects["/apis/apps/v1/deployments"] = {"items": [copy.deepcopy(self.current)]}
        self.objects["/api/v1/services"] = {"items": []}
        setting = {"after": [{"ns": "lab", "app": "gone", "service": "gone", "port": 5432}], "timeout": 300, "on_timeout": "start"}
        START.apply_to(self.current, setting)
        saved = self.edit(starts_after=copy.deepcopy(setting), container={"memory": "1Gi", "memory_limit": "1Gi"})
        self.assertEqual(setting, START.read(saved))
        self.assertEqual("rendered", START.gate_state(saved))

    def test_restore_gate_puts_a_changed_gate_back(self):
        """A gate changed outside Homestead: the setting sent back as it is
        changes nothing, and with restore_gate (Edit's "Put the start gate
        back") the gate is rendered from the setting again."""
        import homestead_start_order as START
        self.objects["/apis/apps/v1/deployments"] = {"items": [copy.deepcopy(self.current)]}
        self.objects["/api/v1/services"] = {"items": []}
        setting = {"after": [{"ns": "lab", "app": "gone", "service": "gone", "port": 5432}], "timeout": 300, "on_timeout": "start"}
        START.apply_to(self.current, setting)
        self.current["spec"]["template"]["spec"]["initContainers"][0]["image"] = "example/other:1"
        self.assertEqual("differs", START.gate_state(self.current))
        kept = self.edit(starts_after=copy.deepcopy(setting))
        self.assertEqual("differs", START.gate_state(kept))
        restored = self.edit(starts_after={**copy.deepcopy(setting), "restore_gate": True})
        self.assertEqual("rendered", START.gate_state(restored))
        self.assertEqual(setting, START.read(restored), "the flag is not stored")

    def test_every_fixture_edit_keeps_gate_and_annotation_in_step(self):
        import homestead_start_order as START
        self.objects["/apis/apps/v1/deployments"] = {"items": [copy.deepcopy(self.current)]}
        self.objects["/api/v1/services"] = {"items": []}
        for extra in ({}, {"icon": "/api/icons/" + "a" * 64 + ".png"}, {"container": {"memory": "1Gi", "memory_limit": "1Gi"}},
                      {"starts_after": None}, {"starts_after": {"after": []}}, {"monitoring": {"mode": "off"}}):
            with self.subTest(extra=list(extra)):
                saved = self.edit(**copy.deepcopy(extra))
                self.assertIn(START.gate_state(saved), ("rendered", "none"))

    def test_a_change_the_pods_run_with_still_restarts_them(self):
        saved = self.edit(container={"memory": "1Gi", "memory_limit": "1Gi"})
        self.assertTrue(WORKLOAD_EDIT.restarts(self.current, saved))
        self.assertIn("homestead.io/editedAt", saved["spec"]["template"]["metadata"]["annotations"])


if __name__ == "__main__":
    unittest.main()
