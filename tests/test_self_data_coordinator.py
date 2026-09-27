import copy
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_coordinator as C
from homestead_storage_journal import Held, identity, shape


OP = "a" * 24
IMAGE = "ghcr.io/wjcloudy/homestead@sha256:" + "b" * 64
RECEIPT = {"manifest": "c" * 64, "files": 7, "bytes": 1234}


def obj(kind, name, spec=None, ns="lab", uid=None, owner=None):
    groups = {"Deployment": "apps/v1", "ReplicaSet": "apps/v1", "Job": "batch/v1", "Lease": "coordination.k8s.io/v1"}
    meta = {"name": name, "uid": uid or name + "-uid", "resourceVersion": "1", "generation": 1}
    if ns is not None:
        meta["namespace"] = ns
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "uid": owner[1], "controller": True}]
    return {"apiVersion": groups.get(kind, "v1"), "kind": kind, "metadata": meta, "spec": spec or {}}


def fact(o):
    return {"name": o["metadata"]["name"], "uid": identity(o)["uid"], "shape": shape(o)}


class Cluster:
    def __init__(self, published=True):
        self.objects, self.sent, self.admissions = {}, [], []
        self.lost = None
        self.reject = None
        self.allow = True
        self.dep_path = "/apis/apps/v1/namespaces/lab/deployments/homestead"
        self.dep = obj("Deployment", "homestead", {"replicas": 2, "template": {"spec": {
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "source"}}],
            "containers": [{"name": "homestead", "image": IMAGE,
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}}}]}}})
        self.dep["status"] = {"observedGeneration": 1, "replicas": 2, "readyReplicas": 2, "availableReplicas": 2}
        self.put(self.dep_path, self.dep)
        self.rs_path = "/apis/apps/v1/namespaces/lab/replicasets/hs-rs"
        self.rs = obj("ReplicaSet", "hs-rs", {"replicas": 2}, owner=("Deployment", "homestead-uid"))
        self.rs["status"] = {"observedGeneration": 1, "replicas": 2}
        self.put(self.rs_path, self.rs)
        for i in range(2):
            pod = obj("Pod", f"old-{i}", {"nodeName": "node1", "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "source"}}]}, owner=("ReplicaSet", "hs-rs-uid"))
            pod["status"] = {"phase": "Running"}
            self.put(f"/api/v1/namespaces/lab/pods/old-{i}", pod)
        self.source = obj("PersistentVolumeClaim", "source", {"volumeName": "old-pv", "accessModes": ["ReadWriteMany"]})
        self.target = obj("PersistentVolumeClaim", "target", {"volumeName": "new-pv", "accessModes": ["ReadWriteMany"]})
        for pvc in (self.source, self.target):
            pvc["status"] = {"phase": "Bound"}
            self.put("/api/v1/namespaces/lab/persistentvolumeclaims/" + pvc["metadata"]["name"], pvc)
        self.oldpv = obj("PersistentVolume", "old-pv", {"claimRef": {"namespace": "lab", "name": "source", "uid": "source-uid"}}, ns=None)
        self.newpv = obj("PersistentVolume", "new-pv", {"claimRef": {"namespace": "lab", "name": "target", "uid": "target-uid"}}, ns=None)
        for pv in (self.oldpv, self.newpv):
            self.put("/api/v1/persistentvolumes/" + pv["metadata"]["name"], pv)
        worker = obj("Pod", "coordinator", {"nodeName": "node1"})
        worker["status"] = {"phase": "Running"}
        self.put("/api/v1/namespaces/lab/pods/coordinator", worker)
        node = obj("Node", "node1", ns=None)
        node["status"] = {"nodeInfo": {"bootID": "boot1"}, "conditions": [{"type": "Ready", "status": "True"}]}
        self.put("/api/v1/nodes/node1", node)
        lease = obj("Lease", "node1", {"holderIdentity": "node1", "leaseDurationSeconds": 40,
                    "renewTime": "1970-01-01T00:16:40Z"}, ns="kube-node-lease")
        self.put("/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node1", lease)
        self.anchor = self.fresh()
        self.handle = self.anchor.create(operation=OP, deployment={"name": "homestead", **identity(self.dep)},
            source={"name": "source", **identity(self.source)}, destination="target", replicas=2)
        self.anchor.configure({"deployment_shape": shape(self.dep), "source_pvc_shape": shape(self.source),
            "source_pv": fact(self.oldpv), "destination_pvc": fact(self.target), "destination_pv": fact(self.newpv),
            "worker": fact(worker), "nodes": [{"name": "node1", "uid": "node1-uid", "boot_id": "boot1"}],
            "data_volume": "data", "target_shareable": True, "copy_image": IMAGE, "copy_node": "node1"})
        if published:
            self.anchor.pointer_published(A.pointer_digest("lab", self.anchor.state, self.handle["uid"]))
        self.logs_text = "HOMESTEAD_SELF_DATA_COPY " + OP + " " + json.dumps(RECEIPT)
        self.job_path = "/apis/batch/v1/namespaces/lab/jobs/homestead-data-copy-" + OP

    def put(self, path, value):
        self.objects[path] = copy.deepcopy(value)

    def read(self, path):
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        if path.endswith(("/pods", "/replicasets", "/horizontalpodautoscalers")):
            return {"items": [copy.deepcopy(v) for p, v in self.objects.items() if p.rsplit("/", 1)[0] == path]}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        if self.reject and self.reject(method, path, body):
            raise TimeoutError("unavailable before applying")
        if method == "POST":
            path += "/" + body["metadata"]["name"]
            if path in self.objects:
                raise urllib.error.HTTPError(path, 409, "exists", {}, None)
            result = copy.deepcopy(body)
            result["metadata"].update(uid=body["metadata"]["name"] + "-created", resourceVersion="1", generation=1)
        else:
            old = self.objects[path]
            expected = body["preconditions"] if method == "DELETE" else body["metadata"]
            if any(expected.get(k) != old["metadata"][k] for k in ("uid", "resourceVersion")):
                raise urllib.error.HTTPError(path, 409, "changed", {}, None)
            if method == "DELETE":
                del self.objects[path]
                result = {"kind": "Status", "status": "Success", "details": {"uid": old["metadata"]["uid"]}}
            else:
                result = copy.deepcopy(body)
                result["metadata"]["resourceVersion"] = str(int(old["metadata"]["resourceVersion"]) + 1)
                if old.get("spec") != body.get("spec"):
                    result["metadata"]["generation"] = old["metadata"]["generation"] + 1
        if method != "DELETE":
            self.put(path, result)
        if self.lost and self.lost(method, path, body):
            raise TimeoutError("secret diagnostic")
        return copy.deepcopy(result)

    def fresh(self):
        return A.Anchor(self.read, self.send, "lab", "homestead")

    def admit(self, purpose, proposed):
        self.admissions.append((purpose, copy.deepcopy(proposed)))
        return self.allow

    def worker(self):
        return C.Coordinator(self.fresh().load(**self.handle), self.read, self.send, lambda _: self.logs_text,
                             self.admit, worker_uid="coordinator-uid", clock=lambda: 1000)

    def step(self):
        return self.worker().step()

    def settle_stop(self):
        dep = self.objects[self.dep_path]
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 0}
        rs = self.objects[self.rs_path]; rs["spec"]["replicas"] = 0
        rs["status"] = {"observedGeneration": 1, "replicas": 0}
        for i in range(2):
            self.objects.pop(f"/api/v1/namespaces/lab/pods/old-{i}", None)

    def copying(self):
        self.step(); self.settle_stop(); self.step(); self.step()

    def finish_copy(self):
        job = self.objects[self.job_path]
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        pod = obj("Pod", "copy-pod", copy.deepcopy(job["spec"]["template"]["spec"]), owner=("Job", job["metadata"]["uid"]))
        pod["spec"]["nodeName"] = "node1"
        pod["status"] = {"phase": "Succeeded", "containerStatuses": [{"name": "copy", "containerID": "containerd://copy-runtime",
                          "restartCount": 0, "state": {"terminated": {"exitCode": 0}}}]}
        self.put("/api/v1/namespaces/lab/pods/copy-pod", pod)

    def switching(self):
        self.copying(); self.finish_copy(); self.step(); self.step()
        self.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        self.step()

    def settle_start(self):
        dep = self.objects[self.dep_path]
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 2,
                         "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}
        for i in range(2):
            pod = obj("Pod", f"new-{i}", {"nodeName": "node1", "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "target"}}]}, owner=("ReplicaSet", "hs-rs-uid"))
            pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
            pod["spec"]["containers"] = copy.deepcopy(dep["spec"]["template"]["spec"]["containers"])
            self.put(f"/api/v1/namespaces/lab/pods/new-{i}", pod)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster()

    def test_ready_condition_without_app_probe_cannot_complete_move(self):
        self.c.switching(); self.c.step(); self.c.settle_stop(); self.c.step(); self.c.settle_start()
        self.c.objects["/api/v1/namespaces/lab/pods/new-0"]["spec"]["containers"][0].pop("readinessProbe")
        with self.assertRaisesRegex(Held, "readiness probe"): self.c.step()
        self.assertEqual("start", self.c.fresh().load(**self.c.handle).state["phase"])

    def test_pod_image_changed_by_admission_cannot_complete_move(self):
        self.c.switching(); self.c.step(); self.c.settle_stop(); self.c.step(); self.c.settle_start()
        self.c.objects["/api/v1/namespaces/lab/pods/new-0"]["spec"]["containers"][0]["image"] = "unreviewed:latest"
        with self.assertRaisesRegex(Held, "source image digest"): self.c.step()

    def test_complete_handoff_waits_for_controllers_mount_release_and_readiness(self):
        c = self.c
        self.assertEqual("quiesce", c.step()["phase"])
        self.assertEqual("quiesce", c.step()["phase"])
        c.settle_stop()
        self.assertEqual("copy", c.step()["phase"])
        self.assertEqual("copy", c.step()["phase"])
        c.finish_copy()
        self.assertEqual("verify", c.step()["phase"])
        self.assertEqual("verify", c.step()["phase"])
        self.assertEqual("verify", c.step()["phase"])  # completed pod still mounts destination
        del c.objects["/api/v1/namespaces/lab/pods/copy-pod"]
        self.assertEqual("switch", c.step()["phase"])
        self.assertEqual("start", c.step()["phase"])
        c.settle_stop()  # controller observes the switched, still-stopped template
        self.assertEqual("start", c.step()["phase"])
        self.assertEqual("start", c.step()["phase"])
        c.settle_start()
        self.assertEqual("done", c.step()["phase"])
        self.assertEqual(["stop", "copy", "switch", "start"], [p for p, _ in c.admissions])
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/source", c.objects)
        deletes = [b for m, _, b in c.sent if m == "DELETE"]
        self.assertEqual(1, len(deletes))
        self.assertEqual("Foreground", deletes[0]["propagationPolicy"])

    def test_stale_heartbeat_and_reboot_block_before_stopping(self):
        for change in ("lease", "boot"):
            c = Cluster()
            if change == "lease":
                c.objects["/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node1"]["spec"]["renewTime"] = "1970-01-01T00:00:00Z"
            else:
                c.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"]["bootID"] = "new-boot"
            before = len(c.sent)
            with self.assertRaises(Held): c.step()
            self.assertEqual(before, len(c.sent))

    def test_source_rebound_or_claim_replaced_blocks(self):
        for path, mutate in (("/api/v1/namespaces/lab/persistentvolumeclaims/source", lambda o: o["metadata"].update(uid="replacement")),
                             ("/api/v1/persistentvolumes/old-pv", lambda o: o["spec"]["claimRef"].update(uid="other"))):
            c = Cluster(); mutate(c.objects[path]); before = len(c.sent)
            with self.assertRaises(Held): c.step()
            self.assertEqual(before, len(c.sent))

    def test_lost_stop_response_is_not_replayed_or_followed_by_copy(self):
        c = self.c
        c.lost = lambda m, p, b: p == c.dep_path
        with self.assertRaises(Held): c.step()
        c.settle_stop()
        with self.assertRaises(Held): c.step()
        self.assertEqual(1, sum(p == c.dep_path for _, p, _ in c.sent))
        self.assertNotIn(c.job_path, c.objects)

    def test_accepted_stop_before_phase_save_is_observed_not_repeated(self):
        c = self.c
        c.lost = lambda m, p, b: p == c.anchor.path and json.loads(b["data"]["state.json"])["phase"] == "quiesce"
        with self.assertRaises(Held): c.step()
        c.lost = None
        self.assertEqual("quiesce", c.step()["phase"])
        self.assertEqual(1, sum(p == c.dep_path for _, p, _ in c.sent))

    def test_stop_receipt_survives_phase_checkpoint_not_reaching_api(self):
        c = self.c
        c.reject = lambda m, p, b: p == c.anchor.path and json.loads(b["data"]["state.json"])["phase"] == "quiesce"
        with self.assertRaises(Held): c.step()
        self.assertEqual("prepare", c.fresh().load(**c.handle).state["phase"])
        c.reject = None
        self.assertEqual("quiesce", c.step()["phase"])
        self.assertEqual(1, sum(p == c.dep_path for _, p, _ in c.sent))

    def test_new_foreign_writer_during_copy_blocks_progress(self):
        c = self.c; c.copying()
        foreign = obj("Pod", "other", {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "source"}}]})
        c.put("/api/v1/namespaces/lab/pods/other", foreign)
        before = len(c.sent)
        with self.assertRaisesRegex(Held, "Another pod"): c.step()
        self.assertEqual(before, len(c.sent))

    def test_unobserved_replica_set_prevents_copy(self):
        c = self.c; c.step(); c.settle_stop()
        c.objects[c.rs_path]["status"]["observedGeneration"] = 0
        self.assertEqual("quiesce", c.step()["phase"])
        self.assertNotIn(c.job_path, c.objects)

    def test_capacity_change_blocks_restart_without_discarding_copy(self):
        c = self.c; c.switching(); c.step(); c.settle_stop()
        c.allow = False
        with self.assertRaisesRegex(Held, "capacity"): c.step()
        self.assertEqual(0, c.objects[c.dep_path]["spec"]["replicas"])
        self.assertEqual("target", c.objects[c.dep_path]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_copy_receipt_requires_matching_operation_and_successful_pod(self):
        for failure in ("wrong-op", "duplicates", "exit", "image", "two-pods"):
            c = Cluster(); c.copying(); c.finish_copy()
            pod = c.objects["/api/v1/namespaces/lab/pods/copy-pod"]
            if failure == "wrong-op": c.logs_text = c.logs_text.replace(OP, "f" * 24)
            if failure == "duplicates": c.logs_text += "\n" + c.logs_text
            if failure == "exit": pod["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 1
            if failure == "image": pod["spec"]["containers"][0]["image"] = "unreviewed:latest"
            if failure == "two-pods":
                other = copy.deepcopy(pod); other["metadata"].update(name="another", uid="another-uid")
                c.put("/api/v1/namespaces/lab/pods/another", other)
            with self.subTest(failure=failure), self.assertRaises(Held): c.step()
            self.assertNotIn("release-copy", [e["step"] for e in c.fresh().load(**c.handle).item()["ref"]["storage_writes"]])

    def test_copy_log_pod_replacement_is_rejected(self):
        c = self.c; c.copying(); c.finish_copy(); worker = c.worker()
        def logs(_):
            c.objects["/api/v1/namespaces/lab/pods/copy-pod"]["metadata"]["uid"] = "replacement"
            return c.logs_text
        worker.logs = logs
        with self.assertRaisesRegex(Held, "replaced"): worker.step()

    def test_other_coordinator_and_new_autoscaler_are_blocked(self):
        c = self.c
        with self.assertRaises(Held):
            C.Coordinator(c.fresh().load(**c.handle), c.read, c.send, lambda _: "", c.admit, worker_uid="other")
        c.put("/apis/autoscaling/v2/namespaces/lab/horizontalpodautoscalers/hpa", obj("HorizontalPodAutoscaler", "hpa", {
            "scaleTargetRef": {"kind": "Deployment", "name": "homestead"}}))
        with self.assertRaisesRegex(Held, "autoscaler"): c.step()

    def test_foreign_mount_blocks_before_homestead_is_stopped(self):
        c = self.c
        c.put("/api/v1/namespaces/lab/pods/other", obj("Pod", "other", {
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "source"}}]}))
        before = len(c.sent)
        with self.assertRaisesRegex(Held, "not stopped"): c.step()
        self.assertEqual(before, len(c.sent))
        self.assertEqual(2, c.objects[c.dep_path]["spec"]["replicas"])

    def test_copy_job_create_lost_response_never_creates_a_second_job(self):
        c = self.c; c.step(); c.settle_stop(); c.step()
        c.lost = lambda m, p, b: m == "POST" and p == c.job_path
        with self.assertRaises(Held): c.step()
        c.lost = None
        with self.assertRaises(Held): c.step()
        self.assertEqual(1, sum(m == "POST" and p.endswith("/jobs") for m, p, _ in c.sent))
        self.assertIn(c.job_path, c.objects)

    def test_lost_switch_or_restart_reply_is_held_without_rollback(self):
        for at_start in (False, True):
            c = Cluster(); c.switching()
            if at_start:
                c.step(); c.settle_stop()
            c.lost = lambda m, p, b: p == c.dep_path
            before = sum(p == c.dep_path for _, p, _ in c.sent)
            with self.assertRaises(Held): c.step()
            c.lost = None
            with self.assertRaises(Held): c.step()
            self.assertEqual(before + 1, sum(p == c.dep_path for _, p, _ in c.sent))
            self.assertEqual("target", c.objects[c.dep_path]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_replacement_copy_job_cannot_be_deleted_by_old_receipt(self):
        c = self.c; c.copying(); c.finish_copy(); c.step()
        c.objects[c.job_path]["metadata"]["uid"] = "new-job"
        with self.assertRaises(Held): c.step()
        self.assertFalse(any(m == "DELETE" for m, _, _ in c.sent))

    def test_returning_writer_during_admission_prevents_cutover(self):
        c = self.c; c.switching(); worker = c.worker()
        def admit(*_):
            c.put("/api/v1/namespaces/lab/pods/old-returned", obj("Pod", "old-returned", {
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "source"}}]}, owner=("ReplicaSet", "hs-rs-uid")))
            return True
        worker.admit = admit
        with self.assertRaisesRegex(Held, "writer returned"): worker.step()
        self.assertEqual("source", c.objects[c.dep_path]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_paginated_inventory_does_not_mean_no_writers(self):
        c = self.c; c.step(); c.settle_stop(); worker = c.worker()
        original = worker.read
        worker.read = lambda p: {"items": [], "metadata": {"continue": "more"}} if p.endswith("/pods") else original(p)
        with self.assertRaisesRegex(Held, "incomplete"): worker.step()
        self.assertNotIn(c.job_path, c.objects)

    def test_stale_coordinator_cannot_continue_after_control_record_advanced(self):
        c = self.c; stale = c.worker(); c.step()
        before = len(c.sent)
        with self.assertRaisesRegex(Held, "advanced elsewhere"): stale.step()
        self.assertEqual(before, len(c.sent))

    def test_no_restart_until_copy_pod_is_gone_even_if_job_is_gone(self):
        c = self.c; c.copying(); c.finish_copy(); c.step(); c.step()
        self.assertNotIn(c.job_path, c.objects)
        for _ in range(3):
            self.assertEqual("verify", c.step()["phase"])
        self.assertEqual(0, c.objects[c.dep_path]["spec"]["replicas"])
        self.assertEqual("source", c.objects[c.dep_path]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_unready_new_replica_is_not_completion(self):
        c = self.c; c.switching(); c.step(); c.settle_stop(); c.step(); c.settle_start()
        c.objects["/api/v1/namespaces/lab/pods/new-0"]["status"]["conditions"] = []
        self.assertEqual("start", c.step()["phase"])


if __name__ == "__main__":
    unittest.main()
