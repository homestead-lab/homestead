"""How much more Longhorn can place on each node, and the settings that decide it.

Longhorn books a replica's full size on a disk when it places it - Allocated
in its UI - however little the volume holds. A disk takes a new replica while

    allocated + new size <= (disk size - reserved) x over-provisioning %

and while its real free space stays above the minimal-available share. Past
that, new volumes come up a copy short, rebuilds wait and expansions are
refused; nothing already placed is moved. A replicated volume needs room on
as many different nodes as it has copies, so the smallest of those is the
biggest volume that still fits - the number worth watching.

The V2 (SPDK) data engine is switched on here too. On Harvester its own
setting drives Longhorn's and prepares the hosts, so that is what changes.
"""
import json
import time
import urllib.error
from datetime import datetime
import homestead_disk_v2 as DISK_V2
import homestead_operations as OPS
import homestead_routes
import homestead_kube as KUBE

kget = ksend = None
v2_status = lambda: {}
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
HARVESTER_V2 = "/apis/harvesterhci.io/v1beta1/settings/longhorn-v2-data-engine-enabled"
OVER = "storage-over-provisioning-percentage"
MINIMAL = "storage-minimal-available-percentage"
V2 = "v2-data-engine"
# What Longhorn does with the pods of a node that stops answering. Left at
# do-nothing, a pod on a dead node stays Terminating and holds its volume, so
# a container told to move to another node cannot mount it there.
NODE_DOWN = "node-down-pod-deletion-policy"
NODE_DOWN_VALUES = ("do-nothing", "delete-statefulset-pod", "delete-deployment-pod",
                    "delete-both-statefulset-and-deployment-pod")
# Longhorn's default, do-nothing, keeps a down node's pods - and with them
# every volume one node mounts at a time - until a person deletes them: an app
# set to move starts on another host and waits there for its volume for as
# long as the host is down. Homestead sets the policy once (ensure_node_down);
# the mark on the setting keeps a later choice, do-nothing included, as it is.
NODE_DOWN_RELEASE = "delete-both-statefulset-and-deployment-pod"
NODE_DOWN_MARK = "homestead.io/node-down-set"
# A down host still holding a volume this long after it stopped answering:
# past the 15 s failover and Longhorn's own clean-up, so the alert names a
# stuck volume, not one about to be let go.
HELD_GRACE = 180
# How many replicas one node rebuilds at once. More gets a failed node's
# volumes whole again sooner, at the cost of disk and network while it runs.
# Longhorn reads 0 as never rebuild, which would leave volumes a copy short
# without a word, so it is not offered.
REBUILD = "concurrent-replica-rebuild-per-node-limit"
REBUILD_RANGE = (1, 10)
WARN_PCT, CRIT_PCT = 80, 95
GiB = 1024 ** 3


def bind(_kget, _ksend, _v2_status):
    global kget, ksend, v2_status
    kget, ksend, v2_status = _kget, _ksend, _v2_status


def _setting(name, default):
    try:
        item = kget(f"{LH}/settings/{name}")
        return str(item.get("value") if item.get("value") not in (None, "") else item.get("default", default))
    except Exception:
        return str(default)


def node_down(read=None):
    """Longhorn's policy for the pods of a node that is down, or None when it
    couldn't be read - Longhorn not installed, say. Unlike _setting, a failed
    read is not taken for the default: the impact preview says "couldn't be
    checked" rather than guess (#440)."""
    try:
        item = (read or kget)(f"{LH}/settings/{NODE_DOWN}") or {}
    except Exception:
        return None
    value = item.get("value") if item.get("value") not in (None, "") else item.get("default")
    return str(value) if value not in (None, "") else "do-nothing"


def ensure_node_down(read=None, send=None):
    """Once per cluster: Longhorn's default policy for a down node becomes
    NODE_DOWN_RELEASE, so a failed host lets go of its volumes. True when it
    was changed, False when the setting was marked already or someone had
    chosen a policy, None when it couldn't be read (Longhorn not installed,
    say) - tried again later."""
    read, send = read or kget, send or ksend
    try:
        item = read(f"{LH}/settings/{NODE_DOWN}") or {}
    except Exception:
        return None
    if ((item.get("metadata") or {}).get("annotations") or {}).get(NODE_DOWN_MARK):
        return False
    value = item.get("value") if item.get("value") not in (None, "") else item.get("default")
    change = value in (None, "", "do-nothing")
    body = {"metadata": {"annotations": {NODE_DOWN_MARK: "1"}}, **({"value": NODE_DOWN_RELEASE} if change else {})}
    send("PATCH", f"{LH}/settings/{NODE_DOWN}", body, ctype="application/merge-patch+json")
    return change


def _ready_since(node):
    """(ready, since): whether a node is Ready, and when that last changed."""
    cond = next((c for c in (node.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready"), {})
    return cond.get("status") == "True", cond.get("lastTransitionTime") or ""


def held_volumes(attachments, nodes, pvs, policy, now, parse_time):
    """The single-host Longhorn volumes each down host still holds, HELD_GRACE
    after it went down: their apps cannot start anywhere else meanwhile.
    attachments, nodes, pvs: the Kubernetes lists; policy: NODE_DOWN's value,
    or None unread; parse_time: an RFC 3339 time to seconds."""
    claims = {}
    for pv in pvs or []:
        spec = pv.get("spec") or {}
        ref = spec.get("claimRef") or {}
        if set(spec.get("accessModes") or []) & {"ReadWriteOnce", "ReadWriteOncePod"} and ref.get("name"):
            claims[pv["metadata"]["name"]] = f"{ref.get('namespace')}/{ref['name']}"
    held = {}
    for node in nodes or []:
        ready, since = _ready_since(node)
        try:
            down_for = now - parse_time(since)
        except Exception:
            continue
        if ready or down_for < HELD_GRACE:
            continue
        name = node["metadata"]["name"]
        rows = sorted(claims[a["spec"]["source"]["persistentVolumeName"]] for a in attachments or []
                      if (a.get("spec") or {}).get("nodeName") == name
                      and (a.get("spec") or {}).get("attacher") == "driver.longhorn.io"
                      and (a.get("status") or {}).get("attached")
                      and ((a.get("spec") or {}).get("source") or {}).get("persistentVolumeName") in claims)
        if rows:
            held[name] = {"claims": rows, "down_minutes": int(down_for // 60), "policy": policy}
    return held


def held_alert_facts(held):
    """One alert per down host still holding volumes, naming them: the summary
    first, then why, and the way out (design.md, Notices)."""
    facts = []
    for node, row in sorted(held.items()):
        count, names = len(row["claims"]), ", ".join(row["claims"][:6]) + (" and more" if len(row["claims"]) > 6 else "")
        releasing = row["policy"] in ("delete-deployment-pod", NODE_DOWN_RELEASE)
        why = ("Longhorn is set to release them but has not done so yet. Check the longhorn-manager logs."
               if releasing else
               f"To fix this, turn on Allow apps with storage to move off a failed host (Settings, Hardware and storage), "
               f"or bring {node} back online.")
        facts.append({"signals": {"held": count}, "key": f"held-volumes:{node}", "category": "outage", "severity": "critical",
                      "title": f"{node} is down; {count} volume{'s are' if count != 1 else ' is'} still attached to it",
                      "resolved": f"No volumes are attached to {node} any more",
                      "body": f"Apps that use these volumes cannot start on another host: {names}. {why}",
                      "href": "/settings?tab=cluster"})
    return facts


def held_status(now=None):
    """held_volumes from the cluster as it is now."""
    parse = lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    held = held_volumes(KUBE.items("/apis/storage.k8s.io/v1/volumeattachments", read=kget),
                        KUBE.items("/api/v1/nodes", read=kget),
                        KUBE.items("/api/v1/persistentvolumes", read=kget),
                        None, now or time.time(), parse)
    # The policy only changes what the alert advises, so it is read only when
    # there is something to advise on: an unread Longhorn never empties the list.
    if held:
        policy = node_down()
        for row in held.values():
            row["policy"] = policy
    return held


def _int(text, default):
    try:
        return int(str(text).strip())
    except ValueError:
        return default


def settings():
    return {"over_provisioning": _int(_setting(OVER, 100), 100),
            "minimal_available": _int(_setting(MINIMAL, 25), 25),
            "node_down": _setting(NODE_DOWN, "do-nothing"),
            "rebuild_limit": _int(_setting(REBUILD, 5), 5),
            "v2": v2_status()}


def _gb(value):
    return round((value or 0) / GiB, 1)


def capacity(cfg=None):
    """Each node's disks: what is allocated against what may be, and the
    room left; then the biggest new volume that fits for 1, 2 and 3 copies."""
    cfg = cfg or settings()
    over, minimal = cfg["over_provisioning"], cfg["minimal_available"]
    nodes = []
    for node in kget(f"{LH}/nodes").get("items", []):
        spec, status = node.get("spec") or {}, node.get("status") or {}
        node_ok = spec.get("allowScheduling", True) is not False
        disks = []
        for disk_id, d in (status.get("diskStatus") or {}).items():
            disk_spec = (spec.get("disks") or {}).get(disk_id) or {}
            maximum, available = d.get("storageMaximum") or 0, d.get("storageAvailable") or 0
            scheduled, reserved = d.get("storageScheduled") or 0, disk_spec.get("storageReserved") or 0
            limit = max(0, (maximum - reserved)) * over / 100
            physical_room = max(0, available - maximum * minimal / 100)
            condition = next((c for c in d.get("conditions") or [] if c.get("type") == "Schedulable"), {})
            reason = ("" if node_ok else "scheduling is off on this node") or \
                     ("" if disk_spec.get("allowScheduling", True) is not False else "scheduling is off on this disk") or \
                     ("" if condition.get("status", "True") == "True" else (condition.get("message") or "Longhorn marks it unschedulable")) or \
                     ("" if available > maximum * minimal / 100 else f"less than {minimal}% of it is physically free")
            disks.append({"id": disk_id, "path": disk_spec.get("path", ""), "type": disk_spec.get("diskType", "filesystem"),
                          "tags": list(disk_spec.get("tags") or []),
                          "size_gb": _gb(maximum - reserved), "allocated_gb": _gb(scheduled), "limit_gb": _gb(limit),
                          "used_gb": _gb(maximum - available), "free_gb": _gb(available),
                          "room_gb": 0.0 if reason else _gb(max(0, limit - scheduled)),
                          "physical_room_gb": 0.0 if reason else _gb(physical_room),
                          "pct": round(scheduled / limit * 100, 1) if limit else 0.0, "blocked": reason})
        usable = [d for d in disks if not d["blocked"]]
        allocated = sum(d["allocated_gb"] for d in disks)
        limit = sum(d["limit_gb"] for d in disks)
        pct = round(allocated / limit * 100, 1) if limit else 0.0
        room = max((d["room_gb"] for d in usable), default=0.0)
        physical_room = max((d["physical_room_gb"] for d in usable), default=0.0)
        nodes.append({"name": node["metadata"]["name"], "tags": list(spec.get("tags") or []), "disks": disks, "allocated_gb": round(allocated, 1),
                      "limit_gb": round(limit, 1), "size_gb": round(sum(d["size_gb"] for d in disks), 1),
                      "used_gb": round(sum(d["used_gb"] for d in disks), 1), "pct": pct, "room_gb": room,
                      "physical_room_gb": physical_room,
                      "level": "crit" if not usable or pct >= CRIT_PCT else "warn" if pct >= WARN_PCT else "ok",
                      "blocked": "" if usable else (disks[0]["blocked"] if disks else "no disks")})
    rooms = sorted((n["room_gb"] for n in nodes), reverse=True)
    largest = {str(copies): (rooms[copies - 1] if len(rooms) >= copies else 0.0) for copies in (1, 2, 3)}
    return {"over_provisioning": over, "minimal_available": minimal, "nodes": nodes, "largest": largest,
            "warn_pct": WARN_PCT, "crit_pct": CRIT_PCT}


def status():
    cfg = settings()
    out = capacity(cfg)
    out["v2"] = cfg["v2"]
    out["node_down"] = cfg["node_down"]
    return out


def _patch_setting(name, value):
    try:
        ksend("PATCH", f"{LH}/settings/{name}", {"value": str(value)}, ctype="application/merge-patch+json")
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            message = ""
        raise ValueError(f"Longhorn refused {name} = {value}: {message or f'HTTP {error.code}'}")


def save(cfg, *, allow_v2_enable=False):
    """Over-provisioning, minimal available space, and the V2 engine."""
    done = []
    current = settings()
    if cfg.get("v2") and not current["v2"].get("enabled") and not allow_v2_enable:
        raise ValueError("Use Set up Longhorn V2 to turn the engine on. It prepares hosts, checks hugepage capacity "
                         "and reviews the change")
    if "over_provisioning" in cfg:
        over = _int(cfg["over_provisioning"], -1)
        if not 100 <= over <= 1000:
            raise ValueError("over-provisioning is between 100% and 1000%")
        if over != current["over_provisioning"]:
            _patch_setting(OVER, over)
            done.append(f"over-provisioning {over}%")
    if "minimal_available" in cfg:
        minimal = _int(cfg["minimal_available"], -1)
        if not 0 <= minimal <= 100:
            raise ValueError("minimal available space is between 0% and 100%")
        if minimal != current["minimal_available"]:
            _patch_setting(MINIMAL, minimal)
            done.append(f"minimal available {minimal}%")
    if "rebuild_limit" in cfg:
        limit = _int(cfg["rebuild_limit"], -1)
        if not REBUILD_RANGE[0] <= limit <= REBUILD_RANGE[1]:
            raise ValueError(f"replicas rebuilt at once on a node is between {REBUILD_RANGE[0]} and {REBUILD_RANGE[1]}")
        if limit != current["rebuild_limit"]:
            _patch_setting(REBUILD, limit)
            done.append(f"{limit} replica rebuild{'s' if limit != 1 else ''} at once per node")
    if cfg.get("node_down") and cfg["node_down"] != current["node_down"]:
        if cfg["node_down"] not in NODE_DOWN_VALUES:
            raise ValueError("pod deletion when a node is down is one of " + ", ".join(NODE_DOWN_VALUES))
        _patch_setting(NODE_DOWN, cfg["node_down"])
        try:
            ksend("PATCH", f"{LH}/settings/{NODE_DOWN}", {"metadata": {"annotations": {NODE_DOWN_MARK: "1"}}},
                  ctype="application/merge-patch+json")
        except Exception:
            pass        # unmarked: the next save marks it; until then only a do-nothing chosen here could be changed
        done.append("pods on a failed node: " + cfg["node_down"])
    if "v2" in cfg and bool(cfg["v2"]) != bool(current["v2"].get("enabled")):
        value = "true" if cfg["v2"] else "false"
        if not cfg["v2"]:
            # Off with V2 data on the cluster would stop every V2 volume and
            # disk: refused here, whatever Longhorn's own checks say.
            volumes = KUBE.items(f"{LH}/volumes", read=kget)
            nodes = KUBE.items(f"{LH}/nodes", read=kget)
            v2_volumes = [v["metadata"]["name"] for v in volumes if (v.get("spec") or {}).get("dataEngine") == "v2"]
            v2_disks = [f"{n['metadata']['name']}:{d.get('path')}" for n in nodes
                        for d in ((n.get("spec") or {}).get("disks") or {}).values() if d.get("diskType") == "block"]
            if v2_volumes or v2_disks:
                raise ValueError("the V2 engine stays on while it holds data. Move these volumes to V1 and "
                                 "remove the V2 disks first: " + ", ".join((v2_volumes + v2_disks)[:5]))
        if current["v2"].get("harvester_setting") is not None:
            # Harvester's setting drives Longhorn's and prepares each host;
            # writing Longhorn's alone would be put back.
            try:
                item = kget(HARVESTER_V2)
                item["value"] = value
                item.get("metadata", {}).pop("managedFields", None)
                ksend("PUT", HARVESTER_V2, item)
            except urllib.error.HTTPError as error:
                raise ValueError(f"Harvester refused to {'enable' if cfg['v2'] else 'disable'} the V2 engine: HTTP {error.code}")
        else:
            _patch_setting(V2, value)
        done.append("V2 data engine " + ("enabled" if cfg["v2"] else "disabled"))
    return {"ok": True, "detail": ("Saved: " + ", ".join(done)) if done else "Nothing changed", **status()}


def alert_facts(cap):
    """A node close to its allocation limit, before the next volume or
    rebuild finds it full."""
    facts = []
    for node in cap.get("nodes") or []:
        if node["level"] == "ok":
            continue
        facts.append({"signals": {"blocked": int(bool(node["blocked"])), "allocation_band": int(node["pct"] // 5)},
                      "key": f"capacity:{node['name']}", "category": "degraded", "severity": "degraded",
                      "title": (f"Longhorn scheduling is blocked on {node['name']}" if node["blocked"] else
                                f"Longhorn allocation limit is nearly reached on {node['name']}"),
                      "resolved": f"Longhorn capacity warning cleared on {node['name']}",
                      "body": (node["blocked"] or f"{node['allocated_gb']} of {node['limit_gb']} GiB allocated ({node['pct']}%); "
                               f"a new replica there can be at most {node['room_gb']} GiB"),
                      "href": "/volumes"})
    return facts


def save_checked(cfg):
    """save, refused when V2 would be switched off under a saved V2 disk
    preparation; checked under the jobs' lock so none starts meanwhile."""
    homestead_routes.forget("lhcap")
    with OPS.transaction():
        if "v2" in cfg and not cfg["v2"] and DISK_V2.tasks():
            raise ValueError("Finish or stop the saved V2 disk preparation task before disabling V2")
        return save(cfg)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/longhorn/capacity"): ("viewer", lambda request: homestead_routes.page("lhcap", status, 15)),
    ("POST", "/api/longhorn/settings"): ("admin", lambda request: save_checked(request.body)),
}
