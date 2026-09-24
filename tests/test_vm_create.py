import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports as imports

HARVESTER = {"harvester": True, "cdi": True}
K3S_CDI = {"harvester": False, "cdi": True}
K3S_BARE = {"harvester": False, "cdi": False}
IMAGE = {"metadata": {"name": "image-ubuntu", "namespace": "default"},
         "spec": {"displayName": "ubuntu.img"},
         "status": {"size": 3 * 1024 ** 3, "storageClassName": "longhorn-image-ubuntu"}}


class VmCreateTests(unittest.TestCase):
    def setUp(self):
        self.objects = {"/apis/harvesterhci.io/v1beta1/virtualmachineimages": {"items": [IMAGE]}}
        self.sent = []

        def get(path):
            if path in self.objects:
                return self.objects[path]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            return body

        imports.kget, imports.ksend, imports.NS, imports._cache = get, send, "lab", {}

    def vm(self):
        return next(b for m, p, b in self.sent if p.endswith("/virtualmachines"))

    def create(self, platform, sc="", **cfg):
        cfg = {"name": "web", "password": "a-long-password", **cfg}
        return imports.create_vm(cfg, platform, sc)

    def test_harvester_blank_disk_is_a_claim_template_on_the_chosen_class(self):
        self.create(HARVESTER, "longhorn-r2")
        vm = self.vm()
        claim = json.loads(vm["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])[0]
        self.assertEqual(("web-disk", "longhorn-r2", "Block", ["ReadWriteMany"]),
                         (claim["metadata"]["name"], claim["spec"]["storageClassName"],
                          claim["spec"]["volumeMode"], claim["spec"]["accessModes"]))
        spec = vm["spec"]
        self.assertNotIn("dataVolumeTemplates", spec)
        self.assertNotIn("running", spec)
        self.assertEqual("RerunOnFailure", spec["runStrategy"])
        self.assertEqual({"name": "root", "persistentVolumeClaim": {"claimName": "web-disk"}},
                         spec["template"]["spec"]["volumes"][0])
        self.assertEqual("LiveMigrate", spec["template"]["spec"]["evictionStrategy"])

    def test_harvester_image_uses_the_images_own_class(self):
        self.create(HARVESTER, "longhorn-r2", image_id="default/image-ubuntu")
        claim = json.loads(self.vm()["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])[0]
        self.assertEqual("longhorn-image-ubuntu", claim["spec"]["storageClassName"])
        self.assertEqual("default/image-ubuntu", claim["metadata"]["annotations"]["harvesterhci.io/imageId"])
        with self.assertRaisesRegex(ValueError, "at least as big"):
            self.create(HARVESTER, disk_gb=2, image_id="default/image-ubuntu")

    def test_other_clusters_let_cdi_choose_the_access_mode(self):
        self.create(K3S_CDI, "local-path", image_url="https://example.test/u.img")
        vm = self.vm()
        template = vm["spec"]["dataVolumeTemplates"][0]["spec"]
        self.assertEqual({"resources": {"requests": {"storage": "20Gi"}}, "storageClassName": "local-path"},
                         template["storage"])
        self.assertEqual("https://example.test/u.img", template["source"]["http"]["url"])
        self.assertNotIn("evictionStrategy", vm["spec"]["template"]["spec"])
        self.assertNotIn("annotations", vm["metadata"])

    def test_without_cdi_a_blank_disk_is_a_plain_claim(self):
        self.create(K3S_BARE, "local-path")
        method, path, claim = self.sent[0]
        self.assertEqual("/api/v1/namespaces/lab/persistentvolumeclaims", path)
        self.assertEqual(("local-path", ["ReadWriteOnce"]), (claim["spec"]["storageClassName"], claim["spec"]["accessModes"]))
        self.assertEqual({"claimName": "web-disk"}, self.vm()["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"])
        with self.assertRaisesRegex(ValueError, "CDI"):
            self.create(K3S_BARE, image_url="https://example.test/u.img")

    def test_harvester_images_are_refused_elsewhere(self):
        with self.assertRaisesRegex(ValueError, "Harvester"):
            self.create(K3S_CDI, image_id="default/image-ubuntu")
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
