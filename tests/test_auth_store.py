import base64
import json
import socket
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_auth as AUTH


class Cluster:
    """An API server that can be slow: every read times out while `slow`."""

    def __init__(self, store=None):
        self.slow = False
        self.saved = []
        self.store = store

    def get(self, path):
        if self.slow:
            raise socket.timeout("timed out")
        if "/secrets/" in path:
            if self.store is None:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return {"data": {"store.json": base64.b64encode(json.dumps(self.store).encode()).decode()}}
        raise urllib.error.HTTPError(path, 404, "missing", None, None)

    def send(self, method, path, body=None, **kw):
        self.saved.append((method, body))
        self.store = json.loads(base64.b64decode(body["data"]["store.json"]))
        return body


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        AUTH._store_cache.update(at=0, data=None)
        self.cluster = Cluster(store={"users": {"robin": {"salt": "s", "hash": "h", "ver": 1, "role": "admin"}},
                                      "signing_key": "k" * 40})
        self.saved_io = (AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME)
        AUTH.kget, AUTH.ksend = self.cluster.get, self.cluster.send
        AUTH.SECRET_NAME = lambda: "homestead-auth"

    def tearDown(self):
        AUTH.kget, AUTH.ksend, AUTH.SECRET_NAME = self.saved_io
        AUTH._store_cache.update(at=0, data=None)

    def test_a_slow_cluster_is_never_mistaken_for_a_fresh_install(self):
        self.cluster.slow = True
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.needs_setup()
        with self.assertRaises(AUTH.StoreUnavailable):
            AUTH.create_user("intruder", "a-long-password", first_only=True)
        self.assertEqual([], self.cluster.saved, "nothing may be written over the real accounts")

    def test_the_last_good_read_stands_in_while_the_cluster_is_slow(self):
        self.assertFalse(AUTH.needs_setup())
        token = AUTH.issue_token("robin")
        self.cluster.slow = True
        self.assertFalse(AUTH.needs_setup())
        self.assertEqual("robin", AUTH.verify_token(token)["user"])
        with self.assertRaises(PermissionError):
            AUTH.create_user("intruder", "a-long-password", first_only=True)

    def test_a_missing_secret_is_still_a_fresh_install(self):
        self.cluster.store = None
        self.assertTrue(AUTH.needs_setup())


if __name__ == "__main__":
    unittest.main()
