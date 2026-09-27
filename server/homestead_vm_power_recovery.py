"""Explicit, read-only-cluster reconciliation of uncertain power tracking.

This never sends power and never claims that a timed-out API call was cancelled.
The shared worker lock proves only that Homestead's synchronous dispatcher has
left its critical section; a remote request may still have a late effect.
"""
import copy
from contextlib import contextmanager
import time
import urllib.error
from urllib.parse import quote

import homestead_capacity_review as REVIEW
import homestead_vm_power_job as POWER


class DispatcherBusy(ValueError):
    pass


@contextmanager
def _exclusive(ident, ops):
    lock = POWER.worker_lock(ident, ops, timeout=0)
    try:
        lock.acquire()
    except TimeoutError as error:
        raise DispatcherBusy("The dispatcher is active; wait and review again") from error
    try:
        yield
    finally:
        lock.release()


def _object(read, path):
    try:
        value = read(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ValueError(f"Recovery inventory is unavailable (HTTP {error.code}); nothing was changed") from error
    except OSError as error:
        raise ValueError("Recovery inventory could not be reached; nothing was changed") from error
    if not isinstance(value, dict):
        raise ValueError("Recovery inventory is malformed; nothing was changed")
    return value


def _identity(value, namespace, name):
    if value is None:
        return None
    meta = value.get("metadata") or {}
    if (meta.get("namespace"), meta.get("name")) != (namespace, name) or not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("Recovery resource identity/version could not be verified")
    return {key: meta[key] for key in ("namespace", "name", "uid", "resourceVersion")}


def _snapshot(item, read, actor):
    ref = item.get("ref") or {}
    if item.get("kind") != "vm-power" or item.get("status") in ("succeeded", "failed", "cancelled", "cancelling") or ref.get("phase") not in ("uncertain", "dispatching"):
        raise ValueError("This job does not have an unresolved uncertain power dispatch")
    blockers = []
    if ref.get("dispatch_protocol") != 1:
        blockers.append("This legacy job has no verifiable dispatcher fence; manual journal recovery is required before resolving it.")
    ns, name = ref["namespace"], ref["name"]
    base = f"{POWER.API}/namespaces/{quote(ns, safe='')}"
    vm = _object(read, base + "/virtualmachines/" + quote(name, safe=""))
    vmi = _object(read, base + "/virtualmachineinstances/" + quote(name, safe=""))
    vid, iid = _identity(vm, ns, name), _identity(vmi, ns, name)
    vmstate, istate = (vm or {}).get("status") or {}, (vmi or {}).get("status") or {}
    requests = vmstate.get("stateChangeRequests")
    if requests is None:
        requests = []
    if not isinstance(requests, list):
        raise ValueError("VM pending power-request inventory is malformed")
    if requests:
        blockers.append("KubeVirt still reports queued power changes. Wait and inspect the VM before resolving this record.")
    if ((vm or {}).get("metadata") or {}).get("deletionTimestamp") or ((vmi or {}).get("metadata") or {}).get("deletionTimestamp"):
        blockers.append("The VM or instance is deleting. Wait for it to settle, then review again.")
    owned = vmi is None or (vm is not None and any(
        owner.get("controller") is True and owner.get("kind") == "VirtualMachine" and
        owner.get("apiVersion", "").split("/")[0] == "kubevirt.io" and
        (owner.get("uid"), owner.get("name")) == (vid["uid"], name)
        for owner in ((vmi.get("metadata") or {}).get("ownerReferences") or [])))
    if not owned:
        blockers.append("The instance cannot be attributed to the current VM; inspect ownership before resolving.")
    conditions = istate.get("conditions") or []
    evidence = {"vm": vid, "instance": iid, "same_vm": bool(vid and vid["uid"] == ref["uid"]),
                "run_strategy": (vm.get("spec", {}).get("runStrategy") or ("Always" if vm.get("spec", {}).get("running") else "Halted")) if vm else "Not applicable",
                "vm_status": vmstate.get("printableStatus", "Unknown") if vm else "Not found",
                "instance_phase": istate.get("phase", "Unknown") if vmi else "Not found", "owned": owned,
                "ready": any(row.get("type") == "Ready" and row.get("status") == "True" for row in conditions),
                "paused": any(row.get("type") == "Paused" and row.get("status") == "True" for row in conditions),
                "queued_changes": len(requests)}
    warnings = ["This resolves Homestead's tracking record only. It sends no power request and changes no VM, disk, Secret or restart policy.",
                "The Homestead dispatcher is not holding this job's lock, but Kubernetes may still apply the earlier request late. An empty queue or a running VM does not prove which request caused it.",
                "Acknowledging releases this job's block on future power reviews. It does not retry or roll back anything; any later action needs its own fresh review.",
                "The original outcome will remain unknown in the audit record, even if the guest is currently running. Guest application health is not verified."]
    if not evidence["same_vm"]:
        warnings.append("The original VM is missing or has been replaced. This review does not adopt or change the replacement.")
    plan = {"blocked": bool(blockers), "blockers": blockers, "warnings": warnings, "requires_confirmation": True,
            "id": item["id"], "resource": {"namespace": ns, "name": name, "original_uid": ref["uid"]}, "action": ref["action"],
            "dispatch_phase": ref["phase"], "observed": evidence, "confirm": name}
    context = {"action": "vm-power-recovery", "actor": actor, "job_id": item["id"],
               "ref": copy.deepcopy(ref), "evidence": evidence}
    return plan, context


def _job(items, ident):
    item = next((row for row in items if row.get("id") == ident), None)
    if item is None:
        raise ValueError("Power recovery job not found")
    return item


def preview(ident, ops, read, actor):
    try:
        with _exclusive(ident, ops), ops._lock:
            plan, context = _snapshot(_job(ops._read(), ident), read, actor)
            return {"plan": plan, "capacity_token": REVIEW.issue({"id": ident}, context)}
    except DispatcherBusy:
        return {"plan": {"id": ident, "blocked": True, "blockers": ["A Homestead dispatcher is still active for this job. Wait; it cannot be resolved while sending power."],
                         "warnings": [], "requires_confirmation": True}, "capacity_token": None}


def resolve(body, ops, read, actor):
    ident = str(body.get("id") or "")
    try:
        with _exclusive(ident, ops), ops._lock:
            items = ops._read()
            item = _job(items, ident)
            plan, context = _snapshot(item, read, actor)
            cfg = {"id": ident, "capacity_token": body.get("capacity_token"), "confirm_capacity": body.get("confirm_capacity")}
            REVIEW.enforce(cfg, plan, context)
            if body.get("acknowledge_unknown") is not True or body.get("confirm") != plan["confirm"]:
                raise ValueError("Type the VM name and explicitly acknowledge the unknown outcome and possible late effect")
            ref = item["ref"]
            ref["recovery"] = {"by": actor, "at": time.time(), "previous_phase": ref["phase"],
                               "observed": copy.deepcopy(plan["observed"]), "outcome": "unknown",
                               "late_effect_acknowledged": True}
            ref.update(phase="resolved-unknown", retain_resources=False)
            ops._finish(item, "failed", item.get("progress", 0),
                        f"{actor} acknowledged an unknown power outcome after inspection. No retry or rollback was sent; a late effect is still possible.")
            ops._write(items)
            return {"ok": True, "detail": "Tracking resolved as unknown; no cluster change was sent. Any new power action requires a fresh review.",
                    "operation": ops._public(item)}
    except DispatcherBusy as error:
        raise ValueError("The dispatcher is active; nothing was resolved. Wait and review again.") from error
