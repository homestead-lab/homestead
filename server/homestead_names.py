"""The names Homestead gives the objects it owns, and the ones it used to.

Homestead was called harvUI, and an install made under that name is still full
of objects called harvui-something and annotated harvui.io/something. Renaming
them in place would mean a migration, a flag day, and a window where a half
renamed install is neither one thing nor the other.

So nothing is renamed. A new install creates homestead-* objects and writes
homestead.io/* keys; an install that already has the old ones keeps using them,
because a lookup answers with whichever is actually there. The old names are
read for as long as anyone still has them, and nobody has to do anything.
"""

import urllib.parse

BRAND = "homestead"
LEGACY = "harvui"
DOMAIN = "homestead.io"
LEGACY_DOMAIN = "harvui.io"

kget = None


def bind(_kget):
    global kget
    kget = _kget


def key(suffix):
    """The annotation or label key to write."""
    return f"{DOMAIN}/{suffix}"


def legacy_key(suffix):
    return f"{LEGACY_DOMAIN}/{suffix}"


def both_keys(suffix):
    return (key(suffix), legacy_key(suffix))


def read(meta, suffix, default=""):
    """An annotation, under whichever domain this object carries."""
    values = (meta or {})
    for candidate in both_keys(suffix):
        if candidate in values:
            return values[candidate]
    return default


def selector(suffix, value):
    """A label selector matching either domain.

    Kubernetes has no OR in a label selector, so a caller wanting both has to
    ask twice; this returns the two queries in the order to try them.
    """
    return [f"{candidate}={value}" for candidate in both_keys(suffix)]


def object_name(suffix, namespace=None, kind="configmaps"):
    """Where this object lives: the old name if it exists, else the new one.

    Writing to whichever already exists is what keeps an upgrade from quietly
    forking an install's settings into two ConfigMaps, one of them ignored.
    """
    new, old = f"{BRAND}-{suffix}", f"{LEGACY}-{suffix}"
    if not kget or not namespace:
        return new

    def present(name):
        try:
            found = kget(f"/api/v1/namespaces/{namespace}/{kind}/{name}")
        except Exception:
            return False
        return bool((found.get("metadata", {}) or {}).get("name"))

    # The new name first: an install holding both has already moved on, and the
    # old object is a leftover rather than the one being kept up to date.
    if present(new):
        return new
    return old if present(old) else new


def labels(task, app=None, **extra):
    """The labels to put on something Homestead creates."""
    made = {key("task"): task}
    if app:
        made[key("app")] = app
    made.update({key(name.replace("_", "-")): value for name, value in extra.items()})
    return made


def find(path, suffix, value=None):
    """Items labelled with this key under either domain, without duplicates.

    A label selector cannot say "or", so an install holding objects from before
    the rename is asked twice and the answers are merged.
    """
    seen, items = set(), []
    for candidate in both_keys(suffix):
        query = candidate if value is None else f"{candidate}={value}"
        try:
            found = kget(f"{path}?labelSelector={urllib.parse.quote(query)}").get("items", [])
        except Exception:
            found = []
        for item in found:
            meta = item.get("metadata", {}) or {}
            identity = meta.get("uid") or meta.get("name")
            if identity not in seen:
                seen.add(identity)
                items.append(item)
    return items


def label_of(meta, suffix, default=""):
    """A label on a fetched object, under whichever domain it carries."""
    return read((meta or {}).get("labels", {}) or {}, suffix, default)


def annotation_of(meta, suffix, default=""):
    return read((meta or {}).get("annotations", {}) or {}, suffix, default)


def write_annotation(meta, suffix, value):
    """Set the current key and clear the old one, so only one is ever true."""
    annotations = meta.setdefault("annotations", {})
    annotations.pop(legacy_key(suffix), None)
    if value is None:
        annotations.pop(key(suffix), None)
    else:
        annotations[key(suffix)] = value
    return annotations


NODEPROBE = (f"{BRAND}-nodeprobe", f"{LEGACY}-nodeprobe")


def nodeprobe_pods(namespace):
    """The probe DaemonSet's pods, under either name it may have been given."""
    found = []
    for app in NODEPROBE:
        try:
            items = kget(f"/api/v1/namespaces/{namespace}/pods"
                         f"?labelSelector=app%3D{app}").get("items", [])
        except Exception:
            items = []
        found.extend(items)
        if items:
            break
    return found
