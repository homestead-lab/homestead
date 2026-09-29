"""Destination readiness is the real loaded app, with writers still stopped."""
import json
import copy
import http.client
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import server
from homestead_storage_journal import Held


class BootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.fence = mock.Mock()
        self.fence.inspect.return_value = {"mode": "start", "writable": False}
        self.fence.require_write.side_effect = Held("held")
        for patch in (mock.patch.object(server, "_self_data_fence", self.fence),
                      mock.patch.object(server, "_self_data_boot_pending", True),
                      mock.patch.object(server, "_self_data_boot_failed", False),
                      mock.patch.object(server.OPS, "DATA_DIR", self.temp.name),
                      mock.patch.object(server.AUTH, "review_signing_key", return_value=b"read-only-fixture")):
            patch.start(); self.addCleanup(patch.stop)

    def handler(self, path, method="GET"):
        h = object.__new__(server.H)
        h.command, h.path, h.headers = method, path, {}
        h._send = mock.Mock(); h._who = mock.Mock(return_value=None)
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False):
            result = h._guard(path)
        return h, result

    def test_ready_route_checks_journal_and_key_without_any_persistent_write(self):
        with mock.patch.object(server.OPS, "list_operations", side_effect=AssertionError("resolver polling")), \
             mock.patch.object(server.OPS, "_lock", side_effect=AssertionError("creating locks")), \
             mock.patch.object(server, "ksend", side_effect=AssertionError("write")):
            h, result = self.handler("/healthz")
        self.assertTrue(result)
        self.assertEqual((200, {"ok": True, "data_handoff": True, "read_only": True}), h._send.call_args.args)
        self.assertEqual([], list(Path(self.temp.name).iterdir()))
        server.AUTH.review_signing_key.assert_called_once()

    def test_unsafe_get_routes_and_all_mutations_are_held_before_auth_or_dispatch(self):
        for path, method in (("/api/operations", "GET"), ("/api/push/key", "GET"), ("/api/files/list", "GET"),
                             ("/api/settings", "GET"), ("/api/console", "GET"), ("/api/auth/login", "POST"),
                             ("/api/settings", "POST"), ("/api/workloads", "DELETE"), ("/healthz", "POST")):
            h, result = self.handler(path, method)
            self.assertTrue(result); self.assertEqual(503, h._send.call_args.args[0]); h._who.assert_not_called()

    def test_read_only_boot_serves_ui_but_never_offers_first_admin_setup(self):
        h, result = self.handler("/api/auth/state")
        self.assertEqual((200, {"data_handoff": True, "setup": False}), h._send.call_args.args)
        for path in ("/", "/settings", "/style.css", "/js/auth.js", "/assets/homestead-mark.svg", "/sw.js"):
            h, result = self.handler(path)
            self.assertFalse(result, path); h._send.assert_not_called()

    def test_handoff_progress_keeps_normal_authentication(self):
        h, result = self.handler("/api/self/data/handoff/" + "a" * 24)
        self.assertTrue(result); self.assertEqual(401, h._send.call_args.args[0]); h._who.assert_called_once()

    def test_missing_store_after_initialization_and_unreadable_accounts_are_not_ready(self):
        Path(self.temp.name, server.OPS.STORE_MARKER).write_text('{"version":1}', encoding="utf-8")
        h, _ = self.handler("/healthz"); self.assertEqual(503, h._send.call_args.args[0])
        Path(self.temp.name, server.OPS.STORE).write_text("[]", encoding="utf-8")
        server.AUTH.review_signing_key.side_effect = RuntimeError("private-detail")
        h, _ = self.handler("/healthz"); self.assertEqual(503, h._send.call_args.args[0])
        self.assertNotIn("private-detail", json.dumps(h._send.call_args.args[1]))
        h, result = self.handler("/js/auth.js")
        self.assertFalse(result); h._send.assert_not_called()

    def test_activation_waits_for_done_and_starts_background_exactly_once(self):
        self.fence.inspect.side_effect = [Held("API unavailable"), {"mode": "start", "writable": False}, {"mode": "done", "writable": True}]
        with mock.patch.object(server, "start_background_tasks") as start, mock.patch.object(server.time, "sleep") as pause:
            server.finish_self_data_boot(); server.finish_self_data_boot()
        start.assert_called_once(); self.assertEqual(2, pause.call_count)
        self.assertFalse(server._self_data_boot_pending)

    def test_partial_background_launch_is_not_repeated_and_readiness_is_revoked(self):
        self.fence.inspect.return_value = {"mode": "done", "writable": True}
        with mock.patch.object(server, "start_background_tasks", side_effect=RuntimeError("private-error")) as start:
            server.finish_self_data_boot()
        start.assert_called_once(); self.assertTrue(server._self_data_boot_pending)
        self.assertTrue(server._self_data_boot_failed)
        h, _ = self.handler("/healthz"); self.assertEqual(503, h._send.call_args.args[0])

    def test_background_launcher_checks_write_fence_before_starting_any_thread(self):
        with mock.patch.object(server.threading, "Thread") as thread:
            with self.assertRaises(Held): server.start_background_tasks()
            thread.assert_not_called()

    def test_normal_health_endpoint_is_json_not_the_application_shell(self):
        self.assertFalse(server.is_page_path("/healthz"))
        self.fence.require_write.side_effect = None
        with mock.patch.object(server, "_self_data_boot_pending", False):
            h, result = self.handler("/healthz")
            self.assertFalse(result)
            h._file = mock.Mock(side_effect=AssertionError("health probe served HTML"))
            h.do_GET()
        self.assertEqual((200, {"ok": True}), h._send.call_args.args)

    def test_source_leaves_service_when_pointer_blocks_writes(self):
        with mock.patch.object(server, "_self_data_boot_pending", False):
            h, result = self.handler("/healthz")
        self.assertTrue(result)
        self.assertEqual(503, h._send.call_args.args[0])

    def test_unpublished_source_keeps_existing_route_for_read_only_recovery(self):
        self.fence.recovery.return_value = {"mode": "recovery", "writable": False, "operation": "a" * 24}
        with mock.patch.object(server, "_self_data_boot_pending", False):
            for path in ("/healthz", "/api/auth/state"):
                h, result = self.handler(path)
                self.assertTrue(result)
                self.assertEqual(200, h._send.call_args.args[0])
                self.assertTrue(h._send.call_args.args[1]["recovery"])

    def test_a_signed_out_admin_can_sign_in_to_recover_without_anything_written(self):
        self.fence.inspect.return_value = {"mode": "recovery", "writable": False, "operation": "a" * 24}
        h = object.__new__(server.H)
        h.command, h.path, h.headers = "POST", "/api/auth/login", {"X-Homestead-Auth": "1", "Content-Length": "40"}
        h._send, h._who, h._set_cookie = mock.Mock(), mock.Mock(return_value=None), mock.Mock()
        h._body = mock.Mock(return_value={"username": "admin", "password": "right"})
        h._client_ip, h._cookies = mock.Mock(return_value="192.0.2.7"), mock.Mock(return_value={})
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
                mock.patch.object(server.AUTH, "login_read_only", return_value="token") as login, \
                mock.patch.object(server.AUTH, "login", side_effect=AssertionError("the writing login")), \
                mock.patch.object(server.HISTORY, "add", side_effect=AssertionError("a sign-in log write"), create=True):
            self.assertTrue(h._guard("/api/auth/login"))
        login.assert_called_once_with("admin", "right", "192.0.2.7", False)
        h._set_cookie.assert_called_once()
        self.assertEqual(200, h._send.call_args.args[0])
        h.headers = {}
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False):
            h._guard("/api/auth/login")
        self.assertEqual(403, h._send.call_args.args[0], "the same cross-site guard as the normal sign-in")
        self.fence.inspect.return_value = {"mode": "start", "writable": False}
        h, _ = self.handler("/api/auth/login", "POST")
        self.assertEqual(503, h._send.call_args.args[0], "only while recovering an unstarted move")

    def test_read_only_sign_in_checks_the_password_and_saves_nothing(self):
        salt = "c2FsdHNhbHRzYWx0c2FsdA=="
        users = {"users": {"admin": {"salt": salt, "hash": server.AUTH._hash("right", salt), "role": "admin"}}}
        with mock.patch.object(server.AUTH, "_load", return_value=users), \
                mock.patch.object(server.AUTH, "_save", side_effect=AssertionError("saved")), \
                mock.patch.object(server.AUTH, "issue_token", return_value="token"), \
                mock.patch.dict(server.AUTH._attempts, clear=True):
            self.assertEqual("token", server.AUTH.login_read_only("Admin", "right", "192.0.2.8"))
            with self.assertRaisesRegex(PermissionError, "incorrect"):
                server.AUTH.login_read_only("admin", "wrong", "192.0.2.8")

    def test_recovery_action_requires_real_admin_and_csrf_not_status_capability(self):
        self.fence.inspect.return_value = {"mode": "recovery", "writable": False, "operation": "a" * 24}
        path = "/api/self/data/abandon"
        h, _ = self.handler(path, "POST")
        self.assertEqual(401, h._send.call_args.args[0])
        h._who.return_value = {"user": "admin", "role": "admin"}
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False):
            h._guard(path)
        self.assertEqual(403, h._send.call_args.args[0])
        h.headers["X-Homestead-Auth"] = "1"
        h._who.return_value = {"user": "viewer", "role": "viewer"}
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False):
            h._guard(path)
        self.assertEqual(403, h._send.call_args.args[0])
        self.assertEqual("admin", server.needed_role(path, "POST"))

    def test_real_http_listener_reports_read_only_readiness_and_refuses_feature_routes(self):
        listener = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False), mock.patch.object(server.H, "log_message"):
            thread.start()
            try:
                for path, method, expected in (("/healthz", "GET", 200), ("/api/auth/state", "GET", 200),
                                               ("/api/operations", "GET", 503), ("/api/settings", "POST", 503)):
                    client = http.client.HTTPConnection(*listener.server_address, timeout=3)
                    try:
                        client.request(method, path)
                        response = client.getresponse()
                        self.assertEqual(expected, response.status)
                        self.assertTrue(json.loads(response.read())["data_handoff"])
                    finally:
                        client.close()
            finally:
                listener.shutdown(); thread.join(3); listener.server_close()
        self.assertEqual([], list(Path(self.temp.name).iterdir()))

    def test_probe_requires_real_application_http_endpoint(self):
        base = {"name": "homestead", "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}}}
        F = server.SELF_DATA_FENCE
        F.require_app_readiness({"containers": [base]}, "homestead")
        named = copy.deepcopy(base)
        named["env"] = [{"name": "PORT", "value": "8090"}]
        named["ports"] = [{"name": "web", "containerPort": 8090}]
        named["readinessProbe"]["httpGet"]["port"] = "web"
        F.require_app_readiness({"containers": [named]}, "homestead")
        for change in (lambda c: c.pop("readinessProbe"),
                       lambda c: c["readinessProbe"]["httpGet"].update(path="/"),
                       lambda c: c["readinessProbe"]["httpGet"].update(port=9999),
                       lambda c: c["readinessProbe"]["httpGet"].update(host="other-pod"),
                       lambda c: c["readinessProbe"]["httpGet"].update(httpHeaders=[{"name": "Host", "value": "other"}]),
                       lambda c: c["readinessProbe"].update(exec={"command": ["true"]}),
                       lambda c: c.update(envFrom=[{"configMapRef": {"name": "unknown-port"}}]),
                       lambda c: c.update(env=[{"name": "PORT", "valueFrom": {"secretKeyRef": {"name": "port"}}}]),
                       lambda c: c.update(command=["custom-proxy"]), lambda c: c.update(args=["other.py"])):
            with self.subTest(change=change):
                candidate = copy.deepcopy(base); change(candidate)
                with self.assertRaises(Held): F.require_app_readiness({"containers": [candidate]}, "homestead")

    def test_real_entrypoint_loads_and_serves_held_destination_without_creating_data(self):
        # Exercise the full module's actual imports/bindings and HTTP handlers.
        # Only cluster identity is mocked here; Fence's UID/mount/control proof
        # is covered by test_self_data_fence, not claimed by this boot test.
        script = r'''
import builtins, io, os, pathlib, runpy, sys, urllib.request
from unittest import mock
sys.path.insert(0, sys.argv[1])
import homestead_self_data_fence as F
import homestead_auth as AUTH
import http.server
root = pathlib.Path(sys.argv[2]) / "uncreated"
os.environ.update(DATA_DIR=str(root), HOSTNAME="pod1", PORT="0", STORAGE_CLASS="fixture")
opened, exists = builtins.open, os.path.exists
def file_open(path, *args, **kwargs):
    if str(path) == "/var/run/secrets/kubernetes.io/serviceaccount/token": return io.StringIO("fixture-token")
    if str(path) == "/var/run/secrets/kubernetes.io/serviceaccount/namespace": return io.StringIO("lab")
    return opened(path, *args, **kwargs)
def present(path):
    if str(path) == "/var/run/secrets/kubernetes.io/serviceaccount/token": return True
    return exists(path)
class Server:
    def __init__(self, address, handler): self.handler = handler
    def serve_forever(self):
        h = object.__new__(self.handler)
        h.command, h.path, h.headers = "GET", "/healthz", {}
        h._send = mock.Mock(); h.do_GET()
        assert h._send.call_args.args == (200, {"ok": True, "data_handoff": True, "read_only": True}), h._send.call_args
        h.path = "/api/push/key"; h.do_GET()
        assert h._send.call_args.args[0] == 503
        assert not root.exists(), "read-only startup wrote the copied data directory"
        assert len(threads.call_args_list) == 1
        assert threads.call_args.kwargs["name"] == "data-move-startup"
        print("full-app-read-only-startup")
with mock.patch("builtins.open", side_effect=file_open), mock.patch("os.path.exists", side_effect=present), \
     mock.patch.object(F, "Fence") as fence, mock.patch("threading.Thread") as threads, \
     mock.patch.object(AUTH, "review_signing_key", return_value=b"fixture"), \
     mock.patch.object(http.server, "ThreadingHTTPServer", Server), \
     mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("unexpected cluster request")):
    fence.return_value.inspect.return_value = {"mode": "start", "writable": False}
    runpy.run_path(str(pathlib.Path(sys.argv[1]) / "server.py"), run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(server.__file__).parent), self.temp.name],
            capture_output=True, text=True, timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("full-app-read-only-startup", result.stdout)


if __name__ == "__main__": unittest.main()
