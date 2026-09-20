import base64
import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import harvui_shares as shares


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/api/v1/namespaces/lab/configmaps/harvui-shares": {
                "metadata": {"name": "harvui-shares", "resourceVersion": "4"},
                "data": {"shares.json": json.dumps([{
                    "name": "secure", "pvc": "share-secure", "path": "/shares/secure",
                    "size_gb": 5, "user": "lab", "password": "legacy-password",
                    "public": False, "created": "existing",
                }])},
            },
            "/api/v1/namespaces/lab/persistentvolumeclaims/share-secure": {
                "metadata": {"name": "share-secure", "resourceVersion": "9"},
                "spec": {"resources": {"requests": {"storage": "10Gi"}}},
                "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}},
            },
            "/apis/apps/v1/namespaces/lab/deployments/samba": {
                "metadata": {"name": "samba", "namespace": "lab", "resourceVersion": "12"},
                "spec": {"replicas": 1, "template": {"metadata": {}, "spec": {
                    "containers": [{"name": "samba", "args": [
                        "-p", "-s", "secure;/shares/secure;yes;no;no;lab",
                        "-u", "lab;legacy-password",
                    ], "volumeMounts": [{"name": "sh0", "mountPath": "/shares/secure"}]}],
                    "volumes": [{"name": "sh0", "persistentVolumeClaim": {
                        "claimName": "share-secure"}}],
                }}},
            },
        }
        self.sent = []

        def get(path):
            if path not in self.objects:
                from urllib.error import HTTPError
                raise HTTPError(path, 404, "missing", {}, None)
            return copy.deepcopy(self.objects[path])

        def send(method, path, body, **_kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            if method == "POST":
                plural = "configmaps" if body["kind"] == "ConfigMap" else "secrets"
                path = f"/api/v1/namespaces/lab/{plural}/{body['metadata']['name']}"
            self.objects[path] = copy.deepcopy(body)
            return copy.deepcopy(body)

        def create_pvc(namespace, name, size):
            self.objects[f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}"] = {
                "metadata": {"name": name, "resourceVersion": "1"},
                "spec": {"resources": {"requests": {"storage": f"{size}Gi"}}},
                "status": {"phase": "Pending"},
            }
            return self.objects[f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}"]

        shares.bind(get, send, create_pvc, "lab", {})

    def decoded_secret(self):
        obj = self.objects["/api/v1/namespaces/lab/secrets/harvui-share-credentials"]
        return json.loads(base64.b64decode(obj["data"]["credentials.json"]).decode())

    def test_inventory_uses_live_pvc_size_and_never_returns_password(self):
        result = shares.list_shares()
        self.assertEqual(10, result[0]["size_gb"])
        self.assertEqual(10, result[0]["actual_size_gb"])
        self.assertTrue(result[0]["has_password"])
        self.assertNotIn("password", result[0])
        self.assertEqual([], self.sent, "GET inventory must remain read-only")

    def test_edit_grows_claim_migrates_secret_and_applies_read_only_access(self):
        result = shares.edit_share("secure", 20, "media", "new-password", False, True)
        self.assertTrue(result["deployment_updated"])
        pvc = self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/share-secure"]
        self.assertEqual("20Gi", pvc["spec"]["resources"]["requests"]["storage"])
        stored = json.loads(self.objects[
            "/api/v1/namespaces/lab/configmaps/harvui-shares"]["data"]["shares.json"])
        self.assertNotIn("password", stored[0])
        self.assertEqual({"user": "media", "password": "new-password"},
                         self.decoded_secret()["secure"])
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/samba"]
        container = dep["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("secure;/shares/secure;yes;yes;no;media", container["args"])
        self.assertIn("media;new-password", container["args"])
        self.assertTrue(container["volumeMounts"][0]["readOnly"])
        self.assertNotIn("password", result["shares"][0])

    def test_blank_password_preserves_existing_private_password(self):
        shares.edit_share("secure", 10, "lab", "", False, False)
        self.assertEqual("legacy-password", self.decoded_secret()["secure"]["password"])

    def test_guest_access_removes_stored_credential(self):
        result = shares.edit_share("secure", 10, "lab", "", True, False)
        self.assertEqual({}, self.decoded_secret())
        self.assertFalse(result["shares"][0]["has_password"])
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/samba"]
        args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("secure;/shares/secure;yes;no;yes;lab", args)
        self.assertNotIn("-u", args)

    def test_claim_cannot_shrink(self):
        with self.assertRaisesRegex(ValueError, "cannot shrink"):
            shares.edit_share("secure", 9, "lab", "", False, False)
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
