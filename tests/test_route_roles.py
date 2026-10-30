"""docs/route-roles.md: the table of which role each route, API endpoint and
MCP tool needs, rendered from the code by scripts/render_route_roles.py. A
change to who may use a route then shows up in review as a changed row."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import render_route_roles as RR  # noqa: E402
import source_scan as SS  # noqa: E402


def handler_method(name):
    return "\n".join(found.text for found in SS.every_function(SS.SERVER_PY)[f"H.{name}"])


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

    def test_a_raised_role_or_a_new_route_is_not_a_lowering(self):
        table = RR.parse(RR.render())
        now = {k: ("admin", v[1]) for k, v in RR.current().items()}
        now[("route", "GET", "/api/brand-new")] = ("public", "")
        self.assertEqual([], RR.lowered(table, now))


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


class TheTableReadsTheGuard(unittest.TestCase):
    """The table repeats two of the guard's decisions: which paths need no
    session, and which take an API key. These fail when the guard changes
    either, so the table's rules are revisited with it."""

    def test_the_unsigned_paths_are_the_guards(self):
        self.assertIn("if (is_spa_route(path) or is_page_path(path) or is_public_path(path) or is_vendor_path(path) or\n"
                      "                (path.startswith(\"/js/\") and path.endswith(\".js\"))):\n"
                      "            return None", handler_method("_guard"))

    def test_the_key_paths_are_the_key_guards(self):
        self.assertIn('if not path.startswith("/api/v1/") and path not in (MCP.PATH, "/metrics"):',
                      handler_method("_api_key_guard"))
        self.assertEqual(("/api/v1/", "/mcp", "/metrics"), RR.KEY_PATHS)


if __name__ == "__main__":
    unittest.main()
