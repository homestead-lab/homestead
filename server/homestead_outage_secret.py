"""Where an outage webhook's address is kept: a Secret per app or VM (#931).

The address can carry a token anywhere after its host (homestead_outage), and
an annotation on an app's Deployment or a VM can be read by anyone who may
read those, a viewer on the Resources page included. So the address is kept
in a Secret of Homestead's own beside it - homestead-outage-app-<app>, or
homestead-outage-vm-<vm> for a VM, since an app and a VM may share a name -
and the annotation keeps only its scheme, host and port, and the Secret's
name. An address with nothing after its host has nothing to hide: it stays
on the annotation, with no Secret.

A Secret is read only when Homestead keeps it for an outage webhook of that
kind (its label), as homestead_env_secrets does for app variables, so an
annotation pointed at any other Secret reads no address. It records whose it
is (OWNER, kind/name), and only its owner's save replaces it or delete
removes it. A rename clones the annotation, so the renamed app reads the old
name's Secret until the leader moves it to the new name (homestead_outage
migrate). It goes with the app or VM.
"""
import base64

import homestead_kube as KUBE
import homestead_names as NAMES

LABEL = NAMES.key("outage-for")
OWNER = NAMES.key("outage-owner")
DATA_KEY = "webhook"


def secret_name(kind, name):
    """The Secret an app's or VM's webhook address is kept in."""
    return NAMES.fitted(NAMES.object_name(f"outage-{'vm' if kind == 'vm' else 'app'}-{name}"), 253)


def _path(ns, name=""):
    return f"/api/v1/namespaces/{ns}/secrets" + (f"/{name}" if name else "")


def _ours(secret, kind):
    return bool(secret) and ((secret.get("metadata") or {}).get("labels") or {}).get(LABEL) == kind


def owner_of(secret):
    """kind/name of the app or VM a Secret was written for."""
    return (((secret or {}).get("metadata") or {}).get("annotations") or {}).get(OWNER, "")


def read_secret(kind, ns, ref, read):
    """Secret ref when Homestead keeps it for this kind's webhook; else None."""
    if not ref:
        return None
    secret = KUBE.get_optional(_path(ns, ref), read=read)
    return secret if _ours(secret, kind) else None


def address(secret):
    """The address a Secret read with read_secret() holds; None when none."""
    try:
        return base64.b64decode(((secret or {}).get("data") or {})[DATA_KEY]).decode("utf-8")
    except (KeyError, TypeError, ValueError):
        return None


def load(kind, ns, ref, read):
    """The address kept in Secret ref; None when it is not there, cannot be
    read as one, or is not a Secret Homestead keeps for this kind's webhook."""
    return address(read_secret(kind, ns, ref, read))


def current(kind, ns, name, read):
    """The app's or VM's own Secret as it is now; None when there is none."""
    return KUBE.get_optional(_path(ns, secret_name(kind, name)), read=read)


_UNREAD = object()


def save(kind, ns, name, url, read, send, now=_UNREAD):
    """Keep url in the app's or VM's own Secret, made or replaced, before the
    annotation names it. now is the Secret as read earlier (current()): the
    write names what was read - a create fails if one was made meanwhile, a
    replace if it changed - so a write made since is never overwritten.
    Returns {"ref", "before", "written"}: the Secret's name, what it was, and
    what was written (None when nothing changed), for undo()."""
    ref = secret_name(kind, name)
    if now is _UNREAD:
        now = current(kind, ns, name, read)
    if now is not None and not _ours(now, kind):
        raise ValueError(f"Secret {ref} already exists in {ns} and is not the one Homestead keeps "
                         f"for {name}'s outage webhook")
    data = {DATA_KEY: base64.b64encode(url.encode()).decode()}
    labels = {LABEL: kind, "app.kubernetes.io/managed-by": "homestead"}
    owner = {OWNER: f"{kind}/{name}"}
    written = None
    if now is None:
        written = send("POST", _path(ns), {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                           "metadata": {"name": ref, "namespace": ns, "labels": labels,
                                                        "annotations": owner}, "data": data}) or {}
    elif (now.get("data") or {}) != data or owner_of(now) != owner[OWNER]:
        # A replace of the Secret as read: its resourceVersion goes with it.
        new = {**now, "data": data, "metadata": dict(now.get("metadata") or {})}
        meta = new["metadata"]
        meta.pop("managedFields", None)
        meta["labels"] = {**(meta.get("labels") or {}), **labels}
        meta["annotations"] = {**(meta.get("annotations") or {}), **owner}
        written = send("PUT", _path(ns, ref), new) or {}
    return {"ref": ref, "before": now, "written": written}


def _version(obj):
    return ((obj or {}).get("metadata") or {}).get("resourceVersion")


def _delete(ns, ref, version, send):
    body = {"kind": "DeleteOptions", "apiVersion": "v1"}
    if version:
        body["preconditions"] = {"resourceVersion": version}
    KUBE.delete_if_present(_path(ns, ref), body, send=send)


def undo(ns, saved, send):
    """Put the Secret back as it was before save(), when the annotation that
    was to name it could not be written: a Secret it made is deleted, one it
    replaced gets its old address back. Only while nothing has written it
    since (the resourceVersion save() left)."""
    written = (saved or {}).get("written")
    if written is None:
        return
    if saved["before"] is None:
        _delete(ns, saved["ref"], _version(written), send)
    else:
        back = {**saved["before"], "metadata": {**saved["before"]["metadata"], "resourceVersion": _version(written)}}
        back["metadata"].pop("managedFields", None)
        send("PUT", _path(ns, saved["ref"]), back)


def remove(kind, ns, ref, read, send, owner):
    """Delete Secret ref - only one Homestead keeps for this kind's webhook,
    written for owner (kind/name), and as it was read."""
    secret = read_secret(kind, ns, ref, read)
    if secret and owner_of(secret) == owner:
        _delete(ns, ref, _version(secret), send)
