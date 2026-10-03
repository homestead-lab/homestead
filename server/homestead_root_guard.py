"""Longhorn kept from filling a host's root filesystem.

On k3s and RKE2 Longhorn's first disk is /var/lib/longhorn, which - unless
it is a volume of its own (the installer makes one where the system is on
LVM) - is a folder on the system's root filesystem. Longhorn keeps 30% of
that filesystem out of its own sums, but that only decides where new copies
go: copies already there, and their snapshots, keep growing, and the
system's logs, images and packages share the same space.

So the leader watches each Longhorn disk whose folder is on a host's root
filesystem (as the node probe's mounts say; no probe, no decision). When
that filesystem's free space falls under a floor - 15%, and never under
10 GB - Longhorn stops placing new copies there; once it is back over 20%
(15 GB) it may again. Only a disk Homestead stopped is started again: one
someone stopped by hand is theirs. Copies already on it stay; moving them is
Move replicas off, which copies data and is a person's decision. What is
stopped is an alert, and says so.
"""
import json
import os
import time

import homestead_shared as SHARED

kget = ksend = platform = probes = None
DATA_DIR = "/data"
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
GB = 1024 ** 3
STOP = (0.15, 10 * GB)      # stop below the larger of these
START = (0.20, 15 * GB)     # start again above the larger of these


def bind(_kget, _ksend, _platform, _probes, data_dir="/data"):
    global kget, ksend, platform, probes, DATA_DIR
    kget, ksend, platform, probes, DATA_DIR = _kget, _ksend, _platform, _probes, data_dir


def _path():
    return os.path.join(DATA_DIR, "root-guard.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def mount_of(path, mounts):
    """The mount point a folder lives under: the longest that contains it."""
    best = ""
    for row in mounts or []:
        point = (row.get("mountpoint") or "").rstrip("/") or "/"
        if path == point or path.startswith(point.rstrip("/") + "/") or point == "/":
            if len(point) > len(best):
                best = point
    return best


def floor(total, rule):
    share, least = rule
    return max(total * share, least)


def tick():
    """Stop or start each Longhorn disk on a root filesystem. Returns [(node, words)]."""
    p = platform(True) or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2") or not p.get("longhorn"):
        return []
    found = probes() or {}
    state = _load()
    stopped = state.setdefault("stopped", {})
    changes = []
    for lh in (kget(f"{LH}/nodes") or {}).get("items", []):
        node = lh["metadata"]["name"]
        mounts = (found.get(node) or {}).get("mounts")
        if not mounts:
            continue
        statuses = (lh.get("status") or {}).get("diskStatus") or {}
        for disk_id, disk in ((lh.get("spec") or {}).get("disks") or {}).items():
            key = f"{node}/{disk_id}"
            if disk.get("diskType") == "block" or mount_of(disk.get("path") or "", mounts) != "/":
                stopped.pop(key, None)
                continue
            status = statuses.get(disk_id) or {}
            total, free = int(status.get("storageMaximum") or 0), int(status.get("storageAvailable") or 0)
            if not total:
                continue
            allowed = disk.get("allowScheduling", True) is not False
            if allowed and free < floor(total, STOP):
                _schedule(node, disk_id, False)
                stopped[key] = {"at": int(time.time()), "free_gb": round(free / GB, 1), "total_gb": round(total / GB, 1)}
                changes.append((node, f"Longhorn stopped placing copies on {disk.get('path')}: the root filesystem "
                                      f"has {round(free / GB, 1)} GB free of {round(total / GB, 1)} GB"))
            elif key in stopped and not allowed and free > floor(total, START):
                _schedule(node, disk_id, True)
                stopped.pop(key)
                changes.append((node, f"Longhorn may place copies on {disk.get('path')} again: "
                                      f"{round(free / GB, 1)} GB free"))
            elif key in stopped:
                stopped[key]["free_gb"] = round(free / GB, 1)
    SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    return changes


def _schedule(node, disk_id, allow):
    ksend("PATCH", f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: {"allowScheduling": allow}}}},
          ctype="application/merge-patch+json")


def alert_facts():
    facts = []
    for key, row in (_load().get("stopped") or {}).items():
        node = key.split("/", 1)[0]
        facts.append({"key": f"rootguard:{key}", "category": "degraded", "severity": "degraded",
                      "title": f"{node}'s root filesystem is low: Longhorn stopped placing copies there",
                      "resolved": f"Root filesystem protection cleared on {node}",
                      "body": (f"{row.get('free_gb')} GB free of {row.get('total_gb')} GB. Copies already there stay; "
                               "Review disk usage and move replicas or expand storage to free space."),
                      "href": "/nodes"})
    return facts
