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


def stamp(seconds, micro=False):
    """A Kubernetes time: metav1.Time, or MicroTime as a Lease's renewTime is."""
    text = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
    return f"{text}.{int(seconds % 1 * 1e6):06d}Z" if micro else f"{text}Z"


def real_node(name, heartbeat, transition, ready=True):
    """A Node as the API server returns it: Ready carries the kubelet's last
    heartbeat and when the status last changed. The kubelet posts status
    only on a change or every 5 minutes, so a Ready host's heartbeat is often
    minutes old, and Ready stays True about 40 s after the kubelet goes quiet."""
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": {}},
            "status": {"conditions": [
                {"type": "MemoryPressure", "status": "False", "lastHeartbeatTime": stamp(heartbeat),
                 "lastTransitionTime": stamp(transition), "reason": "KubeletHasSufficientMemory"},
                {"type": "Ready", "status": "True" if ready else "False", "lastHeartbeatTime": stamp(heartbeat),
                 "lastTransitionTime": stamp(transition), "reason": "KubeletReady",
                 "message": "kubelet is posting ready status"}]}}


def node_lease(name, renewed):
    """The node's Lease in kube-node-lease, as read from a k3s cluster
    (1 Nov): the kubelet renews it about every 10 s, leaseDurationSeconds 40."""
    return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
            "metadata": {"name": name, "namespace": "kube-node-lease",
                         "ownerReferences": [{"apiVersion": "v1", "kind": "Node", "name": name, "uid": "uid-node"}]},
            "spec": {"holderIdentity": name, "leaseDurationSeconds": 40, "renewTime": stamp(renewed, micro=True)}}


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
        self.kube.add(real_node("node-a", heartbeat=SINCE + 20, transition=SINCE - 3600))
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


class RealNodeShapeTests(unittest.TestCase):
    """The restarting stage against the Node and Lease a real cluster shows.

    systemd-run restarts k3s 2 s after the stage starts; until then, and for
    about 40 s after the kubelet's last heartbeat, the Node still reads Ready.
    Only a kubelet heard from after the restart proves the host came back."""

    def setUp(self):
        # Ready since an hour ago, last heartbeat 3 minutes before the restart,
        # the Lease last renewed 4 s before it.
        self.kube = self.enterContext(Kube(real_node("node-a", heartbeat=SINCE - 180, transition=SINCE - 3600),
                                           node_lease("node-a", renewed=SINCE - 4)))
        BONDS.bind(None, K.kget, K.ksend)
        BRIDGE.bind(None, K.kget, K.ksend)

    def each(self):
        return ((BONDS, bond_ref()), (BRIDGE, bridge_ref()))

    def test_a_host_that_still_reads_ready_from_before_the_restart_is_still_waited_for(self):
        for module, ref in self.each():
            for at in (SINCE + 31, SINCE + 45):
                status, _, message = module.status({"ref": ref}, now=at)
                self.assertEqual("running", status, f"{module.__name__} at +{at - SINCE:.0f}s")
                self.assertIn("node-a", message)

    def test_a_renewal_by_the_kubelet_before_it_restarted_is_no_proof(self):
        self.kube.add(node_lease("node-a", renewed=SINCE + 1.5))
        for module, ref in self.each():
            self.assertEqual("running", module.status({"ref": ref}, now=SINCE + 45)[0], module.__name__)

    def test_a_lease_renewed_after_the_restart_succeeds(self):
        self.kube.add(node_lease("node-a", renewed=SINCE + 24.3))
        for module, ref in self.each():
            self.assertEqual("succeeded", module.status({"ref": ref}, now=SINCE + 31)[0], module.__name__)

    def test_ready_heartbeat_or_transition_after_the_restart_succeeds(self):
        for heartbeat, transition in ((SINCE + 20, SINCE - 3600), (SINCE + 20, SINCE + 20)):
            self.kube.add(real_node("node-a", heartbeat=heartbeat, transition=transition))
            for module, ref in self.each():
                self.assertEqual("succeeded", module.status({"ref": ref}, now=SINCE + 31)[0], module.__name__)

    def test_a_fresh_heartbeat_on_a_host_that_is_not_ready_is_still_waited_for(self):
        self.kube.add(real_node("node-a", heartbeat=SINCE + 20, transition=SINCE + 20, ready=False))
        self.kube.add(node_lease("node-a", renewed=SINCE + 24))
        for module, ref in self.each():
            self.assertEqual("running", module.status({"ref": ref}, now=SINCE + 31)[0], module.__name__)

    def test_without_a_lease_the_ready_condition_alone_decides(self):
        self.kube.discard("/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node-a")
        for module, ref in self.each():
            self.assertEqual("running", module.status({"ref": ref}, now=SINCE + 45)[0], module.__name__)
        self.kube.add(real_node("node-a", heartbeat=SINCE + 20, transition=SINCE - 3600))
        for module, ref in self.each():
            self.assertEqual("succeeded", module.status({"ref": ref}, now=SINCE + 45)[0], module.__name__)

    def test_a_host_whose_kubelet_never_returns_fails_naming_it(self):
        for module, ref in self.each():
            status, progress, message = module.status({"ref": ref}, now=SINCE + module.RESTART_SECONDS + 1)
            self.assertEqual(("failed", 100), (status, progress), module.__name__)
            self.assertIn("node-a was not Ready again", message)
            self.assertIn("journalctl -u k3s", message)


LEASE_PATH = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node-a"


class HostBaselineTests(unittest.TestCase):
    """The restarting stage measured from what the host's kubelet last
    reported when the restart was asked for: its Lease's renewTime and Ready's
    heartbeat, both on the host's own clock, so a host clock ahead of or
    behind Homestead's changes nothing."""

    def setUp(self):
        self.kube = self.enterContext(Kube(node("node-b", ready=True, server=True)))
        BONDS.bind(None, K.kget, K.ksend)
        BRIDGE.bind(None, K.kget, K.ksend)

    def each(self):
        return ((BONDS, bond_ref()), (BRIDGE, bridge_ref()))

    def restart(self, module, ref, skew=0, lease=True, times=True):
        """Ask for the restart at SINCE (Homestead's clock) on a host whose
        clock runs skew seconds ahead: Ready since an hour, last heartbeat
        3 minutes ago, Lease renewed 4 s ago - all in the host's time.
        times=False: a Ready condition with no times on it."""
        self.skew = skew
        self.kube.add(real_node("node-a", heartbeat=SINCE + skew - 180, transition=SINCE + skew - 3600)
                      if times else node("node-a", ready=True))
        if lease:
            self.kube.add(node_lease("node-a", renewed=self.renew0))
        else:
            self.kube.discard(LEASE_PATH)
        module.hostrun = types.SimpleNamespace(
            run=lambda node, script, timeout=60, change="", holder="": ("OK\n", ""), let_go=lambda node, holder: None)
        self.addCleanup(setattr, module, "hostrun", None)
        ref.update(stage="confirmed", since=SINCE - 30)
        module.status({"ref": ref}, now=SINCE)
        self.assertEqual(("restarting", SINCE), (ref["stage"], ref["since"]), module.__name__)
        return {"ref": ref}

    @property
    def renew0(self):
        return SINCE + self.skew - 4

    def renewed(self, after_baseline):
        self.kube.add(node_lease("node-a", renewed=self.renew0 + after_baseline))

    def test_a_host_clock_ahead_does_not_pass_a_host_cut_off(self):
        for module, ref in self.each():
            item = self.restart(module, ref, skew=60)
            self.assertEqual("running", module.status(item, now=SINCE + 45)[0], module.__name__)
            self.renewed(10)  # the old kubelet's last renewal, before it stopped
            self.assertEqual("running", module.status(item, now=SINCE + 120)[0], module.__name__)
            status, _, message = module.status(item, now=SINCE + module.RESTART_SECONDS + 1)
            self.assertEqual("failed", status, module.__name__)
            self.assertIn("node-a was not Ready again", message)

    def test_a_host_clock_behind_still_passes_a_host_that_came_back(self):
        for module, ref in self.each():
            item = self.restart(module, ref, skew=-60)
            self.renewed(24)  # the restarted kubelet renews, 20 s after the restart on its own clock
            self.assertEqual("succeeded", module.status(item, now=SINCE + 31)[0], module.__name__)

    def test_a_renewal_counts_only_back_after_seconds_past_the_baseline(self):
        for after, want in ((10, "running"), (14, "running"), (16, "succeeded")):
            for module, ref in self.each():
                item = self.restart(module, ref)
                self.renewed(after)
                self.assertEqual(want, module.status(item, now=SINCE + 45)[0], f"{module.__name__} renew0+{after}")

    def test_an_unreadable_lease_falls_back_to_ready_heartbeats_on_the_host_clock(self):
        self.kube.fail(LEASE_PATH, 403, times=1000)
        for module, ref in self.each():
            item = self.restart(module, ref, skew=-60)
            self.assertEqual("running", module.status(item, now=SINCE + 45)[0], module.__name__)
            self.kube.add(real_node("node-a", heartbeat=SINCE - 60 + 5, transition=SINCE - 60 - 3600))
            self.assertEqual("succeeded", module.status(item, now=SINCE + 45)[0], module.__name__)

    def test_with_no_baseline_readable_homesteads_clock_decides(self):
        """No Lease and no times on Ready when the restart is asked for: the
        step compares with when it started, as before."""
        for module, ref in self.each():
            item = self.restart(module, ref, lease=False, times=False)
            self.assertEqual("running", module.status(item, now=SINCE + 45)[0], module.__name__)
            self.kube.add(node_lease("node-a", renewed=SINCE + 5))
            self.assertEqual("running", module.status(item, now=SINCE + 45)[0], module.__name__)
            self.kube.add(node_lease("node-a", renewed=SINCE + 20))
            self.assertEqual("succeeded", module.status(item, now=SINCE + 45)[0], module.__name__)


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
