"""
Longhorn backups: listing them and the volumes they hold, making one, the
snapshot and backup a cluster move makes, and deleting one.

Split out of homestead_longhorn (#623); what bind() sets, the shared helpers
and the routes stay there.
"""
import json
import homestead_kube as KUBE
import homestead_names as NAMES
import math
import urllib.error
import urllib.parse


def backups(volume=None, strict=False):
    """Longhorn backups, newest first: one volume's, or every one. One volume's
    are asked for by Longhorn's backup-volume label, so the API server sends
    only those rather than the whole list."""
    path = f"{LH.API}/namespaces/{LH.LHNS}/backups"
    if volume:
        path += "?labelSelector=" + urllib.parse.quote(f"backup-volume={volume}", safe="")
    try:
        listing = LH.kget(path)
        if strict and (not isinstance(listing, dict) or not isinstance(listing.get("items"), list)):
            raise ValueError("backup inventory is incomplete; retry after Longhorn responds")
        items = listing.get("items", [])
    except Exception:
        if strict:
            raise
        return []
    out = []
    for b in items:
        st = b.get("status", {}) or {}
        labels = b["metadata"].get("labels", {}) or {}
        vol = labels.get("backup-volume", "") or st.get("volumeName", "")
        if volume and vol != volume:
            continue
        out.append({
            "name": b["metadata"]["name"], "volume": vol,
            "state": st.get("state", ""), "progress": st.get("progress", 0),
            "size_mb": round(int(st.get("size", 0) or 0) / 1048576, 1),
            "volume_size_gb": max(1, math.ceil(int(st.get("volumeSize", 0) or 0) / 1073741824)),
            "created": st.get("backupCreatedAt", ""),
            "error": st.get("error", ""),
            "target": st.get("backupTargetName", "default") or "default",
            "restorable": str(st.get("state", "")).lower() == "completed" and bool(st.get("url")),
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)


def create_backup(volume, name=None):
    tgt = BACKUP_TARGET.backup_target()
    if not tgt.get("configured"):
        raise ValueError("no backup target configured — set one before backing up")
    snap = VOLUMES.create_snapshot(volume, name)["snapshot"]
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": {"generateName": "homestead-", "namespace": LH.LHNS,
                         "labels": {"backup-volume": volume, NAMES.key("managed"): "true"}},
            "spec": {"snapshotName": snap, "labels": {"homestead": "manual"}}}
    out = LH.ksend("POST", f"{LH.API}/namespaces/{LH.LHNS}/backups", body)
    LH._bust("lhbackups")
    return {"ok": True, "backup": out.get("metadata", {}).get("name", ""),
            "snapshot": snap, "volume": volume}


def move_snapshot_error(snapshot):
    """Longhorn reports attachment waits as errors while creating a snapshot."""
    status = (snapshot or {}).get("status") or {}
    if status.get("markRemoved") or (snapshot or {}).get("metadata", {}).get("deletionTimestamp"):
        return "Snapshot was removed"
    error = str(status.get("error") or "")
    if (not status.get("creationTime") and
            error.startswith("failed to take snapshot because the volume engine ") and
            error.endswith(". Waiting for the volume to be attached")):
        return ""
    return error


def ensure_move_backup(volume, snapshot, backup, cleanup_id="", source_uid=""):
    """Advance a persisted migration request without waiting in an HTTP call."""
    if not all(RESTORE._valid_k8s_name(n) for n in (volume, snapshot, backup)):
        raise ValueError("migration backup identity is invalid")
    annotations = {}
    if cleanup_id:
        from homestead_transfer_cleanup import ownership
        annotations = ownership(cleanup_id, source_uid)
    path = f"{LH.API}/namespaces/{LH.LHNS}/backups/{backup}"
    existing = KUBE.get_optional(path, read=LH.kget)
    if existing:
        if ((existing.get("spec") or {}).get("snapshotName") != snapshot or
                (existing.get("metadata", {}).get("labels") or {}).get("backup-volume") != volume):
            raise ValueError("migration backup exists with a different snapshot or volume")
        return {"backup": backup}
    snap = KUBE.get_optional(f"{LH.API}/namespaces/{LH.LHNS}/snapshots/{snapshot}", read=LH.kget)
    if not snap:
        try:
            VOLUMES.create_snapshot(volume, snapshot, annotations=annotations)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
        return {"backup": backup, "pending": True}
    if (snap.get("spec") or {}).get("volume") != volume:
        raise ValueError("migration snapshot belongs to a different volume")
    status = snap.get("status") or {}
    problem = move_snapshot_error(snap)
    if problem:
        raise ValueError(f"migration snapshot is unavailable: {problem}; retry the move for a fresh snapshot")
    if not status.get("readyToUse"):
        return {"backup": backup, "pending": True}
    if not BACKUP_TARGET.backup_target().get("configured"):
        raise ValueError("no backup target configured")
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Backup",
            "metadata": {"name": backup, "namespace": LH.LHNS,
                         "labels": {"backup-volume": volume, NAMES.key("managed"): "true"}},
            "spec": {"snapshotName": snapshot, "labels": {"homestead": "migration"}}}
    if annotations:
        body["metadata"]["annotations"] = annotations
    try:
        LH.ksend("POST", f"{LH.API}/namespaces/{LH.LHNS}/backups", body)
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
        existing = LH.kget(path)
        if ((existing.get("spec") or {}).get("snapshotName") != snapshot or
                (existing.get("metadata", {}).get("labels") or {}).get("backup-volume") != volume):
            raise ValueError("migration backup exists with a different snapshot or volume")
    LH._bust("lhbackups")
    return {"backup": backup}


def backup_volumes():
    """Every volume that has backups, the deleted ones included.

    A backup outlives its volume - that is its point - so this lists what
    the backup target holds, not what the cluster still has."""
    try:
        items = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/backupvolumes").get("items", [])
    except Exception:
        return []
    live = {v["name"]: v for v in VOLUMES._volumes()}
    counts = {}
    for b in backups():
        counts[b["volume"]] = counts.get(b["volume"], 0) + 1
    out = []
    for item in items:
        meta, status = item.get("metadata", {}) or {}, item.get("status", {}) or {}
        name = ((meta.get("labels") or {}).get("backup-volume") or (item.get("spec") or {}).get("volumeName")
                or status.get("volumeName") or meta.get("name", ""))
        volume = live.get(name)
        out.append({
            "name": name, "id": meta.get("name", ""),
            "pvc": (volume or {}).get("pvc", "") or _pvc_of_backup(status),
            "exists": volume is not None,
            "last_backup": status.get("lastBackupName", ""),
            "last_backup_at": status.get("lastBackupAt", ""),
            "size_mb": round(int(status.get("size", 0) or 0) / 1048576, 1),
            "count": counts.get(name, 0),
            "target": status.get("backupTargetName", "") or (item.get("spec") or {}).get("backupTargetName", "") or "default",
        })
    return sorted(out, key=lambda x: (x["exists"], x["name"]))


def _pvc_of_backup(status):
    """The claim a backed-up volume belonged to, as Longhorn wrote it down."""
    try:
        return (json.loads((status.get("labels") or {}).get("KubernetesStatus") or "{}") or {}).get("pvcName", "")
    except (TypeError, ValueError):
        return ""


def delete_backup(name):
    """Delete one backup - from the backup target too, which Longhorn does."""
    RESTORE._backup(name)
    LH.ksend("DELETE", f"{LH.API}/namespaces/{LH.LHNS}/backups/{name}")
    LH._bust("lhbackups", "lhbackupvols", "lhov")
    return {"ok": True, "backup": name}


# Last, so that importing any of these modules first works: homestead_longhorn
# imports this one.
import homestead_longhorn_volumes as VOLUMES  # noqa: E402
import homestead_longhorn_target as BACKUP_TARGET  # noqa: E402
import homestead_longhorn_restore as RESTORE  # noqa: E402
import homestead_longhorn as LH  # noqa: E402
