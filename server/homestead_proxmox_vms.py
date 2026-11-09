"""VMs on a Proxmox VE host, read and mapped for the VM import.

A Proxmox host keeps each VM's settings as `qm config <vmid>` prints them:
its cores and sockets, memory, firmware (SeaBIOS or OVMF), machine, its
disks by bus with the storage volume each lives on, and its network cards.
listing_script() reads every VM on the host over the import source's pinned
SSH - the same login the container import uses - and, for each disk, where
`pvesm path` says it is and what `qemu-img info` says of it. plan() maps a VM
onto what a Homestead VM can be, in the same shape as the Unraid import's, so
the copy, the job and the page are shared (homestead_unraid_vms).

A disk is copied by reading that path over SSH: an LVM-thin or ZFS volume is
a block device, a directory storage's disk a raw or qcow2 file, and both read
the same way. A disk on network storage (Ceph RBD, iSCSI) has no such path
and is left behind, saying so. `qm list` lists the VMs of the host it runs
on, so each node of a Proxmox cluster is added as a source of its own.

Nothing on Proxmox is changed, but for one thing its owner asks for: a VM
still running there can be shut down (qm shutdown - an ACPI power button
press), because a disk copied while its VM writes to it may not boot.
"""
import base64
import json
import math
import re
import shlex

# Every VM on the host: one VM line (its id, state and config, each base64
# so nothing in them can break the listing), then one DISK line for each of
# its disk volumes, with where it is and what qemu-img says of it.
LIST_SCRIPT = r"""
command -v qm >/dev/null 2>&1 || { echo NOQM; exit 0; }
qm list 2>/dev/null | awk 'NR > 1 { print $1, $3 }' | while read -r vmid status; do
  case "$vmid" in ''|*[!0-9]*) continue ;; esac
  config=$(qm config "$vmid" 2>/dev/null) || continue
  printf 'VM %s %s %s\n' "$vmid" "$(printf %s "$status" | base64 | tr -d '\n')" "$(printf %s "$config" | base64 | tr -d '\n')"
  printf '%s\n' "$config" | grep -E '^(virtio|scsi|sata|ide)[0-9]+: ' | grep -v 'media=cdrom' \
    | sed -E 's/^[a-z]+[0-9]+: ([^,]+).*/\1/' | while IFS= read -r volid; do
    case "$volid" in ''|none|/*) continue ;; esac
    path=$(pvesm path "$volid" 2>/dev/null) || path=""
    size=0; used=0; info=""
    case "$path" in
      /*)
        if [ -b "$path" ]; then
          size=$(blockdev --getsize64 "$path" 2>/dev/null || echo 0); used=$size
          # A volume's own count of what it holds: ZFS's referenced, LVM-thin's data percent.
          case "$path" in
            /dev/zvol/*) u=$(zfs get -Hp -o value referenced "${path#/dev/zvol/}" 2>/dev/null) && case "$u" in ''|*[!0-9]*) ;; *) used=$u ;; esac ;;
            /dev/*/*) u=$(lvs --noheadings -o data_percent "$path" 2>/dev/null | tr -d ' ')
              case "$u" in ''|*[!0-9.]*) ;; *) used=$(awk -v s="$size" -v p="$u" 'BEGIN { printf "%d", s * p / 100 }') ;; esac ;;
          esac
        elif [ -f "$path" ]; then size=$(stat -c %s -- "$path" 2>/dev/null || echo 0); used=$(( $(du -k -- "$path" 2>/dev/null | cut -f 1) * 1024 ))
        fi
        info=$(qemu-img info -U --output=json -- "$path" 2>/dev/null | tr -d '\n') || info="" ;;
    esac
    printf 'DISK %s %s %s %s %s %s\n' "$vmid" "$(printf %s "$volid" | base64 | tr -d '\n')" "$(printf %s "$path" | base64 | tr -d '\n')" \
      "${size:-0}" "${used:-0}" "$(printf %s "$info" | base64 | tr -d '\n')"
  done
done
echo END
"""

# What Proxmox calls a card, and what KubeVirt calls it.
NIC_MODELS = {"virtio": "virtio", "e1000": "e1000", "e1000e": "e1000e", "rtl8139": "rtl8139", "vmxnet3": "e1000e"}
# Proxmox's disk buses, and KubeVirt's: IDE becomes SATA, which every guest reads too.
DISK_BUSES = {"virtio": "virtio", "scsi": "scsi", "sata": "sata", "ide": "sata"}
BUS_ORDER = ("virtio", "scsi", "sata", "ide")
DISK_KEY = re.compile(r"^(virtio|scsi|sata|ide)(\d+)$")
# Formats CDI's upload takes as they come: anything else would need converting first.
UPLOADABLE = ("raw", "qcow2")
OS_NAMES = {"win11": "Windows 11", "win10": "Windows 10", "win8": "Windows 8", "win7": "Windows 7",
            "w2k22": "Windows Server 2022", "w2k19": "Windows Server 2019", "w2k16": "Windows Server 2016",
            "w2k12": "Windows Server 2012", "w2k8": "Windows Server 2008", "wvista": "Windows Vista", "wxp": "Windows XP",
            "l26": "Linux", "l24": "Linux 2.4", "solaris": "Solaris", "other": ""}


def _b64(value):
    try:
        return base64.b64decode(value or "", validate=True).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def parse_listing(lines):
    """[(vmid, state, config text)] and {volid: facts} from what LIST_SCRIPT printed."""
    vms, disks, complete, qm = [], {}, False, True
    for line in lines:
        parts = line.split(" ")
        if parts[0] == "NOQM":
            qm = False
        elif parts[0] == "END":
            complete = True
        elif parts[0] == "VM" and len(parts) == 4 and parts[1].isdigit():
            vms.append((parts[1], _b64(parts[2]).strip(), _b64(parts[3])))
        elif parts[0] == "DISK" and len(parts) == 7:
            info = {}
            try:
                info = json.loads(_b64(parts[6]) or "{}")
            except ValueError:
                pass
            size = int(parts[4]) if parts[4].isdigit() else 0
            disks[_b64(parts[2])] = {
                "path": _b64(parts[3]), "format": str(info.get("format") or ""),
                "virtual": int(info.get("virtual-size") or size or 0),
                "size": size, "used": int(parts[5]) if parts[5].isdigit() else 0}
    return {"qm": qm, "complete": complete, "vms": vms, "disks": disks}


def parse_config(text):
    """`qm config` as {key: value}. The description is its comment lines; a
    snapshot's settings follow a [name] line and are not the VM's own."""
    out, description = {}, []
    for line in str(text or "").splitlines():
        if line.startswith("["):
            break
        if line.startswith("#"):
            description.append(line[1:].strip())
            continue
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    out["description"] = " ".join(description)[:200]
    return out


def options(value):
    """"local-lvm:vm-100-disk-0,size=32G,ssd=1" -> ("local-lvm:vm-100-disk-0", {"size": "32G", "ssd": "1"})."""
    head, *rest = str(value or "").split(",")
    opts = {}
    for item in rest:
        key, sep, val = item.partition("=")
        opts[key.strip()] = val.strip() if sep else ""
    return head.strip(), opts


def _memory_mib(value):
    """"4096", or "current=4096" on newer Proxmox."""
    match = re.search(r"(?:^|current=)(\d+)", str(value or ""))
    return int(match.group(1)) if match else 512


def _boot_order(cfg):
    """The disks' keys in the order the VM boots from them."""
    boot = cfg.get("boot", "")
    match = re.search(r"order=([^,]+)", boot)
    if match:
        return [k for k in match.group(1).split(";") if k]
    # The older form: a letter string, with the disk named in bootdisk.
    return [cfg["bootdisk"]] if cfg.get("bootdisk") else []


def slug(name):
    """A Homestead name from a Proxmox one: "win11-gaming" stays, "VM 100" -> "vm-100"."""
    value = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40].strip("-")
    return value or "imported-vm"


def plan(vmid, state, cfg, facts):
    """How one Proxmox VM maps to a Homestead VM, and what stays behind -
    in the shape the Unraid import's plan has, with id its VMID."""
    gib = 1024 ** 3
    name = cfg.get("name") or f"vm-{vmid}"
    order = _boot_order(cfg)
    keys = sorted((k for k in cfg if DISK_KEY.match(k)),
                  key=lambda k: (order.index(k) if k in order else len(order),
                                 BUS_ORDER.index(DISK_KEY.match(k).group(1)), int(DISK_KEY.match(k).group(2))))
    disks, dropped = [], []
    for key in keys:
        volid, opts = options(cfg[key])
        bus = DISK_KEY.match(key).group(1)
        if opts.get("media") == "cdrom":
            if volid and volid != "none":
                dropped.append({"what": "CD-ROM", "detail": volid.rsplit("/", 1)[-1],
                                "reason": "an ISO is only needed to install. Add one in Edit VM if it is still needed."})
            continue
        if volid.startswith("/"):
            dropped.append({"what": f"Disk {key}", "detail": volid,
                            "reason": "a physical disk passed through, not a volume: attach its data another way"})
            continue
        seen = facts.get(volid) or {}
        path, fmt, virtual = seen.get("path", ""), seen.get("format") or "raw", int(seen.get("virtual") or 0)
        problem = ("" if path.startswith("/") else
                   f"{volid} is on storage with no file or device to read on the host (such as Ceph). Move the disk to local storage first.") or (
                   "" if virtual else f"{volid} could not be read on the host") or (
                   "" if fmt in UPLOADABLE else f"{volid} is {fmt}. Convert it to qcow2 or raw on Proxmox first (qm disk move --format qcow2).")
        disk = {"key": key, "volid": volid, "path": path, "target": key, "unraid_bus": bus,
                "bus": DISK_BUSES.get(bus, "virtio"), "format": fmt, "virtual": virtual,
                "size": int(seen.get("size") or 0), "used": int(seen.get("used") or 0),
                "size_gb": max(1, math.ceil(virtual / gib)) if virtual else 0, "found": not problem, "problem": problem}
        if problem and disks:
            # A second disk that can't be read is left behind; the VM still comes.
            dropped.append({"what": f"Disk {key}", "detail": volid, "reason": problem})
            continue
        disks.append(disk)
    for index, disk in enumerate(disks):
        disk["index"] = index
    for key in sorted(k for k in cfg if re.fullmatch(r"unused\d+", k)):
        dropped.append({"what": "Unused disk", "detail": options(cfg[key])[0],
                        "reason": "detached from the VM on Proxmox, so it is not part of it"})
    for key in sorted(k for k in cfg if re.fullmatch(r"hostpci\d+", k)):
        dropped.append({"what": "GPU or PCI device", "detail": options(cfg[key])[0], "hardware": True,
                        "reason": "passthrough is tied to the Proxmox host's hardware. Add it from Homestead's hardware devices in Edit VM."})
    for key in sorted(k for k in cfg if re.fullmatch(r"usb\d+", k)):
        dropped.append({"what": "USB device", "detail": options(cfg[key])[0], "hardware": True,
                        "reason": "passthrough is tied to the Proxmox host's hardware. Add it from Homestead's hardware devices in Edit VM."})
    nets = sorted((k for k in cfg if re.fullmatch(r"net\d+", k)), key=lambda k: int(k[3:]))
    nic = {}
    for i, key in enumerate(nets):
        head, opts = options(cfg[key])
        model, _, mac = head.partition("=")
        row = {"model": model, "mac": mac.lower(), "bridge": opts.get("bridge", ""), "vlan": opts.get("tag", "")}
        if i == 0:
            nic = row
        else:
            dropped.append({"what": "Second network card", "detail": f"{row['bridge']} {row['mac']}",
                            "reason": "a Homestead VM starts with one network card"})
    if cfg.get("affinity"):
        dropped.append({"what": "CPU affinity", "detail": cfg["affinity"],
                        "reason": "the Proxmox host's own tuning for its cores. The number of cores is kept."})
    uefi = cfg.get("bios") == "ovmf"
    efi_opts = options(cfg.get("efidisk0", ""))[1]
    machine = cfg.get("machine", "")
    notes = []
    if uefi and cfg.get("efidisk0"):
        notes.append("its EFI variables (efidisk0) start fresh here, so the boot entries Proxmox kept are lost. "
                     "Windows and most Linux systems start from the disk's default entry")
    if "tpmstate0" in cfg:
        notes.append("its TPM is created new, because Proxmox keeps the contents of the old one. If BitLocker "
                     "is on, it asks once for its recovery key")
    if "q35" not in machine:
        was = machine or "i440fx, Proxmox's default,"
        notes.append(f"{was} becomes q35, the only machine type KubeVirt runs. "
                     "Linux is not affected, and Windows finds its devices again when it first starts")
    if nic.get("vlan"):
        notes.append(f"its network card was on VLAN {nic['vlan']}. Choose a LAN network here that carries that VLAN")
    sockets, cores = int(cfg.get("sockets") or 1), int(cfg.get("cores") or 1)
    vcpus = int(cfg["vcpus"]) if str(cfg.get("vcpus") or "").isdigit() else sockets * cores
    mib = _memory_mib(cfg.get("memory"))
    ostype = cfg.get("ostype", "other")
    boot = disks[0] if disks else None
    problem = ("it has no disk to copy" if not boot else boot["problem"] and f"its boot disk: {boot['problem']}")
    return {
        "id": str(vmid), "vmid": int(vmid), "name": name, "slug": slug(name), "state": state,
        "shut_off": state == "stopped", "os": OS_NAMES.get(ostype, ostype),
        "cores": max(1, min(128, vcpus)),
        "memory": f"{mib // 1024}Gi" if mib >= 1024 and not mib % 1024 else f"{max(64, mib)}Mi",
        "firmware": "uefi" if uefi else "bios", "secure_boot": uefi and efi_opts.get("pre-enrolled-keys") == "1",
        "tpm": "tpmstate0" in cfg, "hyperv": ostype.startswith("w"),
        "cpu_model": "host-passthrough" if options(cfg.get("cpu", ""))[0] == "host" else "",
        "machine": machine or "i440fx",
        "disks": disks,
        "nic": {"mac": nic.get("mac", ""), "bridge": nic.get("bridge", ""), "model": nic.get("model", ""),
                "nic_model": NIC_MODELS.get(nic.get("model", ""), "virtio")},
        "dropped": dropped, "notes": notes,
        "ready": bool(boot) and not problem, "problem": problem or "",
    }


def listing(lines):
    """Every VM on the host, mapped, from what LIST_SCRIPT printed."""
    found = parse_listing(lines)
    if not found["qm"]:
        return {"hypervisor": False, "vms": []}
    if not found["complete"]:
        raise ValueError("Proxmox's VM list was incomplete. Try again.")
    vms = [plan(vmid, state, parse_config(text), found["disks"]) for vmid, state, text in found["vms"]]
    return {"hypervisor": True, "vms": sorted(vms, key=lambda v: (v["name"].lower(), v["vmid"]))}


def status_command(vmid):
    """Run on the host: its state as one word, "stopped" or "running"."""
    return f"qm status {int(vmid)} 2>/dev/null | sed -n 's/^status: //p' | head -n 1"


def shutdown_command(vmid):
    """An ACPI power button press, waited for no longer than the probe allows."""
    return "qm shutdown " + shlex.quote(str(int(vmid))) + " --timeout 50"
