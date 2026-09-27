"""Bringing a container's data along when its storage is restructured.

The editor can point a path at a different volume, or a different folder of
one: two volumes combined into folders of one, one volume split into several,
a folder renamed. Remounting alone would leave the app looking at an empty
folder, so an edit that asks for it runs as a tracked job:

  1. the edit is saved with the workload held at zero replicas, and the count
     it should run at parked in an annotation;
  2. once its pods are gone, a Job copies each old location to its new one;
  3. placement is rechecked before the workload is scaled back to that count.

homestead_copy_job owns the durable, identity-checked handoff. Sources on separate
volumes are mounted read-only; a folder move within one volume necessarily writes
that volume. No volume is deleted. Uncertain mutations are not replayed, and
recovery never automatically restores mappings or starts the workload.
"""
import re
import secrets
import shlex

import homestead_names as NAMES
import homestead_copy_checks as COPY_CHECKS

kget = ksend = ktext = None
IMAGE = "alpine:3.20"
HELD = NAMES.key("restructure-replicas")
CLAIM = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
FOLDER = re.compile(r"^[A-Za-z0-9._ -]+(/[A-Za-z0-9._ -]+)*$")


def bind(_kget, _ksend, _ktext):
    global kget, ksend, ktext
    kget, ksend, ktext = _kget, _ksend, _ktext


def _folder(value, where):
    folder = str(value or "").strip().strip("/")
    if folder and (not FOLDER.fullmatch(folder) or any(part in (".", "..") for part in folder.split("/"))):
        raise ValueError(f"{where}: {folder} is not a folder name this can copy")
    return folder


def _inside(a, b):
    """True when folder a is b or within it; "" is the whole volume."""
    return not b or a == b or a.startswith(b + "/")


def copies(cfg):
    """The data moves an edit asks for, one per mount whose storage changed.

    A row asks by carrying copy_from, the claim and folder it was mounted from
    when the editor opened. Only claims are copied: a host path or scratch
    volume has nothing Homestead should carry.

    With "data": false the person chose to start empty. A volume the edit
    makes is still prepared - owned as the old location was - since a new
    volume is root's, and an app that runs as its own user could not write
    to it and would not start. An existing volume is left as it is."""
    out, seen = [], set()
    for change in cfg.get("containers") or []:
        for row in change.get("volumes") or []:
            origin = row.get("copy_from") or {}
            source = str(origin.get("claim") or "").strip()
            if not source:
                continue
            path = str(row.get("path") or "").strip()
            kind = str(row.get("kind") or "")
            data = origin.get("data", True) is not False
            fresh = kind in ("new-rwo", "new-rwx")
            if not data and not fresh:
                continue
            if kind not in ("existing", "new-rwo", "new-rwx"):
                raise ValueError(f"{path}: only a volume can receive copied data")
            target = str(row.get("source") or "").strip()
            for claim in (source, target):
                if not CLAIM.match(claim):
                    raise ValueError(f"{path}: {claim or '(blank)'} is not a volume name")
            src = _folder(origin.get("sub_path"), path)
            dst = _folder(row.get("sub_path"), path)
            if (source, src) == (target, dst):
                continue
            key = (source, src, target, dst, data)
            if key not in seen:
                seen.add(key)
                out.append({"path": path, "from": source, "from_folder": src,
                            "to": target, "to_folder": dst, "data": data, "fresh": fresh})
    targets = {}
    for move in out:
        spot = (move["to"], move["to_folder"])
        if spot in targets and targets[spot] != (move["from"], move["from_folder"]):
            raise ValueError(f"two paths would be copied into {move['to']}/{move['to_folder']}")
        targets[spot] = (move["from"], move["from_folder"])
    for i, move in enumerate(out):
        for j, other in enumerate(out):
            if i != j and move["to"] == other["to"] and (
                    _inside(move["to_folder"], other["to_folder"]) or _inside(other["to_folder"], move["to_folder"])):
                raise ValueError("copy destinations overlap; move these paths in separate edits")
            if move["to"] == other["from"] and (
                    _inside(move["to_folder"], other["from_folder"]) or _inside(other["from_folder"], move["to_folder"])):
                if i == j and _inside(move["to_folder"], move["from_folder"]):
                    continue  # the script explicitly excludes this destination subtree
                raise ValueError("a copy destination overlaps source data; use a separate volume or staged edits")
    return out


def script(moves, mount_of):
    """The copy: each old location into its new one, keeping owners and times.

    cp -a as root keeps the numeric owner, so an app finds its files as it
    left them. A location that was never written has nothing to bring.

    The new location itself - the folder the app's path is mounted on - is
    then given the old one's owner and permissions, or those of the nearest
    folder above it that exists. A new volume's root belongs to root, and an
    app running as its own user found it could not write there and would not
    start. Only a location this made is changed: a volume or folder that was
    already there keeps its own."""
    lines = ["set -e", COPY_CHECKS.SHELL,
             # The owner and mode of a path, or of the nearest folder above it
             # that exists, no higher than the volume it is in.
             'owner_of() { r="$1"; while [ ! -e "$r" ] && [ "$r" != "$2" ]; do r=$(dirname "$r"); done; '
             'stat -c "%u:%g %a" "$r"; }',
             'take_owner() { set -- $(owner_of "$1" "$2") "$3"; chown "$1" "$3"; chmod "$2" "$3"; }']
    # Refuse symlink aliases before any destination is changed, including
    # existing children that cp could otherwise follow while overwriting.
    for move in moves:
        for side in ("from", "to"):
            base = mount_of[move[side]]
            path = base + ("/" + move[side + "_folder"] if move[side + "_folder"] else "")
            check = "copy_destination" if side == "to" else "copy_path"
            lines.append(f"{check} {shlex.quote(path)} {shlex.quote(base)}")
    for index, move in enumerate(moves, 1):
        src = mount_of[move["from"]] + ("/" + move["from_folder"] if move["from_folder"] else "")
        dst = mount_of[move["to"]] + ("/" + move["to_folder"] if move["to_folder"] else "")
        label = (f"{move['from']}/{move['from_folder']}".rstrip("/") + " -> " +
                 f"{move['to']}/{move['to_folder']}".rstrip("/"))
        s, d = shlex.quote(src), shlex.quote(dst)
        root = shlex.quote(mount_of[move["from"]])
        lines.append(f"echo {shlex.quote(f'[{index}/{len(moves)}] {label}')}")
        # Whether the new location is this job's to shape, decided before
        # anything is written to it.
        lines.append(f"made={'1' if move.get('fresh') else ''}; [ -e {d} ] || made=1")
        own = f'if [ -n "$made" ] && [ -d {d} ]; then take_owner {s} {root} {d}; fi'
        if move.get("data") is False:
            lines += [f"mkdir -p {d}; echo 'starting empty, owned as before'", own]
            continue
        lines.append(f"needed=''; if measured=$(timeout 60 du -sk {s} 2>/dev/null); then "
                     "needed=$(printf '%s\\n' \"$measured\" | awk 'NR == 1 {print $1}'); fi; "
                     f"copy_space {shlex.quote(mount_of[move['to']])} \"$needed\"")
        if move["from"] == move["to"] and _inside(move["to_folder"], move["from_folder"]):
            # Into a folder of itself, as when a whole volume becomes one
            # folder of it: everything but the folder being filled.
            rest = move["to_folder"][len(move["from_folder"]):].strip("/")
            skip = shlex.quote(src + "/" + rest.split("/")[0])
            lines.append(f"mkdir -p {d} && for f in {s}/* {s}/.[!.]* {s}/..?*; do "
                         f"[ -e \"$f\" ] || [ -L \"$f\" ] || continue; [ \"$f\" = {skip} ] && continue; cp -a \"$f\" {d}/; done")
            lines.append(own)
            continue
        lines += [
            f"if [ -d {s} ]; then mkdir -p {d} && cp -a {s}/. {d}/; "
            f"elif [ -e {s} ]; then mkdir -p \"$(dirname {d})\" && cp -a {s} {d}; "
            f"else echo 'nothing there yet; skipped'; mkdir -p {d}; fi",
            own,
        ]
    lines.append("sync; echo done")
    return "\n".join(lines)


def job(ns, name, moves):
    claims = sorted({move["from"] for move in moves} | {move["to"] for move in moves})
    mount_of = {claim: f"/v/{index}" for index, claim in enumerate(claims)}
    job_name = f"{name[:40].rstrip('-')}-restructure-{secrets.token_hex(3)}"
    return job_name, {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job_name, "namespace": ns, "labels": NAMES.labels("restructure", name)},
        "spec": {"backoffLimit": 0,
                 "template": {"metadata": {"labels": NAMES.labels("restructure", name)},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{"name": "copy", "image": IMAGE,
                                                       "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                                                     "limits": {"memory": "256Mi"}},
                                                       "command": ["sh", "-c", script(moves, mount_of)],
                                                       "securityContext": {"runAsUser": 0},
                                                       "volumeMounts": [{"name": f"v{index}", "mountPath": mount_of[claim],
                                                                        "readOnly": not any(m["to"] == claim for m in moves)}
                                                                        for index, claim in enumerate(claims)]}],
                                       "volumes": [{"name": f"v{index}", "persistentVolumeClaim": {"claimName": claim}}
                                                   for index, claim in enumerate(claims)]}}},
    }


def hold(dep):
    """Keep a just-edited workload stopped until its data has been copied."""
    wanted = int(dep["spec"].get("replicas", 1) or 0)
    dep["spec"]["replicas"] = 0
    dep["metadata"].setdefault("annotations", {})[HELD] = str(wanted)
    return wanted


def resolve(item):
    """Legacy records have no receipts: stop automation, preserve all data."""
    from homestead_copy_job import legacy_status
    return legacy_status(item)
