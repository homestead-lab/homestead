import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_root_guard as GUARD

GB = 1024 ** 3
ROOT_ONLY = [{"mountpoint": "/", "disk": "sda"}, {"mountpoint": "/boot", "disk": "sda"}]
OWN_VOLUME = ROOT_ONLY + [{"mountpoint": "/var/lib/longhorn", "disk": "sda"}]


def lh_node(free_gb, allow=True, total_gb=100, path="/var/lib/longhorn/", disk_type="filesystem"):
    return {"metadata": {"name": "k3s-1"},
            "spec": {"disks": {"default-disk": {"path": path, "allowScheduling": allow, "diskType": disk_type}}},
            "status": {"diskStatus": {"default-disk": {"storageMaximum": total_gb * GB, "storageAvailable": free_gb * GB}}}}


class RootGuardTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.sent = []
        self.node, self.mounts = lh_node(50), ROOT_ONLY
        GUARD.bind(lambda path: {"items": [self.node]}, lambda *a, **k: self.sent.append(a[2]["spec"]["disks"]),
                   lambda force=False: {"distribution": "k3s", "longhorn": True},
                   lambda: {"k3s-1": {"mounts": self.mounts}}, self.data)

    def test_a_disk_on_the_root_filesystem_stops_taking_copies_when_it_runs_low(self):
        self.assertEqual([], GUARD.tick())
        self.node = lh_node(12)
        self.assertIn("stopped placing copies", GUARD.tick()[0][1])
        self.assertEqual([{"default-disk": {"allowScheduling": False}}], self.sent)
        self.assertEqual(["rootguard:k3s-1/default-disk"], [f["key"] for f in GUARD.alert_facts()])

    def test_it_starts_again_only_once_there_is_room_and_only_if_homestead_stopped_it(self):
        self.node = lh_node(12)
        GUARD.tick()
        self.node = lh_node(17, allow=False)
        self.assertEqual([], GUARD.tick(), "between the two floors nothing changes")
        self.node = lh_node(30, allow=False)
        GUARD.tick()
        self.assertEqual({"default-disk": {"allowScheduling": True}}, self.sent[-1])
        self.assertEqual([], GUARD.alert_facts())
        sent = len(self.sent)
        self.node = lh_node(80, allow=False)
        GUARD.tick()
        self.assertEqual(sent, len(self.sent), "a disk someone stopped by hand stays stopped")

    def test_a_volume_of_its_own_and_v2_devices_are_left_alone(self):
        for node, mounts in ((lh_node(1), OWN_VOLUME), (lh_node(1, path="/dev/vg/longhorn-v2", disk_type="block"), ROOT_ONLY)):
            self.node, self.mounts = node, mounts
            self.assertEqual([], GUARD.tick())
        self.assertEqual([], self.sent)

    def test_without_the_probe_nothing_is_decided(self):
        self.node, self.mounts = lh_node(1), []
        self.assertEqual([], GUARD.tick())
        self.assertEqual([], self.sent)

    def test_the_floor_is_a_share_or_ten_gb_whichever_is_more(self):
        self.assertEqual(10 * GB, GUARD.floor(40 * GB, GUARD.STOP))
        self.assertEqual(150 * GB, GUARD.floor(1000 * GB, GUARD.STOP))


if __name__ == "__main__":
    unittest.main()
