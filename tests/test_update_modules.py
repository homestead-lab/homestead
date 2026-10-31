"""homestead_updates split by feature (#623): the work lives in
homestead_update_registry, homestead_update_checks and homestead_update_rollouts,
and homestead_updates keeps the bound state and the routes. Other modules,
server.py and tests read and replace each name where it is defined
(test_split_reexports)."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_update_checks as CHECKS
import homestead_update_registry as REGISTRY
import homestead_update_rollouts as ROLLOUTS
import homestead_updates as UPDATES

PARTS = (REGISTRY, CHECKS, ROLLOUTS)


class UpdateModuleTests(unittest.TestCase):
    def test_each_module_can_be_imported_first(self):
        for name in ("homestead_update_registry", "homestead_update_checks", "homestead_update_rollouts",
                     "homestead_updates"):
            done = subprocess.run([sys.executable, "-c", f"import {name}, homestead_updates as U, homestead_update_checks as C, "
                                   "homestead_update_rollouts as R; C.report; R.progress; U.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_are_unchanged(self):
        self.assertEqual({
            ("GET", "/api/image-updates"), ("GET", "/api/image-updates/progress"),
            ("GET", "/api/image-updates/scan-progress"), ("POST", "/api/image-updates/mode"),
            ("POST", "/api/image-updates/channel"), ("POST", "/api/image-updates/preview"),
            ("POST", "/api/image-updates/managed"), ("POST", "/api/image-updates/apply"),
            ("POST", "/api/image-updates/rollback")}, set(UPDATES.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_a_client_set_on_homestead_updates_reaches_the_moved_code(self):
        # server.py binds kget on homestead_updates, and tests replace it there.
        asked = []
        with mock.patch.object(UPDATES, "kget", side_effect=lambda path: asked.append(path) or {"items": []}):
            ROLLOUTS.pull_state("lab", "app-1")
        self.assertEqual(1, len(asked))
        self.assertIn("/api/v1/namespaces/lab/events", asked[0])


if __name__ == "__main__":
    unittest.main()
