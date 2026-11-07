import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_bonds as BONDS
import homestead_hostrun as HOSTRUN

try:
    import yaml
except ImportError:          # the edit runs on the host, which has it; CI's image may not
    yaml = None


def nic(name, carrier=True, master="", mac=None, speed=1000):
    return {"name": name, "mac": mac or f"52:54:00:00:00:{name[-2:]}", "carrier": carrier, "master": master,
            "speed": speed if carrier else None}


class Kubelet:
    """The host's kubelet as its Node shows it: Ready, last heard from at
    `beat` (seconds, on the host's clock). A test moves `beat` past the
    restart to say the kubelet came back."""

    def __init__(self, beat=0):
        self.beat = beat

    def node(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.beat))
        return {"status": {"conditions": [{"type": "Ready", "status": "True", "lastHeartbeatTime": stamp,
                                           "lastTransitionTime": stamp}]}}


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


# CI installs PyYAML (requirements-test.txt), so there a missing one fails rather than skips.
@unittest.skipIf(yaml is None and os.environ.get("CI", "").lower() not in ("true", "1"), "PyYAML is not installed")
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

    Busy = HOSTRUN.Busy

    def __init__(self, f, check="", held_by=""):
        self.facts, self.check, self.ran = f, check, []
        self.held_by, self.holds, self.let_go_of, self.changes = held_by, [], [], []

    def hold(self, node, what, seconds=60, holder=""):
        if self.held_by:
            raise HOSTRUN.Busy(f"another change is running on {node}: {self.held_by}")
        self.holds.append((node, what, seconds))
        return "this-replica/1"

    def let_go(self, node, holder):
        self.let_go_of.append((node, holder))

    def run(self, node, script, timeout=60, change="", holder=""):
        self.ran.append(script)
        if change:
            self.changes.append((script, change, holder))
        if script == BONDS.INSPECT_SCRIPT:
            return "FACTS " + json.dumps({k: v for k, v in self.facts.items() if k != "complete"}) + "\nEND\n", ""
        if script.startswith("set -e"):
            return "OK /var/lib/homestead/netplan-1\n", ""
        if script == BONDS.CONFIRM_SCRIPT:
            return "CONFIRMED\n", ""
        if script == BONDS.ROLLBACK_NOW:
            return "STARTED\n", ""
        if "systemctl restart" in script:
            return "OK\n", ""
        return self.check, ""


class HostLeaseTests(unittest.TestCase):
    """A bond change holds its host from the change until the job ends, so no
    other change (a disk, OS updates) runs there while the network settles
    or rolls back (#597)."""

    def setUp(self):
        self.kubelet = Kubelet()
        BONDS.bind(None, lambda path: {"items": [{"metadata": {"name": "kube-vip-x"}}]} if "pods" in path else
                   {"items": [{}, {}]} if path == "/api/v1/nodes" else self.kubelet.node(),
                   lambda verb, path, *a, **k: None)

    def test_the_host_is_held_from_the_change_until_the_job_ends(self):
        good = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nUP enp1s0\nIN enp2s0\nUP enp2s0\nARMED\nEND\n"
        host, ops = Host(facts(), good), Ops()
        BONDS.hostrun = host
        BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        ref = ops.started[0][2]
        self.assertEqual([("h1", "Bond enp1s0 + enp2s0 on h1")], [h[:2] for h in host.holds])
        self.assertGreater(host.holds[0][2], BONDS.ROLLBACK_SECONDS, "held past the rollback")
        self.assertEqual("this-replica/1", ref["lease"])
        self.assertEqual([], host.let_go_of, "still held when start returns")
        item = {"ref": ref}
        BONDS.status(item, now=ref["since"] + 30)
        BONDS.status(item, now=ref["since"] + 31)
        self.assertEqual({"this-replica/1"}, {holder for _, _, holder in host.changes},
                         "the change, confirm and restart all run inside the hold")
        self.kubelet.beat = ref["since"] + 20  # k3s is back: its kubelet posts status
        self.assertEqual("succeeded", BONDS.status(item, now=ref["since"] + 80)[0])
        self.assertEqual([("h1", "this-replica/1")], host.let_go_of)
        self.assertNotIn("lease", ref)

    def test_a_job_that_fails_lets_go_of_the_host(self):
        host, ops = Host(facts(), ""), Ops()
        BONDS.hostrun = host
        BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        ref = ops.started[0][2]
        self.assertEqual("failed", BONDS.status({"ref": ref}, now=ref["since"] + BONDS.ROLLBACK_SECONDS + 61)[0])
        self.assertEqual([("h1", "this-replica/1")], host.let_go_of)

    def test_another_change_on_the_host_refuses_the_bond_before_anything_changes(self):
        host = Host(facts(), "", held_by="Prepare /dev/sdb on h1")
        BONDS.hostrun = host
        with self.assertRaisesRegex(ValueError, "another change is running on h1: Prepare /dev/sdb on h1"):
            BONDS.start("h1", {"members": ["enp2s0"]}, Ops())
        self.assertFalse(host.changes)

    def test_stopping_tracking_the_job_lets_go_of_the_host_at_once(self):
        host, ops = Host(facts(), ""), Ops()
        BONDS.hostrun = host
        BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        item = {"kind": BONDS.KIND, "ref": ops.started[0][2]}
        BONDS.register(BONDS.OPS)  # its canceller is added by register (#622)
        plan, run = BONDS.OPS.CANCELLERS[BONDS.KIND]
        self.assertTrue(any("puts its old network back" in line for line in plan(item)["keeps"]))
        self.assertIn("other changes on h1 can start", run(item, {}))
        self.assertEqual([("h1", "this-replica/1")], host.let_go_of)
        self.assertNotIn("lease", item["ref"])

    def test_a_job_whose_node_is_gone_lets_go_of_the_host(self):
        import urllib.error
        host, ops = Host(facts(), ""), Ops()
        BONDS.hostrun = host
        BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        ref = dict(ops.started[0][2], stage="restarting")

        def gone(path):
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)

        BONDS.kget = gone
        with self.assertRaises(urllib.error.HTTPError):
            BONDS.status({"ref": ref}, now=ref["since"] + 60)
        self.assertEqual([("h1", "this-replica/1")], host.let_go_of, "the job fails on a 404: the host is let go of")

    def test_a_change_the_host_refuses_lets_go_at_once(self):
        host, ops = Host(facts(), ""), Ops()
        host.run = lambda node, script, timeout=60, change="", holder="": (
            ("ERR netplan is not there\n", "") if change else Host.run(host, node, script, timeout))
        BONDS.hostrun = host
        with self.assertRaisesRegex(ValueError, "was not changed"):
            BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        self.assertEqual([("h1", "this-replica/1")], host.let_go_of)
        self.assertFalse(ops.started)


class TimedOutStartTests(unittest.TestCase):
    """A change script still running at its limit may be mid-change: the job
    keeps the host held and needs attention, rather than letting go."""

    def setUp(self):
        BONDS.bind(None, lambda path: {"items": [{}, {}]}, lambda verb, path, *a, **k: None)

    def start_timed_out(self):
        import homestead_pod_exec as POD_EXEC
        host, ops, messages = Host(facts(), ""), Ops(), []
        real = host.run

        def run(node, script, timeout=60, change="", holder=""):
            if script.startswith("set -e"):
                raise POD_EXEC.ExecTimeout("The script on h1 did not finish within 120 seconds", "STEP=netplan\n", "", 120)
            return real(node, script, timeout, change, holder)

        host.run = run
        started = ops.start
        ops.start = lambda kind, title, resource, href, ref, message="": (messages.append(message),
                                                                          started(kind, title, resource, href, ref, message))[1]
        BONDS.hostrun = host
        op = BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        return host, ops, messages, op

    def test_a_timed_out_change_keeps_the_host_held_and_needs_attention(self):
        host, ops, messages, op = self.start_timed_out()
        self.assertEqual([], host.let_go_of, "the host stays held")
        ref = ops.started[0][2]
        self.assertEqual(("unconfirmed", "this-replica/1"), (ref["stage"], ref["lease"]))
        self.assertTrue(op.get("unconfirmed"))
        for message in (messages[0], BONDS.status({"ref": ref}, now=ref["since"] + 3600)[2]):
            self.assertIn("needs attention", message)
            self.assertIn("did not finish within 120 seconds", message)
            self.assertIn("cannot tell whether", message)
            self.assertIn("cancel this job", message)
        self.assertEqual("running", BONDS.status({"ref": ref}, now=ref["since"] + 7200)[0])
        self.assertEqual([], host.let_go_of, "still held while it needs attention")
        self.assertGreaterEqual(len(host.holds), 2, "the hold is renewed while it waits")

    def test_a_change_that_never_reached_the_host_lets_go_and_starts_no_job(self):
        import homestead_pod_exec as POD_EXEC
        host, ops = Host(facts(), ""), Ops()
        real = host.run

        def run(node, script, timeout=60, change="", holder=""):
            if script.startswith("set -e"):
                raise POD_EXEC.ExecUnreached("Homestead could not reach homestead-host-h1 (timed out); the command never started")
            return real(node, script, timeout, change, holder)

        host.run = run
        BONDS.hostrun = host
        with self.assertRaisesRegex(ConnectionError, "never started"):
            BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        self.assertEqual([("h1", "this-replica/1")], host.let_go_of)
        self.assertFalse(ops.started)

    def test_a_hold_that_cannot_be_renewed_is_said_so(self):
        host, ops, _, _ = self.start_timed_out()
        ref = ops.started[0][2] if isinstance(ops.started[0], tuple) else ops.started[0]

        def refused(node, what, seconds=60, holder=""):
            raise RuntimeError("the API server did not answer")

        host.hold = refused
        message = BONDS.status({"ref": ref}, now=ref["since"] + 3600)[2]
        self.assertNotIn("h1 stays held", message)
        self.assertIn("could not renew its hold on h1", message)
        self.assertIn("the API server did not answer", message)
        host.hold = lambda node, what, seconds=60, holder="": "this-replica/1"
        message = BONDS.status({"ref": ref}, now=ref["since"] + 7200)[2]
        self.assertIn("h1 stays held", message, "renewed again: held again")

    def test_the_cancel_review_of_an_unconfirmed_job_claims_nothing_about_the_host(self):
        _, ops, _, _ = self.start_timed_out()
        keeps = " ".join(BONDS.cancel_plan({"kind": BONDS.KIND, "ref": ops.started[0][2]})["keeps"])
        self.assertIn("cannot tell how far the change got on h1", keeps)
        self.assertIn("check its network first", keeps)
        self.assertNotIn("carries on", keeps)
        self.assertNotIn("puts its old network back by itself", keeps)

    def test_a_restart_that_times_out_may_have_been_scheduled_and_is_not_asked_again(self):
        import homestead_pod_exec as POD_EXEC
        host, ops = Host(facts(), ""), Ops()
        BONDS.hostrun = host
        BONDS.start("h1", {"members": ["enp2s0"]}, ops)
        ref = dict(ops.started[0][2], stage="confirmed", renamed=True, services=["k3s"])
        restarts = []

        def run(node, script, timeout=60, change="", holder=""):
            restarts.append(script)
            raise POD_EXEC.ExecTimeout("The script on h1 did not finish within 45 seconds", "", "", 45)

        host.run = run
        state = BONDS.status({"ref": ref}, now=ref["since"] + 10)
        self.assertEqual("running", state[0])
        self.assertEqual("restarting", ref["stage"], "the Ready check follows; no second restart")
        self.assertEqual(1, len(restarts))

    def test_cancelling_the_unconfirmed_job_lets_go_of_the_host(self):
        host, ops, _, _ = self.start_timed_out()
        item = {"kind": BONDS.KIND, "ref": ops.started[0][2]}
        BONDS.register(BONDS.OPS)
        BONDS.OPS.CANCELLERS[BONDS.KIND][1](item, {})
        self.assertEqual([("h1", "this-replica/1")], host.let_go_of)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.deleted = []
        self.kubelet = Kubelet()
        BONDS.bind(None, lambda path: {"items": [{"metadata": {"name": "kube-vip-x"}}]} if "pods" in path else
                   {"items": [{}, {}]} if path == "/api/v1/nodes" else self.kubelet.node(),
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
        self.assertEqual("running", BONDS.status(item, now=ref["since"] + 45)[0], "not until its kubelet is heard from")
        self.kubelet.beat = ref["since"] + 20  # k3s is back: its kubelet posts status
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
