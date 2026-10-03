import ast
import base64
import copy
import json
import sys
import unittest
import urllib.error
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_auth as AUTH
import homestead_route_policy as POLICY
import server
import homestead_http as HTTP


class DashboardPreferencesTests(unittest.TestCase):
    def test_health_widgets_are_valid_account_preferences(self):
        layout = {"version": 1, "items": [{"id": key, "width": 6, "height": 0}
                  for key in ("health", "workloads", "containers", "vms", "backups", "updates", "jobs")]}
        self.assertEqual(AUTH._dashboard_layout(layout), layout)

    def test_node_width_and_display_settings_persist_with_the_account(self):
        for width in (4,6,8,12):
            layout={"version":1,"items":[{"id":"nodes","width":width,"height":360,"display":"detailed"}]}
            saved=self.save(AUTH.dashboard_preferences("alice")["revision"],layout)
            self.assertEqual(layout,AUTH.dashboard_preferences("alice")["layout"])
        for display in ("other",{},None):
            with self.assertRaises(ValueError):
                AUTH._dashboard_layout({"version":1,"items":[{"id":"nodes","width":6,"height":0,"display":display}]})
        with self.assertRaises(ValueError):
            AUTH._dashboard_layout({"version":1,"items":[{"id":"compute","width":6,"height":0,"display":"compact"}]})

    def test_short_resource_lists_persist(self):
        layout={"version":1,"items":[{"id":"containers","width":12,"height":240},{"id":"vms","width":4,"height":240}]}
        self.save(AUTH.dashboard_preferences("alice")["revision"],layout)
        self.assertEqual(layout,AUTH.dashboard_preferences("alice")["layout"])

    def setUp(self):
        self.store = {"users": {"alice": {"role": "viewer", "ver": 1}, "bob": {"role": "admin", "ver": 2}}, "signing_key": "unchanged"}
        self.rv = 1
        self.before_save = None
        self.slow = False
        self.enterContext(patch.object(AUTH, "_store_cache", {"at": 0, "data": None}))
        self.enterContext(patch.object(AUTH, "kget", self.get))
        self.enterContext(patch.object(AUTH, "ksend", self.send))
        self.enterContext(patch.object(AUTH, "SECRET_NAME", lambda: "homestead-auth"))
        self.layout = {"version": 1, "items": [{"id": "portal", "width": 6, "height": 0}]}

    def get(self, path):
        if self.slow:
            raise TimeoutError("offline")
        return {"metadata": {"resourceVersion": str(self.rv)}, "data": {"store.json": base64.b64encode(json.dumps(self.store).encode()).decode()}}

    def send(self, method, path, body):
        if self.before_save:
            action, self.before_save = self.before_save, None
            action()
            self.rv += 1
        if body["metadata"].get("resourceVersion") != str(self.rv):
            raise urllib.error.HTTPError(path, 409, "conflict", None, None)
        self.store = json.loads(base64.b64decode(body["data"]["store.json"]))
        self.rv += 1
        return {"metadata": {"resourceVersion": str(self.rv)}}

    def save(self, revision=None, layout=None):
        return AUTH.save_dashboard_preferences("alice", {"revision": revision, "layout": self.layout if layout is None else layout})

    def test_persists_across_replica_cache_reset_and_isolates_accounts(self):
        self.assertEqual({"revision": None, "layout": None}, AUTH.dashboard_preferences("alice"))
        result = self.save()
        AUTH._store_cache.update(at=0, data=None)
        self.assertEqual(result, AUTH.dashboard_preferences("alice"))
        self.assertIsNone(AUTH.dashboard_preferences("bob")["layout"])
        self.assertEqual("unchanged", self.store["signing_key"])
        self.assertEqual(1, self.store["users"]["alice"]["ver"])
        result["layout"]["items"].clear()
        self.assertEqual(1, len(AUTH.dashboard_preferences("alice")["layout"]["items"]))

    def test_rejects_stale_session_and_preserves_empty_layout(self):
        first = self.save()
        empty = {"version": 1, "items": []}
        second = self.save(first["revision"], empty)
        with self.assertRaises(AUTH.StoreConflict):
            self.save(first["revision"])
        self.assertEqual(second, AUTH.dashboard_preferences("alice"))

    def test_replica_collision_rechecks_the_dashboard_revision(self):
        winner = {"revision": "another-session", "layout": {"version": 1, "items": []}}
        self.before_save = lambda: self.store["users"]["alice"].update(dashboard=winner)
        with self.assertRaises(AUTH.StoreConflict):
            self.save()
        self.assertEqual(winner, AUTH.dashboard_preferences("alice"))

    def test_unrelated_account_changes_are_preserved_on_retry(self):
        self.before_save = lambda: self.store["users"]["bob"].update(ver=99)
        self.save()
        self.assertEqual(99, self.store["users"]["bob"]["ver"])

    def test_deleted_accounts_and_unavailable_store_do_not_write(self):
        AUTH.dashboard_preferences("alice")
        self.slow = True
        with self.assertRaises(AUTH.StoreUnavailable):
            self.save()
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.dashboard_preferences("alice")
        self.slow = False
        del self.store["users"]["alice"]
        with self.assertRaises(PermissionError):
            self.save()
        self.assertEqual(1, self.rv)

    def test_invalid_or_oversized_layouts_cannot_enter_account_store(self):
        variants = [None, {}, {"version": True, "items": []}, {"version": 1, "items": [{}]*9}]
        for field, value in [("id", "unknown"), ("id", {}), ("width", True), ("width", 100), ("height", -1)]:
            layout = copy.deepcopy(self.layout)
            layout["items"][0][field] = value
            variants.append(layout)
        variants.append({"version": 1, "items": self.layout["items"]*2})
        for layout in variants:
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                AUTH.save_dashboard_preferences("alice", {"revision": None, "layout": layout})
        with self.assertRaises(ValueError):
            AUTH.save_dashboard_preferences("alice", {"revision": None, "layout": self.layout, "username": "bob"})
        self.assertEqual(1, self.rv)

    def test_switched_cluster_layout_uses_home_account_with_csrf_and_conflict_checks(self):
        listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H, max_connections=2)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        identity = self.enterContext(patch.object(server.H, "_who", return_value={"user": "alice", "role": "viewer", "stale": False}))
        self.enterContext(patch.object(server.FLEET, "self_id", return_value="home"))
        self.enterContext(patch.object(server.FLEET, "member", return_value={"id":"remote"}))
        forward = self.enterContext(patch.object(server.FLEET, "forward", side_effect=AssertionError("Account preferences must not be relayed")))
        def request(method, body=None, csrf=True):
            client = HTTPConnection(*listener.server_address, timeout=3)
            try:
                headers = {"Content-Type": "application/json", "Cookie": "homestead_cluster=remote", "X-Homestead-Cluster": "remote"}
                if csrf:
                    headers["X-Homestead-Auth"] = "1"
                client.request(method, "/api/auth/preferences/dashboard?user=bob", json.dumps(body) if body is not None else None, headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        body = {"revision": None, "layout": self.layout}
        self.assertEqual(403, request("POST", body, csrf=False)[0])
        status, saved = request("POST", body)
        self.assertEqual(200, status)
        self.assertEqual((200, saved), request("GET"))
        self.assertIsNone(AUTH.dashboard_preferences("bob")["layout"])
        self.assertEqual(409, request("POST", body)[0])
        identity.return_value = None
        self.assertEqual(401, request("GET")[0])
        self.assertEqual(401, request("POST", body)[0])
        forward.assert_not_called()

    def test_routes_are_personal_and_available_to_viewers(self):
        for method in ("GET", "POST"):
            self.assertEqual("viewer", POLICY.role("/api/auth/preferences/dashboard", method))
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server/server.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr in ("dashboard_preferences", "save_dashboard_preferences")]
        self.assertEqual(2, len(calls))
        for call in calls:
            self.assertEqual("self.user", ast.unparse(call.args[0]))


if __name__ == "__main__":
    unittest.main()
