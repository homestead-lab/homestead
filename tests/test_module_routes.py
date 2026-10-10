"""Routes declared beside their handlers (homestead_routes.py, #363), and the
guard that keeps server.py to routing: it may only shrink."""
import re
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_route_policy as POLICY
import homestead_routes as ROUTES

SERVER = (ROOT / "server" / "server.py").read_text(encoding="utf-8")

# Lower these as routes move out of server.py; never raise them.
MAX_LINES = 11620
MAX_PATH_BRANCHES = 316


def handler_body(method):
    """The text of server.py's do_GET, do_POST, ... method."""
    lines = SERVER.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"def do_{method}(self"))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"    def |\S", lines[i])), len(lines))
    return "\n".join(lines[start:end])


class DeclaredRoutes(unittest.TestCase):
    def test_every_route_has_a_role_and_a_handler(self):
        table = ROUTES.table()
        self.assertTrue(table)
        for (method, path), route in table.items():
            self.assertIn(method, ("GET", "POST", "PUT", "DELETE"), path)
            self.assertTrue(path.startswith("/api/"), path)
            self.assertIn(route.role, ROUTES.ROLES, path)
            self.assertTrue(callable(route.handler), path)

    def test_a_route_and_its_role_are_written_once(self):
        for method, path in ROUTES.table():
            self.assertNotIn((method, path), POLICY.POLICY, f"{method} {path} is declared in its module")

    def test_the_policy_reads_the_module_role(self):
        self.assertEqual("viewer", POLICY.role("/api/firewall", "GET"))
        self.assertEqual("admin", POLICY.role("/api/firewall/save", "POST"))
        self.assertIsNone(POLICY.role("/api/firewall/save", "GET"))

    def test_server_py_has_no_branch_for_a_declared_route(self):
        for method, path in ROUTES.table():
            self.assertNotIn(f'p == "{path}"', handler_body(method), f"{method} {path} is answered by its module")

    def test_handlers_get_the_request(self):
        seen = []
        ROUTES._table[("POST", "/api/test/echo")] = ROUTES.Route("POST", "/api/test/echo", "admin",
                                                                  lambda request: seen.append(request) or {"ok": True}, "test")
        try:
            route = ROUTES.find("post", "/api/test/echo")
            answer = route.handler(ROUTES.Request("POST", "/api/test/echo", {}, {"a": 1}, "ada", "admin"))
        finally:
            del ROUTES._table[("POST", "/api/test/echo")]
        self.assertEqual({"ok": True}, answer)
        self.assertEqual({"a": 1}, seen[0].body)
        self.assertEqual("ada", seen[0].user)
        self.assertIsNone(ROUTES.find("GET", "/api/test/echo"))


class ThroughTheServer(unittest.TestCase):
    """A request for a declared route reaches its module through server.py's
    handler, after the guard, and its errors are answered as before."""

    def handler(self, method, path, body=None):
        import server
        handler = object.__new__(server.H)
        handler.path, handler.headers, handler.command = path, {}, method
        handler._guard = lambda path: False
        handler._body = lambda: body or {}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        handler.user, handler.role = "ada", "admin"
        return server, handler

    def test_a_post_reaches_the_module(self):
        server, handler = self.handler("POST", "/api/firewall/save", {"name": "web"})
        with mock.patch.object(server.FIREWALL, "save", return_value={"ok": True, "saved": "web"}) as save:
            handler.do_POST()
        save.assert_called_once_with({"name": "web"})
        self.assertEqual((200, {"ok": True, "saved": "web"}), handler._send.call_args.args)

    def test_a_get_reaches_the_module(self):
        server, handler = self.handler("GET", "/api/firewall")
        with mock.patch.object(server.FIREWALL, "inventory", return_value={"policies": []}):
            handler.do_GET()
        self.assertEqual((200, {"policies": []}), handler._send.call_args.args)

    def test_a_module_error_is_answered_as_server_py_answers_its_own(self):
        server, handler = self.handler("POST", "/api/firewall/delete", {"name": "gone"})
        with mock.patch.object(server.FIREWALL, "remove", side_effect=ValueError("The policy no longer exists")):
            handler.do_POST()
        code, body = handler._send.call_args.args
        self.assertEqual(400, code)
        self.assertIn("no longer exists", body["error"])


class SharedCache(unittest.TestCase):
    def test_a_module_route_answers_from_server_py_s_cache_under_the_same_key(self):
        # server.py drops "network" after a VIP repair; the route must see that.
        import server
        server._cache.pop("network", None)
        with mock.patch.object(server.NETWORK, "inventory", side_effect=[{"n": 1}, {"n": 2}]) as inventory:
            route = ROUTES.find("GET", "/api/network")
            first = route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer"))
            again = route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer"))
            self.assertEqual(({"n": 1}, {"n": 1}), (first, again))
            server._cache.pop("network", None)
            self.assertEqual({"n": 2}, route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer")))
        self.assertEqual(2, inventory.call_count)


class ServerStaysRouting(unittest.TestCase):
    """CONTRIBUTING.md: server.py routes requests; features live in modules.
    New routes go in their module's ROUTES, so these numbers only fall."""

    def test_server_py_does_not_grow(self):
        lines = len(SERVER.splitlines())
        self.assertLessEqual(lines, MAX_LINES, "server.py grew: put the new code in a homestead_*.py module")

    def test_no_new_path_branches(self):
        branches = len(re.findall(r'\bp == "/api/', SERVER))
        self.assertLessEqual(branches, MAX_PATH_BRANCHES,
                             "a new route belongs in its module's ROUTES (homestead_routes.py), not an if in server.py")


if __name__ == "__main__":
    unittest.main()
