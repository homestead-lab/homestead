"""homestead_deploy split by feature (#623): the App Store catalogue lives in
homestead_deploy_appstore and the Deployment builders in
homestead_deploy_manifests, and homestead_deploy keeps the bound helpers and
the routes. Other modules, server.py and tests read and replace each name
where it is defined (test_split_reexports)."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import homestead_deploy as DEPLOY
import homestead_deploy_appstore as APPSTORE
import homestead_deploy_manifests as MANIFESTS

PARTS = (APPSTORE, MANIFESTS)


class DeployModuleTests(unittest.TestCase):
    def test_each_module_can_be_imported_first(self):
        for name in ("homestead_deploy_appstore", "homestead_deploy_manifests", "homestead_deploy"):
            done = subprocess.run([sys.executable, "-c",
                                   f"import {name}, homestead_deploy as D, homestead_deploy_appstore as A, "
                                   "homestead_deploy_manifests as M; A.fetch_appstore; M.build_deployment; D.ROUTES"],
                                  cwd=SERVER, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, f"importing {name} first: {done.stderr}")

    def test_the_routes_are_unchanged(self):
        self.assertEqual({
            ("GET", "/api/deploy/options"), ("GET", "/api/appstore"), ("GET", "/api/appstore/app"),
            ("POST", "/api/appstore/install"), ("POST", "/api/preview"), ("POST", "/api/deploy"),
            ("POST", "/api/compose/parse"), ("POST", "/api/compose/preview"),
            ("POST", "/api/compose/apply")}, set(DEPLOY.ROUTES))
        for part in PARTS:
            self.assertFalse(hasattr(part, "ROUTES"), part.__name__)

    def test_helpers_bound_on_homestead_deploy_reach_the_moved_code(self):
        # server.py binds cached and get_app_settings on homestead_deploy (bind), and tests replace them there.
        with mock.patch.object(DEPLOY, "cached", lambda key, ttl, read: read(), create=True), \
                mock.patch.object(DEPLOY, "get_app_settings",
                                  lambda: {"catalog_url": "https://feed.example.com/apps.json"}, create=True):
            self.assertEqual("https://feed.example.com/apps.json", APPSTORE.catalog_source())

    def test_a_replacement_where_a_name_is_defined_reaches_the_other_part(self):
        # tests/test_deploy.py replaces analyze_deploy_intent on homestead_deploy_manifests.
        with mock.patch.object(MANIFESTS, "analyze_deploy_intent", return_value={"seen": True}):
            self.assertEqual({"seen": True}, APPSTORE.template_to_cfg({"name": "app", "repo": "nginx", "config": []}))


if __name__ == "__main__":
    unittest.main()
