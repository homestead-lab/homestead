"""#932: the node probe lists the host's iSCSI sessions from its own /sys.

A host lost while a Longhorn engine ran on it can keep a session to the old
instance-manager's address once it comes back, and a later attach there fails
in a loop. The probe already mounts the host's /sys read-only; the kernel's
iSCSI transport class says each session's target, state, address and disks,
so no new privilege is needed to see them."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
SPEC = importlib.util.spec_from_file_location("iscsi_probe", ROOT / "server/probe/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
TARGET = "iqn.2019-10.io.longhorn:pvc-demo-1"


class ISCSIProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch = mock.patch.object(probe, "SYS", str(self.root / "sys"))
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, path, text):
        target = self.root / "sys" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n")

    def session(self, sid, target, address, port="3260", state="LOGGED_IN", disks=(), persistent=True):
        self.put(f"class/iscsi_session/session{sid}/targetname", target)
        self.put(f"class/iscsi_session/session{sid}/state", state)
        for lun, disk in enumerate(disks):
            (self.root / f"sys/class/iscsi_session/session{sid}/device/target3:0:0/3:0:0:{lun}/block/{disk}").mkdir(parents=True)
        # What the kernel also keeps beside the targets: none of it is a disk.
        device = self.root / f"sys/class/iscsi_session/session{sid}/device"
        for other in ("power", "subsystem", f"connection{sid}:0", f"iscsi_session/session{sid}"):
            (device / other).mkdir(parents=True, exist_ok=True)
        (device / "uevent").write_text("DEVTYPE=iscsi_session\n")
        conn = f"class/iscsi_connection/connection{sid}:0"
        if persistent:
            self.put(f"{conn}/persistent_address", address)
            self.put(f"{conn}/persistent_port", port)
        else:
            self.put(f"{conn}/address", address)
            self.put(f"{conn}/port", port)

    def test_each_session_says_its_target_state_address_and_disks(self):
        self.session(67, TARGET, "192.0.2.40", state="FAILED", disks=("sdj",))
        self.session(3, "iqn.2005-10.org.example:other", "198.51.100.7", port="3261")
        self.assertEqual([
            {"sid": 3, "target": "iqn.2005-10.org.example:other", "state": "LOGGED_IN",
             "address": "198.51.100.7", "port": 3261, "disks": []},
            {"sid": 67, "target": TARGET, "state": "FAILED", "address": "192.0.2.40", "port": 3260,
             "disks": ["sdj"]},
        ], probe.iscsi_sessions())

    def test_the_current_address_stands_in_when_no_persistent_one_is_kept(self):
        self.session(5, TARGET, "192.0.2.41", persistent=False)
        self.assertEqual("192.0.2.41", probe.iscsi_sessions()[0]["address"])

    def test_a_host_without_iscsi_has_no_sessions_not_an_unknown(self):
        (self.root / "sys/class").mkdir(parents=True)
        self.assertEqual([], probe.iscsi_sessions())

    def test_the_payload_carries_the_sessions(self):
        self.session(1, TARGET, "192.0.2.40")
        with mock.patch.object(probe, "thermal", return_value=[]), mock.patch.object(probe, "hwmon", return_value=[]):
            self.assertEqual(1, probe.payload()["iscsi"][0]["sid"])


if __name__ == "__main__":
    unittest.main()
