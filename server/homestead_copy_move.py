"""Moving one Longhorn volume's copies onto other disks, while it runs.

A copy's disk is chosen by tags: the volume's disk selector (and, for hosts,
its node selector) says which disks may hold it. Setting them moves nothing
by itself - Longhorn applies them when it places a copy. So each copy on a
disk the new tags exclude is replaced the way a rebalance replaces one: the
volume asks for one copy more, Longhorn builds it on a disk the tags allow,
and once it is whole the old copy is removed. The volume never has fewer
whole copies than it asks for, so an app or VM using it carries on.

A claim's storage class cannot change, so this is a move of where the
copies live, not a change of class: the claim keeps its class, the volume
keeps the new tags, and copies Longhorn rebuilds later go where they say.

On a single host, the new copy has to be built beside the old one; Longhorn
keeps a volume's copies on different hosts unless the volume allows
otherwise, so it does for the move and is put back after. A detached volume
is rebuilt with Longhorn's offline rebuilding, turned on for it while it
moves.
"""
import hashlib
import json
import time
import urllib.error

import homestead_operations as OPS
import homestead_rebalance as REBALANCE
import homestead_routes as ROUTER

LH = REBALANCE.LH
KIND = "volume-copy-move"
GIB = REBALANCE.GIB
# Longhorn's own limits when it places a copy (its defaults).
OVERPROVISION, MINIMAL_FREE = "storage-over-provisioning-percentage", "storage-minimal-available-percentage"


def kget(path):
    return REBALANCE.kget(path)


def ksend(*args, **kwargs):
    return REBALANCE.ksend(*args, **kwargs)


def _tags(value):
    if isinstance(value, str):
        value = value.split(",")
    return sorted({str(t).strip() for t in value or () if str(t).strip()})


def _disks():
    """Every Longhorn disk: its host, name, path, tags and room, by disk UUID."""
    out = {}
    for node in REBALANCE._items(f"{LH}/nodes"):
        host = (node.get("metadata") or {}).get("name", "")
        spec, status = node.get("spec") or {}, node.get("status") or {}
        for key, disk in (spec.get("disks") or {}).items():
            state = (status.get("diskStatus") or {}).get(key) or {}
            uuid = state.get("diskUUID") or f"{host}/{key}"
            out[uuid] = {"uuid": uuid, "host": host, "disk": key, "path": disk.get("path", ""),
                         "tags": _tags(disk.get("tags")), "host_tags": _tags(spec.get("tags")),
                         "takes": bool(disk.get("allowScheduling", True)) and bool(spec.get("allowScheduling", True)),
                         "maximum": int(state.get("storageMaximum") or 0), "reserved": int(disk.get("storageReserved") or 0),
                         "scheduled": int(state.get("storageScheduled") or 0), "available": int(state.get("storageAvailable") or 0)}
    return out


def _on_target(disk, disk_tags, node_tags):
    return bool(disk) and set(disk_tags) <= set(disk["tags"]) and set(node_tags) <= set(disk["host_tags"])


def _room(disk, size, actual):
    """Whether Longhorn would place a copy of this size on the disk, as it
    decides: within over-provisioning, and leaving its minimum free."""
    over = int(REBALANCE._setting(OVERPROVISION, "100") or 100)
    minimal = int(REBALANCE._setting(MINIMAL_FREE, "25") or 25)
    schedulable = (disk["maximum"] - disk["reserved"]) * over / 100 - disk["scheduled"]
    left = disk["available"] - actual - disk["maximum"] * minimal / 100
    return schedulable >= size and left >= 0, round(min(schedulable, disk["available"]) / GIB, 1)


def _volume(name):
    return kget(f"{LH}/volumes/{name}")


def _copies(name):
    return REBALANCE._copies(name)


def plan(name, disk_tags=(), node_tags=()):
    """Where the copies are, which would move, and whether they fit there."""
    disk_tags, node_tags = _tags(disk_tags), _tags(node_tags)
    try:
        volume = _volume(name)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"There is no Longhorn volume {name}") from None
        raise
    meta, spec, status = volume.get("metadata") or {}, volume.get("spec") or {}, volume.get("status") or {}
    disks = _disks()
    copies = _copies(name)
    k8s = status.get("kubernetesStatus") or {}
    size, actual = int(spec.get("size") or 0), int(status.get("actualSize") or 0)
    rows = []
    for replica in copies:
        r = replica.get("spec") or {}
        disk = disks.get(r.get("diskID")) or next((d for d in disks.values() if d["host"] == r.get("nodeID")
                                                    and d["path"].rstrip("/") == (r.get("diskPath") or "").rstrip("/")), None)
        rows.append({"replica": (replica.get("metadata") or {}).get("name", ""), "host": r.get("nodeID", ""),
                     "disk": disk["disk"] if disk else "", "path": (disk or {}).get("path") or r.get("diskPath", ""),
                     "tags": disk["tags"] if disk else [], "whole": REBALANCE._whole(replica),
                     "moves": bool(disk_tags or node_tags) and not _on_target(disk, disk_tags, node_tags)})
    blockers, warnings = [], []
    wanted = int(spec.get("numberOfReplicas") or 0)
    if status.get("state") == "attached" and status.get("robustness") != "healthy":
        blockers.append("The volume is not healthy now; let Longhorn finish repairing it first")
    elif len(copies) != wanted or not all(row["whole"] for row in rows):
        blockers.append("Its copies are being rebuilt or changed; wait until they settle")
    elif status.get("state") not in ("attached", "detached"):
        blockers.append(f"It is {status.get('state') or 'changing'}; wait until it is attached or detached")
    elif status.get("state") == "detached" and "offlineRebuilding" not in spec \
            and REBALANCE._setting("offline-replica-rebuilding", "false") != "true":
        blockers.append("It is detached, and this Longhorn rebuilds copies of a detached volume only with offline "
                        "rebuilding on; start what uses it first, or turn offline rebuilding on in Longhorn")
    if (meta.get("annotations") or {}).get(REBALANCE.MOVING):
        blockers.append("A copy of it is moving already; follow that in Jobs")
    targets = [{**d, "fits": _room(d, size, actual)[0], "room_gb": _room(d, size, actual)[1]}
               for d in disks.values() if (disk_tags or node_tags) and d["takes"] and _on_target(d, disk_tags, node_tags)]
    moving = [row for row in rows if row["moves"]]
    if disk_tags or node_tags:
        if not targets:
            blockers.append("No disk Longhorn may use carries " + " and ".join(
                part for part in (f"disk tags {', '.join(disk_tags)}" if disk_tags else "",
                                  f"host tags {', '.join(node_tags)}" if node_tags else "") if part))
        elif moving and not any(t["fits"] for t in targets):
            blockers.append(f"No disk with those tags has room for a {round(size / GIB, 1)} GiB copy, as Longhorn counts it")
        if not moving and not blockers:
            blockers.append("Every copy is on a disk with those tags already")
        hosts = {t["host"] for t in targets if t["fits"]}
        if wanted > 1 and len(hosts) < wanted:
            warnings.append(f"Disks with those tags are on {len(hosts)} host{'' if len(hosts) == 1 else 's'}, "
                            f"so {wanted} copies cannot all be on different hosts")
    else:
        blockers.append("Choose the disk tags (or host tags) its copies should be on")
    if status.get("state") == "detached" and moving:
        warnings.append("It is detached: Longhorn attaches it briefly to build each copy, and an app started "
                        "meanwhile waits for that")
    token = review_token(name, disk_tags, node_tags, [row["replica"] for row in moving])
    return {"volume": name, "claim": "/".join(x for x in (k8s.get("namespace"), k8s.get("pvcName")) if x) or name,
            "size_gb": round(size / GIB, 1), "used_gb": round(actual / GIB, 1), "state": status.get("state", ""),
            "attached_to": status.get("currentNodeID") or "", "copies": rows, "wanted": wanted,
            "disk_tags": disk_tags, "node_tags": node_tags,
            "current": {"disk_tags": _tags(spec.get("diskSelector")), "node_tags": _tags(spec.get("nodeSelector"))},
            # What can be chosen: every tag on a disk or host Longhorn may use.
            "choices": {"disk_tags": sorted({t for d in disks.values() if d["takes"] for t in d["tags"]}),
                        "node_tags": sorted({t for d in disks.values() if d["takes"] for t in d["host_tags"]})},
            "targets": [{k: t[k] for k in ("host", "disk", "path", "tags", "fits", "room_gb")} for t in targets],
            "moves": len(moving), "blockers": blockers, "warnings": warnings, "ok": not blockers, "review_token": token}


def review_token(name, disk_tags, node_tags, replicas):
    return hashlib.sha256(json.dumps([name, sorted(disk_tags), sorted(node_tags), sorted(replicas)]).encode()).hexdigest()[:20]


# --------------------------------------------------------------- the job
def _patch(name, spec=None, keeps=False):
    """Change the volume's spec; `keeps` (the copy count it keeps) marks it as
    moving a copy, so it reads as that rather than degraded; None clears it."""
    body = {"spec": spec or {}}
    if keeps is not False:
        body["metadata"] = {"annotations": {REBALANCE.MOVING: str(keeps) if keeps else None}}
    ksend("PATCH", f"{LH}/volumes/{name}", body, ctype="application/merge-patch+json")


def _put_back(ref, *, keep_tags):
    """The volume as it was, but for copies already moved - and, when the move
    finished, the new tags, so copies rebuilt later go there too."""
    spec = {"numberOfReplicas": ref["original"]}
    for key in ("replicaSoftAntiAffinity", "offlineRebuilding"):
        if key in (ref.get("was") or {}):
            spec[key] = ref["was"][key]
    if not keep_tags:
        spec["diskSelector"] = (ref.get("was") or {}).get("diskSelector") or []
        spec["nodeSelector"] = (ref.get("was") or {}).get("nodeSelector") or []
    _patch(ref["volume"], spec, keeps=None)


def status(item):
    """One step of the move; the leader takes it, anyone may look."""
    ref = item["ref"]
    name, done, now = ref["volume"], ref.get("moved", 0), time.time()
    label = ref.get("claim") or name
    if not REBALANCE.is_leader():
        return "running", ref.get("progress", 0), item.get("message") or f"Moving the copies of {label}"
    try:
        volume = _volume(name)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "failed", ref.get("progress", 0), f"{label} no longer exists"
        raise
    spec = volume.get("spec") or {}
    disks = _disks()
    copies = _copies(name)

    def where(replica):
        r = replica.get("spec") or {}
        return disks.get(r.get("diskID")) or next((d for d in disks.values() if d["host"] == r.get("nodeID")
                                                   and d["path"].rstrip("/") == (r.get("diskPath") or "").rstrip("/")), None)

    def off(replica):
        return not _on_target(where(replica), ref["disk_tags"], ref["node_tags"])

    def fail(why):
        _put_back(ref, keep_tags=False)
        return "failed", ref.get("progress", 0), f"{why}. {label} keeps the copies it has" + (
            f"; {done} already moved stay where they are" if done else "")

    total = ref.get("total") or 1
    ref["progress"] = progress = int(100 * done / total)
    stage = ref.get("stage", "prepare")
    if stage == "prepare":
        ref["was"] = {key: spec.get(key) for key in ("diskSelector", "nodeSelector", "replicaSoftAntiAffinity", "offlineRebuilding")
                      if key in spec}
        ref["original"] = int(spec.get("numberOfReplicas") or 1)
        change = {"diskSelector": ref["disk_tags"], "nodeSelector": ref["node_tags"]}
        # Beside its old copy when there is nowhere else; put back after.
        if "replicaSoftAntiAffinity" in spec:
            change["replicaSoftAntiAffinity"] = "enabled"
        if (volume.get("status") or {}).get("state") == "detached" and "offlineRebuilding" in spec:
            change["offlineRebuilding"] = "enabled"
        _patch(name, change)
        ref["stage"] = "add"
        return "running", progress, f"Pointing {label} at disks tagged {', '.join(ref['disk_tags'] + ref['node_tags'])}"
    if stage == "add":
        leaving = [r for r in copies if off(r)]
        if not leaving:
            _put_back(ref, keep_tags=True)
            return "succeeded", 100, f"Moved {done} cop{'y' if done == 1 else 'ies'} of {label}; every copy is on the disks chosen"
        if len(copies) != ref["original"] or not all(REBALANCE._whole(r) for r in copies):
            waited = ref.setdefault("unsettled_since", now)
            if now - waited > 900:
                return fail("Its copies did not settle within 15 minutes")
            return "running", progress, f"Waiting for the copies of {label} to settle"
        ref.pop("unsettled_since", None)
        ref.update(stage="building", started=now, before=sorted((r.get("metadata") or {}).get("name") for r in copies))
        _patch(name, {"numberOfReplicas": ref["original"] + 1}, keeps=ref["original"])
        return "running", progress, f"Building a copy of {label} on a disk tagged {', '.join(ref['disk_tags'] + ref['node_tags'])}"
    if stage == "building":
        new = [r for r in copies if (r.get("metadata") or {}).get("name") not in ref["before"]]
        limit = 3600 + 120 * ref.get("size_gb", 0)
        if not new:
            if now - ref["started"] > 600:
                return fail("Longhorn did not place the new copy within 10 minutes - no disk with those tags took it")
            return "running", progress, f"Waiting for Longhorn to place a copy of {label}"
        if off(new[0]):
            ksend("DELETE", f"{LH}/replicas/{(new[0].get('metadata') or {}).get('name')}")
            return fail(f"Longhorn placed the new copy on {(where(new[0]) or {}).get('path') or 'a disk without those tags'}")
        if now - ref["started"] > limit:
            return fail("The new copy was not whole in time")
        if not REBALANCE._built(volume, new[0]):
            target = where(new[0]) or {}
            return "running", progress, f"Building a copy of {label} on {target.get('host', '')} {target.get('path', '')}".rstrip()
        ref["stage"] = stage = "removing"
    if stage == "removing":
        leaving = [r for r in copies if off(r)]
        if leaving:
            # A copy that failed while the new one built: removing one now
            # would leave fewer whole copies than the volume asks for.
            whole = [r for r in copies if REBALANCE._built(volume, r)]
            if len(whole) - 1 < ref["original"]:
                if now - ref["started"] > 3600 + 120 * ref.get("size_gb", 0):
                    return fail("Another copy failed during the move; the old copy is kept")
                return "running", progress, f"Waiting for Longhorn to rebuild a failed copy of {label} first"
            old = max(leaving, key=lambda r: (r.get("spec") or {}).get("nodeID") != (volume.get("status") or {}).get("currentNodeID"))
            ksend("DELETE", f"{LH}/replicas/{(old.get('metadata') or {}).get('name')}")
        _patch(name, {"numberOfReplicas": ref["original"]}, keeps=ref["original"])
        ref.update(stage="settling", settle_from=now)
        return "running", progress, f"Removing the old copy of {label}"
    if stage == "settling":
        if len(copies) == ref["original"] and all(REBALANCE._whole(r) for r in copies) \
                or now - ref.get("settle_from", now) > 900:
            ref.update(stage="add", moved=done + 1)
            return "running", int(100 * (done + 1) / total), f"Moved a copy of {label} ({done + 1} of {total})"
        return "running", progress, f"Waiting for {label} to settle at {ref['original']} cop{'y' if ref['original'] == 1 else 'ies'}"
    return fail("An unknown step")


def cancel_plan(item):
    return {"mode": "stop", "action": "Stop moving copies",
            "undo": ["The copy being built now is dropped; the volume keeps the copies it had, and its old disk tags"],
            "keeps": ["Copies already moved stay where they are"], "needs": "operator"}


def cancel_run(work, chosen):
    ref = work.get("ref") or {}
    if ref.get("original"):
        _put_back(ref, keep_tags=False)
    return f"Stopped after moving {ref.get('moved', 0)} cop{'y' if ref.get('moved', 0) == 1 else 'ies'}"


def start_reviewed(b):
    """Start the move someone reviewed; 409 with the plan as it is now when it
    no longer holds, 202 with the job when it starts."""
    name = str(b.get("volume") or "")
    try:
        now = plan(name, b.get("disk_tags") or [], b.get("node_tags") or [])
    except ValueError as error:
        return ROUTER.Reply(404, {"error": str(error)})
    if now["review_token"] != b.get("review_token"):
        return ROUTER.Reply(409, {"error": "Its copies changed since the review; review again", "plan": now})
    if now["blockers"]:
        return ROUTER.Reply(409, {"error": now["blockers"][0], "plan": now})
    running = next((i for i in OPS.snapshot() if i.get("kind") in (KIND, REBALANCE.KIND) and i.get("status") not in OPS.TERMINAL
                    and (i.get("kind") == REBALANCE.KIND or (i.get("ref") or {}).get("volume") == name)), None)
    if running:
        return ROUTER.Reply(409, {"error": "Copies are moving already; follow that in Jobs", "operation": running})
    where = ", ".join(now["disk_tags"] + now["node_tags"])
    operation = OPS.start(KIND, f"Move {now['claim']} to disks tagged {where}", {"kind": "Volume", "name": name}, "/volumes",
                          {"volume": name, "claim": now["claim"], "disk_tags": now["disk_tags"], "node_tags": now["node_tags"],
                           "total": now["moves"], "moved": 0, "stage": "prepare", "size_gb": now["size_gb"]},
                          f"Moving {now['moves']} cop{'y' if now['moves'] == 1 else 'ies'}, one at a time")
    return ROUTER.Reply(202, {"operation": operation})


def _query_tags(request, key):
    return _tags((request.query.get(key) or [""])[0])


ROUTES = {
    ("GET", "/api/longhorn/copies/plan"): ("viewer", lambda request: plan((request.query.get("volume") or [""])[0],
                                                                         _query_tags(request, "disk_tags"), _query_tags(request, "node_tags"))),
    ("POST", "/api/longhorn/copies/move"): ("operator", lambda request: start_reviewed(request.body)),
}


def register(ops):
    ops.RESOLVERS[KIND] = status
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
