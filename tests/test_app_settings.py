import sys
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


if __name__ == "__main__":
    unittest.main()
