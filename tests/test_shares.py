import base64
import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_shares as shares


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/api/v1/namespaces/lab/configmaps/homestead-shares": {
                "metadata": {"name": "homestead-shares", "resourceVersion": "4"},
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
            "/apis/apps/v1/namespaces/lab/deployments/homestead-smb": {
                "metadata": {"name": "homestead-smb", "namespace": "lab", "resourceVersion": "12"},
                "spec": {"replicas": 1, "template": {"metadata": {}, "spec": {
                    "containers": [{"name": "homestead-smb", "args": [
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

        def send(method, path, body=None, **_kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            if method == "POST":
                plural = "configmaps" if body["kind"] == "ConfigMap" else "secrets"
                path = f"/api/v1/namespaces/lab/{plural}/{body['metadata']['name']}"
            if method == "DELETE":
                self.objects.pop(path, None)
                return {}
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
        obj = self.objects["/api/v1/namespaces/lab/secrets/homestead-share-credentials"]
        return json.loads(base64.b64decode(obj["data"]["credentials.json"]).decode())

    def test_removed_share_clears_every_smb_mapping_but_never_deletes_pvc(self):
        shares.CACHE.update(wl=[1, {}], **{"node:host": [1, {}]})
        result = shares.delete_share("secure")
        spec = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]["spec"]["template"]["spec"]
        self.assertEqual([], spec["volumes"])
        self.assertEqual([], spec["containers"][0]["volumeMounts"])
        self.assertEqual(["share-secure"], result["removed_claims"])
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/share-secure", self.objects)
        self.assertFalse(any(method == "DELETE" and "persistentvolume" in path for method, path, _ in self.sent))
        self.assertNotIn("wl", shares.CACHE)
        self.assertNotIn("node:host", shares.CACHE)

    def test_second_share_on_same_claim_keeps_mount_with_explanation(self):
        shares.create_share("other", 0, "lab", "", False, pvc="share-secure", sub_path="other")
        result = shares.delete_share("secure")
        self.assertEqual([], result["removed_claims"])
        self.assertEqual(["other"], result["remaining_shares"])
        self.assertIn("remains mounted for other", result["message"])
        spec = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]["spec"]["template"]["spec"]
        self.assertEqual(1, len(spec["volumes"]))
        self.assertEqual("/shares/other", spec["containers"][0]["volumeMounts"][0]["mountPath"])

    def test_repeat_remove_repairs_stale_deployment_when_config_already_empty(self):
        self.objects["/api/v1/namespaces/lab/configmaps/homestead-shares"]["data"]["shares.json"] = "[]"
        result = shares.delete_share("secure")
        self.assertIsNotNone(result["deployment"])
        self.assertEqual(["share-secure"], result["removed_claims"])
        spec = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]["spec"]["template"]["spec"]
        self.assertEqual([], spec["volumes"])

    def test_removal_job_waits_for_old_pods_even_when_new_deployment_is_ready(self):
        import time
        from unittest.mock import patch
        shares.delete_share("secure")
        pods_path = "/api/v1/namespaces/lab/pods?labelSelector=app%3Dhomestead-smb"
        self.objects[pods_path] = {"items": [{"metadata": {"name": "old", "deletionTimestamp": "now"},
            "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "share-secure"}}]}}]}
        item = {"ref": {"since": time.time(), "removed_claims": ["share-secure"]}}
        with patch.object(shares, "_samba_ready", return_value=True):
            self.assertEqual(("running", 45), shares.removal_progress(item)[:2])
            self.objects[pods_path] = {"items": []}
            self.assertEqual("succeeded", shares.removal_progress(item)[0])
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/share-secure", self.objects)

    def test_a_new_volume_is_created_under_the_name_given(self):
        shares.create_share("nas", 50, "lab", "a-password", False, new_name="nas-data",
                            storage_class="longhorn-r2")
        self.assertEqual(("nas-data", 50, "longhorn-r2"), self.created[-1][:3])

    def test_the_first_share_installs_samba(self):
        dep = self.objects.pop("/apis/apps/v1/namespaces/lab/deployments/homestead-smb")
        installed = []

        def install(address=""):
            installed.append(address)
            fresh = copy.deepcopy(dep)
            fresh["spec"]["template"]["spec"]["containers"][0].update(args=["-p"], volumeMounts=[])
            fresh["spec"]["template"]["spec"]["volumes"] = []
            self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"] = fresh
            return copy.deepcopy(fresh)
        shares.install = install
        try:
            shares.create_share("media", 10, "lab", "a-password", False, samba_ip="192.0.2.245")
        finally:
            shares.install = None
        self.assertEqual(["192.0.2.245"], installed)
        args = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("media;/shares/media;yes;no;no;lab", args)

    def test_deleted_share_is_not_resurrected_and_can_be_readded(self):
        shares.delete_share("secure")
        configured = json.loads(self.objects[
            "/api/v1/namespaces/lab/configmaps/homestead-shares"]["data"]["shares.json"])
        self.assertEqual([], configured)
        self.assertEqual([], shares.list_shares())
        shares.create_share("secure", 0, "lab", "new-password", False, pvc="share-secure")
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]
        mounts = dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        self.assertEqual(["/shares/secure"], [mount["mountPath"] for mount in mounts])

    def test_manual_duplicate_mount_is_removed_on_create(self):
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]
        spec = dep["spec"]["template"]["spec"]
        spec["containers"][0]["volumeMounts"].append(
            {"name": "manual", "mountPath": "/shares/plex-media"})
        spec["volumes"].append({"name": "manual", "persistentVolumeClaim": {
            "claimName": "share-secure"}})
        shares.create_share("plex-media", 0, "lab", "pw", False, pvc="share-secure")
        # Read the saved Deployment, not the old in-memory fixture object.
        saved = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]
        paths = [mount["mountPath"] for mount in saved["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]]
        self.assertEqual(1, paths.count("/shares/plex-media"))
        self.assertNotIn("manual", [mount["name"] for mount in
                                   saved["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]])

    def test_rejected_mount_update_restores_saved_share_state(self):
        before = copy.deepcopy(self.objects[
            "/api/v1/namespaces/lab/configmaps/homestead-shares"])
        original_send = shares.ksend
        rejected = [False]

        def send(method, path, body=None, **kwargs):
            if (method == "PUT" and path.endswith("/deployments/homestead-smb")
                    and not rejected[0]):
                rejected[0] = True
                from urllib.error import HTTPError
                raise HTTPError(path, 422, "duplicate mountPath", {}, None)
            return original_send(method, path, body, **kwargs)

        shares.ksend = send
        try:
            with self.assertRaisesRegex(Exception, "duplicate mountPath"):
                shares.create_share("plex-media", 0, "lab", "pw", False, pvc="share-secure")
        finally:
            shares.ksend = original_send
        self.assertEqual(before["data"], self.objects[
            "/api/v1/namespaces/lab/configmaps/homestead-shares"]["data"])
        self.assertEqual(["secure"], [row["name"] for row in shares.list_shares()])
        shares.create_share("plex-media", 0, "lab", "pw", False, pvc="share-secure")
        self.assertEqual(["plex-media", "secure"], [row["name"] for row in shares.list_shares()])

    def test_rollback_does_not_overwrite_a_newer_config(self):
        path = "/api/v1/namespaces/lab/configmaps/homestead-shares"
        previous = copy.deepcopy(self.objects[path])
        written = copy.deepcopy(previous)
        self.objects[path]["metadata"]["resourceVersion"] = "5"
        self.objects[path]["data"]["shares.json"] = "[]"
        with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
            shares._restore_object(path, previous, written)
        self.assertEqual("[]", self.objects[path]["data"]["shares.json"])

    def test_samba_that_cannot_be_installed_leaves_no_share_behind(self):
        self.objects.pop("/apis/apps/v1/namespaces/lab/deployments/homestead-smb")

        def install(address=""):
            raise ValueError("there is no free address to give it")
        shares.install = install
        try:
            with self.assertRaisesRegex(ValueError, "no free address"):
                shares.create_share("media", 10, "lab", "a-password", False)
        finally:
            shares.install = None
        self.assertEqual([], self.created, "no volume was made")
        self.assertEqual([], [p for m, p, b in self.sent if "configmaps" in p or "secrets" in p])

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
            "/api/v1/namespaces/lab/configmaps/homestead-shares"]["data"]["shares.json"])
        self.assertNotIn("password", stored[0])
        self.assertEqual({"user": "media", "password": "new-password"},
                         self.decoded_secret()["secure"])
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]
        container = dep["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("secure;/shares/secure;yes;yes;no;media", container["args"])
        self.assertIn("media;new-password", container["args"])
        self.assertTrue(container["volumeMounts"][0]["readOnly"])
        self.assertNotIn("password", result["shares"][0])

    def test_blank_password_preserves_existing_private_password(self):
        shares.edit_share("secure", 10, "lab", "", False, False)
        self.assertEqual("legacy-password", self.decoded_secret()["secure"]["password"])

    def test_guest_access_removes_share_credential_but_retains_reusable_user(self):
        result = shares.edit_share("secure", 10, "lab", "", True, False)
        self.assertEqual({"user:lab": {"user": "lab", "password": "legacy-password"}}, self.decoded_secret())
        self.assertFalse(result["shares"][0]["has_password"])
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]
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
        dep = self.objects["/apis/apps/v1/namespaces/lab/deployments/homestead-smb"]
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

    def _volume(self, claim, **status):
        """A Bound claim backed by a Longhorn volume in the given state."""
        self.objects[f"/api/v1/namespaces/lab/persistentvolumeclaims/{claim}"] = {
            "metadata": {"name": claim},
            "spec": {"volumeName": f"pvc-{claim}", "accessModes": status.pop("modes", ["ReadWriteMany"])},
            "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}
        self.objects[f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-{claim}"] = {
            "metadata": {"name": f"pvc-{claim}"},
            "spec": {"migratable": status.pop("migratable", False)},
            "status": {"currentNodeID": status.pop("node", ""),
                       "robustness": status.pop("robustness", "healthy")}}
        self.objects["/api/v1/namespaces/lab/pods?labelSelector=app%3Dhomestead-smb"] = {
            "items": [{"spec": {"nodeName": "harvester-node2"}}]}

    def test_a_migratable_volume_attached_elsewhere_warns_about_live_migration(self):
        """The case that took Samba down: second node, migratable class."""
        self._volume("plexmedia", migratable=True, node="harvester-node1")

        result = shares.create_share("plex", 0, "plex", "pw", False, pvc="plexmedia")

        warning = result["warnings"][0]
        self.assertIn("harvester-node1", warning)
        self.assertIn("live-migrate", warning)
        self.assertIn("harvester-node2", warning)
        self.assertIn("rolled back", warning)
        self.assertTrue(any("deployments/homestead-smb" in path for _, path, _ in self.sent))

    def test_a_migratable_volume_nobody_holds_is_not_nagged_about(self):
        self._volume("spare", migratable=True, node="")

        self.assertEqual([], shares.create_share("spare", 0, "spare", "pw", False,
                                                 pvc="spare")["warnings"])

    def test_a_volume_attached_on_samba_s_own_node_is_fine(self):
        self._volume("local", migratable=True, node="harvester-node2")

        self.assertEqual([], shares.create_share("local", 0, "local", "pw", False,
                                                 pvc="local")["warnings"])

    def test_rwo_attached_elsewhere_and_degraded_replicas_are_both_called_out(self):
        self._volume("appdata", modes=["ReadWriteOnce"], node="harvester-node1",
                     robustness="degraded")

        warnings = shares.create_share("app", 0, "app", "pw", False, pvc="appdata")["warnings"]

        self.assertIn("ReadWriteOnce", warnings[0])
        self.assertIn("harvester-node1", warnings[0])
        self.assertIn("degraded", warnings[1])

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

        method, path, body = next((sent for sent in reversed(self.sent)
                                   if "/deployments/homestead-smb" in sent[1]))
        self.assertEqual("PUT", method)
        self.assertTrue(path.endswith("/deployments/homestead-smb"))
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

    def test_a_second_share_reuses_the_account_password(self):
        """You cannot be asked to retype a password Homestead never shows you."""
        result = shares.create_share("archive", 0, "lab", "", False, pvc="share-secure")

        self.assertIn("existing lab password", result["message"])
        self.assertEqual("legacy-password", self.decoded_secret()["archive"]["password"])

    def test_a_new_password_moves_the_whole_account(self):
        result = shares.create_share("archive", 0, "lab", "fresh-password", False,
                                     pvc="share-secure")

        secret = self.decoded_secret()
        self.assertEqual("fresh-password", secret["archive"]["password"])
        self.assertEqual("fresh-password", secret["secure"]["password"],
                         "Samba keeps one password per account")
        self.assertIn("also used by secure", result["warnings"][0])

    def test_mismatched_accounts_no_longer_block_unrelated_changes(self):
        """The bug behind 'guest access cannot be set on edit'."""
        rows = json.loads(self.objects[
            "/api/v1/namespaces/lab/configmaps/homestead-shares"]["data"]["shares.json"])
        rows.append({"name": "media", "pvc": "share-media", "path": "/shares/media",
                     "size_gb": 5, "user": "lab", "public": False, "created": "existing"})
        self.objects["/api/v1/namespaces/lab/configmaps/homestead-shares"]["data"]["shares.json"] = json.dumps(rows)
        self.objects["/api/v1/namespaces/lab/secrets/homestead-share-credentials"] = {
            "metadata": {"name": "homestead-share-credentials", "resourceVersion": "3"},
            "data": {"credentials.json": base64.b64encode(json.dumps({
                "secure": {"user": "lab", "password": "one"},
                "media": {"user": "lab", "password": "two"},
            }).encode()).decode()}}

        result = shares.edit_share("secure", 10, "lab", "", True, False)

        self.assertTrue(result["shares"][0]["public"] or result["shares"][1]["public"])
        self.assertEqual({"media", "user:lab"}, set(self.decoded_secret()),
                         "the remaining share keeps one consistent account password")

    def test_user_inventory_migrates_legacy_names_without_exposing_or_writing_passwords(self):
        users = shares.list_users()
        self.assertEqual([{"user": "lab", "has_password": True, "shares": ["secure"]}], users)
        self.assertNotIn("legacy-password", json.dumps(users))
        self.assertEqual([], self.sent)

    def test_unused_user_can_be_saved_selected_and_kept_after_last_share_is_removed(self):
        shares.save_user("backup", "backup-password")
        self.assertFalse(any("deployments" in path for _, path, _ in self.sent))
        self.assertEqual([], next(u for u in shares.list_users() if u["user"] == "backup")["shares"])
        shares.create_share("archive", 0, "backup", "", False, pvc="share-secure", account_mode="existing")
        self.assertEqual("backup-password", self.decoded_secret()["archive"]["password"])
        shares.delete_share("archive")
        self.assertEqual([], next(u for u in shares.list_users() if u["user"] == "backup")["shares"])
        shares.delete_user("backup")
        self.assertNotIn("backup", [u["user"] for u in shares.list_users()])

    def test_user_password_change_updates_every_share_and_rolls_back_on_failure(self):
        from unittest.mock import patch
        shares.create_share("archive", 0, "lab", "", False, pvc="share-secure", account_mode="existing")
        result = shares.save_user("lab", "replacement", "password")
        self.assertNotIn("replacement", json.dumps({k:v for k,v in result.items() if k != "deployment"}))
        self.assertEqual({"replacement"}, {v["password"] for v in self.decoded_secret().values()})
        before = copy.deepcopy(self.objects)
        with patch.object(shares, "apply_samba", side_effect=ValueError("rollout failed")):
            with self.assertRaisesRegex(ValueError, "rollout failed"):
                shares.save_user("lab", "rejected-password", "password")
        self.assertEqual(before, self.objects)

    def test_new_share_cannot_accidentally_reset_existing_user_or_use_a_stale_selection(self):
        for user, password, mode in [("lab", "oops", "new"), ("lab", "oops", "existing"), ("missing", "", "existing")]:
            with self.subTest(mode=mode, user=user):
                with self.assertRaises(ValueError):
                    shares.create_share("archive", 10, user, password, False, account_mode=mode)
        self.assertEqual([], self.created)
        self.assertEqual([], self.sent)

    def test_used_user_cannot_be_deleted_and_duplicate_creation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "secure"):
            shares.delete_user("lab")
        with self.assertRaisesRegex(ValueError, "already exists"):
            shares.save_user("lab", "replacement")
        for password in ("", "bad;argument", "bad\nargument"):
            with self.assertRaises(ValueError):
                shares.save_user("backup", password)
        self.assertEqual([], self.sent)

    def test_folder_cannot_escape_the_volume(self):
        for folder in ("../etc", "media/../../etc", "media/../secrets"):
            with self.assertRaisesRegex(ValueError, "relative path inside the volume"):
                shares.create_share("escape", 0, "escape", "pw", False,
                                    pvc="share-secure", sub_path=folder)
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
