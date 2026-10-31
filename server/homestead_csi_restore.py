"""Import Longhorn backups as retained CSI snapshots, without cloning classes."""
import hashlib
import re
import urllib.error

import homestead_addons as ADDONS
import homestead_kube as KUBE
import homestead_pod_resources as RESOURCES
import homestead_storage_resize as RESIZE
import homestead_names as NAMES

API = "/apis/snapshot.storage.k8s.io/v1"
LABEL = "homestead.io/restore-snapshot"
OWNER = "app.kubernetes.io/managed-by"
CONTROLLER = "homestead-snapshot-controller"
VERSION = "v8.6.0"
UPSTREAM = f"https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/{VERSION}"
CRDS = ("volumesnapshots", "volumesnapshotcontents", "volumesnapshotclasses")
kget = ksend = None


def bind(read, send):
    global kget, ksend
    kget, ksend = read, send


def _items(path):
    try:
        return KUBE.items(path, read=kget)
    except KUBE.Incomplete as error:
        raise ValueError("Snapshot support could not be checked: Kubernetes returned an incomplete inventory") from error


def _controller(deployment):
    containers = ((deployment.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers", [])
    return any(_component(c, "snapshot-controller") for c in containers)


def _component(container, name):
    image_name = str(container.get("image", "")).split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    return image_name == name or image_name.endswith("-sig-storage-" + name) or container.get("name") == name


def support():
    """Read only. Never replace a distribution's existing snapshot controller."""
    discovery = KUBE.get_optional(API, read=kget)
    served = {r.get("name") for r in (discovery or {}).get("resources", [])}
    all_deployments = _items("/apis/apps/v1/deployments")
    deployments = [d for d in all_deployments if _controller(d)]
    ready = any(not (d.get("metadata") or {}).get("deletionTimestamp")
                and (d.get("status") or {}).get("availableReplicas", 0) > 0
                and (d.get("status") or {}).get("observedGeneration", 0) >= (d.get("metadata") or {}).get("generation", 0)
                for d in deployments)
    snapshotters = [d for d in all_deployments
                    if (d.get("metadata") or {}).get("namespace") == "longhorn-system"
                    and any(_component(c, "csi-snapshotter")
                            for c in ((d.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers", []))]
    driver_ready = any((d.get("status") or {}).get("availableReplicas", 0) > 0
                       and not (d.get("metadata") or {}).get("deletionTimestamp") for d in snapshotters)
    if set(CRDS) <= served and ready and not driver_ready:
        return {"ready": False, "can_install": False, "waiting": True,
                "message": "Waiting for Longhorn's CSI snapshotter; repair its deployment if it is missing or unavailable"}
    if set(CRDS) <= served and ready and driver_ready:
        return {"ready": True, "can_install": False, "message": "CSI snapshots are ready"}
    if deployments:
        return {"ready": False, "can_install": False, "waiting": True,
                "message": "Waiting for the existing CSI snapshot controller and its v1 snapshot APIs"}
    platform = ADDONS.platform(True)
    can_install = bool(platform.get("helm_controller") and not platform.get("harvester"))
    message = ("CSI snapshot support will be installed before restoring or stopping the source"
               if can_install else "CSI snapshots need a snapshot controller and the v1 snapshot CRDs; install or repair them through your cluster platform")
    return {"ready": False, "can_install": can_install, "message": message}


def ensure_support():
    checked = support()
    if checked["ready"]:
        return checked
    if not checked["can_install"]:
        # An installed controller starting up is waited out, not duplicated.
        if any(_controller(d) for d in _items("/apis/apps/v1/deployments")):
            return checked
        raise ValueError(checked["message"])
    if CONTROLLER in ADDONS._helmcharts():
        return {**checked, "message": "Waiting for CSI snapshot support to finish installing"}
    nodes = _items("/api/v1/nodes")
    versions = [re.match(r"v?(\d+)\.(\d+)", str((n.get("status", {}).get("nodeInfo") or {}).get("kubeletVersion", ""))) for n in nodes]
    if not versions or any(not v or tuple(map(int, v.groups())) < (1, 25) for v in versions):
        raise ValueError("Automatic CSI snapshot installation requires Kubernetes 1.25 or newer")
    manifests = []
    # Helm leaves existing CRDs alone. Omit them entirely to avoid claiming
    # ownership, and add only definitions this cluster does not have.
    for resource in CRDS:
        if KUBE.get_optional(f"/apis/apiextensions.k8s.io/v1/customresourcedefinitions/{resource}.snapshot.storage.k8s.io", read=kget) is None:
            manifests.append(ADDONS.fetch(f"{UPSTREAM}/client/config/crd/snapshot.storage.k8s.io_{resource}.yaml")[0])
    for filename in ("rbac-snapshot-controller.yaml", "setup-snapshot-controller.yaml"):
        manifest = ADDONS.fetch(f"{UPSTREAM}/deploy/kubernetes/snapshot-controller/{filename}")[0]
        # Unique resource names keep unrelated installations untouched.
        manifest = manifest.replace("snapshot-controller", CONTROLLER).replace(
            f"sig-storage/{CONTROLLER}:", "sig-storage/snapshot-controller:")
        # Upstream release manifests can lag the release's image tag.
        manifests.append(re.sub(r"(registry\.k8s\.io/sig-storage/snapshot-controller:)v[\d.]+", rf"\g<1>{VERSION}", manifest))
    ADDONS._post_chart(CONTROLLER, {"targetNamespace": "kube-system",
        "chartContent": ADDONS.chart_archive(CONTROLLER, VERSION, "\n---\n".join(manifests), "")})
    return {**checked, "message": "Installing CSI snapshot support; the source stays unchanged until it is ready"}


def _owned(obj):
    labels = (obj.get("metadata") or {}).get("labels") or {}
    return labels.get(OWNER) == "homestead" and labels.get(LABEL) == "true"


def _ensure(path, body):
    name = body["metadata"]["name"]
    existing = KUBE.get_optional(f"{path}/{name}", read=kget)
    if existing is None:
        try:
            return ksend("POST", path, body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            existing = kget(f"{path}/{name}")
    spec = existing.get("spec") or {}
    # Snapshot controllers add UID/resourceVersion to volumeSnapshotRef.
    expected = body["spec"]
    matches = all((all((spec.get(key) or {}).get(k) == v for k, v in value.items())
                   if isinstance(value, dict) else spec.get(key) == value)
                  for key, value in expected.items())
    annotations = (existing.get("metadata") or {}).get("annotations") or {}
    annotations_match = all(annotations.get(k) == v for k, v in (body["metadata"].get("annotations") or {}).items())
    if not _owned(existing) or not matches or not annotations_match or (existing.get("metadata") or {}).get("deletionTimestamp"):
        raise ValueError(f"Restore snapshot {name} already exists with different settings or is being deleted")
    return existing


def import_backup(namespace, name, backup, source_volume, url, volume_mode, attempt=""):
    if not source_volume or "/" in source_volume or "/" in backup:
        raise ValueError("Backup source volume or name is unavailable for CSI restore")
    digest = hashlib.sha256(f"{namespace}\0{name}\0{url}\0{attempt}".encode()).hexdigest()[:24]
    snapshot_name = f"homestead-restore-{digest}"
    labels = {OWNER: "homestead", LABEL: "true"}
    annotations = {"homestead.io/restore-pvc": name, "homestead.io/restore-namespace": namespace}
    if re.fullmatch(r"[0-9a-f]{12}", str(attempt)):
        annotations[NAMES.key("move-id")] = attempt
    content = _ensure(f"{API}/volumesnapshotcontents", {
        "apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshotContent",
        "metadata": {"name": snapshot_name, "labels": labels, "annotations": annotations},
        "spec": {"driver": "driver.longhorn.io", "deletionPolicy": "Retain", "sourceVolumeMode": volume_mode,
                 "source": {"snapshotHandle": f"bak://{source_volume}/{backup}"},
                 "volumeSnapshotRef": {"name": snapshot_name, "namespace": namespace}}})
    snapshot = _ensure(f"{API}/namespaces/{namespace}/volumesnapshots", {
        "apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
        "metadata": {"name": snapshot_name, "namespace": namespace, "labels": labels, "annotations": annotations},
        "spec": {"source": {"volumeSnapshotContentName": snapshot_name}}})
    bound_uid = (content.get("spec", {}).get("volumeSnapshotRef") or {}).get("uid")
    if bound_uid and bound_uid != snapshot.get("metadata", {}).get("uid"):
        raise ValueError("Restore snapshot content is bound to an earlier snapshot; start a new restore attempt")
    for obj in (content, snapshot):
        problem = ((obj.get("status") or {}).get("error") or {}).get("message")
        if problem:
            raise ValueError(f"CSI restore snapshot failed: {problem}")
    return snapshot_name


def problem(pvc):
    spec = pvc.get("spec") or {}
    source = spec.get("dataSource") or {}
    if source.get("apiGroup") != "snapshot.storage.k8s.io" or source.get("kind") != "VolumeSnapshot":
        return ""
    snapshot = KUBE.get_optional(f"{API}/namespaces/{pvc['metadata']['namespace']}/volumesnapshots/{source['name']}", read=kget)
    if snapshot is None:
        # Removing import metadata after binding does not affect its disk.
        return "" if (pvc.get("status") or {}).get("phase") == "Bound" else "The restore snapshot no longer exists"
    return (((snapshot.get("status") or {}).get("error") or {}).get("message") or "")


def finish_resize(pvc, cfg):
    """Grow only after restoring the exact source size; Longhorn resets it on create."""
    target = int(cfg.get("size_gb") or 0) * 1073741824
    if target <= int(cfg.get("source_size_bytes") or target):
        return None
    spec = pvc.get("spec") or {}
    requested = RESOURCES.quantity((spec.get("resources") or {}).get("requests", {}).get("storage"), "storage")
    if requested < target:
        RESIZE.require(pvc, kget)
        meta = pvc["metadata"]
        ksend("PATCH", f"/api/v1/namespaces/{meta['namespace']}/persistentvolumeclaims/{meta['name']}",
              {"metadata": {"resourceVersion": meta["resourceVersion"]},
               "spec": {"resources": {"requests": {"storage": str(target)}}}}, ctype="application/merge-patch+json")
        return "running", 96, f"Backup restored; expanding the volume to {cfg['size_gb']} GiB"
    capacity = RESOURCES.quantity((pvc.get("status") or {}).get("capacity", {}).get("storage"), "storage")
    if capacity >= target:
        return "succeeded", 100, f"Backup restored and volume expanded to {cfg['size_gb']} GiB"
    pending = any(c.get("type") == "FileSystemResizePending" and c.get("status") == "True"
                  for c in (pvc.get("status") or {}).get("conditions", []))
    if pending and spec.get("volumeName"):
        pv = kget(f"/api/v1/persistentvolumes/{spec['volumeName']}")
        if RESOURCES.quantity((pv.get("spec") or {}).get("capacity", {}).get("storage"), "storage") >= target:
            return "succeeded", 100, "Backup restored and disk expanded; the filesystem grows when it is next mounted"
    return "running", 97, "Backup restored; waiting for volume expansion"


def cleanup():
    """Remove import metadata only after its PVC binds. Never delete a backup."""
    if KUBE.get_optional(API, read=kget) is None:
        return []
    removed = []
    for content in _items(f"{API}/volumesnapshotcontents?labelSelector={LABEL}%3Dtrue"):
        spec = content.get("spec") or {}
        if not _owned(content) or spec.get("driver") != "driver.longhorn.io" or spec.get("deletionPolicy") != "Retain":
            continue
        annotations = (content.get("metadata") or {}).get("annotations") or {}
        namespace, pvc_name = annotations.get("homestead.io/restore-namespace"), annotations.get("homestead.io/restore-pvc")
        if not namespace or not pvc_name:
            continue
        pvc = KUBE.get_optional(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{pvc_name}", read=kget)
        ref = spec.get("volumeSnapshotRef") or {}
        source = ((pvc or {}).get("spec") or {}).get("dataSource") or {}
        if (ref.get("namespace") != namespace or source.get("apiGroup") != "snapshot.storage.k8s.io"
                or ((pvc or {}).get("status") or {}).get("phase") != "Bound"
                or source.get("name") != ref.get("name") or source.get("kind") != "VolumeSnapshot"):
            continue
        if _remove_pair(content, namespace, ref):
            removed.append(content["metadata"]["name"])
    return removed


def _remove_pair(content, namespace, ref):
    """Delete one restore's snapshot (when it still pairs with content) and
    then its content; False when the snapshot is not that pair's."""
    snapshot_path = f"{API}/namespaces/{namespace}/volumesnapshots/{ref['name']}"
    snapshot = KUBE.get_optional(snapshot_path, read=kget)
    if snapshot:
        if not _owned(snapshot) or (snapshot.get("spec", {}).get("source") or {}).get("volumeSnapshotContentName") != content["metadata"]["name"]:
            return False
        if ref.get("uid") and ref["uid"] != snapshot.get("metadata", {}).get("uid"):
            return False
        _delete(snapshot_path, snapshot)
    # Retain prevents CSI DeleteSnapshot even while Kubernetes waits for
    # snapshot-protection finalizers. Do not remove those finalizers.
    _delete(f"{API}/volumesnapshotcontents/{content['metadata']['name']}", content)
    return True


def discard(namespace, pvc_name, snapshot_name=None):
    """Remove the import metadata of a restore into namespace/pvc_name that
    did not go ahead: its claim could not be made, or it was cancelled and
    its claim deleted. cleanup() removes it only once a claim binds, so
    nothing else would. Only snapshot_name, when given. Kept while a live
    claim of that name restores from it (a reply lost after its create); a
    move's own (its move-id) are left to cleanup_transfer. Never deletes a
    backup: the content is Retain."""
    if KUBE.get_optional(API, read=kget) is None:
        return []
    if snapshot_name:
        found = KUBE.get_optional(f"{API}/volumesnapshotcontents/{snapshot_name}", read=kget)
        contents = [found] if found else []
    else:
        contents = _items(f"{API}/volumesnapshotcontents?labelSelector={LABEL}%3Dtrue")
    claim = KUBE.get_optional(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{pvc_name}", read=kget)
    live = claim if claim and not (claim.get("metadata") or {}).get("deletionTimestamp") else None
    source = ((live or {}).get("spec") or {}).get("dataSource") or {}
    removed = []
    for content in contents:
        spec = content.get("spec") or {}
        annotations = (content.get("metadata") or {}).get("annotations") or {}
        ref = spec.get("volumeSnapshotRef") or {}
        if (not _owned(content) or spec.get("driver") != "driver.longhorn.io" or spec.get("deletionPolicy") != "Retain"
                or annotations.get("homestead.io/restore-namespace") != namespace
                or annotations.get("homestead.io/restore-pvc") != pvc_name or annotations.get(NAMES.key("move-id"))
                or ref.get("namespace") != namespace or not ref.get("name")):
            continue
        if (source.get("apiGroup") == "snapshot.storage.k8s.io" and source.get("kind") == "VolumeSnapshot"
                and source.get("name") == ref["name"]):
            continue
        if _remove_pair(content, namespace, ref):
            removed.append(content["metadata"]["name"])
    return removed


def _delete(path, obj, version=False):
    """Delete Homestead's own snapshot object by its uid alone.

    Callers checked the owner labels (and the snapshot/content pairing) on
    this read. snapshot-controller, kubelet and other controllers keep
    writing these objects (it annotates the content when its snapshot is
    deleted), so a resourceVersion read before then would be refused with
    409. A same-name replacement has a new uid and is still refused. A caller
    whose check depends on content that may still change passes version=True
    to keep the resourceVersion too. An object already deleting is left to
    its finalizers."""
    meta = obj["metadata"]
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("Snapshot cleanup could not verify the object's identity")
    if meta.get("deletionTimestamp"):
        return
    pinned = ("uid", "resourceVersion") if version else ("uid",)
    ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {key: meta[key] for key in pinned}})


def cleanup_transfer(read, send, transfer_id, cancelled=False):
    """Reclaim this transfer's retained metadata, including cancelled imports."""
    # Local import avoids a global binding switch between source/destination.
    import homestead_transfer_cleanup as CLEANUP
    if not re.fullmatch(r"[0-9a-f]{12}", str(transfer_id or "")):
        raise ValueError("Invalid transfer cleanup identity")
    pending = []
    contents = CLEANUP.items(read, f"{API}/volumesnapshotcontents", missing=True)
    claims = CLEANUP.items(read, "/api/v1/persistentvolumeclaims")
    def users_of(namespace, snapshot_name):
        return [p for p in claims
                if any(s.get("name") == snapshot_name and s.get("kind") == "VolumeSnapshot"
                        and s.get("apiGroup") == "snapshot.storage.k8s.io"
                        and s.get("namespace", p["metadata"].get("namespace")) == namespace
                        for s in ((p.get("spec") or {}).get("dataSource") or {},
                                  (p.get("spec") or {}).get("dataSourceRef") or {}))]
    for content in contents:
        spec, meta = content.get("spec") or {}, content["metadata"]
        if not _owned(content) or not CLEANUP.owned(content, transfer_id):
            continue
        if spec.get("driver") != "driver.longhorn.io" or spec.get("deletionPolicy") != "Retain":
            raise ValueError("Transfer snapshot cleanup requires a retained Longhorn snapshot")
        annotations = meta.get("annotations") or {}
        namespace, name = annotations.get("homestead.io/restore-namespace"), annotations.get("homestead.io/restore-pvc")
        ref = spec.get("volumeSnapshotRef") or {}
        if not namespace or not name or ref.get("namespace") != namespace or not ref.get("name"):
            raise ValueError("Transfer snapshot identity does not match its restore")
        users = users_of(namespace, ref["name"])
        if users and (cancelled or any((p.get("status") or {}).get("phase") != "Bound" for p in users)):
            pending.append("restore snapshot " + meta["name"])
            continue
        if not cancelled and not users:
            pending.append("restore snapshot " + meta["name"])
            continue
        path = f"{API}/namespaces/{namespace}/volumesnapshots/{ref['name']}"
        snapshot = CLEANUP.optional(read, path)
        if snapshot:
            if (not _owned(snapshot) or not CLEANUP.owned(snapshot, transfer_id)
                    or (snapshot.get("spec", {}).get("source") or {}).get("volumeSnapshotContentName") != meta["name"]
                    or (ref.get("uid") and ref["uid"] != snapshot["metadata"].get("uid"))):
                raise ValueError("Transfer restore snapshot ownership changed")
            if not CLEANUP.delete(read, send, path, snapshot, own=True):
                pending.append("restore snapshot " + meta["name"])
        if not CLEANUP.delete(read, send, f"{API}/volumesnapshotcontents/{meta['name']}", content, own=True):
            pending.append("restore content " + meta["name"])
    # A previous deletion can remove the content before its snapshot has
    # finished disappearing. Reconcile either half after a lost response.
    for snapshot in CLEANUP.items(read, f"{API}/volumesnapshots", missing=True):
        if not _owned(snapshot) or not CLEANUP.owned(snapshot, transfer_id):
            continue
        meta = snapshot["metadata"]
        content_name = ((snapshot.get("spec") or {}).get("source") or {}).get("volumeSnapshotContentName")
        if not content_name or CLEANUP.optional(read, f"{API}/volumesnapshotcontents/{content_name}"):
            continue
        users = users_of(meta.get("namespace"), meta["name"])
        if ((cancelled and users) or (not cancelled and
                (not users or any((p.get("status") or {}).get("phase") != "Bound" for p in users)))):
            pending.append("restore snapshot " + meta["name"])
        elif not CLEANUP.delete(read, send,
                f"{API}/namespaces/{meta['namespace']}/volumesnapshots/{meta['name']}", snapshot, own=True):
            pending.append("restore snapshot " + meta["name"])
    return pending
