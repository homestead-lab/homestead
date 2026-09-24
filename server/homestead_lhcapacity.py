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
import urllib.error

kget = ksend = None
v2_status = lambda: {}
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
HARVESTER_V2 = "/apis/harvesterhci.io/v1beta1/settings/longhorn-v2-data-engine-enabled"
OVER = "storage-over-provisioning-percentage"
MINIMAL = "storage-minimal-available-percentage"
V2 = "v2-data-engine"
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


def _int(text, default):
    try:
        return int(str(text).strip())
    except ValueError:
        return default


def settings():
    return {"over_provisioning": _int(_setting(OVER, 100), 100),
            "minimal_available": _int(_setting(MINIMAL, 25), 25),
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
            condition = next((c for c in d.get("conditions") or [] if c.get("type") == "Schedulable"), {})
            reason = ("" if node_ok else "scheduling is off on this node") or \
                     ("" if disk_spec.get("allowScheduling", True) is not False else "scheduling is off on this disk") or \
                     ("" if condition.get("status", "True") == "True" else (condition.get("message") or "Longhorn marks it unschedulable")) or \
                     ("" if available > maximum * minimal / 100 else f"less than {minimal}% of it is physically free")
            disks.append({"id": disk_id, "path": disk_spec.get("path", ""), "type": disk_spec.get("diskType", "filesystem"),
                          "size_gb": _gb(maximum - reserved), "allocated_gb": _gb(scheduled), "limit_gb": _gb(limit),
                          "used_gb": _gb(maximum - available), "free_gb": _gb(available),
                          "room_gb": 0.0 if reason else _gb(max(0, limit - scheduled)),
                          "pct": round(scheduled / limit * 100, 1) if limit else 0.0, "blocked": reason})
        usable = [d for d in disks if not d["blocked"]]
        allocated = sum(d["allocated_gb"] for d in disks)
        limit = sum(d["limit_gb"] for d in disks)
        pct = round(allocated / limit * 100, 1) if limit else 0.0
        room = max((d["room_gb"] for d in usable), default=0.0)
        nodes.append({"name": node["metadata"]["name"], "disks": disks, "allocated_gb": round(allocated, 1),
                      "limit_gb": round(limit, 1), "size_gb": round(sum(d["size_gb"] for d in disks), 1),
                      "used_gb": round(sum(d["used_gb"] for d in disks), 1), "pct": pct, "room_gb": room,
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


def save(cfg):
    """Over-provisioning, minimal available space, and the V2 engine."""
    done = []
    current = settings()
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
    if "v2" in cfg and bool(cfg["v2"]) != bool(current["v2"].get("enabled")):
        value = "true" if cfg["v2"] else "false"
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
        facts.append({"key": f"capacity:{node['name']}", "category": "degraded", "severity": "degraded",
                      "title": f"{node['name']} is nearly out of Longhorn space",
                      "resolved": f"{node['name']} has Longhorn space again",
                      "body": (node["blocked"] or f"{node['allocated_gb']} of {node['limit_gb']} GB allocated ({node['pct']}%); "
                               f"a new replica there can be at most {node['room_gb']} GB"),
                      "href": "/volumes"})
    return facts
