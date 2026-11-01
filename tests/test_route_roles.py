"""docs/route-roles.md: the table of which role each route, API endpoint and
MCP tool needs, rendered from the code by scripts/render_route_roles.py. A
change to who may use a route then shows up in review as a changed row."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import homestead_mcp as MCP  # noqa: E402
import homestead_web_paths as WEB  # noqa: E402
import render_route_roles as RR  # noqa: E402
import server  # noqa: E402


class TableIsCurrent(unittest.TestCase):
    def test_the_checked_in_table_matches_the_code(self):
        committed = RR.OUT.read_text(encoding="utf-8")
        self.assertEqual(RR.render(), committed,
                         "docs/route-roles.md is out of date: run python scripts/render_route_roles.py")

    def test_check_mode_says_whether_the_file_is_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "route-roles.md"
            out.write_text("old\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as said:
                self.assertEqual(1, RR.main(["--check"], out))
            self.assertIn("out of date", said.getvalue())
            self.assertEqual("old\n", out.read_text(encoding="utf-8"), "--check writes nothing")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, RR.main([], out))
                self.assertEqual(0, RR.main(["--check"], out))
            self.assertEqual(RR.render(), out.read_text(encoding="utf-8"))


class RolesNeverGoDownUnseen(unittest.TestCase):
    """The ratchet: a role that is lower in the code than in the checked-in
    table fails by name, so a lowering is always a changed row in review."""

    def test_no_role_in_the_code_is_lower_than_in_the_table(self):
        committed = RR.parse(RR.OUT.read_text(encoding="utf-8"))
        self.assertEqual([], RR.lowered(committed, RR.current()),
                         "a role went down: if that is meant, run python scripts/render_route_roles.py "
                         "so the change shows in docs/route-roles.md")

    def test_a_lowered_route_is_named(self):
        table = RR.parse(RR.render())
        route = next(k for k, v in table.items() if k[0] == "route" and v[0] == "admin")
        now = dict(RR.current())
        now[route] = ("viewer", now[route][1])
        self.assertEqual([f"{route[1]} {route[2]}: admin -> viewer"], RR.lowered(table, now))

    def test_a_route_that_becomes_public_is_named(self):
        table = RR.parse(RR.render())
        now = {k: ("public", v[1]) if k == ("route", "GET", "/api/addons") else v for k, v in RR.current().items()}
        self.assertEqual(["GET /api/addons: viewer -> public"], RR.lowered(table, now))

    def test_a_destructive_tool_that_no_longer_waits_for_a_person_is_named(self):
        table = RR.parse(RR.render())
        tool = next(k for k, v in table.items() if k[0] == "tool" and v[1] == "admin")
        now = dict(RR.current())
        now[tool] = (now[tool][0], "")
        self.assertEqual([f"tool {tool[2]}: approved by admin -> approved by nobody"], RR.lowered(table, now))

    def test_a_raised_role_or_a_new_signed_in_route_is_not_a_lowering(self):
        table = RR.parse(RR.render())
        now = {k: ("admin", v[1]) for k, v in RR.current().items()}
        now[("route", "GET", "/api/brand-new")] = ("viewer", "")
        self.assertEqual([], RR.lowered(table, now))

    def test_a_new_public_or_key_row_is_named(self):
        table = RR.parse(RR.render())
        now = dict(RR.current())
        now[("route", "GET", "/api/brand-new")] = ("public", "")
        now[("tool", "-", "brand_new_tool")] = ("key", "")
        self.assertEqual(["GET /api/brand-new: new, public", "tool brand_new_tool: new, key"],
                         RR.lowered(table, now))


class WhatTheTableSays(unittest.TestCase):
    def setUp(self):
        self.rows = RR.current()

    def test_every_module_route_and_policy_entry_is_listed(self):
        import homestead_route_policy as POLICY
        import homestead_routes as ROUTES
        for method, path in [*ROUTES.table(), *POLICY.POLICY]:
            self.assertIn(("route", method, path), self.rows)

    def test_a_route_the_guard_lets_through_unsigned_says_public(self):
        self.assertEqual("public", self.rows[("route", "POST", "/api/auth/login")][0])
        self.assertEqual("public", self.rows[("route", "GET", "/api/icons/*")][0])
        self.assertEqual("public", self.rows[("route", "ANY", "/healthz")][0])

    def test_server_py_own_routes_are_listed(self):
        self.assertEqual("operator", self.rows[("route", "GET", "/api/console")][0])
        self.assertEqual("admin", self.rows[("route", "GET", "/api/node/shell")][0])
        self.assertEqual("key", self.rows[("route", "POST", "/mcp")][0])

    def test_api_endpoints_need_their_scope_role(self):
        self.assertEqual("viewer", self.rows[("api", "GET", "/api/v1/status")][0])
        self.assertEqual("operator", self.rows[("api", "POST", "/api/v1/containers/{namespace}/{name}/restart")][0])
        self.assertEqual("key", self.rows[("api", "GET", "/api/v1/whoami")][0])

    def test_every_mcp_tool_is_listed_with_its_approver_when_destructive(self):
        import homestead_agent_tools as TOOLS
        for tool in TOOLS.TOOLS:
            needs, approves = self.rows[("tool", "-", tool.name)]
            self.assertEqual(tool.role if tool.kind == TOOLS.DESTRUCTIVE else "", approves, tool.name)
        self.assertIn(("tool", "-", "get_agent_request"), self.rows)


class TheTableAndTheGuardShareTheirRules(unittest.TestCase):
    """Which paths need no session and which take an API key are decided once,
    in homestead_web_paths; server.py's guard and the table both ask it."""

    def setUp(self):
        for patch in (mock.patch.object(server, "_self_data_boot_pending", False),
                      mock.patch.object(server.CFACCESS, "enabled", return_value=False)):
            patch.start(); self.addCleanup(patch.stop)
        self.addCleanup(server.REQUEST_SCOPE.unbind)   # the guard binds the role; no request ends here

    def handler(self, path, headers=None):
        h = object.__new__(server.H)
        h.command, h.path, h.headers = "GET", path, dict(headers or {})
        h._send, h._who, h._client_ip = mock.Mock(), mock.Mock(return_value=None), mock.Mock(return_value="192.0.2.1")
        return h

    def test_the_guard_asks_needs_no_session(self):
        with mock.patch.object(WEB, "needs_no_session", return_value=True) as asked:
            h = self.handler("/api/addons")
            self.assertIsNone(h._guard("/api/addons"))
        asked.assert_called_with("/api/addons")
        h._who.assert_not_called()
        with mock.patch.object(WEB, "needs_no_session", return_value=False):
            h = self.handler("/healthz")
            self.assertTrue(h._guard("/healthz"))
        self.assertEqual(401, h._send.call_args.args[0])

    def test_the_key_guard_asks_takes_key(self):
        key = {"id": "k1", "name": "k", "scopes": ["read"]}
        with mock.patch.object(WEB, "takes_key", return_value=True) as asked, \
             mock.patch.object(server.API_KEYS, "verify", return_value=key):
            h = self.handler("/api/addons", {"Authorization": "Bearer hsk_x"})
            self.assertIsNone(h._guard("/api/addons"))
        asked.assert_called_with("/api/addons")
        with mock.patch.object(WEB, "takes_key", return_value=False):
            h = self.handler(MCP.PATH, {"Authorization": "Bearer hsk_x"})
            self.assertTrue(h._guard(MCP.PATH))
        self.assertEqual(401, h._send.call_args.args[0])
        self.assertTrue(WEB.takes_key(MCP.PATH), "the MCP endpoint takes a key")

    def test_the_table_asks_the_same_functions(self):
        with mock.patch.object(WEB, "needs_no_session", side_effect=lambda path: path == "/api/addons"):
            self.assertEqual("public", RR.current()[("route", "GET", "/api/addons")][0])
        with mock.patch.object(WEB, "takes_key", return_value=False):
            self.assertNotIn("| yes |", RR.render())

    def test_a_code_prefix_that_needs_a_session_is_refused_for_everyone(self):
        import homestead_route_policy as POLICY
        with mock.patch.object(POLICY, "CODE_PREFIXES", ("/api/other/",)):
            self.assertEqual("nobody", RR.current()[("route", "GET", "/api/other/*")][0])


if __name__ == "__main__":
    unittest.main()
