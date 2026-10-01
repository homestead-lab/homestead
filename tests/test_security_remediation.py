"""Adversarial checks for the October 2026 review, using bounded local fixtures."""
import ast
import base64
import copy
import gzip
import hashlib
from http.client import HTTPConnection
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import server
import homestead_auth as auth
import homestead_host_access as host
import homestead_http as http
import homestead_icons as icons
import homestead_sessions as sessions
import homestead_helm as helm
import homestead_vms as vms
import homestead_yaml as yaml
import homestead_compose as compose
import homestead_updates as updates
import homestead_files as files
import homestead_fleet as fleet
import homestead_config_backup as backup


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.store = {"users": {"owner": {"id": "owner-id", "ver": 1, "role": "admin", "salt": "YQ==", "hash": "hash"}}, "signing_key": "k" * 48}
        self.stack = mock.patch.object(auth, "_load", side_effect=lambda **kw: self.store)
        self.stack.start()
        self.save = mock.patch.object(auth, "_save").start()
        self.hash = mock.patch.object(auth, "_hash", return_value="hash").start()
        self.addCleanup(mock.patch.stopall)
        auth._attempts.clear()

    def test_recreated_username_cannot_inherit_old_cookie(self):
        auth.create_user("visitor", "long-fixture-password", role="viewer")
        cookie = auth.issue_token("visitor")
        auth.delete_user("visitor", "owner")
        auth.create_user("visitor", "different-long-password", role="admin")
        self.assertIsNone(auth.verify_token(cookie))
        self.assertEqual("admin", auth.verify_token(auth.issue_token("visitor"))["role"])

    def test_legacy_format_cookie_is_invalid_even_with_a_valid_signature(self):
        raw = base64.urlsafe_b64encode(json.dumps({"u": "owner", "v": 1, "exp": time.time() + 100}).encode()).decode().rstrip("=")
        self.assertIsNone(auth.verify_token(raw + "." + auth._sign(raw, b"k" * 48)))

    def test_legacy_accounts_can_sign_in_without_writing_during_recovery(self):
        self.store["users"]["owner"].pop("id")
        token = auth.login_read_only("owner", "password", "127.0.0.1")
        self.assertEqual("owner", auth.verify_token(token)["user"])
        self.save.assert_not_called()

    def test_password_change_and_revoke_all_invalidate_new_tokens(self):
        token = auth.issue_token("owner")
        auth.logout_everywhere("owner")
        self.assertIsNone(auth.verify_token(token))
        token = auth.issue_token("owner")
        auth.change_password("owner", "password", "new-long-password")
        self.assertIsNone(auth.verify_token(token))

    def test_parallel_attempt_reservation_has_one_atomic_limit(self):
        barrier = threading.Barrier(24)
        admitted, lock = [], threading.Lock()
        def attempt():
            barrier.wait()
            try:
                auth._reserve_attempt("owner", "192.0.2.1", shared=False)
                with lock:
                    admitted.append(True)
            except PermissionError:
                pass
        threads = [threading.Thread(target=attempt) for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(auth.MAX_ATTEMPTS, len(admitted))


class SharedAdmissionTests(unittest.TestCase):
    """Model resourceVersion conflicts instead of accepting unconditional writes."""
    def setUp(self):
        self.record = None
        self.version = 0
        self.methods = []
        self.old_cache = dict(auth._store_cache)
        auth._store_cache.update(at=0, data=None)
        auth._attempts.clear()
        self.addCleanup(auth._store_cache.update, self.old_cache)
        self.addCleanup(auth._attempts.clear)
        for module in (auth, fleet):
            for name, callback in (("kget", self.get), ("ksend", self.send)):
                patch = mock.patch.object(module, name, side_effect=callback)
                patch.start()
                self.addCleanup(patch.stop)

    def get(self, path):
        if self.record is None:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.record)

    def send(self, method, path, body):
        self.methods.append(method)
        if ((method == "POST" and self.record is not None) or
                (method == "PUT" and body["metadata"].get("resourceVersion") != str(self.version))):
            raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
        self.version += 1
        self.record = copy.deepcopy(body)
        self.record["metadata"]["resourceVersion"] = str(self.version)
        return copy.deepcopy(self.record)

    def test_login_limits_survive_process_restart(self):
        for _ in range(auth.MAX_ATTEMPTS):
            auth._attempts.clear()  # another replica or restarted process
            auth._reserve_attempt("owner", "192.0.2.1")
        auth._attempts.clear()
        with self.assertRaisesRegex(PermissionError, "too many"):
            auth._reserve_attempt("owner", "192.0.2.1")

    def test_absent_account_store_create_conflict_never_replaces_accounts(self):
        original = {"users": {"owner": {"id": "fixture-id"}}, "signing_key": "fixture-key"}
        auth._save(copy.deepcopy(original))
        with self.assertRaises(auth.StoreConflict):
            auth._save({"users": {}, "signing_key": "other-key"})
        self.assertEqual(["POST", "POST"], self.methods)
        self.assertEqual(original, json.loads(base64.b64decode(self.record["data"]["store.json"])))

    def test_fleet_replay_is_refused_by_another_process(self):
        fleet._claim_nonce("fixture-member", "fixture-nonce")
        with self.assertRaisesRegex(PermissionError, "already used"):
            fleet._claim_nonce("fixture-member", "fixture-nonce")
        fleet._claim_nonce("fixture-member", "another-nonce")
        self.assertEqual(["POST", "PUT"], self.methods)

    def test_fleet_conflict_retries_from_fresh_resource_version(self):
        real_send = self.send
        conflicts = [True]
        def concurrent_create(method, path, body):
            if conflicts and conflicts.pop():
                real_send(method, path, body)
                raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
            return real_send(method, path, body)
        # The competing process claimed this exact nonce; retry must detect it.
        with mock.patch.object(fleet, "ksend", side_effect=concurrent_create), self.assertRaisesRegex(PermissionError, "already used"):
            fleet._claim_nonce("fixture-member", "fixture-race")


class TargetAuthorizationTests(unittest.TestCase):
    def setUp(self):
        host.set_role("operator")
        self.addCleanup(host.set_role, None)
        self.obj = {"metadata": {"name": "app", "namespace": "lab"}, "spec": {"template": {"spec": {"containers": [{"name": "app", "image": "fixture/app"}]}}}}

    def test_operator_can_edit_an_ordinary_app(self):
        host.require_target(self.obj)
        host.require_edit(self.obj, copy.deepcopy(self.obj))

    def test_management_identity_helpers_and_system_namespaces_are_denied(self):
        variants = []
        for key, value in (("serviceAccountName", "homestead"), ("hostPID", True), ("hostNetwork", True)):
            obj = copy.deepcopy(self.obj)
            obj["spec"]["template"]["spec"][key] = value
            variants.append(obj)
        obj = copy.deepcopy(self.obj)
        obj["metadata"]["name"] = "homestead-host-random"
        variants.append(obj)
        obj = copy.deepcopy(self.obj)
        obj["metadata"]["namespace"] = "kube-system"
        variants.append(obj)
        obj = copy.deepcopy(self.obj)
        obj["metadata"]["labels"] = {"homestead.io/task": "host-shell"}
        variants.append(obj)
        for obj in variants:
            with self.subTest(obj=obj), self.assertRaises(PermissionError):
                host.require_target(obj)
        host.set_role("admin")
        for obj in variants:
            host.require_target(obj)

    def test_operator_sidecar_cannot_keep_a_powerful_service_account(self):
        self.obj["spec"]["template"]["spec"]["serviceAccountName"] = "homestead"
        with self.assertRaises(PermissionError):
            server.build_sidecar_deployment({"namespace": "lab", "name": "sidecar", "image": "fixture/app"}, self.obj)

    def test_operator_console_cannot_exec_an_alias_of_management(self):
        pod = {"metadata": {"name": "innocent-name"}, "spec": {"serviceAccountName": "homestead", "containers": [{"name": "main"}]}, "status": {"phase": "Running"}}
        with mock.patch.object(server.CONSOLE_PROXY, "kget", return_value=pod), self.assertRaises(PermissionError):
            server.CONSOLE_PROXY.validate_pod("lab", "innocent-name", "main")

    def test_new_workloads_do_not_mount_service_account_tokens(self):
        obj, _ = server.build_deployment({"name": "ordinary", "image": "fixture/app"})
        self.assertFalse(obj["spec"]["template"]["spec"]["automountServiceAccountToken"])


class SensitiveResponseTests(unittest.TestCase):
    def test_helm_details_hide_values_notes_source_and_history_text(self):
        record = {"name": "demo", "namespace": "lab", "version": 1,
                  "info": {"notes": "fixture-notes-secret", "description": "fixture-history-secret"},
                  "config": {"arbitrary": "fixture-value-secret"}, "chart": {"metadata": {"name": "demo"}}}
        secret = {"data": {"release": base64.b64encode(base64.b64encode(gzip.compress(json.dumps(record).encode()))).decode()}}
        chart = {"metadata": {"name": "demo", "namespace": "lab"}, "spec": {"valuesContent": "fixture-source-secret"}}
        with mock.patch.object(helm, "kget", return_value={"items": [secret]}), mock.patch.object(helm, "helmcharts", return_value=[chart]):
            public = json.dumps(helm.release("lab", "demo", include_sensitive=False))
            private = json.dumps(helm.release("lab", "demo", include_sensitive=True))
        for value in ("fixture-notes-secret", "fixture-value-secret", "fixture-source-secret", "fixture-history-secret"):
            self.assertNotIn(value, public)
            self.assertIn(value, private)

    def test_vm_cloud_init_secret_is_not_read_for_a_viewer(self):
        vm = {"metadata": {"name": "vm", "namespace": "lab"}, "spec": {"template": {"spec": {}}}}
        with mock.patch.object(vms, "_get", return_value=vm), mock.patch.object(vms, "kget", return_value={}), mock.patch.object(vms, "_row", return_value={"disks": []}), mock.patch.object(vms, "_claims", return_value={}), mock.patch.object(vms, "_datavolumes", return_value={}), mock.patch.object(vms, "_filling", return_value=[]), mock.patch.object(vms, "events_for", return_value=[]), mock.patch.object(vms, "_read_cloud_init", return_value={"user_data": "fixture-secret"}) as read:
            answer = vms.detail("lab", "vm", include_sensitive=False)
        read.assert_not_called()
        self.assertNotIn("cloud_init", answer)


class NetworkBoundaryTests(unittest.TestCase):
    def test_icon_connect_uses_only_the_prevalidated_numeric_address(self):
        approved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        raw = mock.Mock()
        raw.getpeername.return_value = ("93.184.216.34", 80)
        with mock.patch.object(icons.socket, "getaddrinfo", side_effect=[approved, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]]) as resolve, mock.patch.object(icons.socket, "socket", return_value=raw):
            parsed, addresses = icons._public_target("http://fixture.example/icon.png")
            icons._PinnedConnection(parsed, addresses, 1).connect()
        self.assertEqual(1, resolve.call_count)
        raw.connect.assert_called_once_with(("93.184.216.34", 80))

    def test_icon_https_verifies_the_original_hostname(self):
        approved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        raw = mock.Mock()
        raw.getpeername.return_value = ("93.184.216.34", 443)
        with mock.patch.object(icons.socket, "getaddrinfo", return_value=approved), mock.patch.object(icons.socket, "socket", return_value=raw), mock.patch.object(icons.ssl, "create_default_context") as ctx:
            parsed, addresses = icons._public_target("https://fixture.example/icon.png")
            icons._PinnedConnection(parsed, addresses, 1).connect()
        ctx.return_value.wrap_socket.assert_called_once_with(raw, server_hostname="fixture.example")

    def test_registry_credential_realms_are_bounded(self):
        for realm in ("http://registry.example/token", "https://attacker.example/token"):
            with self.assertRaises(PermissionError):
                updates._auth_realm("https://registry.example/v2/app", realm, ("user", "secret"))
        updates._auth_realm("https://registry-1.docker.io/v2/app", "https://auth.docker.io/token", ("user", "secret"))

    def test_revocation_closes_idle_console_sockets(self):
        stopped = threading.Event()
        left, right = mock.Mock(), mock.Mock()
        sessions.watch("fixture-session-owner", lambda: True, stopped, left, right)
        sessions.revoke("fixture-session-owner")
        self.assertTrue(stopped.is_set())
        left.shutdown.assert_called_once()
        right.shutdown.assert_called_once()

    def test_cross_replica_recheck_closes_revoked_console(self):
        stopped = threading.Event()
        connection = mock.Mock()
        with mock.patch.object(sessions, "CHECK_SECONDS", 0.01):
            sessions.watch("fixture-other-replica", lambda: False, stopped, connection)
            self.assertTrue(stopped.wait(1))
        connection.shutdown.assert_called_once()


class InputAndStoreTests(unittest.TestCase):
    def test_expanded_aliases_are_bounded_without_expanding_them(self):
        doc = "base: &a [x,x,x,x,x,x,x,x,x,x]\n"
        previous = "a"
        for name in "bcdefghi":
            doc += name + ": &" + name + " [" + ",".join(["*" + previous] * 10) + "]\n"
            previous = name
        with self.assertRaisesRegex(yaml.YamlError, "size limit"):
            yaml.loads(doc)

    def test_nested_environment_values_are_refused(self):
        report = compose._Report()
        result = compose._env({"VALUE": ["a", "b"]}, {}, report, 1)
        self.assertNotIn("VALUE", result)
        self.assertTrue(report.errors)

    def test_stale_authorization_has_a_fixed_maximum_age(self):
        old = dict(auth._store_cache)
        self.addCleanup(auth._store_cache.update, old)
        auth._store_cache.update(at=time.time() - auth.MAX_STALE_SECONDS - 1, data={"users": {"admin": {}}})
        with self.assertRaises(auth.StoreUnavailable):
            auth._unavailable(TimeoutError())
        auth._store_cache.update(at=time.time())
        with self.assertRaises(auth.StoreUnavailable):
            auth._unavailable(TimeoutError(), allow_stale=False)

    def test_backup_rejects_expensive_kdf_before_hashing(self):
        doc = {"format": backup.FORMAT, "version": 1, "kdf": {"name": "scrypt", "n": 2**20, "r": 16, "p": 4}}
        with mock.patch.object(backup, "_keys") as keys, self.assertRaises(ValueError):
            backup.unseal(doc, "password")
        keys.assert_not_called()

    def test_file_save_requires_a_revision_and_uses_verified_rename(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            files.write_file("lab", "data", "config.txt", "new")
        commands = []
        def shell(ns, pod, script, **kw):
            commands.append(script)
            return (b"locked" if "mkdir" in script else b"saved" if "printf saved" in script else b""), ""
        revision = hashlib.sha256(b"old").hexdigest()
        with mock.patch.object(files, "open_session", return_value={"namespace": "lab", "pod": "helper"}), mock.patch.object(files, "_sh", side_effect=shell):
            answer = files.write_file("lab", "data", "config.txt", "new", revision)
        self.assertEqual(hashlib.sha256(b"new").hexdigest(), answer["revision"])
        self.assertTrue(any("mv -fT" in command and "sha256sum" in command for command in commands))
        self.assertFalse(any(" > /data/config.txt" in command for command in commands))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.listener = http.BoundedHTTPServer(("127.0.0.1", 0), server.H, max_connections=2)
        self.thread = threading.Thread(target=self.listener.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.listener.server_close)
        self.addCleanup(self.listener.shutdown)

    def request(self, body, headers, path="/api/auth/setup"):
        client = HTTPConnection(*self.listener.server_address, timeout=3)
        try:
            client.request("POST", path, body=body, headers=headers)
            response = client.getresponse()
            response.read()
            return response.status
        finally:
            client.close()

    def test_cross_origin_form_cannot_claim_bootstrap(self):
        with mock.patch.object(auth, "create_user") as create:
            status = self.request('{"username":"intruder","password":"long-password","ignored":"="}', {"Origin": "https://attacker.example", "Content-Type": "text/plain"})
        self.assertEqual(403, status)
        create.assert_not_called()

    def test_auth_requires_json_even_when_custom_header_is_supplied(self):
        self.assertEqual(415, self.request("{}", {"X-Homestead-Auth": "1", "Content-Type": "text/plain"}))

    def test_operator_http_restart_checks_the_original_workload_identity(self):
        obj = {"metadata": {"name": "target", "namespace": "lab"}, "spec": {"template": {"spec": {"containers": [{"name": "main"}]}}}}
        headers = {"X-Homestead-Auth": "1", "Content-Type": "application/json"}
        body = json.dumps({"ns": "lab", "name": "target"})
        with mock.patch.object(server.H, "_who", return_value={"user": "fixture", "role": "operator"}), mock.patch.object(server, "kget", return_value=obj), mock.patch.object(server, "ksend") as send:
            self.assertEqual(200, self.request(body, headers, "/api/restart"))
            send.assert_called_once()
            send.reset_mock()
            obj["spec"]["template"]["spec"]["serviceAccountName"] = "homestead"
            self.assertEqual(403, self.request(body, headers, "/api/restart"))
            send.assert_not_called()

    def test_control_api_key_cannot_bypass_management_target_checks(self):
        obj = {"metadata": {"name": "target", "namespace": "lab"}, "spec": {"template": {"spec": {"serviceAccountName": "homestead"}}}}
        key = {"name": "fixture-key", "scopes": ["containers:control"]}
        with mock.patch.object(server.API_KEYS, "verify", return_value=key), mock.patch.dict(server.API_V1.ctx, {"workloads": lambda: [{"ns": "lab", "name": "target"}]}), mock.patch.object(server, "kget", return_value=obj), mock.patch.object(server, "ksend") as send:
            self.assertEqual(409, self.request("{}", {"Authorization": "Bearer fixture-token", "Content-Type": "application/json"}, "/api/v1/containers/lab/target/restart"))
            send.assert_not_called()

    def test_slow_headers_have_a_deadline_and_connections_are_bounded(self):
        with mock.patch.object(http, "HEADER_SECONDS", 0.1):
            sock = socket.create_connection(self.listener.server_address)
            try:
                sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: fixture\r\n")
                sock.settimeout(2)
                self.assertEqual(b"", sock.recv(1))
            finally:
                sock.close()

    def test_unknown_routes_have_no_default_low_privilege_policy(self):
        with self.assertRaises(PermissionError):
            server.needed_role("/api/new-undeclared-feature", "GET")

    def test_ambiguous_request_framing_is_rejected_before_auth(self):
        for framing in (b"Content-Length: 0\r\nContent-Length: 1\r\n", b"Transfer-Encoding: chunked\r\n"):
            with self.subTest(framing=framing), socket.create_connection(self.listener.server_address, timeout=3) as sock:
                sock.sendall(b"POST /api/auth/setup HTTP/1.1\r\nHost: fixture\r\n" + framing + b"\r\n")
                self.assertIn(b" 400 ", sock.recv(4096).split(b"\r\n", 1)[0])

    def test_incomplete_body_has_a_total_deadline(self):
        with mock.patch.object(http, "BODY_SECONDS", 0.1), socket.create_connection(self.listener.server_address, timeout=3) as sock:
            sock.sendall(b"POST /api/auth/login HTTP/1.1\r\nHost: fixture\r\nX-Homestead-Auth: 1\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
            self.assertEqual(b"", sock.recv(4096))

    def test_excess_connections_are_closed_and_capacity_recovers(self):
        first = socket.create_connection(self.listener.server_address, timeout=3)
        second = socket.create_connection(self.listener.server_address, timeout=3)
        try:
            until = time.monotonic() + 2
            while self.listener._slots._value and time.monotonic() < until:
                time.sleep(0.01)
            self.assertEqual(0, self.listener._slots._value)
            with socket.create_connection(self.listener.server_address, timeout=3) as excess:
                self.assertEqual(b"", excess.recv(1))
        finally:
            first.close()
            second.close()
        until = time.monotonic() + 2
        while self.listener._slots._value < 2 and time.monotonic() < until:
            time.sleep(0.01)
        self.assertEqual(2, self.listener._slots._value)

    def test_all_literal_http_routes_have_a_declared_policy(self):
        tree = ast.parse((ROOT / "server" / "server.py").read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "H")
        for method in ("GET", "POST", "DELETE"):
            function = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_" + method)
            for node in ast.walk(function):
                if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name) or node.left.id != "p":
                    continue
                for comparator in node.comparators:
                    for literal in ast.walk(comparator):
                        if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and literal.value.startswith("/api/"):
                            with self.subTest(method=method, path=literal.value):
                                server.needed_role(literal.value, method)


if __name__ == "__main__":
    unittest.main()
