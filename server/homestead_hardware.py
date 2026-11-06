"""Configurable hardware inventory and container passthrough mappings.

iGPU stays built in because it is useful on nearly every homelab. Everything
else is data stored in a ConfigMap: a friendly name, scheduling label, device
path and optional USB VID:PID matches.  That lets the UI support accelerators,
capture cards, serial adapters and future hardware without another code change.
"""
import json
import homestead_cache as CACHE
import homestead_kube as KUBE
import homestead_names as NAMES
import hashlib
import re
import urllib.error
import homestead_routes

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
AUTO_ANNOTATION = NAMES.key("auto-hardware")


def bind(_kget, _ksend, _ns, _cache_ref):
    global kget, ksend, NS, _cache
    kget, ksend, NS, _cache = _kget, _ksend, _ns, _cache_ref
    NAMES.bind(_kget)


def _bust():
    CACHE.drop_where(_cache, lambda key: key.startswith(("hardware", "nodes", "ov", "wl", "flow", "impact:")))


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
        "label": old_label or str(raw.get("label") or f"hardware.{NAMES.DOMAIN}/{fid}"),
        "host_path": host, "container_path": dest, "path_type": typ,
        "usb_ids": usb, "builtin": False,
    }


def _hardware_map():
    return NAMES.object_name("hardware", NS)


def custom_features():
    try:
        cm = kget(f"/api/v1/namespaces/{NS}/configmaps/{_hardware_map()}")
        raw = json.loads(cm.get("data", {}).get("features.json", "[]"))
        if not isinstance(raw, list):
            raise ValueError("features.json is not a list")
        return [_normalise(x) for x in raw]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    except Exception:
        # Custom hardware definitions are an extra: a cluster that cannot be
        # reached, or a ConfigMap that will not parse, leaves the built-in set
        # rather than stopping a deployment being built at all.
        pass
    return [dict(x, builtin=False) for x in DEFAULT_CUSTOM]


def features():
    return [dict(IGPU)] + custom_features()


def feature_map():
    return {x["id"]: x for x in features()}


CHANGED_ELSEWHERE = ("The hardware features were changed elsewhere since this list was opened (another admin "
                     "or tab saved first), so nothing was saved. Open the list again and make your change there.")


def _unreadable(what):
    """The refusal when the stored list cannot be read: never written over."""
    where = f"{NS}/{_hardware_map()}"
    return ValueError(f"The stored hardware features in ConfigMap {where} cannot be read: {what}. Nothing was saved. "
                      f"To go back to the built-in defaults, delete that ConfigMap (kubectl -n {NS} delete configmap "
                      f"{_hardware_map()}); or correct its features.json by hand.")


def _stored(obj):
    """The custom definitions in the hardware ConfigMap as read (None: there
    is none yet, so the built-in defaults the list shows), normalised as the
    list shows them. One Homestead cannot read is refused with the fix."""
    if obj is None:
        return [_normalise(x) for x in DEFAULT_CUSTOM]
    data = obj.get("data") or {}
    if "features.json" not in data:
        raise _unreadable("it has no features.json key")
    try:
        stored = json.loads(data["features.json"])
    except (TypeError, ValueError):
        raise _unreadable("features.json is not valid JSON") from None
    if not isinstance(stored, list):
        raise _unreadable("features.json is not a list")
    try:
        return [_normalise(x) for x in stored]
    except (AttributeError, TypeError, ValueError) as error:
        raise _unreadable(f"an entry is not valid ({str(error)[:120]})") from None


def _same_list(read, stored):
    """Whether the list a form read (normalised) is the one stored now, as the list shows it."""
    return sorted(json.dumps(x, sort_keys=True) for x in read) == sorted(json.dumps(x, sort_keys=True) for x in stored)


def save_features(items, read=None):
    """The custom definitions saved as items. read is the custom list the
    form was built on: when the stored list is no longer that one, the save
    is refused in words rather than undoing another admin's change. The write
    names the resourceVersion it read (KUBE.update_json), so a save landing
    in between is checked again, not written over. A stored list that cannot
    be read is refused with the way to reset it, never written over. A page
    that sends no read (an older one) still saves on what is stored."""
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("features must be a list with at most 32 entries")
    if read is not None and (not isinstance(read, list) or len(read) > 32):
        raise ValueError("read must be the list of features the form was built on, with at most 32 entries")
    if read is not None:
        # A read that is not a list of features is a bad request, not a sign
        # that someone else saved: refused plainly, before anything is read.
        try:
            read = [_normalise(x) for x in read]
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"read must be the list of features the form was built on: {str(error)[:120]}") from None
    seen_map = {}

    def read_map(path):
        # The ConfigMap as update_json reads it, so apply can tell no
        # ConfigMap (the defaults) from one without the key or with broken text.
        seen_map.clear()
        seen_map["obj"] = kget(path)
        return seen_map["obj"]

    def apply(_):
        stored = _stored(seen_map.get("obj"))
        if read is not None and not _same_list(read, stored):
            raise ValueError(CHANGED_ELSEWHERE)
        old = {x["id"]: x for x in stored}
        out, ids = [], set()
        for raw in items:
            f = _normalise(raw, old.get(str(raw.get("id") or "").strip().lower()))
            if f["id"] in ids:
                raise ValueError(f"duplicate feature ID: {f['id']}")
            ids.add(f["id"]); out.append(f)
        return out
    # replace: apply reads the stored text itself (above), so a broken one is
    # refused in words here rather than update_json's own error.
    KUBE.update_json(f"/api/v1/namespaces/{NS}/configmaps/{_hardware_map()}", "features.json", apply,
                     replace=True, read=read_map, write=ksend, indent=2)
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


def reconcile_node(name, labels, annotations, devices, definitions=None):
    """Keep scheduler labels in sync with probe detection.

    Auto-managed labels are recorded separately so an administrator's explicit
    true/false choice always wins and the UI can still say "automatic".
    definitions: features(), when the caller read them once for every node.
    """
    before_labels = dict(labels or {})
    labels = dict(before_labels)
    before_auto = {x for x in NAMES.read(annotations, "auto-hardware").split(",") if x}
    auto = set(before_auto)
    defs = features() if definitions is None else definitions
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
              {"metadata": {"labels": changed_labels, "annotations": {AUTO_ANNOTATION: value}}},
              ctype="application/merge-patch+json")
    return labels, auto


def inventory(labels, devices, auto_ids=None, definitions=None):
    """Resolve explicit node labels plus probe detection into feature state."""
    out = []
    auto_ids = set(auto_ids or [])
    for f in features() if definitions is None else definitions:
        val = labels.get(f["label"])
        explicit = None if f["id"] in auto_ids else True if val == "true" else False if val == "false" else None
        detected = _detected(f, devices)
        available = explicit is True or (explicit is not False and detected)
        out.append(dict(f, explicit=explicit, detected=detected, available=available))
    return out


def workload_features(podspec, annotations=None, definitions=None):
    """Read feature IDs from annotations, node selectors or mounted host paths."""
    defs = features() if definitions is None else definitions
    found = []
    ann = NAMES.read(annotations, "hardware")
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


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/hardware/features"): ("admin", lambda request: save_features(request.body.get("features"),
                                                                       request.body.get("read"))),
    ("GET", "/api/hardware/features"): ("viewer", lambda request: homestead_routes.cached("hardware:features", 15, features)),
}
