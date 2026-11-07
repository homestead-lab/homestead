"""Homestead's configuration backed up to a sealed file, and restored part by part."""
import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_config_backup as config
import homestead_kube as K
from fakes.kube import Kube
from rebind import keep_bound


KINDS = {"configmaps": "ConfigMap", "secrets": "Secret"}


def path(kind, name, namespace="lab"):
    return f"/api/v1/namespaces/{namespace}/{kind}/{name}"


PARTS = [
    {"id": "settings", "label": "Settings", "objects": [("configmaps", "lab", "homestead-settings")]},
    {"id": "users", "label": "Users and roles", "default": False, "caution": "signs everyone out",
     "objects": [("secrets", "lab", "homestead-auth")]},
    {"id": "ipam", "label": "IP addresses",
     "objects": [("configmaps", "lab", "homestead-ipam"), ("secrets", "lab", "homestead-unifi")]},
    {"id": "portal", "label": "Portal", "objects": [("configmaps", "lab", "homestead-portal")]},
]
SECRET = base64.b64encode(b'{"users":{"robin":{"hash":"x"}}}').decode()


class ConfigBackupTests(unittest.TestCase):
    """Runs on the shared fake API (tests/fakes/kube.py)."""
    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.restored = []
        keep_bound(self, config)
        config.bind(K.kget, K.ksend, "2.8.212", PARTS, site=lambda: "Loft rack",
                    after_restore=self.restored.extend)
        self.put("configmaps", "homestead-settings", {"settings.json": '{"site_name":"Loft rack"}'})
        self.put("secrets", "homestead-auth", {"store.json": SECRET})
        self.put("configmaps", "homestead-ipam", {"ipam.json": '{"subnets":[1]}'})
        self.put("secrets", "homestead-unifi", {"key": base64.b64encode(b"unifi-key").decode()})

    def put(self, kind, name, data, namespace="lab"):
        """Someone (not the restore) leaves the object as it is now."""
        self.kube.add({"apiVersion": "v1", "kind": KINDS[kind],
                       "metadata": {"name": name, "namespace": namespace, "labels": {"homestead.io/managed": "true"}},
                       "data": data})

    def data(self, kind, name, namespace="lab"):
        return self.kube.object(path(kind, name, namespace))["data"]

    def test_the_file_says_what_it_holds_but_not_the_contents(self):
        doc = config.backup(None, "correct horse")
        text = json.dumps(doc)
        self.assertEqual("Loft rack", doc["site"])
        self.assertEqual(["settings", "users", "ipam", "portal"], [p["id"] for p in doc["parts"]])
        for secret in ("Loft rack\\\"", "unifi-key", SECRET, "subnets", "store.json"):
            self.assertNotIn(secret, text.replace('"site": "Loft rack"', ""))

    def test_a_short_passphrase_is_refused(self):
        with self.assertRaises(ValueError):
            config.backup(None, "short")

    def test_a_wrong_passphrase_is_refused(self):
        doc = config.backup(None, "correct horse")
        with self.assertRaises(PermissionError):
            config.inspect(doc, "wrong horse")

    def test_a_changed_file_is_refused(self):
        doc = config.backup(None, "correct horse")
        doc["site"] = "Somewhere else"
        with self.assertRaises(PermissionError):
            config.inspect(doc, "correct horse")
        doc = config.backup(None, "correct horse")
        data = bytearray(base64.b64decode(doc["data"]))
        data[0] ^= 1
        doc["data"] = base64.b64encode(bytes(data)).decode()
        with self.assertRaises(PermissionError):
            config.inspect(doc, "correct horse")

    def test_inspect_compares_each_part_with_now(self):
        doc = config.backup(None, "correct horse")
        self.put("configmaps", "homestead-ipam", {"ipam.json": '{"subnets":[1,2]}'})
        rows = {r["id"]: r for r in config.inspect(doc, "correct horse")["parts"]}
        self.assertEqual("same", rows["settings"]["state"])
        self.assertEqual("differs", rows["ipam"]["state"])
        self.assertEqual("empty", rows["portal"]["state"])
        self.assertFalse(rows["portal"]["restorable"])
        self.assertFalse(rows["users"]["default"])
        self.assertEqual("signs everyone out", rows["users"]["caution"])

    def test_only_the_chosen_parts_come_back(self):
        doc = config.backup(None, "correct horse")
        self.put("configmaps", "homestead-ipam", {"ipam.json": '{"subnets":[]}'})
        self.put("secrets", "homestead-unifi", {"key": base64.b64encode(b"new-key").decode()})
        self.put("configmaps", "homestead-settings", {"settings.json": '{"site_name":"Changed"}'})
        now = self.kube.object(path("configmaps", "homestead-ipam"))["metadata"]["resourceVersion"]
        result = config.restore(doc, "correct horse", ["ipam"])
        self.assertEqual(["ipam"], result["restored"])
        self.assertEqual('{"subnets":[1]}', self.data("configmaps", "homestead-ipam")["ipam.json"])
        self.assertEqual(base64.b64encode(b"unifi-key").decode(), self.data("secrets", "homestead-unifi")["key"])
        self.assertEqual('{"site_name":"Changed"}', self.data("configmaps", "homestead-settings")["settings.json"])
        self.assertEqual(["ipam"], self.restored)
        # Its labels stay, and the update is made against the object as it is now.
        kept = self.kube.object(path("configmaps", "homestead-ipam"))["metadata"]
        self.assertEqual({"homestead.io/managed": "true"}, kept["labels"])
        put = [w for w in self.kube.writes if w[:2] == ("PUT", path("configmaps", "homestead-ipam"))]
        self.assertEqual(1, len(put))
        self.assertEqual(now, put[0][2]["metadata"].get("resourceVersion"), "the update names the version it read")

    def test_a_part_missing_here_is_created(self):
        doc = config.backup(["settings"], "correct horse")
        self.kube("DELETE", path("configmaps", "homestead-settings"))
        config.restore(doc, "correct horse", ["settings"])
        self.assertEqual('{"site_name":"Loft rack"}', self.data("configmaps", "homestead-settings")["settings.json"])

    def test_a_part_the_backup_did_not_hold_changes_nothing(self):
        doc = config.backup(None, "correct horse")
        self.put("configmaps", "homestead-portal", {"portal.json": "{}"})
        result = config.restore(doc, "correct horse", ["portal"])
        self.assertEqual([], result["restored"])
        self.assertEqual(["Portal"], result["skipped"])
        self.assertEqual({"portal.json": "{}"}, self.data("configmaps", "homestead-portal"))

    def test_nothing_chosen_is_refused(self):
        doc = config.backup(None, "correct horse")
        with self.assertRaises(ValueError):
            config.restore(doc, "correct horse", [])

    def test_another_file_is_not_mistaken_for_a_backup(self):
        with self.assertRaises(ValueError):
            config.inspect({"hello": "world"}, "correct horse")


if __name__ == "__main__":
    unittest.main()
