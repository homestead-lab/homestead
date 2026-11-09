"""What the VM store keeps is changed on the record as stored when it is
saved: a change made meanwhile (another admin keeping an image, or the
leader's twice-daily refresh) stays, and a download or deletion already
made is recorded rather than lost to a 409. A record that cannot be read is
not saved over, and nothing is downloaded or deleted for it (#621)."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_vmstore as STORE
from fakes.kube import Kube

MAP = "/api/v1/namespaces/lab/configmaps/homestead-vm-store"
UBUNTU = STORE.BY_ID["ubuntu-24.04"]["urls"]["amd64"]
BUILD = {"etag": "aaa", "modified": "Sat, 26 Sep 2026 13:13:58 GMT", "size": 600}


def store_map(kept=None, text=None):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "homestead-vm-store", "namespace": "lab", "labels": {"homestead.io/managed": "true"}},
            "data": {"store.json": text if text is not None else json.dumps({"kept": kept or {}})}}


class VmStoreWriteTests(unittest.TestCase):
    def setUp(self):
        saved = (STORE.kget, STORE.ksend, STORE.NS, STORE.platform, STORE.hv_download, STORE.images,
                 STORE.image_disks, STORE.delete_image, STORE.fetch_json, STORE.head, STORE.fetch_text)
        self.addCleanup(lambda: STORE.bind(*saved))
        STORE._heads.clear()
        self.images, self.downloads, self.deleted, self.disks = [], [], [], {}
        self.builds = {UBUNTU: dict(BUILD)}

    def bind(self, send=None):
        STORE.bind(K.kget, send or K.send, "lab", lambda force=False: {"harvester": True, "arch": ["amd64"]},
                   self.download, lambda: list(self.images), lambda: self.disks, self.delete,
                   lambda url: [], lambda url: dict(self.builds.get(url) or {}), lambda url: "")

    def download(self, url, display, reuse):
        name = f"image-{len(self.images) + 1}"
        self.images.append({"namespace": "lab", "name": name, "display": display, "ready": False, "url": url})
        self.downloads.append(url)
        return {"namespace": "lab", "name": name, "reused": False}

    def delete(self, ns, name):
        self.deleted.append(f"{ns}/{name}")
        self.images = [i for i in self.images if i["name"] != name]

    def kept(self, kube):
        return json.loads(kube.object(MAP)["data"]["store.json"])["kept"]

    def meanwhile(self, kube, change):
        """A send that lets another writer change the record just before this one saves it."""
        raced = []

        def send(method, path, body=None, **options):
            if method in ("PUT", "POST") and path.startswith(MAP.rsplit("/", 1)[0]) and not raced:
                raced.append(1)
                other = kube.object(MAP)
                if other is None:
                    K.send("POST", MAP.rsplit("/", 1)[0], store_map(change({})))
                else:
                    data = json.loads(other["data"]["store.json"])
                    K.send("PUT", MAP, dict(other, data={"store.json": json.dumps(dict(data, kept=change(data["kept"])))}))
            return K.send(method, path, body, **options)
        return send

    def test_keeping_one_keeps_an_image_kept_meanwhile_and_downloads_once(self):
        with Kube(store_map()) as kube:
            self.bind(self.meanwhile(kube, lambda kept: dict(kept, **{"debian-12": {"auto": True, "versions": []}})))
            STORE.keep("ubuntu-24.04")
            self.assertEqual({"debian-12", "ubuntu-24.04"}, set(self.kept(kube)), "the other admin's image was lost")
            self.assertEqual(["lab/image-1"], [v["image"] for v in self.kept(kube)["ubuntu-24.04"]["versions"]])
            self.assertEqual([UBUNTU], self.downloads, "downloaded once, however often the record is saved")

    def test_the_first_image_kept_keeps_one_kept_meanwhile(self):
        with Kube() as kube:
            self.bind(self.meanwhile(kube, lambda kept: dict(kept, **{"debian-12": {"auto": True, "versions": []}})))
            STORE.keep("ubuntu-24.04")
            self.assertEqual({"debian-12", "ubuntu-24.04"}, set(self.kept(kube)))
            self.assertEqual("true", kube.object(MAP)["metadata"]["labels"]["homestead.io/managed"])

    def test_forgetting_one_keeps_an_image_kept_meanwhile_and_deletes_its_builds_once(self):
        self.images = [{"namespace": "lab", "name": "image-1", "ready": True, "url": UBUNTU}]
        kept = {"ubuntu-24.04": {"auto": True, "versions": [{"image": "lab/image-1", "added": 1}]}}
        with Kube(store_map(kept)) as kube:
            self.bind(self.meanwhile(kube, lambda kept: dict(kept, **{"debian-12": {"auto": True, "versions": []}})))
            STORE.forget("ubuntu-24.04")
            self.assertEqual({"debian-12"}, set(self.kept(kube)))
            self.assertEqual(["lab/image-1"], self.deleted)

    def test_switching_auto_keeps_a_change_made_meanwhile(self):
        kept = {"ubuntu-24.04": {"auto": True, "versions": []}}
        with Kube(store_map(kept)) as kube:
            self.bind(self.meanwhile(kube, lambda kept: dict(kept, **{"debian-12": {"auto": True, "versions": []}})))
            STORE.set_auto("ubuntu-24.04", False)
            self.assertEqual({"debian-12", "ubuntu-24.04"}, set(self.kept(kube)))
            self.assertFalse(self.kept(kube)["ubuntu-24.04"]["auto"])

    def test_a_new_build_is_recorded_when_the_store_changed_meanwhile(self):
        self.images = [{"namespace": "lab", "name": "image-1", "ready": True, "url": UBUNTU}]
        kept = {"ubuntu-24.04": {"auto": True, "checked": 0, "versions": [{"image": "lab/image-1", "added": 1, **BUILD}]}}
        self.builds[UBUNTU] = {"etag": "bbb", "modified": "Mon, 28 Sep 2026 09:00:00 GMT", "size": 610}
        with Kube(store_map(kept)) as kube:
            self.bind(self.meanwhile(kube, lambda kept: dict(kept, **{"debian-12": {"auto": True, "versions": []}})))
            self.assertEqual(["Ubuntu 24.04 LTS"], STORE.refresh(force=True)["updated"])
            stored = self.kept(kube)
            self.assertEqual({"debian-12", "ubuntu-24.04"}, set(stored))
            self.assertEqual(["lab/image-1", "lab/image-2"], [v["image"] for v in stored["ubuntu-24.04"]["versions"]],
                             "the new build was downloaded but not recorded")
            self.assertEqual([UBUNTU], self.downloads)

    def test_a_refresh_keeps_a_build_recorded_meanwhile(self):
        self.images = [{"namespace": "lab", "name": "image-1", "ready": True, "url": UBUNTU}]
        kept = {"ubuntu-24.04": {"auto": True, "checked": 0, "versions": [{"image": "lab/image-1", "added": 1, **BUILD}]}}
        newer = {"image": "lab/image-9", "added": 2, "etag": "ccc"}

        def change(kept):
            kept["ubuntu-24.04"]["versions"].append(newer)
            return kept
        with Kube(store_map(kept)) as kube:
            self.bind(self.meanwhile(kube, change))
            STORE.refresh(force=True)
            self.assertIn("lab/image-9", [v["image"] for v in self.kept(kube)["ubuntu-24.04"]["versions"]])

    def test_an_unreadable_record_is_not_saved_over_and_nothing_is_downloaded(self):
        with Kube(store_map(text="{broken")) as kube:
            self.bind()
            for change in (lambda: STORE.keep("ubuntu-24.04"), lambda: STORE.set_auto("ubuntu-24.04", False),
                           lambda: STORE.forget("ubuntu-24.04"), lambda: STORE.refresh(force=True)):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    change()
            self.assertEqual("{broken", kube.object(MAP)["data"]["store.json"])
            self.assertEqual([], self.downloads)

    def test_an_empty_record_is_refused_before_anything_is_downloaded(self):
        with Kube(store_map(text="")) as kube:
            self.bind()
            with self.assertRaisesRegex(ValueError, "not written by Homestead"):
                STORE.keep("ubuntu-24.04")
            self.assertEqual("", kube.object(MAP)["data"]["store.json"])
            self.assertEqual([], self.downloads)

    def test_keeping_an_image_found_by_its_address_keeps_what_was_recorded_meanwhile(self):
        """Found here already, the build is recorded; another writer that
        recorded it meanwhile with its etag keeps the etag, or the refresh
        would download it again."""
        self.images = [{"namespace": "lab", "name": "image-old", "ready": True, "url": UBUNTU, "created": 1}]

        def change(kept):
            kept["ubuntu-24.04"] = {"auto": True, "versions": [{"image": "lab/image-old", "added": 1, **BUILD}]}
            return kept
        with Kube(store_map()) as kube:
            self.bind(self.meanwhile(kube, change))
            STORE.keep("ubuntu-24.04")
            [version] = self.kept(kube)["ubuntu-24.04"]["versions"]
            self.assertEqual("aaa", version.get("etag"), "the build recorded meanwhile lost its etag")
            self.assertEqual([], self.downloads)

    def test_a_record_that_is_not_an_object_is_not_saved_over(self):
        for text in ('["ubuntu-24.04"]', '{"kept": ["ubuntu-24.04"]}', '{"kept": {"ubuntu-24.04": "yes"}}'):
            with self.subTest(text=text), Kube(store_map(text=text)) as kube:
                self.bind()
                with self.assertRaises(ValueError):
                    STORE.keep("ubuntu-24.04")
                self.assertEqual(text, kube.object(MAP)["data"]["store.json"])
                self.assertEqual([], self.downloads)


if __name__ == "__main__":
    unittest.main()
