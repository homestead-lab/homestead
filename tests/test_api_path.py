"""A name taken from a request cannot step out of the Kubernetes object it names."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class ApiPathTests(unittest.TestCase):
    def test_ordinary_paths_pass(self):
        for path in ("/api/v1/namespaces/lab/pods/frigate-7d9f8c6b5-x2abc",
                     "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/home-assistant-os",
                     "/api/v1/events?limit=160", "/api/v1/pods?labelSelector=app%3Dfrigate",
                     "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-1.2"):
            self.assertEqual(path, server.api_path(path))

    def test_a_name_that_walks_out_is_refused(self):
        for path in ("/api/v1/namespaces/lab/pods/../secrets/homestead-auth",
                     "/api/v1/namespaces/lab/pods/..", "/api/v1/namespaces/lab/pods/.",
                     "/api/v1/namespaces/lab/pods/%2e%2e/secrets", "/api/v1/namespaces/lab/pods/.%2E/secrets",
                     "/api/v1/namespaces/lab/pods/a%2Fb", "/api/v1/namespaces/lab/pods/a%5cb",
                     "/api/v1/namespaces/lab/pods/a\\b", "/api/v1/namespaces/lab/pods/a b",
                     "/api/v1/namespaces/lab/pods/a#b", "/api/v1/namespaces/lab/pods/a\r\nHost: evil"):
            with self.assertRaises(ValueError, msg=path):
                server.api_path(path)

    def test_nothing_is_asked_of_the_api_server_for_a_refused_path(self):
        with mock.patch.object(server.urllib.request, "urlopen") as opened:
            with self.assertRaises(ValueError):
                server.kget("/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/../../../../api/v1/secrets")
            with self.assertRaises(ValueError):
                server._ksend("DELETE", "/api/v1/namespaces/lab/pods/..")
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
