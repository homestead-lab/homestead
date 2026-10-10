"""Temporary, read-only filesystem clones of existing Longhorn snapshots.

The retained CSI content is the durable session record. Cleanup never requests
DeleteSnapshot: only our helper, restored claim and import metadata are removed.
"""
import base64
import copy
import hashlib
import json
import re
import time
import threading
from functools import wraps
import urllib.error

import homestead_csi_restore as CSI
import homestead_files as FILES
import homestead_pod_resources as RESOURCES

PREFIX = "homestead-snapshot-files-"
LABEL = "homestead.io/snapshot-files"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
TTL = 1800
CHUNK = 1024 * 1024
kget = ksend = image = None
system_namespaces = set()
SC = "/apis/storage.k8s.io/v1/storageclasses/"
_lock = threading.RLock()


def serialized(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return run


def _linked_clone_available():
    crd = _get("/apis/apiextensions.k8s.io/v1/customresourcedefinitions/volumes.longhorn.io") or {}
    supported = any("linked-clone" in v.get("schema", {}).get("openAPIV3Schema", {}).get("properties", {})
                    .get("spec", {}).get("properties", {}).get("cloneMode", {}).get("enum", [])
                    for v in crd.get("spec", {}).get("versions", []) if v.get("served"))
    plugin = _get("/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-csi-plugin") or {}
    images = [c.get("image", "") for c in plugin.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
              if c.get("name") == "longhorn-csi-plugin"]
    version = re.search(r":v?(\d+)\.(\d+)\.(\d+)(?:@sha256:[a-f0-9]+)?$", images[0]) if len(images) == 1 else None
    return bool(supported and version and tuple(map(int, version.groups())) >= (1, 10, 0))


def bind(read, send, helper_image, namespaces):
    global kget, ksend, image, system_namespaces
    kget, ksend, image, system_namespaces = read, send, helper_image, set(namespaces)


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _items(path):
    value = kget(path)
    if not isinstance(value.get("items"), list) or value.get("metadata", {}).get("continue"):
        raise ValueError("Snapshot browser inventory is incomplete")
    return value["items"]


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]|[a-z0-9]", value):
        raise ValueError("Invalid snapshot or volume name")
    return value


def _paths(namespace, name):
    _name(namespace)
    if not re.fullmatch(PREFIX + r"[a-f0-9]{24}", name or ""):
        raise ValueError("Invalid snapshot browser session")
    return (f"{CSI.API}/volumesnapshotcontents/{name}",
            f"{CSI.API}/namespaces/{namespace}/volumesnapshots/{name}",
            f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}",
            f"/api/v1/namespaces/{namespace}/pods/{name}")


def _owned(obj):
    labels = obj.get("metadata", {}).get("labels", {})
    return labels.get(LABEL) == "true" and labels.get("app.kubernetes.io/managed-by") == "homestead"


def _record(namespace, name):
    record = _get(_paths(namespace, name)[0])
    if record is None:
        return None
    spec = record.get("spec", {})
    if (not _owned(record) or spec.get("deletionPolicy") != "Retain" or spec.get("driver") != "driver.longhorn.io"
            or spec.get("volumeSnapshotRef", {}).get("namespace") != namespace
            or spec.get("volumeSnapshotRef", {}).get("name") != name):
        raise ValueError("Snapshot browser identity changed; inspect its resources")
    plan = json.loads(record["metadata"].get("annotations", {}).get("homestead.io/source", "{}"))
    if plan.get("namespace") != namespace or spec.get("source") != {"snapshotHandle": f"snap://{plan.get('volume')}/{plan.get('snapshot')}"}:
        raise ValueError("Snapshot browser source identity changed")
    return record


def review(volume, snapshot):
    volume, snapshot = _name(volume), _name(snapshot)
    if not CSI.support()["ready"]:
        raise ValueError("Snapshot browsing requires ready CSI snapshot support; enable or repair it in the cluster first")
    source = kget(f"{LH}/volumes/{volume}")
    snap = kget(f"{LH}/snapshots/{snapshot}")
    state = snap.get("status", {})
    if (snap.get("spec", {}).get("volume") != volume or not state.get("readyToUse") or state.get("markRemoved")
            or state.get("error") or snap["metadata"].get("deletionTimestamp")):
        raise ValueError("This snapshot is unavailable or being cleaned up")
    pvs = [p for p in _items("/api/v1/persistentvolumes") if p.get("spec", {}).get("csi", {}).get("driver") == "driver.longhorn.io"
           and p["spec"]["csi"].get("volumeHandle") == volume]
    if len(pvs) != 1:
        raise ValueError("The snapshot's original filesystem claim could not be verified")
    pv = pvs[0]
    ref = pv["spec"].get("claimRef", {})
    ns, claim = ref.get("namespace"), ref.get("name")
    _name(ns); _name(claim)
    if ns in system_namespaces:
        raise PermissionError("Homestead does not browse system namespace volumes")
    pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    if (ref.get("uid") != pvc["metadata"].get("uid") or pvc["spec"].get("volumeName") != pv["metadata"]["name"]
            or pvc.get("status", {}).get("phase") != "Bound" or pvc["metadata"].get("deletionTimestamp")):
        raise ValueError("Source volume binding changed; refresh the snapshot")
    if pvc["spec"].get("volumeMode", "Filesystem") != "Filesystem":
        raise ValueError("Only filesystem volumes can be browsed; VM block disks are not supported")
    sc = kget("/apis/storage.k8s.io/v1/storageclasses/" + _name(pvc["spec"].get("storageClassName")))
    if sc.get("provisioner") != "driver.longhorn.io":
        raise ValueError("Temporary browsing requires a Longhorn storage class")
    if (any(sc.get("parameters", {}).get(k) for k in ("fromBackup", "dataSource"))
            or sc.get("parameters", {}).get("encrypted", "false") != "false" or source["spec"].get("encrypted")):
        raise ValueError("This storage class needs a custom restore or encryption configuration and cannot be browsed here")
    size = str(state.get("restoreSize") or source["spec"]["size"])
    if not size.isdigit() or int(size) <= 0:
        raise ValueError("Snapshot volume size is unavailable")
    engine = source["spec"].get("dataEngine", "v1") or "v1"
    if engine not in ("v1", "v2"):
        raise ValueError("Unsupported Longhorn data engine")
    linked = engine == "v2" and _linked_clone_available()
    parameters = {k: v for k, v in sc.get("parameters", {}).items() if k in ("fsType", "diskSelector", "nodeSelector", "staleReplicaTimeout")}
    parameters.update(numberOfReplicas="1", dataEngine=engine)
    parameters["fsType"] = pv["spec"]["csi"].get("fsType") or parameters.get("fsType", "ext4")
    if engine == "v2":
        parameters["cloneMode"] = "linked-clone" if linked else "full-copy"
    plan = {"volume": volume, "snapshot": snapshot, "volume_uid": source["metadata"]["uid"],
            "snapshot_uid": snap["metadata"]["uid"], "namespace": ns, "claim": claim,
            "claim_uid": pvc["metadata"]["uid"], "storage_class": sc["metadata"]["name"],
            "class_uid": sc["metadata"]["uid"], "class_parameters": sc.get("parameters", {}),
            "size": size, "created": state.get("creationTime", ""), "expires_after": TTL,
            "engine": engine, "method": "linked-clone" if linked else "full-copy", "temporary_parameters": parameters}
    return {**plan, "review_token": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}


def _ensure(path, body):
    existing = _get(path)
    if existing is None:
        try:
            return ksend("POST", path.rsplit("/", 1)[0], body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            existing = kget(path)
    if body.get("kind") == "PersistentVolumeClaim":
        requested = existing.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")
        wanted = body["spec"]["resources"]["requests"]["storage"]
        if RESOURCES.quantity(requested, "storage") == RESOURCES.quantity(wanted, "storage"):
            body = copy.deepcopy(body)
            body["spec"]["resources"]["requests"]["storage"] = requested
    # Admission/defaulting may add fields, but never change ours.
    def contains(actual, wanted):
        if isinstance(wanted, dict):
            return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in wanted.items())
        if isinstance(wanted, list):
            return isinstance(actual, list) and len(actual) == len(wanted) and all(contains(a, b) for a, b in zip(actual, wanted))
        return actual == wanted
    if (not _owned(existing) or existing.get("metadata", {}).get("deletionTimestamp")
            or not contains(existing, {k: v for k, v in body.items() if k != "metadata"})
            or not contains(existing.get("metadata", {}).get("ownerReferences", []), body["metadata"].get("ownerReferences", []))
            or not contains(existing.get("metadata", {}).get("annotations", {}), body["metadata"].get("annotations", {}))):
        raise ValueError("Snapshot browser resource already exists with different settings")
    return existing


@serialized
def start(body):
    plan = review(body.get("volume"), body.get("snapshot"))
    if body.get("review_token") != plan["review_token"]:
        raise ValueError("Snapshot details changed; review browsing again")
    request = body.get("request_id", "")
    if not re.fullmatch(r"[a-f0-9]{24}", request):
        raise ValueError("A browser request identity is required")
    name, ns = PREFIX + request, plan["namespace"]
    paths = _paths(ns, name)
    previous = _record(ns, name)
    if previous is None and len(_items(f"{CSI.API}/volumesnapshotcontents?labelSelector={LABEL}%3Dtrue")) >= 4:
        raise ValueError("Four snapshot browsers already exist; close one or wait for expiry")
    expires = previous["metadata"]["annotations"]["homestead.io/expires"] if previous else str(int(time.time()) + TTL)
    if float(expires) <= time.time() or (previous and previous["metadata"]["annotations"].get("homestead.io/closing")):
        raise ValueError("This browser expired or is closing; wait for cleanup before opening another")
    labels = {LABEL: "true", "app.kubernetes.io/managed-by": "homestead"}
    meta = {"name": name, "namespace": ns, "labels": labels}
    content_meta = {"name": name, "labels": labels, "annotations": {
        "homestead.io/expires": expires, "homestead.io/review": plan["review_token"],
        "homestead.io/source": json.dumps(plan), "homestead.io/image": image() if not previous else previous["metadata"]["annotations"]["homestead.io/image"]}}
    helper_image = content_meta["annotations"]["homestead.io/image"]
    if not re.search(r"@sha256:[a-f0-9]{64}$", helper_image):
        raise ValueError("A verified Homestead helper image is required")
    content = _ensure(paths[0], {"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshotContent", "metadata": content_meta,
        "spec": {"driver": "driver.longhorn.io", "deletionPolicy": "Retain", "sourceVolumeMode": "Filesystem",
                 "source": {"snapshotHandle": f"snap://{plan['volume']}/{plan['snapshot']}"},
                 "volumeSnapshotRef": {"name": name, "namespace": ns}}})
    # Cluster-scoped owner references also collect late creations after a concurrent close.
    owner = [{"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshotContent", "name": name, "uid": content["metadata"]["uid"]}]
    meta["ownerReferences"] = owner
    _ensure(SC + name, {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
        "metadata": {"name": name, "labels": labels, "ownerReferences": owner}, "provisioner": "driver.longhorn.io",
        "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate", "parameters": plan["temporary_parameters"]})
    snap = _ensure(paths[1], {"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot", "metadata": meta,
        "spec": {"source": {"volumeSnapshotContentName": name}}})
    content = _record(ns, name)
    bound = content["spec"]["volumeSnapshotRef"].get("uid")
    if bound and bound != snap["metadata"]["uid"]:
        raise ValueError("Snapshot import identity changed")
    pvc = _ensure(paths[2], {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta,
        "spec": {"storageClassName": name, "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                 "resources": {"requests": {"storage": plan["size"]}},
                 "dataSource": {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": name}}})
    # The pod can wait for CSI provisioning; it only ever references our claim.
    _ensure(paths[3], {"apiVersion": "v1", "kind": "Pod", "metadata": {**meta, "ownerReferences": [
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "name": name, "uid": pvc["metadata"]["uid"]}]},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": TTL,
                 "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 5,
                 "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                 "containers": [{"name": "files", "image": helper_image,
                    "command": ["python3", "-c", "import time; time.sleep(1800)"],
                    "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"], "add": ["DAC_READ_SEARCH"]}},
                    "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                    "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True}]}],
                 "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": name, "readOnly": True}}]}})
    return status(ns, name)


def status(namespace, name):
    record = _record(namespace, name)
    if record is None:
        return {"state": "closed", "namespace": namespace, "session": name}
    annotations = record["metadata"]["annotations"]
    result = {"namespace": namespace, "session": name, "expires": float(annotations["homestead.io/expires"]),
              "source": json.loads(annotations["homestead.io/source"]), "read_only": True}
    if annotations.get("homestead.io/closing") or result["expires"] <= time.time():
        return {**result, "state": "closing", "message": "Removing the temporary browser and copy"}
    paths = _paths(namespace, name)
    snapshot, pvc, pod = (_get(p) for p in paths[1:])
    for obj in (snapshot, pvc, pod):
        if obj and not _owned(obj):
            raise ValueError("Snapshot browser resource identity changed")
    for obj in (record, snapshot):
        error = (obj or {}).get("status", {}).get("error", {}) or {}
        if error:
            return {**result, "state": "failed", "message": str(error.get("message", error))}
    phase = (pod or {}).get("status", {}).get("phase")
    if phase in ("Failed", "Succeeded"):
        return {**result, "state": "failed", "message": "The temporary file browser stopped; close it to clean up"}
    clone = _clone(pvc, result["source"], name) if pvc else None
    clone_state = (clone or {}).get("status", {}).get("cloneStatus", {}) or {}
    if clone_state.get("state") == "failed":
        return {**result, "state": "failed", "message": "Longhorn could not prepare the snapshot volume; close the browser and inspect Longhorn"}
    complete = clone_state.get("state") == "completed"
    if complete and (pvc or {}).get("status", {}).get("phase") == "Bound" and phase == "Running" and any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in pod["status"].get("conditions", [])):
        return {**result, "state": "ready", "message": "Snapshot ready to browse read-only"}
    stage = "import" if not (snapshot or {}).get("status", {}).get("readyToUse") else "clone" if not complete else "mount"
    waiting = {"import": "Importing the selected snapshot", "clone": "Creating a linked clone" if result["source"]["method"] == "linked-clone" else "Copying the selected snapshot", "mount": "Mounting the temporary volume read-only"}[stage]
    percent = None
    if clone and not complete:
        rows = [r for e in _items(LH + "/engines") if e.get("spec", {}).get("volumeName") == clone["metadata"]["name"]
                for r in (e.get("status", {}).get("cloneStatus") or {}).values() if r and r.get("snapshotName") == result["source"]["snapshot"]]
        errors = [str(r["error"]) for r in rows if r.get("error")]
        if errors:
            waiting = "; ".join(errors)
        elif rows and all(type(r.get("progress")) in (int, float) and 0 <= r["progress"] <= 100 for r in rows):
            percent = min(r["progress"] for r in rows)
    for c in (pod or {}).get("status", {}).get("conditions", []):
        if c.get("type") == "PodScheduled" and c.get("status") == "False":
            waiting = c.get("message") or waiting
    for c in (pod or {}).get("status", {}).get("containerStatuses", []):
        waiting = c.get("state", {}).get("waiting", {}).get("message") or waiting
    return {**result, "state": "preparing", "stage": stage, "percent": percent, "message": waiting}


def _clone(pvc, plan, name, require_method=True):
    spec = pvc.get("spec", {})
    if spec.get("dataSource") != {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": name} or spec.get("storageClassName") != name:
        raise ValueError("Snapshot claim source changed")
    # Before the PV binds, CSI uses the immutable claim UID as the volume name.
    handle = "pvc-" + pvc["metadata"]["uid"]
    if spec.get("volumeName"):
        pv = kget("/api/v1/persistentvolumes/" + _name(spec["volumeName"]))
        ref = pv.get("spec", {}).get("claimRef", {})
        csi = pv.get("spec", {}).get("csi", {})
        if ref.get("uid") != pvc["metadata"]["uid"] or csi.get("driver") != "driver.longhorn.io":
            raise ValueError("Snapshot volume binding changed")
        handle = csi.get("volumeHandle")
    if handle == plan["volume"]:
        raise ValueError("Refusing to browse the live volume as a snapshot")
    clone = _get(LH + "/volumes/" + _name(handle))
    if clone:
        state = clone.get("status", {}).get("cloneStatus", {}) or {}
        if not state.get("sourceVolume") and not state.get("snapshot"):
            return None  # The controller has not observed the new clone yet.
        if state.get("sourceVolume") != plan["volume"] or state.get("snapshot") != plan["snapshot"]:
            raise ValueError("Longhorn has not verified the selected snapshot as the clone source")
        if require_method and plan["method"] == "linked-clone" and clone.get("spec", {}).get("cloneMode") != "linked-clone":
            raise ValueError("Longhorn did not accept linked-clone mode; no copy fallback was requested")
    return clone


def _delete(path, obj, owner_uid=None):
    if not _owned(obj) or (owner_uid and not any(o.get("uid") == owner_uid for o in obj.get("metadata", {}).get("ownerReferences", []))):
        raise ValueError("Refusing to remove an unrelated snapshot browser resource")
    CSI._delete(path, obj)


@serialized
def close(namespace, name):
    record = _record(namespace, name)
    if record is None:
        return {"state": "closed"}
    paths = _paths(namespace, name)
    if not record["metadata"]["annotations"].get("homestead.io/closing"):
        ksend("PATCH", paths[0], {"metadata": {"resourceVersion": record["metadata"]["resourceVersion"],
              "annotations": {"homestead.io/closing": "true"}}}, ctype="application/merge-patch+json")
    pod = _get(paths[3])
    if pod:
        _delete(paths[3], pod)
        return {"state": "closing"}
    pvc = _get(paths[2])
    if pvc:
        if pvc.get("spec", {}).get("dataSource") != {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": name}:
            raise ValueError("Temporary claim identity changed; cleanup stopped")
        # Delete reclaim policy must never act on a claim rebound to live data.
        _clone(pvc, json.loads(record["metadata"]["annotations"]["homestead.io/source"]), name, require_method=False)
        _delete(paths[2], pvc, record["metadata"]["uid"])
        return {"state": "closing"}
    snapshot = _get(paths[1])
    if snapshot:
        if snapshot.get("spec", {}).get("source") != {"volumeSnapshotContentName": name}:
            raise ValueError("Snapshot import identity changed; cleanup stopped")
        _delete(paths[1], snapshot, record["metadata"]["uid"])
        return {"state": "closing"}
    storage_class = _get(SC + name)
    if storage_class:
        _delete(SC + name, storage_class, record["metadata"]["uid"])
        return {"state": "closing"}
    _delete(paths[0], record)
    return {"state": "closing"}


def cleanup():
    if _get(CSI.API) is None:
        return
    errors = []
    for record in _items(f"{CSI.API}/volumesnapshotcontents?labelSelector={LABEL}%3Dtrue"):
        if not _owned(record):
            continue
        try:
            annotations = record["metadata"].get("annotations", {})
            if annotations.get("homestead.io/closing") or float(annotations.get("homestead.io/expires", "inf")) <= time.time():
                close(record["spec"]["volumeSnapshotRef"]["namespace"], record["metadata"]["name"])
        except Exception as error:
            errors.append(str(error))
    if errors:
        raise ValueError("Snapshot browser cleanup needs attention: " + "; ".join(errors))


# Walk with dirfds and O_NOFOLLOW: snapshot symlinks, FIFOs and device nodes
# must never expose the helper filesystem or block a download indefinitely.
READER = r'''
import os, sys, stat, json, base64
root, action, path, offset = sys.argv[1:]
parts = path.split('/') if path else []
if any(p in ('', '.', '..') for p in parts): raise ValueError('Invalid path')
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for i, part in enumerate(parts):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if i < len(parts)-1 or action == 'list': flags |= os.O_DIRECTORY
        child = os.open(part, flags, dir_fd=fd)
        os.close(fd); fd = child
    info = os.fstat(fd)
    if action == 'list':
        rows = []
        with os.scandir(fd) as entries:
            for entry in entries:
                item = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode):
                    rows.append(dict(name=entry.name, kind='dir' if stat.S_ISDIR(item.st_mode) else 'file', size=item.st_size, editable=False))
                if len(rows) > 500: break
        print(json.dumps(dict(entries=sorted(rows[:500], key=lambda r:(r['kind']!='dir', r['name'].lower())), truncated=len(rows)>500, path=path, read_only=True)))
    else:
        if not stat.S_ISREG(info.st_mode): raise ValueError('Choose a regular file')
        result = dict(size=info.st_size, path=path)
        if action == 'chunk':
            os.lseek(fd, int(offset), os.SEEK_SET)
            result['data'] = base64.b64encode(os.read(fd, 1048576)).decode()
        elif action != 'stat': raise ValueError('Read-only operation required')
        print(json.dumps(result))
finally:
    os.close(fd)
'''


def read(namespace, name, action, path="", offset=0):
    state = status(namespace, name)
    if state["state"] != "ready":
        raise ValueError(state.get("message", "Snapshot browser is closed"))
    paths = _paths(namespace, name)
    pod, pvc = kget(paths[3]), kget(paths[2])
    spec = pod.get("spec", {})
    containers = spec.get("containers", [])
    if (not _owned(pod) or not _owned(pvc) or len(containers) != 1
            or spec.get("automountServiceAccountToken") is not False
            or containers[0].get("image") != _record(namespace, name)["metadata"]["annotations"]["homestead.io/image"]
            or containers[0].get("volumeMounts") != [{"name": "data", "mountPath": "/data", "readOnly": True}]
            or spec.get("volumes") != [{"name": "data", "persistentVolumeClaim": {"claimName": name, "readOnly": True}}]
            or not any(o.get("uid") == pvc["metadata"]["uid"] and o.get("kind") == "PersistentVolumeClaim"
                       for o in pod["metadata"].get("ownerReferences", []))):
        raise ValueError("Read-only snapshot mount could not be verified")
    if action not in ("list", "stat", "chunk") or not isinstance(offset, int) or offset < 0:
        raise ValueError("Read-only operation required")
    relative = str(path or "").lstrip("/")
    if len(relative) > 4096 or "\x00" in relative or any(p in ("", ".", "..") for p in relative.split("/") if relative):
        raise ValueError("Invalid snapshot file path")
    out, err = FILES._exec(namespace, name, ["python3", "-c", READER, "/data", action, relative, str(offset)])
    if err or not out:
        raise ValueError("Cannot read that path; symbolic links and special files are not browsable")
    value = json.loads(out)
    if action == "chunk":
        value["data"] = base64.b64decode(value["data"], validate=True)
    return value


def download(namespace, name, path):
    """A file from the snapshot copy, read a piece at a time as it is sent
    (homestead_routes.Stream); one that changes part-way stops the download."""
    import homestead_routes as ROUTER
    info = read(namespace, name, "stat", path)

    def chunks():
        offset = 0
        while offset < info["size"]:
            chunk = read(namespace, name, "chunk", path, offset)
            data = chunk["data"]
            if chunk["size"] != info["size"] or not data or offset + len(data) > info["size"]:
                raise ValueError("Snapshot file changed during download")
            yield data
            offset += len(data)
    return ROUTER.Stream(chunks(), "application/octet-stream", path.rsplit("/", 1)[-1], info["size"])
