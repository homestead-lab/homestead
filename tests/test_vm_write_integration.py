import copy
import json
import unittest
from unittest import mock

import test_vm_create as create_fixtures
import test_vm_edit as edit_fixtures
import homestead_imports as imports
import homestead_vms as vms
import homestead_vm_write as journal


class WriteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.events, self.calls = [], []
        self.writer = journal.ResourceWriter(self.send, self.events.append)

    def send(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body)))
        target, before = journal._target(method, path, body)
        value = copy.deepcopy(body)
        value.update(apiVersion=target["apiVersion"], kind=target["kind"])
        value["metadata"] = {"namespace": target["namespace"], "name": target["name"],
                             "uid": before["uid"] if before else "created-" + target["name"],
                             "resourceVersion": str(100 + len(self.calls))}
        return value

    def prepare_create(self, platform=None, **cfg):
        fixture = create_fixtures.VmCreateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return imports.prepare_vm({"name": "new-vm", "namespace": "lab", "password": "private-login",
                                   "memory": "2Gi", "network": "pod", **cfg}, platform or create_fixtures.K3S_BARE, "storage")

    def test_create_journals_claim_secret_vm_and_owner_patch_without_global_replacement(self):
        prepared = self.prepare_create()
        with mock.patch.object(imports, "ksend", side_effect=AssertionError("global write bypassed journal")):
            result = imports.commit_vm(prepared, send=self.writer)
        self.writer.check()
        accepted = [row for row in self.events if row["phase"] == "accepted"]
        self.assertEqual(["PersistentVolumeClaim", "Secret", "VirtualMachine", "Secret"], [row["resource"]["kind"] for row in accepted])
        self.assertEqual(["POST", "POST", "POST", "PATCH"], [row["method"] for row in accepted])
        self.assertEqual("created-new-vm", result["vm_identity"]["uid"])
        self.assertNotIn("private-login", json.dumps(self.events))

    def test_edit_journals_secret_new_claim_and_returns_actual_vm_receipt(self):
        edit_fixtures.Cluster({"harvester": False, "cdi": False})
        prepared = vms.prepare_edit("lab", "web", {"cloud_init": {"user_data": "private-cloud-init"},
                "add_disks": [{"kind": "disk", "size": "2Gi", "storage_class": "storage"}]})
        with mock.patch.object(vms, "ksend", side_effect=AssertionError("global write bypassed journal")):
            result = vms.commit_edit(prepared, send=self.writer)
        self.writer.check()
        accepted = [row for row in self.events if row["phase"] == "accepted"]
        self.assertEqual(["Secret", "PersistentVolumeClaim", "VirtualMachine"], [row["resource"]["kind"] for row in accepted])
        self.assertEqual({"namespace": "lab", "name": "web", "uid": "u1", "resourceVersion": "103"}, result["vm_identity"])
        self.assertNotIn("private-cloud-init", json.dumps(self.events))

    def test_owner_patch_warning_cannot_clear_failed_writer(self):
        prepared = self.prepare_create()
        transport = self.writer.send
        def send(method, path, body=None, **kwargs):
            if method == "PATCH":
                raise TimeoutError("private-response")
            return transport(method, path, body, **kwargs)
        self.writer.send = send
        result = imports.commit_vm(prepared, send=self.writer)
        self.assertIn("ownership could not be recorded", result["warning"])
        self.assertEqual("uncertain", self.events[-1]["phase"])
        with self.assertRaises(journal.WriteFailure):
            self.writer.check()  # API must not report complete despite warning

    def test_create_and_edit_downloads_use_same_request_local_writer(self):
        def download(read, send, namespace, url, klass):
            self.assertIs(self.writer, send)
            send("POST", f"/apis/harvesterhci.io/v1beta1/namespaces/{namespace}/virtualmachineimages",
                 {"metadata": {"name": "image-test", "namespace": namespace}, "spec": {"url": url}})
            return {"namespace": namespace, "name": "image-test", "storage_class": "image-sc"}
        prepared = self.prepare_create(create_fixtures.HARVESTER, image_url="https://example.test/private-image")
        with mock.patch.object(imports.HVIMAGE, "download", side_effect=download):
            imports.commit_vm(prepared, send=self.writer)
        self.assertEqual("VirtualMachineImage", self.events[0]["resource"]["kind"])
        self.assertNotIn("private-image", json.dumps(self.events))
        self.events.clear()
        edit_fixtures.Cluster({"harvester": True, "cdi": True})
        prepared = vms.prepare_edit("lab", "web", {"add_disks": [{"url": "https://example.test/private-image"}]})
        with mock.patch.object(vms.HVIMAGE, "download", side_effect=download):
            vms.commit_edit(prepared, send=self.writer)
        self.assertEqual("VirtualMachineImage", self.events[0]["resource"]["kind"])
        self.assertNotIn("private-image", json.dumps(self.events))

    def test_resize_after_vm_save_is_journaled_and_never_relabels_failed_save_as_success(self):
        prepared = {"namespace": "lab", "name": "web", "vm": copy.deepcopy(edit_fixtures.VM),
                    "to_create": [], "resize": [("data", "2Gi")], "dropped": [], "changed_hardware": False,
                    "effects": [], "restart": False, "claims": {"data": {"metadata": {"uid": "claim-uid", "resourceVersion": "8"}}}}
        transport = self.writer.send
        def send(method, path, body=None, **kwargs):
            if method == "PATCH":
                raise TimeoutError("private-error")
            return transport(method, path, body, **kwargs)
        self.writer.send = send
        with mock.patch.object(vms, "_recheck_edit"), self.assertRaises(journal.WriteFailure):
            vms.commit_edit(prepared, send=self.writer)
        self.assertEqual(["intent", "accepted", "intent", "uncertain"], [row["phase"] for row in self.events])
        self.assertEqual("VirtualMachine", self.events[1]["resource"]["kind"])
        self.assertEqual("PersistentVolumeClaim", self.events[-1]["resource"]["kind"])
        self.assertEqual("claim-uid", self.events[-1]["before"]["uid"])


if __name__ == "__main__":
    unittest.main()
