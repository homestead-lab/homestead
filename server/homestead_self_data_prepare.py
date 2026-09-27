"""Prepare a destination while Homestead keeps using its source PVC.

Uses the existing durable job journal. Only the new PVC and its one-shot binding
Pod may be created; only that exact Pod may be deleted. A lost write never becomes
a retry or an adopted same-name resource. Neither data claim is deleted here.
Binding is not an emptiness check; the verified copy still refuses existing data.
Scheduler affinity preserves delayed binding (never set spec.nodeName):
https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode
"""
import copy
import re
import time
import urllib.error

import homestead_capacity_review as SIGN
import homestead_place as PLACE
import homestead_self_data_admission as D
import homestead_self_data_bootstrap as B
import homestead_storage_journal as J


KIND = "self-data-prepare"


def manifests(ref):
    labels = {"app.kubernetes.io/managed-by": "homestead", "homestead.io/data-preparation": ref["operation"]}
    def metadata(name):
        return {"namespace": ref["namespace"], "name": name, "labels": dict(labels)}
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": metadata(ref["destination"]),
           "spec": {"storageClassName": ref["storage_class"], "volumeMode": "Filesystem", "accessModes": [ref["access_mode"]],
                    "resources": {"requests": {"storage": ref["size"]}}}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata("homestead-data-bind-" + ref["operation"]), "spec": {
        "restartPolicy": "Never", "activeDeadlineSeconds": 3600, "automountServiceAccountToken": False,
        "enableServiceLinks": False, "terminationGracePeriodSeconds": 5,
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [ref["node"]]}]}]}}},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "binding", "image": ref["image"], "imagePullPolicy": "IfNotPresent",
            "command": ["python3", "-c", "import time; time.sleep(3600)"],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "destination", "mountPath": "/destination", "readOnly": True}]}],
        "volumes": [{"name": "destination", "persistentVolumeClaim": {"claimName": ref["destination"], "readOnly": True}}]}}
    return pvc, pod


def _class_fact(sc):
    return {"uid": J.identity(sc)["uid"], "shape": J.digest({k: v for k, v in sc.items() if k not in ("metadata", "status")})}


def _get(read, path):
    try:
        return read(path)
    except urllib.error.HTTPError as error:
        if error.code == 404: return None
        raise J.Held("Destination preparation inventory is unavailable") from None
    except Exception:
        raise J.Held("Destination preparation inventory is unavailable") from None


def admission(read, ref, *, clock=time.time):
    """One scheduler-bound helper; no existing app reservations are removed."""
    started = clock()
    _, pod = manifests(ref)
    nodes = D._nodes(read, ref["nodes"], started)
    pods = D._inventory(read, "/api/v1/pods")
    dep = {"metadata": pod["metadata"], "spec": {"template": {"metadata": pod["metadata"], "spec": pod["spec"]}}}
    claim = ref["destination"]
    planned = {claim: {"storage_class": ref["storage_class"], "access_mode": ref["access_mode"]}}
    report = PLACE.manifest_plan(dep, ref["namespace"], pod["metadata"]["name"], 1, ref["threshold"],
        planned_claims=planned, pod_snapshot=pods, nodes_snapshot=nodes, read=read, features=[])
    expected = {f"PVC {claim} is planned, not provisioned; storage capacity and attachment remain unverified",
                f"PVC {claim} will be provisioned after scheduling; storage capacity is unverified",
                f"PVC {claim} is not bound; provisioning and topology need review"}
    if (report["blocked"] or not report["reservations_known"] or report["topology_status"] == "unknown"
            or any(not D._capacity_warning(w) and w not in expected for w in report["warnings"])):
        raise J.Held("The destination binding helper does not fit the current storage, node or capacity constraints")
    if not 0 <= clock() - started <= 30:
        raise J.Held("Destination capacity review expired; check again")
    # Provisioning warnings are displayed separately and acknowledged as a
    # preparation risk, not a RAM override. Their wording changes after creation.
    receipt = {"proposal": J.digest(pod), "warnings": sorted(J.digest(w) for w in report["warnings"] if w not in expected)}
    return report, receipt


def review(read, namespace, deployment, body, *, actor, image, access_mode, threshold, clock=time.time):
    import homestead_self_data_anchor as A
    if not isinstance(body, dict) or not {"operation", "storage_class", "node"} <= set(body) <= {"operation", "storage_class", "node", "capacity_token", "confirm_capacity"}:
        raise J.Held("Choose the destination storage class and preparation host")
    cfg = {key: body[key] for key in ("operation", "storage_class", "node")}
    for value in (namespace, deployment, cfg["storage_class"], cfg["node"]): A._name(value)
    if (not isinstance(cfg["operation"], str) or not re.fullmatch(r"[a-f0-9]{24}", cfg["operation"])
            or not isinstance(actor, str) or not actor or access_mode not in ("ReadWriteOnce", "ReadWriteMany")
            or type(threshold) is not int or not 1 <= threshold <= 100
            or not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image)):
        raise J.Held("Destination preparation needs a verified image and reviewed settings")
    cache = {}
    def current(path):
        if path not in cache: cache[path] = copy.deepcopy(read(path))
        return copy.deepcopy(cache[path])
    dep = current(f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}")
    data = [v for v in dep["spec"]["template"]["spec"].get("volumes", []) if v.get("name") == "data"]
    if len(data) != 1 or not data[0].get("persistentVolumeClaim", {}).get("claimName"):
        raise J.Held("Homestead's current data claim cannot be verified")
    source = current(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/" + data[0]["persistentVolumeClaim"]["claimName"])
    if source.get("status", {}).get("phase") != "Bound" or source["metadata"].get("deletionTimestamp"):
        raise J.Held("Homestead's current data claim is not ready")
    sc = current("/apis/storage.k8s.io/v1/storageclasses/" + cfg["storage_class"])
    mode = sc.get("volumeBindingMode", "Immediate")
    if (sc["metadata"].get("name") != cfg["storage_class"] or sc["metadata"].get("deletionTimestamp")
            or mode not in ("Immediate", "WaitForFirstConsumer") or not sc.get("provisioner")
            or sc["provisioner"] == "driver.longhorn.io" and str(sc.get("parameters", {}).get("migratable", "")).lower() == "true"):
        raise J.Held("Choose a filesystem storage class that supports container data")
    sizes = [source.get("status", {}).get("capacity", {}).get("storage"), source["spec"].get("resources", {}).get("requests", {}).get("storage")]
    size = max(sizes, key=lambda s: D.RESOURCES.quantity(s))
    if D.RESOURCES.quantity(size) <= 0: raise J.Held("The source volume size is unavailable")
    nodes = D._inventory(current, "/api/v1/nodes")
    pins = [{"name": n["metadata"]["name"], "uid": J.identity(n)["uid"], "boot_id": n.get("status", {}).get("nodeInfo", {}).get("bootID")} for n in nodes]
    if cfg["node"] not in {n["name"] for n in pins}: raise J.Held("Choose a current cluster host")
    ref = {**cfg, "namespace": namespace, "destination": "homestead-data-" + cfg["operation"], "image": image,
           "access_mode": access_mode, "size": size, "binding_mode": mode, "nodes": pins, "threshold": threshold,
           "source": {"name": source["metadata"]["name"], "uid": J.identity(source)["uid"], "shape": J.shape(source)},
           "deployment": {"name": deployment, "uid": J.identity(dep)["uid"], "shape": J.shape(dep)}, "class": _class_fact(sc)}
    pvc, pod = manifests(ref)
    for plural, name in (("persistentvolumeclaims", ref["destination"]), ("pods", pod["metadata"]["name"])):
        if _get(current, f"/api/v1/namespaces/{namespace}/{plural}/{name}") is not None:
            raise J.Held("A preparation resource name is already used; it will not be adopted")
    capacity, receipt = admission(current, ref, clock=clock)
    D.validate_worker_approval({"threshold": threshold, "nodes": pins, "receipt": receipt})
    ref["admission"] = receipt
    context = {"action": KIND, "actor": actor, "cluster_uid": J.identity(current("/api/v1/namespaces/kube-system"))["uid"],
               "namespace_uid": J.identity(current("/api/v1/namespaces/" + namespace))["uid"], "plan": ref}
    public = {"destination": ref["destination"], "storage_class": cfg["storage_class"], "size": size, "access_mode": access_mode,
              "node": cfg["node"], "binding_mode": mode, "capacity": capacity,
              "detail": "Prepares a new destination claim. Homestead keeps running on its original volume; copying needs a separate final review and empty-destination check."}
    return cfg, public, context, ref


def start(body, reviewed, ops):
    cfg, public, context, ref = reviewed
    if body.get("confirm_capacity") is not True or not SIGN.valid({**cfg, "capacity_token": body.get("capacity_token")}, context):
        raise J.Held("Review destination preparation and acknowledge its capacity warnings before creating anything")
    # The caller holds ops._lock while recomputing review and starting the job.
    if any(i.get("status") not in ops.TERMINAL or i.get("ref", {}).get("retain_resources") for i in ops._read()):
        raise J.Held("Finish running or recovery jobs before preparing Homestead's destination")
    return ops.start(KIND, "Prepare Homestead data volume", {"kind": "PersistentVolumeClaim", "namespace": ref["namespace"], "name": ref["destination"]},
        "/settings", {**copy.deepcopy(ref), "storage_writes": [], "retain_resources": True}, "Preparation recorded; Homestead keeps running")


def resolve(item, read, send, checkpoint, *, clock=time.time):
    try:
        return _resolve(item, read, send, checkpoint, clock=clock)
    except J.Held:
        raise
    except Exception:
        raise J.Held("Destination preparation could not verify its next step. Retain the job and resources; no request was retried") from None


def _resolve(item, read, send, checkpoint, *, clock=time.time):
    ref = item["ref"]
    D.validate_worker_approval({"threshold": ref["threshold"], "nodes": ref["nodes"], "receipt": ref["admission"]})
    writer = J.Journal(item, read, send, checkpoint)
    writer.check()
    ns = ref["namespace"]
    dep = read(f"/apis/apps/v1/namespaces/{ns}/deployments/" + ref["deployment"]["name"])
    source = read(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/" + ref["source"]["name"])
    if any(J.identity(o)["uid"] != ref[k]["uid"] or J.shape(o) != ref[k]["shape"] or o["metadata"].get("deletionTimestamp") for o, k in ((dep, "deployment"), (source, "source"))):
        raise J.Held("Homestead or its source changed during preparation; both volumes are retained")
    sc = read("/apis/storage.k8s.io/v1/storageclasses/" + ref["storage_class"])
    if sc["metadata"].get("deletionTimestamp") or _class_fact(sc) != ref["class"]:
        raise J.Held("The destination storage class changed after review")
    pvc_body, pod_body = manifests(ref)
    def entry(step): return next((e for e in writer.entries if e["step"] == step), None)
    created = entry("destination")
    if created is None:
        _, approved = admission(read, ref, clock=clock)
        if approved["proposal"] != ref["admission"]["proposal"] or not set(approved["warnings"]) <= set(ref["admission"]["warnings"]):
            raise J.Held("Preparation capacity changed; no destination was created")
        writer.write("destination", "POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", pvc_body)
        return "running", 15, "Creating the destination volume; Homestead is still running"
    pvc = read(created["target"]["path"])
    # Binding legitimately adds volumeName/annotations after creation. Pin the
    # accepted UID and require the reviewed request unchanged; never adopt by name.
    if (J.identity(pvc)["uid"] != created["after"]["uid"] or pvc["metadata"].get("deletionTimestamp")
            or pvc["metadata"].get("ownerReferences") or not B._subset(pvc_body, pvc)
            or set(pvc["spec"]) - set(pvc_body["spec"]) - {"volumeName"}
            or pvc.get("status", {}).get("phase") == "Lost"):
        raise J.Held("The prepared claim was replaced or its reviewed request changed")
    bound = pvc.get("status", {}).get("phase") == "Bound" and bool(pvc["spec"].get("volumeName"))
    binder, removed = entry("binding-pod"), entry("release-binding")
    if not bound:
        if ref["binding_mode"] == "Immediate": return "running", 30, "Waiting for the storage provisioner to bind the destination"
        if binder is None:
            _, approved = admission(read, ref, clock=clock)
            if approved["proposal"] != ref["admission"]["proposal"] or not set(approved["warnings"]) <= set(ref["admission"]["warnings"]):
                raise J.Held("Binding-helper capacity warnings changed; review retained preparation resources")
            preview = send("POST", f"/api/v1/namespaces/{ns}/pods?dryRun=All&fieldValidation=Strict", copy.deepcopy(pod_body))
            B.admitted(pod_body, preview, {"apiVersion": "v1", "kind": "Pod", "namespace": ns, "name": pod_body["metadata"]["name"]}, dry_run=True)
            writer.write("binding-pod", "POST", f"/api/v1/namespaces/{ns}/pods", pod_body)
            return "running", 40, "Scheduling a temporary read-only binding pod on the selected host"
        pod = read(binder["target"]["path"])
        if J.identity(pod)["uid"] != binder["after"]["uid"]:
            raise J.Held("The binding pod was replaced; it will not be adopted")
        B.admitted(pod_body, pod, binder["target"])
        if pod.get("status", {}).get("phase") in ("Failed", "Succeeded"):
            raise J.Held("The binding pod stopped before the destination bound; inspect retained resources")
        return "running", 50, "Waiting for scheduler placement and destination provisioning"
    pv = read("/api/v1/persistentvolumes/" + pvc["spec"]["volumeName"])
    claim = pv.get("spec", {}).get("claimRef", {})
    if (pv["metadata"].get("deletionTimestamp") or (claim.get("namespace"), claim.get("name"), claim.get("uid")) != (ns, ref["destination"], created["after"]["uid"])):
        raise J.Held("The destination PV does not match its created claim")
    J.identity(pv)
    if binder and not removed:
        pod = read(binder["target"]["path"])
        if J.identity(pod)["uid"] != binder["after"]["uid"]: raise J.Held("The binding pod was replaced")
        B.admitted(pod_body, pod, binder["target"])
        writer.write("release-binding", "DELETE", binder["target"]["path"], {"propagationPolicy": "Foreground"}, expected=J.identity(pod))
        return "running", 85, "Destination bound; waiting for the temporary binding pod to release its mount"
    if removed and writer.observe(removed) is not None:
        return "running", 90, "Waiting for the binding pod to finish terminating; no force deletion"
    ref["prepared"] = {"claim_uid": J.identity(pvc)["uid"], "volume_uid": J.identity(pv)["uid"], "volume": pv["metadata"]["name"]}
    ref["retain_resources"] = False
    checkpoint(item)
    return "succeeded", 100, "Destination prepared. Homestead is still on its original volume; review the data move to continue"


def cancel_plan(item):
    return {"can": False, "mode": "forget", "needs": "admin",
            "why_not": "Inspect the preparation job before cleanup; its volume and binding-pod receipts must be retained",
            "keeps": ["Original and destination volumes", "Exact helper and creation receipts"]}


def cancel_run(item, options):
    raise J.Held("Preparation cleanup requires inspection, not forgetting or repeating uncertain writes")
