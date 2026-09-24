import copy
import io
import json
import re
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_resources as RES
import homestead_yaml as YAML


class YamlDumpTests(unittest.TestCase):
    def test_every_document_in_the_manifest_round_trips(self):
        text = (ROOT / "deploy" / "deploy.yaml").read_text(encoding="utf-8")
        docs = [d for d in re.split(r"^---\s*$", text, flags=re.M) if d.strip()]
        self.assertGreater(len(docs), 5)
        for doc in docs:
            data = json.loads(json.dumps(YAML.loads(doc)))
            self.assertEqual(data, json.loads(json.dumps(YAML.loads(YAML.dump(data)))))

    def test_awkward_strings_keep_their_exact_value(self):
        data = {"a": "  leading", "b": "x\n\n", "c": "yes", "d": "1.20", "e": "k: v", "f": "#", "g": "line\nline\n",
                "h": [["nested", 1]], "i": "", "j": "-", "k": "multi\n  indented\nend"}
        self.assertEqual(data, json.loads(json.dumps(YAML.loads(YAML.dump(data)))))


class Cluster:
    def __init__(self):
        self.cm = {"apiVersion": "v1", "kind": "ConfigMap",
                   "metadata": {"name": "cfg", "namespace": "lab", "resourceVersion": "5", "uid": "u1",
                                "managedFields": [{"manager": "kubectl"}],
                                "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"}},
                   "data": {"app.yaml": "port: 80\n"}}
        self.secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "pw", "namespace": "lab", "resourceVersion": "2"},
                       "data": {"password": "aHVudGVyMg=="}}
        self.sent = []

    def get(self, path):
        if path == "/api/v1":
            return {"resources": [{"name": "configmaps", "kind": "ConfigMap", "namespaced": True, "verbs": ["get", "list", "update", "create", "delete"]},
                                  {"name": "secrets", "kind": "Secret", "namespaced": True, "verbs": ["get", "list"]},
                                  {"name": "pods/log", "kind": "Pod", "namespaced": True, "verbs": ["get"]},
                                  {"name": "nodes", "kind": "Node", "namespaced": False, "verbs": ["get", "list"]}]}
        if path == "/apis":
            return {"groups": [{"name": "longhorn.io", "preferredVersion": {"version": "v1beta2"}},
                               {"name": "broken.example", "preferredVersion": {"version": "v1"}}]}
        if path == "/apis/longhorn.io/v1beta2":
            return {"resources": [{"name": "volumes", "kind": "Volume", "namespaced": True, "verbs": ["get", "list"]}]}
        if path == "/apis/broken.example/v1":
            raise urllib.error.HTTPError(path, 503, "down", None, None)
        if path.endswith("/configmaps/cfg"):
            return copy.deepcopy(self.cm)
        if path.endswith("/secrets/pw"):
            return copy.deepcopy(self.secret)
        if "/events" in path:
            return {"items": [{"type": "Warning", "reason": "Failed", "message": "x", "lastTimestamp": "2026-09-24T10:00:00Z"}]}
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if method == "PUT" and body["metadata"].get("resourceVersion") != "5":
            raise urllib.error.HTTPError(path, 409, "conflict", None, io.BytesIO(b'{"message":"the object has been modified"}'))
        return body

    def table(self, path):
        return {"columnDefinitions": [{"name": "Name", "priority": 0}, {"name": "Data", "priority": 0}, {"name": "Extra", "priority": 1},
                                      {"name": "Age", "priority": 0}],
                "rows": [{"cells": ["cfg", 1, "hidden", "5d"], "object": {"metadata": {"name": "cfg", "namespace": "lab"}}}]}


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster()
        RES.bind(self.c.get, self.c.send, self.c.table)
        RES._discovery.update(at=0.0, value=None)

    def test_every_listable_kind_is_found_and_grouped(self):
        kinds = {(k["group"], k["resource"]): k for k in RES.discover()}
        self.assertEqual({("", "configmaps"), ("", "secrets"), ("", "nodes"), ("longhorn.io", "volumes")}, set(kinds))
        self.assertEqual("Configuration", kinds[("", "configmaps")]["category"])
        self.assertEqual("Custom resources", kinds[("longhorn.io", "volumes")]["category"])
        self.assertFalse(kinds[("", "nodes")]["namespaced"])

    def test_lists_carry_the_servers_own_columns(self):
        listing = RES.list_objects("", "v1", "configmaps", "lab")
        self.assertEqual(["Name", "Data", "Age"], [c["name"] for c in listing["columns"]])
        self.assertEqual(["cfg", 1, "5d"], listing["rows"][0]["cells"])

    def test_an_object_reads_without_bookkeeping_and_saves_back(self):
        o = RES.get_object("", "v1", "configmaps", "lab", "cfg")
        self.assertNotIn("managedFields", o["yaml"])
        self.assertNotIn("last-applied", o["yaml"])
        edited = o["yaml"].replace("port: 80", "port: 8080")
        RES.save_object("", "v1", "configmaps", "lab", "cfg", edited)
        method, path, body = self.c.sent[-1]
        self.assertEqual(("PUT", "/api/v1/namespaces/lab/configmaps/cfg"), (method, path))
        self.assertEqual("port: 8080\n", body["data"]["app.yaml"])

    def test_a_stale_edit_is_refused_with_the_servers_reason(self):
        o = RES.get_object("", "v1", "configmaps", "lab", "cfg")
        with self.assertRaisesRegex(ValueError, "has been modified"):
            RES.save_object("", "v1", "configmaps", "lab", "cfg", o["yaml"].replace('resourceVersion: "5"', 'resourceVersion: "4"'))
        with self.assertRaisesRegex(ValueError, "cannot change"):
            RES.save_object("", "v1", "configmaps", "lab", "cfg", o["yaml"].replace("name: cfg", "name: other"))

    def test_secrets_stay_hidden_unless_revealed(self):
        hidden = RES.get_object("", "v1", "secrets", "lab", "pw")
        self.assertNotIn("aHVudGVyMg==", hidden["yaml"])
        self.assertTrue(hidden["secret_hidden"])
        self.assertIn("aHVudGVyMg==", RES.get_object("", "v1", "secrets", "lab", "pw", reveal=True)["yaml"])
        with self.assertRaisesRegex(ValueError, "reveal"):
            RES.save_object("", "v1", "secrets", "lab", "pw", hidden["yaml"].replace('resourceVersion: "2"', 'resourceVersion: "5"'))

    def test_creating_several_objects_from_yaml(self):
        result = RES.create_objects("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n---\n"
                                    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n  namespace: other\n", "lab")
        self.assertEqual(["ConfigMap a", "ConfigMap b"], result["created"])
        self.assertEqual(["/api/v1/namespaces/lab/configmaps", "/api/v1/namespaces/other/configmaps"], [s[1] for s in self.c.sent])
        with self.assertRaisesRegex(ValueError, "serves no Widget"):
            RES.create_objects("apiVersion: example.com/v1\nkind: Widget\nmetadata:\n  name: w\n")

    def test_events_for_an_object(self):
        self.assertEqual("Failed", RES.events_for("lab", "cfg", "u1")[0]["reason"])


if __name__ == "__main__":
    unittest.main()
