"""The clusters added by address and account (Settings > Other clusters) are
changed on what is stored when they are saved, so two changes at once both
stay, and a registry that could not be read is not saved over (#621)."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_move as move
from fakes.kube import Kube
from rebind import keep_bound

MAP = "/api/v1/namespaces/lab/configmaps/homestead-clusters"


def registry(rows=None, text=None):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "homestead-clusters", "namespace": "lab", "labels": {"homestead.io/managed": "true"}},
            "data": {"clusters.json": text if text is not None else json.dumps(rows)}}


def row(name):
    return {"name": name, "url": f"http://{name}.example.com:8088", "user": "admin", "added": "2026-10-19 09:00"}


class ClusterRegistryWriteTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, move, move.NAMES)
        self.addCleanup(setattr, move, "FLEET", move.FLEET)
        move.FLEET = None

    def bind(self, send=None):
        move.bind(K.kget, send or K.send, "lab")

    def stored(self, kube):
        return [r["name"] for r in json.loads(kube.object(MAP)["data"]["clusters.json"])]

    def meanwhile(self, kube, change):
        """A send that lets another copy change the registry just before this one saves it."""
        raced = []

        def send(method, path, body=None, **options):
            if method == "PUT" and path == MAP and not raced:
                raced.append(1)
                other = kube.object(MAP)
                rows = change(json.loads(other["data"]["clusters.json"]))
                K.send("PUT", MAP, dict(other, data={"clusters.json": json.dumps(rows)}))
            return K.send(method, path, body, **options)
        return send

    def test_a_cluster_added_meanwhile_is_kept(self):
        with Kube(registry([row("attic")])) as kube:
            self.bind(self.meanwhile(kube, lambda rows: rows + [row("garage")]))
            move.add_cluster("loft", "http://loft.example.com:8088", "admin", "x")
            self.assertEqual({"attic", "garage", "loft"}, set(self.stored(kube)), "the other change was lost")

    def test_removing_one_keeps_a_cluster_added_meanwhile(self):
        with Kube(registry([row("attic"), row("loft")])) as kube:
            self.bind(self.meanwhile(kube, lambda rows: rows + [row("garage")]))
            move.remove_cluster("loft")
            self.assertEqual({"attic", "garage"}, set(self.stored(kube)))

    def test_an_unreadable_registry_is_not_saved_over(self):
        with Kube(registry(text="[{not json")) as kube:
            self.bind()
            with self.assertRaises(ValueError):
                move.add_cluster("loft", "http://loft.example.com:8088", "admin", "x")
            self.assertEqual("[{not json", kube.object(MAP)["data"]["clusters.json"])

    def test_a_registry_that_is_not_a_list_is_not_saved_over(self):
        with Kube(registry(text='{"loft": "x"}')) as kube:
            self.bind()
            with self.assertRaisesRegex(ValueError, "not a list"):
                move.remove_cluster("loft")
            self.assertEqual('{"loft": "x"}', kube.object(MAP)["data"]["clusters.json"])

    def test_adding_a_name_a_linked_cluster_keeps_is_refused(self):
        linked = dict(row("old"), user="", fleet=True, alias=True, id="m1")
        with Kube(registry([linked])) as kube:
            self.bind()
            with self.assertRaisesRegex(ValueError, "linked"):
                move.add_cluster("old", "http://old.example.com:8088", "admin", "x")
            stored = json.loads(kube.object(MAP)["data"]["clusters.json"])
            self.assertEqual([linked], stored, "the linked row was replaced")

    def test_linking_keeps_a_cluster_added_meanwhile(self):
        from unittest import mock
        fleet = mock.Mock()
        fleet.others.return_value = [{"id": "m1", "name": "old", "url": "http://old.example.com:8088"}]
        with Kube(registry([row("old")])) as kube:
            self.bind(self.meanwhile(kube, lambda rows: rows + [row("garage")]))
            move.FLEET = fleet
            move.link_legacy("old")
            rows = {r["name"]: r for r in json.loads(kube.object(MAP)["data"]["clusters.json"])}
        self.assertEqual({"old", "garage"}, set(rows))
        self.assertTrue(rows["old"]["alias"] and rows["old"]["fleet"])
        fleet.join.assert_not_called()

    def test_the_first_cluster_creates_the_registry(self):
        with Kube() as kube:
            self.bind()
            move.add_cluster("loft", "http://loft.example.com:8088", "admin", "x")
            self.assertEqual(["loft"], self.stored(kube))
            self.assertEqual("true", kube.object(MAP)["metadata"]["labels"]["homestead.io/managed"])


if __name__ == "__main__":
    unittest.main()
