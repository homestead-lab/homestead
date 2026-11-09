"""Durable snapshot removal; Longhorn owns merging, purge and finalizers.

Never remove volume-head or force finalizers. A removed head parent can remain
until another snapshot exists; report that wait rather than promising space.
"""
import re
import time

import homestead_kube as KUBE
import homestead_operations as OPS
import homestead_storage_guard as STORAGE_GUARD

API = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
kget = ksend = None
TIMEOUT = 3600


def bind(get, send):
    global kget, ksend
    kget, ksend = get, send


def identity(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", str(value or "")):
        raise ValueError("invalid snapshot or volume name")
    return value


def source(snapshot):
    status = snapshot.get("status") or {}
    labels = status.get("labels") or (snapshot.get("spec") or {}).get("labels") or {}
    if status.get("userCreated") is False:
        return "system"
    if status.get("userCreated") is not True:
        return "unknown"
    return "scheduled" if any("recurring-job" in key for key in labels) else "user"


def progress(volume):
    """Read-only, volume-wide Longhorn purge status, including externally initiated cleanup."""
    volume = identity(volume)
    listing = kget(f"{API}/engines")
    if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
        raise ValueError("engine inventory incomplete")
    engines = [e for e in listing["items"] if (e.get("spec") or {}).get("volumeName") == volume]
    replicas = []
    for engine in engines:
        for name, row in ((engine.get("status") or {}).get("purgeStatus") or {}).items():
            try:
                pct = max(0, min(100, int(row["progress"])))
            except (ValueError, TypeError, KeyError):
                pct = None
            replicas.append({"replica": name, "active": bool(row.get("isPurging")),
                             "percent": pct, "error": str(row.get("error") or "")})
    active = [r for r in replicas if r["active"]]
    return {"volume": volume, "known": bool(engines), "active": bool(active),
            "percent": min(r["percent"] for r in active) if active and all(r["percent"] is not None for r in active) else None,
            "replicas": replicas, "errors": [r["error"] for r in replicas if r["error"]]}


def plan(volume, name):
    volume, name = identity(volume), identity(name)
    if name == "volume-head":
        raise ValueError("Volume Head is live data, not a deletable snapshot")
    snap = kget(f"{API}/snapshots/{name}")
    if (snap.get("spec") or {}).get("volume") != volume:
        raise ValueError("snapshot does not belong to this volume")
    vol = kget(f"{API}/volumes/{volume}")
    status = snap.get("status") or {}
    blockers = []
    if (vol.get("status") or {}).get("isStandby") or (vol.get("status") or {}).get("restoreRequired"):
        blockers.append("Standby/restoring volumes cannot have recovery points removed here")
    if source(snap) == "unknown":
        blockers.append("Longhorn has not identified this snapshot's source yet")
    if not snap.get("metadata", {}).get("uid") or not vol.get("metadata", {}).get("uid"):
        blockers.append("Snapshot or volume identity is unavailable")
    purge = KUBE.get_optional(f"{API}/settings/disable-snapshot-purge", read=kget)
    if purge and str(purge.get("value")).lower() == "true":
        blockers.append("Longhorn snapshot purge is off. Turn it on in Longhorn before asking for cleanup.")
    return {"volume": volume, "name": name, "uid": snap["metadata"].get("uid", ""),
            "volume_uid": vol["metadata"].get("uid", ""), "source": source(snap),
            "created": status.get("creationTime", ""),
            "head_parent": "volume-head" in (status.get("children") or {}),
            "deleting": bool(snap["metadata"].get("deletionTimestamp")),
            "removed": bool(status.get("markRemoved")), "blockers": blockers,
            "ready": not blockers}


def start(cfg, ops):
    p = plan(cfg.get("volume"), cfg.get("name"))
    if p["blockers"]:
        raise ValueError("; ".join(p["blockers"]))
    if not cfg.get("uid") or cfg["uid"] != p["uid"] or cfg.get("volume_uid") != p["volume_uid"]:
        raise ValueError("The snapshot or volume changed. Review the deletion again.")
    if cfg.get("confirmation") != p["name"]:
        raise ValueError("type the snapshot name exactly to confirm")
    return ops.start("snapshot-delete", f"Remove {p['source']} snapshot {p['name']}",
                     {"kind": "Snapshot", "name": p["name"]}, "/data-protection",
                     {**p, "phase": "request", "since": time.time()},
                     "Queued for Longhorn to remove and merge. The space is not freed immediately.")


def resolve(item):
    ref = item["ref"]
    if time.time() - ref["since"] > TIMEOUT:
        return "failed", item.get("progress", 0), "The cleanup is still pending after one hour. Longhorn may continue. Check how the snapshot depends on the current data, then choose Resume job to follow it again."
    volume = KUBE.get_optional(f"{API}/volumes/{ref['volume']}", read=kget)
    if not volume or volume["metadata"].get("uid") != ref["volume_uid"]:
        return "failed", 0, "The volume was removed or changed, so nothing more was done"
    snap = KUBE.get_optional(f"{API}/snapshots/{ref['name']}", read=kget)
    if snap and snap["metadata"].get("uid") != ref["uid"]:
        return "failed", 0, "The snapshot changed, so its replacement was not touched"
    if ref["phase"] == "request" and snap and not snap["metadata"].get("deletionTimestamp"):
        p = plan(ref["volume"], ref["name"])
        if p["uid"] != ref["uid"] or p["volume_uid"] != ref["volume_uid"]:
            return "failed", 0, "The snapshot or volume changed during the second check, so its replacement was not touched"
        if p["blockers"] or p["source"] != ref["source"]:
            return "failed", 0, "; ".join(p["blockers"]) or "The snapshot source changed. Review again."
        ksend("DELETE", f"{API}/snapshots/{ref['name']}",
              {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": ref["uid"]}})
        ref["phase"] = "monitor"
        return "running", 10, "Removal requested. Longhorn marks, merges and purges the blocks it can."
    ref["phase"] = "monitor"
    listing = kget(f"{API}/engines")
    if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
        raise ValueError("engine inventory incomplete")
    engines = [e for e in listing["items"] if (e.get("spec") or {}).get("volumeName") == ref["volume"]]
    statuses = [e.get("status") or {} for e in engines]
    purges = [p for st in statuses for p in (st.get("purgeStatus") or {}).values()]
    errors = [p["error"] for p in purges if p.get("error")]
    if errors:
        return "failed", item.get("progress", 10), "Longhorn purge failed: " + "; ".join(errors)
    running = [p for p in purges if p.get("isPurging")]
    if running:
        pct = min(max(0, min(100, int(p.get("progress") or 0))) for p in running)
        return "running", 10 + int(pct * .8), f"Longhorn merge and purge {pct}% (slowest volume copy). The removal is checked afterwards."
    remaining = [st["snapshots"][ref["name"]] for st in statuses
                 if ref["name"] in (st.get("snapshots") or {})]
    if snap is None and engines and all(isinstance(st.get("snapshots"), dict) for st in statuses) and not remaining:
        return "succeeded", 100, "Snapshot removed from Longhorn and its engines. Shared blocks may remain in newer snapshots."
    st = (snap or {}).get("status") or {}
    if (st.get("markRemoved") and "volume-head" in (st.get("children") or {})) or any(
            r.get("removed") and "volume-head" in (r.get("children") or {}) for r in remaining):
        return "running", 20, "Marked for removal, but it is still the parent of Volume Head. Take a new snapshot, and Longhorn can then finish merging. No snapshot is taken automatically."
    if st.get("error"):
        return "running", 15, "Longhorn is waiting: " + str(st["error"])
    return "running", 15, "Waiting for Longhorn to attach, merge and clean up. Nothing is force-detached, and no finalizer is removed."


def resumable(item):
    # Resuming only re-observes the same UID; a failed request is rechecked.
    return "" if item.get("ref", {}).get("uid") else "Snapshot identity is missing"


def resume_resolve(item):
    if item.get("message") == "Carrying on from where it stopped":
        item["ref"]["since"] = time.time()
    return resolve(item)


def start_guarded(cfg):
    """start, holding the volume's storage fence so no other storage job
    changes it while this one is set up."""
    with STORAGE_GUARD.volume(OPS, kget, cfg.get("volume")):
        return start(cfg, OPS)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/lh/snapshot/delete-plan"): ("viewer", lambda request: plan((request.query.get("volume") or [""])[0], (request.query.get("name") or [""])[0])),
    ("GET", "/api/lh/snapshot-progress"): ("viewer", lambda request: progress((request.query.get("volume") or [""])[0])),
    ("POST", "/api/lh/snapshot/delete"): ("admin", lambda request: {"ok": True, "operation": start_guarded(request.body)}),
}
