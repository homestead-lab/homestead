"""Bringing a container's data along when its storage is restructured.

The editor can point a path at a different volume, or a different folder of
one: two volumes combined into folders of one, one volume split into several,
a folder renamed. Remounting alone would leave the app looking at an empty
folder, so an edit that asks for it runs as a tracked job:

  1. the edit is saved with the workload held at zero replicas, and the count
     it should run at parked in an annotation;
  2. once its pods are gone, a Job copies each old location to its new one;
  3. the workload is scaled back to the parked count.

The old volumes are never touched: they are only read, and nothing here
deletes them, so a mistake is undone by pointing the paths back. The steps
advance from the operations poll, which the alert loop runs every few seconds
whether or not anyone is watching, and every step is recorded, so a Homestead
restart part-way picks up where it stopped.
"""
import re
import secrets
import shlex
import urllib.error
import urllib.parse

import homestead_names as NAMES

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
    if folder and (not FOLDER.match(folder) or ".." in folder.split("/")):
        raise ValueError(f"{where}: {folder} is not a folder name this can copy")
    return folder


def _inside(a, b):
    """True when folder a is b or within it; "" is the whole volume."""
    return not b or a == b or a.startswith(b + "/")


def copies(cfg):
    """The data moves an edit asks for, one per mount whose storage changed.

    A row asks by carrying copy_from, the claim and folder it was mounted from
    when the editor opened. Only claims are copied: a host path or scratch
    volume has nothing Homestead should carry."""
    out, seen = [], set()
    for change in cfg.get("containers") or []:
        for row in change.get("volumes") or []:
            origin = row.get("copy_from") or {}
            source = str(origin.get("claim") or "").strip()
            if not source:
                continue
            path = str(row.get("path") or "").strip()
            kind = str(row.get("kind") or "")
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
            key = (source, src, target, dst)
            if key not in seen:
                seen.add(key)
                out.append({"path": path, "from": source, "from_folder": src,
                            "to": target, "to_folder": dst})
    targets = {}
    for move in out:
        spot = (move["to"], move["to_folder"])
        if spot in targets and targets[spot] != (move["from"], move["from_folder"]):
            raise ValueError(f"two paths would be copied into {move['to']}/{move['to_folder']}")
        targets[spot] = (move["from"], move["from_folder"])
    return out


def script(moves, mount_of):
    """The copy: each old location into its new one, keeping owners and times.

    cp -a as root keeps the numeric owner, so an app finds its files as it
    left them. A location that was never written has nothing to bring."""
    lines = ["set -e"]
    for index, move in enumerate(moves, 1):
        src = mount_of[move["from"]] + ("/" + move["from_folder"] if move["from_folder"] else "")
        dst = mount_of[move["to"]] + ("/" + move["to_folder"] if move["to_folder"] else "")
        label = (f"{move['from']}/{move['from_folder']}".rstrip("/") + " -> " +
                 f"{move['to']}/{move['to_folder']}".rstrip("/"))
        s, d = shlex.quote(src), shlex.quote(dst)
        lines.append(f"echo {shlex.quote(f'[{index}/{len(moves)}] {label}')}")
        if move["from"] == move["to"] and _inside(move["to_folder"], move["from_folder"]):
            # Into a folder of itself, as when a whole volume becomes one
            # folder of it: everything but the folder being filled.
            rest = move["to_folder"][len(move["from_folder"]):].strip("/")
            skip = shlex.quote(src + "/" + rest.split("/")[0])
            lines.append(f"mkdir -p {d} && for f in {s}/* {s}/.[!.]* {s}/..?*; do "
                         f"[ -e \"$f\" ] || continue; [ \"$f\" = {skip} ] && continue; cp -a \"$f\" {d}/; done")
            continue
        lines += [
            f"if [ -d {s} ]; then mkdir -p {d} && cp -a {s}/. {d}/; "
            f"elif [ -e {s} ]; then mkdir -p \"$(dirname {d})\" && cp -a {s} {d}; "
            f"else echo 'nothing there yet; skipped'; fi",
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
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 86400,
                 "template": {"metadata": {"labels": NAMES.labels("restructure", name)},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{"name": "copy", "image": IMAGE,
                                                       "command": ["sh", "-c", script(moves, mount_of)],
                                                       "securityContext": {"runAsUser": 0},
                                                       "volumeMounts": [{"name": f"v{index}", "mountPath": mount_of[claim]}
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


def _pods(ns, dep):
    labels = ((dep.get("spec", {}) or {}).get("selector", {}) or {}).get("matchLabels", {}) or {}
    if not labels:
        return []
    selector = urllib.parse.quote(",".join(f"{k}={v}" for k, v in sorted(labels.items())), safe="")
    return kget(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}").get("items", [])


def _release(ns, name, replicas):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    dep["spec"]["replicas"] = replicas
    dep["metadata"].setdefault("annotations", {}).pop(HELD, None)
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)


def _failure(ns, job_name):
    """The copy's last words, so a failure says why."""
    try:
        selector = urllib.parse.quote(f"job-name={job_name}", safe="")
        pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}").get("items", [])
        if pods:
            text = ktext(f"/api/v1/namespaces/{ns}/pods/{pods[0]['metadata']['name']}/log?tailLines=3")
            return " ".join(str(text).split())[-240:]
    except Exception:
        pass
    return ""


def resolve(item):
    """Advances a restructure one step, for the operations poll."""
    ref = item["ref"]
    ns, name, moves = ref["namespace"], ref["name"], ref.get("moves") or []
    replicas = int(ref.get("replicas") or 0)
    try:
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "failed", item.get("progress", 0), f"{name} no longer exists, so its data was not copied"
        raise
    phase = ref.get("phase") or "stopping"
    if phase == "stopping":
        running = _pods(ns, dep)
        if running:
            return "running", 10, f"Waiting for {name} to stop ({len(running)} pod{'s' if len(running) != 1 else ''} left)"
        job_name, body = job(ns, name, moves)
        ksend("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", body)
        ref.update(phase="copying", job=job_name)
        return "running", 25, f"Copying {len(moves)} location{'s' if len(moves) != 1 else ''}"
    if phase == "copying":
        try:
            status = kget(f"/apis/batch/v1/namespaces/{ns}/jobs/{ref['job']}").get("status", {}) or {}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return ("failed", 25, f"The copy job went missing; {name} stays stopped. "
                        "Its old volumes are untouched.")
            raise
        if status.get("succeeded"):
            _release(ns, name, replicas)
            ref.update(phase="done")
            return ("succeeded", 100, f"Data copied; {name} is starting" if replicas
                    else f"Data copied; {name} stays stopped, as it was set to")
        if status.get("failed"):
            why = _failure(ns, ref["job"])
            return ("failed", 60, f"The copy failed, so {name} stays stopped and its old volumes are "
                    f"untouched. Point the paths back, or fix and start it." + (f" Last output: {why}" if why else ""))
        return "running", 40 if status.get("active") else 30, "Copying data" if status.get("active") else "Starting the copy"
    return "succeeded", 100, item.get("message", "")
