"""A settings save changes only the sections it sends, and one based on
settings that have changed since is refused (#601)."""
import copy
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_update_checks as CHECKS


STORED = server.APP_SETTINGS.validate({
    "site_name": "Shed", "catalog_url": "https://example.com/feed",
    "updates": {"policy": "maintenance_window", "notify_failures": False,
                "maintenance": {"days": [5, 6], "start": "03:00", "duration_minutes": 60}},
    "placement": {"rebalance": "suggest", "mode": "spread"}})


class SettingsMergeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self.forget)
        self.forget()

    def forget(self):
        for key in [k for k in server._cache if k.startswith("settings")]:
            server._cache.pop(key, None)

    def call(self, method, body=None, revision="7", put_fails=None, before=None):
        current = {"metadata": {"resourceVersion": revision},
                   "data": {"settings.json": json.dumps(STORED)}}
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/settings", {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server.KUBE_HELPERS, "kget", return_value=copy.deepcopy(current)), \
             mock.patch.object(server.KUBE_HELPERS, "ksend", side_effect=put_fails) as send, \
             mock.patch.object(CHECKS, "invalidate"), \
             mock.patch.object(server.SETTINGS_HELPERS, "_fleet_rename"):
            if before:
                before()
            getattr(handler, "do_" + method)()
        return handler._send.call_args.args, send

    def saved(self, send):
        send.assert_called_once()
        method, path, body = send.call_args.args
        self.assertEqual("PUT", method)
        return json.loads(body["data"]["settings.json"])

    def test_the_settings_carry_the_revision_they_were_read_at(self):
        result, _ = self.call("GET")
        self.assertEqual(200, result[0])
        self.assertEqual("7", result[1]["revision"])

    def test_the_settings_carry_the_revision_when_another_reader_filled_the_cache_first(self):
        # The placement loop, MCP and others cache the settings without one.
        fill = lambda: server.CACHE.cached("settings", 15, server.SETTINGS_HELPERS.get_app_settings)
        result, _ = self.call("GET", before=fill)
        self.assertEqual("7", result[1]["revision"])

    def test_a_save_of_one_section_keeps_every_other_section(self):
        result, send = self.call("POST", {"mcp": {"enabled": True}})
        self.assertEqual(200, result[0])
        saved = self.saved(send)
        self.assertTrue(saved["mcp"]["enabled"])
        for section in ("site_name", "catalog_url", "updates", "placement", "thresholds", "smart"):
            self.assertEqual(STORED[section], saved[section], section)
        self.assertEqual(saved, {k: v for k, v in result[1].items() if k not in ("ok", "revision")})

    def test_a_save_of_part_of_a_section_keeps_the_rest_of_it(self):
        window = {"days": [0], "start": "01:00", "duration_minutes": 30}
        result, send = self.call("POST", {"updates": {"maintenance": window}})
        self.assertEqual(200, result[0])
        saved = self.saved(send)
        self.assertEqual(window, saved["updates"]["maintenance"])
        self.assertEqual("maintenance_window", saved["updates"]["policy"])
        self.assertFalse(saved["updates"]["notify_failures"])

    def test_a_save_based_on_the_current_revision_is_made(self):
        result, send = self.call("POST", {**STORED, "site_name": "Barn", "revision": "7"})
        self.assertEqual(200, result[0])
        self.assertEqual("Barn", self.saved(send)["site_name"])
        self.assertEqual("7", send.call_args.args[2]["metadata"]["resourceVersion"])

    def test_a_save_based_on_settings_changed_since_is_refused_and_changes_nothing(self):
        result, send = self.call("POST", {**STORED, "site_name": "Barn", "revision": "6"})
        self.assertEqual(409, result[0])
        send.assert_not_called()
        self.assertIn("changed", result[1]["error"])
        self.assertEqual("Shed", result[1]["settings"]["site_name"])
        self.assertEqual("7", result[1]["settings"]["revision"])

    def test_a_save_that_loses_the_race_to_kubernetes_is_refused_too(self):
        conflict = urllib.error.HTTPError("/api/v1", 409, "Conflict", {}, io.BytesIO(b"{}"))
        result, send = self.call("POST", {"site_name": "Barn", "revision": "7"}, put_fails=conflict)
        self.assertEqual(409, result[0])
        self.assertIn("changed", result[1]["error"])

    def test_a_save_before_the_settings_were_ever_stored_needs_no_revision(self):
        missing = urllib.error.HTTPError("/api/v1", 404, "Not Found", {}, io.BytesIO(b"{}"))
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=missing), \
             mock.patch.object(server.KUBE_HELPERS, "ksend") as send, \
             mock.patch.object(CHECKS, "invalidate"), \
             mock.patch.object(server.SETTINGS_HELPERS, "_fleet_rename"):
            saved = server.SETTINGS_HELPERS.save_app_settings({"site_name": "Barn", "revision": ""})
        self.assertEqual("Barn", saved["site_name"])
        self.assertEqual("POST", send.call_args.args[0])

    def test_a_save_lets_the_kept_app_feed_go(self):
        # A different catalogue address is a different catalogue.
        import homestead_deploy_appstore as APPSTORE
        self.addCleanup(APPSTORE.forget_feed)
        APPSTORE._feed.update(source="https://example.com/feed", apps=[{"name": "kept"}], at=1e12)
        missing = urllib.error.HTTPError("/api/v1", 404, "Not Found", {}, io.BytesIO(b"{}"))
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=missing), \
             mock.patch.object(server.KUBE_HELPERS, "ksend"), \
             mock.patch.object(CHECKS, "invalidate"), \
             mock.patch.object(server.SETTINGS_HELPERS, "_fleet_rename"):
            server.SETTINGS_HELPERS.save_app_settings({"catalog_url": "https://mirror.example.com/f.json", "revision": ""})
        self.assertIsNone(APPSTORE._feed["apps"])

    def test_an_invalid_section_is_refused_and_changes_nothing(self):
        result, send = self.call("POST", {"mcp": {"enabled": "yes"}})
        self.assertEqual(400, result[0])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
