#!/usr/bin/env python3
"""Read host sensors, devices and disk counters and serve them as JSON."""
import json, os, socket, stat, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
            out.append({"chip": chip, "name": label, "celsius": c})
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
        out.append({"name": name, "kind": kind, "up": _read(f"{path}/operstate") in ("up", "unknown"),
                    "master": master})
    return out

def default_interface():
    """The interface the host's default route leaves by - where it meets the
    LAN - from the route table of the host's first process."""
    raw = _read(f"{PROC}/1/net/route") or ""
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 7 and parts[1] == "00000000" and parts[7] == "00000000":
            return parts[0]
    return ""

def payload():
    t, hw = thermal(), hwmon()
    allt = [x["celsius"] for x in t] + [x["celsius"] for x in hw]
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
            "default_interface": default_interface()}

class H(BaseHTTPRequestHandler):
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
    ThreadingHTTPServer(("0.0.0.0", 9099), H).serve_forever()
