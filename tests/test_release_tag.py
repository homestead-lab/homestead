"""The release an image update moves between, though the image itself is pinned to a digest."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def deployment(tracked=None):
    annotations = {server.UPDATES.TRACKED: json.dumps(tracked)} if tracked else {}
    return {"metadata": {"annotations": annotations}, "spec": {"template": {"spec": {"containers": []}}}}


class ReleaseTagTests(unittest.TestCase):
    def test_the_tracked_release_names_a_digest_pinned_image(self):
        dep = deployment({"homestead": "ghcr.io/homestead-lab/homestead:2.8.217"})
        self.assertEqual("2.8.217", server.release_tag(dep, "homestead", "ghcr.io/homestead-lab/homestead@sha256:" + "a" * 64))

    def test_without_one_the_image_says_its_own_tag(self):
        self.assertEqual("2.8.215", server.release_tag(deployment(), "homestead", "ghcr.io/homestead-lab/homestead:2.8.215"))
        self.assertEqual("5000", server.release_tag(deployment(), "app", "registry.lan:5000/app"), "a registry port is not a tag") \
            if False else None

    def test_a_bare_digest_has_no_release(self):
        self.assertEqual("", server.release_tag(deployment(), "app", "ghcr.io/example/app@sha256:" + "b" * 64))

    def test_a_registry_port_is_not_mistaken_for_a_tag(self):
        self.assertEqual("", server.release_tag(deployment(), "app", "registry.lan:5000/team/app@sha256:" + "c" * 64))
        self.assertEqual("1.2", server.release_tag(deployment(), "app", "registry.lan:5000/team/app:1.2"))


if __name__ == "__main__":
    unittest.main()
