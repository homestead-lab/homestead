"""Reviewed recovery of a held durable storage move: retry, finish, roll back, pause.

These actions change only the shared journal under its lock; the resolver does
the work with its usual checks. They never repeat a request whose outcome is
unknown (no receipt, lost or unverified reply), never delete data and never
claim that pausing stops an already accepted copy Job.
- Retry this step: the same step again with fresh checks and a new deadline. A
  request Kubernetes rejected (it applied nothing) is set aside and sent again.
- Finish the move: the same, once the copy is verified, towards the new volume.
- Roll back: restart what the move stopped on the original volume, offered
  only while the journal shows the original claim was never released.
"""
import copy
import time

import homestead_capacity_review as REVIEW
import homestead_reclass_handoff as HANDOFF
import homestead_storage_journal as JOURNAL
import homestead_storage_workflow as WORKFLOW


def _job(items, ident):
    item = next((i for i in items if i["id"] == ident), None)
    if item is None:
        raise ValueError("Storage move was not found")
    WORKFLOW.check(item)
    return item


ACTIONS = ("retry", "finish", "continue", "rollback", "pause")


def _set_aside(item):
    """Move rejected requests out of a retry's way; kept, in order, as history.

    Kubernetes answered those with a refusal (400-422), so nothing was applied."""
    ref = item["ref"]
    refused = [e for e in ref.get("storage_writes", []) if e.get("state") == "refused"]
    if refused:
        ref["storage_writes"] = [e for e in ref["storage_writes"] if e.get("state") != "refused"]
        ref.setdefault("storage_refused", []).extend(refused)
    ref.pop("storage_wait", None)
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
    ref, phase = item["ref"], item["ref"]["handoff_phase"]
    inspection = WORKFLOW.inspect(_set_aside(copy.deepcopy(item)), helper_admission, restart_admission, runtime_check=runtime_check)
    rows = _resources(item, read)
    warnings = sorted({warning for d in inspection["decisions"] for warning in d["warnings"]})
    warnings.append("This review does not delete data. Continuing may restart workloads and never rolls back or repeats an uncertain request.")
    writes = ref.get("storage_writes", [])
    unresolved = [e for e in writes if e.get("state") not in ("accepted", "refused")]
    if any(e.get("state") == "refused" for e in writes):
        warnings.append("Kubernetes rejected a request, so it applied nothing. Retrying sends it again after fresh checks.")
    blockers = list(inspection["blockers"])
    blockers.extend(reason for decision in inspection["decisions"] for reason in decision["blockers"])
    common = []
    if unresolved:
        common.append("An API request has no confirmed outcome. Inspect its Kubernetes audit trail and retained resources; this screen cannot safely retry or adopt it.")
    if any(row["relationship"] == "unavailable" for row in rows):
        common.append("Some retained resources could not be read; restore access before continuing.")
    blockers += common
    held = item.get("status") == "failed" and bool(ref.get("storage_hold"))
    can_continue = held and phase not in ("done", "rolled-back") and not blockers
    verified = bool(ref.get("copy_verified")) and phase in ("copy", "cutover", "restart")
    can_pause = item.get("status") in ("queued", "running") and phase not in ("done", "rolled-back")
    rollback = {"next_write": None, "blockers": [], "decisions": [], "message": ""}
    if not held or not HANDOFF.can_roll_back(item):
        rollback["blockers"].append("Rolling back is offered for a held move whose original claim was never released."
                                    if held else "Pause the move before rolling it back.")
    else:
        trial = _set_aside(copy.deepcopy(item))
        trial["ref"]["handoff_phase"] = "rollback"
        found = WORKFLOW.inspect(trial, helper_admission, restart_admission, runtime_check=runtime_check)
        rollback.update(next_write=found["next_write"], decisions=found["decisions"], message=found.get("message", ""),
                        blockers=sorted(set(common + found["blockers"] +
                                            [r for d in found["decisions"] for r in d["blockers"]])))
        warnings.extend(sorted({w for d in found["decisions"] for w in d["warnings"]} - set(warnings)))
    can_rollback = not rollback["blockers"]
    if can_rollback:
        warnings.append("Rolling back restarts what this move stopped on the original volume. The copy volume is kept; nothing is deleted.")
    allowed = {"continue": can_continue, "retry": can_continue and not verified, "finish": can_continue and verified,
               "rollback": can_rollback, "pause": can_pause}
    plan = {"id": item["id"], "claim": ref["claim"], "namespace": ref["namespace"],
            "from_class": ref.get("from_class"), "to_class": ref["target"],
            "phase": phase, "status": item.get("status"), "message": ref.get("storage_hold") or inspection.get("message", ""),
            **{"can_" + action: value for action, value in allowed.items()},
            "blockers": sorted(set(blockers)), "warnings": warnings, "resources": rows, "requires_confirmation": True,
            "next_write": inspection["next_write"],
            "rollback": {key: rollback[key] for key in ("next_write", "blockers", "message")},
            "workloads": [{"kind": c["kind"], "name": c["name"]} for c in ref["consumers"]]}
    context = {"action": "storage-recovery", "actor": actor, "job": inspection["job_digest"],
               "resources": rows, "next_write": inspection["next_write"], "decisions": inspection["decisions"],
               "blockers": plan["blockers"], "rollback": rollback, "allowed": allowed}
    return plan, context, {"continue": inspection["decisions"], "rollback": rollback["decisions"]}


def preview(ident, ops, read, actor, helper_admission, restart_admission, *, runtime_check=None):
    with ops.transaction():
        plan, context, _ = _snapshot(_job(ops.stored(), ident), read, actor, helper_admission, restart_admission, runtime_check)
        tokens = {action: REVIEW.issue({"id": ident, "action": action}, context) for action in ACTIONS
                  if plan["can_" + action]}
        return {"plan": plan, "tokens": tokens}


DETAIL = {"pause": "Move paused; data and running copy jobs retained",
          "rollback": "Move queued to roll back onto the original volume with fresh safety checks",
          "finish": "Move queued to finish on the copied volume with fresh safety checks"}


def act(body, ops, read, actor, helper_admission, restart_admission, *, runtime_check=None):
    ident, action = str(body.get("id") or ""), body.get("action")
    if action not in ACTIONS:
        raise ValueError("Choose Retry, Finish, Roll back or Pause")
    with ops.transaction() as t:
        items = t.items
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
            _set_aside(item)
            chosen = decisions["continue"]
            if action == "rollback":
                ref.update(rollback_from=ref["handoff_phase"], handoff_phase="rollback")
                chosen = decisions["rollback"]
            grants = ref.setdefault("storage_approvals", {})
            for decision in chosen:
                grants.setdefault(decision["phase"], {})[decision["role"]] = {
                    key: decision[key] for key in ("warnings", "shapes")}
            ref.pop("storage_hold", None)
            item["finished_at"] = ""
            ops._finish(item, "running", item.get("progress", 0),
                        "Review accepted; rolling back onto the original volume with fresh checks" if action == "rollback" else
                        "Review accepted; continuing from confirmed steps with fresh checks")
        t.save(items)
        return {"ok": True, "operation": ops._public(item),
                "detail": DETAIL.get(action, "Move queued to continue with fresh safety checks")}
