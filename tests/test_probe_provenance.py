import io
import json
import unittest
from unittest import mock

import test_numa_evidence as fixtures
import server


class ProbeProvenanceTests(unittest.TestCase):
    def test_source_identity_is_backend_supplied_not_response_supplied(self):
        pod = fixtures.objects()["/api/v1/namespaces/lab/pods/probe-1"]
        pod["status"]["podIP"] = "127.0.0.1"
        data = fixtures.host()["temps"]
        data["numa_source"] = {"pod": {"uid": "forged"}, "received_at": -1}
        def fetch(url, **kwargs):
            if ":9099/" in url:
                return io.BytesIO(json.dumps(data).encode())
            raise OSError("unavailable")
        with mock.patch.object(server, "_TEMP_CACHE", {"at": 0, "data": {}}), \
                mock.patch.object(server.NAMES, "nodeprobe_pods", return_value=[pod]), \
                mock.patch.object(server.urllib.request, "urlopen", side_effect=fetch), \
                mock.patch.object(server.time, "time", return_value=1000):
            result = server.node_temps()["node1"]
        self.assertEqual("probe-uid", result["numa_source"]["pod"]["uid"])
        self.assertEqual(1000, result["numa_source"]["received_at"])

    def test_oversized_or_unreadable_response_has_no_trusted_source(self):
        pod = fixtures.objects()["/api/v1/namespaces/lab/pods/probe-1"]
        pod["status"]["podIP"] = "127.0.0.1"
        for raw in (b"x" * (4 * 1024**2 + 1), b"not-json"):
            with mock.patch.object(server, "_TEMP_CACHE", {"at": 0, "data": {}}), \
                    mock.patch.object(server.NAMES, "nodeprobe_pods", return_value=[pod]), \
                    mock.patch.object(server.urllib.request, "urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(raw)):
                result = server.node_temps()["node1"]
            self.assertNotIn("numa_source", result)


if __name__ == "__main__":
    unittest.main()
