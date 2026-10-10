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

The handler takes a Request and returns what is sent back as JSON, with 200;
an exception it raises is answered as one from server.py's own branches is.
server.py looks a request up here after its guard has checked who may make
it, before its own branches. homestead_route_policy.role() reads the same
table, so a route and its role are written once.

A module is listed in MODULES to be read. A route stays in server.py while it
needs the handler itself - headers, streaming, a redirect.
"""
import collections
import importlib

MODULES = (
    "homestead_firewall",
)
ROLES = ("viewer", "operator", "admin")

Request = collections.namedtuple("Request", "method path query body user role")
Route = collections.namedtuple("Route", "method path role handler module")

_table = {}


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
