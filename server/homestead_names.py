"""The names Homestead gives the objects it owns, and the keys it writes on them.

Every object Homestead creates is called homestead-something, and every label
or annotation it writes is homestead.io/something. The helpers here keep that
in one place, so no module spells a name or key its own way.
"""

import urllib.parse

BRAND = "homestead"
DOMAIN = "homestead.io"
NODEPROBE = f"{BRAND}-nodeprobe"

kget = None


def bind(_kget):
    global kget
    kget = _kget


def key(suffix):
    """The annotation or label key to write."""
    return f"{DOMAIN}/{suffix}"


def read(meta, suffix, default=""):
    """An annotation or label value from a dict of them."""
    return (meta or {}).get(key(suffix), default)


def object_name(suffix, namespace=None, kind="configmaps"):
    """What Homestead calls its own object of this kind."""
    return f"{BRAND}-{suffix}"


def labels(task, app=None, **extra):
    """The labels to put on something Homestead creates."""
    made = {key("task"): task}
    if app:
        made[key("app")] = app
    made.update({key(name.replace("_", "-")): value for name, value in extra.items()})
    return made


def find(path, suffix, value=None):
    """Items carrying this label (with this value, when one is given)."""
    query = key(suffix) if value is None else f"{key(suffix)}={value}"
    try:
        return kget(f"{path}?labelSelector={urllib.parse.quote(query)}").get("items", [])
    except Exception:
        return []


def label_of(meta, suffix, default=""):
    """A label on a fetched object."""
    return read((meta or {}).get("labels", {}) or {}, suffix, default)


def annotation_of(meta, suffix, default=""):
    return read((meta or {}).get("annotations", {}) or {}, suffix, default)


def write_annotation(meta, suffix, value):
    """Set an annotation, or clear it with None."""
    annotations = meta.setdefault("annotations", {})
    if value is None:
        annotations.pop(key(suffix), None)
    else:
        annotations[key(suffix)] = value
    return annotations


def nodeprobe_pods(namespace):
    """The probe DaemonSet's pods."""
    try:
        return kget(f"/api/v1/namespaces/{namespace}/pods?labelSelector=app%3D{NODEPROBE}").get("items", [])
    except Exception:
        return []
