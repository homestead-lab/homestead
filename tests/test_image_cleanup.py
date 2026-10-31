"""Unused images cleaned from every host: a reviewed Clean all unused, and
an automatic round that removes images unused for some days (homestead_image_cleanup)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_image_cleanup as CLEANUP
import homestead_imports as I
import homestead_image_cache as IMAGES
import homestead_leader_state as LEADER_STATE
import homestead_shared as SHARED

DAY = 86400


def image(digest, nodes, size=100, protected=False, system=False, name=None):
    return {"digest": "sha256:" + digest * 64, "name": name or f"docker.io/library/{digest}:1", "nodes": nodes,
            "size_mb": size, "protected": protected, "system": system}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (I.SCAN_DIR, SHARED.DIR)
        I.SCAN_DIR = self.tmp.name
        SHARED.bind(self.tmp.name)
        LEADER_STATE._written.clear()
        CLEANUP._auto["at"] = 0.0
        self.images = [image("a", ["node1", "node2"], 200), image("b", ["node1"], 50),
                       image("c", ["node1"], 300, protected=True), image("d", ["node2"], 80, system=True),
                       {**image("e", ["node1"]), "digest": ""}]
        self.removed = []
        self.settings = {"auto": False, "after_days": 14}
        patches = [mock.patch.object(IMAGES, "image_cache", lambda: {"images": self.images, "complete": True}),
                   mock.patch.object(IMAGES, "cleanup_image", self.cleanup),
                   mock.patch.object(CLEANUP, "_settings", lambda: self.settings)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        I.SCAN_DIR, dir_ = self.saved
        SHARED.bind(dir_)
        LEADER_STATE._written.clear()
        self.tmp.cleanup()

    def cleanup(self, digest, nodes=None, report=None):
        self.assertIsNotNone(report, "the plan's report is reused, not read again per image")
        self.removed.append((digest, tuple(nodes)))


class CleanAllUnusedTests(Base):
    def test_the_review_lists_only_what_nothing_keeps(self):
        review = CLEANUP.plan(now=1000)
        self.assertEqual(["docker.io/library/a:1", "docker.io/library/b:1"], [i["name"] for i in review["images"]])
        # Each host holds its own copy: a on two hosts, b on one.
        self.assertEqual((2, 450.0, ["node1", "node2"]), (review["count"], review["freed_mb"], review["hosts"]))

    def test_it_removes_exactly_what_was_reviewed_from_every_host(self):
        review = CLEANUP.plan(now=1000)
        result = CLEANUP.clean_unused({"token": review["token"]})
        self.assertEqual(2, result["removed"])
        self.assertEqual({("sha256:" + "a" * 64, ("node1", "node2")), ("sha256:" + "b" * 64, ("node1",))}, set(self.removed))

    def test_a_change_since_the_review_removes_nothing(self):
        review = CLEANUP.plan(now=1000)
        self.images.append(image("f", ["node3"]))
        with self.assertRaisesRegex(ValueError, "changed since the review"):
            CLEANUP.clean_unused({"token": review["token"]})
        with self.assertRaisesRegex(ValueError, "review the unused images first"):
            CLEANUP.clean_unused({})
        self.assertEqual([], self.removed)

    def test_an_image_needed_again_meanwhile_is_skipped_not_fatal(self):
        def refuse(digest, nodes=None, report=None):
            if digest.startswith("sha256:a"):
                raise PermissionError("image is protected by running:lab/web")
            self.removed.append((digest, tuple(nodes)))
        with mock.patch.object(IMAGES, "cleanup_image", refuse):
            result = CLEANUP.clean_unused({"token": CLEANUP.plan()["token"]})
        self.assertEqual((1, "docker.io/library/a:1"), (result["removed"], result["skipped"][0]["name"]))


class AutomaticTests(Base):
    def test_unused_since_is_when_homestead_first_saw_it_and_forgets_reused_images(self):
        CLEANUP.track({"images": self.images}, now=1000)
        CLEANUP.track({"images": self.images}, now=5000)
        with open(os.path.join(self.tmp.name, CLEANUP.STORE), encoding="utf-8") as handle:
            since = json.load(handle)
        self.assertEqual({"sha256:" + "a" * 64: 1000, "sha256:" + "b" * 64: 1000}, since)
        self.images[0]["protected"] = True
        self.assertEqual(["sha256:" + "b" * 64], list(CLEANUP.track({"images": self.images}, now=6000)))

    def test_unused_since_is_kept_while_a_record_that_keeps_every_image_cannot_be_read(self):
        # Wrap-up row 80: an unreadable record keeps every image, so nothing is
        # unused for a while; that must not restart every image's clock.
        unreadable = {"reason": "unreadable", "holds_all": True, "namespace": "lab", "workload": "web",
                      "container": "update-previous", "ref": "", "digest": ""}
        CLEANUP.track({"images": self.images}, now=1000)            # a and b unused since 1000
        held = [{**i, "protected": True, "retained_by": [unreadable]} for i in self.images]
        held.append({**image("f", ["node1"]), "protected": True, "retained_by": [unreadable]})
        self.assertEqual({}, CLEANUP.track({"images": held}, now=5000))
        # Read again: a and b keep their dates; f, never seen unused, starts now.
        self.images.append(image("f", ["node1"]))
        self.assertEqual({"sha256:" + "a" * 64: 1000, "sha256:" + "b" * 64: 1000, "sha256:" + "f" * 64: 9000},
                         CLEANUP.track({"images": self.images}, now=9000))
        # An image something readable keeps is used again, unreadable or not.
        held[0]["retained_by"] = [unreadable, {"reason": "active", "namespace": "lab", "workload": "web",
                                               "container": "web", "ref": "", "digest": ""}]
        CLEANUP.track({"images": held}, now=9500)
        self.assertEqual({"sha256:" + "b" * 64: 1000, "sha256:" + "f" * 64: 9000},
                         CLEANUP.track({"images": self.images[1:]}, now=10000))
        self.assertEqual({"sha256:" + "a" * 64: 10500, "sha256:" + "b" * 64: 1000, "sha256:" + "f" * 64: 9000},
                         CLEANUP.track({"images": self.images}, now=10500))

    def test_off_it_only_tracks(self):
        self.assertIsNone(CLEANUP.auto_tick(now=100 * DAY))
        self.assertEqual([], self.removed)

    def test_on_it_removes_images_unused_for_its_days_and_says_so(self):
        self.settings = {"auto": True, "after_days": 14}
        CLEANUP.track({"images": self.images[:1]}, now=0)          # a unused since day 0
        CLEANUP.track({"images": self.images}, now=10 * DAY)        # b since day 10
        result = CLEANUP.auto_tick(now=15 * DAY)
        self.assertEqual([("sha256:" + "a" * 64, ("node1", "node2"))], self.removed)
        self.assertEqual((1, 400.0), (result["removed"], result["freed_mb"]))
        self.assertEqual(1, CLEANUP.status()["last"]["removed"])
        # Once an hour, not every minute.
        self.assertIsNone(CLEANUP.auto_tick(now=15 * DAY + 60))

    def test_the_setting_is_checked(self):
        import homestead_app_settings as APP
        self.assertEqual({"auto": False, "after_days": 14}, APP.validate({})["image_cleanup"])
        self.assertEqual({"auto": True, "after_days": 30}, APP.validate({"image_cleanup": {"auto": True, "after_days": 30}})["image_cleanup"])
        for bad in ({"auto": "yes"}, {"after_days": 0}, {"after_days": 400}, {"after_days": "soon"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                APP.validate({"image_cleanup": bad})


if __name__ == "__main__":
    unittest.main()
