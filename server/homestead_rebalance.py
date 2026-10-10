"""Moving Longhorn volume copies so hosts hold similar amounts.

A host left cordoned, or out for a while, comes back with Longhorn having
rebuilt its copies elsewhere: the others full, it nearly empty. Longhorn's
own replica auto-balance does not undo that - it spreads one volume's copies
across hosts, and two copies on two hosts are spread already.

The plan moves the fewest copies that bring hosts closest, largest first
where that helps. A copy is never moved off the host its volume is attached
to, so nothing restarts and reads stay local. Each move adds a copy on the
emptier host, waits until it is whole, then removes the one on the fuller
host - the volume never has fewer whole copies than it asks for.

The job runs one move at a time, driven by its resolver (the leader looks
after it), so a restart of Homestead carries on where it was. Cancelling
puts the copy count back mid-move, and Longhorn drops the copy it was
building; moves already done stay done.
"""
import hashlib
import json
import time
import urllib.error

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
KIND = "volume-rebalance"
# On a Longhorn volume while one of its copies moves: the copies it keeps.
# Building the new copy first means asking Longhorn for one more than that,
# and Longhorn calls a volume short of its asked-for count "degraded" - though
# every copy it had is still whole. Homestead reads this to say so instead.
MOVING = "homestead.io/rebalancing"
GIB = 1024 ** 3
MAX_MOVES = 40

kget = ksend = None
is_leader = lambda: True


def bind(_kget, _ksend, _is_leader=None):
    global kget, ksend, is_leader
    kget, ksend = _kget, _ksend
    if _is_leader:
        is_leader = _is_leader


def _items(path):
    try:
        return kget(path).get("items", [])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise


def _setting(name, default=""):
    try:
        return str(kget(f"{LH}/settings/{name}").get("value", default))
    except urllib.error.HTTPError:
        return default


def _whole(replica):
    spec, status = replica.get("spec") or {}, replica.get("status") or {}
    return not spec.get("failedAt") and (status.get("currentState") == "running" or bool(spec.get("healthyAt")))


def _app(volume):
    """Who uses a volume: its Deployment, StatefulSet or VM, else its claim."""
    k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
    ns, claim = k8s.get("namespace", ""), k8s.get("pvcName", "")
    for row in k8s.get("workloadsStatus") or []:
        name, kind = row.get("workloadName") or "", row.get("workloadType") or ""
        if name:
            if kind == "ReplicaSet" and "-" in name:
                name = name.rsplit("-", 1)[0]
            return f"{ns}/{name}" if ns else name
    return f"{ns}/{claim}" if claim else (volume.get("metadata") or {}).get("name", "")


def inventory():
    """Hosts that may take a copy, and each volume with where its copies are."""
    lh_nodes = _items(f"{LH}/nodes")
    k8s_nodes = {(n.get("metadata") or {}).get("name"): n for n in _items("/api/v1/nodes")}
    skip_cordoned = _setting("disable-scheduling-on-cordoned-node", "true") == "true"
    offline = _setting("offline-replica-rebuilding", "false") == "true"
    hosts = {}
    for node in lh_nodes:
        name = (node.get("metadata") or {}).get("name")
        k8s = k8s_nodes.get(name) or {}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (k8s.get("status") or {}).get("conditions") or [])
        cordoned = bool((k8s.get("spec") or {}).get("unschedulable"))
        status_of = (node.get("status") or {}).get("diskStatus") or {}
        disks = [{"tags": set(d.get("tags") or []), "takes": bool(d.get("allowScheduling", True)),
                  "available": int((status_of.get(key) or {}).get("storageAvailable") or 0),
                  "maximum": int((status_of.get(key) or {}).get("storageMaximum") or 0)}
                 for key, d in ((node.get("spec") or {}).get("disks") or {}).items()]
        hosts[name] = {"name": name, "load": 0, "tags": set((node.get("spec") or {}).get("tags") or []), "disks": disks,
                       "available": sum(d["available"] for d in disks),
                       "maximum": sum(d["maximum"] for d in disks),
                       "takes": bool((node.get("spec") or {}).get("allowScheduling", True)) and ready
                                and not (cordoned and skip_cordoned)}
    replicas = {}
    for replica in _items(f"{LH}/replicas"):
        spec = replica.get("spec") or {}
        replicas.setdefault(spec.get("volumeName"), []).append(replica)
    volumes = []
    for volume in _items(f"{LH}/volumes"):
        meta, spec, status = volume.get("metadata") or {}, volume.get("spec") or {}, volume.get("status") or {}
        name = meta.get("name", "")
        copies = replicas.get(name, [])
        size = int(status.get("actualSize") or 0)
        for replica in copies:
            host = (replica.get("spec") or {}).get("nodeID")
            if host in hosts and _whole(replica):
                hosts[host]["load"] += size
        k8s = status.get("kubernetesStatus") or {}
        why = ""
        whole = [r for r in copies if _whole(r)]
        if status.get("state") == "attached" and status.get("robustness") != "healthy":
            why = "it is not healthy now"
        elif len(whole) != int(spec.get("numberOfReplicas") or 0) or len(copies) != len(whole):
            why = "its copies are being rebuilt or changed"
        elif status.get("state") == "detached" and not offline and spec.get("offlineRebuilding") != "enabled":
            why = "it is detached, and Longhorn rebuilds copies of detached volumes only with offline rebuilding on"
        elif status.get("state") not in ("attached", "detached"):
            why = f"it is {status.get('state') or 'changing'}"
        volumes.append({"name": name, "claim": "/".join(x for x in (k8s.get("namespace"), k8s.get("pvcName")) if x) or name,
                        "app": _app(volume), "size": size, "attached": status.get("currentNodeID") or "",
                        "hosts": sorted({(r.get("spec") or {}).get("nodeID") for r in whole}), "why": why,
                        "node_tags": set(spec.get("nodeSelector") or []), "disk_tags": set(spec.get("diskSelector") or [])})
    return hosts, volumes


def plan(exclude=()):
    """The fewest moves that bring hosts closest, without the apps excluded."""
    hosts, volumes = inventory()
    exclude = set(exclude or ())
    load = {name: h["load"] for name, h in hosts.items()}
    room = {name: h["available"] for name, h in hosts.items()}
    takers = [name for name, h in hosts.items() if h["takes"]]
    movable = [v for v in volumes if not v["why"] and v["app"] not in exclude and v["size"] > 0]
    moves, moved, planned = [], set(), {}
    average = sum(load.values()) / max(1, len(load))
    def disk_room(target, v):
        """Free space on the target's disks this volume may use - its disk
        tags, as the storage class asked - less what is planned for there."""
        host = hosts[target]
        if not v["node_tags"] <= host["tags"]:
            return 0
        usable = [d for d in host["disks"] if d["takes"] and v["disk_tags"] <= d["tags"]]
        free = max((d["available"] - planned.get((target, id(d)), 0) for d in usable), default=0)
        return free - max(GIB, max((d["maximum"] for d in usable), default=0) * 0.25)

    def fits(source, target, gap):
        return [v for v in movable if v["name"] not in moved and source in v["hosts"] and target not in v["hosts"]
                and v["attached"] != source            # never off the host it is attached to
                and v["size"] < gap                     # a move that narrows the gap
                and v["size"] >= GIB                    # and is worth a move: tiny ones barely change it
                and disk_room(target, v) > v["size"]]
    while len(moves) < MAX_MOVES and takers:
        target = min(takers, key=lambda h: load[h])
        # The fullest host that has something it can give; two equally full
        # hosts may each hold only what is attached there.
        best = None
        for source in sorted(load, key=load.get, reverse=True):
            gap = load[source] - load[target]
            if source == target or gap <= max(GIB, average * 0.1):
                break
            choices = fits(source, target, gap)
            if choices:
                best = min(choices, key=lambda v: abs(v["size"] - gap / 2))
                break
        if not best:
            break
        moves.append({"volume": best["name"], "claim": best["claim"], "app": best["app"], "from": source, "to": target,
                      "size_gb": round(best["size"] / GIB, 1), "attached": bool(best["attached"])})
        moved.add(best["name"])
        load[source] -= best["size"]
        load[target] += best["size"]
        room[target] -= best["size"]
        usable = [d for d in hosts[target]["disks"] if d["takes"] and best["disk_tags"] <= d["tags"]]
        disk = max(usable, key=lambda d: d["available"] - planned.get((target, id(d)), 0))
        planned[(target, id(disk))] = planned.get((target, id(disk)), 0) + best["size"]
        best["hosts"] = [h for h in best["hosts"] if h != source] + [target]
    before = {name: h["load"] for name, h in hosts.items()}
    apps = sorted({v["app"] for v in volumes if not v["why"] and v["size"] > 0
                   and any(m["app"] == v["app"] for m in moves)} | (exclude & {v["app"] for v in volumes}))
    token = review_token(moves, exclude)
    return {"hosts": [{"name": name, "before_gb": round(before[name] / GIB, 1), "after_gb": round(load[name] / GIB, 1),
                       "capacity_gb": round(hosts[name]["maximum"] / GIB, 1), "takes": hosts[name]["takes"]}
                      for name in sorted(hosts)],
            "moves": moves, "apps": apps, "excluded": sorted(exclude),
            # Why nothing moves when the emptiest host is the one that cannot take copies.
            "closed": sorted(name for name, h in hosts.items() if not h["takes"]),
            "skipped": [{"claim": v["claim"], "why": v["why"]} for v in volumes if v["why"]],
            "review_token": token}


# --------------------------------------------------------------- the job
def review_token(moves, exclude):
    """What was reviewed: which volume's copy goes from where to where, and
    what was vetoed - not sizes, which grow while the apps write."""
    return hashlib.sha256(json.dumps([[[m["volume"], m["from"], m["to"]] for m in moves], sorted(exclude or ())],
                                     sort_keys=True).encode()).hexdigest()[:20]


def reviewed(moves, exclude, token):
    """The copy moves a person reviewed, checked against Longhorn now: each
    volume still whole, with a copy on the host it leaves and none on the one
    it goes to, not attached where it leaves, and that host still taking
    copies. Data written since does not matter."""
    if not isinstance(moves, list) or not moves or len(moves) > MAX_MOVES:
        raise ValueError("Review the copies to move again")
    keys = ("volume", "claim", "app", "from", "to")
    if any(not isinstance(m, dict) or not all(isinstance(m.get(k), str) and m.get(k) for k in keys) for m in moves):
        raise ValueError("Review the copies to move again")
    if review_token(moves, exclude) != token:
        raise ValueError("The moves differ from the review; review again")
    hosts, volumes = inventory()
    by_name = {v["name"]: v for v in volumes}
    out = []
    for m in moves:
        v, host = by_name.get(m["volume"]), hosts.get(m["to"])
        if (not v or v["why"] or m["from"] not in v["hosts"] or m["to"] in v["hosts"] or v["attached"] == m["from"]
                or v["app"] in set(exclude or ())):
            raise ValueError(f"{m['claim']} changed since the review; review again")
        if not host or not host["takes"]:
            raise ValueError(f"{m['to']} does not take copies now; review again")
        out.append({**{k: m[k] for k in keys}, "size_gb": round(v["size"] / GIB, 1), "attached": bool(v["attached"])})
    return out


def _volume(name):
    return kget(f"{LH}/volumes/{name}")


def _copies(name):
    return [r for r in _items(f"{LH}/replicas") if (r.get("spec") or {}).get("volumeName") == name]


def _replicas(name, count, keeps=None):
    """Set the copy count; `keeps` marks the volume as moving a copy (the
    count it keeps meanwhile), and None clears that mark."""
    ksend("PATCH", f"{LH}/volumes/{name}", {"metadata": {"annotations": {MOVING: str(keeps) if keeps else None}},
                                           "spec": {"numberOfReplicas": int(count)}},
          ctype="application/merge-patch+json")


def _built(volume, replica):
    """A new copy is whole: Longhorn marked it healthy and, attached, its engine reads and writes it."""
    if not _whole(replica) or not (replica.get("spec") or {}).get("healthyAt"):
        return False
    if (volume.get("status") or {}).get("state") != "attached":
        return True
    name = (replica.get("metadata") or {}).get("name")
    for engine in _items(f"{LH}/engines"):
        if (engine.get("spec") or {}).get("volumeName") == (volume.get("metadata") or {}).get("name"):
            return ((engine.get("status") or {}).get("replicaModeMap") or {}).get(name) == "RW"
    return False


def status(item):
    """One step of the current move; the leader takes it, anyone may look."""
    ref = item["ref"]
    moves, i = ref["moves"], ref.get("index", 0)
    done = ref.get("moved", 0)
    if i >= len(moves):
        skipped = ref.get("skipped") or []
        return "succeeded", 100, (f"Moved {done} cop{'y' if done == 1 else 'ies'}"
                                  + (f"; left {len(skipped)}: " + "; ".join(skipped[:4]) if skipped else ""))
    move = moves[i]
    progress = int(100 * i / max(1, len(moves)))
    label = f"{move['claim']} from {move['from']} to {move['to']} ({i + 1} of {len(moves)})"
    if not is_leader():
        return "running", progress, item.get("message") or f"Moving {label}"
    stage, now = ref.get("stage", "add"), time.time()

    def skip(why):
        if stage in ("building", "settling") and ref.get("original"):
            _replicas(move["volume"], ref["original"])
        ref.setdefault("skipped", []).append(f"{move['claim']}: {why}")
        ref.update(index=i + 1, stage="add", original=None, before=None, placed=False, gone_since=None)
        return "running", progress, f"Skipped {move['claim']}: {why}"

    try:
        volume = _volume(move["volume"])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return skip("it no longer exists")
        raise
    copies = _copies(move["volume"])
    hosts = {(r.get("spec") or {}).get("nodeID"): r for r in copies}
    spec = volume.get("spec") or {}
    if stage == "add":
        whole = [r for r in copies if _whole(r)]
        if move["from"] not in hosts or move["to"] in hosts or len(whole) != len(copies) \
                or len(whole) != int(spec.get("numberOfReplicas") or 0):
            return skip("its copies changed since the review")
        ref.update(stage="building", original=int(spec["numberOfReplicas"]), started=now,
                   before=sorted((r.get("metadata") or {}).get("name") for r in copies))
        _replicas(move["volume"], ref["original"] + 1, keeps=ref["original"])
        return "running", progress, f"Building a copy of {label}"
    if stage == "building":
        new = [r for r in copies if (r.get("metadata") or {}).get("name") not in (ref.get("before") or [])]
        if not new:
            if ref.get("placed"):
                # It was placed and is gone: Longhorn drops a copy whose
                # rebuild failed and makes another. That is not "never
                # placed", and the hour already spent building is no reason
                # to give up on the first look without it.
                gone = ref.setdefault("gone_since", now)
                if now - gone > 600:
                    return skip("Longhorn dropped the new copy and did not make another within 10 minutes")
                return "running", progress, f"Longhorn is replacing the new copy of {label}"
            if now - ref["started"] > 600:
                return skip("Longhorn did not place the new copy within 10 minutes")
            return "running", progress, f"Waiting for Longhorn to place a copy of {label}"
        ref["placed"] = True
        ref.pop("gone_since", None)
        host = (new[0].get("spec") or {}).get("nodeID")
        if host == move["from"]:
            return skip("Longhorn placed the new copy on the same host")
        limit = 3600 + 120 * move.get("size_gb", 0)
        if now - ref["started"] > limit:
            return skip("the new copy was not whole in time")
        if not _built(volume, new[0]):
            return "running", progress, f"Building a copy of {label} on {host}"
        move["to"] = host
        ref["stage"] = "removing"
        stage = "removing"
    if stage == "removing":
        old = hosts.get(move["from"])
        if old:
            ksend("DELETE", f"{LH}/replicas/{(old.get('metadata') or {}).get('name')}")
        # Still marked: until the old copy is gone it is one over, not short.
        _replicas(move["volume"], ref["original"], keeps=ref["original"])
        ref.update(stage="settling", settle_from=now)
        return "running", progress, f"Removing the copy of {move['claim']} on {move['from']}"
    if stage == "settling":
        whole = [r for r in copies if _whole(r)]
        if len(copies) == len(whole) == int(spec.get("numberOfReplicas") or 0):
            _replicas(move["volume"], ref["original"])
            ref.update(index=i + 1, stage="add", original=None, before=None, moved=done + 1, placed=False, gone_since=None)
            return "running", int(100 * (i + 1) / len(moves)), f"Moved {move['claim']} to {move['to']}"
        if now - ref.get("settle_from", now) > 900:
            _replicas(move["volume"], ref["original"])
            ref.update(index=i + 1, stage="add", original=None, before=None, moved=done + 1, placed=False, gone_since=None)
            return "running", progress, f"{move['claim']} moved; Longhorn is still tidying its copies"
        return "running", progress, f"Waiting for {move['claim']} to settle at {spec.get('numberOfReplicas')} copies"
    return skip("an unknown step")


def cancel_plan(item):
    return {"mode": "stop", "action": "Stop rebalancing",
            "undo": ["The copy being built now is dropped; the volume keeps the copies it had"],
            "keeps": ["Copies already moved stay where they are"], "needs": "operator"}


def cancel_run(work, chosen):
    ref = work.get("ref") or {}
    moves, i = ref.get("moves") or [], ref.get("index", 0)
    if i < len(moves) and ref.get("stage") in ("building", "settling") and ref.get("original"):
        _replicas(moves[i]["volume"], ref["original"])
    return f"Stopped after moving {ref.get('moved', 0)} cop{'y' if ref.get('moved', 0) == 1 else 'ies'}"


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/longhorn/rebalance/plan"): ("viewer", lambda request: plan([a for a in (request.query.get("exclude") or [""])[0].split(",") if a])),
}
