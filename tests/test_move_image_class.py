import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move_engine as ENGINE

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def klass(name, default=False, backing=""):
    return {"metadata": {"name": name, "annotations": (
                {"storageclass.kubernetes.io/is-default-class": "true"} if default else {})},
            "provisioner": "driver.longhorn.io",
            "parameters": {"backingImage": backing} if backing else {}}


class Longhorn:
    STORAGE_CLASS = ""


class ImageClassTests(unittest.TestCase):
    """Harvester's webhook refused a restored image - "no default
    storageClass found for backingImage" - on a cluster with no default."""

    def setUp(self):
        self.saved = ENGINE.kget, ENGINE.ksend, ENGINE.LH, ENGINE._get
        self.sent = []
        ENGINE.LH = Longhorn()

    def tearDown(self):
        ENGINE.kget, ENGINE.ksend, ENGINE.LH, ENGINE._get = self.saved

    def classes(self, *rows):
        ENGINE.kget = lambda path: {"items": list(rows)}

    def test_homesteads_own_class_is_used_when_it_is_here(self):
        ENGINE.LH.STORAGE_CLASS = "longhorn-r2"
        self.classes(klass("harvester-longhorn"), klass("longhorn-r2"))
        self.assertEqual("longhorn-r2", ENGINE._image_class())

    def test_with_no_default_harvesters_stock_class_is_used(self):
        self.classes(klass("longhorn-ubuntu", backing="ubuntu"), klass("harvester-longhorn"), klass("longhorn"))
        self.assertEqual("harvester-longhorn", ENGINE._image_class())

    def test_an_images_own_class_is_never_chosen(self):
        self.classes(klass("longhorn-ubuntu", backing="ubuntu"), klass("fast"))
        self.assertEqual("fast", ENGINE._image_class())

    def test_the_restored_image_names_its_class(self):
        self.classes(klass("harvester-longhorn"))
        ENGINE._get = lambda path: ({"status": {"url": "s3://bucket/backupbackingimages/ubuntu"}}
                                    if "backupbackingimages" in path else None)
        ENGINE.ksend = lambda method, path, body=None, **kw: self.sent.append((method, path, body))

        ENGINE._ensure_image({"id": "m1"}, "ubuntu")

        method, path, body = self.sent[0]
        self.assertEqual(("POST", f"{LH}/backingimages"), (method, path))
        self.assertEqual("harvester-longhorn",
                         body["metadata"]["annotations"]["harvesterhci.io/storageClassName"])


if __name__ == "__main__":
    unittest.main()
