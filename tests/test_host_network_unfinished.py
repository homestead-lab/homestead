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
import sys
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
import homestead_kube as K
import homestead_operations as OPS
from fakes.kube import Kube
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


class Unfinished(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(DEADLINE.node("node-a", ready=True), DEADLINE.node("node-b", ready=True, server=True), vip_pod()))
        self.host = Host()
        for module in (BONDS, BRIDGE):
            self.enterContext(mock.patch.multiple(module, hostrun=module.hostrun, kget=module.kget, ksend=module.ksend))
            module.bind(self.host, K.kget, K.ksend)

    def step(self, kind, ref, after):
        return kind.module.status({"ref": ref}, now=SINCE + after)

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
    DOWN = ("1 of the other servers is not Ready: if node-a went quiet too, the cluster could lose its quorum. "
            "Wait until they are Ready (node-b), then run `systemctl restart k3s` on node-a.")

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
            self.assertEqual("node-a is on its new network; waiting for node-b to be Ready before it restarts k3s, "
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
            self.assertIn("2 of the other servers is not Ready", message)
            self.assertIn("(node-b, node-c)", message)

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


class RestartNotScheduledTests(Unfinished):
    """systemd-run that fails prints no OK: nothing was scheduled, so a kubelet
    that keeps renewing proves nothing, and asking again cannot restart it twice."""
    SAYS = ("", "Failed to start transient timer unit\n")

    def test_it_stays_confirmed_and_asks_again(self):
        for kind in self.each():
            self.host.restart_says = self.SAYS
            ref = kind.saved()
            status, progress, message = self.step(kind, ref, 5)
            self.assertEqual(("running", kind.confirmed_at, "confirmed"), (status, progress, ref["stage"]))
            self.assertIn("still trying to schedule the restart of k3s (Failed to start transient timer unit", message)
            self.assertEqual(LEASE, ref["lease"])
            self.host.restart_says = ("OK\n", "")
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
        for kind in self.each():
            self.host.restart_says = ("", "")
            status, _, message = self.step(kind, kind.saved(), kind.module.RESTART_SECONDS + 1)
            self.assertEqual("failed", status)
            self.assertEqual(f"{kept(kind)} node-a did not schedule the restart of k3s (no answer), so it did not restart, and flannel "
                             "may still carry pod traffic by the old interface. Run `systemctl restart k3s` on node-a.", message)

    def test_a_restart_that_timed_out_may_have_been_scheduled_and_is_not_asked_again(self):
        import homestead_pod_exec as POD_EXEC

        def timed_out(node, script, timeout=60, change="", holder=""):
            self.host.ran.append((script, change, holder))
            raise POD_EXEC.ExecTimeout("did not finish", "", "", 45)
        for kind in self.each():
            self.host.run = timed_out
            ref = kind.saved()
            self.step(kind, ref, 5)
            self.assertEqual("restarting", ref["stage"])
            self.step(kind, ref, 35)
            self.assertEqual(1, len(self.host.restarts()))
            del self.host.run


class CancelTests(Unfinished):
    KEEPS = ["node-a keeps its new network: its rollback is already disarmed",
             "Homestead stops restarting kube-vip and k3s or RKE2 on node-a: restart any it had not restarted by hand, "
             "or VIPs and flannel may stay on the old interface",
             "Other changes on node-a can start again at once"]

    def test_cancelling_while_confirmed_says_what_will_not_happen(self):
        for kind in self.each():
            self.assertEqual({"mode": "forget", "needs": "admin", "keeps": self.KEEPS},
                             kind.module.cancel_plan({"ref": kind.saved()}))

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
