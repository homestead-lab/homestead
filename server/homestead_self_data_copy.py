"""One-shot, verified filesystem copy for the independent self-data worker.

The caller must have stopped and fenced every writer and recorded its copy
intent first. This module proves matching contents/metadata, not quiescence or
storage durability after a device failure. It never resumes a partial copy.
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys

from homestead_storage_journal import Held


def _roots(source, destination):
    source, destination = os.path.abspath(source), os.path.abspath(destination)
    for path in (source, destination):
        if os.path.islink(path) or not os.path.isdir(path) or not os.path.ismount(path):
            raise Held("Data copy requires two mounted volume roots, not folders or symlinks")
    if os.path.commonpath((source, destination)) in (source, destination) or os.path.samefile(source, destination):
        raise Held("Data copy source and destination overlap")
    return source, destination


def _lost_found(root):
    path = os.path.join(root, "lost+found")
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path) or os.listdir(path)):
        raise Held("A volume contains recovered filesystem data; inspect it before copying")


def _fingerprint(info):
    # The Linux worker uses ctime as a change detector. Windows stat/fstat
    # disagree on creation/change time; it is not a change detector there.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name == "posix" else None)


def inventory(root):
    """Hash regular files without following links; retain link topology and xattrs.

    Entries contain hashes, not contents or xattr values. Filenames remain only
    in memory; only the aggregate digest and totals enter the control record.
    """
    root = os.path.abspath(root)
    device = os.lstat(root).st_dev
    links = {}
    manifest = hashlib.sha256()
    totals = {"files": 0, "bytes": 0}
    _lost_found(root)

    def visit(path, relative):
        before = os.lstat(path)
        if before.st_dev != device:
            raise Held("Data contains a nested filesystem; a complete copy cannot be verified")
        mode = before.st_mode
        row = {"path": relative, "mode": mode, "uid": before.st_uid, "gid": before.st_gid,
               "mtime_ns": before.st_mtime_ns}
        if hasattr(os, "listxattr"):
            row["xattrs"] = {key: hashlib.sha256(os.getxattr(path, key, follow_symlinks=False)).hexdigest()
                             for key in sorted(os.listxattr(path, follow_symlinks=False))}
        if stat.S_ISREG(mode):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if _fingerprint(os.fstat(stream.fileno())) != _fingerprint(before):
                    raise Held("Data changed during verification; the copy remains held")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if _fingerprint(os.fstat(stream.fileno())) != _fingerprint(before):
                    raise Held("Data changed during verification; the copy remains held")
            row.update(size=before.st_size, sha256=digest)
            key = (before.st_dev, before.st_ino)
            if before.st_nlink > 1:
                row["hardlink"] = links.setdefault(key, relative)
            totals["files"] += 1
            totals["bytes"] += before.st_size
        elif stat.S_ISLNK(mode):
            row["link"] = os.readlink(path)
        elif not stat.S_ISDIR(mode):
            raise Held("Data contains a socket, device or other special file; stop its owner and inspect it")
        manifest.update(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
        if stat.S_ISDIR(mode):
            for name in sorted(os.listdir(path)):
                if not relative and name == "lost+found":
                    continue
                visit(os.path.join(path, name), relative + "/" + name if relative else name)
        if _fingerprint(os.lstat(path)) != _fingerprint(before):
            raise Held("Data changed during verification; the copy remains held")

    visit(root, "")
    return {"manifest": manifest.hexdigest(), **totals}


def _sync_tree(root):
    """Flush copied files and directory metadata, not every filesystem on the host."""
    for folder, directories, files in os.walk(root, topdown=False, followlinks=False):
        paths = [os.path.join(folder, name) for name in files] + [folder]
        for path in paths:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                continue  # link metadata is persisted with its parent directory
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise Held("The destination changed while flushing its copied data")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if stat.S_ISDIR(mode) else 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def copy_verified(source, destination, *, timeout=86400):
    """Copy exactly once into an empty claim and compare full inventories.

    Never use --delete, follow source symlinks, or continue into a nonempty
    destination. A crash/timeout is recovered by the control journal, not here.
    """
    try:
        source, destination = _roots(source, destination)
        _lost_found(destination)
        if set(os.listdir(destination)) - {"lost+found"}:
            raise Held("The destination already contains data; the copy will not overwrite or resume it")
        before = inventory(source)
        available = os.statvfs(destination)
        if available.f_bavail * available.f_frsize < before["bytes"]:
            raise Held("The destination filesystem has insufficient free space for this copy")
        subprocess.run(["rsync", "-aHAX", "--numeric-ids", "--modify-window=-1", "--one-file-system", "--fsync",
                        "--exclude=/lost+found", "--", source + "/", destination + "/"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        # Flush filesystem metadata as well as rsync's file fsyncs before the
        # completion receipt. Device/controller guarantees remain external.
        _sync_tree(destination)
        after, copied = inventory(source), inventory(destination)
        if before != after or copied != after:
            raise Held("The copied data or metadata did not match; both volumes remain retained")
        return copied
    except Held:
        raise
    except Exception:
        # Neither raw command diagnostics nor sensitive filenames enter logs.
        raise Held("The data copy could not be verified; inspect the retained volumes and worker. Nothing was retried") from None


def job(namespace, name, source, destination, image, operation, node):
    """A copy-only pod: no API token, no mutable image tag and no automatic retry.

    Node affinity (not nodeName) leaves volume binding to the scheduler. The
    caller must perform joint admission, validate API feature support, and only
    create this Job after its source writers have been verified stopped.
    The coordinator must remove this pod before starting on the destination.
    """
    from homestead_self_data_anchor import _name
    for value in (namespace, name, source, destination, node):
        _name(value)
    if source == destination or not re.fullmatch(r"[a-f0-9]{24}", operation):
        raise Held("The data copy identity or volume selection is invalid")
    if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
        raise Held("The data copy worker requires a verified immutable image digest")
    labels = {"app.kubernetes.io/managed-by": "homestead", "homestead.io/self-data-copy": operation}
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"namespace": namespace, "name": name, "labels": labels},
            "spec": {"backoffLimit": 0, "parallelism": 1, "completions": 1,
                     "podReplacementPolicy": "Failed", "activeDeadlineSeconds": 86400,
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
                         "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                             "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}]}}},
                         "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [{"name": "copy", "image": image,
                             "command": ["python3", "/srv/homestead_self_data_copy.py", operation],
                             "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                           "limits": {"cpu": "1", "memory": "512Mi"}},
                             "securityContext": {"runAsUser": 0, "runAsGroup": 0,
                                 "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                 "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID"]}},
                             "volumeMounts": [{"name": "source", "mountPath": "/source", "readOnly": True},
                                              {"name": "destination", "mountPath": "/destination"}]}],
                         "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": source, "readOnly": True}},
                                     {"name": "destination", "persistentVolumeClaim": {"claimName": destination}}]}}}}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not re.fullmatch(r"[a-f0-9]{24}", args[0]):
        print("Data copy operation identity is invalid", file=sys.stderr)
        return 2
    try:
        receipt = copy_verified("/source", "/destination")
    except Held as error:
        print(str(error), file=sys.stderr)
        return 1
    print("HOMESTEAD_SELF_DATA_COPY " + args[0] + " " + json.dumps(receipt, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
