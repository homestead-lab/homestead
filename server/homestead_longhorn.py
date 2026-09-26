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
import homestead_snapshot_delete as SNAPSHOT_DELETE
import hashlib
import math
import re
import secrets
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
KEEPING = ("snapshot", "snapshot-force-create", "snapshot-delete", "backup", "backup-force-create")
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
    retain = int(cfg.get("retain", 7) or 0)
    # A cleanup or a trim keeps nothing, so it has nothing to count.
    if cfg["task"] in KEEPING and retain < 1:
        raise ValueError("retain must be at least 1 for snapshot and backup jobs")
    if cfg["task"] not in KEEPING:
        retain = 0
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
        ksend("PUT", f"{API}/namespaces/{LHNS}/recurringjobs/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"{API}/namespaces/{LHNS}/recurringjobs", body)
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


SOURCE_LABEL = "recurring-job.longhorn.io/source"


def _patch_labels(volume, changes):
    """Set or clear recurring-job labels on a volume - and on its PVC, when
    the PVC is where Longhorn reads them from.

    A PVC labelled recurring-job.longhorn.io/source=enabled has its own
    labels copied onto the volume, so a change made to the volume alone was
    undone on Longhorn's next sync."""
    patch = {"metadata": {"labels": changes}}
    ksend("PATCH", f"{API}/namespaces/{LHNS}/volumes/{volume}", patch,
          ctype="application/merge-patch+json")
    try:
        status = kget(f"{API}/namespaces/{LHNS}/volumes/{volume}").get("status", {}) or {}
        ks = status.get("kubernetesStatus", {}) or {}
        if ks.get("pvcName") and ks.get("namespace"):
            path = f"/api/v1/namespaces/{ks['namespace']}/persistentvolumeclaims/{ks['pvcName']}"
            if ((kget(path).get("metadata", {}) or {}).get("labels", {}) or {}).get(SOURCE_LABEL) == "enabled":
                ksend("PATCH", path, patch, ctype="application/merge-patch+json")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


def assign(volume, name, kind="group", enabled=True):
    """Add or remove a job/group label on one volume."""
    key = (GROUP_LABEL if kind == "group" else JOB_LABEL) + name
    _patch_labels(volume, {key: "enabled" if enabled else None})
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


def _job_objects():
    try:
        return kget(f"{API}/namespaces/{LHNS}/recurringjobs").get("items", [])
    except Exception:
        return []


def _set_job_groups(job, wanted):
    """Save a recurring job with a new list of groups, the rest unchanged."""
    job = json.loads(json.dumps(job))
    job["spec"]["groups"] = wanted
    job["metadata"].pop("managedFields", None)
    ksend("PUT", f"{API}/namespaces/{LHNS}/recurringjobs/{job['metadata']['name']}", job)


def _only_default(labels):
    keys = [k for k in labels if k.startswith((JOB_LABEL, GROUP_LABEL)) and labels[k] == "enabled"]
    return keys == [GROUP_LABEL + "default"]


def save_group(cfg):
    """Make or change a group: its name, the volumes in it and the jobs that
    cover it, in one go.

    A group is nothing but a label on volumes and a name in jobs' lists, so
    one with neither is kept nowhere; it needs a volume or a job. Longhorn
    puts every volume without a job or group label in default, and leaves it
    there when it joins another group - so it would get both groups' plans.
    Joining here takes it out of default unless asked not to."""
    name = str(cfg.get("name") or "").strip()
    original = str(cfg.get("original") or "").strip() or name
    if not SAFE.match(name):
        raise ValueError("a group name is lowercase letters, numbers and dashes, up to 40")
    if "default" in (original, name) and original != name:
        raise ValueError("default is Longhorn's own group and keeps its name")
    wanted_volumes = set(cfg.get("volumes") or [])
    wanted_jobs = set(cfg.get("jobs") or [])
    if name != "default" and not wanted_volumes and not wanted_jobs:
        raise ValueError("choose a volume or a job: a group with neither is not kept anywhere")
    vols = {v["name"]: v for v in _volumes()}
    unknown = wanted_volumes - set(vols)
    if unknown:
        raise ValueError("no such volume: " + ", ".join(sorted(unknown)))
    jobs = {j["metadata"]["name"]: j for j in _job_objects()}
    unknown = wanted_jobs - set(jobs)
    if unknown:
        raise ValueError("no such job: " + ", ".join(sorted(unknown)))
    if original != name and name in groups():
        raise ValueError(f"there is already a group called {name}")
    old_key, key, default_key = GROUP_LABEL + original, GROUP_LABEL + name, GROUP_LABEL + "default"
    added, removed, left_default, back_to_default, kept = [], [], [], [], []
    for vol, v in vols.items():
        labels = v["labels"]
        has = labels.get(old_key) == "enabled"
        want = vol in wanted_volumes
        changes = {}
        if original != name and has:
            changes[old_key] = None
        if want and (not has or original != name):
            changes[key] = "enabled"
        if want and not has:
            added.append(vol)
            if name != "default" and cfg.get("leave_default", True) and labels.get(default_key) == "enabled":
                changes[default_key] = None
                left_default.append(vol)
        elif has and not want:
            if name == "default" and _only_default(labels):
                # Longhorn would put it straight back.
                kept.append(vol)
                continue
            changes[old_key] = None
            removed.append(vol)
            rest = {k for k, value in labels.items() if value == "enabled"
                    and k.startswith((JOB_LABEL, GROUP_LABEL)) and k != old_key}
            if not rest:
                back_to_default.append(vol)
        if changes:
            _patch_labels(vol, changes)
    for job_name, job in jobs.items():
        current = list((job.get("spec") or {}).get("groups") or [])
        has = original in current
        want = job_name in wanted_jobs
        if has and (not want or original != name):
            current = [g for g in current if g != original]
        if want and name not in current:
            current.append(name)
        if current != list((job.get("spec") or {}).get("groups") or []):
            _set_job_groups(job, current)
    _bust("lhvols", "lhjobs", "lhov")
    return {"ok": True, "name": name, "added": added, "removed": removed, "left_default": left_default,
            "back_to_default": back_to_default, "kept_in_default": kept}


def delete_group(name):
    """Take a group off every volume and out of every job.

    Snapshots and backups already taken are kept. A volume left with no
    group or job goes back to default, as Longhorn does; a job left with no
    group protects nothing until it is given one - both are said by name."""
    if name == "default":
        raise ValueError("default is Longhorn's own group; every volume without another comes back to it")
    if not SAFE.match(str(name or "")):
        raise ValueError("unknown group")
    key = GROUP_LABEL + name
    back, idle = [], []
    for v in _volumes():
        if v["labels"].get(key) != "enabled":
            continue
        rest = [k for k, value in v["labels"].items() if value == "enabled"
                and k.startswith((JOB_LABEL, GROUP_LABEL)) and k != key]
        _patch_labels(v["name"], {key: None})
        if not rest:
            back.append(v["pvc"] or v["name"])
    for job in _job_objects():
        current = list((job.get("spec") or {}).get("groups") or [])
        if name in current:
            left = [g for g in current if g != name]
            _set_job_groups(job, left)
            if not left:
                idle.append(job["metadata"]["name"])
    _bust("lhvols", "lhjobs", "lhov")
    return {"ok": True, "back_to_default": back, "idle_jobs": idle}


# ------------------------------------------------------------------ snapshots
def snapshots(volume=None):
    listing = kget(f"{API}/namespaces/{LHNS}/snapshots")
    if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
        raise ValueError("snapshot inventory is incomplete; refresh before changing recovery points")
    items = listing["items"]
    out = []
    for s in items:
        sp, st = s.get("spec", {}), s.get("status", {}) or {}
        vol = sp.get("volume", "")
        if volume and vol != volume:
            continue
        out.append({
            "name": s["metadata"]["name"], "volume": vol,
            "uid": s["metadata"].get("uid", ""),
            "created": st.get("creationTime", ""),
            "size_mb": round(int(st.get("size", 0) or 0) / 1048576, 1),
            "ready": bool(st.get("readyToUse")),
            "user_created": bool(st.get("userCreated")),
            "source": SNAPSHOT_DELETE.source(s),
            "removed": bool(st.get("markRemoved")),
            "deleting": bool(s["metadata"].get("deletionTimestamp")),
            "parent": st.get("parent", ""), "error": st.get("error", ""),
            "children": list((st.get("children") or {}).keys()),
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)


def create_snapshot(volume, name=None):
    # The second alone is not enough: a workload with two volumes has both
    # snapshotted in the same one, and the second name was refused.
    name = name or f"homestead-{int(time.time())}-{secrets.token_hex(3)}"
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
            "metadata": {"name": name, "namespace": LHNS,
                         "labels": {NAMES.key("managed"): "true"}},
            "spec": {"volume": volume, "createSnapshot": True}}
    ksend("POST", f"{API}/namespaces/{LHNS}/snapshots", body)
    _bust("lhsnaps")
    return {"ok": True, "snapshot": name, "volume": volume}


# ------------------------------------------------------------------ backups
def backup_target():
    try:
        items = kget(f"{API}/namespaces/{LHNS}/backuptargets").get("items", [])
    except Exception:
        return {"configured": False, "url": "", "available": False,
                "reason": "backuptargets CRD unreadable"}
    if not items:
        return {"configured": False, "url": "", "available": False, "reason": "none defined",
                "harvester": _harvester_setting() is not None}
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
        "secret_missing": bool(sp.get("credentialSecret")) and _get_or_none(
            f"/api/v1/namespaces/{LHNS}/secrets/{sp.get('credentialSecret')}") is None,
        # Set through Harvester's own setting there, which VM backups use too.
        "harvester": _harvester_setting() is not None,
    }


TARGET_SECRET = "homestead-backup-target"
SCHEMES = ("s3://", "nfs://", "cifs://", "azblob://")


def _target_secret(name, keys):
    """The Secret Longhorn reads S3 keys from, made from what was typed."""
    data = {"AWS_ACCESS_KEY_ID": keys["access_key"], "AWS_SECRET_ACCESS_KEY": keys["secret_key"]}
    if keys.get("endpoint"):
        data["AWS_ENDPOINTS"] = keys["endpoint"]
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": LHNS, "labels": {NAMES.key("managed"): "true"}},
            "stringData": data}
    path = f"/api/v1/namespaces/{LHNS}/secrets"
    current = _get_or_none(f"{path}/{name}")
    if current:
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        ksend("PUT", f"{path}/{name}", body)
    else:
        ksend("POST", path, body)


# On Harvester the backup target is Harvester's own setting: it writes
# Longhorn's from it, and VM backups use it too. Set in Longhorn alone, it is
# overwritten the next time Harvester applies its setting - blank, if that
# was never set.
HARVESTER_TARGET = "/apis/harvesterhci.io/v1beta1/settings/backup-target"


def _harvester_setting():
    try:
        return kget(HARVESTER_TARGET)
    except Exception:
        return None


def _seconds(poll):
    match = re.fullmatch(r"(\d+)\s*([smh]?)", str(poll or "5m").strip())
    if not match:
        return 300
    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def harvester_target_value(url, keys, poll, current=None):
    """Harvester's backup-target setting for a Longhorn-style URL: nfs://host:/path,
    or s3://bucket@region with the keys (kept from before when none are typed)."""
    current = current or {}
    value = {"refreshIntervalInSeconds": _seconds(poll)}
    if not url:
        return {}
    if url.startswith("nfs://"):
        return dict(value, type="nfs", endpoint=url)
    match = re.fullmatch(r"s3://([^@/]+)@([^/]+)/?", url)
    if not url.startswith("s3://"):
        raise ValueError("Harvester writes backups to NFS or S3: nfs://host:/path or s3://bucket@region")
    if not match:
        raise ValueError("on Harvester an S3 target is s3://bucket@region - Harvester writes to the top of the bucket, "
                         "so leave any path off")
    access = keys.get("access_key") or current.get("accessKeyId", "")
    secret = keys.get("secret_key") or current.get("secretAccessKey", "")
    if not (access and secret):
        raise ValueError("an S3 target needs its access key and secret key")
    return dict(value, type="s3", bucketName=match.group(1), bucketRegion=match.group(2),
                endpoint=keys.get("endpoint") if "endpoint" in keys else current.get("endpoint", ""),
                accessKeyId=access, secretAccessKey=secret,
                virtualHostedStyle=bool(current.get("virtualHostedStyle", False)))


def _set_harvester_target(setting, url, poll, keys):
    try:
        current = json.loads(setting.get("value") or "{}")
    except ValueError:
        current = {}
    if keys.get("endpoint") and not re.match(r"^https?://", keys["endpoint"]):
        raise ValueError("the endpoint is a URL, such as http://192.168.1.20:9000")
    value = harvester_target_value(url, keys, poll, current)
    setting["value"] = json.dumps(value) if value else ""
    setting.get("metadata", {}).pop("managedFields", None)
    ksend("PUT", f"{HARVESTER_TARGET}", setting)
    _bust("lhtarget")
    return {"ok": True, "url": url, "harvester": True}


def set_backup_target(url, secret="", poll="5m", keys=None):
    url, secret = str(url or "").strip(), str(secret or "").strip()
    if url and not url.startswith(SCHEMES):
        raise ValueError("a backup target starts with s3://, nfs://, cifs:// or azblob://")
    keys = {k: str(v or "").strip() for k, v in (keys or {}).items()}
    setting = _harvester_setting()
    if setting is not None:
        return _set_harvester_target(setting, url, poll, keys)
    if keys.get("access_key") or keys.get("secret_key"):
        if not (keys.get("access_key") and keys.get("secret_key")):
            raise ValueError("an S3 target needs both the access key and the secret key")
        if keys.get("endpoint") and not re.match(r"^https?://", keys["endpoint"]):
            raise ValueError("the endpoint is a URL, such as http://192.168.1.20:9000")
        secret = secret or TARGET_SECRET
        if not _valid_k8s_name(secret):
            raise ValueError("the secret name is lowercase letters, numbers, dots and dashes")
        _target_secret(secret, keys)
    elif url.startswith("s3://") and not secret:
        raise ValueError("an S3 target needs its keys, or the name of a Secret that holds them")
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


# Tasks that keep a copy of a volume; trims and cleanups tidy, and protect nothing.
PROTECTING = ("snapshot", "snapshot-force-create", "backup", "backup-force-create")


def overview():
    jobs, vols, tgt = list_jobs(), _volumes(), backup_target()
    by_volume = {}
    for j in jobs:
        if j["task"] in PROTECTING:
            for name in j["volumes"]:
                by_volume.setdefault(name, []).append(j)
    for v in vols:
        mine = by_volume.get(v["name"], [])
        v["protected_by"] = [j["name"] for j in mine]
        v["snapshotted"] = any(j["task"].startswith("snapshot") for j in mine)
        v["backed_up"] = any(j["task"].startswith("backup") for j in mine)
    names = groups()
    return {
        "jobs": jobs, "volumes": vols, "groups": names, "target": tgt,
        "tasks": TASKS,
        "group_rows": [{"name": g,
                        "volumes": [v["name"] for v in vols if g in v["groups"]],
                        "jobs": [j["name"] for j in jobs if g in j["groups"]]} for g in names],
        "protected": sum(1 for v in vols if v["protected_by"]),
        "backed_up": sum(1 for v in vols if v["backed_up"]),
        "unprotected": [v["pvc"] or v["name"] for v in vols if not v["protected_by"]],
        "total": len(vols),
    }


def backup_volumes():
    """Every volume that has backups, the deleted ones included.

    A backup outlives its volume - that is its point - so this lists what
    the backup target holds, not what the cluster still has."""
    try:
        items = kget(f"{API}/namespaces/{LHNS}/backupvolumes").get("items", [])
    except Exception:
        return []
    live = {v["name"]: v for v in _volumes()}
    counts = {}
    for b in backups():
        counts[b["volume"]] = counts.get(b["volume"], 0) + 1
    out = []
    for item in items:
        meta, status = item.get("metadata", {}) or {}, item.get("status", {}) or {}
        name = ((meta.get("labels") or {}).get("backup-volume") or (item.get("spec") or {}).get("volumeName")
                or status.get("volumeName") or meta.get("name", ""))
        volume = live.get(name)
        out.append({
            "name": name, "id": meta.get("name", ""),
            "pvc": (volume or {}).get("pvc", "") or _pvc_of_backup(status),
            "exists": volume is not None,
            "last_backup": status.get("lastBackupName", ""),
            "last_backup_at": status.get("lastBackupAt", ""),
            "size_mb": round(int(status.get("size", 0) or 0) / 1048576, 1),
            "count": counts.get(name, 0),
            "target": status.get("backupTargetName", "") or (item.get("spec") or {}).get("backupTargetName", "") or "default",
        })
    return sorted(out, key=lambda x: (x["exists"], x["name"]))


def _pvc_of_backup(status):
    """The claim a backed-up volume belonged to, as Longhorn wrote it down."""
    try:
        return (json.loads((status.get("labels") or {}).get("KubernetesStatus") or "{}") or {}).get("pvcName", "")
    except (TypeError, ValueError):
        return ""


def delete_backup(name):
    """Delete one backup - from the backup target too, which Longhorn does."""
    _backup(name)
    ksend("DELETE", f"{API}/namespaces/{LHNS}/backups/{name}")
    _bust("lhbackups", "lhbackupvols", "lhov")
    return {"ok": True, "backup": name}
