"""Persistent, user-visible operation tracking.

The durable record lives on Homestead's Longhorn volume, while live progress is
derived from Kubernetes resources. This means a browser refresh or Homestead pod
restart cannot lose an in-flight image pull, rollout, import, migration, or
backup. Only identifiers and status are stored; request bodies and credentials
are deliberately excluded.
"""
import homestead_shared as SHARED
import homestead_vm_power_receipts as POWER_RECEIPTS
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse


kget = None
deployment_progress = None
smart_progress = None
DATA_DIR = "/data"
STORE = "operations.json"
STORE_MARKER = ".operations-initialized.json"
MAX_OPERATIONS = 100
TERMINAL = {"succeeded", "failed", "cancelled"}
# A cancel that has begun and not yet finished. The poll leaves such a job
# alone, so a step cannot move it on while it is being put back.
CANCELLING = "cancelling"
# A cancel that stops part-way - Homestead restarted during it - may be asked
# again after this long; every canceller's steps are safe to repeat.
CANCEL_RETRY_AFTER = 120
# Shared with any other Homestead replica on the same data volume.
_lock = SHARED.SharedLock("operations", strict=True, directory=lambda: DATA_DIR)


def bind(_kget, data_dir, _deployment_progress, _smart_progress=None):
    global kget, DATA_DIR, deployment_progress, smart_progress
    kget, DATA_DIR, deployment_progress = _kget, data_dir, _deployment_progress
    smart_progress = _smart_progress


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _store_path():
    return os.path.join(DATA_DIR, STORE)


def _initialized():
    try:
        with open(os.path.join(DATA_DIR, STORE_MARKER), encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as error:
        raise ValueError("Job history initialization could not be verified; recover store access before changing jobs") from error
    if value != {"version": 1}:
        raise ValueError("Job history initialization marker is invalid; recover the store before changing jobs")
    return True


def _read():
    initialized = _initialized()
    try:
        with open(_store_path(), encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        if not initialized:
            return []
        raise ValueError("Previously initialized job history is missing. Recover the store before changing jobs; missing history is not an empty queue.")
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("Job history could not be read safely. Restore access or recover the store before starting or changing jobs; existing records were not replaced.") from error
    if (not isinstance(value, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not item["id"] or not isinstance(item.get("ref"), dict) or not isinstance(item.get("status"), str)
            for item in value) or len({item["id"] for item in value}) != len(value)):
        raise ValueError("Job history has invalid records. Recover the store before starting or changing jobs; existing records were not replaced.")
    return value


def _write(items):
    initialized = _initialized()
    # Record consumption before writing/pruning visible history. A clock jump,
    # history clear or restart must never resurrect an already-used approval.
    POWER_RECEIPTS.remember(DATA_DIR, items)
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _store_path()
    # A busy history must not evict an in-flight dispatch intent or a failed
    # batch's recovery record. Bound ordinary terminal history, not active work.
    protected = {i for i, item in enumerate(items) if item.get("status") not in TERMINAL or
                 (item.get("status") == "failed" and item.get("ref", {}).get("retain_resources")) or _receipt_needed(item)}
    recent = [i for i in range(len(items)) if i not in protected][-MAX_OPERATIONS:] if MAX_OPERATIONS > 0 else []
    kept = protected | set(recent)
    SHARED.write_json(path, [item for i, item in enumerate(items) if i in kept], durable=True, separators=(",", ":"))
    # A later missing file is not a new installation. Publish the marker before
    # returning permission to perform the mutation the journal precedes.
    if not initialized:
        SHARED.write_json(os.path.join(DATA_DIR, STORE_MARKER), {"version": 1}, durable=True)


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
        "history": [{"t": now, "s": "queued", "p": 0, "m": str(message)[:240]}],
    }
    with _lock:
        items = _read()
        if kind == "vm-power":
            consumed = POWER_RECEIPTS.find(DATA_DIR, ref.get("review_digest"))
            if consumed:
                raise ValueError(f"This approval already has job {consumed}; inspect history instead of repeating it. The displayed job may have been cleared.")
            for existing in items:
                if existing.get("kind") != kind:
                    continue
                previous = existing.get("ref") or {}
                if previous.get("review_digest") == ref.get("review_digest"):
                    raise ValueError(f"This approval already has job {existing['id']}; inspect it instead of repeating the request")
                if (previous.get("namespace"), previous.get("name")) == (ref.get("namespace"), ref.get("name")) and (
                        existing.get("status") not in TERMINAL or previous.get("retain_resources")):
                    raise ValueError(f"VM power job {existing['id']} is still active or needs recovery; inspect it first")
        if kind == "node-power" and any(i.get("kind") == kind and i.get("status") not in TERMINAL and
                                        i.get("ref", {}).get("node") == ref.get("node") for i in items):
            raise ValueError("Host maintenance is already active; inspect its job before retrying")
        if kind == "k3s-cluster" and any(i.get("kind") == kind and i.get("status") not in TERMINAL and
                                        (i.get("ref", {}).get("namespace"), i.get("ref", {}).get("name")) ==
                                        (ref.get("namespace"), ref.get("name")) for i in items):
            raise ValueError("This k3s VM batch is already active; inspect its job before retrying")
        if kind in ("snapshot-delete", "snapshot-revert") and any(
                i.get("kind") in ("snapshot-delete", "snapshot-revert") and i.get("status") not in TERMINAL
                and i.get("ref", {}).get("volume") == ref.get("volume") for i in items):
            raise ValueError("A snapshot operation is already active on this volume; inspect its job first")
        items.append(item)
        _write(items)
    return _public(item)


def record_phase(operation_id, phase, progress, message, **ref_updates):
    """Persist synchronous maintenance progress before irreversible steps."""
    with _lock:
        items = _read()
        item = next(i for i in items if i["id"] == operation_id)
        if item.get("status") in TERMINAL or (item.get("kind") == "vm-power" and item.get("status") == CANCELLING):
            raise ValueError("Maintenance job has ended; refusing further actions")
        item["ref"].update(ref_updates, phase=phase, phase_at=time.time())
        _finish(item, "failed" if phase == "failed" else "running", progress, message)
        _write(items)
        return _public(item)


def _public(item):
    out = {key: value for key, value in item.items()
           if key not in ("ref", "cancel_started", "previous_status", "history")}
    # Every job still going can be cancelled; what that does is asked for
    # separately, as it reads Kubernetes and the tray is polled often.
    out["cancellable"] = item.get("status") not in TERMINAL and (
        item.get("status") != CANCELLING or _cancel_stale(item))
    out["cleanable"] = _cleanable(item)
    out["dismissible"] = item.get("status") in TERMINAL and not _receipt_needed(item)
    if item.get("kind") == "vm-power":
        out["cancellable"] = out["cancellable"] and item.get("ref", {}).get("phase") in ("prepared", "accepted")
        out["power_recovery"] = item.get("status") not in TERMINAL and item.get("ref", {}).get("phase") in ("uncertain", "dispatching")
    if item.get("kind") == "snapshot-delete":
        out["cancellable"] = False  # Longhorn merging cannot be undone or safely interrupted.
    if item.get("kind") == "k3s-cluster" and item.get("ref", {}).get("retain_resources") and item["ref"].get("phase") == "provisioning":
        out["cancellable"] = False  # synchronous dispatch may still be in flight
    check = RESUMABLE.get(item.get("kind"))
    if item.get("status") == "failed" and check:
        try:
            out["resumable"] = not check(item)
        except Exception:
            out["resumable"] = False
    return out


def _receipt_needed(item):
    if item.get("kind") != "vm-power":
        return False
    ref = item.get("ref") or {}
    return bool(ref.get("retain_resources") or float(ref.get("review_expires") or 0) >= time.time())


# Each step a job has said, kept with it: what the Log view shows for any job,
# whatever it is, and all a job that runs no pod of its own has to show.
HISTORY_KEEP = 40


def _note(item, status, progress, message):
    history = item.setdefault("history", [])
    if history and (history[-1].get("m"), history[-1].get("s")) == (message, status):
        return
    history.append({"t": _now(), "s": status, "p": progress, "m": str(message or "")[:240]})
    del history[:-HISTORY_KEEP]


def _finish(item, status, progress, message):
    changed = (item.get("status"), item.get("progress"), item.get("message")) != (
        status, progress, message)
    item.update(status=status, progress=max(0, min(100, int(progress or 0))),
                message=str(message or "")[:500])
    _note(item, item["status"], item["progress"], item["message"])
    if status in TERMINAL and not item.get("finished_at"):
        item["finished_at"] = _now()
        changed = True
    if changed:
        item["updated_at"] = _now()
    return changed


def _mb(size):
    size = int(size or 0)
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 else f"{max(1, round(size / 1024 ** 2))} MB"


def _elapsed(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


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
    # "0/1 replicas ready" for four minutes says nothing; a big image being
    # fetched says everything. Kubernetes reports no percentage for a pull, so
    # this reports what it does know: the image, the node and how long.
    pull = state.get("pull") or {}
    if pull.get("state") == "pulling":
        where = f" on {pull['node']}" if pull.get("node") else ""
        if pull.get("total_bytes"):
            # containerd's own count of what it has fetched, against the
            # registry's sizes: the pull is most of a first start.
            pct = int(pull.get("percent") or 0)
            return "running", max(progress, min(90, 5 + pct * 85 // 100)), (
                f"Pulling image{where} · {pct}% of {_mb(pull['total_bytes'])} · {_elapsed(pull.get('seconds'))} so far")
        return "running", progress, f"Pulling image{where} · {_elapsed(pull.get('seconds'))} so far"
    if pull.get("state") == "pulled" and ready < desired:
        took = f" in {pull['took']}" if pull.get("took") else ""
        return "running", progress, f"Image pulled{took}; starting container"
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


def _vm_disk_import(item):
    ref = item["ref"]
    obj = kget(
        f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ref['namespace']}"
        f"/datavolumes/{ref['name']}")
    status = obj.get("status", {}) or {}
    phase = str(status.get("phase", "Pending") or "Pending")
    raw_progress = str(status.get("progress", "") or "").rstrip("%")
    try:
        progress = max(0, min(99, round(float(raw_progress))))
    except ValueError:
        progress = 5 if phase in ("Pending", "ImportScheduled") else 15
    failed_phases = {"Failed", "Error", "Unknown"}
    conditions = status.get("conditions", []) or []
    detail = next((condition.get("message") or condition.get("reason")
                   for condition in reversed(conditions)
                   if condition.get("status") == "False" and
                   (condition.get("message") or condition.get("reason"))), "")
    if phase == "Succeeded":
        return "succeeded", 100, "Disk image imported and PVC is ready"
    if phase in failed_phases:
        return "failed", progress, detail or f"CDI import {phase.lower()}"
    if phase == "WaitForFirstConsumer":
        return "running", progress, "Waiting for a VM to request this disk"
    message = {
        "Pending": "Preparing the CDI import",
        "ImportScheduled": "Importer pod scheduled",
        "ImportInProgress": "Downloading and converting the disk image",
        "Paused": "CDI import paused",
    }.get(phase, f"CDI import {phase.lower()}")
    return "running", progress, message


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


def _volume_restore(item):
    ref = item["ref"]
    namespace, name = ref["namespace"], ref["name"]
    pvc = _get_or_none(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}")
    if pvc is None:
        return "failed", 0, "The destination PVC no longer exists"
    pvc_status = pvc.get("status", {}) or {}
    phase = str(pvc_status.get("phase", "Pending") or "Pending")
    if phase == "Lost":
        return "failed", 10, "Kubernetes reports the restored PVC as lost"
    pv_name = (pvc.get("spec", {}) or {}).get("volumeName", "")
    if not pv_name:
        return "running", 8, "Provisioning the destination PVC"
    pv = _get_or_none(f"/api/v1/persistentvolumes/{pv_name}")
    if pv is None:
        return "running", 12, "Waiting for the restored persistent volume"
    csi = (pv.get("spec", {}) or {}).get("csi", {}) or {}
    volume_name = csi.get("volumeHandle", "")
    if not volume_name:
        return "failed", 12, "The provisioned PV is not a Longhorn CSI volume"
    volume = _get_or_none(
        f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/{volume_name}")
    if volume is None:
        return "running", 16, "Waiting for Longhorn to create the volume"
    status = volume.get("status", {}) or {}
    for condition in status.get("conditions", []) or []:
        if condition.get("type") == "Scheduled" and condition.get("status") == "False":
            detail = condition.get("message") or condition.get("reason")
            return "failed", 18, detail or "Longhorn cannot schedule the restored volume"
    robustness = str(status.get("robustness", "") or "").lower()
    if robustness == "faulted":
        return "failed", 20, "The restored Longhorn volume is faulted"

    selector = urllib.parse.quote(f"longhornvolume={volume_name}", safe="")
    engines = kget(
        "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines"
        f"?labelSelector={selector}").get("items", [])
    restore_rows = []
    restored_backups = []
    for engine in engines:
        engine_status = engine.get("status", {}) or {}
        restored_backups.append(str(engine_status.get("lastRestoredBackup", "") or ""))
        restore_rows.extend((engine_status.get("restoreStatus", {}) or {}).values())
    errors = [str(row.get("error")) for row in restore_rows if row.get("error")]
    if errors:
        return "failed", min([int(row.get("progress", 0) or 0) for row in restore_rows] or [20]), errors[0]
    restoring = any(bool(row.get("isRestoring")) for row in restore_rows)
    progresses = [int(row.get("progress", 0) or 0) for row in restore_rows]
    restore_progress = min(progresses) if progresses else 0
    operation_progress = min(92, 20 + (restore_progress * 70 + 50) // 100)
    restored = bool(restore_rows) and all(
        not row.get("isRestoring") and (
            int(row.get("progress", 0) or 0) >= 100 or row.get("lastRestored") or
            str(row.get("state", "")).lower() in ("complete", "completed"))
        for row in restore_rows)
    restored = restored or (
        bool(status.get("restoreInitiated")) and not bool(status.get("restoreRequired")) and
        any(restored_backups))
    if restored and phase == "Bound" and robustness == "healthy":
        return "succeeded", 100, f"Restored PVC {namespace}/{name} is bound and healthy"
    if restoring or restore_progress:
        return "running", operation_progress, f"Restoring backup data · {restore_progress}%"
    if restored:
        return "running", 96, f"Data restored; waiting for {robustness or 'healthy'} replicas"
    if status.get("restoreInitiated"):
        return "running", 20, "Longhorn initialized the backup restore"
    return "running", 18, "Waiting for Longhorn restore engines"


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


def _get_or_none(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _volume_delete(item):
    ref = item["ref"]
    pvc = None if ref.get("orphan") else _get_or_none(
        f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{ref['name']}")
    if pvc is not None:
        deleting = bool((pvc.get("metadata", {}) or {}).get("deletionTimestamp"))
        return "running", 35 if deleting else 15, (
            "Waiting for workloads to release the claim" if deleting else "Waiting for PVC deletion")
    if ref.get("action") == "delete_claim":
        return "succeeded", 100, "Claim deleted; backing data retained"

    pv = _get_or_none(f"/api/v1/persistentvolumes/{ref['pv']}") if ref.get("pv") else None
    if pv is not None:
        return "running", 70, "Claim deleted; removing the backing persistent volume"
    volume = (_get_or_none(
        f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/{ref['volume']}")
              if ref.get("volume") else None)
    if volume is not None:
        return "running", 90, "Persistent volume deleted; Longhorn is removing replica data"
    return "succeeded", 100, "Claim and backing volume data deleted"


def _smart_test(item):
    if smart_progress is None:
        raise RuntimeError("SMART progress provider is not configured")
    return smart_progress(item["ref"])


def _network_service(item):
    ref = item["ref"]
    namespace, name = ref["namespace"], ref["name"]
    service = kget(f"/api/v1/namespaces/{namespace}/services/{name}")
    spec, status = service.get("spec", {}) or {}, service.get("status", {}) or {}
    selector = urllib.parse.quote(f"kubernetes.io/service-name={name}", safe="")
    slices = kget(
        f"/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices"
        f"?labelSelector={selector}").get("items", [])
    ready = sum(1 for row in slices for endpoint in endpoint_rows(row)
                if endpoint.get("conditions", {}).get("ready") is not False and
                not endpoint.get("conditions", {}).get("terminating", False))
    if spec.get("type") == "LoadBalancer":
        addresses = (status.get("loadBalancer", {}) or {}).get("ingress", []) or []
        if not addresses:
            return "running", 55 if ready else 25, f"{ready} ready endpoint(s); waiting for kube-vip"
    address = spec.get("clusterIP") or "Service"
    return "succeeded", 100, f"{address} active · {ready} ready endpoint(s)"


def endpoint_rows(endpoint_slice):
    return endpoint_slice.get("endpoints", []) or []


RESOLVERS = {
    "deployment": _deployment,
    "image-update": _deployment,
    "image-rollback": _deployment,
    "image-pull": _prepull,
    "image-cleanup": _image_cleanup,
    "volume-delete": _volume_delete,
    "smart-test": _smart_test,
    "network-service": _network_service,
    "import": _job,
    "vm-disk-import": _vm_disk_import,
    "vm-migration": _migration,
    "backup": _backup,
    "volume-restore": _volume_restore,
}


def _refresh(item):
    if item.get("status") in TERMINAL or item.get("status") == CANCELLING:
        return False
    resolver = RESOLVERS.get(item.get("kind"))
    if not resolver:
        return _finish(item, "failed", item.get("progress", 0), "Unknown operation type")
    # A step can move on without its message changing - a new job's name, the
    # next phase - and that has to be written too, or a restart repeats it.
    before = json.dumps(item, sort_keys=True, default=str)
    try:
        return _finish(item, *resolver(item)) or json.dumps(item, sort_keys=True, default=str) != before
    except urllib.error.HTTPError as error:
        if error.code == 404:
            # Which object, so a job that stops here says what went missing.
            path = urllib.parse.urlparse(getattr(error, "url", "") or "").path
            parts = [part for part in path.split("/") if part][-2:]
            what = " ".join(parts) if len(parts) == 2 else "the resource it follows"
            return _finish(item, "failed", item.get("progress", 0),
                           f"Kubernetes no longer has {what}")
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


def dismiss_finished():
    """Clear every job that has finished, leaving anything still running.

    One at a time is fine for a stray failure and tedious after a batch of
    twenty. Active jobs are kept regardless: the tray is how you watch them.
    """
    with _lock:
        items = _read()
        keep = [item for item in items if item.get("status") not in TERMINAL or _receipt_needed(item)]
        removed = len(items) - len(keep)
        if removed:
            _write(keep)
        protected = sum(item.get("status") in TERMINAL for item in keep)
    return {"ok": True, "dismissed": removed, "remaining": len(keep),
            "detail": (f"cleared {removed} finished job" + ("" if removed == 1 else "s")
                       if removed else "nothing finished to clear")
                      + (f"; {len(keep) - protected} still running" if len(keep) > protected else "")
                      + (f"; {protected} power receipt(s) retained for replay protection" if protected else "")}


# Kinds whose steps are each safe to run again, and which say whether a
# stopped job of theirs can carry on: kind -> function(item) -> reason or "".
RESUMABLE = {}


def resume(operation_id):
    """Carry a failed job on from the step it stopped at."""
    with _lock:
        items = _read()
        match = next((item for item in items if item.get("id") == operation_id), None)
        if not match:
            raise ValueError("operation not found")
        check = RESUMABLE.get(match.get("kind"))
        if match.get("status") != "failed" or not check:
            raise ValueError("only a failed job that can carry on can be resumed")
        why_not = check(match)
        if why_not:
            raise ValueError(why_not)
        match.update(status="running", finished_at="", updated_at=_now(),
                     message="Carrying on from where it stopped")
        _note(match, "running", match.get("progress", 0), match["message"])
        _write(items)
    return {"ok": True, "id": operation_id}


def dismiss(operation_id):
    with _lock:
        items = _read()
        match = next((item for item in items if item.get("id") == operation_id), None)
        if not match:
            raise ValueError("operation not found")
        if match.get("status") not in TERMINAL:
            raise ValueError("an active operation cannot be dismissed")
        if _receipt_needed(match):
            raise ValueError("This power receipt is still needed for recovery or replay protection; inspect its job and wait for the approval to expire")
        items = [item for item in items if item.get("id") != operation_id]
        _write(items)
    return {"ok": True, "id": operation_id}


# What cancelling each kind of job does: kind -> (plan, run).
#
#   plan(item) -> dict saying what a cancel would do, read before anything
#     changes, so the person sees the cost first. Keys, all optional:
#       mode      "rollback" puts things back as they were; "stop" halts it,
#                 keeping what is already done; "forget" cannot stop it and
#                 only stops tracking it here.
#       can       False when it cannot be cancelled at this step (why_not says
#                 why), as in the seconds a volume swap takes.
#       undo      what is put back or removed
#       keeps     what stays as it is, and anything that cannot be undone
#       severity  "high" when the cancel deletes or restarts something
#       confirm   a name that must be typed, when data is deleted
#       needs     the role cancelling needs: "operator" or "admin"
#       options   [{"id", "label", "detail", "default"}] choices for the person
#   run(item, options) -> the message the cancelled job is left with. It may
#     change item["ref"], which is kept. Each step must be safe to run again:
#     a cancel interrupted part-way is asked again from the start.
CANCELLERS = {}
# Kinds whose cancel also tidies up after the job has failed: what a failed
# k3s build made - VMs, disks, addresses - stays until something removes it.
CLEANUPS = set()


def _cleanable(item):
    return (item.get("status") == "failed" and item.get("kind") in CLEANUPS and not item.get("cleaned")
            and not item.get("ref", {}).get("retain_resources"))
MODES = {"rollback": "Cancel and put back", "stop": "Cancel it", "forget": "Stop tracking it"}


def _cancel_stale(item):
    try:
        started = float(item.get("cancel_started") or 0)
    except (TypeError, ValueError):
        started = 0
    return time.time() - started > CANCEL_RETRY_AFTER


def _plan_for(item):
    plan = {"mode": "forget", "can": True, "why_not": "", "undo": [], "severity": "low",
            "keeps": ["Homestead has no way to stop this kind of job: it carries on in Kubernetes "
                      "and only stops showing here."],
            "confirm": "", "needs": "operator", "options": []}
    entry = CANCELLERS.get(item.get("kind"))
    if item.get("kind") == "snapshot-delete":
        plan.update(can=False, why_not="Longhorn snapshot removal cannot be undone or safely cancelled")
        return plan
    if entry:
        plan.update(entry[0](item) or {})
    plan["mode"] = plan["mode"] if plan["mode"] in MODES else "stop"
    plan.setdefault("action", MODES[plan["mode"]])
    plan["undo"] = [str(line) for line in plan.get("undo") or []]
    plan["keeps"] = [str(line) for line in plan.get("keeps") or []]
    return plan


def _active(operation_id):
    match = next((item for item in _read() if item.get("id") == operation_id), None)
    if not match:
        raise ValueError("operation not found")
    if match.get("status") in TERMINAL and not _cleanable(match):
        raise ValueError(f"it has {match['status']} already, so there is nothing left to cancel")
    return match


def cancel_plan(operation_id):
    """What cancelling a job would do, before anything is changed."""
    with _lock:
        item = _active(operation_id)
    plan = _plan_for(item)
    return {"id": item["id"], "kind": item.get("kind", ""), "title": item.get("title", ""),
            "status": item.get("status", ""), "progress": item.get("progress", 0),
            "message": item.get("message", ""), "resource": item.get("resource") or {},
            "cleanup": item.get("status") == "failed", **plan}


def cancel(operation_id, options=None, confirm="", allowed=None, by=""):
    """Cancel a job: stop it, and put back what it changed where that can be.

    The job is marked as being cancelled first, under the lock the poll takes,
    so no step of it runs while it is put back. The work itself runs outside
    the lock - some of it waits for pods to go - and the job is left
    cancelled, or, if putting it back failed, running again as it was, with
    the reason, so nothing is left pretending to be finished."""
    with _lock:
        items = _read()
        match = next((item for item in items if item.get("id") == operation_id), None)
        if not match:
            raise ValueError("operation not found")
        status = match.get("status")
        if status in TERMINAL and not _cleanable(match):
            raise ValueError(f"it has {status} already, so there is nothing left to cancel")
        if status == CANCELLING and not _cancel_stale(match):
            raise ValueError("it is being cancelled already")
        plan = _plan_for(match)
        if not plan["can"]:
            raise ValueError(plan["why_not"] or "it cannot be cancelled at this step")
        if allowed and not allowed(plan["needs"]):
            raise PermissionError(f"cancelling this job needs the {plan['needs']} role")
        if plan["confirm"] and str(confirm or "").strip() != plan["confirm"]:
            raise ValueError(f"type {plan['confirm']} to confirm")
        chosen = {row["id"]: bool((options or {}).get(row["id"], row.get("default", False)))
                  for row in plan["options"]}
        before = match.get("previous_status") or status
        match.update(status=CANCELLING, previous_status=before, cancel_started=time.time(),
                     updated_at=_now(), message=f"Cancelling: {plan['action'].lower()}")
        _note(match, CANCELLING, match.get("progress", 0), match["message"])
        _write(items)
        work = json.loads(json.dumps(match))
    entry = CANCELLERS.get(work.get("kind"))
    try:
        message = entry[1](work, chosen) if entry else ""
    except Exception as error:
        with _lock:
            items = _read()
            match = next((item for item in items if item.get("id") == operation_id), None)
            if match:
                match["ref"] = work.get("ref", match.get("ref"))
                match.pop("cancel_started", None)
                match.update(status=match.pop("previous_status", "running"), updated_at=_now(),
                             message=f"Cancelling did not finish: {error}"[:500])
                _write(items)
        raise ValueError(f"cancelling did not finish: {error}") from error
    message = message or ("Stopped tracking it here; it carries on in Kubernetes"
                          if plan["mode"] == "forget" else "Cancelled")
    with _lock:
        items = _read()
        match = next((item for item in items if item.get("id") == operation_id), None)
        if match:
            match["ref"] = work.get("ref", match.get("ref"))
            match.pop("cancel_started", None)
            match.pop("previous_status", None)
            if by:
                match["cancelled_by"] = str(by)[:120]
            if before == "failed":
                # It failed; that stays its outcome. Only what it left is gone.
                match.update(status="failed", cleaned=True, updated_at=_now(),
                             message=f"Cleaned up after failing: {message}"[:500])
                _note(match, "failed", match.get("progress", 0), match["message"])
            else:
                _finish(match, "cancelled", match.get("progress", 0), message)
            _write(items)
    return {"ok": True, "id": operation_id, "detail": message,
            "operation": _public(match) if match else None}


# Where a kind of job has output of its own - a pod's log, a VM's console -
# kind -> function(item) -> [{"title", "text", "note"}]. Read only when asked.
LOGGERS = {}


def log(operation_id):
    """A job's steps so far and, where it has one, its own output."""
    with _lock:
        item = next((row for row in _read() if row.get("id") == operation_id), None)
    if not item:
        raise ValueError("operation not found")
    sources, reader = [], LOGGERS.get(item.get("kind"))
    if reader:
        try:
            sources = reader(item) or []
        except Exception as error:
            sources = [{"title": "Output", "text": "", "note": f"could not be read: {error}"[:300]}]
    return {**_public(item), "history": item.get("history") or [], "sources": sources}
