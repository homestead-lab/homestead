"""Impact-aware PersistentVolumeClaim deletion.

Deleting a PVC is deceptively small Kubernetes API call whose result depends on
the bound PV's reclaim policy.  This module makes that policy explicit and
re-checks every safety condition immediately before deletion.
"""
import re
import urllib.error


kget = ksend = snapshots = backups = None
cache = None
SYSTEM_NAMESPACES = set()
DEFAULT_NAMESPACE = "lab"
LONGHORN_NAMESPACE = "longhorn-system"


def bind(_kget, _ksend, _snapshots, _backups, _cache, system_namespaces, default_namespace):
    global kget, ksend, snapshots, backups, cache, SYSTEM_NAMESPACES, DEFAULT_NAMESPACE
    kget, ksend = _kget, _ksend
    snapshots, backups = _snapshots, _backups
    cache = _cache
    SYSTEM_NAMESPACES = set(system_namespaces or ())
    DEFAULT_NAMESPACE = default_namespace


def _identity(namespace, name):
    namespace = str(namespace or DEFAULT_NAMESPACE).strip()
    name = str(name or "").strip()
    dns = r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"
    if not re.fullmatch(dns, namespace) or not re.fullmatch(dns, name):
        raise ValueError("namespace and volume name must be valid Kubernetes names")
    return namespace, name


def _items(path, label, warnings, optional=False):
    try:
        return kget(path).get("items", []) or []
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return []
        warnings.append(f"{label} inventory unavailable (HTTP {error.code})")
    except Exception as error:
        warnings.append(f"{label} inventory unavailable: {error}")
    return []


def _claim_volume_names(spec, claim):
    return {row.get("name", "") for row in (spec.get("volumes") or [])
            if (row.get("persistentVolumeClaim") or {}).get("claimName") == claim}


def _mounts(spec, claim):
    volume_names = _claim_volume_names(spec, claim)
    if not volume_names:
        return []
    out = []
    for kind, rows in (("app", spec.get("containers") or []),
                       ("init", spec.get("initContainers") or [])):
        for container in rows:
            for mount in container.get("volumeMounts") or []:
                if mount.get("name") not in volume_names:
                    continue
                out.append({
                    "container": container.get("name", ""),
                    "container_kind": kind,
                    "path": mount.get("mountPath", ""),
                    "read_only": bool(mount.get("readOnly", False)),
                })
    return out


def _consumer(kind, obj, spec, claim, active, detail=""):
    if not _claim_volume_names(spec, claim):
        return None
    meta = obj.get("metadata", {}) or {}
    return {
        "kind": kind,
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", ""),
        "active": bool(active),
        "detail": detail,
        "mounts": _mounts(spec, claim),
    }


def _consumers(namespace, claim, warnings):
    result = []
    pods = _items(f"/api/v1/namespaces/{namespace}/pods", "pod", warnings)
    for pod in pods:
        spec = pod.get("spec", {}) or {}
        phase = (pod.get("status", {}) or {}).get("phase", "Pending")
        row = _consumer("Pod", pod, spec, claim, phase not in ("Succeeded", "Failed"),
                        f"{phase} on {spec.get('nodeName') or 'no node'}")
        if row:
            result.append(row)

    controllers = (
        ("Deployment", f"/apis/apps/v1/namespaces/{namespace}/deployments"),
        ("StatefulSet", f"/apis/apps/v1/namespaces/{namespace}/statefulsets"),
        ("DaemonSet", f"/apis/apps/v1/namespaces/{namespace}/daemonsets"),
    )
    for kind, path in controllers:
        for obj in _items(path, kind.lower(), warnings):
            desired = int((obj.get("spec", {}) or {}).get("replicas", 1) or 0)
            spec = (((obj.get("spec", {}) or {}).get("template") or {}).get("spec") or {})
            row = _consumer(kind, obj, spec, claim, desired > 0,
                            f"{desired} desired replica{'s' if desired != 1 else ''}")
            if row:
                result.append(row)

    # Jobs can hold PVC protection even after their owning UI has disappeared.
    for job in _items(f"/apis/batch/v1/namespaces/{namespace}/jobs", "job", warnings):
        spec = (((job.get("spec", {}) or {}).get("template") or {}).get("spec") or {})
        status = job.get("status", {}) or {}
        active = int(status.get("active", 0) or 0) > 0
        row = _consumer("Job", job, spec, claim, active,
                        "running" if active else "not running")
        if row:
            result.append(row)

    # KubeVirt is optional.  A VM may reference a claim while it is powered off;
    # that still matters because deleting the claim breaks the VM definition.
    vm_path = f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines"
    for vm in _items(vm_path, "virtual machine", warnings, optional=True):
        vm_spec = vm.get("spec", {}) or {}
        template_spec = (((vm_spec.get("template") or {}).get("spec") or {}))
        claims = []
        for volume in template_spec.get("volumes") or []:
            source = volume.get("persistentVolumeClaim") or {}
            if source.get("claimName") == claim:
                claims.append(volume.get("name", ""))
        if claims:
            running = bool(vm_spec.get("running")) or vm_spec.get("runStrategy") not in (None, "Halted")
            meta = vm.get("metadata", {}) or {}
            result.append({"kind": "VirtualMachine", "name": meta.get("name", ""),
                           "namespace": namespace, "active": running,
                           "detail": "running" if running else "powered off",
                           "mounts": [{"container": "virtual disk", "container_kind": "vm",
                                       "path": name, "read_only": False} for name in claims]})
    result.sort(key=lambda row: (not row["active"], row["kind"], row["name"]))
    return result


def deletion_plan(namespace, name):
    namespace, name = _identity(namespace, name)
    pvc = kget(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}")
    meta, spec, status = pvc.get("metadata", {}) or {}, pvc.get("spec", {}) or {}, pvc.get("status", {}) or {}
    warnings = []
    consumers = _consumers(namespace, name, warnings)
    dependency_inventory_complete = not warnings
    active_consumers = [row for row in consumers if row["active"]]

    pv_name = spec.get("volumeName", "")
    pv = {}
    pv_inventory_complete = True
    if pv_name:
        try:
            pv = kget(f"/api/v1/persistentvolumes/{pv_name}")
        except urllib.error.HTTPError as error:
            pv_inventory_complete = False
            warnings.append("the bound persistent volume no longer exists" if error.code == 404
                            else f"bound PV inventory unavailable (HTTP {error.code})")
        except Exception as error:
            pv_inventory_complete = False
            warnings.append(f"bound PV inventory unavailable: {error}")
    pv_spec = pv.get("spec", {}) or {}
    csi = pv_spec.get("csi", {}) or {}
    longhorn_name = csi.get("volumeHandle") or pv_name
    driver = csi.get("driver", "")

    longhorn = {}
    longhorn_inventory_complete = True
    if longhorn_name and (not driver or "longhorn" in driver):
        try:
            longhorn = kget(
                f"/apis/longhorn.io/v1beta2/namespaces/{LONGHORN_NAMESPACE}/volumes/{longhorn_name}")
        except urllib.error.HTTPError as error:
            longhorn_inventory_complete = False
            warnings.append("the Longhorn backing volume no longer exists" if error.code == 404
                            else f"Longhorn volume inventory unavailable (HTTP {error.code})")
        except Exception as error:
            longhorn_inventory_complete = False
            warnings.append(f"Longhorn volume inventory unavailable: {error}")
    lh_spec, lh_status = longhorn.get("spec", {}) or {}, longhorn.get("status", {}) or {}

    snapshot_rows = backup_rows = []
    inventory_complete = longhorn_inventory_complete
    if longhorn_name and longhorn:
        try:
            snapshot_rows = snapshots(longhorn_name) or []
        except Exception as error:
            inventory_complete = False
            warnings.append(f"snapshot inventory unavailable: {error}")
        try:
            backup_rows = backups(longhorn_name) or []
        except Exception as error:
            inventory_complete = False
            warnings.append(f"backup inventory unavailable: {error}")

    actual_bytes = int(lh_status.get("actualSize", 0) or 0)
    attachment_state = str(lh_status.get("state", "") or "").lower()
    attached_node = lh_status.get("currentNodeID", "") or ""
    protected = []
    labels = meta.get("labels", {}) or {}
    if namespace in SYSTEM_NAMESPACES:
        protected.append("system namespaces can only be changed with Kubernetes administration tools")
    if labels.get("app") == "harvui" or name == "harvui-data" or any(
            row["name"] == "harvui" for row in consumers):
        protected.append("this claim stores Homestead's own state")

    blockers = list(protected)
    if consumers:
        blockers.append(f"{len(consumers)} Kubernetes object reference(s) must be removed first")
    if attachment_state == "attached" or attached_node:
        blockers.append(f"Longhorn still reports the volume attached{f' to {attached_node}' if attached_node else ''}")
    if not dependency_inventory_complete:
        blockers.append("workload dependency inventory is incomplete")
    if not pv_inventory_complete:
        blockers.append("the bound persistent volume could not be verified")

    reclaim = pv_spec.get("persistentVolumeReclaimPolicy", "Unknown") if pv else "Unbound"
    data_present = actual_bytes > 0 or bool(snapshot_rows)
    return {
        "namespace": namespace,
        "name": name,
        "uid": meta.get("uid", ""),
        "resource_version": meta.get("resourceVersion", ""),
        "phase": status.get("phase", "Unknown"),
        "storage_class": spec.get("storageClassName", ""),
        "access_modes": spec.get("accessModes", []) or [],
        "requested_storage": ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
        "pv": {"name": pv_name, "reclaim_policy": reclaim, "driver": driver},
        "longhorn": {
            "name": longhorn_name if longhorn else "",
            "state": attachment_state or "unknown",
            "robustness": lh_status.get("robustness", "unknown"),
            "attached_node": attached_node,
            "replicas": int(lh_spec.get("numberOfReplicas", 0) or 0),
            "actual_bytes": actual_bytes,
            "actual_gb": round(actual_bytes / 1024 ** 3, 2),
        },
        "consumers": consumers,
        "active_consumers": len(active_consumers),
        "snapshots": {"count": len(snapshot_rows), "names": [x.get("name", "") for x in snapshot_rows[:20]]},
        "backups": {"count": len(backup_rows), "names": [x.get("name", "") for x in backup_rows[:20]]},
        "data_present": data_present,
        "inventory_complete": inventory_complete,
        "warnings": warnings,
        "blocked": bool(blockers),
        "blocking_reasons": blockers,
        "actions": {
            "detach": {"complete": not consumers and not attached_node and attachment_state != "attached",
                       "description": "Stop/unmount consumers while keeping the claim and all data."},
            "delete_claim": {"enabled": not blockers,
                             "description": "Delete the PVC but force the PV reclaim policy to Retain, preserving backing data for manual recovery."},
            "delete_data": {"enabled": not blockers and inventory_complete,
                            "description": "Force the PV reclaim policy to Delete, then delete the PVC and its backing volume data."},
        },
    }


def delete(cfg):
    namespace, name = _identity(cfg.get("namespace"), cfg.get("name"))
    action = cfg.get("action")
    if action not in ("delete_claim", "delete_data"):
        raise ValueError("action must be delete_claim or delete_data")
    plan = deletion_plan(namespace, name)
    if not cfg.get("uid") or cfg.get("uid") != plan["uid"]:
        raise ValueError("the volume changed after preview; review its impact again")
    if cfg.get("confirmation") != name:
        raise ValueError(f'type "{name}" exactly to confirm')
    if plan["blocking_reasons"]:
        raise PermissionError("volume deletion blocked: " + "; ".join(plan["blocking_reasons"]))
    if action == "delete_data" and not plan["inventory_complete"]:
        raise PermissionError("permanent deletion is blocked until snapshot and backup impact can be checked")

    pv_name = plan["pv"]["name"]
    desired_policy = "Retain" if action == "delete_claim" else "Delete"
    if pv_name and plan["pv"]["reclaim_policy"] != desired_policy:
        ksend("PATCH", f"/api/v1/persistentvolumes/{pv_name}",
              {"spec": {"persistentVolumeReclaimPolicy": desired_policy}},
              ctype="application/merge-patch+json")
    ksend("DELETE", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}", {
        "apiVersion": "v1", "kind": "DeleteOptions",
        "preconditions": {"uid": plan["uid"]},
        "propagationPolicy": "Foreground",
    })
    for key in list(cache or {}):
        if key.startswith(("vol", "stor", "flow", "lhov")):
            cache.pop(key, None)
    return {
        "ok": True, "namespace": namespace, "name": name, "action": action,
        "pv": pv_name, "longhorn_volume": plan["longhorn"]["name"],
        "message": "PVC deletion requested; backing data is retained" if action == "delete_claim"
                   else "PVC and backing-data deletion requested",
    }
