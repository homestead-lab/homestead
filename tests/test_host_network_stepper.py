"""A host bond or bridge change, step by step, exactly as a person and a saved
job see it (#1129).

These pin what the job does for both kinds through their public face (each
module's status, start, convert, change, cancel_plan, cancel_run and the job
store), so the sequence can be rewritten without changing it: the wording,
the progress, the markers it needs from the host, the commands it sends and
under whose hold, and the fields a job row keeps. A saved job is written here
by hand, with the fields the code that saved it wrote; it must keep working.

Where the two kinds differ today (progress numbers, words, what a follow-up
is called, how long a hold is renewed for), the difference is pinned as it is.
This code can take a host off the network: a change here is a change to what
it does, on purpose.

On the shared Kubernetes fake (tests/fakes/kube.py) and a recording host."""
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
import homestead_kube as K
import homestead_operations as OPS
import homestead_pod_exec as POD_EXEC
from fakes.kube import Kube
import test_host_bonds as BOND_TESTS
import test_host_bridge as BRIDGE_TESTS
import test_host_network_deadline as DEADLINE

SINCE = 10_000.0
LEASE = "this-replica/1"
ADDRESS = "192.0.2.10/24"
BACKUP = "/var/lib/homestead/netplan-x"
VIP_POD = "/api/v1/namespaces/kube-system/pods/kube-vip-abcde"
GOOD_BOND = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nUP enp1s0\nIN enp2s0\nUP enp2s0\nARMED\nEND\n"
GOOD_BRIDGE = "ADDR\nPORT\nROUTE\nGATEWAY\nARMED\nEND\n"


def saved_bond(**changes):
    """A bond job's ref, field for field as start() saves it."""
    return {"node": "node-a", "action": "create", "bond": "bond0", "members": ["enp1s0", "enp2s0"],
            "mode": "active-backup", "carries_on": "bond0", "address": ADDRESS, "renamed": True, "backup": BACKUP,
            "services": ["k3s"], "stage": "applied", "since": SINCE, "change": "Bond enp1s0 + enp2s0 on node-a",
            "lease": LEASE, **changes}


def saved_bridge(**changes):
    """A bridge job's ref, field for field as start() saves it."""
    return {"node": "node-a", "interface": "enp1s0", "address": ADDRESS, "bridge": "br0", "backup": BACKUP,
            "stage": "applied", "since": SINCE, "services": ["k3s"], "change": "Move node-a's enp1s0 into br0",
            "lease": LEASE, **changes}


def saved_unconfirmed(change, **changes):
    """A job whose change script ran past its limit, as start() saves it."""
    return {"node": "node-a", "stage": "unconfirmed", "since": SINCE, "renewed": SINCE, "change": change,
            "lease": LEASE, "timeout": 120, **changes}


def keep_bound(test):
    """Puts back what bind() sets on both kinds' modules when the test ends."""
    for module in (BONDS, BRIDGE):
        test.enterContext(mock.patch.multiple(module, hostrun=module.hostrun, kget=module.kget, ksend=module.ksend))


class Bond:
    """What differs for a bond, in the words a person reads."""
    module = BONDS
    kind = "host-bond"
    saved = staticmethod(saved_bond)
    good = GOOD_BOND
    title = "Bond enp1s0 + enp2s0 on node-a"
    check = BONDS.check_script("bond0", ADDRESS, "bond0", ["enp1s0", "enp2s0"], "active-backup")
    waiting_for = (f"{ADDRESS} on bond0, the default route by bond0, the gateway answering, every member in bond0")
    never_up = (f"node-a did not come up with {ADDRESS} on bond0, so it put its old network back by itself. "
                f"The configuration tried is kept in {BACKUP} beside the old one")
    confirming = (50, "node-a is up; confirming")
    confirmed = (70, f"node-a carries {ADDRESS} on bond0. The rollback is turned off.")
    restarting = 85, "Restarting {service} on node-a, so flannel carries pod traffic by bond0"
    waiting_ready = (92, "Waiting for node-a to be Ready again")
    done = "node-a carries its network on bond0"
    after_done = "node-a carries its network on bond0"
    interface = "bond0"
    unnamed_change = "Network change on node-a"
    hold_seconds = 900


class Bridge:
    """What differs for a bridge, in the words a person reads."""
    module = BRIDGE
    kind = "host-bridge"
    saved = staticmethod(saved_bridge)
    good = GOOD_BRIDGE
    title = "Move node-a's enp1s0 into br0"
    check = BRIDGE.check_script("enp1s0", ADDRESS)
    waiting_for = f"{ADDRESS} on br0, enp1s0 in br0, the default route by br0, the gateway answering"
    never_up = (f"node-a did not come up on br0 with {ADDRESS}, so it put its old network back by itself. "
                f"The configuration tried is kept in {BACKUP} beside the old one")
    confirming = (40, "node-a is up on br0. Confirming.")
    confirmed = (60, f"node-a is on br0, with {ADDRESS}. The rollback is turned off.")
    restarting = 80, "Restarting {service} on node-a, so flannel carries pod traffic by br0"
    waiting_ready = (90, "Waiting for node-a to be Ready again")
    done = "node-a is on br0: make a LAN network on br0 for VMs and containers"
    after_done = "node-a is on br0"
    interface = "br0"
    unnamed_change = "Move node-a into br0"
    hold_seconds = 840


KINDS = (Bond, Bridge)


class Ran:
    def __init__(self, node, script, timeout, change, holder):
        self.node, self.script, self.timeout, self.change, self.holder = node, script, timeout, change, holder

    def __repr__(self):
        return f"Ran({self.node}, {self.script[:50]!r}, {self.timeout}, {self.change!r}, {self.holder!r})"


class Host:
    """A host that answers a check with what it is told to, and records every
    command, its limit and the hold it ran under."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.__dict__.clear()
        self.check, self.confirm = "", "CONFIRMED\n"
        self.raises = {}   # a piece of a script -> what running it raises
        self.ran, self.holds, self.let_go_of = [], [], []
        self.on_restart = lambda: None

    def hold(self, node, what, seconds=60, holder=""):
        self.holds.append((node, what, seconds, holder))
        return LEASE

    def let_go(self, node, holder):
        self.let_go_of.append((node, holder))

    def run(self, node, script, timeout=60, change="", holder=""):
        self.ran.append(Ran(node, script, timeout, change, holder))
        for piece, error in self.raises.items():
            if piece in script:
                raise error
        if "systemctl restart" in script:
            self.on_restart()
            return "OK\n", ""
        if script in (BONDS.CONFIRM_SCRIPT, BRIDGE.CONFIRM_SCRIPT):
            return self.confirm, ""
        if script == BONDS.ROLLBACK_NOW:
            return "STARTED\n", ""
        return self.check, ""

    def sent(self):
        return [run.script for run in self.ran]


def host_node(heartbeat, transition):
    """node-a as the API server returns it: its Lease is owned by it, so the
    Lease stays when something else in the cluster is deleted."""
    found = DEADLINE.real_node("node-a", heartbeat=heartbeat, transition=transition)
    found["metadata"]["uid"] = "uid-node"
    return found


def pod(name, on="node-a"):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "kube-system"},
            "spec": {"nodeName": on}}


class Steps(unittest.TestCase):
    """Both kinds bound to one recording host, and a two-host cluster with a
    kube-vip pod on the host being changed."""

    def setUp(self):
        self.kube = self.enterContext(Kube(DEADLINE.node("node-a", ready=True), DEADLINE.node("node-b", ready=True, server=True),
                                           pod("kube-vip-abcde"), pod("coredns-1"), pod("kube-vip-other", on="node-b")))
        self.host = Host()
        keep_bound(self)
        for module in (BONDS, BRIDGE):
            module.bind(self.host, K.kget, K.ksend)
        self.now = SINCE
        DEADLINE.kubelets(self, "node-b")

    def each(self):
        for kind in KINDS:
            self.host.reset()
            yield kind

    def step(self, kind, ref, after):
        self.now = SINCE + after
        return kind.module.status({"ref": ref}, now=self.now)


class AppliedStageTests(Steps):
    """After the change is applied: look, and confirm only on a full answer."""

    def test_the_first_fifteen_seconds_only_say_it_is_applying(self):
        for kind in self.each():
            self.assertEqual(("running", 20, "Applying on node-a"), self.step(kind, kind.saved(), 14.9))
            self.assertEqual([], self.host.ran, "the host is not asked yet")

    def test_the_check_runs_from_fifteen_seconds_outside_the_hold(self):
        for kind in self.each():
            self.step(kind, kind.saved(), 15)
            self.assertEqual(1, len(self.host.ran))
            run = self.host.ran[0]
            self.assertEqual(("node-a", kind.check, 45, "", ""), (run.node, run.script, run.timeout, run.change, run.holder))

    def test_nothing_heard_says_everything_it_waits_for_and_when_the_host_puts_itself_back(self):
        for kind in self.each():
            self.assertEqual(("running", 30, f"Waiting for {kind.waiting_for}; node-a puts itself back in 210s"),
                             self.step(kind, kind.saved(), 30))
            self.assertEqual([kind.check], self.host.sent(), "nothing is confirmed")

    def test_a_check_that_could_not_run_reads_as_nothing_heard(self):
        for kind in self.each():
            self.host.raises["ping"] = RuntimeError("the helper is gone")
            self.assertEqual(("running", 30, f"Waiting for {kind.waiting_for}; node-a puts itself back in 210s"),
                             self.step(kind, kind.saved(), 30))

    def test_every_marker_is_needed_before_it_confirms(self):
        for kind in self.each():
            for marker in {"ADDR", "ROUTE", "GATEWAY", "ARMED"} | ({"PORT"} if kind is Bridge else set()):
                with self.subTest(marker=marker):
                    self.host.ran = []
                    self.host.check = "\n".join(line for line in kind.good.splitlines() if line != marker) + "\n"
                    ref = kind.saved()
                    status, progress, message = self.step(kind, ref, 30)
                    self.assertEqual(("running", 30, "applied"), (status, progress, ref["stage"]))
                    self.assertEqual([kind.check], self.host.sent())
                    self.assertIn("node-a puts itself back in 210s", message)

    def test_only_a_disarmed_rollback_missing_waits_for_an_answer_from_the_host(self):
        for kind in self.each():
            self.host.check = kind.good.replace("ARMED\n", "")
            self.assertEqual(("running", 30, "Waiting for an answer from the host; node-a puts itself back in 210s"),
                             self.step(kind, kind.saved(), 30))

    def test_each_missing_thing_is_named_in_its_own_words(self):
        self.host.check = "ROUTE\nGATEWAY\nIN enp1s0\nIN enp2s0\nARMED\nEND\n"
        self.assertEqual(f"Waiting for {ADDRESS} on bond0; node-a puts itself back in 210s", self.step(Bond, saved_bond(), 30)[2])
        self.host.check = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nARMED\nEND\n"
        self.assertEqual("Waiting for every member in bond0; node-a puts itself back in 210s", self.step(Bond, saved_bond(), 30)[2])
        self.host.check = "ADDR\nROUTE\nGATEWAY\nARMED\nEND\n"
        self.assertEqual("Waiting for enp1s0 in br0; node-a puts itself back in 210s", self.step(Bridge, saved_bridge(), 30)[2])

    def test_the_countdown_stops_at_nothing_and_the_job_ends_a_minute_past_it(self):
        for kind in self.each():
            ref = kind.saved()
            self.assertIn("node-a puts itself back in 0s", self.step(kind, ref, 250)[2])
            self.assertEqual("running", self.step(kind, ref, 300)[0], "the minute is not past yet")
            self.assertEqual([], self.host.let_go_of)
            self.assertEqual(("failed", 100, kind.never_up), self.step(kind, ref, 300.5))
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.assertNotIn("lease", ref)
            self.assertEqual("applied", ref["stage"])

    def test_a_full_answer_is_confirmed_inside_the_hold_and_the_stage_moves_on(self):
        for kind in self.each():
            self.host.check = kind.good
            ref = kind.saved()
            self.assertEqual(("running", *kind.confirmed), self.step(kind, ref, 30))
            run = self.host.ran[1]
            self.assertEqual(("node-a", kind.module.CONFIRM_SCRIPT, 45, kind.title, LEASE),
                             (run.node, run.script, run.timeout, run.change, run.holder))
            self.assertEqual(("confirmed", SINCE + 30), (ref["stage"], ref["since"]))
            self.assertEqual(LEASE, ref["lease"], "still held")

    def test_a_full_answer_past_the_deadline_is_still_confirmed(self):
        for kind in self.each():
            self.host.check = kind.good
            ref = kind.saved()
            self.assertEqual(("running", *kind.confirmed), self.step(kind, ref, 400))

    def test_a_confirm_the_host_did_not_answer_is_asked_again(self):
        for kind in self.each():
            self.host.check, self.host.confirm = kind.good, ""
            ref = kind.saved()
            self.assertEqual(("running", *kind.confirming), self.step(kind, ref, 30))
            self.assertEqual(("applied", SINCE), (ref["stage"], ref["since"]))

    def test_a_confirm_that_fails_goes_to_the_job_store_with_the_host_still_held(self):
        for kind in self.each():
            self.host.check = kind.good
            self.host.raises["echo CONFIRMED"] = RuntimeError("the helper is gone")
            ref = kind.saved()
            with self.assertRaisesRegex(RuntimeError, "the helper is gone"):
                self.step(kind, ref, 30)
            self.assertEqual(([], LEASE, "applied"), (self.host.let_go_of, ref["lease"], ref["stage"]))

    def test_markers_on_one_line_count_for_a_bridge(self):
        self.host.check = "ADDR PORT ROUTE GATEWAY ARMED"
        self.assertEqual(("running", *Bridge.confirmed), self.step(Bridge, saved_bridge(), 30))


class BondOnlyCheckTests(Steps):
    """What only a bond asks of its host: its members, and the switch's LACP."""
    LACP = dict(mode="802.3ad")
    NO_PARTNER = GOOD_BOND
    LACP_FAILED = ("failed", 100, "The switch did not answer LACP on bond0 within 60 seconds, so node-a put its old network "
                   "back by itself. Group the switch ports as one LACP (802.3ad) group, or choose active-backup")

    def step(self, ref, after):
        return BONDS.status({"ref": ref}, now=SINCE + after)

    def test_a_member_counts_only_by_its_whole_line(self):
        self.host.check = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nIN enp2s0x\nARMED\nEND\n"
        self.assertEqual(("running", 30, "Waiting for every member in bond0; node-a puts itself back in 210s"),
                         self.step(saved_bond(), 30))

    def test_a_member_with_no_link_is_named_when_it_is_confirmed(self):
        self.host.check = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nUP enp1s0\nIN enp2s0\nARMED\nEND\n"
        self.assertEqual(("running", 70, f"node-a carries {ADDRESS} on bond0. The rollback is turned off. enp2s0 has no link yet."),
                         self.step(saved_bond(), 30))
        self.host.check = "ADDR\nROUTE\nGATEWAY\nIN enp1s0\nIN enp2s0\nARMED\nEND\n"
        self.assertEqual(("running", 70, f"node-a carries {ADDRESS} on bond0. The rollback is turned off. enp1s0, enp2s0 has no link yet."),
                         self.step(saved_bond(), 30))

    def test_removing_a_bond_asks_about_no_members(self):
        ref = saved_bond(action="remove", carries_on="enp1s0", mode="802.3ad")
        self.host.check = "ADDR\nROUTE\nGATEWAY\nARMED\nEND\n"
        self.assertEqual(("running", 70, f"node-a carries {ADDRESS} on enp1s0. The rollback is turned off."), self.step(ref, 30))
        self.assertEqual(BONDS.check_script("enp1s0", ADDRESS, "bond0", [], "802.3ad"), self.host.ran[0].script)
        self.assertEqual(BONDS.CONFIRM_SCRIPT, self.host.ran[1].script, "a bond being removed waits for no switch")

    def test_the_host_saying_lacp_failed_ends_the_job_whatever_else_it_says(self):
        for answer in ("LACPFAILED\nEND\n", GOOD_BOND + "LACPFAILED\n"):
            self.host.reset()
            self.host.check = answer
            ref = saved_bond(**self.LACP)
            self.assertEqual(self.LACP_FAILED, self.step(ref, 30))
            self.assertEqual("done", ref["stage"])
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.assertNotIn(BONDS.CONFIRM_SCRIPT, self.host.sent())

    def test_lacp_failed_is_read_on_any_mode(self):
        self.host.check = "LACPFAILED\nEND\n"
        self.assertEqual(self.LACP_FAILED, self.step(saved_bond(), 30))

    def test_up_with_no_partner_waits_for_the_switch_then_rolls_back_inside_the_hold(self):
        self.host.check = self.NO_PARTNER
        ref = saved_bond(**self.LACP)
        waiting = ("running", 40, "node-a is up on bond0. Waiting for the switch to answer LACP.")
        self.assertEqual(waiting, self.step(ref, 30))
        self.assertEqual(waiting, self.step(ref, 74.9))
        self.assertNotIn(BONDS.ROLLBACK_NOW, self.host.sent())
        self.assertEqual(("running", 60, "The switch did not answer LACP on bond0 in 60 seconds, so node-a is putting its old network back now"),
                         self.step(ref, 75))
        run = self.host.ran[-1]
        self.assertEqual((BONDS.ROLLBACK_NOW, 30, "Bond enp1s0 + enp2s0 on node-a", LEASE), (run.script, run.timeout, run.change, run.holder))
        self.assertEqual(("rolled-back", SINCE + 75), (ref["stage"], ref["since"]))
        self.assertNotIn(BONDS.CONFIRM_SCRIPT, self.host.sent())
        self.assertEqual([], self.host.let_go_of, "held until its old network is back")

    def test_rolled_back_says_so_for_thirty_seconds_then_fails(self):
        ref = saved_bond(stage="rolled-back", **self.LACP)
        self.assertEqual(("running", 80, "node-a is putting its old network back"), self.step(ref, 29.9))
        self.assertEqual(("failed", 100, "The switch did not answer LACP, so node-a put its old network back. Group the switch ports "
                          "as one LACP (802.3ad) group, or choose active-backup"), self.step(ref, 30))
        self.assertEqual([("node-a", LEASE)], self.host.let_go_of)

    def test_a_partner_lets_it_confirm(self):
        self.host.check = GOOD_BOND + "PARTNER\n"
        ref = saved_bond(**self.LACP)
        self.assertEqual(("running", 70, f"node-a carries {ADDRESS} on bond0. The rollback is turned off."), self.step(ref, 30))
        self.assertEqual(BONDS.check_script("bond0", ADDRESS, "bond0", ["enp1s0", "enp2s0"], "802.3ad"), self.host.ran[0].script)


class ConfirmedStageTests(Steps):
    """Once the rollback is turned off: kube-vip, then k3s or RKE2 on a cluster."""

    def restart_of(self, service):
        return f"systemd-run --on-active=2 systemctl restart {service} >/dev/null && echo OK"

    def test_a_bond_that_kept_its_interface_is_done_with_nothing_restarted(self):
        ref = saved_bond(stage="confirmed", renamed=False)
        self.assertEqual(("succeeded", 100, Bond.done), self.step(Bond, ref, 5))
        self.assertEqual("done", ref["stage"])
        self.assertEqual([], self.host.ran)
        self.assertEqual([], self.kube.writes, "kube-vip is left alone")
        self.assertEqual([("node-a", LEASE)], self.host.let_go_of)

    def test_a_bond_job_that_never_saved_renamed_counts_as_kept(self):
        ref = saved_bond(stage="confirmed")
        del ref["renamed"]
        self.assertEqual(("succeeded", 100, Bond.done), self.step(Bond, ref, 5))
        self.assertEqual([], self.host.ran)

    def test_on_more_than_one_host_kube_vip_goes_then_the_service_restarts_inside_the_hold(self):
        for kind in self.each():
            self.kube.add(pod("kube-vip-abcde"))
            gone_first = []
            self.host.on_restart = lambda: gone_first.append(self.kube.object(VIP_POD) is None)
            ref = kind.saved(stage="confirmed", since=SINCE - 5)
            progress, words = kind.restarting
            self.assertEqual(("running", progress, words.format(service="k3s")), self.step(kind, ref, 0))
            self.assertEqual([True], gone_first)
            self.assertIsNotNone(self.kube.object("/api/v1/namespaces/kube-system/pods/coredns-1"))
            self.assertIsNotNone(self.kube.object("/api/v1/namespaces/kube-system/pods/kube-vip-other"))
            self.assertEqual(1, len(self.host.ran))
            run = self.host.ran[0]
            self.assertEqual(("node-a", self.restart_of("k3s"), 45, kind.title, LEASE),
                             (run.node, run.script, run.timeout, run.change, run.holder))
            self.assertEqual(("restarting", SINCE, LEASE), (ref["stage"], ref["since"], ref["lease"]))

    def test_the_restart_keeps_what_the_kubelet_last_said(self):
        self.kube.add(host_node(heartbeat=SINCE - 180, transition=SINCE - 3600))
        self.kube.add(DEADLINE.node_lease("node-a", renewed=SINCE - 4))
        for kind in self.each():
            ref = kind.saved(stage="confirmed")
            self.step(kind, ref, 0)
            self.assertEqual((SINCE - 4, SINCE - 180), (ref["renew0"], ref["hb0"]))

    def test_a_restart_keeps_none_for_what_it_could_not_read(self):
        for kind in self.each():
            ref = kind.saved(stage="confirmed")
            self.step(kind, ref, 0)
            self.assertEqual((None, None), (ref["renew0"], ref["hb0"]))

    def test_rke2_restarts_as_k3s_does_and_the_first_service_of_either_is_the_one(self):
        for kind in self.each():
            ref = kind.saved(stage="confirmed", services=["containerd", "rke2-server", "k3s"])
            self.assertEqual(kind.restarting[1].format(service="rke2-server"), self.step(kind, ref, 0)[2])
            self.assertEqual(self.restart_of("rke2-server"), self.host.ran[0].script)

    def test_on_one_host_kube_vip_goes_and_nothing_restarts(self):
        self.kube.discard("/api/v1/nodes/node-b")
        for kind in self.each():
            self.kube.add(pod("kube-vip-abcde"))
            ref = kind.saved(stage="confirmed")
            self.assertEqual(("succeeded", 100, kind.done), self.step(kind, ref, 0))
            self.assertEqual("done", ref["stage"])
            self.assertIsNone(self.kube.object(VIP_POD))
            self.assertEqual([], self.host.ran)
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)

    def test_with_no_k3s_or_rke2_on_the_host_nothing_restarts(self):
        for kind in self.each():
            for services in ([], ["containerd"]):
                ref = kind.saved(stage="confirmed", services=services)
                self.assertEqual(("succeeded", 100, kind.done), self.step(kind, ref, 0))
            ref = kind.saved(stage="confirmed")
            del ref["services"]
            self.assertEqual(("succeeded", 100, kind.done), self.step(kind, ref, 0))
            self.assertEqual([], self.host.ran)

    def test_kube_vip_that_cannot_be_restarted_does_not_stop_the_job(self):
        for kind in self.each():
            self.kube.fail("/api/v1/namespaces/kube-system/pods", 500, times=1)
            ref = kind.saved(stage="confirmed")
            self.assertEqual("running", self.step(kind, ref, 0)[0])
            self.assertEqual("restarting", ref["stage"])
            self.assertEqual(1, len(self.host.ran))

    def test_a_host_count_that_cannot_be_read_restarts_nothing_and_is_asked_again(self):
        for kind in self.each():
            self.kube.add(pod("kube-vip-abcde"))
            self.kube.fail("/api/v1/nodes", 500, times=1)
            ref = kind.saved(stage="confirmed")
            self.assertEqual(("running", kind.confirmed[0], "node-a is on its new network; still trying to reach the cluster's "
                              "hosts (HTTP Error 500: Failure), for up to 600s more"), self.step(kind, ref, 0))
            self.assertEqual(([], "confirmed", {"done": True}), (self.host.ran, ref["stage"], ref["kube_vip"]))

    def test_kube_vip_that_cannot_be_restarted_on_one_host_is_asked_again(self):
        self.kube.discard("/api/v1/nodes/node-b")
        for kind in self.each():
            self.kube.add(pod("kube-vip-abcde"))
            self.kube.fail("/api/v1/namespaces/kube-system/pods", 500, times=1)
            ref = kind.saved(stage="confirmed")
            self.assertEqual(("running", kind.confirmed[0], "node-a is on its new network; still trying to reach kube-vip on "
                              "node-a (HTTP Error 500: Failure), for up to 600s more"), self.step(kind, ref, 0))
            self.assertEqual({"error": "HTTP Error 500: Failure"}, ref["kube_vip"])

    def test_a_host_back_with_kube_vip_still_undone_says_so(self):
        for kind in self.each():
            self.kube.fail("/api/v1/namespaces/kube-system/pods", 500, times=2)
            ref = kind.saved(stage="confirmed")
            self.step(kind, ref, 0)
            self.kube.add(DEADLINE.real_node("node-a", heartbeat=SINCE + 20, transition=SINCE - 3600))
            self.assertEqual(("running", kind.waiting_ready[0], "node-a is on its new network; still trying to reach kube-vip on "
                              "node-a (HTTP Error 500: Failure), for up to 569s more"), self.step(kind, ref, 31))
            self.kube.add(DEADLINE.node("node-a", ready=True))

    def test_a_restart_that_times_out_is_not_asked_again(self):
        for kind in self.each():
            self.host.raises["systemctl restart"] = POD_EXEC.ExecTimeout("did not finish", "", "", 45)
            ref = kind.saved(stage="confirmed")
            self.assertEqual(("running", kind.restarting[0], kind.restarting[1].format(service="k3s")), self.step(kind, ref, 0))
            self.assertEqual("restarting", ref["stage"])
            self.step(kind, ref, 5)
            self.assertEqual(1, len(self.host.ran))

    def test_a_restart_that_fails_otherwise_goes_to_the_job_store_and_is_asked_again(self):
        for kind in self.each():
            self.host.raises["systemctl restart"] = RuntimeError("the helper is gone")
            ref = kind.saved(stage="confirmed")
            with self.assertRaisesRegex(RuntimeError, "the helper is gone"):
                self.step(kind, ref, 0)
            self.assertEqual(("confirmed", LEASE), (ref["stage"], ref["lease"]))
            self.assertEqual([], self.host.let_go_of)


class RestartingStageTests(Steps):
    """After the restart: the job ends on a kubelet heard from, or on a deadline."""

    def setUp(self):
        super().setUp()
        self.kube.add(host_node(heartbeat=SINCE - 180, transition=SINCE - 3600))
        self.kube.add(DEADLINE.node_lease("node-a", renewed=SINCE - 4))

    def saved(self, kind, **changes):
        return kind.saved(stage="restarting", renew0=SINCE - 4, hb0=SINCE - 180, **changes)

    def back(self):
        self.kube.add(DEADLINE.node_lease("node-a", renewed=SINCE + 24))

    def not_back(self, kind, service="k3s"):
        return (f"node-a was not Ready again 10 minutes after {service} restarted on {kind.interface}. "
                f"On node-a, check `journalctl -u {service}` and that {kind.interface} carries {ADDRESS}. "
                "If it becomes Ready later, nothing more is needed: the new network is kept. "
                f"The old configuration is in {BACKUP}")

    def test_a_kubelet_heard_from_is_not_believed_in_the_first_thirty_seconds(self):
        self.back()
        for kind in self.each():
            self.assertEqual(("running", *kind.waiting_ready), self.step(kind, self.saved(kind), 30))

    def test_a_kubelet_heard_from_ends_it(self):
        self.back()
        for kind in self.each():
            ref = self.saved(kind)
            self.assertEqual(("succeeded", 100, kind.done), self.step(kind, ref, 30.5))
            self.assertEqual("done", ref["stage"])
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)

    def test_it_waits_up_to_the_deadline_and_fails_past_it_naming_what_to_check(self):
        for kind in self.each():
            ref = self.saved(kind)
            self.assertEqual(("running", *kind.waiting_ready), self.step(kind, ref, 600))
            self.assertEqual([], self.host.let_go_of)
            self.assertEqual(("failed", 100, self.not_back(kind)), self.step(kind, ref, 600.5))
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.assertEqual("restarting", ref["stage"])

    def test_the_failure_names_the_service_the_host_has(self):
        for kind in self.each():
            self.assertEqual(self.not_back(kind, "rke2-agent"),
                             self.step(kind, self.saved(kind, services=["containerd", "rke2-agent"]), 601)[2])
            self.assertEqual(self.not_back(kind, "k3s"), self.step(kind, self.saved(kind, services=[]), 601)[2])

    def test_a_kubelet_heard_from_past_the_deadline_still_succeeds(self):
        self.back()
        for kind in self.each():
            self.assertEqual(("succeeded", 100, kind.done), self.step(kind, self.saved(kind), 700))

    def test_a_job_saved_before_baselines_goes_by_when_the_stage_started(self):
        self.back()
        for kind in self.each():
            ref = kind.saved(stage="restarting")
            self.assertEqual(("succeeded", 100, kind.done), self.step(kind, ref, 31))

    def test_a_node_gone_lets_go_and_goes_to_the_job_store(self):
        for kind in self.each():
            self.kube.fail("/api/v1/nodes/node-a", 404, times=1)
            ref = self.saved(kind)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.step(kind, ref, 31)
            self.assertEqual(404, caught.exception.code)
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.assertNotIn("lease", ref)

    def test_any_other_api_error_keeps_the_host_held(self):
        for kind in self.each():
            self.kube.fail("/api/v1/nodes/node-a", 500, times=1)
            ref = self.saved(kind)
            with self.assertRaises(urllib.error.HTTPError):
                self.step(kind, ref, 31)
            self.assertEqual([], self.host.let_go_of)
            self.assertEqual(LEASE, ref["lease"])


class FinishedTests(Steps):
    def test_a_job_saved_as_done_says_so_and_lets_go(self):
        for kind in self.each():
            ref = kind.saved(stage="done")
            self.assertEqual(("succeeded", 100, kind.after_done), self.step(kind, ref, 5))
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.assertEqual([], self.host.ran)

    def test_a_job_with_no_lease_lets_go_of_nothing(self):
        for kind in self.each():
            ref = kind.saved(stage="done")
            del ref["lease"]
            self.assertEqual(("succeeded", 100, kind.after_done), self.step(kind, ref, 5))
            self.assertEqual([], self.host.let_go_of)

    def test_a_host_is_let_go_of_only_once(self):
        for kind in self.each():
            ref = kind.saved(stage="done")
            self.step(kind, ref, 5)
            self.step(kind, ref, 6)
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)


class FollowUpsOfAnOlderJobTests(Steps):
    """A job saved with no name for its change or no lease still runs its
    follow-ups, under the name each kind has always given them."""

    def older(self, kind, **changes):
        ref = kind.saved(**changes)
        del ref["change"], ref["lease"]
        return ref

    def test_its_confirm(self):
        for kind in self.each():
            self.host.check = kind.good
            self.assertEqual(("running", *kind.confirmed), self.step(kind, self.older(kind), 30))
            run = self.host.ran[1]
            self.assertEqual((kind.module.CONFIRM_SCRIPT, kind.unnamed_change, ""), (run.script, run.change, run.holder))

    def test_its_restart(self):
        for kind in self.each():
            self.step(kind, self.older(kind, stage="confirmed"), 0)
            run = self.host.ran[0]
            self.assertIn("systemctl restart k3s", run.script)
            self.assertEqual((kind.unnamed_change, ""), (run.change, run.holder))

    def test_a_bonds_rollback(self):
        self.host.check = GOOD_BOND
        self.step(Bond, self.older(Bond, mode="802.3ad"), 80)
        run = self.host.ran[1]
        self.assertEqual((BONDS.ROLLBACK_NOW, Bond.unnamed_change, ""), (run.script, run.change, run.holder))

    def test_a_job_with_no_since_counts_from_now(self):
        for kind in self.each():
            ref = kind.saved()
            del ref["since"]
            self.assertEqual(("running", 20, "Applying on node-a"), self.step(kind, ref, 5000))


class UnconfirmedStageTests(Steps):
    """A change script that ran past its limit: the job needs a person, and
    keeps the host held until one comes."""

    def needs_attention(self, kind, held="node-a stays held, so no other change starts there"):
        return ("running", 20,
                f"{kind.title} needs attention: the script on node-a did not finish within 120 seconds, so Homestead cannot "
                f"tell whether it changed node-a's network. {held}. Check its network (if the change got as far as arming "
                "its rollback, node-a puts its old network back by itself within 4 minutes), then cancel this job to let go of node-a")

    def test_it_says_what_is_not_known_and_holds_on(self):
        for kind in self.each():
            ref = saved_unconfirmed(kind.title)
            self.assertEqual(self.needs_attention(kind), self.step(kind, ref, 59.5))
            self.assertEqual([], self.host.holds, "not renewed yet")
            self.assertEqual([], self.host.let_go_of)
            self.assertEqual([], self.host.ran, "the host is not asked anything")

    def test_the_hold_is_renewed_each_minute_for_as_long_as_each_kind_holds(self):
        for kind in self.each():
            ref = saved_unconfirmed(kind.title)
            self.assertEqual(self.needs_attention(kind), self.step(kind, ref, 60))
            self.assertEqual([("node-a", kind.title, kind.hold_seconds, LEASE)], self.host.holds)
            self.assertEqual(SINCE + 60, ref["renewed"])
            self.step(kind, ref, 119)
            self.assertEqual(1, len(self.host.holds))
            self.step(kind, ref, 120)
            self.assertEqual(2, len(self.host.holds))

    def test_a_hold_not_renewed_is_said_until_one_is(self):
        for kind in self.each():
            real = self.host.hold

            def refused(node, what, seconds=60, holder=""):
                raise RuntimeError("the API server did not answer " + "x" * 200)
            self.host.hold = refused
            ref = saved_unconfirmed(kind.title)
            cut = "the API server did not answer ".ljust(160, "x")
            held = (f"Homestead could not renew its hold on node-a ({cut}), so another change may start "
                    "there once the hold runs out")
            self.assertEqual(self.needs_attention(kind, held), self.step(kind, ref, 60))
            self.assertEqual(cut, ref["renew_error"])
            self.assertEqual(SINCE, ref["renewed"], "asked again next round")
            self.assertEqual(self.needs_attention(kind, held), self.step(kind, ref, 61))
            self.host.hold = real
            self.assertEqual(self.needs_attention(kind), self.step(kind, ref, 62))
            self.assertNotIn("renew_error", ref)

    def test_a_refusal_with_no_words_is_named_by_its_type(self):
        for kind in self.each():
            def refused(node, what, seconds=60, holder=""):
                raise RuntimeError()
            self.host.hold = refused
            ref = saved_unconfirmed(kind.title)
            self.step(kind, ref, 60)
            self.assertEqual("RuntimeError", ref["renew_error"])

    def test_a_job_saved_with_no_renewal_renews_at_once_and_with_no_lease_under_no_holder(self):
        for kind in self.each():
            ref = saved_unconfirmed(kind.title)
            del ref["renewed"], ref["lease"]
            self.step(kind, ref, 0)
            self.assertEqual([("node-a", kind.title, kind.hold_seconds, "")], self.host.holds)

    def test_a_limit_of_a_fraction_is_written_shortly(self):
        for kind in self.each():
            ref = saved_unconfirmed(kind.title, timeout=2.5)
            self.assertIn("did not finish within 2.5 seconds", self.step(kind, ref, 1)[2])


class Ops:
    def __init__(self):
        self.started = []

    def start(self, kind, title, resource, href, ref, message="Waiting for Kubernetes"):
        self.started.append({"kind": kind, "title": title, "resource": resource, "href": href, "ref": ref, "message": message})
        return {"id": "op1"}


class WhatAJobSavesTests(unittest.TestCase):
    """The row each kind's start() writes: running jobs carry these fields
    across a restart of Homestead, and across an upgrade."""

    def setUp(self):
        keep_bound(self)

    def bond(self, host):
        BONDS.bind(host, lambda path: {"items": [{}, {}]}, lambda *a, **k: None)
        ops = Ops()
        return BONDS.start("h1", {"members": ["enp2s0"]}, ops), ops.started[0]

    def bridge(self, host):
        BRIDGE.bind(host, lambda path: {"items": [{}, {}]}, lambda *a, **k: None)
        ops = Ops()
        return BRIDGE.start("k3s", ops), ops.started[0]

    def timed_out(self, host):
        real = host.run

        def run(node, script, timeout=60, change="", holder=""):
            if change:
                raise POD_EXEC.ExecTimeout("did not finish", "", "", 120)
            return real(node, script, timeout, change, holder)
        host.run = run
        return host

    def test_a_bond(self):
        host = BOND_TESTS.Host(BOND_TESTS.facts(), "")
        op, row = self.bond(host)
        ref = dict(row["ref"])
        self.assertIsInstance(ref.pop("since"), float)
        self.assertEqual({"node": "h1", "action": "create", "bond": "bond0", "members": ["enp1s0", "enp2s0"],
                          "mode": "active-backup", "carries_on": "bond0", "address": "192.0.2.12/24", "renamed": True,
                          "backup": "/var/lib/homestead/netplan-1", "services": ["k3s"], "stage": "applied",
                          "change": "Bond enp1s0 + enp2s0 on h1", "lease": "this-replica/1"}, ref)
        self.assertEqual(("host-bond", "Bond enp1s0 + enp2s0 on h1", {"kind": "Node", "name": "h1"}, "/nodes?node=h1&section=network",
                          "Applied. Checking h1, which restores its old network in 4 minutes unless the change is confirmed."),
                         (row["kind"], row["title"], row["resource"], row["href"], row["message"]))
        self.assertNotIn("unconfirmed", op)

    def test_a_bridge(self):
        host = BRIDGE_TESTS.Host(BRIDGE_TESTS.HostLeaseTests.GOOD)
        op, row = self.bridge(host)
        ref = dict(row["ref"])
        self.assertIsInstance(ref.pop("since"), float)
        self.assertEqual({"node": "k3s", "interface": "enp1s0", "address": "192.0.2.109/24", "bridge": "br0",
                          "backup": "/var/lib/homestead/netplan-x", "stage": "applied", "services": ["k3s"],
                          "change": "Move k3s's enp1s0 into br0", "lease": "this-replica/1"}, ref)
        self.assertEqual(("host-bridge", "Move k3s's enp1s0 into br0", {"kind": "Node", "name": "k3s"}, "/nodes",
                          "Applied. Checking k3s on br0, which restores its old network in 4 minutes unless the change is confirmed."),
                         (row["kind"], row["title"], row["resource"], row["href"], row["message"]))
        self.assertNotIn("unconfirmed", op)

    def check_unconfirmed(self, row, op, node, change):
        ref = dict(row["ref"])
        self.assertIsInstance(ref.pop("since"), float)
        self.assertIsInstance(ref.pop("renewed"), float)
        self.assertEqual({"node": node, "stage": "unconfirmed", "change": change, "lease": "this-replica/1", "timeout": 120}, ref)
        self.assertIs(True, op["unconfirmed"])
        self.assertEqual(f"{change} needs attention: the script on {node} did not finish within 120 seconds, so Homestead cannot "
                         f"tell whether it changed {node}'s network. {node} stays held, so no other change starts there. Check its "
                         f"network (if the change got as far as arming its rollback, {node} puts its old network back by itself "
                         f"within 4 minutes), then cancel this job to let go of {node}", row["message"])

    def test_a_bond_whose_script_ran_past_its_limit(self):
        op, row = self.bond(self.timed_out(BOND_TESTS.Host(BOND_TESTS.facts(), "")))
        self.check_unconfirmed(row, op, "h1", "Bond enp1s0 + enp2s0 on h1")
        self.assertEqual(("host-bond", "/nodes?node=h1&section=network"), (row["kind"], row["href"]))

    def test_a_bridge_whose_script_ran_past_its_limit(self):
        op, row = self.bridge(self.timed_out(BRIDGE_TESTS.Host(BRIDGE_TESTS.HostLeaseTests.GOOD)))
        self.check_unconfirmed(row, op, "k3s", "Move k3s's enp1s0 into br0")
        self.assertEqual(("host-bridge", "/nodes"), (row["kind"], row["href"]))


class CancelTests(Steps):
    """Cancelling is each kind's own, and lets go of the host."""
    WORDS = {
        Bond: "The change carries on on node-a: unless it was confirmed, node-a puts its old network back by itself",
        Bridge: "The move carries on on node-a: unless it was confirmed, node-a puts its old network back by itself"}

    def test_the_review_says_what_carries_on(self):
        for kind in self.each():
            self.assertEqual({"mode": "forget", "needs": "admin",
                              "keeps": [self.WORDS[kind], "Other changes on node-a can start again at once"]},
                             kind.module.cancel_plan({"ref": kind.saved()}))

    def test_the_review_of_an_unconfirmed_job_claims_nothing(self):
        for kind in self.each():
            self.assertEqual({"mode": "forget", "needs": "admin",
                              "keeps": ["Homestead cannot tell how far the change got on node-a. Check its network first.",
                                        "Other changes on node-a can start again at once"]},
                             kind.module.cancel_plan({"ref": saved_unconfirmed(kind.title)}))

    def test_stopping_tracking_lets_go_at_once_and_says_so(self):
        for kind in self.each():
            ref = kind.saved()
            self.assertEqual("Stopped tracking it. Other changes on node-a can start again.", kind.module.cancel_run({"ref": ref}, {}))
            self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
            self.assertNotIn("lease", ref)

    def test_each_kind_registers_its_status_and_cancel_under_the_name_saved_jobs_carry(self):
        for kind in self.each():
            ops = mock.Mock(RESOLVERS={}, CANCELLERS={})
            kind.module.register(ops)
            self.assertEqual([kind.kind], list(ops.RESOLVERS))
            self.assertIs(kind.module.status, ops.RESOLVERS[kind.kind])
            self.assertEqual((kind.module.cancel_plan, kind.module.cancel_run), tuple(ops.CANCELLERS[kind.kind]))


class SavedJobTests(DEADLINE.GuardBase):
    """A row in the real job store, read back from its file, carried through
    to the end: what the Jobs page shows at each round, on a fake clock."""

    def setUp(self):
        super().setUp()
        self.kube.add(pod("kube-vip-abcde"))
        self.kube.add(host_node(heartbeat=SINCE - 180, transition=SINCE - 3600))
        self.kube.add(DEADLINE.node_lease("node-a", renewed=SINCE - 4))
        self.host = Host()
        # The fake clock with everything reading a kubelet's times needs too.
        clock = types.SimpleNamespace(time=lambda: self.now, strftime=time.strftime, strptime=time.strptime)
        for module in (BONDS, BRIDGE):
            self.enterContext(mock.patch.object(module, "time", clock))
            self.enterContext(mock.patch.multiple(module, hostrun=module.hostrun, kget=module.kget, ksend=module.ksend))
            module.bind(self.host, K.kget, K.ksend)

    def round(self, job):
        OPS.list_operations()
        row = self.job(job)
        return row["status"], row["message"], row["ref"]["stage"]

    def check_from_applied_to_the_end(self, kind):
        self.host.check = kind.good
        job = OPS.start(kind.kind, kind.title, {"kind": "Node", "name": "node-a"}, "/nodes", kind.saved(), "Applied")["id"]
        self.now = SINCE + 5
        self.assertEqual(("running", "Applying on node-a", "applied"), self.round(job))
        self.now = SINCE + 20
        self.assertEqual(("running", kind.confirmed[1], "confirmed"), self.round(job))
        self.now = SINCE + 50
        self.assertEqual(("running", kind.restarting[1].format(service="k3s"), "restarting"), self.round(job))
        self.assertIsNone(self.kube.object(VIP_POD))
        self.now = SINCE + 70
        self.assertEqual(("running", kind.waiting_ready[1], "restarting"), self.round(job))
        self.assertEqual(kind.title, BONDS.network_guard("node-b")[0], "other hosts wait for it")
        self.now = SINCE + 90
        self.kube.add(DEADLINE.node_lease("node-a", renewed=SINCE + 80))
        self.assertEqual(("succeeded", kind.done, "done"), self.round(job))
        self.assertEqual("", BONDS.network_guard("node-b")[0], "and no longer")
        self.assertEqual([("node-a", LEASE)], self.host.let_go_of)
        self.assertEqual([kind.check, kind.module.CONFIRM_SCRIPT], self.host.sent()[:2])
        self.assertIn("systemctl restart k3s", self.host.sent()[2])
        self.assertEqual(3, len(self.host.ran), "each asked once")

    def test_a_bond_saved_just_after_it_applied(self):
        self.check_from_applied_to_the_end(Bond)

    def test_a_bridge_saved_just_after_it_applied(self):
        self.check_from_applied_to_the_end(Bridge)

    def test_a_job_saved_at_each_later_stage_carries_on(self):
        for kind, stage, want in ((Bond, "confirmed", "restarting"), (Bridge, "confirmed", "restarting"),
                                  (Bond, "restarting", "restarting"), (Bridge, "restarting", "restarting"),
                                  (Bond, "rolled-back", "rolled-back"), (Bond, "unconfirmed", "unconfirmed"),
                                  (Bridge, "unconfirmed", "unconfirmed")):
            with self.subTest(kind=kind.kind, stage=stage):
                self.now = SINCE + 5
                ref = kind.saved(stage=stage, timeout=120, renewed=SINCE) if stage == "unconfirmed" else kind.saved(stage=stage)
                job = OPS.start(kind.kind, kind.title, {"kind": "Node", "name": "node-a"}, "/nodes", ref, "Saved")["id"]
                status, _, stage_now = self.round(job)
                self.assertEqual(("running", want), (status, stage_now))


class BothKindsWaitTests(DEADLINE.GuardBase):
    """Neither kind starts while another host's network is changing or another
    server is not Ready: the guard is shared, whichever kind asks."""

    def setUp(self):
        super().setUp()
        self.bond_host = BOND_TESTS.Host(BOND_TESTS.facts(), "")
        self.bridge_host = BRIDGE_TESTS.Host(BRIDGE_TESTS.HostLeaseTests.GOOD)
        keep_bound(self)
        BONDS.bind(self.bond_host, K.kget, K.ksend)
        BRIDGE.bind(self.bridge_host, K.kget, K.ksend)

    def send(self, kind):
        if kind is Bond:
            return BONDS.change({"node": "node-b", "confirm": "node-b", "members": ["enp2s0"]})
        return BRIDGE.convert({"node": "node-b", "confirm": "node-b"})

    def refused(self, words):
        for kind in KINDS:
            with self.subTest(kind=kind.kind):
                with self.assertRaises(ValueError) as caught:
                    self.send(kind)
                self.assertTrue(str(caught.exception).startswith("node-b: "))
                self.assertIn(words, str(caught.exception))
        self.assertEqual([], self.bond_host.holds + self.bridge_host.holds, "nothing is held")
        self.assertEqual([], self.bond_host.changes + self.bridge_host.changes, "nothing is changed")

    def running(self, kind):
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {kind.kind: lambda item: ("running", 92, "Waiting")}))
        OPS.start(kind.kind, kind.title, {"kind": "Node", "name": "node-a"}, "/nodes", kind.saved(stage="restarting"))

    def test_both_wait_while_a_bond_on_another_host_runs(self):
        self.running(Bond)
        self.refused("another host's network is changing (Bond enp1s0 + enp2s0 on node-a): one host at a time")

    def test_both_wait_while_a_bridge_on_another_host_runs(self):
        self.running(Bridge)
        self.refused("another host's network is changing (Move node-a's enp1s0 into br0): one host at a time")

    def test_both_wait_while_another_server_is_not_ready(self):
        self.kube.add(DEADLINE.node("node-c", ready=False, server=True))
        self.refused("1 other server is not Ready or has sent no recent heartbeat. If node-b stops answering "
                     "too, the cluster could lose its quorum. Try again when every server is Ready")

    def test_a_bond_goes_ahead_on_a_quiet_cluster(self):
        self.assertIs(True, self.send(Bond)["ok"])
        self.assertEqual(["host-bond"], [row["kind"] for row in OPS._read()])
        self.assertEqual([("node-b", "Bond enp1s0 + enp2s0 on node-b")], [h[:2] for h in self.bond_host.holds])

    def test_a_bridge_goes_ahead_on_a_quiet_cluster(self):
        self.assertIs(True, self.send(Bridge)["ok"])
        self.assertEqual(["host-bridge"], [row["kind"] for row in OPS._read()])
        self.assertEqual([("node-b", "Move node-b's enp1s0 into br0")], [h[:2] for h in self.bridge_host.holds])


if __name__ == "__main__":
    unittest.main()
