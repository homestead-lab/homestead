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
        self.objects[path] = obj
        self.item["ref"]["review_fences"][path] = journal.identity(obj)
        return path

    def consumer(self, kind, name, **kwargs):
        self.item["ref"]["consumers"].append({"kind": kind, "name": name, **kwargs})

    def read(self, path):
        if path == "/api/v1/namespaces/lab/pods":
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
            "template": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}})
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
        if path.endswith(("/deployments", "/statefulsets", "/daemonsets", "/cronjobs", "/virtualmachines")):
            return {"items": [copy.deepcopy(obj) for key, obj in self.objects.items() if key.rsplit("/", 1)[0] == path]}
        return super().read(path)

    def send(self, method, path, body, **kwargs):
        if method != "POST":
            return super().send(method, path, body, **kwargs)
        event = self.durable["ref"]["storage_writes"][-1]
        assert event["state"] == "intent"
        self.sent.append((method, path, copy.deepcopy(body)))
        obj = copy.deepcopy(body)
        obj["metadata"].update(uid=obj["metadata"]["name"] + "-uid", resourceVersion="1")
        self.objects[path + "/" + obj["metadata"]["name"]] = obj
        if obj["kind"] == "PersistentVolumeClaim":
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


class CopyHandoffTests(unittest.TestCase):
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
