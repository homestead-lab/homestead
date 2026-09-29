"""Setting a new disk up for Longhorn on k3s or RKE2, as Harvester does on
Harvester: look at it, format it if it is blank, mount it the safe way, and
hand it to Longhorn.

Longhorn stores a V1 disk's data in a folder, so the disk must be formatted
and mounted on the host first - which used to be a list of commands to run by
hand. Homestead does it through a short-lived helper on the host
(homestead_hostrun), in two steps:

inspect() only looks. It reads the disk's size, partitions and filesystem,
whether it is the system disk or mounted, and - mounting it read-only for a
moment, without replaying a journal - whether it already holds Longhorn's
data (longhorn-disk.cfg) or anything else.

setup() then does one of: format a blank disk (ext4 or XFS) - or, with the
device's path typed to confirm, erase one holding something else; or keep a
disk that already holds Longhorn data and mount it as it is. The mount is the
one a dead drive cannot hurt: the empty folder is made immutable, so nothing
lands on the system disk in its place; fstab names the filesystem by UUID,
with nofail so the host still starts without it, a copy of fstab kept first;
and the mount is checked to be this device before Longhorn is told. A V2
disk is not formatted: Longhorn is given the raw device by its stable
/dev/disk/by-id name. The system disk, and a disk that is mounted, are
refused.

A disk something else holds open is found first, from the kernel's holders,
where wipefs could only say "Device or resource busy". Ubuntu runs
multipathd, which claims every plain SCSI or SATA disk it sees as a map of
its own; setting such a disk up releases the map and adds the disk's WWID to
multipath.conf's blacklist, so multipathd leaves that one disk alone and
nothing else changes. A disk in an LVM volume group, a RAID array or an
encrypted volume is refused, naming what holds it.
"""
import re

hostrun = None          # homestead_hostrun
DEVICE = re.compile(r"/dev/(?:[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)")
SYSTEM_POINTS = {"/", "/boot", "/boot/efi", "/usr", "/var", "/var/lib/rancher", "/var/lib/kubelet", "/home"}
FSTYPES = ("ext4", "xfs")


def bind(_hostrun):
    global hostrun
    hostrun = _hostrun


def _device(value):
    device = str(value or "").strip()
    if not DEVICE.fullmatch(device):
        raise ValueError("give the whole disk's device, like /dev/sdb or /dev/nvme0n1")
    return device


def mount_point(device):
    return "/mnt/" + device.rsplit("/", 1)[-1]


def inspect_script(device):
    # Only reads. A filesystem is mounted read-only, without replaying its
    # journal, into a temporary folder, and unmounted again.
    return f"""D={device}
[ -b "$D" ] || {{ echo "ERR $D is not a block device on this host"; exit 0; }}
echo "SIZE $(blockdev --getsize64 "$D" 2>/dev/null || echo 0)"
lsblk -rno NAME,TYPE,FSTYPE,MOUNTPOINT "$D" 2>/dev/null | while read -r n t f m; do echo "PART $n|$t|$f|$m"; done
FS=$(blkid -s TYPE -o value "$D" 2>/dev/null)
echo "FS $FS"
echo "UUID $(blkid -s UUID -o value "$D" 2>/dev/null)"
for l in /dev/disk/by-id/*; do case "$l" in *-part*) continue;; esac; [ "$(readlink -f "$l")" = "$D" ] && {{ echo "BYID $l"; break; }}; done
# What holds it or its partitions open: a multipath map, LVM, RAID, dm-crypt.
B=${{D##*/}}
for h in /sys/block/$B/holders/* /sys/block/$B/$B*/holders/*; do
  [ -e "$h" ] || continue; n=${{h##*/}}
  echo "HOLDER $n|$(cat /sys/block/$n/dm/name 2>/dev/null)|$(cat /sys/block/$n/dm/uuid 2>/dev/null)"
done
if [ -n "$FS" ] && ! grep -q "^$D " /proc/mounts; then
  case "$FS" in ext*) O=ro,noload;; xfs) O=ro,norecovery;; *) O=ro;; esac
  T=$(mktemp -d)
  if mount -o "$O" "$D" "$T" 2>/dev/null; then
    [ -f "$T/longhorn-disk.cfg" ] && echo "LONGHORN $(tr -d '\\n' < "$T/longhorn-disk.cfg")"
    [ -d "$T/replicas" ] && echo "REPLICAS $(ls "$T/replicas" 2>/dev/null | wc -l)"
    echo "ENTRIES $(ls -A "$T" | grep -v '^lost+found$' | wc -l)"
    umount "$T"
  fi
  rmdir "$T"
fi
for c in mkfs.ext4 mkfs.xfs wipefs chattr findmnt; do command -v $c >/dev/null && echo "TOOL $c"; done
echo END"""


def parse(device, out):
    facts = {"device": device, "size_gb": 0, "partitions": [], "mounts": [], "fstype": "", "uuid": "",
             "by_id": "", "longhorn": None, "replicas": 0, "entries": 0, "tools": [], "error": "",
             "holders": []}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "ERR":
            facts["error"] = value
        elif key == "SIZE":
            facts["size_gb"] = round(int(value or 0) / 1024 ** 3, 1) if value.isdigit() else 0
        elif key == "PART":
            name, kind, fstype, mount = (value.split("|") + ["", "", "", ""])[:4]
            if kind == "part":
                facts["partitions"].append({"name": name, "fstype": fstype, "mount": mount})
            if mount:
                facts["mounts"].append(mount.replace("\\x20", " "))
        elif key == "FS":
            facts["fstype"] = value
        elif key == "UUID":
            facts["uuid"] = value
        elif key == "BYID":
            facts["by_id"] = value
        elif key == "LONGHORN":
            facts["longhorn"] = value[:300]
        elif key == "REPLICAS":
            facts["replicas"] = int(value) if value.isdigit() else 0
        elif key == "ENTRIES":
            facts["entries"] = int(value) if value.isdigit() else 0
        elif key == "TOOL":
            facts["tools"].append(value)
        elif key == "HOLDER":
            name, dm_name, uuid = (value.split("|") + ["", "", ""])[:3]
            kind = ("multipath" if uuid.startswith("mpath-") else "LVM" if uuid.startswith("LVM-")
                    else "encryption" if uuid.startswith("CRYPT-") else "RAID" if name.startswith("md")
                    else "device-mapper")
            facts["holders"].append({"name": dm_name or name, "kind": kind,
                                     "wwid": uuid[6:] if kind == "multipath" else ""})
    if "END" not in out.split() and not facts["error"]:
        facts["error"] = "the host did not finish looking at the disk"
    facts["system"] = any(m in SYSTEM_POINTS or m.startswith("/boot") for m in facts["mounts"])
    held = [h for h in facts["holders"] if h["kind"] != "multipath"]
    facts["multipath"] = [h for h in facts["holders"] if h["kind"] == "multipath" and h["wwid"]]
    facts["state"] = ("missing" if facts["error"] else "system" if facts["system"]
                      else "mounted" if facts["mounts"] else "held" if held
                      else "partitioned" if facts["partitions"]
                      else "longhorn" if facts["longhorn"] else "data" if facts["fstype"] else "blank")
    return facts


def inspect(node, device):
    device = _device(device)
    out, err = hostrun.run(node, inspect_script(device), timeout=60)
    facts = parse(device, out)
    if not out.strip() and err:
        facts.update(error=err[:300], state="missing")
    facts["mount_point"] = mount_point(device)
    facts["choices"] = choices(facts)
    return facts


def choices(facts):
    """What can be done with it, in the order offered."""
    state = facts["state"]
    if state in ("missing", "system", "mounted", "held"):
        return []
    if state == "blank":
        return ["format"]
    if state == "longhorn":
        return ["import", "erase"]
    return ["erase"]


SAFE_NAME = re.compile(r"[A-Za-z0-9_.:-]+")


def release_script(maps):
    """Let go of multipath maps over the disk and keep multipathd off it: its
    WWID in the blacklist, not every sd device, so a disk that really is
    multipathed stays as it is."""
    lines = ['command -v multipath >/dev/null || { echo "ERR multipathd holds $D but the multipath tool is missing"; exit 1; }',
             'C=/etc/multipath.conf', '[ -f "$C" ] && cp "$C" "$C.homestead-backup"',
             'grep -qs "^blacklist *{" "$C" || printf \'blacklist {\\n}\\n\' >> "$C"']
    for row in maps:
        wwid, name = row["wwid"], row["name"]
        if not SAFE_NAME.fullmatch(wwid) or not SAFE_NAME.fullmatch(name):
            raise ValueError(f"multipath map {name!r} has a name Homestead will not put in a command")
        lines.append(f'grep -qs \'wwid "{wwid}"\' "$C" || sed -i \'/^blacklist *{{/a\\    wwid "{wwid}"\' "$C"')
    lines.append("multipathd reconfigure >/dev/null 2>&1 || systemctl restart multipathd 2>/dev/null || true")
    lines += [f"multipath -f {row['name']} >/dev/null 2>&1 || true" for row in maps]
    lines.append('B=${D##*/}; ls /sys/block/$B/holders/ 2>/dev/null | grep -q . && '
                 '{ echo "ERR multipathd still holds $D after releasing it; see multipath -ll on the host"; exit 1; }')
    return "\n".join(lines) + "\n"


def setup_script(device, mode, fstype, point, multipath=()):
    label = ("hs-" + device.rsplit("/", 1)[-1])[:12]
    make = release_script(multipath) if multipath and mode in ("format", "erase") else ""
    if mode in ("format", "erase"):
        mkfs = f'mkfs.ext4 -F -L {label} "$D"' if fstype == "ext4" else f'mkfs.xfs -f -L {label} "$D"'
        make += f'grep -q "^$D" /proc/mounts && {{ echo "ERR $D is mounted"; exit 1; }}\nwipefs -a "$D"\n{mkfs}\n'
    return f"""set -e
D={device}; P={point}
[ -b "$D" ] || {{ echo "ERR $D is not there"; exit 1; }}
{make}U=$(blkid -s UUID -o value "$D"); F=$(blkid -s TYPE -o value "$D")
[ -n "$U" ] || {{ echo "ERR $D has no filesystem"; exit 1; }}
mkdir -p "$P"
mountpoint -q "$P" || chattr +i "$P" 2>/dev/null || true
if ! grep -q "UUID=$U " /etc/fstab; then
  cp /etc/fstab /etc/fstab.homestead-backup
  echo "UUID=$U $P $F defaults,nofail,x-systemd.device-timeout=10s 0 2" >> /etc/fstab
fi
mountpoint -q "$P" || mount "$P"
S=$(findmnt -n -o SOURCE --target "$P")
[ "$(readlink -f "$S")" = "$(readlink -f "$D")" ] || {{ echo "ERR $P is not $D after mounting ($S)"; exit 1; }}
echo "OK $U $F $P"
"""


def setup(node, device, mode, fstype="ext4", confirm=""):
    """Format (or erase) and mount, or mount as it is. Returns the folder,
    ready for Longhorn. The disk is looked at again first: nothing is done
    to one that changed since it was shown."""
    device = _device(device)
    facts = inspect(node, device)
    if mode not in facts["choices"]:
        raise ValueError(refusal(facts, mode))
    if fstype not in FSTYPES:
        raise ValueError("format it as ext4 or XFS")
    if mode in ("format", "erase") and confirm.strip() != device:
        raise ValueError(f"type {device} to confirm: formatting erases it")
    if mode in ("format", "erase") and f"mkfs.{fstype}" not in facts["tools"]:
        raise ValueError(f"{node} has no mkfs.{fstype}; install {'xfsprogs' if fstype == 'xfs' else 'e2fsprogs'} there, or choose the other filesystem")
    point = mount_point(device)
    out, err = hostrun.run(node, setup_script(device, mode, fstype, point, facts.get("multipath") or ()), timeout=600)
    done = next((line for line in out.splitlines() if line.startswith("OK ")), "")
    if not done:
        problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
        raise ValueError(f"setting {device} up on {node} stopped: {problem}")
    return {"path": point, "facts": facts, "kept_data": mode == "import"}


# ---- free space on the OS drive ---------------------------------------------
#
# Ubuntu's installer, among others, puts the system on LVM and gives the root
# volume only part of the drive (100 GB by default), leaving the rest of the
# volume group free. That space can hold Longhorn without repartitioning
# anything: a new logical volume, formatted and mounted the same safe way as a
# whole disk. A reserve is kept free in the group, for the system to grow into
# (lvextend) - and a filesystem of its own means Longhorn filling it cannot
# fill the system's. Partitions of a running system are never resized.

OS_LV = "longhorn"
OS_POINT = "/mnt/longhorn-os"
VG_NAME = re.compile(r"[A-Za-z0-9+_.-]{1,64}")

OS_SPACE_SCRIPT = r"""R=$(findmnt -n -o SOURCE /)
echo "ROOT $R"
echo "ROOTFREE $(df -B1 --output=avail / | tail -n 1 | tr -d ' ')"
if command -v lvs >/dev/null 2>&1; then
  VG=$(lvs --noheadings -o vg_name,lv_path,lv_dm_path 2>/dev/null </dev/null | awk -v r="$R" '$2 == r || $3 == r {print $1; exit}')
  if [ -n "$VG" ]; then
    echo "VG $VG"
    vgs --noheadings --units b --nosuffix -o vg_size,vg_free "$VG" </dev/null | awk '{print "SIZE " $1; print "FREE " $2}'
    lvs --noheadings --units b --nosuffix -o lv_name,lv_size "$VG" </dev/null | awk '{print "LV " $1 " " $2}'
    pvs --noheadings -o pv_name --select "vg_name=$VG" </dev/null | awk '{print "PV " $1}'
  fi
else
  echo "NOLVMTOOLS"
fi
for c in lvcreate mkfs.ext4 chattr findmnt; do command -v $c >/dev/null && echo "TOOL $c"; done
echo END"""


def parse_os_space(out):
    gb = lambda value: round(int(value) / 1024 ** 3, 1) if value.isdigit() else 0
    facts = {"root": "", "root_free_gb": 0, "vg": "", "size_gb": 0, "free_gb": 0, "lvs": {}, "pvs": [], "tools": [],
             "lvm_tools": True}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "ROOT":
            facts["root"] = value
        elif key == "ROOTFREE":
            facts["root_free_gb"] = gb(value)
        elif key == "VG" and VG_NAME.fullmatch(value):
            facts["vg"] = value
        elif key == "SIZE":
            facts["size_gb"] = gb(value)
        elif key == "FREE":
            facts["free_gb"] = gb(value)
        elif key == "LV":
            name, _, size = value.partition(" ")
            facts["lvs"][name] = gb(size)
        elif key == "PV":
            facts["pvs"].append(value)
        elif key == "TOOL":
            facts["tools"].append(value)
        elif key == "NOLVMTOOLS":
            facts["lvm_tools"] = False
    if "END" not in out.split():
        facts["error"] = "the host did not finish looking at its OS drive"
    # Kept free for the system: a tenth of the group, and never under 10 GB.
    facts["reserve_gb"] = max(10, round(facts["size_gb"] * 0.1)) if facts["vg"] else 0
    facts["usable_gb"] = max(0, int(facts["free_gb"] - facts["reserve_gb"])) if facts["vg"] else 0
    facts["exists"] = OS_LV in facts["lvs"]
    facts["mount_point"] = OS_POINT
    return facts


def os_space(node):
    out, err = hostrun.run(node, OS_SPACE_SCRIPT, timeout=60)
    facts = parse_os_space(out)
    if not out.strip() and err:
        facts["error"] = err[:300]
    facts["problem"] = os_space_problem(facts)
    return facts


def os_space_problem(facts):
    if facts.get("error"):
        return facts["error"]
    if not facts["vg"]:
        return ("its system is not on LVM, so there is no free space Homestead can use without resizing partitions, "
                "which it does not do to a running system" if facts["lvm_tools"] else "the host has no LVM tools")
    if facts["exists"]:
        return f"{facts['vg']}/{OS_LV} already exists"
    for tool in ("lvcreate", "mkfs.ext4", "chattr", "findmnt"):
        if tool not in facts["tools"]:
            return f"the host has no {tool}"
    if facts["usable_gb"] < 5:
        return (f"{facts['vg']} has {facts['free_gb']} GB free; {facts['reserve_gb']} GB of that is kept for the "
                "system, which leaves too little for Longhorn")
    return ""


def use_os_space(node, size_gb):
    """Make a logical volume of size_gb in the OS drive's free space, format
    and mount it, ready for Longhorn. The drive is looked at again first."""
    facts = os_space(node)
    if facts["problem"]:
        raise ValueError(facts["problem"])
    try:
        size = int(size_gb)
    except (TypeError, ValueError):
        raise ValueError("give the size in whole GB") from None
    if not 5 <= size <= facts["usable_gb"]:
        raise ValueError(f"choose from 5 to {facts['usable_gb']} GB: {facts['reserve_gb']} GB of {facts['vg']} "
                         "is kept free for the system")
    vg = facts["vg"]
    device = f"/dev/{vg}/{OS_LV}"
    script = f"""set -e
lvs "{vg}/{OS_LV}" >/dev/null 2>&1 && {{ echo "ERR {vg}/{OS_LV} already exists"; exit 1; }}
lvcreate -y -L {size}G -n {OS_LV} "{vg}" >/dev/null
udevadm settle 2>/dev/null || true
mkfs.ext4 -F -L hs-longhorn "{device}" >/dev/null
""" + setup_script(device, "mount", "ext4", OS_POINT)
    out, err = hostrun.run(node, script, timeout=600)
    if not any(line.startswith("OK ") for line in out.splitlines()):
        problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
        raise ValueError(f"making {vg}/{OS_LV} on {node} stopped: {problem}")
    return {"path": OS_POINT, "vg": vg, "size_gb": size, "device": device}


def refusal(facts, mode):
    state, device = facts["state"], facts["device"]
    if state == "missing":
        return f"{device} is not there: {facts['error']}"
    if state == "system":
        return f"{device} is this host's system disk ({', '.join(facts['mounts'])}); Longhorn keeps its default disk there already"
    if state == "held":
        what = ", ".join(f"{h['kind']} ({h['name']})" for h in facts["holders"] if h["kind"] != "multipath")
        return f"{device} is in use by {what}; take it out of that on the host first"
    if state == "mounted":
        return (f"{device} is mounted at {', '.join(facts['mounts'])}; unmount it first, or give Longhorn that "
                "folder with Add a folder")
    if mode == "import":
        return f"{device} holds no Longhorn data to keep"
    if mode == "format":
        return f"{device} is not blank ({facts['fstype'] or 'partitioned'}); erasing it needs its own confirmation"
    return f"{device} cannot be set up that way"
