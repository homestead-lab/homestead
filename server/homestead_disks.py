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
import urllib.error

kget = ksend = None
temps = lambda: {}
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
BD = "/apis/harvesterhci.io/v1beta1/namespaces/longhorn-system/blockdevices"
GiB = 1024 ** 3
# Where a system disk shows itself, whatever the distribution.
SYSTEM_MOUNTS = ("/", "/usr/local", "/var/lib/rancher", "/var/lib/kubelet", "/oem", "/run/initramfs/cos-state")


def bind(_kget, _ksend, _temps):
    global kget, ksend, temps
    kget, ksend, temps = _kget, _ksend, _temps


def _gb(value):
    return round((value or 0) / GiB, 1)


def _blockdevices():
    try:
        return kget(BD).get("items", [])
    except Exception:
        return None              # not Harvester


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
        ready = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready"), {})
        out.append({"id": disk_id, "path": d.get("path", ""), "type": d.get("diskType", "filesystem"),
                    "scheduling": d.get("allowScheduling", True) is not False,
                    "evicting": bool(d.get("evictionRequested")),
                    "size_gb": _gb(maximum - (d.get("storageReserved") or 0)), "used_gb": _gb(maximum - available),
                    "allocated_gb": _gb(st.get("storageScheduled")), "free_gb": _gb(available),
                    "replicas": len(st.get("scheduledReplica") or {}),
                    "ready": ready.get("status", "True") == "True", "problem": ready.get("message", "") if ready.get("status") == "False" else ""})
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
    out = {}
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
        unplaced = []
        system_devs = {m["disk"] for m in mounts if m["mountpoint"] in SYSTEM_MOUNTS}
        for disk in _lh_disks(lh.get(name) or {}):
            bd = next((b for b in node_bds if b["name"] == disk["id"] or (b["mountpoint"] and b["mountpoint"] == disk["path"])), None)
            dev = (_disk_of(bd["path"]) if bd else "") or (_disk_of(disk["path"]) if disk["type"] == "block" else _mount_disk(disk["path"], mounts))
            disk["missing"] = _missing(disk, bd, harvester, dev, system_devs, bool(mounts))
            disk["failed"] = not disk["ready"]
            if dev in rows:
                rows[dev]["longhorn"].append(disk)
            else:
                unplaced.append(disk)
        disks = []
        for row in rows.values():
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
                          "system": False, "role": "longhorn", "can_add": False, "needs_wipe": False})
        out[name] = sorted(disks, key=lambda r: (not r["system"], r["device"] or "~"))
    return {"harvester": harvester, "nodes": out}


def summary():
    """A line per disk for the node cards."""
    inv = inventory()
    return {node: [{"device": d["device"] or "longhorn", "size_gb": d["size_gb"], "role": d["role"],
                    "lh_used_gb": round(sum(x["used_gb"] for x in d["longhorn"]), 1),
                    "lh_size_gb": round(sum(x["size_gb"] for x in d["longhorn"]), 1)} for d in disks]
            for node, disks in inv["nodes"].items()}


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

def _replicas():
    try:
        return kget(f"{LH}/replicas").get("items", [])
    except Exception:
        return []


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
    replicas = _replicas()
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
    ref = {"node": node, "disk": disk_id, "path": review["path"], "force": force, "phase": "stop", "keep": keep}
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
        left = [r for r in _replicas() if _on_disk(r, node, disk, uuid, scheduled)
                and r["metadata"]["name"] not in keep]
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
                              "title": f"A Longhorn disk on {node} has failed",
                              "resolved": f"The Longhorn disk {disk['path']} on {node} is working again",
                              "body": (disk.get("missing") or disk.get("problem") or "Longhorn reports it not ready")
                                      + f" ({disk['path']}). Its volumes run on their other copies meanwhile.",
                              "href": f"/nodes/{node}"})
    return facts
