import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_os as HOST_OS
import homestead_hostrun as HOSTRUN

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

    def test_workload_iscsi_attachments_do_not_appear_as_local_disks(self):
        extra = '\n'.join([
            'BLK NAME="sdc" TYPE="disk" SIZE="10737418240" FSTYPE="ext4" TRAN="iscsi" VENDOR="" MODEL=""',
            'BLK NAME="sdd" TYPE="disk" SIZE="10737418240" FSTYPE="" TRAN="" VENDOR="IET" MODEL="VIRTUAL-DISK"',
            'BLK NAME="nvme0n1" TYPE="disk" SIZE="512110190592" FSTYPE="" TRAN="nvme" VENDOR="" MODEL="Local SSD"',
        ])
        disks = {d["name"]: d for d in HOST_OS.parse(UBUNTU.replace("END", extra + "\nEND"))["disks"]}
        self.assertEqual({"sda", "sdb", "nvme0n1"}, set(disks))
        self.assertEqual(["/"], disks["sda"]["partitions"][2]["mounts"], "keep the LVM root partition")

    def test_dnf_security_advisories_mark_their_packages(self):
        facts = HOST_OS.parse("PKG dnf\nSEC kernel.x86_64\nUPD kernel.x86_64|\nUPD vim.x86_64|\nEND\n")
        self.assertEqual((2, 1), (len(facts["updates"]), facts["security"]))


class Host:
    def __init__(self, out, held_by=""):
        self.out, self.scripts = out, []
        self.held_by, self.holds, self.let_go_of, self.changes = held_by, [], [], []

    def hold(self, node, what, seconds=60, holder=""):
        if self.held_by:
            raise HOSTRUN.Busy(f"another change is running on {node}: {self.held_by}")
        self.holds.append((node, what, seconds))
        return "this-replica/1"

    def let_go(self, node, holder):
        self.let_go_of.append((node, holder))

    def run(self, node, script, timeout=60, change="", holder=""):
        self.scripts.append(script)
        if change:
            self.changes.append((change, holder))
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

    def test_check_every_host_now_reads_each_ready_host_with_fresh_lists(self):
        # #371: what Refresh promised - each host read again, not the card.
        host = Host(UBUNTU)
        self.nodes["items"].append({"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}})
        self.bind(host)
        started = HOST_OS.check_all(wait=True)
        self.assertEqual(["node-1"], started["hosts"], "a host that is not Ready is left")
        self.assertEqual(1, len(host.scripts))
        self.assertIn("REFRESH=1", host.scripts[0])
        done = HOST_OS.report()
        self.assertFalse(done["checking"]["running"])
        self.assertEqual(["node-1"], done["checking"]["done"])
        self.assertIn("node-1", done["hosts"])
        self.assertEqual(HOST_OS.EVERY, done["every_s"])

    def test_a_read_cut_short_says_how_far_it_got_not_the_raw_lines(self):
        # apt-get killed by the helper's memory limit: output stops after PKG.
        self.bind(Host("OS Ubuntu 26.04.1 LTS\nOSID ubuntu 26.04\nKERNEL 7.0.0-34-generic\nUP 1654\nPKG apt\n"))
        with self.assertRaises(ValueError) as caught:
            HOST_OS.read("node-1", refresh=True)
        self.assertIn("stopped part-way, after PKG", str(caught.exception))
        self.assertIn("out of memory", str(caught.exception))
        self.assertNotIn("KERNEL", str(caught.exception))

    def test_harvester_hosts_are_left_to_harvester(self):
        self.bind(Host(UBUNTU), {"harvester": True, "distribution": "rke2"})
        self.assertEqual([], HOST_OS.tick())
        self.assertFalse(HOST_OS.report()["applies"])

    def test_a_restart_since_the_read_clears_restart_needed_and_reads_again(self):
        # A manual restart left "restart needed" until the next six-hourly read.
        node = {"metadata": {"name": "node-1"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"bootID": "boot-a"}}}
        self.nodes = {"items": [node]}
        host = Host(UBUNTU)
        HOST_OS.bind(lambda path: node if path.endswith("/node-1") else self.nodes, host,
                     lambda force=False: {"distribution": "k3s"}, self.data)
        self.assertEqual(["node-1"], HOST_OS.tick(now=10 ** 10))
        self.assertIn("restart needed", HOST_OS.report("node-1")["hosts"]["node-1"]["summary"]["text"])
        self.assertEqual([], HOST_OS.tick(now=10 ** 10 + 60), "same boot: not read again yet")
        node["status"]["nodeInfo"]["bootID"] = "boot-b"
        row = HOST_OS.report("node-1")["hosts"]["node-1"]
        self.assertNotIn("restart needed", row["summary"]["text"])
        self.assertTrue(row["restarted_since_read"])
        self.assertNotIn("hostos:node-1:reboot", {fact["key"] for fact in HOST_OS.alert_facts()})
        self.assertEqual(["node-1"], HOST_OS.tick(now=10 ** 10 + 120), "a restarted host is read again at once")

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
        self.assertIn("--force-confold upgrade --with-new-pkgs", started)
        self.assertNotIn("dist-upgrade", started, "no package is ever removed")
        self.assertEqual(("host-os", "Install 3 updates on node-1"), ops.started[0][:2])

    def test_the_upgrade_command_is_quoted_for_the_unit_whatever_it_holds(self):
        import shlex
        host = Host(lambda script: "STARTED\n" if "systemd-run" in script else UBUNTU)
        self.bind(host)
        command = "apt-get -o 'APT::Get::Assume-Yes=true' upgrade"
        with mock.patch.dict(HOST_OS.UPGRADE, {"apt": command}):
            ref = {}
            HOST_OS.upgrade_begin("node-1", ref=ref)
        launch = next(s for s in host.scripts if "systemd-run" in s)
        inner = launch.split(" --quiet sh -c ", 1)[1].rsplit(" && echo STARTED", 1)[0]
        self.assertEqual([f"( {command} ) > {HOST_OS.LOG} 2>&1; echo {ref['run']} $? > {HOST_OS.STATUS}"],
                         shlex.split(inner))

    def test_updates_hold_the_host_until_the_job_ends(self):
        host = Host(lambda script: "STARTED\n" if "systemd-run" in script else UBUNTU)
        self.bind(host)
        ops = Ops()
        HOST_OS.upgrade_start("node-1", ops)
        ref = ops.started[0][2]
        self.assertEqual([("node-1", "Install 3 updates on node-1")], [h[:2] for h in host.holds])
        self.assertGreater(host.holds[0][2], 7200, "held as long as an upgrade may run")
        self.assertEqual([("Install 3 updates on node-1", "this-replica/1")], host.changes)
        self.assertEqual([], host.let_go_of, "still held while the update installs")
        host.out = lambda script: "RUNNING\n" if 'echo "CODE' in script else UBUNTU
        self.assertEqual("running", HOST_OS.status({"ref": ref}, now=ref["since"] + 60)[0])
        self.assertEqual([], host.let_go_of)
        host.out = lambda script: "CODE 0\n" if 'echo "CODE' in script else UBUNTU
        self.assertEqual("succeeded", HOST_OS.status({"ref": ref}, now=ref["since"] + 120)[0])
        self.assertEqual([("node-1", "this-replica/1")], host.let_go_of)

    def test_stopping_tracking_the_updates_lets_go_of_the_host_at_once(self):
        host = Host(lambda script: "STARTED\n" if "systemd-run" in script else UBUNTU)
        self.bind(host)
        ops = Ops()
        HOST_OS.upgrade_start("node-1", ops)
        item = {"kind": "host-os", "ref": ops.started[0][2]}
        HOST_OS.register(HOST_OS.OPS)  # its canceller is added by register (#622)
        plan, run = HOST_OS.OPS.CANCELLERS["host-os"]
        keeps = " ".join(plan(item)["keeps"])
        self.assertIn("keep installing on node-1", keeps)
        self.assertIn("no longer wait", keeps)
        run(item, {})
        self.assertEqual([("node-1", "this-replica/1")], host.let_go_of)

    def start_timed_out(self):
        import homestead_pod_exec as POD_EXEC

        def out(script):
            if "systemd-run" in script:
                raise POD_EXEC.ExecTimeout("The script on node-1 did not finish within 60 seconds", "", "", 60)
            return UBUNTU

        host = Host(out)
        real = host.run
        host.run = lambda node, script, timeout=60, change="", holder="": (
            out(script) if "systemd-run" in script else real(node, script, timeout, change, holder))
        self.bind(host)
        ops, messages = Ops(), []
        started = ops.start
        ops.start = lambda kind, title, target, href, ref, message: (messages.append(message),
                                                                     started(kind, title, target, href, ref, message))[1]
        HOST_OS.upgrade_start("node-1", ops)
        return host, ops.started[0][2], messages

    def test_a_launch_that_times_out_keeps_the_host_held_and_needs_attention(self):
        host, ref, messages = self.start_timed_out()
        self.assertEqual([], host.let_go_of, "the updates may be running detached")
        self.assertEqual("this-replica/1", ref["lease"])
        host.out = lambda script: "" if 'echo "CODE' in script else UBUNTU   # no result yet
        state = HOST_OS.status({"ref": ref}, now=ref["since"] + 60)
        self.assertEqual("running", state[0])
        for message in (messages[0], state[2]):
            self.assertIn("needs attention", message)
            self.assertIn("did not finish within 60 seconds", message)
            self.assertIn("cannot tell whether", message)
            self.assertIn("node-1 stays held", message)
        self.assertEqual([], host.let_go_of)

    def test_an_unconfirmed_launch_that_did_start_is_followed_to_its_end(self):
        host, ref, _ = self.start_timed_out()
        self.assertTrue(ref.get("run"), "the launch has an id of its own")
        host.out = lambda script: f"CODE {ref['run']} 0\n" if 'echo "CODE' in script else UBUNTU
        self.assertEqual("succeeded", HOST_OS.status({"ref": ref}, now=ref["since"] + 60)[0])
        self.assertEqual([("node-1", "this-replica/1")], host.let_go_of)

    def test_an_old_result_on_the_host_is_not_this_unconfirmed_launch(self):
        # A STATUS left by an earlier install: the launch may never have run.
        host, ref, _ = self.start_timed_out()
        for old in ("CODE 0\n", "CODE 0a1b2c3d4e5f 0\n", "RUNNING\nCODE 0\n"):
            with self.subTest(old):
                host.out = lambda script, old=old: old if 'echo "CODE' in script else UBUNTU
                state = HOST_OS.status({"ref": ref}, now=ref["since"] + 60)
                self.assertEqual("running", state[0], state[2])
                self.assertEqual([], host.let_go_of)

    def test_a_confirmed_launch_writes_its_id_and_its_result_is_read(self):
        host = Host(lambda script: "STARTED\n" if "systemd-run" in script else UBUNTU)
        self.bind(host)
        ops = Ops()
        HOST_OS.upgrade_start("node-1", ops)
        ref = ops.started[0][2]
        self.assertIn(f"echo {ref['run']} $?", next(s for s in host.scripts if "systemd-run" in s))
        host.out = lambda script: f"CODE {ref['run']} 0\n" if 'echo "CODE' in script else UBUNTU
        self.assertEqual("succeeded", HOST_OS.status({"ref": ref}, now=ref["since"] + 60)[0])

    def test_an_unconfirmed_launch_seen_running_says_so_and_still_needs_its_own_result(self):
        host, ref, _ = self.start_timed_out()
        host.out = lambda script: "RUNNING\nunpacking\n" if 'echo "CODE' in script else UBUNTU
        state = HOST_OS.status({"ref": ref}, now=ref["since"] + 60)
        self.assertEqual(("running", "Installing on node-1: unpacking"), (state[0], state[2]))
        self.assertIn("unconfirmed", ref, "only a result with this launch's id proves it")

    def test_cancelling_an_unconfirmed_launch_claims_nothing_about_the_install(self):
        host, ref, _ = self.start_timed_out()
        item = {"kind": "host-os", "ref": ref}
        HOST_OS.register(HOST_OS.OPS)
        plan, run = HOST_OS.OPS.CANCELLERS["host-os"]
        keeps = " ".join(plan(item)["keeps"])
        self.assertIn("cannot tell whether the updates started on node-1", keeps)
        self.assertNotIn("keep installing", keeps)
        run(item, {})
        self.assertEqual([("node-1", "this-replica/1")], host.let_go_of)

    def test_updates_wait_for_another_change_on_the_host(self):
        host = Host(lambda script: "STARTED\n" if "systemd-run" in script else UBUNTU, held_by="Prepare /dev/sdb on node-1")
        self.bind(host)
        ops = Ops()
        with self.assertRaisesRegex(ValueError, "another change is running on node-1: Prepare /dev/sdb on node-1"):
            HOST_OS.upgrade_start("node-1", ops)
        self.assertFalse([s for s in host.scripts if "systemd-run" in s])
        self.assertFalse(ops.started)

    def test_holding_automatic_updates_is_a_change(self):
        host = Host("DONE\n")
        self.bind(host)
        HOST_OS.set_hold("node-1", True)
        self.assertEqual([("Hold automatic updates on node-1", "")], host.changes)

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


class Slow(Host):
    """A host helper that takes `delay` seconds and counts how many run at once;
    hosts in `broken` print nothing."""

    def __init__(self, out, delay=0.2, broken=()):
        super().__init__(out)
        self.delay, self.broken, self.now, self.most, self.read = delay, set(broken), 0, 0, []
        self._lock = threading.Lock()

    def run(self, node, script, timeout=60, change="", holder=""):
        with self._lock:
            self.now += 1
            self.most = max(self.most, self.now)
            self.read.append(node)
        try:
            time.sleep(self.delay)
            return ("" if node in self.broken else self.out), ""
        finally:
            with self._lock:
                self.now -= 1


class PoolTests(unittest.TestCase):
    """Hosts are read a few at a time, and a broken one is not read every round (#976)."""

    def setUp(self):
        self.data = tempfile.mkdtemp()
        HOST_OS._FAILED.clear()
        self.addCleanup(HOST_OS._FAILED.clear)
        self.nodes = {"items": [{"metadata": {"name": f"node-{i}"},
                                 "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for i in range(6)]}

    def bind(self, host):
        HOST_OS.bind(lambda path: self.nodes, host, lambda force=False: {"distribution": "k3s"}, self.data)

    def test_a_round_reads_at_most_three_hosts_at_once(self):
        host = Slow(UBUNTU)
        self.bind(host)
        started = time.monotonic()
        self.assertEqual(6, len(HOST_OS.tick(now=10 ** 10)))
        took = time.monotonic() - started
        self.assertEqual(3, host.most)
        self.assertLess(took, 0.9, "six hosts at 0.2 s were read one after another")

    def test_check_now_reads_at_most_three_hosts_at_once(self):
        host = Slow(UBUNTU)
        self.bind(host)
        HOST_OS.check_all(wait=True)
        self.assertEqual(3, host.most)
        self.assertEqual(6, len(HOST_OS.report()["checking"]["done"]))

    def test_check_now_starts_a_backing_off_host_afresh(self):
        self.nodes["items"] = self.nodes["items"][:1]
        host = Slow(UBUNTU, delay=0, broken={"node-0"})
        self.bind(host)
        for round_ in range(3):
            HOST_OS.tick(now=10 ** 10 + round_ * 600)
        self.assertEqual(3, HOST_OS._FAILED["node-0"]["count"])
        HOST_OS.check_all(wait=True)
        self.assertNotIn("node-0", HOST_OS._FAILED, "after Check now the next round tries it again")
        HOST_OS.tick(now=10 ** 10 + 3 * 600)
        self.assertEqual(5, len(host.read))

    def test_a_host_that_fails_three_times_waits_for_the_six_hour_cadence(self):
        self.nodes["items"] = self.nodes["items"][:2]
        host = Slow(UBUNTU, delay=0, broken={"node-1"})
        self.bind(host)
        start = 10 ** 10
        for round_ in range(5):
            HOST_OS.tick(now=start + round_ * 600)
        self.assertEqual(3, host.read.count("node-1"), "tried three times, then left")
        HOST_OS.tick(now=start + 2 * 600 + HOST_OS.EVERY)
        self.assertEqual(4, host.read.count("node-1"), "read again at the six-hour cadence")
        HOST_OS.tick(now=start + 2 * 600 + HOST_OS.EVERY + 600)
        self.assertEqual(4, host.read.count("node-1"), "failed again: six hours more")
        host.broken = set()
        HOST_OS.tick(now=start + 2 * 600 + 2 * HOST_OS.EVERY)
        self.assertEqual(5, host.read.count("node-1"))
        self.assertEqual({}, HOST_OS._FAILED, "a read that works starts the count again")


class HelperTests(unittest.TestCase):
    def test_the_host_helper_leaves_room_for_the_tools_it_runs(self):
        import homestead_hostrun as HOSTRUN
        limit = HOSTRUN.body("node-1")["spec"]["containers"][0]["resources"]["limits"]["memory"]
        self.assertEqual("1Gi", limit, "apt-get was killed at 64Mi: nsenter keeps it in the pod's cgroup")
