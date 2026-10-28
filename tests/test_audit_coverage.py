"""Every request that changes something writes one audit line (#738).

Where the change routes come from, so a route moving between server.py and a
module (the rest of #764) changes nothing here:
- every non-GET route a module declares (homestead_routes.table());
- every non-GET row of homestead_route_policy.POLICY (server.py's own routes);
- every non-GET row of test_module_routes.INLINE_ROUTES (server.py's branches);
- every "/api/..." literal written in server.py's do_POST;
- /mcp, /api/v1/*, DELETE /api/workload/<ns>/<name>, PUT and PATCH;
- and the three console GETs.
"""
import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))
import homestead_audit as AUDIT_LOG
import homestead_route_policy as POLICY
import homestead_routes as ROUTES
import test_module_routes as MODULE_TESTS

CONSOLES = {("GET", "/api/console"), ("GET", "/api/node/shell"), ("GET", "/api/vm/console")}
# A floor, so an enumeration that silently finds nothing fails. 308 module
# POSTs + 32 POLICY rows at a35e8a48; most overlap is none (POLICY is server.py's).
AT_LEAST = 330


def concrete(path):
    """A path a request could carry: a prefix route ("/x/*") or prefix row ("/x/") gets a tail."""
    if path.endswith("/*"):
        return path[:-1] + "x"
    return path + "x" if path.endswith("/") else path


def change_routes():
    found = {(m, concrete(p)) for (m, p) in ROUTES.table() if m != "GET"}
    found |= {(m, p) for (m, p) in POLICY.POLICY if m != "GET"}
    for method, path, reason in MODULE_TESTS.INLINE_ROUTES:
        if reason.startswith("not a route"):
            continue
        if method in ("ANY", "POST", "DELETE"):
            found.add(("POST" if method == "ANY" else method, concrete(path)))
    found |= {("POST", concrete(p)) for p in MODULE_TESTS.api_literals("POST")}
    found |= {("DELETE", concrete(p)) for p in MODULE_TESTS.api_literals("DELETE")}
    found |= {("POST", "/mcp"), ("POST", "/api/v1/x"), ("DELETE", "/api/workload/a/b"),
              ("PUT", "/api/x"), ("PATCH", "/api/x")}
    return found


class EveryChangeIsAudited(unittest.TestCase):
    def test_every_change_route_is_audited_or_exempt_by_name(self):
        routes = change_routes()
        self.assertGreaterEqual(len(routes), AT_LEAST, "the enumeration found too few routes")
        missed = sorted(r for r in routes | CONSOLES
                        if not AUDIT_LOG.audited(*r) and r not in AUDIT_LOG.NOT_AUDITED)
        self.assertEqual([], missed)

    def test_reads_are_not_audited(self):
        """Option A of #738: changes only. The UI polls; reads would bury the changes."""
        reads = {(m, concrete(p)) for (m, p) in ROUTES.table() if m == "GET"} - CONSOLES
        self.assertEqual([], sorted(r for r in reads if AUDIT_LOG.audited(*r)))
        self.assertFalse(AUDIT_LOG.audited("GET", "/api/overview"))
        self.assertFalse(AUDIT_LOG.audited("POST", "/index.html"))

    def test_every_exemption_is_a_change_route_with_a_reason(self):
        routes = change_routes()
        for route, reason in AUDIT_LOG.NOT_AUDITED.items():
            with self.subTest(route=route):
                self.assertIn(route, routes, "a stale exemption: no such change route")
                self.assertTrue(str(reason).strip(), "an exemption needs its reason")

    def test_the_exemptions_are_exactly_these(self):
        """A new exemption is a visible edit here, for the reviewer."""
        self.assertEqual({("POST", "/api/auth/login"), ("POST", "/api/auth/preferences/dashboard"),
                          ("POST", "/api/timeline/seen"), ("POST", "/api/timeline/summary")},
                         set(AUDIT_LOG.NOT_AUDITED))

    def test_the_hook_is_registered_through_modules_for_named_fields_only(self):
        self.assertIn("homestead_audit", ROUTES.MODULES)
        ROUTES.table()
        mine = [tuple(fields) for fn, fields in ROUTES._hooks if fn is AUDIT_LOG.record]
        self.assertEqual([tuple(ROUTES.HOOK_FIELDS)], mine)

    def test_server_py_does_not_call_the_audit(self):
        """No server.py line: the hook is wired by PR 1's after_request alone."""
        self.assertNotIn("homestead_audit", MODULE_TESTS.SERVER)
        self.assertNotIn("AUDIT_LOG", MODULE_TESTS.SERVER)


class EndToEnd(unittest.TestCase):
    """Through a real server.H: one line per change request, whichever branch answers it."""

    @classmethod
    def setUpClass(cls):
        import homestead_http as HTTP
        import server
        cls.server = server
        ROUTES.table()              # reads the modules, homestead_audit among them
        cls.listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=cls.listener.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()

    def setUp(self):
        self.lines, self.arrived = [], threading.Condition()

        def out(text):
            with self.arrived:
                self.lines.append(json.loads(text))
                self.arrived.notify_all()
        self.enterContext(mock.patch.object(AUDIT_LOG, "_stdout", side_effect=out))
        AUDIT_LOG._buckets.clear()
        self.addCleanup(AUDIT_LOG._buckets.clear)
        self.assertTrue(any(fn is AUDIT_LOG.record for fn, _ in ROUTES._hooks), "the audit hook is registered")
        self.who = self.enterContext(mock.patch.object(
            self.server.H, "_who", return_value={"user": "ada", "role": "admin", "stale": False,
                                                 "remember": False, "started": None}))
        self.enterContext(mock.patch.object(self.server.H, "_signin", lambda *a, **k: None))
        self.enterContext(mock.patch.object(self.server.FLEET, "self_id", return_value="home"))

    def send(self, method, path, body=None):
        client = HTTPConnection(*self.listener.server_address, timeout=5)
        try:
            client.request(method, path, json.dumps(body) if body is not None else None,
                           {"Content-Type": "application/json", "X-Homestead-Auth": "1"})
            response = client.getresponse()
            response.read()
            return response.status
        finally:
            client.close()

    def wait_for(self, count):
        """The hook writes after the answer is sent, so wait for it (at most 3 s)."""
        with self.arrived:
            self.arrived.wait_for(lambda: len(self.lines) >= count, timeout=3)
        return list(self.lines)

    def test_a_module_route_writes_one_line_with_its_target(self):
        route = ROUTES.Route("POST", "/api/audit-test/save", "operator", lambda request: {"ok": True}, "test")
        self.enterContext(mock.patch.dict(ROUTES._table, {("POST", "/api/audit-test/save"): route}))
        self.assertEqual(200, self.send("POST", "/api/audit-test/save",
                                        {"ns": "lab", "name": "app-1", "password": "Pw-0123456789"}))
        (line,) = self.wait_for(1)
        self.assertEqual(("POST", "/api/audit-test/save", 200, "ada", "browser"),
                         (line["method"], line["path"], line["status"], line["user"], line["via"]))
        self.assertEqual({"ns": "lab", "name": "app-1"}, line["target"])
        self.assertNotIn("Pw-0123456789", json.dumps(line))

    def test_a_server_py_branch_writes_one_line(self):
        self.enterContext(mock.patch.object(self.server.AUTH, "set_role"))
        self.enterContext(mock.patch.object(self.server.AUTH, "list_users", return_value=[]))
        self.assertEqual(200, self.send("POST", "/api/auth/role", {"username": "node1", "role": "viewer"}))
        (line,) = self.wait_for(1)
        self.assertEqual(("POST", "/api/auth/role", 200, "admin"),
                         (line["method"], line["path"], line["status"], line["role"]))

    def test_delete_takes_its_target_from_the_path(self):
        # The same stand-in wherever the route finds it: server.py today, its
        # module once #764 moves DELETE there.
        import homestead_deploy_server as DEPLOY_SERVER
        for module in (self.server, DEPLOY_SERVER):
            self.enterContext(mock.patch.object(module, "delete_workload", return_value=[]))
        self.assertEqual(200, self.send("DELETE", "/api/workload/lab/app-1"))
        (line,) = self.wait_for(1)
        self.assertEqual({"ns": "lab", "name": "app-1"}, line["target"])

    def test_a_route_that_raises_still_writes_one_line(self):
        def boom(request):
            raise RuntimeError("no")
        route = ROUTES.Route("POST", "/api/audit-test/boom", "operator", boom, "test")
        self.enterContext(mock.patch.dict(ROUTES._table, {("POST", "/api/audit-test/boom"): route}))
        self.assertEqual(500, self.send("POST", "/api/audit-test/boom", {}))
        (line,) = self.wait_for(1)
        self.assertEqual(500, line["status"])

    def test_a_refused_stranger_is_on_stdout_every_time(self):
        self.who.return_value = None
        self.assertEqual(401, self.send("POST", "/api/audit-test/save", {}))
        self.assertEqual(401, self.send("POST", "/api/audit-test/save", {}))
        lines = self.wait_for(2)          # stdout carries every line; the file keeps a few (test_audit.Throttle)
        self.assertEqual(2, len(lines))
        self.assertEqual(("none", "", 401), (lines[0]["via"], lines[0]["user"], lines[0]["status"]))

    def test_a_relayed_request_names_its_cluster(self):
        """to is noted on the request's thread; the line is written on the hooks' thread."""
        F = self.server.FLEET
        self.enterContext(mock.patch.object(F, "member", return_value={"id": "member-2", "name": "loft",
                                                                       "url": "https://192.0.2.20"}))
        self.enterContext(mock.patch.object(F, "_connect", side_effect=OSError("no route")))
        client = HTTPConnection(*self.listener.server_address, timeout=5)
        try:
            client.request("POST", "/api/audit-test/save", "{}", {"Content-Type": "application/json",
                           "X-Homestead-Auth": "1", "X-Homestead-Cluster": "member-2"})
            self.assertEqual(502, client.getresponse().status)
        finally:
            client.close()
        (line,) = self.wait_for(1)
        self.assertEqual(("member-2", 502), (line.get("to"), line["status"]))

    def test_what_one_request_noted_never_reaches_the_next(self):
        route = ROUTES.Route("POST", "/api/audit-test/note", "operator",
                             lambda request: AUDIT_LOG.note(tool="container_restart", kind="change") or {}, "test")
        plain = ROUTES.Route("POST", "/api/audit-test/save", "operator", lambda request: {"ok": True}, "test")
        self.enterContext(mock.patch.dict(ROUTES._table, {("POST", "/api/audit-test/note"): route,
                                                          ("POST", "/api/audit-test/save"): plain}))
        self.send("POST", "/api/audit-test/note", {})
        self.send("POST", "/api/audit-test/save", {})
        first, second = self.wait_for(2)
        self.assertEqual("container_restart", first.get("tool"))
        self.assertNotIn("tool", second)

    def test_no_line_is_lost_in_normal_use(self):
        route = ROUTES.Route("POST", "/api/audit-test/save", "operator", lambda request: {"ok": True}, "test")
        self.enterContext(mock.patch.dict(ROUTES._table, {("POST", "/api/audit-test/save"): route}))
        before = ROUTES.hooks_dropped()
        for _ in range(300):
            self.assertEqual(200, self.send("POST", "/api/audit-test/save", {}))
        self.assertEqual(300, len(self.wait_for(300)))
        self.assertEqual(before, ROUTES.hooks_dropped())

    def test_a_read_writes_nothing(self):
        route = ROUTES.Route("GET", "/api/audit-test/read", "viewer", lambda request: {}, "test")
        self.enterContext(mock.patch.dict(ROUTES._table, {("GET", "/api/audit-test/read"): route}))
        self.assertEqual(200, self.send("GET", "/api/audit-test/read"))
        self.assertEqual([], self.wait_for(1))


if __name__ == "__main__":
    unittest.main()
