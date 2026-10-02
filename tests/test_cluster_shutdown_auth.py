"""Authentication must remain usable behind the cluster shutdown write fence."""
import base64
import copy
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import server


class ShutdownAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.auth = server.AUTH
        for target, name, value in (
                (self.auth, "NS", "lab"), (self.auth, "SECRET_NAME", lambda: "homestead-auth"),
                (self.auth, "ITERATIONS", 1000), (self.auth, "_attempts", {}),
                (self.auth, "_store_cache", {"at": 0, "data": None}),
                (self.auth, "ksend", server._auth_ksend),
                (server, "_self_data_fence", None), (server, "_self_data_barrier", None)):
            self.enterContext(mock.patch.object(target, name, value))
        self.store = {"users": {"admin": {"salt": "test-salt", "hash": self.auth._hash("test-password", "test-salt"),
                                        "ver": 1, "role": "admin", "id": "test-account"}},
                      "signing_key": "test-key"}
        self.version = 1
        self.saved = []
        self.enterContext(mock.patch.object(self.auth, "kget", side_effect=self.get))
        self.network = self.enterContext(mock.patch.object(server.urllib.request, "urlopen", side_effect=self.send))
        self.shutdown = self.enterContext(mock.patch.object(server.CLUSTER_SHUTDOWN.Shutdown, "state",
                                                            return_value={"phase": "handoff"}))
        self.revoke = self.enterContext(mock.patch.object(self.auth.SESSIONS, "revoke"))

    def get(self, path):
        self.assertEqual(path, "/api/v1/namespaces/lab/secrets/homestead-auth")
        return {"metadata": {"resourceVersion": str(self.version)},
                "data": {"store.json": base64.b64encode(json.dumps(self.store).encode()).decode()}}

    def send(self, request, **kwargs):
        self.assertEqual(request.full_url, server.API + "/api/v1/namespaces/lab/secrets/homestead-auth")
        self.assertEqual(request.method, "PUT")
        body = json.loads(request.data)
        self.assertEqual(body["metadata"]["resourceVersion"], str(self.version))
        self.store = json.loads(base64.b64decode(body["data"]["store.json"]))
        self.saved.append(copy.deepcopy(self.store))
        self.version += 1
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"metadata": {"resourceVersion": str(self.version)}}).encode()
        return response

    def body(self):
        return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": "homestead-auth", "namespace": "lab", "resourceVersion": str(self.version)},
                "data": {"store.json": base64.b64encode(json.dumps(self.store).encode()).decode()}}

    def test_login_and_durable_revocation_work_in_every_active_shutdown_phase(self):
        for phase in ("preparing", "draining", "stopping-homestead", "detaching", "handoff"):
            with self.subTest(phase=phase):
                self.shutdown.return_value = {"phase": phase}
                before = len(self.saved)
                token = self.auth.login("admin", "test-password", "192.0.2.1")
                self.assertEqual(self.auth.verify_token(token, force=True)["role"], "admin")
                self.assertTrue(self.saved[before]["login_limits"], "reserve a durable attempt before checking the password")
                self.assertIn("last_login", self.store["users"]["admin"])
                version = self.store["users"]["admin"]["ver"]
                self.auth.logout_everywhere("admin")
                self.assertEqual(self.store["users"]["admin"]["ver"], version + 1)
                self.revoke.assert_called_with("admin")
                self.auth._store_cache.update(at=0, data=None)
                self.assertIsNone(self.auth.verify_token(token, force=True))

    def test_failed_login_limits_survive_a_restart_during_handoff(self):
        for _ in range(self.auth.MAX_ATTEMPTS):
            with self.assertRaisesRegex(PermissionError, "incorrect"):
                self.auth.login("admin", "wrong", "192.0.2.2")
            self.auth._attempts.clear()
            self.auth._store_cache.update(at=0, data=None)
        before = self.network.call_count
        with self.assertRaisesRegex(PermissionError, "too many attempts"):
            self.auth.login("admin", "test-password", "192.0.2.2")
        self.assertEqual(self.network.call_count, before)

    def test_password_change_invalidates_old_tokens_during_handoff(self):
        token = self.auth.login("admin", "test-password", "192.0.2.3")
        self.auth.change_password("admin", "test-password", "replacement-password")
        self.assertIsNone(self.auth.verify_token(token, force=True))
        self.revoke.assert_called_once_with("admin")
        self.assertTrue(self.auth.login("admin", "replacement-password", "192.0.2.3"))

    def test_unreadable_shutdown_journal_does_not_prevent_authentication(self):
        self.shutdown.side_effect = ValueError("invalid shutdown journal")
        self.assertTrue(self.auth.login("admin", "test-password", "192.0.2.4"))
        self.auth.logout_everywhere("admin")
        self.shutdown.assert_not_called()

    def test_regular_writes_including_the_auth_secret_remain_fenced(self):
        for method, path, body in (
                ("PUT", "/api/v1/namespaces/lab/secrets/homestead-auth", self.body()),
                ("PUT", "/api/v1/namespaces/lab/secrets/another-secret", self.body()),
                ("PATCH", "/apis/apps/v1/namespaces/lab/deployments/app", {})):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "shutdown is active"):
                server.ksend(method, path, body)
        self.network.assert_not_called()

    def test_auth_binding_rejects_other_resources_and_request_shapes(self):
        path = "/api/v1/namespaces/lab/secrets/homestead-auth"
        for method, target, changes in (
                ("PUT", path + "?fieldManager=other", {}),
                ("PUT", path.replace("lab", "other"), {}),
                ("PUT", path.replace("homestead-auth", "another-secret"), {}),
                ("POST", path.rsplit("/", 1)[0], {"metadata": {"name": "another-secret", "namespace": "lab"}}),
                ("PATCH", path, {}), ("DELETE", path, {}),
                ("PUT", path, {"kind": "ConfigMap"}),
                ("PUT", path, {"metadata": {"name": "homestead-auth", "namespace": "other"}}),
                ("PUT", path, {"data": {"other": "value"}}),
                ("PUT", path, {"stringData": {"other": "value"}})):
            with self.subTest(method=method, target=target, changes=changes), self.assertRaises(ValueError):
                server._auth_ksend(method, target, {**self.body(), **changes})
        self.network.assert_not_called()

    def test_auth_secret_creation_is_allowed_only_through_internal_binding(self):
        with mock.patch.object(server, "_ksend", return_value={}) as transport:
            server._auth_ksend("POST", "/api/v1/namespaces/lab/secrets", self.body())
        self.assertTrue(transport.call_args.kwargs["shutdown_bypass"])

    def test_auth_bypass_preserves_data_move_and_storage_guards(self):
        with mock.patch.object(server, "require_self_data_write", side_effect=ValueError("data held")):
            with self.assertRaisesRegex(ValueError, "data held"):
                self.auth.logout_everywhere("admin")
        with mock.patch.object(server.STORAGE_GUARD, "send", side_effect=ValueError("storage held")):
            with self.assertRaisesRegex(ValueError, "storage held"):
                self.auth.logout_everywhere("admin")
        self.network.assert_not_called()
        self.revoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
