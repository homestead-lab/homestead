"""What people do to a move: retry it, abandon it, dismiss finished ones, and
finish it by removing the source.

Split out of homestead_move_engine (#623).
"""
import urllib.parse
import homestead_kube as KUBE
import homestead_move_state as STATE
import homestead_move_target as TARGET
import homestead_move_plan as PLAN


def retry(move_id):
    with STATE._serialized(move_id):
        return _retry(move_id)


def _retry(move_id):
    move = STATE._find(move_id)
    if not move:
        raise ValueError("no such move")
    if move.get("status") in ("failed", "cancelled") and move.get("flags", {}).get("cancelling"):
        return _abandon(move_id)
    if move.get("status") in ("succeeded", "cancelled") and move.get("flags", {}).get("resource_cleanup_pending"):
        with STATE.recording(f"Ran another round of cleaning up what the move of {move['name']} left behind",
                             "what it cleaned up", "Cleanup stays pending, and Homestead tries again on its next pass."):
            PHASE._cleanup_resources(move)
            STATE._store(move)
        return STATE._public(move)
    if move["status"] != "failed":
        raise ValueError("only a failed move can be retried")
    if move.get("flags", {}).get("released_on_give_up") or move.get("flags", {}).get("release_requested"):
        # Its backups may be from before the source ran again (a lost reply
        # still counts); resuming would bring old data here.
        raise ValueError(f"{move['name']} may run again on {move['cluster']}, so this move's backups may be out of "
                         "date; cancel it, then start a new move")
    if move.get("phase") == "starting":
        # A fresh start request and a fresh START_GRACE: the old one already ran
        # out. start_requested stays: the source must not start once this one may have.
        move.setdefault("flags", {}).pop("started", None)
    if move.get("phase") == "backing-up":
        move.setdefault("flags", {})["retry_backups"] = True
    done = []
    # Our own phrases, joined as one sentence: only its first letter is raised.
    with STATE.recording(lambda: _sentence(done) or f"Began retrying {move['name']}", "the retry",
                         "The move stays failed; retry again once the data volume answers."):
        if move.get("phase") == "joining":
            done.append("began releasing the backup storage this move had borrowed")
            TARGET._release_target(move)
            done[-1] = "released the backup storage this move had borrowed"
            move.pop("transfer_target", None)
            move.setdefault("flags", {}).pop("snapshot_wait_started", None)
        move.update(status="running", failures=0, finished_at="",
                    message=f"Retrying from {move['phase']}", updated_at=STATE._now())
        move["op"] = PLAN._operation(move)
        done.append(f"added an Activity item to retry {move['name']}, which ends as failed")
        STATE._store(move)
    return STATE._public(move)


def _sentence(phrases):
    text = "; ".join(phrases)
    return text[:1].upper() + text[1:]


def abandon(move_id):
    with STATE._serialized(move_id):
        return _abandon(move_id)


def _abandon(move_id):
    """Put the workload back where it came from, and remove what arrived here.

    Only what this move made is removed - each object carries the move's id -
    so nothing that happened to share a name is touched.
    """
    move = STATE._find(move_id)
    if not move:
        raise ValueError("no such move")
    if move.get("source_removed"):
        raise ValueError(f"{move['name']} was already removed from {move['cluster']}; "
                         "there is nothing to put back")
    stopped = STATE._source_held(move)
    move.update(status="cancelled", message="Putting it back" if stopped else "Cancelling", updated_at=STATE._now())
    move.setdefault("flags", {})["cancelling"] = True
    move["flags"]["resource_cleanup_pending"] = True
    STATE._store(move)
    # What this move made here goes first, and is confirmed gone, before the
    # source starts again: never two copies at once, nor one address announced
    # by both clusters. Until then the source stays stopped.
    name, cluster = move["name"], move["cluster"]
    # From here a change on a cluster may have been made: a failed write says what.
    done = ["began removing what this move made here"]
    with STATE.recording(lambda: _sentence(done), "the cancellation",
                         f"{name} stays stopped on {cluster}, and Homestead tries the cancellation again." if stopped else
                         "The move stays cancelling, and Homestead tries again on its next pass."):
        try:
            _remember_selector(move)
            removed = PHASE._remove_created(move)
            running = _still_running_here(move)
        except Exception as error:
            if not isinstance(error, STATE.STORE_ERRORS):
                done.append(f"removing them stopped: {str(error)[:200]}")
            move.update(status="failed", message=(
                f"Removing what this move made here failed; {name} stays stopped on {cluster} until it is gone. "
                "Retry cancellation" if stopped else
                "Transfer cleanup failed; retry cleanup to remove its remaining destination objects"))
            STATE._store(move)
            raise
        done[-1] = f"removed {', '.join(removed)} here" if removed else "nothing of it was here to remove"
        if stopped and (running or move["flags"].get("destination_deletion_pending")):
            move.update(status="failed", message=(
                f"{running or 'What this move made'} is still running here; {name} stays stopped on {cluster} until it "
                "is gone. Homestead tries the cancellation again"))
            STATE._store(move)
            return STATE._public(move)
    # Nothing stopped there yet means nothing to start again there - and no
    # need for the source to answer before this move can be cancelled.
    try:
        if stopped and move.get("transfer_mode") == "copy":
            status = STATE._source_status(move)
            # An unowned source still needs a version fence: an HTTP request
            # may have timed out before its stop patch was applied.
            stopped = status.get("source_uid") == move.get("source_uid") and status.get("transfer_owner") in ("", move["id"])
        released = STATE._source_action(move, "release") if stopped else {"detail": f"Source on {cluster} kept as it was"}
    except Exception:
        move.update(status="failed", message="Source recovery failed; retry cancellation to restore its running state")
        STATE._store(move)
        raise
    # The source runs again: a failed write says so.
    if stopped:
        done.append(released.get("detail") or f"{name} runs on {cluster} again")
    with STATE.recording(lambda: _sentence(done), "everything",
                         "The move stays cancelling, and Homestead tries again on its next pass."):
        if stopped or move.get("transfer_mode") == "copy":
            move.setdefault("flags", {})["source_released"] = True
            STATE._store(move)
        move.update(message=(released.get("detail") or "Running on the source again")
                    + (f"; removed {', '.join(removed)} here" if removed else ""),
                    finished_at=STATE._now())
        move.pop("definition", None)
        move.setdefault("flags", {})["resource_cleanup_pending"] = True
        PHASE._cleanup_resources(move)
        move["flags"].pop("cancelling", None)
        TARGET._cleanup_target(move)
        STATE._store(move)
    return STATE._public(move)


def _remember_selector(move):
    """Which pods are the app's here, read while its Deployment still exists."""
    flags = move.setdefault("flags", {})
    if move["kind"] != "container" or "destination_selector" in flags:
        return
    obj = KUBE.get_optional(STATE._object_path("container", move["namespace"], move["name"]), read=STATE.kget)
    if obj and STATE._ours(obj, move["id"]):
        flags["destination_selector"] = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        STATE._store(move)


def _still_running_here(move):
    """What of the app this move made here still runs, after its objects were
    deleted: a deleted Deployment's pods, or a VM's instance, go after it."""
    namespace, name, flags = move["namespace"], move["name"], move.setdefault("flags", {})
    if move["kind"] == "vm":
        vmi = KUBE.get_optional(f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}",
                                read=STATE.kget)
        return f"VM instance {name}" if vmi else ""
    may_have_started = (flags.get("start_requested") or flags.get("started")
                        or move.get("phase") in ("starting", "done"))
    if move["kind"] != "container" or not may_have_started:
        return ""
    labels = flags.get("destination_selector") or {}
    if labels:
        selector = urllib.parse.quote(",".join(f"{k}={v}" for k, v in sorted(labels.items())))
        pods = KUBE.items(f"/api/v1/namespaces/{namespace}/pods?labelSelector={selector}", read=STATE.kget)
    else:
        # No selector known: a Deployment's pods are named after it.
        pods = [p for p in KUBE.items(f"/api/v1/namespaces/{namespace}/pods", read=STATE.kget)
                if str((p.get("metadata") or {}).get("name", "")).startswith(f"{name}-")]
    pods = [p for p in pods if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]
    return ", ".join(f"pod {p['metadata']['name']}" for p in pods)


DISMISSABLE = ("succeeded", "cancelled")


def dismiss(move_id=None):
    """Clear moves from the list - one, or every finished one - and nothing
    else: neither cluster is touched. The source keeps its stopped original,
    to be removed there when it suits. A failed move is cleared only when
    asked for by itself: its workload stays stopped on the source, and what it
    made here stays, both for someone to deal with - which is why clearing
    every finished move leaves failed ones listed."""
    with STATE._lock:
        rows = STATE._read()
        allowed = DISMISSABLE + (("failed",) if move_id is not None else ())
        gone = [m for m in rows if m.get("status") in allowed and (move_id is None or m["id"] == move_id)]
        if move_id is not None and not gone:
            match = next((m for m in rows if m["id"] == move_id), None)
            if not match:
                raise ValueError("no such move")
            raise ValueError("a move still running cannot be cleared; cancel it, or wait for it to finish or fail")
        if any(m.get("transfer_mode") == "copy" and STATE._source_held(m) for m in gone):
            raise ValueError("Cancel the failed copy to restore its source before dismissing it")
        if any((m.get("flags") or {}).get("target_cleanup_pending")
               or m["id"] == TARGET._target_journal().get("owner") for m in gone):
            raise ValueError("Wait for backup storage cleanup before dismissing this transfer")
        if any((m.get("flags") or {}).get("resource_cleanup_pending") for m in gone):
            raise ValueError("Wait for temporary resource cleanup before dismissing this transfer")
        if any(m.get("status") == "failed" and m.get("cleanup_protocol") == 1
               and ((m.get("flags") or {}).get("quiesced") or (m.get("flags") or {}).get("quiesce_requested")) for m in gone):
            raise ValueError("Cancel or put back the failed transfer before dismissing it. That cleans up what it made")
        STATE._write([m for m in rows if m not in gone])
    failed = [m for m in gone if m.get("status") == "failed"]
    if failed:
        m = failed[0]
        stopped = STATE._source_held(m) and not m.get("source_removed")
        made = [c["claim"] for c in m.get("claims") or [] if c.get("created")]
        created = bool((m.get("flags") or {}).get("created_at"))
        left = ([f"{m['name']} stays stopped on {m['cluster']}; start it there from its page"] if stopped else []) +                ([f"what it made here stays ({', '.join(([m['name']] if created else []) + made)})"] if made or created else [])
        return {"ok": True, "dismissed": 1,
                "detail": f"cleared the failed move of {m['name']}" + (f"; {'; '.join(left)}" if left else "")}
    kept = [m for m in gone if m.get("status") == "succeeded" and not m.get("source_removed")
            and m.get("transfer_mode") != "copy"]
    return {"ok": True, "dismissed": len(gone),
            "detail": f"cleared {len(gone)} move{'s' if len(gone) != 1 else ''}"
                      + (f"; {', '.join(sorted({m['cluster'] for m in kept}))} still "
                         f"{'has' if len({m['cluster'] for m in kept}) == 1 else 'have'} the stopped original"
                         f"{'s' if len(kept) != 1 else ''} of {', '.join(m['name'] for m in kept)}" if kept else "")}


def finish(move_id, volumes=False):
    """Remove the stopped original from the source, once the move has landed."""
    with STATE._serialized(move_id):
        return _remove_source(move_id, volumes)


def _remove_source(move_id, volumes):
    move = STATE._find(move_id)
    if not move:
        raise ValueError("no such move")
    if move.get("transfer_mode") == "copy":
        raise ValueError("A copy keeps its source. Remove the original separately from its own cluster if intended")
    if move["status"] != "succeeded":
        raise ValueError("only a finished move's source can be removed")
    if move.get("source_removed"):
        return STATE._public(move)
    body = {"action": "remove", "kind": move["kind"], "name": move["name"], "volumes": bool(volumes),
            "namespace": move.get("source_namespace", "")}
    moved = [c["claim"] for c in STATE._moving(move)]
    if volumes and len(moved) != len(move["claims"]):
        # Skipped and blank volumes are the only copy of their data there. A
        # source older than 2.8.250 ignores the list and would delete them.
        there = str((STATE.CLIENT.check_cluster(move["cluster"]) or {}).get("version") or "")
        try:
            new_enough = tuple(int(x) for x in there.lstrip("v").split(".")[:3]) >= (2, 8, 250)
        except ValueError:
            new_enough = False
        if not new_enough:
            raise ValueError(f"Update {move['cluster']}'s Homestead to 2.8.250 or later first. "
                             f"Its release ({there or 'unknown'}) would delete every volume of {move['name']}, "
                             "not only the moved ones; or remove the original without its volumes")
        body["claims"] = moved
    done = STATE.CLIENT.remote(move["cluster"], "/api/move/source", body)
    move.update(source_removed=True, updated_at=STATE._now(),
                message=f"{move['name']} lives here now; {done.get('detail', 'removed')} "
                        f"on {move['cluster']}")
    with STATE.recording(f"Removed {move['name']} from {move['cluster']}", "that",
                         "This move may still offer to remove it; the original is gone there."):
        STATE._store(move)
    return STATE._public(move)


# Last, so that importing this module first works: homestead_move_phases
# imports this one and, at its end, homestead_move_engine, which imports it.
import homestead_move_phases as PHASE  # noqa: E402
