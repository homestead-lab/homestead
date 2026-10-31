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
Stream for a download too large to hold, or a Reply(code, body) for an
answer that is not a 200 - a 409 that carries the plan it refused, say. An
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

A route that needs the connection reads `request.conn` (`connected(request)`
refuses a call made without one): its headers, without Cookie, Authorization,
Proxy-Authorization, Cf-Access-Jwt-Assertion and the fleet signature; Homestead's
own cookies; whether it came over TLS or through Cloudflare; and who the guard
found - the Access sign-in, the linked Homestead, the API key - with `who()` for
the session. A Reply sets Homestead's own cookies (`cookies=(Cookie(...),)`, each
at its own path and only from the module whose OWN_COOKIES names it, #936) and only the ROUTE_HEADERS (`headers=(("Location", "/"),)`); its
status and type are checked too, all before anything is written: one that is
refused (`Refused`) is answered 500, with none of them. A path ending "/*" is a
prefix route: it answers the plain paths under it (no ., .., // or %).

A module is listed in MODULES to be read. A route stays in server.py while it
needs the handler itself: a WebSocket, /mcp, a file served with its own headers.
"""
import collections
import collections.abc
import functools
import importlib
import json
import queue
import re
import threading
import time
import types
import urllib.parse
import homestead_agent_audit as AUDIT
import homestead_cache as CACHE
import homestead_http as HTTP
import homestead_self_data_cookie as MOVE_COOKIE

MODULES = (
    "homestead_addons",
    "homestead_agent_audit",
    "homestead_agent_requests",
    "homestead_alert_webhook",
    "homestead_alerts",
    "homestead_allocation_probe",
    "homestead_api_keys",
    "homestead_api_v1",
    "homestead_app_copies",
    "homestead_app_settings",
    "homestead_audit",
    "homestead_audit_view",
    "homestead_auth",
    "homestead_auth_routes",
    "homestead_baseline",
    "homestead_capacity",
    "homestead_changes",
    "homestead_cluster",
    "homestead_cluster_server",
    "homestead_cluster_shutdown",
    "homestead_components",
    "homestead_config_backup",
    "homestead_container_diagnostics",
    "homestead_container_rebalance",
    "homestead_copy_move",
    "homestead_data_copies",
    "homestead_deploy",
    "homestead_deploy_server",
    "homestead_diagnostics",
    "homestead_disk_v2",
    "homestead_disk_allocation",
    "homestead_longhorn_runtime",
    "homestead_disks",
    "homestead_expiry",
    "homestead_files",
    "homestead_firewall",
    "homestead_fleet",
    "homestead_fleet_routes",
    "homestead_flow",
    "homestead_forecast",
    "homestead_fs_freeze",
    "homestead_gitops",
    "homestead_group_tiers",
    "homestead_hardware",
    "homestead_helm",
    "homestead_history",
    "homestead_host_bonds",
    "homestead_host_bridge",
    "homestead_host_console",
    "homestead_host_os",
    "homestead_housekeeping",
    "homestead_icons",
    "homestead_image_cleanup",
    "homestead_imports",
    "homestead_ipam",
    "homestead_isos",
    "homestead_lhcapacity",
    "homestead_lhrebuild",
    "homestead_lhv2_setup",
    "homestead_lhv2_cleanup",
    "homestead_lhv2_upgrade",
    "homestead_lifecycle",
    "homestead_logalerts",
    "homestead_longhorn",
    "homestead_metrics",
    "homestead_move",
    "homestead_move_engine",
    "homestead_move_source",
    "homestead_mqtt",
    "homestead_namespaces",
    "homestead_network_server",
    "homestead_networking",
    "homestead_nodes",
    "homestead_nodeshell",
    "homestead_objectstore",
    "homestead_operations",
    "homestead_os_rollout",
    "homestead_outage",
    "homestead_overview_server",
    "homestead_passthrough",
    "homestead_place",
    "homestead_placement_mode",
    "homestead_platform",
    "homestead_portal",
    "homestead_power_jobs",
    "homestead_priority",
    "homestead_promote",
    "homestead_probe",
    "homestead_push",
    "homestead_readyz",
    "homestead_rebalance",
    "homestead_reclass",
    "homestead_requests",
    "homestead_resources",
    "homestead_restore_swap",
    "homestead_restore_test",
    "homestead_revert",
    "homestead_schedules",
    "homestead_self",
    "homestead_self_address",
    "homestead_self_data_server",
    "homestead_self_health",
    "homestead_set_restart",
    "homestead_settings_helpers",
    "homestead_setup",
    "homestead_share_servers",
    "homestead_shares",
    "homestead_signins",
    "homestead_snapshot_delete",
    "homestead_start_order",
    "homestead_storage",
    "homestead_storage_classes",
    "homestead_storage_leftovers",
    "homestead_storage_server",
    "homestead_timeline",
    "homestead_timeline_summary",
    "homestead_unraid_vms",
    "homestead_update_notes",
    "homestead_updates",
    "homestead_upgrades",
    "homestead_uplinks",
    "homestead_uptime",
    "homestead_vm_review",
    "homestead_vm_server",
    "homestead_vms",
    "homestead_vmstore",
    "homestead_volumes",
    "homestead_weekly_trim",
    "homestead_workloads",
)
ROLES = ("viewer", "operator", "admin")
# The methods server.py's handler H answers, each with its own do_GET, do_POST,
# do_DELETE: a route declared with any other would get http.server's 501, so
# table() refuses it. tests/test_module_routes.py keeps the two in step.
METHODS = ("GET", "POST", "DELETE")

# addr: the address the request came from, as server.py sees it; conn: the
# Connection it came on, or None when a route is called in-process.
Request = collections.namedtuple("Request", "method path query body user role addr conn", defaults=("", None))
# What a route may know of the connection. headers: a Headers; cookies: Homestead's
# own, by name; tls, cloudflare: how it reached us; access: the Cloudflare Access
# claims; fleet: the linked Homestead that sent it; key: the API key it carried;
# who: a call that answers who is signed in (it may read the account store).
Connection = collections.namedtuple("Connection", "headers cookies tls cloudflare access fleet key who")
Route = collections.namedtuple("Route", "method path role handler module")
Raw = collections.namedtuple("Raw", "body ctype")
# A download sent as it is read: chunks (an iterator of bytes), its type, the
# name it saves as, and its size when known.
Stream = collections.namedtuple("Stream", "chunks ctype filename size")
# An answer with its own status: a refusal that carries more than its words,
# or a 202 for work that carries on. JSON unless told otherwise.
# headers: (name, value) pairs; cookies: Cookies. Both are checked before sending.
Reply = collections.namedtuple("Reply", "code body ctype headers cookies", defaults=("application/json", (), ()))
# max_age None: a cookie that lasts as long as the browser keeps it.
Cookie = collections.namedtuple("Cookie", "name value path max_age", defaults=("/", None))
# The only cookies a route may read or set, each with the one path it is set at.
# Each is set by one module only, the one whose OWN_COOKIES names it (#936).
COOKIES ={"homestead_session": "/", "homestead_cluster": "/", MOVE_COOKIE.NAME: MOVE_COOKIE.PATH}
# The only response headers a route may set: everything else (framing,
# encoding, caching, security, cross-origin, cookies) is the server's own.
ROUTE_HEADERS = ("location", "content-disposition", "retry-after", "etag", "last-modified", "vary",
                 "x-homestead-fleet-missing")
# Request headers a route never sees: they carry a secret, or the fleet
# signature that conn.fleet already checked.
SECRET_HEADERS = ("cookie", "authorization", "proxy-authorization", "cf-access-jwt-assertion", "x-homestead-fleet")
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_VALUE = re.compile(r"[\t\x20-\x7e\x80-\xff]*")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*")
_TOKEN_TEXT = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
# type/subtype, then ; name=value parameters, the value a token or a quoted string.
_MEDIA = re.compile(rf"{_TOKEN_TEXT}/{_TOKEN_TEXT}(?:[ \t]*;[ \t]*{_TOKEN_TEXT}=(?:{_TOKEN_TEXT}|\"[^\"\\\x00-\x1f\x7f]*\"))*")
# The rest of a path under a prefix route: plain segments, none empty, no . or .., no % or \.
_REST = re.compile(r"(?!\.\.?(?:/|$))[^/%\\]+(?:/(?!\.\.?(?:/|$))[^/%\\]+)*")
# A path on this site: never //host or /\host, which a browser reads as another site.
_LOCATION = re.compile(r"/(?![/\\])[^\s\\]*")

# The body fields an after-request hook may ask for, and the values it gets:
# names, never free text. A hook gets no body at all unless it asks.
HOOK_FIELDS = ("ns", "namespace", "kind", "name")
_HOOK_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_HOOK_BODY = 64 * 1024
# How many requests' hooks may wait for the hooks' thread before more are dropped.
HOOK_QUEUE = 256
_hooks = []
# What a request may note for its hooks (note()): keys each hook declared,
# values short names or whole numbers, kept on the request's thread and
# cleared when the next request on it starts.
NOTE_CAP = 64
_NOTE_KEY = re.compile(r"[a-z][a-z_]{0,31}")
_NOTE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_note_keys = {}
_notes = threading.local()
_queue = queue.Queue(HOOK_QUEUE)
_served = []
_dropped = [0]
_worker_lock = threading.Lock()


class Refused(Exception):
    """A reply a route may not send: Homestead's own mistake, answered 500."""


class Headers(collections.abc.Mapping):
    """The request's headers, read-only, by any case, without SECRET_HEADERS.
    A header sent twice is read as its first value."""

    def __init__(self, pairs):
        self._pairs = [(str(name), str(value)) for name, value in pairs if not _secret(str(name))]
        self._first = {}
        for name, value in self._pairs:
            self._first.setdefault(name.lower(), (name, value))

    def __getitem__(self, name):
        return self._first[str(name).lower()][1]

    def __iter__(self):
        return (name for name, _ in self._first.values())

    def __len__(self):
        return len(self._first)

    def __repr__(self):
        return f"Headers({self._pairs!r})"


def _secret(name):
    name = name.lower()
    return name in SECRET_HEADERS or name.startswith("x-homestead-fleet-")


def connected(request):
    """The request's Connection; a route that needs one is refused in-process."""
    if request.conn is None:
        raise PermissionError(f"{request.path} answers only over HTTP")
    return request.conn


def cookie_header(cookie, tls):
    """The Set-Cookie value for one of Homestead's cookies, as server.py writes
    its session cookie: Secure only behind TLS."""
    name, value, path, max_age = cookie
    if name not in COOKIES:
        raise Refused(f"a route may set only Homestead's own cookies, not {name!r}")
    if not isinstance(value, str) or not _COOKIE_VALUE.fullmatch(value):
        raise Refused(f"cookie {name} has a value a cookie cannot carry")
    if path != COOKIES[name]:
        raise Refused(f"cookie {name} is set at {COOKIES[name]}, not {path!r}")
    if max_age is not None and (type(max_age) is not int or max_age < 0):
        raise Refused(f"cookie {name} has a Max-Age that is not a whole number of seconds")
    return (f"{name}={value}; Path={path}; HttpOnly; SameSite=Strict{'; Secure' if tls else ''}"
            f"{f'; Max-Age={max_age}' if max_age is not None else ''}")


def check_header(name, value):
    """(name, value) when a route may send it; Refused otherwise."""
    if not isinstance(name, str) or not _TOKEN.fullmatch(name):
        raise Refused(f"{name!r} is not a header name")
    if name.lower() not in ROUTE_HEADERS:
        raise Refused(f"a route may not set {name}: only {', '.join(ROUTE_HEADERS)}")
    if not isinstance(value, str) or not _VALUE.fullmatch(value):
        raise Refused(f"{name} has a value a header cannot carry")
    if name.lower() == "location" and not _LOCATION.fullmatch(value):
        raise Refused("Location must be a path on this site")
    return name, value


def check_answer(code, ctype):
    """A status and Content-Type a route may answer with; Refused otherwise.
    No 1xx, 204 or 304: each would break the framing of the answer's body."""
    if type(code) is not int or not 200 <= code <= 599 or code in (204, 304):
        raise Refused(f"a route may not answer with status {code!r}")
    if not isinstance(ctype, str) or not _MEDIA.fullmatch(ctype):
        raise Refused(f"{ctype!r} is not a media type")


def _tls(handler):
    try:
        return handler._over_tls()
    except AttributeError:      # a handler with no socket: the proxy's word alone
        return False


def _headers(handler):
    if getattr(handler, "headers", None) is None:
        handler.headers = {}    # none read: a request refused before its headers, or a test's handler
    return handler.headers


def connection(handler):
    """The Connection a route sees, from server.py's handler after its guard."""
    raw = _headers(handler)
    if (raw.get("X-Homestead-Fleet") and getattr(handler, "_fleet_who", None) is False
            and not getattr(handler, "api_key", None)):
        handler._fleet_identity()       # a public path: the guard did not check the sender (a key is its own)
    cookies = {name: value for name, value in handler._cookies().items() if name in COOKIES}
    return Connection(Headers(raw.items()), types.MappingProxyType(cookies), _tls(handler),
                      handler._via_cloudflare(), getattr(handler, "_access", None),
                      getattr(handler, "_fleet_from", None), getattr(handler, "api_key", None), handler._who)


def _owned(cookie, module, owned):
    """cookie, when module owns it (owners()); Refused when another module
    does. A name that is none of Homestead's is cookie_header's to refuse."""
    owner = owned.get(cookie.name)
    if cookie.name in COOKIES and owner != module:
        raise Refused(f"a route in {module} may not set cookie {cookie.name}: "
                      + (f"only {owner} sets it (OWN_COOKIES)" if owner else "no module owns it (OWN_COOKIES)"))
    return cookie


def answer(handler, answer, module):
    """Send what the route in module returned. A Reply's cookies and headers are
    all checked before any is added, after what the guard already set (a
    refreshed session): each cookie must be one module owns (OWN_COOKIES)."""
    if isinstance(answer, Stream):
        check_answer(200, answer.ctype)
        handler._send_stream(answer)
    elif isinstance(answer, Raw):
        check_answer(200, answer.ctype)
        handler._extra_headers += [("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")]
        handler._send(200, answer.body, answer.ctype)
    elif isinstance(answer, Reply):
        check_answer(answer.code, answer.ctype)
        tls, owned = (_tls(handler), owners()) if answer.cookies else (False, {})
        lines = ([("Set-Cookie", cookie_header(_owned(Cookie(*cookie), module, owned), tls))
                  for cookie in answer.cookies] +
                 [check_header(*header) for header in answer.headers])
        handler._extra_headers += lines
        handler._send(answer.code, answer.body, answer.ctype)
    else:
        handler._send(200, answer)

_table = {}
# The table the cookies' owners were read with, and the owners (owners()).
_owned_by = [None, {}]
# How long the pages' main reads (overview, hosts, apps, VMs, volumes,
# storage) are kept for the UI: longer than its 15 s refresh, so a refresh can
# be served from the last one (#705). A write Homestead makes to what they show
# drops them at once (homestead_kube.fresh_reads).
PAGE_SECONDS = 20
# The pages' keys, and how long each page keeps its own: any reader of them
# (a loop, /metrics, a dialog) is served the snapshot's part, or keeps its
# read as long as the page would, never less (#978).
CACHE.at_least(PAGE_SECONDS, "nodes", "wl", "vol", "stor", "ov", "vms")
CACHE.at_least(15, "lhcap")
# For a check made just before a write: the cluster as it is (CACHE.fresh).
fresh = CACHE.fresh
_cached = [lambda key, seconds, fn: fn()]
_store = [{}]
_snapshot = [lambda key: None]


def bind(cached, store=None):
    """server.py's cache, for routes whose answer it keeps a few seconds,
    and the dict it keeps them in, for routes that make one stale."""
    _cached[0] = cached
    if store is not None:
        _store[0] = store


def forget(*keys):
    """Drop these keys from server.py's cache, and from a read of them that
    is still running, so it is not kept after the change (#705)."""
    for key in keys:
        _store[0].pop(key, None)
    CACHE.forget_where(lambda key: key in keys)


def forget_prefix(prefix):
    """Drop every key from server.py's cache that starts with prefix."""
    for key in [k for k in _store[0] if k.startswith(prefix)]:
        _store[0].pop(key, None)
    CACHE.forget_where(lambda key: key.startswith(prefix))


def cached(key, seconds, fn):
    """fn(), or what it returned less than seconds ago under key."""
    return _cached[0](key, seconds, fn)


def use_snapshot(read):
    """The leader's page snapshot (homestead_page_snapshot.read): read(key)
    gives (as_of, value) while a part is fresh, else None."""
    _snapshot[0] = read


def page(key, fn, seconds=PAGE_SECONDS):
    """A page's main read: the leader's snapshot part for key while it is
    fresh, else fn() kept seconds under key, as cached() does (#705)."""
    return page_dated(key, fn, seconds)[1]


def page_dated(key, fn, seconds=PAGE_SECONDS):
    """page() and when its answer was read from the cluster: (as_of, value).
    A snapshot part is dated by its cycle; a kept answer by its read, also
    when it is served because a newer read failed; a read just made, but not
    kept (a write overtook it), is dated now."""
    part = _snapshot[0](key)
    if part is not None:
        return part
    value = cached(key, seconds, fn)
    kept = CACHE.kept(key)
    return (kept[0] if kept and kept[1] is value else time.time()), value


def page_kept(key):
    """page_dated() without reading the cluster: (as_of, value) of the
    leader's fresh snapshot part, else of the kept read, else None. For a
    route read on every page that must never wait on a page's build (#940)."""
    return _snapshot[0](key) or CACHE.kept(key)


def page_aged(key, fn, seconds=PAGE_SECONDS):
    """page() for a route whose answer is a dict, with as_of (when its numbers
    were read) and age_s (how long ago that is), so the page can say how old
    they are (#705 step 8). The kept answer itself is not changed."""
    as_of, value = page_dated(key, fn, seconds)
    if not isinstance(value, dict):
        return value
    return {**value, "as_of": round(as_of, 3), "age_s": max(0, int(time.time() - as_of))}


def table():
    """Every declared route, by (method, path). Read once; a module that
    declares a route twice, or one another module declares, or one with a
    method H does not answer (METHODS), is refused; so is a module that owns a
    cookie that is not Homestead's, or one another module owns (#936)."""
    if _table:
        return _table
    found, owned = {}, {}
    for name in MODULES:
        module = importlib.import_module(name)
        _claim(owned, name, module)
        for (method, path), (role, handler) in getattr(module, "ROUTES", {}).items():
            key = (method.upper(), path)
            if key[0] not in METHODS:
                raise ValueError(f"{method} {path} in {name}: server.py answers only {', '.join(METHODS)}")
            if key in found:
                raise ValueError(f"{method} {path} is declared by {found[key].module} and {name}")
            if role not in ROLES:
                raise ValueError(f"{method} {path} in {name} has no known role: {role!r}")
            if not callable(handler):
                raise ValueError(f"{method} {path} in {name} has no handler")
            if "*" in path and not (path.endswith("/*") and path.count("*") == 1):
                raise ValueError(f"{method} {path} in {name}: a prefix route ends in /* and has no other *")
            if path.startswith("/api/v1/") and name != "homestead_api_v1":
                raise ValueError(f"{method} {path} in {name}: /api/v1 is homestead_api_v1's, where a key's scopes are checked")
            if path.endswith("/*") and _covers(key[0], path[:-1]):
                raise ValueError(f"{method} {path} in {name} covers a path answered elsewhere")
            found[key] = Route(key[0], path, role, handler, name)
    _table.update(found)
    _owned_by[:] = [_table, owned]
    return _table


def _claim(owned, name, module):
    """Add the cookies module (called name) owns to owned, cookie: module;
    ValueError for one that is not Homestead's or that another owns."""
    cookies = getattr(module, "OWN_COOKIES", ())
    if not isinstance(cookies, tuple) or len(set(cookies)) != len(cookies):
        raise ValueError(f"OWN_COOKIES in {name} names each cookie it sets once, in a tuple, not {cookies!r}")
    for cookie in cookies:
        if cookie not in COOKIES:
            raise ValueError(f"{name} owns {cookie!r}, which is not one of Homestead's cookies: {', '.join(COOKIES)}")
        if cookie in owned:
            raise ValueError(f"cookie {cookie} is owned by {owned[cookie]} and {name}: one module sets it")
        owned[cookie] = name


def owners():
    """Each cookie a module owns, by name, with that module: only it may set
    the cookie (answer()). Read with the table, once."""
    routes = table()
    if _owned_by[0] is not routes:     # a table put in place without table() reading it (a test's)
        owned = {}
        for name in MODULES:
            _claim(owned, name, importlib.import_module(name))
        _owned_by[:] = [routes, owned]
    return dict(_owned_by[1])


def _covers(method, stem):
    """Whether a prefix route under stem would take a path the route policy,
    the public paths, server.py's own prefix branches, a page or a file of the
    app already answer."""
    import homestead_route_policy as POLICY
    import homestead_web_paths as WEB
    taken = [p for m, p in POLICY.POLICY if m == method] + sorted(WEB.PUBLIC) + sorted(WEB.SPA_ROUTES)
    probe = stem + "x"
    return (any(p.startswith(stem) for p in taken)
            or any(p.startswith(stem) or stem.startswith(p) for p in POLICY.CODE_PREFIXES)
            or WEB.is_page_path(probe) or WEB.is_public_path(probe) or not stem.startswith("/api/"))


def find(method, path):
    """The Route for a request, or None when server.py answers it itself: the
    exact path, or else the longest prefix route ("/api/x/*") above it."""
    method, routes = method.upper(), table()
    route = routes.get((method, path))
    if route is not None or "*" in path:
        return route
    stems = [p for m, p in routes if m == method and p.endswith("/*") and path.startswith(p[:-1])
             and _REST.fullmatch(path[len(p) - 1:])]
    return routes[(method, max(stems, key=len))] if stems else None


def role(path, method):
    """The role a declared route needs, or None when it is not one."""
    route = find(method, path)
    return route.role if route else None


def after(fn=None, fields=(), notes=()):
    """Register fn(request, status, ms) to run after each request that reached
    its request line, answered or refused, wherever it was answered. request is
    what a route gets, without its body, query, cookies or session; status is
    all it learns of the answer (None when the headers never arrived). fields
    names the HOOK_FIELDS it needs from a JSON body. Usable as @after or
    @after(fields=(...)).

    Hooks run one after another on one worker thread, after the answer is
    sent; when they fall HOOK_QUEUE requests behind, a request's hooks are
    dropped and counted (hooks_dropped()). What a hook sees came from the
    client: escape and cut the path and headers before keeping them. It must
    not read thread-locals (homestead_changes.REQUEST, the host-access role):
    it runs on another thread. What it needs from the request's own thread
    comes through notes: the keys it declares here, which code handling the
    request sets with note(key, value); fn is then called with notes={...}
    holding those of its keys that were set."""
    if isinstance(fields, str) or any(field not in HOOK_FIELDS for field in fields):
        raise ValueError(f"a hook may ask only for {', '.join(HOOK_FIELDS)}, not {fields!r}")
    if isinstance(notes, str) or any(not isinstance(key, str) or not _NOTE_KEY.fullmatch(key) for key in notes):
        raise ValueError(f"a hook declares its note keys as names, not {notes!r}")

    def register(fn):
        if not callable(fn):
            raise TypeError(f"a hook is a function, not {fn!r}")
        _hooks.append((fn, tuple(fields)))
        if notes:
            _note_keys[fn] = tuple(notes)
        else:
            _note_keys.pop(fn, None)
        return fn
    return register if fn is None else register(fn)


def note(key, value):
    """Something about this request for the hooks that declared key: a short
    name (cut to NOTE_CAP) or a whole number, never free text."""
    if key not in {k for keys in _note_keys.values() for k in keys}:
        raise ValueError(f"no hook takes a note called {key!r}")
    if isinstance(value, str) and value:
        value = value[:NOTE_CAP]
        if not _NOTE_VALUE.fullmatch(value):
            raise ValueError(f"a note is a name or a number, not {value!r}")
    elif type(value) is not int:
        raise ValueError(f"a note is a name or a number, not {value!r}")
    _notes.__dict__.update(AUDIT.redact({key: value}))


def _take_notes():
    taken = dict(_notes.__dict__)
    _notes.__dict__.clear()
    return taken


def _clear_notes():
    _notes.__dict__.clear()


HTTP.REQUEST_STARTED.append(_clear_notes)


def hooks_dropped():
    """How many requests' hooks were dropped because the hooks fell behind."""
    return _dropped[0]


def _ensure_worker():
    """One daemon thread runs the hooks queued on the current _queue."""
    with _worker_lock:
        if _queue not in _served:
            _served.append(_queue)
            threading.Thread(target=_run_hooks, args=(_queue,), name="after-request", daemon=True).start()


def _run_hooks(jobs):
    while True:
        for fn, request, status, ms in jobs.get():
            try:
                fn(request, status, ms)
            except Exception:
                pass    # one hook's failure is not another's, and never the answer's


def _hook_fields(raw, fields):
    """The asked-for fields of a body the request already read; the socket is
    never read for a hook."""
    try:
        body = json.loads(raw) if raw and len(raw) <= _HOOK_BODY else {}
    except (ValueError, RecursionError):
        body = {}
    if not isinstance(body, dict):
        return {}
    return AUDIT.redact({field: body[field] for field in fields
                         if isinstance(body.get(field), str) and _HOOK_VALUE.fullmatch(body[field])})


def _with_notes(fn, noted):
    keys = _note_keys.get(fn)
    return functools.partial(fn, notes={k: v for k, v in noted.items() if k in keys}) if keys else fn


def _read_only(value):
    return types.MappingProxyType(dict(value)) if isinstance(value, dict) else value


def after_request(handler):
    """Queue every hook for the request handler just answered (homestead_http):
    what they see is taken now, and they run on the hooks' own thread."""
    table()     # the modules that register hooks are read
    if not _hooks:
        return
    raw = _headers(handler)
    view = Request(handler.command or "", urllib.parse.urlparse(handler.path or "").path, None, None,
                   getattr(handler, "user", None), getattr(handler, "role", None),
                   handler._client_ip() if getattr(handler, "client_address", None) else "",
                   Connection(Headers(raw.items()), types.MappingProxyType({}), _tls(handler),
                              handler._via_cloudflare(), _read_only(getattr(handler, "_access", None)),
                              getattr(handler, "_fleet_from", None), _read_only(getattr(handler, "api_key", None)),
                              None))
    ms = round((time.monotonic() - handler._started) * 1000)
    body = getattr(handler, "_raw", None)
    noted = _take_notes()
    jobs = [(_with_notes(fn, noted), view._replace(body=_hook_fields(body, fields)) if fields else view,
             handler._status, ms) for fn, fields in list(_hooks)]
    _ensure_worker()
    try:
        _queue.put_nowait(jobs)
    except queue.Full:
        with _worker_lock:
            _dropped[0] += 1
