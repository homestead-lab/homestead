import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_os as HOST_OS

UBUNTU = """OS Ubuntu 24.04.3 LTS
OSID ubuntu 24.04
KERNEL 6.8.0-85-generic
UP 86400
REBOOT linux-image-6.8.0-86-generic linux-base
PKG apt
LISTS 1790000000
UPD libc6|security
UPD openssl|security
UPD tzdata|
FAILED multipathd.service
NTP yes
ROOT 102400000 94208000
BLK NAME="sda" PKNAME="" TYPE="disk" SIZE="256060514304" FSTYPE="" MOUNTPOINT="" PTTYPE="gpt" PARTLABEL="" LABEL=""
BLK NAME="sda1" PKNAME="sda" TYPE="part" SIZE="1127219200" FSTYPE="vfat" MOUNTPOINT="/boot/efi" PTTYPE="gpt" PARTLABEL="" LABEL=""
BLK NAME="sda3" PKNAME="sda" TYPE="part" SIZE="252783804416" FSTYPE="LVM2_member" MOUNTPOINT="" PTTYPE="gpt" PARTLABEL="" LABEL=""
BLK NAME="sda2" PKNAME="sda" TYPE="part" SIZE="2147483648" FSTYPE="ext4" MOUNTPOINT="/boot" PTTYPE="gpt" PARTLABEL="" LABEL=""
BLK NAME="ubuntu--vg-ubuntu--lv" PKNAME="sda3" TYPE="lvm" SIZE="107374182400" FSTYPE="ext4" MOUNTPOINT="/" PTTYPE="" PARTLABEL="" LABEL=""
BLK NAME="sdb" PKNAME="" TYPE="disk" SIZE="1000204886016" FSTYPE="" MOUNTPOINT="" PTTYPE="" PARTLABEL="" LABEL=""
BLK NAME="loop0" PKNAME="" TYPE="loop" SIZE="1000" FSTYPE="squashfs" MOUNTPOINT="/snap/core" PTTYPE="" PARTLABEL="" LABEL=""
START sda1 2048
START sda2 2203648
START sda3 6397952
END
"""


class ParseTests(unittest.TestCase):
    def test_what_a_host_needs_is_read_from_its_own_tools(self):
        facts = HOST_OS.parse(UBUNTU)
        self.assertTrue(facts["complete"])
        self.assertEqual(("Ubuntu 24.04.3 LTS", "ubuntu", "24.04", "apt"),
                         (facts["os"], facts["id"], facts["version"], facts["package_manager"]))
        self.assertEqual((3, 2), (len(facts["updates"]), facts["security"]))
        self.assertTrue(facts["reboot"])
        self.assertIn("linux-image", facts["reboot_for"])
        self.assertEqual((["multipathd.service"], True, 92), (facts["failed_units"], facts["ntp"], facts["root_used_pct"]))
        summary = HOST_OS.summary(facts)
        self.assertEqual("bad", summary["tone"])
        self.assertIn("2 security updates", summary["text"])
        self.assertIn("restart needed", summary["text"])

    def test_partitions_are_laid_out_in_order_with_what_they_hold(self):
        disks = {d["name"]: d for d in HOST_OS.parse(UBUNTU)["disks"]}
        self.assertEqual(["sda", "sdb"], sorted(disks), "loop devices are not disks")
        sda = disks["sda"]
        self.assertEqual(("gpt", ["sda1", "sda2", "sda3"]), (sda["table"], [p["name"] for p in sda["partitions"]]))
        self.assertEqual(2048 * 512, sda["partitions"][0]["start"])
        root = sda["partitions"][2]
        self.assertEqual((["/"], ["lvm"]), (root["mounts"], root["holds"]), "the root volume sits on sda3")
        self.assertEqual(([], 0), (disks["sdb"]["partitions"], disks["sdb"]["free"]))

    def test_an_unfinished_read_is_not_taken_as_a_healthy_host(self):
        self.assertFalse(HOST_OS.parse("OS Ubuntu\n")["complete"])

    def test_dnf_security_advisories_mark_their_packages(self):
        facts = HOST_OS.parse("PKG dnf\nSEC kernel.x86_64\nUPD kernel.x86_64|\nUPD vim.x86_64|\nEND\n")
        self.assertEqual((2, 1), (len(facts["updates"]), facts["security"]))


class Host:
    def __init__(self, out):
        self.out, self.scripts = out, []

    def run(self, node, script, timeout=60):
        self.scripts.append(script)
        return self.out(script) if callable(self.out) else self.out, ""


class Ops:
    def __init__(self):
        self.started = []

    def start(self, kind, title, target, href, ref, message):
        self.started.append((kind, title, ref))
        return {"id": "op1"}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.nodes = {"items": [{"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}

    def bind(self, host, platform=None):
        HOST_OS.bind(lambda path: self.nodes, host, lambda force=False: platform or {"distribution": "k3s"}, self.data)

    def test_each_host_is_read_every_six_hours(self):
        host = Host(UBUNTU)
        self.bind(host)
        self.assertEqual(["node-1"], HOST_OS.tick(now=10 ** 10))
        self.assertEqual([], HOST_OS.tick(now=10 ** 10 + 60))
        self.assertIn("REFRESH=0", host.scripts[0], "a scheduled read leaves the package lists alone")
        self.assertEqual("2 security updates, restart needed, 1 failed service, root 92% full",
                         HOST_OS.report("node-1")["hosts"]["node-1"]["summary"]["text"])

    def test_harvester_hosts_are_left_to_harvester(self):
        self.bind(Host(UBUNTU), {"harvester": True, "distribution": "rke2"})
        self.assertEqual([], HOST_OS.tick())
        self.assertFalse(HOST_OS.report()["applies"])

    def test_what_needs_someone_is_an_alert(self):
        self.bind(Host(UBUNTU))
        HOST_OS.read("node-1")
        keys = {fact["key"] for fact in HOST_OS.alert_facts()}
        self.assertEqual({"hostos:node-1:security", "hostos:node-1:reboot", "hostos:node-1:failed", "hostos:node-1:root"}, keys)

    def test_updates_run_detached_on_the_host_and_are_followed_as_a_job(self):
        host = Host(lambda script: "STARTED\n" if "systemd-run" in script else UBUNTU)
        self.bind(host)
        ops = Ops()
        HOST_OS.upgrade_start("node-1", ops)
        started = host.scripts[-1]
        self.assertIn("systemd-run --unit=homestead-os-upgrade", started)
        self.assertIn("--force-confold dist-upgrade", started)
        self.assertEqual(("host-os", "Install 3 updates on node-1"), ops.started[0][:2])

    def test_a_finished_upgrade_says_whether_a_restart_is_needed(self):
        host = Host(lambda script: "CODE 0\n" if 'echo "CODE' in script else UBUNTU)
        self.bind(host)
        status, _, message = HOST_OS.status({"ref": {"node": "node-1", "since": 0}}, now=1000)
        self.assertEqual("succeeded", status)
        self.assertIn("needs a restart", message)
        host.out = lambda script: "CODE 100\n" if 'echo "CODE' in script else UBUNTU
        self.assertEqual("failed", HOST_OS.status({"ref": {"node": "node-1", "since": 0}}, now=1000)[0])


if __name__ == "__main__":
    unittest.main()
