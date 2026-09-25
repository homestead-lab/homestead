import sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server

PVC = "/api/v1/namespaces/lab/persistentvolumeclaims/qdirstat-appdata"
LH_VOLUME = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-123"


class VolumeEditTests(unittest.TestCase):
    """Growing a volume said "404 page not found": the replica count went to
    a Longhorn path without its namespace, after the size had changed."""

    def setUp(self):
        self.saved = server.kget, server.ksend
        self.sent = []
        self.replicas = 1

        def kget(path):
            if path == PVC:
                return {"spec": {"volumeName": "pvc-123", "resources": {"requests": {"storage": "5Gi"}}}}
            if path == LH_VOLUME:
                return {"spec": {"numberOfReplicas": self.replicas}}
            raise urllib.error.HTTPError(path, 404, "page not found", None, None)

        def ksend(method, path, body=None, **kw):
            if path not in (PVC, LH_VOLUME):
                raise urllib.error.HTTPError(path, 404, "page not found", None, None)
            self.sent.append((method, path, body))
            return body

        server.kget, server.ksend = kget, ksend

    def tearDown(self):
        server.kget, server.ksend = self.saved

    def test_growing_sends_only_the_new_size(self):
        result = server.edit_volume({"namespace": "lab", "name": "qdirstat-appdata", "size_gb": 10, "replicas": 1})

        self.assertEqual([("PATCH", PVC, {"spec": {"resources": {"requests": {"storage": "10Gi"}}}})], self.sent)
        self.assertIn("growing to 10 GB", result["detail"])

    def test_the_replica_count_goes_to_longhorns_namespace(self):
        server.edit_volume({"namespace": "lab", "name": "qdirstat-appdata", "size_gb": 5, "replicas": 3})

        self.assertEqual([("PATCH", LH_VOLUME, {"spec": {"numberOfReplicas": 3}})], self.sent)

    def test_a_smaller_size_is_refused_before_anything_changes(self):
        with self.assertRaisesRegex(ValueError, "grow but not shrink"):
            server.edit_volume({"namespace": "lab", "name": "qdirstat-appdata", "size_gb": 2, "replicas": 3})
        self.assertEqual([], self.sent)

    def test_nothing_changed_sends_nothing(self):
        result = server.edit_volume({"namespace": "lab", "name": "qdirstat-appdata", "size_gb": 5, "replicas": 1})

        self.assertEqual([], self.sent)
        self.assertIn("unchanged", result["detail"])


if __name__ == "__main__":
    unittest.main()
