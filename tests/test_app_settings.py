import sys
import calendar
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import server


class AppSettingsTests(unittest.TestCase):
    def test_defaults_are_complete(self):
        settings = server.validate_app_settings({})
        self.assertEqual({"cpu", "memory", "disk", "temperature"}, set(settings["thresholds"]))
        self.assertLess(settings["thresholds"]["memory"]["warning"],
                        settings["thresholds"]["memory"]["critical"])
        self.assertEqual("approval_required", settings["updates"]["policy"])
        self.assertEqual(1, settings["smart"]["pending_critical"])

    def test_partial_update_uses_defaults_for_other_metrics(self):
        settings = server.validate_app_settings({
            "thresholds": {"memory": {"warning": 76, "critical": 91}},
        })
        self.assertEqual({"warning": 76, "critical": 91}, settings["thresholds"]["memory"])
        self.assertEqual(server.DEFAULT_APP_SETTINGS["thresholds"]["cpu"], settings["thresholds"]["cpu"])

    def test_warning_must_be_below_critical(self):
        with self.assertRaisesRegex(ValueError, "memory thresholds"):
            server.validate_app_settings({
                "thresholds": {"memory": {"warning": 90, "critical": 90}},
            })

    def test_percentage_and_temperature_limits_differ(self):
        settings = server.validate_app_settings({
            "thresholds": {"temperature": {"warning": 92, "critical": 110}},
        })
        self.assertEqual(110, settings["thresholds"]["temperature"]["critical"])
        with self.assertRaisesRegex(ValueError, "disk thresholds"):
            server.validate_app_settings({
                "thresholds": {"disk": {"warning": 90, "critical": 110}},
            })

    def test_update_policy_validation(self):
        settings = server.validate_app_settings({"updates": {
            "policy": "maintenance_window", "notify_available": False,
            "notify_failures": True,
            "maintenance": {"days": [0, 2, 4], "start": "23:30", "duration_minutes": 90},
        }})
        self.assertEqual([0, 2, 4], settings["updates"]["maintenance"]["days"])
        with self.assertRaisesRegex(ValueError, "update policy"):
            server.validate_app_settings({"updates": {"policy": "automatic"}})
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            server.validate_app_settings({"updates": {
                "maintenance": {"start": "2am"}}})

    def test_maintenance_window_handles_midnight_and_approval(self):
        settings = server.validate_app_settings({"updates": {
            "policy": "maintenance_window",
            "maintenance": {"days": [0], "start": "23:30", "duration_minutes": 90},
        }})
        monday = calendar.timegm(time.strptime("2026-09-21T23:45:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        tuesday_carry = calendar.timegm(time.strptime("2026-09-22T00:30:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        tuesday_closed = calendar.timegm(time.strptime("2026-09-22T01:15:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(server.update_policy_status(settings, monday)["allows_install"])
        self.assertTrue(server.update_policy_status(settings, tuesday_carry)["allows_install"])
        self.assertFalse(server.update_policy_status(settings, tuesday_closed)["allows_install"])
        with self.assertRaisesRegex(PermissionError, "explicit approval"):
            server.enforce_update_policy({}, settings, monday)
        self.assertTrue(server.enforce_update_policy({"approved": True}, settings, monday)["allows_install"])

    def test_notify_only_is_enforced_server_side(self):
        settings = server.validate_app_settings({"updates": {"policy": "notify_only"}})
        with self.assertRaisesRegex(PermissionError, "notify only"):
            server.enforce_update_policy({"approved": True}, settings)

    def test_image_cleanup_is_admin_only(self):
        self.assertEqual("admin", server.needed_role("/api/images/cleanup", "POST"))

    def test_smart_policy_and_test_authorization(self):
        settings = server.validate_app_settings({"smart": {
            "temperature": {"warning": 60, "critical": 72},
            "reallocated_warning": 4, "pending_critical": 2,
            "uncorrectable_critical": 3, "notify_failures": False,
        }})
        self.assertEqual(72, settings["smart"]["temperature"]["critical"])
        self.assertFalse(settings["smart"]["notify_failures"])
        with self.assertRaisesRegex(ValueError, "drive temperature"):
            server.validate_app_settings({"smart": {
                "temperature": {"warning": 80, "critical": 70}}})
        self.assertEqual("viewer", server.needed_role("/api/node/smart", "GET"))
        self.assertEqual("admin", server.needed_role("/api/node/smart/test", "POST"))

    def test_volume_impact_is_viewable_but_deletion_is_admin_only(self):
        self.assertEqual("viewer", server.needed_role("/api/volumes/delete-plan", "GET"))
        self.assertEqual("admin", server.needed_role("/api/volumes/delete", "POST"))


if __name__ == "__main__":
    unittest.main()
