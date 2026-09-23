"""
Longhorn data protection: recurring jobs, groups, snapshots, backups.

Longhorn's own model, used as-is rather than reinvented:

* A **RecurringJob** has a task (snapshot / backup / trim / cleanup), a cron
  expression, a retain count, and a list of **groups**.
* A volume opts into a job by carrying a label:
      recurring-job.longhorn.io/<job>        = enabled
      recurring-job-group.longhorn.io/<grp>  = enabled
* The group named `default` is special: Longhorn puts every new volume in it,
  so a job in `default` protects everything without per-volume wiring.

That label model is what makes "groups" cheap here — assigning a volume is one
label patch, not a controller.
"""
import json
import homestead_names as NAMES
import hashlib
import math
import re
import time
import urllib.error

kget = ksend = None
LHNS = "longhorn-system"
API = "/apis/longhorn.io/v1beta2"
_cache = {}
STORAGE_CLASS = "longhorn-r2"

TASKS = {
    "snapshot": "Snapshot — point-in-time, stored on the volume",
    "snapshot-force-create": "Snapshot (force) — even if nothing changed",
    "snapshot-cleanup": "Cleanup — purge system snapshots",
    "snapshot-delete": "Delete — remove snapshots beyond retain",
    "backup": "Backup — snapshot then upload to the backup target",
    "backup-force-create": "Backup (force) — always upload",
    "filesystem-trim": "Trim — reclaim space the guest has freed",
}
JOB_LABEL = "recurring-job.longhorn.io/"
GROUP_LABEL = "recurring-job-group.longhorn.io/"
SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")


def bind(_kget, _ksend, _cache_ref, storage_class="longhorn-r2"):
    global kget, ksend, _cache, STORAGE_CLASS
    kget, ksend, _cache = _kget, _ksend, _cache_ref
    STORAGE_CLASS = storage_class


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


# ------------------------------------------------------------------ jobs
def list_jobs():
    try:
        items = kget(f"{API}/namespaces/{LHNS}/recurringjobs").get("items", [])
    except Exception:
        return []
    vols = _volumes()
    runs = _cron_runs()
    out = []
    for j in items:
        sp = j.get("spec", {})
        name = j["metadata"]["name"]
        run = runs.get(name, {})
        groups = sp.get("groups") or []
        covered = [v["name"] for v in vols
                   if v["labels"].get(JOB_LABEL + name) == "enabled"
                   or any(v["labels"].get(GROUP_LABEL + g) == "enabled" for g in groups)]
        out.append({
            "name": name, "task": sp.get("task", ""), "cron": sp.get("cron", ""),
            "retain": sp.get("retain", 0), "concurrency": sp.get("concurrency", 1),
            "groups": groups, "covers": len(covered), "volumes": sorted(covered),
            "desc": TASKS.get(sp.get("task", ""), ""),
            "last_run": run.get("last_run", ""), "last_success": run.get("last_success", ""),
            "running": run.get("running", 0), "last_failed": run.get("last_failed", False),
        })
    return sorted(out, key=lambda x: x["name"])


def _cron_runs():
    """When each job last ran, from the CronJob Longhorn keeps for it.

    Longhorn names the CronJob after its recurring job. A run that started
    after the last success and is no longer active failed."""
    try:
        crons = kget(f"/apis/batch/v1/namespaces/{LHNS}/cronjobs").get("items", [])
    except Exception:
        return {}
    runs = {}
    for cron in crons:
        status = cron.get("status", {}) or {}
        last, ok = status.get("lastScheduleTime", "") or "", status.get("lastSuccessfulTime", "") or ""
        active = len(status.get("active") or [])
        runs[cron["metadata"]["name"]] = {
            "last_run": last, "last_success": ok, "running": active,
            "last_failed": bool(last and not active and (not ok or ok < last)),
        }
    return runs


def run_job(name):
    """Runs a recurring job now, as the schedule would, from its CronJob."""
    if not SAFE.match(str(name or "")):
        raise ValueError("unknown job")
    try:
        cron = kget(f"/apis/batch/v1/namespaces/{LHNS}/cronjobs/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"Longhorn has not set up {name}'s schedule yet; try again in a moment")
        raise
    template = (cron.get("spec", {}) or {}).get("jobTemplate", {}) or {}
    job_name = f"{name[:40]}-now-{int(time.time()) % 1000000:06d}"
    meta = template.get("metadata", {}) or {}
    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job_name, "namespace": LHNS,
                     "labels": dict(meta.get("labels") or {}),
                     "annotations": {**(meta.get("annotations") or {}), "cronjob.kubernetes.io/instantiate": "manual"},
                     "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob", "name": name,
                                          "uid": cron["metadata"]["uid"], "controller": False}]},
        "spec": template.get("spec", {}),
    }
    ksend("POST", f"/apis/batch/v1/namespaces/{LHNS}/jobs", body)
    _bust("lhjobs")
    return {"ok": True, "job": job_name, "namespace": LHNS}


def run_status(item):
    """The operations poll's view of a job run started from Homestead."""
    ref = item["ref"]
    job = kget(f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs/{ref['name']}")
    status = job.get("status", {}) or {}
    for condition in status.get("conditions", []) or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return "failed", 100, condition.get("message") or condition.get("reason") or "The run failed"
    if status.get("succeeded"):
        return "succeeded", 100, "Finished"
    if status.get("failed") and not status.get("active"):
        return "failed", 100, "The run failed; its pod's log in longhorn-system says why"
    return "running", 40 if status.get("active") else 10, "Running" if status.get("active") else "Starting"


def save_job(cfg):
    name = cfg["name"]
    if not SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    if cfg.get("task") not in TASKS:
        raise ValueError(f"task must be one of: {', '.join(TASKS)}")
    retain = int(cfg.get("retain", 7))
    if cfg["task"].startswith(("snapshot", "backup")) and retain < 1:
        raise ValueError("retain must be at least 1 for snapshot and backup jobs")
    body = {
        "apiVersion": "longhorn.io/v1beta2", "kind": "RecurringJob",
        "metadata": {"name": name, "namespace": LHNS,
                     "labels": {NAMES.key("managed"): "true"}},
        "spec": {"name": name, "task": cfg["task"], "cron": cfg["cron"],
                 "retain": retain, "concurrency": int(cfg.get("concurrency", 1)),
                 "groups": cfg.get("groups") or [], "labels": cfg.get("labels") or {}},
    }
    try:
        cur = kget(f"{API}/namespaces/{LHNS}/recurringjobs/{name}")
        body["metadata"]["resourceVersion"] = cur["metadata"]["resourceVersion"]
        out = ksend("PUT", f"{API}/namespaces/{LHNS}/recurringjobs/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        out = ksend("POST", f"{API}/namespaces/{LHNS}/recurringjobs", body)
    _bust("lhjobs", "lhvols")
    return {"ok": True, "name": name}


def delete_job(name):
    ksend("DELETE", f"{API}/namespaces/{LHNS}/recurringjobs/{name}")
    _bust("lhjobs", "lhvols")
    return {"ok": True}


# ------------------------------------------------------------------ volumes & groups
def _volumes():
    try:
        items = kget(f"{API}/namespaces/{LHNS}/volumes").get("items", [])
    except Exception:
        return []
    out = []
    for v in items:
        md, st = v["metadata"], v.get("status", {}) or {}
        ks = st.get("kubernetesStatus", {}) or {}
        labels = md.get("labels", {}) or {}
        out.append({
            "name": md["name"],
            "pvc": ks.get("pvcName", ""),
            "namespace": ks.get("namespace", ""),
            "size_gb": round(int(v.get("spec", {}).get("size", 0) or 0) / 1024**3, 1),
            "robustness": st.get("robustness", ""),
            "state": st.get("state", ""),
            "labels": labels,
            "jobs": sorted(k[len(JOB_LABEL):] for k in labels if k.startswith(JOB_LABEL)),
            "groups": sorted(k[len(GROUP_LABEL):] for k in labels if k.startswith(GROUP_LABEL)),
            "last_backup": st.get("lastBackup", ""),
            "last_backup_at": st.get("lastBackupAt", ""),
        })
    return sorted(out, key=lambda x: x["pvc"] or x["name"])


def volumes():
    return _volumes()


def groups():
    """Every group referenced by a job or worn by a volume."""
    g = set()
    for j in list_jobs():
        g.update(j["groups"])
    for v in _volumes():
        g.update(v["groups"])
    g.add("default")
    return sorted(g)


def assign(volume, name, kind="group", enabled=True):
    """Add or remove a job/group label on one volume."""
    key = (GROUP_LABEL if kind == "group" else JOB_LABEL) + name
    patch = {"metadata": {"labels": {key: "enabled" if enabled else None}}}
    ksend("PATCH", f"{API}/namespaces/{LHNS}/volumes/{volume}", patch,
          ctype="application/merge-patch+json")
    _bust("lhvols", "lhjobs")
    return {"ok": True, "volume": volume, "label": key, "enabled": enabled}


def bulk_assign(volumes_list, name, kind="group", enabled=True):
    done, failed = [], []
    for v in volumes_list:
        try:
            assign(v, name, kind, enabled)
            done.append(v)
        except Exception as e:
            failed.append(f"{v}: {e}")
    return {"ok": True, "updated": done, "failed": failed}


# ------------------------------------------------------------------ snapshots
def snapshots(volume=None):
    try:
        items = kget(f"{API}/namespaces/{LHNS}/snapshots").get("items", [])
    except Exception:
        return []
    out = []
    for s in items:
        sp, st = s.get("spec", {}), s.get("status", {}) or {}
        vol = sp.get("volume", "")
        if volume and vol != volume:
            continue
        out.append({
            "name": s["metadata"]["name"], "volume": vol,
            "created": st.get("creationTime", ""),
            "size_mb": round(int(st.get("size", 0) or 0) / 1048576, 1),
            "ready": bool(st.get("readyToUse")),
            "user_created": bool(st.get("userCreated")),
            "children": list((st.get("children") or {}).keys()),
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)


def create_snapshot(volume, name=None):
    name = name or f"homestead-{int(time.time())}"
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
            "metadata": {"name": name, "namespace": LHNS,
                         "labels": {NAMES.key("managed"): "true"}},
            "spec": {"volume": volume, "createSnapshot": True}}
    out = ksend("POST", f"{API}/namespaces/{LHNS}/snapshots", body)
    _bust("lhsnaps")
    return {"ok": True, "snapshot": name, "volume": volume}


def delete_snapshot(name):
    ksend("DELETE", f"{API}/namespaces/{LHNS}/snapshots/{name}")
    _bust("lhsnaps")
    return {"ok": True}


# ------------------------------------------------------------------ backups
def backup_target():
    try:
        items = kget(f"{API}/namespaces/{LHNS}/backuptargets").get("items", [])
    except Exception:
        return {"configured": False, "url": "", "available": False,
                "reason": "backuptargets CRD unreadable"}
    if not items:
        return {"configured": False, "url": "", "available": False, "reason": "none defined"}
    t = items[0]
    sp, st = t.get("spec", {}), t.get("status", {}) or {}
    conds = {c["type"]: c for c in st.get("conditions", []) or []}
    avail = conds.get("Unavailable", {})
    return {
        "configured": bool(sp.get("backupTargetURL")),
        "name": t["metadata"]["name"],
        "url": sp.get("backupTargetURL", ""),
        "secret": sp.get("credentialSecret", ""),
        "interval": sp.get("pollInterval", ""),
        "available": avail.get("status") != "True",
        "reason": avail.get("message", "") or avail.get("reason", ""),
    }


def set_backup_target(url, secret="", poll="5m"):
    name = "default"
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "BackupTarget",
            "metadata": {"name": name, "namespace": LHNS},
            "spec": {"backupTargetURL": url, "credentialSecret": secret or "",
                     "pollInterval": poll}}
    try:
        cur = kget(f"{API}/namespaces/{LHNS}/backuptargets/{name}")
        body["metadata"]["resourceVersion"] = cur["metadata"]["resourceVersion"]
        ksend("PUT", f"{API}/namespaces/{LHNS}/backuptargets/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"{API}/namespaces/{LHNS}/backuptargets", body)
    _bust("lhtarget")
    return {"ok": True, "url": url}


def backups(volume=None):
    try:
        items = kget(f"{API}/namespaces/{LHNS}/backups").get("items", [])
    except Exception:
        return []
    out = []
    for b in items:
        st = b.get("status", {}) or {}
        labels = b["metadata"].get("labels", {}) or {}
        vol = labels.get("backup-volume", "") or st.get("volumeName", "")
        if volume and vol != volume:
            continue
        out.append({
            "name": b["metadata"]["name"], "volume": vol,
            "state": st.get("state", ""), "progress": st.get("progress", 0),
            "size_mb": round(int(st.get("size", 0) or 0) / 1048576, 1),
            "volume_size_gb": max(1, math.ceil(int(st.get("volumeSize", 0) or 0) / 1073741824)),
            "created": st.get("backupCreatedAt", ""),
            "error": st.get("error", ""),
            "target": st.get("backupTargetName", "default") or "default",
            "restorable": str(st.get("state", "")).lower() == "completed" and bool(st.get("url")),
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)


def create_backup(volume, name=None):
    tgt = backup_target()
    if not tgt.get("configured"):
        raise ValueError("no backup target configured — set one before backing up")
    snap = create_snapshot(volume, name)["snapshot"]
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": {"generateName": "homestead-", "namespace": LHNS,
                         "labels": {"backup-volume": volume, NAMES.key("managed"): "true"}},
            "spec": {"snapshotName": snap, "labels": {"homestead": "manual"}}}
    out = ksend("POST", f"{API}/namespaces/{LHNS}/backups", body)
    _bust("lhbackups")
    return {"ok": True, "backup": out.get("metadata", {}).get("name", ""),
            "snapshot": snap, "volume": volume}


K8S_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _valid_k8s_name(value):
    value = str(value or "")
    return bool(value) and len(value) <= 253 and all(
        K8S_LABEL.fullmatch(part) for part in value.split("."))


def _get_or_none(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _backup(name):
    if not _valid_k8s_name(name):
        raise ValueError("backup name is invalid")
    item = _get_or_none(f"{API}/namespaces/{LHNS}/backups/{name}")
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


def _restore_class_name(url, replicas, extra=""):
    # Extra settings only join the digest when present, so the classes earlier
    # restores created keep the names they already have.
    seed = f"{STORAGE_CLASS}\0{replicas}\0{url}" + (f"\0{extra}" if extra else "")
    digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return f"homestead-restore-{digest}"


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
        if _get_or_none(f"/api/v1/namespaces/{namespace}") is None:
            conflict = {"kind": "Namespace", "name": namespace,
                        "message": f"Namespace {namespace} does not exist"}
        elif pvc_name and _get_or_none(
                f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{pvc_name}") is not None:
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
    }


def restore_backup(cfg):
    backup = str(cfg.get("backup") or "").strip()
    namespace = str(cfg.get("namespace") or "").strip()
    pvc_name = str(cfg.get("name") or "").strip()
    plan = restore_plan(backup, namespace, pvc_name)
    if plan["conflict"]:
        raise ValueError(plan["conflict"]["message"])
    access_mode = str(cfg.get("access_mode") or "ReadWriteOnce")
    if access_mode not in ("ReadWriteOnce", "ReadWriteMany"):
        raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
    replicas = int(cfg.get("replicas", 2) or 2)
    if replicas < 1 or replicas > 5:
        raise ValueError("replicas must be between 1 and 5")
    size_gb = int(cfg.get("size_gb") or plan["minimum_size_gb"])
    if size_gb < plan["minimum_size_gb"]:
        raise ValueError(f"restored PVC cannot be smaller than {plan['minimum_size_gb']} GiB")

    item = _backup(backup)
    status, url, _ = _restore_source(item)
    base = kget(f"/apis/storage.k8s.io/v1/storageclasses/{STORAGE_CLASS}")
    if base.get("provisioner") != "driver.longhorn.io":
        raise ValueError(f"storage class {STORAGE_CLASS} is not managed by Longhorn")
    parameters = dict(base.get("parameters", {}) or {})
    volume_mode = str(cfg.get("volume_mode") or "Filesystem")
    if volume_mode not in ("Filesystem", "Block"):
        raise ValueError("volume mode must be Filesystem or Block")
    # A container's claim is a filesystem and never live-migrates; Harvester's
    # VM-oriented class may say migratable=true, which is valid only for block
    # volumes. A VM disk is one, and one built on a Harvester image needs that
    # image named, since its backup holds only what changed on top of it.
    migratable = bool(cfg.get("migratable")) and volume_mode == "Block"
    backing_image = str(cfg.get("backing_image") or "")
    parameters.update(fromBackup=url, numberOfReplicas=str(replicas),
                      migratable="true" if migratable else "false",
                      backupTargetName=status.get("backupTargetName", "default") or "default")
    if backing_image:
        parameters["backingImage"] = backing_image
    plain = (volume_mode, migratable, backing_image) == ("Filesystem", False, "")
    extra = "" if plain else f"{volume_mode}|{migratable}|{backing_image}"
    class_name = _restore_class_name(url, replicas, extra)
    storage_class = {
        "apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
        "metadata": {"name": class_name,
                     "labels": {"app.kubernetes.io/managed-by": "homestead",
                                "homestead.io/restore-class": "true"},
                     "annotations": {"homestead.io/source-backup": backup,
                                     "homestead.io/base-storage-class": STORAGE_CLASS}},
        "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True,
        "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate",
        "parameters": parameters,
    }
    if base.get("mountOptions"):
        storage_class["mountOptions"] = list(base["mountOptions"])
    if base.get("allowedTopologies"):
        storage_class["allowedTopologies"] = list(base["allowedTopologies"])
    existing_class = _get_or_none(f"/apis/storage.k8s.io/v1/storageclasses/{class_name}")
    if existing_class:
        if (existing_class.get("provisioner") != "driver.longhorn.io" or
                (existing_class.get("parameters", {}) or {}) != parameters or
                (existing_class.get("metadata", {}).get("labels", {}) or {}).get(
                    "app.kubernetes.io/managed-by") != "homestead"):
            raise ValueError(f"restore storage class {class_name} exists with different settings")
    else:
        ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", storage_class)

    annotations = {"homestead.io/restored-from-backup": backup,
                   "homestead.io/source-volume": plan["source_volume"]}
    annotations.update({str(k): str(v) for k, v in (cfg.get("annotations") or {}).items()})
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "homestead",
                                "homestead.io/restored-volume": "true"},
                     "annotations": annotations},
        "spec": {"storageClassName": class_name, "accessModes": [access_mode],
                 "volumeMode": volume_mode,
                 "resources": {"requests": {"storage": f"{size_gb}Gi"}}},
    }
    try:
        out = ksend("POST", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims", pvc)
    except Exception:
        # A class is safe to retain and reuse, but never hide a failed PVC create.
        raise
    _bust("lhvols", "vol")
    return {"ok": True, "backup": backup, "namespace": namespace, "name": pvc_name,
            "storage_class": class_name, "size_gb": size_gb,
            "access_mode": access_mode,
            "uid": (out.get("metadata", {}) or {}).get("uid", ""),
            "message": f"Restore of {backup} into {namespace}/{pvc_name} started"}


def overview():
    jobs, vols, tgt = list_jobs(), _volumes(), backup_target()
    protected = {v for j in jobs for v in j["volumes"]}
    return {
        "jobs": jobs, "volumes": vols, "groups": groups(), "target": tgt,
        "tasks": TASKS,
        "protected": len(protected),
        "unprotected": [v["pvc"] or v["name"] for v in vols
                        if (v["pvc"] or v["name"]) not in protected and v["name"] not in protected],
        "total": len(vols),
    }
