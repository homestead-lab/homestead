import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_bridge as BRIDGE
import homestead_hostrun as HOSTRUN
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebind import keep_bound

try:
    import yaml
except ImportError:  # the rewrite runs on the host, where netplan brings it
    yaml = None

K3S = """IFACE enp1s0
MAC 18:60:24:f5:e5:09
PHYSICAL
ADDR 192.0.2.108/32 static
ADDR 192.0.2.109/24 dhcp
GW 192.0.2.1
NETPLAN
NETWORKD
SYSTEMDRUN
PYYAML
DEFINED /etc/netplan/00-installer-config.yaml enp1s0
SERVICE k3s
END"""


class Host:
    def __init__(self, answers, held_by=""):
        self.answers, self.scripts = answers, []
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
        for marker, out in self.answers:
            if marker in script:
                return out, ""
        return "", ""


class LookTests(unittest.TestCase):
    def test_the_hosts_own_address_is_carried_not_a_vip_beside_it(self):
        facts = BRIDGE.parse(K3S)
        self.assertEqual(("enp1s0", "192.0.2.109/24", True, ""), (facts["interface"], facts["primary"], facts["dhcp"], BRIDGE.problem(facts)))

    def test_what_is_not_converted(self):
        cases = {"ISBRIDGE": "a bridge already", "BOND": "plain wired", "EXISTS": "br0 already", "NM": "systemd-networkd",
                 "ARMED": "still waiting", "FLANNELIFACE": "flannel-iface"}
        for flag, words in cases.items():
            with self.subTest(flag=flag):
                self.assertIn(words, BRIDGE.problem(BRIDGE.parse(K3S.replace("END", flag + "\nEND"))))
        self.assertIn("not set up in /etc/netplan", BRIDGE.problem(BRIDGE.parse(K3S.replace("DEFINED", "X"))))
        self.assertIn("did not finish", BRIDGE.problem(BRIDGE.parse(K3S.replace("END", ""))))


# CI installs PyYAML (requirements-test.txt), so there a missing one fails rather than skips.
@unittest.skipIf(yaml is None and os.environ.get("CI", "").lower() not in ("true", "1"), "PyYAML is not installed here")
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
      addresses: [192.0.2.109/24]
      routes: [{to: default, via: 192.0.2.1}]
      nameservers: {addresses: [192.0.2.1]}
""", key="lan")
        eth, br = net["ethernets"]["lan"], net["bridges"]["br0"]
        self.assertEqual(({"macaddress": "18:60:24:f5:e5:09"}, "enp1s0"), (eth["match"], eth["set-name"]), "the NIC keeps its naming")
        self.assertNotIn("addresses", eth)
        self.assertEqual((["192.0.2.109/24"], ["lan"]), (br["addresses"], br["interfaces"]))
        self.assertEqual([{"to": "default", "via": "192.0.2.1"}], br["routes"])
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


class Ops:
    def __init__(self):
        self.started = []

    def start(self, kind, title, resource, href, ref, message=""):
        self.started.append(ref)
        return {"id": "op1"}


class HostLeaseTests(unittest.TestCase):
    """The move into the bridge holds its host until the job ends (#597)."""
    GOOD = [(BRIDGE.INSPECT_SCRIPT, K3S), ("python3 -", "OK /var/lib/homestead/netplan-x\n"),
            ("CONFIRMED", "CONFIRMED\n"), ("ip -4", "ADDR\nPORT\nROUTE\nGATEWAY\nARMED\n")]

    def setUp(self):
        keep_bound(self, BRIDGE)

    def bind(self, host):
        BRIDGE.bind(host, lambda path: {"items": [{}]} if "nodes" in path else {"items": []}, lambda *a, **k: None)

    def test_the_host_is_held_from_the_move_until_the_job_ends(self):
        host, ops = Host(self.GOOD), Ops()
        self.bind(host)
        BRIDGE.start("k3s", ops)
        ref = ops.started[0]
        self.assertEqual([("k3s", "Move k3s's enp1s0 into br0")], [h[:2] for h in host.holds])
        self.assertGreater(host.holds[0][2], BRIDGE.ROLLBACK_SECONDS)
        self.assertEqual([], host.let_go_of)
        item = {"ref": ref}
        BRIDGE.status(item, now=ref["since"] + 20)
        self.assertEqual({"this-replica/1"}, {holder for _, holder in host.changes})
        self.assertEqual("succeeded", BRIDGE.status(item, now=ref["since"] + 21)[0])
        self.assertEqual([("k3s", "this-replica/1")], host.let_go_of)

    def test_stopping_tracking_the_job_lets_go_of_the_host_at_once(self):
        host, ops = Host(self.GOOD), Ops()
        self.bind(host)
        BRIDGE.start("k3s", ops)
        item = {"kind": "host-bridge", "ref": ops.started[0]}
        BRIDGE.register(BRIDGE.OPS)  # its canceller is added by register (#622)
        plan, run = BRIDGE.OPS.CANCELLERS["host-bridge"]
        self.assertTrue(any("puts its old network back" in line for line in plan(item)["keeps"]))
        self.assertIn("other changes on k3s can start", run(item, {}))
        self.assertEqual([("k3s", "this-replica/1")], host.let_go_of)

    def test_another_change_on_the_host_refuses_the_move_before_anything_changes(self):
        host = Host(self.GOOD, held_by="Install 3 updates on k3s")
        self.bind(host)
        with self.assertRaisesRegex(ValueError, "another change is running on k3s: Install 3 updates on k3s"):
            BRIDGE.start("k3s", Ops())
        self.assertFalse(host.changes)


class TimedOutStartTests(unittest.TestCase):
    """A move script still running at its limit may be mid-change: the job
    keeps the host held and needs attention, rather than letting go."""

    def start_timed_out(self):
        import homestead_pod_exec as POD_EXEC
        host, messages = Host(HostLeaseTests.GOOD), []
        real = host.run

        def run(node, script, timeout=60, change="", holder=""):
            if "python3 -" in script and change:
                raise POD_EXEC.ExecTimeout("The script on k3s did not finish within 120 seconds", "", "", 120)
            return real(node, script, timeout, change, holder)

        host.run = run
        ops = Ops()
        started = ops.start
        ops.start = lambda kind, title, resource, href, ref, message="": (messages.append(message),
                                                                          started(kind, title, resource, href, ref, message))[1]
        BRIDGE.bind(host, lambda path: {"items": [{}]} if "nodes" in path else {"items": []}, lambda *a, **k: None)
        op = BRIDGE.start("k3s", ops)
        return host, ops, messages, op

    def test_a_timed_out_move_keeps_the_host_held_and_needs_attention(self):
        host, ops, messages, op = self.start_timed_out()
        self.assertEqual([], host.let_go_of)
        ref = ops.started[0]
        self.assertEqual(("unconfirmed", "this-replica/1"), (ref["stage"], ref["lease"]))
        self.assertTrue(op.get("unconfirmed"))
        for message in (messages[0], BRIDGE.status({"ref": ref}, now=ref["since"] + 3600)[2]):
            self.assertIn("needs attention", message)
            self.assertIn("did not finish within 120 seconds", message)
            self.assertIn("cannot tell whether", message)
        self.assertEqual([], host.let_go_of)
        self.assertGreaterEqual(len(host.holds), 2)

    def test_a_hold_that_cannot_be_renewed_is_said_so(self):
        host, ops, _, _ = self.start_timed_out()
        ref = ops.started[0][2] if isinstance(ops.started[0], tuple) else ops.started[0]

        def refused(node, what, seconds=60, holder=""):
            raise RuntimeError("the API server did not answer")

        host.hold = refused
        message = BRIDGE.status({"ref": ref}, now=ref["since"] + 3600)[2]
        self.assertNotIn("k3s stays held", message)
        self.assertIn("could not renew its hold on k3s", message)
        self.assertIn("the API server did not answer", message)
        host.hold = lambda node, what, seconds=60, holder="": "this-replica/1"
        message = BRIDGE.status({"ref": ref}, now=ref["since"] + 7200)[2]
        self.assertIn("k3s stays held", message, "renewed again: held again")

    def test_the_cancel_review_of_an_unconfirmed_job_claims_nothing_about_the_host(self):
        _, ops, _, _ = self.start_timed_out()
        keeps = " ".join(BRIDGE.cancel_plan({"kind": "host-bridge", "ref": ops.started[0]})["keeps"])
        self.assertIn("cannot tell how far the change got on k3s", keeps)
        self.assertIn("check its network first", keeps)
        self.assertNotIn("carries on", keeps)

    def test_a_restart_that_times_out_may_have_been_scheduled_and_is_not_asked_again(self):
        import homestead_pod_exec as POD_EXEC
        host, ops = Host(HostLeaseTests.GOOD), Ops()
        BRIDGE.bind(host, lambda path: {"items": [{}, {}]} if "nodes" in path else {"items": []}, lambda *a, **k: None)
        BRIDGE.start("k3s", ops)
        ref = dict(ops.started[0], stage="confirmed", services=["k3s"])
        restarts = []

        def run(node, script, timeout=60, change="", holder=""):
            restarts.append(script)
            raise POD_EXEC.ExecTimeout("The script on k3s did not finish within 45 seconds", "", "", 45)

        host.run = run
        self.assertEqual("running", BRIDGE.status({"ref": ref}, now=ref["since"] + 10)[0])
        self.assertEqual("restarting", ref["stage"])
        self.assertEqual(1, len(restarts))

    def test_cancelling_the_unconfirmed_job_lets_go_of_the_host(self):
        host, ops, _, _ = self.start_timed_out()
        item = {"kind": "host-bridge", "ref": ops.started[0]}
        BRIDGE.register(BRIDGE.OPS)
        BRIDGE.OPS.CANCELLERS["host-bridge"][1](item, {})
        self.assertEqual([("k3s", "this-replica/1")], host.let_go_of)


class JobTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, BRIDGE)
        self.deleted = []
        self.nodes = 1
        self.item = {"ref": {"node": "k3s", "interface": "enp1s0", "address": "192.0.2.109/24", "bridge": "br0",
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
