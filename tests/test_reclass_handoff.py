"""Integration tests for the staged replacement of the legacy reclass engine."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_reclass as rc
import homestead_storage_journal as journal
import homestead_reclass_handoff as handoff


class Crash(BaseException):
    pass


class Cluster:
    def __init__(self):
        self.objects, self.sent, self.saves = {}, [], []
        self.crash_after = self.lose_reply = None
        self.item = {"id": "handoff", "kind": "reclass", "status": "running",
                     "ref": {"namespace": "lab", "claim": "data", "consumers": [], "review_fences": {}}}
        self.durable = copy.deepcopy(self.item)

    def add(self, version, plural, name, kind, spec):
        prefix = "/api/" if version == "v1" else "/apis/"
        path = f"{prefix}{version}/namespaces/lab/{plural}/{name}"
        obj = {"apiVersion": version, "kind": kind,
               "metadata": {"name": name, "namespace": "lab", "uid": name + "-uid", "resourceVersion": "1"},
               "spec": spec}
        if kind in ("Deployment", "StatefulSet", "ReplicaSet"):
            obj["metadata"]["generation"] = 1
            obj["status"] = {"observedGeneration": 1, "replicas": spec.get("replicas", 1)}
        self.objects[path] = obj
        self.item["ref"]["review_fences"][path] = journal.identity(obj)
        return path

    def consumer(self, kind, name, **kwargs):
        self.item["ref"]["consumers"].append({"kind": kind, "name": name, **kwargs})

    def read(self, path):
        if path in ("/api/v1/namespaces/lab/pods", "/api/v1/pods"):
            return {"items": [copy.deepcopy(obj) for obj in self.objects.values() if obj["kind"] == "Pod"]}
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.objects[path])

    @staticmethod
    def merge(obj, patch):
        for key, value in patch.items():
            if value is None:
                obj.pop(key, None)
            elif isinstance(value, dict):
                Cluster.merge(obj.setdefault(key, {}), value)
            else:
                obj[key] = copy.deepcopy(value)

    def send(self, method, path, body, **kwargs):
        entry = self.durable["ref"]["storage_writes"][-1]
        assert entry["state"] == "intent" and entry["target"]["path"] == path
        self.sent.append((method, path, copy.deepcopy(body)))
        current = self.objects[path]
        before = journal.identity(current)
        if method == "DELETE":
            assert body["preconditions"] == before
            del self.objects[path]
            result = {"apiVersion": "v1", "kind": "Status", "status": "Success", "details": {"uid": before["uid"]}}
        else:
            assert journal.identity(body) == before
            if method == "PUT":
                current = self.objects[path] = copy.deepcopy(body)
            else:
                self.merge(current, body)
            current["metadata"]["resourceVersion"] = str(int(before["resourceVersion"]) + 1)
            if current["kind"] in ("Deployment", "StatefulSet", "ReplicaSet"):
                # This fake controller observes stops immediately by default.
                # Quiescence tests override status to simulate reconciliation lag.
                current["status"] = {"observedGeneration": current["metadata"]["generation"], "replicas": current["spec"].get("replicas", 1)}
            result = copy.deepcopy(current)
        if len(self.sent) == self.lose_reply:
            raise TimeoutError("connection lost")
        return result

    def checkpoint(self, item):
        self.durable = copy.deepcopy(item)
        self.saves.append(self.durable)
        if self.crash_after == len(self.saves):
            raise Crash()

    def stop(self, restored=False):
        if restored:
            self.item = copy.deepcopy(self.durable)
        with mock.patch.object(rc, "kget", self.read), mock.patch.object(rc, "ksend", self.send):
            rc.journaled_stop(self.item, self.checkpoint)


class StopHandoffTests(unittest.TestCase):
    def fleet(self):
        cluster = Cluster()
        for kind, plural, name in (("Deployment", "deployments", "app"), ("StatefulSet", "statefulsets", "db")):
            cluster.add("apps/v1", plural, name, kind, {"replicas": 1})
            cluster.consumer(kind, name, replicas=1)
        cluster.add("batch/v1", "cronjobs", "backup", "CronJob", {"suspend": False})
        cluster.consumer("CronJob", "backup", suspend=False)
        return cluster

    def test_stops_each_controller_with_exact_uid_and_version(self):
        cluster = self.fleet(); cluster.stop()
        self.assertEqual(3, len(cluster.sent))
        self.assertTrue(all(e["state"] == "accepted" for e in cluster.durable["ref"]["storage_writes"]))
        self.assertTrue(all(c["stopped"] for c in cluster.item["ref"]["consumers"]))
        cluster.stop(restored=True)
        self.assertEqual(3, len(cluster.sent))

    def test_dangerous_retention_blocks_all_stops_not_only_the_statefulset(self):
        cluster = self.fleet()
        cluster.objects["/apis/apps/v1/namespaces/lab/statefulsets/db"]["spec"]["persistentVolumeClaimRetentionPolicy"] = {"whenScaled": "Delete"}
        with self.assertRaisesRegex(journal.Held, "retention policy"): cluster.stop()
        self.assertEqual([], cluster.sent)
        self.assertEqual([], cluster.saves)

    def test_restart_between_receipt_and_next_controller_observes_without_replay(self):
        cluster = self.fleet(); cluster.crash_after = 2  # first API response durably saved, before stopped flag
        with self.assertRaises(Crash): cluster.stop()
        self.assertEqual(1, len(cluster.sent))
        cluster.crash_after = None; cluster.stop(restored=True)
        self.assertEqual(3, len(cluster.sent))
        self.assertEqual(1, sum(path.endswith("/app") for _, path, _ in cluster.sent))

    def test_restart_after_intent_never_sends_that_request(self):
        cluster = self.fleet(); cluster.crash_after = 1
        with self.assertRaises(Crash): cluster.stop()
        cluster.crash_after = None
        with self.assertRaises(journal.Held): cluster.stop(restored=True)
        self.assertEqual([], cluster.sent)

    def test_second_stop_loses_reply_and_third_is_never_sent(self):
        cluster = self.fleet(); cluster.lose_reply = 2
        with self.assertRaises(journal.Held): cluster.stop()
        with self.assertRaises(journal.Held): cluster.stop(restored=True)
        self.assertEqual(2, len(cluster.sent))
        self.assertFalse(cluster.objects["/apis/batch/v1/namespaces/lab/cronjobs/backup"]["spec"]["suspend"])

    def test_already_stopped_boolean_cannot_hide_a_replacement(self):
        cluster = self.fleet(); cluster.stop()
        cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]["metadata"]["uid"] = "replacement"
        with self.assertRaises(journal.Held): cluster.stop(restored=True)
        self.assertEqual(3, len(cluster.sent))

    def test_vm_datavolume_conversion_pins_every_write_and_orphans_only_reviewed_uid(self):
        cluster = Cluster()
        vm = cluster.add("kubevirt.io/v1", "virtualmachines", "guest", "VirtualMachine", {
            "running": True, "template": {"spec": {"volumes": [{"name": "disk", "dataVolume": {"name": "data"}}]}},
            "dataVolumeTemplates": [{"metadata": {"name": "data"}}]})
        pvc = cluster.add("v1", "persistentvolumeclaims", "data", "PersistentVolumeClaim", {"volumeName": "pv"})
        cluster.objects[pvc]["metadata"]["ownerReferences"] = [{"kind": "DataVolume", "uid": "data-uid"}]
        dv = cluster.add("cdi.kubevirt.io/v1beta1", "datavolumes", "data", "DataVolume", {})
        cluster.consumer("VirtualMachine", "guest", via="dv", run_strategy="Always")
        cluster.stop()
        self.assertEqual("Halted", cluster.objects[vm]["spec"]["runStrategy"])
        self.assertEqual({"claimName": "data"}, cluster.objects[vm]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"])
        self.assertNotIn(dv, cluster.objects)
        self.assertIn(pvc, cluster.objects)
        self.assertEqual("Orphan", cluster.sent[-1][2]["propagationPolicy"])
        self.assertEqual("data-uid", cluster.sent[-1][2]["preconditions"]["uid"])
        cluster.stop(restored=True)
        self.assertEqual(3, len(cluster.sent))

    def test_unreviewed_datavolume_is_never_deleted(self):
        cluster = Cluster()
        cluster.add("kubevirt.io/v1", "virtualmachines", "guest", "VirtualMachine", {
            "runStrategy": "Always", "template": {"spec": {"volumes": [{"name": "disk", "dataVolume": {"name": "data"}}]}}})
        cluster.add("v1", "persistentvolumeclaims", "data", "PersistentVolumeClaim", {})
        cluster.consumer("VirtualMachine", "guest", via="dv", run_strategy="Always")
        with self.assertRaises(journal.Held): cluster.stop()
        self.assertFalse(any(method == "DELETE" for method, _, _ in cluster.sent))


class CopyCluster(Cluster):
    def __init__(self):
        super().__init__()
        ref = self.item["ref"]
        ref.update(temp="data-copy", from_class="old", target="new", old_pv="pv-data", mode="Filesystem", size="10Gi",
                   claim_spec={"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem"}, job_name="data-copy-job")
        self.add("v1", "persistentvolumeclaims", "data", "PersistentVolumeClaim", {
            **ref["claim_spec"], "storageClassName": "old", "volumeName": "pv-data", "resources": {"requests": {"storage": "10Gi"}}})
        self.bind_volume("data", "pv-data")
        ref["review_fences"]["/api/v1/persistentvolumes/pv-data"] = journal.identity(self.objects["/api/v1/persistentvolumes/pv-data"])
        self.add("apps/v1", "deployments", "app", "Deployment", {"replicas": 1,
            "template": {"spec": {"containers": [{"name": "app", "image": "example/app:1",
                "resources": {"requests": {"memory": "1Gi"}, "limits": {"memory": "1Gi"}}}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}})
        self.consumer("Deployment", "app", replicas=1)
        self.stop()

    def bind_volume(self, name, volume):
        pvc = self.objects[f"/api/v1/namespaces/lab/persistentvolumeclaims/{name}"]
        pvc["spec"]["volumeName"] = volume
        pvc["status"] = {"phase": "Bound"}
        self.objects[f"/api/v1/persistentvolumes/{volume}"] = {
            "apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": volume, "uid": volume + "-uid", "resourceVersion": "1"},
            "spec": {"claimRef": {"name": name, "namespace": "lab", "uid": pvc["metadata"]["uid"]}, "persistentVolumeReclaimPolicy": "Delete"}}

    def read(self, path):
        if path.endswith("/pods"):
            return super().read(path)
        if path.endswith(("/deployments", "/statefulsets", "/daemonsets", "/cronjobs", "/virtualmachines", "/replicasets", "/jobs")):
            return {"items": [copy.deepcopy(obj) for key, obj in self.objects.items() if key.rsplit("/", 1)[0] == path]}
        return super().read(path)

    def send(self, method, path, body, **kwargs):
        if method != "POST":
            if method == "DELETE" and "/jobs/" in path:
                uid = self.objects[path]["metadata"]["uid"]
                for pod_path, pod in list(self.objects.items()):
                    if pod["kind"] == "Pod" and handoff._owned(pod, uid):
                        del self.objects[pod_path]
            return super().send(method, path, body, **kwargs)
        event = self.durable["ref"]["storage_writes"][-1]
        assert event["state"] == "intent"
        self.sent.append((method, path, copy.deepcopy(body)))
        obj = copy.deepcopy(body)
        obj["metadata"].update(uid=obj["metadata"]["name"] + ("-replacement-uid" if obj["metadata"]["name"] == "data" else "-uid"), resourceVersion="1")
        self.objects[path + "/" + obj["metadata"]["name"]] = obj
        if obj["kind"] == "PersistentVolumeClaim":
            if obj["spec"].get("volumeName"):
                pv = self.objects["/api/v1/persistentvolumes/" + obj["spec"]["volumeName"]]
                pv["spec"]["claimRef"] = {"name": obj["metadata"]["name"], "namespace": "lab", "uid": obj["metadata"]["uid"]}
                obj["status"] = {"phase": "Bound"}
            else:
                self.bind_volume(obj["metadata"]["name"], "pv-copy")
        if len(self.sent) == self.lose_reply:
            raise TimeoutError("lost after creation")
        return copy.deepcopy(obj)

    def stage(self, admit=None, log="==> verified\n", restored=False):
        if restored:
            self.item = copy.deepcopy(self.durable)
        with mock.patch.object(rc, "kget", self.read), mock.patch.object(rc, "ksend", self.send), mock.patch.object(rc, "ktext", return_value=log):
            return handoff.copy_stage(self.item, self.checkpoint, admit or (lambda obj: {"blocked": False}))

    def complete(self):
        job = self.objects["/apis/batch/v1/namespaces/lab/jobs/data-copy-job"]
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        self.add("v1", "pods", "copy-pod", "Pod", copy.deepcopy(job["spec"]["template"]["spec"]))
        pod = self.objects["/api/v1/namespaces/lab/pods/copy-pod"]
        pod["metadata"]["ownerReferences"] = [{"kind": "Job", "controller": True, "uid": job["metadata"]["uid"]}]
        pod["status"] = {"phase": "Succeeded"}

    def ready_cutover(self):
        self.stage(); self.stage(); self.complete(); self.stage()
        path = "/apis/storage.k8s.io/v1/storageclasses/new"
        self.objects[path] = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": "new", "uid": "class-new", "resourceVersion": "1"}, "reclaimPolicy": "Delete"}
        self.item["ref"]["review_fences"][path] = journal.identity(self.objects[path])
        self.checkpoint(self.item)

    def cutover(self, admit=None, restored=False):
        if restored:
            self.item = copy.deepcopy(self.durable)
        with mock.patch.object(rc, "kget", self.read), mock.patch.object(rc, "ksend", self.send), mock.patch.object(rc, "ktext", return_value="==> verified\n"):
            result = handoff.cutover_stage(self.item, self.checkpoint, admit or (lambda proposals: {"blocked": False}))
            self.checkpoint(self.item)  # normal operations-poll persistence
            return result

    def restart(self, admit=None, restored=False):
        if restored:
            self.item = copy.deepcopy(self.durable)
        with mock.patch.object(rc, "kget", self.read), mock.patch.object(rc, "ksend", self.send):
            result = handoff.restart_stage(self.item, self.checkpoint, admit or (lambda proposals: {"blocked": False}))
            self.checkpoint(self.item)
            return result


class CopyHandoffTests(unittest.TestCase):
    def test_copy_progress_comes_from_verified_owned_pod_without_claiming_completion(self):
        cluster = CopyCluster(); cluster.stage(); cluster.stage(); cluster.complete()
        job = cluster.objects["/apis/batch/v1/namespaces/lab/jobs/data-copy-job"]
        job["status"] = {"active": 1}
        pod = cluster.objects["/api/v1/namespaces/lab/pods/copy-pod"]
        pod["status"]["phase"] = "Running"
        outcome = cluster.stage(log="==> copying\n  1,234,567  57%  11.83MB/s  0:00:04\n")
        self.assertEqual(43, outcome[1])
        self.assertEqual(57, cluster.item["copy"]["percent"])
        self.assertIn("11.83MB/s", outcome[2])
        self.assertNotIn("copy_verified", cluster.item["ref"])
        self.assertEqual(70, cluster.stage(log="==> verifying\n")[1])
        self.assertNotIn("copy_verified", cluster.item["ref"])

    def test_unavailable_copy_logs_show_unknown_progress_not_zero_percent(self):
        cluster = CopyCluster(); cluster.stage(); cluster.stage()
        outcome = cluster.stage(log="")
        self.assertIsNone(cluster.item["copy"]["percent"])
        self.assertTrue(cluster.item["copy"]["unavailable"])
        self.assertIn("Waiting for the copy pod", outcome[2])

    def test_copy_progress_rejects_pod_replacement_during_log_read(self):
        cluster = CopyCluster(); cluster.stage(); cluster.stage(); cluster.complete()
        cluster.objects["/apis/batch/v1/namespaces/lab/jobs/data-copy-job"]["status"] = {"active": 1}
        cluster.objects["/api/v1/namespaces/lab/pods/copy-pod"]["status"]["phase"] = "Running"
        def replaced(_):
            cluster.objects["/api/v1/namespaces/lab/pods/copy-pod"]["metadata"]["uid"] = "replacement"
            return "1,234 99% 10MB/s\n"
        with mock.patch.object(rc, "kget", cluster.read), mock.patch.object(rc, "ksend", cluster.send), mock.patch.object(rc, "ktext", side_effect=replaced):
            with self.assertRaises(journal.Held): handoff.copy_stage(cluster.item, cluster.checkpoint, lambda _: {"blocked": False})

    def test_scale_down_must_be_observed_even_when_no_pods_exist(self):
        cluster = CopyCluster()
        obj = cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]
        obj["status"]["observedGeneration"] = 0
        before = len(cluster.sent)
        self.assertEqual(8, cluster.stage()[1])
        self.assertEqual(before, len(cluster.sent))
        obj["status"]["observedGeneration"] = obj["metadata"]["generation"]
        self.assertEqual(12, cluster.stage()[1])

    def test_old_replicaset_must_finish_scaling_before_copy_helper(self):
        cluster = CopyCluster()
        path = cluster.add("apps/v1", "replicasets", "app-old", "ReplicaSet", {"replicas": 1})
        child = cluster.objects[path]
        child["metadata"]["ownerReferences"] = [{"kind": "Deployment", "uid": "app-uid", "controller": True}]
        before = len(cluster.sent)
        self.assertEqual(8, cluster.stage()[1])
        child["spec"]["replicas"] = 0
        self.assertEqual(8, cluster.stage()[1])
        self.assertEqual(before, len(cluster.sent))
        child["status"]["replicas"] = 0
        self.assertEqual(12, cluster.stage()[1])

    def test_suspended_cronjob_with_active_job_cannot_write_after_copy_starts(self):
        cluster = CopyCluster()
        cluster.add("batch/v1", "cronjobs", "backup", "CronJob", {"suspend": False,
            "jobTemplate": {"spec": {"template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}}}})
        cluster.consumer("CronJob", "backup", suspend=False)
        cluster.stop()
        path = cluster.add("batch/v1", "jobs", "backup-old", "Job", {})
        cluster.objects[path]["metadata"]["ownerReferences"] = [{"kind": "CronJob", "uid": "backup-uid", "controller": True}]
        before = len(cluster.sent)
        self.assertEqual(8, cluster.stage()[1])
        self.assertEqual(before, len(cluster.sent))
        cluster.objects[path]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        self.assertEqual(12, cluster.stage()[1])

    def test_halted_vm_with_instance_still_present_is_not_quiescent(self):
        cluster = CopyCluster()
        cluster.add("kubevirt.io/v1", "virtualmachines", "guest", "VirtualMachine", {
            "runStrategy": "Always", "template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}})
        cluster.consumer("VirtualMachine", "guest", via="pvc", run_strategy="Always")
        cluster.stop()
        path = cluster.add("kubevirt.io/v1", "virtualmachineinstances", "guest", "VirtualMachineInstance", {})
        before = len(cluster.sent)
        self.assertEqual(8, cluster.stage()[1])
        self.assertEqual(before, len(cluster.sent))
        del cluster.objects[path]
        self.assertEqual(12, cluster.stage()[1])

    def test_complete_copy_requires_job_receipt_bound_claims_and_verified_log(self):
        cluster = CopyCluster()
        self.assertEqual(12, cluster.stage()[1])
        self.assertEqual(20, cluster.stage()[1])
        self.assertEqual(35, cluster.stage()[1])
        cluster.complete()
        self.assertEqual(75, cluster.stage()[1])
        proof = cluster.durable["ref"]["copy_verified"]
        self.assertEqual("data-copy-job-uid", proof["job_uid"])
        self.assertEqual("pv-data-uid", proof["claims"]["data"]["pv_uid"])
        self.assertEqual("pv-copy-uid", proof["claims"]["data-copy"]["pv_uid"])
        self.assertEqual(3, len(cluster.sent))  # stop, new PVC, copy Job; no deletion/restart

    def test_resume_after_created_claim_receipt_never_posts_it_again(self):
        cluster = CopyCluster(); cluster.stage()
        cluster.stage(restored=True)
        self.assertEqual(1, sum(method == "POST" and path.endswith("persistentvolumeclaims") for method, path, _ in cluster.sent))

    def test_copy_post_lost_response_blocks_even_when_same_name_job_succeeded(self):
        cluster = CopyCluster(); cluster.stage(); cluster.lose_reply = len(cluster.sent) + 1
        with self.assertRaises(journal.Held): cluster.stage()
        cluster.complete()
        with self.assertRaises(journal.Held): cluster.stage(restored=True)
        self.assertEqual(3, len(cluster.sent))
        self.assertNotIn("copy_verified", cluster.item["ref"])

    def test_helper_admission_failure_retains_new_volume_without_creating_job(self):
        cluster = CopyCluster(); cluster.stage()
        with self.assertRaises(journal.Held): cluster.stage(lambda obj: {"blocked": True})
        self.assertEqual(2, len(cluster.sent))
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/data-copy", cluster.objects)

    def test_pod_appearing_during_admission_prevents_job_creation(self):
        cluster = CopyCluster(); cluster.stage()
        def admit(_):
            cluster.add("v1", "pods", "other", "Pod", {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]})
            return {"blocked": False}
        with self.assertRaises(journal.Held): cluster.stage(admit)
        self.assertEqual(2, len(cluster.sent))

    def test_replaced_job_is_not_trusted_even_with_successful_log(self):
        cluster = CopyCluster(); cluster.stage(); cluster.stage(); cluster.complete()
        cluster.objects["/apis/batch/v1/namespaces/lab/jobs/data-copy-job"]["metadata"]["uid"] = "other"
        with self.assertRaises(journal.Held): cluster.stage()
        self.assertNotIn("copy_verified", cluster.item["ref"])

    def test_missing_checksum_proof_or_active_copy_pod_prevents_cutover(self):
        cluster = CopyCluster(); cluster.stage(); cluster.stage(); cluster.complete()
        with self.assertRaises(journal.Held): cluster.stage(log="copy exited zero\n")
        cluster.objects["/api/v1/namespaces/lab/pods/copy-pod"]["status"]["phase"] = "Running"
        self.assertEqual(35, cluster.stage()[1])
        self.assertNotIn("copy_verified", cluster.item["ref"])

    def test_claim_rebind_or_new_consumer_blocks_handoff(self):
        cluster = CopyCluster(); cluster.stage(); cluster.stage(); cluster.complete(); cluster.stage()
        cluster.objects["/api/v1/persistentvolumes/pv-copy"]["metadata"]["uid"] = "replaced"
        with self.assertRaises(journal.Held): cluster.stage()
        cluster.objects["/api/v1/persistentvolumes/pv-copy"]["metadata"]["uid"] = "pv-copy-uid"
        cluster.add("apps/v1", "deployments", "new-app", "Deployment", {"replicas": 0,
            "template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}})
        with self.assertRaises(journal.Held): cluster.stage()
        self.assertEqual(3, len(cluster.sent))

    def test_missing_source_pv_review_never_creates_a_copy(self):
        cluster = CopyCluster(); cluster.item["ref"]["review_fences"].pop("/api/v1/persistentvolumes/pv-data")
        with self.assertRaises(journal.Held): cluster.stage()
        self.assertEqual(1, len(cluster.sent))


class CutoverTests(unittest.TestCase):
    def ready(self):
        cluster = CopyCluster(); cluster.ready_cutover()
        return cluster

    def finish(self, cluster):
        for _ in range(20):
            if cluster.cutover(restored=True)[1] == 91:
                return
        self.fail("Cutover never completed")

    def test_each_poll_and_restart_performs_at_most_one_new_write(self):
        cluster = self.ready()
        for _ in range(20):
            count = len(cluster.sent)
            result = cluster.cutover(restored=True)
            self.assertLessEqual(len(cluster.sent) - count, 1)
            if result[1] == 91:
                break
        self.assertTrue(cluster.item["ref"]["cutover_complete"])
        old = cluster.objects["/api/v1/persistentvolumes/pv-data"]
        new = cluster.objects["/api/v1/persistentvolumes/pv-copy"]
        self.assertEqual("Retain", old["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual("Delete", new["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual("lab/data", old["metadata"]["annotations"][rc.OLD_COPY])
        self.assertEqual("data-replacement-uid", new["spec"]["claimRef"]["uid"])
        self.assertEqual(0, cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]["spec"]["replicas"])
        deletions = [path for method, path, _ in cluster.sent if method == "DELETE"]
        self.assertFalse(any("persistentvolumes/" in path for path in deletions))
        reserve = next(body for method, path, body in cluster.sent if method == "PATCH" and body.get("spec", {}).get("claimRef"))
        self.assertEqual("data", reserve["spec"]["claimRef"]["name"])
        self.assertIsNotNone(reserve["spec"]["claimRef"])

    def test_capacity_block_prevents_all_cutover_writes(self):
        cluster = self.ready(); count = len(cluster.sent)
        with self.assertRaises(journal.Held): cluster.cutover(lambda proposals: {"blocked": True})
        self.assertEqual(count, len(cluster.sent))
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/data", cluster.objects)


class RestartTests(unittest.TestCase):
    def ready(self, second=False):
        cluster = CopyCluster()
        if second:
            cluster.add("apps/v1", "deployments", "second", "Deployment", {"replicas": 1,
                "template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}})
            cluster.consumer("Deployment", "second", replicas=1)
            cluster.stop()
        cluster.ready_cutover()
        CutoverTests().finish(cluster)
        return cluster

    def test_restart_has_fresh_admission_and_readiness_not_just_accepted_write(self):
        cluster = self.ready(); seen = []
        def admit(proposals):
            seen.extend(proposals)
            self.assertEqual(1, proposals[0]["object"]["spec"]["replicas"])
            self.assertNotIn("homestead.io/storage-copy-job", proposals[0]["object"]["metadata"]["annotations"])
            return {"blocked": False}
        self.assertEqual(93, cluster.restart(admit)[1])
        count = len(cluster.sent)
        self.assertEqual(96, cluster.restart(restored=True)[1])
        self.assertEqual(count, len(cluster.sent))
        dep = cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]
        dep["status"] = {"observedGeneration": 1, "replicas": 1, "readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1}
        self.assertEqual("succeeded", cluster.restart()[0])
        self.assertFalse(cluster.item["ref"]["retain_resources"])
        self.assertEqual(1, len(seen))

    def test_partial_restart_capacity_failure_does_not_roll_back_started_workload(self):
        cluster = self.ready(second=True)
        cluster.restart()
        count = len(cluster.sent)
        def deny(proposals):
            self.assertEqual(["second"], [p["object"]["metadata"]["name"] for p in proposals])
            return {"blocked": True}
        with self.assertRaises(journal.Held): cluster.restart(deny, restored=True)
        self.assertEqual(count, len(cluster.sent))
        self.assertEqual(1, cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]["spec"]["replicas"])
        self.assertEqual(0, cluster.objects["/apis/apps/v1/namespaces/lab/deployments/second"]["spec"]["replicas"])

    def test_lost_restart_reply_never_repeats_or_starts_next_controller(self):
        cluster = self.ready(second=True); cluster.lose_reply = len(cluster.sent) + 1
        with self.assertRaises(journal.Held): cluster.restart()
        count = len(cluster.sent)
        with self.assertRaises(journal.Held): cluster.restart(restored=True)
        self.assertEqual(count, len(cluster.sent))
        self.assertEqual(0, cluster.objects["/apis/apps/v1/namespaces/lab/deployments/second"]["spec"]["replicas"])

    def test_changed_binding_during_admission_blocks_restart(self):
        cluster = self.ready(); count = len(cluster.sent)
        def change(_):
            cluster.objects["/api/v1/persistentvolumes/pv-copy"]["spec"]["claimRef"]["uid"] = "other"
            return {"blocked": False}
        with self.assertRaises(journal.Held): cluster.restart(change)
        self.assertEqual(count, len(cluster.sent))

    def test_unowned_pod_is_not_mistaken_for_a_previously_restarted_workload(self):
        cluster = self.ready(second=True); cluster.restart()
        cluster.add("v1", "pods", "rogue", "Pod", {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]})
        cluster.objects["/api/v1/namespaces/lab/pods/rogue"]["status"] = {"phase": "Running"}
        count = len(cluster.sent)
        with self.assertRaises(journal.Held): cluster.restart()
        self.assertEqual(count, len(cluster.sent))

    def test_expected_replicaset_pod_is_allowed_without_trusting_labels(self):
        cluster = self.ready(second=True); cluster.restart()
        rs_path = cluster.add("apps/v1", "replicasets", "app-rs", "ReplicaSet", {})
        cluster.objects[rs_path]["metadata"]["ownerReferences"] = [{"kind": "Deployment", "name": "app", "uid": "app-uid", "controller": True}]
        pod_path = cluster.add("v1", "pods", "app-pod", "Pod", {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]})
        cluster.objects[pod_path]["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "app-rs", "uid": "app-rs-uid", "controller": True}]
        cluster.objects[pod_path]["status"] = {"phase": "Running"}
        self.assertEqual(93, cluster.restart()[1])


class CutoverFaultTests(unittest.TestCase):
    ready = CutoverTests.ready
    finish = CutoverTests.finish

    def test_lost_response_at_every_cutover_write_prevents_replay_or_restart(self):
        baseline = self.ready(); initial = len(baseline.sent); self.finish(baseline)
        count = len(baseline.sent) - initial
        self.assertGreater(count, 8)
        for offset in range(1, count + 1):
            with self.subTest(write=offset):
                cluster = self.ready(); cluster.lose_reply = len(cluster.sent) + offset
                with self.assertRaises(journal.Held): self.finish(cluster)
                sent = len(cluster.sent)
                with self.assertRaises(journal.Held): cluster.cutover(restored=True)
                self.assertEqual(sent, len(cluster.sent))
                self.assertEqual(0, cluster.objects["/apis/apps/v1/namespaces/lab/deployments/app"]["spec"]["replicas"])

    def test_crash_after_each_accepted_write_advances_without_resending(self):
        baseline = self.ready(); self.finish(baseline)
        expected = len(baseline.sent)
        for index in range(1, 10):
            with self.subTest(step=index):
                cluster = self.ready()
                for _ in range(index - 1): cluster.cutover()
                # First stage rechecks copy proof, then checkpoints cutover state.
                cluster.crash_after = len(cluster.saves) + (4 if index == 1 else 2)
                with self.assertRaises(Crash): cluster.cutover()
                cluster.crash_after = None; self.finish(cluster)
                self.assertEqual(expected, len(cluster.sent))

    def test_protection_removed_or_claim_replaced_stops_before_deleting(self):
        cluster = self.ready(); cluster.cutover(); cluster.cutover()
        count = len(cluster.sent)
        cluster.objects["/api/v1/persistentvolumes/pv-data"]["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
        with self.assertRaises(journal.Held): cluster.cutover()
        self.assertEqual(count, len(cluster.sent))
        cluster.objects["/api/v1/persistentvolumes/pv-data"]["spec"]["persistentVolumeReclaimPolicy"] = "Retain"
        cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data-copy"]["metadata"]["uid"] = "replacement"
        with self.assertRaises(journal.Held): cluster.cutover()
        self.assertEqual(count, len(cluster.sent))

    def test_accepted_claim_delete_waits_for_finalizers_without_force_or_resend(self):
        cluster = self.ready()
        path = "/api/v1/namespaces/lab/persistentvolumeclaims/data-copy"
        retained = copy.deepcopy(cluster.objects[path])
        for _ in range(10):
            cluster.cutover()
            if handoff._receipt(cluster.item, "delete-temporary"):
                break
        self.assertIsNotNone(handoff._receipt(cluster.item, "delete-temporary"))
        retained["metadata"]["deletionTimestamp"] = "2026-09-27T12:00:00Z"
        retained["metadata"]["finalizers"] = ["kubernetes.io/pvc-protection"]
        cluster.objects[path] = retained
        count = len(cluster.sent)
        for _ in range(3):
            self.assertEqual(84, cluster.cutover(restored=True)[1])
        self.assertEqual(count, len(cluster.sent))
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/data", cluster.objects)

    def test_both_data_volumes_remain_retained_while_original_name_is_absent(self):
        cluster = self.ready()
        for _ in range(10):
            cluster.cutover()
            if handoff._receipt(cluster.item, "delete-source"):
                break
        self.assertNotIn("/api/v1/namespaces/lab/persistentvolumeclaims/data", cluster.objects)
        for pv in ("pv-data", "pv-copy"):
            self.assertEqual("Retain", cluster.objects["/api/v1/persistentvolumes/" + pv]["spec"]["persistentVolumeReclaimPolicy"])
        count = len(cluster.sent)
        cluster.objects["/api/v1/persistentvolumes/pv-copy"]["metadata"]["uid"] = "replacement"
        with self.assertRaises(journal.Held): cluster.cutover(restored=True)
        self.assertEqual(count, len(cluster.sent))

    def test_changed_replacement_configuration_cannot_restore_delete_policy(self):
        cluster = self.ready()
        for _ in range(15):
            cluster.cutover()
            if handoff._receipt(cluster.item, "replacement"):
                break
        cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/data"]["spec"]["accessModes"] = ["ReadWriteMany"]
        count = len(cluster.sent)
        with self.assertRaises(journal.Held): cluster.cutover()
        self.assertEqual(count, len(cluster.sent))
        self.assertEqual("Retain", cluster.objects["/api/v1/persistentvolumes/pv-copy"]["spec"]["persistentVolumeReclaimPolicy"])
