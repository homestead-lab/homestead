"""PCI and USB devices of a host, given to virtual machines - and a GPU's
own ROM (vBIOS) with it.

A VM reaches a host's device through KubeVirt: the device is listed as one
VMs may use (permittedHostDevices, under a resource name), KubeVirt's device
plugin offers it on the host that has it, and a VM asks for it by that name
(spec.domain.devices.hostDevices) - which also keeps the VM on that host.

On Harvester, its pcidevices add-on does the host's part: a PCIDeviceClaim
or USBDeviceClaim hands a device to VMs and lists it with KubeVirt, and
Homestead makes and removes those claims, as Harvester's own UI does.

On k3s and RKE2 nothing does, so Homestead does it, through the host helper
(homestead_hostrun):
- it looks at the host (read-only): IOMMU on or not, every PCI device with
  its IOMMU group and the driver it has, and every USB device;
- IOMMU, when off, is switched on in GRUB's kernel command line
  (intel_iommu=on iommu=pt; AMD's is on by default), taking effect at the
  next restart - Host actions does that with its drain review;
- a PCI device is handed to vfio-pci now - the whole IOMMU group with it, as
  the kernel requires - and at every boot, by a small unit that runs before
  k3s or RKE2 starts; handing it back reverses both. A device holding the
  host's network or a mounted disk is refused;
- KubeVirt is told the device's vendor:device may be passed through, under
  homestead.io/pci-<vendor>-<device>; a USB device by vendor and product,
  homestead.io/usb-<vendor>-<product>, which needs nothing on the host.

A GPU sometimes needs its ROM given to the VM: some cards hide it once the
host has booted from them, some need a patched one. KubeVirt has no field
for that, so Homestead uses its hook sidecar (the Sidecar feature): a small
script, kept in a ConfigMap of the VM's with the ROM inside it, runs as
KubeVirt defines the VM, writes the ROM into the folder the hook shares with
the VM's QEMU, and names it as that device's ROM. A ConfigMap holds 1 MiB,
so a ROM can be up to 640 KB - GPU ROMs are 64 KB to 256 KB once any dump
header is trimmed; one that does not start 55 AA is refused.
"""
import base64
import copy
import json
import re
import time

import homestead_shared as SHARED

kget = ksend = hostrun = platform = None
# The raw devices Longhorn's V2 engine uses on a node (homestead_disks); None
# when unreadable. They are not mounted, so they are looked for by name: the
# controller under one holds Longhorn's data and never goes to a VM.
longhorn_block_paths = lambda node: []
BLOCK_PATH = re.compile(r"/dev/[A-Za-z0-9/_.:+@-]+")
DATA_DIR = "/data"
KUBEVIRTS = "/apis/kubevirt.io/v1/kubevirts"
HV = "/apis/devices.harvesterhci.io/v1beta1"
PREFIX = "homestead.io/"
ADDRESS = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
HEX4 = re.compile(r"[0-9a-f]{4}")
ROM_LIMIT = 640 * 1024
HOOK_KEY = "vbios.py"
HOOK_ANNOTATION = "hooks.kubevirt.io/hookSidecars"
VFIO_LIST = "/etc/homestead/vfio.list"
VFIO_UNIT = "homestead-vfio.service"

INSPECT = r"""echo "CMDLINE $(cat /proc/cmdline)"
echo "GROUPS $(ls /sys/kernel/iommu_groups 2>/dev/null | wc -l)"
echo "CPU $(awk -F': ' '/^vendor_id/ {print $2; exit}' /proc/cpuinfo)"
for d in /sys/bus/pci/devices/*; do
  a=${d##*/}
  drv=""; [ -e "$d/driver" ] && drv=$(basename "$(readlink "$d/driver")")
  grp=""; [ -e "$d/iommu_group" ] && grp=$(basename "$(readlink "$d/iommu_group")")
  echo "PCI $a|$(cat "$d/vendor")|$(cat "$d/device")|$(cat "$d/class")|$drv|$grp|$(cat "$d/boot_vga" 2>/dev/null)|$(ls "$d/net" 2>/dev/null | tr '\n' ' ')"
done
command -v lspci >/dev/null 2>&1 && lspci -Dmm 2>/dev/null | sed 's/^/NAME /'
for u in /sys/bus/usb/devices/*; do
  [ -f "$u/idVendor" ] || continue
  echo "USB $(cat "$u/idVendor")|$(cat "$u/idProduct")|$(cat "$u/bDeviceClass" 2>/dev/null)|$(cat "$u/manufacturer" 2>/dev/null | tr '|' ' ')|$(cat "$u/product" 2>/dev/null | tr '|' ' ')|${u##*/}"
done
ip -4 route show default 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") print "DEFAULT " $(i + 1)}'
for s in $(findmnt -rn -o SOURCE 2>/dev/null | grep '^/dev/' | sort -u); do
  b=$(readlink -f "$s"); b=${b##*/}
  for p in /sys/class/block/$b $(ls -d /sys/class/block/$b/slaves/* 2>/dev/null); do echo "MOUNTED $(readlink -f "$p")"; done
done
[ -f __LIST__ ] && sed 's/^/LISTED /' __LIST__
echo END
""".replace("__LIST__", VFIO_LIST)

# At every boot, before k3s or RKE2: each listed device to vfio-pci.
BOOT_SCRIPT = r"""#!/bin/sh
# Homestead: PCI devices handed to virtual machines (Nodes > Devices).
modprobe vfio-pci 2>/dev/null
[ -f __LIST__ ] || exit 0
while read -r a; do
  d=/sys/bus/pci/devices/$a
  [ -e "$d" ] || continue
  echo vfio-pci > "$d/driver_override"
  [ -e "$d/driver" ] && [ "$(basename "$(readlink "$d/driver")")" != vfio-pci ] && echo "$a" > "$d/driver/unbind"
  echo "$a" > /sys/bus/pci/drivers_probe
done < __LIST__
""".replace("__LIST__", VFIO_LIST)

BOOT_UNIT = """[Unit]
Description=Homestead: PCI devices for virtual machines, to vfio-pci
DefaultDependencies=no
After=systemd-modules-load.service
Before=k3s.service k3s-agent.service rke2-server.service rke2-agent.service display-manager.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/homestead-vfio
RemainAfterExit=yes
[Install]
WantedBy=sysinit.target
"""


def bind(_kget, _ksend, _hostrun, _platform, data_dir="/data"):
    global kget, ksend, hostrun, platform, DATA_DIR
    kget, ksend, hostrun, platform, DATA_DIR = _kget, _ksend, _hostrun, _platform, data_dir


def _get(path):
    try:
        return kget(path)
    except Exception:
        return None


def _harvester():
    return bool((platform() or {}).get("harvester"))


# ---- looking at a host (k3s and RKE2) ----------------------------------------

def _names(line):
    """One `lspci -Dmm` line: address, class, vendor, device names."""
    fields = re.findall(r'"([^"]*)"', line)
    address = line.split(" ", 1)[0]
    return address, (fields + ["", "", ""])[:3]


def parse(out):
    facts = {"iommu": False, "cmdline_iommu": False, "cpu": "", "pci": [], "usb": [], "listed": [], "complete": False}
    default_ifaces, mounted, names = set(), [], {}
    rows = []
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "CMDLINE":
            facts["cmdline_iommu"] = bool(re.search(r"\b(intel_iommu=on|amd_iommu=on|iommu=pt)\b", value))
        elif key == "GROUPS":
            facts["iommu"] = value.strip().isdigit() and int(value.strip()) > 0
        elif key == "CPU":
            facts["cpu"] = "intel" if "Intel" in value else "amd" if "AMD" in value else value.strip()
        elif key == "PCI":
            parts = (value.split("|") + [""] * 8)[:8]
            rows.append(parts)
        elif key == "NAME":
            address, fields = _names(value)
            names[address] = fields
        elif key == "USB":
            vid, pid, klass, maker, product, port = (value.split("|") + [""] * 6)[:6]
            # Hubs, and Linux's own root hubs, are not devices to hand over.
            if klass.strip() == "09" or vid == "1d6b" or not HEX4.fullmatch(vid) or not HEX4.fullmatch(pid):
                continue
            facts["usb"].append({"vendor": vid, "product": pid, "name": " ".join(x for x in (maker.strip(), product.strip()) if x)
                                 or f"{vid}:{pid}", "port": port, "resource": usb_resource(vid, pid)})
        elif key == "DEFAULT":
            default_ifaces.add(value.strip())
        elif key == "MOUNTED":
            mounted.append(value.strip())
        elif key == "LISTED":
            facts["listed"].append(value.strip())
        elif key == "END":
            facts["complete"] = True
    groups = {}
    for address, vendor, device, klass, driver, group, boot_vga, nets in rows:
        if not ADDRESS.fullmatch(address):
            continue
        vendor, device, klass = vendor.replace("0x", ""), device.replace("0x", ""), klass.replace("0x", "")
        class_name, vendor_name, device_name = names.get(address, ["", "", ""])
        row = {"address": address, "vendor": vendor, "device": device, "class": klass[:4], "class_name": class_name,
               "name": " ".join(x for x in (vendor_name, device_name) if x) or f"{vendor}:{device}",
               "driver": driver, "group": group, "boot_vga": boot_vga.strip() == "1",
               "nets": nets.split(), "vfio": driver == "vfio-pci", "listed": address in facts["listed"],
               "resource": pci_resource(vendor, device)}
        problems = []
        if default_ifaces & set(row["nets"]):
            problems.append("it carries this host's network")
        if any(f"/{address}/" in path for path in mounted):
            problems.append("a disk on it is in use by the host or by Longhorn")
        row["problems"] = problems
        # Worth giving a VM: storage, network, display, multimedia, USB
        # controllers and accelerators - not bridges or the chipset's own.
        row["offered"] = klass[:2] in ("01", "02", "03", "04", "12") or klass[:4] == "0c03"
        groups.setdefault(group, []).append(address)
        facts["pci"].append(row)
    for row in facts["pci"]:
        row["group_members"] = [a for a in groups.get(row["group"], []) if a != row["address"]] if row["group"] else []
    return facts


def pci_resource(vendor, device):
    return f"{PREFIX}pci-{vendor}-{device}"


def usb_resource(vendor, product):
    return f"{PREFIX}usb-{vendor}-{product}"


INVENTORY_LOCK = SHARED.SharedLock("passthrough-inventory", strict=True, directory=lambda: DATA_DIR)


def _inventory():
    try:
        with open(f"{DATA_DIR}/passthrough-inventory.json", encoding="utf-8") as handle:
            value = json.load(handle)
        return {node: facts for node, facts in value.items() if isinstance(facts, dict) and facts.get("complete")
                and isinstance(facts.get("pci"), list) and isinstance(facts.get("usb"), list)} if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def inventory(node):
    """Last complete inspection, for display only. No host helper is started."""
    return {"facts": _inventory().get(node)}


def _remember(facts):
    facts["inspected_at"] = int(time.time())
    # A display cache must not prevent a device inspection or handoff when
    # the data volume is unavailable. Mutations always inspect the host anew.
    try:
        with INVENTORY_LOCK:
            saved = _inventory()
            saved[facts["node"]] = facts
            SHARED.write_json(f"{DATA_DIR}/passthrough-inventory.json", saved)
    except (OSError, ValueError, RuntimeError) as error:
        facts["inventory_error"] = str(error)
    return facts


def inspect(node):
    """A host's devices, as it sees them now."""
    if _harvester():
        return _remember(_harvester_inventory(node))
    claimed = longhorn_block_paths(node)
    if claimed is None:
        raise ValueError(f"Longhorn's disks on {node} could not be read, so its devices are not offered")
    extra = "".join(f'r=$(readlink -f "{p}" 2>/dev/null); [ -b "$r" ] && for n in $(lsblk -snro NAME "$r" 2>/dev/null); '
                    f'do echo "MOUNTED $(readlink -f /sys/class/block/$n)"; done\n'
                    for p in claimed if BLOCK_PATH.fullmatch(p))
    out, err = hostrun.run(node, INSPECT.replace("echo END\n", extra + "echo END\n"), timeout=90)
    facts = parse(out)
    if not facts["complete"]:
        raise ValueError(f"could not look at {node}'s devices: {(err or out)[-200:]}")
    permitted = _permitted()
    for row in facts["pci"]:
        row["permitted"] = row["resource"] in permitted
    for row in facts["usb"]:
        row["permitted"] = row["resource"] in permitted
    facts.update(node=node, harvester=False, kubevirt=bool(_kubevirt()))
    return _remember(facts)


# ---- IOMMU --------------------------------------------------------------------

def capture_script(address):
    if not ADDRESS.fullmatch(address):
        raise ValueError("Choose a valid PCI address")
    return f'''set -e
d=/sys/bus/pci/devices/{address}
[ -f "$d/rom" ] || {{ echo "ERR This GPU does not expose a ROM through sysfs"; exit 1; }}
drv=""; [ ! -e "$d/driver" ] || drv=$(basename "$(readlink "$d/driver")")
[ -z "$drv" ] || [ "$drv" = vfio-pci ] || {{ echo "ERR Give this GPU to VMs first; its host driver is still active"; exit 1; }}
t=$(mktemp)
pm=""
cleanup() {{
  echo 0 > "$d/rom" 2>/dev/null || :
  [ -z "$pm" ] || echo "$pm" > "$d/power/control" || :
  rm -f "$t"
}}
trap cleanup EXIT HUP INT TERM
# VFIO runtime-suspends unused cards. A ROM read in D3hot fails even though
# sysfs exposes the ROM; wake the card and restore its original policy.
if [ -f "$d/power/control" ]; then
  pm=$(cat "$d/power/control")
  case "$pm" in auto|on) ;; *) echo "ERR The GPU power policy could not be read"; exit 1 ;; esac
  echo on > "$d/power/control" || {{ echo "ERR The GPU could not be woken for ROM capture"; exit 1; }}
fi
echo 1 > "$d/rom" || {{ echo "ERR The GPU ROM could not be enabled"; exit 1; }}
dd if="$d/rom" of="$t" bs=4096 count={ROM_LIMIT // 4096 + 1} 2>/dev/null || {{ echo "ERR This GPU ROM cannot be read; upload a ROM dumped from this card instead"; exit 1; }}
echo ROM
base64 "$t"
echo END
'''


def capture_vbios(node, address):
    """Read a stopped GPU's ROM without unbinding drivers or resetting it."""
    if not ADDRESS.fullmatch(address):
        raise ValueError("Choose a valid PCI address")
    facts = inspect(node)
    gpu = next((r for r in facts["pci"] if r["address"] == address), None)
    if not gpu or not str(gpu.get("class") or "").startswith("03"):
        raise ValueError("vBIOS capture is only available for a GPU on this host")
    if gpu.get("driver") not in (None, "", "vfio-pci"):
        raise ValueError("Give this GPU to VMs first; capture does not detach an active host display")
    try:
        instances = kget("/apis/kubevirt.io/v1/virtualmachineinstances")["items"]
    except Exception:
        raise ValueError("VM device use could not be checked; no ROM was read") from None
    resources_in_group = {r.get("resource") for r in facts["pci"]
                          if r["address"] in [address, *gpu.get("group_members", [])]}
    for instance in instances:
        status = instance.get("status") or {}
        if status.get("phase") in ("Succeeded", "Failed"):
            continue
        # Unscheduled instances can acquire this device while capture starts.
        if status.get("nodeName") and status["nodeName"] != node:
            continue
        devices = ((instance.get("spec") or {}).get("domain") or {}).get("devices") or {}
        if any(d.get("deviceName") in resources_in_group for d in
               (devices.get("hostDevices") or []) + (devices.get("gpus") or [])):
            raise ValueError(f"Stop VM {instance['metadata']['name']} before capturing this GPU's vBIOS")
    out, err = hostrun.run(node, capture_script(address), timeout=60)
    if not out.startswith("ROM\n") or not out.rstrip().endswith("\nEND"):
        problem = next((l[4:] for l in out.splitlines() if l.startswith("ERR ")), "")
        raise ValueError(problem or "The GPU ROM could not be read; upload a dump from this card instead")
    raw = check_rom("".join(out.splitlines()[1:-1]))
    # Check every PCI image, including the EFI image, before offering a dump.
    offset = 0
    while True:
        if offset + 26 > len(raw) or raw[offset:offset + 2] != b"\x55\xaa":
            raise ValueError("The captured ROM is truncated or invalid; upload a dump from this card instead")
        pcir = offset + int.from_bytes(raw[offset + 24:offset + 26], "little")
        if pcir + 22 > len(raw) or raw[pcir:pcir + 4] != b"PCIR":
            raise ValueError("The captured ROM has no valid PCI image header")
        vendor = int.from_bytes(raw[pcir + 4:pcir + 6], "little")
        device = int.from_bytes(raw[pcir + 6:pcir + 8], "little")
        if (vendor, device) != (int(gpu["vendor"], 16), int(gpu["device"], 16)):
            raise ValueError("The captured ROM does not match this GPU's vendor and device")
        size = int.from_bytes(raw[pcir + 16:pcir + 18], "little") * 512
        if not size or pcir + 22 > offset + size or offset + size > len(raw):
            raise ValueError("The captured ROM is truncated or invalid")
        if raw[pcir + 21] & 128:
            break
        offset += size
    return {"ok": True, "data": base64.b64encode(raw).decode(), "size": len(raw),
            "filename": f"vbios-{address.replace(':', '-')}-{gpu['vendor']}-{gpu['device']}.rom"}

def iommu_script(cpu):
    words = "intel_iommu=on iommu=pt" if cpu == "intel" else "iommu=pt"
    return f"""set -e
F=/etc/default/grub
[ -f "$F" ] || {{ echo "ERR this host has no /etc/default/grub; add {words} to its kernel command line by hand"; exit 1; }}
command -v update-grub >/dev/null 2>&1 || command -v grub2-mkconfig >/dev/null 2>&1 || {{ echo "ERR no update-grub or grub2-mkconfig"; exit 1; }}
cp "$F" /etc/default/grub.homestead-backup
for w in {words}; do
  grep -q "^GRUB_CMDLINE_LINUX_DEFAULT=.*$w" "$F" || sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\\"\\(.*\\)\\"/GRUB_CMDLINE_LINUX_DEFAULT=\\"\\1 $w\\"/" "$F"
done
grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$F" || echo 'GRUB_CMDLINE_LINUX_DEFAULT="{words}"' >> "$F"
if command -v update-grub >/dev/null 2>&1; then update-grub >/dev/null 2>&1; else grub2-mkconfig -o /boot/grub2/grub.cfg >/dev/null 2>&1; fi
echo "OK {words}"
"""


def enable_iommu(node):
    """IOMMU in the kernel command line, from the next restart."""
    if _harvester():
        raise ValueError("Harvester's hosts have IOMMU set up by Harvester")
    facts = inspect(node)
    if facts["iommu"]:
        return {"ok": True, "detail": f"IOMMU is on on {node} already"}
    out, err = hostrun.run(node, iommu_script(facts["cpu"]), timeout=120)
    done = next((line for line in out.splitlines() if line.startswith("OK ")), "")
    if not done:
        problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-200:]
        raise ValueError(f"IOMMU was not switched on on {node}: {problem}")
    return {"ok": True, "restart_needed": True,
            "detail": f"{done[3:]} is in {node}'s kernel command line; restart it from Host actions for IOMMU to be on "
                      "(firmware must have VT-d or AMD-Vi enabled too)"}


# ---- handing a PCI device over (k3s and RKE2) -----------------------------------

def _state_path():
    return f"{DATA_DIR}/passthrough.json"


def _load():
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    SHARED.write_json(_state_path(), state, indent=1, sort_keys=True)


def vfio_script(addresses, give):
    """Each address to vfio-pci (give) or back to its own driver, now and at boot."""
    for address in addresses:
        if not ADDRESS.fullmatch(address):
            raise ValueError(f"{address} is not a PCI address")
    words = " ".join(addresses)
    if give:
        body = f"""modprobe vfio-pci
mkdir -p /etc/homestead /usr/local/sbin
for a in {words}; do grep -qx "$a" {VFIO_LIST} 2>/dev/null || echo "$a" >> {VFIO_LIST}; done
cat > /usr/local/sbin/homestead-vfio <<'HOMESTEAD_VFIO'
{BOOT_SCRIPT}HOMESTEAD_VFIO
chmod 755 /usr/local/sbin/homestead-vfio
cat > /etc/systemd/system/{VFIO_UNIT} <<'HOMESTEAD_UNIT'
{BOOT_UNIT}HOMESTEAD_UNIT
systemctl daemon-reload
systemctl enable {VFIO_UNIT} >/dev/null 2>&1
for a in {words}; do
  d=/sys/bus/pci/devices/$a
  echo vfio-pci > "$d/driver_override"
  if [ -e "$d/driver" ] && [ "$(basename "$(readlink "$d/driver")")" != vfio-pci ]; then echo "$a" > "$d/driver/unbind"; fi
  echo "$a" > /sys/bus/pci/drivers_probe
  [ "$(basename "$(readlink "$d/driver" 2>/dev/null)")" = vfio-pci ] || {{ echo "ERR $a did not move to vfio-pci; its driver may be in use"; exit 1; }}
done
"""
    else:
        body = f"""for a in {words}; do
  [ -f {VFIO_LIST} ] && sed -i "/^$a\\$/d" {VFIO_LIST}
  d=/sys/bus/pci/devices/$a
  echo > "$d/driver_override"
  if [ -e "$d/driver" ] && [ "$(basename "$(readlink "$d/driver")")" = vfio-pci ]; then echo "$a" > "$d/driver/unbind"; fi
  echo "$a" > /sys/bus/pci/drivers_probe
done
"""
    return "set -e\n" + body + "echo OK\n"


def give(node, address):
    """Hand a PCI device - and its IOMMU group - to VMs on node."""
    address = str(address or "").lower()
    if _harvester():
        return _harvester_claim(node, address, True)
    facts = inspect(node)
    row = next((r for r in facts["pci"] if r["address"] == address), None)
    if not row:
        raise ValueError(f"{node} has no PCI device {address}")
    if not facts["iommu"]:
        raise ValueError(f"IOMMU is off on {node}; switch it on and restart the host first")
    if row["problems"]:
        raise ValueError(f"{address} stays with {node}: {'; '.join(row['problems'])}")
    members = [r for r in facts["pci"] if r["address"] in row["group_members"]]
    # A PCI bridge in the group stays with the host; anything else goes too.
    moving = [row] + [r for r in members if not r["class"].startswith("0604")]
    blocked = [f"{r['address']} ({'; '.join(r['problems'])})" for r in moving if r["problems"]]
    if blocked:
        raise ValueError(f"{address} shares IOMMU group {row['group']} with {', '.join(blocked)}, which stay with the host")
    out, err = hostrun.run(node, vfio_script([r["address"] for r in moving], True), timeout=180)
    if "OK" not in out.split():
        problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-200:]
        raise ValueError(f"{address} was not handed over: {problem}")
    state = _load()
    for r in moving:
        state.setdefault("pci", {}).setdefault(node, {})[r["address"]] = f"{r['vendor']}:{r['device']}"
    _save(state)
    for r in moving:
        _permit(pci=(r["vendor"], r["device"]))
    return {"ok": True, "resource": row["resource"], "moved": [r["address"] for r in moving],
            "detail": f"{row['name']} on {node} is for VMs now, as {row['resource']}"
                      + (f", with {', '.join(r['address'] for r in moving[1:])} from its IOMMU group" if len(moving) > 1 else "")}


def take_back(node, address):
    """Give a PCI device back to its host driver."""
    address = str(address or "").lower()
    if _harvester():
        return _harvester_claim(node, address, False)
    state = _load()
    listed = state.get("pci", {}).get(node, {})
    if address not in listed:
        raise ValueError(f"Homestead did not hand {address} on {node} to VMs")
    facts = inspect(node)
    row = next((r for r in facts["pci"] if r["address"] == address), None)
    group = [address] + [a for a in (row or {}).get("group_members", []) if a in listed]
    # A card pulled from the host has no group to read: its other functions
    # (a GPU's HDMI audio) were handed over with it, in the same slot.
    slot = address.rsplit(".", 1)[0] + "."
    group += [a for a in listed if a.startswith(slot) and a not in group]
    out, err = hostrun.run(node, vfio_script(group, False), timeout=120)
    if "OK" not in out.split():
        raise ValueError(f"{address} was not given back: {(err or out)[-200:]}")
    # The vendor:device each was handed over as, as recorded then: a device
    # no longer on the host has no facts to read it from.
    pairs = {listed.pop(a) for a in group if a in listed}
    _save(state)
    still = {v for rows in state.get("pci", {}).values() for v in rows.values()}
    for pair in pairs - still:
        vendor, _, device = str(pair).partition(":")
        if HEX4.fullmatch(vendor) and HEX4.fullmatch(device):
            _permit(pci=(vendor, device), remove=True)
    return {"ok": True, "detail": f"{address} on {node} is back with the host"}


def allow_usb(vendor, product, allow=True):
    """A USB device, by vendor and product, for VMs - on any host that has one."""
    vendor, product = str(vendor).lower(), str(product).lower()
    if not HEX4.fullmatch(vendor) or not HEX4.fullmatch(product):
        raise ValueError("a USB device is its vendor and product, like 0bda:8153")
    if _harvester():
        raise ValueError("on Harvester, USB devices are given to VMs per host: use the device's own row")
    _permit(usb=(vendor, product), remove=not allow)
    word = "can be given to VMs" if allow else "is no longer offered to VMs"
    return {"ok": True, "resource": usb_resource(vendor, product), "detail": f"USB {vendor}:{product} {word}"}


# ---- KubeVirt ---------------------------------------------------------------------

def _kubevirt():
    items = (_get(KUBEVIRTS) or {}).get("items") or []
    return items[0] if items else None


def _kv_path(kv):
    meta = kv["metadata"]
    return f"/apis/kubevirt.io/v1/namespaces/{meta['namespace']}/kubevirts/{meta['name']}"


def _permitted(kv=None):
    kv = kv if kv is not None else _kubevirt()
    hd = ((((kv or {}).get("spec") or {}).get("configuration") or {}).get("permittedHostDevices") or {})
    return {row.get("resourceName") for key in ("pciHostDevices", "usb", "mediatedDevices") for row in hd.get(key) or []}


def ensure_gates(*gates, send=None):
    """KubeVirt's feature gates, with these on."""
    reviewed = send is not None
    send = send or ksend
    kv = _kubevirt()
    if not kv:
        raise ValueError("KubeVirt is not installed")
    developer = ((kv["spec"].get("configuration") or {}).get("developerConfiguration") or {})
    current = list(developer.get("featureGates") or [])
    missing = [g for g in gates if g not in current]
    if missing:
        meta = kv.get("metadata") or {}
        if reviewed and not all(meta.get(key) for key in ("uid", "resourceVersion")):
            raise ValueError("KubeVirt identity/version is unavailable; review the device settings again")
        identity = {key: meta[key] for key in ("uid", "resourceVersion") if meta.get(key)}
        send("PATCH", _kv_path(kv), {"metadata": identity, "spec": {"configuration": {"developerConfiguration": {"featureGates": current + missing}}}},
              ctype="application/merge-patch+json")
    return missing


def _permit(pci=None, usb=None, remove=False):
    kv = _kubevirt()
    if not kv:
        raise ValueError("KubeVirt is not installed")
    hd = copy_dict(((kv["spec"].get("configuration") or {}).get("permittedHostDevices") or {}))
    if pci:
        vendor, device = pci
        rows = [r for r in hd.get("pciHostDevices") or [] if r.get("resourceName") != pci_resource(vendor, device)]
        if not remove:
            rows.append({"pciVendorSelector": f"{vendor}:{device}".upper(), "resourceName": pci_resource(vendor, device)})
        hd["pciHostDevices"] = rows
    if usb:
        vendor, product = usb
        rows = [r for r in hd.get("usb") or [] if r.get("resourceName") != usb_resource(vendor, product)]
        if not remove:
            rows.append({"resourceName": usb_resource(vendor, product), "selectors": [{"vendor": vendor, "product": product}]})
        hd["usb"] = rows
    ksend("PATCH", _kv_path(kv), {"spec": {"configuration": {"permittedHostDevices": hd}}},
          ctype="application/merge-patch+json")
    if not remove:
        ensure_gates("HostDevices")


def copy_dict(value):
    return json.loads(json.dumps(value))


# ---- Harvester ------------------------------------------------------------------

def _harvester_inventory(node):
    pci = [d for d in (_get(f"{HV}/pcidevices") or {}).get("items") or [] if (d.get("status") or {}).get("nodeName") == node]
    usb = [d for d in (_get(f"{HV}/usbdevices") or {}).get("items") or [] if (d.get("status") or {}).get("nodeName") == node]
    if _get(f"{HV}/pcidevices") is None:
        raise ValueError("Harvester's pcidevices-controller add-on is off; enable it under Harvester's Advanced > Add-ons")
    claimed = {c["metadata"]["name"] for c in (_get(f"{HV}/pcideviceclaims") or {}).get("items") or []}
    uclaimed = {c["metadata"]["name"] for c in (_get(f"{HV}/usbdeviceclaims") or {}).get("items") or []}
    rows = []
    for d in pci:
        s = d.get("status") or {}
        klass = str(s.get("classId") or "")
        rows.append({"address": s.get("address", ""), "vendor": s.get("vendorId", ""), "device": s.get("deviceId", ""),
                     "class": klass[:4], "class_name": "", "name": s.get("description") or d["metadata"]["name"],
                     "driver": s.get("kernelDriverInUse", ""), "group": s.get("iommuGroup", ""),
                     "vfio": d["metadata"]["name"] in claimed, "listed": d["metadata"]["name"] in claimed,
                     "permitted": d["metadata"]["name"] in claimed, "resource": s.get("resourceName", ""),
                     "problems": [], "group_members": [], "boot_vga": False, "nets": [], "harvester_name": d["metadata"]["name"],
                     "offered": klass[:2] in ("01", "02", "03", "04", "12") or klass[:4] == "0c03"})
    for row in rows:
        group = row["group"]
        row["group_members"] = [r["address"] for r in rows if r is not row and r["group"] == group] if group not in (None, "") else []
    usb_rows = [{"vendor": (d.get("status") or {}).get("vendorID", ""), "product": (d.get("status") or {}).get("productID", ""),
                 "name": (d.get("status") or {}).get("description") or d["metadata"]["name"],
                 "resource": (d.get("status") or {}).get("resourceName", ""), "port": (d.get("status") or {}).get("devicePath", ""),
                 "permitted": d["metadata"]["name"] in uclaimed, "harvester_name": d["metadata"]["name"]} for d in usb]
    return {"node": node, "harvester": True, "iommu": True, "cmdline_iommu": True, "cpu": "", "pci": rows,
            "usb": usb_rows, "listed": [], "complete": True, "kubevirt": True}


def _harvester_claim(node, address, claim, kind="pci"):
    items = (_get(f"{HV}/{kind}devices") or {}).get("items") or []
    key = "address" if kind == "pci" else "devicePath"
    device = next((d for d in items if (d.get("status") or {}).get("nodeName") == node
                   and ((d.get("status") or {}).get(key) == address or d["metadata"]["name"] == address)), None)
    if not device:
        raise ValueError(f"Harvester lists no {kind.upper()} device {address} on {node}")
    name = device["metadata"]["name"]
    path = f"{HV}/{kind}deviceclaims"
    if claim:
        spec = {"address": address, "nodeName": node, "userName": "homestead"} if kind == "pci" else {"userName": "homestead"}
        ksend("POST", path, {"apiVersion": "devices.harvesterhci.io/v1beta1",
                             "kind": "PCIDeviceClaim" if kind == "pci" else "USBDeviceClaim",
                             "metadata": {"name": name, "ownerReferences": [{
                                 "apiVersion": "devices.harvesterhci.io/v1beta1",
                                 "kind": "PCIDevice" if kind == "pci" else "USBDevice",
                                 "name": name, "uid": device["metadata"]["uid"]}]},
                             "spec": spec})
        return {"ok": True, "detail": f"Harvester is handing {name} to VMs"}
    ksend("DELETE", f"{path}/{name}")
    return {"ok": True, "detail": f"Harvester is giving {name} back to {node}"}


def harvester_usb(node, name, claim):
    return _harvester_claim(node, name, claim, kind="usb")


# ---- what VMs can use, and what a VM has --------------------------------------

def resources(with_usage=False):
    """Every device resource a VM may ask for, and the hosts offering it now."""
    offered = {}
    nodes = (kget("/api/v1/nodes") or {}).get("items", [])
    live_nodes = {n["metadata"]["name"] for n in nodes}
    snapshots = _inventory()
    if _harvester():
        snapshots = {}
        # Harvester exposes descriptions through its inventory CRs; no host
        # helper (or prior Homestead inspection) is necessary for names.
        for kind in ("pci", "usb"):
            for device in (_get(f"{HV}/{kind}devices") or {}).get("items") or []:
                status = device.get("status") or {}
                node = status.get("nodeName", "")
                snapshots.setdefault(node, {}).setdefault(kind, []).append({
                    "resource": status.get("resourceName", ""),
                    "name": status.get("description") or (device.get("metadata") or {}).get("name", ""),
                    "address": status.get("address") if kind == "pci" else status.get("devicePath"),
                    "class": str(status.get("classId") or "").lower().removeprefix("0x"),
                    "group": status.get("iommuGroup") if kind == "pci" else None})
    for n in nodes:
        for key, value in ((n.get("status") or {}).get("allocatable") or {}).items():
            if "/" in key and str(value) not in ("0", ""):
                offered.setdefault(key, []).append(n["metadata"]["name"])
    kv = _kubevirt()
    hd = ((((kv or {}).get("spec") or {}).get("configuration") or {}).get("permittedHostDevices") or {})
    out = []
    for kind, key in (("pci", "pciHostDevices"), ("usb", "usb")):
        for row in hd.get(key) or []:
            name = row.get("resourceName", "")
            label = row.get("pciVendorSelector") or ", ".join(f"{s.get('vendor')}:{s.get('product')}" for s in row.get("selectors") or [])
            devices = []
            for node, facts in snapshots.items():
                if node not in live_nodes:
                    continue
                for device in facts.get(kind) or []:
                    selector = f"{device.get('vendor', '')}:{device.get('device' if kind == 'pci' else 'product', '')}".lower()
                    matches = (selector == label.lower() if kind == "pci" else
                               any(selector == f"{s.get('vendor')}:{s.get('product')}".lower() for s in row.get("selectors") or []))
                    if device.get("resource") == name or matches:
                        detail = {"node": node, "name": device.get("name", ""),
                                  "class": device.get("class", ""),
                                  "address": device.get("address") or device.get("port", ""), "group": device.get("group")}
                        if detail not in devices:
                            devices.append(detail)
            names = sorted({d["name"] for d in devices if d["name"]})
            out.append({"resource": name, "kind": kind, "label": " / ".join(names) or label, "selector": label,
                        "gpu": kind == "pci" and bool(devices) and all(str(d["class"]).startswith("03") for d in devices),
                        "devices": devices, "nodes": sorted(offered.get(name, []))})
    result = {"resources": out, "sidecar": "Sidecar" in (((((kv or {}).get("spec") or {}).get("configuration") or {})
                                                       .get("developerConfiguration") or {}).get("featureGates") or [])}
    if with_usage:
        import homestead_vm_device_usage as USAGE
        try:
            inventories = [kget(f"/apis/kubevirt.io/v1/{kind}") for kind in ("virtualmachines", "virtualmachineinstances")]
            if any(not isinstance(v.get("items"), list) or (v.get("metadata") or {}).get("continue") for v in inventories):
                raise ValueError("Incomplete VM inventory")
            for row in out:
                row["configured_vms"] = sorted({f"{vm['metadata']['namespace']}/{vm['metadata']['name']}" for vm in inventories[0]["items"]
                    if row["resource"] in USAGE.requests(((vm.get("spec") or {}).get("template") or {}).get("spec") or {})})
                row["active_vms"] = sorted({f"{vmi['metadata']['namespace']}/{vmi['metadata']['name']}" for vmi in inventories[1]["items"]
                    if vmi.get("status", {}).get("phase") not in USAGE.TERMINAL and row["resource"] in USAGE.requests(vmi.get("spec") or {})})
        except Exception:
            result["usage_error"] = "VM device use could not be loaded. Availability will be checked again before Start."
    return result


def vm_devices(vm):
    """A VM's host devices and GPUs, and which have a ROM of Homestead's."""
    tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    devices = ((tspec.get("domain") or {}).get("devices") or {})
    roms = set(json.loads(((((vm.get("spec") or {}).get("template") or {}).get("metadata") or {})
                           .get("annotations") or {}).get("homestead.io/vbios", "[]") or "[]"))
    return [{"name": d.get("name", ""), "resource": d.get("deviceName", ""), "gpu": key == "gpus",
             "rom": d.get("name", "") in roms} for key in ("hostDevices", "gpus") for d in devices.get(key) or []]


def check_rom(data):
    """A ROM's bytes from base64, checked: small enough, and a PCI ROM."""
    try:
        raw = base64.b64decode(str(data), validate=True)
    except Exception:
        raise ValueError("the ROM file could not be read") from None
    if not raw:
        raise ValueError("the ROM file is empty")
    if len(raw) > ROM_LIMIT:
        raise ValueError(f"the ROM is {len(raw) // 1024} KB; up to {ROM_LIMIT // 1024} KB fits (trim any dump header)")
    if raw[:2] != b"\x55\xaa":
        raise ValueError("the file does not start with a PCI ROM signature (55 AA); a dump from GPU-Z or nvflash may "
                         "carry a header to trim first")
    return raw


def hook_script(roms, sidecar_index=0):
    """The onDefineDomain hook: each ROM written where QEMU can read it, and
    named as its device's ROM. On any failure the domain goes on unchanged."""
    table = ",\n".join(f"    {json.dumps(name)}: {json.dumps(base64.b64encode(raw).decode())}" for name, raw in roms.items())
    return f'''#!/usr/bin/env python3
# Homestead: this VM's GPU ROMs (vBIOS), given to QEMU as each device's ROM.
import base64, os, sys
import xml.etree.ElementTree as ET

ROMS = {{
{table}
}}
HOOKS = "/var/run/kubevirt-hooks"
QEMU_HOOKS = "/var/run/kubevirt-hooks/hook-sidecar-{sidecar_index}"


def main():
    args = sys.argv[1:]
    domain = args[args.index("--domain") + 1]
    try:
        ET.register_namespace("qemu", "http://libvirt.org/schemas/domain/qemu/1.0")
        root = ET.fromstring(domain)
        pci = [h for h in root.iter("hostdev") if h.get("type") == "pci"]
        for name, data in ROMS.items():
            target = None
            for h in pci:
                alias = h.find("alias")
                if alias is not None and alias.get("name", "").endswith(name):
                    target = h
            if target is None and len(pci) == 1 and len(ROMS) == 1:
                target = pci[0]
            if target is None:
                continue
            path = os.path.join(HOOKS, "vbios-" + name + ".rom")
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(data))
            os.chmod(path, 0o644)
            for old in target.findall("rom"):
                target.remove(old)
            # Compute mounts the parent of the sidecar's own subdirectory.
            qemu_path = os.path.join(QEMU_HOOKS, os.path.basename(path))
            ET.SubElement(target, "rom", {{"bar": "on", "file": qemu_path}})
        sys.stdout.write(ET.tostring(root, encoding="unicode"))
    except Exception as error:
        sys.stderr.write("homestead vbios hook: " + str(error) + "\\n")
        sys.stdout.write(domain)


main()
'''


def edit_vm(vm, ns, cfg, effects, current_roms=None):
    """Host devices added and removed, and ROMs set or cleared, on vm.
    Returns True when anything changed. The ROMs' ConfigMap is an effect,
    written just before the VM itself."""
    tspec = vm["spec"]["template"]["spec"]
    devices = tspec["domain"].setdefault("devices", {})
    hostdevs = list(devices.get("hostDevices") or [])
    changed = False
    remove = set(cfg.get("remove") or [])
    if remove:
        before = len(hostdevs)
        hostdevs = [d for d in hostdevs if d.get("name") not in remove]
        gpus = [d for d in devices.get("gpus") or [] if d.get("name") not in remove]
        changed |= before != len(hostdevs) or len(gpus) != len(devices.get("gpus") or [])
        if devices.get("gpus") is not None:
            devices["gpus"] = gpus
    names = {d.get("name") for d in hostdevs} | {d.get("name") for d in devices.get("gpus") or []}
    offered = resources()["resources"]
    known = {r["resource"] for r in offered}
    kinds = {r["resource"]: r.get("kind") for r in offered}
    for name, resource in (cfg.get("map") or {}).items():
        if name not in names:
            raise ValueError(f"{name} is not one of this VM's devices")
        if not isinstance(resource, str) or resource not in known:
            raise ValueError(f"{resource or 'that device'} is not a device VMs may use; hand it over from its host first")
        if kinds.get(resource) == "usb" and any(d.get("name") == name for d in devices.get("gpus") or []):
            raise ValueError(f"GPU {name} needs a PCI device")
        for device in hostdevs + list(devices.get("gpus") or []):
            if device.get("name") == name and device.get("deviceName") != resource:
                device["deviceName"] = resource
                changed = True
    for add in cfg.get("add") or []:
        resource = str(add.get("resource") or "")
        if resource not in known:
            raise ValueError(f"{resource or 'that device'} is not a device VMs may use; hand it over from its host first")
        index = 0
        while f"hostdev-{index}" in names:
            index += 1
        name = add.get("name") or f"hostdev-{index}"
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", name) or name in names:
            raise ValueError("Each passthrough device needs a unique valid name")
        names.add(name)
        hostdevs.append({"name": name, "deviceName": resource})
        changed = True
    if hostdevs:
        devices["hostDevices"] = hostdevs
    else:
        devices.pop("hostDevices", None)
    # ROMs: kept per device name in the VM's own ConfigMap.
    meta = vm["spec"]["template"].setdefault("metadata", {})
    annotations = meta.setdefault("annotations", {})
    have = set(json.loads(annotations.get("homestead.io/vbios", "[]") or "[]"))
    roms = dict(current_roms or {})
    for name, data in (cfg.get("roms") or {}).items():
        if name not in names:
            raise ValueError(f"{name} is not one of this VM's devices")
        if data:
            resource = next(d.get("deviceName") for d in hostdevs + list(devices.get("gpus") or []) if d.get("name") == name)
            if kinds.get(resource) == "usb":
                raise ValueError("USB devices cannot use a vBIOS file; clear the ROM or choose a PCI device")
            roms[name] = check_rom(data)
        else:
            roms.pop(name, None)
        changed = True
    for name in list(roms):
        if name not in names:
            roms.pop(name)
        elif kinds.get(next(d.get("deviceName") for d in hostdevs + list(devices.get("gpus") or []) if d.get("name") == name)) == "usb":
            raise ValueError("USB devices cannot use a vBIOS file; clear the ROM or choose a PCI device")
    if set(roms) != have or cfg.get("roms"):
        cm = f"{vm['metadata']['name']}-vbios"
        path = f"/api/v1/namespaces/{ns}/configmaps/{cm}"
        sidecars = [s for s in json.loads(annotations.get(HOOK_ANNOTATION, "[]") or "[]")
                    if (s.get("configMap") or {}).get("name") != cm]
        if roms:
            body = {"apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": cm, "namespace": ns, "labels": {"homestead.io/managed": "true",
                                                                           "app": vm["metadata"]["name"]}},
                    "data": {HOOK_KEY: hook_script(roms, len(sidecars))}}
            if vm["metadata"].get("uid"):
                body["metadata"]["ownerReferences"] = [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
                    "name": vm["metadata"]["name"], "uid": vm["metadata"]["uid"]}]
            if len(json.dumps(body["data"]).encode()) > 1024 * 1024:
                raise ValueError("The combined vBIOS files exceed the ConfigMap's 1 MiB limit; use smaller ROM files")
            effects.append({"kind": "configmap", "path": path, "body": body})
            sidecars.append({"args": ["--version", "v1alpha2"],
                             "configMap": {"name": cm, "key": HOOK_KEY, "hookPath": "/usr/bin/onDefineDomain"}})
            annotations["homestead.io/vbios"] = json.dumps(sorted(roms))
            effects.append({"kind": "kubevirt-gates", "gates": ["Sidecar"]})
        else:
            effects.append({"kind": "configmap", "path": path, "body": None})
            annotations.pop("homestead.io/vbios", None)
        if sidecars:
            annotations[HOOK_ANNOTATION] = json.dumps(sidecars)
        else:
            annotations.pop(HOOK_ANNOTATION, None)
        changed = True
    if not annotations:
        meta.pop("annotations", None)
    return changed


def current_roms(vm, ns, replaced=()):
    """The ROMs already in a VM's ConfigMap, so a save keeps the ones not changed."""
    if not any(d["rom"] for d in vm_devices(vm)):
        return {}
    cm = _get(f"/api/v1/namespaces/{ns}/configmaps/{vm['metadata']['name']}-vbios")
    return roms_from_configmap(vm, cm, replaced)


def roms_from_configmap(vm, cm, replaced=()):
    """Refuse a missing or incomplete managed ROM instead of silently losing it."""
    script = ((cm or {}).get("data") or {}).get(HOOK_KEY, "")
    replaced = set(replaced)
    roms = {name: check_rom(data) for name, data in re.findall(r'^\s+"([^"]+)": "([A-Za-z0-9+/=]+)"', script, re.M) if name not in replaced}
    expected = {d["name"] for d in vm_devices(vm) if d["rom"]}
    if set(roms) != expected - replaced:
        raise ValueError("The VM's vBIOS ConfigMap is missing or incomplete; restore its ROM files before editing or transferring devices")
    return roms


def write_configmap(effect, send):
    """The ROMs' ConfigMap made, replaced or removed."""
    path, body = effect["path"], effect["body"]
    existing = _get(path)
    exists = existing is not None
    identity = {key: (existing.get("metadata") or {}).get(key) for key in ("uid", "resourceVersion")} if exists else None
    if "identity" in effect and identity != effect["identity"]:
        raise ValueError("The vBIOS ConfigMap changed; review the edit again")
    if body is None:
        if exists:
            # Save's journal never deletes dependencies. Clearing the data
            # removes the ROM while retaining an inspectable, reusable object.
            cleared = copy.deepcopy(existing)
            cleared["data"] = {}
            cleared.pop("binaryData", None)
            send("PUT", path, cleared)
        return
    if exists:
        body = copy.deepcopy(body)
        body["metadata"].update(identity)
        for key in ("ownerReferences",):
            if (existing.get("metadata") or {}).get(key):
                body["metadata"][key] = existing["metadata"][key]
        send("PUT", path, body)
    else:
        send("POST", path.rsplit("/", 1)[0], body)
