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


def _claims(spec):
    return {(volume.get("persistentVolumeClaim") or {}).get("claimName")
            for volume in (spec or {}).get("volumes") or [] if isinstance(volume, dict)} - {None, ""}


def marked(dep):
    """A Deployment a storage copy holds."""
    return bool(((dep.get("metadata") or {}).get("annotations") or {}).get(COPY_JOB.MARKER))


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
    """Why the volume a held copy copies from is not deleted."""
    return (f"The storage copy of {app} is not confirmed. This volume holds {app}'s data from before the edit, "
            f"and undoing the copy needs it. Its job is \"Move {app}'s data\" in Jobs.")


def unread_warning(app, why):
    return (f"The storage copy of {app} is not confirmed. Its record of the volumes from before the edit {why}, "
            "so the volume it copies from is not known.")
