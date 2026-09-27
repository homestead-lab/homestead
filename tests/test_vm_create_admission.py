import copy
import tempfile
import unittest
import urllib.error
from unittest import mock

import test_vm_capacity as fixtures
import server
import homestead_capacity_review as review


class VMCreateAdmissionTests(unittest.TestCase):
    read = fixtures.VMCapacityTests.read
    disk = fixtures.VMCapacityTests.disk

    def setUp(self):
        fixtures.VMCapacityTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(server.OPS, "DATA_DIR", temporary.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.body = {"name": "fresh", "namespace": "lab", "password": "test-only-password", "memory": "2Gi"}
        self.platform = {"harvester": False, "cdi": False}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/storage"] = {
            "metadata": {"name": "storage", "uid": "sc-uid", "resourceVersion": "1"},
            "provisioner": "example.test/storage", "volumeBindingMode": "WaitForFirstConsumer"}
        for patch in (mock.patch.object(server, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.IMP, "kget", side_effect=lambda path: copy.deepcopy(self.read(path))),
                      mock.patch.object(server.PLATFORM, "detect", side_effect=lambda: self.platform),
                      mock.patch.object(server, "vm_default_class", return_value="storage"),
                      mock.patch.object(server.PLACE, "get_nodes", side_effect=lambda: copy.deepcopy(self.nodes)),
                      mock.patch.object(server.PLACE, "hardware_features", return_value=[]),
                      mock.patch.object(server, "get_app_settings", return_value=server.DEFAULT_APP_SETTINGS),
                      mock.patch.object(review, "_key", return_value=b"create-review-tests")):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, path, body):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        def send(method, path, body=None, **kwargs):
            value = copy.deepcopy(body or {})
            value.setdefault("metadata", {}).update(uid="created-uid", resourceVersion="1")
            value["metadata"].setdefault("namespace", "lab")
            if method == "PATCH":
                value.update(apiVersion="v1", kind="Secret")
                value["metadata"].setdefault("name", path.rsplit("/", 1)[-1])
            return value
        with mock.patch.object(server.IMP, "ksend", side_effect=send) as writes, \
                mock.patch.object(server, "ksend") as other_writes, \
                mock.patch.object(server.IPAM, "save_record") as records:
            handler.do_POST()
        other_writes.assert_not_called()
        return handler._send.call_args.args, writes, records

    def reviewed(self):
        result, writes, records = self.call("/api/vm/create/preview", self.body)
        self.assertEqual(200, result[0], result)
        writes.assert_not_called()
        records.assert_not_called()
        return {**result[1]["config"], "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}

    def test_preview_freezes_mac_and_lists_planned_claim_without_secret_manifest(self):
        result, writes, _ = self.call("/api/vm/create/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertRegex(result[1]["config"]["mac"], r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
        self.assertEqual("fresh-disk", result[1]["volumes"][0]["name"])
        self.assertNotIn("secrets", result[1])
        self.assertNotIn("prepared", result[1])
        writes.assert_not_called()

    def test_unreviewed_create_never_creates_a_dependency(self):
        for body in (self.body, {**self.body, "mac": "52:54:00:11:22:33", "confirm_capacity": True}):
            result, writes, records = self.call("/api/vm/create", body)
            self.assertIn(result[0], (400, 409))
            writes.assert_not_called()
            records.assert_not_called()

    def test_reviewed_create_posts_claim_secret_and_vm(self):
        body = self.reviewed()
        result, writes, _ = self.call("/api/vm/create", body)
        self.assertEqual(200, result[0], result)
        self.assertEqual(3, len([c for c in writes.call_args_list if c.args[0] == "POST"]))
        vm = next(c.args[2] for c in writes.call_args_list if c.args[1].endswith("/virtualmachines"))
        self.assertEqual("RerunOnFailure", vm["spec"]["runStrategy"])
        self.assertEqual(body["mac"], vm["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"][0]["macAddress"])
        claim = next(c.args[2] for c in writes.call_args_list if c.args[1].endswith("/persistentvolumeclaims"))
        self.assertEqual("Filesystem", claim["spec"]["volumeMode"])
        self.assertEqual("storage", claim["spec"]["storageClassName"])
        self.assertEqual("succeeded", result[1]["operation"]["status"])
        duplicate, repeated, _ = self.call("/api/vm/create", body)
        self.assertEqual(400, duplicate[0], duplicate)
        self.assertIn("already has job", duplicate[1]["error"])
        repeated.assert_not_called()

    def cdi_setup(self):
        self.platform["cdi"] = True
        self.objects["/apis/cdi.kubevirt.io/v1beta1/storageprofiles/storage"] = {
            "metadata": {"name": "storage", "uid": "profile-uid", "resourceVersion": "1"},
            "status": {"claimPropertySets": [{"accessModes": ["ReadWriteOnce"], "volumeMode": "Block"}]}}

    def test_cdi_create_submits_reviewed_modes_not_later_defaults(self):
        self.cdi_setup()
        body = self.reviewed()
        result, writes, _ = self.call("/api/vm/create", body)
        self.assertEqual(200, result[0], result)
        vm = next(c.args[2] for c in writes.call_args_list if c.args[1].endswith("/virtualmachines"))
        storage = vm["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]
        self.assertEqual(["ReadWriteOnce"], storage["accessModes"])
        self.assertEqual("Block", storage["volumeMode"])
        self.assertEqual("storage", storage["storageClassName"])

    def test_changed_cdi_modes_invalidate_creation_approval(self):
        self.cdi_setup()
        body = self.reviewed()
        profile = self.objects["/apis/cdi.kubevirt.io/v1beta1/storageprofiles/storage"]
        profile["status"]["claimPropertySets"][0]["volumeMode"] = "Filesystem"
        result, writes, _ = self.call("/api/vm/create", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def test_create_context_is_snapshot_not_mutable_manifest_alias(self):
        cfg = server.vm_create_configuration(self.body, preview=True)
        prepared = server.IMP.prepare_vm(cfg, self.platform, "storage")
        _, _, context = server.vm_creation_capacity(prepared)
        prepared["claims"][0]["spec"]["volumeMode"] = "Block"
        self.assertEqual("Filesystem", context["prepared"]["claims"][0]["spec"]["volumeMode"])

    def test_address_record_failure_does_not_hide_prior_secret_warning(self):
        body = self.reviewed()
        with mock.patch.object(server.VM_MUTATION_JOB, "dispatch", return_value={"ok": True, "address": "192.0.2.4", "warning": "Login Secret ownership needs inspection."}), \
                mock.patch.object(server.IPAM, "save_record", side_effect=OSError("unavailable")):
            result = server.reviewed_vm_create(body)
        self.assertIn("Secret ownership", result["warning"])
        self.assertIn("IP-address record", result["warning"])

    def test_mutated_memory_mac_password_or_source_cannot_reuse_approval(self):
        body = self.reviewed()
        for changes in ({"memory": "8Gi"}, {"mac": "52:54:00:11:22:33"}, {"password": "different-test-password"}):
            result, writes, _ = self.call("/api/vm/create", {**body, **changes})
            self.assertEqual(409, result[0], result)
            writes.assert_not_called()

    def test_preexisting_controller_claim_or_datavolume_is_not_adopted(self):
        for platform, path in (({"harvester": True, "cdi": False}, "/api/v1/namespaces/lab/persistentvolumeclaims/fresh-disk"),
                               ({"harvester": False, "cdi": True}, "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/fresh-disk")):
            self.platform = platform
            self.objects[path] = {"metadata": {"name": "fresh-disk", "uid": "someone-elses-disk", "resourceVersion": "1"}}
            result, writes, _ = self.call("/api/vm/create/preview", self.body)
            self.assertEqual(400, result[0], result)
            self.assertIn("already exists", result[1]["error"])
            writes.assert_not_called()
            del self.objects[path]

    def test_capacity_after_review_can_block_before_any_writes(self):
        body = self.reviewed()
        self.nodes[0]["allocatable"]["memory"] = "1Gi"
        result, writes, _ = self.call("/api/vm/create", body)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()

    def image_setup(self):
        self.platform = {"harvester": True, "cdi": False}
        self.body["image_url"] = "https://example.test/image.qcow2"
        self.objects["/apis/storage.k8s.io/v1/storageclasses/image-sc"] = {
            "metadata": {"name": "image-sc", "uid": "image-sc-uid", "resourceVersion": "1"},
            "provisioner": "driver.longhorn.io", "parameters": {"migratable": "true"}}

    def test_image_preparation_rechecks_capacity_before_claim_secret_vm(self):
        self.image_setup()
        body = self.reviewed()
        def download(*args, **kwargs):
            self.nodes[0]["allocatable"]["memory"] = "1Gi"
            return {"namespace": "lab", "name": "image", "storage_class": "image-sc"}
        with mock.patch.object(server.IMP.HVIMAGE, "download", side_effect=download) as image:
            result, writes, records = self.call("/api/vm/create", body)
        self.assertEqual(409, result[0], result)
        image.assert_called_once()
        writes.assert_not_called()
        records.assert_not_called()

    def test_resolved_image_class_can_pass_fresh_admission(self):
        self.image_setup()
        body = self.reviewed()
        with mock.patch.object(server.IMP.HVIMAGE, "download", return_value={"namespace": "lab", "name": "image", "storage_class": "image-sc"}):
            result, writes, _ = self.call("/api/vm/create", body)
        self.assertEqual(200, result[0], result)
        self.assertTrue(any(c.args[1].endswith("/virtualmachines") for c in writes.call_args_list))

    def test_failed_vm_write_never_rolls_back_by_deleting_claims(self):
        body = self.reviewed()
        original = server.IMP.commit_vm
        sent=[]
        def commit(prepared, before_save=None):
            def send(method, path, data=None, **kwargs):
                sent.append((method,path))
                if path.endswith("/virtualmachines"):
                    raise urllib.error.HTTPError(path, 500, "unknown outcome", {}, None)
                return data
            with mock.patch.object(server.IMP,"ksend",side_effect=send):
                return original(prepared,before_save=before_save)
        with mock.patch.object(server.IMP,"commit_vm",side_effect=commit):
            result, _, _ = self.call("/api/vm/create",body)
        self.assertEqual(500,result[0])
        self.assertNotIn("DELETE",[method for method,_ in sent])

    def borrow_disk(self):
        self.platform["cdi"] = True
        self.body["disk_import"] = "root"
        self.disk()
        self.objects["/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/root"] = {
            "metadata": {"name": "root", "namespace": "lab", "uid": "dv-uid", "resourceVersion": "1"},
            "status": {"phase": "Succeeded"}}
        self.objects["/apis/kubevirt.io/v1/virtualmachines"] = {"items": []}

    def test_incomplete_borrowed_disk_ownership_inventory_fails_closed(self):
        self.borrow_disk()
        self.objects["/apis/kubevirt.io/v1/virtualmachines"]["metadata"] = {"continue": "next-page"}
        result, writes, _ = self.call("/api/vm/create/preview", self.body)
        self.assertEqual(400, result[0], result)
        self.assertIn("ownership inventory is incomplete", result[1]["error"])
        writes.assert_not_called()

    def test_borrowed_disk_claimed_by_stopped_vm_after_review_is_not_reused(self):
        self.borrow_disk()
        body = self.reviewed()
        self.vm["spec"]["runStrategy"] = "Halted"
        self.objects["/apis/kubevirt.io/v1/virtualmachines"]["items"] = [self.vm]
        result, writes, _ = self.call("/api/vm/create", body)
        self.assertIn(result[0], (400,409), result)
        writes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
