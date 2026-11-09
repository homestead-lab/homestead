"""What transfers from another cluster hold here, for a person to end.

A move or copy is driven from the destination: it holds the workload here,
backs it up, and later asks this side to put the workload back or remove it,
and to delete the backups it made. If the destination stops asking - its
record cleared, the cluster rebuilt, the copy gone from its list - nothing on
this side ever ends the hold. The workload keeps its move annotations, every
new copy of it is refused as "held by another transfer", and its backups and
snapshots stay in Longhorn and in the backup store.

This page's half lets a person on this cluster see those holds and end one:
the workload is put back exactly as the destination would have put it back
(release), and what that transfer made here is deleted (its cleanup), with
the same identity checks either side uses. Backups that outlive the release,
because Longhorn deletes them in the background, are listed as leftovers until
a later pass removes them.
"""
import homestead_move as MOVE
import homestead_move_source as SRC
import homestead_names as NAMES
import homestead_transfer_cleanup as CLEANUP

LISTS = {"vm": "/apis/kubevirt.io/v1/virtualmachines",
         "container": "/apis/apps/v1/deployments",
         "volume": "/api/v1/persistentvolumeclaims"}


def _workloads():
    """Every VM, Deployment and claim on the cluster, as (kind, object)."""
    return [(kind, obj) for kind, path in LISTS.items() for obj in CLEANUP.items(SRC.kget, path)]


def _hold(kind, obj):
    """One workload's hold, or None when no transfer holds it."""
    annotations = SRC._annotations(obj)
    origin, owner = SRC._origin(obj), NAMES.read(annotations, SRC.OWNER)
    if not origin and not owner:
        return None
    meta = obj.get("metadata") or {}
    backups = SRC._recorded_backups(obj)
    return {"kind": kind, "namespace": meta.get("namespace", ""), "name": meta.get("name", ""),
            "uid": meta.get("uid", ""), "transfer_id": owner,
            # Kept running throughout: ending the hold changes nothing it runs.
            "kept_running": bool(origin.get("_copy_live")) or kind == "volume",
            "copy": any(key.startswith("_copy_") for key in origin),
            "backups": len(backups),
            "since": min((row.get("requested_at") or 0 for row in backups), default=0)}


def _stamped(obj):
    meta = obj.get("metadata") or {}
    return NAMES.annotation_of(meta, "move-id"), NAMES.annotation_of(meta, CLEANUP.SOURCE_UID)


def holds():
    """The holds, and what transfers no longer holding anything left behind.

    Longhorn is read before the workloads: a transfer marks the workload
    before it makes a snapshot, so any snapshot or backup read here already
    has its mark on the workload by the time the workloads are read, and a
    transfer starting meanwhile is never taken for leftovers."""
    artifacts = [(what, obj) for what in ("backups", "snapshots")
                 for obj in CLEANUP.items(SRC.kget, f"{CLEANUP.LH}/{what}")]
    workloads = _workloads()
    rows = [row for row in (_hold(kind, obj) for kind, obj in workloads) if row]
    held = {(row["uid"], row["transfer_id"]) for row in rows}
    names = {(obj.get("metadata") or {}).get("uid"): (kind, (obj.get("metadata") or {}).get("namespace", ""),
                                                       (obj.get("metadata") or {}).get("name", ""))
             for kind, obj in workloads}
    left = {}
    for what, obj in artifacts:
        transfer_id, source_uid = _stamped(obj)
        if not transfer_id or not source_uid or (source_uid, transfer_id) in held:
            continue
        kind, namespace, name = names.get(source_uid, ("", "", ""))
        row = left.setdefault((transfer_id, source_uid), {
            "transfer_id": transfer_id, "source_uid": source_uid, "kind": kind,
            "namespace": namespace, "name": name, "backups": 0, "snapshots": 0, "deleting": 0})
        row[what] += 1
        row["deleting"] += bool((obj.get("metadata") or {}).get("deletionTimestamp"))
    return {"holds": sorted(rows, key=lambda r: (r["namespace"], r["name"], r["kind"])),
            "leftovers": sorted(left.values(), key=lambda r: (r["namespace"], r["name"], r["transfer_id"]))}


def _tidy(transfer_id, source_uid):
    result = CLEANUP.source(SRC.kget, SRC.ksend, transfer_id, source_uid)
    pending = result.get("pending") or []
    return {"complete": not pending, "pending": pending, "retained": result.get("retained") or []}


def release(kind, namespace, name, transfer_id="", uid=""):
    """End one hold: put the workload back as the transfer would, then delete
    what that transfer made here. transfer_id and uid are what the person saw,
    so a hold that changed since - another transfer, a replaced workload - is
    refused rather than ended."""
    def run():
        kind_ = SRC._kind(kind)
        obj = SRC._object(kind_, name)
        meta = obj.get("metadata") or {}
        if not uid or meta.get("uid") != uid:
            raise ValueError(f"{name} was replaced since this page read it. Refresh and check again.")
        if NAMES.read(SRC._annotations(obj), SRC.OWNER) != (transfer_id or ""):
            raise ValueError(f"{name} is held by a different transfer now. Refresh and check again.")
        if not transfer_id and not SRC._origin(obj):
            return {"ok": True, "detail": f"{name} is no longer held", "complete": True}
        answer = SRC.release(kind_, name, transfer_id, uid) if transfer_id else SRC.release(kind_, name)
        tidy = _tidy(transfer_id, uid) if transfer_id else {"complete": True, "pending": [], "retained": []}
        detail = answer.get("detail", f"{name} is no longer held")
        if not tidy["complete"]:
            detail += ". Longhorn is still deleting what the transfer created here, so it shows under Leftovers until it has been removed"
        return {"ok": True, "detail": detail, **tidy}
    return SRC.in_namespace(namespace, run)


def tidy(transfer_id, source_uid):
    """Delete what a transfer that holds nothing any more left here."""
    for kind, obj in _workloads():
        meta = obj.get("metadata") or {}
        if meta.get("uid") == source_uid and NAMES.read(SRC._annotations(obj), SRC.OWNER) == transfer_id:
            raise ValueError(f"{meta.get('name', 'That workload')} is still held by that transfer. "
                             "Release it first, or let the transfer finish.")
    result = _tidy(transfer_id, source_uid)
    return {"ok": True, **result,
            "detail": "Removed what the transfer left here" if result["complete"]
                      else "Longhorn is still deleting some of it. Check again in a minute."}


def _body(request, *keys):
    return [str(request.body.get(key) or "") for key in keys]


# Admin's: ending a hold starts a workload that was stopped for a move, and
# tidying deletes backups.
ROUTES = {
    ("GET", "/api/move/holds"): ("admin", lambda request: MOVE.answer(holds)),
    ("POST", "/api/move/holds/release"): ("admin", lambda request: MOVE.answer(
        lambda: release(*_body(request, "kind", "namespace", "name", "transfer_id", "uid")))),
    ("POST", "/api/move/holds/tidy"): ("admin", lambda request: MOVE.answer(
        lambda: tidy(*_body(request, "transfer_id", "source_uid")))),
}
