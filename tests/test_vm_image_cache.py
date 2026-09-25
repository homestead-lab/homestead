"""The image cache lists Harvester's VM images too: where each is kept, what
was made from it, and deleting one only when nothing was."""
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports as imports

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def image(name, url, klass, ready=True):
    return {"metadata": {"name": name, "namespace": "lab"},
            "spec": {"displayName": url.rsplit("/", 1)[-1], "url": url},
            "status": {"size": 600 * 1024 ** 2, "virtualSize": 3 * 1024 ** 3, "storageClassName": klass,
                       "conditions": [{"type": "Imported", "status": "True" if ready else "Unknown"}]}}


class VmImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.objects = {
            "/apis/harvesterhci.io/v1beta1/virtualmachineimages": {"items": [
                image("image-a", "https://cloud-images.ubuntu.com/noble/x.img?token=secret", "longhorn-image-a"),
                image("image-b", "https://cloud.debian.org/debian.qcow2", "longhorn-image-b")]},
            "/apis/storage.k8s.io/v1/storageclasses": {"items": [
                {"metadata": {"name": "longhorn-image-a"}, "parameters": {"backingImage": "vmi-aaa"}},
                {"metadata": {"name": "longhorn-image-b"}, "parameters": {"backingImage": "vmi-bbb"}}]},
            f"{LH}/backingimages": {"items": [
                {"metadata": {"name": "vmi-aaa"}, "status": {"diskFileStatusMap": {
                    "uuid-1": {"state": "ready"}, "uuid-2": {"state": "ready"}, "uuid-3": {"state": "in-progress"}}}},
                {"metadata": {"name": "vmi-bbb"}, "status": {"diskFileStatusMap": {"uuid-3": {"state": "ready"}}}}]},
            f"{LH}/nodes": {"items": [
                {"metadata": {"name": "node1"}, "status": {"diskStatus": {"d": {"diskUUID": "uuid-1"}}}},
                {"metadata": {"name": "node2"}, "status": {"diskStatus": {"d": {"diskUUID": "uuid-2"}}}},
                {"metadata": {"name": "node3"}, "status": {"diskStatus": {"d": {"diskUUID": "uuid-3"}}}}]},
            "/api/v1/persistentvolumeclaims": {"items": [
                {"metadata": {"name": "pihole-disk", "namespace": "lab",
                              "annotations": {"harvesterhci.io/imageId": "lab/image-a"}},
                 "spec": {"storageClassName": "longhorn-image-a"}}]},
            "/apis/kubevirt.io/v1/virtualmachines": {"items": [
                {"metadata": {"name": "pihole", "namespace": "lab"}, "spec": {"template": {"spec": {"volumes": [
                    {"name": "root", "persistentVolumeClaim": {"claimName": "pihole-disk"}}]}}}}]},
        }
        self.sent = []

        def get(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return self.objects[path]
        self.restore = imports.kget, imports.ksend, imports._cache
        imports.kget = get
        imports.ksend = lambda method, path, body=None, **kw: self.sent.append((method, path))
        imports._cache = {}

    def tearDown(self):
        imports.kget, imports.ksend, imports._cache = self.restore

    def rows(self):
        return {row["name"]: row for row in imports.vm_image_cache()["images"]}

    def test_each_image_says_where_it_is_kept_and_what_was_made_from_it(self):
        a = self.rows()["image-a"]
        self.assertEqual((["node1", "node2"], ["lab/pihole-disk"], ["lab/pihole"]), (a["nodes"], a["disks"], a["used_by"]))
        self.assertEqual((600.0, 3.0, "ready"), (a["size_mb"], a["virtual_size_gb"], a["state"]))

    def test_a_download_address_shows_only_its_host(self):
        self.assertEqual("cloud-images.ubuntu.com", self.rows()["image-a"]["source"])

    def test_an_image_a_disk_was_made_from_is_not_deleted(self):
        with self.assertRaisesRegex(PermissionError, "lab/pihole-disk"):
            imports.delete_vm_image("lab", "image-a", "x.img")
        self.assertEqual([], self.sent)

    def test_an_unused_image_is_deleted_once_its_name_is_typed(self):
        with self.assertRaisesRegex(ValueError, "type debian.qcow2"):
            imports.delete_vm_image("lab", "image-b", "debian")
        imports.delete_vm_image("lab", "image-b", "debian.qcow2")
        self.assertEqual([("DELETE", "/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachineimages/image-b")], self.sent)

    def test_without_harvester_it_says_why_there_is_no_cache(self):
        del self.objects["/apis/harvesterhci.io/v1beta1/virtualmachineimages"]
        report = imports.vm_image_cache()
        self.assertEqual((False, []), (report["harvester"], report["images"]))
        self.assertIn("CDI downloads each VM's disk", report["note"])


if __name__ == "__main__":
    unittest.main()
