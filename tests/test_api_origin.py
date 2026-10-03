"""Homestead reaches the Kubernetes API without DNS, and says plainly why
it could not.

With the cluster's one CoreDNS replica on a host that was shut down, every
lookup of kubernetes.default.svc failed, and Homestead stayed down showing
"<urlopen error [Errno -3] Try again>" until that host came back."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_auth as AUTH


class ApiOriginTests(unittest.TestCase):
    def test_the_kubelet_address_is_used_rather_than_a_dns_name(self):
        self.assertEqual("https://10.43.0.1:443",
                         server.api_origin({"KUBERNETES_SERVICE_HOST": "10.43.0.1", "KUBERNETES_SERVICE_PORT": "443"}))
        self.assertEqual("https://[fd00:10:43::1]:6443",
                         server.api_origin({"KUBERNETES_SERVICE_HOST": "fd00:10:43::1", "KUBERNETES_SERVICE_PORT": "6443"}))
        self.assertEqual("https://kubernetes.default.svc", server.api_origin({}), "outside a pod")

    def test_why_the_accounts_could_not_be_read_is_said_plainly(self):
        cases = {"<urlopen error [Errno -3] Try again>": "DNS did not answer",
                 "<urlopen error [Errno -3] Temporary failure in name resolution>": "DNS did not answer",
                 "timed out": "did not answer in time",
                 "<urlopen error [Errno 111] Connection refused>": "refused the connection"}
        for error, words in cases.items():
            self.assertIn(words, AUTH.unavailable_cause(Exception(error)), error)
        self.assertEqual("something odd", AUTH.unavailable_cause(Exception("something odd")))
        with self.assertRaises(AUTH.StoreUnavailable) as raised:
            AUTH._store_cache.update(data=None, at=0)
            AUTH._unavailable(Exception("<urlopen error [Errno -3] Try again>"))
        self.assertNotIn("Errno", str(raised.exception), "the headline is plain; the cause is separate")
        self.assertIn("DNS", raised.exception.cause)


if __name__ == "__main__":
    unittest.main()
