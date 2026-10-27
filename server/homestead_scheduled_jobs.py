"""
Schedules: the CronJobs Homestead lists, saves, runs and deletes.

Split by feature from homestead_imports (#623), moved unchanged. Names that
homestead_imports owns or that server.py binds there are read through it (I.name),
so a replacement there still applies; read and replace the names defined here, here.
"""
import copy
import re
import time
import homestead_self as SELF
import urllib.error
import urllib.parse
import homestead_names as NAMES
import homestead_cronjob_health as HEALTH
import homestead_routes


# --------------------------------------------------------------- schedules
# Schedules lists every CronJob in the namespace but changes only those it
# made: save_job labels each one homestead.io/managed=true (under the old
# domain before the rename, which the start-up adoption of old keys does not
# move for CronJobs). Others (Helm, kubectl) carry volumes, env and a service
# account this page does not show, so they are read-only here.
# Any CronJob name Kubernetes allows (a DNS subdomain of at most 52
# characters), for Run now, which works on CronJobs Homestead did not name.
CRONJOB_NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,50}[a-z0-9])?")


def _job_managed(cj):
    meta = cj.get("metadata") or {}
    return "true" in (NAMES.label_of(meta, "managed"), SELF.old_label(meta, "managed"))


def _job_managed_by(cj):
    labels = (cj.get("metadata") or {}).get("labels") or {}
    return labels.get("app.kubernetes.io/managed-by", "")


def list_jobs():
    """Every CronJob in the cluster (#739), Homestead's own namespace first,
    with how its last run ended and whether a run is overdue. A list that
    could not be read raises - an empty one would say there are none - and
    runs that could not be read make each row's result "unknown"."""
    cjs = I.kget("/apis/batch/v1/cronjobs").get("items", [])
    # Homestead's own namespace first, so it is never past MAX_JUDGED.
    cjs.sort(key=lambda c: (c.get("metadata") or {}).get("namespace") != I.NS)
    try:
        jobs, error = I.kget("/apis/batch/v1/jobs").get("items", []), ""
    except Exception as e:
        jobs, error = None, str(e)[:160]
    now = time.time()
    out = []
    for c in cjs:
        sp, st = c.get("spec") or {}, c.get("status") or {}
        containers = (((sp.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or [{}]
        cont = containers[0]
        managed = _job_managed(c)
        row = {
            "name": c["metadata"]["name"],
            "schedule": sp.get("schedule", ""),
            "time_zone": sp.get("timeZone") or "",
            "suspend": bool(sp.get("suspend")),
            "image": cont.get("image", ""),
            "command": " ".join(cont.get("command", [])[-1:]) if cont.get("command") else "",
            "last": st.get("lastScheduleTime", ""),
            "created": c["metadata"].get("creationTimestamp", ""),
            "active": len(st.get("active", []) or []),
            "managed": managed,
            "managed_by": "" if managed else _job_managed_by(c),
            "generation": c["metadata"].get("generation"),
        }
        own = c["metadata"].get("namespace") == I.NS
        row.update(HEALTH.health(c, jobs, now, error, detail=own, judge=len(out) < HEALTH.MAX_JUDGED))
        # Saved, deleted and run from here only in Homestead's own namespace.
        row["runnable"], row["editable"] = own, managed and own
        if not own:
            # Health only: viewers never saw other namespaces' CronJob specs,
            # and a command or image often names a token or a private host.
            for field in ("image", "command", "generation"):
                row.pop(field)
        out.append(row)
    return sorted(out, key=lambda x: (x["ns"] != I.NS, x["ns"], x["name"]))


def cronjob_alert_facts():
    """The alerts loop's source: raises when the CronJobs could not be read,
    so the alerts already raised are kept, not cleared."""
    return HEALTH.alert_facts(homestead_routes.cached("cron", 8, list_jobs), time.time())


def _managed_job(name):
    """The CronJob Homestead made under this name, or None when there is none."""
    current = VM_CREATE._get_or_none(f"/apis/batch/v1/namespaces/{I.NS}/cronjobs/{name}")
    if current is not None and not _job_managed(current):
        by = _job_managed_by(current)
        raise ValueError(f"Homestead did not make the CronJob {name}"
                         + (f" ({by} manages it)" if by else "")
                         + "; change it where it was made")
    return current


def save_job(cfg):
    name = VM_CREATE._required_name(cfg.get("name"))
    current = _managed_job(name)
    # The page sends the generation it read ("" for a new schedule), so a
    # save never lands on a job whose spec changed, or that appeared, since
    # the page loaded. Not resourceVersion: that moves with every status
    # write, so each run starting or ending would refuse the save.
    read = cfg.get("generation")
    now = (current or {}).get("metadata", {}).get("generation", "")
    if read is not None and str(read) != str(now):
        if read == "":
            raise ValueError(f"a schedule named {name} already exists; reload the page to edit it")
        raise ValueError(f"{name} changed since the page loaded; reload it and save again")
    I._bust("cron")
    if current is None:
        body = {
            "apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": name, "namespace": I.NS, "labels": {NAMES.key("managed"): "true"}},
            "spec": {"schedule": cfg["schedule"], "timeZone": "Etc/UTC", "suspend": bool(cfg.get("suspend")),
                     "concurrencyPolicy": "Forbid",
                     "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
                     "jobTemplate": {"spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 86400,
                         "template": {"spec": {"restartPolicy": "Never", "containers": [{
                             "name": "task", "image": cfg.get("image", "alpine:3.24"),
                             "command": ["sh", "-c", cfg["command"]],
                         }]}}}}},
        }
        return I.ksend("POST", f"/apis/batch/v1/namespaces/{I.NS}/cronjobs", body)
    # An edit changes only what the page shows and keeps the rest of the
    # object (volumes, env, service account, annotations). The PUT carries
    # the resourceVersion just read, so Kubernetes refuses it with a 409 if
    # the job changed in between.
    body = copy.deepcopy(current)
    body.pop("status", None)
    labels = body["metadata"].setdefault("labels", {})
    labels.pop(SELF.OLD_DOMAIN + "managed", None)
    labels[NAMES.key("managed")] = "true"
    spec = body["spec"]
    spec["schedule"] = cfg["schedule"]
    spec["suspend"] = bool(cfg.get("suspend"))
    # Without a timeZone the schedule runs on the controller host's clock,
    # so a saved one is pinned to UTC, which is what the page shows. A zone
    # already set, say with kubectl, is kept.
    spec["timeZone"] = spec.get("timeZone") or "Etc/UTC"
    container = spec["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    container["image"] = cfg.get("image") or container.get("image") or "alpine:3.24"
    container["command"] = ["sh", "-c", cfg["command"]]
    try:
        return I.ksend("PUT", f"/apis/batch/v1/namespaces/{I.NS}/cronjobs/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
        raise ValueError(f"{name} changed since the page loaded; reload it and save again") from None


def del_job(name):
    name = VM_CREATE._required_name(name)
    _managed_job(name)
    I._bust("cron")
    I.ksend("DELETE", f"/apis/batch/v1/namespaces/{I.NS}/cronjobs/{name}?propagationPolicy=Background")
    return {"ok": True}


def run_job_now(name):
    # Any CronJob may be run now: it starts a Job from the template and
    # changes nothing on the CronJob itself.
    name = str(name or "")
    if not CRONJOB_NAME.fullmatch(name):
        raise ValueError("name must be a CronJob's: lowercase letters, numbers, dashes and dots, at most 52")
    cj = I.kget(f"/apis/batch/v1/namespaces/{I.NS}/cronjobs/{name}")
    body = {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": f"{name}-{int(time.time()) % 1000000}", "namespace": I.NS,
                         "labels": {NAMES.key("managed"): "true", NAMES.key("from"): name}},
            "spec": cj["spec"]["jobTemplate"]["spec"]}
    return I.ksend("POST", f"/apis/batch/v1/namespaces/{I.NS}/jobs", body)


import homestead_imports as I  # noqa: E402  (imports at the bottom: homestead_imports imports this module)
import homestead_vm_create as VM_CREATE  # noqa: E402
