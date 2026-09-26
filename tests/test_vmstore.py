import json, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vmstore as STORE

FEDORA = [
    {"version": "44", "arch": "x86_64", "variant": "Cloud", "subvariant": "Cloud_Base",
     "link": "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"},
    {"version": "44", "arch": "x86_64", "variant": "Cloud", "subvariant": "Cloud_Base_UKI",
     "link": "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-UEFI-UKI-44-1.7.x86_64.qcow2"},
    {"version": "45 Beta", "arch": "x86_64", "variant": "Cloud", "subvariant": "Cloud_Base",
     "link": "https://download.fedoraproject.org/pub/fedora/linux/releases/test/45_Beta/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-45_Beta-1.3.x86_64.qcow2"},
    {"version": "43", "arch": "x86_64", "variant": "Cloud", "subvariant": "Cloud_Base",
     "link": "https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2"},
]
UBUNTU = STORE.BY_ID["ubuntu-24.04"]["urls"]["amd64"]


class Cluster:
    def __init__(self, harvester=True):
        self.cm, self.images, self.deleted, self.downloads = None, [], [], []
        self.builds = {UBUNTU: {"etag": "aaa", "modified": "Sat, 26 Sep 2026 13:13:58 GMT", "size": 600}}
        self.disks = {}
        self.harvester = harvester
        STORE._heads.clear()
        STORE._fedora.update(at=0.0, value={})
        STORE.bind(self.get, self.send, "lab", lambda force=False: {"harvester": self.harvester, "arch": ["amd64"]},
                   self.download, lambda: list(self.images), lambda: self.disks, self.delete,
                   lambda url: FEDORA, lambda url: dict(self.builds[url]))

    def get(self, path):
        if self.cm is None:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return json.loads(json.dumps(self.cm))

    def send(self, method, path, body=None, **kw):
        self.cm = dict(body, metadata=dict(body["metadata"], resourceVersion="1"))

    def download(self, url, display, reuse):
        name = f"image-{len(self.images) + 1}"
        self.images.append({"namespace": "lab", "name": name, "display": display, "ready": False, "progress": 10})
        self.downloads.append((url, display, reuse))
        return {"namespace": "lab", "name": name, "reused": False}

    def delete(self, ns, name):
        self.deleted.append(f"{ns}/{name}")
        self.images = [i for i in self.images if i["name"] != name]


class StoreTests(unittest.TestCase):
    def test_fedora_is_its_newest_release_not_a_beta_or_uki(self):
        Cluster()
        self.assertIn("Fedora-Cloud-Base-Generic-44-1.7.x86_64", STORE.url_for(STORE.BY_ID["fedora"]))

    def test_keeping_downloads_once_and_a_new_vm_waits_for_it_to_be_ready(self):
        c = Cluster()
        STORE.keep("ubuntu-24.04")
        self.assertEqual([(UBUNTU, "Ubuntu 24.04 LTS · 2026-09-26", True)], c.downloads)
        self.assertEqual({"image_url": UBUNTU, "min_gb": 10, "user": "ubuntu"}, STORE.source_for("ubuntu-24.04"),
                         "until the copy is ready, a VM downloads from the publisher")
        c.images[0]["ready"] = True
        self.assertEqual("lab/image-1", STORE.source_for("ubuntu-24.04")["image_id"])
        self.assertTrue(next(r for r in STORE.choices() if r["id"] == "ubuntu-24.04")["ready"])

    def test_a_new_build_downloads_beside_the_old_which_goes_once_nothing_uses_it(self):
        c = Cluster()
        STORE.keep("ubuntu-24.04")
        c.images[0]["ready"] = True
        self.assertEqual([], STORE.refresh(force=True)["updated"], "the same build is not fetched again")
        c.builds[UBUNTU] = {"etag": "bbb", "modified": "Mon, 28 Sep 2026 09:00:00 GMT", "size": 610}
        self.assertEqual(["Ubuntu 24.04 LTS"], STORE.refresh(force=True)["updated"])
        self.assertEqual((UBUNTU, "Ubuntu 24.04 LTS · 2026-09-28", False), c.downloads[-1])
        self.assertTrue(next(r for r in STORE.view()["images"] if r["id"] == "ubuntu-24.04")["versions"])
        # The old build still has a disk: kept. Then the disk goes.
        c.images[1]["ready"] = True
        c.disks = {"lab/image-1": ["lab/web-1-disk"]}
        self.assertEqual([], STORE.refresh(force=True)["tidied"])
        c.disks = {}
        self.assertEqual(["lab/image-1"], STORE.refresh(force=True)["tidied"])
        self.assertEqual("lab/image-2", STORE.source_for("ubuntu-24.04")["image_id"])

    def test_elsewhere_a_vm_always_starts_from_the_publisher(self):
        c = Cluster(harvester=False)
        STORE.keep("debian-12")
        self.assertEqual([], c.downloads)
        self.assertIn("debian-12-genericcloud-amd64", STORE.source_for("debian-12")["image_url"])


if __name__ == "__main__":
    unittest.main()
