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
import homestead_import_job as IMPORT


def _snapshot(item, read, actor):
    ref = item.get("ref") or {}
    batch = item.get("kind") == "k3s-cluster"
    importing = item.get("kind") == IMPORT.KIND
    if (item.get("kind") not in ("vm-create", "vm-edit", "k3s-cluster", IMPORT.KIND) or not ref.get("retain_resources")
            or item.get("status") in ("succeeded", "cancelled", "cancelling")
            or ref.get("phase") not in (("provisioning", "awaiting-ready", "failed") if batch else ("prepared", "writing", "failed", "copying") if importing else ("prepared", "writing", "failed"))):
        raise ValueError("This job has no unresolved configuration dispatch")
    if ref.get("dispatch_protocol") != (2 if batch else 1):
        raise ValueError("This older job has no dispatcher fence that can be checked. Its record must be recovered manually.")
    names = [row["name"] for row in ref["nodes"]] if batch else [ref["name"]]
    if not names or len(names) != len(set(names)):
        raise ValueError("Recovery target list is malformed")
    targets = [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "namespace": ref["namespace"], "name": name} for name in names]
    if importing:
        targets = copy.deepcopy(ref["planned"])
    entries = ref.get("writes", [])
    for entry in entries:
        if entry["resource"] not in targets:
            targets.append(entry["resource"])
    observed, blockers = [], []
    for target in targets:
        version, kind, ns, name = (target[key] for key in ("apiVersion", "kind", "namespace", "name"))
        plural = next((plural for (api, plural), resource_kind in WRITE.RESOURCES.items() if (api, resource_kind) == (version, kind)), None)
        if not plural or not WRITE.NAME.fullmatch(ns) or not WRITE.NAME.fullmatch(name):
            raise ValueError("Recovery target is malformed")
        prefix = "/api/v1" if version == "v1" else "/apis/" + version
        value = RECOVERY._object(read, f"{prefix}/namespaces/{quote(ns, safe='')}/{plural}/{quote(name, safe='')}")
        identity = RECOVERY._identity(value, ns, name)
        if value is not None and (value.get("kind"), value.get("apiVersion")) != (kind, version):
            raise ValueError("Recovery resource type could not be verified")
        deleting = bool((value or {}).get("metadata", {}).get("deletionTimestamp"))
        if deleting:
            blockers.append(f"{kind} {ns}/{name} is deleting. Wait for it to settle, then review again.")
        receipts = [entry for entry in entries if entry["resource"] == target]
        last = receipts[-1] if receipts else {}
        expected = last.get("identity") or last.get("before") or (ref.get("identity") if target == targets[0] else None)
        if importing and kind == "PersistentVolumeClaim" and not expected:
            expected = ref.get("claims", {}).get(name)
        observed.append({"resource": target, "current": identity, "expected": expected,
                         "relationship": "not found" if not identity else "identity unproven" if not expected else
                         "same identity" if identity["uid"] == expected["uid"] else "replacement; not adopted",
                         "last_write": last.get("phase", "not dispatched"), "deleting": deleting})
    warnings = ["This only resolves the tracking. It changes no VM, image, disk or Secret, and anything already created stays so it can be checked.",
                "The Homestead dispatcher is no longer active, but Kubernetes may still apply a previously sent request late. Current state does not prove its original outcome.",
                "Accepted receipts prove only that individual resource writes were acknowledged, not that the full save completed or the guest is healthy.",
                "This releases the job's block on future VM reviews. It never retries, adopts replacements or rolls back resources. The old approval remains consumed."]
    if batch:
        warnings.append("All planned VMs are shown, including those not sent. Guest installation may continue. This does not check k3s health or free IP addresses, and it does not resume the rest of the batch.")
    if importing:
        blockers += IMPORT.recovery_blockers(item, read)
        warnings = ["This records an unknown outcome only. It does not retry the import, start the app, remove its safety hold or delete any workload, Service, Job or volume.",
                    "Inspect partial files and retained resources before a new review. Accepted write receipts do not prove data integrity or a complete import. A previously sent request can still take effect late."]
    plan = {"id": item["id"], "blocked": bool(blockers), "blockers": blockers, "warnings": warnings,
            "requires_confirmation": True, "confirm": ref["name"], "resource": {"namespace": ref["namespace"], "name": ref["name"]},
            "action": item["kind"], "dispatch_phase": ref["phase"], "resources": observed}
    context = {"action": "vm-mutation-recovery", "actor": actor, "job_id": item["id"], "ref": copy.deepcopy(ref), "resources": observed}
    return plan, context


def preview(ident, ops, read, actor):
    try:
        with RECOVERY._exclusive(ident, ops), ops.transaction():
            plan, context = _snapshot(RECOVERY._job(ops.stored(), ident), read, actor)
            return {"plan": plan, "capacity_token": REVIEW.issue({"id": ident}, context)}
    except RECOVERY.DispatcherBusy:
        return {"plan": {"id": ident, "blocked": True, "blockers": ["The configuration dispatcher is active. Wait before inspecting its outcome."],
                         "warnings": [], "requires_confirmation": True}, "capacity_token": None}


def resolve(body, ops, read, actor):
    ident = str(body.get("id") or "")
    try:
        with RECOVERY._exclusive(ident, ops), ops.transaction() as t:
            items = t.items
            item = RECOVERY._job(items, ident)
            plan, context = _snapshot(item, read, actor)
            REVIEW.enforce({"id": ident, "capacity_token": body.get("capacity_token"), "confirm_capacity": body.get("confirm_capacity")}, plan, context)
            if body.get("acknowledge_unknown") is not True or (item["kind"] != IMPORT.KIND and body.get("confirm") != plan["confirm"]):
                raise ValueError("Type the reviewed name and tick each acknowledgement. They cover retained resources, "
                                 "the unknown outcome and possible late effects")
            ref = item["ref"]
            ref["recovery"] = {"by": actor, "at": time.time(), "previous_phase": ref["phase"],
                               "resources": copy.deepcopy(plan["resources"]), "outcome": "unknown", "late_effect_acknowledged": True}
            ref.update(phase="resolved-unknown", retain_resources=False)
            if item["kind"] == "k3s-cluster":
                item.update(tracking_stopped=True, tracking_stopped_at=ops._now())
            importing = item["kind"] == IMPORT.KIND
            ops._finish(item, "failed", item.get("progress", 0),
                        f"{actor} checked the unfinished {'import' if importing else 'VM save'}, and its outcome is still unknown. The resources are kept. Nothing was retried or rolled back, and a late effect is still possible.")
            t.save(items)
            return {"ok": True, "detail": "Tracking resolved as unknown, and nothing was sent to the cluster. The resources and import holds stay. Any new action needs a new review." if importing else "Tracking resolved as unknown; no cluster change was sent. Resources remain and any new VM action requires a fresh review.",
                    "operation": ops._public(item)}
    except RECOVERY.DispatcherBusy:
        raise ValueError("The dispatcher is running, so nothing was resolved. Wait and review again.") from None
