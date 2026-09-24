import base64
import gzip
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_helm as HELM
import homestead_yaml as YAML

MANIFEST = """---
# Source: grafana/templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: grafana
  namespace: monitoring
  labels:
    app: grafana
spec: {}
---
# Source: grafana/templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    app: grafana
  name: grafana
"""


def secret(ns, name, version, status="deployed", chart_version="8.0.0", config=None):
    record = {"name": name, "namespace": ns, "version": version,
              "info": {"status": status, "last_deployed": f"2026-09-2{version}T10:00:00Z", "notes": "Open it on port 3000",
                       "description": "Install complete" if version == 1 else "Upgrade complete"},
              "chart": {"metadata": {"name": "grafana", "version": chart_version, "appVersion": "11.0.0",
                                     "icon": "https://example.com/g.png", "description": "Dashboards"},
                        "values": {"replicas": 1, "persistence": {"enabled": False}}},
              "config": config or {}, "manifest": MANIFEST}
    blob = base64.b64encode(gzip.compress(json.dumps(record).encode())).decode()
    return {"metadata": {"name": f"sh.helm.release.v1.{name}.v{version}", "namespace": ns,
                         "labels": {"owner": "helm", "name": name, "version": str(version), "status": status}},
            "data": {"release": base64.b64encode(blob.encode()).decode()}}


class HelmTests(unittest.TestCase):
    def setUp(self):
        self.secrets = [secret("monitoring", "grafana", 1, "superseded", "7.0.0"),
                        secret("monitoring", "grafana", 2, config={"adminUser": "me", "persistence": {"enabled": True, "size": "10Gi"},
                                                                   "hosts": ["a.lan", "b.lan"], "note": "yes: quoted"}),
                        secret("cattle-system", "rancher", 3)]
        self.charts = [{"metadata": {"name": "grafana", "namespace": "kube-system", "labels": {"homestead.io/managed": "true"}},
                        "spec": {"repo": "https://grafana.github.io/helm-charts", "chart": "grafana", "version": "8.0.0",
                                 "targetNamespace": "monitoring", "valuesContent": "adminUser: me\n"}}]
        self.sent = []

        def get(path):
            if path.startswith("/apis/helm.cattle.io"):
                return {"items": self.charts}
            if "/secrets" in path:
                ns = path.split("/namespaces/")[1].split("/")[0] if "/namespaces/" in path else None
                return {"items": [s for s in self.secrets if ns is None or s["metadata"]["namespace"] == ns]}
            raise AssertionError(path)

        HELM.bind(get, lambda method, path, body=None, **kw: self.sent.append((method, path, body)) or body)

    def test_every_release_is_read_from_helms_own_secrets(self):
        rows = {r["name"]: r for r in HELM.releases()}
        g = rows["grafana"]
        self.assertEqual((2, "deployed", "8.0.0", "11.0.0", "homestead"),
                         (g["revision"], g["status"], g["chart_version"], g["app_version"], g["managed"]))
        self.assertFalse(g["system"])
        self.assertTrue(rows["rancher"]["system"])
        self.assertEqual("", rows["rancher"]["managed"])

    def test_a_release_shows_its_values_history_and_objects(self):
        r = HELM.release("monitoring", "grafana")
        self.assertEqual([2, 1], [h["revision"] for h in r["history"]])
        self.assertEqual([("Service", "grafana", "monitoring"), ("Deployment", "grafana", "")],
                         [(o["kind"], o["name"], o["namespace"]) for o in r["objects"]])
        self.assertEqual("adminUser: me\n", r["source"]["values"])
        # the values read back as the same YAML
        self.assertEqual({"adminUser": "me", "persistence": {"enabled": True, "size": "10Gi"},
                          "hosts": ["a.lan", "b.lan"], "note": "yes: quoted"}, YAML.loads(r["values"]))

    def test_installing_writes_a_helmchart_for_the_controller(self):
        result = HELM.install({"name": "immich", "namespace": "media", "repo": "https://immich-app.github.io/immich-charts",
                               "chart": "immich", "version": "0.9.3", "values": "persistence: {}\n"})
        method, path, body = self.sent[0]
        self.assertEqual(("POST", "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts"), (method, path))
        self.assertEqual({"repo": "https://immich-app.github.io/immich-charts", "chart": "immich", "targetNamespace": "media",
                          "createNamespace": True, "valuesContent": "persistence: {}\n", "version": "0.9.3"}, body["spec"])
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        self.assertIn("immich", result["detail"])

    def test_an_oci_chart_names_its_address(self):
        HELM.install({"name": "podinfo", "namespace": "lab", "repo": "oci://ghcr.io/stefanprodan/charts", "chart": "podinfo"})
        spec = self.sent[0][2]["spec"]
        self.assertEqual("oci://ghcr.io/stefanprodan/charts/podinfo", spec["chart"])
        self.assertNotIn("repo", spec)

    def test_bad_installs_are_refused(self):
        for cfg in ({"name": "Bad Name", "namespace": "lab", "repo": "https://x", "chart": "c"},
                    {"name": "ok", "namespace": "lab", "repo": "ftp://x", "chart": "c"},
                    {"name": "ok", "namespace": "lab", "repo": "https://x", "chart": "c; rm -rf"},
                    {"name": "grafana", "namespace": "monitoring", "repo": "https://x", "chart": "grafana"}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                HELM.install(cfg)
        self.assertEqual([], self.sent)

    def test_only_helmchart_releases_are_changed(self):
        HELM.upgrade({"namespace": "monitoring", "name": "grafana", "version": "8.1.0", "values": "adminUser: you\n"})
        method, path, body = self.sent[0]
        self.assertEqual(("PUT", "8.1.0", "adminUser: you\n"), (method, body["spec"]["version"], body["spec"]["valuesContent"]))
        with self.assertRaisesRegex(ValueError, "leaves it alone"):
            HELM.upgrade({"namespace": "cattle-system", "name": "rancher", "version": "9"})
        HELM.uninstall("monitoring", "grafana")
        self.assertEqual("DELETE", self.sent[-1][0])

    def test_search_reads_artifact_hub(self):
        HELM.bind(HELM.kget, HELM.ksend, lambda url, raw=False: {"packages": [
            {"name": "grafana", "version": "8.0.0", "app_version": "11", "description": "d", "package_id": "p1",
             "logo_image_id": "img", "repository": {"url": "https://grafana.github.io/helm-charts", "name": "grafana",
                                                    "verified_publisher": True, "display_name": "Grafana"}}]})
        HELM._cache.clear()
        rows = HELM.search("grafana")
        self.assertEqual(("grafana", "https://artifacthub.io/image/img", True), (rows[0]["repo_name"], rows[0]["logo"], rows[0]["verified"]))
        self.assertEqual([], HELM.search("g"))


if __name__ == "__main__":
    unittest.main()
