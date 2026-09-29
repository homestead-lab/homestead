import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_disk_setup as SETUP
import homestead_disks as DISKS

TOOLS = "TOOL mkfs.ext4\nTOOL mkfs.xfs\nTOOL wipefs\nTOOL chattr\nTOOL findmnt\nEND"


def seen(**lines):
    base = {"SIZE": str(1000 * 1024 ** 3), "FS": "", "UUID": "", "BYID": "/dev/disk/by-id/nvme-Samsung_990_S123"}
    base.update(lines)
    parts = [f"{k} {v}" for k, v in base.items() if k != "PART"]
    parts += [f"PART {p}" for p in lines.get("PART", [])]
    return "\n".join(parts) + "\n" + TOOLS


class Host:
    """What the host says: inspect output, then what set-up printed."""

    def __init__(self, inspect_out, setup_out="OK 1234 ext4 /mnt/nvme0n1\n"):
        self.inspect_out, self.setup_out, self.scripts = inspect_out, setup_out, []

    def run(self, node, script, timeout=60):
        self.scripts.append(script)
        return (self.setup_out if "set -e" in script else self.inspect_out), ""


class InspectTests(unittest.TestCase):
    def facts(self, out):
        return SETUP.parse("/dev/nvme0n1", out)

    def test_what_it_holds_decides_what_is_offered(self):
        cases = {
            "blank": (seen(), ["format"]),
            "longhorn": (seen(FS="ext4", LONGHORN='{"diskUUID":"8f1c"}', REPLICAS="3"), ["import", "erase"]),
            "data": (seen(FS="ext4", ENTRIES="4"), ["erase"]),
            "partitioned": (seen(PART=["nvme0n1|disk||", "nvme0n1p1|part|ntfs|"]), ["erase"]),
            "system": (seen(PART=["nvme0n1|disk||", "nvme0n1p2|part|ext4|/"]), []),
            "mounted": (seen(PART=["nvme0n1|disk|ext4|/srv/media"]), []),
            "missing": ("ERR /dev/nvme0n1 is not a block device on this host\n", []),
        }
        for state, (out, offered) in cases.items():
            with self.subTest(state=state):
                facts = self.facts(out)
                self.assertEqual((state, offered), (facts["state"], SETUP.choices(facts)))

    def test_looking_changes_nothing(self):
        script = SETUP.inspect_script("/dev/nvme0n1")
        # mkfs and wipefs are only looked for (command -v), never run.
        for action in ("mkfs.ext4 -", "mkfs.xfs -", "wipefs -a", "/etc/fstab", "chattr +i"):
            self.assertNotIn(action, script)
        self.assertIn("ro,noload", script, "ext4 is looked at without replaying its journal")
        self.assertIn("umount", script)

    def test_only_whole_disk_devices(self):
        for bad in ("/dev/sda1", "/dev/../etc/passwd", "sda", "/dev/sda; rm -rf /"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SETUP._device(bad)
        self.assertEqual("/dev/nvme0n1", SETUP._device("/dev/nvme0n1"))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(SETUP.bind, SETUP.hostrun)

    def test_formatting_needs_the_device_typed(self):
        SETUP.bind(Host(seen()))
        with self.assertRaisesRegex(ValueError, "type /dev/nvme0n1 to confirm"):
            SETUP.setup("k3s", "/dev/nvme0n1", "format", "ext4", "")

    def test_a_blank_disk_is_formatted_and_mounted_the_safe_way(self):
        host = Host(seen())
        SETUP.bind(host)
        result = SETUP.setup("k3s", "/dev/nvme0n1", "format", "ext4", "/dev/nvme0n1")
        self.assertEqual(("/mnt/nvme0n1", False), (result["path"], result["kept_data"]))
        script = host.scripts[-1]
        for part in ("mkfs.ext4 -F", "wipefs -a", "chattr +i", "nofail", "x-systemd.device-timeout=10s", "UUID=$U",
                     "/etc/fstab.homestead-backup", "findmnt"):
            self.assertIn(part, script)

    def test_longhorn_data_is_kept_without_formatting(self):
        host = Host(seen(FS="ext4", LONGHORN='{"diskUUID":"8f1c"}'))
        SETUP.bind(host)
        result = SETUP.setup("k3s", "/dev/nvme0n1", "import", "ext4", "")
        self.assertTrue(result["kept_data"])
        self.assertNotIn("mkfs", host.scripts[-1])
        self.assertNotIn("wipefs", host.scripts[-1])

    def test_a_disk_holding_something_else_is_not_formatted_as_blank(self):
        SETUP.bind(Host(seen(FS="ntfs", ENTRIES="9")))
        with self.assertRaisesRegex(ValueError, "not blank"):
            SETUP.setup("k3s", "/dev/nvme0n1", "format", "ext4", "/dev/nvme0n1")

    def test_the_system_disk_is_refused(self):
        SETUP.bind(Host(seen(PART=["nvme0n1|disk||", "nvme0n1p2|part|ext4|/"])))
        with self.assertRaisesRegex(ValueError, "system disk"):
            SETUP.setup("k3s", "/dev/nvme0n1", "erase", "ext4", "/dev/nvme0n1")

    def test_a_disk_multipathd_claimed_is_released_and_only_it_blacklisted(self):
        host = Host(seen(HOLDER="dm-0|mpatha|mpath-36001405b1a2c3d4e"))
        SETUP.bind(host)
        facts = SETUP.inspect("k3s", "/dev/nvme0n1")
        self.assertEqual(("blank", ["format"]), (facts["state"], facts["choices"]))
        SETUP.setup("k3s", "/dev/nvme0n1", "format", "ext4", "/dev/nvme0n1")
        script = host.scripts[-1]
        self.assertIn('wwid "36001405b1a2c3d4e"', script)
        self.assertNotIn("devnode", script, "no blanket blacklist of every sd disk")
        self.assertLess(script.index("multipath -f mpatha"), script.index("wipefs -a"))

    def test_a_disk_in_lvm_or_raid_is_refused_naming_what_holds_it(self):
        for holder, word in (("dm-1|data-vg--lv|LVM-abc", "LVM"), ("md0||", "RAID")):
            with self.subTest(word=word):
                SETUP.bind(Host(seen(FS="ext4", HOLDER=holder)))
                with self.assertRaisesRegex(ValueError, f"in use by {word}"):
                    SETUP.setup("k3s", "/dev/nvme0n1", "erase", "ext4", "/dev/nvme0n1")

    def test_a_mount_that_is_not_this_disk_stops_it(self):
        SETUP.bind(Host(seen(), setup_out="ERR /mnt/nvme0n1 is not /dev/nvme0n1 after mounting (/dev/sda1)\n"))
        with self.assertRaisesRegex(ValueError, "is not /dev/nvme0n1"):
            SETUP.setup("k3s", "/dev/nvme0n1", "format", "ext4", "/dev/nvme0n1")


def lvm(free_gb=135, size_gb=235, lvs=("ubuntu-lv 107374182400",), vg="ubuntu-vg"):
    g = 1024 ** 3
    lines = ["ROOT /dev/mapper/ubuntu--vg-ubuntu--lv", f"ROOTFREE {70 * g}"]
    if vg:
        lines += [f"VG {vg}", f"SIZE {size_gb * g}", f"FREE {free_gb * g}"] + [f"LV {row}" for row in lvs] + ["PV /dev/sda3"]
    lines += ["TOOL lvcreate", "TOOL mkfs.ext4", "TOOL chattr", "TOOL findmnt", "END"]
    return "\n".join(lines)


class OsSpaceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(SETUP.bind, SETUP.hostrun)

    def test_free_space_in_the_system_volume_group_is_offered_less_a_reserve(self):
        SETUP.bind(Host(lvm()))
        facts = SETUP.os_space("k3s")
        self.assertEqual(("ubuntu-vg", 24, 111, ""), (facts["vg"], facts["reserve_gb"], facts["usable_gb"], facts["problem"]))

    def test_nothing_is_offered_without_lvm_or_room_or_twice(self):
        for out, words in ((lvm(vg=""), "not on LVM"), (lvm(free_gb=12), "too little"),
                           (lvm(lvs=("ubuntu-lv 1", "longhorn 2", "longhorn-v2 3")), "already exists")):
            with self.subTest(words=words):
                SETUP.bind(Host(out))
                self.assertIn(words, SETUP.os_space("k3s")["problem"])
                with self.assertRaises(ValueError):
                    SETUP.use_os_space("k3s", 20)

    def test_v2_gets_a_raw_volume_of_its_own_beside_v1s(self):
        host = Host(lvm(lvs=("ubuntu-lv 1", "longhorn 2")), setup_out="OK /dev/ubuntu-vg/longhorn-v2\n")
        SETUP.bind(host)
        facts = SETUP.os_space("k3s")
        self.assertEqual(("", ""), (facts["problem"], facts["lvm_problems"]["v2"]))
        self.assertIn("already exists", facts["lvm_problems"]["v1"])
        done = SETUP.use_os_space("k3s", 50, "v2")
        self.assertEqual(("/dev/ubuntu-vg/longhorn-v2", "v2"), (done["path"], done["engine"]))
        self.assertIn('lvcreate -y -L 50G -n longhorn-v2 "ubuntu-vg"', host.scripts[-1])
        self.assertNotIn("mkfs", host.scripts[-1], "V2 takes the device raw")

    def test_a_volume_of_its_own_is_made_and_mounted_the_safe_way_within_the_reserve(self):
        host = Host(lvm(), setup_out="OK 1234 ext4 /mnt/longhorn-os\n")
        SETUP.bind(host)
        with self.assertRaisesRegex(ValueError, "from 5 to 111 GB"):
            SETUP.use_os_space("k3s", 120)
        self.assertEqual("/mnt/longhorn-os", SETUP.use_os_space("k3s", 100)["path"])
        script = host.scripts[-1]
        for part in ('lvcreate -y -L 100G -n longhorn "ubuntu-vg"', 'mkfs.ext4 -F -L hs-longhorn "/dev/ubuntu-vg/longhorn"',
                     "chattr +i", "nofail", "findmnt"):
            self.assertIn(part, script)
        for never in ("lvextend", "lvresize", "parted", "sgdisk", "wipefs"):
            self.assertNotIn(never, script, "nothing that exists is resized or wiped")


def regions(*rows, table="gpt", held=""):
    """The OS drive with no LVM, and the free space on each disk."""
    lines = ["ROOT /dev/sda2", "ROOTFREE 1", f"PT sda {table} 512", "PT nvme0n1 gpt 512"]
    lines += [f"HELD {held}"] if held else []
    lines += [f"REGION {row}" for row in rows]
    lines += ["TOOL mkfs.ext4", "TOOL chattr", "TOOL findmnt", "TOOL sfdisk", "TOOL partx", "END"]
    return "\n".join(lines)


G = 1024 ** 3 // 512       # sectors in a GiB


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(SETUP.bind, SETUP.hostrun)

    def test_unallocated_space_on_a_gpt_disk_is_offered_without_lvm(self):
        SETUP.bind(Host(regions(f"sda 209717248 {200 * G}", f"sda 34 2014", f"nvme0n1 2048 {5 * G}")))
        facts = SETUP.os_space("k3s")
        self.assertEqual("", facts["problem"])
        self.assertEqual([("sda", 209717248, 200)], [(r["disk"], r["start"], r["size_gb"]) for r in facts["regions"]],
                         "slivers and small gaps are not offered")

    def test_mbr_and_held_disks_are_not_partitioned(self):
        for out in (regions(f"sda 2048 {200 * G}", table="dos"), regions(f"sda 2048 {200 * G}", held="sda")):
            SETUP.bind(Host(out))
            self.assertEqual([], SETUP.os_space("k3s")["regions"])

    def test_the_new_partition_is_aligned_and_inside_the_free_space(self):
        first, length = SETUP.region_plan({"sector": 512, "start": 209717249, "sectors": 100 * G}, 500)
        self.assertEqual(0, first % 2048)
        self.assertGreaterEqual(first, 209717249)
        self.assertLessEqual(first + length, 209717249 + 100 * G)
        self.assertEqual((0, 60 * G), (0, SETUP.region_plan({"sector": 512, "start": 2048, "sectors": 100 * G}, 60)[1]))
        with self.assertRaisesRegex(ValueError, "less than 5 GB"):
            SETUP.region_plan({"sector": 512, "start": 2048, "sectors": 100 * G}, 2)

    def test_a_partition_is_added_live_without_touching_the_others(self):
        out = regions(f"sda 209717248 {200 * G}") + "\nPART /dev/sda4 6f1c-44\nOK 1234 ext4 /mnt/sda-longhorn\n"
        host = Host(out, setup_out=out)
        SETUP.bind(host)
        with self.assertRaisesRegex(ValueError, "type /dev/sda"):
            SETUP.use_region("k3s", "sda", 209717248, 100, "v1", "")
        done = SETUP.use_region("k3s", "sda", 209717248, 100, "v1", "/dev/sda")
        self.assertEqual(("/mnt/sda-longhorn", "/dev/sda4"), (done["path"], done["device"]))
        script = host.scripts[-1]
        for part in ("sfdisk --dump", "--append --no-reread --no-tell-kernel", 'partx --add --nr "$N"',
                     "changed since it was looked at", 'mkfs.ext4 -F -L hs-longhorn "$NEWP"', "nofail"):
            self.assertIn(part, script)
        for never in ("resize2fs", "xfs_growfs", "sfdisk --delete", "wipefs", "partprobe", "--move-data"):
            self.assertNotIn(never, script, "nothing existing is changed")

    def test_v2_is_given_the_new_partition_raw_by_its_partuuid(self):
        out = regions(f"sda 209717248 {200 * G}") + "\nPART /dev/sda4 6f1c-44\n"
        host = Host(out, setup_out=out)
        SETUP.bind(host)
        done = SETUP.use_region("k3s", "sda", 209717248, 100, "v2", "/dev/sda")
        self.assertEqual("/dev/disk/by-partuuid/6f1c-44", done["path"])
        self.assertNotIn("mkfs", host.scripts[-1])

    def test_free_space_that_moved_is_not_written(self):
        SETUP.bind(Host(regions(f"sda 209717248 {200 * G}")))
        with self.assertRaisesRegex(ValueError, "not there any more"):
            SETUP.use_region("k3s", "sda", 1234, 100, "v1", "/dev/sda")


class LonghornTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patched = []
        self.node = {"spec": {"disks": {"disk-mnt-nvme0n1": {"path": "/mnt/nvme0n1", "allowScheduling": True}}},
                     "status": {"diskStatus": {"disk-mnt-nvme0n1": {"conditions": [{"type": "Ready", "status": "False"}]}}}}
        # Patched for this test only: other tests use the same modules.
        for name, value in (("kget", lambda path: copy.deepcopy(self.node)),
                            ("_patch", lambda path, body, what: self.patched.append(body)),
                            ("autotag_state", lambda: str(Path(self.tmp.name, "tags.json"))),
                            ("setup_module", SETUP)):
            patcher = mock.patch.object(DISKS, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(SETUP, "hostrun", Host(seen()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_failed_empty_entry_for_the_folder_is_replaced_and_the_disk_tagged(self):
        with mock.patch.object(DISKS, "_row", return_value={"device": "nvme0n1", "kind": "NVMe", "system": False}):
            result = DISKS.set_up({"node": "k3s", "device": "/dev/nvme0n1", "mode": "format", "confirm": "/dev/nvme0n1"})
        self.assertEqual({"allowScheduling": False}, self.patched[0]["spec"]["disks"]["disk-mnt-nvme0n1"])
        self.assertIsNone(self.patched[1]["spec"]["disks"]["disk-mnt-nvme0n1"])
        added = self.patched[2]["spec"]["disks"]["disk-mnt-nvme0n1"]
        self.assertEqual(("/mnt/nvme0n1", "filesystem", ["ssd"]), (added["path"], added["diskType"], added["tags"]))
        self.assertEqual(["ssd"], result["tags"])

    def test_a_new_v2_partition_on_the_system_disk_is_added_raw_and_tagged_os(self):
        made = {"path": "/dev/disk/by-partuuid/6f1c", "device": "/dev/sda4", "size_gb": 100, "engine": "v2"}
        with mock.patch.object(SETUP, "use_region", return_value=made) as use, \
             mock.patch.object(DISKS, "_row", return_value={"device": "sda", "kind": "SSD", "system": True}):
            result = DISKS.use_os_space({"node": "k3s", "source": "part:sda:209717248", "size_gb": 100,
                                         "engine": "v2", "confirm": "/dev/sda"})
        use.assert_called_once_with("k3s", "sda", 209717248, 100, "v2", "/dev/sda")
        added = self.patched[-1]["spec"]["disks"]
        disk = next(iter(added.values()))
        self.assertEqual(("/dev/disk/by-partuuid/6f1c", "block", ["os"]), (disk["path"], disk["diskType"], disk["tags"]))
        self.assertIn("given to the V2 engine", result["detail"])

    def test_the_system_disk_is_tagged_os_only(self):
        self.assertEqual(["os"], DISKS.kind_tags({"system": True, "kind": "SSD"}))
        self.assertEqual(["hdd"], DISKS.kind_tags({"system": False, "kind": "HDD"}))
        self.assertEqual([], DISKS.kind_tags({"system": False, "kind": ""}))

    def test_existing_disks_are_tagged_once_and_tags_someone_set_are_kept(self):
        rows = {"k3s": [{"device": "sda", "system": True, "kind": "SSD",
                         "longhorn": [{"id": "default", "tags": [], "ready": True}]},
                        {"device": "sdb", "system": False, "kind": "HDD",
                         "longhorn": [{"id": "big", "tags": ["media"], "ready": True}]},
                        {"device": "sdc", "system": False, "kind": "",
                         "longhorn": [{"id": "unknown", "tags": [], "ready": True}]}]}
        calls = []
        with mock.patch.object(DISKS, "inventory", return_value={"nodes": rows}), \
                mock.patch.object(DISKS, "set_disk_tags", lambda node, disk, tags: calls.append((node, disk, tags))):
            self.assertEqual([("k3s", "default", ["os"])], DISKS.auto_tag())
            rows["k3s"][0]["longhorn"][0]["tags"] = []          # someone took the tag off
            self.assertEqual([], DISKS.auto_tag(), "once only")
            rows["k3s"][2]["kind"] = "SSD"                     # the probe has now said
            self.assertEqual([("k3s", "unknown", ["ssd"])], DISKS.auto_tag())


if __name__ == "__main__":
    unittest.main()
