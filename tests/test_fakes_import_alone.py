"""Every shared fake in tests/fakes loads in a fresh interpreter that has only
tests/ on its path, the way a test file run on its own or first starts.

tests/fakes/kube.py imports homestead_kube from server/. A test file that imported
the fake before putting server/ on the path loaded only when an earlier test file
had done it for it: it failed alone and when the suite ran in another order."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FAKES = sorted(path.stem for path in (TESTS / "fakes").glob("*.py") if path.stem != "__init__")


class FakesImportAloneTests(unittest.TestCase):
    def test_the_fakes_are_found(self):
        self.assertIn("kube", FAKES)

    def test_each_fake_imports_with_only_tests_on_the_path(self):
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        for name in FAKES:
            with self.subTest(fake=name):
                done = subprocess.run(
                    [sys.executable, "-c", "import importlib, sys; importlib.import_module(sys.argv[1])",
                     f"fakes.{name}"],
                    cwd=TESTS, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
