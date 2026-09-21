import copy
import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import harvui_imports as imports


class ContainerImportStorageTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.created = []

        def get(path):
            if path.endswith("/configmaps/harvui-sources"):
                return {"data": {"sources.json": json.dumps([{
                    "name": "unraid", "host": "192.0.2.10", "user": "root", "base_path": "/mnt/user/appdata",
                }])}}
            if path.endswith("/persistentvolumeclaims/shared-appdata"):
                return {"spec": {"storageClassName": "longhorn-rwx", "accessModes": ["ReadWriteMany"]}}
            raise AssertionError(path)

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            return body or {}

        def create_pvc(ns, name, size, storage_class=None, access_mode="ReadWriteOnce"):
            self.created.append((ns, name, size, storage_class, access_mode))

        def build_deployment(cfg):
            return ({"metadata": {"name": cfg["name"], "namespace": cfg["namespace"]}}, None)

        imports.bind(get, send, create_pvc, build_deployment, "lab", {})
        self.original_sleep = imports.time.sleep
        imports.time.sleep = lambda _: None

    def tearDown(self):
        imports.time.sleep = self.original_sleep

    def config(self, **extra):
        return {"source": "unraid", "remote_path": "/mnt/user/appdata/example", "name": "example",
                "image": "example/app:latest", "create_workload": False, **extra}

    def test_new_import_uses_selected_longhorn_settings(self):
        result = imports.import_container(self.config(
            pvc_name="example-config", size_gb=25, storage_class="longhorn-rwx",
            access_mode="ReadWriteMany"))
        self.assertEqual([("lab", "example-config", 25, "longhorn-rwx", "ReadWriteMany")], self.created)
        self.assertEqual("example-config", result["pvc"])
        self.assertEqual("ReadWriteMany", result["access_mode"])
        job = next(body for method, path, body in self.sent if method == "POST" and path.endswith("/jobs"))
        claim = job["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        self.assertEqual("example-config", claim)

    def test_existing_import_reuses_selected_claim_without_creating_one(self):
        result = imports.import_container(self.config(
            pvc_name="shared-appdata", reuse_existing=True, size_gb=1))
        self.assertEqual([], self.created)
        self.assertEqual("longhorn-rwx", result["storage_class"])
        self.assertEqual("ReadWriteMany", result["access_mode"])


if __name__ == "__main__":
    unittest.main()


class ImportJobRemovalTests(unittest.TestCase):
    """A failed import must be clearable, or it traps its own volume."""

    def setUp(self):
        self.sent = []
        self.job = {}
        self.deployments = set()

        def get(path):
            if "/deployments/" in path:
                if path.rsplit("/", 1)[-1] in self.deployments:
                    return {"metadata": {"name": path.rsplit("/", 1)[-1]}}
                raise ValueError("not found")
            if "/jobs/" in path:
                if not self.job:
                    raise ValueError("not found")
                return self.job
            return {}

        imports.bind(get, lambda method, path, body=None, **kw:
                     self.sent.append((method, path)) or {},
                     lambda *a, **k: {}, lambda cfg: ({}, None), "lab", {})

    def test_a_failed_import_job_can_be_removed(self):
        result = imports.delete_import("homestead-import-obsidian")

        self.assertEqual("DELETE", self.sent[-1][0])
        self.assertIn("jobs/homestead-import-obsidian", self.sent[-1][1])
        self.assertIn("propagationPolicy=Background", self.sent[-1][1])
        self.assertIn("removed", result["message"])

    def test_a_job_from_before_the_rename_is_still_removable(self):
        imports.delete_import("harvui-import-obsidian")

        self.assertIn("jobs/harvui-import-obsidian", self.sent[-1][1])

    def test_a_cleanup_plan_reports_what_the_import_created(self):
        self.job = {"metadata": {"name": "homestead-import-obsidian", "namespace": "lab",
                                 "labels": {"harvui.io/app": "obsidian"},
                                 "annotations": {"homestead.io/import-workload": "obsidian",
                                                 "homestead.io/import-volume": "obsidian-appdata",
                                                 "homestead.io/import-volume-created": "true"}}}
        self.deployments = {"obsidian"}

        plan = imports.import_cleanup_plan("homestead-import-obsidian")

        self.assertEqual(("obsidian", "obsidian-appdata", True, True),
                         (plan["workload"], plan["volume"], plan["volume_created"], plan["known"]))

    def test_a_borrowed_volume_is_never_offered_for_deletion(self):
        self.job = {"metadata": {"name": "homestead-import-plex", "namespace": "lab",
                                 "annotations": {"homestead.io/import-workload": "plex",
                                                 "homestead.io/import-volume": "plexmedia",
                                                 "homestead.io/import-volume-created": "false"}}}
        self.deployments = {"plex"}

        self.assertFalse(imports.import_cleanup_plan("homestead-import-plex")["volume_created"])

    def test_a_workload_already_gone_is_not_offered_either(self):
        self.job = {"metadata": {"name": "homestead-import-obsidian", "namespace": "lab",
                                 "annotations": {"homestead.io/import-workload": "obsidian",
                                                 "homestead.io/import-volume": "obsidian-appdata",
                                                 "homestead.io/import-volume-created": "true"}}}
        self.deployments = set()

        self.assertEqual("", imports.import_cleanup_plan("homestead-import-obsidian")["workload"])

    def test_an_import_from_before_the_record_says_it_does_not_know(self):
        self.job = {"metadata": {"name": "homestead-import-old", "namespace": "lab",
                                 "labels": {"harvui.io/app": "old"}}}
        self.deployments = {"old"}

        plan = imports.import_cleanup_plan("homestead-import-old")

        self.assertFalse(plan["known"])
        self.assertFalse(plan["volume_created"], "an unknown volume is never deletable")

    def test_only_homestead_import_jobs_are_removable(self):
        for name in ("", "kube-system-thing", "homestead-pull-frigate", "../../etc"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "unknown import job"):
                    imports.delete_import(name)
        self.assertEqual([], self.sent)
