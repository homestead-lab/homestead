"""Persistent, user-visible operation tracking.

The durable record lives on Homestead's Longhorn volume, while live progress is
derived from Kubernetes resources. This means a browser refresh or Homestead pod
restart cannot lose an in-flight image pull, rollout, import, migration, or
backup. Only identifiers and status are stored; request bodies and credentials
are deliberately excluded.
"""
import json
import os
import secrets
import threading
import time
import urllib.error


kget = None
deployment_progress = None
DATA_DIR = "/data"
STORE = "operations.json"
MAX_OPERATIONS = 100
TERMINAL = {"succeeded", "failed", "cancelled"}
_lock = threading.RLock()


def bind(_kget, data_dir, _deployment_progress):
    global kget, DATA_DIR, deployment_progress
    kget, DATA_DIR, deployment_progress = _kget, data_dir, _deployment_progress


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _store_path():
    return os.path.join(DATA_DIR, STORE)


def _read():
    try:
        with open(_store_path(), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _store_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(items[-MAX_OPERATIONS:], handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def start(kind, title, resource, href, ref, message="Waiting for Kubernetes"):
    """Persist a newly started operation and return its public representation."""
    now = _now()
    item = {
        "id": secrets.token_hex(12), "kind": kind, "title": str(title)[:180],
        "resource": {
            "kind": str((resource or {}).get("kind", ""))[:60],
            "name": str((resource or {}).get("name", ""))[:253],
            "namespace": str((resource or {}).get("namespace", ""))[:253],
        },
        "href": str(href or "/")[:500], "ref": dict(ref or {}),
        "status": "queued", "progress": 0, "message": str(message)[:500],
        "started_at": now, "updated_at": now, "finished_at": "",
    }
    with _lock:
        items = _read()
        items.append(item)
        _write(items)
    return _public(item)


def _public(item):
    return {key: value for key, value in item.items() if key != "ref"}


def _finish(item, status, progress, message):
    changed = (item.get("status"), item.get("progress"), item.get("message")) != (
        status, progress, message)
    item.update(status=status, progress=max(0, min(100, int(progress or 0))),
                message=str(message or "")[:500])
    if status in TERMINAL and not item.get("finished_at"):
        item["finished_at"] = _now()
        changed = True
    if changed:
        item["updated_at"] = _now()
    return changed


def _deployment(item):
    ref = item["ref"]
    state = deployment_progress(ref["namespace"], ref["name"])
    desired = max(0, int(state.get("desired", 0) or 0))
    ready = max(0, int(state.get("ready", 0) or 0))
    progress = 100 if state.get("phase") == "ready" else (
        min(95, round(ready / desired * 90)) if desired else 10)
    if state.get("phase") == "failed":
        return "failed", progress, "; ".join(state.get("problems") or []) or "Rollout failed"
    if state.get("phase") == "ready":
        return "succeeded", 100, f"{ready}/{desired} replicas ready"
    return "running", progress, f"{ready}/{desired} replicas ready"


def _prepull(item):
    ref = item["ref"]
    ds = kget(f"/apis/apps/v1/namespaces/{ref['namespace']}/daemonsets/{ref['name']}")
    status = ds.get("status", {}) or {}
    desired = int(status.get("desiredNumberScheduled", 0) or 0)
    ready = int(status.get("numberReady", 0) or 0)
    unavailable = int(status.get("numberUnavailable", 0) or 0)
    for condition in status.get("conditions", []) or []:
        if condition.get("status") == "False" and condition.get("type") in ("Progressing", "Available"):
            return "failed", 0, condition.get("message") or condition.get("reason") or "Image pull failed"
    if desired and ready >= desired and unavailable == 0:
        return "succeeded", 100, f"Image cached on {ready}/{desired} nodes"
    progress = min(95, round(ready / desired * 100)) if desired else 5
    return "running", progress, f"Image cached on {ready}/{desired or '?'} nodes"


def _job(item):
    ref = item["ref"]
    job = kget(f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs/{ref['name']}")
    status = job.get("status", {}) or {}
    failed = int(status.get("failed", 0) or 0)
    succeeded = int(status.get("succeeded", 0) or 0)
    active = int(status.get("active", 0) or 0)
    for condition in status.get("conditions", []) or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return "failed", 100, condition.get("message") or condition.get("reason") or "Job failed"
    if succeeded:
        return "succeeded", 100, "Transfer complete"
    if failed and not active:
        return "failed", 100, "Transfer failed"
    return "running", 20 if active else 5, "Copying data" if active else "Waiting for transfer pod"


def _migration(item):
    ref = item["ref"]
    obj = kget(f"/apis/kubevirt.io/v1/namespaces/{ref['namespace']}/virtualmachineinstancemigrations/{ref['name']}")
    phase = (obj.get("status", {}) or {}).get("phase", "")
    if phase == "Succeeded":
        return "succeeded", 100, "Virtual machine migration complete"
    if phase in ("Failed", "Cancelled"):
        return "failed" if phase == "Failed" else "cancelled", 100, f"Migration {phase.lower()}"
    progress = 60 if phase in ("Running", "TargetReady") else 10
    return "running", progress, f"Migration {phase.lower() or 'pending'}"


def _backup(item):
    ref = item["ref"]
    obj = kget(f"/apis/longhorn.io/v1beta2/namespaces/{ref['namespace']}/backups/{ref['name']}")
    status = obj.get("status", {}) or {}
    state = str(status.get("state", "") or "").lower()
    progress = int(status.get("progress", 0) or 0)
    if state == "completed":
        return "succeeded", 100, "Backup complete"
    if state in ("error", "failed", "unknown") and status.get("error"):
        return "failed", progress, status.get("error")
    return "running", progress, f"Backup {state or 'pending'}"


def _image_cleanup(item):
    ref = item["ref"]
    pods = [kget(f"/api/v1/namespaces/{ref['namespace']}/pods/{name}")
            for name in ref.get("pods", [])]
    complete = 0
    for pod in pods:
        status = pod.get("status", {}) or {}
        phase = status.get("phase", "Pending")
        states = [row.get("state", {}) for row in status.get("containerStatuses", []) or []]
        failed = next((state.get("terminated") for state in states
                       if (state.get("terminated") or {}).get("exitCode", 0) != 0), None)
        if phase == "Failed" or failed:
            detail = (failed or {}).get("message") or (failed or {}).get("reason") or status.get("message")
            return "failed", round(complete / max(1, len(pods)) * 100), detail or "Image cleanup failed"
        if phase == "Succeeded":
            complete += 1
    if pods and complete == len(pods):
        return "succeeded", 100, f"Image removed from {complete}/{len(pods)} nodes"
    progress = round(complete / max(1, len(pods)) * 90)
    return "running", progress, f"Image removed from {complete}/{len(pods)} nodes"


RESOLVERS = {
    "deployment": _deployment,
    "image-update": _deployment,
    "image-rollback": _deployment,
    "image-pull": _prepull,
    "image-cleanup": _image_cleanup,
    "import": _job,
    "vm-migration": _migration,
    "backup": _backup,
}


def _refresh(item):
    if item.get("status") in TERMINAL:
        return False
    resolver = RESOLVERS.get(item.get("kind"))
    if not resolver:
        return _finish(item, "failed", item.get("progress", 0), "Unknown operation type")
    try:
        return _finish(item, *resolver(item))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return _finish(item, "failed", item.get("progress", 0),
                           "The Kubernetes operation resource no longer exists")
        return _finish(item, "running", item.get("progress", 0),
                       f"Status temporarily unavailable (HTTP {error.code})")
    except Exception as error:
        # Monitoring failures are not operation failures. Preserve the active
        # state and let the next poll recover when Kubernetes is reachable.
        return _finish(item, "running", item.get("progress", 0),
                       f"Status temporarily unavailable: {error}")


def list_operations():
    with _lock:
        items = _read()
        changed = False
        for item in items:
            changed = _refresh(item) or changed
        if changed:
            _write(items)
        items.sort(key=lambda item: item.get("started_at", ""), reverse=True)
        return [_public(item) for item in items]


def dismiss(operation_id):
    with _lock:
        items = _read()
        match = next((item for item in items if item.get("id") == operation_id), None)
        if not match:
            raise ValueError("operation not found")
        if match.get("status") not in TERMINAL:
            raise ValueError("an active operation cannot be dismissed")
        items = [item for item in items if item.get("id") != operation_id]
        _write(items)
    return {"ok": True, "id": operation_id}
