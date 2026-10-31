"""
Longhorn's recurring jobs: listing them with when each last ran, saving,
deleting and running one now, the name and schedule of the trim job earlier
releases made, and trimming a single volume now.

Split out of homestead_longhorn (#623); what bind() sets, the shared helpers
and the routes stay there.
"""
import json
import homestead_longhorn_access as ACCESS
import homestead_names as NAMES
import homestead_self as SELF
import re
import time
import urllib.error
import urllib.request
import homestead_routes


def list_jobs(strict=False):
    """Every recurring job, with the volumes it covers and when it last ran.
    strict: a failed read raises, for a page that lists the jobs and must say
    it could not read them (#620, #927). Otherwise it answers [], for the
    Dashboard's health, where a cluster without Longhorn has no jobs to fail."""
    try:
        items = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs").get("items", [])
    except Exception:
        if strict:
            raise
        return []
    vols = VOLUMES._volumes()
    runs = _cron_runs()
    out = []
    for j in items:
        sp = j.get("spec", {})
        name = j["metadata"]["name"]
        run = runs.get(name, {})
        groups = sp.get("groups") or []
        covered = [v["name"] for v in vols
                   if v["labels"].get(LH.JOB_LABEL + name) == "enabled"
                   or any(v["labels"].get(LH.GROUP_LABEL + g) == "enabled" for g in groups)]
        out.append({
            "name": name, "generation": str(j["metadata"].get("generation") or ""),
            "task": sp.get("task", ""), "cron": sp.get("cron", ""),
            "retain": sp.get("retain", 0), "concurrency": sp.get("concurrency", 1),
            "groups": groups, "covers": len(covered), "volumes": sorted(covered),
            "desc": LH.TASKS.get(sp.get("task", ""), ""),
            "last_run": run.get("last_run", ""), "last_success": run.get("last_success", ""),
            "running": run.get("running", 0), "last_failed": run.get("last_failed", False),
        })
    return sorted(out, key=lambda x: x["name"])


def _cron_runs():
    """When each job last ran, from the CronJob Longhorn keeps for it.

    Longhorn names the CronJob after its recurring job. A run that started
    after the last success and is no longer active failed."""
    try:
        crons = LH.kget(f"/apis/batch/v1/namespaces/{LH.LHNS}/cronjobs").get("items", [])
    except Exception:
        return {}
    return {cron["metadata"]["name"]: cron_run_state(cron.get("status")) for cron in crons}


def cron_run_state(status):
    """A CronJob's last run from its status: when, whether it is running, and
    whether it failed - a run that started after the last success and is no
    longer active did."""
    status = status or {}
    last, ok = status.get("lastScheduleTime", "") or "", status.get("lastSuccessfulTime", "") or ""
    active = len(status.get("active") or [])
    return {"last_run": last, "last_success": ok, "running": active,
            "last_failed": bool(last and not active and (not ok or ok < last))}


def run_job(name):
    """Runs a recurring job now, as the schedule would, from its CronJob."""
    if not LH.SAFE.match(str(name or "")):
        raise ValueError("unknown job")
    try:
        cron = LH.kget(f"/apis/batch/v1/namespaces/{LH.LHNS}/cronjobs/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"Longhorn has not set up {name}'s schedule yet; try again in a moment")
        raise
    template = (cron.get("spec", {}) or {}).get("jobTemplate", {}) or {}
    job_name = f"{name[:40]}-now-{int(time.time()) % 1000000:06d}"
    meta = template.get("metadata", {}) or {}
    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job_name, "namespace": LH.LHNS,
                     "labels": dict(meta.get("labels") or {}),
                     "annotations": {**(meta.get("annotations") or {}), "cronjob.kubernetes.io/instantiate": "manual"},
                     "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob", "name": name,
                                          "uid": cron["metadata"]["uid"], "controller": False}]},
        "spec": template.get("spec", {}),
    }
    LH.ksend("POST", f"/apis/batch/v1/namespaces/{LH.LHNS}/jobs", body)
    LH._bust("lhjobs")
    return {"ok": True, "job": job_name, "namespace": LH.LHNS}


def run_status(item):
    """The operations poll's view of a job run started from Homestead."""
    ref = item["ref"]
    job = LH.kget(f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs/{ref['name']}")
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
    if not LH.SAFE.match(name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    if cfg.get("task") not in LH.TASKS:
        raise ValueError(f"task must be one of: {', '.join(LH.TASKS)}")
    retain = int(cfg.get("retain", 7) or 0)
    # A cleanup or a trim keeps nothing, so it has nothing to count.
    if cfg["task"] in LH.KEEPING and retain < 1:
        raise ValueError("retain must be at least 1 for snapshot and backup jobs")
    if cfg["task"] not in LH.KEEPING:
        retain = 0
    spec = {"name": name, "task": cfg["task"], "cron": cfg["cron"],
            "retain": retain, "concurrency": int(cfg.get("concurrency", 1)),
            "groups": cfg.get("groups") or [], "labels": cfg.get("labels") or {}}
    managed = {NAMES.key("managed"): "true"}
    path = f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs/{name}"

    def change(job):
        # The fields the form edits; labels, annotations and anything else
        # on the job stay as stored.
        job.setdefault("spec", {}).update(spec)
        job["metadata"]["labels"] = {**(job["metadata"].get("labels") or {}), **managed}
        return job

    def create():
        return {"apiVersion": "longhorn.io/v1beta2", "kind": "RecurringJob",
                "metadata": {"name": name, "namespace": LH.LHNS, "labels": dict(managed)},
                "spec": dict(spec)}

    # The editor names the generation of the job it opened ("" for a new
    # job): a job whose settings were saved since, or that was deleted or
    # made since, is not saved over (#621). The generation moves only when
    # the spec does; Longhorn counting a run in the status does not move it,
    # so a job that has merely run is still saved, on its current version.
    generation = cfg.get("generation")
    try:
        if generation is None:
            LH._update(path, change, create)
        elif generation == "":
            LH.ksend("POST", path.rsplit("/", 1)[0], create())
        else:
            def checked(job):
                if str(job["metadata"].get("generation") or "") != str(generation):
                    raise LH._Changed()
                return change(job)
            LH._update(path, checked)
    except LH._Changed:
        return homestead_routes.Reply(409, {"error": LH.JOB_CHANGED})
    except urllib.error.HTTPError as error:
        if generation is None or error.code not in (404, 409):
            raise
        error.close()
        return homestead_routes.Reply(409, {"error": LH.JOB_CHANGED if generation else
                                            f"There is already a job called {name}; edit that one instead"})
    LH._bust("lhjobs", "lhvols", "lhov")
    return {"ok": True, "name": name}


def delete_job(name):
    LH.ksend("DELETE", f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs/{name}")
    LH._bust("lhjobs", "lhvols", "lhov")
    return {"ok": True}


# Deleted files leave their blocks allocated in a Longhorn volume until the
# filesystem is trimmed: a volume that churns - media, databases, VM disks -
# holds far more space than its files use. Homestead trims every attached
# volume itself each week (homestead_weekly_trim). Earlier releases kept a
# recurring job for it, TRIM_JOB on TRIM_CRON; the weekly trim removes that
# job once if it is unchanged, and reads these to tell.
TRIM_JOB = "homestead-weekly-trim"
TRIM_CRON = "0 4 * * 6"
BACKEND = ACCESS.URL


def _backend(method, path, body=None):
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(f"{BACKEND}{path}", data=data if method == "POST" else None, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise ValueError(f"Longhorn refused the trim: {detail or error.reason}") from error


def trim_volume(volume):
    """Trim one volume now: its filesystem gives back the blocks its deleted
    files held. Longhorn trims only a volume that is attached - in use."""
    name = str(volume or "")
    if not LH.SAFE.match(name) and not re.fullmatch(r"pvc-[0-9a-f-]{36}", name):
        raise ValueError("unknown volume")
    try:
        found = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/volumes/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"no Longhorn volume {name}")
        raise
    state = (found.get("status") or {}).get("state", "")
    if state != "attached":
        raise ValueError(f"{name} is {state or 'not attached'}: Longhorn trims a volume only while it is in use; "
                         "the weekly trim reaches it the next time it is")
    ACCESS.require(LH.kget, LH.ksend, SELF.NS, f"Trimming {name}")
    _backend("POST", f"/volumes/{name}?action=trimFilesystem", {})
    return {"ok": True, "detail": f"{name} is being trimmed: space its deleted files held goes back to Longhorn"}


# Last, so that importing any of these modules first works: homestead_longhorn
# imports this one.
import homestead_longhorn_volumes as VOLUMES  # noqa: E402
import homestead_longhorn as LH  # noqa: E402
