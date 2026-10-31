"""A host bond or bridge change that never sees its host Ready again ends.

Before, the "restarting" stage (k3s or RKE2 restarting so flannel follows the
new interface) waited for the host to be Ready with no deadline. A host the
new network had cut off stayed NotReady, the job stayed running for ever, and
while it ran the network guard refused every bond or bridge change on every
other host. Now the stage fails after RESTART_SECONDS, naming the host and
what to check there, and the failed job no longer blocks other hosts.

On the shared Kubernetes fake (tests/fakes/kube.py), with a fake clock."""
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_host_bonds as BONDS
import homestead_host_bridge as BRIDGE
import homestead_job_errors as ERRORS
import homestead_kube as K
import homestead_operations as OPS
from fakes.kube import Kube

SINCE = 10_000.0


def node(name, ready, server=False):
    labels = {"node-role.kubernetes.io/control-plane": "true"} if server else {}
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": labels},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def bond_ref(name="node-a"):
    return {"node": name, "action": "create", "bond": "bond0", "members": ["enp1s0", "enp2s0"],
            "mode": "active-backup", "carries_on": "bond0", "address": "192.0.2.10/24", "renamed": True,
            "backup": "/var/lib/homestead/netplan-x", "services": ["k3s"], "stage": "restarting",
            "since": SINCE, "change": f"Bond enp1s0 + enp2s0 on {name}"}


def bridge_ref(name="node-a"):
    return {"node": name, "interface": "enp1s0", "address": "192.0.2.10/24", "bridge": "br0",
            "backup": "/var/lib/homestead/netplan-x", "stage": "restarting", "since": SINCE,
            "services": ["k3s"], "change": f"Move {name}'s enp1s0 into br0"}


class RestartDeadlineTests(unittest.TestCase):
    """The restarting stage of both kinds, step by step."""

    def setUp(self):
        self.kube = self.enterContext(Kube(node("node-a", ready=False), node("node-b", ready=True, server=True)))
        BONDS.bind(None, K.kget, K.ksend)
        BRIDGE.bind(None, K.kget, K.ksend)

    def check_never_returns(self, module, ref):
        item = {"ref": ref}
        status, _, message = module.status(item, now=SINCE + module.RESTART_SECONDS - 1)
        self.assertEqual("running", status, "still waiting inside the deadline")
        self.assertIn("node-a", message)
        status, progress, message = module.status(item, now=SINCE + module.RESTART_SECONDS + 1)
        self.assertEqual(("failed", 100), (status, progress))
        self.assertIn("node-a", message)
        self.assertIn(f"{module.RESTART_SECONDS // 60} minutes", message)
        self.assertIn("k3s", message, "says which service to look at")
        self.assertIn("journalctl -u k3s", message, "says what to check on the host")
        self.assertIn("If it becomes Ready later, nothing more is needed: the new network is kept.", message,
                      "a slow host that comes back late is not a fault")

    def test_a_bond_whose_host_never_returns_fails_naming_the_host(self):
        self.check_never_returns(BONDS, bond_ref())

    def test_a_bridge_whose_host_never_returns_fails_naming_the_host(self):
        self.check_never_returns(BRIDGE, bridge_ref())

    def test_a_host_back_in_time_still_succeeds(self):
        self.kube.add(node("node-a", ready=True))
        for module, ref in ((BONDS, bond_ref()), (BRIDGE, bridge_ref())):
            self.assertEqual("succeeded", module.status({"ref": ref}, now=SINCE + 31)[0], module.__name__)

    def test_restart_seconds_fit_inside_the_hold_measured_from_its_renewal_at_the_restart(self):
        """RESTART_SECONDS < HOLD_SECONDS, measured from the renewal at restart:
        the restart runs inside the job's hold (hostrun.run with its holder
        renews the lease), so the hold outlasts the wait that follows."""
        self.assertLess(BONDS.RESTART_SECONDS, BONDS.HOLD_SECONDS)
        self.assertLess(BRIDGE.RESTART_SECONDS, BRIDGE.HOLD_SECONDS)
        for module, ref in ((BONDS, bond_ref()), (BRIDGE, bridge_ref())):
            ran = []
            module.hostrun = types.SimpleNamespace(
                run=lambda node, script, timeout=60, change="", holder="": ran.append((script, holder)) or ("OK\n", ""),
                let_go=lambda node, holder: None)
            ref.update(stage="confirmed", lease="this-replica/1")
            self.assertEqual("running", module.status({"ref": ref}, now=SINCE)[0], module.__name__)
            self.assertEqual("restarting", ref["stage"])
            self.assertEqual([("this-replica/1", True)], [(holder, "systemctl restart k3s" in script)
                                                          for script, holder in ran], module.__name__)
            module.hostrun = None


class GuardTests(unittest.TestCase):
    """The real job store, the network guard, and a fake clock."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(Kube(node("node-a", ready=False), node("node-b", ready=True, server=True),
                               node("node-c", ready=True, server=True)))
        OPS.bind(K.kget, self.tmp, lambda namespace, name: {})
        BONDS.bind(None, K.kget, K.ksend)
        BRIDGE.bind(None, K.kget, K.ksend)
        self.now = SINCE
        clock = lambda: self.now
        fake_time = types.SimpleNamespace(time=clock, strftime=time.strftime)
        self.enterContext(mock.patch.object(BONDS, "time", fake_time))
        self.enterContext(mock.patch.object(BRIDGE, "time", fake_time))
        self.enterContext(mock.patch.object(ERRORS, "clock", clock))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {BONDS.KIND: BONDS.status, "host-bridge": BRIDGE.status}))

    def rounds(self, seconds, every=30):
        end = self.now + seconds
        while self.now < end:
            OPS.list_operations()
            self.now += every

    def job(self, job_id):
        return next(row for row in OPS._read() if row["id"] == job_id)

    def check_lets_go(self, kind, ref, module):
        job = OPS.start(kind, ref["change"], {"kind": "Node", "name": "node-a"}, "/nodes", ref)["id"]
        self.rounds(60)
        self.assertEqual("running", self.job(job)["status"])
        self.assertEqual(ref["change"], BONDS.network_guard("node-b")[0], "another host waits while it runs")
        self.rounds(module.RESTART_SECONDS)
        item = self.job(job)
        self.assertEqual("failed", item["status"])
        self.assertIn("node-a", item["message"])
        self.assertEqual("", BONDS.network_guard("node-b")[0], "a failed job no longer blocks other hosts")

    def test_a_bond_that_never_returns_lets_other_hosts_change_again(self):
        self.check_lets_go(BONDS.KIND, bond_ref(), BONDS)

    def test_a_bridge_that_never_returns_lets_other_hosts_change_again(self):
        self.check_lets_go("host-bridge", bridge_ref(), BRIDGE)


if __name__ == "__main__":
    unittest.main()
