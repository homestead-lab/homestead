"""Durable snapshot removal; Longhorn owns merging, purge and finalizers.

Never remove volume-head or force finalizers. A removed head parent can remain
until another snapshot exists; report that wait rather than promising space.
"""
import re
import time
import urllib.error

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


def optional(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


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
    purge = optional(f"{API}/settings/disable-snapshot-purge")
    if purge and str(purge.get("value")).lower() == "true":
        blockers.append("Longhorn snapshot purge is disabled; enable it in Longhorn before requesting cleanup")
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
        raise ValueError("snapshot or volume changed; review deletion again")
    if cfg.get("confirmation") != p["name"]:
        raise ValueError("type the snapshot name exactly to confirm")
    return ops.start("snapshot-delete", f"Remove {p['source']} snapshot {p['name']}",
                     {"kind": "Snapshot", "name": p["name"]}, "/data-protection",
                     {**p, "phase": "request", "since": time.time()},
                     "Queued for Longhorn removal and merge; space is not reclaimed immediately")


def resolve(item):
    ref = item["ref"]
    if time.time() - ref["since"] > TIMEOUT:
        return "failed", item.get("progress", 0), "Cleanup still pending after one hour. Longhorn may continue; inspect the snapshot/head dependency, then Carry on to monitor again."
    volume = optional(f"{API}/volumes/{ref['volume']}")
    if not volume or volume["metadata"].get("uid") != ref["volume_uid"]:
        return "failed", 0, "Backing volume disappeared or changed; no further actions taken"
    snap = optional(f"{API}/snapshots/{ref['name']}")
    if snap and snap["metadata"].get("uid") != ref["uid"]:
        return "failed", 0, "Snapshot identity changed; replacement left untouched"
    if ref["phase"] == "request" and snap and not snap["metadata"].get("deletionTimestamp"):
        p = plan(ref["volume"], ref["name"])
        if p["uid"] != ref["uid"] or p["volume_uid"] != ref["volume_uid"]:
            return "failed", 0, "Snapshot or volume changed during recheck; replacement left untouched"
        if p["blockers"] or p["source"] != ref["source"]:
            return "failed", 0, "; ".join(p["blockers"]) or "Snapshot source changed; review again"
        ksend("DELETE", f"{API}/snapshots/{ref['name']}",
              {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": ref["uid"]}})
        ref["phase"] = "monitor"
        return "running", 10, "Removal requested; Longhorn will mark, merge and purge eligible blocks"
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
        return "running", 10 + int(pct * .8), f"Longhorn merge/purge {pct}% (slowest active replica); verifying removal afterwards"
    remaining = [st["snapshots"][ref["name"]] for st in statuses
                 if ref["name"] in (st.get("snapshots") or {})]
    if snap is None and engines and all(isinstance(st.get("snapshots"), dict) for st in statuses) and not remaining:
        return "succeeded", 100, "Snapshot removed from Longhorn and its engines; shared blocks may remain in newer snapshots"
    st = (snap or {}).get("status") or {}
    if (st.get("markRemoved") and "volume-head" in (st.get("children") or {})) or any(
            r.get("removed") and "volume-head" in (r.get("children") or {}) for r in remaining):
        return "running", 20, "Marked for removal but still parent of Volume Head. Take a fresh snapshot to create a boundary; Longhorn can then finish merging. No automatic snapshot is created."
    if st.get("error"):
        return "running", 15, "Longhorn is waiting: " + str(st["error"])
    return "running", 15, "Waiting for Longhorn attachment/merge and finalizer cleanup; no force-detach or finalizer removal"


def resumable(item):
    # Resuming only re-observes the same UID; a failed request is rechecked.
    return "" if item.get("ref", {}).get("uid") else "Snapshot identity is missing"


def resume_resolve(item):
    if item.get("message") == "Carrying on from where it stopped":
        item["ref"]["since"] = time.time()
    return resolve(item)
