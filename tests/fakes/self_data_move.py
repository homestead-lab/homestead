"""A move of Homestead's own data on the shared fake API, up to the point Homestead
hands over: the real helper setup, plan and publication, on tests/fakes/kube.py.

It talks to the fake's transport directly, as setup and the coordinator use their
own unguarded clients in the app: not through homestead_kube's write guards, whose
state another test may have left. The fake has no controllers, so this plays them,
the way the real ones behave:
- the API server's Job defaults (a generated selector and labels) appear on the
  answer to a create and on a dry run, as #467's fixture learned;
- a Job's pods carry the template's labels, the controller's own labels and its
  tracking finalizer, and are started by start_pod();
- Deployment status and pods are settled by settle_stop() and settle_start().

Time is a number the test moves; leases are renewed as it moves, as kubelets do."""
import copy
import datetime
import json
from unittest import mock

import homestead_self_data_anchor as A
import homestead_self_data_admission as D
import homestead_self_data_bootstrap as B
import homestead_self_data_kube as HK
import homestead_self_data_worker as W
from homestead_storage_journal import identity, shape

OP = "a" * 24
IMAGE = "registry.example.com/homestead@sha256:" + "b" * 64
RECEIPT = {"manifest": "c" * 64, "files": 7, "bytes": 1234}
DIGEST = "d" * 64
NS = "lab"


def _stamp(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _obj(kind, name, spec=None, *, ns=NS, version="v1", owner=None, status=None, labels=None):
    meta = {"name": name}
    if ns:
        meta["namespace"] = ns
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "uid": owner[1], "name": owner[2], "controller": True}]
    if labels:
        meta["labels"] = labels
    result = {"apiVersion": version, "kind": kind, "metadata": meta, "spec": spec or {}}
    if status:
        result["status"] = status
    return result


class Move:
    def __init__(self, kube, *, hosts=("node-a", "node-b"), now=100000, handed_over=True):
        self.kube, self.now, self.hosts = kube, now, tuple(hosts)
        self.logs_text = "HOMESTEAD_SELF_DATA_COPY " + OP + " " + json.dumps(RECEIPT)
        self.dep_path = f"/apis/apps/v1/namespaces/{NS}/deployments/homestead"
        self.pods_path = f"/api/v1/namespaces/{NS}/pods"
        self.copy_path = f"/apis/batch/v1/namespaces/{NS}/jobs/homestead-data-copy-{OP}"
        self._pod_serial = 0
        self._cluster()
        self._setup()
        if handed_over:
            self.hand_over()

    # The cluster before the move ------------------------------------------------

    def _cluster(self):
        kube = self.kube
        for host in self.hosts:
            kube.add(_obj("Node", host, ns=None, status={"nodeInfo": {"bootID": "boot-" + host},
                                                         "conditions": [{"type": "Ready", "status": "True"}]}))
            kube.add(_obj("Lease", host, {"holderIdentity": host, "leaseDurationSeconds": 40, "renewTime": _stamp(self.now)},
                          ns="kube-node-lease", version="coordination.k8s.io/v1"))
        self.pins = [{"name": h, "uid": kube.object("/api/v1/nodes/" + h)["metadata"]["uid"], "boot_id": "boot-" + h} for h in self.hosts]
        dep = _obj("Deployment", "homestead", {"replicas": 1, "template": {"metadata": {"labels": {"app": "homestead"}}, "spec": {
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "source"}}],
            "containers": [{"name": "homestead", "image": IMAGE, "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}}}]}}},
            version="apps/v1", status={"observedGeneration": 1, "replicas": 1, "readyReplicas": 1, "availableReplicas": 1})
        dep["metadata"]["generation"] = 1
        kube.add(dep)
        self.dep_uid = kube.object(self.dep_path)["metadata"]["uid"]
        rs = _obj("ReplicaSet", "hs-rs", {"replicas": 1}, version="apps/v1", owner=("Deployment", self.dep_uid, "homestead"),
                  status={"observedGeneration": 1, "replicas": 1})
        rs["metadata"]["generation"] = 1
        kube.add(rs)
        self.rs_path = f"/apis/apps/v1/namespaces/{NS}/replicasets/hs-rs"
        self.rs_uid = kube.object(self.rs_path)["metadata"]["uid"]
        self.old_pod = kube.add(_obj("Pod", "homestead-old", {"nodeName": self.hosts[0], "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "source"}}]}, owner=("ReplicaSet", self.rs_uid, "hs-rs"),
            status={"phase": "Running"}))
        for claim, volume in (("source", "old-pv"), ("target", "new-pv")):
            kube.add(_obj("PersistentVolumeClaim", claim, {"volumeName": volume, "accessModes": ["ReadWriteMany"]}, status={"phase": "Bound"}))
            uid = kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{claim}")["metadata"]["uid"]
            kube.add(_obj("PersistentVolume", volume, {"claimRef": {"namespace": NS, "name": claim, "uid": uid}}, ns=None))

    # What the API server adds -----------------------------------------------------

    def _job_defaults(self, job, uid):
        job = copy.deepcopy(job)
        job["metadata"].setdefault("uid", uid)
        spec = job["spec"]
        spec.update(selector={"matchLabels": {"batch.kubernetes.io/controller-uid": uid}},
                    completionMode="NonIndexed", suspend=False, manualSelector=False)
        template = spec["template"].setdefault("metadata", {})
        template["labels"] = {**template.get("labels", {}), **self._job_labels(job["metadata"]["name"], uid)}
        return job

    @staticmethod
    def _job_labels(name, uid):
        return {"controller-uid": uid, "job-name": name, "batch.kubernetes.io/controller-uid": uid, "batch.kubernetes.io/job-name": name}

    def read(self, path):
        return self.kube("GET", path)

    def send(self, method, path, body=None, **kwargs):
        if "dryRun=All" in path:
            return self._job_defaults(body, "dry-run-uid") if body["kind"] == "Job" else copy.deepcopy(body)
        if method == "DELETE" and "/jobs/" in path and (body or {}).get("propagationPolicy") == "Foreground":
            # The API server marks a Job and keeps it until its pods are gone.
            current = self.kube.object(path)
            current["metadata"]["finalizers"] = ["foregroundDeletion"]
            self.kube.add(current)
        result = self.kube(method, path, body, **kwargs)
        if method == "POST" and isinstance(result, dict) and result.get("kind") == "Job":
            full = self._job_defaults(result, result["metadata"]["uid"])
            self.kube.add(full)
            return copy.deepcopy(self.kube.object(path + "/" + result["metadata"]["name"]))
        return result

    # The real setup, plan and publication -----------------------------------------

    def _setup(self):
        self.scope = HK.Scope(NS, "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], list(self.hosts))
        self.anchor = A.Anchor(self.read, self.send, NS, "homestead")
        dep = self.kube.object(self.dep_path)
        source = self.kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/source")
        self.handle = self.anchor.create(operation=OP, deployment={"name": "homestead", **identity(dep)},
                                         source={"name": "source", **identity(source)}, destination="target", replicas=1)
        approval = {"threshold": 88, "nodes": self.pins, "receipt": {"proposal": "0" * 64, "warnings": []}}
        with mock.patch.object(D.WorkerAdmitter, "__call__", lambda *_: True):
            self.setup = B.Setup(self.anchor, self.scope, image=IMAGE, node=self.hosts[0], status_digest=DIGEST,
                                 approval=approval, clock=lambda: self.now)
            while not self.setup.step()["complete"]:
                pass
        self.worker_path = self.setup.resources[-2]["target"]["path"]
        self.job = self.kube.object(self.worker_path)
        self.first_pod = self.start_pod(self.hosts[0])

    def hand_over(self):
        dep = self.kube.object(self.dep_path)
        source = self.kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/source")
        fact = lambda obj: {"name": obj["metadata"]["name"], "uid": identity(obj)["uid"], "shape": shape(obj)}
        read = lambda path: self.kube.object(path)
        plan = {"deployment_shape": shape(dep), "source_pvc_shape": shape(source),
                "source_pv": fact(read("/api/v1/persistentvolumes/old-pv")),
                "destination_pvc": fact(read(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/target")),
                "destination_pv": fact(read("/api/v1/persistentvolumes/new-pv")),
                "worker": self.setup.worker_fact(), "nodes": self.pins, "data_volume": "data", "target_shareable": True,
                "copy_image": IMAGE, "copy_node": self.hosts[0]}
        plan["copy_preflight"] = self.setup.preflight_copy(plan)
        self.anchor.configure(plan)
        self.anchor.pointer_published(A.pointer_digest(NS, self.anchor.state, self.handle["uid"]))

    # The Job controller -----------------------------------------------------------

    def start_pod(self, host=None, *, phase="Running"):
        job = self.kube.object(self.worker_path)
        uid = job["metadata"]["uid"]
        self._pod_serial += 1
        name = f"{job['metadata']['name']}-{self._pod_serial:05d}"
        template = copy.deepcopy(job["spec"]["template"])
        pod = _obj("Pod", name, {**template["spec"], "nodeName": host or self.hosts[0]},
                   owner=("Job", uid, job["metadata"]["name"]), labels=template["metadata"]["labels"],
                   status={"phase": phase, "conditions": [{"type": "Ready", "status": "True"}],
                           "containerStatuses": [{"name": "coordinator", "ready": True, "restartCount": 0, "containerID": "containerd://c",
                                                  "imageID": IMAGE, "state": {"running": {"startedAt": _stamp(self.now)}}}]})
        pod["metadata"]["finalizers"] = ["batch.kubernetes.io/job-tracking"]
        pod["metadata"]["ownerReferences"][0].update(apiVersion="batch/v1", blockOwnerDeletion=True)
        path = self.kube.add(pod)
        return self.kube.object(path)["metadata"]["uid"], path

    def pod_lost(self, pod):
        """It left the API: evicted and removed, or its host's pod collected."""
        self.kube.discard(pod[1])

    def pod_failed(self, pod):
        current = self.kube.object(pod[1])
        current["status"]["phase"] = "Failed"
        self.kube.add(current)

    def pod_terminating(self, pod, *, grace=60):
        """deletionTimestamp is the moment its grace ends, as the API server sets it."""
        current = self.kube.object(pod[1])
        current["metadata"]["deletionTimestamp"] = _stamp(self.now + grace)
        self.kube.add(current)

    # Time, and the other controllers ----------------------------------------------

    def pass_time(self, seconds):
        self.now += seconds
        for host in self.hosts:
            path = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/" + host
            lease = self.kube.object(path)
            lease["spec"]["renewTime"] = _stamp(self.now)
            self.kube.add(lease)

    def runner(self, pod=None, *, clock=None, worker_uid=None, config=None):
        pod = pod or self.first_pod
        return W.Runner(self.read, self.send, lambda _: self.logs_text, lambda purpose, proposal: True,
                        namespace=NS, deployment="homestead", operation=OP, anchor_uid=self.handle["uid"],
                        worker_uid=worker_uid or self.job["metadata"]["uid"], pod_uid=pod[0],
                        config=config or self.setup.config, clock=clock or (lambda: self.now), require_setup_receipts=True)

    def state(self):
        return A.Anchor(self.read, self.send, NS, "homestead").load(**self.handle).state

    def steps(self):
        return [e["step"] for e in self.state()["journal"]["ref"]["storage_writes"]]

    def writes_to(self, path, method=None):
        return [w for w in self.kube.writes if w[1] == path and (method is None or w[0] == method)]

    def stopped(self):
        """The Deployment and ReplicaSet controllers have scaled Homestead down."""
        dep = self.kube.object(self.dep_path)
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 0}
        self.kube.add(dep)
        rs = self.kube.object(self.rs_path)
        rs["spec"]["replicas"] = 0
        rs["status"] = {"observedGeneration": rs["metadata"].get("generation", 1), "replicas": 0}
        self.kube.add(rs)
        self.kube.discard(self.old_pod)

    def copy_running(self):
        """The copy Job's pod, running on the copy host, with both volumes mounted."""
        job = self.kube.object(self.copy_path)
        pod = _obj("Pod", "copy-pod", {**copy.deepcopy(job["spec"]["template"]["spec"]), "nodeName": self.hosts[0]},
                   owner=("Job", job["metadata"]["uid"], job["metadata"]["name"]), labels=job["spec"]["template"]["metadata"]["labels"],
                   status={"phase": "Running"})
        self.copy_pod = self.kube.add(pod)

    def rebooted(self, host):
        """It came back: Ready, a fresh lease, another boot id."""
        node = self.kube.object("/api/v1/nodes/" + host)
        node["status"]["nodeInfo"]["bootID"] = "another-boot-of-" + host
        self.kube.add(node)

    def copy_finished(self):
        job = self.kube.object(self.copy_path)
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        self.kube.add(job)
        uid = job["metadata"]["uid"]
        pod = _obj("Pod", "copy-pod", {**copy.deepcopy(job["spec"]["template"]["spec"]), "nodeName": self.hosts[0]},
                   owner=("Job", uid, job["metadata"]["name"]), labels=job["spec"]["template"]["metadata"]["labels"],
                   status={"phase": "Succeeded", "containerStatuses": [{"name": "copy", "containerID": "containerd://copy", "restartCount": 0,
                                                                        "state": {"terminated": {"exitCode": 0}}}]})
        pod["metadata"]["ownerReferences"][0].update(apiVersion="batch/v1", blockOwnerDeletion=True)
        self.copy_pod = self.kube.add(pod)

    def copy_released(self):
        """The foreground delete finished: pod and Job gone."""
        self.kube.discard(self.copy_pod)
        self.kube.discard(self.copy_path)

    def started(self):
        dep = self.kube.object(self.dep_path)
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 1, "updatedReplicas": 1,
                         "readyReplicas": 1, "availableReplicas": 1}
        self.kube.add(dep)
        self.kube.add(_obj("Pod", "homestead-new", {"nodeName": self.hosts[0], "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "target"}}],
            "containers": copy.deepcopy(dep["spec"]["template"]["spec"]["containers"])},
            owner=("ReplicaSet", self.rs_uid, "hs-rs"),
            status={"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}))
