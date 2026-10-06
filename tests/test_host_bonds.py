import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_bonds as BONDS

try:
    import yaml
except ImportError:          # the edit runs on the host, which has it; CI's image may not
    yaml = None


def nic(name, carrier=True, master="", mac=None, speed=1000):
    return {"name": name, "mac": mac or f"52:54:00:00:00:{name[-2:]}", "carrier": carrier, "master": master,
            "speed": speed if carrier else None}


def facts(kind="nic", iface="enp1s0", nics=None, files=None, bond=None, ports=None, **extra):
    base = {"complete": True, "iface": iface, "kind": kind, "address": "192.0.2.12/24", "dhcp": True, "gateway": "192.0.2.1",
            "netplan": True, "networkd": True, "nm": False, "systemd_run": True, "pyyaml": True, "armed": False,
            "services": ["k3s"], "flannel_iface": False, "bond_exists": False,
            "nics": nics if nics is not None else [nic("enp1s0"), nic("enp2s0"), nic("enp3s0", carrier=False)],
            "files": files if files is not None else {"/etc/netplan/50-cloud-init.yaml": {
                "renderer": "", "cloud_init": True, "bonds": {}, "bridges": {},
                "ethernets": {"enp1s0": {"match": {}, "set-name": "", "addressed": True}}}},
            "bond": bond or {}, "bridge_ports": ports or []}
    base.update(extra)
    return base


class PlanTests(unittest.TestCase):
    def test_a_plain_nic_becomes_a_bond_with_its_address_and_mac(self):
        p = BONDS.plan("h1", {"members": ["enp2s0"]}, facts())
        self.assertEqual([], p["refusals"])
        self.assertEqual(("create", ["enp1s0", "enp2s0"], "bond0", "enp1s0"), (p["action"], p["members"], p["carries_on"], p["primary"]))
        self.assertTrue(p["renamed"])
        self.assertEqual("52:54:00:00:00:s0", p["spec"]["mac"])
        self.assertEqual({"mode": "active-backup", "mii-monitor-interval": 100, "primary": "enp1s0"}, p["spec"]["params"])
        self.assertTrue(any("kube-vip restarts" in w for w in p["warnings"]))
        self.assertTrue(any("cloud-init" in w for w in p["warnings"]))

    def test_under_a_bridge_the_address_stays_on_the_bridge(self):
        files = {"/etc/netplan/50-cloud-init.yaml": {"renderer": "", "cloud_init": False, "bonds": {},
                 "bridges": {"br0": {"interfaces": ["enp1s0"]}}, "ethernets": {"enp1s0": {"match": {}, "set-name": "", "addressed": False}}}}
        nics = [nic("enp1s0", master="br0"), nic("enp2s0")]
        p = BONDS.plan("h1", {"members": ["enp1s0", "enp2s0"]}, facts("bridge", "br0", nics, files, ports=["enp1s0"]))
        self.assertEqual([], p["refusals"])
        self.assertEqual(("br0", False), (p["carries_on"], p["renamed"]))

    def test_refusals(self):
        cases = [
            ({"members": []}, facts(), "two or more NICs"),
            ({"members": ["enp3s0"]}, facts(), "enp3s0 has no link"),
            ({"members": ["enp9s0"]}, facts(), "h1 has no NIC enp9s0"),
            ({"members": ["enp2s0"], "mode": "802.3ad"}, facts(), "LACP group"),
            ({"members": ["enp2s0"]}, facts(nics=[nic("enp1s0"), nic("enp2s0", master="br9")]), "enp2s0 is in br9 already"),
            ({"members": ["enp2s0"]}, facts(nm=True), "systemd-networkd"),
            ({"members": ["enp2s0"]}, facts(armed=True), "still waiting to be checked"),
            ({"members": ["enp2s0"]}, facts(flannel_iface=True), "flannel-iface"),
            ({"members": ["enp2s0"], "mode": "broadcast"}, facts(), "not a bond mode"),
        ]
        for req, f, words in cases:
            with self.subTest(words=words):
                self.assertIn(words, " ".join(BONDS.plan("h1", req, f)["refusals"]))
        p = BONDS.plan("h1", {"members": ["enp2s0"]}, facts(), busy="Bond enp1s0 + enp2s0 on h2", servers_ready=(1, 2))
        said = " ".join(p["refusals"])
        self.assertIn("one host at a time", said)
        self.assertIn("quorum", said)

    def test_an_empty_bond_left_behind_is_made_again_not_refused(self):
        p = BONDS.plan("h1", {"members": ["enp2s0"]}, facts(bond_exists=True, bond_empty=True))
        self.assertEqual([], p["refusals"])
        self.assertTrue(p["spec"]["recreate"])
        self.assertIn("not carrying its address", " ".join(BONDS.plan("h1", {"members": ["enp2s0"]}, facts(bond_exists=True))["refusals"]))

    def test_a_switched_off_spare_is_allowed_and_said(self):
        nics = [nic("enp1s0"), dict(nic("enp2s0"), carrier=None, speed=None)]
        p = BONDS.plan("h1", {"members": ["enp2s0"]}, facts(nics=nics))
        self.assertEqual([], p["refusals"])
        self.assertTrue(any("switched off" in w for w in p["warnings"]))

    def test_a_nic_with_an_address_of_its_own_is_not_taken(self):
        files = facts()["files"]
        files["/etc/netplan/50-cloud-init.yaml"]["ethernets"]["enp2s0"] = {"match": {}, "set-name": "", "addressed": True}
        self.assertIn("address of its own", " ".join(BONDS.plan("h1", {"members": ["enp2s0"]}, facts(files=files))["refusals"]))

    def test_overrides(self):
        p = BONDS.plan("h1", {"members": ["enp2s0", "enp3s0"], "allow_down": True, "mode": "802.3ad", "lacp_confirmed": True}, facts())
        self.assertEqual([], p["refusals"])
        self.assertEqual({"mode": "802.3ad", "mii-monitor-interval": 100, "lacp-rate": "fast", "transmit-hash-policy": "layer3+4"},
                         p["spec"]["params"])

    def bonded(self):
        files = {"/etc/netplan/60-homestead.yaml": {"renderer": "", "cloud_init": False, "bridges": {},
                 "ethernets": {"enp1s0": {"match": {}, "set-name": "", "addressed": False}, "enp2s0": {"match": {}, "set-name": "", "addressed": False}},
                 "bonds": {"bond0": {"interfaces": ["enp1s0", "enp2s0"], "parameters": {"mode": "active-backup"}}}}}
        nics = [nic("enp1s0", master="bond0"), nic("enp2s0", master="bond0"), nic("enp3s0")]
        return facts("bond", "bond0", nics, files, bond={"mode": "active-backup", "slaves": ["enp1s0", "enp2s0"], "active": "enp1s0"})

    def test_a_mode_change_makes_the_bond_again_and_a_rollback_always_does(self):
        p = BONDS.plan("h1", {"action": "change", "members": ["enp1s0", "enp2s0"], "mode": "802.3ad", "lacp_confirmed": True}, self.bonded())
        self.assertTrue(p["spec"]["recreate"])
        script = BONDS.change_script(p["spec"], "1")
        self.assertIn("--on-active=3 /bin/sh -c 'ip link delete bond0 2>/dev/null; netplan apply'", script)
        rollback = next(line for line in script.splitlines() if "--unit=homestead-bond-rollback" in line)
        self.assertIn("ip link delete bond0 2>/dev/null; netplan apply'", rollback)
        # Back to one NIC: netplan would leave bond0 behind, empty and down.
        removed = BONDS.plan("h1", {"action": "remove", "keep": "enp1s0"}, self.bonded())
        self.assertIn("--on-active=3 /bin/sh -c 'ip link delete bond0 2>/dev/null; netplan apply'", BONDS.change_script(removed["spec"], "1"))
        same = BONDS.plan("h1", {"action": "change", "members": ["enp1s0", "enp2s0", "enp3s0"]}, self.bonded())
        self.assertFalse(same["spec"]["recreate"])
        self.assertIn("--on-active=3 /bin/sh -c 'netplan apply'", BONDS.change_script(same["spec"], "1"))

    def test_change_and_remove_a_bond(self):
        p = BONDS.plan("h1", {"action": "change", "members": ["enp1s0", "enp2s0", "enp3s0"]}, self.bonded())
        self.assertEqual([], p["refusals"])
        self.assertFalse(p["renamed"])
        # The member carrying traffic is not taken out under it.
        p = BONDS.plan("h1", {"action": "change", "members": ["enp2s0", "enp3s0"]}, self.bonded())
        self.assertIn("carrying traffic now", " ".join(p["refusals"]))
        p = BONDS.plan("h1", {"action": "remove", "keep": "enp1s0"}, self.bonded())
        self.assertEqual([], p["refusals"])
        self.assertEqual(("enp1s0", True), (p["carries_on"], p["renamed"]))
        self.assertIn("is on bond0 already", " ".join(BONDS.plan("h1", {"action": "create", "members": ["enp3s0"]}, self.bonded())["refusals"]))


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class EditTests(unittest.TestCase):
    """The edit that runs on the host, run here against netplan files."""

    def run_edit(self, document, spec):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "50.yaml")
            with open(path, "w") as handle:
                yaml.safe_dump(document, handle)
            subprocess.run([sys.executable, "-c", BONDS.EDIT, json.dumps({**spec, "file": path})], check=True)
            with open(path) as handle:
                return yaml.safe_load(handle)["network"]

    def test_create_from_a_plain_nic(self):
        doc = {"network": {"version": 2, "ethernets": {"enp1s0": {"dhcp4": True, "match": {"macaddress": "52:54:00:00:00:01"},
                                                                  "set-name": "enp1s0", "nameservers": {"addresses": ["192.0.2.1"]}}}}}
        p = BONDS.plan("h1", {"members": ["enp2s0"]}, facts())
        net = self.run_edit(doc, p["spec"])
        self.assertEqual({"dhcp4": False, "dhcp6": False, "match": {"macaddress": "52:54:00:00:00:01"}, "set-name": "enp1s0"}, net["ethernets"]["enp1s0"])
        self.assertEqual({"dhcp4": False, "dhcp6": False}, net["ethernets"]["enp2s0"])
        bond = net["bonds"]["bond0"]
        self.assertEqual((["enp1s0", "enp2s0"], True, "mac", "52:54:00:00:00:s0"),
                         (bond["interfaces"], bond["dhcp4"], bond["dhcp-identifier"], bond["macaddress"]))
        self.assertEqual({"addresses": ["192.0.2.1"]}, bond["nameservers"])

    def test_create_under_a_bridge(self):
        doc = {"network": {"version": 2, "ethernets": {"enp1s0": {"dhcp4": False}},
                           "bridges": {"br0": {"interfaces": ["enp1s0"], "dhcp4": True, "macaddress": "52:54:00:00:00:01"}}}}
        files = {"x": {"renderer": "", "cloud_init": False, "bonds": {}, "bridges": {"br0": {"interfaces": ["enp1s0"]}},
                       "ethernets": {"enp1s0": {"match": {}, "set-name": "", "addressed": False}}}}
        p = BONDS.plan("h1", {"members": ["enp1s0", "enp2s0"]}, facts("bridge", "br0", [nic("enp1s0", master="br0"), nic("enp2s0")], files, ports=["enp1s0"]))
        net = self.run_edit(doc, p["spec"])
        self.assertEqual(["bond0"], net["bridges"]["br0"]["interfaces"])
        self.assertTrue(net["bridges"]["br0"]["dhcp4"])
        self.assertEqual(["enp1s0", "enp2s0"], net["bonds"]["bond0"]["interfaces"])
        self.assertFalse(net["bonds"]["bond0"]["dhcp4"])

    def test_change_then_remove(self):
        doc = {"network": {"version": 2, "ethernets": {"enp1s0": {"dhcp4": False}, "enp2s0": {"dhcp4": False}},
                           "bonds": {"bond0": {"interfaces": ["enp1s0", "enp2s0"], "dhcp4": True, "dhcp-identifier": "mac",
                                               "macaddress": "52:54:00:00:00:01", "parameters": {"mode": "active-backup"}}}}}
        changed = self.run_edit(doc, BONDS.plan("h1", {"action": "change", "members": ["enp1s0", "enp2s0", "enp3s0"]},
                                                PlanTests.bonded(None))["spec"])
        self.assertEqual(["enp1s0", "enp2s0", "enp3s0"], changed["bonds"]["bond0"]["interfaces"])
        removed = self.run_edit(doc, BONDS.plan("h1", {"action": "remove", "keep": "enp1s0"}, PlanTests.bonded(None))["spec"])
        self.assertNotIn("bonds", removed)
        self.assertEqual((True, "mac"), (removed["ethernets"]["enp1s0"]["dhcp4"], removed["ethernets"]["enp1s0"]["dhcp-identifier"]))


class Ops:
    def __init__(self):
        self.started = []

    def start(self, kind, title, resource, href, ref, message=""):
        self.started.append((kind, title, ref))
        return {"id": "op1"}


class Host:
    """A host answering the scripts: inspect, change, check, confirm."""

    def __init__(self, f, check=""):
        self.facts, self.check, self.ran = f, check, []

    def run(self, node, script, timeout=60):
        self.ran.append(script)
        if script == BONDS.INSPECT_SCRIPT:
            return "FACTS " + json.dumps({k: v for k, v in self.facts.items() if k != "complete"}) + "\nEND\n", ""
        if script.startswith("set -e"):
            return "OK /var/lib/homestead/netplan-1\n", ""
        if script == BONDS.CONFIRM_SCRIPT:
            return "CONFIRMED\n", ""
        if script == BONDS.ROLLBACK_NOW:
            return "STARTED\n", ""
        return self.check, ""


class JobTests(unittest.TestCase):
    def setUp(self):
        self.deleted = []
        BONDS.bind(None, lambda path: {"items": [{"metadata": {"name": "kube-vip-x"}}]} if "pods" in path else
                   {"items": [{}, {}]} if path == "/api/v1/nodes" else {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                   lambda verb, path, *a, **k: self.deleted.append(path))

    def start(self, check, req=None):
        host = Host(facts(), check)
        BONDS.hostrun = host
        ops = Ops()
        BONDS.start("h1", req or {"members": ["enp2s0"]}, ops)
        return host, ops.started[0]

    def test_a_good_change_is_confirmed_then_kube_vip_and_k3s_follow(self):
        good = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nUP enp1s0\nIN enp2s0\nUP enp2s0\nARMED\nEND\n"
        host, (kind, title, ref) = self.start(good)
        self.assertEqual((BONDS.KIND, "Bond enp1s0 + enp2s0 on h1"), (kind, title))
        item = {"ref": ref}
        self.assertEqual("running", BONDS.status(item, now=ref["since"] + 5)[0])
        status, _, message = BONDS.status(item, now=ref["since"] + 30)
        self.assertIn("rollback is disarmed", message)
        self.assertIn(BONDS.CONFIRM_SCRIPT, host.ran)
        self.assertIn("Restarting k3s", BONDS.status(item, now=ref["since"] + 31)[2])
        self.assertTrue(any("kube-vip-x" in path for path in self.deleted))
        self.assertEqual("succeeded", BONDS.status(item, now=ref["since"] + 80)[0])

    def test_a_host_that_never_answers_puts_itself_back(self):
        host, (_, _, ref) = self.start("")
        item = {"ref": ref}
        self.assertIn("puts itself back", BONDS.status(item, now=ref["since"] + 60)[2])
        status, _, message = BONDS.status(item, now=ref["since"] + BONDS.ROLLBACK_SECONDS + 61)
        self.assertEqual("failed", status)
        self.assertNotIn(BONDS.CONFIRM_SCRIPT, host.ran)

    def test_no_lacp_partner_rolls_back_at_once(self):
        up = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nIN enp2s0\nARMED\nEND\n"
        host, (_, _, ref) = self.start(up, {"members": ["enp2s0"], "mode": "802.3ad", "lacp_confirmed": True})
        item = {"ref": ref}
        self.assertIn("waiting for the switch", BONDS.status(item, now=ref["since"] + 30)[2])
        self.assertIn("putting its old network back now", BONDS.status(item, now=ref["since"] + BONDS.LACP_WAIT + 20)[2])
        self.assertIn(BONDS.ROLLBACK_NOW, host.ran)
        self.assertNotIn(BONDS.CONFIRM_SCRIPT, host.ran)
        self.assertEqual("failed", BONDS.status(item, now=ref["since"] + BONDS.LACP_WAIT + 60)[0])

    def test_lacp_is_checked_on_the_host_too(self):
        # An 802.3ad bond with no partner can carry nothing: Homestead cannot
        # reach the host to check, so the host checks for itself and says why.
        p = BONDS.plan("h1", {"members": ["enp2s0"], "mode": "802.3ad", "lacp_confirmed": True}, facts())
        script = BONDS.change_script(p["spec"], "1")
        self.assertIn(f"--unit={BONDS.LACP_UNIT} --on-active={3 + BONDS.LACP_WAIT}", script)
        self.assertIn(f"touch {BONDS.LACP_FAILED}; systemctl start {BONDS.UNIT}.service", script)
        self.assertIn(f"systemctl stop {BONDS.UNIT}.timer {BONDS.LACP_UNIT}.timer", script)
        plain = BONDS.change_script(BONDS.plan("h1", {"members": ["enp2s0"]}, facts())["spec"], "1")
        self.assertNotIn(BONDS.LACP_UNIT + " --on", plain.replace(".timer", ""))
        host, (_, _, ref) = self.start("LACPFAILED\nEND\n", {"members": ["enp2s0"], "mode": "802.3ad", "lacp_confirmed": True})
        status, _, message = BONDS.status({"ref": ref}, now=ref["since"] + 90)
        self.assertEqual("failed", status)
        self.assertIn("did not answer LACP", message)
        self.assertNotIn(BONDS.CONFIRM_SCRIPT, host.ran)

    def test_a_stale_review_changes_nothing(self):
        host = Host(facts())
        BONDS.hostrun = host
        with self.assertRaisesRegex(ValueError, "review it again"):
            BONDS.start("h1", {"members": ["enp2s0"], "digest": "stale"}, Ops())
        self.assertFalse(any(s.startswith("set -e") for s in host.ran))


if __name__ == "__main__":
    unittest.main()
