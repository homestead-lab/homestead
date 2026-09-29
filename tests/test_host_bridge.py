import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_bridge as BRIDGE

try:
    import yaml
except ImportError:  # the rewrite runs on the host, where netplan brings it
    yaml = None

K3S = """IFACE enp1s0
MAC 18:60:24:f5:e5:09
PHYSICAL
ADDR 192.168.1.108/32 static
ADDR 192.168.1.109/24 dhcp
GW 192.168.1.1
NETPLAN
NETWORKD
SYSTEMDRUN
PYYAML
DEFINED /etc/netplan/00-installer-config.yaml enp1s0
SERVICE k3s
END"""


class Host:
    def __init__(self, answers):
        self.answers, self.scripts = answers, []

    def run(self, node, script, timeout=60):
        self.scripts.append(script)
        for marker, out in self.answers:
            if marker in script:
                return out, ""
        return "", ""


class LookTests(unittest.TestCase):
    def test_the_hosts_own_address_is_carried_not_a_vip_beside_it(self):
        facts = BRIDGE.parse(K3S)
        self.assertEqual(("enp1s0", "192.168.1.109/24", True, ""), (facts["interface"], facts["primary"], facts["dhcp"], BRIDGE.problem(facts)))

    def test_what_is_not_converted(self):
        cases = {"ISBRIDGE": "a bridge already", "BOND": "plain wired", "EXISTS": "br0 already", "NM": "systemd-networkd",
                 "ARMED": "still waiting", "FLANNELIFACE": "flannel-iface"}
        for flag, words in cases.items():
            with self.subTest(flag=flag):
                self.assertIn(words, BRIDGE.problem(BRIDGE.parse(K3S.replace("END", flag + "\nEND"))))
        self.assertIn("not set up in /etc/netplan", BRIDGE.problem(BRIDGE.parse(K3S.replace("DEFINED", "X"))))
        self.assertIn("did not finish", BRIDGE.problem(BRIDGE.parse(K3S.replace("END", ""))))


@unittest.skipIf(yaml is None, "PyYAML is not installed here")
class RewriteTests(unittest.TestCase):
    """The Python the host runs, run here on netplan files like Ubuntu's."""

    def rewrite(self, text, key="enp1s0"):
        script = BRIDGE.convert_script({"file": "FILE", "netplan_id": key, "mac": "18:60:24:f5:e5:09"}, "x")
        code = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "00-installer-config.yaml")
            Path(path).write_text(text)
            subprocess.run([sys.executable, "-c", code, path, key, "br0", "18:60:24:f5:e5:09"], check=True)
            return yaml.safe_load(Path(path).read_text())["network"]

    def test_dhcp_moves_to_the_bridge_with_the_nics_mac(self):
        net = self.rewrite("network:\n  version: 2\n  ethernets:\n    enp1s0:\n      dhcp4: true\n")
        self.assertEqual({"dhcp4": False, "dhcp6": False}, net["ethernets"]["enp1s0"])
        br = net["bridges"]["br0"]
        self.assertEqual((True, ["enp1s0"], "18:60:24:f5:e5:09", "mac"),
                         (br["dhcp4"], br["interfaces"], br["macaddress"], br["dhcp-identifier"]))
        self.assertEqual({"stp": False, "forward-delay": 0}, br["parameters"])

    def test_a_static_address_moves_with_its_routes_and_dns_and_a_renamed_nic_is_named_by_its_id(self):
        net = self.rewrite("""network:
  version: 2
  ethernets:
    lan:
      match: {macaddress: "18:60:24:f5:e5:09"}
      set-name: enp1s0
      addresses: [192.168.1.109/24]
      routes: [{to: default, via: 192.168.1.1}]
      nameservers: {addresses: [192.168.1.1]}
""", key="lan")
        eth, br = net["ethernets"]["lan"], net["bridges"]["br0"]
        self.assertEqual(({"macaddress": "18:60:24:f5:e5:09"}, "enp1s0"), (eth["match"], eth["set-name"]), "the NIC keeps its naming")
        self.assertNotIn("addresses", eth)
        self.assertEqual((["192.168.1.109/24"], ["lan"]), (br["addresses"], br["interfaces"]))
        self.assertEqual([{"to": "default", "via": "192.168.1.1"}], br["routes"])
        self.assertNotIn("dhcp-identifier", br, "a static address asks no DHCP")


class ScriptTests(unittest.TestCase):
    def test_the_rollback_is_armed_before_anything_is_applied_and_the_old_files_are_kept(self):
        script = BRIDGE.convert_script({"file": "/etc/netplan/00-installer-config.yaml", "netplan_id": "enp1s0",
                                        "mac": "18:60:24:f5:e5:09"}, "20260929-120000")
        backup = "/var/lib/homestead/netplan-20260929-120000"
        self.assertLess(script.index(f"cp -a /etc/netplan/. {backup}/"), script.index("python3 -"))
        self.assertLess(script.index("netplan generate"), script.index("--unit=homestead-bridge-rollback"))
        self.assertLess(script.index("--unit=homestead-bridge-rollback"), script.index("--unit=homestead-bridge-apply"))
        self.assertIn(f"--on-active={BRIDGE.ROLLBACK_SECONDS}", script)
        self.assertNotIn("netplan apply\n", script.split("--unit=homestead-bridge-rollback")[0], "nothing applied before the rollback is armed")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.deleted = []
        self.nodes = 1
        self.item = {"ref": {"node": "k3s", "interface": "enp1s0", "address": "192.168.1.109/24", "bridge": "br0",
                             "backup": "/var/lib/homestead/netplan-x", "stage": "applied", "since": 1000, "services": ["k3s"]}}

    def bind(self, host):
        pods = {"items": [{"metadata": {"name": "kube-vip-abcde"}}, {"metadata": {"name": "coredns-1"}}]}
        BRIDGE.bind(host, lambda path: pods if "pods" in path else {"items": [{}] * self.nodes},
                    lambda method, path, body=None, **k: self.deleted.append(path))

    def test_it_is_confirmed_only_when_the_host_is_up_on_the_bridge(self):
        host = Host([("ip -4 -o addr", "ADDR\nPORT\nROUTE\nARMED\nEND")])
        self.bind(host)
        status, _, message = BRIDGE.status(self.item, now=1030)
        self.assertEqual("running", status)
        self.assertIn("the gateway answering", message)
        self.assertFalse(any("systemctl stop" in s for s in host.scripts), "not confirmed while anything is missing")
        host.answers = [("ip -4 -o addr", "ADDR\nPORT\nROUTE\nGATEWAY\nARMED\nEND"), ("systemctl stop", "CONFIRMED")]
        status, _, message = BRIDGE.status(self.item, now=1040)
        self.assertEqual(("running", "confirmed"), (status, self.item["ref"]["stage"]))
        status, _, message = BRIDGE.status(self.item, now=1045)
        self.assertEqual("succeeded", status)
        self.assertEqual(["/api/v1/namespaces/kube-system/pods/kube-vip-abcde"], self.deleted, "kube-vip starts again on br0")

    def test_a_host_that_never_answers_puts_itself_back(self):
        self.bind(Host([]))
        self.assertEqual("running", BRIDGE.status(self.item, now=1100)[0])
        status, _, message = BRIDGE.status(self.item, now=1000 + BRIDGE.ROLLBACK_SECONDS + 61)
        self.assertEqual("failed", status)
        self.assertIn("put its old network back by itself", message)

    def test_more_than_one_host_restarts_k3s_for_flannel(self):
        host = Host([("systemctl restart", "OK")])
        self.nodes = 3
        self.bind(host)
        self.item["ref"]["stage"] = "confirmed"
        status, _, message = BRIDGE.status(self.item, now=2000)
        self.assertEqual(("running", "restarting"), (status, self.item["ref"]["stage"]))
        self.assertIn("systemctl restart k3s", host.scripts[-1])


if __name__ == "__main__":
    unittest.main()
