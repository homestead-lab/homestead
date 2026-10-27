"""DELETE requests, pinned through the real HTTP handler (#764).

These answers must stay the same when do_DELETE dispatches module routes:
the status, the body, the headers and who may ask."""
import http.client
import json
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_deploy_server as DEPLOY_SERVER
import homestead_http as HTTP
import server
import source_scan as SS

CSRF = {"X-Homestead-Auth": "1"}
WORKLOAD = "/api/workload/lab/web"


class Served(unittest.TestCase):
    """A real server.H on 127.0.0.1:0, signed in as `ada` with self.role."""

    role = "operator"

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
        self.enterContext(mock.patch.object(
            server.H, "_who", side_effect=lambda: self.role and {"user": "ada", "role": self.role, "stale": False}))
        # The same stand-in wherever the route's handler looks it up.
        self.delete = mock.Mock(return_value=[{"name": "web", "ns": "lab"}])
        for module in (server, DEPLOY_SERVER):
            if hasattr(module, "delete_workload"):
                self.enterContext(mock.patch.object(module, "delete_workload", self.delete))

    def request(self, path, headers=CSRF, method="DELETE"):
        """(status, headers, body); status None when the server closed the
        connection without answering."""
        client = http.client.HTTPConnection(*self.address, timeout=5)
        try:
            client.request(method, path, headers=headers)
            response = client.getresponse()
            return response.status, response.getheaders(), response.read()
        except (http.client.RemoteDisconnected, ConnectionResetError):
            return None, [], b""
        finally:
            client.close()


class DeleteAWorkload(Served):
    def test_deleting_a_workload_answers_its_services(self):
        status, headers, body = self.request(WORKLOAD)
        self.assertEqual(200, status)
        self.assertEqual({"ok": True, "services": [{"name": "web", "ns": "lab"}]}, json.loads(body))
        self.delete.assert_called_once_with("lab", "web")
        names = dict(headers)
        self.assertEqual("application/json", names["Content-Type"])
        self.assertEqual(str(len(body)), names["Content-Length"])
        self.assertEqual("no-store", names["Cache-Control"])
        self.assertEqual("nosniff", names["X-Content-Type-Options"])
        self.assertEqual(server.HOMESTEAD_VERSION, names["X-Homestead-Version"])
        self.assertNotIn("Set-Cookie", names)
        self.assertNotIn("Connection", names)

    def test_a_query_string_does_not_change_the_target(self):
        self.assertEqual(200, self.request(WORKLOAD + "?force=1")[0])
        self.delete.assert_called_once_with("lab", "web")

    def test_an_admin_may_delete_too(self):
        self.role = "admin"
        self.assertEqual(200, self.request(WORKLOAD)[0])

    def test_a_viewer_is_refused_with_the_role_it_needs(self):
        self.role = "viewer"
        status, headers, body = self.request(WORKLOAD)
        self.assertEqual(403, status)
        self.assertEqual({"error": "your role (viewer) cannot do this — operator required",
                          "role": "viewer", "needed": "operator"}, json.loads(body))
        self.assertEqual("close", dict(headers)["Connection"])
        self.delete.assert_not_called()

    def test_without_the_csrf_header_it_is_refused(self):
        status, _, body = self.request(WORKLOAD, headers={})
        self.assertEqual((403, {"error": "missing X-Homestead-Auth header"}), (status, json.loads(body)))
        self.delete.assert_not_called()

    def test_signed_out_it_is_401(self):
        self.role = None
        status, _, body = self.request(WORKLOAD)
        self.assertEqual((401, {"error": "not signed in", "auth": False}), (status, json.loads(body)))
        self.delete.assert_not_called()

    def test_a_malformed_delete_path_is_refused_and_deletes_nothing(self):
        # Before #764's PR 6 the guard raised outside do_DELETE's try and the
        # connection closed unanswered; whatever answers it, nothing is deleted.
        for role in ("operator", "viewer"):
            self.role = role
            for path in ("/api/workload/lab", "/api/workload/Lab/web", "/api/workload/lab/web/extra",
                         "/api/workload/lab_1/web", "/api/workload/lab/web/"):
                with self.subTest(role=role, path=path):
                    status = self.request(path)[0]
                    self.assertNotIn(status, (200, 202, 204, 404))
        self.delete.assert_not_called()


class MalformedDeletes(Served):
    def test_a_malformed_delete_path_is_403(self):
        for path in ("/api/workload/lab", "/api/workload/Lab/web", "/api/workload/lab/web/extra",
                     "/api/workload/lab_1/web"):
            with self.subTest(path=path):
                self.role = "operator"
                status, _, body = self.request(path)
                self.assertEqual((403, {"error": "this route has no declared authorization policy"}),
                                 (status, json.loads(body)))
                self.role = "viewer"
                status, _, body = self.request(path)
                self.assertEqual((403, {"error": "your role (viewer) cannot do this — operator required",
                                        "role": "viewer", "needed": "operator"}), (status, json.loads(body)))
        self.delete.assert_not_called()


class UndeclaredDeletes(Served):
    def test_a_delete_with_no_declared_route_is_403(self):
        # As GET and POST answer it; before #764's PR 6 the connection closed.
        for path in ("/api/workloads", "/api/settings", "/api/self/data/move", "/api/workload/lab/web/"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual((403, {"error": "this route has no declared authorization policy"}),
                                 (status, json.loads(body)))
        self.delete.assert_not_called()


class ApiV1Deletes(Served):
    def test_a_delete_under_api_v1_is_still_404_and_runs_nothing(self):
        # homestead_api_v1 declares DELETE /api/v1/* only for its role. Now that
        # do_DELETE dispatches module routes, it still answers as server.py did:
        # no endpoint runs and nothing is written to the change history.
        import homestead_api_v1 as API_V1
        import homestead_mcp as MCP
        handle = self.enterContext(mock.patch.object(API_V1, "handle", return_value=(200, {})))
        audit = self.enterContext(mock.patch.object(MCP, "audit_api"))
        status, _, body = self.request("/api/v1/nodes")
        self.assertEqual((404, {"error": "no route"}), (status, json.loads(body)))
        handle.assert_not_called()
        audit.assert_not_called()


class DeclaredInItsModule(unittest.TestCase):
    def test_delete_routes_are_declared_in_their_module(self):
        import homestead_routes as ROUTES
        route = ROUTES.find("DELETE", "/api/workload/lab/web")
        self.assertIsNotNone(route, "DELETE /api/workload/<ns>/<name> is not a module route")
        self.assertEqual(("operator", "homestead_deploy_server"), (route.role, route.module))
        source = SS.text(Path(server.__file__))
        self.assertNotIn("delete_workload(parts", source)

    def test_the_workload_prefix_is_no_longer_matched_in_code(self):
        import homestead_route_policy as POLICY
        import homestead_routes as ROUTES
        self.assertNotIn("/api/workload/", POLICY.CODE_PREFIXES)
        self.assertEqual("homestead_deploy_server", ROUTES.find("DELETE", "/api/workload/lab/web").module)
        self.assertEqual("operator", POLICY.role("/api/workload/lab/web", "DELETE"))


class DeleteErrors(Served):
    def answer(self, error):
        self.delete.side_effect = error
        status, headers, body = self.request(WORKLOAD)
        return status, json.loads(body)

    def test_a_value_error_is_400(self):
        self.assertEqual((400, {"error": "The app is protected"}), self.answer(ValueError("The app is protected")))

    def test_a_cluster_refusal_keeps_its_status(self):
        error = urllib.error.HTTPError("https://192.0.2.1", 409, "Conflict", {}, None)
        with mock.patch.object(server.API_ERRORS, "message", return_value="the cluster said no"):
            self.assertEqual((409, {"error": "the cluster said no"}), self.answer(error))

    def test_an_unexpected_error_is_500(self):
        self.assertEqual((500, {"error": "boom"}), self.answer(RuntimeError("boom")))

    def test_an_unreadable_account_store_is_503(self):
        error = server.AUTH.StoreUnavailable("the account store did not answer")
        error.cause = "timeout"
        self.assertEqual((503, {"error": "the account store did not answer", "cause": "timeout", "unavailable": True}),
                         self.answer(error))


class OtherDeletes(Served):
    def test_delete_on_a_path_with_no_route_answers_nothing_useful(self):
        # No other DELETE route exists: none answers 200.
        for path in ("/api/workloads", "/api/settings", "/api/self/data/move", "/api/workload/lab/web/"):
            with self.subTest(path=path):
                self.assertNotEqual(200, self.request(path)[0])
        self.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
