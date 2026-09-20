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
        self.created = []

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

        def create_pvc(namespace, name, size, storage_class=None, access_mode="ReadWriteOnce"):
            self.created.append((name, size, storage_class, access_mode))
            self.objects[f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}"] = {
                "metadata": {"name": name, "resourceVersion": "1"},
                "spec": {"accessModes": [access_mode], "storageClassName": storage_class,
                         "resources": {"requests": {"storage": f"{size}Gi"}}},
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

    def test_share_can_publish_a_folder_of_an_existing_volume(self):
        result = shares.create_share("clips", 0, "clips", "pw", False,
                                     pvc="share-secure", sub_path="/cameras/front")

        self.assertEqual([], self.created, "an existing volume must not be recreated")
        row = next(item for item in result["shares"] if item["name"] == "clips")
        self.assertFalse(row["owned"])
        self.assertEqual(10, row["size_gb"], "size comes from the borrowed claim")
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/samba"]
        container = dep["spec"]["template"]["spec"]["containers"][0]
        mount = next(m for m in container["volumeMounts"] if m["mountPath"] == "/shares/clips")
        self.assertEqual("cameras/front", mount["subPath"],
                         "a typed leading slash is normalised, not escaped")
        claims = [v for v in dep["spec"]["template"]["spec"]["volumes"]
                  if (v.get("persistentVolumeClaim") or {}).get("claimName") == "share-secure"]
        self.assertEqual(1, len(claims), "one volume entry serves both shares of a claim")
        self.assertEqual(claims[0]["name"], mount["name"])

    def test_new_share_creates_its_own_claim_with_the_chosen_class(self):
        shares.create_share("media", 50, "media", "pw", False,
                            storage_class="longhorn-r2", access_mode="ReadWriteMany")

        self.assertEqual([("share-media", 50, "longhorn-r2", "ReadWriteMany")], self.created)

    def test_borrowed_volume_is_never_resized_by_a_share_edit(self):
        shares.create_share("clips", 0, "clips", "pw", False, pvc="share-secure")
        self.sent.clear()

        shares.edit_share("clips", 500, "clips", "", False, True)

        pvc = self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/share-secure"]
        self.assertEqual("10Gi", pvc["spec"]["resources"]["requests"]["storage"])
        self.assertFalse(any("persistentvolumeclaims" in path for _, path, _ in self.sent))

    def test_a_migratable_volume_is_warned_about_but_still_allowed(self):
        """Other pods do mount these, so the rollout guard decides, not a blanket ban."""
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/vmdisk"] = {
            "metadata": {"name": "vmdisk"}, "spec": {"volumeName": "pvc-abc"},
            "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-abc"] = {
            "metadata": {"name": "pvc-abc"},
            "spec": {"migratable": True, "numberOfControllers": 2, "accessMode": "rwx"}}

        result = shares.create_share("vm", 0, "vm", "pw", False, pvc="vmdisk")

        self.assertIn("live-migratable", result["warnings"][0])
        self.assertIn("rolled back", result["warnings"][0])
        self.assertTrue(any("deployments/samba" in path for _, path, _ in self.sent))

    def test_an_unbound_volume_is_refused(self):
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/pending"] = {
            "metadata": {"name": "pending"}, "spec": {}, "status": {"phase": "Pending"}}

        with self.assertRaisesRegex(ValueError, "pending"):
            shares.create_share("later", 0, "later", "pw", False, pvc="pending")
        self.assertEqual([], self.sent)

    def test_a_share_that_cannot_start_restores_the_working_shares(self):
        """Samba uses Recreate, so a bad mount must never outlive the attempt."""
        ready, timeout = shares._samba_ready, shares.ROLLOUT_TIMEOUT
        shares._samba_ready = lambda: True   # serving before the change
        shares.ROLLOUT_TIMEOUT = 0           # and never ready after it
        try:
            with self.assertRaisesRegex(ValueError, "previous shares were restored"):
                shares.create_share("clips", 0, "clips", "pw", False, pvc="share-secure")
        finally:
            shares._samba_ready, shares.ROLLOUT_TIMEOUT = ready, timeout

        method, path, body = self.sent[-1]
        self.assertEqual("PUT", method)
        self.assertTrue(path.endswith("/deployments/samba"))
        args = body["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertNotIn("clips;/shares/clips;yes;no;no;clips", args)
        self.assertIn("secure;/shares/secure;yes;no;no;lab", args)

    def test_a_broken_samba_can_still_be_repaired(self):
        """With Samba already down, a change must apply instead of rolling back."""
        ready, timeout = shares._samba_ready, shares.ROLLOUT_TIMEOUT
        shares._samba_ready = lambda: False
        shares.ROLLOUT_TIMEOUT = 0
        try:
            shares.delete_share("secure")
        finally:
            shares._samba_ready, shares.ROLLOUT_TIMEOUT = ready, timeout

        args = self.sent[-1][2]["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertNotIn("secure;/shares/secure;yes;no;no;lab", args)

    def test_folder_cannot_escape_the_volume(self):
        for folder in ("../etc", "media/../../etc", "media/../secrets"):
            with self.assertRaisesRegex(ValueError, "relative path inside the volume"):
                shares.create_share("escape", 0, "escape", "pw", False,
                                    pvc="share-secure", sub_path=folder)
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
