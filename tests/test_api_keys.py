"""API keys: every one expires, holds only its scopes, and is kept as a hash."""
import base64
import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_auth as auth
import homestead_api_keys as keys

DAY = 86400


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.store = {"users": {}, "signing_key": "k" * 48}
        auth.bind(self._get, self._send, "lab")
        auth._store_cache.update(at=0, data=None)
        auth._attempts.clear()
        auth.create_user("ada", "correct-horse-battery", first_only=True)
        auth.create_user("bob", "correct-horse-battery-2", role="operator")
        keys.bind(tempfile.mkdtemp())
        keys._usage_written.clear()

    rv = None

    def _get(self, path):
        if "/secrets/" in path:
            return {"metadata": {"name": path.rsplit("/", 1)[-1], **({"resourceVersion": self.rv} if self.rv else {})},
                    "data": {"store.json": base64.b64encode(json.dumps(self.store).encode()).decode()}}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def _send(self, _method, _path, body=None, **_kw):
        raw = (body or {}).get("data", {}).get("store.json", "")
        if raw:
            self.store = json.loads(base64.b64decode(raw).decode())
        return body or {}

    def make(self, scopes=("read",), ttl=30 * DAY, networks=(), owner="ada", name="Home Assistant"):
        return keys.create(name, list(scopes), ttl, list(networks), owner)

    def test_a_key_is_shown_once_and_kept_only_as_a_hash(self):
        made = self.make()
        token = made["token"]
        self.assertRegex(token, r"^hsk_[0-9a-f]{12}_[A-Za-z0-9_-]{43}$")
        stored = json.dumps(self.store)
        self.assertNotIn(token.rsplit("_", 1)[1], stored, "the secret is never written down")
        self.assertNotIn("hash", json.dumps(keys.list_keys()), "nor its hash shown")
        self.assertEqual({"id", "name", "owner", "scopes", "expires"}, set(keys.verify(token, "192.0.2.5")))

    def test_every_key_expires_within_a_year_and_an_expired_one_is_refused(self):
        for ttl in (60, 400 * DAY, None, "soon"):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.make(ttl=ttl, name=f"k{ttl}")
        token = self.make(ttl=3600)["token"]
        with self.assertRaisesRegex(PermissionError, "expired"):
            keys.verify(token, "192.0.2.5", now=time.time() + 3601)

    def test_a_wrong_secret_or_unknown_key_is_refused_and_counted(self):
        token = self.make()["token"]
        kid = token.split("_")[1]
        for bad in (token[:-1] + ("A" if token[-1] != "A" else "B"), f"hsk_{'0' * 12}_{'a' * 43}", "nonsense", ""):
            with self.subTest(bad=bad), self.assertRaisesRegex(PermissionError, "not valid"):
                keys.verify(bad, "192.0.2.9")
        self.assertTrue(kid)
        for _ in range(keys.FAILURES_ALLOWED):
            try:
                keys.verify("hsk_000000000000_" + "b" * 43, "192.0.2.9")
            except PermissionError:
                pass
        with self.assertRaisesRegex(PermissionError, "too many"):
            keys.verify("hsk_000000000000_" + "c" * 43, "192.0.2.9")
        # Many clients can share an address behind the load balancer: one
        # misbehaving must not lock the right key out.
        self.assertTrue(keys.verify(token, "192.0.2.9"), "a right key is never held back")

    def test_every_kind_of_refusal_counts(self):
        expired = self.make(ttl=3600, name="short")["token"]
        for _ in range(keys.FAILURES_ALLOWED):
            with self.assertRaises(PermissionError):
                keys.verify(expired, "192.0.2.11", now=time.time() + 7200)
        with self.assertRaisesRegex(PermissionError, "too many"):
            keys.verify(expired, "192.0.2.11", now=time.time() + 7200)

    def test_removing_a_user_removes_their_keys_so_a_new_namesake_gets_none(self):
        auth.set_role("bob", "admin", "ada")
        token = self.make()["token"]
        auth.delete_user("ada", "bob")
        self.assertEqual([], keys.list_keys())
        auth.create_user("ada", "another-persons-password", role="admin")
        with self.assertRaisesRegex(PermissionError, "not valid"):
            keys.verify(token, "192.0.2.5")

    def test_a_save_over_a_change_made_elsewhere_is_refused_not_written(self):
        made = self.make()
        self.rv = "7"
        original = self._send

        def send(method, path, body=None, **kw):
            if method == "PUT" and (body or {}).get("metadata", {}).get("resourceVersion") != "8":
                raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
            return original(method, path, body, **kw)
        auth.bind(self._get, send, "lab")
        auth._store_cache.update(at=0, data=None)
        with self.assertRaises(auth.StoreConflict):
            keys.revoke(made["key"]["id"])
        self.assertTrue(keys.verify(made["token"], "192.0.2.5"), "nothing was written over")

    def test_a_revoked_key_is_refused_within_seconds_on_another_replica(self):
        made = self.make()
        auth._store_cache["at"] = time.time() - keys.FRESH_FOR - 1   # this replica's copy is old
        self.store["api_keys"] = {}                                   # revoked on another
        with self.assertRaisesRegex(PermissionError, "not valid"):
            keys.verify(made["token"], "192.0.2.5")

    def test_a_key_held_to_networks_is_refused_elsewhere(self):
        token = self.make(networks=["192.0.2.20", "198.51.100.0/24"])["token"]
        self.assertTrue(keys.verify(token, "192.0.2.20"))
        self.assertTrue(keys.verify(token, "198.51.100.77"))
        with self.assertRaisesRegex(PermissionError, "cannot be used from"):
            keys.verify(token, "192.0.2.21")
        with self.assertRaises(ValueError):
            self.make(networks=["not-an-address"], name="bad net")

    def test_a_key_never_outranks_its_maker(self):
        token = self.make(scopes=["read", "containers:control"])["token"]
        self.assertEqual(["containers:control", "read"], keys.verify(token, "192.0.2.5")["scopes"])
        auth.set_role("bob", "admin", "ada")
        auth.set_role("ada", "viewer", "bob")
        self.assertEqual(["read"], keys.verify(token, "192.0.2.5")["scopes"], "demoted: control is gone")
        auth.delete_user("ada", "bob")
        with self.assertRaisesRegex(PermissionError, "not valid"):
            keys.verify(token, "192.0.2.5")              # removed with her

    def test_only_an_administrator_makes_keys_and_scopes_are_checked(self):
        with self.assertRaises(PermissionError):
            self.make(owner="bob")
        for scopes in ([], ["admin"], ["read", "everything"]):
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                self.make(scopes=scopes, name=f"s{len(scopes)}")
        self.make()
        with self.assertRaisesRegex(ValueError, "already a key named"):
            self.make()

    def test_revoking_stops_a_key_at_once(self):
        made = self.make()
        keys.revoke(made["key"]["id"])
        with self.assertRaisesRegex(PermissionError, "not valid"):
            keys.verify(made["token"], "192.0.2.5")
        with self.assertRaises(ValueError):
            keys.revoke(made["key"]["id"])

    def test_last_use_is_kept_beside_not_in_the_accounts(self):
        made = self.make()
        before = json.dumps(self.store)
        keys.verify(made["token"], "192.0.2.5")
        self.assertEqual(before, json.dumps(self.store), "using a key never rewrites the accounts")
        listed = keys.list_keys()[0]
        self.assertEqual("192.0.2.5", listed["last_ip"])
        self.assertTrue(listed["last_used"])

    def test_a_signed_in_person_reaches_the_api_with_their_roles_scopes(self):
        self.assertEqual(["read"], keys.scopes_for_role("viewer"))
        self.assertEqual(["read", "containers:control", "vms:control"], keys.scopes_for_role("operator"))


if __name__ == "__main__":
    unittest.main()
