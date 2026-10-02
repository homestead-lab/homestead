"""Cleanup of transfer-owned artifacts, without deleting shared or live data."""
import re
import urllib.error

import homestead_csi_restore as CSI
import homestead_names as NAMES

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
SOURCE_UID = "transfer-source-uid"


def ownership(transfer_id, source_uid):
    if not re.fullmatch(r"[0-9a-f]{12}", str(transfer_id or "")) or not source_uid:
        raise ValueError("Transfer cleanup identity is unavailable")
    return {NAMES.key("move-id"): transfer_id, NAMES.key(SOURCE_UID): source_uid}


def optional(read, path):
    try:
        obj = read(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if not isinstance(obj, dict) or not isinstance(obj.get("metadata"), dict):
        raise ValueError("Cleanup could not verify the resource inventory")
    return obj


def items(read, path, missing=False):
    try:
        result = read(path)
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return []
        raise
    if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
            or (result.get("metadata") or {}).get("continue")
            or any(not isinstance(row, dict) or not isinstance(row.get("metadata"), dict) for row in result["items"])):
        raise ValueError("Cleanup needs a complete resource inventory")
    return result["items"]


def owned(obj, transfer_id, source_uid=None):
    meta = obj.get("metadata") or {}
    return (NAMES.annotation_of(meta, "move-id") == transfer_id
            and (source_uid is None or NAMES.annotation_of(meta, SOURCE_UID) == source_uid))


def delete(read, send, path, obj):
    """Request normal deletion with identity/version fences; wait for finalizers."""
    meta = obj.get("metadata") or {}
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("Cleanup cannot verify the resource identity and version")
    if not meta.get("deletionTimestamp"):
        try:
            send("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions",
                "propagationPolicy": "Background",
                "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}})
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return optional(read, path) is None


def source(read, send, transfer_id, source_uid):
    """Delete only artifacts stamped when this transfer created them."""
    ownership(transfer_id, source_uid)
    backups = items(read, LH + "/backups")
    snapshots = items(read, LH + "/snapshots")
    images = items(read, LH + "/backupbackingimages", missing=True)
    preparing = [s for s in snapshots if not owned(s, transfer_id, source_uid)
                 and NAMES.annotation_of(s["metadata"], "move-id")]
    image_volumes = items(read, LH + "/volumes") if preparing and any(owned(i, transfer_id, source_uid) for i in images) else []
    preparing_images = {v.get("spec", {}).get("backingImage") for v in image_volumes
                        if any(s.get("spec", {}).get("volume") == v["metadata"].get("name") for s in preparing)}
    pending, retained = [], []
    for backup in backups:
        if owned(backup, transfer_id, source_uid):
            name = backup["metadata"]["name"]
            if not delete(read, send, LH + "/backups/" + name, backup):
                pending.append("backup " + name)
    # Do not remove snapshots while any Backup still names them. Re-list after
    # requesting deletion; asynchronous Longhorn finalizers may still be working.
    backups = items(read, LH + "/backups")
    for snapshot in snapshots:
        if not owned(snapshot, transfer_id, source_uid):
            continue
        name = snapshot["metadata"]["name"]
        if name == "volume-head" or any((b.get("spec") or {}).get("snapshotName") == name for b in backups):
            pending.append("snapshot " + name)
        elif not delete(read, send, LH + "/snapshots/" + name, snapshot):
            pending.append("snapshot " + name)
    for image in images:
        if not owned(image, transfer_id, source_uid):
            continue
        name = image["metadata"]["name"]
        if any(owned(b, transfer_id, source_uid) for b in backups):
            pending.append("image backup " + name)
        elif name in preparing_images:
            # Another transfer may have a Snapshot but no Backup CR yet. Its
            # next backup still needs this shared image backup to stay readable.
            pending.append("image backup " + name + " waiting for another transfer")
        elif any((b.get("status") or {}).get("volumeBackingImageName") == name for b in backups):
            retained.append("shared image backup " + name)
        elif any(str((b.get("status") or {}).get("state", "")).lower() not in ("completed", "error", "failed")
                 for b in backups):
            # An unfinished foreign backup may not have discovered its image
            # yet. Missing status is not evidence that the image is unused.
            pending.append("image backup " + name + " waiting for backup inventory")
        elif not delete(read, send, LH + "/backupbackingimages/" + name, image):
            pending.append("image backup " + name)
    return {"ok": True, "complete": not pending, "pending": pending, "retained": retained}


def destination(read, send, move, cancelled=False):
    """Clean restore metadata; unused restored images only after cancellation."""
    pending = CSI.cleanup_transfer(read, send, move["id"], cancelled)
    retained = []
    if cancelled:
        # Longhorn volumes outlive PVC DELETE until detach/reclaim has finished.
        images = items(read, LH + "/backingimages", missing=True)
        images = [image for image in images if owned(image, move["id"])]
        volumes = items(read, LH + "/volumes") if images else []
        for image in images:
            if not owned(image, move["id"]):
                continue
            name = image["metadata"]["name"]
            users = [v for v in volumes if (v.get("spec") or {}).get("backingImage") == name]
            own_volumes = {c.get("cleanup_volume") for c in move.get("claims") or [] if not c.get("cleanup_retain")}
            if any(v["metadata"].get("name") in own_volumes for v in users):
                pending.append("backing image " + name + " waiting for disk reclamation")
            elif users:
                retained.append("Backing image " + name + " retained because another or retained volume uses it")
            elif not delete(read, send, LH + "/backingimages/" + name, image):
                pending.append("backing image " + name)
    return {"complete": not pending, "pending": pending, "retained": retained}
