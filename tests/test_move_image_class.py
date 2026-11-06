import io, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move_state as STATE
import homestead_move_phases as PHASE
import homestead_storage_classes as STORAGE_CLASSES
from unittest import mock

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def klass(name, default=False, backing=""):
    return {"metadata": {"name": name, "annotations": (
                {"storageclass.kubernetes.io/is-default-class": "true"} if default else {})},
            "provisioner": "driver.longhorn.io",
            "parameters": {"backingImage": backing} if backing else {}}


class Longhorn:
    pass


class ImageClassTests(unittest.TestCase):
    """Harvester's webhook refused a restored image - "no default
    storageClass found for backingImage" - on a cluster with no default."""

    def setUp(self):
        self.saved = STATE.kget, STATE.ksend, STATE.LH
        self.sent = []
        STATE.LH = Longhorn()

    def tearDown(self):
        STATE.kget, STATE.ksend, STATE.LH = self.saved

    def classes(self, *rows):
        STATE.kget = lambda path: {"items": list(rows)}

    def test_homesteads_own_class_is_used_when_it_is_here(self):
        self.enterContext(mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", "longhorn-r2"))
        self.classes(klass("harvester-longhorn"), klass("longhorn-r2"))
        self.assertEqual("longhorn-r2", PHASE._image_class())

    def test_with_no_default_harvesters_stock_class_is_used(self):
        self.classes(klass("longhorn-ubuntu", backing="ubuntu"), klass("harvester-longhorn"), klass("longhorn"))
        self.assertEqual("harvester-longhorn", PHASE._image_class())

    def test_an_images_own_class_is_never_chosen(self):
        self.classes(klass("longhorn-ubuntu", backing="ubuntu"), klass("fast"))
        self.assertEqual("fast", PHASE._image_class())

    def test_an_unread_class_list_is_said_and_waited_on_not_called_empty(self):
        def unread(path):
            raise urllib.error.URLError("timed out")
        STATE.kget = unread
        with self.assertRaisesRegex(RuntimeError, "could not read the storage classes.*timed out") as raised:
            PHASE._image_class()
        # Not a ValueError: that would end the move; a read that failed is asked again.
        self.assertNotIsInstance(raised.exception, ValueError)

    def test_the_restored_image_names_its_class(self):
        classes = {"items": [klass("harvester-longhorn")]}

        def read(path):
            if "backupbackingimages" in path:
                return {"status": {"url": "s3://bucket/backupbackingimages/ubuntu"}}
            if "/backingimages/" in path:
                raise urllib.error.HTTPError(path, 404, "not found", {}, io.BytesIO(b""))
            return classes
        STATE.kget = read
        STATE.ksend = lambda method, path, body=None, **kw: self.sent.append((method, path, body))

        PHASE._ensure_image({"id": "m1"}, "ubuntu")

        method, path, body = self.sent[0]
        self.assertEqual(("POST", f"{LH}/backingimages"), (method, path))
        self.assertEqual("harvester-longhorn",
                         body["metadata"]["annotations"]["harvesterhci.io/storageClassName"])


if __name__ == "__main__":
    unittest.main()
