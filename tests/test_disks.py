import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "probe"))
import homestead_disks as DISKS
import homestead_kube as K
import homestead_operations as OPS
from fakes.kube import Kube

GiB = 1024 ** 3
LONGHORN, HARVESTER = "longhorn.io/v1beta2", "harvesterhci.io/v1beta1"


def lh_node(disks, status):
    return {"apiVersion": LONGHORN, "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": "node1"},
            "spec": {"disks": disks}, "status": {"diskStatus": status}}


BD_SDB = {"apiVersion": HARVESTER, "kind": "BlockDevice", "metadata": {"namespace": "longhorn-system", "name": "bd-sdb"},
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
BD_PATH = f"{DISKS.BD}/bd-sdb"
NODE_PATH = f"{DISKS.LH}/nodes/node1"


def replica(name, **spec):
    return {"apiVersion": LONGHORN, "kind": "Replica", "metadata": {"namespace": "longhorn-system", "name": name}, "spec": spec}


def cluster(test, harvester=True, lh=None, *objects):
    """Longhorn's node, and Harvester's block device where there is Harvester,
    on the shared fake API (tests/fakes/kube.py)."""
    kube = test.enterContext(Kube(copy.deepcopy(lh or LH_NODE), *objects, *([copy.deepcopy(BD_SDB)] if harvester else []),
                                  absent_groups=() if harvester else ("harvesterhci.io",)))
    # The shared fake runs the real write guards; the storage guard takes the job
    # store's lock, which lives under its DATA_DIR.
    test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))
    saved = (DISKS.kget, DISKS.ksend, DISKS.temps, DISKS.mutation_scope, DISKS.v2_tasks)
    test.addCleanup(lambda: (DISKS.bind(*saved[:3]), setattr(DISKS, "mutation_scope", saved[3]), setattr(DISKS, "v2_tasks", saved[4])))
    DISKS.bind(K.kget, K.ksend, lambda: PROBE)
    return kube


def sent(kube):
    return [(method, path, body) for method, path, body, _ in kube.writes]


def edited(kube, path, change):
    """The stored object at path after change(obj), as someone else saved it."""
    obj = kube.object(path)
    change(obj)
    kube.add(obj)


class DiskTests(unittest.TestCase):
    def test_every_disk_is_listed_with_what_it_is_for(self):
        cluster(self)
        inv = DISKS.inventory()
        system, spare = inv["nodes"]["node1"]
        self.assertEqual(("nvme0n1", True, "longhorn"), (system["device"], system["system"], system["role"]))
        self.assertEqual((27.0, 2), (system["longhorn"][0]["used_gb"], system["longhorn"][0]["replicas"]))
        self.assertEqual(("sdb", "unused", True, True), (spare["device"], spare["role"], spare["can_add"], spare["needs_wipe"]))
        self.assertEqual([{"device": "nvme0n1", "name": "", "size_gb": 465.8, "role": "longhorn", "system": True,
                           "model": "Samsung", "lh_paths": ["/var/lib/harvester/defaultdisk"],
                           "root_fs": True, "node_fs": True, "lh_filesystems": [{"capacity_gb": 117.0, "used_gb": 27.0,
                               "available_gb": 90.0, "reserved_gb": 0.0, "on_root": False, "on_node_fs": False, "data_gb": 27.0}],
                           "lh_used_gb": 27.0, "lh_size_gb": 117.0, "lh_root_used_gb": 0},
                          {"device": "sdb", "name": "", "size_gb": 1863, "role": "unused", "system": False,
                           "model": "IronWolf", "lh_paths": [], "root_fs": False, "node_fs": False, "lh_filesystems": [],
                           "lh_used_gb": 0, "lh_size_gb": 0, "lh_root_used_gb": 0}],
                         DISKS.summary()["node1"])

    def test_shared_longhorn_folders_keep_one_capacity_and_the_reserved_space(self):
        node = lh_node({"disk-a": {"path": "/var/lib/longhorn/a", "storageReserved": 30 * GiB},
                        "disk-b": {"path": "/var/lib/longhorn/b", "storageReserved": 30 * GiB}},
                       {name: {"storageMaximum": 100 * GiB, "storageAvailable": 60 * GiB,
                               "scheduledReplica": {replica: 1}}
                        for name, replica in [("disk-a", "replica-a"), ("disk-b", "replica-b")]})
        cluster(self, harvester=False, lh=node)
        with mock.patch.object(DISKS, "_replica_sizes", return_value={"replica-a": 4 * GiB, "replica-b": 6 * GiB}):
            disk = DISKS.summary()["node1"][0]
        self.assertTrue(disk["root_fs"])
        self.assertEqual([{"capacity_gb": 100.0, "used_gb": 40.0, "available_gb": 60.0,
                           "reserved_gb": 30.0, "on_root": True, "on_node_fs": True, "data_gb": 10.0}], disk["lh_filesystems"])
        self.assertEqual((70.0, 10.0), (disk["lh_size_gb"], disk["lh_root_used_gb"]))

    def test_kubelet_filesystem_is_identified_when_it_is_on_another_disk(self):
        cluster(self, harvester=False, lh=lh_node({"disk-a": {"path": "/var/lib/kubelet/longhorn"}},
            {"disk-a": {"storageMaximum": 200 * GiB, "storageAvailable": 150 * GiB,
                        "scheduledReplica": {"replica-a": 1}}}))
        probe = copy.deepcopy(PROBE)
        probe["node1"]["mounts"] = [{"disk": "nvme0n1", "device": "nvme0n1p2", "mountpoint": "/"},
                                    {"disk": "sdb", "device": "sdb1", "mountpoint": "/var/lib/kubelet"}]
        DISKS.bind(K.kget, K.ksend, lambda: probe)
        with mock.patch.object(DISKS, "_replica_sizes", return_value={"replica-a": 10 * GiB}):
            disks = DISKS.summary()["node1"]
        self.assertTrue(disks[0]["root_fs"])
        self.assertFalse(disks[0]["node_fs"])
        self.assertTrue(disks[1]["node_fs"])
        self.assertFalse(disks[1]["root_fs"])
        usage = disks[1]["lh_filesystems"][0]
        self.assertTrue(usage["on_node_fs"])
        self.assertFalse(usage["on_root"])
        self.assertEqual(10.0, usage["data_gb"], "replica data is also separated on a non-root kubelet filesystem")

    def test_harvester_adds_a_disk_by_provisioning_its_blockdevice(self):
        c = cluster(self)
        with self.assertRaisesRegex(ValueError, "tick erase"):
            DISKS.add({"node": "node1", "blockdevice": "bd-sdb"})
        DISKS.add({"node": "node1", "blockdevice": "bd-sdb", "wipe": True})
        method, path, body = sent(c)[-1]
        self.assertEqual(("PATCH", DISKS.BD + "/bd-sdb"), (method, path))
        self.assertEqual({"spec": {"provision": True, "provisioner": {"longhorn": {"engineVersion": "LonghornV1"}},
                                   "fileSystem": {"forceFormatted": True}}}, body)

    def test_an_older_harvester_uses_its_older_field(self):
        c = cluster(self)
        def older(bd):
            bd["spec"] = {"nodeName": "node1", "devPath": "/dev/sdb", "fileSystem": {"provisioned": False}}
            bd["status"]["deviceStatus"]["fileSystem"] = {}
        edited(c, BD_PATH, older)
        DISKS.add({"node": "node1", "blockdevice": "bd-sdb"})
        self.assertEqual({"spec": {"fileSystem": {"provisioned": True, "forceFormatted": False}}}, sent(c)[-1][2])

    def test_elsewhere_a_mounted_folder_or_a_raw_device_is_added(self):
        c = cluster(self, harvester=False)
        DISKS.add({"node": "node1", "path": "/mnt/disk2"})
        disks = sent(c)[-1][2]["spec"]["disks"]
        self.assertEqual({"disk-mnt-disk2": {"path": "/mnt/disk2", "allowScheduling": True, "diskType": "filesystem",
                                             "storageReserved": 0, "tags": []}}, disks)
        with self.assertRaisesRegex(ValueError, "raw device"):
            DISKS.add({"node": "node1", "path": "/mnt/disk3", "engine": "v2"})
        with self.assertRaisesRegex(ValueError, "already uses"):
            DISKS.add({"node": "node1", "path": "/var/lib/harvester/defaultdisk"})

    def test_a_disk_is_removed_only_once_it_is_empty_and_closed(self):
        c = cluster(self, harvester=False)
        with self.assertRaisesRegex(ValueError, "evict it first"):
            DISKS.remove("node1", "default-disk")
        DISKS.evict("node1", "default-disk")
        self.assertEqual({"evictionRequested": True, "allowScheduling": False},
                         sent(c)[-1][2]["spec"]["disks"]["default-disk"])
        def drained(node):
            node["status"]["diskStatus"]["default-disk"]["scheduledReplica"] = {}
            node["spec"]["disks"]["default-disk"]["allowScheduling"] = False
        edited(c, NODE_PATH, drained)
        # A disk whose status forgot its replicas still has them: they say so.
        c.add(replica("r3", nodeID="node1", diskPath="/var/lib/harvester/defaultdisk", volumeName="pvc-1"))
        with self.assertRaisesRegex(ValueError, "1 replica"):
            DISKS.remove("node1", "default-disk")
        c.discard(f"{DISKS.LH}/replicas/r3")
        DISKS.remove("node1", "default-disk")
        self.assertEqual({"spec": {"disks": {"default-disk": None}}}, sent(c)[-1][2])


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


    def test_a_root_filesystem_on_lvm_is_followed_down_to_its_nvme(self):
        import probe
        # Ubuntu: / on ubuntu-vg (dm-0), built on nvme0n1p3; /boot on nvme0n1p2.
        mountinfo = ("22 1 252:0 / / rw - ext4 /dev/mapper/ubuntu--vg-ubuntu--lv rw\n"
                     "23 22 259:2 / /boot rw - ext4 /dev/nvme0n1p2 rw\n")
        links = {"252:0": "../../devices/virtual/block/dm-0",
                 "259:2": "../../devices/pci0000:00/nvme/nvme0/nvme0n1/nvme0n1p2"}
        partitions = ("nvme0n1p2", "nvme0n1p3")
        with mock.patch.object(probe, "_read", lambda p: mountinfo if p.endswith("mountinfo") else None), \
                mock.patch.object(probe.os, "readlink", lambda p: next(v for k, v in links.items() if p.endswith(k))), \
                mock.patch.object(probe.os, "listdir", lambda p: ["nvme0n1p3"] if p.replace("\\", "/").endswith("dm-0/slaves") else []), \
                mock.patch.object(probe.os.path, "realpath", lambda p: "/sys/devices/pci0000:00/nvme/nvme0/nvme0n1/nvme0n1p3"), \
                mock.patch.object(probe.os.path, "exists", lambda p: any(x in p.replace("\\", "/") for x in partitions)):
            rows = probe.mounts()
        self.assertEqual([("nvme0n1", "/", "dm-0"), ("nvme0n1", "/boot", "nvme0n1p2")],
                         [(r["disk"], r["mountpoint"], r["device"]) for r in rows])


class SystemDriveTests(unittest.TestCase):
    """node-3: Longhorn's default folder /var/lib/longhorn on / on LVM showed as
    a disk of its own, "L", beside the NVMe it is on."""

    def setUp(self):
        self.probe = {"node1": {"disks": [{"name": "nvme0n1", "size_gb": 476.9, "model": "WD SN770", "serial": "S123"},
                                          {"name": "sda", "size_gb": 2794.5, "model": "WD30EFRX", "serial": "WD-WCC1"}],
                                "mounts": [{"disk": "dm-0", "mountpoint": "/"},
                                           {"disk": "nvme0n1", "mountpoint": "/boot"},
                                           {"disk": "nvme0n1", "mountpoint": "/boot/efi"},
                                           {"disk": "sda", "mountpoint": "/mnt/sda"}]}}
        lh = lh_node({"default-disk": {"path": "/var/lib/longhorn/", "allowScheduling": True},
                      "disk-mnt-sda": {"path": "/mnt/sda", "allowScheduling": True}},
                     {"default-disk": {"storageMaximum": 68 * GiB, "storageAvailable": 44 * GiB,
                                       "scheduledReplica": {"vol-a-r-1": 1, "vol-b-r-1": 1}},
                      "disk-mnt-sda": {"storageMaximum": 2750 * GiB, "storageAvailable": 2700 * GiB}})
        self.kube = cluster(
            self, False, lh,
            {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node1", "annotations": {}}},
            *[replica(f"vol-{v}-r-1", volumeName=f"vol-{v}") for v in "ab"],
            *[{"apiVersion": LONGHORN, "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": f"vol-{v}"},
               "status": {"actualSize": size * GiB}} for v, size in (("a", 3), ("b", 2))])
        DISKS.bind(K.kget, K.ksend, lambda: self.probe)

    @property
    def sent(self):
        return sent(self.kube)

    def test_longhorns_folder_on_the_lvm_root_shows_on_the_nvme_it_is_on(self):
        lines = DISKS.summary()["node1"]
        self.assertEqual(["nvme0n1", "sda"], [d["device"] for d in lines], "no separate 'longhorn' disk")
        nvme = lines[0]
        self.assertTrue(nvme["system"])
        self.assertEqual(68.0, nvme["lh_size_gb"])
        self.assertEqual(["/var/lib/longhorn/"], nvme["lh_paths"])
        self.assertEqual(476.9, nvme["size_gb"], "the drive's own size, not Longhorn's share of it")
        # Longhorn counts all 24 GiB the root filesystem uses as its own; its
        # replicas hold 5, and the rest is the system's.
        self.assertEqual(5.0, nvme["lh_used_gb"], "what its replicas hold, not the whole filesystem's use")
        self.assertEqual(5.0, nvme["lh_root_used_gb"], "on /, so inside the root filesystem's use too")
        self.assertEqual(50.0, lines[1]["lh_used_gb"], "a drive of its own: Longhorn's own figure")
        self.assertEqual(0, lines[1]["lh_root_used_gb"], "a drive of its own")

    def test_a_disk_can_be_named_by_its_serial_and_the_name_cleared(self):
        DISKS.set_disk_name("node1", "sda", "  Media   3TB ")
        self.assertIn('"WD-WCC1":"Media 3TB"', self.sent[-1][2]["metadata"]["annotations"]["homestead.io/disk-names"])
        self.assertEqual("Media 3TB", next(d for d in DISKS.summary()["node1"] if d["device"] == "sda")["name"])
        DISKS.set_disk_name("node1", "sda", "")
        self.assertIsNone(self.sent[-1][2]["metadata"]["annotations"]["homestead.io/disk-names"])
        with self.assertRaisesRegex(ValueError, "no disk sdz"):
            DISKS.set_disk_name("node1", "sdz", "x")


if __name__ == "__main__":
    unittest.main()
