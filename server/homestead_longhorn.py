"""
Longhorn data protection: recurring jobs, groups, snapshots, backups.

Longhorn's own model, used as-is rather than reinvented:

* A **RecurringJob** has a task (snapshot / backup / trim / cleanup), a cron
  expression, a retain count, and a list of **groups**.
* A volume opts into a job by carrying a label:
      recurring-job.longhorn.io/<job>        = enabled
      recurring-job-group.longhorn.io/<grp>  = enabled
* The group named `default` is special: Longhorn puts a volume in it while
  the volume has no other group or job, so a job in `default` protects the
  volumes nobody has placed yet - not every volume.

That label model is what makes "groups" cheap here — assigning a volume is one
label patch, not a controller.

Split by responsibility (#623): recurring jobs and the weekly trim live in
homestead_longhorn_jobs, volumes, groups and snapshots in
homestead_longhorn_volumes, the backup target in homestead_longhorn_target,
backups in homestead_longhorn_backups and restores in
homestead_longhorn_restore. This module keeps what bind() sets (kget, ksend,
_cache, STORAGE_CLASS, DEFAULT_NS: server.py sets STORAGE_CLASS here as it
finds the cluster's default class, and other modules read kget and
STORAGE_CLASS here), the helpers the parts share, the overview, the routes
and their handlers. Read and replace a moved name in the module that defines
it; this module offers only the few the move source and the move engine read
through the LH their bind() is given (see the end of this file).
"""
import json
import homestead_csi_restore as CSI_RESTORE
import re
import secrets
import urllib.error
import urllib.parse
import homestead_operations as OPS
import homestead_routes
import homestead_longhorn_jobs as JOBS
import homestead_longhorn_volumes as VOLUMES
import homestead_longhorn_target as BACKUP_TARGET
import homestead_longhorn_backups as BACKUPS
import homestead_longhorn_restore as RESTORE
import homestead_schedule_clashes as CLASHES

kget = ksend = None
LHNS = "longhorn-system"
API = "/apis/longhorn.io/v1beta2"
_cache = {}
STORAGE_CLASS = "longhorn-r2"
DEFAULT_NS = "lab"           # where a restore goes when none is named

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


def bind(_kget, _ksend, _cache_ref, storage_class="longhorn-r2", default_namespace=None):
    global kget, ksend, _cache, STORAGE_CLASS, DEFAULT_NS
    kget, ksend, _cache = _kget, _ksend, _cache_ref
    STORAGE_CLASS = storage_class
    DEFAULT_NS = default_namespace or DEFAULT_NS
    CSI_RESTORE.bind(_kget, _ksend)


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


JOB_CHANGED = "This job was changed or deleted elsewhere since you opened it; open it again to see it"


class _Changed(Exception):
    """A job's settings were saved elsewhere since the editor opened it."""


def _plain(obj):
    """A copy of an object as read, without what a write must not send back."""
    obj = json.loads(json.dumps(obj))
    obj.setdefault("metadata", {}).pop("managedFields", None)
    return obj


def _update(path, change, create=None, attempts=4):
    """Change the object at path as it is stored now, without undoing a change
    another writer made meanwhile (#621). change(obj) gets a copy as stored and
    returns it changed; the write names the resourceVersion read, so Kubernetes
    refuses it (409) if anything was saved in between, and then it is read and
    changed again. Without the object, create() is made, if given; one made
    meanwhile is a 409 too, and one deleted meanwhile is made next time round."""
    for _ in range(attempts):
        try:
            current = kget(path)
        except urllib.error.HTTPError as error:
            if error.code != 404 or create is None:
                raise
            error.close()
            current = None
        try:
            if current is None:
                return ksend("POST", path.rsplit("/", 1)[0], create())
            return ksend("PUT", path, change(_plain(current)))
        except urllib.error.HTTPError as error:
            if error.code != 409 and not (error.code == 404 and current is not None):
                raise
            error.close()
    raise RuntimeError(f"{path.rsplit('/', 1)[-1]} kept changing while it was being saved, "
                       "so this change was not saved; try again")


# Tasks that keep a copy of a volume; trims and cleanups tidy, and protect nothing.
PROTECTING = ("snapshot", "snapshot-force-create", "backup", "backup-force-create")


def overview():
    jobs, vols, tgt = JOBS.list_jobs(), VOLUMES._volumes(), BACKUP_TARGET.backup_target()
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
    names = VOLUMES.groups()
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
        # A trim or backup starting with a backup on a shared volume this week.
        "clashes": CLASHES.clashes(jobs),
    }


def run_job_now(b):
    """Run a recurring job once, followed as a job of its own."""
    result = JOBS.run_job(b.get("name"))
    result["operation"] = OPS.start(
        "protect-run", f"Run {b['name']} now", {"kind": "Job", "name": result["job"],
                                                "namespace": result["namespace"]},
        "/data-protection", {"namespace": result["namespace"], "name": result["job"]}, "Starting")
    return result


def backup_now(b):
    result = BACKUPS.create_backup(b["volume"], b.get("name"))
    if result.get("backup"):
        result["operation"] = OPS.start(
            "backup", f"Back up {b['volume']}",
            {"kind": "Volume", "name": b["volume"], "namespace": "longhorn-system"},
            "/data-protection", {"namespace": "longhorn-system", "name": result["backup"]})
    return result


def restore_reviewed(b):
    """Restore a backup as a job, or a 409 with the plan when what it would
    make is already there. Planned and started under the jobs' lock."""
    with OPS.transaction():
        plan = RESTORE.restore_plan(b.get("backup"), b.get("namespace", DEFAULT_NS), b.get("name"))
        if plan.get("conflict"):
            return homestead_routes.Reply(409, {"error": plan["conflict"]["message"], "plan": plan})
        b["restore_id"] = secrets.token_hex(12)
        b["annotations"] = {"homestead.io/restore-id": b["restore_id"]}
        result = RESTORE.restore_backup(b)
        b["source_size_bytes"] = result["source_size_bytes"]
        result["operation"] = OPS.start(
            "volume-restore", f"Restore {result['name']}",
            {"kind": "PersistentVolumeClaim", "name": result["name"],
             "namespace": result["namespace"]},
            "/volumes?" + urllib.parse.urlencode({"find": result["name"]}),
            {"namespace": result["namespace"], "name": result["name"],
             "backup": result["backup"], "restore_config": b,
             "restore_started": result["created"]}, result["message"])
    return result


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/lh/restore"): ("admin", lambda request: restore_reviewed(request.body)),
    ("POST", "/api/lh/job"): ("operator", lambda request: JOBS.save_job(request.body)),
    ("POST", "/api/lh/job/delete"): ("admin", lambda request: JOBS.delete_job(request.body["name"])),
    ("POST", "/api/lh/snapshot"): ("operator", lambda request: VOLUMES.create_snapshot(request.body["volume"], request.body.get("name"))),
    ("POST", "/api/lh/trim"): ("operator", lambda request: JOBS.trim_volume(request.body.get("volume"))),
    ("POST", "/api/lh/group"): ("operator", lambda request: VOLUMES.save_group(request.body)),
    ("POST", "/api/lh/group/delete"): ("admin", lambda request: VOLUMES.delete_group(request.body.get("name", ""))),
    ("POST", "/api/lh/backup/delete"): ("admin", lambda request: BACKUPS.delete_backup(request.body.get("name", ""))),
    ("GET", "/api/lh/overview"): ("viewer", lambda request: homestead_routes.cached("lhov", 8, overview)),
    ("GET", "/api/lh/backupvolumes"): ("viewer", lambda request: homestead_routes.cached("lhbackupvols", 15, BACKUPS.backup_volumes)),
    ("POST", "/api/lh/assign"): ("operator", lambda request: VOLUMES.bulk_assign(request.body["volumes"], request.body["name"], request.body.get("kind", "group"), request.body.get("enabled", True))),
    ("POST", "/api/lh/target"): ("admin", lambda request: BACKUP_TARGET.set_backup_target(request.body["url"], request.body.get("secret", ""), request.body.get("poll", "5m"), request.body.get("keys"))),
    ("GET", "/api/lh/snapshots"): ("viewer", lambda request: VOLUMES.snapshots((request.query.get("volume") or [None])[0])),
    ("GET", "/api/lh/backups"): ("viewer", lambda request: BACKUPS.backups((request.query.get("volume") or [None])[0])),
    ("GET", "/api/lh/restore/plan"): ("viewer", lambda request: RESTORE.restore_plan(
        (request.query.get("backup") or [""])[0], (request.query.get("ns") or [DEFAULT_NS])[0],
        (request.query.get("name") or [""])[0])),
    ("POST", "/api/lh/job/run"): ("operator", lambda request: run_job_now(request.body)),
    ("POST", "/api/lh/backup"): ("operator", lambda request: backup_now(request.body)),
}


def register(ops):
    """The job store's entry for a protection run (#622)."""
    ops.RESOLVERS["protect-run"] = JOBS.run_status


# What the move source and the move engine read through this module, which
# server.py hands them in bind() (their tests hand in a fake). Everything else
# is read, and replaced in tests, where it is defined (#623).
from homestead_longhorn_target import (  # noqa: E402,F401
    backup_target, backup_target_state, transfer_target_state, replace_backup_target_state,
)
from homestead_longhorn_backups import backups, ensure_move_backup  # noqa: E402,F401
from homestead_longhorn_restore import (  # noqa: E402,F401
    restore_backup, validate_restore_class, restore_support, ensure_restore_support, restore_problem,
)
