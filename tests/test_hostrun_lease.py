"""Two changes to one host never run at once: a run that changes a host takes
that host's Lease first and gives it back when it ends; another change is
refused while it is held, a read never waits for it, and a lease left by a
replica that stopped runs out and is taken over (#597)."""
import datetime
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_hostrun as HOSTRUN
import homestead_kube as K
import homestead_operations as OPS
from fakes.kube import Kube

PODS = "/api/v1/namespaces/lab/pods"
LEASES = "/apis/coordination.k8s.io/v1/namespaces/lab/leases"


def stamp(seconds_ago=0):
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def held(node, holder, what, seconds_ago=0, duration=60):
    return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
            "metadata": {"name": HOSTRUN.lease_name(node), "namespace": "lab", "resourceVersion": "1",
                         "annotations": {HOSTRUN.NAMES.key("change"): what}},
            "spec": {"holderIdentity": holder, "leaseDurationSeconds": duration,
                     "renewTime": stamp(seconds_ago), "acquireTime": stamp(seconds_ago)}}


class Cluster:
    """The host's side (what the exec runs, and what happens during it) around
    the shared fake API (tests/fakes/kube.py), where a helper pod that is made
    starts Running, as it does once its node is up."""

    def __init__(self, test, leases=()):
        self.ran = []
        self.kube = test.enterContext(Kube(*leases))
        self.during = None
        # The shared fake runs the real write guards; the storage guard takes the job store's lock under DATA_DIR.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))

    @property
    def pods(self):
        return K.kget(PODS)["items"]

    @property
    def sent(self):
        return [(method, path) for method, path, _, _ in self.kube.writes]

    def send(self, method, path, body=None, **kw):
        result = K.ksend(method, path, body, **kw)
        if method == "POST" and path == PODS:
            K.ksend("PATCH", f"{PODS}/{body['metadata']['name']}/status", {"status": {"phase": "Running"}},
                    ctype="application/merge-patch+json")
        return result

    def exec_in(self, namespace, pod, argv, **kwargs):
        self.ran.append(argv[-1])
        if self.during:
            self.during()
        return b"ok", ""

    def lease(self, node):
        return self.kube.object(f"{LEASES}/{HOSTRUN.lease_name(node)}")

    def holder(self, node):
        return ((self.lease(node) or {}).get("spec") or {}).get("holderIdentity", "")


def nap(_seconds):
    pass


class HostLeaseTests(unittest.TestCase):
    def use(self, leases=()):
        self.cluster = Cluster(self, leases)
        self.enterContext(mock.patch.multiple(HOSTRUN, kget=HOSTRUN.kget, ksend=HOSTRUN.ksend,
                                              exec_in=HOSTRUN.exec_in, NS=HOSTRUN.NS))
        HOSTRUN.bind(K.kget, self.cluster.send, self.cluster.exec_in, "lab")
        return self.cluster

    def test_a_change_holds_the_hosts_lease_while_it_runs_and_gives_it_back(self):
        c = self.use()
        seen = []
        c.during = lambda: seen.append(c.holder("node-2"))
        HOSTRUN.run("node-2", "wipefs -a /dev/sdb", change="Erase /dev/sdb on node-2", sleep=nap)
        self.assertTrue(seen[0], "the lease is held while the script runs")
        self.assertEqual("", c.holder("node-2"), "and given back when it ends")

    def test_a_second_change_on_the_same_host_is_refused(self):
        c = self.use()
        started, finish, refused = threading.Event(), threading.Event(), []

        def first_runs():
            started.set()
            finish.wait(5)

        c.during = first_runs
        first = threading.Thread(target=HOSTRUN.run, args=("node-2", "mkfs /dev/sdb"),
                                 kwargs={"change": "Prepare /dev/sdb on node-2", "sleep": nap})
        first.start()
        self.assertTrue(started.wait(5))
        c.during = None
        try:
            HOSTRUN.run("node-2", "netplan apply", change="Bond eth0 + eth1 on node-2", sleep=nap)
        except HOSTRUN.Busy as error:
            refused.append(str(error))
        finally:
            finish.set()
            first.join(5)
        self.assertEqual(["another change is running on node-2: Prepare /dev/sdb on node-2; try again once it has finished"], refused)
        self.assertEqual(["mkfs /dev/sdb"], c.ran, "the second change's script never ran")
        self.assertEqual("", c.holder("node-2"))

    def test_a_held_lease_refuses_before_any_helper_is_made(self):
        c = self.use([held("node-2", "other-replica/abc", "Install 3 updates on node-2")])
        with self.assertRaisesRegex(HOSTRUN.Busy, "another change is running on node-2: Install 3 updates on node-2"):
            HOSTRUN.run("node-2", "netplan apply", change="Move eth0 into br0 on node-2", sleep=nap)
        self.assertEqual([], c.pods)
        self.assertFalse([p for m, p in c.sent if p.startswith(PODS)])
        self.assertEqual("other-replica/abc", c.holder("node-2"), "the other change keeps its lease")

    def test_a_read_never_waits_for_a_change(self):
        c = self.use([held("node-2", "other-replica/abc", "Install 3 updates on node-2")])
        out, _ = HOSTRUN.run("node-2", "cat /etc/os-release", sleep=nap)
        self.assertEqual("ok", out)
        self.assertFalse([p for m, p in c.sent if p.startswith(LEASES)], "a read leaves the lease alone")

    def test_a_change_on_another_host_is_not_held_back(self):
        c = self.use([held("node-2", "other-replica/abc", "Install 3 updates on node-2")])
        HOSTRUN.run("node-3", "mkfs /dev/sdb", change="Prepare /dev/sdb on node-3", sleep=nap)
        self.assertEqual(["mkfs /dev/sdb"], c.ran)

    def test_a_lease_that_ran_out_is_taken_over(self):
        c = self.use([held("node-2", "stopped-replica/abc", "Prepare /dev/sdb on node-2", seconds_ago=120, duration=60)])
        seen = []
        c.during = lambda: seen.append(c.holder("node-2"))
        HOSTRUN.run("node-2", "mkfs /dev/sdc", change="Prepare /dev/sdc on node-2", sleep=nap)
        self.assertTrue(seen[0] and seen[0] != "stopped-replica/abc")
        self.assertEqual("", c.holder("node-2"))

    def test_the_lease_is_given_back_when_the_run_fails(self):
        c = self.use()

        def cut_off():
            raise ConnectionError("the exec was cut off")

        c.during = cut_off
        with self.assertRaises(ConnectionError):
            HOSTRUN.run("node-2", "mkfs /dev/sdb", change="Prepare /dev/sdb on node-2", sleep=nap)
        self.assertEqual("", c.holder("node-2"))
        c.during = None
        HOSTRUN.run("node-2", "mkfs /dev/sdb", change="Prepare /dev/sdb on node-2", sleep=nap)
        self.assertEqual(2, len(c.ran), "the next change is free to go")

    def test_the_lease_is_renewed_while_a_long_change_runs(self):
        c = self.use()
        saved = HOSTRUN.RENEW_EVERY
        HOSTRUN.RENEW_EVERY = 0.02
        try:
            c.during = lambda: time.sleep(0.15)
            HOSTRUN.run("node-2", "apt-get -y upgrade", change="Install updates on node-2", sleep=nap)
        finally:
            HOSTRUN.RENEW_EVERY = saved
        puts = [p for m, p in c.sent if m == "PUT" and p.startswith(LEASES)]
        self.assertGreaterEqual(len(puts), 3, "renewed at least once between taking and giving back")

    def test_the_renewal_stops_with_the_run_and_never_takes_the_lease_back(self):
        c = self.use()
        saved = HOSTRUN.RENEW_EVERY
        HOSTRUN.RENEW_EVERY = 0.02
        try:
            c.during = lambda: time.sleep(0.05)
            HOSTRUN.run("node-2", "apt-get -y upgrade", change="Install updates on node-2", sleep=nap)
            time.sleep(0.1)
        finally:
            HOSTRUN.RENEW_EVERY = saved
        self.assertEqual("", c.holder("node-2"), "nothing renews a lease given back")
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("host-lease-")])

    def test_a_hold_keeps_the_host_across_several_runs(self):
        c = self.use()
        holder = HOSTRUN.hold("node-2", "Bond eth0 + eth1 on node-2", seconds=600)
        HOSTRUN.run("node-2", "netplan apply", change="Bond eth0 + eth1 on node-2", holder=holder, sleep=nap)
        self.assertEqual(holder, c.holder("node-2"), "a run inside a hold leaves it held")
        self.assertEqual(600, c.lease("node-2")["spec"]["leaseDurationSeconds"], "and keeps its length")
        with self.assertRaisesRegex(HOSTRUN.Busy, "Bond eth0 \\+ eth1 on node-2"):
            HOSTRUN.run("node-2", "mkfs /dev/sdb", change="Prepare /dev/sdb on node-2", sleep=nap)
        HOSTRUN.run("node-2", "systemctl stop rollback.timer", change="Bond eth0 + eth1 on node-2", holder=holder, sleep=nap)
        HOSTRUN.let_go("node-2", holder)
        self.assertEqual("", c.holder("node-2"))

    def test_letting_go_never_frees_another_holders_lease(self):
        c = self.use([held("node-2", "other-replica/abc", "Install 3 updates on node-2")])
        HOSTRUN.let_go("node-2", "this-replica/old")
        self.assertEqual("other-replica/abc", c.holder("node-2"))

    def test_the_lease_name_fits_and_is_one_per_host(self):
        node = "a-very-long-node-name-from-a-cloud-provider-eu-west-1-instance-0001"
        self.assertEqual(HOSTRUN.lease_name(node), HOSTRUN.lease_name(node))
        self.assertLessEqual(len(HOSTRUN.lease_name(node)), 63)
        self.assertNotEqual(HOSTRUN.lease_name(node), HOSTRUN.lease_name(node[:-1] + "2"),
                            "two long names that differ only at the end are two hosts")
        self.assertNotEqual(HOSTRUN.lease_name("node-2"), HOSTRUN.lease_name("node-3"))


if __name__ == "__main__":
    unittest.main()
