import os
import runpy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))


class PowerDefaultTests(unittest.TestCase):
    def setting(self, value):
        with mock.patch.dict(os.environ):
            if value is None:
                os.environ.pop("ENABLE_NODE_POWER", None)
            else:
                os.environ["ENABLE_NODE_POWER"] = value
            # Evaluate startup configuration without rebinding the running
            # server's imported lifecycle module or calling Kubernetes.
            return runpy.run_path(str(ROOT / "server" / "homestead_lifecycle.py"))["NODE_POWER_ENABLED"]

    def test_unset_enables_guarded_power_by_default(self):
        self.assertTrue(self.setting(None))

    def test_explicit_opt_out_and_invalid_values_stay_disabled(self):
        for value in ("false", "FALSE", "0", "no", "", "invalid"):
            with self.subTest(value=value):
                self.assertFalse(self.setting(value))

    def test_explicit_opt_in_is_case_and_whitespace_tolerant(self):
        for value in ("true", "1", "yes", " TRUE "):
            with self.subTest(value=value):
                self.assertTrue(self.setting(value))


if __name__ == "__main__":
    unittest.main()
