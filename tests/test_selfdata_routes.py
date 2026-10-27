"""The diagnostics download and the self-data move and handoff, pinned
through the real HTTP handler (#764): status, body, headers, cookies and
who may ask stay the same when they are answered by their modules."""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_http as HTTP
import homestead_self_data_server as SELF_DATA_ROUTES
import server
import source_scan as SS

CSRF = {"X-Homestead-Auth": "1", "Content-Type": "application/json"}
OP = "0123456789abcdef01234567"
HANDOFF = "/api/self/data/handoff/" + OP
TOKEN = "t" * 64
STARTED = ({"ok": True, "operation": "job-1", "handoff": OP}, TOKEN)
MOVE_COOKIE = f"homestead-data-move={TOKEN}; Path=/api/self/data/handoff/; HttpOnly; SameSite=Strict; Max-Age=86400"
# The same attributes as before #764's PR 7, in the order the shared cookie
# writer (homestead_routes.cookie_header) puts them: Secure after SameSite.
MOVE_COOKIE_TLS = f"homestead-data-move={TOKEN}; Path=/api/self/data/handoff/; HttpOnly; SameSite=Strict; Secure; Max-Age=86400"


class Served(unittest.TestCase):
    """A real server.H on 127.0.0.1:0, signed in as `ada` with self.role."""

    role = "admin"
    stale = False

    def setUp(self):
        listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        self.address = listener.server_address
        self.enterContext(mock.patch.object(server.CFACCESS, "enabled", return_value=False))
        self.enterContext(mock.patch.object(server.H, "log_message"))
        self.enterContext(mock.patch.object(server.H, "log_error"))
        self.enterContext(mock.patch.object(server.H, "_who", side_effect=lambda: self.role and {
            "user": "ada", "role": self.role, "stale": self.stale, "remember": False, "started": 1}))

    def everywhere(self, name, stand_in):
        """The same stand-in wherever a route's handler looks `name` up."""
        for module in (server, SELF_DATA_ROUTES):
            if hasattr(module, name):
                self.enterContext(mock.patch.object(module, name, stand_in))
        return stand_in

    def request(self, method, path, headers=None, body=None):
        client = http.client.HTTPConnection(*self.address, timeout=5)
        try:
            client.request(method, path, body, headers or {})
            response = client.getresponse()
            return response.status, response.getheaders(), response.read()
        except (http.client.RemoteDisconnected, ConnectionResetError):
            return None, [], b""
        finally:
            client.close()


def cookies(headers):
    return [value for name, value in headers if name.lower() == "set-cookie"]


class DiagnosticsDownload(Served):
    def setUp(self):
        super().setUp()
        self.export = self.enterContext(mock.patch.object(
            server.DIAGNOSTICS, "export",
            return_value=("homestead-diagnostics-0123abcd-full.zip", "application/zip", b"PK\x03\x04zip")))

    def test_the_download_is_an_attachment_with_its_type(self):
        status, headers, body = self.request("GET", "/api/diagnostics/download?id=0123abcd&format=full&package=1")
        self.assertEqual((200, b"PK\x03\x04zip"), (status, body))
        self.export.assert_called_once_with("0123abcd", "ada", "full", True)
        names = [name for name, _ in headers if name not in ("Server", "Date")]
        self.assertEqual(["Content-Type", "Content-Length", "Cache-Control", "Content-Security-Policy",
                          "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy",
                          "X-Homestead-Version", "Content-Disposition"], names)
        values = dict(headers)
        self.assertEqual("application/zip", values["Content-Type"])
        self.assertEqual(str(len(body)), values["Content-Length"])
        self.assertEqual("no-store", values["Cache-Control"])
        self.assertEqual("nosniff", values["X-Content-Type-Options"])
        self.assertEqual('attachment; filename="homestead-diagnostics-0123abcd-full.zip"', values["Content-Disposition"])
        self.assertEqual([], cookies(headers))

    def test_the_defaults_are_an_anonymised_log(self):
        self.export.return_value = ("homestead-diagnostics-0123abcd-anonymised.log", "text/plain; charset=utf-8", b"log")
        status, headers, body = self.request("GET", "/api/diagnostics/download?id=0123abcd")
        self.assertEqual((200, b"log"), (status, body))
        self.export.assert_called_once_with("0123abcd", "ada", "anonymised", False)
        values = dict(headers)
        self.assertEqual("text/plain; charset=utf-8", values["Content-Type"])
        self.assertEqual('attachment; filename="homestead-diagnostics-0123abcd-anonymised.log"', values["Content-Disposition"])

    def test_only_an_admin_may_download(self):
        for role in ("viewer", "operator"):
            self.role = role
            with self.subTest(role=role):
                status, headers, body = self.request("GET", "/api/diagnostics/download?id=0123abcd")
                self.assertEqual(403, status)
                self.assertEqual("admin", json.loads(body)["needed"])
                self.assertNotIn("Content-Disposition", dict(headers))
        self.export.assert_not_called()

    def test_errors_carry_no_attachment_header(self):
        for error, code in ((ValueError("unknown format"), 400), (PermissionError("not your report"), 403)):
            self.export.side_effect = error
            with self.subTest(code=code):
                status, headers, body = self.request("GET", "/api/diagnostics/download?id=0123abcd&format=x")
                self.assertEqual((code, {"error": str(error)}), (status, json.loads(body)))
                self.assertNotIn("Content-Disposition", dict(headers))


class SelfDataMove(Served):
    def setUp(self):
        super().setUp()
        self.start = self.everywhere("start_self_data_move", mock.Mock(return_value=STARTED))

    def move(self, headers=CSRF, body=b'{"storage_class": "fast"}'):
        return self.request("POST", "/api/self/data/move", headers, body)

    def test_move_sets_the_handoff_cookie(self):
        status, headers, body = self.move()
        self.assertEqual((202, STARTED[0]), (status, json.loads(body)))
        self.start.assert_called_once_with({"storage_class": "fast"}, "ada")
        self.assertEqual([MOVE_COOKIE], cookies(headers))

    def test_move_sets_a_secure_handoff_cookie_under_tls(self):
        status, headers, _ = self.move({**CSRF, "X-Forwarded-Proto": "https"})
        self.assertEqual(202, status)
        self.assertEqual([MOVE_COOKIE_TLS], cookies(headers))

    def test_a_refreshed_session_cookie_comes_before_the_move_cookie(self):
        self.stale = True
        with mock.patch.object(server.AUTH, "issue_token", return_value="session-token") as issue:
            status, headers, _ = self.move()
        self.assertEqual(202, status)
        issue.assert_called_once_with("ada", False, 1)
        age = server.AUTH.idle_ttl(False)
        self.assertEqual([f"{server.AUTH.COOKIE}=session-token; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}",
                          MOVE_COOKIE], cookies(headers))

    def test_only_an_admin_may_move(self):
        for role in ("viewer", "operator"):
            self.role = role
            with self.subTest(role=role):
                status, headers, body = self.move()
                self.assertEqual(403, status)
                self.assertEqual("admin", json.loads(body)["needed"])
                self.assertEqual([], cookies(headers))
        self.start.assert_not_called()

    def test_without_the_csrf_header_it_is_refused(self):
        status, headers, body = self.move({"Content-Type": "application/json"})
        self.assertEqual((403, {"error": "missing X-Homestead-Auth header"}), (status, json.loads(body)))
        self.start.assert_not_called()

    def test_a_held_move_is_409_without_a_cookie(self):
        self.start.side_effect = server.SELF_DATA_FENCE.Held("Another data move is running")
        status, headers, body = self.move()
        self.assertEqual((409, {"error": "Another data move is running", "review_required": True}),
                         (status, json.loads(body)))
        self.assertEqual([], cookies(headers))

    def test_a_capacity_refusal_is_409_with_its_plan(self):
        self.start.side_effect = server.CAPACITY_REVIEW.Rejected("needs a fresh review", {"stages": []})
        status, headers, body = self.move()
        self.assertEqual((409, {"error": "needs a fresh review", "capacity": {"stages": []}, "review_required": True}),
                         (status, json.loads(body)))
        self.assertEqual([], cookies(headers))

    def test_other_refusals_keep_their_status(self):
        for error, code in ((ValueError("pick a storage class"), 400), (PermissionError("not allowed"), 403),
                            (RuntimeError("boom"), 500)):
            self.start.side_effect = error
            with self.subTest(code=code):
                status, headers, body = self.move()
                self.assertEqual((code, {"error": str(error)}), (status, json.loads(body)))
                self.assertEqual([], cookies(headers))


class SelfDataHandoff(Served):
    role = "viewer"

    def setUp(self):
        super().setUp()
        self.status = self.everywhere("self_data_handoff_status",
                                      mock.Mock(return_value={"status": "running", "phase": "copy"}))

    def test_handoff_status_for_a_viewer(self):
        status, headers, body = self.request("GET", HANDOFF)
        self.assertEqual((200, {"status": "running", "phase": "copy"}), (status, json.loads(body)))
        self.status.assert_called_once_with(OP)
        self.assertEqual("application/json", dict(headers)["Content-Type"])
        self.assertEqual([], cookies(headers))

    def test_an_unknown_state_is_503(self):
        self.status.return_value = {"status": "unknown", "message": "cannot read"}
        status, _, body = self.request("GET", HANDOFF)
        self.assertEqual((503, {"status": "unknown", "message": "cannot read"}), (status, json.loads(body)))

    def test_an_unknown_move_is_404(self):
        self.status.return_value = None
        status, _, body = self.request("GET", HANDOFF)
        self.assertEqual((404, {"error": "No recorded data move with this identity"}), (status, json.loads(body)))

    def test_the_view_is_the_maintenance_page(self):
        status, headers, body = self.request("GET", HANDOFF + "/view")
        self.assertEqual(200, status)
        self.assertEqual(server.SELF_DATA_WORKER.maintenance_page(OP).encode(), body)
        self.assertEqual("text/html; charset=utf-8", dict(headers)["Content-Type"])
        self.assertEqual("no-store", dict(headers)["Cache-Control"])
        self.status.assert_not_called()

    def test_a_malformed_handoff_path_is_403(self):
        for path in ("/api/self/data/handoff/zz", "/api/self/data/handoff/" + OP.upper(),
                     "/api/self/data/handoff/" + OP[:-1], HANDOFF + "/view/more", HANDOFF + "/", "/api/self/data/handoff/"):
            for role in ("viewer", "admin"):
                self.role = role
                with self.subTest(path=path, role=role):
                    status, _, body = self.request("GET", path)
                    self.assertEqual((403, {"error": "this route has no declared authorization policy"}),
                                     (status, json.loads(body)))
        self.status.assert_not_called()

    def test_only_get_reads_the_handoff(self):
        self.role = "admin"
        for method in ("POST", "DELETE"):
            with self.subTest(method=method):
                self.assertNotIn(self.request(method, HANDOFF, CSRF, b"{}")[0], (200, 202, 503))
        self.status.assert_not_called()

    def test_signed_out_it_is_401(self):
        self.role = None
        status, _, body = self.request("GET", HANDOFF)
        self.assertEqual((401, {"error": "not signed in", "auth": False}), (status, json.loads(body)))
        self.status.assert_not_called()


class DeclaredInTheirModules(unittest.TestCase):
    def test_the_download_move_and_handoff_are_module_routes(self):
        import homestead_routes as ROUTES
        for method, path, role, module in (
                ("GET", "/api/diagnostics/download", "admin", "homestead_diagnostics"),
                ("POST", "/api/self/data/move", "admin", "homestead_self_data_server"),
                ("GET", HANDOFF, "viewer", "homestead_self_data_server"),
                ("GET", HANDOFF + "/view", "viewer", "homestead_self_data_server")):
            with self.subTest(path=path):
                route = ROUTES.find(method, path)
                self.assertIsNotNone(route, f"{method} {path} is not a module route")
                self.assertEqual((role, module), (route.role, route.module))
        import homestead_route_policy as POLICY
        self.assertNotIn("/api/self/data/handoff/", POLICY.CODE_PREFIXES)
        self.assertEqual("viewer", POLICY.role(HANDOFF, "GET"))
        self.assertEqual("/api/self/data/handoff/", ROUTES.COOKIES["homestead-data-move"])
        source = SS.text(Path(server.__file__))
        for gone in ('"/api/diagnostics/download"', "homestead-data-move=", 'self_data_handoff_status(p['):
            self.assertNotIn(gone, source)


if __name__ == "__main__":
    unittest.main()
