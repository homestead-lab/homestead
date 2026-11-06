"""The Hosts build asks for a line per disk (DISKS.summary()) on every
snapshot cycle. It takes the disk inventory the Disks page and the alert
source keep under "disks", so a cycle costs no list of Longhorn nodes and
blockdevices of its own, and a disk write drops it (#978)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cache as CACHE
import homestead_disks as DISKS
import homestead_kube as K
from test_page_snapshot import clean_cache

INVENTORY = {"nodes": {"node-a": [{
    "device": "sda", "serial": "S1", "size_gb": 100, "role": "data", "mounts": [], "model": "m",
    "longhorn": [{"id": "d1", "path": "/var/lib/longhorn", "filesystem_key": "fs1", "capacity_gb": 90,
                  "used_gb": 10, "free_gb": 80, "reserved_gb": 5, "on_root": False, "on_node_fs": False,
                  "data_gb": 8}]}]}}


class SummaryKeptTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.now = 1_800_000_000.0
        self.enterContext(mock.patch.object(CACHE.time, "time", lambda: self.now))
        self.enterContext(mock.patch.object(DISKS.homestead_routes, "_cached", [CACHE.cached]))
        self.lists = []
        self.names = {}

        def inventory():
            import copy
            self.lists.append(1)     # a Longhorn nodes list and a blockdevices list
            self.lists.append(1)
            return copy.deepcopy(INVENTORY)
        self.enterContext(mock.patch.object(DISKS, "inventory", inventory))
        self.enterContext(mock.patch.object(DISKS, "kget", lambda path: {"items": []}))

    def test_a_cycle_reads_the_inventory_once_not_on_every_summary(self):
        for _ in range(5):
            DISKS.summary()
        self.assertEqual(2, len(self.lists), "reads: 2 lists for 5 summaries (before: 10)")

    def test_the_inventory_is_read_again_after_the_kept_time(self):
        DISKS.summary()
        self.now += DISKS.homestead_routes.PAGE_SECONDS + 1
        DISKS.summary()
        self.assertEqual(4, len(self.lists))

    def test_a_disk_write_drops_it(self):
        before = DISKS.summary()
        K.fresh_reads("PATCH", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/node-a", {}, lambda: None)
        self.assertEqual(before, DISKS.summary())
        self.assertEqual(4, len(self.lists), "read again after the write")

    def test_the_Disks_page_and_the_summary_share_one_reading(self):
        DISKS.homestead_routes.cached("disks", DISKS.homestead_routes.PAGE_SECONDS, DISKS.inventory)
        DISKS.summary()
        self.assertEqual(2, len(self.lists))

    def test_naming_a_disk_does_not_write_into_the_kept_page(self):
        names = {"sda": "fast"}
        with mock.patch.object(DISKS, "_disk_names", lambda node: names):
            with mock.patch.object(DISKS, "kget", lambda path: {"items": [{"metadata": {"name": "node-a"}}]}):
                lines = DISKS.summary()
        self.assertEqual("fast", lines["node-a"][0]["name"])
        kept = DISKS.homestead_routes.cached("disks", DISKS.homestead_routes.PAGE_SECONDS, boom)
        self.assertNotIn("name", kept["nodes"]["node-a"][0])


def boom():
    raise AssertionError("read: the kept inventory should have been served")


if __name__ == "__main__":
    unittest.main()
