"""Routes a feature module declares for itself (#363).

server.py routes requests and each feature lives in its own module
(CONTRIBUTING.md). Most of server.py's routing was one-line hand-offs -
`if p == "/api/firewall/save": return self._send(200, FIREWALL.save(b))` -
with the route's role declared again, apart, in homestead_route_policy.py.

A module can instead declare its routes, each with its role, beside the code
they call:

    ROUTES = {
        ("GET", "/api/firewall"): ("viewer", lambda request: inventory()),
        ("POST", "/api/firewall/save"): ("admin", lambda request: save(request.body)),
    }

The handler takes a Request and returns what is sent back as JSON, with 200,
or a Raw(body, ctype) for anything that is not JSON - an image, say - or a
Stream for a download too large to hold; an
exception it raises is answered as one from server.py's own branches is.
server.py looks a request up here after its guard has checked who may make
it, before its own branches. homestead_route_policy.role() reads the same
table, so a route and its role are written once.

A route whose answer server.py keeps for a few seconds asks for it through
`cached(key, seconds, fn)` here, which is server.py's own cache once bound:
the same keys, so what server.py drops from it when something changes (the
network picture after a VIP repair, say) is dropped for the route too. A
route that changes something drops what it made stale with `forget(*keys)`,
or every key that starts with a prefix with `forget_prefix(prefix)`.

A module is listed in MODULES to be read. A route stays in server.py while it
needs the handler itself - headers, streaming, a redirect.
"""
import collections
import importlib

MODULES = (
    "homestead_addons",
    "homestead_api_keys",
    "homestead_app_settings",
    "homestead_auth",
    "homestead_baseline",
    "homestead_changes",
    "homestead_cluster",
    "homestead_config_backup",
    "homestead_container_rebalance",
    "homestead_diagnostics",
    "homestead_disk_v2",
    "homestead_disks",
    "homestead_files",
    "homestead_firewall",
    "homestead_fleet",
    "homestead_forecast",
    "homestead_hardware",
    "homestead_helm",
    "homestead_history",
    "homestead_host_bridge",
    "homestead_host_console",
    "homestead_host_os",
    "homestead_housekeeping",
    "homestead_icons",
    "homestead_imports",
    "homestead_ipam",
    "homestead_isos",
    "homestead_lhcapacity",
    "homestead_lhrebuild",
    "homestead_lhv2_setup",
    "homestead_lhv2_upgrade",
    "homestead_lifecycle",
    "homestead_longhorn",
    "homestead_move",
    "homestead_move_engine",
    "homestead_mqtt",
    "homestead_namespaces",
    "homestead_networking",
    "homestead_objectstore",
    "homestead_operations",
    "homestead_os_rollout",
    "homestead_outage",
    "homestead_passthrough",
    "homestead_place",
    "homestead_platform",
    "homestead_portal",
    "homestead_probe",
    "homestead_push",
    "homestead_rebalance",
    "homestead_reclass",
    "homestead_resources",
    "homestead_revert",
    "homestead_self",
    "homestead_self_address",
    "homestead_self_health",
    "homestead_setup",
    "homestead_shares",
    "homestead_signins",
    "homestead_snapshot_delete",
    "homestead_unraid_vms",
    "homestead_updates",
    "homestead_vms",
    "homestead_vmstore",
)
ROLES = ("viewer", "operator", "admin")

Request = collections.namedtuple("Request", "method path query body user role")
Route = collections.namedtuple("Route", "method path role handler module")
Raw = collections.namedtuple("Raw", "body ctype")
# A download sent as it is read: chunks (an iterator of bytes), its type, the
# name it saves as, and its size when known.
Stream = collections.namedtuple("Stream", "chunks ctype filename size")

_table = {}
_cached = [lambda key, seconds, fn: fn()]
_store = [{}]


def bind(cached, store=None):
    """server.py's cache, for routes whose answer it keeps a few seconds,
    and the dict it keeps them in, for routes that make one stale."""
    _cached[0] = cached
    if store is not None:
        _store[0] = store


def forget(*keys):
    """Drop these keys from server.py's cache."""
    for key in keys:
        _store[0].pop(key, None)


def forget_prefix(prefix):
    """Drop every key from server.py's cache that starts with prefix."""
    for key in [k for k in _store[0] if k.startswith(prefix)]:
        _store[0].pop(key, None)


def cached(key, seconds, fn):
    """fn(), or what it returned less than seconds ago under key."""
    return _cached[0](key, seconds, fn)


def table():
    """Every declared route, by (method, path). Read once; a module that
    declares a route twice, or one another module declares, is refused."""
    if _table:
        return _table
    found = {}
    for name in MODULES:
        module = importlib.import_module(name)
        for (method, path), (role, handler) in getattr(module, "ROUTES", {}).items():
            key = (method.upper(), path)
            if key in found:
                raise ValueError(f"{method} {path} is declared by {found[key].module} and {name}")
            if role not in ROLES:
                raise ValueError(f"{method} {path} in {name} has no known role: {role!r}")
            if not callable(handler):
                raise ValueError(f"{method} {path} in {name} has no handler")
            found[key] = Route(key[0], path, role, handler, name)
    _table.update(found)
    return _table


def find(method, path):
    """The Route for a request, or None when server.py answers it itself."""
    return table().get((method.upper(), path))


def role(path, method):
    """The role a declared route needs, or None when it is not one."""
    route = find(method, path)
    return route.role if route else None
