"""The portal: one page of links to everything with a web interface.

Apps in the cluster, and the things around it - the router, the switches,
the access points, the NAS - each have a management page at an address
nobody remembers. The portal keeps them as tiles, in sections, with icons,
and says which ones answer right now.

A link's icon is one of three kinds:

* builtin:<name>        a device glyph drawn by the page (router, switch...);
* workload:<ns>/<name>  that container's own logo, so it follows the app;
* /api/icons/<hash>     an image fetched once from a public URL and cached,
                        the way workload logos are.

Links live in their own ConfigMap, apart from the settings, because they are
a list that grows rather than a handful of switches.
"""
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse

import homestead_forced as FORCED
import homestead_names as NAMES
import homestead_routes as ROUTER

kget = ksend = None
NAMESPACE = "default"
workloads = lambda: []
persist_icon = lambda source: source
icon_data = lambda reference: ""
DATA_KEY = "links.json"
MAX_LINKS = 200
BUILTIN = ("router", "switch", "wifi", "firewall", "nas", "server", "printer", "camera", "ups", "globe")
_status = {"at": 0.0, "value": {}}
_status_lock = threading.Lock()
STATUS_TTL = 30
# A forced link check (TCP probes only) runs at most this often.
FORCED_INTERVAL = 10
CHANGED = "The portal links were changed elsewhere since you opened them; open Edit links again to see them"


def bind(_kget, _ksend, namespace, _workloads, _persist_icon, _icon_data):
    global kget, ksend, NAMESPACE, workloads, persist_icon, icon_data
    kget, ksend, NAMESPACE = _kget, _ksend, namespace
    workloads, persist_icon, icon_data = _workloads, _persist_icon, _icon_data


def _map():
    return NAMES.object_name("portal")


def stored():
    return _stored()[0]


def _stored():
    """The links, and the version of the map they came from ("" when there
    is none yet): the page saves its whole list over that version."""
    try:
        cm = kget(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return [], ""
        raise
    version = str((cm.get("metadata") or {}).get("resourceVersion") or "")
    try:
        rows = json.loads((cm.get("data") or {}).get(DATA_KEY, "[]"))
    except ValueError:
        return [], version
    return ([row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []), version


def _url(value, title):
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > 500:
        raise ValueError(f"{title}: the address must be an http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{title}: leave the password out of the address; the portal is readable by every user")
    return url


def _icon(value, title):
    icon = str(value or "").strip()
    if not icon:
        return ""
    if icon.startswith("builtin:"):
        if icon[8:] not in BUILTIN:
            raise ValueError(f"{title}: there is no built-in icon called {icon[8:]}")
        return icon
    if icon.startswith("workload:"):
        if not re.fullmatch(r"workload:[a-z0-9-]{1,63}/[a-z0-9.-]{1,253}", icon):
            raise ValueError(f"{title}: that is not a workload")
        return icon
    # A public image address, or one already cached: both end up cached.
    return persist_icon(icon)


def save(rows, version=None):
    """Replaces the links, after checking every one. version is the one the
    page read its links at ("" for none yet): links saved since then, in
    another tab or on another copy, are not saved over - the API server's 409
    is passed on as one. Without it, the links are replaced as they stand."""
    if not isinstance(rows, list):
        raise ValueError("links must be a list")
    if len(rows) > MAX_LINKS:
        raise ValueError(f"the portal holds at most {MAX_LINKS} links")
    clean, ids = [], set()
    for row in rows:
        title = " ".join(str((row or {}).get("title") or "").split())
        if not title or len(title) > 60:
            raise ValueError("every link needs a title of at most 60 characters")
        section = " ".join(str(row.get("section") or "").split())
        if len(section) > 40:
            raise ValueError(f"{title}: a section name is at most 40 characters")
        link_id = str(row.get("id") or "")
        if not re.fullmatch(r"[a-f0-9]{8,32}", link_id) or link_id in ids:
            link_id = secrets.token_hex(6)
        ids.add(link_id)
        clean.append({"id": link_id, "title": title, "url": _url(row.get("url"), title),
                      "section": section, "icon": _icon(row.get("icon"), title),
                      "note": " ".join(str(row.get("note") or "").split())[:120]})
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": _map(), "namespace": NAMESPACE, "labels": {NAMES.key("managed"): "true"}},
            "data": {DATA_KEY: json.dumps(clean, indent=1)}}
    try:
        if version is None:
            saved = _replace(body)
        elif version:
            body["metadata"]["resourceVersion"] = str(version)
            saved = ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}", body)
        else:
            saved = ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body)
    except urllib.error.HTTPError as error:
        # 409: saved meanwhile. 404: the version shown was removed meanwhile.
        if version is not None and error.code in (404, 409):
            return ROUTER.Reply(409, {"error": CHANGED})
        raise
    with _status_lock:
        _status["at"] = 0.0
    FORCED.forget("portal-status")
    shown, now = _stored()
    return {"ok": True, "links": view(shown), "version": str(((saved or {}).get("metadata") or {}).get("resourceVersion") or now)}


def _replace(body):
    """The links saved as they stand, for a caller that names no version."""
    try:
        current = kget(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}")
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        return ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}", body)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body)


def view(rows=None):
    """The links as the page draws them: each icon resolved to something to show."""
    rows = stored() if rows is None else rows
    logos = {}
    if any(str(row.get("icon", "")).startswith("workload:") for row in rows):
        logos = {f"workload:{w['ns']}/{w['name']}": w.get("icon", "") for w in workloads()}
    out = []
    for row in rows:
        icon = str(row.get("icon") or "")
        shown = {"kind": "letter", "src": ""}
        if icon.startswith("builtin:"):
            shown = {"kind": "builtin", "src": icon[8:]}
        elif icon.startswith("workload:"):
            shown = {"kind": "image", "src": logos.get(icon, "")} if logos.get(icon) else shown
        elif icon.startswith("/api/icons/"):
            try:
                shown = {"kind": "image", "src": icon_data(icon)}
            except (FileNotFoundError, ValueError, OSError):
                pass
        out.append({**row, "shown": shown})
    return out


def _page():
    rows, version = _stored()
    return {"links": view(rows), "icons": list(BUILTIN), "version": version}


def _scheme(port):
    return "https" if int(port) in (443, 8443, 9443, 5001) else "http"


def candidates():
    """Container listeners that could be links: each exposed port on its address."""
    out = []
    for w in workloads():
        for port in w.get("ports") or []:
            ip, number = port.get("ip"), port.get("port")
            if not ip or not number:
                continue
            host = f"[{ip}]" if ":" in str(ip) else ip
            default = (_scheme(number) == "http" and int(number) == 80) or (_scheme(number) == "https" and int(number) == 443)
            out.append({"title": w["name"], "ns": w["ns"], "name": w["name"],
                        "url": f"{_scheme(number)}://{host}" + ("" if default else f":{number}"),
                        "port": number, "port_name": port.get("name", ""),
                        "icon": f"workload:{w['ns']}/{w['name']}", "has_logo": bool(w.get("icon")),
                        "group": w.get("group", "")})
    return out


def _reachable(url, timeout=1.5):
    parsed = urllib.parse.urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    started = time.monotonic()
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return {"up": True, "ms": int((time.monotonic() - started) * 1000)}
    except OSError:
        return {"up": False, "ms": None}


def status(force=False):
    """Whether each link's host answers on its port: a TCP connection, not a
    page load, so a login screen or a self-signed certificate still counts."""
    with _status_lock:
        if not force and time.monotonic() - _status["at"] < STATUS_TTL:
            return _status["value"]
    return FORCED.run("portal-status", _probe, FORCED_INTERVAL) if force else _probe()


def _probe():
    rows = stored()
    result = {}

    def check(row):
        result[row["id"]] = _reachable(row["url"])

    threads = [threading.Thread(target=check, args=(row,), daemon=True) for row in rows if row.get("url")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    with _status_lock:
        _status["value"], _status["at"] = result, time.monotonic()
    return result


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/portal/candidates"): ("viewer", lambda request: candidates()),
    ("POST", "/api/portal"): ("admin", lambda request: save(request.body.get("links"), request.body.get("version"))),
    ("GET", "/api/portal"): ("viewer", lambda request: _page()),
}
