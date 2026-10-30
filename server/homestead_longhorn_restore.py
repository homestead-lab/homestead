"""
Restoring a Longhorn backup into a new claim: the plan and its checks, the
restore itself, and the CSI snapshot support it uses.

Split out of homestead_longhorn (#623); what bind() sets, the shared helpers
and the routes stay there.
"""
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_csi_restore as CSI_RESTORE
import math
import re


K8S_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _valid_k8s_name(value):
    value = str(value or "")
    return bool(value) and len(value) <= 253 and all(
        K8S_LABEL.fullmatch(part) for part in value.split("."))


def _backup(name):
    if not _valid_k8s_name(name):
        raise ValueError("backup name is invalid")
    item = KUBE.get_optional(f"{LH.API}/namespaces/{LH.LHNS}/backups/{name}", read=LH.kget)
    if item is None:
        raise ValueError("backup was not found")
    return item


def _restore_source(item):
    status = item.get("status", {}) or {}
    state = str(status.get("state", "") or "")
    url = str(status.get("url", "") or "")
    size = int(status.get("volumeSize", 0) or 0)
    if state.lower() != "completed":
        raise ValueError(f"backup is not ready to restore (state: {state or 'pending'})")
    if not url:
        raise ValueError("backup URL is not available yet")
    if size <= 0:
        raise ValueError("backup volume size is unavailable")
    return status, url, size


def restore_plan(backup, namespace, pvc_name):
    item = _backup(backup)
    status, url, size = _restore_source(item)
    namespace = str(namespace or "").strip()
    pvc_name = str(pvc_name or "").strip()
    if namespace and not _valid_k8s_name(namespace):
        raise ValueError("namespace is invalid")
    if pvc_name and not _valid_k8s_name(pvc_name):
        raise ValueError("PVC name must use lowercase letters, numbers, dots and dashes")
    source_volume = str(status.get("volumeName", "") or "backup")
    suggested = re.sub(r"[^a-z0-9-]+", "-", source_volume.lower()).strip("-")[:230]
    suggested = (suggested or "restored-volume") + "-restore"
    conflict = None
    if namespace:
        if KUBE.get_optional(f"/api/v1/namespaces/{namespace}", read=LH.kget) is None:
            conflict = {"kind": "Namespace", "name": namespace,
                        "message": f"Namespace {namespace} does not exist"}
        elif pvc_name and KUBE.get_optional(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{pvc_name}", read=LH.kget) is not None:
            conflict = {"kind": "PersistentVolumeClaim", "name": pvc_name,
                        "message": f"PVC {namespace}/{pvc_name} already exists; choose a new name"}
    return {
        "backup": item["metadata"]["name"], "source_volume": source_volume,
        "created": status.get("backupCreatedAt", ""),
        "backup_size_mb": round(int(status.get("size", 0) or 0) / 1048576, 1),
        "volume_size_bytes": size, "minimum_size_gb": max(1, math.ceil(size / 1073741824)),
        "suggested_name": suggested, "namespace": namespace, "pvc_name": pvc_name,
        "conflict": conflict, "ready": conflict is None,
        "target": status.get("backupTargetName", "default") or "default",
        "storage_class": LH.STORAGE_CLASS,
        "storage_classes": [c["metadata"]["name"] for c in LH.kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])
                            if c.get("provisioner") == "driver.longhorn.io"
                            and not any((c.get("parameters") or {}).get(k) for k in ("fromBackup", "backingImage"))],
    }


def restore_backup(cfg, discard_on_failure=False):
    backup = str(cfg.get("backup") or "").strip()
    namespace = str(cfg.get("namespace") or "").strip()
    pvc_name = str(cfg.get("name") or "").strip()
    if not namespace or not pvc_name:
        raise ValueError("Choose a namespace and a new PVC name")
    plan = restore_plan(backup, namespace, pvc_name)
    if plan["conflict"]:
        raise ValueError(plan["conflict"]["message"])
    access_mode = str(cfg.get("access_mode") or "ReadWriteOnce")
    if access_mode not in ("ReadWriteOnce", "ReadWriteMany"):
        raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
    base_name = str(cfg.get("storage_class") or LH.STORAGE_CLASS)
    if not _valid_k8s_name(base_name):
        raise ValueError("storage class name is invalid")
    base = LH.kget(f"/apis/storage.k8s.io/v1/storageclasses/{base_name}")
    if base.get("provisioner") != "driver.longhorn.io":
        raise ValueError(f"storage class {base_name} is not managed by Longhorn")
    parameters = dict(base.get("parameters", {}) or {})
    if cfg.get("storage_class") and any(parameters.get(k) for k in ("fromBackup", "backingImage")):
        raise ValueError("choose a regular Longhorn storage class")
    size_gb = int(cfg.get("size_gb") or plan["minimum_size_gb"])
    if size_gb < plan["minimum_size_gb"]:
        raise ValueError(f"restored PVC cannot be smaller than {plan['minimum_size_gb']} GiB")
    if cfg.get("size_gb") and size_gb * 1073741824 > plan["volume_size_bytes"] and base.get("allowVolumeExpansion") is not True:
        raise ValueError("A larger restore needs a storage class that allows volume expansion")
    volume_mode = str(cfg.get("volume_mode") or "Filesystem")
    if volume_mode not in ("Filesystem", "Block"):
        raise ValueError("volume mode must be Filesystem or Block")
    validate_restore_class(base, volume_mode)
    status, url, _ = _restore_source(_backup(backup))
    # Replica count, tags and migratability come from the configured class.
    # Longhorn's restore webhook recovers backingImage/backupTargetName from
    # the backup; migration prepares that image before creating this PVC.
    backing_image = str(status.get("volumeBackingImageName") or cfg.get("backing_image") or "")
    if backing_image and KUBE.get_optional(f"{LH.API}/namespaces/{LH.LHNS}/backingimages/{backing_image}", read=LH.kget) is None:
        raise ValueError(f"Restore the {backing_image} backing image before restoring this disk")
    supported = CSI_RESTORE.ensure_support()
    result = {"ok": True, "backup": backup, "namespace": namespace, "name": pvc_name,
              "storage_class": base_name, "size_gb": size_gb, "access_mode": access_mode,
              "source_size_bytes": plan["volume_size_bytes"]}
    if not supported["ready"]:
        return {**result, "created": False, "message": supported["message"]}
    snapshot_name = CSI_RESTORE.import_backup(namespace, pvc_name, backup,
        status.get("volumeName", ""), url, volume_mode,
        str(cfg.get("restore_id") or (cfg.get("annotations") or {}).get(NAMES.key("move-id")) or ""))

    annotations = {"homestead.io/restored-from-backup": backup,
                   "homestead.io/source-volume": plan["source_volume"]}
    annotations.update({str(k): str(v) for k, v in (cfg.get("annotations") or {}).items()})
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "homestead",
                                "homestead.io/restored-volume": "true"},
                     "annotations": annotations},
        "spec": {"storageClassName": base_name, "accessModes": [access_mode],
                 "dataSource": {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": snapshot_name},
                 "volumeMode": volume_mode,
                 "resources": {"requests": {"storage": str(plan["volume_size_bytes"])}}},
    }
    try:
        out = LH.ksend("POST", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims", pvc)
    except Exception:
        # A job retrying this restore reuses its snapshot; with none to
        # follow (discard_on_failure), nothing else would ever remove it.
        if discard_on_failure:
            try:
                CSI_RESTORE.discard(namespace, pvc_name, snapshot_name)
            except Exception as error:
                print(f"Restore metadata cleanup deferred: {error}", flush=True)
        raise
    LH._bust("lhvols", "vol")
    return {**result, "created": True, "snapshot": snapshot_name,
            "uid": (out.get("metadata", {}) or {}).get("uid", ""),
            "message": f"Restore of {backup} into {namespace}/{pvc_name} started"}


def validate_restore_class(base, volume_mode="Filesystem", migration=False):
    parameters = base.get("parameters") or {}
    if base.get("provisioner") != "driver.longhorn.io" or any(parameters.get(k) for k in ("fromBackup", "backingImage")):
        raise ValueError("Choose a regular Longhorn storage class")
    if volume_mode == "Filesystem" and str(parameters.get("migratable", "false")).lower() == "true":
        raise ValueError("Filesystem volumes need a storage class with migratable disabled")
    if migration and base.get("volumeBindingMode") == "WaitForFirstConsumer":
        raise ValueError("Migrated volumes need an Immediate storage class so they can restore before the workload starts")


def restore_support():
    return CSI_RESTORE.support()


def ensure_restore_support():
    return CSI_RESTORE.ensure_support()


def restore_problem(pvc):
    return CSI_RESTORE.problem(pvc)


def cleanup_restore_snapshots():
    return CSI_RESTORE.cleanup()


def finish_restore_resize(pvc, cfg):
    return CSI_RESTORE.finish_resize(pvc, cfg)


# Last, so that importing any of these modules first works: homestead_longhorn
# imports this one.
import homestead_longhorn as LH  # noqa: E402
