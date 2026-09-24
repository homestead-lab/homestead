"""The source half of a move: what a cluster does when another one takes a workload.

The destination drives. This side only ever does four things, each of them
safe to ask twice: describe a workload, stop it, back up its volumes, and -
once the destination confirms - either restart it or remove it.

Everything this side must remember lives on the workload itself as
annotations, not in a file here: what it was running as before it was stopped,
and which backups hold its data. A Homestead restart halfway through a move
therefore loses nothing, and the workload can always be put back as it was.
"""
import base64
import json
import time
import urllib.error

import homestead_names as NAMES

kget = ksend = None
LH = None
NS = "lab"
LHNS = "longhorn-system"
LH_API = "/apis/longhorn.io/v1beta2"
ORIGIN = "move-origin"
BACKUPS = "move-backups"
# Annotations that describe this cluster's copy of an object rather than the
# thing itself, and would be wrong or meaningless on the far side.
DROP_ANNOTATIONS = {
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
    "kubevirt.io/latest-observed-api-version",
    "kubevirt.io/storage-observed-api-version",
    # Harvester creates a VM's claims from this; the claims will already exist.
    "harvesterhci.io/volumeClaimTemplates",
    "kube-vip.io/loadbalancerIPs",
}
DROP_METADATA = ("uid", "resourceVersion", "creationTimestamp", "generation",
                 "managedFields", "ownerReferences", "finalizers", "selfLink",
                 "deletionTimestamp", "deletionGracePeriodSeconds")


def bind(_kget, _ksend, longhorn, namespace):
    global kget, ksend, LH, NS
    kget, ksend, LH, NS = _kget, _ksend, longhorn, namespace
    NAMES.bind(_kget)


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _kind(kind):
    if kind not in ("container", "vm"):
        raise ValueError("kind must be container or vm")
    return kind


def _path(kind, name):
    if kind == "vm":
        return f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachines/{name}"
    return f"/apis/apps/v1/namespaces/{NS}/deployments/{name}"


def _object(kind, name):
    found = _get(_path(_kind(kind), name))
    if not found:
        raise ValueError(f"{name} does not exist on this cluster")
    return found


def _annotations(obj):
    return (obj.get("metadata", {}) or {}).get("annotations", {}) or {}


def _merge(kind, name, patch):
    return ksend("PATCH", _path(kind, name), patch, ctype="application/merge-patch+json")


# ------------------------------------------------------------------ describing
def _clean_metadata(meta, namespace=None):
    meta = json.loads(json.dumps(meta or {}))
    for key in DROP_METADATA:
        meta.pop(key, None)
    annotations = {
        key: value for key, value in (meta.get("annotations") or {}).items()
        if key not in DROP_ANNOTATIONS
        and key not in (NAMES.key(ORIGIN), NAMES.key(BACKUPS))}
    if annotations:
        meta["annotations"] = annotations
    else:
        meta.pop("annotations", None)
    if namespace is not None:
        meta["namespace"] = namespace
    return meta


def _claims_of(kind, obj):
    """The claims a workload mounts, in the order it mounts them."""
    if kind == "vm":
        volumes = (((obj.get("spec", {}) or {}).get("template", {}) or {})
                   .get("spec", {}) or {}).get("volumes", []) or []
        names = [(v.get("persistentVolumeClaim") or {}).get("claimName")
                 or (v.get("dataVolume") or {}).get("name") for v in volumes]
    else:
        volumes = (((obj.get("spec", {}) or {}).get("template", {}) or {})
                   .get("spec", {}) or {}).get("volumes", []) or []
        names = [(v.get("persistentVolumeClaim") or {}).get("claimName") for v in volumes]
    seen, ordered = set(), []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _longhorn_volume(claim):
    """The Longhorn volume behind a claim, or a reason there is not one."""
    pvc = _get(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{claim}")
    if not pvc:
        raise ValueError(f"claim {claim} does not exist")
    pv_name = (pvc.get("spec", {}) or {}).get("volumeName", "")
    pv = _get(f"/api/v1/persistentvolumes/{pv_name}") if pv_name else None
    csi = ((pv or {}).get("spec", {}) or {}).get("csi", {}) or {}
    if csi.get("driver") != "driver.longhorn.io":
        raise ValueError(f"claim {claim} is not on Longhorn, so it has no backup to travel in")
    return pvc, csi.get("volumeHandle") or pv_name


def _claim_row(claim):
    pvc, volume = _longhorn_volume(claim)
    spec = pvc.get("spec", {}) or {}
    request = ((spec.get("resources", {}) or {}).get("requests", {}) or {}).get("storage", "")
    digits = "".join(ch for ch in str(request) if ch.isdigit())
    storage_class = spec.get("storageClassName", "")
    parameters = ((_get(f"/apis/storage.k8s.io/v1/storageclasses/{storage_class}")
                   if storage_class else None) or {}).get("parameters", {}) or {}
    lh_volume = _get(f"{LH_API}/namespaces/{LHNS}/volumes/{volume}") or {}
    return {
        "claim": claim, "volume": volume,
        "size_gb": int(digits) if digits else 1,
        "access_mode": (spec.get("accessModes") or ["ReadWriteOnce"])[0],
        "volume_mode": spec.get("volumeMode") or "Filesystem",
        "storage_class": storage_class,
        "migratable": str(parameters.get("migratable", "")).lower() == "true",
        "replicas": int(parameters.get("numberOfReplicas") or 2),
        # A disk built on a Harvester image holds only its changes; the image
        # has to exist on the far side before the disk can be restored there.
        "backing_image": ((lh_volume.get("spec", {}) or {}).get("backingImage") or ""),
    }


def _services_for(pod_labels):
    """Services whose selector picks out this workload's pods."""
    try:
        services = kget(f"/api/v1/namespaces/{NS}/services").get("items", [])
    except Exception:
        return []
    found = []
    for service in services:
        selector = (service.get("spec", {}) or {}).get("selector") or {}
        if selector and all(pod_labels.get(k) == v for k, v in selector.items()):
            found.append(service)
    return found


def _clean_service(service):
    spec = json.loads(json.dumps(service.get("spec", {}) or {}))
    for key in ("clusterIP", "clusterIPs", "healthCheckNodePort", "loadBalancerIP",
                "ipFamilies", "ipFamilyPolicy"):
        spec.pop(key, None)
    for port in spec.get("ports", []) or []:
        port.pop("nodePort", None)
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": _clean_metadata(service.get("metadata")), "spec": spec}


def _origin(obj):
    raw = NAMES.read(_annotations(obj), ORIGIN)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _run_state(kind, obj):
    """How the workload was running, in a form that can be put back exactly."""
    spec = obj.get("spec", {}) or {}
    if kind == "vm":
        if "runStrategy" in spec:
            return {"runStrategy": spec.get("runStrategy")}
        return {"running": bool(spec.get("running"))}
    return {"replicas": int(spec.get("replicas", 1) if spec.get("replicas") is not None else 1)}


def definition(kind, name):
    """Everything the far side needs to rebuild this workload.

    Returned to an authenticated admin on another Homestead only. A VM's
    cloud-init Secrets are part of the VM and travel with it; nothing else
    Homestead did not create does.
    """
    kind = _kind(kind)
    obj = _object(kind, name)
    origin = _origin(obj) or _run_state(kind, obj)
    template = ((obj.get("spec", {}) or {}).get("template", {}) or {})
    pod_labels = (template.get("metadata", {}) or {}).get("labels", {}) or {}
    body = json.loads(json.dumps(obj))
    body["metadata"] = _clean_metadata(obj.get("metadata"))
    body.pop("status", None)
    secrets = []
    if kind == "vm":
        spec = body.setdefault("spec", {})
        spec.pop("dataVolumeTemplates", None)
        volumes = spec.get("template", {}).get("spec", {}).get("volumes", []) or []
        for volume in volumes:
            # The disk is restored as a plain claim, so it is mounted as one.
            if "dataVolume" in volume:
                volume["persistentVolumeClaim"] = {"claimName": volume.pop("dataVolume")["name"]}
            for source in ("cloudInitNoCloud", "cloudInitConfigDrive"):
                for ref in ("userDataSecretRef", "networkDataSecretRef"):
                    secret_name = ((volume.get(source) or {}).get(ref) or {}).get("name")
                    if secret_name:
                        secret = _get(f"/api/v1/namespaces/{NS}/secrets/{secret_name}")
                        if secret:
                            secrets.append({"apiVersion": "v1", "kind": "Secret",
                                            "type": secret.get("type", "Opaque"),
                                            "metadata": _clean_metadata(secret.get("metadata")),
                                            "data": secret.get("data", {}) or {}})
        spec.pop("running", None)
        spec["runStrategy"] = "Halted"
    else:
        body.setdefault("spec", {})["replicas"] = 0
    return {
        "kind": kind, "name": name, "namespace": NS,
        "object": body, "origin": origin,
        "services": [_clean_service(s) for s in _services_for(pod_labels)] if kind == "container" else [],
        "secrets": secrets,
        "claims": [_claim_row(claim) for claim in _claims_of(kind, obj)],
        "node_selector": (template.get("spec", {}) or {}).get("nodeSelector", {}) or {},
        "pull_secrets": [ref.get("name") for ref in
                         (template.get("spec", {}) or {}).get("imagePullSecrets", []) or []],
        "networks": [(n.get("multus") or {}).get("networkName") for n in
                     (template.get("spec", {}) or {}).get("networks", []) or []
                     if (n.get("multus") or {}).get("networkName")],
    }


# ------------------------------------------------------------------- stopping
def _remaining(kind, obj):
    """How much of the workload is still running."""
    if kind == "vm":
        vmi = _get(f"/apis/kubevirt.io/v1/namespaces/{NS}/virtualmachineinstances/"
                   f"{obj['metadata']['name']}")
        return 1 if vmi else 0
    labels = ((obj.get("spec", {}) or {}).get("selector", {}) or {}).get("matchLabels", {}) or {}
    if not labels:
        return 0
    selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    try:
        pods = kget(f"/api/v1/namespaces/{NS}/pods?labelSelector={selector}").get("items", [])
    except Exception:
        return 0
    # A terminating pod still holds its claim until it is gone, so it counts.
    return len(pods)


def quiesce(kind, name):
    """Stop the workload so its data is at rest, remembering how it was running.

    Asked twice, it does not overwrite what it remembered the first time -
    otherwise a retry would record the workload as stopped, and putting it
    back would leave it stopped.
    """
    kind = _kind(kind)
    obj = _object(kind, name)
    origin = _origin(obj)
    if not origin:
        origin = _run_state(kind, obj)
    patch = {"metadata": {"annotations": {NAMES.key(ORIGIN): json.dumps(origin)}}}
    if kind == "vm":
        if "runStrategy" in (obj.get("spec", {}) or {}):
            patch["spec"] = {"runStrategy": "Halted"}
        else:
            patch["spec"] = {"running": False}
    else:
        patch["spec"] = {"replicas": 0}
    _merge(kind, name, patch)
    return {"ok": True, "origin": origin, "detail": f"{name} is stopping"}


# ------------------------------------------------------------------ backing up
def _recorded_backups(obj):
    raw = NAMES.read(_annotations(obj), BACKUPS)
    try:
        rows = json.loads(raw) if raw else []
    except ValueError:
        rows = []
    return rows if isinstance(rows, list) else []


def _backup_backing_image(image):
    """Ask Longhorn to store a Harvester image alongside the disks built on it."""
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "BackupBackingImage",
            "metadata": {"name": image, "namespace": LHNS,
                         "labels": {NAMES.key("managed"): "true"}},
            "spec": {"userCreated": True, "labels": {}}}
    try:
        ksend("POST", f"{LH_API}/namespaces/{LHNS}/backupbackingimages", body)
    except urllib.error.HTTPError as error:
        if error.code != 409:           # already backed up is what we wanted
            raise


def backup(kind, name):
    """Back up every claim the workload mounts. Refuses while it still runs."""
    kind = _kind(kind)
    obj = _object(kind, name)
    if not _origin(obj):
        raise ValueError(f"{name} has not been stopped for a move")
    if _remaining(kind, obj):
        raise ValueError(f"{name} is still running; its data is not at rest yet")
    recorded = _recorded_backups(obj)
    have = {row.get("claim") for row in recorded}
    for claim in _claims_of(kind, obj):
        if claim in have:
            continue            # asked twice: the first request's backup stands
        row = _claim_row(claim)
        made = LH.create_backup(row["volume"])
        if row["backing_image"]:
            _backup_backing_image(row["backing_image"])
        recorded.append({"claim": claim, "volume": row["volume"], "backup": made["backup"],
                         "backing_image": row["backing_image"]})
        # Recorded as each is made: a retry after a failure part-way carries
        # on from there instead of backing up the first volumes again.
        _merge(kind, name, {"metadata": {"annotations": {
            NAMES.key(BACKUPS): json.dumps(recorded, separators=(",", ":"))}}})
    return {"ok": True, "backups": recorded}


def status(kind, name):
    """Where this side of a move has got to."""
    kind = _kind(kind)
    obj = _object(kind, name)
    recorded = _recorded_backups(obj)
    by_name = {row["name"]: row for row in LH.backups()} if recorded else {}
    backups = []
    for row in recorded:
        found = by_name.get(row["backup"], {})
        image_state = {}
        if row.get("backing_image"):
            image = _get(f"{LH_API}/namespaces/{LHNS}/backupbackingimages/{row['backing_image']}")
            st = (image or {}).get("status", {}) or {}
            image_state = {"state": st.get("state", "") or "Pending",
                           "progress": int(st.get("progress", 0) or 0),
                           "error": st.get("error", "")}
        backups.append(dict(row, state=found.get("state", "") or "Pending",
                            progress=int(found.get("progress", 0) or 0),
                            error=found.get("error", ""), image=image_state))
    return {"kind": kind, "name": name, "stopped_for_move": bool(_origin(obj)),
            "origin": _origin(obj), "running": _remaining(kind, obj), "backups": backups}


# ------------------------------------------------------------ putting it back
def release(kind, name):
    """Undo a quiesce: run the workload exactly as it was before the move."""
    kind = _kind(kind)
    obj = _object(kind, name)
    origin = _origin(obj)
    if not origin:
        return {"ok": True, "detail": f"{name} was not stopped for a move"}
    patch = {"metadata": {"annotations": {NAMES.key(ORIGIN): None, NAMES.key(BACKUPS): None}},
             "spec": dict(origin)}
    _merge(kind, name, patch)
    return {"ok": True, "detail": f"{name} is running here again, as it was"}


def remove(kind, name, volumes=False):
    """Delete the source copy once the far side has it. Only after a move.

    Refuses a workload that was not stopped for a move: this is a clean-up
    step, not a general-purpose delete someone could reach by accident.
    """
    kind = _kind(kind)
    obj = _object(kind, name)
    if not _origin(obj):
        raise ValueError(f"{name} was not stopped for a move, so it is not this move's to remove")
    if _remaining(kind, obj):
        raise ValueError(f"{name} is running again; it will not be removed while in use")
    claims = _claims_of(kind, obj)
    removed = []
    template = ((obj.get("spec", {}) or {}).get("template", {}) or {})
    if kind == "container":
        for service in _services_for((template.get("metadata", {}) or {}).get("labels", {}) or {}):
            service_name = service["metadata"]["name"]
            try:
                ksend("DELETE", f"/api/v1/namespaces/{NS}/services/{service_name}")
                removed.append(f"service {service_name}")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
    ksend("DELETE", _path(kind, name) + "?propagationPolicy=Background")
    removed.append(f"{'VM' if kind == 'vm' else 'workload'} {name}")
    if volumes:
        for claim in claims:
            try:
                ksend("DELETE", f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{claim}")
                removed.append(f"volume {claim}")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
    return {"ok": True, "removed": removed,
            "detail": "removed " + ", ".join(removed)}


# ------------------------------------------------------------ the shared bucket
def target():
    """This cluster's Longhorn backup target, keys included, for another Homestead.

    Admin-only, because the keys open every backup this cluster has written.
    The far side needs them to read the backups a move writes.
    """
    current = LH.backup_target()
    if not current.get("configured"):
        raise ValueError("this cluster has no Longhorn backup target; set up backup "
                         "storage under Data protection first")
    credentials = {}
    if current.get("secret"):
        secret = _get(f"/api/v1/namespaces/{LHNS}/secrets/{current['secret']}") or {}
        for key, value in (secret.get("data") or {}).items():
            try:
                credentials[key] = base64.b64decode(value).decode()
            except Exception:
                continue
    endpoint = credentials.get("AWS_ENDPOINTS", "")
    return {"url": current.get("url", ""), "endpoint": endpoint,
            "credentials": credentials,
            "reachable_off_cluster": bool(endpoint) and ".svc" not in endpoint,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
