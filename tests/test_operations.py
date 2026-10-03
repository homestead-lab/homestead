import json
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_operations as operations


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
            "/image-cache", {"namespace": "lab", "name": "homestead-pull-example"})
        self.assertNotIn("ref", item)
        stored = json.loads((Path(self.tmp.name) / operations.STORE).read_text())
        self.assertEqual("homestead-pull-example", stored[0]["ref"]["name"])

    def test_snapshot_cleanup_is_durable_exclusive_and_not_cancellable(self):
        item = operations.start("snapshot-delete", "Remove checkpoint", {}, "/data-protection",
                                {"volume": "vol", "uid": "snapshot-id", "phase": "request"})
        self.assertFalse(item["cancellable"])
        stored = json.loads((Path(self.tmp.name) / operations.STORE).read_text())
        self.assertEqual("request", stored[0]["ref"]["phase"])
        self.assertFalse(operations._plan_for(stored[0])["can"])
        for kind in ("snapshot-delete", "snapshot-revert"):
            with self.assertRaisesRegex(ValueError, "already active"):
                operations.start(kind, "second", {}, "/", {"volume": "vol"})

    def test_orphan_deletion_does_not_wait_on_a_recreated_same_name_claim(self):
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/old"] = {"metadata": {"uid": "replacement"}}
        result = operations._volume_delete({"ref": {"orphan": True, "namespace": "lab", "name": "old",
            "action": "delete_data", "pv": "gone-pv", "volume": "gone-volume"}})
        self.assertEqual("succeeded", result[0])

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

    def _finished(self, name, status):
        item = operations.start("image-pull", "Pull " + name, {"kind": "Image", "name": name},
                                "/image-cache", {"namespace": "lab", "name": "pull-" + name})
        stored = json.loads((Path(self.tmp.name) / operations.STORE).read_text())
        for row in stored:
            if row["id"] == item["id"]:
                row["status"] = status
                row["finished_at"] = "2026-09-22T10:00:00Z"
        (Path(self.tmp.name) / operations.STORE).write_text(json.dumps(stored))
        return item

    def test_clearing_finished_jobs_leaves_the_running_one_alone(self):
        """The tray is how you watch a running job; clearing must not hide it."""
        self._finished("one", "succeeded")
        self._finished("two", "failed")
        running = operations.start("image-pull", "Pull three", {"kind": "Image", "name": "three"},
                                   "/image-cache", {"namespace": "lab", "name": "pull-three"})
        self.objects["/apis/apps/v1/namespaces/lab/daemonsets/pull-three"] = {
            "status": {"desiredNumberScheduled": 3, "numberReady": 1, "numberUnavailable": 2}}

        result = operations.dismiss_finished()

        self.assertEqual(2, result["dismissed"])
        self.assertEqual([running["id"]], [x["id"] for x in operations.list_operations()])
        self.assertIn("still running", result["detail"])

    def test_clearing_with_nothing_finished_says_so_rather_than_nothing(self):
        operations.start("image-pull", "Pull four", {"kind": "Image", "name": "four"},
                         "/image-cache", {"namespace": "lab", "name": "pull-four"})

        result = operations.dismiss_finished()

        self.assertEqual(0, result["dismissed"])
        self.assertIn("nothing finished", result["detail"])

    def test_clearing_an_empty_tray_is_not_an_error(self):
        result = operations.dismiss_finished()

        self.assertEqual(0, result["dismissed"])
        self.assertEqual(0, result["remaining"])

    def _data_move(self, status="succeeded", retained=False, *, legacy=False):
        item = {"id": "move", "kind": "self-data-handoff", "status": status,
                "title": "Move Homestead data", "history": [{"m": "Verified completion"}],
                "ref": {"namespace": "lab", "operation": "original-dispatch",
                        "source": "original", "destination": "copy", "anchor_uid": "anchor",
                        "retain_resources": retained}}
        if legacy:
            operations.SHARED.write_json(Path(self.tmp.name) / operations.LEGACY_STORE, [item], durable=True)
        else:
            operations._write([item])
        return item

    def test_dismissing_settled_data_moves_keeps_audit_dispatch_and_volumes_after_restart_and_pruning(self):
        for status in ("succeeded", "cancelled"):
            for legacy in (False, True):
                with self.subTest(status=status, legacy=legacy):
                    # Reset both disjoint stores between cases.
                    operations._write([])
                    item = self._data_move(status, legacy=legacy)
                    audit = Path(self.tmp.name) / "self-data-completed.json"
                    audit.write_text('{"recovery":"original-volume"}', encoding="utf-8")
                    self.assertTrue(operations._public(item)["dismissible"])
                    with mock.patch.object(operations, "kget", side_effect=AssertionError("cluster access")):
                        operations.dismiss(item["id"])
                        operations.dismiss(item["id"])  # lost response can safely be retried
                    with mock.patch.object(operations, "MAX_OPERATIONS", 0):
                        operations._write(operations._read())
                    operations.bind(operations.kget, self.tmp.name, operations.deployment_progress)
                    saved = operations._read()[0]
                    self.assertEqual(item["ref"], saved["ref"])
                    self.assertEqual(item["history"], saved["history"])
                    self.assertEqual(item["history"], operations.log(item["id"])["history"])
                    self.assertTrue(saved["history_dismissed_at"])
                    self.assertEqual(legacy, bool(saved.get("_legacy_store")))
                    self.assertTrue(operations._receipt_needed(saved))
                    self.assertEqual([], operations.snapshot())
                    self.assertEqual([], operations.list_operations())
                    self.assertEqual('{"recovery":"original-volume"}', audit.read_text(encoding="utf-8"))
                    self.assertEqual(0, operations.dismiss_finished()["dismissed"])
                    self.assertEqual(1, len(operations._read()))

    def test_data_moves_with_unresolved_or_missing_holds_cannot_be_hidden(self):
        for status, retained in (("running", False), ("failed", False), ("cancelled", True),
                                 ("succeeded", True), ("cancelled", None), ("succeeded", None)):
            with self.subTest(status=status, retained=retained):
                item = self._data_move(status, retained)
                if retained is None:
                    item["ref"].pop("retain_resources")
                item["history_dismissed_at"] = "old-hidden-flag"
                operations._write([item])
                self.assertFalse(operations._public(item)["dismissible"])
                with self.assertRaises(ValueError): operations.dismiss(item["id"])
                self.assertEqual(0, operations.dismiss_finished()["dismissed"])
                self.assertEqual([item["id"]], [x["id"] for x in operations.snapshot()])

    def test_bulk_clear_counts_hidden_data_moves_once_and_keeps_other_recovery_jobs(self):
        move = self._data_move("cancelled")
        ordinary = self._finished("download", "succeeded")
        retained = {"id": "held", "kind": "backup", "status": "failed", "ref": {"retain_resources": True}}
        active = {"id": "active", "kind": "backup", "status": "running", "ref": {}}
        operations._write(operations._read() + [retained, active])
        result = operations.dismiss_finished()
        self.assertEqual(2, result["dismissed"])
        self.assertEqual(2, result["remaining"])
        self.assertEqual({move["id"], "held", "active"}, {x["id"] for x in operations._read()})
        self.assertNotIn(ordinary["id"], {x["id"] for x in operations._read()})
        again = operations.dismiss_finished()
        self.assertEqual(0, again["dismissed"])
        self.assertEqual(2, again["remaining"])

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

    def test_network_service_waits_for_vip_then_reports_ready_endpoints(self):
        operations.start(
            "network-service", "Expose pihole",
            {"kind": "Service", "name": "pihole-lan", "namespace": "lab"},
            "/networking", {"namespace": "lab", "name": "pihole-lan"})
        service_path = "/api/v1/namespaces/lab/services/pihole-lan"
        slices_path = ("/apis/discovery.k8s.io/v1/namespaces/lab/endpointslices"
                       "?labelSelector=kubernetes.io%2Fservice-name%3Dpihole-lan")
        self.objects[service_path] = {
            "spec": {"type": "LoadBalancer", "clusterIP": "10.43.0.53"},
            "status": {"loadBalancer": {}}}
        self.objects[slices_path] = {"items": [{"endpoints": [{
            "conditions": {"ready": True}}]}]}
        running = operations.list_operations()[0]
        self.assertEqual("running", running["status"])
        self.assertIn("waiting for kube-vip", running["message"])
        self.objects[service_path]["status"]["loadBalancer"]["ingress"] = [
            {"ip": "192.0.2.243"}]
        complete = operations.list_operations()[0]
        self.assertEqual("succeeded", complete["status"])
        self.assertIn("1 ready endpoint", complete["message"])


if __name__ == "__main__":
    unittest.main()
