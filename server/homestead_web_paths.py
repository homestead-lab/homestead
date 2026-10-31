"""Which request paths are pages, assets, vendored files or public (track 1,
part 8): the checks server.py's handler runs before it answers.

server.py binds what it still owns through bind_routes(), the same entry point
every moved module has whether or not it declares ROUTES: each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value."""
import os
import re

ROUTE_NEEDS = ()
VALUES = ("VENDOR_TYPES",)


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_web_paths needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_web_paths is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


# Browser routes serve the same authenticated application shell. Keep this an
# explicit allowlist: an unknown path must not accidentally shadow an API 404.
SPA_ROUTES = frozenset({
    "/", "/timeline", "/architecture", "/nodes", "/deploy", "/containers", "/vms",
    "/app-store", "/shares", "/volumes", "/image-cache", "/data-protection", "/portal", "/helm", "/resources",
    "/schedules", "/schedules/week", "/monitoring", "/import", "/vms/import", "/events", "/networking", "/system/cluster", "/settings", "/setup", "/agents",
})


def is_spa_route(path):
    clean = (path or "/").rstrip("/") or "/"
    return clean in SPA_ROUTES


def is_page_path(path):
    """A browser address that is not a known page: the app says so itself.

    An API path or anything that looks like a file keeps its plain 404, so a
    missing endpoint or script is never answered with a web page.

    A page path passes the guard before any sign-in check, so a path a
    module serves at the top level (/readyz, /metrics) must not be one:
    it would be served to anyone (#736).
    """
    clean = path or "/"
    last = clean.rstrip("/").rsplit("/", 1)[-1]
    return (not clean.startswith("/api/") and clean not in ("/api", "/healthz", "/mcp", "/readyz", "/metrics")
            and "." not in last and ".." not in clean and len(clean) < 200)


# Paths reachable without a session. Everything else needs one.
PUBLIC = {"/healthz", "/readyz", "/style.css", "/index.html", "/sw.js", "/manifest.webmanifest",
          "/api/auth/login", "/api/auth/state", "/api/auth/setup",
          # Back to this cluster from one that stopped answering: it only
          # forgets which linked cluster this browser was looking at.
          "/api/fleet/home"}


def handoff_body(path, body):
    """What the self-data fence in server.py's guard sends to a health probe.

    /readyz gets the same status /healthz gets, with nothing but whether
    that status means ready: {"ready": true} beside a 200, {"ready": false}
    beside a 503 (#736). Every other path gets body itself."""
    return {"ready": bool(body.get("ok"))} if path == "/readyz" else body


def is_public_path(path):
    # Cached icons contain only size/type-validated images fetched from public
    # URLs. Serving their content-addressed paths without a session lets
    # browsers load them as subresources even when cookies are restricted.
    return path in PUBLIC or is_asset_path(path) or path.startswith("/api/icons/")


def is_vendor_path(path):
    """A file from a vendored library, addressed by its own relative path."""
    path = path or ""
    return (bool(re.fullmatch(r"/vendor/[A-Za-z0-9][A-Za-z0-9/._-]*", path)) and
            ".." not in path and os.path.splitext(path)[1] in VENDOR_TYPES)


def is_asset_path(path):
    """Allow only flat, bundled SVG assets; never user-controlled filesystem paths."""
    return bool(re.fullmatch(r"/assets/[A-Za-z0-9][A-Za-z0-9._-]*\.svg", path or "")) or is_icon_png(path)


def is_icon_png(path):
    """The installed app's icons: flat, bundled PNGs."""
    return bool(re.fullmatch(r"/icons/[a-z0-9][a-z0-9-]*\.png", path or ""))


def is_app_identity(path):
    """What a browser reads to install the app: its manifest and icons."""
    return path == "/manifest.webmanifest" or is_icon_png(path)


def needs_no_session(path):
    """Whether server.py's guard lets path through with no session or key: the
    app's pages and files and the public paths. docs/route-roles.md asks the same."""
    return (is_spa_route(path) or is_page_path(path) or is_public_path(path) or is_vendor_path(path)
            or (path.startswith("/js/") and path.endswith(".js")))


# Where server.py's _api_key_guard takes an API key instead of a session: the
# /api/v1/ prefix, then exact paths (homestead_mcp.PATH and the metrics scrape).
KEY_PATHS = ("/api/v1/", "/mcp", "/metrics")


def takes_key(path):
    return path.startswith(KEY_PATHS[0]) or path in KEY_PATHS[1:]
