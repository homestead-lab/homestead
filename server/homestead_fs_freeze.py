"""Filesystem-consistent snapshots: Longhorn's filesystem freeze.

Longhorn 1.7 and later can freeze a volume's filesystem for the moment it
takes a snapshot, then thaw it. Writes wait for about a second; the snapshot,
and every backup made from it, then holds the filesystem as it was at one
instant, not as a crash would have left it. Longhorn's own recurring jobs
use it too, as do Homestead's snapshots and backups: each asks Longhorn,
which reads the volume's freezeFilesystemForSnapshot ("ignored" follows the
cluster setting, "enabled" and "disabled" override it) and then the setting
freeze-filesystem-for-snapshot. Homestead only shows and changes those two.

What Longhorn does not freeze, and syncs instead: block volumes (VM disks)
and anything without a filesystem mounted on the host from Longhorn's own
device, which in practice includes shared (RWX) volumes, whose filesystem
Longhorn's share manager holds. V2 volumes are never frozen. If the freeze
itself fails, Longhorn fails that snapshot rather than take it unfrozen.

Longhorn ships it off because a kernel 5.17 or older may hang a filesystem
when a volume fails mid-freeze; the page names Longhorn hosts that old.
Homestead never turns it on by itself: an admin does, after a review.

Longhorn 1.10 and later keep the value per data engine ({"v1":"false"});
1.7 to 1.9 keep it plain ("false"). A write keeps the format it found.
"""
import json
import re

import homestead_changes as CHANGES
import homestead_kube as KUBE
import homestead_routes

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
LHNS = "longhorn-system"
SETTING = "freeze-filesystem-for-snapshot"
MODES = ("ignored", "enabled", "disabled")
OLD_KERNEL = (5, 17)
CHART_SETTINGS = "/api/v1/namespaces/longhorn-system/configmaps/longhorn-default-setting"
MERGE = "application/merge-patch+json"

kget = KUBE.kget
ksend = KUBE.ksend


def _optional(path):
    return KUBE.get_optional(path, read=kget)


def _items(path):
    return KUBE.items(path, read=kget)


def parse_value(raw):
    """(on, format) of the setting's value: on is None when it is neither
    true nor false; format is "json" for {"v1": ...}, else "plain"."""
    text = raw.strip() if isinstance(raw, str) else ""
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError:
            return None, "json"
        v1 = value.get("v1") if isinstance(value, dict) else None
        return ({"true": True, "false": False}.get(v1.lower()) if isinstance(v1, str) else None), "json"
    return {"true": True, "false": False}.get(text.lower()), "plain"


def encode_value(enabled, raw):
    """The value to write: the V1 engine's, in the format the setting has,
    keeping any other engine's value as it is."""
    word = "true" if enabled else "false"
    if parse_value(raw)[1] != "json":
        return word
    try:
        value = json.loads(raw)
    except ValueError:
        value = {}
    value = dict(value) if isinstance(value, dict) else {}
    value["v1"] = word
    return json.dumps(value, separators=(",", ":"))


def kernel_old(version):
    """True for a kernel at or below 5.17, None when it cannot be read."""
    found = re.match(r"\s*(\d+)\.(\d+)", version or "") if isinstance(version, str) else None
    return None if not found else (int(found.group(1)), int(found.group(2))) <= OLD_KERNEL


def _version():
    return str((_optional(f"{LH}/settings/current-longhorn-version") or {}).get("value") or "")


def _from_chart():
    """Whether Longhorn's chart values set it: Longhorn applies those again
    whenever its chart is applied, over a change made here."""
    data = ((_optional(CHART_SETTINGS) or {}).get("data") or {}).get("default-setting.yaml") or ""
    return any(line.split(":", 1)[0].strip().strip("\"'") == SETTING for line in data.splitlines())


def _old_kernels():
    storing = {(n.get("metadata") or {}).get("name") for n in _items(f"{LH}/nodes")}
    out = []
    for item in _items("/api/v1/nodes"):
        name = (item.get("metadata") or {}).get("name")
        kernel = ((item.get("status") or {}).get("nodeInfo") or {}).get("kernelVersion") or ""
        if name in storing and kernel_old(kernel):
            out.append({"host": name, "kernel": kernel})
    return out


def _row(volume, enabled, modes):
    meta, spec = volume.get("metadata") or {}, volume.get("spec") or {}
    k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
    name = meta.get("name", "")
    mode = spec.get("freezeFilesystemForSnapshot") or "ignored"
    engine = spec.get("dataEngine") or "v1"
    access = spec.get("accessMode") or "rwo"
    volume_mode = modes.get(k8s.get("pvName") or name, "unknown")
    on = mode == "enabled" or (mode not in ("enabled", "disabled") and bool(enabled))
    if engine == "v2":
        freezes, why = "no", "A V2 volume: Longhorn never freezes these"
    elif not on:
        freezes, why = "no", ("Set to never freeze" if mode == "disabled" else "Follows the cluster, which is off")
    elif volume_mode == "Block":
        freezes, why = "sync", "A block volume, such as a VM disk: there is no filesystem to freeze, so Longhorn syncs it instead"
    elif access == "rwx":
        freezes, why = "sync", "A shared (RWX) volume: Longhorn's share manager holds its filesystem, so Longhorn syncs it instead"
    elif spec.get("frontend") not in (None, "", "blockdev"):
        freezes, why = "sync", "No Longhorn device on the host to freeze, so Longhorn syncs it instead"
    else:
        freezes, why = "yes", ""
    return {"name": name, "claim": "/".join(x for x in (k8s.get("namespace"), k8s.get("pvcName")) if x),
            "mode": mode, "freezes": freezes, "why": why, "engine": engine, "access": access, "volume_mode": volume_mode}


def status():
    version = _version()
    item = _optional(f"{LH}/settings/{SETTING}")
    if item is None:
        return {"supported": False, "version": version, "volumes": [],
                "why": f"Longhorn {version or 'on this cluster'} cannot freeze filesystems for snapshots: Longhorn 1.7 added it."}
    raw = item.get("value")
    enabled, _ = parse_value(raw)
    modes = {(p.get("metadata") or {}).get("name"): (p.get("spec") or {}).get("volumeMode") or "Filesystem"
             for p in _items("/api/v1/persistentvolumes")}
    rows = sorted((_row(v, enabled, modes) for v in _items(f"{LH}/volumes")), key=lambda r: (r["claim"] or "~", r["name"]))
    return {"supported": True, "version": version, "enabled": bool(enabled), "value": raw if isinstance(raw, str) else "",
            "unknown_value": enabled is None, "from_chart": _from_chart(),
            "changed": CHANGES.object_author("Setting", LHNS, SETTING, item),
            "old_kernels": _old_kernels(), "volumes": rows,
            "counts": {"total": len(rows), "freezing": sum(r["freezes"] == "yes" for r in rows)}}
