"""An update refreshes the spare copies of an app's image (Nov 2026).

A node an app moved off keeps its image, so the app can come back there
without a download. An update left those copies on the old release, so a
failover to such a node waited for the new image. After an update, every
node that holds an old image now pulls the new one as well."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports  # noqa: F401  (binds homestead_image_cache.I)
import homestead_image_cache as IMAGES
import homestead_update_rollouts as ROLLOUTS

OLD_DB, NEW_DB = "sha256:" + "1" * 64, "sha256:" + "2" * 64
OLD_REDIS, NEW_REDIS = "sha256:" + "3" * 64, "sha256:" + "4" * 64
DB, REDIS = "ghcr.io/example/postgres@", "docker.io/valkey/valkey@"


def node(name, *refs):
    return {"metadata": {"name": name}, "status": {"images": [{"names": [ref]} for ref in refs]}}


class RefreshSparesTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [node("k1", DB + OLD_DB, REDIS + OLD_REDIS),  # runs the app
                      node("k2", DB + OLD_DB),                     # the app moved off it
                      node("k3", REDIS + NEW_REDIS, REDIS + OLD_REDIS),  # has the new redis already
                      node("k4", "docker.io/library/nginx@sha256:" + "5" * 64)]  # never ran it
        self.pulls = []
        patches = [mock.patch.object(IMAGES, "_load_scans", lambda: None),
                   mock.patch.object(IMAGES, "_SCANS", {}),
                   mock.patch.object(IMAGES.I, "kget", lambda path: {"items": self.nodes}),
                   mock.patch.object(IMAGES, "prepull", self.prepull)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def prepull(self, image, nodes=None, own=False, reuse=False):
        self.assertTrue(own, "a spare pull has a name of its own, so it never replaces another pull")
        self.pulls.append((image, sorted(nodes)))
        return {"nodes": sorted(nodes), "daemonset": f"homestead-pull-{len(self.pulls)}"}

    def test_every_node_holding_an_old_image_pulls_the_new_one(self):
        started = IMAGES.refresh_spares({DB + OLD_DB: DB + NEW_DB, REDIS + OLD_REDIS: REDIS + NEW_REDIS})
        self.assertEqual(self.pulls, [(DB + NEW_DB, ["k1", "k2"]), (REDIS + NEW_REDIS, ["k1"])])
        self.assertEqual([pull["daemonset"] for pull in started], ["homestead-pull-1", "homestead-pull-2"])

    def test_a_scanned_image_counts_as_held(self):
        # A node's status lists only its largest images; the scan has the rest.
        IMAGES._SCANS["k4"] = {"at": 0, "images": [{"repoDigests": [DB + OLD_DB], "size": 1}]}
        IMAGES.refresh_spares({DB + OLD_DB: DB + NEW_DB})
        self.assertEqual(self.pulls, [(DB + NEW_DB, ["k1", "k2", "k4"])])

    def test_an_image_not_pinned_by_digest_is_left_alone(self):
        self.assertEqual(IMAGES.refresh_spares({"docker.io/valkey/valkey:8-alpine": REDIS + NEW_REDIS}), [])
        self.assertEqual(self.pulls, [])

    def test_nodes_that_cannot_take_a_pull_skip_that_image_only(self):
        def prepull(image, nodes=None, own=False, reuse=False):
            if image == DB + NEW_DB:
                raise ValueError("k1, k2 is not ready to take an image pull")
            return self.prepull(image, nodes, own, reuse)
        with mock.patch.object(IMAGES, "prepull", prepull):
            IMAGES.refresh_spares({DB + OLD_DB: DB + NEW_DB, REDIS + OLD_REDIS: REDIS + NEW_REDIS})
        self.assertEqual(self.pulls, [(REDIS + NEW_REDIS, ["k1"])])


class CommitRefreshesSparesTests(unittest.TestCase):
    def prepared(self, action="update"):
        containers = [{"name": "immich-db", "image": DB + NEW_DB}, {"name": "redis", "image": REDIS + OLD_REDIS}]
        result = {"spec": {"template": {"spec": {"containers": containers}}}}
        return {"action": action, "current": {}, "before": {"immich-db": DB + OLD_DB, "redis": REDIS + OLD_REDIS}}, result

    def test_only_the_changed_containers_are_refreshed(self):
        prepared, result = self.prepared()
        with mock.patch.object(IMAGES, "refresh_spares", return_value=["pull"]) as refresh:
            self.assertEqual(ROLLOUTS._refresh_spares(prepared, result), ["pull"])
        self.assertEqual(refresh.call_args[0][0], {DB + OLD_DB: DB + NEW_DB})

    def test_a_failed_refresh_leaves_the_update_standing(self):
        prepared, result = self.prepared()
        with mock.patch.object(IMAGES, "refresh_spares", side_effect=RuntimeError("API down")), \
                mock.patch("builtins.print") as said:
            self.assertEqual(ROLLOUTS._refresh_spares(prepared, result), [])
        self.assertIn("spare image copies were not refreshed: API down", said.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
