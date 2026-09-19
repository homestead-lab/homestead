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
import re
import time
import urllib.error

kget = ksend = None
LHNS = "longhorn-system"
API = "/apis/longhorn.io/v1beta2"
_cache = {}

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


def bind(_kget, _ksend, _cache_ref):
    global kget, ksend, _cache
    kget, ksend, _cache = _kget, _ksend, _cache_ref


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
    out = []
    for j in items:
        sp = j.get("spec", {})
        name = j["metadata"]["name"]
        groups = sp.get("groups") or []
        covered = [v["name"] for v in vols
                   if v["labels"].get(JOB_LABEL + name) == "enabled"
                   or any(v["labels"].get(GROUP_LABEL + g) == "enabled" for g in groups)]
        out.append({
            "name": name, "task": sp.get("task", ""), "cron": sp.get("cron", ""),
            "retain": sp.get("retain", 0), "concurrency": sp.get("concurrency", 1),
            "groups": groups, "covers": len(covered), "volumes": sorted(covered),
            "desc": TASKS.get(sp.get("task", ""), ""),
        })
    return sorted(out, key=lambda x: x["name"])


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
                     "labels": {"harvui.io/managed": "true"}},
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
    name = name or f"harvui-{int(time.time())}"
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
            "metadata": {"name": name, "namespace": LHNS,
                         "labels": {"harvui.io/managed": "true"}},
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
            "created": st.get("backupCreatedAt", ""),
            "error": st.get("error", ""),
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)


def create_backup(volume, name=None):
    tgt = backup_target()
    if not tgt.get("configured"):
        raise ValueError("no backup target configured — set one before backing up")
    snap = create_snapshot(volume, name)["snapshot"]
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": {"generateName": "harvui-", "namespace": LHNS,
                         "labels": {"backup-volume": volume, "harvui.io/managed": "true"}},
            "spec": {"snapshotName": snap, "labels": {"harvui": "manual"}}}
    out = ksend("POST", f"{API}/namespaces/{LHNS}/backups", body)
    _bust("lhbackups")
    return {"ok": True, "backup": out.get("metadata", {}).get("name", ""),
            "snapshot": snap, "volume": volume}


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
