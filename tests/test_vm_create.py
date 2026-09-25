import json
import sys
import unittest
import unittest.mock
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

    def test_a_vm_can_ask_for_its_console_to_be_kept_and_others_do_not(self):
        self.create(HARVESTER, "harvester-longhorn", log_console=True)
        self.assertTrue(self.vm()["spec"]["template"]["spec"]["domain"]["devices"]["logSerialConsole"])
        self.sent.clear()
        self.create(HARVESTER, "harvester-longhorn", name="plain")
        self.assertNotIn("logSerialConsole", self.vm()["spec"]["template"]["spec"]["domain"]["devices"])

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

    def test_on_harvester_a_url_becomes_a_harvester_image(self):
        # CDI's importer cannot be given Harvester's shared block volumes; its
        # own image download is what works there.
        self.objects["/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachineimages"] = {"items": []}
        self.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"] = {
            "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "2", "migratable": "true"}}
        self.harvester_names_classes_later()
        self.create({"harvester": True, "cdi": False}, "longhorn-r2",
                    image_url="https://cloud-images.ubuntu.com/minimal/releases/resolute/release/ubuntu-26.04-minimal-cloudimg-amd64.img")
        image = next(b for m, p, b in self.sent if p.endswith("/virtualmachineimages"))
        self.assertEqual(("download", "ubuntu-26.04-minimal-cloudimg-amd64.img", "2", "true"),
                         (image["spec"]["sourceType"], image["spec"]["displayName"],
                          image["spec"]["storageClassParameters"]["numberOfReplicas"],
                          image["spec"]["storageClassParameters"]["migratable"]))
        vm = self.vm()
        self.assertNotIn("dataVolumeTemplates", vm["spec"])
        claim = json.loads(vm["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])[0]
        name = image["metadata"]["name"]
        self.assertEqual(("lh-8b6ca866-806d-430a-8fd4-d584f0f06128", f"lab/{name}"),
                         (claim["spec"]["storageClassName"], claim["metadata"]["annotations"]["harvesterhci.io/imageId"]))

    def harvester_names_classes_later(self, klass="lh-8b6ca866-806d-430a-8fd4-d584f0f06128", after=2):
        """Harvester reports an image's storage class in its status a moment
        after the image is made - under a name of its choosing."""
        real_get, asked = self.objects, {"n": 0}
        base = "/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachineimages/"

        def get(path):
            if path.startswith(base):
                asked["n"] += 1
                return {"metadata": {"name": path[len(base):]},
                        "status": {"storageClassName": klass} if klass and asked["n"] >= after else {}}
            if path in real_get:
                return real_get[path]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        imports.kget = get
        self.addCleanup(setattr, imports.HVIMAGE, "CLASS_WAIT_SECONDS", imports.HVIMAGE.CLASS_WAIT_SECONDS)
        imports.HVIMAGE.CLASS_WAIT_SECONDS = 5
        patcher = unittest.mock.patch("time.sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_downloaded_images_class_is_harvesters_never_a_guess(self):
        """The disk's claim asked for longhorn-image-151065, which did not exist
        - Harvester had named it lh-8b6ca866-... - so the VM sat unschedulable:
        "pod has unbound immediate PersistentVolumeClaims"."""
        self.objects["/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachineimages"] = {"items": []}
        self.harvester_names_classes_later()
        self.create(HARVESTER, "longhorn-r2", image_url="https://example.test/noble.img")
        claim = json.loads(self.vm()["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])[0]
        self.assertEqual("lh-8b6ca866-806d-430a-8fd4-d584f0f06128", claim["spec"]["storageClassName"])
        self.assertFalse(claim["spec"]["storageClassName"].startswith("longhorn-image-"))

    def test_a_reused_image_without_its_class_yet_is_waited_for(self):
        self.objects["/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachineimages"] = {"items": [
            {"metadata": {"name": "image-151065"}, "spec": {"url": "https://example.test/noble.img"}, "status": {}}]}
        self.harvester_names_classes_later()
        self.create(HARVESTER, "longhorn-r2", image_url="https://example.test/noble.img")
        claim = json.loads(self.vm()["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])[0]
        self.assertEqual("lh-8b6ca866-806d-430a-8fd4-d584f0f06128", claim["spec"]["storageClassName"])

    def test_no_vm_is_made_while_harvester_has_no_class_for_the_image(self):
        self.objects["/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachineimages"] = {"items": []}
        self.harvester_names_classes_later(klass="")
        with self.assertRaisesRegex(ValueError, "not set up the storage"):
            self.create(HARVESTER, "longhorn-r2", image_url="https://example.test/noble.img")
        self.assertFalse([p for m, p, b in self.sent if p.endswith("/virtualmachines")])

    def test_the_same_url_is_not_downloaded_twice(self):
        self.objects["/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachineimages"] = {"items": [
            {"metadata": {"name": "image-abc"}, "spec": {"url": "https://example.test/u.img", "displayName": "u.img"},
             "status": {"storageClassName": "longhorn-image-abc"}}]}
        self.create(HARVESTER, "longhorn-r2", image_url="https://example.test/u.img")
        self.assertFalse([p for m, p, b in self.sent if p.endswith("/virtualmachineimages")])
        claim = json.loads(self.vm()["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])[0]
        self.assertEqual("longhorn-image-abc", claim["spec"]["storageClassName"])

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

    def test_a_file_name_is_not_a_download_address(self):
        with self.assertRaisesRegex(ValueError, "not a download address"):
            self.create(K3S_CDI, image_url="ubuntu-26.04-minimal-cloudimg-amd64.img")
        self.assertEqual([], self.sent)

    def test_harvester_images_are_refused_elsewhere(self):
        with self.assertRaisesRegex(ValueError, "Harvester"):
            self.create(K3S_CDI, image_id="default/image-ubuntu")
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
