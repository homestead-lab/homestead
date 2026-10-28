"""Routes declared beside their handlers (homestead_routes.py, #363), and the
guard that keeps server.py to routing: it may only shrink."""
import ast
import re
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_route_policy as POLICY
import homestead_routes as ROUTES
import homestead_web_paths as WEB_PATHS
import source_scan as SS

SERVER = SS.text(SS.SERVER_PY)

# Lower these as routes move out of server.py; never raise them.
MAX_LINES = 1848
MAX_PATH_BRANCHES = 3
# How far below MAX_LINES server.py may fall before MAX_LINES has to come down.
RATCHET_SLACK = 20
# Lines in server.py's alias block (names tests still patch on server). Only lowered.
ALIASES = 0
ALIAS_START = "# Names tests still patch on server; track 2 moves their tests."
ALIAS_END = "# End of the names tests still patch on server."

# The routes server.py's handler still answers itself, each with the reason it
# needs the handler. Every other route is declared in its module's ROUTES.
INLINE_ROUTES = (
    ("GET", "/api/console", "connection-bound: WebSocket upgrade"),
    ("GET", "/api/node/shell", "connection-bound: WebSocket upgrade"),
    ("GET", "/api/vm/console", "connection-bound: WebSocket upgrade"),
    ("GET", "/api/icons/", "connection-bound: serves a file with its own headers (path prefix)"),
    ("ANY", "/api/self/data/handoff/", "not a route here: the guard checks it before routing"),
    ("ANY", "/api/self/data/abandon", "not a route here: _guard lets the recovery action through while boot is pending; the route is in homestead_self_data_server"),
    ("ANY", "/api/fleet/home", "not a route here: the guard checks it before routing"),
    ("ANY", "/api/v1/", "not a route here: the guard checks it before routing"),
    ("ANY", "/api/auth/login", "not a route here: the guard checks it before routing"),
    ("ANY", "/api/auth/setup", "not a route here: the guard checks it before routing"),
    ("ANY", "/api/auth/state", "not a route here: the guard checks it before routing"),
    ("ANY", "/api/auth/", "not a route: a prefix _send tests before it hides a diagnostic report"),
    ("ANY", "/api/diagnostics", "not a route: a prefix _send tests before it hides a diagnostic report"),
)


def handler_methods(source=SS.SERVER_PY):
    """class H's own methods in server.py, by name: each name's defs, so a
    method written twice (if/else alternatives) is read whole."""
    return {name[2:]: defs for name, defs in SS.every_function(source).items()
            if name.startswith("H.") and name.count(".") == 1}


def handler_body(method, source=SS.SERVER_PY):
    """The text of server.py's do_GET, do_POST, ... method (every def of it)."""
    return "\n".join(found.text for found in handler_methods(source)[f"do_{method}"])


def literals_in(text):
    """Every "/api/..." path written in text, however it is quoted or matched
    (single or double quotes, a regex, an f-string, a "?" tail). A path compared
    with a constant (p == MCP.PATH) has no literal and cannot be seen here."""
    return set(re.findall(r'["\'](/api/[A-Za-z0-9/._-]*)', text))


def api_literals(method):
    """Every "/api/..." path written in server.py's do_GET, do_POST, ..."""
    return literals_in(handler_body(method))


def helper_literals(source=SS.SERVER_PY):
    """Every "/api/..." path written in class H outside its do_* methods:
    helpers such as _fleet_target check routes too, and so could a class
    attribute they read."""
    lines = (source if isinstance(source, SS.Code) else SS.text(source)).splitlines()
    cls = next(node for node in SS.tree(source).body if isinstance(node, ast.ClassDef) and node.name == "H")
    keep = set(range(cls.lineno, cls.end_lineno + 1))
    for name, defs in handler_methods(source).items():
        for found in defs if name.startswith("do_") else ():
            first = min([found.node.lineno, *(d.lineno for d in found.node.decorator_list)])
            keep -= set(range(first, found.node.end_lineno + 1))
    return literals_in("\n".join(lines[number - 1] for number in sorted(keep)))


class DeclaredRoutes(unittest.TestCase):
    def test_every_route_has_a_role_and_a_handler(self):
        import homestead_web_paths as WEB_PATHS
        table = ROUTES.table()
        self.assertTrue(table)
        for (method, path), route in table.items():
            self.assertIn(method, ROUTES.METHODS, path)
            # Outside /api/ only where the guard does not take it for a page,
            # which it lets through before sign-in (/readyz, /metrics: #736).
            # ModuleRoutesNeedCredentials below is this rule's safety net.
            self.assertTrue(path.startswith("/api/") or not WEB_PATHS.is_page_path(path), path)
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

    def test_prefix_routes_end_in_slash_star(self):
        for method, path in ROUTES.table():
            self.assertNotIn("*", path.removesuffix("/*"), f"{method} {path}: only a final /* makes a prefix route")

    def test_no_module_reads_forwarding_headers(self):
        """Who sent a request, and over what, is decided once in server.py
        (_client_ip, _over_tls, _via_cloudflare); a module reads request.conn."""
        for module in sorted((ROOT / "server").glob("homestead_*.py")):
            text = module.read_text(encoding="utf-8").lower()
            for header in ("cf-connecting-ip", "x-forwarded-for", "x-forwarded-proto", "cf-ray"):
                self.assertNotIn(header, text, f"{module.name} reads {header}: use request.conn")

    def test_h_wires_the_hook_and_keeps_handle_one_request(self):
        import server
        self.assertIs(server.H.after_request, ROUTES.after_request)
        self.assertNotIn("handle_one_request", vars(server.H))

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


def _bound_names(target):
    """The names an assignment target binds, through tuple and list unpacking."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def declares_routes(source):
    """Whether a module binds ROUTES at module level, as homestead_routes.table()
    finds it through getattr: assigned (ROUTES = ..., ROUTES: ... = ..., unpacked)
    or imported, directly or inside a top-level if, try or with. A def or class
    body does not count."""
    pending = list(SS.tree(source).body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Assign):
            names = [name for target in node.targets for name in _bound_names(target)]
        elif isinstance(node, ast.AnnAssign):
            names = _bound_names(node.target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or alias.name for alias in node.names]
        else:
            names = []
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for field in ("body", "orelse", "finalbody", "handlers"):
                    pending += getattr(node, field, None) or []
        if "ROUTES" in names:
            return True
    return False


class ModuleList(unittest.TestCase):
    """table() reads only the modules in MODULES (#870): a module with ROUTES
    left out of it loads without error, and its routes answer 404."""

    def test_every_module_with_routes_is_listed(self):
        unlisted = [path.stem for path in SS.server_modules() if declares_routes(path) and path.stem not in ROUTES.MODULES]
        self.assertEqual([], unlisted, "these declare ROUTES but are not in homestead_routes.MODULES, so their routes answer 404")

    def test_every_listed_module_exists(self):
        # A module may be listed without ROUTES (homestead_audit is, to register
        # its after-request hook), but every listed name is a module here.
        missing = [name for name in ROUTES.MODULES if not (SS.SERVER / f"{name}.py").is_file()]
        self.assertEqual([], missing, "listed in homestead_routes.MODULES, but there is no server/<name>.py")

    def test_only_a_top_level_routes_assignment_counts(self):
        for source, declares in (("ROUTES = {}", True), ("ROUTES: dict = {}", True),
                                 ("def f():\n    ROUTES = {}", False), ("# ROUTES = {}", False),
                                 ("ROUTES_V1 = {}", False), ("x = ROUTES", False),
                                 ("if X:\n    ROUTES = {}", True), ("try:\n    pass\nexcept E:\n    ROUTES = {}", True),
                                 ("ROUTES, X = {}, 1", True), ("from x import ROUTES", True),
                                 ("from x import TABLE as ROUTES", True), ("from x import ROUTES as OTHER", False),
                                 ("class C:\n    ROUTES = {}", False)):
            with self.subTest(source=source):
                self.assertIs(declares, declares_routes(SS.code(source)))


def cookie_calls(source):
    """The names a module calls that make or write one of Homestead's cookies:
    Cookie(...) or cookie_header(...), bare or through a module."""
    return sorted({node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                   for node in ast.walk(SS.tree(source)) if isinstance(node, ast.Call)
                   and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) in ("Cookie", "cookie_header")})


class CookieOwnersInSource(unittest.TestCase):
    """Every cookie a module sets is one it owns (#936): the module that makes
    a Cookie declares OWN_COOKIES and is in MODULES, where table() reads it, and
    only server.py writes a Set-Cookie line itself (cookie_header)."""

    def test_every_module_that_sets_a_cookie_owns_one(self):
        setters = {path.stem for path in SS.server_modules()
                   if path.stem != "homestead_routes" and "Cookie" in cookie_calls(path)}
        self.assertTrue(setters, "no module makes a Cookie: the scan has stopped seeing them")
        self.assertEqual(setters, set(ROUTES.owners().values()),
                         "a module that makes a Cookie must own it (OWN_COOKIES), and an owner must set it")
        self.assertEqual([], sorted(setters - set(ROUTES.MODULES)), "a module that sets a cookie must be in MODULES")

    def test_only_server_py_writes_a_set_cookie_line_itself(self):
        writers = [path.stem for path in SS.server_modules()
                   if path.stem != "homestead_routes" and "cookie_header" in cookie_calls(path)]
        self.assertEqual([], writers, "a module returns a Reply with cookies=(Cookie(...),); answer() checks its owner")

    def test_the_scan_sees_each_written_form(self):
        for source, found in (("Cookie(A, b)", ["Cookie"]), ("ROUTES.Cookie(A, b)", ["Cookie"]),
                              ("X.cookie_header(c, tls)", ["cookie_header"]), ("SimpleCookie()", []),
                              ("# Cookie(a)", []), ("'Cookie(a)'", [])):
            with self.subTest(source=source):
                self.assertEqual(found, cookie_calls(SS.code(source)))


class ServedMethods(unittest.TestCase):
    """A route is only reached through server.py's handler H, which answers a
    method only with a do_<METHOD> of its own (#870): any other method gets
    http.server's 501, so table() refuses a route declared with one."""

    def declare(self, method, path="/api/test/method"):
        """table() as it reads a module that declares only this route."""
        module = type(sys)("homestead_test_method")
        module.ROUTES = {(method, path): ("viewer", lambda request: {})}
        with mock.patch.dict(sys.modules, {"homestead_test_method": module}), \
                mock.patch.object(ROUTES, "MODULES", ("homestead_test_method",)), mock.patch.object(ROUTES, "_table", {}):
            return ROUTES.table()

    def test_the_methods_are_the_ones_h_serves(self):
        import server
        served = {name[3:] for name in dir(server.H) if name.startswith("do_")}
        self.assertEqual(served, set(ROUTES.METHODS), "homestead_routes.METHODS and server.H's do_* methods disagree")

    def test_a_method_h_does_not_serve_is_refused_naming_the_module_and_route(self):
        for method in ("PUT", "PATCH", "patch", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                with self.assertRaises(ValueError) as refused:
                    self.declare(method)
                self.assertIn("homestead_test_method", str(refused.exception))
                self.assertIn(f"{method} /api/test/method", str(refused.exception))

    def test_a_served_method_is_read_in_any_case(self):
        for method in ("GET", "post", "Delete"):
            with self.subTest(method=method):
                self.assertEqual({(method.upper(), "/api/test/method")}, set(self.declare(method)))


def guard(path, method="GET", headers=None):
    """server.H's guard for one request with no session, outside any fence."""
    import server
    h = object.__new__(server.H)
    h.command, h.path, h.headers = method, path, dict(headers or {})
    h._send = mock.Mock(); h._who = mock.Mock(return_value=None)
    h.client_address = ("192.0.2.10", 40000)
    with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
         mock.patch.object(server, "_self_data_boot_pending", False), \
         mock.patch.object(server, "require_self_data_write", return_value=None):
        result = h._guard(path)
    return h, result


class ModuleRoutesNeedCredentials(unittest.TestCase):
    """The net under DeclaredRoutes' rule (a module route may sit outside
    /api/ where the guard does not take it for a page): every module route
    that is not public answers 401 without a session or a key, so none is
    served before sign-in as a page would be (#736 review, gap 39)."""
    def test_metrics_and_readyz_are_not_page_paths(self):
        self.assertFalse(WEB_PATHS.is_page_path("/metrics"))
        self.assertFalse(WEB_PATHS.is_page_path("/readyz"))

    def test_a_module_route_at_a_bare_path_needs_credentials(self):
        """A route a module declares at /metrics is not let through as a page."""
        fake = ROUTES.Route("GET", "/metrics", "viewer", lambda request: "secret", "test")
        with mock.patch.dict(ROUTES._table, {("GET", "/metrics"): fake}):
            h, result = guard("/metrics")
        self.assertTrue(result)
        self.assertEqual(401, h._send.call_args.args[0])

    def test_the_walk_meets_a_route_outside_api(self):
        """Not an empty walk: the rule's exception is in the table."""
        self.assertTrue([path for _, path in ROUTES.table() if not path.startswith("/api/")])

    def test_every_module_route_that_is_not_public_needs_credentials(self):
        """Any module route, at any path, that is not public answers 401 to
        a request with no session and no key: none is served as a page."""
        for (method, path) in ROUTES.table():
            if WEB_PATHS.is_public_path(path):
                continue
            with self.subTest(method=method, path=path):
                h, result = guard(path, method)
                self.assertTrue(result, f"{method} {path} passed the guard without credentials")
                self.assertEqual(401, h._send.call_args.args[0])


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

    def test_a_delete_reaches_the_module(self):
        server, handler = self.handler("DELETE", "/api/workload/lab/web")
        import homestead_deploy_server as DEPLOY_SERVER
        with mock.patch.object(DEPLOY_SERVER, "delete_workload", return_value=["web"]) as delete:
            handler.do_DELETE()
        delete.assert_called_once_with("lab", "web")
        self.assertEqual((200, {"ok": True, "services": ["web"]}), handler._send.call_args.args[:2])

    def test_a_reply_keeps_its_own_status(self):
        server, handler = self.handler("POST", "/api/test/busy")
        ROUTES._table[("POST", "/api/test/busy")] = ROUTES.Route(
            "POST", "/api/test/busy", "admin", lambda request: ROUTES.Reply(409, {"error": "busy", "plan": []}), "test")
        try:
            handler.do_POST()
        finally:
            del ROUTES._table[("POST", "/api/test/busy")]
        self.assertEqual((409, {"error": "busy", "plan": []}, "application/json"), handler._send.call_args.args)

    def test_a_reply_keeps_its_status(self):
        # A drain that would strand workloads is refused with what it would strand.
        server, handler = self.handler("POST", "/api/node/drain", {"node": "node1"})
        impact = {"stranded": [{"ns": "lab", "name": "web"}]}
        with mock.patch.object(server.PLACE, "impact", return_value=impact), \
                mock.patch.object(server.LC, "drain") as drain:
            handler.do_POST()
        code, body = handler._send.call_args.args[:2]
        self.assertEqual((409, impact), (code, body["impact"]))
        drain.assert_not_called()

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
        import homestead_network_inventory as INV
        server._cache.pop("network", None)
        with mock.patch.object(INV, "inventory", side_effect=[{"n": 1}, {"n": 2}]) as inventory:
            route = ROUTES.find("GET", "/api/network")
            first = route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer"))
            again = route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer"))
            self.assertEqual(({"n": 1}, {"n": 1}), (first, again))
            server._cache.pop("network", None)
            self.assertEqual({"n": 2}, route.handler(ROUTES.Request("GET", "/api/network", {}, None, "ada", "viewer")))
        self.assertEqual(2, inventory.call_count)


class RouteLiterals(unittest.TestCase):
    def test_every_written_form_of_a_path_is_seen(self):
        for form in ('p == "/api/x/new"', "p == '/api/x/new'", 're.fullmatch(r"/api/x/new/[a-f0-9]{24}", p)',
                     'f"/api/x/new/{name}"', 'p.startswith("/api/x/new?")'):
            with self.subTest(form=form):
                self.assertTrue(any(path.startswith("/api/x/new") for path in literals_in(f"if {form}:")), form)

    def test_a_path_anywhere_in_class_h_outside_its_do_methods_is_seen(self):
        """Helpers, class attributes and comments of H count; do_* and
        anything outside H do not."""
        source = SS.code('def before():\n    return "/api/x/before"\n\n\n'
                         'class H(Base):\n    PUBLIC = ("/api/x/attribute",)\n\n'
                         '    @wrap\n    def do_GET(self):\n        if p == "/api/x/get":\n            pass\n\n'
                         '    def _helper(self):\n        return "/api/x/helper"\n    # "/api/x/comment"\n'
                         '    LATER = "/api/x/later"\n\n\ndef outside():\n    return "/api/x/outside"\n')
        self.assertEqual({"/api/x/attribute", "/api/x/helper", "/api/x/comment", "/api/x/later"},
                         helper_literals(source))


class TwoDefsOfOneHandler(unittest.TestCase):
    """A do_* written twice (if/else alternatives) is read whole: a route in
    either def is seen, and neither def counts as a helper."""
    SOURCE = SS.code('class H(Base):\n    if FAST:\n        def do_GET(self):\n            if p == "/api/x/first":\n'
                     '                pass\n    else:\n        def do_GET(self):\n            if p == "/api/x/second":\n'
                     '                pass\n\n    def _helper(self):\n        return "/api/x/helper"\n')

    def test_a_route_in_either_def_is_in_the_handler_body(self):
        body = handler_body("GET", self.SOURCE)
        self.assertIn("/api/x/first", body)
        self.assertIn("/api/x/second", body)

    def test_neither_def_counts_as_a_helper(self):
        self.assertEqual({"/api/x/helper"}, helper_literals(self.SOURCE))


class MovedHelpers(unittest.TestCase):
    def test_each_moved_helper_lives_in_its_module_and_is_not_late_bound_there(self):
        """MOVED (#527 track 2) says which module a helper moved to; _late looks
        it up there, so tests patch it there. That module must define it and must
        not late-bind it too: bind_routes would replace the function with a
        lookup that finds itself. A copy server.py imports back is the same."""
        import server
        self.assertTrue(server.MOVED)
        for name, module in server.MOVED.items():
            with self.subTest(name=name, module=module.__name__):
                found = vars(module).get(name)
                self.assertEqual(module.__name__, getattr(found, "__module__", None),
                                 f"{module.__name__} defines {name}")
                late = set(getattr(module, "ROUTE_NEEDS", ())) | set(getattr(module, "NEEDS", ()))
                self.assertNotIn(name, late)
                if name in vars(server):
                    self.assertIs(found, vars(server)[name])


class ServerStaysRouting(unittest.TestCase):
    """CONTRIBUTING.md: server.py is the composition root - imports, constants,
    each module's bind(), start-up and a thin handler - and each feature lives
    in its own homestead_*.py module. These guards keep it that way."""

    def test_server_py_is_at_its_cap(self):
        """The ratchet: server.py is at most MAX_LINES lines, and at least
        MAX_LINES - RATCHET_SLACK. Growing fails: put the code in a module.
        Shrinking further fails until MAX_LINES is lowered to the new length,
        in the same commit, so the lines a move frees can't be spent again."""
        lines = len(SERVER.splitlines())
        self.assertLessEqual(lines, MAX_LINES,
                             "server.py grew: put the new code in a homestead_*.py module")
        self.assertGreaterEqual(lines, MAX_LINES - RATCHET_SLACK,
                                f"server.py shrank to {lines} lines: lower MAX_LINES to {lines}")

    def test_no_new_path_branches(self):
        branches = len(re.findall(r'\bp == "/api/', SERVER))
        self.assertLessEqual(branches, MAX_PATH_BRANCHES,
                             "a new route belongs in its module's ROUTES (homestead_routes.py), not an if in server.py")

    def test_every_inline_route_is_listed_with_its_reason(self):
        """The allow-list: every "/api/..." path written in class H is in
        INLINE_ROUTES with the reason it needs the handler (cookies, request
        headers, fleet forwarding, a WebSocket): under its method when it is in
        do_GET, do_POST or do_DELETE, under "ANY" when only a helper method
        (such as _fleet_target) writes it. A new route goes in its module's ROUTES
        instead. A route that moves out of server.py comes off INLINE_ROUTES in
        the same commit. Only literals are seen: a path compared with a constant
        (p == MCP.PATH) is not."""
        listed = {(method, path) for method, path, _ in INLINE_ROUTES}
        found = {(method, path) for method in ("GET", "POST", "DELETE") for path in api_literals(method)}
        found |= {("ANY", path) for path in helper_literals() if path not in {path for _, path in found}}
        self.assertEqual([], [f"{method} {path}" for method, path in sorted(found - listed)],
                         "answered inline in server.py: declare it in its module's ROUTES (homestead_routes.py), "
                         "or add it to INLINE_ROUTES with the reason it needs the handler")
        self.assertEqual([], [f"{method} {path}" for method, path in sorted(listed - found)],
                         "no longer answered inline in server.py: take it off INLINE_ROUTES")
        self.assertEqual(len(INLINE_ROUTES), len(listed), "a route is listed twice in INLINE_ROUTES")
        self.assertEqual([], [f"{method} {path}" for method, path, reason in INLINE_ROUTES if not reason.strip()],
                         "say why the handler answers it")

    def test_the_alias_block_only_shrinks(self):
        """The alias cap: names tests still patch on server
        (mock.patch.object(server, "X")) after their code moved are kept only
        between ALIAS_START and ALIAS_END, one `X = MODULE.X` a line, exactly
        ALIASES of them: more fails (point the test at the module instead),
        fewer fails until ALIASES is lowered, like the ratchet. Track 2 points
        those tests at the module and lowers ALIASES."""
        lines = SERVER.splitlines()
        self.assertEqual((1, 1), (lines.count(ALIAS_START), lines.count(ALIAS_END)),
                         "server.py keeps one alias block, opened and closed by ALIAS_START and ALIAS_END")
        start, end = lines.index(ALIAS_START), lines.index(ALIAS_END)
        self.assertLess(start, end, "ALIAS_START has to come before ALIAS_END in server.py")
        block = [line for line in lines[start + 1:end] if line.strip()]
        self.assertEqual([], [line for line in block if not re.fullmatch(r"[A-Za-z_]\w* = [A-Z][A-Z0-9_]*\.[A-Za-z_]\w*", line)],
                         "the alias block holds only `X = MODULE.X` lines")
        self.assertLessEqual(len(block), ALIASES,
                             f"server.py's alias block has {len(block)} lines (cap {ALIASES}): "
                             "point the test at the module the code lives in, not at server")
        self.assertGreaterEqual(len(block), ALIASES, f"the alias block shrank to {len(block)}: lower ALIASES to {len(block)}")

    def test_tests_patch_only_what_server_py_defines(self):
        """A test that patches a name on server (mock.patch.object(server, "X")
        or server.X = ...) must patch something server.py defines. A name
        server.py only imports back from a module (`from homestead_x import X`)
        is a copy: patching it changes nothing the module's code calls, and
        the test passes for the wrong reason. Patch the module instead."""
        imported = {}
        for module, names in re.findall(r"^from (homestead_\w+) import \(([^)]*)\)", SERVER, re.M):
            for name in re.findall(r"\w+", names):
                imported[name] = module
        offenders = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            text = SS.text(path)
            patched = set(re.findall(r'patch\.object\(server, "(\w+)"', text)) | set(re.findall(r"^\s*server\.(\w+) = ", text, re.M))
            offenders += [f"{path.name} patches server.{name}, which server.py imports from {imported[name]}: "
                          f"patch {imported[name]}.{name}" for name in sorted(patched & set(imported))]
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
