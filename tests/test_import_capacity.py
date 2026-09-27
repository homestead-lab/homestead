import copy
import tempfile
import unittest
import urllib.error
from unittest import mock

import test_rollout_capacity as fixtures
import server
import homestead_imports as imports
import homestead_import_guard as guard
import homestead_place as place


class ImportCapacityTests(unittest.TestCase):
    get = fixtures.RolloutCapacityTests.get

    def setUp(self):
        fixtures.RolloutCapacityTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journal = mock.patch.object(server.OPS, "DATA_DIR", temporary.name)
        journal.start()
        self.addCleanup(journal.stop)
        self.body = {"name": "imported", "source": "tower", "image": "example/app:1",
                     "memory": "256Mi", "memory_limit": "1Gi", "network_mode": "internal",
                     "volumes": [{"name": "new-data", "size_gb": 10, "storage_class": "storage"}],
                     "mappings": [{"remote_path": "/data", "mount_path": "/config", "pvc": "new-data"}]}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/storage"] = {
            "metadata": {"name": "storage", "uid": "sc"}, "provisioner": "driver.longhorn.io"}
        for patch in (mock.patch.object(imports, "kget", side_effect=self.get),
                      mock.patch.object(imports, "build_deployment", side_effect=server.build_deployment),
                      mock.patch.object(imports, "_source", return_value={"name": "tower", "host": "192.0.2.10", "user": "test"}),
                      mock.patch.object(imports, "source_secret", return_value="source-credentials"),
                      mock.patch.object(imports, "NS", "lab"),
                      mock.patch.object(server, "DEFAULT_NS", "lab"),
                      mock.patch.object(server, "guard_managed_smb")):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, path, body):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        def send(method, path, obj=None, **kwargs):
            result = copy.deepcopy(obj or {})
            result.setdefault("metadata", {}).update(uid="created-uid", resourceVersion="1", namespace="lab")
            self.objects[path + ("/" + result["metadata"]["name"] if method == "POST" else "")] = copy.deepcopy(result)
            return result
        with mock.patch.object(server, "ksend", side_effect=send) as writes, \
             mock.patch.object(server, "create_pvc", wraps=server.create_pvc) as pvc, \
             mock.patch.object(server, "persist_icon_config") as icons, \
             mock.patch.object(server.OPS, "start", wraps=server.OPS.start) as jobs:
            handler.do_POST()
        return handler._send.call_args.args, writes, pvc, icons, jobs

    def reviewed(self):
        response, writes, pvc, icons, jobs = self.call("/api/import/preview", self.body)
        self.assertEqual(200, response[0], response)
        for mutation in (writes, pvc, icons, jobs):
            mutation.assert_not_called()
        return {**self.body, "capacity_token": response[1]["capacity_token"], "confirm_capacity": True}

    def test_preview_has_two_phases_and_no_writes(self):
        response, *_ = self.call("/api/import/preview", self.body)
        self.assertEqual(200, response[0], response)
        self.assertEqual(["Copy files", "Imported application"], [p["title"] for p in response[1]["phases"]])
        self.assertEqual(.12, response[1]["phases"][0]["capacity"]["pod_request_gb"])
        self.reviewed()

    def test_unreviewed_import_never_mutates(self):
        response, *mutations = self.call("/api/import", self.body)
        self.assertEqual(409, response[0], response)
        for mutation in mutations:
            mutation.assert_not_called()

    def test_reviewed_copy_preserves_limits_and_stops_application(self):
        body = self.reviewed()
        response, writes, pvc, _, jobs = self.call("/api/import", body)
        self.assertEqual(200, response[0], response)
        dep = next(c.args[2] for c in writes.call_args_list if c.args[1].endswith("/deployments"))
        job = next(c.args[2] for c in writes.call_args_list if c.args[1].endswith("/jobs"))
        self.assertEqual(0, dep["spec"]["replicas"])
        self.assertEqual("1Gi", dep["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])
        self.assertEqual("512Mi", job["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])
        self.assertEqual("created-uid", job["metadata"]["ownerReferences"][0]["uid"])
        self.assertNotIn("DELETE", [c.args[0] for c in writes.call_args_list])
        pvc.assert_called_once()
        jobs.assert_called_once()

    def test_old_start_during_copy_option_cannot_start_writer(self):
        self.body["start_after_copy"] = False
        response, writes, *_ = self.call("/api/import", self.reviewed())
        self.assertEqual(200, response[0], response)
        dep = next(c.args[2] for c in writes.call_args_list if c.args[1].endswith("/deployments"))
        self.assertEqual(0, dep["spec"]["replicas"])

    def test_mutated_input_and_expired_token_have_zero_writes(self):
        body = self.reviewed()
        for changed in ({**body, "memory_limit": "2Gi"}, {**body, "capacity_token": "1.expired"}):
            response, *mutations = self.call("/api/import", changed)
            self.assertEqual(409, response[0], response)
            for mutation in mutations:
                mutation.assert_not_called()

    def test_hard_capacity_cannot_be_overridden(self):
        body = self.reviewed()
        self.nodes[0]["allocatable"]["memory"] = "5Gi"
        response, *mutations = self.call("/api/import", body)
        self.assertEqual(409, response[0], response)
        for mutation in mutations:
            mutation.assert_not_called()

    def test_capacity_warning_can_be_explicitly_overridden(self):
        self.nodes[0]["mem_used_gb"] = 7.8
        response, *_ = self.call("/api/import", self.reviewed())
        self.assertEqual(200, response[0], response)

    def test_no_copy_import_has_no_phantom_job(self):
        self.body.update(volumes=[], mappings=[])
        response, writes, pvc, _, jobs = self.call("/api/import", self.reviewed())
        self.assertEqual(200, response[0], response)
        self.assertEqual("", response[1]["job"])
        jobs.assert_called_once()  # durable setup record, not a Kubernetes copy Job
        pvc.assert_not_called()
        self.assertEqual(1, writes.call_count)

    def test_new_name_collisions_are_not_reused(self):
        body = self.reviewed()
        for path in ("/apis/apps/v1/namespaces/lab/deployments/imported",
                     "/apis/batch/v1/namespaces/lab/jobs/homestead-import-imported",
                     "/api/v1/namespaces/lab/persistentvolumeclaims/new-data"):
            self.objects[path] = {"metadata": {"uid": "someone-else"}}
            response, *mutations = self.call("/api/import", body)
            self.assertEqual(400, response[0], response)
            for mutation in mutations:
                mutation.assert_not_called()
            del self.objects[path]

    def existing(self):
        self.body["volumes"][0]["create"] = False
        self.path = "/api/v1/namespaces/lab/persistentvolumeclaims/new-data"
        self.objects[self.path] = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "new-data", "namespace": "lab", "uid": "original-claim", "resourceVersion": "1"},
            "spec": {"accessModes": ["ReadWriteMany"], "storageClassName": "storage"},
            "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}}}

    def test_borrowed_volume_is_not_created_and_changed_uid_is_rejected(self):
        self.existing()
        body = self.reviewed()
        response, _, pvc, *_ = self.call("/api/import", body)
        self.assertEqual(200, response[0], response)
        pvc.assert_not_called()
        for path in list(self.objects):
            if "/deployments/imported" in path or "/jobs/homestead-import-imported" in path:
                del self.objects[path]
        self.objects[self.path]["metadata"]["uid"] = "replacement"
        response, *mutations = self.call("/api/import", body)
        self.assertEqual(409, response[0], response)
        for mutation in mutations:
            mutation.assert_not_called()

    def test_rwx_writer_appearing_after_review_blocks_copy(self):
        self.existing()
        body = self.reviewed()
        self.pods[0]["spec"]["volumes"] = [{"persistentVolumeClaim": {"claimName": "new-data"}}]
        response, *mutations = self.call("/api/import", body)
        self.assertEqual(400, response[0], response)
        self.assertIn("Stop volume consumers", str(response))
        for mutation in mutations:
            mutation.assert_not_called()

    def test_unknown_or_paginated_inventory_is_not_empty(self):
        for value in ({}, {"items": [], "metadata": {"continue": "next-page"}}):
            self.objects["/api/v1/pods"] = value
            response, *mutations = self.call("/api/import", self.body)
            self.assertEqual(400, response[0], response)
            for mutation in mutations:
                mutation.assert_not_called()

    def test_copy_guard_is_enforced_by_workload_start_plan(self):
        self.current["metadata"]["annotations"] = {guard.JOB: "copy"}
        self.current["spec"]["replicas"] = 0
        self.assertTrue(place.start_plan("lab", "shared", 1)["blocked"])
        self.assertFalse(place.start_plan("lab", "shared", 0)["blocked"])

    def test_capacity_is_checked_again_after_icon_fetch(self):
        body = self.reviewed()
        def fill_node(cfg):
            self.nodes[0]["allocatable"]["memory"] = "5Gi"
        with mock.patch.object(server, "persist_icon_config", side_effect=fill_node), \
             mock.patch.object(imports, "ksend") as writes, \
             mock.patch.object(imports, "create_pvc") as pvc:
            with self.assertRaises(server.CAPACITY_REVIEW.Rejected):
                server.reviewed_import(body)
        writes.assert_not_called()
        pvc.assert_not_called()

    def test_persisted_icon_does_not_change_the_signed_dispatch_context(self):
        body = self.reviewed()
        def icon(cfg):
            cfg["icon"] = "/api/icons/persisted.svg"
        with mock.patch.object(server, "persist_icon_config", side_effect=icon), \
             mock.patch.object(server.IMPORT_JOB, "dispatch", return_value={"ok": True}) as dispatch:
            server.reviewed_import(body)
        submitted, prepared, context = dispatch.call_args.args[:3]
        self.assertTrue(server.CAPACITY_REVIEW.valid(submitted, context))
        self.assertEqual("/api/icons/persisted.svg", prepared["deployment"]["metadata"]["annotations"]["homestead.io/icon"])

    def test_preview_requires_same_admin_role_as_import(self):
        self.assertIn("/api/import/preview", server.ADMIN_ROUTES)


class ImportInterlockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journal = mock.patch.object(server.OPS, "DATA_DIR", temporary.name)
        journal.start()
        self.addCleanup(journal.stop)
        self.dep = {"metadata": {"name": "app", "uid": "deployment", "annotations": {guard.JOB: "copy"}}}
        self.job = {"metadata": {"ownerReferences": [{"kind": "Deployment", "uid": "deployment"}]},
                    "status": {"conditions": [{"type": "Complete", "status": "True"}]}}

    def test_completed_matching_copy_unlocks_start(self):
        self.assertEqual("", guard.pending(self.dep, "lab", lambda _: self.job))

    def test_incomplete_failed_unknown_and_replaced_copy_block_start(self):
        for job in ({}, {**self.job, "status": {}},
                    {**self.job, "status": {"conditions": [{"type": "Failed", "status": "True"}]}},
                    {**self.job, "metadata": {"ownerReferences": [{"kind": "Deployment", "uid": "other"}]}}):
            self.assertTrue(guard.pending(self.dep, "lab", lambda _: job))
        self.assertTrue(guard.pending(self.dep, "lab", mock.Mock(side_effect=OSError("offline"))))

    def test_partial_failure_preserves_volumes_and_does_not_launch_copy(self):
        prepared = {"volumes": [{"name": "data", "size_gb": 10, "storage_class": "storage", "access_mode": "ReadWriteOnce", "create": True}],
                    "deployment": self.dep, "job": self.job, "service": None, "result": {}}
        with mock.patch.object(imports, "create_pvc") as pvc, \
             mock.patch.object(imports, "ksend", side_effect=OSError("conflict")) as send:
            with self.assertRaises(OSError):
                imports.commit_import(prepared)
        pvc.assert_called_once()
        self.assertEqual(1, send.call_count)
        self.assertEqual("POST", send.call_args.args[0])

    def test_successful_cleanup_records_completion_before_removing_job(self):
        self.dep["metadata"].update(resourceVersion="123", annotations={guard.JOB:"homestead-import-app"})
        self.job["metadata"].update(uid="job-uid", ownerReferences=[{"kind":"Deployment", "uid":"deployment", "name":"app"}])
        def get(path):
            return copy.deepcopy(self.job if "/jobs/" in path else self.dep)
        with mock.patch.object(imports, "kget", side_effect=get), \
             mock.patch.object(imports, "ksend") as send, \
             mock.patch.object(imports, "_stop_job_pods", return_value=[]), \
             mock.patch.object(imports, "_daemonset_exists", return_value=False):
            imports.delete_import("homestead-import-app")
        first = send.call_args_list[0]
        self.assertEqual("PUT", first.args[0])
        meta = first.args[2]["metadata"]
        self.assertEqual("123", meta["resourceVersion"])
        self.assertNotIn(guard.JOB, meta["annotations"])
        self.assertEqual("job-uid", meta["annotations"]["homestead.io/import-completed-job"])
        self.assertEqual("DELETE", send.call_args_list[1].args[0])

    def test_failed_copy_cleanup_never_unlocks_application(self):
        self.job["status"] = {"conditions":[{"type":"Failed", "status":"True"}]}
        with mock.patch.object(imports, "kget", return_value=self.job), \
             mock.patch.object(imports, "ksend") as send, \
             mock.patch.object(imports, "_stop_job_pods", return_value=[]), \
             mock.patch.object(imports, "_daemonset_exists", return_value=False):
            imports.delete_import("homestead-import-app")
        self.assertEqual(["DELETE"], [c.args[0] for c in send.call_args_list])


if __name__ == "__main__":
    unittest.main()
