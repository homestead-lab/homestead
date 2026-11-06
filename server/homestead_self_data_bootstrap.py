"""Create scoped handoff helpers once, with independent pre-dispatch receipts.

No application/volume changes or deletion. Requires the caller's already-reviewed
setup permissions; a 403 is a hold, never a reason to broaden the app's Role.
The control record stores fingerprints and identities, not resource bodies.
"""
import copy
import urllib.error

import homestead_kube as KUBE
import homestead_self_data_kube as K
import homestead_self_data_launch as L
from homestead_storage_journal import Held, digest, identity, pod_annotations, shape


def targets(namespace, deployment, operation, *, worker="Job"):
    """The helpers a move creates. worker="Pod" is the record of a move started by
    an earlier release: its bare coordinator keeps its pinned image and finishes
    by itself, and this release has only to read that record and clean up after it."""
    name = K.access_name(namespace, deployment, operation)
    plural_of = {"Job": "jobs", "Pod": "pods"}
    result = []
    for kind, plural, ns, suffix in (
        ("ServiceAccount", "serviceaccounts", namespace, ""),
        ("Role", "roles", namespace, ""), ("RoleBinding", "rolebindings", namespace, ""),
        ("ClusterRole", "clusterroles", None, ""), ("ClusterRoleBinding", "clusterrolebindings", None, ""),
        ("Role", "roles", "kube-node-lease", "-leases"), ("RoleBinding", "rolebindings", "kube-node-lease", "-leases"),
        (worker, plural_of[worker], namespace, ""), ("Service", "services", namespace, "")):
        version = ("v1" if kind in ("ServiceAccount", "Pod", "Service") else "batch/v1" if kind == "Job"
                   else "rbac.authorization.k8s.io/v1")
        base = "/api/v1" if version == "v1" else "/apis/" + version
        path = base + ("/namespaces/" + ns if ns else "") + "/" + plural + "/" + name + suffix
        result.append({"kind": kind, "apiVersion": version, "namespace": ns, "name": name + suffix, "path": path})
    return result


def validate(value, namespace, deployment, operation):
    # Local import avoids making the anchor import a second copy of itself.
    import homestead_self_data_anchor as A
    A._keys(value, ("resources", "receipts"), ("admission", "config"))
    if "admission" in value:
        from homestead_self_data_admission import validate_worker_approval
        validate_worker_approval(value["admission"])
    if "config" in value:
        L.parse_configuration(value["config"])
    if not isinstance(value["resources"], list) or len(value["resources"]) != 9:
        raise Held("The helper setup plan is incomplete")
    row = value["resources"][-2]
    earlier = isinstance(row, dict) and isinstance(row.get("target"), dict) and row["target"].get("kind") == "Pod"
    expected = targets(namespace, deployment, operation, worker="Pod" if earlier else "Job")
    for resource, target in zip(value["resources"], expected):
        A._keys(resource, ("target", "payload"))
        if resource["target"] != target:
            raise Held("The helper setup target is outside this operation")
        A._hash(resource["payload"])
    receipts = value["receipts"]
    if not isinstance(receipts, list) or len(receipts) > len(expected):
        raise Held("The helper setup receipts are invalid")
    for index, receipt in enumerate(receipts):
        A._keys(receipt, ("state",), ("after", "fingerprint"))
        if receipt["state"] not in ("intent", "accepted", "refused", "uncertain", "unverified"):
            raise Held("The helper setup receipt state is invalid")
        if index < len(receipts) - 1 and receipt["state"] != "accepted":
            raise Held("The helper setup continued past an unresolved request")
        if receipt["state"] == "accepted":
            A._keys(receipt, ("state", "after", "fingerprint"))
            A._identity(receipt["after"]); A._hash(receipt["fingerprint"])
        elif set(receipt) != {"state"}:
            raise Held("An unverified helper cannot have an acceptance receipt")


def complete(value):
    return len(value["receipts"]) == len(value["resources"]) and all(r["state"] == "accepted" for r in value["receipts"])


def fingerprint(obj):
    value = copy.deepcopy(obj)
    meta = value.pop("metadata", {})
    value.pop("status", None)
    value["metadata"] = {key: meta.get(key) for key in ("labels", "annotations", "ownerReferences", "finalizers")}
    if value.get("kind") == "Pod":
        # The network plugin reports on a Pod after it starts; the receipt was
        # taken before that, so what it reports is left out of both. Kubernetes
        # keeps no empty map, so none left is the same as none sent.
        value["metadata"]["annotations"] = pod_annotations(obj) or None
    if value.get("kind") == "Pod":
        # Scheduling may assign this after creation. observe() separately
        # verifies it matches the one reviewed host; all other spec changes hold.
        value["spec"].pop("nodeName", None)
    return digest(value)


def _subset(wanted, actual):
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in wanted.items())
    if isinstance(wanted, list):
        return isinstance(actual, list) and len(wanted) == len(actual) and all(_subset(a, b) for a, b in zip(wanted, actual))
    return type(wanted) is type(actual) and wanted == actual


class PriorityUnknown(Held):
    """A helper's priority couldn't be checked: the classes couldn't be read (#466)."""


UNREAD = {"unread": True}


def default_priority(read):
    """The class Kubernetes' Priority admission gives a pod that names none,
    None when there is none, or UNREAD when the classes couldn't be read.

    A globalDefault PriorityClass makes admission add its name, value and
    policy to every such pod (#437). Unread classes are checked as no default,
    so such a helper is held, never let through, and the hold says why (#466).
    """
    def listed(path):
        # Any answer but a JSON object (dict) is unread, so it is checked
        # here: a None from get_optional is then a 404 and nothing else.
        answer = read(path)
        if not isinstance(answer, dict):
            raise TypeError("PriorityClasses answer is not an object")
        return answer
    try:
        classes = KUBE.get_optional("/apis/scheduling.k8s.io/v1/priorityclasses", read=listed)
        if classes is None:
            # No scheduling API at all: there can be no default class.
            return None
        found = [c for c in classes.get("items", []) if c.get("globalDefault") is True]
        if not found:
            return None
        # Admission takes the lowest value when two claim to be the default.
        chosen = min(found, key=lambda c: c["value"])
        return {"name": chosen["metadata"]["name"], "value": chosen["value"],
                "preemptionPolicy": chosen.get("preemptionPolicy", "PreemptLowerPriority")}
    except Exception:
        return dict(UNREAD)


def admitted(body, obj, target, *, dry_run=False, default=None):
    meta = obj.get("metadata", {}) if isinstance(obj, dict) else {}
    if (not isinstance(obj, dict) or any(obj.get(k) != target[k] for k in ("kind", "apiVersion"))
            or any(meta.get(k) != target[k] for k in ("name", "namespace"))
            or meta.get("deletionTimestamp") or meta.get("ownerReferences", []) != body.get("metadata", {}).get("ownerReferences", []) or meta.get("finalizers")):
        raise Held("The helper creation response does not match the requested resource")
    if not dry_run:
        identity(obj)
    if meta.get("labels", {}) != body.get("metadata", {}).get("labels", {}):
        raise Held("Admission changed helper labels; it must not join another workload's service")
    if pod_annotations(obj) != body.get("metadata", {}).get("annotations", {}):
        raise Held("Admission added unreviewed helper annotations")
    if not _subset(body, obj):
        raise Held("Admission changed a reviewed helper setting; inspect it before continuing")
    kind = obj["kind"]
    if kind == "Job":
        _job_admitted(body, obj, target, default=default)
    elif kind not in ("Pod", "Service"):
        # rules, subjects, roleRef, aggregationRule, token settings and any
        # future RBAC fields must be exactly what this operation requested.
        fields = lambda value: {k: v for k, v in value.items() if k not in ("apiVersion", "kind", "metadata", "status")}
        if fields(body) != fields(obj):
            raise Held("Admission changed the helper's scoped access")
    elif kind == "Pod":
        wanted, spec = body["spec"], obj["spec"]
        allowed = {"nodeName", "dnsPolicy", "schedulerName", "serviceAccount", "serviceAccountName", "priorityClassName", "priority", "preemptionPolicy", "tolerations", "terminationGracePeriodSeconds"}
        if set(spec) - set(wanted) - allowed:
            raise Held("Admission added unreviewed helper pod settings")
        # Only what admission gives every pod that names no class: the cluster's
        # default class, or none at priority 0. A raised class is still held.
        unread = bool(default and default.get("unread"))
        given = default if default and not unread else {"name": None, "value": 0, "preemptionPolicy": "PreemptLowerPriority"}
        hosts = [host for term in wanted["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
                 for host in term["matchFields"][0]["values"]]
        if (spec.get("nodeName") not in (None, "", *hosts) or spec.get("dnsPolicy", "ClusterFirst") != "ClusterFirst"
                or spec.get("schedulerName", "default-scheduler") != "default-scheduler"
                or spec.get("serviceAccount", wanted.get("serviceAccountName", "default")) != wanted.get("serviceAccountName", "default")
                or spec.get("serviceAccountName", "default") != wanted.get("serviceAccountName", "default")
                or spec.get("terminationGracePeriodSeconds", 30) != wanted.get("terminationGracePeriodSeconds", 30)):
            raise Held("The helper placement changed after review")
        if (spec.get("priorityClassName", given["name"]) != given["name"]
                or spec.get("priority", given["value"]) != given["value"]
                or spec.get("preemptionPolicy", given["preemptionPolicy"]) != given["preemptionPolicy"]):
            if unread:
                raise PriorityUnknown("Homestead couldn't read the cluster's PriorityClasses, so it can't confirm the helper's "
                                      "priority. Check that it may list them, and try again")
            raise Held("The helper placement changed after review")
        for toleration in spec.get("tolerations", []):
            if (set(toleration) != {"key", "operator", "effect", "tolerationSeconds"}
                    or toleration["key"] not in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")
                    or toleration["operator"] != "Exists" or toleration["effect"] != "NoExecute"
                    or type(toleration["tolerationSeconds"]) is not int or not 0 <= toleration["tolerationSeconds"] <= 300):
                raise Held("Admission added unreviewed helper tolerations")
        for key in ("volumes", "securityContext", "affinity"):
            if spec[key] != wanted[key]:
                raise Held("Admission changed helper mounts, privileges or placement")
        container, original = spec["containers"][0], wanted["containers"][0]
        extras = set(container) - set(original)
        if extras - {"terminationMessagePath", "terminationMessagePolicy", "imagePullPolicy"} or any(
                container.get(key, default) != original.get(key, default) for key, default in (("terminationMessagePath", "/dev/termination-log"), ("terminationMessagePolicy", "File"), ("imagePullPolicy", "IfNotPresent"))):
            raise Held("Admission added unreviewed helper container settings")
        for key in ("env", "volumeMounts", "securityContext", "resources", "command", "ports"):
            if container.get(key) != original.get(key):
                raise Held("Admission changed the helper container")
    else:
        wanted, spec = body["spec"], obj["spec"]
        allowed = {"clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy", "sessionAffinity", "internalTrafficPolicy"}
        if (set(spec) - set(wanted) - allowed or spec["selector"] != wanted["selector"] or spec["ports"] != wanted["ports"]
                or spec.get("sessionAffinity", "None") != "None" or spec.get("internalTrafficPolicy", "Cluster") != "Cluster"):
            raise Held("Admission changed the private helper progress service")


def _job_admitted(body, obj, target, *, default=None):
    """The Job as the API answered it: only Kubernetes' own defaults and its
    generated selector and labels were added, and what its template describes
    passes the Pod check, because the Job controller makes pods from it."""
    meta, spec, wanted = obj["metadata"], obj["spec"], body["spec"]
    if (set(spec) - set(wanted) - {"selector", "manualSelector", "completionMode", "suspend"}
            or spec.get("manualSelector", False) is not False or spec.get("suspend", False) is not False
            or spec.get("completionMode", "NonIndexed") != "NonIndexed"):
        raise Held("Admission changed the helper Job's policy")
    template = copy.deepcopy(spec["template"])
    generated = {"controller-uid": meta.get("uid"), "batch.kubernetes.io/controller-uid": meta.get("uid"),
                 "job-name": meta["name"], "batch.kubernetes.io/job-name": meta["name"]}
    labels = template.get("metadata", {}).get("labels", {})
    original = wanted["template"].get("metadata", {}).get("labels", {})
    for key in set(labels) - set(original):
        if key not in generated or not generated[key] or labels[key] != generated[key]:
            raise Held("Admission added an unreviewed helper Job label")
        labels.pop(key)
    selector = spec.get("selector", {})
    if (set(selector) - {"matchLabels"} or any(not generated.get(key) or value != generated[key]
            for key, value in selector.get("matchLabels", {}).items())):
        raise Held("Admission added an unreviewed helper Job selector")
    if template.get("metadata", {}).get("annotations", {}) != wanted["template"].get("metadata", {}).get("annotations", {}):
        raise Held("Admission added unreviewed helper Job annotations")
    answered = {"metadata": {"name": meta["name"], "namespace": meta["namespace"]}, "spec": {"template": template}}
    pod_target = {"apiVersion": "v1", "kind": "Pod", "namespace": target["namespace"], "name": target["name"]}
    admitted(L.pod_of(body), L.pod_of(answered), pod_target, dry_run=True, default=default)


def job_pod_admitted(body, job, pod, *, default=None):
    """A Job's pod passes the check its dry run passed (#467): admitted() on the
    reviewed template, so its security contexts, priority, service account and
    tolerations are what was reviewed. The Job controller's additions are the only
    allowance: the generated name, its owner, the controller-uid and job-name
    labels, which must be this Job's, and its tracking finalizer, which stays on
    the pod until the controller has counted it. The pod comes from a list, so it
    names no kind itself."""
    meta, job_uid = pod.get("metadata", {}), identity(job)["uid"]
    owners = meta.get("ownerReferences", [])
    if len(owners) != 1 or owners[0].get("kind") != "Job" or owners[0].get("uid") != job_uid:
        raise Held("The helper pod is not owned by its Job alone")
    added = {"controller-uid": job_uid, "job-name": job["metadata"]["name"],
             "batch.kubernetes.io/controller-uid": job_uid, "batch.kubernetes.io/job-name": job["metadata"]["name"]}
    labels = dict(meta.get("labels", {}))
    if any(labels.pop(key, value) != value for key, value in added.items()):
        raise Held("The helper pod's Job labels are not its Job's")
    body = copy.deepcopy(body)
    body["metadata"].update(name=meta.get("name"), ownerReferences=copy.deepcopy(owners))
    real = {"apiVersion": "v1", "kind": "Pod", **copy.deepcopy(pod)}
    real["metadata"]["labels"] = labels
    real["metadata"]["finalizers"] = [f for f in meta.get("finalizers", []) if f != "batch.kubernetes.io/job-tracking"]
    admitted(body, real, {"apiVersion": "v1", "kind": "Pod", "namespace": body["metadata"]["namespace"], "name": meta.get("name")},
             default=default)


class Setup:
    def __init__(self, anchor, scope, *, image, node, status_digest, approval=None, admit=None, clock=None, route=None, handshake=None):
        self.anchor, self.read, self.send = anchor, anchor.read, anchor.send
        self.scope, self.clock = scope, clock
        state = anchor.state
        if ((scope.namespace, scope.deployment, scope.operation) != (anchor.namespace, state["deployment"]["name"], state["operation"])
                or not {state["source"]["name"], state["destination"]} <= set(scope.claims)):
            raise Held("Helper setup scope does not match this data move")
        self.route, self.handshake = route, handshake
        self.config = L.configuration(scope, anchor.handle()["uid"], status_digest, route)
        self.bodies = L.resources(scope, anchor_uid=anchor.handle()["uid"], image=image, node=node, status_digest=status_digest, route=route)
        self.resources = [{"target": target, "payload": digest(body)} for target, body in
                          zip(targets(scope.namespace, scope.deployment, scope.operation), self.bodies)]
        self.approval = copy.deepcopy(approval if approval is not None else state.get("setup", {}).get("admission"))
        if self.approval is not None:
            from homestead_self_data_admission import WorkerAdmitter
            current_admission = WorkerAdmitter(self.read, scope.namespace, self.approval, **({"clock": clock} if clock is not None else {}))
            if {n["name"] for n in self.approval["nodes"]} != set(scope.nodes):
                raise Held("The coordinator review does not match the helper's host scope")
            if admit is not None:
                raise Held("A persisted coordinator approval cannot use an alternative admission callback")
            admit = current_admission
        if admit is None:
            raise Held("Current capacity and placement must be reviewed before starting the coordinator")
        self.failed, self.admit = False, admit
        self.default = default_priority(self.read)

    def _current(self, *, allow_plan=False):
        if self.failed:
            raise Held("Helper setup stopped after an unverified request; nothing will be retried")
        if len(self.bodies) != len(self.resources) or any(digest(body) != resource["payload"] for body, resource in zip(self.bodies, self.resources)):
            raise Held("A helper manifest changed after its setup review")
        handle = self.anchor.handle()
        obj = self._get(self.anchor.path)
        state = self.anchor._decode(obj, handle["operation"], handle["uid"])
        if identity(obj) != identity(self.anchor.obj) or state != self.anchor.state:
            raise Held("The helper setup record advanced elsewhere")
        if (state.get("setup_aborted") or state["phase"] != "prepare" or "pointer_receipt" in state or not allow_plan and "plan" in state
                or state["journal"]["ref"]["storage_writes"]):
            raise Held("The data move is no longer in helper setup")

    def _get(self, path):
        try:
            return KUBE.get_optional(path, read=self.read)
        except Exception:
            raise Held("Helper setup inventory is unavailable") from None

    def _observe(self, index, receipt):
        if receipt["state"] != "accepted":
            raise Held("A helper request is unresolved; inspect it without retrying or adopting by name")
        target = self.resources[index]["target"]
        obj = self._get(target["path"])
        admitted(self.bodies[index], obj, target, default=self.default)
        if identity(obj)["uid"] != receipt["after"]["uid"] or fingerprint(obj) != receipt["fingerprint"]:
            raise Held("A created helper changed or was replaced")
        return obj

    def step(self):
        """Checkpoint setup, or create at most one resource, in reviewed order."""
        self._current()
        if "setup" not in self.anchor.state:
            # Check before granting helper permissions, then again immediately
            # before Pod creation. No live pod reservations are removed here.
            if self.admit(copy.deepcopy(self.bodies[-2])) is not True:
                raise Held("Current capacity and placement must be reviewed before starting the coordinator")
            self._current()
            self.anchor.prepare_setup(self.resources, admission=self.approval, config=self.config)
            return {"created": 0, "total": len(self.resources), "complete": False}
        setup = self.anchor.state["setup"]
        if setup["resources"] != self.resources or setup.get("admission") != self.approval or setup.get("config") != self.config:
            raise Held("The helper configuration differs from the saved setup review")
        receipts = copy.deepcopy(setup["receipts"])
        for i, receipt in enumerate(receipts):
            self._observe(i, receipt)
        if complete(setup):
            return {"created": len(receipts), "total": len(self.resources), "complete": True}
        i = len(receipts)
        target, body = self.resources[i]["target"], self.bodies[i]
        if self._get(target["path"]) is not None:
            raise Held("A helper name is already in use; it will not be adopted or replaced")
        if target["kind"] == "Job":
            if self.admit is None or self.admit(copy.deepcopy(body)) is not True:
                raise Held("Current capacity and placement must be reviewed before starting the coordinator")
            # The source account checks actual Job and Pod admission before persisting
            # a create intent. Do not grant this creation to the coordinator Role.
            from homestead_self_data_preflight import Preflight
            Preflight(self.scope, self.send, default=self.default, **({"clock": self.clock} if self.clock is not None else {})).worker(body)
            # Admission may take time; recheck prior setup receipts afterwards.
            for previous, receipt in enumerate(receipts):
                self._observe(previous, receipt)
        self._current()
        receipts.append({"state": "intent"})
        self.anchor.checkpoint_setup(receipts)
        try:
            obj = self.send("POST", target["path"].rsplit("/", 1)[0], copy.deepcopy(body))
        except Exception as error:
            self.failed = True
            receipts[-1] = {"state": "refused" if isinstance(error, urllib.error.HTTPError) and error.code in (400, 401, 403, 404, 409, 422) else "uncertain"}
            self._finish_failed(receipts)
            raise Held("Helper creation failed or its outcome is uncertain. No request was retried") from None
        try:
            admitted(body, obj, target, default=self.default)
            receipts[-1] = {"state": "accepted", "after": identity(obj), "fingerprint": fingerprint(obj)}
        except Exception:
            self.failed = True
            receipts[-1] = {"state": "unverified"}
            self._finish_failed(receipts)
            raise Held("The helper creation receipt could not be verified; inspect the retained resource") from None
        self.anchor.checkpoint_setup(receipts)
        return {"created": len(receipts), "total": len(self.resources), "complete": len(receipts) == len(self.resources)}

    def _finish_failed(self, receipts):
        try:
            self.anchor.checkpoint_setup(receipts)
        except Held:
            pass  # The earlier durable intent still prohibits replay.

    def worker_fact(self):
        """Proof for configure(): the actual Job and its one running Pod, never the
        manifest. The Job is what is pinned: a retry's new Pod is still this worker."""
        self._current(allow_plan=True)
        setup = self.anchor.state.get("setup")
        if not setup or setup["resources"] != self.resources or not complete(setup):
            raise Held("Helper setup is not complete")
        observed = [self._observe(i, receipt) for i, receipt in enumerate(setup["receipts"])]
        job = observed[-2]
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in job.get("status", {}).get("conditions", [])):
            raise Held("The move's coordinator Job failed before the move began; Homestead was not stopped. Close this move and start again")
        listed = self._get(f"/api/v1/namespaces/{self.anchor.namespace}/pods") or {}
        pods = [p for p in listed.get("items", [])
                if any(o.get("kind") == "Job" and o.get("uid") == identity(job)["uid"] for o in p.get("metadata", {}).get("ownerReferences", []))
                and p.get("status", {}).get("phase") not in ("Succeeded", "Failed") and not p["metadata"].get("deletionTimestamp")]
        if len(pods) != 1:
            raise Held("Waiting for the exact coordinator pod to become ready")
        pod = pods[0]
        job_pod_admitted(L.pod_of(self.bodies[-2]), job, pod, default=self.default)
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if (pod.get("status", {}).get("phase") != "Running" or not pod["spec"].get("nodeName")
                or self.route is None and not any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod["status"].get("conditions", []))
                or len(statuses) != 1 or statuses[0].get("name") != "coordinator" or type(statuses[0].get("restartCount")) is not int
                or statuses[0]["restartCount"] != 0 or self.route is None and statuses[0].get("ready") is not True or not statuses[0].get("state", {}).get("running")
                or not statuses[0].get("containerID") or not statuses[0].get("imageID")):
            raise Held("Waiting for the exact coordinator pod to become ready")
        # The Service has the Job's name, and /identity answers with the Job's uid.
        if self.route is not None and (self.handshake is None or self.handshake(job) is not True):
            raise Held("Waiting for the maintenance page to answer on the exact coordinator pod")
        self._current(allow_plan=True)
        return {"name": job["metadata"]["name"], "uid": identity(job)["uid"], "shape": shape(job)}

    def preflight_copy(self, plan):
        """Source-side final check; pin its receipt with the immutable plan.

        This does not authorize downtime or publish the local fence. The caller
        still needs signed review, source proof and writer-drain orchestration.
        """
        from homestead_self_data_preflight import Preflight, copy_job
        if plan.get("worker") != self.worker_fact():
            raise Held("Copy preflight needs the exact ready coordinator identity")
        job = copy_job(self.anchor.namespace, {**self.anchor.state, "plan": plan})
        receipt = Preflight(self.scope, self.send, default=self.default, **({"clock": self.clock} if self.clock is not None else {})).copy(job)
        if plan.get("worker") != self.worker_fact():
            raise Held("The coordinator changed during copy admission preflight")
        return receipt
