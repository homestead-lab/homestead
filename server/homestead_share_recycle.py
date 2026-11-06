"""Each share's recycle bin: files deleted over SMB kept in .deleted.

The Samba image keeps a recycle bin on every share, and never empties it:
files deleted over SMB move to .deleted at the share's root and stay there,
so a share fills with what its users already deleted. Homestead now
turns the image's bin off (-r) and gives it back per share as the share's
settings say: on or off, and how many days a deleted file is kept. A share
saved before this setting keeps its bin, as it always had one, with no limit
until someone sets one.

A bin with a limit is emptied by a small container beside Samba, which every
hour removes what was deleted more than that many days ago. Samba stamps a
file's modified time when it moves it to the bin (touch_mtime), so that is
the time it was deleted. Files already in a bin when a limit is first set
keep their own modified time, and go by that.
"""
import posixpath

import homestead_pod_exec as POD_EXEC

FOLDER = ".deleted"
DEFAULT_DAYS = 30           # a new share's bin keeps deleted files this long
MAX_DAYS = 3650
CLEANER = "homestead-recycle-cleanup"
CLEANER_IMAGE = "alpine:3.24"
EVERY_SECONDS = 3600
VFS_WITH_BIN = "catia fruit recycle streams_xattr"   # the image's own list, with recycle


def setting(row):
    """(on, days) for a share. days 0 keeps deleted files until emptied. A
    share saved before the setting existed keeps the bin it always had."""
    on = row.get("recycle")
    on = True if on is None else bool(on)
    try:
        days = int(row.get("recycle_days") or 0)
    except (TypeError, ValueError):
        days = 0
    return on, max(0, min(days, MAX_DAYS))


def days_value(value, default=DEFAULT_DAYS):
    """A number of days from a form: 0 to MAX_DAYS, or a ValueError."""
    if value in (None, ""):
        return default
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError("keep deleted files for a whole number of days (0 keeps them until emptied)")
    if isinstance(value, float) and value != days or not 0 <= days <= MAX_DAYS:
        raise ValueError(f"keep deleted files for 0 to {MAX_DAYS} days (0 keeps them until emptied)")
    return days


def share_args(row):
    """The Samba arguments that give one share its bin, after its -s; none
    when it has no bin. Read-only shares cannot delete, so need none."""
    on, _ = setting(row)
    if not on or row.get("read_only"):
        return []
    name = row["name"]
    return [arg for option in (f"vfs objects = {VFS_WITH_BIN}", f"recycle:repository = {FOLDER}",
                               "recycle:keeptree = yes", "recycle:versions = yes",
                               "recycle:touch_mtime = yes", "recycle:maxsize = 0")
            for arg in ("-G", f"{name};{option}")]


def cleaner(rows, mounts):
    """The container that empties bins with a limit, or None when no share has
    one. mounts are the share mounts by share name, as Samba has them."""
    kept = []
    for row in sorted(rows, key=lambda item: item["name"]):
        on, days = setting(row)
        mount = mounts.get(row["name"])
        if on and days and mount and not row.get("read_only"):
            kept.append((row["name"], days, mount))
    if not kept:
        return None
    lines = [f'find "/bins/{index}/{FOLDER}" -mindepth 1 -type f -mmin +{days * 1440} -delete 2>/dev/null; '
             f'find "/bins/{index}/{FOLDER}" -mindepth 1 -type d -empty -delete 2>/dev/null'
             for index, (_, days, _) in enumerate(kept)]
    script = "while true; do\n" + "\n".join(lines) + f"\nsleep {EVERY_SECONDS}\ndone"
    return {"name": CLEANER, "image": CLEANER_IMAGE, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", script],
            "resources": {"requests": {"cpu": "5m", "memory": "8Mi"}, "limits": {"memory": "64Mi"}},
            "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File",
            "securityContext": {"runAsUser": 0},
            "volumeMounts": [{**mount, "mountPath": f"/bins/{index}"} for index, (_, _, mount) in enumerate(kept)]}


def place_cleaner(spec, container_name, rows, mounts):
    """Put the cleaner beside Samba, or take it away, in the pod spec."""
    others = [item for item in spec["containers"] if item.get("name") != CLEANER]
    wanted = cleaner(rows, mounts)
    spec["containers"] = others + ([wanted] if wanted else [])


def folder(row):
    """The bin's path inside the Samba container, from the share's own mount."""
    path = row.get("path") or f"/shares/{row['name']}"
    if not path.startswith("/shares/") or ".." in path.split("/"):
        raise ValueError("this share's folder is not one Homestead mounts")
    return posixpath.join(path, FOLDER)


def _run(namespace, pod, container, script, timeout):
    out = POD_EXEC.run(namespace, pod, ["sh", "-c", script], timeout=timeout, container=container)
    if out.exit_code not in (0, None):
        raise RuntimeError((out.stderr or "").strip()[:200] or f"the command in {pod} failed")
    return out.stdout.decode("utf-8", "replace")


def size(namespace, pod, container, row):
    """What the bin holds: bytes and files, 0 and 0 when it is missing."""
    path = folder(row)
    text = _run(namespace, pod, container,
                f'if [ -d "{path}" ]; then du -sk "{path}" | cut -f1; find "{path}" -type f | wc -l; '
                f'else echo 0; echo 0; fi', 60)
    numbers = [int(part) for part in text.split() if part.isdigit()]
    kib, files = (numbers + [0, 0])[:2]
    return {"bytes": kib * 1024, "gb": round(kib / 1024 ** 2, 2), "files": files, "folder": path}


def empty(namespace, pod, container, row):
    """Delete everything in the bin. It cannot be undone."""
    before = size(namespace, pod, container, row)
    path = folder(row)
    _run(namespace, pod, container, f'[ ! -d "{path}" ] || find "{path}" -mindepth 1 -delete', 600)
    return before
