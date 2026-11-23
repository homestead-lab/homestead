"""Names and icons that lived in server.py (track 1, part 8): a DNS-safe
name, Homestead's own group, and an app's display icon with its repair.

server.py binds what it still owns through bind_routes(), the same entry point
every moved module has whether or not it declares ROUTES: each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value."""
import re
import threading
import time
import homestead_icons as ICONS
import homestead_move_phases as MOVE_PHASES
import homestead_names as NAMES
import homestead_nfs as NFS
import homestead_objectstore as OBJECTS
import homestead_self as SELF

ROUTE_NEEDS = ()
VALUES = ("DATA_DIR", "DEFAULT_NS", "OWN_GROUP", "PLATFORM_NS", "SMB_NAMESPACE")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_names_helpers needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_names_helpers is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def own_group(ns, name):
    """Homestead's own containers - itself, the Samba that serves shares, the
    backup store moves go through - sit together, apart from your apps,
    unless someone put them in a group of their own."""
    own = {(SELF.NS, NAMES.BRAND), (SMB_NAMESPACE, NAMES.object_name("smb")),
           (SMB_NAMESPACE, NFS.NAME),
           (SMB_NAMESPACE, "samba"), (DEFAULT_NS, OBJECTS.NAME)}
    return OWN_GROUP if (ns, name) in own or ns in PLATFORM_NS else ""


def _dns_name(value, label="name"):
    value = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(f"{label} must use lowercase letters, numbers and dashes")
    return value


# Logos whose cached file is missing here - a workload moved from another
# cluster before moves brought logos, or a data volume that was replaced -
# fetched again in the background: reference -> what it is cached as now.
_ICON_HEALED = {}
_ICON_TRIED = {}
ICON_RETRY_SECONDS = 15 * 60


def _heal_icon(reference, annotations):
    moved_from = (NAMES.read(annotations, "moved-from") or "").split("/", 1)[0]
    found = ""
    if moved_from:
        try:
            found = MOVE_PHASES.carry_icon(moved_from, dict(annotations))
        except Exception:
            found = ""
    if not found:
        source = NAMES.read(annotations, "icon-source") or ""
        if source.startswith(("http://", "https://")):
            try:
                found = ICONS.persist(source, DATA_DIR)
            except Exception:
                found = ""
    if found:
        _ICON_HEALED[reference] = found


def display_icon(annotations):
    """An app's logo for a list row: its cached file's /api/icons/ reference,
    which the browser fetches once and keeps (#1211), not the picture itself:
    a list read every 15 seconds carried every logo again each time."""
    reference = NAMES.read(annotations, "icon")
    if not ICONS.is_reference(reference):
        # Anything else - another path or host, an address, a query - was
        # written by hand: the page fetches what a row names, so it is no logo.
        return ""
    try:
        shown = _ICON_HEALED.get(reference, reference)
        ICONS.resolve(shown, DATA_DIR)         # a file not here is no logo, and its repair is asked for
        return shown
    except FileNotFoundError:
        if time.time() - _ICON_TRIED.get(reference, 0) > ICON_RETRY_SECONDS:
            _ICON_TRIED[reference] = time.time()
            threading.Thread(target=_heal_icon, args=(reference, dict(annotations or {})),
                             daemon=True, name="icon-heal").start()
        return ""
    except (ValueError, OSError):
        return ""
