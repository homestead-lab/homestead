"""Shared-journal fence for Homestead writes to storage held by a move.

This is a guard inside Homestead, not a Kubernetes admission webhook or protection
from external administrators/controllers. Each checked API call holds the same
lock as move creation and progression. Only the owning dispatcher can exempt its
own record, never another move's record.
"""
import contextlib
import re
import threading
import urllib.parse

_local = threading.local()
_PVC = re.compile(r"^/api/v1/namespaces/([^/]+)/persistentvolumeclaims(?:/([^/]+))?$" )
_PV = re.compile(r"^/api/v1/persistentvolumes(?:/([^/]+))?$" )
_LH = re.compile(r"^/apis/longhorn.io/[^/]+/namespaces/([^/]+)/volumes(?:/([^/]+))?$" )
_JOB = re.compile(r"^/apis/batch/v1/namespaces/([^/]+)/jobs(?:/([^/]+))?$" )
_SNAPSHOT = re.compile(r"^/apis/longhorn.io/[^/]+/namespaces/([^/]+)/snapshots(?:/([^/]+))?$" )

PROTECTED = "Storage is protected by an active or interrupted move; review that job before changing or deleting this data"


@contextlib.contextmanager
def dispatching(item):
    if item.get("kind") != "reclass" or not item.get("id"):
        raise ValueError("Storage dispatcher identity is invalid")
    previous = getattr(_local, "job", None)
    _local.job = item["id"]
    try:
        yield
    finally:
        _local.job = previous


def _active(items):
    return [item for item in items if item.get("kind") == "reclass" and
            (item.get("status") not in ("succeeded", "failed", "cancelled") or item.get("ref", {}).get("retain_resources") or
             (item.get("status") in ("failed", "cancelled") and item.get("ref", {}).get("phase") not in ("done", "rolled-back")))]


def _matches(item, namespace="", claim="", pv="", handle="", job=""):
    ref = item["ref"]
    claims = {n for n in (ref.get("claim"), ref.get("temp")) if n}
    pvs = {n for n in (ref.get("old_pv"), ref.get("new_pv"), ref.get("cutover", {}).get("old_pv"), ref.get("cutover", {}).get("new_pv")) if n}
    handles = set()
    for pinned in ref.get("copy_claims", {}).values():
        if pinned.get("pv"): pvs.add(pinned["pv"])
        if pinned.get("csi_driver") == "driver.longhorn.io" and pinned.get("csi_handle"):
            handles.add(pinned["csi_handle"])
    jobs = {n for n in (ref.get("job"), ref.get("job_name")) if n}
    return ((namespace == ref.get("namespace") and (claim in claims or job in jobs)) or pv in pvs or handle in handles)


def _objects(read, path):
    data = read(path)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or data.get("metadata", {}).get("continue"):
        raise ValueError("Storage protection inventory is incomplete; nothing was changed")
    if any(not isinstance(obj, dict) or not obj.get("metadata", {}).get("name") for obj in data["items"]):
        raise ValueError("Storage protection inventory is invalid; nothing was changed")
    return data["items"]


def reasons(items, *, namespace="", claim="", pv="", handle="", job="", read=None):
    active = _active(items)
    matched = [item for item in active if _matches(item, namespace, claim, pv, handle, job)]
    # A provisioner may bind the new claim between Homestead polls. Match its
    # live claimRef as well as already journaled PV/CSI handles.
    candidates = []
    if active and read and handle:
        candidates = [obj for obj in _objects(read, "/api/v1/persistentvolumes") if
                      obj.get("spec", {}).get("csi", {}).get("driver") == "driver.longhorn.io" and
                      obj.get("spec", {}).get("csi", {}).get("volumeHandle") == handle]
    elif active and read and pv:
        try:
            candidates = [read("/api/v1/persistentvolumes/" + pv)]
        except Exception as error:
            # A missing PV can still be protected by a journaled name.
            if getattr(error, "code", None) != 404:
                raise
    for obj in candidates:
        owner = obj.get("spec", {}).get("claimRef", {})
        matched.extend(item for item in active if _matches(item, owner.get("namespace"), owner.get("name"), obj["metadata"]["name"], handle))
    return sorted({item["id"] for item in matched})


def review(plan, ops, read):
    with ops._lock:
        jobs = reasons(ops._read(), namespace=plan.get("namespace"), claim=plan.get("name"),
                       pv=plan.get("pv", {}).get("name"), handle=plan.get("longhorn", {}).get("name"), read=read)
    if jobs:
        plan["blocked"] = True
        plan.setdefault("blocking_reasons", []).append("This data is protected by an active or interrupted storage move. Finish its recovery before deleting it.")
        for action in ("delete_claim", "delete_data"):
            plan.setdefault("actions", {}).setdefault(action, {})["enabled"] = False
    return plan


@contextlib.contextmanager
def volume(ops, read, handle):
    """Fence the whole snapshot workflow, including stops and Longhorn REST calls."""
    if not handle:
        raise ValueError("Backing volume identity is missing; nothing was changed")
    with ops._lock:
        if reasons(ops._read(), handle=handle, read=read):
            raise ValueError(PROTECTED)
        yield


def send(method, path, body, dispatch, ops, read):
    if method not in ("POST", "PUT", "PATCH", "DELETE"):
        return dispatch()
    path_only = urllib.parse.unquote(urllib.parse.urlsplit(path).path).rstrip("/")
    pvc, pv, lh, job_match = _PVC.fullmatch(path_only), _PV.fullmatch(path_only), _LH.fullmatch(path_only), _JOB.fullmatch(path_only)
    snapshot = _SNAPSHOT.fullmatch(path_only)
    if not any((pvc, pv, lh, job_match, snapshot)):
        return dispatch()
    namespace, claim, volume, handle, job = "", "", "", "", ""
    name = ((body or {}).get("metadata") or {}).get("name") if method == "POST" and isinstance(body, dict) else None
    if pvc: namespace, claim = pvc[1], pvc[2] or name
    if pv: volume = pv[1] or name
    if lh: handle = lh[2] or name
    if job_match: namespace, job = job_match[1], job_match[2] or name
    with ops._lock:
        items = [item for item in ops._read() if item["id"] != getattr(_local, "job", None)]
        if snapshot and _active(items) and (snapshot[2] or method == "POST"):
            obj = body if method == "POST" else read(path_only)
            handle = (obj.get("spec") or {}).get("volume") if isinstance(obj, dict) else None
            if not handle:
                raise ValueError("Snapshot backing volume cannot be verified; nothing was changed")
        if not any((claim, volume, handle, job)):
            # Collection deletes cannot omit individually protected names.
            blocked = [i["id"] for i in _active(items) if not (pvc or job_match) or i["ref"].get("namespace") == namespace]
        else:
            blocked = reasons(items, namespace=namespace, claim=claim, pv=volume, handle=handle, job=job, read=read)
        if blocked:
            raise ValueError(PROTECTED)
        return dispatch()
