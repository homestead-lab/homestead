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
nothing else changes. A disk in a RAID array or an encrypted volume is
refused, naming what holds it.

A disk from an old install - Ubuntu's LVM, say, which the host may even have
switched on at boot - can be wiped and prepared, typed to confirm. inspect()
reads its LVM: each physical volume on the disk or its partitions, the group
it belongs to (by UUID - an old Ubuntu disk and the running system both call
theirs ubuntu-vg), the group's other disks, and its volumes. The wipe is
refused, with each reason, when anything on the disk is mounted or swapped
on, a volume is open, the group is the one the running system's root is in,
or the group spans another disk, which wiping this one would break. Else the
groups are switched off and removed, the LVM labels, filesystem signatures and
partition table (both GPT copies) wiped, the kernel told to re-read the disk,
and the disk looked at again: only a disk that is now blank is formatted.
"""
import re
import time

hostrun = None          # homestead_hostrun
# The raw devices Longhorn's V2 engine uses on a node (their paths as Longhorn
# has them), bound by homestead_disks; None when Longhorn could not be read.
# Such a disk has no filesystem and would look blank: it is refused instead.
longhorn_block_paths = lambda node: []
BLOCK_PATH = re.compile(r"/dev/[A-Za-z0-9/_.:+@-]+")
LVM_UUID = re.compile(r"[A-Za-z0-9-]{8,64}")
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


def claimed_script(paths):
    """Which of Longhorn's V2 devices sit on $D: the device itself, a partition
    of it, or a volume on it - whatever lsblk lists beneath the device."""
    safe = [p for p in paths if BLOCK_PATH.fullmatch(p)]
    return "".join(f'r=$(readlink -f "{p}" 2>/dev/null); [ -b "$r" ] && lsblk -snro NAME "$r" 2>/dev/null | '
                   f'grep -qx "${{D##*/}}" && echo "LHBLOCK {p}"\n' for p in safe)


def _claimed(node):
    paths = longhorn_block_paths(node)
    if paths is None:
        raise ValueError(f"Longhorn's disks on {node} could not be read, so nothing is changed on its drives")
    return paths


def inspect_script(device, claimed=()):
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
# LVM on the disk or its partitions: each physical volume and its group, every
# group's disks and volumes, and the group the running system's root is in.
if command -v pvs >/dev/null 2>&1; then
  echo "TOOL pvs"
  lsblk -lnpo NAME "$D" 2>/dev/null | while read -r p; do
    pvs --noheadings --separator '|' -o pv_name,vg_name,vg_uuid "$p" 2>/dev/null | sed 's/^ *//' | while read -r l; do echo "PV $l"; done
  done
  pvs --noheadings --separator '|' -o vg_uuid,pv_name 2>/dev/null | sed 's/^ *//' | while read -r l; do echo "VGPV $l"; done
  lvs --noheadings --separator '|' -o vg_uuid,vg_name,lv_name,lv_attr 2>/dev/null | sed 's/^ *//' | while read -r l; do echo "LV $l"; done
  R=$(findmnt -n -o SOURCE / 2>/dev/null)
  [ -n "$R" ] && echo "ROOTVG $(lvs --noheadings -o vg_uuid "$R" 2>/dev/null | tr -d ' ')"
fi
""" + claimed_script(claimed) + "echo END"


def parse(device, out):
    facts = {"device": device, "size_gb": 0, "partitions": [], "mounts": [], "fstype": "", "uuid": "",
             "by_id": "", "longhorn": None, "replicas": 0, "entries": 0, "tools": [], "error": "",
             "holders": [], "longhorn_block": []}
    pvs, vg_pvs, vg_lvs, root_vg = [], {}, {}, ""
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
        elif key == "LHBLOCK":
            facts["longhorn_block"].append(value)
        elif key == "PV":
            pv, vg, uuid = (value.split("|") + ["", "", ""])[:3]
            if pv and not any(row["pv"] == pv for row in pvs):
                pvs.append({"pv": pv, "vg": vg, "uuid": uuid})
        elif key == "VGPV":
            uuid, pv = (value.split("|") + ["", ""])[:2]
            if uuid and pv:
                vg_pvs.setdefault(uuid, []).append(pv)
        elif key == "LV":
            uuid, vg, lv, attr = (value.split("|") + ["", "", "", ""])[:4]
            if uuid and lv:
                vg_lvs.setdefault(uuid, []).append({"name": lv, "active": attr[4:5] == "a", "open": attr[5:6] == "o"})
        elif key == "ROOTVG":
            root_vg = value.strip()
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
    facts["multipath"] = [h for h in facts["holders"] if h["kind"] == "multipath" and h["wwid"]]
    facts["lvm"] = _lvm_groups(pvs, vg_pvs, vg_lvs, root_vg)
    facts["orphan_pvs"] = [row["pv"] for row in pvs if not row["uuid"]]
    facts["reasons"] = _wipe_reasons(facts, pvs)
    facts["state"] = ("missing" if facts["error"] else "system" if facts["system"]
                      else "longhorn-v2" if facts["longhorn_block"]
                      else "mounted" if facts["mounts"] else "held" if facts["reasons"]
                      else "lvm" if facts["lvm"] or facts["orphan_pvs"]
                      else "partitioned" if facts["partitions"]
                      else "longhorn" if facts["longhorn"] else "data" if facts["fstype"] else "blank")
    return facts


def _lvm_groups(pvs, vg_pvs, vg_lvs, root_vg):
    """The volume groups with a physical volume on this disk: where else they
    live, what volumes they hold, and whether the running system is on one."""
    groups = {}
    for row in pvs:
        if not row["uuid"]:
            continue
        group = groups.setdefault(row["uuid"], {"vg": row["vg"], "uuid": row["uuid"], "pvs": [],
                                                "lvs": vg_lvs.get(row["uuid"], []), "root": row["uuid"] == root_vg})
        group["pvs"].append(row["pv"])
    for group in groups.values():
        group["elsewhere"] = [pv for pv in vg_pvs.get(group["uuid"], []) if pv not in group["pvs"]]
    return list(groups.values())


def _wipe_reasons(facts, pvs):
    """Everything that would make wiping this disk unsafe, said plainly. A
    disk with none of these holds nothing this host is using."""
    reasons = []
    for holder in facts["holders"]:
        if holder["kind"] not in ("multipath", "LVM"):
            reasons.append(f"{holder['kind']} ({holder['name']}) holds it; take it out of that on the host first")
    for group in facts["lvm"]:
        vg = group["vg"] or group["uuid"]
        if group["root"]:
            reasons.append(f"volume group {vg} is the one this host's running system is on")
        if group["elsewhere"]:
            reasons.append(f"volume group {vg} also uses {', '.join(group['elsewhere'])}; wiping this disk would break it there")
        for lv in group["lvs"]:
            if lv["open"]:
                reasons.append(f"{vg}/{lv['name']} is open - something on this host is using it")
    lvm_held = [h["name"] for h in facts["holders"] if h["kind"] == "LVM"]
    if lvm_held and not facts["lvm"]:
        reasons.append(f"LVM holds it ({', '.join(lvm_held)}), but "
                       + ("the host's LVM tools did not say which volume group" if "pvs" in facts["tools"]
                          else "the host has no LVM tools (pvs) to say which volume group"))
    for group in facts["lvm"]:
        if not LVM_UUID.fullmatch(group["uuid"]) or any(not BLOCK_PATH.fullmatch(pv) for pv in group["pvs"]):
            reasons.append(f"volume group {group['vg']} has a name Homestead will not put in a command")
    if any(not BLOCK_PATH.fullmatch(row["pv"]) for row in pvs):
        reasons.append("a physical volume on it has a path Homestead will not put in a command")
    return list(dict.fromkeys(reasons))


def inspect(node, device):
    device = _device(device)
    out, err = hostrun.run(node, inspect_script(device, _claimed(node)), timeout=60)
    facts = parse(device, out)
    if not out.strip() and err:
        facts.update(error=err[:300], state="missing")
    facts["mount_point"] = mount_point(device)
    facts["choices"] = choices(facts)
    return facts


def choices(facts):
    """What can be done with it, in the order offered."""
    state = facts["state"]
    if state in ("missing", "system", "mounted", "held", "longhorn-v2"):
        return []
    if state == "blank":
        return ["format"]
    if state == "lvm":
        return ["wipe"]
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


def wipe_script(device, facts):
    """Take an unused disk back to blank: its LVM groups off and removed, the
    LVM labels, filesystem signatures and partition table (wipefs clears both
    GPT copies) wiped - partitions first, then the disk - and the kernel told
    to re-read it. Checked again on the host before anything is removed."""
    lines = ["set -e", f"D={device}"]
    if facts.get("multipath"):
        lines.append(release_script(facts["multipath"]).rstrip())
    lines.append('M=$(lsblk -rno MOUNTPOINT "$D" 2>/dev/null | grep . | tr "\\n" " ")')
    lines.append('[ -z "$M" ] || { echo "ERR something on $D is in use: $M"; exit 1; }')
    for group in facts.get("lvm") or []:
        uuid = group["uuid"]
        lines.append(f'O=$(lvs --noheadings -o lv_attr --select vg_uuid={uuid} 2>/dev/null | tr -d " " | cut -c6 | grep o || true)')
        lines.append(f'[ -z "$O" ] || {{ echo "ERR a volume in {group["vg"]} is open"; exit 1; }}')
        lines.append(f"vgchange -an --select vg_uuid={uuid} >/dev/null")
        lines.append(f"vgremove -ff -y --select vg_uuid={uuid} >/dev/null")
    for pv in [pv for group in facts.get("lvm") or [] for pv in group["pvs"]] + list(facts.get("orphan_pvs") or []):
        lines.append(f'pvremove -ff -y "{pv}" >/dev/null 2>&1 || true')
    lines += [
        'for p in $(lsblk -lnpo NAME,TYPE "$D" | awk \'$2=="part"{print $1}\' | sort -r); do wipefs -a "$p" >/dev/null; done',
        'wipefs -a "$D" >/dev/null',
        'blockdev --rereadpt "$D" 2>/dev/null || partx -d "$D" 2>/dev/null || true',
        "udevadm settle 2>/dev/null || true",
        'L=$(lsblk -rno NAME "$D" 2>/dev/null | sed 1d | tr "\\n" " ")',
        '[ -z "$L" ] || { echo "ERR $D still shows $L after the wipe; restart the host and look again"; exit 1; }',
        'S=$(blkid -p -s TYPE -o value "$D" 2>/dev/null || true)',
        '[ -z "$S" ] || { echo "ERR $D still has a $S signature after the wipe"; exit 1; }',
        "echo WIPED",
    ]
    return "\n".join(lines) + "\n"


def wipe(node, device, facts):
    """Wipe a disk that holds nothing this host is using, then look again.
    Returns what the disk is now, which is blank - or says what is left."""
    if facts["state"] not in ("lvm", "partitioned", "data", "longhorn"):
        raise ValueError(refusal(facts, "wipe"))
    if "wipefs" not in facts["tools"]:
        raise ValueError(f"{node} has no wipefs; install util-linux there")
    out, err = hostrun.run(node, wipe_script(device, facts), timeout=300)
    if "WIPED" not in out.split():
        problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
        raise ValueError(f"wiping {device} on {node} stopped: {problem}")
    after = inspect(node, device)
    if after["state"] != "blank":
        raise ValueError(f"{device} was wiped but is not blank yet ({after['state']}): "
                         + (refusal(after, "format") if after["state"] != "blank" else ""))
    return after


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
    if mode in ("format", "erase", "wipe") and confirm.strip() != device:
        raise ValueError(f"type {device} to confirm: formatting erases it")
    if mode in ("format", "erase", "wipe") and f"mkfs.{fstype}" not in facts["tools"]:
        raise ValueError(f"{node} has no mkfs.{fstype}; install {'xfsprogs' if fstype == 'xfs' else 'e2fsprogs'} there, or choose the other filesystem")
    if mode in ("erase", "wipe"):
        # Back to blank first - old LVM, partitions and all - then formatted
        # as a blank disk is.
        facts, mode = wipe(node, device, facts), "format"
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
# whole disk for the V1 engine, or given raw to the V2 engine. A reserve is
# kept free in the group, for the system to grow into (lvextend) - and a
# filesystem of its own means Longhorn filling it cannot fill the system's.
#
# A disk can also have space no partition covers - past the last one, where
# an installer was told to leave some. A new partition there is made live:
# the partition table gets one more entry, written without asking the kernel
# to re-read the disk, and the kernel is told of that one partition alone
# (partx --add --nr), so nothing mounted is touched. The table is saved
# first, and the space is checked to be free again right before the write.
# Only GPT disks: an MBR table has four entries, often an extended one.
#
# What is never done: shrinking or moving a partition, or a filesystem, of a
# running system. ext4 cannot shrink while mounted and XFS cannot shrink at
# all; that is a job for a rescue boot, not for Homestead.

OS_LV = "longhorn"
OS_LV_V2 = "longhorn-v2"
OS_POINT = "/mnt/longhorn-os"
MIN_REGION_GB = 10
LINUX_DATA = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
DISK_NAME = re.compile(r"(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)")
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
# Space on each GPT disk that no partition covers.
if command -v sfdisk >/dev/null 2>&1; then
  for d in $(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2 == "disk" {print $1}'); do
    case "$d" in loop*|zram*|ram*|sr*) continue;; esac
    T=$(lsblk -dn -o PTTYPE "/dev/$d" 2>/dev/null)
    [ -n "$T" ] || continue
    echo "PT $d $T $(blockdev --getss "/dev/$d" 2>/dev/null)"
    ls /sys/block/$d/holders 2>/dev/null | grep -q . && echo "HELD $d"
    sfdisk -F "/dev/$d" 2>/dev/null | awk -v d="$d" 'NF == 4 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ {print "REGION " d " " $1 " " $3}'
  done
fi
for c in lvcreate mkfs.ext4 chattr findmnt sfdisk partx; do command -v $c >/dev/null && echo "TOOL $c"; done
echo END"""


def parse_os_space(out):
    gb = lambda value: round(int(value) / 1024 ** 3, 1) if value.isdigit() else 0
    facts = {"root": "", "root_free_gb": 0, "vg": "", "size_gb": 0, "free_gb": 0, "lvs": {}, "pvs": [], "tools": [],
             "lvm_tools": True, "regions": []}
    tables, held = {}, set()
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
        elif key == "PT":
            parts = value.split()
            if len(parts) == 3 and DISK_NAME.fullmatch(parts[0]) and parts[2].isdigit():
                tables[parts[0]] = (parts[1], int(parts[2]))
        elif key == "HELD":
            held.add(value)
        elif key == "REGION":
            parts = value.split()
            if len(parts) == 3 and parts[0] in tables and parts[1].isdigit() and parts[2].isdigit():
                table, sector = tables[parts[0]]
                size = int(parts[2]) * sector
                if table == "gpt" and parts[0] not in held and size >= MIN_REGION_GB * 1024 ** 3:
                    facts["regions"].append({"disk": parts[0], "start": int(parts[1]), "sectors": int(parts[2]),
                                             "sector": sector, "size_gb": int(size / 1024 ** 3)})
    if "END" not in out.split():
        facts["error"] = "the host did not finish looking at its OS drive"
    # Kept free for the system: a tenth of the group, and never under 10 GB.
    facts["reserve_gb"] = max(10, round(facts["size_gb"] * 0.1)) if facts["vg"] else 0
    facts["usable_gb"] = max(0, int(facts["free_gb"] - facts["reserve_gb"])) if facts["vg"] else 0
    facts["exists"] = OS_LV in facts["lvs"]
    facts["taken"] = {"v1": OS_LV in facts["lvs"], "v2": OS_LV_V2 in facts["lvs"]}
    facts["mount_point"] = OS_POINT
    return facts


def os_space(node):
    out, err = hostrun.run(node, OS_SPACE_SCRIPT, timeout=60)
    facts = parse_os_space(out)
    if not out.strip() and err:
        facts["error"] = err[:300]
    facts["lvm_problems"] = {engine: os_space_problem(facts, engine) for engine in ("v1", "v2")}
    # Unallocated space on a disk is a way in without LVM, and V2 has a volume of its own.
    usable = facts["regions"] or not all(facts["lvm_problems"].values())
    facts["problem"] = "" if usable and not facts.get("error") else facts["lvm_problems"]["v1"]
    return facts


def os_space_problem(facts, engine="v1"):
    if facts.get("error"):
        return facts["error"]
    if not facts["vg"]:
        return ("its system is not on LVM, so there is no free space Homestead can use without resizing partitions, "
                "which it does not do to a running system" if facts["lvm_tools"] else "the host has no LVM tools")
    if facts["taken"].get(engine):
        return f"{facts['vg']}/{OS_LV if engine == 'v1' else OS_LV_V2} already exists"
    for tool in ("lvcreate", "mkfs.ext4", "chattr", "findmnt"):
        if tool not in facts["tools"]:
            return f"the host has no {tool}"
    if facts["usable_gb"] < 5:
        return (f"{facts['vg']} has {facts['free_gb']} GB free; {facts['reserve_gb']} GB of that is kept for the "
                "system, which leaves too little for Longhorn")
    return ""


def _size(size_gb):
    try:
        return int(size_gb)
    except (TypeError, ValueError):
        raise ValueError("give the size in whole GB") from None


def use_os_space(node, size_gb, engine="v1"):
    """Make a logical volume of size_gb in the OS drive's free space: for V1
    formatted and mounted, for V2 left raw. The drive is looked at again first."""
    facts = os_space(node)
    problem = os_space_problem(facts, engine)
    if problem:
        raise ValueError(problem)
    size = _size(size_gb)
    if not 5 <= size <= facts["usable_gb"]:
        raise ValueError(f"choose from 5 to {facts['usable_gb']} GB: {facts['reserve_gb']} GB of {facts['vg']} "
                         "is kept free for the system")
    vg = facts["vg"]
    if engine == "v2":
        device = f"/dev/{vg}/{OS_LV_V2}"
        out, err = hostrun.run(node, f"""set -e
lvs "{vg}/{OS_LV_V2}" >/dev/null 2>&1 && {{ echo "ERR {vg}/{OS_LV_V2} already exists"; exit 1; }}
lvcreate -y -L {size}G -n {OS_LV_V2} "{vg}" >/dev/null
udevadm settle 2>/dev/null || true
[ -b "{device}" ] && echo "OK {device}"
""", timeout=120)
        if not any(line.startswith("OK ") for line in out.splitlines()):
            problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
            raise ValueError(f"making {vg}/{OS_LV_V2} on {node} stopped: {problem}")
        return {"path": device, "vg": vg, "size_gb": size, "device": device, "engine": "v2"}
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
    return {"path": OS_POINT, "vg": vg, "size_gb": size, "device": device, "engine": "v1"}


def region_plan(region, size_gb):
    """Where the new partition goes: its first sector on a 1 MiB boundary,
    its length whole MiB, inside the free region."""
    sector, start, sectors = region["sector"], region["start"], region["sectors"]
    align = max(1, 1024 ** 2 // sector)
    first = -(-start // align) * align
    room = (start + sectors - first) // align * align
    want = _size(size_gb) * 1024 ** 3 // sector // align * align
    length = min(want, room)
    if length * sector < 5 * 1024 ** 3:
        raise ValueError("that leaves less than 5 GB; choose a larger size")
    return first, length


def partition_script(disk, region, first, length, stamp):
    """One partition appended in free space, on a disk in use: saved, checked
    free again, written without a re-read, and only it told to the kernel."""
    if not DISK_NAME.fullmatch(disk):
        raise ValueError(f"{disk} is not a disk Homestead partitions")
    last = first + length - 1
    return f"""set -e
D=/dev/{disk}
command -v sfdisk >/dev/null && command -v partx >/dev/null || {{ echo "ERR the host has no sfdisk or partx"; exit 1; }}
[ "$(lsblk -dn -o PTTYPE "$D")" = gpt ] || {{ echo "ERR $D has no GPT partition table"; exit 1; }}
mkdir -p /var/lib/homestead
sfdisk --dump "$D" > /var/lib/homestead/partitions-{disk}-{stamp}.sfdisk
sfdisk -F "$D" 2>/dev/null | awk 'NF == 4 && $1 <= {first} && $2 >= {last} {{ok = 1}} END {{exit !ok}}' ||
  {{ echo "ERR the space on $D changed since it was looked at; nothing was written"; exit 1; }}
echo 'start={first}, size={length}, type={LINUX_DATA}, name="homestead-longhorn"' |
  sfdisk --append --no-reread --no-tell-kernel -q "$D" >/dev/null
NEWP=$(sfdisk --dump "$D" | awk -F' : ' '$2 ~ /start= *{first},/ {{print $1; exit}}')
[ -n "$NEWP" ] || {{ echo "ERR the new partition is not in $D's table"; exit 1; }}
N=${{NEWP##*[!0-9]}}
# udev may have told the kernel already; if not, partx adds this one only.
udevadm settle 2>/dev/null || true
[ -b "$NEWP" ] || partx --add --nr "$N" "$D" || true
udevadm settle 2>/dev/null || true
[ -b "$NEWP" ] || {{ echo "ERR the kernel does not show $NEWP; the table is saved in /var/lib/homestead"; exit 1; }}
echo "PART $NEWP $(blkid -s PARTUUID -o value "$NEWP" 2>/dev/null)"
"""


def use_region(node, disk, start, size_gb, engine="v1", confirm=""):
    """A new partition in a disk's unallocated space, live: for V1 formatted
    and mounted, for V2 given raw by its PARTUUID."""
    facts = os_space(node)
    region = next((r for r in facts["regions"] if r["disk"] == disk and r["start"] == int(start)), None)
    if region is None:
        raise ValueError(f"that free space on {disk} is not there any more; look again")
    # Longhorn's V2 engine may keep a raw device here with a stale GPT header
    # still readable: writing a table would overwrite its data.
    claimed = inspect(node, f"/dev/{disk}")
    if claimed["longhorn_block"]:
        raise ValueError(refusal(claimed, "partition"))
    if confirm.strip() != f"/dev/{disk}":
        raise ValueError(f"type /dev/{disk} to confirm: its partition table is changed")
    for tool in ("sfdisk", "partx") + (("mkfs.ext4", "chattr", "findmnt") if engine == "v1" else ()):
        if tool not in facts["tools"]:
            raise ValueError(f"{node} has no {tool}")
    first, length = region_plan(region, size_gb)
    point = f"/mnt/{disk}-longhorn"
    script = partition_script(disk, region, first, length, time.strftime("%Y%m%d-%H%M%S"))
    if engine == "v1":
        script += 'mkfs.ext4 -F -L hs-longhorn "$NEWP" >/dev/null\n' + setup_script('"$NEWP"', "mount", "ext4", point)
    out, err = hostrun.run(node, script, timeout=600)
    made = next((line.split() for line in out.splitlines() if line.startswith("PART ")), None)
    problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
    if not made or engine == "v1" and not any(line.startswith("OK ") for line in out.splitlines()):
        where = f" (made {made[1]}, which is kept)" if made else ""
        raise ValueError(f"the new partition on {disk} of {node} stopped{where}: {problem}")
    partition = made[1]
    path = point if engine == "v1" else (f"/dev/disk/by-partuuid/{made[2]}" if len(made) > 2 and made[2] else partition)
    return {"path": path, "device": partition, "size_gb": length * region["sector"] // 1024 ** 3, "engine": engine}


def refusal(facts, mode):
    state, device = facts["state"], facts["device"]
    if state == "missing":
        return f"{device} is not there: {facts['error']}"
    if state == "system":
        return f"{device} is this host's system disk ({', '.join(facts['mounts'])}); Longhorn keeps its default disk there already"
    if state == "longhorn-v2":
        return (f"Longhorn's V2 engine keeps data on {device} ({', '.join(facts['longhorn_block'])}); "
                "move its replicas off and remove it from Longhorn first")
    if state == "held":
        what = ", ".join(f"{h['kind']} ({h['name']})" for h in facts["holders"] if h["kind"] != "multipath")
        reasons = "; ".join(facts.get("reasons") or []) or "take it out of that on the host first"
        return f"{device} is in use by {what}: {reasons}" if what else f"{device} cannot be wiped: {reasons}"
    if state == "mounted":
        swap = [m for m in facts["mounts"] if m.upper() == "[SWAP]"]
        points = [m for m in facts["mounts"] if m.upper() != "[SWAP]"]
        return (f"{device} is in use: " + "; ".join(
            ([f"mounted at {', '.join(points)}"] if points else []) + (["used as swap"] if swap else []))
                + "; unmount it (swapoff for swap) first, or give Longhorn a mounted folder with Add a folder")
    if mode == "import":
        return f"{device} holds no Longhorn data to keep"
    if mode == "format":
        return f"{device} is not blank ({facts['fstype'] or 'partitioned'}); erasing it needs its own confirmation"
    if mode == "wipe" and state == "blank":
        return f"{device} is blank already; format it instead"
    return f"{device} cannot be set up that way"
