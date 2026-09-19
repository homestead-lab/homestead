import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import harvui_operations as operations


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.objects = {}

        def get(path):
            value = self.objects.get(path)
            if value is None:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return value

        def progress(namespace, name):
            return self.objects[(namespace, name)]

        operations.bind(get, self.tmp.name, progress)

    def tearDown(self):
        self.tmp.cleanup()

    def test_started_operation_is_persisted_without_private_ref(self):
        item = operations.start(
            "image-pull", "Pull example/image:1", {"kind": "Image", "name": "example/image:1"},
            "/image-cache", {"namespace": "lab", "name": "harvui-pull-example"})
        self.assertNotIn("ref", item)
        stored = json.loads((Path(self.tmp.name) / operations.STORE).read_text())
        self.assertEqual("harvui-pull-example", stored[0]["ref"]["name"])

    def test_image_pull_survives_reload_and_completes_from_daemonset(self):
        item = operations.start(
            "image-pull", "Pull image", {"kind": "Image", "name": "image"}, "/image-cache",
            {"namespace": "lab", "name": "pull-image"})
        path = "/apis/apps/v1/namespaces/lab/daemonsets/pull-image"
        self.objects[path] = {"status": {"desiredNumberScheduled": 3, "numberReady": 2,
                                         "numberUnavailable": 1}}
        current = operations.list_operations()[0]
        self.assertEqual("running", current["status"])
        self.assertEqual(67, current["progress"])
        self.objects[path]["status"].update(numberReady=3, numberUnavailable=0)
        complete = operations.list_operations()[0]
        self.assertEqual("succeeded", complete["status"])
        self.assertEqual(100, complete["progress"])
        self.assertTrue(complete["finished_at"])
        self.assertEqual(item["id"], complete["id"])

    def test_deployment_uses_rollout_readiness(self):
        operations.start(
            "deployment", "Deploy demo", {"kind": "Deployment", "name": "demo", "namespace": "lab"},
            "/containers", {"namespace": "lab", "name": "demo"})
        self.objects[("lab", "demo")] = {"phase": "progressing", "desired": 2, "ready": 1}
        self.assertEqual("running", operations.list_operations()[0]["status"])
        self.objects[("lab", "demo")] = {"phase": "ready", "desired": 2, "ready": 2}
        self.assertEqual("succeeded", operations.list_operations()[0]["status"])

    def test_active_operation_cannot_be_dismissed(self):
        item = operations.start(
            "image-pull", "Pull image", {"kind": "Image", "name": "image"}, "/image-cache",
            {"namespace": "lab", "name": "pull-image"})
        with self.assertRaisesRegex(ValueError, "active operation"):
            operations.dismiss(item["id"])

    def test_image_cleanup_tracks_each_node_pod(self):
        operations.start(
            "image-cleanup", "Clean image", {"kind": "Image", "name": "repo@sha256:abc"},
            "/image-cache", {"namespace": "lab", "pods": ["clean-a", "clean-b"]})
        for name in ("clean-a", "clean-b"):
            self.objects[f"/api/v1/namespaces/lab/pods/{name}"] = {
                "status": {"phase": "Running", "containerStatuses": []}}
        self.assertEqual("running", operations.list_operations()[0]["status"])
        self.objects["/api/v1/namespaces/lab/pods/clean-a"]["status"]["phase"] = "Succeeded"
        self.objects["/api/v1/namespaces/lab/pods/clean-b"]["status"]["phase"] = "Succeeded"
        complete = operations.list_operations()[0]
        self.assertEqual("succeeded", complete["status"])
        self.assertEqual(100, complete["progress"])

    def test_permanent_volume_delete_waits_for_claim_pv_and_longhorn_data(self):
        operations.start(
            "volume-delete", "Delete volume scratch",
            {"kind": "PersistentVolumeClaim", "name": "scratch", "namespace": "lab"},
            "/volumes", {"namespace": "lab", "name": "scratch", "action": "delete_data",
                         "pv": "pv-scratch", "volume": "lh-scratch"})
        pvc_path = "/api/v1/namespaces/lab/persistentvolumeclaims/scratch"
        pv_path = "/api/v1/persistentvolumes/pv-scratch"
        lh_path = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/lh-scratch"
        self.objects[pvc_path] = {"metadata": {"deletionTimestamp": "now"}}
        self.objects[pv_path] = {"metadata": {"name": "pv-scratch"}}
        self.objects[lh_path] = {"metadata": {"name": "lh-scratch"}}
        self.assertEqual(35, operations.list_operations()[0]["progress"])
        del self.objects[pvc_path]
        self.assertEqual(70, operations.list_operations()[0]["progress"])
        del self.objects[pv_path]
        self.assertEqual(90, operations.list_operations()[0]["progress"])
        del self.objects[lh_path]
        complete = operations.list_operations()[0]
        self.assertEqual("succeeded", complete["status"])
        self.assertEqual(100, complete["progress"])

    def test_claim_only_volume_delete_completes_when_claim_is_gone(self):
        operations.start(
            "volume-delete", "Delete claim scratch",
            {"kind": "PersistentVolumeClaim", "name": "scratch", "namespace": "lab"},
            "/volumes", {"namespace": "lab", "name": "scratch", "action": "delete_claim",
                         "pv": "pv-scratch", "volume": "lh-scratch"})
        complete = operations.list_operations()[0]
        self.assertEqual("succeeded", complete["status"])
        self.assertIn("retained", complete["message"])


if __name__ == "__main__":
    unittest.main()
