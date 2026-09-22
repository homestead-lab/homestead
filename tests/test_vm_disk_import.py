import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports as imports


class VmDiskImportTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.sent = []

        def get(path):
            if path in self.objects:
                return self.objects[path]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            return body

        imports.kget = get
        imports.ksend = send
        imports.NS = "lab"
        imports._cache = {}

    def test_import_builds_cdi_datavolume_without_returning_source_url(self):
        secret_url = "https://images.example.test/server.qcow2?token=sensitive"
        result = imports.import_vm_disk({
            "namespace": "lab", "name": "server-disk", "source_url": secret_url,
            "size_gb": 32, "storage_class": "longhorn-r2",
            "access_mode": "ReadWriteOnce", "checksum": "sha256:" + "a" * 64,
        })
        self.assertNotIn("sensitive", repr(result))
        method, path, body = self.sent[-1]
        self.assertEqual("POST", method)
        self.assertEqual(
            "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes", path)
        self.assertEqual("kubevirt", body["spec"]["contentType"])
        self.assertEqual(secret_url, body["spec"]["source"]["http"]["url"])
        self.assertEqual("sha256:" + "a" * 64,
                         body["spec"]["source"]["http"]["checksum"])
        self.assertEqual("32Gi", body["spec"]["storage"]["resources"]["requests"]["storage"])
        self.assertEqual("Filesystem", body["spec"]["storage"]["volumeMode"])
        self.assertNotIn("url", repr(body["metadata"]))

    def test_import_rejects_unsafe_source_and_bad_checksum(self):
        with self.assertRaisesRegex(ValueError, "HTTP or HTTPS"):
            imports.import_vm_disk({"name": "disk", "source_url": "file:///tmp/disk.qcow2"})
        with self.assertRaisesRegex(ValueError, "credentials"):
            imports.import_vm_disk({"name": "disk", "source_url": "https://user:pass@example.test/a.vmdk"})
        with self.assertRaisesRegex(ValueError, "checksum"):
            imports.import_vm_disk({"name": "disk", "source_url": "https://example.test/a.vmdk",
                                    "checksum": "sha256:1234"})
        self.assertEqual([], self.sent)

    def test_plan_blocks_either_datavolume_or_pvc_conflict(self):
        dv_path = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/taken"
        pvc_path = "/api/v1/namespaces/lab/persistentvolumeclaims/taken"
        self.objects[dv_path] = {"metadata": {"name": "taken"}}
        self.objects[pvc_path] = {"metadata": {"name": "taken"}}
        plan = imports.vm_disk_import_plan("lab", "taken")
        self.assertFalse(plan["ready"])
        self.assertEqual({"DataVolume", "PersistentVolumeClaim"},
                         {item["kind"] for item in plan["conflicts"]})
        with self.assertRaisesRegex(ValueError, "already exists"):
            imports.import_vm_disk({"name": "taken", "source_url": "https://example.test/a.qcow2"})

    def test_list_returns_status_but_never_source_url(self):
        self.objects["/apis/cdi.kubevirt.io/v1beta1/datavolumes"] = {"items": [{
            "metadata": {"namespace": "lab", "name": "router-disk",
                         "labels": {imports.VM_DISK_LABEL: "true"}},
            "spec": {"source": {"http": {"url": "https://example.test/a.vmdk?token=secret"}},
                     "storage": {"storageClassName": "longhorn-r2",
                                 "accessModes": ["ReadWriteOnce"],
                                 "resources": {"requests": {"storage": "16Gi"}}}},
            "status": {"phase": "ImportInProgress", "progress": "47.3%", "claimName": "router-disk"},
        }]}
        self.objects["/apis/kubevirt.io/v1/virtualmachines"] = {"items": []}
        rows = imports.list_vm_disks()
        self.assertEqual(47.3, rows[0]["progress"])
        self.assertEqual("16Gi", rows[0]["capacity"])
        self.assertNotIn("example.test", repr(rows))
        self.assertNotIn("secret", repr(rows))

    def test_vm_can_attach_completed_import_without_copy_or_password(self):
        disk_path = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/server-disk"
        self.objects[disk_path] = {"status": {"phase": "Succeeded"}}
        self.objects["/apis/kubevirt.io/v1/virtualmachines"] = {"items": []}
        result = imports.create_vm({"name": "server", "namespace": "lab",
                                    "disk_import": "server-disk", "password": ""})
        vm = self.sent[-1][2]
        self.assertEqual("server-disk", result["datavolume"])
        self.assertNotIn("dataVolumeTemplates", vm["spec"])
        pod_spec = vm["spec"]["template"]["spec"]
        self.assertEqual([{"name": "root", "dataVolume": {"name": "server-disk"}}],
                         pod_spec["volumes"])
        self.assertEqual(1, len(pod_spec["domain"]["devices"]["disks"]))

    def test_vm_refuses_incomplete_or_already_attached_import(self):
        disk_path = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/server-disk"
        self.objects[disk_path] = {"status": {"phase": "ImportInProgress"}}
        with self.assertRaisesRegex(ValueError, "not ready"):
            imports.create_vm({"name": "server", "disk_import": "server-disk"})
        self.objects[disk_path]["status"]["phase"] = "Succeeded"
        self.objects["/apis/kubevirt.io/v1/virtualmachines"] = {"items": [{
            "metadata": {"namespace": "lab", "name": "existing"},
            "spec": {"template": {"spec": {"volumes": [
                {"name": "root", "dataVolume": {"name": "server-disk"}}]}}},
        }]}
        with self.assertRaisesRegex(ValueError, "already attached"):
            imports.create_vm({"name": "server", "disk_import": "server-disk"})


if __name__ == "__main__":
    unittest.main()
