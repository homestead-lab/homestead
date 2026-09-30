"""kube-vip stopped electing for a shared lease when one Service on it lost
its pods for a moment, and did not start again: SMB, Homestead's VIP and the
object store on 192.0.2.108 were all unreachable (k3s-test, 2.8.231)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vips as VIPS

PLATFORM = {"load_balancer": "kube-vip", "vip_class": "kube-vip.io/kube-vip-class", "servicelb": True,
            "vip_service_election": True}
LEASE = "homestead-vip-192-0-2-108"


def service(name):
    return {"metadata": {"namespace": "lab", "name": name,
                         "annotations": {VIPS.LEASE_KEY: LEASE, VIPS.VIP_KEY: "192.0.2.108"}},
            "spec": {"type": "LoadBalancer", "loadBalancerClass": "kube-vip.io/kube-vip-class"}}


def lease(holder, renewed="2026-09-29T08:03:37.500357Z"):
    return {"metadata": {"namespace": "lab", "name": LEASE},
            "spec": {"holderIdentity": holder, "renewTime": renewed, "leaseDurationSeconds": 15}}


def slices(*ready):
    return [{"metadata": {"namespace": "lab", "labels": {"kubernetes.io/service-name": name}},
             "endpoints": [{"conditions": {"ready": True}}]} for name in ready]


class ReviveTests(unittest.TestCase):
    def setUp(self):
        self.deleted = []
        pods = {"items": [{"metadata": {"name": "kube-vip-24rxl", "ownerReferences": [{"kind": "DaemonSet"}]}},
                          {"metadata": {"name": "kube-vip-helm-install", "ownerReferences": [{"kind": "Job"}]}}]}
        VIPS.bind(lambda path: pods, lambda method, path, body=None, **k: self.deleted.append(path))
        VIPS._stranded_since.clear()
        VIPS._restarted[0] = 0.0
        self.services = [service("homestead-smb"), service("homestead-vip"), service("homestead-objectstore")]

    def test_a_shared_lease_nobody_holds_while_its_services_run_restarts_kube_vip_once(self):
        now = VIPS._when("2026-09-29T08:30:00Z")
        leases = [lease("")]
        ready = slices("homestead-smb", "homestead-vip")
        self.assertEqual([("lab", LEASE, ["homestead-objectstore", "homestead-smb", "homestead-vip"])],
                         VIPS.stranded(self.services, leases, ready, PLATFORM, now))
        self.assertEqual([], VIPS.revive(self.services, leases, ready, PLATFORM, now), "given a minute first")
        self.assertEqual([("lab", LEASE)], VIPS.revive(self.services, leases, ready, PLATFORM, now + 61))
        self.assertEqual(["/api/v1/namespaces/kube-system/pods/kube-vip-24rxl"], self.deleted, "the DaemonSet's pod only")
        VIPS.revive(self.services, leases, ready, PLATFORM, now + 200)
        VIPS.revive(self.services, leases, ready, PLATFORM, now + 300)
        self.assertEqual(1, len(self.deleted), "at most every ten minutes")

    def test_nothing_is_restarted_while_a_node_holds_it_or_nothing_behind_it_is_ready(self):
        now = VIPS._when("2026-09-29T08:30:00Z")
        held = [lease("k3s", "2026-09-29T08:29:55Z")]
        self.assertEqual([], VIPS.stranded(self.services, held, slices("homestead-smb"), PLATFORM, now))
        self.assertEqual([], VIPS.stranded(self.services, [lease("")], slices(), PLATFORM, now),
                         "no pod ready: kube-vip is right to hold nothing")
        import time as _time
        for later in (0, 61, 700):
            renewed = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now + later - 5))
            VIPS.revive(self.services, [lease("k3s", renewed)], slices("homestead-smb"), PLATFORM, now + later)
        self.assertEqual([], self.deleted)


if __name__ == "__main__":
    unittest.main()


class GlobalElectionTests(unittest.TestCase):
    """node-3, 2026-09-30: with one leader for every VIP (plndr-svcs-lock), no
    Service's own lease is held, and kube-vip was restarted every ten minutes,
    dropping 192.0.2.200 each time."""

    def test_under_global_election_nothing_is_stranded_or_restarted(self):
        deleted = []
        pods = {"items": [{"metadata": {"name": "kube-vip-24rxl", "ownerReferences": [{"kind": "DaemonSet"}]}}]}
        VIPS.bind(lambda path: pods, lambda method, path, body=None, **k: deleted.append(path))
        VIPS._stranded_since.clear()
        VIPS._restarted[0] = 0.0
        now = VIPS._when("2026-09-30T06:30:00Z")
        services = [service("homestead-vip"), service("home-assistant-core")]
        ready = slices("homestead-vip", "home-assistant-core")
        for election in (False, None):
            with self.subTest(svc_election=election):
                platform = dict(PLATFORM, vip_service_election=election)
                self.assertEqual([], VIPS.stranded(services, [lease("")], ready, platform, now))
                self.assertEqual([], VIPS.revive(services, [lease("")], ready, platform, now + 3600))
        self.assertEqual([], deleted)
