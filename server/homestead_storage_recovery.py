"""Reviewed pause/continue for the durable storage workflow.

These actions change only the shared journal under its lock. They never retry an
uncertain request, release retained data, roll back, or claim that pausing stops
an already accepted copy Job. Production routes are enabled with the new engine.
"""
import copy
import time

import homestead_capacity_review as REVIEW
import homestead_storage_journal as JOURNAL
import homestead_storage_workflow as WORKFLOW


def _job(items, ident):
    item = next((i for i in items if i["id"] == ident), None)
    if item is None:
        raise ValueError("Storage move was not found")
    WORKFLOW.check(item)
    return item


def _resources(item, read):
    last = {entry["target"]["path"]: entry for entry in item["ref"].get("storage_writes", [])}
    rows = []
    for path, entry in last.items():
        target = JOURNAL.target("DELETE", path, None, item["ref"]["namespace"])
        if target != entry["target"]:
            raise JOURNAL.Held("Storage journal target is invalid")
        row = {"resource": target, "receipt": entry["state"], "current": None, "shape": None,
               "relationship": "unavailable", "deleting": False}
        writer = JOURNAL.Journal(copy.deepcopy(item), read, None, None)
        try:
            obj = writer._get(path)
            if obj is None:
                row["relationship"] = "not found"
            else:
                writer._check_target(obj, target)
                row.update(current=JOURNAL.identity(obj), shape=JOURNAL.shape(obj),
                           deleting=bool(obj["metadata"].get("deletionTimestamp")))
                expected = entry.get("after") or entry.get("before") or {}
                row["relationship"] = ("same identity" if row["current"]["uid"] == expected.get("uid") else
                                       "replacement; not adopted" if expected else "identity unproven")
        except JOURNAL.Held:
            pass
        rows.append(row)
    return rows


def _snapshot(item, read, actor, helper_admission, restart_admission, runtime_check=None):
    inspection = WORKFLOW.inspect(item, helper_admission, restart_admission, runtime_check=runtime_check)
    rows = _resources(item, read)
    warnings = sorted({warning for d in inspection["decisions"] for warning in d["warnings"]})
    warnings.append("This review does not delete data. Continuing may restart workloads and never rolls back or repeats an uncertain request.")
    unresolved = [e for e in item["ref"].get("storage_writes", []) if e.get("state") != "accepted"]
    blockers = list(inspection["blockers"])
    blockers.extend(reason for decision in inspection["decisions"] for reason in decision["blockers"])
    if unresolved:
        blockers.append("An API request has no confirmed outcome. Inspect its Kubernetes audit trail and retained resources; this screen cannot safely retry or adopt it.")
    if any(row["relationship"] == "unavailable" for row in rows):
        blockers.append("Some retained resources could not be read; restore access before continuing.")
    phase = item["ref"]["handoff_phase"]
    can_continue = item.get("status") == "failed" and bool(item["ref"].get("storage_hold")) and phase != "done" and not blockers
    can_pause = item.get("status") in ("queued", "running") and phase != "done"
    plan = {"id": item["id"], "claim": item["ref"]["claim"], "namespace": item["ref"]["namespace"],
            "from_class": item["ref"].get("from_class"), "to_class": item["ref"]["target"],
            "phase": phase, "status": item.get("status"), "message": item["ref"].get("storage_hold") or inspection.get("message", ""),
            "can_continue": can_continue, "can_pause": can_pause, "blockers": sorted(set(blockers)),
            "warnings": warnings, "resources": rows, "requires_confirmation": True,
            "next_write": inspection["next_write"],
            "workloads": [{"kind": c["kind"], "name": c["name"]} for c in item["ref"]["consumers"]]}
    context = {"action": "storage-recovery", "actor": actor, "job": inspection["job_digest"],
               "resources": rows, "next_write": inspection["next_write"], "decisions": inspection["decisions"],
               "blockers": plan["blockers"], "can_continue": can_continue, "can_pause": can_pause}
    return plan, context, inspection["decisions"]


def preview(ident, ops, read, actor, helper_admission, restart_admission, *, runtime_check=None):
    with ops._lock:
        plan, context, _ = _snapshot(_job(ops._read(), ident), read, actor, helper_admission, restart_admission, runtime_check)
        tokens = {action: REVIEW.issue({"id": ident, "action": action}, context) for action in ("continue", "pause")
                  if plan["can_" + action]}
        return {"plan": plan, "tokens": tokens}


def act(body, ops, read, actor, helper_admission, restart_admission, *, runtime_check=None):
    ident, action = str(body.get("id") or ""), body.get("action")
    if action not in ("continue", "pause"):
        raise ValueError("Choose Continue or Pause")
    with ops._lock:
        items = ops._read()
        item = _job(items, ident)
        plan, context, decisions = _snapshot(item, read, actor, helper_admission, restart_admission, runtime_check)
        config = {"id": ident, "action": action, "capacity_token": body.get("capacity_token")}
        if not plan["can_" + action] or body.get("confirm_capacity") is not True or not REVIEW.valid(config, context):
            raise ValueError("Review this storage move again and acknowledge its current state before continuing or pausing")
        ref = item["ref"]
        ref["retain_resources"] = True
        ref["storage_recovery"] = {"action": action, "by": actor, "at": time.time(),
                                   "revision": ref.get("storage_recovery", {}).get("revision", 0) + 1}
        if action == "pause":
            ref["storage_hold"] = "Paused: no further handoff steps will run. Already accepted copy jobs may continue; workloads and data remain as they are."
            ops._finish(item, "failed", item.get("progress", 0), ref["storage_hold"])
        else:
            grants = ref.setdefault("storage_approvals", {})
            for decision in decisions:
                grants.setdefault(decision["phase"], {})[decision["role"]] = {
                    key: decision[key] for key in ("warnings", "shapes")}
            ref.pop("storage_hold", None)
            item["finished_at"] = ""
            ops._finish(item, "running", item.get("progress", 0), "Review accepted; continuing from confirmed steps with fresh checks")
        ops._write(items)
        return {"ok": True, "operation": ops._public(item),
                "detail": "Move paused; data and running copy jobs retained" if action == "pause" else "Move queued to continue with fresh safety checks"}
