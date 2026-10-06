import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "probe"))
import probe
import homestead_ports as PORTS


def nic(name, carrier=True, speed=1000, master="", counters=None, member=None, mtu=1500):
    counters = {"rx_errors": 0, "tx_errors": 0, "rx_crc_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
                "carrier_changes": 2, **(counters or {})}
    return {"name": name, "kind": "nic", "up": carrier, "master": master, "admin_up": True, "carrier": carrier,
            "speed_mbps": speed if carrier else None, "duplex": "full" if carrier else None, "mtu": mtu,
            "mac": "52:54:00:00:00:01", "driver": "igc", "counters": counters,
            **({"bond_member": member} if member else {})}


def bond(name, slaves, mode="active-backup", partner=None, master=""):
    return {"name": name, "kind": "bond", "up": True, "master": master, "admin_up": True, "carrier": True,
            "speed_mbps": 1000, "duplex": "full", "mtu": 1500, "mac": "52:54:00:00:00:01", "driver": "",
            "counters": {"carrier_changes": 1},
            "bond": {"mode": mode, "slaves": slaves, "active_slave": slaves[0], "mii_status": "up",
                     "ad_partner_mac": partner}}


def member(state="active", mii="up"):
    return {"state": state, "mii_status": mii, "link_failure_count": 0, "aggregator": None}


class ProbePortFactsTests(unittest.TestCase):
    """The probe reads link, speed, counters and bond state from host sysfs."""

    def _iface(self, root, name, files):
        base = Path(root, "class", "net", name)
        for rel, value in files.items():
            Path(base, rel).parent.mkdir(parents=True, exist_ok=True)
            Path(base, rel).write_text(value + "\n")
        return base

    def test_nic_bond_and_member(self):
        with tempfile.TemporaryDirectory() as root:
            self._iface(root, "enp1s0", {"operstate": "up", "flags": "0x1003", "carrier": "1", "speed": "2500",
                                         "duplex": "full", "mtu": "1500", "address": "52:54:00:0a:00:01",
                                         "carrier_changes": "4", "statistics/rx_errors": "7",
                                         "statistics/rx_crc_errors": "5", "statistics/tx_errors": "0",
                                         "statistics/rx_dropped": "1", "statistics/tx_dropped": "0",
                                         "device/vendor": "0x8086", "bonding_slave/state": "active",
                                         "bonding_slave/mii_status": "up", "bonding_slave/link_failure_count": "2"})
            self._iface(root, "enp3s0", {"operstate": "down", "flags": "0x1002", "speed": "-1",
                                         "device/vendor": "0x10ec"})
            self._iface(root, "bond0", {"operstate": "up", "flags": "0x1003", "carrier": "1", "speed": "2500",
                                        "bonding/mode": "802.3ad 4", "bonding/slaves": "enp1s0 enp2s0",
                                        "bonding/active_slave": "", "bonding/mii_status": "up",
                                        "bonding/ad_partner_mac": "00:00:00:00:00:00"})
            old, probe.SYS = probe.SYS, root
            try:
                rows = {row["name"]: row for row in probe.interfaces()}
            finally:
                probe.SYS = old
        nic_row = rows["enp1s0"]
        self.assertEqual((True, True, 2500, "full", 1500), (nic_row["admin_up"], nic_row["carrier"],
                         nic_row["speed_mbps"], nic_row["duplex"], nic_row["mtu"]))
        self.assertEqual({"rx_errors": 7, "tx_errors": 0, "rx_crc_errors": 5, "rx_dropped": 1, "tx_dropped": 0,
                          "carrier_changes": 4}, nic_row["counters"])
        self.assertEqual("active", nic_row["bond_member"]["state"])
        self.assertEqual(2, nic_row["bond_member"]["link_failure_count"])
        # Admin-down: carrier and speed unreadable, which is off, not broken.
        self.assertEqual((False, None, None), (rows["enp3s0"]["admin_up"], rows["enp3s0"]["carrier"], rows["enp3s0"]["speed_mbps"]))
        self.assertEqual("802.3ad", rows["bond0"]["bond"]["mode"])
        self.assertEqual(["enp1s0", "enp2s0"], rows["bond0"]["bond"]["slaves"])
        self.assertEqual("00:00:00:00:00:00", rows["bond0"]["bond"]["ad_partner_mac"])


class PortsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        PORTS.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def probe(self, rows, uptime=100000, uplink="br0"):
        return {"interfaces": rows, "uptime_s": uptime, "default_interface": uplink}

    def kinds(self, rep, node="h1"):
        return {(c["iface"], c["kind"]): c for c in rep["hosts"][node]["conditions"]}

    def test_a_healthy_bonded_host_raises_nothing_and_carries_down_the_chain(self):
        rows = [nic("enp1s0", master="bond0", member=member()), nic("enp2s0", master="bond0", member=member("backup")),
                nic("enp3s0", carrier=False), bond("bond0", ["enp1s0", "enp2s0"], master="br0"),
                {"name": "br0", "kind": "bridge", "up": True, "master": "", "carrier": True, "counters": {}}]
        rep = PORTS.report({"h1": self.probe(rows)}, {"h1": {"uplink": "br0", "networks": {"br0": ["default/lan"]}}})
        self.assertEqual([], rep["conditions"])
        ports = {p["name"]: p for p in rep["hosts"]["h1"]["ports"]}
        self.assertEqual(["host address", "default/lan"], ports["enp2s0"]["carries"])
        self.assertTrue(ports["enp1s0"]["uplink"])
        self.assertEqual([], ports["enp3s0"]["carries"])           # a spare with no cable is fine
        self.assertEqual("down", ports["enp3s0"]["link"])

    def test_a_bond_member_without_link(self):
        rows = [nic("enp1s0", master="bond0", member=member()),
                nic("enp2s0", carrier=False, master="bond0", member=member("backup", "down")),
                bond("bond0", ["enp1s0", "enp2s0"])]
        found = self.kinds(PORTS.report({"h1": self.probe(rows, uplink="bond0")}))
        self.assertEqual("degraded", found[("bond0", "members")]["severity"])
        self.assertNotIn(("enp2s0", "down"), found)              # said once, as the bond's

    def test_a_bond_with_no_working_member_is_critical(self):
        rows = [nic("enp1s0", carrier=False, master="bond0", member=member("active", "down")),
                bond("bond0", ["enp1s0"])]
        found = self.kinds(PORTS.report({"h1": self.probe(rows, uplink="bond0")}))
        self.assertEqual("critical", found[("bond0", "members")]["severity"])

    def test_a_lan_network_port_without_link_is_critical(self):
        rows = [nic("eth0"), nic("eth1", carrier=False)]
        rep = PORTS.report({"h1": self.probe(rows, uplink="eth0")}, {"h1": {"networks": {"eth1": ["iot/cams"]}}})
        self.assertEqual("critical", self.kinds(rep)[("eth1", "down")]["severity"])

    def test_lacp_without_partner_after_two_minutes(self):
        rows = [nic("enp1s0", master="bond0", member=member()), nic("enp2s0", master="bond0", member=member()),
                bond("bond0", ["enp1s0", "enp2s0"], mode="802.3ad", partner="00:00:00:00:00:00")]
        PORTS.observe({"h1": self.probe(rows)}, now=1000)
        self.assertNotIn(("bond0", "lacp"), self.kinds(PORTS.report({"h1": self.probe(rows)}, now=1060)))
        self.assertIn(("bond0", "lacp"), self.kinds(PORTS.report({"h1": self.probe(rows)}, now=1130)))
        rows[2]["bond"]["ad_partner_mac"] = "aa:bb:cc:00:00:01"
        PORTS.observe({"h1": self.probe(rows)}, now=1200)
        self.assertNotIn(("bond0", "lacp"), self.kinds(PORTS.report({"h1": self.probe(rows)}, now=1300)))

    def test_slower_than_it_was_and_slower_than_its_peers(self):
        PORTS.observe({"h1": self.probe([nic("eth0", speed=2500)], uplink="eth0")}, now=1000)
        rep = PORTS.report({"h1": self.probe([nic("eth0", speed=1000)], uplink="eth0")}, now=1100)
        self.assertIn("2.5 Gb/s", self.kinds(rep)[("eth0", "slower")]["body"])
        self.assertEqual(2500, rep["hosts"]["h1"]["ports"][0]["was_mbps"])
        rows = [nic("enp1s0", speed=2500, master="bond0", member=member()),
                nic("enp2s0", speed=1000, master="bond0", member=member("backup")), bond("bond0", ["enp1s0", "enp2s0"])]
        found = self.kinds(PORTS.report({"h1": self.probe(rows, uplink="bond0")}, now=1100))
        self.assertIn("bond peers", found[("enp2s0", "slower")]["body"])

    def test_errors_and_flaps_over_the_hour(self):
        PORTS.observe({"h1": self.probe([nic("eth0", counters={"rx_errors": 10, "carrier_changes": 2})], uplink="eth0")}, now=1000)
        later = self.probe([nic("eth0", counters={"rx_errors": 400, "rx_crc_errors": 380, "carrier_changes": 6})], uplink="eth0")
        # Too soon to say anything about an hour.
        self.assertEqual({}, self.kinds(PORTS.report({"h1": later}, now=1300)))
        found = self.kinds(PORTS.report({"h1": later}, now=2000))
        self.assertIn("390 errors", found[("eth0", "errors")]["body"])
        self.assertIn("380 of them CRC", found[("eth0", "errors")]["body"])
        self.assertIn(("eth0", "flaps"), found)

    def test_a_reboot_starts_a_new_hour(self):
        PORTS.observe({"h1": self.probe([nic("eth0", counters={"rx_errors": 900})], uptime=100000, uplink="eth0")}, now=1000)
        PORTS.observe({"h1": self.probe([nic("eth0", counters={"rx_errors": 1})], uptime=60, uplink="eth0")}, now=2000)
        rep = PORTS.report({"h1": self.probe([nic("eth0", counters={"rx_errors": 1})], uptime=1060, uplink="eth0")}, now=3000)
        self.assertEqual(0, rep["hosts"]["h1"]["ports"][0]["errors"])

    def test_an_old_probe_and_a_missing_one_keep_their_alerts(self):
        rep = PORTS.report({"h1": {"interfaces": [{"name": "eth0", "kind": "nic", "up": True, "master": ""}]}, "h2": None})
        self.assertFalse(rep["hosts"]["h1"]["available"])
        self.assertIn("older", rep["hosts"]["h1"]["reason"])
        facts = PORTS.alert_facts(rep)
        self.assertEqual([{"unknown_prefix": "ports:h1:"}, {"unknown_prefix": "ports:h2:"}], facts)

    def test_alerts_link_to_the_hosts_network_section(self):
        rows = [nic("eth0"), nic("eth1", carrier=False)]
        rep = PORTS.report({"h1": self.probe(rows, uplink="eth0")}, {"h1": {"networks": {"eth1": ["iot/cams"]}}})
        [fact] = PORTS.alert_facts(rep)
        self.assertEqual(("ports:h1:eth1:down", "outage", "/nodes?node=h1&section=network"),
                         (fact["key"], fact["category"], fact["href"]))

    def test_the_same_lan_network_on_different_mtus(self):
        rep = PORTS.report({"h1": self.probe([nic("eth1", mtu=9000)]), "h2": self.probe([nic("eth1", mtu=1500)])},
                           {"h1": {"networks": {"eth1": ["default/storage"]}}, "h2": {"networks": {"eth1": ["default/storage"]}}})
        [mtu] = [c for c in rep["conditions"] if c["kind"] == "mtu"]
        self.assertEqual("info", mtu["severity"])
        self.assertEqual([], [f for f in PORTS.alert_facts(rep) if "key" in f])


if __name__ == "__main__":
    unittest.main()
