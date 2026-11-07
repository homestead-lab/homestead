"""Share settings and passwords are saved on the ConfigMap and Secret as they
are stored: their labels, annotations and other keys stay, passwords that
cannot be read are not saved over, and a change saved meanwhile on another
copy of Homestead is refused in words rather than undone (#621)."""
import base64
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_shares as SHARES
from fakes.kube import Kube
from rebind import keep_bound

MAP = "/api/v1/namespaces/lab/configmaps/homestead-shares"
SECRET = "/api/v1/namespaces/lab/secrets/homestead-share-credentials"


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def secret(credentials=None, raw=None, **meta):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": dict({"name": "homestead-share-credentials", "namespace": "lab"}, **meta),
            "data": {"credentials.json": raw if raw is not None else encoded(credentials or {})}}


def config(rows=(), **meta):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": dict({"name": "homestead-shares", "namespace": "lab"}, **meta),
            "data": {"shares.json": json.dumps(list(rows))}}


def stored_credentials(kube):
    return json.loads(base64.b64decode(kube.object(SECRET)["data"]["credentials.json"]))


class ShareWriteTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, SHARES, SHARES.NAMES)

    def bind(self, send=None):
        SHARES.bind(K.kget, send or K.send, lambda *a, **k: None, "lab", {})

    def test_passwords_that_cannot_be_read_are_not_saved_over(self):
        with Kube(secret(raw=base64.b64encode(b"not json").decode())) as kube:
            self.bind()
            with self.assertRaisesRegex(ValueError, "could not be read"):
                SHARES.save_user("ada", "pw-ada")
            self.assertEqual(base64.b64encode(b"not json").decode(),
                             kube.object(SECRET)["data"]["credentials.json"])

    def test_the_lists_still_show_the_shares_when_their_passwords_cannot_be_read(self):
        with Kube(config([{"name": "media", "pvc": "media-data", "user": "ada"}]),
                  secret(raw=base64.b64encode(b"not json").decode())):
            self.bind()
            shares = SHARES.list_shares()
            users = SHARES.list_users()
        self.assertEqual(["media"], [row["name"] for row in shares])
        self.assertFalse(shares[0]["has_password"])
        self.assertEqual(["ada"], [row["user"] for row in users])

    def test_nfs_does_not_depend_on_the_passwords(self):
        import homestead_share_servers as SERVERS
        keep_bound(self, SERVERS)
        with Kube(config([{"name": "media", "pvc": "media-data", "user": "ada", "nfs_clients": "192.0.2.0/24"}]),
                  secret(raw=base64.b64encode(b"not json").decode())):
            self.bind()
            SERVERS.bind(K.kget, K.send, lambda cfg: {}, "lab", {})
            state = SERVERS.nfs_state()
        self.assertEqual(["media"], state["exports"])

    def test_saving_passwords_keeps_the_secrets_labels_annotations_and_other_keys(self):
        stored = secret({"user:bob": {"user": "bob", "password": "pw-bob"}},
                        labels={"team": "media"}, annotations={"note": "set by hand"})
        stored["data"]["extra"] = encoded({"kept": True})
        with Kube(stored) as kube:
            self.bind()
            SHARES.save_user("ada", "pw-ada")
            saved = kube.object(SECRET)
            credentials = stored_credentials(kube)
        self.assertEqual("pw-ada", credentials["user:ada"]["password"])
        self.assertEqual("pw-bob", credentials["user:bob"]["password"])
        self.assertEqual("media", saved["metadata"]["labels"]["team"])
        self.assertEqual("set by hand", saved["metadata"]["annotations"]["note"])
        self.assertIn("extra", saved["data"])

    def test_saving_share_settings_keeps_the_maps_labels_annotations_and_other_keys(self):
        stored = config([], labels={"team": "media"}, annotations={"note": "set by hand"})
        stored["data"]["notes.txt"] = "kept"
        with Kube(stored) as kube:
            self.bind()
            SHARES._save_config([{"name": "media", "pvc": "media-data", "public": True}], kube.object(MAP))
            saved = kube.object(MAP)
        self.assertEqual(["media"], [row["name"] for row in json.loads(saved["data"]["shares.json"])])
        self.assertEqual("media", saved["metadata"]["labels"]["team"])
        self.assertEqual("set by hand", saved["metadata"]["annotations"]["note"])
        self.assertEqual("kept", saved["data"]["notes.txt"])

    def test_passwords_saved_meanwhile_elsewhere_are_refused_in_words_and_kept(self):
        with Kube(secret({"user:bob": {"user": "bob", "password": "pw-bob"}})) as kube:
            raced = []

            def send(method, path, body=None, **options):
                if method == "PUT" and path == SECRET and not raced:
                    raced.append(1)
                    other = kube.object(SECRET)
                    other["data"]["credentials.json"] = encoded({"user:eve": {"user": "eve", "password": "pw-eve"}})
                    K.send("PUT", SECRET, other)
                return K.send(method, path, body, **options)
            self.bind(send)
            with self.assertRaisesRegex(ValueError, "changed elsewhere"):
                SHARES.save_user("ada", "pw-ada")
            self.assertEqual({"user:eve"}, set(stored_credentials(kube)))


if __name__ == "__main__":
    unittest.main()
