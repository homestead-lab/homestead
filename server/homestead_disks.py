"""Every disk on every node, and which of them Longhorn stores data on.

A node has a system disk and often more. Kubernetes reports only the
filesystem its kubelet runs on, so the rest are read from three places: the
node probe (each physical disk, and which filesystems sit on it), Longhorn's
node (the disks it stores replicas on, with their size and use), and - on
Harvester - its BlockDevice list, which names every disk the host has that is
not the system disk and whether it is handed to Longhorn.

Adding a disk to Longhorn is Harvester's to do where Harvester runs: its
BlockDevice is marked for provisioning, and Harvester formats, mounts and
registers it, as its own UI does. Elsewhere Longhorn is told about a folder
where a disk is already mounted, or - for the V2 engine - the raw device.
Taking a disk out goes the other way, and only once nothing is on it:
scheduling stops, replicas are moved off, then the disk is removed.
"""
import json
import posixpath
import re
import threading
import urllib.error

import homestead_names as NAMES
import homestead_operations as OPS
import homestead_routes

kget = ksend = None
temps = lambda: {}
_cache, _lock = {}, threading.Lock()
mutation_scope = None
v2_tasks = lambda: []
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
BD = "/apis/harvesterhci.io/v1beta1/namespaces/longhorn-system/blockdevices"
GiB = 1024 ** 3
# Where a system disk shows itself, whatever the distribution.
SYSTEM_MOUNTS = ("/", "/usr/local", "/var/lib/rancher", "/var/lib/kubelet", "/oem", "/run/initramfs/cos-state")


def bind(_kget, _ksend, _temps, cache=None, lock=None):
    """The cluster and the node probe's readings; for the routes, server.py's
    cache and its lock, which a disk change outdates."""
    global kget, ksend, temps, mutation_scope, v2_tasks, _cache, _lock
    kget, ksend, temps = _kget, _ksend, _temps
    if cache is not None:
        _cache, _lock = cache, lock or _lock
    # A fresh reader binding has its own workflow context. The server installs
    # its shared disk guards after binding; standalone clients supply no store.
    mutation_scope, v2_tasks = None, lambda: []


def _gb(value):
    return round((value or 0) / GiB, 1)


def _blockdevices():
    try:
        return kget(BD).get("items", [])
    except Exception:
        return None              # not Harvester


def longhorn_block_paths(node):
    """The raw devices Longhorn's V2 engine uses on node; None if unreadable.
    No Longhorn, or no such node in it, is no devices."""
    try:
        lh = kget(f"{LH}/nodes/{node}")
    except urllib.error.HTTPError as error:
        return [] if error.code == 404 else None
    except Exception:
        return None
    return [d.get("path") for d in ((lh.get("spec") or {}).get("disks") or {}).values()
            if d.get("diskType") == "block" and d.get("path")]


def _lh_nodes():
    try:
        return {n["metadata"]["name"]: n for n in kget(f"{LH}/nodes").get("items", [])}
    except Exception:
        return {}


def _bd_row(bd):
    spec, status = bd.get("spec") or {}, bd.get("status") or {}
    dev = status.get("deviceStatus") or {}
    details, fs = dev.get("details") or {}, dev.get("fileSystem") or {}
    new_style = "provision" in spec or "provisioner" in spec
    provisioned = bool(spec.get("provision")) if new_style else bool((spec.get("fileSystem") or {}).get("provisioned"))
    return {"name": bd["metadata"]["name"], "node": spec.get("nodeName", ""),
            "path": dev.get("devPath") or spec.get("devPath", ""),
            "size_gb": _gb((dev.get("capacity") or {}).get("sizeBytes")),
            "kind": details.get("deviceType", "disk"), "parent": dev.get("parentDevice", ""),
            "model": " ".join(x for x in (details.get("vendor"), details.get("model")) if x and x != "unknown"),
            "serial": details.get("serialNumber", ""), "fstype": fs.get("type", ""),
            "mountpoint": fs.get("mountPoint", ""), "provisioned": provisioned,
            "phase": status.get("provisionPhase", ""), "state": status.get("state", ""),
            "engine": ((spec.get("provisioner") or {}).get("longhorn") or {}).get("engineVersion", "")}


def _lh_disks(node):
    spec, status = node.get("spec") or {}, node.get("status") or {}
    out = []
    for disk_id, d in (spec.get("disks") or {}).items():
        st = (status.get("diskStatus") or {}).get(disk_id) or {}
        maximum, available = st.get("storageMaximum") or 0, st.get("storageAvailable") or 0
        reserved = d.get("storageReserved") or 0
        ready = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready"), {})
        out.append({"id": disk_id, "path": d.get("path", ""), "type": d.get("diskType", "filesystem"),
                    "scheduling": d.get("allowScheduling", True) is not False,
                    "evicting": bool(d.get("evictionRequested")),
                    "size_gb": _gb(max(0, maximum - reserved)), "used_gb": _gb(maximum - available),
                    "capacity_gb": _gb(maximum), "reserved_gb": _gb(reserved),
                    "allocated_gb": _gb(st.get("storageScheduled")), "free_gb": _gb(available),
                    "replicas": len(st.get("scheduledReplica") or {}),
                    "replica_names": sorted(st.get("scheduledReplica") or {}),
                    "tags": [t for t in d.get("tags") or [] if not str(t).startswith(INTERNAL_TAG)],
                    "ready": ready.get("status", "True") == "True", "problem": ready.get("message", "") if ready.get("status") == "False" else ""})
    return out


def _replica_sizes():
    """{replica: bytes it holds}: its volume's actual size, which every copy of
    a volume holds. None when Longhorn's volumes or replicas cannot be read."""
    try:
        volumes = {v["metadata"]["name"]: int((v.get("status") or {}).get("actualSize") or 0)
                   for v in kget(f"{LH}/volumes").get("items", [])}
        replicas = kget(f"{LH}/replicas").get("items", [])
    except Exception:
        return None
    return {r["metadata"]["name"]: volumes.get((r.get("spec") or {}).get("volumeName"), 0) for r in replicas}


# Harvester marks a disk it is taking out of Longhorn with a tag of its own;
# that is its business, not one of the disk's tags.
INTERNAL_TAG = "harvester-ndm-"
TAG = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")


def clean_tags(tags):
    """Tags as Longhorn takes them: letters, numbers, dots, dashes and
    underscores, each once, in the order given."""
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags)
    out = []
    for tag in tags or []:
        tag = str(tag).strip()
        if not tag:
            continue
        if not TAG.match(tag):
            raise ValueError(f"{tag} is not a tag Longhorn takes: letters, numbers, dots, dashes and underscores")
        if tag not in out:
            out.append(tag)
    return out


def _missing(disk, bd, harvester, dev, system_devs, probed):
    """Why a Longhorn disk's drive is gone, in words, or "".

    Longhorn only says a disk is not ready. Whether the drive itself has gone
    is known elsewhere: Harvester marks the drive's block device inactive when
    it disappears, and a folder whose drive is not mounted - missing, dead, or
    left out at boot - resolves to the system disk it sits on.
    """
    if disk["ready"]:
        return ""
    if bd and str(bd.get("state") or "").lower() == "inactive":
        return f"Harvester no longer finds this drive ({bd['path'] or bd['name']}): it is missing or dead"
    if harvester and not bd and disk["type"] != "block" and disk["path"].startswith("/var/lib/harvester/extra-disks/"):
        return "Harvester no longer lists this drive: it is missing or dead"
    if disk["type"] == "block":
        return f"{disk['path']} is not there: the drive is missing or dead" if probed and not dev else ""
    if probed and dev in system_devs and disk["path"].rstrip("/") not in ("/var/lib/longhorn", "/var/lib/harvester/defaultdisk"):
        return (f"nothing is mounted at {disk['path']}: its drive is missing, dead, or was not mounted "
                "when the host started")
    return ""


def _disk_of(path):
    """/dev/sdb1 -> sdb, /dev/nvme0n1p2 -> nvme0n1."""
    name = posixpath.basename(path or "")
    match = re.fullmatch(r"(nvme\d+n\d+|mmcblk\d+)(p\d+)?", name) or re.fullmatch(r"([a-z]+)\d*", name)
    return match.group(1) if match else name


DISK_NAMES = "disk-names"
NAME_MAX = 40


def _disk_names(node_obj):
    """{serial or device: name} a person gave a node's disks, kept on the node."""
    raw = NAMES.read(((node_obj or {}).get("metadata") or {}).get("annotations") or {}, DISK_NAMES)
    try:
        names = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return names if isinstance(names, dict) else {}


def set_disk_name(node, device, name):
    """Name a disk - "Media 3TB", "System NVMe" - so the node card says which
    one it is. Kept by the drive's serial where it has one, so the name follows
    the drive to another port; else by its device name. An empty name clears it."""
    node, device = str(node or ""), str(device or "").strip()
    name = " ".join(str(name or "").split())[:NAME_MAX]
    row = next((d for d in (inventory()["nodes"].get(node) or []) if d["device"] == device and device), None)
    if not row:
        raise ValueError(f"{node} has no disk {device}")
    key = row.get("serial") or device
    obj = kget(f"/api/v1/nodes/{node}")
    names = _disk_names(obj)
    names.pop(device, None)
    if name:
        names[key] = name
    else:
        names.pop(key, None)
    _patch(f"/api/v1/nodes/{node}", {"metadata": {"annotations": {
        NAMES.key(DISK_NAMES): json.dumps(names, separators=(",", ":")) if names else None}}}, "naming the disk")
    return {"ok": True, "name": name, "detail": f"{device} on {node} is named {name}" if name else f"{device} on {node} has no name now"}


def _mount_point(path, mounts):
    """The mount point a folder is under: the longest that contains it."""
    best = None
    for m in mounts:
        point = m["mountpoint"].rstrip("/") or "/"
        if path == point or path.startswith(point.rstrip("/") + "/") or point == "/":
            if not best or len(point) > len(best):
                best = point
    return best or ""


def _mount_disk(path, mounts):
    """The disk under a folder: the longest mount point that contains it."""
    best = None
    for m in mounts:
        point = m["mountpoint"].rstrip("/") or "/"
        if path == point or path.startswith(point.rstrip("/") + "/") or point == "/":
            if not best or len(point) > len(best["mountpoint"].rstrip("/") or "/"):
                best = m
    return best["disk"] if best else ""


def inventory():
    """{node: [disk rows]}: every physical disk, what it is used for, and the
    Longhorn disks on it."""
    probed, lh, bds = temps() or {}, _lh_nodes(), _blockdevices()
    harvester = bds is not None
    names = sorted(set(probed) | set(lh) | {b["spec"].get("nodeName", "") for b in bds or []} - {""})
    out, sizes = {}, []
    for name in names:
        probe = probed.get(name) or {}
        mounts = probe.get("mounts") or []
        node_bds = [_bd_row(b) for b in bds or [] if (b.get("spec") or {}).get("nodeName") == name]
        rows = {}
        for d in probe.get("disks") or []:
            rows[d["name"]] = {"device": d["name"], "path": f"/dev/{d['name']}", "size_gb": d.get("size_gb", 0),
                               "model": d.get("model", ""), "kind": d.get("kind", ""), "serial": d.get("serial", ""),
                               "longhorn": [], "blockdevice": None, "mounts": []}
        for bd in node_bds:
            if bd["kind"] == "part":
                continue
            dev = _disk_of(bd["path"])
            row = rows.setdefault(dev, {"device": dev, "path": bd["path"], "size_gb": bd["size_gb"], "model": bd["model"],
                                        "kind": "", "serial": bd["serial"], "longhorn": [], "blockdevice": None, "mounts": []})
            row["blockdevice"] = bd
        for m in mounts:
            if m["disk"] in rows:
                rows[m["disk"]]["mounts"].append(m["mountpoint"])
        # A system on LVM mounts / from dm-0, which is no drive of its own; a
        # probe that does not follow it down says so. The drive holding /boot
        # is then the one the system is on, where exactly one does.
        root = next((m for m in mounts if m["mountpoint"] == "/"), None)
        if root and root["disk"] not in rows:
            booting = [dev for dev, row in rows.items() if any(p in ("/boot", "/boot/efi") for p in row["mounts"])]
            if len(booting) == 1:
                rows[booting[0]]["mounts"].append("/")
                mounts = [dict(m, disk=booting[0]) if m["disk"] == root["disk"] else m for m in mounts]
        unplaced = []
        system_devs = {m["disk"] for m in mounts if m["mountpoint"] in SYSTEM_MOUNTS}
        node_point = _mount_point("/var/lib/kubelet", mounts)
        node_mount = next((m for m in mounts if m["mountpoint"] == node_point), {})
        node_key = node_mount.get("device") or node_point
        for disk in _lh_disks(lh.get(name) or {}):
            bd = next((b for b in node_bds if b["name"] == disk["id"] or (b["mountpoint"] and b["mountpoint"] == disk["path"])), None)
            dev = (_disk_of(bd["path"]) if bd else "") or (_disk_of(disk["path"]) if disk["type"] == "block" else _mount_disk(disk["path"], mounts))
            disk["missing"] = _missing(disk, bd, harvester, dev, system_devs, bool(mounts))
            # A folder on / (Longhorn's default /var/lib/longhorn): its data is
            # counted in the root filesystem's use as well.
            point = _mount_point(disk["path"], mounts) if disk["type"] != "block" else ""
            mounted = next((m for m in mounts if m["mountpoint"] == point), {})
            disk["on_root"] = disk["type"] != "block" and (point == "/" or bool(
                mounted.get("device") and root and mounted["device"] == root.get("device")))
            disk["filesystem_key"] = mounted.get("device") or point or disk["path"]
            disk["on_node_fs"] = disk["type"] != "block" and bool(node_key) and disk["filesystem_key"] == node_key
            # A shared host filesystem includes the host's own files; its
            # Longhorn data is what the replicas hold.
            disk["data_gb"] = disk["used_gb"]
            if disk["on_root"] or disk["on_node_fs"]:
                if not sizes:
                    sizes.append(_replica_sizes())
                if sizes[0] is not None:
                    disk["data_gb"] = _gb(sum(sizes[0].get(r, 0) for r in disk["replica_names"]))
            disk["failed"] = not disk["ready"]
            if dev in rows:
                rows[dev]["longhorn"].append(disk)
            else:
                unplaced.append(disk)
        disks = []
        for row in rows.values():
            row["node_fs"] = row["device"] == node_mount.get("disk")
            row["system"] = any(point in SYSTEM_MOUNTS for point in row["mounts"]) or (
                harvester and row["blockdevice"] is None and bool(row["mounts"]))
            bd = row["blockdevice"]
            row["role"] = ("longhorn" if row["longhorn"] else "system" if row["system"]
                           else "provisioning" if bd and bd["provisioned"] else "in use" if row["mounts"] else "unused")
            # Harvester adds whole disks it knows and has not been given; a
            # disk already holding a filesystem or partitions is wiped first.
            row["can_add"] = bool(bd and not bd["provisioned"] and not row["system"] and bd["state"] in ("", "Active"))
            row["needs_wipe"] = bool(bd and (bd["fstype"] or any(b["parent"] == bd["path"] for b in node_bds)))
            disks.append(row)
        if unplaced:
            disks.append({"device": "", "path": "", "size_gb": sum(d["size_gb"] for d in unplaced), "model": "",
                          "kind": "", "serial": "", "longhorn": unplaced, "blockdevice": None, "mounts": [],
                          "system": False, "node_fs": False, "role": "longhorn", "can_add": False, "needs_wipe": False})
        out[name] = sorted(disks, key=lambda r: (not r["system"], r["device"] or "~"))
    for task in v2_tasks():
        rows = out.get(task['node'], [])
        row = next((r for r in rows if r['path'] == task['device'] or any(d['id'] == task['disk'] for d in r['longhorn'])), None)
        if row is not None:
            row['v2_preparation'] = {'id': task['id'], 'phase': task['phase']}
    node_tags = {name: list(((lh.get(name) or {}).get("spec") or {}).get("tags") or []) for name in lh}
    disk_tags = sorted({t for disks in out.values() for d in disks for x in d["longhorn"] for t in x["tags"]})
    return {"harvester": harvester, "nodes": out, "node_tags": node_tags, "disk_tags": disk_tags,
            "all_node_tags": sorted({t for tags in node_tags.values() for t in tags})}


def set_disk_tags(node, disk_id, tags):
    """Tag a Longhorn disk - "ssd", "nvme", "fast" - for storage classes to
    choose by.

    A disk Harvester added is Harvester's: its node-disk-manager writes the
    Longhorn disk from the block device's own tags, and would put back any
    set on Longhorn alone. So the block device is tagged too, where there is
    one, and Longhorn at once so the change shows without waiting for it."""
    tags = clean_tags(tags)
    disk, _ = _lh_disk(node, disk_id)
    kept = [t for t in disk.get("tags") or [] if str(t).startswith(INTERNAL_TAG)]
    bd = next((b for b in _blockdevices() or [] if b["metadata"]["name"] == disk_id), None)
    if bd is not None:
        _patch(f"{BD}/{disk_id}", {"spec": {"tags": tags}}, "tagging the disk in Harvester")
    _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: {"tags": tags + kept}}}}, "tagging the disk")
    return {"ok": True, "tags": tags,
            "detail": f"{disk_id} on {node} " + (f"is tagged {', '.join(tags)}" if tags else "has no tags now")}


def set_node_tags(node, tags):
    """Tag a node, for storage classes that keep their replicas on some."""
    tags = clean_tags(tags)
    try:
        kget(f"{LH}/nodes/{node}")
    except urllib.error.HTTPError:
        raise ValueError(f"Longhorn does not know node {node}")
    _patch(f"{LH}/nodes/{node}", {"spec": {"tags": tags}}, "tagging the node")
    return {"ok": True, "tags": tags,
            "detail": f"{node} " + (f"is tagged {', '.join(tags)}" if tags else "has no tags now")}


def tag_reach(disk_tags=(), node_tags=()):
    """Which nodes could hold a replica for a class that asks for these tags:
    a node with every node tag and a disk, taking replicas, with every disk
    tag."""
    disk_tags, node_tags = set(disk_tags or ()), set(node_tags or ())
    reach = []
    for name, node in _lh_nodes().items():
        spec = node.get("spec") or {}
        if not node_tags <= set(spec.get("tags") or []):
            continue
        if any(disk_tags <= set(d.get("tags") or []) and d.get("allowScheduling", True) is not False
               for d in (spec.get("disks") or {}).values()):
            reach.append(name)
    return sorted(reach)


def _named(inv):
    """Each disk row with the name a person gave it, where they did."""
    try:
        nodes = {n["metadata"]["name"]: n for n in kget("/api/v1/nodes").get("items", [])}
    except Exception:
        nodes = {}
    for node, disks in inv["nodes"].items():
        names = _disk_names(nodes.get(node))
        for d in disks:
            d["name"] = names.get(d.get("serial") or "") or names.get(d["device"]) or ""
    return inv


def _usage_filesystems(disks):
    """Count a filesystem once, even if several Longhorn folders share it."""
    groups = {}
    for disk in disks:
        key = disk["filesystem_key"]
        capacity = disk["capacity_gb"]
        if key not in groups:
            groups[key] = {"capacity_gb": capacity, "used_gb": disk["used_gb"],
                           "available_gb": disk["free_gb"], "reserved_gb": disk["reserved_gb"],
                           "on_root": disk["on_root"], "on_node_fs": disk["on_node_fs"], "data_gb": disk["data_gb"]}
        else:
            row = groups[key]
            row["capacity_gb"] = max(row["capacity_gb"], capacity)
            row["used_gb"] = max(row["used_gb"], disk["used_gb"])
            row["available_gb"] = min(row["available_gb"], disk["free_gb"])
            row["reserved_gb"] = max(row["reserved_gb"], disk["reserved_gb"])
            row["on_root"] = row["on_root"] or disk["on_root"]
            row["on_node_fs"] = row["on_node_fs"] or disk["on_node_fs"]
            row["data_gb"] = (round(row["data_gb"] + disk["data_gb"], 1) if row["on_root"] or row["on_node_fs"]
                              else max(row["data_gb"], disk["data_gb"]))
    return list(groups.values())


def summary():
    """A line per disk for the node cards: its name or device, and - for a
    Longhorn disk on the system drive - that it is both."""
    inv = _named(inventory())
    return {node: [_disk_summary(d) for d in disks] for node, disks in inv["nodes"].items()}


def _disk_summary(d):
    filesystems = _usage_filesystems(d["longhorn"])
    return {"device": d["device"] or "longhorn", "name": d.get("name", ""), "size_gb": d["size_gb"],
            "role": d["role"], "system": bool(d.get("system")), "model": d.get("model", ""),
            "root_fs": "/" in d["mounts"], "node_fs": bool(d.get("node_fs")), "lh_filesystems": filesystems,
            "lh_paths": [x.get("path", "") for x in d["longhorn"]],
            "lh_used_gb": round(sum(x["data_gb"] for x in filesystems), 1),
            "lh_size_gb": round(sum(max(0, x["capacity_gb"] - x["reserved_gb"]) for x in filesystems), 1),
            "lh_root_used_gb": round(sum(x["data_gb"] for x in filesystems if x["on_root"]), 1)}


# ---- changing what Longhorn uses ---------------------------------------------

def _refused(error, what):
    try:
        message = json.loads(error.read().decode("utf-8", "replace")).get("message", "")
    except Exception:
        message = ""
    return ValueError(f"{what} was refused: {message or f'HTTP {error.code}'}")


def _patch(path, body, what):
    try:
        ksend("PATCH", path, body, ctype="application/merge-patch+json")
    except urllib.error.HTTPError as error:
        raise _refused(error, what)


def add(cfg):
    """Harvester: provision a BlockDevice. Elsewhere: a mounted folder, or a
    raw device for the V2 engine, becomes a Longhorn disk."""
    node = str(cfg.get("node") or "")
    engine = "LonghornV2" if str(cfg.get("engine") or "v1").lower() in ("v2", "longhornv2") else "LonghornV1"
    if cfg.get("blockdevice"):
        name = str(cfg["blockdevice"])
        try:
            bd = kget(f"{BD}/{name}")
        except urllib.error.HTTPError:
            raise ValueError(f"block device {name} was not found")
        row = _bd_row(bd)
        if row["provisioned"]:
            raise ValueError(f"{row['path']} is already given to Longhorn")
        wipe = bool(cfg.get("wipe"))
        if not wipe and engine == "LonghornV1" and (row["fstype"] or any(
                _bd_row(b)["parent"] == row["path"] for b in _blockdevices() or [])):
            raise ValueError(f"{row['path']} already holds a filesystem or partitions; tick erase to wipe and use it")
        spec = bd.get("spec") or {}
        if "provision" in spec or "provisioner" in spec:
            body = {"spec": {"provision": True, "provisioner": {"longhorn": {"engineVersion": engine}},
                             "fileSystem": {"forceFormatted": wipe}}}
        else:
            if engine == "LonghornV2":
                raise ValueError("this Harvester release adds disks to the V1 engine only")
            body = {"spec": {"fileSystem": {"provisioned": True, "forceFormatted": wipe}}}
        _patch(f"{BD}/{name}", body, f"adding {row['path']}")
        return {"ok": True, "detail": f"Harvester is {'wiping and ' if wipe else ''}adding {row['path']} on {row['node']} to Longhorn; "
                                      "it appears in Longhorn within a minute or two"}
    path = str(cfg.get("path") or "").strip()
    block = engine == "LonghornV2"
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", path) or ".." in path.split("/"):
        raise ValueError("give the folder the disk is mounted at, like /mnt/disk2" + (", or the device, like /dev/sdb" if block else ""))
    if block and not path.startswith("/dev/"):
        raise ValueError("a V2 disk is a raw device, like /dev/sdb")
    try:
        lh = kget(f"{LH}/nodes/{node}")
    except urllib.error.HTTPError:
        raise ValueError(f"Longhorn does not know node {node}")
    if any(d.get("path") == path for d in ((lh.get("spec") or {}).get("disks") or {}).values()):
        raise ValueError(f"Longhorn already uses {path} on {node}")
    disk_id = "disk-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[:50]
    _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: {
        "path": path, "allowScheduling": True, "diskType": "block" if block else "filesystem",
        "storageReserved": 0, "tags": []}}}}, f"adding {path}")
    return {"ok": True, "detail": f"Longhorn is adding {path} on {node}"}


def _lh_disk(node, disk_id):
    lh = kget(f"{LH}/nodes/{node}")
    disk = ((lh.get("spec") or {}).get("disks") or {}).get(disk_id)
    if disk is None:
        raise ValueError(f"{node} has no Longhorn disk {disk_id}")
    status = ((lh.get("status") or {}).get("diskStatus") or {}).get(disk_id) or {}
    return disk, status


def set_scheduling(node, disk_id, allow):
    _lh_disk(node, disk_id)
    _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: {"allowScheduling": bool(allow)}}}}, "changing the disk")
    return {"ok": True, "detail": f"{disk_id} on {node} {'takes new replicas again' if allow else 'takes no new replicas'}"}


def evict(node, disk_id, on=True):
    """Move every replica off the disk (and stop new ones) - the step before
    removing it. Longhorn rebuilds each elsewhere first."""
    _lh_disk(node, disk_id)
    body = {"evictionRequested": bool(on)}
    if on:
        body["allowScheduling"] = False
    _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: body}}}, "evicting the disk")
    return {"ok": True, "detail": f"moving replicas off {disk_id} on {node}" if on else f"{disk_id} on {node} keeps its replicas"}


def remove(node, disk_id):
    disk, status = _lh_disk(node, disk_id)
    if status.get("scheduledReplica"):
        raise ValueError(f"{disk_id} still holds {len(status['scheduledReplica'])} replica(s); evict it first")
    # The disk's own status can be empty while it is not ready; the replicas
    # themselves say what is on it.
    on_it = [r for r in _replicas(strict=True)
             if _on_disk(r, node, disk, status.get("diskUUID", ""), status.get("scheduledReplica") or {})]
    if on_it:
        raise ValueError(f"{disk_id} still holds {len(on_it)} replica(s); evict it first")
    if disk.get("allowScheduling", True) is not False:
        raise ValueError("stop scheduling on the disk before removing it")
    bds = _blockdevices()
    bd = next((b for b in bds or [] if b["metadata"]["name"] == disk_id
               or ((b.get("status") or {}).get("deviceStatus") or {}).get("fileSystem", {}).get("mountPoint") == disk.get("path")), None)
    if bd:
        spec = bd.get("spec") or {}
        body = ({"spec": {"provision": False}} if "provision" in spec or "provisioner" in spec
                else {"spec": {"fileSystem": {"provisioned": False}}})
        _patch(f"{BD}/{bd['metadata']['name']}", body, "removing the disk")
        return {"ok": True, "detail": f"Harvester is releasing {disk.get('path')} from Longhorn; its data stays on the disk"}
    _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: None}}}, "removing the disk")
    return {"ok": True, "detail": f"Longhorn no longer uses {disk.get('path')} on {node}; its files are left where they are"}


# ---- a failed disk, replaced -------------------------------------------------
# Longhorn keeps a dead disk's entry, and the replicas it held, until told
# otherwise - so the disk cannot simply be removed, and a volume missing a
# copy may have nowhere to rebuild it: every other node already holds one.
# Retiring the disk deletes its failed replicas where a healthy copy exists
# elsewhere, then takes the disk out, leaving the node ready for a new drive.
# A volume whose only copy was on it is never touched unless asked: a drive
# that is merely unplugged comes back with its data.

def _replicas(strict=False):
    """Longhorn's replicas. strict: anything that decides what to delete must
    know them all - a failed read is not "no replicas"."""
    try:
        return kget(f"{LH}/replicas").get("items", [])
    except Exception:
        if strict:
            raise
        return []


def _copy_elsewhere(replica, replicas, node, disk, uuid, scheduled):
    """A healthy replica of the same volume that is not on this disk."""
    name = (replica.get("spec") or {}).get("volumeName", "")
    return any((r.get("spec") or {}).get("volumeName") == name and r["metadata"]["name"] != replica["metadata"]["name"]
               and _healthy(r) and not _on_disk(r, node, disk, uuid, scheduled) for r in replicas)


def _on_disk(replica, node, disk, uuid, scheduled):
    spec = replica.get("spec") or {}
    if replica["metadata"]["name"] in scheduled:
        return True
    if spec.get("nodeID") != node:
        return False
    return bool(uuid and spec.get("diskID") == uuid) or spec.get("diskPath") == disk.get("path")


def _healthy(replica):
    spec, status = replica.get("spec") or {}, replica.get("status") or {}
    return bool(spec.get("healthyAt")) and not spec.get("failedAt") and status.get("currentState") != "error"


def retire_plan(node, disk_id):
    """What replacing a failed disk does to each volume that had a copy on it."""
    disk, status = _lh_disk(node, disk_id)
    uuid = status.get("diskUUID", "")
    scheduled = status.get("scheduledReplica") or {}
    ready = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {})
    if ready.get("status", "True") == "True":
        raise ValueError(f"{disk.get('path')} on {node} is working: move its replicas off and remove it instead")
    replicas = _replicas(strict=True)
    mine = [r for r in replicas if _on_disk(r, node, disk, uuid, scheduled)]
    try:
        volumes = {v["metadata"]["name"]: v for v in kget(f"{LH}/volumes").get("items", [])}
    except Exception:
        volumes = {}
    schedulable = {n for n, obj in _lh_nodes().items()
                   if (obj.get("spec") or {}).get("allowScheduling", True) is not False}
    rows = []
    for replica in mine:
        name = (replica.get("spec") or {}).get("volumeName", "")
        others = [r for r in replicas if (r.get("spec") or {}).get("volumeName") == name
                  and r["metadata"]["name"] != replica["metadata"]["name"] and _healthy(r)
                  and not _on_disk(r, node, disk, uuid, scheduled)]
        holders = {(r.get("spec") or {}).get("nodeID") for r in others}
        volume = volumes.get(name) or {}
        k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
        free_nodes = sorted(schedulable - holders - {node})
        if not others:
            outcome, why = "only-copy", "its only copy was on this disk: reconnect the drive, or restore it from a backup"
        elif free_nodes:
            outcome, why = "elsewhere", f"rebuilds on {free_nodes[0]} from its healthy copy"
        else:
            outcome, why = "waits", f"every other node already has a copy: it rebuilds on {node} once a new disk is added there"
        claim = "/".join(x for x in (k8s.get("namespace"), k8s.get("pvcName")) if x) or name
        rows.append({"replica": replica["metadata"]["name"], "volume": name, "claim": claim,
                     "copies": int(((volume.get("spec") or {}).get("numberOfReplicas")) or 1),
                     "healthy_elsewhere": len(others), "outcome": outcome, "why": why})
    order = {"only-copy": 0, "waits": 1, "elsewhere": 2}
    rows.sort(key=lambda r: (order[r["outcome"]], r["claim"]))
    bd = next((b for b in _blockdevices() or [] if b["metadata"]["name"] == disk_id), None)
    return {"node": node, "disk": disk_id, "path": disk.get("path", ""),
            "problem": ready.get("message", "") or ready.get("reason", ""),
            "harvester_device": _bd_row(bd) if bd else None,
            "volumes": rows, "only_copies": sum(r["outcome"] == "only-copy" for r in rows)}


def retire_start(cfg, ops):
    node, disk_id = str(cfg.get("node") or ""), str(cfg.get("disk") or "")
    review = retire_plan(node, disk_id)
    force = bool(cfg.get("force"))
    if force and cfg.get("confirm") != disk_id:
        raise ValueError(f"type {disk_id} to confirm")
    keep = [r["replica"] for r in review["volumes"] if r["outcome"] == "only-copy" and not force]
    disk, _ = _lh_disk(node, disk_id)
    # How it was, so cancelling before anything is removed puts it back so.
    ref = {"node": node, "disk": disk_id, "path": review["path"], "force": force, "phase": "stop", "keep": keep,
           "was_scheduling": disk.get("allowScheduling", True) is not False}
    return ops.start("disk-retire", f"Replace failed disk {review['path'] or disk_id} on {node}",
                     {"kind": "Node", "name": node}, f"/nodes/{node}", ref, "Stopping new replicas on the disk")


def retire_resumable(item):
    return "it finished" if (item.get("ref") or {}).get("phase") == "done" else ""


def _bd_provisioned(bd):
    spec = bd.get("spec") or {}
    if "provision" in spec or "provisioner" in spec:
        return bool(spec.get("provision"))
    return bool((spec.get("fileSystem") or {}).get("provisioned"))


def retire_step(item):
    """One step of retiring a failed disk. Each is safe to run again."""
    ref = item["ref"]
    node, disk_id = ref["node"], ref["disk"]
    phase = ref.get("phase", "stop")
    lh = kget(f"{LH}/nodes/{node}")
    disk = ((lh.get("spec") or {}).get("disks") or {}).get(disk_id)
    status = ((lh.get("status") or {}).get("diskStatus") or {}).get(disk_id) or {}
    if disk is None and phase not in ("cleanup", "done"):
        ref["phase"] = phase = "cleanup"
    if phase == "stop":
        _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: {"allowScheduling": False,
                                                                     "evictionRequested": False}}}},
               "stopping new replicas")
        ref["phase"] = "replicas"
        return "running", 10, "No new replicas go to the disk"
    if phase == "replicas":
        uuid = status.get("diskUUID", "")
        scheduled = status.get("scheduledReplica") or {}
        keep = set(ref.get("keep") or [])
        replicas = _replicas(strict=True)
        left = [r for r in replicas if _on_disk(r, node, disk, uuid, scheduled)
                and r["metadata"]["name"] not in keep]
        # Checked again at each step, not only at review: a volume whose other
        # copies failed since then has its last one here, and it stays.
        if not ref.get("force"):
            last = [r for r in left if not _copy_elsewhere(r, replicas, node, disk, uuid, scheduled)]
            if last:
                keep |= {r["metadata"]["name"] for r in last}
                ref["keep"] = sorted(keep)
                left = [r for r in left if r["metadata"]["name"] not in keep]
        for replica in left:
            try:
                ksend("DELETE", f"{LH}/replicas/{replica['metadata']['name']}")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        ref["removed"] = sorted(set(ref.get("removed") or []) | {r["metadata"]["name"] for r in left})
        if left:
            return "running", 30, f"Letting go of {len(left)} failed replica{'' if len(left) == 1 else 's'}"
        if keep:
            ref["phase"] = "done"
            return "succeeded", 100, (
                "Failed replicas removed, so their volumes rebuild from their healthy copies. The disk itself is "
                f"kept: {len(keep)} volume{'' if len(keep) == 1 else 's'} had the only copy on it. Reconnect the "
                "drive to get them back; if it is dead, restore them from a backup and replace the disk again.")
        if [name for name in scheduled if name not in keep]:
            return "running", 45, f"Waiting for Longhorn to let go of {len(scheduled)} replica record(s)"
        ref["phase"] = "remove"
        return "running", 55, "Nothing is left on the disk"
    if phase == "remove":
        bd = next((b for b in _blockdevices() or [] if b["metadata"]["name"] == disk_id), None)
        if bd and _bd_provisioned(bd):
            spec = bd.get("spec") or {}
            body = ({"spec": {"provision": False}} if "provision" in spec or "provisioner" in spec
                    else {"spec": {"fileSystem": {"provisioned": False}}})
            _patch(f"{BD}/{disk_id}", body, "releasing the drive")
        # Harvester takes a disk out of Longhorn itself when its drive is
        # there to unmount; a missing one it may not, so this does.
        try:
            ksend("PATCH", f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: None}}},
                  ctype="application/merge-patch+json")
        except urllib.error.HTTPError as error:
            return "running", 70, f"Longhorn is not letting the disk go yet - {_refused(error, 'removing it')}"
        ref["phase"] = "cleanup"
        return "running", 80, "Taking the disk out of Longhorn"
    if phase == "cleanup":
        bd = next((b for b in _blockdevices() or [] if b["metadata"]["name"] == disk_id), None)
        # The dead drive's record, so a replacement is the only disk listed.
        if bd and str((bd.get("status") or {}).get("state") or "").lower() == "inactive" and not _bd_provisioned(bd):
            try:
                ksend("DELETE", f"{BD}/{disk_id}")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        ref["phase"] = "done"
        return "succeeded", 100, (
            f"{ref.get('path') or disk_id} is out of Longhorn on {node}. Add the new drive there (Nodes > Disks > "
            "Add to Longhorn); volumes that had nowhere else to go rebuild onto it, and the rest are rebuilding "
            "on other nodes now - Volumes shows each one's progress.")
    return item.get("status", "succeeded"), item.get("progress", 100), item.get("message", "")


def alert_facts(inv):
    """A Longhorn disk that has failed, or whose drive has gone."""
    facts = []
    for node, disks in (inv.get("nodes") or {}).items():
        for row in disks:
            for disk in row["longhorn"]:
                if disk.get("ready"):
                    continue
                facts.append({"key": f"disks:{node}:{disk['id']}", "category": "outage", "severity": "critical",
                              "title": f"Longhorn disk is not ready on {node}",
                              "resolved": f"Longhorn disk warning cleared: {node} / {disk['path']}",
                              "body": (disk.get("missing") or disk.get("problem") or "Longhorn reports it not ready")
                                      + f" ({disk['path']}). Check volume replicas; workload availability depends on healthy copies.",
                              "href": f"/nodes/{node}"})
    return facts


# ---- a new disk set up, and every disk tagged -------------------------------
setup_module = None     # homestead_disk_setup
autotag_state = None    # () -> path of the auto-tag record


def kind_tags(row):
    """What a disk is, as tags: "hdd", "ssd", or "nvme" and "ssd" (a class
    asking for SSDs takes NVMe too), with "os" as well on the one the system
    runs from. The system disk was once "os" alone, to keep SSD classes off
    it - but a machine whose only drive is the system one then had nowhere
    for them at all; "os" still lets a class leave it out. [] - or "os"
    alone - when the probe has not said what it is."""
    kind = str(row.get("kind") or "").upper()
    by_kind = ["hdd"] if kind == "HDD" else ["nvme", "ssd"] if kind == "NVME" else ["ssd"] if kind == "SSD" else []
    return (["os"] if row.get("system") else []) + by_kind


def _earlier_tags(row):
    """What an earlier release tagged a disk: "os" alone on the system disk,
    and NVMe as plain "ssd"."""
    if row.get("system"):
        return ["os"]
    kind = str(row.get("kind") or "").upper()
    return ["hdd"] if kind == "HDD" else ["ssd"] if kind in ("SSD", "NVME") else []


def _system_row(node):
    return next((r for r in (inventory()["nodes"].get(node) or []) if r.get("system")), {"system": True})


def _row(node, device):
    name = device.rsplit("/", 1)[-1]
    return next((r for r in (inventory()["nodes"].get(node) or []) if r["device"] == name), {})


def inspect_disk(node, device):
    return setup_module.inspect(node, device)


def set_up(cfg):
    """A disk made ready and given to Longhorn in one step: V1 formatted (or
    kept, with its Longhorn data) and mounted by setup_module, V2 by its raw
    device. A failed, empty Longhorn entry at the same place is cleared first,
    so the new disk is not held back by the old one."""
    node, device = str(cfg.get("node") or ""), str(cfg.get("device") or "")
    engine = "v2" if str(cfg.get("engine") or "v1").lower() in ("v2", "longhornv2") else "v1"
    mode, confirm = str(cfg.get("mode") or ""), str(cfg.get("confirm") or "")
    row = _row(node, device)
    if engine == "v2":
        facts = setup_module.inspect(node, device)
        if facts["state"] in ("missing", "system", "mounted", "held", "longhorn-v2"):
            raise ValueError(setup_module.refusal(facts, "erase"))
        # The V2 engine opens the raw device, which a multipath map holds as
        # surely as wipefs finds it busy: let go of it either way.
        release = setup_module.release_script(facts.get("multipath") or ()) if facts.get("multipath") else ""
        if facts["state"] != "blank":
            if confirm.strip() != device:
                raise ValueError(f"type {device} to confirm: it holds {facts['fstype'] or 'partitions or LVM'}, which Longhorn's V2 engine erases")
            facts = setup_module.wipe(node, device, facts)
        elif release:
            out, err = setup_module.hostrun.run(node, f"D={device}\n{release}echo WIPED", timeout=60)
            if "WIPED" not in out:
                raise ValueError(f"could not release {device}: {(err or out)[:200]}")
        path, kept = facts["by_id"] or device, False
    else:
        done = setup_module.setup(node, device, mode, str(cfg.get("fstype") or "ext4"), confirm)
        path, kept = done["path"], done["kept_data"]
    tags = kind_tags(row)
    added = _give_longhorn(node, path, engine, tags, device)
    if added:
        return added
    words = (f"{device} kept its Longhorn data and is mounted at {path}" if kept
             else f"{device} is {'given to the V2 engine' if engine == 'v2' else f'formatted and mounted at {path}'}")
    return {"ok": True, "path": path, "tags": tags,
            "detail": f"{words}; Longhorn is adding it on {node}" + (f", tagged {', '.join(tags)}" if tags else "")}


def os_space(node):
    return setup_module.os_space(str(node or ""))


def use_os_space(cfg):
    """Space nothing uses given to Longhorn: a logical volume of its own in
    the OS drive's volume group, or a new partition in a disk's unallocated
    space - formatted and mounted for V1, raw for V2. Tagged os on the
    system's drive (and its kind), like its default disk; by kind elsewhere."""
    node = str(cfg.get("node") or "")
    engine = "v2" if str(cfg.get("engine") or "v1").lower() in ("v2", "longhornv2") else "v1"
    source = str(cfg.get("source") or "lvm")
    if source == "lvm":
        done, tags = setup_module.use_os_space(node, cfg.get("size_gb"), engine), kind_tags(_system_row(node))
        where = f"{done['size_gb']} GB volume in {done['vg']}"
    else:
        kind, _, rest = source.partition(":")
        disk, _, start = rest.partition(":")
        if kind != "part" or not start.isdigit():
            raise ValueError("choose where the space comes from")
        done = setup_module.use_region(node, disk, int(start), cfg.get("size_gb"), engine, str(cfg.get("confirm") or ""))
        tags = kind_tags(_row(node, f"/dev/{disk}"))
        where = f"{done['size_gb']} GB partition {done['device']}"
    path = done["path"]
    added = _give_longhorn(node, path, engine, tags, done["device"])
    if added:
        return added
    how = "given to the V2 engine" if engine == "v2" else f"mounted at {path}"
    return {"ok": True, "path": path, "tags": tags,
            "detail": f"A {where} is {how}; Longhorn is adding it on {node}" + (f", tagged {', '.join(tags)}" if tags else "")}


def _give_longhorn(node, path, engine, tags, device):
    """Add the folder (or V2 device) to Longhorn on node. Returns a result
    when Longhorn already uses it, else None once it has been added."""
    lh = kget(f"{LH}/nodes/{node}")
    for disk_id, d in ((lh.get("spec") or {}).get("disks") or {}).items():
        if d.get("path") != path:
            continue
        status = ((lh.get("status") or {}).get("diskStatus") or {}).get(disk_id) or {}
        ready = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {}).get("status") == "True"
        if ready:
            return {"ok": True, "path": path, "detail": f"{device} is mounted at {path}, which Longhorn already uses on {node}"}
        if status.get("scheduledReplica"):
            raise ValueError(f"Longhorn's disk at {path} on {node} still lists replicas; replace it from its row first")
        # The old entry for this folder, from before anything was mounted there.
        _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: {"allowScheduling": False}}}}, "clearing the old entry")
        _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: None}}}, "clearing the old entry")
    disk_id = "disk-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[:50]
    _patch(f"{LH}/nodes/{node}", {"spec": {"disks": {disk_id: {
        "path": path, "allowScheduling": True, "diskType": "block" if engine == "v2" else "filesystem",
        "storageReserved": 0, "tags": tags}}}}, f"adding {path}")
    _note_tagged(f"{node}/{disk_id}")
    return None


def _tagged():
    import json as _json
    try:
        with open(autotag_state(), encoding="utf-8") as handle:
            return set(_json.load(handle))
    except (OSError, ValueError, TypeError):
        return set()


def _note_tagged(key):
    import homestead_shared as SHARED
    try:
        SHARED.write_json(autotag_state(), sorted(_tagged() | {key}))
    except (OSError, TypeError):
        pass


def auto_tag():
    """Each Longhorn disk with no tags, seen for the first time: tagged by
    what it is. Once only - tags someone removes or changes are theirs.
    A disk still carrying exactly what an earlier release gave it ("os" on
    the system disk, "ssd" on NVMe) is brought up to today's tags, once.
    Returns [(node, disk id, tags)]."""
    done, tagged = _tagged(), []
    for node, rows in inventory()["nodes"].items():
        for row in rows:
            tags = kind_tags(row)
            for disk in row["longhorn"]:
                key = f"{node}/{disk['id']}"
                if key in done:
                    again = "kinds:" + key
                    if again in done or not disk["ready"] or sorted(tags) == sorted(_earlier_tags(row)):
                        continue
                    if sorted(disk["tags"]) == sorted(_earlier_tags(row)):
                        set_disk_tags(node, disk["id"], tags)
                        tagged.append((node, disk["id"], tags))
                    _note_tagged(again)         # changed, or theirs: either way once
                    done.add(again)
                    continue
                if disk["tags"]:
                    pass                        # someone tagged it: theirs
                elif tags and tags != ["os"] and disk["ready"]:
                    set_disk_tags(node, disk["id"], tags)
                    tagged.append((node, disk["id"], tags))
                else:
                    continue                    # not known yet: looked at again later
                _note_tagged(key)
                _note_tagged("kinds:" + key)
                done |= {key, "kinds:" + key}
    return tagged


def _forget(*keys):
    for key in keys:
        _cache.pop(key, None)


def _forget_storage(result):
    # Disks, the storage picture and Longhorn's capacity all change together.
    with _lock:
        for key in [k for k in _cache if k.startswith(("disk", "stor", "lhcap"))]:
            _cache.pop(key, None)
    return result


def _retire(body):
    op = retire_start(body, OPS)
    _forget("disks", "lhcap", "nodes", "ov")
    return {"ok": True, "operation": op}


def _longhorn_disk(action, body):
    """Add a disk to Longhorn, or allow, evict or remove one it has."""
    result = (add(body) if action == "add"
              else set_scheduling(body.get("node", ""), body.get("disk", ""), body.get("allow", True)) if action == "scheduling"
              else evict(body.get("node", ""), body.get("disk", ""), body.get("on", True)) if action == "evict"
              else remove(body.get("node", ""), body.get("disk", "")))
    _forget("disks", "lhcap", "nodes", "ov")
    return result


def _renamed(result):
    _forget("disks")
    return result


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/disks/retire"): ("admin", lambda request: _retire(request.body)),
    ("POST", "/api/disks/os-space/use"): ("admin", lambda request: _forget_storage(use_os_space(request.body))),
    ("POST", "/api/disks/setup"): ("admin", lambda request: _forget_storage(set_up(request.body))),
    ("POST", "/api/disks/add"): ("admin", lambda request: _longhorn_disk("add", request.body)),
    ("POST", "/api/disks/scheduling"): ("admin", lambda request: _longhorn_disk("scheduling", request.body)),
    ("POST", "/api/disks/evict"): ("admin", lambda request: _longhorn_disk("evict", request.body)),
    ("POST", "/api/disks/remove"): ("admin", lambda request: _longhorn_disk("remove", request.body)),
    ("POST", "/api/disks/name"): ("admin", lambda request: _renamed(set_disk_name(
        request.body.get("node", ""), request.body.get("device", ""), request.body.get("name", "")))),
    ("POST", "/api/disks/tags"): ("admin", lambda request: _renamed(set_disk_tags(
        request.body.get("node", ""), request.body.get("disk", ""), request.body.get("tags") or []))),
    ("POST", "/api/disks/node-tags"): ("admin", lambda request: _renamed(set_node_tags(
        request.body.get("node", ""), request.body.get("tags") or []))),
    ("POST", "/api/disks/retire/plan"): ("admin", lambda request: retire_plan(request.body.get("node", ""), request.body.get("disk", ""))),
    ("POST", "/api/disks/os-space"): ("admin", lambda request: os_space(str(request.body.get("node") or ""))),
    ("POST", "/api/disks/inspect"): ("admin", lambda request: inspect_disk(str(request.body.get("node") or ""), str(request.body.get("device") or ""))),
    ("GET", "/api/disks"): ("viewer", lambda request: homestead_routes.cached("disks", 10, inventory)),
}
