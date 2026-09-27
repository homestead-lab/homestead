"""Read-only inspection and explicit release of an interrupted VM save journal.

Releasing tracking is not cleanup, retry, rollback or proof that writes failed.
The dispatcher fence cannot cancel a request already received by Kubernetes.
"""
import copy
import time
from urllib.parse import quote

import homestead_capacity_review as REVIEW
import homestead_vm_power_recovery as RECOVERY
import homestead_vm_write as WRITE


def _snapshot(item, read, actor):
    ref = item.get("ref") or {}
    batch = item.get("kind") == "k3s-cluster"
    if (item.get("kind") not in ("vm-create", "vm-edit", "k3s-cluster") or not ref.get("retain_resources")
            or item.get("status") in ("succeeded", "cancelled", "cancelling")
            or ref.get("phase") not in (("provisioning", "awaiting-ready", "failed") if batch else ("prepared", "writing", "failed"))):
        raise ValueError("This job has no unresolved VM configuration dispatch")
    if ref.get("dispatch_protocol") != (2 if batch else 1):
        raise ValueError("This legacy job has no verifiable dispatcher fence; manual journal recovery is required")
    names = [row["name"] for row in ref["nodes"]] if batch else [ref["name"]]
    if not names or len(names) != len(set(names)):
        raise ValueError("VM recovery target list is malformed")
    targets = [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "namespace": ref["namespace"], "name": name} for name in names]
    entries = ref.get("writes", [])
    for entry in entries:
        if entry["resource"] not in targets:
            targets.append(entry["resource"])
    observed, blockers = [], []
    for target in targets:
        version, kind, ns, name = (target[key] for key in ("apiVersion", "kind", "namespace", "name"))
        plural = next((plural for (api, plural), resource_kind in WRITE.RESOURCES.items() if (api, resource_kind) == (version, kind)), None)
        if not plural or not WRITE.NAME.fullmatch(ns) or not WRITE.NAME.fullmatch(name):
            raise ValueError("VM recovery target is malformed")
        prefix = "/api/v1" if version == "v1" else "/apis/" + version
        value = RECOVERY._object(read, f"{prefix}/namespaces/{quote(ns, safe='')}/{plural}/{quote(name, safe='')}")
        identity = RECOVERY._identity(value, ns, name)
        if value is not None and (value.get("kind"), value.get("apiVersion")) != (kind, version):
            raise ValueError("VM recovery resource type could not be verified")
        deleting = bool((value or {}).get("metadata", {}).get("deletionTimestamp"))
        if deleting:
            blockers.append(f"{kind} {ns}/{name} is deleting. Wait for it to settle, then review again.")
        receipts = [entry for entry in entries if entry["resource"] == target]
        last = receipts[-1] if receipts else {}
        expected = last.get("identity") or last.get("before") or (ref.get("identity") if target == targets[0] else None)
        observed.append({"resource": target, "current": identity, "expected": expected,
                         "relationship": "not found" if not identity else "identity unproven" if not expected else
                         "same identity" if identity["uid"] == expected["uid"] else "replacement; not adopted",
                         "last_write": last.get("phase", "not dispatched"), "deleting": deleting})
    warnings = ["This resolves tracking only. It changes no VM, image, disk or Secret; partial resources remain for inspection.",
                "The Homestead dispatcher is no longer active, but Kubernetes may still apply a previously sent request late. Current state does not prove its original outcome.",
                "Accepted receipts prove only that individual resource writes were acknowledged, not that the full save completed or the guest is healthy.",
                "This releases the job's block on future VM reviews. It never retries, adopts replacements or rolls back resources. The old approval remains consumed."]
    if batch:
        warnings.append("All planned VMs are shown, including those not dispatched. Guest installation may continue; this does not verify k3s health, free IP addresses or resume the remaining batch.")
    plan = {"id": item["id"], "blocked": bool(blockers), "blockers": blockers, "warnings": warnings,
            "requires_confirmation": True, "confirm": ref["name"], "resource": {"namespace": ref["namespace"], "name": ref["name"]},
            "action": item["kind"], "dispatch_phase": ref["phase"], "resources": observed}
    context = {"action": "vm-mutation-recovery", "actor": actor, "job_id": item["id"], "ref": copy.deepcopy(ref), "resources": observed}
    return plan, context


def preview(ident, ops, read, actor):
    try:
        with RECOVERY._exclusive(ident, ops), ops._lock:
            plan, context = _snapshot(RECOVERY._job(ops._read(), ident), read, actor)
            return {"plan": plan, "capacity_token": REVIEW.issue({"id": ident}, context)}
    except RECOVERY.DispatcherBusy:
        return {"plan": {"id": ident, "blocked": True, "blockers": ["The VM configuration dispatcher is active. Wait before inspecting its outcome."],
                         "warnings": [], "requires_confirmation": True}, "capacity_token": None}


def resolve(body, ops, read, actor):
    ident = str(body.get("id") or "")
    try:
        with RECOVERY._exclusive(ident, ops), ops._lock:
            items = ops._read()
            item = RECOVERY._job(items, ident)
            plan, context = _snapshot(item, read, actor)
            REVIEW.enforce({"id": ident, "capacity_token": body.get("capacity_token"), "confirm_capacity": body.get("confirm_capacity")}, plan, context)
            if body.get("confirm") != plan["confirm"] or body.get("acknowledge_unknown") is not True:
                raise ValueError("Type the reviewed VM or batch name and acknowledge retained resources, the unknown outcome and possible late effects")
            ref = item["ref"]
            ref["recovery"] = {"by": actor, "at": time.time(), "previous_phase": ref["phase"],
                               "resources": copy.deepcopy(plan["resources"]), "outcome": "unknown", "late_effect_acknowledged": True}
            ref.update(phase="resolved-unknown", retain_resources=False)
            if item["kind"] == "k3s-cluster":
                item.update(tracking_stopped=True, tracking_stopped_at=ops._now())
            ops._finish(item, "failed", item.get("progress", 0),
                        f"{actor} inspected the incomplete VM save; outcome remains unknown. Resources retained; no retry or rollback; a late effect remains possible.")
            ops._write(items)
            return {"ok": True, "detail": "Tracking resolved as unknown; no cluster change was sent. Resources remain and any new VM action requires a fresh review.",
                    "operation": ops._public(item)}
    except RECOVERY.DispatcherBusy:
        raise ValueError("The dispatcher is active; nothing was resolved. Wait and review again.") from None
