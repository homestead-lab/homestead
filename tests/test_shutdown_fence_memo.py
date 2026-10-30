"""The cluster-shutdown fence in front of every write keeps what it read for
FENCE_TTL seconds (#985 §4): N lease renews cost N writes and a few journal
reads, not two reads each. It still fails closed, and a shutdown begun on
another copy blocks writes here within the TTL."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_cluster_shutdown as CS
import homestead_kube as K
from fakes.kube import Kube

LEASE = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": {"name": "leader", "namespace": "lab"}}
LEASE_PATH = "/apis/coordination.k8s.io/v1/namespaces/lab/leases/leader"


class FenceMemoTests(unittest.TestCase):
    def setUp(self):
        self.kube = Kube(LEASE).__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.now = [1000.0]
        self.state = [None]
        self.reads = []
        def state():
            self.reads.append(1)
            if isinstance(self.state[0], Exception):
                raise self.state[0]
            return self.state[0]
        shutdown = type("Shutdown", (), {"state": staticmethod(state)})
        for patch in (mock.patch.object(K, "cluster_shutdown", lambda: shutdown, create=True),
                      mock.patch.object(K, "FENCE_READY", True),
                      mock.patch.object(K, "fence_clock", lambda: self.now[0])):
            patch.start()
            self.addCleanup(patch.stop)
        K.forget_fence()
        self.addCleanup(K.forget_fence)

    def renew(self):
        K._ksend("PATCH", LEASE_PATH, {"spec": {"holderIdentity": "pod-a"}}, "application/merge-patch+json")

    def test_renews_inside_the_ttl_read_the_journal_once(self):
        for _ in range(20):
            self.renew()
            self.now[0] += 0.05
        self.assertEqual(1, len(self.reads))
        self.now[0] += K.FENCE_TTL
        self.renew()
        self.assertEqual(2, len(self.reads), "read again once the TTL is out")

    def test_a_shutdown_begun_elsewhere_blocks_writes_within_the_ttl(self):
        self.renew()
        self.state[0] = {"phase": "draining"}
        self.now[0] += K.FENCE_TTL
        with self.assertRaisesRegex(ValueError, "Cluster shutdown is active"):
            self.renew()

    def test_a_read_that_fails_refuses_the_write_and_is_not_kept(self):
        self.state[0] = RuntimeError("journal unreadable")
        with self.assertRaises(RuntimeError):
            self.renew()
        self.state[0] = None
        self.renew()
        self.assertEqual(2, len(self.reads), "a failure is never the remembered answer")

    def test_the_coordinators_own_journal_write_is_seen_at_once(self):
        self.renew()
        self.state[0] = {"phase": "draining"}
        K._ksend("POST", "/api/v1/namespaces/lab/secrets",
                 {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": CS.NAME, "namespace": "lab"},
                  "data": {}}, shutdown_bypass=True)
        with self.assertRaisesRegex(ValueError, "Cluster shutdown is active"):
            self.renew()


class LegacyJournalTests(unittest.TestCase):
    def test_a_missing_legacy_configmap_is_not_asked_for_on_every_read(self):
        with Kube() as kube:
            gets = []
            kube.react("GET", f"/api/v1/namespaces/lab/configmaps/{CS.NAME}", lambda m, p, b: gets.append(p))
            clock = [5000.0]
            with mock.patch.object(CS, "legacy_clock", lambda: clock[0]):
                CS.Shutdown._legacy_absent.clear()
                for _ in range(5):
                    self.assertIsNone(CS.Shutdown(K.kget, K.ksend, "lab", "pod-a", "").state())
                self.assertEqual(1, len(gets))
                clock[0] += CS.LEGACY_RECHECK
                CS.Shutdown(K.kget, K.ksend, "lab", "pod-a", "").state()
                self.assertEqual(2, len(gets), "asked again after LEGACY_RECHECK")
                kube.add({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": CS.NAME, "namespace": "lab"}})
                clock[0] += CS.LEGACY_RECHECK
                with self.assertRaisesRegex(ValueError, "Legacy shutdown ConfigMap"):
                    CS.Shutdown(K.kget, K.ksend, "lab", "pod-a", "").state()


if __name__ == "__main__":
    unittest.main()
