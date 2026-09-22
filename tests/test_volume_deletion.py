import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_volumes as volumes


class VolumeDeletionTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.sent = []
        self.snapshot_rows = []
        self.backup_rows = []
        self.cache = {"vol": [1, []], "stor": [1, {}], "unrelated": [1, {}]}

        def get(path):
            if path in self.objects:
                return self.objects[path]
            if path.endswith(("/pods", "/deployments", "/statefulsets", "/daemonsets", "/jobs",
                              "/virtualmachines")):
                return {"items": []}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, ctype="application/json", timeout=15):
            self.sent.append((method, path, body, ctype))
            return {}

        volumes.bind(get, send, lambda name: self.snapshot_rows,
                     lambda name: self.backup_rows, self.cache,
                     {"kube-system", "longhorn-system"}, "lab")

    def claim(self, policy="Delete", state="detached", node="", actual=0, labels=None):
        self.objects["/api/v1/namespaces/lab/persistentvolumeclaims/media"] = {
            "metadata": {"name": "media", "namespace": "lab", "uid": "uid-1",
                         "resourceVersion": "10", "labels": labels or {}},
            "spec": {"volumeName": "pv-1", "storageClassName": "longhorn-r2",
                     "accessModes": ["ReadWriteOnce"],
                     "resources": {"requests": {"storage": "10Gi"}}},
            "status": {"phase": "Bound"},
        }
        self.objects["/api/v1/persistentvolumes/pv-1"] = {
            "metadata": {"name": "pv-1"},
            "spec": {"persistentVolumeReclaimPolicy": policy,
                     "csi": {"driver": "driver.longhorn.io", "volumeHandle": "lh-1"}},
        }
        self.objects["/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/lh-1"] = {
            "metadata": {"name": "lh-1"},
            "spec": {"numberOfReplicas": 3},
            "status": {"state": state, "currentNodeID": node,
                       "robustness": "healthy", "actualSize": actual},
        }

    def test_plan_exposes_mounts_data_protection_and_attachment_blockers(self):
        self.claim(state="attached", node="harvester-node2", actual=2 * 1024 ** 3)
        pod_spec = {
            "nodeName": "harvester-node2",
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "media"}}],
            "containers": [{"name": "app", "volumeMounts": [{"name": "data", "mountPath": "/config"}]}],
        }
        self.objects["/api/v1/namespaces/lab/pods"] = {"items": [{
            "metadata": {"name": "frigate-abc", "namespace": "lab"},
            "spec": pod_spec, "status": {"phase": "Running"},
        }]}
        self.objects["/apis/apps/v1/namespaces/lab/deployments"] = {"items": [{
            "metadata": {"name": "frigate", "namespace": "lab"},
            "spec": {"replicas": 1, "template": {"spec": pod_spec}},
        }]}
        self.snapshot_rows = [{"name": "before-upgrade"}, {"name": "daily"}]
        self.backup_rows = [{"name": "backup-1"}]

        plan = volumes.deletion_plan("lab", "media")

        self.assertTrue(plan["blocked"])
        self.assertEqual(2.0, plan["longhorn"]["actual_gb"])
        self.assertEqual(3, plan["longhorn"]["replicas"])
        self.assertEqual(2, plan["snapshots"]["count"])
        self.assertEqual(1, plan["backups"]["count"])
        self.assertEqual("/config", plan["consumers"][0]["mounts"][0]["path"])
        self.assertTrue(any("Kubernetes object" in reason for reason in plan["blocking_reasons"]))
        self.assertTrue(any("attached" in reason for reason in plan["blocking_reasons"]))

    def test_permanent_delete_sets_delete_policy_and_uses_uid_precondition(self):
        self.claim(policy="Retain", actual=1024)

        result = volumes.delete({"namespace": "lab", "name": "media", "uid": "uid-1",
                                 "action": "delete_data", "confirmation": "media"})

        self.assertEqual("delete_data", result["action"])
        self.assertEqual("Delete", self.sent[0][2]["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual("application/merge-patch+json", self.sent[0][3])
        self.assertEqual("DELETE", self.sent[1][0])
        self.assertEqual("uid-1", self.sent[1][2]["preconditions"]["uid"])
        self.assertNotIn("vol", self.cache)
        self.assertIn("unrelated", self.cache)

    def test_claim_only_delete_forces_retain_policy(self):
        self.claim(policy="Delete", actual=1024)

        result = volumes.delete({"namespace": "lab", "name": "media", "uid": "uid-1",
                                 "action": "delete_claim", "confirmation": "media"})

        self.assertEqual("delete_claim", result["action"])
        self.assertEqual("Retain", self.sent[0][2]["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual("DELETE", self.sent[1][0])

    def test_active_volume_is_refused_even_with_exact_confirmation(self):
        self.claim(state="attached", node="harvester-node1")
        with self.assertRaisesRegex(PermissionError, "deletion blocked"):
            volumes.delete({"namespace": "lab", "name": "media", "uid": "uid-1",
                            "action": "delete_data", "confirmation": "media"})
        self.assertEqual([], self.sent)

    def test_stale_preview_and_inexact_confirmation_are_refused(self):
        self.claim()
        with self.assertRaisesRegex(ValueError, "changed after preview"):
            volumes.delete({"namespace": "lab", "name": "media", "uid": "old",
                            "action": "delete_data", "confirmation": "media"})
        with self.assertRaisesRegex(ValueError, "exactly"):
            volumes.delete({"namespace": "lab", "name": "media", "uid": "uid-1",
                            "action": "delete_data", "confirmation": "Media"})

    def test_scaled_down_controller_still_blocks_breaking_its_claim_reference(self):
        self.claim()
        self.objects["/apis/apps/v1/namespaces/lab/deployments"] = {"items": [{
            "metadata": {"name": "archive", "namespace": "lab"},
            "spec": {"replicas": 0, "template": {"spec": {
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "media"}}],
                "containers": [{"name": "archive", "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
            }}},
        }]}
        plan = volumes.deletion_plan("lab", "media")
        self.assertTrue(plan["blocked"])
        self.assertFalse(plan["consumers"][0]["active"])
        self.assertTrue(any("object reference" in reason for reason in plan["blocking_reasons"]))

    def test_a_finished_import_job_does_not_block_the_volume_it_filled(self):
        """The copy job outlives the import; it must not trap the volume."""
        self.claim()
        self.objects["/apis/batch/v1/namespaces/lab/jobs"] = {"items": [{
            "metadata": {"name": "harvui-import-media", "namespace": "lab"},
            "spec": {"template": {"spec": {
                "volumes": [{"name": "appdata", "persistentVolumeClaim": {"claimName": "media"}}],
                "containers": [{"name": "copy", "volumeMounts": [{"name": "appdata", "mountPath": "/appdata"}]}],
            }}},
            "status": {"active": 0, "succeeded": 1},
        }]}

        plan = volumes.deletion_plan("lab", "media")

        self.assertFalse(plan["blocked"], "a completed job is not a reason to refuse")
        self.assertEqual(["harvui-import-media"], plan["removable_jobs"])
        self.assertEqual(1, len(plan["stale_consumers"]))

    def test_deleting_the_claim_clears_the_finished_job_first(self):
        self.claim()
        self.objects["/apis/batch/v1/namespaces/lab/jobs"] = {"items": [{
            "metadata": {"name": "harvui-import-media", "namespace": "lab"},
            "spec": {"template": {"spec": {
                "volumes": [{"name": "appdata", "persistentVolumeClaim": {"claimName": "media"}}],
                "containers": [{"name": "copy", "volumeMounts": [{"name": "appdata", "mountPath": "/appdata"}]}],
            }}},
            "status": {"active": 0, "succeeded": 1},
        }]}

        volumes.delete({"namespace": "lab", "name": "media", "uid": "uid-1",
                        "action": "delete_claim", "confirmation": "media"})

        deletes = [path for method, path, *_ in self.sent if method == "DELETE"]
        self.assertTrue(any("jobs/harvui-import-media" in path for path in deletes))
        self.assertTrue(any("persistentvolumeclaims/media" in path for path in deletes))
        self.assertLess([i for i, p in enumerate(deletes) if "jobs/" in p][0],
                        [i for i, p in enumerate(deletes) if "persistentvolumeclaims/" in p][0],
                        "the job goes before the claim, or the claim can hang in Terminating")

    def test_homestead_and_system_claims_are_hard_blocked(self):
        for app_name in ("harvui", "homestead"):
            with self.subTest(app_name=app_name):
                self.claim(labels={"app": app_name})
                plan = volumes.deletion_plan("lab", "media")
                self.assertTrue(any("Homestead" in reason for reason in plan["blocking_reasons"]))


if __name__ == "__main__":
    unittest.main()
