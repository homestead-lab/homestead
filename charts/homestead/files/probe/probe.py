#!/usr/bin/env python3
"""Read host sensors, devices and disk counters and serve them as JSON."""
import json, os, platform, re, socket, stat, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from homestead_http import BoundedHTTPServer, LimitedHandler

SYS = "/host/sys"
DEV = "/host/dev"
PROC = "/host/proc"
_DISK_PREV = {}
_DISK_LOCK = threading.Lock()

# USB devices worth naming, so "Coral" shows up as Coral rather than a hex pair
KNOWN_USB = {
    ("1a6e", "089a"): "Google Coral TPU (unflashed)",
    ("18d1", "9302"): "Google Coral TPU",
    ("0403", "6001"): "FTDI serial",
    ("10c4", "ea60"): "CP210x serial",
    ("1cf1", "0030"): "ConBee/deCONZ Zigbee",
    ("0451", "16a8"): "TI CC2531 Zigbee",
}

def _read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return None

def thermal():
    out = []
    base = os.path.join(SYS, "class/thermal")
    if not os.path.isdir(base):
        return out
    for z in sorted(os.listdir(base)):
        if not z.startswith("thermal_zone"):
            continue
        t = _read(os.path.join(base, z, "temp"))
        if t is None:
            continue
        try:
            c = round(int(t) / 1000.0, 1)
        except ValueError:
            continue
        if not (-50 < c < 200):      # ignore obviously bogus zones
            continue
        out.append({"name": _read(os.path.join(base, z, "type")) or z,
                    "zone": z, "celsius": c})
    return out

def hwmon():
    out = []
    base = os.path.join(SYS, "class/hwmon")
    if not os.path.isdir(base):
        return out
    for h in sorted(os.listdir(base)):
        d = os.path.join(base, h)
        chip = _read(os.path.join(d, "name")) or h
        try:
            files = os.listdir(d)
        except Exception:
            continue
        for f in sorted(files):
            if not (f.startswith("temp") and f.endswith("_input")):
                continue
            raw = _read(os.path.join(d, f))
            if raw is None:
                continue
            try:
                c = round(int(raw) / 1000.0, 1)
            except ValueError:
                continue
            if not (-50 < c < 200):
                continue
            label = _read(os.path.join(d, f.replace("_input", "_label"))) or f[:-6]
            try:
                device = os.path.basename(os.path.realpath(os.path.join(d, "device"))) if os.path.exists(os.path.join(d, "device")) else ""
            except OSError:
                device = ""
            out.append({"chip": chip, "name": label, "celsius": c, "device": device})
    return out

def devices():
    """What hardware this host actually has. Used to decide whether a
    workload that binds a device can legally run here."""
    out = {"dri": [], "apex": [], "video": [], "tty": [], "usb": [],
           "paths": [], "path_entries": []}
    try:
        entries = os.listdir(DEV)
    except Exception:
        entries = []
    for e in entries:
        if e.startswith("apex"):
            out["apex"].append(e)               # Coral PCIe/M.2
        elif e.startswith("video"):
            out["video"].append(e)
        elif e.startswith(("ttyUSB", "ttyACM")):
            out["tty"].append(e)
    try:
        out["dri"] = sorted(os.listdir(os.path.join(DEV, "dri")))
    except Exception:
        pass

    # Generic device inventory for user-defined hardware features. Keep the
    # walk shallow and bounded: it covers /dev/dri, /dev/bus/usb,
    # /dev/serial/by-id and ordinary accelerator nodes without dumping an
    # unbounded host filesystem tree.
    try:
        for root, dirs, files in os.walk(DEV, followlinks=False):
            depth = os.path.relpath(root, DEV).count(os.sep)
            if depth >= 2:
                dirs[:] = []
            for entry in sorted(dirs + files):
                full = os.path.join(root, entry)
                rel = os.path.relpath(full, DEV).replace(os.sep, "/")
                path = "/dev/" + rel
                out["paths"].append(path)
                try:
                    mode = os.lstat(full).st_mode
                    typ = ("Directory" if stat.S_ISDIR(mode) else
                           "CharDevice" if stat.S_ISCHR(mode) else
                           "BlockDevice" if stat.S_ISBLK(mode) else
                           "Socket" if stat.S_ISSOCK(mode) else "File")
                except Exception:
                    typ = "File"
                out["path_entries"].append({"path": path, "type": typ})
                if len(out["paths"]) >= 800:
                    break
            if len(out["paths"]) >= 800:
                break
    except Exception:
        pass

    base = os.path.join(SYS, "bus/usb/devices")
    try:
        for d in sorted(os.listdir(base)):
            vid = _read(os.path.join(base, d, "idVendor"))
            pid = _read(os.path.join(base, d, "idProduct"))
            if not vid or not pid:
                continue
            name = (_read(os.path.join(base, d, "product")) or "").strip()
            bus = _read(os.path.join(base, d, "busnum"))
            dev = _read(os.path.join(base, d, "devnum"))
            out["usb"].append({
                "vid": vid, "pid": pid,
                "name": KNOWN_USB.get((vid, pid), name or f"{vid}:{pid}"),
                "known": (vid, pid) in KNOWN_USB,
                "path": (f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}"
                         if bus and dev else ""),
            })
    except Exception:
        pass
    return out

def disk_activity():
    """Return physical whole-disk throughput calculated between requests.

    Linux diskstats sectors are always 512 bytes, independently of a
    device's logical block size. Partitions and virtual devices are left
    out so the node page shows each physical host disk exactly once.
    """
    raw = _read(os.path.join(PROC, "diskstats")) or ""
    now = time.monotonic()
    current = {}
    disks = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 14:
            continue
        name = fields[2]
        block = os.path.join(SYS, "class/block", name)
        # A real disk has a sysfs device link. Partitions have a partition
        # marker; loop, device-mapper and RAM devices have no device link.
        if not os.path.isdir(os.path.join(block, "device")):
            continue
        if os.path.exists(os.path.join(block, "partition")):
            continue
        # An optical or floppy drive is no disk to store anything on: sr0
        # was offered to Longhorn as a 1 GB SSD.
        if name.startswith(("sr", "fd")) or _read(os.path.join(block, "device/type")) == "5":
            continue
        try:
            sectors_read = int(fields[5])
            sectors_written = int(fields[9])
            size_sectors = int(_read(os.path.join(block, "size")) or 0)
        except (TypeError, ValueError):
            continue
        model = (_read(os.path.join(block, "device/model")) or "").strip()
        vendor = (_read(os.path.join(block, "device/vendor")) or "").strip()
        # Harvester/Longhorn iSCSI attachments also have a sysfs device
        # link. They are workload volumes, not node hardware, and can
        # appear/disappear as workloads move.
        if vendor.upper() in ("IET", "LIO-ORG") or "VIRTUAL-DISK" in model.upper():
            continue
        current[name] = (now, sectors_read, sectors_written)
        serial = (_read(os.path.join(block, "device/serial")) or "").strip()
        rotational = _read(os.path.join(block, "queue/rotational")) == "1"
        kind = "NVMe" if name.startswith("nvme") else ("HDD" if rotational else "SSD")
        disks.append({
            "name": name,
            "model": " ".join(x for x in (vendor, model) if x) or name,
            "serial": serial,
            "kind": kind,
            "size_gb": round(size_sectors * 512 / (1024 ** 3), 1),
            "read_sectors": sectors_read,
            "write_sectors": sectors_written,
        })

    with _DISK_LOCK:
        previous = dict(_DISK_PREV)
        _DISK_PREV.clear()
        _DISK_PREV.update(current)

    for disk in disks:
        before = previous.get(disk["name"])
        read_mbps = write_mbps = 0.0
        if before and now > before[0]:
            seconds = now - before[0]
            read_mbps = max(0, disk["read_sectors"] - before[1]) * 512 / seconds / 1_000_000
            write_mbps = max(0, disk["write_sectors"] - before[2]) * 512 / seconds / 1_000_000
        disk["read_mbps"] = round(read_mbps, 3)
        disk["write_mbps"] = round(write_mbps, 3)
        del disk["read_sectors"]
        del disk["write_sectors"]
    return sorted(disks, key=lambda d: d["name"])

def physical_disk(device, depth=0):
    """The first physical disk under a stacked block device (dm-*, md*),
    following /sys/class/block/<dev>/slaves; "" when there is none."""
    if depth > 8:
        return ""
    try:
        below = sorted(os.listdir(os.path.join(SYS, "class/block", device, "slaves")))
    except OSError:
        below = []
    for name in below:
        if name.startswith(("dm-", "md")):
            found = physical_disk(name, depth + 1)
            if found:
                return found
            continue
        if os.path.exists(os.path.join(SYS, "class/block", name, "partition")):
            return os.path.basename(os.path.dirname(os.path.realpath(os.path.join(SYS, "class/block", name))))
        return name
    return ""

def mounts():
    """Which disk each host filesystem lives on, from PID 1's mount table:
    lets Homestead tell which physical disk a Longhorn disk folder is on,
    and which disk the system runs from."""
    out, seen = [], set()
    raw = _read(os.path.join(PROC, "1/mountinfo")) or ""
    for line in raw.splitlines():
        left, _, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        majmin, mountpoint, fstype, source = fields[2], fields[4], tail[0], tail[1]
        if majmin.startswith("0:"):
            continue                     # tmpfs, overlay, proc: no disk behind them
        try:
            target = os.readlink(os.path.join(SYS, "dev/block", majmin))
        except OSError:
            continue
        # .../block/sda/sda1 for SATA, .../nvme0/nvme0n1/nvme0n1p1 for NVMe:
        # the device is the last part, and a partition's disk the one before.
        parts = [x for x in target.split("/") if x]
        device = parts[-1] if parts else ""
        partition = os.path.exists(os.path.join(SYS, "class/block", device, "partition"))
        disk = parts[-2] if partition and len(parts) > 1 else device
        # LVM, dm-crypt and RAID sit on a device-mapper (dm-0) or md device,
        # not a disk: follow what it is built on down to the physical disk, so
        # a root filesystem on Ubuntu's LVM is known to be on its NVMe.
        if device.startswith(("dm-", "md")):
            disk = physical_disk(device) or disk
        after = [disk, device]
        key = (disk, mountpoint)
        if not disk or key in seen or mountpoint.startswith(("/var/lib/kubelet/pods", "/run/", "/proc", "/sys")):
            continue
        seen.add(key)
        out.append({"disk": disk, "device": after[-1], "mountpoint": mountpoint.replace("\040", " "),
                    "fstype": fstype, "source": source})
        if len(out) >= 200:
            break
    return out

def uptime():
    """Seconds since the host booted, from its own /proc."""
    raw = _read(f"{PROC}/uptime")
    try:
        return round(float(raw.split()[0])) if raw else None
    except (ValueError, IndexError):
        return None

# Interfaces the pod network makes for itself, which no LAN network rides on.
POD_IFACES = ("lo", "veth", "cni", "flannel", "cali", "vxlan", "kube", "docker", "tunl", "genev",
              "lxc", "tap", "vnet", "cilium", "weave", "nodelocaldns", "k6t", "virbr")

def interfaces():
    """The host's own network interfaces a LAN network can sit on: NICs,
    bridges, bonds and VLANs, with the bridge each NIC is in. Read from the
    host's /sys, whose network entries are the host's, not this pod's."""
    base = f"{SYS}/class/net"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith(POD_IFACES):
            continue
        path = f"{base}/{name}"
        if os.path.isdir(f"{path}/bridge"):
            kind = "bridge"
        elif os.path.isdir(f"{path}/bonding"):
            kind = "bond"
        elif os.path.exists(f"{path}/device"):
            kind = "nic"
        elif "." in name:
            kind = "vlan"
        else:
            continue
        master = os.path.basename(os.path.realpath(f"{path}/master")) if os.path.exists(f"{path}/master") else ""
        row = {"name": name, "kind": kind, "up": _read(f"{path}/operstate") in ("up", "unknown"),
               "master": master}
        row.update(port_facts(path, kind))
        out.append(row)
    return out

# Counters a port keeps from boot; a rise in an hour is a cable, a port or a
# driver going bad. carrier_changes sits beside statistics, not in it.
PORT_COUNTERS = ("rx_errors", "tx_errors", "rx_crc_errors", "rx_dropped", "tx_dropped")

def _int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None

def port_facts(path, kind):
    """What a port says of its link, all from world-readable sysfs: carrier,
    negotiated speed and duplex, MTU, MAC, driver, its error counters, and a
    bond's or bond member's state. Nothing is written. carrier and speed
    cannot be read while the interface is down by choice (admin-down), so
    those read as None: off, not broken."""
    try:
        flags = int(_read(f"{path}/flags") or "", 16)
    except ValueError:
        flags = None
    carrier = _read(f"{path}/carrier")
    speed = _int(_read(f"{path}/speed"))
    driver = os.path.realpath(f"{path}/device/driver") if os.path.exists(f"{path}/device/driver") else ""
    row = {"admin_up": bool(flags & 1) if flags is not None else None,
           "carrier": None if carrier is None else carrier == "1",
           # -1 or 2^32-1 when a driver cannot say (virtual NICs, no link).
           "speed_mbps": speed if speed and 0 < speed < 4000000 else None,
           "duplex": _read(f"{path}/duplex") if carrier == "1" else None,
           "mtu": _int(_read(f"{path}/mtu")), "mac": _read(f"{path}/address") or "",
           "driver": os.path.basename(driver) if driver else "",
           "counters": {key: _int(_read(f"{path}/statistics/{key}")) for key in PORT_COUNTERS}}
    row["counters"]["carrier_changes"] = _int(_read(f"{path}/carrier_changes"))
    if kind == "bond":
        bonding = f"{path}/bonding"
        partner = _read(f"{bonding}/ad_partner_mac")
        row["bond"] = {"mode": (_read(f"{bonding}/mode") or "").split(" ")[0],
                       "slaves": (_read(f"{bonding}/slaves") or "").split(),
                       "active_slave": _read(f"{bonding}/active_slave") or "",
                       "mii_status": _read(f"{bonding}/mii_status") or "",
                       "miimon": _int(_read(f"{bonding}/miimon")),
                       "primary": (_read(f"{bonding}/primary") or "").split(" ")[0],
                       # 802.3ad only: all zeros until the switch answers LACP.
                       "ad_partner_mac": partner or None}
    member = f"{path}/bonding_slave"
    if os.path.isdir(member):
        row["bond_member"] = {"state": _read(f"{member}/state") or "",
                              "mii_status": _read(f"{member}/mii_status") or "",
                              "link_failure_count": _int(_read(f"{member}/link_failure_count")),
                              "aggregator": _int(_read(f"{member}/ad_aggregator_id"))}
    return row

# Kernel modules Longhorn's V2 (SPDK) engine needs on a host.
V2_MODULES = ("vfio_pci", "uio_pci_generic", "nvme_tcp")

def v2_facts():
    """What Longhorn's V2 engine asks of a host that the host's /proc and
    /sys can say: an x86 CPU with SSE4.2 (arm64 needs none), and its kernel
    modules - loaded, or built in, both show under /sys/module."""
    flags = None
    for line in (_read(f"{PROC}/cpuinfo") or "").splitlines():
        if line.startswith("flags"):
            flags = line.split(":", 1)[-1].split()
            break
    return {"arch": platform.machine(), "sse4_2": ("sse4_2" in flags) if flags is not None else None,
            "modules": {name: os.path.isdir(f"{SYS}/module/{name}") for name in V2_MODULES}}

def nfs_facts():
    filesystems = _read(f"{PROC}/filesystems")
    if filesystems is None:
        return {"server": None, "client": None}
    names = {line.split()[-1] for line in filesystems.splitlines() if line.split()}
    return {"server": "nfsd" in names, "client": bool(names & {"nfs", "nfs4"})}

def default_interface():
    """The interface the host's default route leaves by - where it meets the
    LAN - from the route table of the host's first process."""
    raw = _read(f"{PROC}/1/net/route") or ""
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 7 and parts[1] == "00000000" and parts[7] == "00000000":
            return parts[0]
    return ""


def _cpu_list(raw):
    """Bounded Linux CPU/node-list parser; missing is not an empty set."""
    if not isinstance(raw, str) or len(raw) > 65536:
        raise ValueError()
    result = set()
    for item in raw.split(",") if raw else []:
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", item):
            raise ValueError()
        ends = [int(part) for part in item.split("-")]
        first, last = ends[0], ends[-1]
        if not 0 <= first <= last < 8192:
            raise ValueError()
        values = set(range(first, last + 1))
        if result & values:
            raise ValueError()
        result.update(values)
    return result


def numa_facts():
    """Physical topology only: never claim online CPUs are allocatable CPUs.

    Existing read-only /sys and /proc mounts suffice. No kubelet credentials,
    checkpoint writes, hotplug, hugepage allocation or privileged calls.
    Per-node free pages include globally reserved pages; report reservations
    separately instead of inventing a NUMA-local available count.
    """
    result = {"schema": 1, "complete": False, "cells": [], "page_pools": {},
              "node": os.environ.get("NODE_NAME", socket.gethostname()),
              "sampled_at": time.time(), "boot_id": None,
              "cpu_allocation": "unverified", "reason": "Host NUMA evidence is incomplete"}
    boot_path = f"{PROC}/sys/kernel/random/boot_id"
    online_path = f"{SYS}/devices/system/cpu/online"
    nodes_path = f"{SYS}/devices/system/node/online"
    try:
        boot = _read(boot_path)
        if not isinstance(boot, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", boot):
            raise ValueError()
        online_raw, nodes_raw = _read(online_path), _read(nodes_path)
        online, nodes = _cpu_list(online_raw), _cpu_list(nodes_raw)
        if not online or not nodes or len(nodes) > 256:
            raise ValueError()
        covered, cells, memberships, pools = set(), [], {}, {}

        def counter(path):
            value = _read(path)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
                raise ValueError()
            return int(value)

        for node in sorted(nodes):
            base = f"{SYS}/devices/system/node/node{node}"
            memberships[node] = _read(f"{base}/cpulist")
            cpus = _cpu_list(memberships[node]) & online
            if covered & cpus:
                raise ValueError()
            covered.update(cpus)
            cores, seen = [], set()
            for cpu in sorted(cpus):
                siblings = _cpu_list(_read(f"{SYS}/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")) & online
                if cpu not in siblings or not siblings <= cpus:
                    raise ValueError()
                if cpu not in seen:
                    if seen & siblings:
                        raise ValueError()
                    cores.append(sorted(siblings))
                    seen.update(siblings)
                elif not any(set(core) == siblings for core in cores):
                    raise ValueError()
            page_dir = f"{base}/hugepages"
            names = sorted(os.listdir(page_dir))
            if len(names) > 32:
                raise ValueError()
            pages = {}
            for name in names:
                match = re.fullmatch(r"hugepages-([0-9]{1,12})kB", name)
                if not match or int(match[1]) == 0:
                    raise ValueError()
                size = str(int(match[1]) * 1024)
                row = {key: counter(f"{page_dir}/{name}/{file}") for key, file in
                       (("total", "nr_hugepages"), ("free", "free_hugepages"), ("surplus", "surplus_hugepages"))}
                if row["free"] > row["total"] + row["surplus"]:
                    raise ValueError()
                pages[size] = row
                if size not in pools:
                    pools[size] = {"reserved": counter(f"{SYS}/kernel/mm/hugepages/{name}/resv_hugepages")}
            cells.append({"id": node, "online_cpus": sorted(cpus), "cores": cores, "hugepages": pages})
        # Hotplug/reboot during collection invalidates the topology. Counters
        # may still change immediately after sampling: this is no reservation.
        if (covered != online or _read(online_path) != online_raw or _read(nodes_path) != nodes_raw or
                _read(boot_path) != boot or any(_read(f"{SYS}/devices/system/node/node{node}/cpulist") != raw
                                                for node, raw in memberships.items())):
            raise ValueError()
        result.update(complete=True, boot_id=boot, cells=cells, page_pools=pools,
                      reason="Physical snapshot only; dedicated CPU ownership and NUMA-local page reservations are unverified")
    except (ValueError, OSError, TypeError, OverflowError):
        # Do not turn missing, partial or malformed data into zero/free capacity.
        pass
    return result

# What a sensor is, in words: chips and thermal zones name their driver.
SENSOR_KINDS = (("coretemp", "CPU"), ("k10temp", "CPU"), ("zenpower", "CPU"), ("cpu_thermal", "CPU"),
                ("cpu-thermal", "CPU"), ("x86_pkg_temp", "CPU"), ("soc_thermal", "CPU"), ("nvme", "NVMe"),
                ("drivetemp", "Drive"), ("amdgpu", "GPU"), ("radeon", "GPU"), ("nouveau", "GPU"), ("i915", "GPU"),
                ("acpitz", "Motherboard"), ("pch_", "Chipset"), ("iwlwifi", "Wi-Fi"), ("mt79", "Wi-Fi"),
                ("r8169", "Network"), ("igc", "Network"), ("ixgbe", "Network"), ("bnxt", "Network"))


def sensor_label(chip, name="", device=""):
    """A sensor as people would say it: "CPU Package id 0", "NVMe nvme0"."""
    chip = chip or ""
    kind = next((word for prefix, word in SENSOR_KINDS if chip.lower().startswith(prefix)), chip)
    if kind == "NVMe" and re.fullmatch(r"nvme\d+", device or ""):
        return f"NVMe {device}"
    name = name or ""
    if not name or name.lower() in ("composite", chip.lower()) or re.fullmatch(r"temp\d+", name):
        return kind
    if kind == "CPU" and name.lower().startswith(("package", "tctl", "tdie")):
        return "CPU package"
    return f"{kind} {name}"


def cpu_model():
    """The host's processor, as /proc/cpuinfo names it; this is not namespaced."""
    text = _read("/proc/cpuinfo") or ""
    for key in ("model name", "Model", "Hardware", "cpu model"):
        match = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", text, re.M)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()[:120]
    return ""


def payload():
    t, hw = thermal(), hwmon()
    allt = [x["celsius"] for x in t] + [x["celsius"] for x in hw]
    # Which sensor the hottest reading is: a number alone says nothing of where.
    readings = [(x["celsius"], sensor_label(x["name"])) for x in t] +                [(x["celsius"], sensor_label(x["chip"], x["name"], x.get("device", ""))) for x in hw]
    hottest = max(readings, key=lambda r: r[0])[1] if readings else ""
    # prefer a package/core sensor for the headline number
    pkg = next((x["celsius"] for x in hw
                if "package" in (x["name"] or "").lower()
                or "tctl" in (x["name"] or "").lower()), None)
    if pkg is None:
        pkg = next((x["celsius"] for x in t
                    if "x86_pkg" in (x["name"] or "").lower()), None)
    return {"node": os.environ.get("NODE_NAME", socket.gethostname()),
            "cpu_c": pkg if pkg is not None else (max(allt) if allt else None),
            "max_c": max(allt) if allt else None,
            "max_source": hottest,
            "cpu_model": cpu_model(),
            "thermal": t, "hwmon": hw,
            "sensors": len(t) + len(hw),
            "devices": devices(),
            "disks": disk_activity(),
            "mounts": mounts(),
            "uptime_s": uptime(),
            # Hardware virtualisation, which KubeVirt runs VMs with; without
            # it KubeVirt can only emulate, many times slower.
            "kvm": os.path.exists(f"{DEV}/kvm"),
            "interfaces": interfaces(),
            "default_interface": default_interface(),
            "v2": v2_facts(), "nfs": nfs_facts(), "numa": numa_facts()}

class H(LimitedHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps(payload()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

if __name__ == "__main__":
    # PID 1 in its container ignores SIGTERM unless it says otherwise, so a
    # host shutting down would wait out its whole stop timeout for this.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    BoundedHTTPServer(("0.0.0.0", 9099), H, max_connections=8).serve_forever()
