"""Longhorn disk and node tags, and storage classes that choose by them."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "probe"))
import homestead_disks as DISKS
import homestead_kube as K
import homestead_storage_classes as STORAGE_CLASSES
import server  # noqa: F401 - binds the storage-class module to the app's helpers, which the tests rely on
from fakes.kube import Kube

NODES = {
    "node1": {"metadata": {"name": "node1"}, "spec": {"tags": ["rack-a"], "disks": {
        "default-disk": {"path": "/var/lib/harvester/defaultdisk", "allowScheduling": True, "tags": ["ssd"]},
        "bd-sdb": {"path": "/var/lib/harvester/extra-disks/abc", "allowScheduling": True,
                   "tags": ["hdd", "harvester-ndm-disk-remove"]}}}, "status": {}},
    "node2": {"metadata": {"name": "node2"}, "spec": {"tags": [], "disks": {
        "default-disk": {"path": "/var/lib/longhorn", "allowScheduling": True, "tags": ["ssd"]}}}, "status": {}},
    "node3": {"metadata": {"name": "node3"}, "spec": {"disks": {
        "default-disk": {"path": "/var/lib/longhorn", "allowScheduling": False, "tags": ["ssd"]}}}, "status": {}},
}
BDS = [{"metadata": {"name": "bd-sdb"}, "spec": {"nodeName": "node1", "tags": ["hdd"]}, "status": {}}]


class Cluster:
    """Longhorn's nodes (and Harvester's block devices) on the shared fake API
    (tests/fakes/kube.py), with the disk module reading and writing through it."""

    def __init__(self, test, harvester=True):
        objects = [dict(copy.deepcopy(node), apiVersion="longhorn.io/v1beta2", kind="Node",
                        metadata=dict(node["metadata"], namespace="longhorn-system")) for node in NODES.values()]
        if harvester:
            objects += [dict(copy.deepcopy(bd), apiVersion="harvesterhci.io/v1beta1", kind="BlockDevice",
                             metadata=dict(bd["metadata"], namespace="longhorn-system")) for bd in BDS]
        self.kube = test.enterContext(Kube(*objects, absent_groups=() if harvester else ("harvesterhci.io",)))
        test.enterContext(mock.patch.multiple(DISKS, kget=DISKS.kget, ksend=DISKS.ksend, temps=DISKS.temps,
                                              mutation_scope=DISKS.mutation_scope, v2_tasks=DISKS.v2_tasks))
        DISKS.bind(K.kget, K.ksend, lambda: {})

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]


class TagTests(unittest.TestCase):
    def test_tags_are_checked_and_kept_once_each(self):
        self.assertEqual(["ssd", "fast"], DISKS.clean_tags("ssd, fast ssd"))
        with self.assertRaisesRegex(ValueError, "not a tag"):
            DISKS.clean_tags(["ssd!"])

    def test_a_harvester_disk_is_tagged_on_its_block_device_too(self):
        cluster = Cluster(self)
        DISKS.set_disk_tags("node1", "bd-sdb", ["ssd", "fast"])

        paths = [(path.rsplit("/", 2)[-2], body) for method, path, body in cluster.sent]
        self.assertEqual(("blockdevices", {"spec": {"tags": ["ssd", "fast"]}}), paths[0])
        # Harvester's own removal tag is not the user's to drop.
        self.assertEqual({"spec": {"disks": {"bd-sdb": {"tags": ["ssd", "fast", "harvester-ndm-disk-remove"]}}}},
                         paths[1][1])

    def test_a_disk_harvester_does_not_manage_is_tagged_in_longhorn_alone(self):
        cluster = Cluster(self, harvester=False)
        DISKS.set_disk_tags("node1", "default-disk", [])
        self.assertEqual([("PATCH", {"spec": {"disks": {"default-disk": {"tags": []}}}})],
                         [(method, body) for method, path, body in cluster.sent])

    def test_node_tags(self):
        cluster = Cluster(self)
        DISKS.set_node_tags("node2", ["rack-b"])
        self.assertEqual({"spec": {"tags": ["rack-b"]}}, cluster.sent[0][2])
        with self.assertRaisesRegex(ValueError, "does not know"):
            DISKS.set_node_tags("node9", ["x"])

    def test_which_nodes_a_class_could_use(self):
        Cluster(self)
        # node3's ssd takes no new replicas, so it does not count.
        self.assertEqual(["node1", "node2"], DISKS.tag_reach(["ssd"]))
        self.assertEqual(["node1"], DISKS.tag_reach(["ssd"], ["rack-a"]))
        self.assertEqual([], DISKS.tag_reach(["nvme"]))


class StorageClassTagTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster(self)
        # STORAGE_CLASSES reads the cluster through the shared client here, whatever another test bound.
        self.enterContext(mock.patch.multiple(STORAGE_CLASSES, kget=K.kget, ksend=K.ksend))

    @property
    def sent(self):
        return [body for _, _, body in self.cluster.sent]

    def test_a_class_asks_for_its_tags(self):
        result = STORAGE_CLASSES.create_storage_class({"name": "longhorn-ssd", "replicas": 2, "disk_tags": ["ssd"],
                                              "node_tags": "rack-a"})
        params = self.sent[-1]["parameters"]
        self.assertEqual(("ssd", "rack-a"), (params["diskSelector"], params["nodeSelector"]))
        self.assertIn("only node1", result["message"])

    def test_a_class_no_disk_fits_says_so(self):
        result = STORAGE_CLASSES.create_storage_class({"name": "longhorn-nvme", "disk_tags": ["nvme"]})
        self.assertIn("will not schedule", result["message"])

    def test_a_class_without_tags_has_no_selectors(self):
        result = STORAGE_CLASSES.create_storage_class({"name": "longhorn-any"})
        self.assertNotIn("diskSelector", self.sent[-1]["parameters"])
        self.assertNotIn("schedule", result["message"])


if __name__ == "__main__":
    unittest.main()
