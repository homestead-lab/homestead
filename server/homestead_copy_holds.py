"""The volumes a storage copy that is not confirmed still needs (#1525).

Moving an app's data saves the edit with the app stopped, its settings already
naming the new volume, and marks the Deployment (homestead_copy_job.MARKER).
Until the copy is confirmed, undone or its hold released, the volume it copies
from is the app's data from before the edit, and undoing the copy needs it -
yet the app's settings no longer name it. What the settings named before the
edit is in the Secret the copy keeps beside the Deployment. That is read here,
not the job's record: it is on the cluster, so it is there as long as the mark
is, even when the job's record is lost.

Only claim names are taken from the Secret. Its other values (env and the
rest) can be credentials, so none goes into a message, the page or a log.
"""
import base64
import binascii
import json
import urllib.error

import homestead_copy_job as COPY_JOB
import homestead_restructure as RESTRUCTURE


def _claims(spec):
    return {(volume.get("persistentVolumeClaim") or {}).get("claimName")
            for volume in (spec or {}).get("volumes") or [] if isinstance(volume, dict)} - {None, ""}


def marked(dep):
    """A Deployment a storage copy holds: its mark, and the hold count the
    edit saved with it (RESTRUCTURE.HELD). A storage class move marks the
    apps it stops with the same mark and no count; they still name their
    volume, and it keeps no record of before."""
    annotations = (dep.get("metadata") or {}).get("annotations") or {}
    return bool(annotations.get(COPY_JOB.MARKER)) and RESTRUCTURE.HELD in annotations


def _earlier(dep, read):
    """The claims the app's settings named before the edit, or the words for why they are not known."""
    meta = dep.get("metadata") or {}
    path = f"/api/v1/namespaces/{meta.get('namespace')}/secrets/{COPY_JOB.earlier_name(meta.get('uid') or '')}"
    try:
        secret = read(path)
    except urllib.error.HTTPError as error:
        return None, "is missing" if error.code == 404 else f"could not be read (HTTP {error.code})"
    except Exception:
        return None, "could not be read"
    try:
        owners = {owner.get("uid") for owner in (secret.get("metadata") or {}).get("ownerReferences") or []}
        if not meta.get("uid") or meta["uid"] not in owners:
            return None, "belongs to another app"
        record = json.loads(base64.b64decode(secret["data"]["earlier"], validate=True))
        claims = _claims(record["template"]["spec"])
        if any(not isinstance(name, str) for name in claims):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError, binascii.Error):
        # Nothing of the record goes into the words: not even the parser's.
        return None, "could not be read"
    return claims, ""


def needed(deployments, read):
    """For the Deployments a storage copy holds: {(namespace, claim): [app]}
    for each claim the app named before the edit and does not name now, and
    [(namespace, app, why)] for each app whose record could not be read.
    One read per held app; none when no app is held."""
    held, unread = {}, []
    for dep in deployments:
        if not marked(dep):
            continue
        meta = dep.get("metadata") or {}
        ns, app = meta.get("namespace", ""), meta.get("name", "")
        before, why = _earlier(dep, read)
        if before is None:
            unread.append((ns, app, why))
            continue
        now = _claims(((dep.get("spec") or {}).get("template") or {}).get("spec"))
        for claim in sorted(before - now):
            held.setdefault((ns, claim), []).append(app)
    return held, unread


def reason(app):
    """Why the volume a held copy copies from is not deleted. True of a copy
    still running and of one kept stopped: the hold ends only when the copy
    is confirmed, undone or released."""
    return (f"The storage copy of {app} has not finished. This volume holds {app}'s data from before the edit, "
            f"and undoing the copy needs it. Its job is \"Move {app}'s data\" in Jobs.")


def unread_warning(app, why):
    """Why the volume a held copy copies from is not known, and the way out:
    the copy's own release, or, when its job is gone or the app was created
    again (a new uid, so no record and a release that refuses), both
    annotations: the hold count left alone still stops a start and a new copy."""
    return (f"The storage copy of {app} has not finished. Its record of the volumes from before the edit {why}, "
            f"so the volume it copies from is not known. Release the hold from the job \"Move {app}'s data\" in Jobs "
            "(Check outcome, then Release hold and stop tracking). If that job is gone or says the app was replaced, "
            f"remove the annotations {COPY_JOB.MARKER} and {RESTRUCTURE.HELD} from the app in Kubernetes. "
            "The app then stays stopped until it is scaled up.")
