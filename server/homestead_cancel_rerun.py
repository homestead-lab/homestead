"""Which cancels the leader may run again by itself after a restart, and the
delete they use so that a run after a restart never takes a new object of the name.

A Homestead restart during a cancel leaves the job "cancelling"; the leader runs
its canceller again with what the person chose (homestead_operations
._resume_cancel), so a canceller here is run twice, part-way through or after it
has finished. Each one was read for that: a leader change, two replicas, a cancel
half done, a job finished meanwhile. This list is closed: a canceller on neither
side stays with a person, and tests/test_cancel_rerun.py fails until it is added.
Each entry on SAFE is a claim its proof must back: tests/test_cancel_rerun_kinds.py
PROOF names the test, in which the name is used again and the new object survives."""
import urllib.error
import urllib.parse

import homestead_kube as KUBE

# kind -> why running it twice is safe.
SAFE = {
    "backup": "Saves the snapshot's name and both uids before the Backup that names it goes; deletes by those uids.",
    "container-rebalance": "Deletes its pulls by saved uids, ignoring errors; the rest is the job's own record.",
    "deployment": "Saves uids before deleting; a rollback marks its write, and parks if its reviewed target moved.",
    "disk-v2-convert": "Checks the host and disk identity every run, then an idempotent patch.",
    "helm": "Deletes the Job and chart by saved uids; an upgrade's rollback parks if the chart changed since review.",
    "host-bond": "Gives the host's lease back only if this job still holds it; no other write.",
    "host-bridge": "Gives the host's lease back only if this job still holds it; no other write.",
    "host-os": "Gives the host's lease back only if this job still holds it; no other write.",
    "image-cleanup": "Deletes the pods not finished by saved uids.",
    "image-pull": "Deletes the pull DaemonSet by its saved uid.",
    "image-rollback": "Refuses when a rollback exists (a fresh review); otherwise no write.",
    "image-update": "Refuses when a rollback exists (a fresh review); otherwise no write.",
    "import": "Saves what the import Job says, with uids, before it goes, and deletes it and those objects by uid.",
    "import-create": "Deletes the copy Job by uid; 404 is done; a finished copy refuses.",
    "k3s-cluster": "No cluster write: it only stops tracking, and refuses while VMs are being created.",
    "move": "Built to be retried: its record keeps how far it got, and release is fenced by owner and uid.",
    "network-service": "Saves the Service's uid before deleting it and leaves another of that name.",
    "node-power": "Never marked: its plan always refuses.",
    "platform-upgrade": "Plans by saved uids; cordon marks live on nodes; V2 pause sent once, and says if it reads on.",
    "protect-run": "Deletes the Job by its saved uid.",
    "restore-test": "Deletes its copy, fence and claims by saved uids.",
    "restructure": "No cluster write: it only stops tracking.",
    "self-data-move": "Never marked: its plan always refuses.",
    "self-data-prepare": "Never marked: its plan always refuses.",
    "set-priority": "No cluster write: no more apps are changed.",
    "set-requests": "No cluster write: no more apps are changed.",
    "snapshot-revert": "Refuses once a revert was sent; its start-again step is built for lost replies.",
    "unraid-vm-import": "Deletes only what carries this job's stamp.",
    "vm-create": "Never marked: its plan always refuses.",
    "vm-disk-import": "Saves each object's uid before deleting it and leaves another of that name.",
    "vm-edit": "Never marked: its plan always refuses.",
    "vm-migration": "Deletes the migration request by its saved uid.",
    "vm-power": "No cluster write; the dispatcher is fenced by the job's state.",
    "volume-delete": "No write: it only stops tracking.",
    "volume-restore": "Deletes only the claim stamped with this backup, by its saved uid.",
    "workload-copy": "A hold released by uid and resourceVersion; no hold left means nothing to do.",
    "workload-rename": "No cluster write: it only stops tracking.",
}

# kind -> what a person is told: why it was not run again.
PERSON = {
    "vm-disk-remove": "A removal stops at what a person reviewed; its cancel only says what is left, so a person looks again.",
    "host-promote": ("Cancelling this job uncordons the host, and nothing marks the cordon as this job's, so running "
                     "it again could lift a cordon someone set since. The host is left as it is now."),
    "disk-retire": ("Cancelling this job puts back whether the disk takes replicas, and running it again could undo "
                    "a change to the disk made since."),
    "reclass": ("This older kind of move deletes its new volume by name and sets each app's replicas, with nothing "
                "to tell a change made since."),
    "restore-swap": ("Cancelling this job stops the app and sets its replicas, and running it again could undo a "
                     "change made since, or restart the app once more."),
    "volume-copy-move": "Cancelling this job sets the volume's settings back, which could undo a change made since.",
    "volume-rebalance": "Cancelling this job sets the volume's copy count back, which could undo a change made since.",
    "smart-test": "Cancelling this job aborts the disk's self-test, and running it again could abort one started since.",
}


def why_not(kind, cancellers):
    """Why this kind of cancel must not run again by itself, or "" when it may.
    A kind with no canceller only stops being tracked."""
    if kind not in cancellers or kind in SAFE:
        return ""
    return PERSON.get(kind) or "Nobody has checked that this kind of cancel is safe to run twice."


def is_blip(error):
    """A read that failed, as against one that answered: the API server or the network,
    which may answer next time. A 404 is an answer."""
    return isinstance(error, OSError) and getattr(error, "code", None) != 404


def _delete_same(path, uid, send):
    """Delete only the object with this uid. The options go in the body because the
    API server reads a delete's options from its body when it has one, and ignores
    the query. -> whether it went: 404 is gone, 409 another object of that name."""
    bare, _, query = path.partition("?")
    options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
    policy = urllib.parse.parse_qs(query).get("propagationPolicy")
    if policy:
        options["propagationPolicy"] = policy[-1]
    try:
        send("DELETE", bare, options)
        return True
    except urllib.error.HTTPError as error:
        if error.code in (404, 409):
            return False
        raise


def remember(item, read, *paths):
    """Save the uid each path names now, durably, the first time it is asked. A
    path with nothing there is saved as None: what appears there later is new."""
    import homestead_operations as OPS
    uids = item["ref"].setdefault("cancel_uids", {})
    new = [path for path in paths if path not in uids]
    for path in new:
        found = KUBE.get_optional(path.partition("?")[0], read=read)
        uids[path] = ((found or {}).get("metadata") or {}).get("uid")
    if new:
        OPS.keep_cancel_ref(item)
    return uids


def delete_saved(item, read, send, *paths):
    """Delete these, each only if it is the object the first run of this cancel
    saw. All are saved before any goes. -> how many went."""
    uids = remember(item, read, *paths)
    return sum(bool(uids[path] and _delete_same(path, uids[path], send)) for path in paths)


def kept_since(item, read, *paths):
    """Those of these that hold an object other than the one first saw: kept, not gone."""
    uids = item["ref"].get("cancel_uids") or {}
    out = []
    for path in paths:
        found = KUBE.get_optional(path.partition("?")[0], read=read)
        if found and ((found.get("metadata") or {}).get("uid")) != uids.get(path):
            out.append(path.partition("?")[0].rsplit("/", 1)[-1])
    return out
