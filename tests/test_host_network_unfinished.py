"""A host bond or bridge change does not report success on what it could not do.

After the rollback is disarmed the job restarts kube-vip's pod on the host and,
on a cluster of more than one host, k3s or RKE2. Before, a kube-vip pod list or
delete that failed was ignored, and a node list that could not be read counted
as one host, so nothing restarted; either way the job said it succeeded, with
kube-vip or flannel still on the old interface. Now it keeps trying for as long
as the job waits for a restart (RESTART_SECONDS), and then fails, saying what was
not done and what to do. The new network is kept either way.

A reply can be lost after the write landed, so a retry goes by what the cluster
shows: a replacement pod is never deleted.

On the shared Kubernetes fake (tests/fakes/kube.py) and a recording host."""
import copy
import sys
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_host_bonds as BONDS
import homestead_host_bridge as BRIDGE
import homestead_host_network_job as NETWORK_JOB
import homestead_kube as K
import homestead_operations as OPS
import homestead_pod_exec as POD_EXEC
from fakes.kube import Kube
import test_host_bonds as BOND_TESTS
import test_host_network_deadline as DEADLINE

SINCE = 10_000.0
LEASE = "this-replica/1"
ADDRESS = "192.0.2.10/24"
PODS = "/api/v1/namespaces/kube-system/pods"
VIP = PODS + "/kube-vip-abcde"
NODES = "/api/v1/nodes"
NODE_LEASE = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node-a"


def saved_bond(**changes):
    return {"node": "node-a", "action": "create", "bond": "bond0", "members": ["enp1s0", "enp2s0"],
            "mode": "active-backup", "carries_on": "bond0", "address": ADDRESS, "renamed": True,
            "backup": "/var/lib/homestead/netplan-x", "services": ["k3s"], "stage": "confirmed", "since": SINCE,
            "change": "Bond enp1s0 + enp2s0 on node-a", "lease": LEASE, **changes}


def saved_bridge(**changes):
    return {"node": "node-a", "interface": "enp1s0", "address": ADDRESS, "bridge": "br0",
            "backup": "/var/lib/homestead/netplan-x", "stage": "confirmed", "since": SINCE, "services": ["k3s"],
            "change": "Move node-a's enp1s0 into br0", "lease": LEASE, **changes}


class Bond:
    module, kind, saved, interface, confirmed_at, waiting_at = BONDS, "host-bond", staticmethod(saved_bond), "bond0", 70, 92


class Bridge:
    module, kind, saved, interface, confirmed_at, waiting_at = BRIDGE, "host-bridge", staticmethod(saved_bridge), "br0", 60, 90


KINDS = (Bond, Bridge)


def vip_pod(uid="uid-old", **meta):
    return {"apiVersion": "v1", "kind": "Pod", "spec": {"nodeName": "node-a"},
            "metadata": {"name": "kube-vip-abcde", "namespace": "kube-system", "uid": uid, **meta}}


def kept(kind):
    return f"node-a carries {ADDRESS} on {kind.interface} and keeps it, but the change is not finished."


def no_vip(kind, why):
    return (f"Homestead could not restart kube-vip on node-a ({why}), so it may still announce its addresses on the old "
            f"interface. Delete the kube-vip pod on node-a, and it starts again on {kind.interface}.")


def no_restart(why, service="k3s"):
    return (f"Homestead could not read the cluster's hosts ({why}), so it did not restart {service} on node-a, and flannel "
            f"may still carry pod traffic by the old interface. If the cluster has other hosts, run `systemctl restart {service}` on node-a.")


class Host:
    def __init__(self):
        self.ran, self.let_go_of = [], []
        self.restart_says = ("OK\n", "")    # what the real command prints once it is scheduled

    def run(self, node, script, timeout=60, change="", holder=""):
        self.ran.append((script, change, holder))
        return self.restart_says

    def let_go(self, node, holder):
        self.let_go_of.append((node, holder))

    def restarts(self):
        return [script for script, _, _ in self.ran if "systemctl restart" in script]


BOOT = SINCE - 86_400    # when node-a booted, on Homestead's clock


class RealHelper(Host):
    """The host helper as homestead_hostrun.run answers, per way the answer
    to the restart command can end, and a host that restarts k3s 2 s after
    it is scheduled. It counts restarts the host really scheduled, which is
    not what the job was told. systemd-run prints its unit names on stderr
    when it schedules one; only `&& echo OK` prints on stdout. A plain-tuple
    answer with no OK would read as a lost one: fakes answer with Output."""
    UNITS = "Running timer as unit: run-r1.timer\nWill run service as unit: run-r1.service"

    def __init__(self, ends="answered", clock=lambda: SINCE, reads="answered", request_lag=1, read_lag=5):
        super().__init__()
        self.ends, self.clock, self.reads, self.scheduled = ends, clock, reads, 0
        self.on_restart = lambda: None    # what else happens while the restart command runs
        # Real time passes inside a step: a helper pod is made and started for
        # each run, so the request reaches the host request_lag seconds after
        # the step's clock, and a read read_lag seconds after that.
        self.request_lag, self.read_lag, self.step_at, self.spent = request_lag, read_lag, None, 0
        self.show_says = None    # what the read prints in place of the host's state
        self.started, self.state = SINCE - 3600, "active"    # k3s's main process, on Homestead's clock

    def run(self, node, script, timeout=60, change="", holder=""):
        self.ran.append((script, change, holder))
        if "systemctl show" in script:
            return self.show()
        if "systemctl restart" not in script:
            return POD_EXEC.Output("", "", 0)
        if self.ends == "refused":      # systemd-run failed: nothing scheduled, exit code 1
            return POD_EXEC.Output("", "Failed to start transient timer unit: Unit run-r1.timer already exists.", 1)
        if self.ends == "no status":    # a Failure Status with no exit code: the exec never started the command
            return POD_EXEC.Output("", "", None)
        self.scheduled += 1
        self.started = self.passes(self.request_lag) + 2
        self.on_restart()
        if self.ends == "answered":
            return POD_EXEC.Output("OK\n", self.UNITS, 0)
        if self.ends == "dropped":      # the stream closed before any output: no exit code
            return POD_EXEC.Output("", "", None)
        if self.ends == "dropped after stderr":
            return POD_EXEC.Output("", self.UNITS, None)
        if self.ends == "no stdout":    # it said it ended well, but OK never came
            return POD_EXEC.Output("", self.UNITS, 0)
        if self.ends == "late":         # the answer came after the deadline
            raise POD_EXEC.ExecTimeout("The script on node-a did not finish within 45 seconds", "", "", 45)
        if self.ends == "connection lost":    # the API server's connection broke after the request was sent
            raise ConnectionResetError(104, "Connection reset by peer")
        if self.ends == "helper not removed":   # the helper pod's DELETE failed after a good answer
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        raise AssertionError(self.ends)

    def real(self):
        """The time now, as it passes inside the step the test is running."""
        if self.step_at != self.clock():
            self.step_at, self.spent = self.clock(), 0
        return self.step_at + self.spent

    def passes(self, seconds):
        self.real()
        self.spent += seconds
        return self.real()

    def show(self):
        at = self.passes(self.read_lag)
        if self.reads != "answered":
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        if self.show_says is not None:
            return POD_EXEC.Output(self.show_says, "", 0)
        return POD_EXEC.Output(f"ActiveState={self.state}\nExecMainStartTimestampMonotonic={int((self.started - BOOT) * 1e6)}\n"
                               f"{at - BOOT:.2f} 1000.00\n", "", 0)


# Answers lost after the command ran: the restart is scheduled on the host.
LOST = ("dropped", "dropped after stderr", "late", "connection lost", "helper not removed")


class Unfinished(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(DEADLINE.node("node-a", ready=True), DEADLINE.node("node-b", ready=True, server=True), vip_pod()))
        self.host = Host()
        for module in (BONDS, BRIDGE):
            self.enterContext(mock.patch.multiple(module, hostrun=module.hostrun, kget=module.kget, ksend=module.ksend))
            module.bind(self.host, K.kget, K.ksend)
        self.now = SINCE
        DEADLINE.kubelets(self, "node-b", "node-c", "node-m")
        # The job store's checkpoint: what a new leader would read if the step's result were lost.
        self.checkpoints = []
        self.enterContext(mock.patch.object(OPS, "checkpoint", lambda item: self.checkpoints.append(copy.deepcopy(item))))

    def step(self, kind, ref, after):
        self.now = SINCE + after
        return kind.module.status({"id": "job-1", "ref": ref}, now=self.now)

    def each(self):
        for kind in KINDS:
            self.host.__init__()
            self.kube.add(DEADLINE.node("node-a", ready=True))
            self.kube.discard(NODE_LEASE)
            self.kube.add(vip_pod())
            self.kube.writes.clear()
            yield kind

    def deletes(self):
        return [path for method, path, _, _ in self.kube.writes if method == "DELETE"]

    def come_back(self):
        self.kube.add(DEADLINE.real_node("node-a", heartbeat=SINCE + 20, transition=SINCE - 3600))

    def stay_away(self):
        self.kube.add(DEADLINE.real_node("node-a", heartbeat=SINCE - 180, transition=SINCE - 3600))


class NodeListTests(Unfinished):
    """Whether the host is one of many is not known: k3s or RKE2 is not
    restarted on a guess, and no success is claimed."""

    def test_a_list_that_cannot_be_read_is_asked_again_and_nothing_restarts_yet(self):
        for kind in self.each():
            self.kube.fail(NODES, 500, times=1)
            ref = kind.saved()
            status, progress, message = self.step(kind, ref, 5)
            self.assertEqual(("running", kind.confirmed_at, "confirmed"), (status, progress, ref["stage"]))
            self.assertIn("the cluster's hosts", message)
            self.assertEqual([], self.host.restarts())
            self.assertEqual([], self.host.let_go_of, "still held")
            self.assertEqual("running", self.step(kind, ref, 35)[0])
            self.assertEqual("restarting", ref["stage"], "the list answers: the restart follows")
            self.assertEqual(1, len(self.host.restarts()))

    def test_a_list_lost_to_a_timeout_is_treated_the_same(self):
        for kind in self.each():
            self.kube.fail(NODES, TimeoutError("timed out"), times=1)
            ref = kind.saved()
            self.assertEqual(("running", "confirmed"), (self.step(kind, ref, 5)[0], ref["stage"]))
            self.assertEqual([], self.host.restarts())

    def test_an_empty_list_is_no_answer_since_the_host_itself_is_in_it(self):
        for kind in self.each():
            self.kube.react("GET", NODES, lambda method, path, body: {"items": []})
            ref = kind.saved()
            self.assertEqual(("running", "confirmed"), (self.step(kind, ref, 5)[0], ref["stage"]))
            self.kube._reactions.clear()

    def test_it_ends_at_the_deadline_saying_what_was_not_done_and_what_to_do(self):
        for kind in self.each():
            self.kube.fail(NODES, 500, times=100)
            ref = kind.saved()
            self.assertEqual("running", self.step(kind, ref, kind.module.RESTART_SECONDS)[0])
            status, progress, message = self.step(kind, ref, kind.module.RESTART_SECONDS + 1)
            self.assertEqual(("failed", 100), (status, progress))
            self.assertTrue(message.startswith(kept(kind)), message)
            self.assertIn(no_restart("HTTP Error 500: Failure"), message.replace("\n", " "))
            self.assertNotIn("kube-vip", message, "kube-vip was done")
            self.assertEqual([], self.host.restarts())
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.kube._failures.clear()

    def test_the_failure_names_the_service_the_host_has(self):
        for kind in self.each():
            self.kube.fail(NODES, 500, times=100)
            ref = kind.saved(services=["rke2-server"])
            message = self.step(kind, ref, kind.module.RESTART_SECONDS + 1)[2]
            self.assertIn("did not restart rke2-server on node-a", message)
            self.assertIn("`systemctl restart rke2-server`", message)
            self.kube._failures.clear()

    def test_a_host_with_no_service_to_restart_never_needs_the_list(self):
        for kind in self.each():
            self.kube.fail(NODES, 500, times=100)
            self.assertEqual("succeeded", self.step(kind, kind.saved(services=[]), 5)[0])
            self.kube._failures.clear()


class KubeVipTests(Unfinished):
    """kube-vip is restarted, or the job says it was not."""

    def to_restarting(self, kind):
        ref = kind.saved()
        self.assertEqual("running", self.step(kind, ref, 0)[0])
        self.assertEqual("restarting", ref["stage"], "k3s does not wait for kube-vip")
        return ref

    def test_a_pod_list_that_fails_does_not_hold_up_the_restart_and_is_asked_again(self):
        for kind in self.each():
            self.kube.fail(PODS, 500, times=1)
            ref = self.to_restarting(kind)
            self.assertEqual(1, len(self.host.restarts()))
            self.assertIsNotNone(self.kube.object(VIP))
            self.come_back()
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0], "the next round restarts it")
            self.assertIsNone(self.kube.object(VIP))

    def test_a_delete_that_fails_is_asked_again(self):
        for kind in self.each():
            self.kube.fail(VIP, 500, times=1, method="DELETE")
            ref = self.to_restarting(kind)
            self.assertIsNotNone(self.kube.object(VIP))
            self.come_back()
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0])
            self.assertIsNone(self.kube.object(VIP))
            self.assertEqual(2, len(self.deletes()))

    def test_a_delete_whose_reply_was_lost_is_not_sent_again_once_the_pod_is_gone(self):
        for kind in self.each():
            self.kube.fail(VIP, TimeoutError("timed out"), times=1, method="DELETE", applied=True)
            ref = self.to_restarting(kind)
            self.assertIsNone(self.kube.object(VIP), "it landed; the reply did not")
            self.come_back()
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0])
            self.assertEqual(1, len(self.deletes()))

    def test_after_a_lost_reply_the_replacement_pod_is_left_alone(self):
        for kind in self.each():
            self.kube.fail(VIP, TimeoutError("timed out"), times=1, method="DELETE", applied=True)
            ref = self.to_restarting(kind)
            self.kube.add(vip_pod(uid="uid-new"))    # its controller made another, under the same name
            self.come_back()
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0])
            self.assertEqual(1, len(self.deletes()))
            self.assertEqual("uid-new", self.kube.object(VIP)["metadata"]["uid"])

    def test_a_pod_already_going_is_not_deleted_again(self):
        for kind in self.each():
            self.kube.fail(VIP, TimeoutError("timed out"), times=1, method="DELETE")
            ref = self.to_restarting(kind)
            self.kube.add(vip_pod(deletionTimestamp="2026-01-01T00:00:00Z"))
            self.come_back()
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0])
            self.assertEqual(1, len(self.deletes()))

    def test_a_pod_gone_by_the_time_it_is_deleted_counts(self):
        for kind in self.each():
            self.kube.fail(VIP, 404, times=1, method="DELETE")
            ref = self.to_restarting(kind)
            self.come_back()
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0])
            self.assertEqual(1, len(self.deletes()))

    def test_with_no_kube_vip_on_the_host_there_is_nothing_to_prove(self):
        for kind in self.each():
            self.kube.discard(VIP)
            ref = self.to_restarting(kind)
            self.come_back()
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0])

    def test_a_host_back_with_kube_vip_not_restarted_is_no_success_and_ends_at_the_deadline(self):
        for kind in self.each():
            self.kube.fail(PODS, 500, times=100)
            ref = self.to_restarting(kind)
            self.come_back()
            status, progress, message = self.step(kind, ref, 31)
            self.assertEqual(("running", kind.waiting_at), (status, progress))
            self.assertIn("kube-vip", message)
            self.assertEqual([], self.host.let_go_of)
            status, progress, message = self.step(kind, ref, kind.module.RESTART_SECONDS + 1)
            self.assertEqual(("failed", 100), (status, progress))
            self.assertEqual(f"{kept(kind)} {no_vip(kind, 'HTTP Error 500: Failure')}", message)
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.assertEqual(1, len(self.host.restarts()), "never restarted twice")
            self.kube._failures.clear()

    def test_a_host_that_never_returns_says_kube_vip_too(self):
        for kind in self.each():
            self.kube.fail(PODS, 500, times=100)
            self.stay_away()
            self.kube.add(DEADLINE.node_lease("node-a", renewed=SINCE - 4))
            ref = self.to_restarting(kind)
            message = self.step(kind, ref, kind.module.RESTART_SECONDS + 1)[2]
            self.assertIn("node-a was not Ready again", message)
            self.assertIn(no_vip(kind, "HTTP Error 500: Failure"), message)
            self.kube._failures.clear()

    def test_on_one_host_it_waits_for_kube_vip_then_fails(self):
        self.kube.discard(NODES + "/node-b")
        for kind in self.each():
            self.kube.fail(VIP, 500, times=100, method="DELETE")
            ref = kind.saved()
            status, progress, message = self.step(kind, ref, 5)
            self.assertEqual(("running", kind.confirmed_at, "confirmed"), (status, progress, ref["stage"]))
            self.assertIn("kube-vip", message)
            status, _, message = self.step(kind, ref, kind.module.RESTART_SECONDS + 1)
            self.assertEqual("failed", status)
            self.assertEqual(f"{kept(kind)} {no_vip(kind, 'HTTP Error 500: Failure')}", message)
            self.assertEqual([], self.host.restarts())
            self.kube._failures.clear()

    def test_on_one_host_a_retry_that_works_succeeds(self):
        self.kube.discard(NODES + "/node-b")
        for kind in self.each():
            self.kube.fail(PODS, 500, times=1)
            ref = kind.saved()
            self.assertEqual("running", self.step(kind, ref, 5)[0])
            self.assertEqual("succeeded", self.step(kind, ref, 35)[0])
            self.assertIsNone(self.kube.object(VIP))

    def test_when_neither_could_be_done_it_says_both(self):
        for kind in self.each():
            self.kube.fail(PODS, 500, times=100)
            self.kube.fail(NODES, 500, times=100)
            message = self.step(kind, kind.saved(), kind.module.RESTART_SECONDS + 1)[2]
            why = "HTTP Error 500: Failure"
            self.assertEqual(f"{kept(kind)} {no_vip(kind, why)} {no_restart(why)}", message)
            self.kube._failures.clear()


class QuorumTests(Unfinished):
    """Restarting k3s or RKE2 takes the host quiet as the change did, so it goes by
    the other servers as they are at that round, however late that round is."""
    DOWN = ("1 other server (node-b) is not Ready or has sent no recent heartbeat. If node-a stops "
            "answering too, the cluster could lose its quorum. When every server is Ready, run `systemctl restart k3s` on node-a.")

    def tearDown(self):
        self.kube.add(DEADLINE.node("node-b", ready=True, server=True))

    def test_a_list_back_late_does_not_restart_on_a_quorum_it_never_checked(self):
        for kind in self.each():
            self.kube.add(DEADLINE.node("node-b", ready=True, server=True))
            self.kube.fail(NODES, 500, method="GET")
            ref = kind.saved()
            self.assertEqual("running", self.step(kind, ref, 5)[0])
            self.kube.add(DEADLINE.node("node-b", ready=False, server=True))
            status, progress, message = self.step(kind, ref, 300)
            self.assertEqual(("running", kind.confirmed_at, "confirmed"), (status, progress, ref["stage"]))
            self.assertEqual([], self.host.restarts())
            self.assertEqual("node-a is on its new network; waiting for node-b to be Ready with a recent heartbeat "
                             "before it restarts k3s, "
                             "for up to 300s more", message)
            self.kube.add(DEADLINE.node("node-b", ready=True, server=True))
            self.assertEqual("running", self.step(kind, ref, 330)[0])
            self.assertEqual(("restarting", 1), (ref["stage"], len(self.host.restarts())))

    def test_a_server_down_on_the_first_round_holds_the_restart_too(self):
        for kind in self.each():
            self.kube.add(DEADLINE.node("node-b", ready=False, server=True))
            ref = kind.saved()
            self.assertEqual(("running", "confirmed"), (self.step(kind, ref, 0)[0], ref["stage"]))
            self.assertEqual([], self.host.restarts())
            self.assertIsNone(self.kube.object(VIP), "kube-vip goes all the same: it restarts in moments, off the quorum")

    def test_it_fails_at_the_deadline_naming_them_in_the_guards_words(self):
        for kind in self.each():
            self.kube.add(DEADLINE.node("node-b", ready=False, server=True))
            ref = kind.saved()
            status, _, message = self.step(kind, ref, kind.module.RESTART_SECONDS + 1)
            self.assertEqual("failed", status)
            self.assertTrue(message.startswith(kept(kind)), message)
            self.assertIn(self.DOWN, message)
            self.assertIn("Homestead did not restart k3s on node-a", message)
            self.assertEqual([], self.host.restarts())
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)

    def test_several_are_all_named(self):
        self.kube.add(DEADLINE.node("node-c", ready=False, server=True))
        self.addCleanup(self.kube.discard, NODES + "/node-c")
        for kind in self.each():
            self.kube.add(DEADLINE.node("node-b", ready=False, server=True))
            message = self.step(kind, kind.saved(), kind.module.RESTART_SECONDS + 1)[2]
            self.assertIn("2 other servers (node-b, node-c) are not Ready or have sent no recent heartbeat", message)

    def test_a_worker_down_and_the_host_itself_do_not_hold_it(self):
        self.kube.add(DEADLINE.node("worker-c", ready=False))
        self.addCleanup(self.kube.discard, NODES + "/worker-c")
        for kind in self.each():
            self.kube.add(DEADLINE.node("node-a", ready=False, server=True))
            ref = kind.saved()
            self.assertEqual("running", self.step(kind, ref, 0)[0])
            self.assertEqual("restarting", ref["stage"])

    def test_a_single_host_has_no_other_servers_to_wait_for(self):
        self.kube.discard(NODES + "/node-b")
        for kind in self.each():
            self.assertEqual("succeeded", self.step(kind, kind.saved(), 0)[0])


class ServerHeartbeatTests(Unfinished):
    """Ready stays True about 40 s after a server's kubelet goes quiet, so a
    server counts as up only while its kubelet renews its node Lease (#1318):
    within half the Lease's own length (20 s for the usual 40), ahead of
    Homestead's clock or behind it. A Lease outside that, or missing, holds the
    restart and the next change, as a server not Ready does."""
    CLOCKS = ("1 other server (node-b) is Ready, but its clock disagrees with Homestead's clock. Homestead cannot "
              "check its heartbeat. If node-a stops answering too, the cluster could lose its quorum. When the "
              "clocks agree, run `systemctl restart k3s` on node-a.")

    def tearDown(self):
        for name in ("node-b", "node-m", "node-e"):
            self.kube.discard(DEADLINE.LEASE_PREFIX + name)
        self.kube.discard(NODES + "/node-m")
        self.kube.discard(NODES + "/node-e")
        self.kube.add(DEADLINE.node("node-b", ready=True, server=True))

    def held_back(self, why, names="node-b"):
        for kind in self.each():
            ref = kind.saved()
            status, progress, message = self.step(kind, ref, 5)
            self.assertEqual(("running", kind.confirmed_at, "confirmed"), (status, progress, ref["stage"]), why)
            self.assertEqual([], self.host.restarts(), why)
            self.assertIn(f"waiting for {names} to be Ready with a recent heartbeat before it restarts k3s", message, why)

    def goes_ahead(self, why):
        for kind in self.each():
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.assertEqual(("restarting", 1), (ref["stage"], len(self.host.restarts())), why)

    def final_words(self):
        return [self.step(kind, kind.saved(), kind.module.RESTART_SECONDS + 1)[2] for kind in self.each()]

    def test_a_ready_server_whose_kubelet_stopped_holds_the_restart(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE - 15))
        self.held_back("renewed 20 s before the step: half its 40 s, past the limit")

    def test_a_server_renewed_just_inside_the_window_lets_it_go_ahead(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE - 14.5))
        self.goes_ahead("renewed 19.5 s before the step")

    def test_the_window_is_half_the_leases_own_length(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE + 1, duration=10))
        self.goes_ahead("renewed 4 s ago, inside half of 10 s")
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE - 1, duration=10))
        self.held_back("renewed 6 s ago, past half of 10 s")
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE - 35, duration=120))
        self.goes_ahead("renewed 40 s ago, inside half of 120 s")

    def test_a_server_with_no_lease_holds_the_restart(self):
        self.kube.fail(DEADLINE.LEASE_PREFIX + "node-b", 404, times=10)
        self.held_back("a Lease that cannot be found proves nothing")

    def test_a_stamp_a_little_ahead_of_homestead_is_a_heartbeat(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE + 8))
        self.goes_ahead("3 s ahead: clocks a little apart")
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE + 24.5))
        self.goes_ahead("19.5 s ahead: still inside the window")

    def test_a_stamp_far_ahead_holds_the_restart_and_blames_the_clocks(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE + 25.5))
        self.held_back("20.5 s ahead: outside the window")
        for message in self.final_words():
            self.assertIn(self.CLOCKS, message)

    def test_a_ready_server_with_a_stamp_far_behind_blames_the_clocks(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE - 1000))
        for message in self.final_words():
            self.assertIn(self.CLOCKS, message)

    def test_a_ready_server_with_a_stamp_a_minute_old_has_no_recent_heartbeat(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE + BONDS.RESTART_SECONDS + 1 - 60))
        for message in self.final_words():
            self.assertIn("1 other server (node-b) is not Ready or has sent no recent heartbeat.", message)

    def test_a_server_with_only_the_old_master_label_counts(self):
        self.kube.add({**DEADLINE.node("node-m", ready=True),
                       "metadata": {"name": "node-m", "labels": {"node-role.kubernetes.io/master": "true"}}})
        self.kube.add(DEADLINE.stopped_kubelet("node-m", renewed=SINCE - 60))
        self.held_back("an older k3s labels its servers master only", names="node-m")

    def test_a_server_with_only_the_etcd_label_counts(self):
        self.kube.add({**DEADLINE.node("node-e", ready=True),
                       "metadata": {"name": "node-e", "labels": {"node-role.kubernetes.io/etcd": "true"}}})
        self.kube.add(DEADLINE.stopped_kubelet("node-e", renewed=SINCE - 60))
        self.held_back("RKE2 split roles: an etcd-only server holds etcd's quorum", names="node-e")

    def test_the_guard_counts_a_stale_server_as_down(self):
        self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE - 60))
        with mock.patch.object(BONDS, "time", types.SimpleNamespace(time=lambda: SINCE, strftime=time.strftime)):
            self.assertEqual(("", (0, 1, 0)), BONDS.network_guard("node-a"))
            self.kube.add(DEADLINE.stopped_kubelet("node-b", renewed=SINCE + 600))
            self.assertEqual(("", (0, 1, 1)), BONDS.network_guard("node-a"), "one server down by its clock")
            self.kube.discard(DEADLINE.LEASE_PREFIX + "node-b")
            self.assertEqual(("", (1, 1, 0)), BONDS.network_guard("node-a"))

    def test_the_guard_says_why_in_one_idea_per_sentence(self):
        def said(ready):
            return BONDS.plan("node-a", {"members": ["enp2s0"]}, BOND_TESTS.facts(), servers_ready=ready)["refusals"]
        self.assertEqual(["1 other server is not Ready or has sent no recent heartbeat. If node-a stops "
                          "answering too, the cluster could lose its quorum. Try again when every server is Ready"], said((1, 2)))
        self.assertTrue(said((1, 3))[0].startswith("2 other servers are not Ready or have sent no recent heartbeat."))
        self.assertEqual(["1 other server is Ready, but its clock disagrees with Homestead's clock. Homestead cannot check "
                          "its heartbeat. If node-a stops answering too, the cluster could lose its quorum. Set the same "
                          "time on every host, then try again"], said((1, 2, 1)))
        self.assertTrue(said((1, 3, 2))[0].startswith("2 other servers are Ready, but their clocks disagree with Homestead's clock."))
        mixed = said((0, 2, 1))[0]
        self.assertTrue(mixed.startswith("2 other servers are not Ready or have sent no recent heartbeat. The clock of 1 of "
                                         "them disagrees with Homestead's clock."), mixed)
        mixed = said((0, 3, 2))[0]
        self.assertTrue(mixed.startswith("3 other servers are not Ready or have sent no recent heartbeat. The clocks of 2 of "
                                         "them disagree with Homestead's clock."), mixed)
        for words in (said((1, 2)), said((1, 2, 1)), said((1, 3, 2)), said((0, 2, 1)), said((0, 3, 2))):
            self.assertLess(len("node-a: " + words[0].split(". ")[0] + "."), 90, words)


class RestartNotScheduledTests(Unfinished):
    """systemd-run that fails exits 1 with no OK: nothing was scheduled, so a
    kubelet that keeps renewing proves nothing, and asking again cannot restart
    it twice. Only an exit code other than 0 proves that."""
    SAYS = POD_EXEC.Output("", "Failed to start transient timer unit", 1)

    def test_it_stays_confirmed_and_asks_again(self):
        for kind in self.each():
            self.host.restart_says = self.SAYS
            ref = kind.saved()
            status, progress, message = self.step(kind, ref, 5)
            self.assertEqual(("running", kind.confirmed_at, "confirmed"), (status, progress, ref["stage"]))
            self.assertIn("still trying to schedule the restart of k3s (Failed to start transient timer unit", message)
            self.assertEqual(LEASE, ref["lease"])
            self.host.restart_says = POD_EXEC.Output("OK\n", "", 0)
            self.step(kind, ref, 35)
            self.assertEqual(("restarting", 2), (ref["stage"], len(self.host.restarts())))

    def test_a_kubelet_that_keeps_renewing_is_no_proof_of_a_restart_never_scheduled(self):
        for kind in self.each():
            self.host.restart_says = self.SAYS
            self.kube.add(DEADLINE.real_node("node-a", heartbeat=SINCE + 40, transition=SINCE - 3600))
            ref = kind.saved()
            self.assertEqual("running", self.step(kind, ref, 60)[0])
            self.assertEqual("confirmed", ref["stage"])

    def test_it_ends_at_the_deadline_saying_to_run_it(self):
        # Before, an empty answer ("", "") was this refusal, read as "no answer".
        # Now an empty answer is a lost one (LostAnswerTests); a refusal is an
        # exit code other than 0, and with nothing printed the code is the reason.
        for kind in self.each():
            self.host.restart_says = POD_EXEC.Output("", "", 1)
            status, _, message = self.step(kind, kind.saved(), kind.module.RESTART_SECONDS + 1)
            self.assertEqual("failed", status)
            self.assertEqual(f"{kept(kind)} node-a did not schedule the restart of k3s (exit code 1), so it did not restart, and flannel "
                             "may still carry pod traffic by the old interface. Run `systemctl restart k3s` on node-a.", message)

    def test_a_refusal_from_the_real_helper_is_asked_again_until_it_is_scheduled(self):
        for kind in self.each():
            self.host = RealHelper("refused")
            kind.module.bind(self.host, K.kget, K.ksend)
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.step(kind, ref, 35)
            self.assertEqual(("confirmed", 2, 0), (ref["stage"], len(self.host.restarts()), self.host.scheduled))
            self.host.ends = "answered"
            self.step(kind, ref, 65)
            self.assertEqual(("restarting", 1), (ref["stage"], self.host.scheduled))
            self.assertNotIn("restart_unsure", ref)


def unsure(why="", service="k3s"):
    return (f"Homestead cannot tell whether {service} on node-a restarted after it asked for the restart"
            + (f" ({why})" if why else "") + f". The network settings on node-a are changed, but {service} may still use the old "
            f"interface until it restarts. If `systemctl status {service}` on node-a shows it running since before the change, "
            f"run `systemctl restart {service}` on node-a.")


class LostAnswerTests(Unfinished):
    """The answer to the restart was lost: the stream closed with no exit
    code, the answer came after the deadline, the connection broke after the
    request was sent, or removing the helper failed after a good answer. The
    restart may be scheduled, so the job never asks again: a second restart
    of k3s or RKE2 on the host is the harm. The host then shows whether it
    restarted; the job does not end on a guess."""

    def helper(self, kind, ends, reads="answered", **lags):
        self.host = RealHelper(ends, clock=lambda: self.now, reads=reads, **lags)
        kind.module.bind(self.host, K.kget, K.ksend)
        # Homestead's clock after a run returns: the time the helper took has passed.
        self.enterContext(mock.patch.object(NETWORK_JOB, "clock", self.host.real, create=True))

    def round(self, kind, ref, after):
        """One round as the job store runs it: a step that raises keeps only
        what the job saved before (its checkpoints), as the store does."""
        before = copy.deepcopy(ref)
        self.checkpoints.clear()
        try:
            return self.step(kind, ref, after)
        except Exception:
            ref.clear()
            ref.update(copy.deepcopy(self.checkpoints[-1]["ref"] if self.checkpoints else before))
            return None

    def back(self):
        self.come_back()
        self.kube.add(DEADLINE.node_lease("node-a", renewed=self.now - 3))

    def test_a_lost_answer_never_leads_to_a_second_restart(self):
        for ends in (*LOST, "no status"):
            for kind in self.each():
                self.helper(kind, ends)
                ref = kind.saved()
                for after in (5, 35, 65, 125, 300, kind.module.RESTART_SECONDS + 10):
                    self.round(kind, ref, after)
                self.assertEqual(1, len(self.host.restarts()), ends)
                self.assertLessEqual(self.host.scheduled, 1, ends)

    def test_a_result_lost_after_the_restart_was_sent_never_leads_to_a_second_restart(self):
        # Homestead restarts, the leader changes, or someone else writes the job
        # row while the step runs: the step's result is not saved, and the next
        # round starts from the job as it was last saved.
        for ends in ("answered", "dropped"):
            for kind in self.each():
                self.helper(kind, ends)
                before = copy.deepcopy(kind.saved())
                self.step(kind, copy.deepcopy(before), 5)
                ref = copy.deepcopy(self.checkpoints[-1]["ref"] if self.checkpoints else before)
                for after in (35, 65, 125):
                    self.round(kind, ref, after)
                self.assertEqual((1, 1), (self.host.scheduled, len(self.host.restarts())), ends)
                self.assertEqual(("restarting", True), (ref["stage"], ref.get("restart_unsure")), ends)

    def test_once_asked_it_moves_on_even_when_another_server_is_down_now(self):
        for kind in self.each():
            self.helper(kind, "connection lost")
            ref = kind.saved()
            self.round(kind, ref, 5)
            self.kube.add(DEADLINE.node("node-c", ready=False, server=True))
            self.step(kind, ref, 35)
            self.assertEqual(("restarting", True, 1), (ref["stage"], ref.get("restart_unsure"), len(self.host.restarts())))
            self.kube.discard(NODES + "/node-c")

    def test_a_lost_answer_moves_on_to_waiting_for_the_host(self):
        for ends in LOST:
            for kind in self.each():
                self.helper(kind, ends)
                ref = kind.saved()
                self.round(kind, ref, 5)
                self.round(kind, ref, 6)
                self.assertEqual(("restarting", True), (ref["stage"], ref.get("restart_unsure")), ends)
                self.assertEqual("running", self.step(kind, ref, 20)[0], ends)

    def test_exit_code_0_proves_it_was_scheduled_even_without_its_ok(self):
        for kind in self.each():
            self.helper(kind, "no stdout")
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.assertEqual("restarting", ref["stage"])
            self.assertNotIn("restart_unsure", ref)

    def test_a_host_that_restarted_after_the_request_succeeds_without_a_note(self):
        for ends in LOST:
            for kind in self.each():
                self.helper(kind, ends)
                ref = kind.saved()
                self.round(kind, ref, 5)
                self.round(kind, ref, 6)
                self.now = SINCE + 45
                self.back()
                status, _, message = self.step(kind, ref, 45)
                self.assertEqual("succeeded", status, ends)
                self.assertEqual(kind.module._SPEC.done(ref), message)

    def test_a_command_that_never_ran_is_not_a_success_and_is_not_asked_again(self):
        for kind in self.each():
            self.helper(kind, "no status")
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.now = SINCE + 45
            self.back()
            status, _, message = self.step(kind, ref, 45)
            self.assertEqual("failed", status)
            self.assertEqual(f"{kept(kind)} k3s on node-a did not restart. It has run since before Homestead asked for the restart. "
                             "The network settings on node-a are changed, but k3s still uses the old interface until it restarts. "
                             "Run `systemctl restart k3s` on node-a.", message)
            self.assertEqual((1, 0), (len(self.host.restarts()), self.host.scheduled))

    def test_a_host_that_cannot_show_it_waits_then_fails_with_the_step_by_hand(self):
        for kind in self.each():
            self.helper(kind, "dropped", reads="fail")
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.now = SINCE + 45
            self.back()
            status, _, message = self.step(kind, ref, 45)
            self.assertEqual("running", status)
            self.assertTrue(message.startswith("Checking whether k3s restarted on node-a (node-a did not answer: "), message)
            self.now = SINCE + kind.module.RESTART_SECONDS + 10
            self.back()
            status, _, message = self.step(kind, ref, kind.module.RESTART_SECONDS + 10)
            self.assertEqual("failed", status)
            self.assertTrue(message.startswith(f"{kept(kind)} Homestead cannot tell whether k3s on node-a restarted after it "
                                               "asked for the restart (node-a did not answer: "), message)
            self.assertTrue(message.endswith(unsure()[unsure().index(". The network"):]), message)
            self.assertEqual(1, message.count("cannot tell"), message)
            self.assertEqual(1, len(self.host.restarts()))

    def test_a_restart_counts_when_the_helper_is_slow_to_send_it_and_slower_to_read(self):
        # A helper pod is made and started for each run: the request reaches
        # the host well after the step's clock, and the read later still.
        for request_lag, read_lag in ((1, 5), (20, 25), (0, 40)):
            for kind in self.each():
                self.helper(kind, "dropped", request_lag=request_lag, read_lag=read_lag)
                ref = kind.saved()
                self.step(kind, ref, 5)
                self.now = SINCE + 45
                self.back()
                self.assertEqual("succeeded", self.step(kind, ref, 45)[0], (request_lag, read_lag))

    def test_what_the_host_cannot_show_is_said_in_words(self):
        for says, why in (("ActiveState=inactive\nExecMainStartTimestampMonotonic=0\n123.00 1.00\n", "k3s is not running"),
                          ("ActiveState=failed\nExecMainStartTimestampMonotonic=5000000\n123.00 1.00\n", "k3s has failed"),
                          ("", "Homestead could not read the answer from node-a"),
                          ("ActiveState=active\nExecMainStartTimestampMonotonic=x\nabc\n", "Homestead could not read the answer from node-a")):
            for kind in self.each():
                self.helper(kind, "dropped")
                self.host.show_says = says
                ref = kind.saved()
                self.step(kind, ref, 5)
                self.now = SINCE + 45
                self.back()
                self.assertEqual(f"Checking whether k3s restarted on node-a ({why})", self.step(kind, ref, 45)[2])

    def test_a_service_still_starting_is_waited_for(self):
        for kind in self.each():
            self.helper(kind, "dropped")
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.host.state = "activating"
            self.now = SINCE + 45
            self.back()
            self.assertEqual(("running", kind.waiting_at, "Checking whether k3s restarted on node-a (k3s is starting)"),
                             self.step(kind, ref, 45))

    def test_a_host_not_back_after_a_lost_answer_fails_without_saying_it_restarted(self):
        for kind in self.each():
            self.helper(kind, "dropped")
            self.stay_away()
            self.kube.add(DEADLINE.node_lease("node-a", renewed=SINCE - 4))
            ref = kind.saved()
            self.step(kind, ref, 5)
            status, _, message = self.step(kind, ref, kind.module.RESTART_SECONDS + 10)
            self.assertEqual("failed", status)
            self.assertTrue(message.startswith("node-a was not Ready again 10 minutes after Homestead asked for the restart of "
                                               "k3s. On node-a, check `journalctl -u k3s`"), message)
            self.assertIn(unsure(), message)
            self.assertEqual(1, self.host.scheduled)

    def test_the_real_helper_failing_to_remove_its_pod_after_a_good_answer_never_leads_to_a_second_restart(self):
        # The real homestead_hostrun, on the fake API, around an exec that
        # answers OK: the DELETE of its helper pod then fails on transport, and
        # that error replaces the good answer (hostrun._delete catches HTTPError only).
        import homestead_hostrun as HOSTRUN
        self.addCleanup(HOSTRUN.bind, *(getattr(HOSTRUN, name, None) for name in ("kget", "ksend", "exec_in", "NS")))
        restarts = []

        def exec_in(namespace, pod, argv, **options):
            if "systemctl restart" in argv[-1]:
                restarts.append(pod)
                return POD_EXEC.Output(b"OK\n", RealHelper.UNITS, 0)
            return POD_EXEC.Output(b"", "", 0)

        def ksend(method, path, *args, **options):
            if method == "DELETE" and "/pods/homestead-host-" in path:
                raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
            return K.ksend(method, path, *args, **options)

        def kubelet(method, path, body):
            body["status"] = {"phase": "Running"}    # the kubelet starts the helper straight away
        self.kube.react("POST", "/api/v1/namespaces/homestead/pods", kubelet)
        HOSTRUN.bind(K.kget, ksend, exec_in, "homestead")
        for kind in self.each():
            restarts.clear()
            kind.module.bind(HOSTRUN, K.kget, K.ksend)
            ref = kind.saved()
            for after in (5, 35, 65):
                self.round(kind, ref, after)
            self.assertEqual(1, len(restarts), kind.kind)
            self.assertEqual("restarting", ref["stage"])

    def test_an_answered_restart_says_nothing_more(self):
        for kind in self.each():
            self.helper(kind, "answered")
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.now = SINCE + 45
            self.back()
            status, _, message = self.step(kind, ref, 45)
            self.assertEqual("succeeded", status)
            self.assertEqual(kind.module._SPEC.done(ref), message)
            self.assertFalse([script for script, _, _ in self.host.ran if "systemctl show" in script], "no check needed")


class CancelTests(Unfinished):
    KEEPS = ["node-a keeps its new network: its rollback is already disarmed",
             "Homestead stops restarting kube-vip and k3s or RKE2 on node-a: restart any it had not restarted by hand, "
             "or VIPs and flannel may stay on the old interface",
             "Other changes on node-a can start again at once"]

    def test_cancelling_while_confirmed_says_what_will_not_happen(self):
        for kind in self.each():
            self.assertEqual({"mode": "forget", "needs": "admin", "keeps": self.KEEPS},
                             kind.module.cancel_plan({"ref": kind.saved()}))

    def test_cancelling_while_k3s_or_rke2_restarts_says_the_same(self):
        # Its rollback is disarmed here too, and kube-vip may still wait for its restart.
        for kind in self.each():
            plan = kind.module.cancel_plan({"ref": kind.saved(stage="restarting")})
            self.assertEqual({"mode": "forget", "needs": "admin", "keeps": self.KEEPS}, plan)
            self.assertNotIn("puts its old network back", " ".join(plan["keeps"]))

    def test_cancelling_before_it_is_confirmed_says_the_host_puts_itself_back(self):
        for kind in self.each():
            keeps = kind.module.cancel_plan({"ref": kind.saved(stage="applied")})["keeps"]
            self.assertIn("puts its old network back by itself", keeps[0])


class SavedJobTests(Unfinished):
    """A job the old code saved goes on as it did."""

    def test_a_job_saved_while_restarting_never_goes_back_to_kube_vip(self):
        self.come_back()
        for kind in self.each():
            self.come_back()
            ref = kind.saved(stage="restarting")
            self.assertEqual("succeeded", self.step(kind, ref, 31)[0])
            self.assertEqual([], self.deletes())
            self.assertIsNotNone(self.kube.object(VIP))

    def test_a_job_saved_as_confirmed_goes_through_kube_vip_then_the_restart(self):
        for kind in self.each():
            ref = kind.saved()
            self.assertEqual("running", self.step(kind, ref, 5)[0])
            self.assertEqual("restarting", ref["stage"])
            self.assertIsNone(self.kube.object(VIP))


class JobStoreTests(DEADLINE.GuardBase):
    """Through the real job store: what is remembered survives its file."""

    def setUp(self):
        super().setUp()
        self.kube.add(vip_pod())
        self.host = Host()
        clock = types.SimpleNamespace(time=lambda: self.now, strftime=time.strftime, strptime=time.strptime)
        for module in (BONDS, BRIDGE):
            self.enterContext(mock.patch.object(module, "time", clock))
            self.enterContext(mock.patch.multiple(module, hostrun=module.hostrun, kget=module.kget, ksend=module.ksend))
            module.bind(self.host, K.kget, K.ksend)

    def check_a_lost_reply_is_remembered_across_rounds(self, kind):
        self.kube.fail(VIP, TimeoutError("timed out"), times=1, method="DELETE", applied=True)
        job = OPS.start(kind.kind, "A change", {"kind": "Node", "name": "node-a"}, "/nodes", kind.saved(), "Saved")["id"]
        OPS.list_operations()
        self.assertEqual("restarting", self.job(job)["ref"]["stage"])
        self.kube.add(vip_pod(uid="uid-new"))
        self.kube.add(DEADLINE.real_node("node-a", heartbeat=SINCE + 20, transition=SINCE - 3600))
        self.now = SINCE + 40
        OPS.list_operations()
        self.assertEqual("succeeded", self.job(job)["status"])
        self.assertEqual("uid-new", self.kube.object(VIP)["metadata"]["uid"])

    def test_a_bond(self):
        self.check_a_lost_reply_is_remembered_across_rounds(Bond)

    def check_a_step_result_dropped_after_the_restart_was_sent_never_leads_to_a_second_restart(self, kind):
        # Someone else writes the job row while the restart is asked for: the
        # store drops the step's result. The next round starts from the job as
        # it was last saved, which already says the restart was asked for.
        host = RealHelper("answered", clock=lambda: self.now)
        kind.module.bind(host, K.kget, K.ksend)
        job = OPS.start(kind.kind, "A change", {"kind": "Node", "name": "node-a"}, "/nodes", kind.saved(), "Saved")["id"]

        def written_elsewhere():
            with OPS._lock:
                rows = OPS._read()
                next(row for row in rows if row["id"] == job)["message"] = "Written elsewhere"
                OPS._write(rows)
        host.on_restart = written_elsewhere
        OPS.list_operations()
        self.assertEqual(("confirmed", "Written elsewhere"), (self.job(job)["ref"]["stage"], self.job(job)["message"]))
        host.on_restart = lambda: None
        self.now = SINCE + 30
        OPS.list_operations()
        self.assertEqual((1, 1), (host.scheduled, len(host.restarts())))
        self.assertEqual("restarting", self.job(job)["ref"]["stage"])

    def test_a_bond_whose_step_result_is_dropped(self):
        self.check_a_step_result_dropped_after_the_restart_was_sent_never_leads_to_a_second_restart(Bond)

    def test_a_bridge_whose_step_result_is_dropped(self):
        self.check_a_step_result_dropped_after_the_restart_was_sent_never_leads_to_a_second_restart(Bridge)

    def test_a_bridge(self):
        self.check_a_lost_reply_is_remembered_across_rounds(Bridge)

    def check_a_job_that_could_not_finish_lets_other_hosts_go(self, kind):
        self.kube.fail(NODES, 500, times=1000, method=None)
        job = OPS.start(kind.kind, "A change", {"kind": "Node", "name": "node-a"}, "/nodes", kind.saved(), "Saved")["id"]
        OPS.list_operations()
        self.assertEqual("running", self.job(job)["status"])
        self.now = SINCE + kind.module.RESTART_SECONDS + 1
        OPS.list_operations()
        row = self.job(job)
        self.assertEqual("failed", row["status"])
        self.assertIn("did not restart k3s on node-a", row["message"])
        self.kube._failures.clear()
        self.assertEqual("", BONDS.network_guard("node-b")[0])

    def test_a_bond_that_could_not_finish(self):
        self.check_a_job_that_could_not_finish_lets_other_hosts_go(Bond)

    def test_a_bridge_that_could_not_finish(self):
        self.check_a_job_that_could_not_finish_lets_other_hosts_go(Bridge)


if __name__ == "__main__":
    unittest.main()
