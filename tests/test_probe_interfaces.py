import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "probe"))
import probe


class ProbeInterfaceTests(unittest.TestCase):
    """The host's interfaces, for a LAN network off Harvester: NICs, bridges
    and the bridge a NIC is in - never the pod network's own."""

    def test_nics_bridges_and_what_they_are_in(self):
        with tempfile.TemporaryDirectory() as sys_root:
            net = Path(sys_root, "class", "net")
            for name, parts in (("eth0", ["device"]), ("enp2s0", ["device"]), ("br0", ["bridge"]),
                                ("bond0", ["bonding"]), ("eth0.20", []), ("veth12ab", ["device"]),
                                ("flannel.1", []), ("cni0", ["bridge"]), ("lo", [])):
                (net / name).mkdir(parents=True)
                (net / name / "operstate").write_text("up\n")
                for part in parts:
                    (net / name / part).mkdir()
            try:
                os.symlink(net / "br0", net / "enp2s0" / "master")
                linked = True
            except OSError:  # Windows without symlink rights
                linked = False
            old, probe.SYS = probe.SYS, sys_root
            try:
                rows = {row["name"]: row for row in probe.interfaces()}
            finally:
                probe.SYS = old
        self.assertEqual({"eth0", "enp2s0", "br0", "bond0", "eth0.20"}, set(rows))
        self.assertEqual("nic", rows["enp2s0"]["kind"])
        if linked:
            self.assertEqual("br0", rows["enp2s0"]["master"])
        self.assertEqual(("bridge", "bond", "vlan"), (rows["br0"]["kind"], rows["bond0"]["kind"], rows["eth0.20"]["kind"]))
        self.assertTrue(rows["eth0"]["up"])


if __name__ == "__main__":
    unittest.main()
