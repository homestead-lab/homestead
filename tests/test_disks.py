import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "probe"))
import homestead_disks as DISKS

GiB = 1024 ** 3


def lh_node(disks, status):
    return {"metadata": {"name": "node1"}, "spec": {"disks": disks}, "status": {"diskStatus": status}}


BD_SDB = {"metadata": {"name": "bd-sdb"},
          "spec": {"nodeName": "node1", "devPath": "/dev/sdb", "provision": False, "fileSystem": {}},
          "status": {"state": "Active", "provisionPhase": "Unprovisioned",
                     "deviceStatus": {"devPath": "/dev/sdb", "capacity": {"sizeBytes": 2000 * GiB},
                                      "details": {"deviceType": "disk", "vendor": "ATA", "model": "IronWolf"},
                                      "fileSystem": {"type": "ext4"}}}}
PROBE = {"node1": {"disks": [{"name": "nvme0n1", "size_gb": 465.8, "model": "Samsung", "kind": "NVMe"},
                             {"name": "sdb", "size_gb": 1863, "model": "IronWolf", "kind": "HDD"}],
                   "mounts": [{"disk": "nvme0n1", "mountpoint": "/"},
                              {"disk": "nvme0n1", "mountpoint": "/var/lib/harvester/defaultdisk"}]}}
LH_NODE = lh_node({"default-disk": {"path": "/var/lib/harvester/defaultdisk", "allowScheduling": True}},
                  {"default-disk": {"storageMaximum": 117 * GiB, "storageAvailable": 90 * GiB,
                                    "storageScheduled": 99 * GiB, "scheduledReplica": {"r1": 1, "r2": 1}}})


class Cluster:
    def __init__(self, harvester=True, lh=None):
        self.bds = [copy.deepcopy(BD_SDB)] if harvester else None
        self.lh = copy.deepcopy(lh or LH_NODE)
        self.sent = []
        DISKS.bind(self.get, self.send, lambda: PROBE)

    def get(self, path):
        if path == DISKS.BD:
            if self.bds is None:
                raise urllib.error.HTTPError(path, 404, "no", None, None)
            return {"items": self.bds}
        if path.startswith(DISKS.BD + "/"):
            return next(b for b in self.bds if b["metadata"]["name"] == path.rsplit("/", 1)[1])
        if path.endswith("/nodes"):
            return {"items": [self.lh]}
        if path.endswith("/nodes/node1"):
            return self.lh
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        return body


class DiskTests(unittest.TestCase):
    def test_every_disk_is_listed_with_what_it_is_for(self):
        Cluster()
        inv = DISKS.inventory()
        system, spare = inv["nodes"]["node1"]
        self.assertEqual(("nvme0n1", True, "longhorn"), (system["device"], system["system"], system["role"]))
        self.assertEqual((27.0, 2), (system["longhorn"][0]["used_gb"], system["longhorn"][0]["replicas"]))
        self.assertEqual(("sdb", "unused", True, True), (spare["device"], spare["role"], spare["can_add"], spare["needs_wipe"]))
        self.assertEqual([{"device": "nvme0n1", "size_gb": 465.8, "role": "longhorn", "lh_used_gb": 27.0, "lh_size_gb": 117.0},
                          {"device": "sdb", "size_gb": 1863, "role": "unused", "lh_used_gb": 0, "lh_size_gb": 0}],
                         DISKS.summary()["node1"])

    def test_harvester_adds_a_disk_by_provisioning_its_blockdevice(self):
        c = Cluster()
        with self.assertRaisesRegex(ValueError, "tick erase"):
            DISKS.add({"node": "node1", "blockdevice": "bd-sdb"})
        DISKS.add({"node": "node1", "blockdevice": "bd-sdb", "wipe": True})
        method, path, body = c.sent[-1]
        self.assertEqual(("PATCH", DISKS.BD + "/bd-sdb"), (method, path))
        self.assertEqual({"spec": {"provision": True, "provisioner": {"longhorn": {"engineVersion": "LonghornV1"}},
                                   "fileSystem": {"forceFormatted": True}}}, body)

    def test_an_older_harvester_uses_its_older_field(self):
        c = Cluster()
        c.bds[0]["spec"] = {"nodeName": "node1", "devPath": "/dev/sdb", "fileSystem": {"provisioned": False}}
        c.bds[0]["status"]["deviceStatus"]["fileSystem"] = {}
        DISKS.add({"node": "node1", "blockdevice": "bd-sdb"})
        self.assertEqual({"spec": {"fileSystem": {"provisioned": True, "forceFormatted": False}}}, c.sent[-1][2])

    def test_elsewhere_a_mounted_folder_or_a_raw_device_is_added(self):
        c = Cluster(harvester=False)
        DISKS.add({"node": "node1", "path": "/mnt/disk2"})
        disks = c.sent[-1][2]["spec"]["disks"]
        self.assertEqual({"disk-mnt-disk2": {"path": "/mnt/disk2", "allowScheduling": True, "diskType": "filesystem",
                                             "storageReserved": 0, "tags": []}}, disks)
        with self.assertRaisesRegex(ValueError, "raw device"):
            DISKS.add({"node": "node1", "path": "/mnt/disk3", "engine": "v2"})
        with self.assertRaisesRegex(ValueError, "already uses"):
            DISKS.add({"node": "node1", "path": "/var/lib/harvester/defaultdisk"})

    def test_a_disk_is_removed_only_once_it_is_empty_and_closed(self):
        c = Cluster(harvester=False)
        with self.assertRaisesRegex(ValueError, "evict it first"):
            DISKS.remove("node1", "default-disk")
        DISKS.evict("node1", "default-disk")
        self.assertEqual({"evictionRequested": True, "allowScheduling": False},
                         c.sent[-1][2]["spec"]["disks"]["default-disk"])
        c.lh["status"]["diskStatus"]["default-disk"]["scheduledReplica"] = {}
        c.lh["spec"]["disks"]["default-disk"]["allowScheduling"] = False
        DISKS.remove("node1", "default-disk")
        self.assertEqual({"spec": {"disks": {"default-disk": None}}}, c.sent[-1][2])


class ProbeMountTests(unittest.TestCase):
    def test_mounts_name_the_disk_under_each_filesystem(self):
        import probe
        mountinfo = ("22 1 259:2 / / rw - ext4 /dev/nvme0n1p2 rw\n"
                     "30 22 8:17 / /var/lib/longhorn rw - ext4 /dev/sdb1 rw\n"
                     "31 22 0:25 / /run rw - tmpfs tmpfs rw\n")
        # As real sysfs has them: NVMe partitions have no "block" part in the path.
        links = {"259:2": "../../devices/pci0000:00/0000:3d:00.0/nvme/nvme0/nvme0n1/nvme0n1p2",
                 "8:17": "../../devices/pci0000:00/ata1/host0/target0:0:0/0:0:0:0/block/sdb/sdb1"}
        partitions = ("nvme0n1p2", "sdb1")
        with mock.patch.object(probe, "_read", lambda p: mountinfo if p.endswith("mountinfo") else None), \
                mock.patch.object(probe.os, "readlink", lambda p: next(v for k, v in links.items() if p.endswith(k))), \
                mock.patch.object(probe.os.path, "exists", lambda p: any(x in p.replace("\\", "/") for x in partitions)):
            rows = probe.mounts()
        self.assertEqual([("nvme0n1", "/"), ("sdb", "/var/lib/longhorn")], [(r["disk"], r["mountpoint"]) for r in rows])


if __name__ == "__main__":
    unittest.main()
