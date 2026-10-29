"""Impact-aware PersistentVolumeClaim deletion.

Deleting a PVC is deceptively small Kubernetes API call whose result depends on
the bound PV's reclaim policy.  This module makes that policy explicit and
re-checks every safety condition immediately before deletion.
"""
import re
import math
import time
import urllib.error

import homestead_kube as KUBE
import homestead_operations as OPS
import homestead_storage_guard as STORAGE_GUARD


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
    # Retry a throttled inventory read once. Never retry deletion writes or
    # turn a failed/partial inventory into permission to remove data.
    for attempt in range(2):
        try:
            listing = kget(path)
            if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
                raise ValueError("incomplete inventory")
            return listing["items"]
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt == 0:
                try:
                    delay = float((error.headers or {}).get("Retry-After", "0.5"))
                    if not math.isfinite(delay) or delay < 0:
                        delay = 0.5
                except (TypeError, ValueError):
                    delay = 0.5
                error.close()
                time.sleep(min(2.0, delay))
                continue
            if optional and error.code == 404:
                return []
            warnings.append(f"{label} inventory unavailable (HTTP {error.code})" +
                            ("; temporarily rate limited, refresh the review shortly" if error.code == 429 else ""))
        except Exception as error:
            warnings.append(f"{label} inventory unavailable: {error}")
        return []


def _claim_volume_names(spec, claim):
    return {row.get("name", "") for row in (spec.get("volumes") or [])
            if (row.get("persistentVolumeClaim") or {}).get("claimName") == claim}


def _template_claim_names(obj, claim):
    """A StatefulSet's own claims come from its volumeClaimTemplates, named
    <template>-<set>-<n>, and are absent from its pod template's volumes;
    containers mount them by the template's name. Retained ordinals beyond
    the current scale count too: scaling back up reuses them."""
    name = (obj.get("metadata") or {}).get("name", "")
    return {template.get("metadata", {}).get("name", "")
            for template in ((obj.get("spec") or {}).get("volumeClaimTemplates") or [])
            if re.fullmatch(re.escape(f"{(template.get('metadata') or {}).get('name', '')}-{name}") + r"-\d+", claim)}


def _mounts(spec, claim, templated=frozenset()):
    volume_names = _claim_volume_names(spec, claim) | set(templated)
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


def _consumer(kind, obj, spec, claim, active, detail="", templated=frozenset()):
    if not _claim_volume_names(spec, claim) and not templated:
        return None
    meta = obj.get("metadata", {}) or {}
    return {
        "kind": kind,
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", ""),
        "active": bool(active),
        "detail": detail,
        "mounts": _mounts(spec, claim, templated),
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
                            f"{desired} desired instance{'s' if desired != 1 else ''}",
                            _template_claim_names(obj, claim) if kind == "StatefulSet" else frozenset())
            if row:
                result.append(row)

    # A CronJob between runs has no pod; only its template says it needs the
    # claim, and its next run would find it gone.
    for job in _items(f"/apis/batch/v1/namespaces/{namespace}/cronjobs", "cronjob", warnings):
        template = ((job.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}
        row = _consumer("CronJob", job, (template.get("template") or {}).get("spec") or {}, claim, False,
                        "suspended" if (job.get("spec") or {}).get("suspend") else "runs on a schedule")
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
            # A disk is given either as the claim itself or as the
            # DataVolume that made it, which has the claim's name.
            if claim in ((volume.get("persistentVolumeClaim") or {}).get("claimName"),
                         (volume.get("dataVolume") or {}).get("name")):
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


def deletion_plan(namespace, name, volume="", pv=""):
    namespace, name = _identity(namespace, name)
    if pv:
        return released_plan(namespace, name, volume, pv)
    try:
        pvc = kget(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404 and volume:
            return orphan_plan(namespace, name, volume)
        raise
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
    if volume and longhorn_name != volume:
        raise ValueError("This name now belongs to a different backing volume; its claim will not be deleted")

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
    # Protected while Homestead's Deployment refers to it - not by name: after
    # its data moves to another class, the old homestead-data claim is a
    # copy nothing uses, kept to be deleted once the move has proved itself.
    # Only when what refers to claims could not be read does the name stand in.
    own_state = (any(row["name"] == "homestead" for row in consumers)
                 or (not dependency_inventory_complete and (labels.get("app") == "homestead" or name == "homestead-data")))
    if own_state:
        protected.append("this claim stores Homestead's own state")

    blockers = list(protected)
    # A Job that finished, or the Pod it left behind, references the claim but
    # will never run again - the copy job from an import is the usual case, and
    # it used to block deleting the very volume it had just filled. A stopped
    # controller is different: it starts again one day and would find no claim,
    # so it still blocks.
    stale = [row for row in consumers
             if row["kind"] in ("Job", "Pod") and not row.get("active")]
    lingering = [row for row in consumers if row not in stale]
    if lingering:
        blockers.append(f"{len(lingering)} Kubernetes object reference(s) must be removed first")
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
        # Homestead made these and they have finished, so it can tidy them up
        # as part of the deletion instead of asking for manual kubectl work.
        "stale_consumers": stale,
        "removable_jobs": [row["name"] for row in stale if row["kind"] == "Job"
                           and row["name"].startswith("homestead-")],
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


def delete(cfg, validate=None):
    namespace, name = _identity(cfg.get("namespace"), cfg.get("name"))
    action = cfg.get("action")
    if action not in ("delete_claim", "delete_data"):
        raise ValueError("action must be delete_claim or delete_data")
    plan = deletion_plan(namespace, name, cfg.get("volume", ""), cfg.get("pv", ""))
    if validate is not None:
        plan = validate(plan)
    if not cfg.get("uid") or cfg.get("uid") != plan["uid"]:
        raise ValueError("the volume changed after preview; review its impact again")
    # A Released volume whose claim name is in use again is confirmed by its
    # own name: the claim name is the current volume's too.
    expected = plan.get("confirm") or name
    if cfg.get("confirmation") != expected:
        raise ValueError(f'type "{expected}" exactly to confirm')
    if plan["blocking_reasons"]:
        raise PermissionError("volume deletion blocked: " + "; ".join(plan["blocking_reasons"]))
    if action == "delete_data" and not plan["inventory_complete"]:
        raise PermissionError("permanent deletion is blocked until snapshot and backup impact can be checked")
    if plan.get("orphan"):
        return delete_orphan(plan, cfg)

    # Clear Homestead's own finished Jobs first: while one exists, Kubernetes
    # can hold the PVC in Terminating even though the Job stopped long ago.
    for job in plan.get("removable_jobs") or []:
        try:
            ksend("DELETE", f"/apis/batch/v1/namespaces/{namespace}/jobs/{job}"
                            "?propagationPolicy=Background")
        except Exception:
            pass

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


def orphan_plan(namespace, name, volume):
    """Explicit retained backing volume, never guess a PVC from a stale name."""
    _, volume = _identity(namespace, volume)
    path = f"/apis/longhorn.io/v1beta2/namespaces/{LONGHORN_NAMESPACE}"
    lh = kget(f"{path}/volumes/{volume}")
    meta, spec, status = lh.get("metadata") or {}, lh.get("spec") or {}, lh.get("status") or {}
    ks = status.get("kubernetesStatus") or {}
    if ks.get("namespace") not in (None, "", namespace) or ks.get("pvcName") not in (None, "", name):
        raise ValueError("Backing volume identity does not match the selected former claim")
    warnings, blockers = [], []
    # Stopped CronJobs included; an old claim name must not be broken by cleanup.
    consumers = _consumers(namespace, name, warnings)
    if consumers:
        blockers.append("Remove remaining workload references to the former claim first")
    if namespace in SYSTEM_NAMESPACES:
        blockers.append("System namespace backing data requires Kubernetes administration tools")
    if status.get("state") != "detached" or status.get("currentNodeID") or spec.get("nodeID") or spec.get("migrationNodeID"):
        blockers.append("Longhorn must be fully detached with no requested attachment")
    pvs = [p for p in _items("/api/v1/persistentvolumes", "persistent volume", warnings)
           if ((p.get("spec") or {}).get("csi") or {}).get("driver") == "driver.longhorn.io"
           and ((p.get("spec") or {}).get("csi") or {}).get("volumeHandle") == volume]
    if len(pvs) > 1:
        blockers.append("Multiple PVs refer to this Longhorn volume; resolve ownership first")
    pv = pvs[0] if len(pvs) == 1 else {}
    pm, ps = pv.get("metadata") or {}, pv.get("spec") or {}
    if pv and (pv.get("status") or {}).get("phase") != "Released":
        blockers.append("The backing PV is not Released; it may be bound or available for reuse")
    claim_ref = ps.get("claimRef") or {}
    if pv and (claim_ref.get("name") != name or claim_ref.get("namespace") != namespace):
        blockers.append("The PV claim reference differs from the selected former claim")
    for claim in _items("/api/v1/persistentvolumeclaims", "claim", warnings):
        cm, cs = claim.get("metadata") or {}, claim.get("spec") or {}
        if (cm.get("namespace"), cm.get("name")) == (namespace, name) or cs.get("volumeName") in {p["metadata"]["name"] for p in pvs}:
            blockers.append("A PVC now references this name or backing PV; refresh before deleting")
    for attachment in _items("/apis/storage.k8s.io/v1/volumeattachments", "CSI attachment", warnings):
        if ((attachment.get("spec") or {}).get("source") or {}).get("persistentVolumeName") in {p["metadata"]["name"] for p in pvs}:
            blockers.append("A CSI attachment still references this backing PV")
    for attachment in _items(f"{path}/volumeattachments", "Longhorn attachment", warnings):
        a = attachment.get("spec") or {}
        if a.get("volume") == volume and a.get("attachmentTickets"):
            blockers.append("Longhorn attachment tickets still request this volume")
    snaps = [s for s in _items(f"{path}/snapshots", "snapshot", warnings) if (s.get("spec") or {}).get("volume") == volume]
    bks = [b for b in _items(f"{path}/backups", "backup", warnings)
           if ((b.get("metadata") or {}).get("labels") or {}).get("backup-volume", (b.get("status") or {}).get("volumeName")) == volume]
    if warnings:
        blockers.append("Impact inventory is incomplete")
    if not meta.get("uid") or not meta.get("resourceVersion") or (pv and (not pm.get("uid") or not pm.get("resourceVersion"))):
        blockers.append("Backing object identity/version is unavailable")
    return {"orphan": True, "namespace": namespace, "name": name, "uid": meta.get("uid", ""),
            "resource_version": meta.get("resourceVersion", ""), "phase": "PVC already absent",
            "requested_storage": "", "consumers": consumers, "warnings": warnings,
            "snapshots": {"count": len(snaps)}, "backups": {"count": len(bks)},
            "inventory_complete": not warnings, "blocked": bool(blockers), "blocking_reasons": blockers,
            "pv": {"name": pm.get("name", ""), "uid": pm.get("uid", ""), "resource_version": pm.get("resourceVersion", ""),
                   "reclaim_policy": ps.get("persistentVolumeReclaimPolicy", "No PV")},
            "longhorn": {"name": volume, "state": status.get("state"), "attached_node": status.get("currentNodeID", ""),
                         "replicas": spec.get("numberOfReplicas", 0), "actual_gb": round(int(status.get("actualSize") or 0) / 1024**3, 2)},
            "actions": {"detach": {"complete": not blockers}, "delete_claim": {"enabled": False},
                        "delete_data": {"enabled": not blockers}}}


def _released_identity(pv_name, namespace, name, volume):
    """The Released PV asked for, read by name: it must be Longhorn's volume
    `volume`, last claimed as namespace/name. Anything else is a different
    volume than the one reviewed, and is refused outright."""
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", str(pv_name or "")):
        raise ValueError("the persistent volume name must be a valid Kubernetes name")
    pv = KUBE.get_optional(f"/api/v1/persistentvolumes/{pv_name}", read=kget)
    if pv is None:
        raise ValueError("That Released volume no longer exists; refresh the list")
    spec = pv.get("spec") or {}
    csi, ref = spec.get("csi") or {}, spec.get("claimRef") or {}
    if csi.get("driver") != "driver.longhorn.io" or csi.get("volumeHandle") != volume:
        raise ValueError("This persistent volume is not that Longhorn volume's; nothing will be deleted")
    if (ref.get("namespace"), ref.get("name")) != (namespace, name):
        raise ValueError("This persistent volume was not that claim's; nothing will be deleted")
    return pv


def released_plan(namespace, name, volume, pv_name):
    """A Released Longhorn volume asked for by its PV (#932 follow-up). When
    a claim of its old name exists again, that claim and its volume are the
    app's now: the review names both, and refuses unless the Released one is
    plainly another volume (another PV, PV UID, claim UID and Longhorn volume).
    With no claim of that name, the orphan review answers, for this PV only."""
    _, volume = _identity(namespace, volume)
    pv = _released_identity(pv_name, namespace, name, volume)
    pm, ps, pstatus = pv.get("metadata") or {}, pv.get("spec") or {}, pv.get("status") or {}
    claim = KUBE.get_optional(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}", read=kget)
    if claim is None:
        plan = orphan_plan(namespace, name, volume)
        if plan["pv"]["name"] != pv_name:
            raise ValueError("Another persistent volume holds that Longhorn volume; nothing will be deleted")
        return plan
    warnings, blockers = [], []
    cm, cs, cstatus = claim.get("metadata") or {}, claim.get("spec") or {}, claim.get("status") or {}
    current_pv_name = cs.get("volumeName") or ""
    current = {"claim_uid": cm.get("uid", ""), "phase": cstatus.get("phase", "Unknown"), "pv": current_pv_name,
               "pv_uid": "", "longhorn": "", "consumers": []}
    if pstatus.get("phase") != "Released":
        blockers.append("This persistent volume is not Released; it may be bound or available for reuse")
    if current["phase"] != "Bound" or not current_pv_name:
        blockers.append(f"The claim now named {name} is not bound yet, so the volume it uses cannot be shown; refresh once it is")
    if current_pv_name == pv_name:
        blockers.append("This is the volume the claim uses now")
    if not (ps.get("claimRef") or {}).get("uid") or (ps.get("claimRef") or {}).get("uid") == current["claim_uid"]:
        blockers.append("This persistent volume's claim reference is the current claim's")
    if current_pv_name and current_pv_name != pv_name:
        try:
            now = kget(f"/api/v1/persistentvolumes/{current_pv_name}")
            current["pv_uid"] = (now.get("metadata") or {}).get("uid", "")
            current["longhorn"] = ((now.get("spec") or {}).get("csi") or {}).get("volumeHandle", "")
        except Exception as error:
            warnings.append(f"the volume the claim uses now could not be read: {error}")
        if current["pv_uid"] and current["pv_uid"] == pm.get("uid"):
            blockers.append("This is the volume the claim uses now")
        if current["longhorn"] == volume:
            blockers.append("The claim uses this Longhorn volume now")
    # The app mounts the claim name, which is the volume it uses now: shown
    # beside the review, never a reason to keep the Released one.
    current["consumers"] = _consumers(namespace, name, warnings)

    path = f"/apis/longhorn.io/v1beta2/namespaces/{LONGHORN_NAMESPACE}"
    lh = KUBE.get_optional(f"{path}/volumes/{volume}", read=kget)
    if lh is None:
        raise ValueError("Longhorn no longer has that volume; refresh the list")
    meta, spec, status = lh.get("metadata") or {}, lh.get("spec") or {}, lh.get("status") or {}
    ks = status.get("kubernetesStatus") or {}
    if ks.get("pvName") not in (None, "", pv_name):
        blockers.append("Longhorn last bound this volume to another persistent volume")
    if ks.get("namespace") not in (None, "", namespace) or ks.get("pvcName") not in (None, "", name):
        blockers.append("Longhorn last bound this volume to another claim")
    if status.get("state") != "detached" or status.get("currentNodeID") or spec.get("nodeID") or spec.get("migrationNodeID"):
        blockers.append("Longhorn must be fully detached with no requested attachment")
    pvs = [p for p in _items("/api/v1/persistentvolumes", "persistent volume", warnings)
           if ((p.get("spec") or {}).get("csi") or {}).get("volumeHandle") == volume]
    if len(pvs) > 1:
        blockers.append("Multiple PVs refer to this Longhorn volume; resolve ownership first")
    for other in _items("/api/v1/persistentvolumeclaims", "claim", warnings):
        if (other.get("spec") or {}).get("volumeName") == pv_name:
            blockers.append("A claim is bound to this persistent volume")
    for attachment in _items("/apis/storage.k8s.io/v1/volumeattachments", "CSI attachment", warnings):
        if ((attachment.get("spec") or {}).get("source") or {}).get("persistentVolumeName") == pv_name:
            blockers.append("A CSI attachment still references this persistent volume")
    for attachment in _items(f"{path}/volumeattachments", "Longhorn attachment", warnings):
        a = attachment.get("spec") or {}
        if a.get("volume") == volume and a.get("attachmentTickets"):
            blockers.append("Longhorn attachment tickets still request this volume")
    snaps = [s for s in _items(f"{path}/snapshots", "snapshot", warnings) if (s.get("spec") or {}).get("volume") == volume]
    bks = [b for b in _items(f"{path}/backups", "backup", warnings)
           if ((b.get("metadata") or {}).get("labels") or {}).get("backup-volume", (b.get("status") or {}).get("volumeName")) == volume]
    if namespace in SYSTEM_NAMESPACES:
        blockers.append("System namespace backing data requires Kubernetes administration tools")
    if warnings:
        blockers.append("Impact inventory is incomplete")
    if not meta.get("uid") or not pm.get("uid") or not pm.get("resourceVersion"):
        blockers.append("Backing object identity/version is unavailable")
    blockers = list(dict.fromkeys(blockers))
    return {"orphan": True, "released": True, "namespace": namespace, "name": name, "confirm": pv_name,
            "uid": meta.get("uid", ""), "resource_version": meta.get("resourceVersion", ""),
            "phase": "Claim name in use again", "requested_storage": (ps.get("capacity") or {}).get("storage", ""),
            "consumers": [], "current": current, "warnings": warnings,
            "snapshots": {"count": len(snaps)}, "backups": {"count": len(bks)},
            "inventory_complete": not warnings, "blocked": bool(blockers), "blocking_reasons": blockers,
            "pv": {"name": pv_name, "uid": pm.get("uid", ""), "resource_version": pm.get("resourceVersion", ""),
                   "phase": pstatus.get("phase", ""), "claim_uid": (ps.get("claimRef") or {}).get("uid", ""),
                   "longhorn": volume, "reclaim_policy": ps.get("persistentVolumeReclaimPolicy", "")},
            "longhorn": {"name": volume, "state": status.get("state"), "attached_node": status.get("currentNodeID", ""),
                         "replicas": spec.get("numberOfReplicas", 0),
                         "actual_gb": round(int(status.get("actualSize") or 0) / 1024**3, 2)},
            "actions": {"detach": {"complete": not blockers}, "delete_claim": {"enabled": False},
                        "delete_data": {"enabled": not blockers}}}


def delete_orphan(plan, cfg):
    if cfg.get("action") != "delete_data":
        raise ValueError("The PVC is already absent; only explicit backing-data deletion applies")
    pv = plan["pv"]
    if cfg.get("pv_uid", "") != pv.get("uid", ""):
        raise ValueError("Backing PV changed after preview; review again")
    pinned = []
    if plan.get("released"):
        # The claim name is in use again: write only the old PV, and never one
        # the current claim is bound to, whatever the plan said.
        current = plan.get("current") or {}
        if not pv["name"] or pv["name"] == current.get("pv") or pv.get("uid") == current.get("pv_uid") \
                or plan["longhorn"]["name"] == current.get("longhorn") or not pv.get("claim_uid"):
            raise PermissionError("This is the volume the claim uses now; it is not deleted")
        pinned = [{"op": "test", "path": "/spec/claimRef/uid", "value": pv["claim_uid"]},
                  {"op": "test", "path": "/spec/csi/volumeHandle", "value": plan["longhorn"]["name"]}]
    if pv["name"]:
        # A Released PV with Delete policy is reclaimed by CSI, with its own
        # finalizers. Test the complete observed revision before changing policy.
        ksend("PATCH", f"/api/v1/persistentvolumes/{pv['name']}", [
            {"op": "test", "path": "/metadata/uid", "value": pv["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": pv["resource_version"]},
            {"op": "test", "path": "/status/phase", "value": "Released"}, *pinned,
            {"op": "add", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Delete"}],
              ctype="application/json-patch+json")
    else:
        ksend("DELETE", f"/apis/longhorn.io/v1beta2/namespaces/{LONGHORN_NAMESPACE}/volumes/{plan['longhorn']['name']}",
              {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
                  "uid": plan["uid"], "resourceVersion": plan["resource_version"]}})
    for key in list(cache or {}):
        if key.startswith(("vol", "stor", "flow", "lhov")):
            cache.pop(key, None)
    return {"ok": True, "orphan": True, "namespace": plan["namespace"], "name": plan["name"],
            "action": "delete_data", "pv": pv["name"], "longhorn_volume": plan["longhorn"]["name"], "released": bool(plan.get("released")),
            "message": "Retained backing-data deletion requested; external backups are kept"}


def _review(plan):
    """A deletion plan with what Homestead's own storage jobs hold."""
    return STORAGE_GUARD.review(plan, OPS, kget)


def delete_reviewed(cfg):
    """Delete as reviewed, recorded as a job, under the jobs' lock so no
    storage job starts on the volume meanwhile."""
    with OPS.transaction():
        result = delete(cfg, validate=_review)
        if result.get("released"):
            # The claim name is the current volume's: the job is the PV's
            # alone, so nothing fences or names the claim in use.
            result["operation"] = OPS.start(
                "volume-delete", f"Delete Released volume {result['pv']}",
                {"kind": "PersistentVolume", "name": result["pv"]},
                "/volumes", {"namespace": result["namespace"], "name": result["pv"], "claim": result["name"],
                              "released": True, "action": result["action"], "pv": result["pv"], "orphan": True,
                              "volume": result["longhorn_volume"]}, result["message"])
            return result
        result["operation"] = OPS.start(
            "volume-delete", f"Delete volume {result['name']}",
            {"kind": "PersistentVolumeClaim", "name": result["name"],
             "namespace": result["namespace"]},
            "/volumes", {"namespace": result["namespace"], "name": result["name"],
                          "action": result["action"], "pv": result["pv"],
                          "orphan": result.get("orphan", False),
                          "volume": result["longhorn_volume"]}, result["message"])
    return result


def _q(request, name, default=""):
    return (request.query.get(name) or [default])[0]


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/volumes/delete-plan"): ("viewer", lambda request: _review(deletion_plan(
        _q(request, "ns", DEFAULT_NAMESPACE), _q(request, "name"), _q(request, "volume"), _q(request, "pv")))),
    ("POST", "/api/volumes/delete"): ("admin", lambda request: delete_reviewed(request.body)),
}
