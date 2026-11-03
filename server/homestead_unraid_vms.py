"""VMs on an Unraid server, and bringing one across to KubeVirt.

Unraid runs its VMs with libvirt, and `virsh dumpxml` says everything about
one: its CPUs and memory, its firmware, its vdisk files, the bridge and MAC
of its network card, and what is passed through to it. listing() reads that
over the import source's pinned SSH - the same login the container import
uses - and plan() maps it onto what a Homestead VM can be, saying what cannot
come across and why.

Importing copies each vdisk into a CDI DataVolume through CDI's upload proxy:
a Job reads the file over SSH and streams it into an authenticated upload, and
CDI turns raw or qcow2 into a VM disk as it arrives. An upload token lasts a
few minutes, so every disk has its own Job, all started together. Once every
disk has arrived the VM is made, stopped, for its owner to start.
Before any disk is made, storage_room() checks the chosen storage class can
still place them all, so an import doesn't run out of room partway through.

A Proxmox VE host comes across the same way: homestead_proxmox_vms reads
and maps its VMs (qm config) into the same plan, and the copy, the job and
the page below are shared, with only the stopped check and shut down its own.

Nothing on Unraid is changed, but for one thing its owner asks for: a VM still
running there can be shut down (virsh shutdown - its power button), because a
disk copied while its VM writes to it may not boot.
"""
import base64
import json
import math
import re
import shlex
from datetime import datetime
import urllib.error
import xml.etree.ElementTree as ET

import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_cdi_cleanup as CDI_CLEANUP
import homestead_proxmox_vms as PROXMOX

IMP = kget = ksend = OPS = None
platform = lambda: {}
KIND = "unraid-vm-import"
TASK = "vm-import"
IMAGE = "alpine:3.24"
UPLOAD_API = "/apis/upload.cdi.kubevirt.io/v1beta1"
CDI_API = "/apis/cdi.kubevirt.io/v1beta1"
# What Unraid's templates name a card, and what KubeVirt calls it.
NIC_MODELS = {"virtio": "virtio", "virtio-net": "virtio", "e1000": "e1000", "e1000e": "e1000e",
              "rtl8139": "rtl8139", "vmxnet3": "e1000e"}
DISK_BUSES = {"virtio": "virtio", "sata": "sata", "scsi": "scsi", "ide": "sata", "usb": "sata"}
# A VM's name on Unraid is free text; it has to be one line of something sane.
UNRAID_NAME = re.compile(r"[^\x00-\x1f/]{1,128}")


# What the room check reads: the cluster's storage classes, Longhorn's
# capacity (homestead_lhcapacity.capacity) and the class a VM disk lands on
# when none is chosen. Unbound, the room is unknown and said to be.
storage_classes = lh_capacity = None
default_class = lambda: ""


# What bind() puts back for an argument it is not given: the module's own default,
# not the last bind's (a test's fake must not outlive the test).
_UNBOUND = {"platform": platform, "storage_classes": storage_classes, "lh_capacity": lh_capacity, "default_class": default_class}


def bind(imports, _kget, _ksend, ops, _platform=None, storage=None):
    global IMP, kget, ksend, OPS, platform, storage_classes, lh_capacity, default_class
    IMP, kget, ksend, OPS = imports, _kget, _ksend, ops
    platform = _platform if _platform else _UNBOUND["platform"]
    if storage:
        storage_classes, lh_capacity, default_class = storage
    else:
        storage_classes, lh_capacity, default_class = (
            _UNBOUND[n] for n in ("storage_classes", "lh_capacity", "default_class"))


# ------------------------------------------------------------ cancelling
# An import can stop where it cannot go on by itself - paused because a
# claim it made was replaced, or its cleanup stopped by the same check - and
# its job then never ends. Cancel removes what it made, and only that: each
# object must carry the stamp its receipt was written with. Deleting a
# DataVolume takes its claim, CDI's staging claim and upload pod with it.
def cancel_plan(item):
    ref = item.get("ref") or {}
    work = ref.get("cdi_cleanup") or {}
    if not work.get("id"):
        return {"can": False, "why_not": "This older import kept no record of what it made: review its volumes under Volumes"}
    where = _where(ref)
    disks = ", ".join(d["dv"] for d in ref.get("disks") or [])
    return {"mode": "stop", "action": "Stop the import and remove its disks", "severity": "medium", "needs": "operator",
            "undo": [f"Stops the copy from {where}", f"Removes the disks it was making ({disks}) and their temporary storage"],
            "keeps": [f"The VM on {where} is unchanged",
                      "A volume on a class that keeps released data (Retain) stays under Volumes until you remove it"]}


def cancel_run(item, _options):
    ref = item["ref"]
    ns, stamp = ref["namespace"], ref["cdi_cleanup"]["id"]
    paths = []
    for disk in ref.get("disks") or []:
        if disk.get("job"):
            paths += [f"/apis/batch/v1/namespaces/{ns}/jobs/{disk['job']}",
                      f"/api/v1/namespaces/{ns}/secrets/{disk['job']}-upload"]
        paths.append(f"{CDI_API}/namespaces/{ns}/datavolumes/{disk['dv']}")
    removed, foreign = 0, []
    for path in paths:
        found = CDI_CLEANUP.RESOURCES.optional(kget, path)
        if not found:
            continue
        if ((found.get("metadata") or {}).get("annotations") or {}).get(CDI_CLEANUP.STAMP) != stamp:
            foreign.append(path.rsplit("/", 1)[-1])
            continue
        CDI_CLEANUP.RESOURCES.delete(kget, ksend, path, found)
        removed += 1
    ref.update(phase="failed", retain_resources=False)
    note = (f"; {', '.join(foreign)} belong to another import and were left" if foreign else "")
    return f"Stopped; removed {removed} of the import's objects and their temporary storage{note}. The VM on {_where(ref)} is unchanged"


# ------------------------------------------------------------ reading Unraid

# One line per VM (name, state, XML, each base64 so nothing in them can break
# the listing), then one per file disk with what qemu-img says of it.
LIST_SCRIPT = r"""
command -v virsh >/dev/null 2>&1 || { echo NOVIRSH; command -v qm >/dev/null 2>&1 && echo HASQM; exit 0; }
virsh list --all --name 2>/dev/null | while IFS= read -r vm; do
  [ -n "$vm" ] || continue
  state=$(virsh domstate --domain "$vm" 2>/dev/null | head -n 1)
  xml=$(virsh dumpxml --domain "$vm" 2>/dev/null) || continue
  printf 'VM %s %s %s\n' "$(printf %s "$vm" | base64 | tr -d '\n')" "$(printf %s "$state" | base64 | tr -d '\n')" "$(printf %s "$xml" | base64 | tr -d '\n')"
  virsh domblklist --details --domain "$vm" 2>/dev/null | awk '$1 == "file" && $2 == "disk" { $1 = $2 = $3 = ""; sub(/^ +/, ""); print }' | while IFS= read -r disk; do
    [ -f "$disk" ] || continue
    info=$(qemu-img info -U --output=json -- "$disk" 2>/dev/null | tr -d '\n') || info=""
    size=$(stat -c %s -- "$disk" 2>/dev/null || echo 0)
    used=$(du -k -- "$disk" 2>/dev/null | cut -f 1)
    printf 'DISK %s %s %s %s\n' "$(printf %s "$disk" | base64 | tr -d '\n')" "${size:-0}" "$(( ${used:-0} * 1024 ))" "$(printf %s "$info" | base64 | tr -d '\n')"
  done
done
echo END
"""


def _b64(value):
    try:
        return base64.b64decode(value or "", validate=True).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def parse_listing(lines):
    """[(name, state, xml)] and {path: facts} from what LIST_SCRIPT printed."""
    domains, disks, complete, virsh, qm = [], {}, False, True, False
    for line in lines:
        parts = line.split(" ")
        if parts[0] == "NOVIRSH":
            virsh = False
        elif parts[0] == "HASQM":
            # A Proxmox host added as Unraid: the page says to add it as Proxmox.
            qm = True
        elif parts[0] == "END":
            complete = True
        elif parts[0] == "VM" and len(parts) == 4:
            domains.append((_b64(parts[1]), _b64(parts[2]).strip(), _b64(parts[3])))
        elif parts[0] == "DISK" and len(parts) == 5:
            info = {}
            try:
                info = json.loads(_b64(parts[4]) or "{}")
            except ValueError:
                pass
            size = int(parts[2]) if parts[2].isdigit() else 0
            disks[_b64(parts[1])] = {
                "format": str(info.get("format") or ""),
                "virtual": int(info.get("virtual-size") or size or 0),
                "size": size, "used": int(parts[3]) if parts[3].isdigit() else 0}
    return {"virsh": virsh, "qm": qm, "complete": complete, "domains": domains, "disks": disks}


def _kib(element, default=0):
    if element is None or not (element.text or "").strip().isdigit():
        return default
    value = int(element.text.strip())
    unit = (element.get("unit") or "KiB").lower()
    factor = {"b": 1 / 1024, "bytes": 1 / 1024, "k": 1, "kib": 1, "kb": 1000 / 1024, "m": 1024, "mib": 1024,
              "mb": 1000 ** 2 / 1024, "g": 1024 ** 2, "gib": 1024 ** 2, "gb": 1000 ** 3 / 1024}.get(unit, 1)
    return int(value * factor)


def _template(root):
    """Unraid's own note on the VM: <vmtemplate xmlns="unraid" os="windows11" .../>."""
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag.endswith("}vmtemplate") or element.tag == "vmtemplate":
            return element
    return ET.Element("vmtemplate")


def parse_domain(text):
    """What libvirt's XML says about one VM, in plain fields."""
    root = ET.fromstring(text)
    os_el = root.find("os")
    os_type = os_el.find("type") if os_el is not None else None
    loader = os_el.find("loader") if os_el is not None else None
    loader_path = (loader.text or "").strip() if loader is not None else ""
    uefi = bool(loader is not None and ("ovmf" in loader_path.lower() or loader.get("type") == "pflash")) or (
        os_el is not None and os_el.get("firmware") == "efi")
    devices = root.find("devices")
    devices = devices if devices is not None else ET.Element("devices")
    disks, cdroms, other_disks = [], [], []
    for disk in devices.findall("disk"):
        device, kind = disk.get("device", "disk"), disk.get("type", "")
        source, target, driver = disk.find("source"), disk.find("target"), disk.find("driver")
        path = (source.get("file") or source.get("dev") or "") if source is not None else ""
        row = {"path": path, "target": target.get("dev", "") if target is not None else "",
               "bus": target.get("bus", "") if target is not None else "",
               "format": driver.get("type", "") if driver is not None else "",
               "boot": int((disk.find("boot").get("order") or 0)) if disk.find("boot") is not None and
               str(disk.find("boot").get("order") or "").isdigit() else 0}
        if device == "cdrom":
            cdroms.append(row)
        elif device == "disk" and kind == "file" and path:
            disks.append(row)
        elif device == "disk":
            other_disks.append(dict(row, type=kind))
    nics = []
    for nic in devices.findall("interface"):
        mac, source, model = nic.find("mac"), nic.find("source"), nic.find("model")
        nics.append({"type": nic.get("type", ""), "mac": (mac.get("address", "") if mac is not None else "").lower(),
                     "bridge": (source.get("bridge") or source.get("network") or source.get("dev") or "") if source is not None else "",
                     "model": model.get("type", "") if model is not None else ""})
    hostdevs = []
    for dev in devices.findall("hostdev"):
        kind, source = dev.get("type", ""), dev.find("source")
        if kind == "pci" and source is not None and source.find("address") is not None:
            a = source.find("address")
            hostdevs.append({"type": "pci", "id": "{}:{}:{}.{}".format(*(str(a.get(k, "0")).replace("0x", "")
                                                                         for k in ("domain", "bus", "slot", "function")))})
        elif kind == "usb" and source is not None:
            vendor, product = source.find("vendor"), source.find("product")
            hostdevs.append({"type": "usb", "id": ":".join(x.get("id", "").replace("0x", "")
                                                            for x in (vendor, product) if x is not None)})
        else:
            hostdevs.append({"type": kind or "device", "id": ""})
    vcpu = root.find("vcpu")
    features = root.find("features")
    cpu = root.find("cpu")
    return {
        "name": (root.findtext("name") or "").strip(),
        "uuid": (root.findtext("uuid") or "").strip(),
        "description": (root.findtext("description") or "").strip()[:200],
        "os": _template(root).get("os", ""),
        "vcpus": int(vcpu.text) if vcpu is not None and (vcpu.text or "").strip().isdigit() else 1,
        "memory_kib": _kib(root.find("currentMemory"), _kib(root.find("memory"), 1024 ** 2)),
        "machine": os_type.get("machine", "") if os_type is not None else "",
        "uefi": uefi, "secure_boot": loader is not None and (loader.get("secure") == "yes" or "secboot" in loader_path.lower()),
        "tpm": devices.find("tpm") is not None,
        "hyperv": features is not None and features.find("hyperv") is not None,
        "cpu_mode": cpu.get("mode", "") if cpu is not None else "",
        "pinned": root.find("cputune/vcpupin") is not None,
        "disks": sorted(disks, key=lambda d: (d["boot"] or 99, d["target"])),
        "other_disks": other_disks, "cdroms": cdroms, "nics": nics, "hostdevs": hostdevs,
        "graphics": [g.get("type", "") for g in devices.findall("graphics")],
        "sound": devices.find("sound") is not None,
    }


def slug(name):
    """A Homestead name from an Unraid one: "Windows 11" -> "windows-11"."""
    value = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40].strip("-")
    return value or "imported-vm"


# The disk image is written into a filesystem on the volume, and the
# filesystem takes room of its own: a volume exactly the disk's size is
# refused by CDI ("virtual image size ... is larger than the reported
# available storage"). So a volume is the disk plus 6%, and at least 256 MiB
# more, in MiB.
FS_OVERHEAD_PCT = 6
FS_MINIMUM = 256 * 1024 ** 2


def request_mib(disk):
    virtual = int(disk.get("virtual") or 0) or int(disk["size_gb"]) * 1024 ** 3
    total = virtual + max(-(-virtual * FS_OVERHEAD_PCT // 100), FS_MINIMUM)
    return -(-total // 1024 ** 2)


def volume_request(disk):
    return f"{request_mib(disk)}Mi"


# ------------------------------------------------------------ room for the disks

# Under this share of what the class can still place left over afterwards,
# the import goes ahead with a warning: the next volume, or a rebuild, may not fit.
ROOM_TIGHT_PCT = 10
LONGHORN = "driver.longhorn.io"


def _subset(wanted, have):
    return set(wanted or []) <= set(have or [])


def _most_placeable(rooms, copies):
    """The most, in GiB, that volumes of `copies` copies can still take in
    all, no two copies of one volume on the same node: the largest T with
    sum(min(room, T)) >= copies x T over the nodes' rooms."""
    if copies < 1 or len(rooms) < copies:
        return 0.0
    low, high = 0.0, sum(rooms) / copies
    for _ in range(60):
        mid = (low + high) / 2
        if sum(min(r, mid) for r in rooms) >= copies * mid:
            low = mid
        else:
            high = mid
    return low


def _place(sizes, nodes, copies):
    """Place each volume, biggest first, a copy on each of the `copies` nodes
    whose roomiest disk fits it. nodes: a list of disk rooms (GiB) per node.
    False when one won't go: each copy needs its whole size on one disk."""
    nodes = [sorted(disks, reverse=True) for disks in nodes]
    for size in sorted(sizes, reverse=True):
        fits = sorted((i for i, disks in enumerate(nodes) if disks and disks[0] >= size),
                      key=lambda i: -nodes[i][0])[:copies]
        if len(fits) < copies:
            return False
        for i in fits:
            nodes[i][0] -= size
            nodes[i].sort(reverse=True)
    return True


def _gib(value):
    value = round(value, 1)
    return f"{int(value)}" if value == int(value) else f"{value}"


def storage_room(disks, storage_class=""):
    """Whether the chosen class can still place the disks an import makes,
    checked before anything is created. A Longhorn disk takes a replica while
    what is allocated on it stays under (size - reserved) x over-provisioning %
    and its real free space above the minimal-available share - room_gb and
    physical_room_gb in homestead_lhcapacity.capacity - and each of a volume's
    copies goes on a different node. Too little is a blocker; less than
    ROOM_TIGHT_PCT left afterwards is a warning."""
    sizes = [request_mib(d) / 1024 for d in disks]
    needed = sum(sizes)
    try:
        name = str(storage_class or "").strip() or default_class() or ""
    except Exception:
        # The default class could not be read: the room is unknown, said as such.
        name = ""
    out = {"storage_class": name, "needed_gb": round(needed, 1), "free_gb": None, "copies": None,
           "level": "unknown", "blockers": [], "warnings": []}

    def unknown(why):
        return {**out, "warnings": [f"{why}, so Homestead cannot check it has room for {_gib(needed)} GiB; "
                                    "make sure it does before importing"]}
    try:
        rows = storage_classes() if storage_classes else None
    except Exception:
        rows = None
    if rows is None or lh_capacity is None:
        return unknown(f"The free space on {name or 'the default storage class'} could not be read")
    row = next((r for r in rows if r.get("name") == name), None)
    if row is None:
        return unknown(f"There is no storage class {name}" if name else "No default storage class is set")
    if row.get("provisioner") != LONGHORN:
        return unknown(f"{name} is not Longhorn")
    try:
        copies = max(1, int(str(row.get("replicas") or "3").strip()))
    except ValueError:
        copies = 3
    try:
        cap = lh_capacity()
    except Exception:
        return unknown("Longhorn's free space could not be read")
    nodes = []
    for node in cap.get("nodes") or []:
        if not _subset(row.get("node_tags"), node.get("tags")):
            continue
        rooms = [min(d.get("room_gb") or 0, d.get("physical_room_gb") or 0) for d in node.get("disks") or []
                 if not d.get("blocked") and _subset(row.get("disk_tags"), d.get("tags"))]
        nodes.append([r for r in rooms if r > 0])
    free = _most_placeable([sum(n) for n in nodes], copies)
    out.update(free_gb=round(free, 1), copies=copies)
    with_copies = f" ({_gib(needed * copies)} GiB with its {copies} copies)" if copies > 1 else ""
    usable = sum(1 for n in nodes if n)
    if usable < copies:
        out["blockers"].append(f"Not enough room on {name}: it keeps {copies} copies of a disk, each on a different "
                               f"node, and {usable} node{'' if usable == 1 else 's'} {'has' if usable == 1 else 'have'} "
                               f"room for one. These disks need {_gib(needed)} GiB{with_copies}; free some space "
                               "or add a disk first")
    elif needed > free + 0.05:
        out["blockers"].append(f"Not enough room on {name}: these disks need {_gib(needed)} GiB{with_copies}, and "
                               f"it can still place {_gib(free)} GiB. Free some space, add a disk, or leave a disk out")
    elif not _place(sizes, nodes, copies):
        biggest = sorted((max(n) for n in nodes if n), reverse=True)[copies - 1]
        out["blockers"].append(f"Not enough room on {name} in one place: a {_gib(max(sizes))} GiB disk needs that "
                               f"much free on one Longhorn disk on each of {copies} node{'' if copies == 1 else 's'}, "
                               f"and {_gib(biggest)} GiB is the most there is. Free some space or add a bigger disk")
    elif free - needed < free * ROOM_TIGHT_PCT / 100:
        left = max(0.0, free - needed)
        out["warnings"].append(f"Room is tight on {name}: these disks take {_gib(needed)} of the {_gib(free)} GiB "
                               f"it can still place, leaving {_gib(left)} GiB ({round(left / free * 100) if free else 0}%). "
                               "New volumes and replica rebuilds may not fit afterwards")
    out["level"] = "bad" if out["blockers"] else "warn" if out["warnings"] else "ok"
    return out


def _chosen_disks(found, skip_disks):
    skip = {int(i) for i in skip_disks or [] if str(i).isdigit()}
    return [d for d in found["disks"] if d["index"] == 0 or d["index"] not in skip]


def room(body):
    """The import dialog's room check, on the class chosen, for the disks it
    would make, from the listing the dialog already has. Starting the import
    reads the disks again and checks again before anything is created."""
    disks = []
    for d in body.get("disks") or []:
        try:
            virtual, size_gb = int(d.get("virtual") or 0), int(d.get("size_gb") or 0)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("disk sizes are whole numbers")
        if virtual < 0 or size_gb < 0 or not (virtual or size_gb):
            raise ValueError("every disk needs a size")
        disks.append({"virtual": virtual, "size_gb": size_gb})
    if not disks or len(disks) > 64:
        raise ValueError("which disks?")
    return storage_room(disks, body.get("storage_class"))


def plan(dom, facts):
    """How one Unraid VM maps to a Homestead VM, and what stays behind."""
    gib = 1024 ** 3
    disks, dropped = [], []
    for index, disk in enumerate(dom["disks"]):
        seen = facts.get(disk["path"]) or {}
        virtual = int(seen.get("virtual") or 0)
        disks.append({"index": index, "path": disk["path"], "target": disk["target"],
                      "unraid_bus": disk["bus"], "bus": DISK_BUSES.get(disk["bus"], "virtio"),
                      "format": seen.get("format") or disk["format"] or "raw",
                      "virtual": virtual, "size": int(seen.get("size") or 0), "used": int(seen.get("used") or 0),
                      "size_gb": max(1, math.ceil(virtual / gib)) if virtual else 0,
                      "found": bool(seen)})
    for disk in dom["other_disks"]:
        dropped.append({"what": "Disk " + (disk["target"] or ""), "detail": disk["path"],
                        "reason": "a physical disk passed through, not a file: attach its data another way"})
    for cd in dom["cdroms"]:
        if cd["path"]:
            dropped.append({"what": "CD-ROM", "detail": cd["path"].rsplit("/", 1)[-1],
                            "reason": "an ISO is only needed to install; add one in Edit VM if you still need it"})
    for dev in dom["hostdevs"]:
        dropped.append({"what": "GPU or PCI device" if dev["type"] == "pci" else "USB device" if dev["type"] == "usb" else "Device",
                        "detail": dev["id"], "hardware": True,
                        "reason": "passthrough is tied to Unraid's hardware; add it from Homestead's hardware devices in Edit VM"})
    for extra in dom["nics"][1:]:
        dropped.append({"what": "Second network card", "detail": extra["bridge"] + " " + extra["mac"],
                        "reason": "a Homestead VM starts with one network card"})
    if dom["pinned"]:
        dropped.append({"what": "CPU pinning", "detail": "vcpupin",
                        "reason": "Unraid's own tuning for its cores; the core count comes across"})
    machine = dom["machine"]
    notes = []
    if dom["tpm"]:
        notes.append("its TPM comes across as a new one - Unraid keeps the old one's contents - so BitLocker, "
                     "if it is on, asks once for its recovery key")
    if machine and "q35" not in machine:
        notes.append(f"{machine} becomes q35, the only machine type KubeVirt runs; Linux doesn't mind, "
                     "and Windows finds its devices again on first boot")
    nic = dom["nics"][0] if dom["nics"] else {}
    unraid_state = dom.get("state", "")
    return {
        "id": dom["name"], "name": dom["name"], "slug": slug(dom["name"]), "state": unraid_state,
        "shut_off": unraid_state == "shut off", "os": dom["os"],
        "cores": max(1, min(128, dom["vcpus"])),
        "memory": f"{dom['memory_kib'] // 1024 ** 2}Gi" if dom["memory_kib"] >= 1024 ** 2 and not dom["memory_kib"] % 1024 ** 2
        else f"{max(64, dom['memory_kib'] // 1024)}Mi",
        "firmware": "uefi" if dom["uefi"] else "bios", "secure_boot": bool(dom["secure_boot"]),
        "tpm": bool(dom["tpm"]), "hyperv": bool(dom["hyperv"]),
        "cpu_model": "host-passthrough" if dom["cpu_mode"] == "host-passthrough" else "",
        "machine": machine,
        "disks": disks, "nic": {"mac": nic.get("mac", ""), "bridge": nic.get("bridge", ""),
                                "model": nic.get("model", ""), "nic_model": NIC_MODELS.get(nic.get("model", ""), "virtio")},
        "dropped": dropped, "notes": notes,
        "ready": bool(disks) and all(d["found"] and d["virtual"] for d in disks),
        "problem": ("" if disks else "it has no vdisk file to copy") or next(
            (f"{d['path']} could not be read on Unraid" for d in disks if not d["found"] or not d["virtual"]), ""),
    }


def _source(name):
    src = IMP._source(name)
    if src.get("kind") not in ("unraid", "ssh", "proxmox"):
        raise ValueError("VMs are imported from an Unraid server or a Proxmox host")
    return src


KIND_NAMES = {"unraid": "Unraid", "ssh": "Unraid", "proxmox": "Proxmox"}


def _where(ref):
    """What the import came from, in words: "Proxmox" or "Unraid"."""
    return KIND_NAMES.get(ref.get("kind", "unraid"), "the source")


def listing(source):
    """Every VM on one Unraid server or Proxmox host, mapped."""
    src = _source(source)
    if src.get("kind") == "proxmox":
        lines = IMP.run_probe(f"vms-{source}", IMP._ssh_script(src, PROXMOX.LIST_SCRIPT), src, timeout=120)
        found = PROXMOX.listing(lines)
        # "virsh" is what the page asks: whether the host runs VMs at all.
        return {"source": source, "kind": "proxmox", "virsh": found["hypervisor"], "vms": found["vms"]}
    lines = IMP.run_probe(f"vms-{source}", IMP._ssh_script(src, LIST_SCRIPT), src, timeout=120)
    found = parse_listing(lines)
    if not found["virsh"]:
        return {"source": source, "kind": src.get("kind"), "virsh": False, "proxmox": found["qm"], "vms": []}
    if not found["complete"]:
        raise ValueError("Unraid's VM list was cut short; try again")
    vms = []
    for name, state, xml in found["domains"]:
        try:
            dom = parse_domain(xml)
        except ET.ParseError:
            continue
        dom["state"] = state
        vms.append(plan(dom, found["disks"]))
    return {"source": source, "kind": src.get("kind"), "virsh": True, "vms": sorted(vms, key=lambda v: v["name"].lower())}


def _one(source, vm):
    """One VM by its id: its name on Unraid, its VMID on Proxmox."""
    vm = str(vm or "")
    proxmox = _source(source).get("kind") == "proxmox"
    if not (vm.isdigit() if proxmox else UNRAID_NAME.fullmatch(vm) and not vm.startswith("-")):
        raise ValueError("which VM?")
    found = next((v for v in listing(source)["vms"] if v["id"] == vm), None)
    if not found:
        raise ValueError(f"{source} has no VM {vm}" if proxmox else f"{source} has no VM named {vm}")
    return found


def shutdown(source, vm):
    """Ask Unraid to shut a VM down cleanly - its power button, not a pull of the plug."""
    found = _one(source, vm)
    if found["shut_off"]:
        return {"ok": True, "message": f"{vm} is already shut off"}
    src = _source(source)
    if src.get("kind") == "proxmox":
        IMP.run_probe(f"vm-off-{source}", IMP._ssh_script(src, PROXMOX.shutdown_command(found["vmid"])), src, timeout=70)
        return {"ok": True, "message": f"Asked {source} to shut {found['name']} down; it shows as stopped once the guest has stopped"}
    IMP.run_probe(f"vm-off-{source}", IMP._ssh_script(src, "virsh shutdown --domain " + shlex.quote(vm)), src, timeout=60)
    return {"ok": True, "message": f"Asked {source} to shut {vm} down; it shows as shut off once the guest has stopped"}


# ------------------------------------------------------------ importing

def _upload_url():
    """Where CDI's upload proxy answers inside the cluster."""
    try:
        rows = kget("/api/v1/services?fieldSelector=metadata.name%3Dcdi-uploadproxy").get("items") or []
    except Exception:
        rows = []
    namespace = (rows[0].get("metadata") or {}).get("namespace", "cdi") if rows else ""
    if not namespace:
        raise ValueError("CDI's upload proxy was not found; the containerized data importer (CDI) is needed to import VM disks")
    return f"https://cdi-uploadproxy.{namespace}.svc/v1beta1/upload", namespace


def disk_names(name, count):
    return [f"{name}-disk" if i == 0 else f"{name}-disk-{i + 1}" for i in range(count)]


def start(body, actor=""):
    """Check, make the empty disks, and start the job that fills them."""
    source = str(body.get("source") or "")
    kind = _source(source).get("kind", "unraid")
    found = _one(source, body.get("vm"))
    if not found["shut_off"]:
        raise ValueError(f"{found['name']} is {found['state'] or 'running'} on {source}: shut it down first, "
                         "so its disk is copied as it was left")
    if not found["ready"]:
        raise ValueError(f"{found['name']} cannot be imported: {found['problem']}")
    name = IMP._required_name(body.get("name") or found["slug"])
    ns = IMP._required_name(body.get("namespace") or IMP.NS, "namespace")
    if IMP._get_or_none(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}") is not None:
        raise ValueError(f"a VM named {name} already exists in {ns}")
    disks = _chosen_disks(found, body.get("skip_disks"))
    names = disk_names(name, len(disks))
    for dv in names:
        plan_ = IMP.vm_disk_import_plan(ns, dv)
        if not plan_["ready"]:
            raise ValueError(plan_["message"])
    cores = int(body.get("cores") or found["cores"])
    memory = str(body.get("memory") or found["memory"])
    if not 1 <= cores <= 128:
        raise ValueError("between 1 and 128 cores")
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:Mi|Gi)", memory):
        raise ValueError("memory must be like 8Gi or 512Mi")
    storage_class = str(body.get("storage_class") or "").strip()
    # Checked before any disk is made, not partway through a copy.
    space = storage_room(disks, storage_class)
    if space["blockers"]:
        raise ValueError(space["blockers"][0])
    network = str(body.get("network") or "pod").strip()
    if network != "pod":
        # Checked now, not after an hour of copying.
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9.-]+", network) or IMP._get_or_none(
                "/apis/k8s.cni.cncf.io/v1/namespaces/{}/network-attachment-definitions/{}".format(*network.split("/", 1))) is None:
            raise ValueError(f"there is no LAN network {network}")
    mac = found["nic"]["mac"] if body.get("keep_mac", True) and found["nic"]["mac"] else ""
    url, _ = _upload_url()
    hardware = {"firmware": found["firmware"], "machine": "q35", "hyperv": found["hyperv"]}
    if found["firmware"] == "uefi":
        hardware["secure_boot"] = found["secure_boot"]
    if found["tpm"]:
        # A new TPM, as the note says; "on" works on every KubeVirt, where a
        # persistent one needs the cluster set up for VM state.
        hardware["tpm"] = "on"
    vm_cfg = {"name": name, "namespace": ns, "cores": cores, "memory": memory, "start": False,
              "disk_import": names[0], "disk_bus": disks[0]["bus"],
              "extra_disks": [{"name": dv, "bus": d["bus"]} for dv, d in zip(names[1:], disks[1:])],
              "network": network, "nic_model": str(body.get("nic_model") or found["nic"]["nic_model"]),
              "hardware": hardware, "labels": {NAMES.key("imported-from"): slug(source)[:63]}}
    if mac:
        vm_cfg["mac"] = mac
    if found["cpu_model"]:
        vm_cfg["cpu_model"] = found["cpu_model"]
    bodies = []
    for dv, disk in zip(names, disks):
        # Filesystem mode avoids raw block-device ownership requirements.
        storage = {"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                   "resources": {"requests": {"storage": volume_request(disk)}}}
        if storage_class:
            storage["storageClassName"] = storage_class
        bodies.append({
            "apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
            "metadata": {"name": dv, "namespace": ns,
                         "labels": {NAMES.key("managed"): "true", IMP.VM_DISK_LABEL: "true"},
                         "annotations": {"homestead.io/import-source": "proxmox" if kind == "proxmox" else "unraid",
                                         "homestead.io/disk-format": disk["format"]}},
            "spec": {"source": {"upload": {}}, "contentType": "kubevirt", "storage": storage}})
    # vm is what the copy checks is still off: Unraid's name, Proxmox's VMID.
    ref = {"source": source, "kind": kind, "vm": found["id"], "vm_name": found["name"], "namespace": ns, "name": name, "url": url,
           "disks": [{"dv": dv, "path": d["path"], "bytes": d["size"], "job": ""} for dv, d in zip(names, disks)],
           "vm_cfg": vm_cfg, "by": actor}
    return CDI_CLEANUP.dispatch(kget, ksend, OPS, KIND, f"Import {found['name']} from {source}",
        {"kind": "VirtualMachine", "name": name, "namespace": ns}, "/vms/import", ref, bodies, "disks")


def copy_script(src, vm, path, size):
    """Read one disk over SSH into the upload, the VM checked still off first.
    vm is Unraid's VM name or Proxmox's VMID; path is a file, or on Proxmox a
    block device (an LVM-thin or ZFS volume), which reads the same way."""
    ssh = lambda remote: IMP._ssh_script(src, remote)
    if src.get("kind") == "proxmox":
        check = [f"state=$({ssh(PROXMOX.status_command(vm))})",
                 "[ \"$state\" = 'stopped' ] || { echo \"HSVM-FAILED VM $vm is ${state:-unknown} on Proxmox again; its disk was not copied\"; exit 5; }"]
    else:
        check = [f"state=$({ssh('virsh domstate --domain ' + shlex.quote(vm))} | head -n 1)",
                 "[ \"$state\" = 'shut off' ] || { echo \"HSVM-FAILED $vm is $state on Unraid again; its disk was not copied\"; exit 5; }"]
    return "\n".join([
        "set -eu", "set -o pipefail",
        # The name is Unraid's free text: a shell variable, never part of a command.
        f"vm={shlex.quote(str(vm))}",
        "apk add --no-cache openssh-client sshpass curl pv >/dev/null 2>&1 || apk add --no-cache openssh-client sshpass curl >/dev/null",
        IMP.SOURCE_SSH.setup(src).rstrip("\n"),
        *check,
        "meter() { if command -v pv >/dev/null 2>&1; then pv -n -t -b -i 10 -s " + str(int(size)) + "; else cat; fi; }",
        # Compressed on the wire: a raw vdisk is mostly empty space, which
        # crosses as almost nothing; pv counts the disk's own bytes after SSH
        # has unpacked them, so progress is unchanged.
        f"if {IMP.SOURCE_SSH.command(src, 'cat -- ' + shlex.quote(path), compress=True)} | meter | curl -sS --fail-with-body --cacert /ca/ca.crt -X POST -T - "
        "-H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/octet-stream' \"$UPLOAD_URL\"",
        "then echo HSVM-DONE",
        "else code=$?; echo \"HSVM-FAILED Disk stream or CDI upload exited with status $code; see the disk copy output\"; exit \"$code\"; fi",
    ])


def _job(ref, disk, index, token, ca, item=None):
    src = _source(ref["source"])
    work = ref.get("cdi_cleanup")
    suffix = f"-{work['id'][:8]}-{index + 1}" if work else f"-{index + 1}"
    job = f"homestead-vmimport-{ref['name']}"[:63 - len(suffix)].rstrip("-") + suffix
    secret = job + "-upload"
    ns = ref["namespace"]
    stamp = {CDI_CLEANUP.STAMP: work["id"]} if work else {}
    disk["job"] = job
    aux = [{"path": f"/api/v1/namespaces/{ns}/secrets/{secret}"},
           {"path": f"/apis/batch/v1/namespaces/{ns}/jobs/{job}"}]
    if work:
        work.setdefault("aux", []).extend(aux)
        OPS.checkpoint(item)
    made_secret = ksend("POST", f"/api/v1/namespaces/{ns}/secrets", {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": secret, "namespace": ns, "labels": NAMES.labels(TASK), "annotations": stamp},
        "stringData": {"token": token, "ca.crt": ca}})
    body = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": ns, "labels": NAMES.labels(TASK), "annotations": stamp},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 3600, "template": {
            "metadata": {"labels": NAMES.labels(TASK)},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "containers": [{
                         "name": "copy", "image": IMAGE,
                         "command": ["sh", "-c", copy_script(src, ref["vm"], disk["path"], disk["bytes"])],
                         "env": [{"name": "SSHPASS", "valueFrom": {"secretKeyRef": {"name": IMP.source_secret(src["name"], src), "key": "password"}}},
                                 {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": secret, "key": "token"}}},
                                 {"name": "UPLOAD_URL", "value": ref["url"]}],
                         "volumeMounts": [{"name": "ca", "mountPath": "/ca", "readOnly": True}],
                         "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}}}],
                     "volumes": [{"name": "ca", "secret": {"secretName": secret, "items": [{"key": "ca.crt", "path": "ca.crt"}]}}]}}}}
    made = ksend("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", body)
    disk["job_uid"] = (made or {}).get("metadata", {}).get("uid", "")
    aux[0]["uid"] = (made_secret or {}).get("metadata", {}).get("uid", "")
    aux[1]["uid"] = disk["job_uid"]
    if work:
        OPS.checkpoint(item)
    return job, secret


def _token(ns, dv):
    answer = ksend("POST", f"{UPLOAD_API}/namespaces/{ns}/uploadtokenrequests", {
        "apiVersion": "upload.cdi.kubevirt.io/v1beta1", "kind": "UploadTokenRequest",
        "metadata": {"name": dv, "namespace": ns}, "spec": {"pvcName": dv}})
    token = ((answer or {}).get("status") or {}).get("token", "")
    if not token:
        raise ValueError("CDI gave no upload token")
    return token


def _ca(proxy_ns):
    """CDI's upload proxy signs with its own CA, which it publishes."""
    for name in ("cdi-uploadproxy-signer-bundle", "cdi-uploadproxy-server-cert"):
        found = IMP._get_or_none(f"/api/v1/namespaces/{proxy_ns}/configmaps/{name}")
        bundle = ((found or {}).get("data") or {}).get("ca-bundle.crt", "")
        if bundle:
            return bundle
    raise ValueError("CDI's upload proxy CA was not found, so the copy could not check who it talks to")


def _job_logs(ns, job):
    try:
        pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{job}").get("items") or []
        if not pods:
            return ""
        from homestead_shim import raw_get
        return raw_get(f"/api/v1/namespaces/{ns}/pods/{pods[0]['metadata']['name']}/log?tailLines=200") or ""
    except Exception:
        return ""


def _copy_sample(line, size):
    """Read old percentage-only pv output and elapsed/byte samples, with optional pod timestamps."""
    text, timestamp, clock = str(line).strip(), None, ""
    stamped = re.match(r"^(\d{4}-\d\d-\d\dT\S+)\s+(.*)$", text)
    if stamped:
        try:
            timestamp = datetime.fromisoformat(stamped[1].replace("Z", "+00:00")).timestamp()
            clock = stamped[1][11:19]
        except ValueError:
            pass
        text = stamped[2].strip()
    timed = re.fullmatch(r"(\d+(?:\.\d+)?)\s+(\d+)", text)
    if timed and size > 0:
        elapsed, copied = float(timed[1]), min(size, int(timed[2]))
        return {"percent": min(100, copied / size * 100), "bytes": copied, "elapsed": elapsed,
                "time": timestamp if timestamp is not None else elapsed, "clock": clock, "coarse": False}
    if text.isdigit() and 0 <= int(text) <= 100:
        percent = int(text)
        return {"percent": percent, "bytes": size * percent / 100 if size > 0 else None,
                "elapsed": None, "time": timestamp, "clock": clock, "coarse": True}
    return None


def progress(log, size=0):
    samples = [_copy_sample(line, size) for line in str(log or "").splitlines()]
    return next((sample["percent"] for sample in reversed(samples) if sample), 0)


def copy_output(source, disk):
    """Readable progress lines and a live summary; transport errors remain in the output."""
    size = max(0, int(disk.get("bytes") or 0))
    samples, lines = [], []
    for line in str(source.get("text") or "").splitlines():
        sample = _copy_sample(line, size)
        if not sample:
            lines.append(line)
            continue
        samples.append(sample)
        rate = None
        if not sample["coarse"] and sample["time"] is not None and sample["bytes"] is not None:
            recent = [old for old in samples[:-1] if not old["coarse"] and old["time"] is not None and old["bytes"] is not None
                      and 0 < sample["time"] - old["time"] <= 60]
            if recent:
                old = recent[0]
                rate = max(0, (sample["bytes"] - old["bytes"]) / (sample["time"] - old["time"]))
            elif sample["elapsed"] and sample["elapsed"] > 0:
                rate = sample["bytes"] / sample["elapsed"]
        eta = max(0, math.ceil((size - sample["bytes"]) / rate)) if rate and size else None
        sample.update(total_bytes=size or None, bytes_per_second=rate, eta_seconds=eta)
        approximate = "~" if sample["coarse"] and sample["percent"] < 100 else ""
        parts = [f"{approximate}{sample['percent']:.1f}%"]
        if size:
            parts.append(f"{approximate}{sample['bytes'] / 1024 ** 3:.1f} / {size / 1024 ** 3:.1f} GiB")
        if rate is not None:
            parts.append(f"{rate / 1024 ** 2:.1f} MiB/s")
        if sample["percent"] >= 100:
            parts.append("Stream transferred; waiting for CDI to finish")
        elif eta is not None:
            parts.append(f"ETA {eta // 3600}h {(eta % 3600) // 60}m {eta % 60}s")
        elif sample["coarse"]:
            parts.append("Speed / ETA unavailable (1% progress)")
        else:
            parts.append("ETA estimating")
        lines.append((sample["clock"] + " · " if sample["clock"] else "") + " · ".join(parts))
    result = {**source, "kind": "disk-copy", "text": "\n".join(lines)}
    if samples:
        result["progress"] = {key: samples[-1][key] for key in ("percent", "bytes", "total_bytes", "bytes_per_second", "eta_seconds", "coarse")}
        if samples[-1]["coarse"]:
            result["note"] = " ".join(filter(None, [source.get("note"),
                "This older job reports whole percentages. Repeated values do not confirm a stalled copy; speed and ETA cannot be measured from this output."]))
    return result


def failure_reason(log, fallback="the copy stopped; inspect its disk copy output"):
    """A percentage is progress, never an error. Keep the actual transport error."""
    lines = [line.strip() for line in str(log or "").splitlines() if line.strip()
             and not _copy_sample(line, 1) and line.strip() != "HSVM-DONE"]
    errors = [line for line in lines if re.search(
        r"curl:|no space|not enough space|too large|permission denied|host key|connection.*(?:failed|refused|reset)|"
        r"timed? out|out of memory|oomkilled|unauthorized|forbidden|certificate|error|failed", line, re.I)
        and not line.startswith("HSVM-FAILED ")]
    markers = [line[len("HSVM-FAILED "):] for line in lines if line.startswith("HSVM-FAILED ")]
    # curl's HTTP code follows the response body. Prefer the actionable reason
    # (permissions, space, etc.) over that generic transport summary.
    causes = [line for line in errors if not line.startswith("curl:")]
    return (causes[-1] if causes else errors[-1] if errors else markers[-1] if markers else fallback)[-300:]


def log_sources(item):
    """Live disk copy/upload output, or bounded evidence saved before cleanup."""
    ref = item["ref"]
    if "diagnostics" in ref:
        disks = {f"Disk {index + 1} · {disk['dv']} · copy": disk for index, disk in enumerate(ref.get("disks") or [])}
        return [copy_output(source, disks[source["title"]]) if source.get("title") in disks else source
                for source in ref["diagnostics"]]
    if ref.get("phase") == "failed":
        return [{"title": "Disk copy", "text": "", "note": "This older failed import kept no copy output before cleanup. Its earlier error cannot be recovered from a new attempt."}]
    ns, out = ref["namespace"], []
    from homestead_joblogs import _pod_source, _events
    for index, disk in enumerate(ref.get("disks") or []):
        title = f"Disk {index + 1} · {disk['dv']}"
        if disk.get("job"):
            try:
                pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{disk['job']}").get("items") or []
                if disk.get("job_uid"):
                    pods = [p for p in pods if any(o.get("uid") == disk["job_uid"] and o.get("kind") == "Job"
                            and o.get("controller") is True for o in p.get("metadata", {}).get("ownerReferences", []))]
                else:
                    pods = []  # an older attempt may share this name with a retry
                for pod in pods[:1]:
                    source = _pod_source(ns, pod, title + " · copy", timestamps=True)
                    status = pod.get("status") or {}
                    stopped = [c["state"]["terminated"] for c in status.get("containerStatuses") or []
                               if c.get("state", {}).get("terminated")]
                    source["note"] = "; ".join(f"{s.get('reason', 'exited')} (exit {s.get('exitCode', '?')})" for s in stopped) or source["note"]
                    out.append(copy_output(source, disk))
                if not pods:
                    out.append({"title": title + " · copy", "text": "", "note": "The confirmed copy pod is unavailable, or this older import recorded no Job identity."})
            except Exception:
                out.append({"title": title + " · copy", "text": "", "note": "Copy output is unavailable or was already cleaned up."})
        # CDI upload pods are tied to the destination PVC, including prime PVCs.
        try:
            targets = {disk["dv"]}
            pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{disk['dv']}")
            prime = pvc.get("metadata", {}).get("annotations", {}).get("cdi.kubevirt.io/storage.populator.pvcPrime")
            if prime:
                targets.add(prime)
            out.append({"title": title + " · storage", "text": _events(ns, disk["dv"], pvc.get("metadata", {}).get("uid", "")),
                        "note": f"Requested {pvc.get('spec', {}).get('resources', {}).get('requests', {}).get('storage', '?')}; capacity {pvc.get('status', {}).get('capacity', {}).get('storage', '?')}; phase {pvc.get('status', {}).get('phase', 'unknown')}"})
        except Exception:
            targets = {disk["dv"]}
        try:
            pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=cdi.kubevirt.io%3Dcdi-upload-server").get("items") or []
            for pod in pods:
                claims = [v.get("persistentVolumeClaim", {}).get("claimName") for v in pod.get("spec", {}).get("volumes", [])]
                if not targets.intersection(claims):
                    continue
                out.append(_pod_source(ns, pod, title + " · CDI upload"))
                out.append({"title": title + " · upload events", "text": _events(ns, pod["metadata"]["name"], pod["metadata"].get("uid", "")), "note": ""})
        except Exception:
            pass
    return out or [{"title": "Disk copy", "text": "", "note": "The import has no retained copy output yet, or its pods were cleaned up by an older release."}]


def _fail(ref, message, item=None):
    """Clear what this import made, so it can simply be run again."""
    # Save before deleting Jobs and DataVolumes: their pods disappear with them.
    remaining, saved = 60000, []
    for source in log_sources({"ref": ref}):
        if remaining <= 0:
            break
        text = str(source.get("text") or "")[-min(20000, remaining):]
        remaining -= len(text) + len(str(source.get("note") or "")) + 300
        saved.append({"title": str(source.get("title") or "Output")[:200], "text": text,
                      "note": str(source.get("note") or "")[:500],
                      **({"kind": "disk-copy", "progress": source.get("progress")} if source.get("kind") == "disk-copy" else {})})
    ref["diagnostics"] = saved
    if ref.get("cdi_cleanup"):
        ref.update(phase="cleanup", cleanup_outcome="failed",
                   cleanup_detail=message + f"; the VM on {_where(ref)} is unchanged")
        return CDI_CLEANUP.finish(item, kget, ksend, OPS.checkpoint)
    # Older jobs lack identity receipts. Never delete a retry's disks by name.
    ref["phase"] = "failed"
    return "failed", 100, (message + "; this older import has no cleanup receipt; review its disconnected volumes. "
                           f"The VM on {_where(ref)} is unchanged")


def status(item):
    ref = item["ref"]
    ns = ref["namespace"]
    total = sum(max(1, d["bytes"]) for d in ref["disks"])
    if ref["phase"] == "done":
        return "succeeded", 100, ref.get("detail", "Imported")
    if ref["phase"] == "failed":
        return "failed", 100, item.get("message", "Import failed")
    if ref.get("cdi_cleanup"):
        if ref["phase"] == "creating":
            ref.update(phase="cleanup", cleanup_outcome="failed", cleanup_detail="Import setup was interrupted")
        if ref["phase"] == "cleanup":
            return CDI_CLEANUP.finish(item, kget, ksend, OPS.checkpoint)
        hold = CDI_CLEANUP.track(item, kget, OPS.checkpoint)
        if hold:
            return hold
    if ref.get("cdi_cleanup"):
        dvs = [CDI_CLEANUP.RESOURCES.optional(kget, f"{CDI_API}/namespaces/{ns}/datavolumes/{d['dv']}") for d in ref["disks"]]
        if any(dv is None for dv in dvs):
            return _fail(ref, "An import disk was removed before completion", item)
    else:
        dvs = [kget(f"{CDI_API}/namespaces/{ns}/datavolumes/{d['dv']}") for d in ref["disks"]]
    phases = [((dv.get("status") or {}).get("phase") or "Pending") for dv in dvs]
    if any(p in ("Failed", "Error") for p in phases):
        return _fail(ref, "CDI could not take the disk", item)
    if ref["phase"] == "disks":
        if not all(p in ("UploadReady", "Succeeded") for p in phases):
            return "running", 2, "Waiting for CDI to be ready to receive the disks"
        _, proxy_ns = _upload_url()
        ca = _ca(proxy_ns)
        try:
            for index, disk in enumerate(ref["disks"]):
                if disk.get("job"):
                    return _fail(ref, "Copy setup was interrupted", item)
                disk["job"], _ = _job(ref, disk, index, _token(ns, disk["dv"]), ca, item)
        except Exception:
            return _fail(ref, "Could not start the disk copy", item)
        ref["phase"] = "copy"
        return "running", 3, "Copying from " + ref["source"]
    if ref["phase"] == "copy":
        done, running, failed = 0, 0, ""
        for disk in ref["disks"]:
            job = IMP._get_or_none(f"/apis/batch/v1/namespaces/{ns}/jobs/{disk['job']}") or {}
            st = job.get("status") or {}
            if any(c.get("type") == "Failed" and c.get("status") == "True" for c in st.get("conditions") or []) or (
                    st.get("failed") and not st.get("active")):
                log = _job_logs(ns, disk["job"])
                reason = next((c.get("message") or c.get("reason") for c in st.get("conditions") or []
                               if c.get("type") == "Failed" and c.get("status") == "True"), "")
                failed = failure_reason(log, reason or "the copy stopped; inspect its disk copy output")
                break
            if st.get("succeeded"):
                disk["pct"] = 100
                done += 1
            else:
                disk["pct"] = progress(_job_logs(ns, disk["job"]), disk["bytes"])
                running += 1
        if failed:
            return _fail(ref, f"Copying {ref.get('vm_name', ref['vm'])} failed: {failed}", item)
        pct = sum(max(1, d["bytes"]) * d.get("pct", 0) for d in ref["disks"]) / total
        if done < len(ref["disks"]):
            gib = sum(d["bytes"] * d.get("pct", 0) / 100 for d in ref["disks"]) / 1024 ** 3
            return "running", max(3, min(94, round(pct * 0.94))), f"Copying from {ref['source']}: {gib:.1f} of {total / 1024 ** 3:.1f} GiB"
        ref["phase"] = "convert"
        return "running", 95, "CDI is finishing the disk"
    if ref["phase"] == "convert":
        if not all(p == "Succeeded" for p in phases):
            return "running", 96, "CDI is finishing the disk"
        try:
            prepared = IMP.prepare_vm(dict(ref["vm_cfg"]), platform() or {}, "")
            IMP._recheck_vm_creation(prepared)
            try:
                IMP.commit_vm(prepared, send=ksend)
            except urllib.error.HTTPError as error:
                # Refused: commit_vm removed what it made for the VM, and the
                # next round would only make and remove it again. Anything else
                # may have made the VM, and is asked again as before.
                if not KUBE.definite_refusal(error.code):
                    raise
                error.close()
                raise ValueError(f"Kubernetes refused it (HTTP {error.code})") from None
        except ValueError as error:
            # The disks are whole: keep them, and say how to finish by hand.
            detail = (f"The disks arrived, but the VM could not be made: {error}. They are kept as "
                      f"{', '.join(d['dv'] for d in ref['disks'])}; make the VM from them under Import")
            if ref.get("cdi_cleanup"):
                ref.update(phase="cleanup", cleanup_outcome="failed", cleanup_keep_disks=True, cleanup_detail=detail)
                return CDI_CLEANUP.finish(item, kget, ksend, OPS.checkpoint)
            ref["phase"] = "failed"
            return "failed", 100, detail
        ref["phase"] = "done"
        ref["detail"] = (f"{ref['name']} is ready, stopped: start it in Virtual machines. "
                         f"{ref.get('vm_name', ref['vm'])} on {ref['source']} is untouched")
        if ref.get("cdi_cleanup"):
            ref.update(phase="cleanup", cleanup_outcome="succeeded", cleanup_detail=ref["detail"])
            return CDI_CLEANUP.finish(item, kget, ksend, OPS.checkpoint)
        return "succeeded", 100, ref["detail"]
    return "failed", 100, "Unknown import step"


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/sources/vms"): ("admin", lambda request: listing(str(request.body.get("name") or ""))),
    ("POST", "/api/sources/vms/shutdown"): ("admin", lambda request: shutdown(str(request.body.get("source") or ""), request.body.get("vm"))),
    ("POST", "/api/vms/import-unraid/room"): ("admin", lambda request: room(request.body)),
    ("POST", "/api/vms/import-unraid"): ("admin", lambda request: {"ok": True, "operation": start(request.body, request.user or "")}),
}


def register(ops):
    """The job store's entry for a VM import from Unraid or Proxmox (#622).
    Cancelling one also removes what CDI made for it."""
    ops.RESOLVERS[KIND] = status
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
    ops.CDI_CANCELLABLE.add(KIND)
