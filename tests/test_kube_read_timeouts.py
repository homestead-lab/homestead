"""A read of one object is given less time than a list (#984): one object is a
few kilobytes, so an API server that has not answered it in 5 s is slow or
hung, and waiting the 10 s a list may need only holds the page. A list keeps
its 10 s, and the request's deadline still bounds every read together."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server  # noqa: F401  (binds homestead_kube)
import homestead_kube as KUBE
import homestead_read_deadline as DEADLINE


class ReadTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def transport(method, path, body=None, ctype=None, timeout=15):
            self.asked.append((path, timeout))
            return {"items": []}
        self.enterContext(mock.patch.object(KUBE, "transport", transport))
        DEADLINE.end()
        self.addCleanup(DEADLINE.end)

    def in_a_request(self):
        """A request's deadline far off: what a page's reads are given."""
        clock = [100.0]
        self.enterContext(mock.patch.object(DEADLINE, "clock", lambda: clock[0]))
        DEADLINE.begin(60)
        return clock

    def timeout_for(self, path, **options):
        self.asked.clear()
        KUBE.kget(path, **options)
        return self.asked[0][1]

    def test_one_object_is_given_the_short_timeout(self):
        self.in_a_request()
        for path in ("/api/v1/namespaces/lab/configmaps/settings",
                     "/api/v1/namespaces/lab/pods/web-1?resourceVersion=0",
                     "/api/v1/nodes/node-a",
                     "/api/v1/namespaces/lab",
                     "/apis/apps/v1/namespaces/lab/deployments/web",
                     "/apis/storage.k8s.io/v1/storageclasses/fast"):
            self.assertEqual(KUBE.OBJECT_TIMEOUT, self.timeout_for(path), path)
        self.assertEqual(5, KUBE.OBJECT_TIMEOUT)

    def test_without_a_request_one_object_keeps_the_longer_timeout(self):
        # Background work (a power or host move, a VM's state) has no page
        # waiting on it, and keeps the 10 s it had.
        for path in ("/api/v1/nodes/node-a", "/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/vm"):
            self.assertEqual(10, self.timeout_for(path), path)

    def test_a_list_keeps_the_longer_timeout(self):
        self.in_a_request()
        for path in ("/api/v1/pods",
                     "/api/v1/namespaces",
                     "/api/v1/namespaces/lab/pods?labelSelector=app%3Dweb",
                     "/apis/apps/v1/namespaces/lab/deployments",
                     "/apis/metrics.k8s.io/v1beta1/nodes",
                     "/apis/storage.k8s.io/v1/storageclasses"):
            self.assertEqual(10, self.timeout_for(path), path)

    def test_anything_else_keeps_the_longer_timeout(self):
        # Subresources (a node's stats summary, a proxy), discovery and the
        # version are not known to be small: they keep the list's time.
        self.in_a_request()
        for path in ("/api/v1/nodes/node-a/proxy/stats/summary",
                     "/api/v1/namespaces/lab/pods/web-1/status",
                     "/apis/apps/v1", "/apis", "/api", "/version"):
            self.assertEqual(10, self.timeout_for(path), path)

    def test_a_timeout_the_caller_gives_is_kept(self):
        self.assertEqual(8, self.timeout_for("/api/v1/nodes/node-a", timeout=8))
        self.assertEqual(30, self.timeout_for("/api/v1/nodes/node-a", timeout=30))
        self.assertEqual(2, self.timeout_for("/api/v1/pods", timeout=2))

    def test_the_request_deadline_still_bounds_both(self):
        clock = self.in_a_request()
        clock[0] += 57
        self.assertEqual(3, self.timeout_for("/api/v1/nodes/node-a"))
        self.assertEqual(3, self.timeout_for("/api/v1/pods"))

    def test_get_optional_gets_the_short_timeout_too(self):
        self.in_a_request()
        self.asked.clear()
        KUBE.get_optional("/api/v1/namespaces/lab/configmaps/settings")
        self.assertEqual(KUBE.OBJECT_TIMEOUT, self.asked[0][1])


if __name__ == "__main__":
    unittest.main()
