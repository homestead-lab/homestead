"""ISO images for VMs' CD-ROM drives, from folders on the SMB shares.

An admin picks folders on existing Network Shares; every .iso in them is
listed. A VM's CD-ROM cannot read one file on a shared volume, so an ISO is
made ready once: copied into a volume of its own, as the disk.img KubeVirt
reads from a filesystem volume. That volume is then attached read-only to any
VM that wants it - every VM in its namespace shares the one copy, on any node
when the storage class allows many (ReadWriteMany).

The copy is a Job beside the share's volume; the ISO volume says where it
came from (share, path, size) so a changed file is copied again under a new
name, and it is ready once the Job has finished. An ISO volume a VM still
uses is not deleted.
"""
import hashlib
import json
import posixpath
import re
import urllib.error

import homestead_names as NAMES

kget = ksend = None
shares = None            # () -> the Network Shares rows (name, pvc, sub_path, ...)
list_files = None        # (namespace, pvc, path) -> {"entries": [...]}
pick_class = None        # () -> (storage class, supports ReadWriteMany)
share_node = None        # () -> the node the SMB server runs on, for a ReadWriteOnce share
SHARE_NS = "lab"
DEFAULT_NS = "lab"
IMAGE = "alpine:3.20"
CONFIGMAP = "homestead-iso-library"
LABEL = "homestead.io/iso"
SOURCE = "homestead.io/iso-source"
SIZE = "homestead.io/iso-size"
FILE = "homestead.io/iso-file"
READY = "homestead.io/iso-ready"
QEMU = "107"             # the user KubeVirt's launcher runs QEMU as
DNS = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
GIB = 1024 ** 3


def bind(_kget, _ksend, _shares, _list_files, _pick_class, _share_node, share_ns="lab", default_ns="lab",
         image="alpine:3.20"):
    global kget, ksend, shares, list_files, pick_class, share_node, SHARE_NS, DEFAULT_NS, IMAGE
    kget, ksend, shares, list_files = _kget, _ksend, _shares, _list_files
    pick_class, share_node, SHARE_NS, DEFAULT_NS, IMAGE = _pick_class, _share_node, share_ns, default_ns, image


def _optional(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _clean(path):
    raw = str(path or "").replace("\\", "/").strip().strip("/")
    if not raw:
        return ""
    if any(part in ("..", ".") for part in raw.split("/")):
        raise ValueError("a folder path cannot climb out of its share")
    cleaned = posixpath.normpath(raw)
    if len(cleaned) > 512:
        raise ValueError("that path is too long")
    return cleaned


# ------------------------------------------------------------------ folders
def folders():
    """The share folders the library reads: [{share, path}]."""
    cm = _optional(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{CONFIGMAP}") or {}
    try:
        rows = json.loads((cm.get("data") or {}).get("folders") or "[]")
    except ValueError:
        rows = []
    return [{"share": str(r.get("share") or ""), "path": _clean(r.get("path"))}
            for r in rows if isinstance(r, dict) and r.get("share")]


def set_folders(rows):
    known = {s["name"]: s for s in shares()}
    clean, seen = [], set()
    for row in rows or []:
        share, path = str((row or {}).get("share") or ""), _clean((row or {}).get("path"))
        if share not in known:
            raise ValueError(f"there is no share named {share}")
        if (share, path) not in seen:
            seen.add((share, path))
            clean.append({"share": share, "path": path})
    if len(clean) > 20:
        raise ValueError("at most 20 ISO folders")
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CONFIGMAP, "namespace": DEFAULT_NS, "labels": {NAMES.key("managed"): "true"}},
            "data": {"folders": json.dumps(clean)}}
    path = f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{CONFIGMAP}"
    if _optional(path) is None:
        ksend("POST", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps", body)
    else:
        ksend("PUT", path, body)
    return {"ok": True, "folders": clean,
            "detail": f"{len(clean)} ISO folder{'' if len(clean) == 1 else 's'} saved"}


def _share(name):
    row = next((s for s in shares() if s.get("name") == name), None)
    if not row or not row.get("pvc"):
        raise ValueError(f"the share {name} is not there any more")
    return row


def _in_volume(share, relative):
    """A path in the share's volume: the share's own sub-folder, then the rest."""
    return _clean(posixpath.join(_clean(share.get("sub_path")), relative))


def iso_files():
    """Every .iso in the chosen folders, one level deep, with any problem
    reading a folder said beside it."""
    out, problems = [], []
    for folder in folders():
        try:
            share = _share(folder["share"])
            listing = list_files(SHARE_NS, share["pvc"], _in_volume(share, folder["path"]))
        except Exception as error:
            problems.append({"share": folder["share"], "path": folder["path"], "error": str(error)[:200]})
            continue
        for entry in listing.get("entries") or []:
            if entry.get("kind") == "file" and entry.get("name", "").lower().endswith(".iso"):
                out.append({"share": folder["share"], "folder": folder["path"], "name": entry["name"],
                            "path": posixpath.join(folder["path"], entry["name"]) if folder["path"] else entry["name"],
                            "size": int(entry.get("size") or 0)})
    return sorted(out, key=lambda row: row["name"].lower()), problems


# ------------------------------------------------------------------ volumes
def volume_name(share, path, size):
    """One name per file and size: a file replaced by a new one of another
    size is copied again rather than served stale."""
    stem = re.sub(r"[^a-z0-9]+", "-", posixpath.basename(path).lower().removesuffix(".iso")).strip("-")[:40] or "image"
    digest = hashlib.sha256(f"{share}/{path}/{size}".encode()).hexdigest()[:8]
    return f"iso-{stem}-{digest}"


def _jobs(ns):
    try:
        return {j["metadata"]["name"]: j for j in kget(f"/apis/batch/v1/namespaces/{ns}/jobs?labelSelector={LABEL}%3Dtrue").get("items", [])}
    except Exception:
        return {}


def _users(ns):
    """ISO volume -> the VMs in its namespace that have it in a drive."""
    users = {}
    try:
        vms = kget(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines").get("items", [])
    except Exception:
        vms = []
    for vm in vms:
        for volume in ((vm.get("spec") or {}).get("template", {}).get("spec") or {}).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                users.setdefault(claim, []).append(vm["metadata"]["name"])
    return users


def _state(pvc, job):
    annotations = (pvc.get("metadata") or {}).get("annotations") or {}
    if annotations.get(READY) == "true":
        return "ready", ""
    status = (job or {}).get("status") or {}
    if status.get("succeeded"):
        return "ready", ""
    if status.get("failed") and not status.get("active"):
        return "failed", "the copy failed; delete this ISO volume and make it ready again"
    if not job:
        return "failed", "its copy job is gone before finishing; delete this ISO volume and make it ready again"
    return "copying", ""


def volumes(ns=None):
    ns = ns or DEFAULT_NS
    try:
        pvcs = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims?labelSelector={LABEL}%3Dtrue").get("items", [])
    except Exception:
        pvcs = []
    jobs, users = _jobs(ns), _users(ns)
    out = []
    for pvc in pvcs:
        meta = pvc["metadata"]
        annotations = meta.get("annotations") or {}
        state, problem = _state(pvc, jobs.get(meta["name"]))
        if state == "ready" and annotations.get(READY) != "true":
            _mark_ready(ns, meta["name"])
        out.append({"name": meta["name"], "namespace": ns, "file": annotations.get(FILE, ""),
                    "source": annotations.get(SOURCE, ""), "size": int(annotations.get(SIZE) or 0),
                    "state": state, "problem": problem, "used_by": sorted(users.get(meta["name"], [])),
                    "rwx": "ReadWriteMany" in ((pvc.get("spec") or {}).get("accessModes") or [])})
    return sorted(out, key=lambda row: row["file"].lower())


def _mark_ready(ns, name):
    try:
        ksend("PATCH", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}",
              {"metadata": {"annotations": {READY: "true"}}}, ctype="application/merge-patch+json")
        ksend("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}?propagationPolicy=Background")
    except Exception:
        pass


def library(ns=None):
    files, problems = iso_files()
    vols = volumes(ns)
    by_source = {v["source"]: v for v in vols}
    for row in files:
        volume = by_source.get(f"{row['share']}/{row['path']}")
        row["volume"] = volume["name"] if volume and volume["size"] == row["size"] else ""
        row["state"] = (volume or {}).get("state", "") if row["volume"] else ""
    return {"folders": folders(), "files": files, "problems": problems, "volumes": vols,
            "shares": [{"name": s["name"], "pvc": s.get("pvc", ""), "sub_path": s.get("sub_path", "")}
                       for s in shares()]}


def browse(share, path=""):
    """The sub-folders of a share folder, to choose one for the library."""
    row = _share(share)
    listing = list_files(SHARE_NS, row["pvc"], _in_volume(row, _clean(path)))
    return {"share": share, "path": _clean(path),
            "folders": [e["name"] for e in listing.get("entries") or [] if e.get("kind") == "dir"],
            "isos": sum(1 for e in listing.get("entries") or []
                        if e.get("kind") == "file" and e.get("name", "").lower().endswith(".iso"))}


def prepare(share, path, ns=None):
    """Copy one ISO from its share into a volume VMs can attach. Returns the
    volume's name; nothing is copied twice."""
    ns = ns or DEFAULT_NS
    if not DNS.fullmatch(ns):
        raise ValueError("that is not a namespace name")
    if ns != SHARE_NS:
        # The copy mounts the share's volume, which lives beside the SMB server.
        raise ValueError(f"ISO volumes are made in {SHARE_NS}, the shares' namespace")
    path = _clean(path)
    if not path.lower().endswith(".iso"):
        raise ValueError("only .iso files can go in a CD-ROM drive")
    folder = _clean(posixpath.dirname(path))
    if not any(f["share"] == share and f["path"] == folder for f in folders()):
        raise ValueError("that file is not in one of the ISO folders")
    file = next((f for f in iso_files()[0] if f["share"] == share and f["path"] == path), None)
    if not file:
        raise ValueError(f"{path} is not on {share} any more")
    name = volume_name(share, path, file["size"])
    if _optional(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}"):
        return {"ok": True, "name": name, "detail": f"{file['name']} is already a volume"}
    klass, rwx = pick_class()
    # The copy, and a filesystem's own room: disk.img must fit whole.
    gib = max(1, -(-int(file["size"] * 1.08 + 256 * 1024 ** 2) // GIB))
    labels = {LABEL: "true", NAMES.key("managed"): "true"}
    annotations = {SOURCE: f"{share}/{path}", SIZE: str(file["size"]), FILE: file["name"]}
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
           "metadata": {"name": name, "namespace": ns, "labels": labels, "annotations": annotations},
           "spec": {"accessModes": ["ReadWriteMany" if rwx else "ReadWriteOnce"], "volumeMode": "Filesystem",
                    "resources": {"requests": {"storage": f"{gib}Gi"}},
                    **({"storageClassName": klass} if klass else {})}}
    ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", pvc)
    row = _share(share)
    source = "/share/" + _in_volume(row, path)
    script = ("set -e; cp \"$SRC\" /iso/disk.img.part; mv /iso/disk.img.part /iso/disk.img; "
              f"chown {QEMU}:{QEMU} /iso/disk.img; chmod 0444 /iso/disk.img; sync")
    spec = {"restartPolicy": "Never",
            "containers": [{"name": "copy", "image": IMAGE, "command": ["sh", "-c", script],
                            "env": [{"name": "SRC", "value": source}],
                            "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}},
                            "volumeMounts": [{"name": "share", "mountPath": "/share", "readOnly": True},
                                             {"name": "iso", "mountPath": "/iso"}]}],
            "volumes": [{"name": "share", "persistentVolumeClaim": {"claimName": row["pvc"], "readOnly": True}},
                        {"name": "iso", "persistentVolumeClaim": {"claimName": name}}]}
    node = share_node() if callable(share_node) else ""
    if node and "ReadWriteMany" not in (row.get("access_modes") or []):
        # A share's volume one node can mount: the copy runs where it is.
        spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    job = {"apiVersion": "batch/v1", "kind": "Job",
           "metadata": {"name": name, "namespace": ns, "labels": labels, "annotations": {FILE: file["name"]}},
           "spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 86400, "template": {
               "metadata": {"labels": {**labels, **NAMES.labels("iso-copy")}}, "spec": spec}}}
    try:
        ksend("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", job)
    except Exception:
        ksend("DELETE", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
        raise
    return {"ok": True, "name": name, "job": name,
            "detail": f"Copying {file['name']} into a volume; it can go in a CD-ROM drive once ready"}


def ready_volume(ns, name):
    """An ISO volume a drive can hold now, or ValueError saying why not."""
    pvc = _optional(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    if not pvc or ((pvc.get("metadata") or {}).get("labels") or {}).get(LABEL) != "true":
        raise ValueError(f"{name} is not an ISO volume in {ns}")
    state, problem = _state(pvc, _jobs(ns).get(name))
    if state != "ready":
        raise ValueError(problem or f"{name} is still being copied; attach it once it is ready")
    return pvc


def delete(name, ns=None):
    ns = ns or DEFAULT_NS
    users = _users(ns).get(name, [])
    if users:
        raise ValueError(f"{', '.join(sorted(users))} still {'has' if len(users) == 1 else 'have'} it in a "
                         "CD-ROM drive; eject it there first")
    pvc = _optional(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    if not pvc or ((pvc.get("metadata") or {}).get("labels") or {}).get(LABEL) != "true":
        raise ValueError(f"{name} is not an ISO volume")
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}?propagationPolicy=Background")
    except Exception:
        pass
    ksend("DELETE", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    return {"ok": True, "detail": f"{(pvc['metadata'].get('annotations') or {}).get(FILE, name)}'s volume deleted; "
                                  "the file on the share is kept"}
