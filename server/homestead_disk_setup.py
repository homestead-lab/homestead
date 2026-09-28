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
             "by_id": "", "longhorn": None, "replicas": 0, "entries": 0, "tools": [], "error": ""}
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
    if "END" not in out.split() and not facts["error"]:
        facts["error"] = "the host did not finish looking at the disk"
    facts["system"] = any(m in SYSTEM_POINTS or m.startswith("/boot") for m in facts["mounts"])
    facts["state"] = ("missing" if facts["error"] else "system" if facts["system"]
                      else "mounted" if facts["mounts"] else "partitioned" if facts["partitions"]
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
    if state in ("missing", "system", "mounted"):
        return []
    if state == "blank":
        return ["format"]
    if state == "longhorn":
        return ["import", "erase"]
    return ["erase"]


def setup_script(device, mode, fstype, point):
    label = ("hs-" + device.rsplit("/", 1)[-1])[:12]
    make = ""
    if mode in ("format", "erase"):
        mkfs = f'mkfs.ext4 -F -L {label} "$D"' if fstype == "ext4" else f'mkfs.xfs -f -L {label} "$D"'
        make = f'grep -q "^$D" /proc/mounts && {{ echo "ERR $D is mounted"; exit 1; }}\nwipefs -a "$D"\n{mkfs}\n'
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
    out, err = hostrun.run(node, setup_script(device, mode, fstype, point), timeout=600)
    done = next((line for line in out.splitlines() if line.startswith("OK ")), "")
    if not done:
        problem = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
        raise ValueError(f"setting {device} up on {node} stopped: {problem}")
    return {"path": point, "facts": facts, "kept_data": mode == "import"}


def refusal(facts, mode):
    state, device = facts["state"], facts["device"]
    if state == "missing":
        return f"{device} is not there: {facts['error']}"
    if state == "system":
        return f"{device} is this host's system disk ({', '.join(facts['mounts'])}); Longhorn keeps its default disk there already"
    if state == "mounted":
        return (f"{device} is mounted at {', '.join(facts['mounts'])}; unmount it first, or give Longhorn that "
                "folder with Add a folder")
    if mode == "import":
        return f"{device} holds no Longhorn data to keep"
    if mode == "format":
        return f"{device} is not blank ({facts['fstype'] or 'partitioned'}); erasing it needs its own confirmation"
    return f"{device} cannot be set up that way"
