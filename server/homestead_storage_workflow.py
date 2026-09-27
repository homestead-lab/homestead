"""Durable storage-move orchestration, pending production/UI registration.

The operations lock owns every resolver/inspection call. Stage functions share
one implementation for real work and read-only next-write inspection; preview
has a request-local journal and no cluster writer or durable checkpoint.
"""
import copy

import homestead_reclass as RC
import homestead_reclass_handoff as HANDOFF
import homestead_storage_journal as JOURNAL

PROTOCOL = 1
PHASES = ("stop", "copy", "cutover", "restart", "done")


def check(item):
    ref = item.get("ref", {})
    if item.get("kind") != "reclass" or ref.get("storage_protocol") != PROTOCOL:
        raise JOURNAL.Held("This older storage job requires its existing recovery procedure; it cannot enter the new handoff")
    if ref.get("handoff_phase") not in PHASES:
        raise JOURNAL.Held("Storage move phase is invalid; inspect the job history")


def _decision(role, objects, result):
    return {"role": role, "warnings": sorted(set(result.get("warnings", []))),
            "blockers": sorted(set(result.get("blockers", []) + result.get("reasons", []))),
            "shapes": sorted({JOURNAL.shape(obj) for obj in objects})}


def _approved(item, decision):
    granted = item["ref"].get("storage_approvals", {}).get(item["ref"]["handoff_phase"], {}).get(decision["role"], {})
    return (set(decision["warnings"]) <= set(granted.get("warnings", [])) and
            set(decision["shapes"]) <= set(granted.get("shapes", [])))


def step(item, checkpoint, helper_admission, restart_admission, *, inspecting=False, decisions=None):
    check(item)
    ref, phase = item["ref"], item["ref"]["handoff_phase"]
    if ref.get("storage_hold") and not inspecting:
        raise JOURNAL.Held("Storage move is held; review it before continuing")
    def journal_factory(work, read, send, save):
        return JOURNAL.Journal(work, read, None if inspecting else send, None if inspecting else save, preview=inspecting)
    def admit(role, objects, callback, value):
        result = callback(item, value)
        decision = _decision(role, objects, result)
        decision["phase"] = phase
        if decisions is not None:
            decisions.append(decision)
        if result.get("blocked") is not False:
            raise JOURNAL.Held("Current placement or storage constraints block this step; review capacity before continuing")
        if not inspecting and not _approved(item, decision):
            raise JOURNAL.Held("Review the current capacity warnings before continuing this storage move")
        return result
    helper = lambda obj: admit("helper", [obj], helper_admission, obj)
    restart = lambda proposals: admit("restart", [p["object"] for p in proposals], restart_admission, proposals)
    if phase == "stop":
        if not ref.get("storage_writes"):
            review = RC.plan(ref["namespace"], ref["claim"], ref["target"], capture=True)
            if not review["ok"] or review["_fences"] != ref.get("review_fences"):
                raise JOURNAL.Held("Volume use or identity changed before stopping; review the move again")
        RC.journaled_stop(item, checkpoint, journal_factory=journal_factory)
        ref["handoff_phase"] = "copy"
        checkpoint(item)
        return "running", 5, "Workload stop requests confirmed; waiting before copying"
    if phase == "copy":
        result = HANDOFF.copy_stage(item, checkpoint, helper, journal_factory=journal_factory)
        if ref.get("copy_verified"):
            ref["handoff_phase"] = "cutover"
            checkpoint(item)
        return result
    if phase == "cutover":
        result = HANDOFF.cutover_stage(item, checkpoint, restart, journal_factory=journal_factory)
        if ref.get("cutover_complete"):
            ref["handoff_phase"] = "restart"
            checkpoint(item)
        return result
    if phase == "restart":
        return HANDOFF.restart_stage(item, checkpoint, restart, journal_factory=journal_factory)
    return "succeeded", 100, "Storage move completed; the original copy is retained"


def resolve(item, checkpoint, helper_admission, restart_admission):
    """Failures become an explicit durable hold, not a retry or rollback."""
    try:
        return step(item, checkpoint, helper_admission, restart_admission)
    except Exception as error:
        message = str(error) if isinstance(error, JOURNAL.Held) else "Storage state could not be verified. Inspect retained resources before continuing."
        item["ref"].update(retain_resources=True, storage_hold=message)
        return "failed", item.get("progress", 0), message


def inspect(item, helper_admission, restart_admission):
    """Simulate until the next write or wait using the exact guarded stages."""
    check(item)
    work, decisions = copy.deepcopy(item), []
    before = JOURNAL.digest(work)
    result = {"next_write": None, "blockers": [], "decisions": decisions}
    try:
        # Advancing a phase without an API request is harmless in this copy.
        # Bound the walk so inspection can never spin on a waiting controller.
        for _ in PHASES:
            phase = work["ref"]["handoff_phase"]
            outcome = step(work, lambda _: None, helper_admission, restart_admission, inspecting=True, decisions=decisions)
            result["message"] = outcome[2]
            if work["ref"]["handoff_phase"] == phase or outcome[0] == "succeeded":
                break
    except JOURNAL.NextWrite as proposed:
        result.update(next_write=proposed.plan, message="The next step is ready for review")
    except Exception as error:
        result["blockers"].append(str(error) if isinstance(error, JOURNAL.Held) else "Current storage state could not be verified; check cluster access and retained resources")
    result["blocked"] = bool(result["blockers"])
    result["job_digest"] = before
    result["phase"] = work["ref"]["handoff_phase"]
    return result
