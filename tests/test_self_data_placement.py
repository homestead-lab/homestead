"""Homestead's own data is checked for what made 2026-11-10's outage: a
shared volume with one copy of Homestead, and its only copy off the system
disk (design 3.1). Each finding names the reviewed move that fixes it."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_self_data_placement as P
import homestead_self_data_prepare as PREPARE


def lhnode(name, disks):
    """disks: {disk: (uuid, tags)}"""
    return {"metadata": {"name": name},
            "spec": {"disks": {d: {"path": f"/mnt/{d}", "tags": tags} for d, (uuid, tags) in disks.items()}},
            "status": {"diskStatus": {d: {"diskUUID": uuid} for d, (uuid, tags) in disks.items()}}}


def replica(node, disk, volume="pvc-data", failed=False):
    return {"spec": {"volumeName": volume, "nodeID": node, "diskID": disk, "diskPath": f"/mnt/{disk}-path",
                     "healthyAt": "2026-11-10T00:00:00Z", "failedAt": "x" if failed else ""}, "status": {}}


VOLUME = {"metadata": {"name": "pvc-data"}}
NODES = [lhnode("node1", {"nvme": ("u-nvme", ["os", "nvme", "ssd"]), "sata": ("u-sata", ["ssd"])})]


class FindingTests(unittest.TestCase):
    def test_a_shared_volume_with_one_copy_on_a_second_disk_is_both(self):
        found = P.findings(VOLUME, ["ReadWriteMany"], 1, [replica("node1", "u-sata")], NODES)
        self.assertEqual([{"kind": "shared"}, {"kind": "off-system-disk", "node": "node1", "path": "/mnt/u-sata-path"}], found)

    def test_what_is_already_right_is_left_alone(self):
        self.assertEqual([], P.findings(VOLUME, ["ReadWriteOnce"], 1, [replica("node1", "u-nvme")], NODES))
        # Several copies of Homestead need sharing, and two copies of the data are not "only".
        self.assertEqual([], P.findings(VOLUME, ["ReadWriteMany"], 2,
                                        [replica("node1", "u-sata"), replica("node1", "u-nvme")], NODES))

    def test_a_failed_copy_does_not_count_and_unknown_tags_offer_nothing(self):
        found = P.findings(VOLUME, ["ReadWriteOnce"], 1, [replica("node1", "u-sata"), replica("node1", "u-nvme", failed=True)], NODES)
        self.assertEqual(["off-system-disk"], [f["kind"] for f in found])
        untagged = [lhnode("node1", {"nvme": ("u-nvme", []), "sata": ("u-sata", [])})]
        self.assertEqual([], P.findings(VOLUME, ["ReadWriteOnce"], 1, [replica("node1", "u-sata")], untagged))

    def test_alerts_name_the_reviewed_move(self):
        state = {"claim": "homestead-data", "findings": P.findings(VOLUME, ["ReadWriteMany"], 1, [replica("node1", "u-sata")], NODES)}
        shared, disk = P.alert_facts(state)
        self.assertEqual(("self-data-placement:shared", "info", "/settings?tab=about"), (shared["key"], shared["severity"], shared["href"]))
        self.assertIn("homestead-data is ReadWriteMany, so it is served by a share-manager pod", shared["body"])
        self.assertIn("in Settings, About, Redundancy, choose Use a volume one host mounts", shared["body"])
        self.assertEqual(("Homestead's data is not on the system disk", "/volumes"), (disk["title"], disk["href"]))
        self.assertIn("Its only copy is on /mnt/u-sata-path on node1", disk["body"])
        self.assertIn("Move the copy to the disk tagged os with Move copies on Volumes", disk["body"])
        self.assertEqual([], P.alert_facts({"findings": []}))


class OneHostReviewTests(unittest.TestCase):
    """The choice of one host at a time is part of what is reviewed."""
    def test_one_host_is_a_reviewed_key_and_must_be_a_bool(self):
        body = {"operation": "a" * 24, "storage_class": "longhorn", "node": "node1", "one_host": "yes"}
        with self.assertRaisesRegex(Exception, "one host at a time"):
            PREPARE.review(lambda path: {}, "lab", "homestead", body, actor="admin", image="x@sha256:" + "0" * 64,
                           access_mode="ReadWriteOnce", threshold=90)
        with self.assertRaisesRegex(Exception, "destination storage class"):
            PREPARE.review(lambda path: {}, "lab", "homestead", {**body, "one_host": True, "extra": 1}, actor="admin",
                           image="x@sha256:" + "0" * 64, access_mode="ReadWriteOnce", threshold=90)


if __name__ == "__main__":
    unittest.main()
