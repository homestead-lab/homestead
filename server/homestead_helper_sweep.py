"""Finished helpers Homestead made, removed once their result has been read.

Image clean-up, host power and cluster shutdown each start a bare pod, and a
storage copy starts a Job; nothing removed them once they ended, so one more
was left behind for every image, host action, shutdown and copy. The leader
removes them here a while after they end: long enough that Recent jobs still
shows their log (a day for an image clean-up, a week for anything that is
evidence of a host action or a copy), and never while a job still follows
one, or one kept for inspection names it. Pull watchers that finished go
too (homestead_pullwatch.sweep, which used to run only while a pull was being
watched), and an import whose Job finished a week ago is removed the way its
Remove button does it, so the workload keeps its proof of completion.

Only Homestead's own objects: the task label and the name both have to be
Homestead's, and each delete names the uid that was read."""
import calendar
import re
import time
import urllib.error
import urllib.parse

import homestead_import_job as IMPORT_JOB
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_pullwatch as PULLWATCH

NS = "lab"              # where Homestead starts its bare helper pods
HOUR, DAY = 3600, 86400

# task: (how long a finished one is kept, what Homestead calls it)
PODS = {
    "image-cleanup": (DAY, re.compile(r"homestead-image-clean-[0-9a-f]{10}-[0-9a-f]{1,6}")),
    "node-power": (7 * DAY, re.compile(r"homestead-(reboot|poweroff|handoff-reboot|handoff-poweroff)-[a-z0-9.-]*")),
    "cluster-shutdown": (7 * DAY, re.compile(r"homestead-shutdown-[0-9a-f]{16}-(coordinator|[0-9]+)")),
    "pull-watch": (HOUR, re.compile(r"homestead-pull-watch-[0-9a-f]{14}")),
}
JOBS = {
    "restructure": (7 * DAY, re.compile(r"[a-z0-9.-]*-restructure-[0-9a-f]{6}")),
}
IMPORT_KEPT = 7 * DAY
IMPORT_NAME = re.compile(r"homestead-import-[a-z0-9][a-z0-9-]{0,60}")


def tick(now=None):
    """The leader's minute: finished pull watchers, then the other helpers."""
    now = time.time() if now is None else now
    try:
        PULLWATCH.sweep(now)
    except Exception as error:
        print(f"finished pull watchers not removed: {str(error)[:160]}", flush=True)
    sweep(now=now)


def sweep(now=None):
    """Remove finished helpers older than their bound; the names removed.
    When the jobs cannot be read, nothing is removed: what a job follows
    is not known."""
    now = time.time() if now is None else now
    try:
        items = OPS.stored()
    except Exception as error:
        print(f"finished helpers not removed, the jobs could not be read: {str(error)[:160]}", flush=True)
        return []
    followed = _followed(items)
    removed = []
    for task, (kept, name) in PODS.items():
        for pod in _listed(f"/api/v1/namespaces/{NS}/pods", task):
            meta = pod.get("metadata") or {}
            if (name.fullmatch(meta.get("name", "")) and (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed")
                    and now - _pod_ended(pod) > kept and not _is_followed(meta, followed)
                    and _delete(f"/api/v1/namespaces/{NS}/pods/{meta['name']}", meta)):
                removed.append(meta["name"])
    for task, (kept, name) in JOBS.items():
        for job in _listed("/apis/batch/v1/jobs", task):
            meta = job.get("metadata") or {}
            ended = _job_ended(job)
            if (name.fullmatch(meta.get("name", "")) and ended is not None and now - ended > kept
                    and not _is_followed(meta, followed)
                    and _delete(f"/apis/batch/v1/namespaces/{meta['namespace']}/jobs/{meta['name']}", meta,
                                propagationPolicy="Background")):
                removed.append(meta["name"])
    removed += _old_imports(items, now)
    return removed


def _old_imports(items, now):
    """Imports whose Job completed a week ago, with a receipt of a verified
    success not yet removed: removed as the import page's Remove does it."""
    receipts = {(i["ref"].get("namespace"), i["ref"].get("job")): i["ref"].get("job_uid")
                for i in items if i.get("kind") == IMPORT_JOB.KIND and i.get("status") == "succeeded"
                and isinstance(i.get("ref"), dict) and i["ref"].get("job_uid")
                and not i["ref"].get("helper_removed") and not i["ref"].get("retain_resources")}
    removed = []
    for job in _listed("/apis/batch/v1/jobs", "import"):
        meta = job.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if not IMPORT_NAME.fullmatch(name) or receipts.get((ns, name)) != meta.get("uid") or not meta.get("uid"):
            continue
        if not _complete(job) or now - (_job_ended(job) or now) <= IMPORT_KEPT:
            continue
        try:
            IMPORT_JOB.remove_completed_job(ns, name, job, KUBE.kget, KUBE.ksend, OPS)
        except Exception as error:
            print(f"finished import {ns}/{name} not removed: {str(error)[:160]}", flush=True)
            continue
        removed.append(name)
    return removed


def _followed(items):
    """Every name and uid a job still follows, or one kept for inspection."""
    words = set()

    def walk(value):
        if isinstance(value, str):
            words.add(value)
        elif isinstance(value, dict):
            for inner in value.values():
                walk(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                walk(inner)
    for item in items:
        ref = item.get("ref") if isinstance(item, dict) else None
        if item.get("status") not in OPS.TERMINAL or (isinstance(ref, dict) and ref.get("retain_resources")):
            walk(ref)
    return words


def _is_followed(meta, followed):
    """Named by a job: by its name or uid, or by a run id in its name (a
    shutdown names its helpers after the run)."""
    name, uid = meta.get("name", ""), meta.get("uid", "")
    return (name in followed or (uid and uid in followed)
            or any(len(word) >= 12 and word in name for word in followed))


def _listed(path, task):
    try:
        listed = KUBE.kget(f"{path}?labelSelector={urllib.parse.quote(NAMES.key('task') + '=' + task)}")
    except Exception as error:
        print(f"finished {task} helpers not listed: {str(error)[:160]}", flush=True)
        return []
    return [row for row in listed.get("items") or [] if isinstance(row, dict)]


def _delete(path, meta, **options):
    if not meta.get("uid"):
        return False
    body = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": meta["uid"]}, **options}
    try:
        KUBE.ksend("DELETE", path, body)
    except urllib.error.HTTPError as error:
        error.close()
        return False        # gone already (404), or replaced since it was read (409)
    except Exception as error:
        print(f"finished helper {meta.get('name')} not removed: {str(error)[:160]}", flush=True)
        return False
    return True


def _epoch(stamp):
    try:
        return float(calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        return None


def _pod_ended(pod):
    """When a finished pod ended: its last container's end, else when it
    started or was made. Unknown is now, so an unreadable one is kept."""
    status = pod.get("status") or {}
    ends = [_epoch(((c.get("state") or {}).get("terminated") or {}).get("finishedAt"))
            for c in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or [])
            if isinstance(c, dict)]
    ends = [t for t in ends if t is not None]
    if ends:
        return max(ends)
    for stamp in (status.get("startTime"), (pod.get("metadata") or {}).get("creationTimestamp")):
        if _epoch(stamp) is not None:
            return _epoch(stamp)
    return time.time()


def _complete(job):
    return any(c.get("type") == "Complete" and c.get("status") == "True"
               for c in (job.get("status") or {}).get("conditions") or [] if isinstance(c, dict))


def _job_ended(job):
    """When a Job ended, Complete or Failed with no pod still running; None
    while it has not."""
    status = job.get("status") or {}
    if status.get("active"):
        return None
    ended = [c for c in status.get("conditions") or [] if isinstance(c, dict)
             and c.get("type") in ("Complete", "Failed") and c.get("status") == "True"]
    if not ended:
        return None
    for stamp in [status.get("completionTime")] + [c.get("lastTransitionTime") for c in ended]:
        if _epoch(stamp) is not None:
            return _epoch(stamp)
    return time.time()
