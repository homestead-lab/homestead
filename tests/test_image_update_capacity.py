import copy
import unittest
import urllib.error
from unittest import mock

import test_rollout_capacity as fixtures
import server
import homestead_updates as updates
import homestead_cancel as cancel


class ImageUpdateCapacityTests(unittest.TestCase):
    get = fixtures.RolloutCapacityTests.get

    def setUp(self):
        fixtures.RolloutCapacityTests.setUp(self)
        self.body = {"ns": "lab", "name": "shared", "action": "update", "approved": True}
        self.digest = "sha256:" + "b" * 64
        self.old_digest = "sha256:" + "a" * 64
        self.current["spec"]["template"]["spec"]["containers"][0]["image"] = "example/main@" + self.old_digest
        for patch in (mock.patch.object(updates, "kget", side_effect=self.get),
                      mock.patch.object(updates, "_check_deployment", side_effect=self.check),
                      mock.patch.object(updates, "progress", return_value={"phase": "starting"}),
                      mock.patch.object(updates, "_history")):
            patch.start()
            self.addCleanup(patch.stop)

    def check(self, *args, **kwargs):
        self.assertFalse(kwargs["persist"])
        return {"images": [{"container": "main", "available": True,
                            "source": "example/main:1", "current_digest": self.old_digest,
                            "candidate": "example/main:2", "remote_digest": self.digest}]}

    def call(self, path, body):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(body)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(updates, "ksend", side_effect=lambda method, path, dep: dep) as send, \
                mock.patch.object(server, "ksend") as other_send, \
                mock.patch.object(server.OPS, "start", return_value={}) as job:
            handler.do_POST()
        other_send.assert_not_called()
        return handler._send.call_args.args, send, job

    def reviewed(self):
        result, send, job = self.call("/api/image-updates/preview", self.body)
        self.assertEqual(200, result[0], result)
        send.assert_not_called()
        job.assert_not_called()
        return {**self.body, "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}

    def test_preview_is_pure_and_reports_exact_image_and_rollout(self):
        before = copy.deepcopy(self.current)
        result, send, _ = self.call("/api/image-updates/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertEqual("Recreate", result[1]["capacity"]["rollout"]["strategy"])
        self.assertTrue(result[1]["images"][0]["after"].endswith(self.digest))
        self.assertEqual(before, self.current)
        send.assert_not_called()
        updates._history.assert_not_called()

    def test_no_acknowledgement_never_writes(self):
        result, send, job = self.call("/api/image-updates/apply", self.body)
        self.assertEqual(409, result[0], result)
        send.assert_not_called()
        job.assert_not_called()
        updates._history.assert_not_called()

    def test_approved_put_preserves_controller_version_and_digest(self):
        result, send, job = self.call("/api/image-updates/apply", self.reviewed())
        self.assertEqual(200, result[0], result)
        dep = send.call_args.args[2]
        self.assertEqual("10", dep["metadata"]["resourceVersion"])
        self.assertTrue(dep["spec"]["template"]["spec"]["containers"][0]["image"].endswith(self.digest))
        job.assert_called_once()
        updates._history.assert_called_once()

    def test_registry_target_drift_requires_new_review(self):
        body = self.reviewed()
        self.digest = "sha256:" + "c" * 64
        result, send, job = self.call("/api/image-updates/apply", body)
        self.assertEqual(409, result[0], result)
        send.assert_not_called()
        job.assert_not_called()

    def test_changed_identity_or_version_rejects_without_writes(self):
        for field in ("uid", "resourceVersion"):
            body = self.reviewed()
            self.current["metadata"][field] += "-changed"
            result, send, _ = self.call("/api/image-updates/apply", body)
            self.assertEqual(409, result[0], result)
            send.assert_not_called()

    def test_hard_shortfall_cannot_be_overridden(self):
        body = self.reviewed()
        self.nodes[0]["allocatable"]["memory"] = "4Gi"
        result, send, _ = self.call("/api/image-updates/apply", body)
        self.assertEqual(409, result[0], result)
        send.assert_not_called()

    def test_policy_change_after_review_blocks(self):
        body = self.reviewed()
        with mock.patch.object(server, "enforce_update_policy", side_effect=PermissionError("window closed")):
            result, send, _ = self.call("/api/image-updates/apply", body)
        self.assertEqual(403, result[0])
        send.assert_not_called()

    def test_policy_is_rechecked_after_slow_preparation(self):
        body = self.reviewed()
        with mock.patch.object(server, "enforce_update_policy", side_effect=[{}, PermissionError("window closed")]), \
                mock.patch.object(updates, "ksend") as send:
            with self.assertRaisesRegex(PermissionError, "window closed"):
                server.reviewed_image_update(body, "update")
        send.assert_not_called()

    def test_rollback_uses_same_review_and_capacity_guard(self):
        self.current["metadata"]["annotations"] = {updates.PREVIOUS: server.json.dumps({
            "images": {"main": "example/main@" + self.digest}, "sources": {}})}
        self.body["action"] = "rollback"
        result, send, _ = self.call("/api/image-updates/rollback", self.body)
        self.assertEqual(409, result[0], result)
        send.assert_not_called()
        result, send, _ = self.call("/api/image-updates/rollback", self.reviewed())
        self.assertEqual(200, result[0], result)
        send.assert_called_once()

    def test_rollback_token_cannot_authorize_update(self):
        self.current["metadata"]["annotations"] = {updates.PREVIOUS: server.json.dumps({
            "images": {"main": "example/main@" + self.digest}})}
        self.body["action"] = "rollback"
        body = self.reviewed()
        body["action"] = "update"
        result, send, _ = self.call("/api/image-updates/apply", body)
        self.assertEqual(409, result[0], result)
        send.assert_not_called()

    def test_mutable_legacy_rollback_is_not_silently_followed(self):
        self.current["metadata"]["annotations"] = {updates.PREVIOUS: server.json.dumps({
            "images": {"main": "example/main:old"}})}
        self.body["action"] = "rollback"
        result, send, _ = self.call("/api/image-updates/preview", self.body)
        self.assertEqual(400, result[0], result)
        self.assertIn("mutable", result[1]["error"])
        send.assert_not_called()

    def test_job_cancellation_cannot_bypass_review(self):
        self.current["metadata"]["annotations"] = {updates.PREVIOUS: server.json.dumps({
            "images": {"main": "example/main@" + self.digest}})}
        item = {"ref": {"namespace": "lab", "name": "shared"}}
        with mock.patch.object(cancel, "kget", side_effect=self.get), mock.patch.object(updates, "ksend") as send:
            plan = cancel.image_plan(item)
            self.assertFalse(plan["can"])
            self.assertEqual({"ns": "lab", "name": "shared"}, plan["image_review"])
            with self.assertRaisesRegex(ValueError, "review"):
                cancel.image_cancel(item, {"confirm_capacity": True})
        send.assert_not_called()

    def test_expired_review_cannot_be_replayed(self):
        with mock.patch.object(server.CAPACITY_REVIEW.time, "time", return_value=1000):
            body = self.reviewed()
        with mock.patch.object(server.CAPACITY_REVIEW.time, "time", return_value=2000):
            result, send, _ = self.call("/api/image-updates/apply", body)
        self.assertEqual(409, result[0], result)
        send.assert_not_called()

    def test_new_competitor_blocks_a_previously_fitting_rollout(self):
        body = self.reviewed()
        self.pods.append({"metadata": {"name": "other", "namespace": "other"},
                          "spec": {"nodeName": "a", "containers": [{"resources": {"requests": {"memory": "4Gi"}}}]},
                          "status": {"phase": "Running"}})
        result, send, _ = self.call("/api/image-updates/apply", body)
        self.assertEqual(409, result[0], result)
        send.assert_not_called()

    def test_zero_unavailable_rolling_surge_deadlock_is_blocked(self):
        fixtures.RolloutCapacityTests.rolling(self, surge=1, unavailable=0)
        preview, _, _ = self.call("/api/image-updates/preview", self.body)
        self.assertTrue(preview[1]["capacity"]["rollout"]["start_blocked"])
        result, send, _ = self.call("/api/image-updates/apply", self.reviewed())
        self.assertEqual(409, result[0], result)
        send.assert_not_called()

    def test_single_writer_volume_prevents_rolling_overlap(self):
        self.nodes[0]["allocatable"]["memory"] = "32Gi"
        self.nodes[0]["mem_cap_gb"] = 32
        fixtures.RolloutCapacityTests.claim(self)
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"]["spec"]["accessModes"] = ["ReadWriteOncePod"]
        fixtures.RolloutCapacityTests.rolling(self, surge=1, unavailable=0)
        result, _, _ = self.call("/api/image-updates/preview", self.body)
        self.assertTrue(result[1]["capacity"]["blocked"])

    def test_put_conflict_does_not_write_history_or_start_job(self):
        body = self.reviewed()
        with mock.patch.object(updates, "ksend", side_effect=urllib.error.HTTPError("put", 409, "conflict", {}, None)), \
                mock.patch.object(server.OPS, "start") as job:
            with self.assertRaises(urllib.error.HTTPError):
                server.reviewed_image_update(body, "update")
        updates._history.assert_not_called()
        job.assert_not_called()

    def test_capacity_warning_override_is_preserved(self):
        self.nodes[0]["mem_used_gb"] = 8
        result, send, _ = self.call("/api/image-updates/apply", self.reviewed())
        self.assertEqual(200, result[0], result)
        send.assert_called_once()

    def test_init_recovery_uses_its_own_digest_not_the_app_digest(self):
        init = {"name": "permissions", "image": "example/main:1"}
        self.current["spec"]["template"]["spec"]["initContainers"] = [init]
        self.pods[0]["spec"]["initContainers"] = [copy.deepcopy(init)]
        own = "sha256:" + "c" * 64
        self.pods[0]["status"]["initContainerStatuses"] = [{"name": "permissions", "imageID": "example/main@" + own}]
        result, send, _ = self.call("/api/image-updates/preview", self.body)
        self.assertEqual(200, result[0], result)
        image = next(row for row in result[1]["images"] if row["container"] == "permissions")
        self.assertTrue(image["rollback"].endswith(own))
        send.assert_not_called()

    def test_unknown_init_image_does_not_borrow_app_observation(self):
        self.current["spec"]["template"]["spec"]["initContainers"] = [{"name": "permissions", "image": "example/main:1"}]
        result, send, _ = self.call("/api/image-updates/preview", self.body)
        self.assertEqual(400, result[0], result)
        self.assertIn("permissions", result[1]["error"])
        send.assert_not_called()

    def test_stopped_mutable_reference_is_resolved_without_writes(self):
        self.current["spec"]["replicas"] = 0
        self.pods = []
        init = {"name": "permissions", "image": "example/main:1"}
        self.current["spec"]["template"]["spec"]["initContainers"] = [init]
        with mock.patch.object(updates, "_secret_credentials", return_value={}), \
                mock.patch.object(updates, "manifest_info", return_value={"digest": self.old_digest}):
            result, send, _ = self.call("/api/image-updates/preview", self.body)
        self.assertEqual(200, result[0], result)
        self.assertIn("not claimed", " ".join(result[1]["capacity"]["warnings"]))
        self.assertTrue(result[1]["images"][0]["rollback"].endswith(self.old_digest))
        send.assert_not_called()

    def test_mixed_observed_digests_are_not_an_arbitrary_rollback(self):
        container = {"name": "main", "image": "example/main:1"}
        self.current["spec"]["template"]["spec"]["containers"][0].update(container)
        one = copy.deepcopy(self.pods[0])
        one["spec"]["containers"] = [container]
        one["status"]["containerStatuses"] = [{"name": "main", "imageID": "example/main@" + self.old_digest}]
        two = copy.deepcopy(one)
        two["status"]["containerStatuses"][0]["imageID"] = "example/main@" + self.digest
        self.pods = [one, two]
        result, send, _ = self.call("/api/image-updates/preview", self.body)
        self.assertEqual(400, result[0], result)
        self.assertIn("multiple running digests", result[1]["error"])
        send.assert_not_called()
