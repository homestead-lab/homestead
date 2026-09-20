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

    def test_volume_restore_tracks_longhorn_engine_progress_until_healthy(self):
        operations.start(
            "volume-restore", "Restore restored-data",
            {"kind": "PersistentVolumeClaim", "name": "restored-data", "namespace": "lab"},
            "/volumes?q=restored-data",
            {"namespace": "lab", "name": "restored-data", "backup": "backup-123"})
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"] = {
            "spec": {"volumeName": "pv-restored"}, "status": {"phase": "Bound"}}
        self.objects["/api/v1/persistentvolumes/pv-restored"] = {
            "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": "lh-restored"}}}
        self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/lh-restored"
        ] = {"status": {"robustness": "healthy", "restoreInitiated": True,
                         "restoreRequired": True, "conditions": []}}
        engines_path = ("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines"
                        "?labelSelector=longhornvolume%3Dlh-restored")
        self.objects[engines_path] = {"items": [{"status": {
            "lastRestoredBackup": "", "restoreStatus": {"replica-a": {
                "isRestoring": True, "progress": 45, "state": "in_progress", "error": ""}}}}]}
        running = operations.list_operations()[0]
        self.assertEqual("running", running["status"])
        self.assertEqual(52, running["progress"])
        engine = self.objects[engines_path]["items"][0]["status"]
        engine["lastRestoredBackup"] = "backup-123"
        engine["restoreStatus"]["replica-a"].update(
            isRestoring=False, progress=100, state="complete")
        self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/lh-restored"
        ]["status"]["restoreRequired"] = False
        complete = operations.list_operations()[0]
        self.assertEqual("succeeded", complete["status"])
        self.assertEqual(100, complete["progress"])

    def test_volume_restore_surfaces_longhorn_scheduling_failure(self):
        operations.start(
            "volume-restore", "Restore restored-data",
            {"kind": "PersistentVolumeClaim", "name": "restored-data", "namespace": "lab"},
            "/volumes", {"namespace": "lab", "name": "restored-data"})
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/restored-data"] = {
            "spec": {"volumeName": "pv-restored"}, "status": {"phase": "Bound"}}
        self.objects["/api/v1/persistentvolumes/pv-restored"] = {
            "spec": {"csi": {"volumeHandle": "lh-restored"}}}
        self.objects[
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/lh-restored"
        ] = {"status": {"conditions": [{"type": "Scheduled", "status": "False",
                                          "message": "insufficient storage"}]}}
        failed = operations.list_operations()[0]
        self.assertEqual("failed", failed["status"])
        self.assertIn("insufficient storage", failed["message"])

    def test_smart_test_progress_is_persisted_through_shared_resolver(self):
        states = [("running", 55, "Self-test in progress"),
                  ("succeeded", 100, "Completed without error")]
        operations.bind(lambda path: self.objects[path], self.tmp.name,
                        lambda namespace, name: self.objects[(namespace, name)],
                        lambda ref: states.pop(0))
        operations.start("smart-test", "SMART short test · sda",
                         {"kind": "Disk", "name": "sda", "namespace": "node-1"},
                         "/nodes?node=node-1", {"node": "node-1", "disk": "sda"})
        self.assertEqual(55, operations.list_operations()[0]["progress"])
        self.assertEqual("succeeded", operations.list_operations()[0]["status"])

    def test_vm_disk_import_tracks_cdi_progress_and_completion(self):
        operations.start(
            "vm-disk-import", "Import VM disk router",
            {"kind": "DataVolume", "name": "router", "namespace": "lab"},
            "/import", {"namespace": "lab", "name": "router"})
        path = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/router"
        self.objects[path] = {"status": {"phase": "ImportInProgress", "progress": "48.7%"}}
        running = operations.list_operations()[0]
        self.assertEqual("running", running["status"])
        self.assertEqual(49, running["progress"])
        self.assertIn("converting", running["message"])
        self.objects[path]["status"] = {"phase": "Succeeded", "progress": "100.0%"}
        complete = operations.list_operations()[0]
        self.assertEqual("succeeded", complete["status"])
        self.assertEqual(100, complete["progress"])

    def test_vm_disk_import_surfaces_cdi_failure_detail(self):
        operations.start(
            "vm-disk-import", "Import VM disk broken",
            {"kind": "DataVolume", "name": "broken", "namespace": "lab"},
            "/import", {"namespace": "lab", "name": "broken"})
        path = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/broken"
        self.objects[path] = {"status": {"phase": "Failed", "progress": "12%",
            "conditions": [{"type": "Running", "status": "False",
                            "message": "checksum mismatch"}]}}
        failed = operations.list_operations()[0]
        self.assertEqual("failed", failed["status"])
        self.assertEqual(12, failed["progress"])
        self.assertEqual("checksum mismatch", failed["message"])


if __name__ == "__main__":
    unittest.main()
