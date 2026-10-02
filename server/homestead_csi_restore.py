"""Import Longhorn backups as retained CSI snapshots, without cloning classes."""
import hashlib
import re
import urllib.error

import homestead_addons as ADDONS
import homestead_pod_resources as RESOURCES
import homestead_storage_resize as RESIZE

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


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _items(path):
    result = kget(path)
    if "items" not in result or (result.get("metadata") or {}).get("continue"):
        raise ValueError("Snapshot support could not be checked: Kubernetes returned an incomplete inventory")
    return result["items"]


def _controller(deployment):
    containers = ((deployment.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers", [])
    return any(_component(c, "snapshot-controller") for c in containers)


def _component(container, name):
    image_name = str(container.get("image", "")).split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    return image_name == name or image_name.endswith("-sig-storage-" + name) or container.get("name") == name


def support():
    """Read only. Never replace a distribution's existing snapshot controller."""
    discovery = _get(API)
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
        if _get(f"/apis/apiextensions.k8s.io/v1/customresourcedefinitions/{resource}.snapshot.storage.k8s.io") is None:
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
    existing = _get(f"{path}/{name}")
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
    snapshot = _get(f"{API}/namespaces/{pvc['metadata']['namespace']}/volumesnapshots/{source['name']}")
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
    if _get(API) is None:
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
        pvc = _get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{pvc_name}")
        ref = spec.get("volumeSnapshotRef") or {}
        source = ((pvc or {}).get("spec") or {}).get("dataSource") or {}
        if (ref.get("namespace") != namespace or source.get("apiGroup") != "snapshot.storage.k8s.io"
                or ((pvc or {}).get("status") or {}).get("phase") != "Bound"
                or source.get("name") != ref.get("name") or source.get("kind") != "VolumeSnapshot"):
            continue
        snapshot_path = f"{API}/namespaces/{namespace}/volumesnapshots/{ref['name']}"
        snapshot = _get(snapshot_path)
        if snapshot:
            if not _owned(snapshot) or (snapshot.get("spec", {}).get("source") or {}).get("volumeSnapshotContentName") != content["metadata"]["name"]:
                continue
            if ref.get("uid") and ref["uid"] != snapshot.get("metadata", {}).get("uid"):
                continue
            _delete(snapshot_path, snapshot)
        # Retain prevents CSI DeleteSnapshot even while Kubernetes waits for
        # snapshot-protection finalizers. Do not remove those finalizers.
        _delete(f"{API}/volumesnapshotcontents/{content['metadata']['name']}", content)
        removed.append(content["metadata"]["name"])
    return removed


def _delete(path, obj):
    meta = obj["metadata"]
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("Snapshot cleanup could not verify the object's identity")
    ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions",
          "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}})
