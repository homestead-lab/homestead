"""Configurable hardware inventory and container passthrough mappings.

iGPU stays built in because it is useful on nearly every homelab. Everything
else is data stored in a ConfigMap: a friendly name, scheduling label, device
path and optional USB VID:PID matches.  That lets the UI support accelerators,
capture cards, serial adapters and future hardware without another code change.
"""
import json
import hashlib
import re
import urllib.error

kget = ksend = None
NS = "lab"
_cache = {}

IGPU = {
    "id": "igpu", "name": "Intel/AMD iGPU", "description": "Integrated GPU render devices",
    "label": "hardware/igpu", "host_path": "/dev/dri", "container_path": "/dev/dri",
    "path_type": "Directory", "usb_ids": [], "builtin": True,
}

DEFAULT_CUSTOM = [
    {
        "id": "coral_pcie", "name": "Google Coral PCIe/M.2",
        "description": "Coral Edge TPU exposed as an Apex character device",
        "label": "hardware/coral-pcie", "host_path": "/dev/apex_0",
        "container_path": "/dev/apex_0", "path_type": "CharDevice", "usb_ids": [],
    },
    {
        "id": "coral_usb", "name": "Google Coral USB",
        "description": "Coral USB TPU; schedules by USB ID and passes the USB bus through",
        "label": "hardware/coral-usb", "host_path": "/dev/bus/usb",
        "container_path": "/dev/bus/usb", "path_type": "Directory",
        "usb_ids": ["18d1:9302", "1a6e:089a"],
    },
]

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,48}[a-z0-9])?$")
USB_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
PATH_TYPES = {"Directory", "CharDevice", "BlockDevice", "Socket", "File"}
AUTO_ANNOTATION = "harvui.io/auto-hardware"


def bind(_kget, _ksend, _ns, _cache_ref):
    global kget, ksend, NS, _cache
    kget, ksend, NS, _cache = _kget, _ksend, _ns, _cache_ref


def _bust():
    for key in list(_cache):
        if key.startswith(("hardware", "nodes", "ov", "wl", "flow", "impact:")):
            _cache.pop(key, None)


def _normalise(raw, existing=None):
    fid = str(raw.get("id") or "").strip().lower()
    if not ID_RE.fullmatch(fid) or fid == "igpu":
        raise ValueError("feature ID must be 1–50 lowercase letters, numbers, dashes or underscores")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 64:
        raise ValueError(f"{fid}: name must be 1–64 characters")
    host = str(raw.get("host_path") or "").strip()
    dest = str(raw.get("container_path") or host).strip()
    if not host.startswith("/dev/") or not dest.startswith("/dev/"):
        raise ValueError(f"{fid}: passthrough paths must be absolute paths under /dev")
    typ = str(raw.get("path_type") or "CharDevice")
    if typ not in PATH_TYPES:
        raise ValueError(f"{fid}: unsupported path type")
    usb = raw.get("usb_ids") or []
    if isinstance(usb, str):
        usb = re.split(r"[\s,]+", usb.strip()) if usb.strip() else []
    usb = sorted({str(x).strip().lower() for x in usb if str(x).strip()})
    if any(not USB_RE.fullmatch(x) for x in usb):
        raise ValueError(f"{fid}: USB IDs must look like 18d1:9302")
    old_label = (existing or {}).get("label")
    return {
        "id": fid, "name": name,
        "description": str(raw.get("description") or "").strip()[:180],
        "label": old_label or str(raw.get("label") or f"hardware.harvui.io/{fid}"),
        "host_path": host, "container_path": dest, "path_type": typ,
        "usb_ids": usb, "builtin": False,
    }


def custom_features():
    try:
        cm = kget(f"/api/v1/namespaces/{NS}/configmaps/harvui-hardware")
        raw = json.loads(cm.get("data", {}).get("features.json", "[]"))
        if not isinstance(raw, list):
            raise ValueError("features.json is not a list")
        return [_normalise(x) for x in raw]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return [dict(x, builtin=False) for x in DEFAULT_CUSTOM]


def features():
    return [dict(IGPU)] + custom_features()


def feature_map():
    return {x["id"]: x for x in features()}


def save_features(items):
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("features must be a list with at most 32 entries")
    old = {x["id"]: x for x in custom_features()}
    out, seen = [], set()
    for raw in items:
        f = _normalise(raw, old.get(str(raw.get("id") or "").strip().lower()))
        if f["id"] in seen:
            raise ValueError(f"duplicate feature ID: {f['id']}")
        seen.add(f["id"]); out.append(f)
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "harvui-hardware", "namespace": NS},
            "data": {"features.json": json.dumps(out, indent=2)}}
    try:
        kget(f"/api/v1/namespaces/{NS}/configmaps/harvui-hardware")
        ksend("PATCH", f"/api/v1/namespaces/{NS}/configmaps/harvui-hardware",
              {"data": body["data"]}, ctype="application/merge-patch+json")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{NS}/configmaps", body)
    _bust()
    return features()


def _path_present(path, devices):
    low = path.lower().rstrip("/")
    if low == "/dev/dri":
        return bool(devices.get("dri"))
    if low.startswith("/dev/apex"):
        return any(("/dev/" + str(x)).lower() == low for x in devices.get("apex", []))
    paths = [str(x).lower().rstrip("/") for x in devices.get("paths", [])]
    return low in paths or any(x.startswith(low + "/") for x in paths)


def _detected(feature, devices):
    usb = {f"{str(x.get('vid', '')).lower()}:{str(x.get('pid', '')).lower()}"
           for x in devices.get("usb", []) if isinstance(x, dict)}
    return bool(usb.intersection(feature["usb_ids"])) if feature["usb_ids"] else _path_present(feature["host_path"], devices)


def reconcile_node(name, labels, annotations, devices):
    """Keep scheduler labels in sync with probe detection.

    Auto-managed labels are recorded separately so an administrator's explicit
    true/false choice always wins and the UI can still say "automatic".
    """
    before_labels = dict(labels or {})
    labels = dict(before_labels)
    before_auto = {x for x in str((annotations or {}).get(AUTO_ANNOTATION, "")).split(",") if x}
    auto = set(before_auto)
    defs = features()
    for feature in defs:
        fid, label = feature["id"], feature["label"]
        detected = _detected(feature, devices)
        if fid in auto:
            if detected:
                labels[label] = "true"
            else:
                labels.pop(label, None)
                auto.discard(fid)
        elif label not in labels and detected:
            labels[label] = "true"
            auto.add(fid)
    changed_labels = {f["label"]: labels.get(f["label"])
                      for f in defs if before_labels.get(f["label"]) != labels.get(f["label"])}
    if changed_labels or auto != before_auto:
        value = ",".join(sorted(auto)) or None
        ksend("PATCH", f"/api/v1/nodes/{name}",
              {"metadata": {"labels": changed_labels,
                            "annotations": {AUTO_ANNOTATION: value}}},
              ctype="application/merge-patch+json")
    return labels, auto


def inventory(labels, devices, auto_ids=None):
    """Resolve explicit node labels plus probe detection into feature state."""
    out = []
    auto_ids = set(auto_ids or [])
    for f in features():
        val = labels.get(f["label"])
        explicit = None if f["id"] in auto_ids else True if val == "true" else False if val == "false" else None
        detected = _detected(f, devices)
        available = explicit is True or (explicit is not False and detected)
        out.append(dict(f, explicit=explicit, detected=detected, available=available))
    return out


def workload_features(podspec, annotations=None):
    """Read feature IDs from annotations, node selectors or mounted host paths."""
    defs = features(); found = []
    ann = (annotations or {}).get("harvui.io/hardware", "")
    for fid in [x.strip() for x in ann.split(",") if x.strip()]:
        if fid not in found:
            found.append(fid)
    selectors = podspec.get("nodeSelector", {}) or {}
    paths = [(v.get("hostPath") or {}).get("path", "") for v in podspec.get("volumes", []) or []]
    for f in defs:
        hp = f["host_path"].rstrip("/")
        if selectors.get(f["label"]) == "true" or any(p == hp or p.startswith(hp + "/") for p in paths):
            if f["id"] not in found:
                found.append(f["id"])
    return found


def mount_spec(feature):
    slug = re.sub(r"[^a-z0-9-]", "-", feature["id"].replace("_", "-")).strip("-")
    name = f"hw-{slug[:50]}-{hashlib.sha1(feature['id'].encode()).hexdigest()[:6]}"
    return {
        "id": feature["id"], "label": feature["label"], "name": name,
        "host_path": feature["host_path"], "container_path": feature["container_path"],
        "path_type": feature["path_type"],
    }
