"""Homestead's Kubernetes client: the reads and writes everything goes
through, and the API helpers that lived in server.py (#594 moved them here as
homestead_kube_helpers; #619 grew it into this client).

Reads: get, get_optional (None when the object is not there), items (a whole
list or an error). Writes: send and delete_if_present, which go round
WRITE_GUARDS in order before _ksend (or through the module's own sender,
when it passes one: send=), and update_json, which changes JSON kept in a
ConfigMap or Secret without losing another writer's change (#621).
Both reach the cluster through transport, the one place tests put a fake API
(tests/fakes/kube.py). kget and ksend stay as the old names; the new
functions call them, so a test that patches kget or ksend here reaches the
new functions too. Also here: who may write to a workload, raw and table
reads, path checks, log search reads, and CPU, memory and age parsing.

server.py binds what it still owns through bind_routes(), the same entry
point every moved module has whether or not it declares ROUTES: each helper
in ROUTE_NEEDS is looked up in server.py when it is called; each name in
VALUES is server.py's own value. Tests patch kget and ksend here (server.py
imports this module as KUBE_HELPERS)."""
import base64
import copy
import http.client
import io
import json
import re
import threading
import time
import urllib.request, urllib.parse, urllib.error
import homestead_auth as AUTH
import homestead_cache as CACHE
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_self as SELF
import homestead_storage_guard as STORAGE_GUARD

ROUTE_NEEDS = ("cluster_shutdown", "require_self_data_write", "self_data_activity")
VALUES = ("API", "CTX", "TOKEN")
KGET_RETRY = (0.5, 1.0, 2.0)  # a busy API server (priority and fairness) answers reads 429 for a moment
# Set once server.py has imported what the cluster shutdown fence reads: a
# write before then (while server.py is still importing) is not fenced.
FENCE_READY = False


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_kube needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_kube is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


# A dot segment, plain or percent-encoded, anywhere in a path.
_DOT_SEGMENT = re.compile(r"(^|/)(\.|%2e){1,2}(/|$)", re.I)


# A query parameter's name, as Kubernetes' own are (labelSelector, limit...).
_QUERY_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_OBJECT_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


def api_path(path):
    """A Kubernetes API path, refused if a name in it could step out of the
    object it names. Names come from requests - ?name=, a body - and the API
    server is asked as Homestead, so "../" in one must never reach it, nor a
    "?" or "#" (plain or encoded) that cuts the path short. A backstop on
    structure only: one "?", no "#", and query parameters with plain names;
    names are checked where they enter (request_name)."""
    head, cut, query = str(path).partition("?")
    if (_DOT_SEGMENT.search(head) or any(code in head.lower() for code in ("%2f", "%5c", "%3f", "%23"))
            or any(c in head for c in ("\\", " ", "\t", "\r", "\n", "#"))):
        raise ValueError("that is not a valid Kubernetes name")
    if cut and ("?" in query or "#" in query or not all(_query_part(part) for part in query.split("&") if part)):
        raise ValueError("that is not a valid Kubernetes name")
    return path


def _query_part(part):
    """A query parameter with a plain name. Its value - a selector, a
    token - is Kubernetes' to read: names are checked where they enter."""
    return bool(_QUERY_KEY.fullmatch(part.partition("=")[0]))


def request_name(value, label="name", namespace=False):
    """A name from a request, refused unless it is a Kubernetes name (a
    namespace: a DNS label; an object: a DNS subdomain), before it is put
    into an API path."""
    if not isinstance(value, str) or not (_NAMESPACE if namespace else _OBJECT_NAME).fullmatch(value):
        raise ValueError(f"{label} is not a valid Kubernetes name")
    return value


# Each write guard is guard(method, path, body, proceed): it checks or records
# the write and returns proceed(), or wraps it, or raises to stop the write.
# They run in this order around every write; the last one hands over to the
# transport.
def self_data_fence(method, path, body, proceed):
    # Holds Homestead's own data still while the write runs, and refuses
    # writes while that data is being moved.
    with self_data_activity():
        require_self_data_write()
        return proceed()


def workload_access(method, path, body, proceed):
    authorize_workload_write(method, path, body)
    return proceed()


def change_note(method, path, body, proceed):
    CHANGES.note_write(method, path, body)
    return proceed()


def storage_guard(method, path, body, proceed):
    return STORAGE_GUARD.send(method, path, body, proceed, OPS, kget, own_controller=(SELF.NS, NAMES.BRAND))


# The pages' main reads are kept PAGE_SECONDS (homestead_routes, #705). A write
# that lands on what they show drops them, so whoever made it sees it on the
# next refresh. Leases, ConfigMaps and Secrets are Homestead's own bookkeeping
# (the leader renews its lease every 2 s) and leave them be.
_SHOWN = re.compile(r"/(deployments|statefulsets|daemonsets|replicasets|cronjobs|jobs|pods|nodes|namespaces/[^/]+$"
                    r"|services|persistentvolumeclaims|persistentvolumes|volumesnapshots|virtualmachines"
                    r"|virtualmachineinstances|virtualmachineinstancemigrations|datavolumes|volumes|replicas"
                    r"|engines|volumeattachments|snapshots|backups)(/|\?|$)")
SHOWN_KEYS = ("ov", "nodes", "wl", "vms", "vol", "volmap", "volother", "stor")
SHOWN_PREFIXES = ("node:", "impact:")


def _shown(key):
    return key in SHOWN_KEYS or key.startswith(SHOWN_PREFIXES)


def fresh_reads(method, path, body, proceed):
    result = proceed()
    if method != "GET" and _SHOWN.search(str(path).split("?", 1)[0]):
        CACHE.forget_where(_shown)
    return result


WRITE_GUARDS = [self_data_fence, workload_access, change_note, storage_guard, fresh_reads]


def add_write_guard(guard, *, before=None):
    """Register a write guard: last, or just before the guard named before."""
    if before is None:
        WRITE_GUARDS.append(guard)
        return
    names = [existing.__name__ for existing in WRITE_GUARDS]
    if before not in names:
        raise ValueError(f"no write guard named {before}")
    WRITE_GUARDS.insert(names.index(before), guard)


def run_write_guards(method, path, body, dispatch):
    guards = tuple(WRITE_GUARDS)

    def step(index):
        if index == len(guards):
            return dispatch()
        return guards[index](method, path, body, lambda: step(index + 1))
    return step(0)


def _guarded_ksend(method, path, body=None, ctype="application/json", timeout=15, *, shutdown_bypass=False):
    return run_write_guards(method, path, body,
                            lambda: _ksend(method, path, body, ctype, timeout, shutdown_bypass=shutdown_bypass))


def _auth_ksend(method, path, body=None, ctype="application/json", timeout=15):
    # Only AUTH receives this binding. Login rate limits and session revocation
    # must remain durable while workloads are fenced for shutdown/recovery.
    base = f"/api/v1/namespaces/{AUTH.NS}/secrets"
    name = AUTH.SECRET_NAME()
    metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
    if (method not in ("POST", "PUT") or path != (base if method == "POST" else base + "/" + name)
            or not isinstance(body, dict) or body.get("apiVersion") != "v1" or body.get("kind") != "Secret"
            or body.get("type") != "Opaque" or not isinstance(metadata, dict)
            or metadata.get("name") != name or metadata.get("namespace") != AUTH.NS
            or not isinstance(body.get("data"), dict) or set(body["data"]) != {"store.json"}
            or "stringData" in body or ctype != "application/json"):
        raise ValueError("Authentication transport only writes the account Secret")
    return _guarded_ksend(method, path, body, ctype, timeout, shutdown_bypass=True)


def authorize_workload_write(method, path, body):
    if HOSTACCESS.role() in (None, "admin"):
        return
    match = re.fullmatch(r"(/(?:api/v1|apis/apps/v1|apis/batch/v1)/namespaces/([^/]+)/(deployments|statefulsets|daemonsets|pods|jobs))(?:/([^/?]+)(?:/[^?]+)?)?", path.split("?", 1)[0])
    if not match:
        return
    base, ns, _, name = match.groups()
    if name:
        HOSTACCESS.require_target(kget(base + "/" + name), ns)
    if isinstance(body, dict) and (body.get("spec", {}).get("template") or method == "POST"):
        HOSTACCESS.require_target(body, ns)


def age_secs(ts):
    if not ts:
        return 0
    try:
        t = time.strptime(ts.replace("Z", "GMT"), "%Y-%m-%dT%H:%M:%S%Z")
        import calendar
        return max(0, int(time.time() - calendar.timegm(t)))
    except Exception:
        return 0


def parse_cpu(s):
    if not s: return 0.0
    s = str(s)
    if s.endswith("n"): return float(s[:-1]) / 1e9
    if s.endswith("u"): return float(s[:-1]) / 1e6
    if s.endswith("m"): return float(s[:-1]) / 1e3
    try: return float(s)
    except: return 0.0


def parse_mem(s):
    if not s: return 0
    s = str(s)
    mult = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
            "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    for suf, m in mult.items():
        if s.endswith(suf):
            try: return int(float(s[:-len(suf)]) * m)
            except: return 0
    try: return int(float(s))
    except: return 0


def logsearch_read(ns, pod, container, since, limit, previous=False):
    """One container's log lines of the last since seconds, with timestamps;
    with previous, those of the container before its last restart."""
    query = f"sinceSeconds={int(since)}&limitBytes={int(limit)}&timestamps=true" + (
        f"&container={urllib.parse.quote(container)}" if container else "") + ("&previous=true" if previous else "")
    req = urllib.request.Request(f"{API}/api/v1/namespaces/{urllib.parse.quote(ns)}/pods/{urllib.parse.quote(pod)}/log?{query}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
        return r.read(int(limit) + 1).decode("utf-8", "replace")


def _never_started(pod):
    """Nothing ever ran in it: every container still waiting, or none reported."""
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return all(not (cs.get("state") or {}).get("running") and not (cs.get("state") or {}).get("terminated")
               and not (cs.get("lastState") or {}).get("terminated") for cs in statuses)


def raw_get(path, timeout=20):
    """Plain-text GET against the API (pod logs and similar)."""
    return _request("GET", path, None, {}, timeout).decode("utf-8", "replace")


def ktable(path, timeout=20):
    """A list as the API server prints it: the columns kubectl get shows."""
    return json.loads(_request("GET", path, None, {
        "Accept": "application/json;as=Table;v=v1;g=meta.k8s.io,application/json"}, timeout).decode())


def kget(path, timeout=10):
    for attempt in range(len(KGET_RETRY) + 1):
        try:
            return transport("GET", api_path(path), timeout=timeout)
        except urllib.error.HTTPError as error:
            # A read is safe to ask again; a write's 429 (an eviction a
            # disruption budget refuses) is an answer, and ksend keeps it.
            if error.code != 429 or attempt == len(KGET_RETRY):
                raise
            try:
                delay = float((error.headers or {}).get("Retry-After") or KGET_RETRY[attempt])
            except (TypeError, ValueError):
                delay = KGET_RETRY[attempt]
            error.close()
            time.sleep(max(0.1, min(3.0, delay)))


def ksend(method, path, body=None, ctype="application/json", timeout=15):
    return _guarded_ksend(method, path, body, ctype, timeout)


def get(path, **options):
    return kget(path, **options)


_CLIENT = object()


class Incomplete(ValueError):
    """A list that came back incomplete or with rows that are not objects, or
    API discovery that did not say what it serves: items' own errors, so a
    module can put them in its words and leave every other ValueError be."""


# What get_optional answers for a 404 when a module passes missing=GONE: a
# 404 then reads apart from a null answer.
GONE = object()


def get_optional(path, read=_CLIENT, missing=None, **options):
    """The object, or missing (None) when it is not there (404). Any other
    failure raises: a read that failed is not an object that is missing. A
    module bound to its own reader passes it as read, so it keeps reading
    through it; one whose bind() never ran passes None and fails, rather than
    reading through the real client. A module that must tell a 404 from a
    null answer passes a marker of its own as missing."""
    try:
        return (kget if read is _CLIENT else read)(path, **options)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return missing


def items(path, optional_group=None, read=_CLIENT):
    """Every row of a list, or an error: never a list that failed, came back
    without items, or was cut short (continue). A 429 is retried by kget.
    optional_group names an API group that may not be installed (say
    snapshot.storage.k8s.io): a 404 is then an empty list if API discovery
    confirms the group is absent, and an error if the group is there. read is
    a module's own reader, as for get_optional; discovery goes through it too."""
    read = kget if read is _CLIENT else read
    try:
        listing = read(path)
    except urllib.error.HTTPError as error:
        if error.code != 404 or not optional_group:
            raise
        groups = read("/apis").get("groups")
        if (not isinstance(groups, list) or not groups
                or any(not isinstance(g, dict) or not g.get("name") for g in groups)):
            raise Incomplete("Kubernetes did not say which APIs it serves; refresh and try again")
        if any(g["name"] == optional_group for g in groups):
            raise
        error.close()
        return []
    if (not isinstance(listing, dict) or not isinstance(listing.get("items"), list)
            or (listing.get("metadata") or {}).get("continue")):
        raise Incomplete(f"Kubernetes returned an incomplete list for {path}; refresh and try again")
    if any(not isinstance(row, dict) for row in listing["items"]):
        raise Incomplete(f"Kubernetes returned a list with invalid entries for {path}; refresh and try again")
    return listing["items"]


def send(method, path, body=None, **options):
    return ksend(method, path, body, **options)


def delete_if_present(path, body=None, send=_CLIENT, **options):
    """Delete it; None when it was already gone (404). Any other failure raises.
    send is a module's own sender, as read is for get_optional."""
    try:
        return (ksend if send is _CLIENT else send)("DELETE", path, body, **options)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


# A ConfigMap or Secret by name: where update_json keeps its JSON.
_JSON_HOLDER = re.compile(r"^(/api/v1/namespaces/([a-z0-9.-]+)/(configmaps|secrets))/([a-z0-9.-]+)$")


def update_json(path, key, fn, *, replace=False, attempts=4, labels=None, read=None, write=None, **dump):
    """Change JSON kept in one key of a ConfigMap or Secret without undoing a
    change another writer made meanwhile (#621). fn(current) gets the stored
    value - None when the object or key is not there - and returns the new
    one; it may raise to stop, and nothing is written. The write names the
    resourceVersion read, so Kubernetes refuses it (409) if anything was saved
    in between: then it reads again and calls fn again on what is there now,
    up to attempts times. An object not there yet is created; if another
    writer creates it first (409 on the create), that is an update next time
    round, and one deleted before the write (404 on the update) is created.
    Stored text that is not JSON is an error, unless replace says the new
    value does not depend on the old: then fn gets None and a broken value
    is written over. Other keys, labels and annotations are kept. Returns
    what was written; dump goes to json.dumps (compact and sorted unless
    given).
    read(path) and write(method, path, body) are a module's own bound kget
    and ksend, for the modules still bound with them; this client's by
    default."""
    match = _JSON_HOLDER.match(path)
    if not match:
        raise ValueError(f"update_json keeps JSON in a ConfigMap or Secret, not {path}")
    collection, namespace, plural, name = match.groups()
    secret = plural == "secrets"
    dump = dump or {"separators": (",", ":"), "sort_keys": True}
    write = write or send
    for _ in range(attempts):
        try:
            current = (read or kget)(path)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            current = None
        raw = None if replace else ((current or {}).get("data") or {}).get(key)
        try:
            if raw is not None and secret:
                raw = base64.b64decode(raw).decode("utf-8")
            value = json.loads(raw) if raw is not None else None
        except (ValueError, TypeError):
            raise ValueError(f"{key} in {name} is not valid JSON; nothing was changed") from None
        text = json.dumps(fn(copy.deepcopy(value)), **dump)
        stored = base64.b64encode(text.encode("utf-8")).decode("ascii") if secret else text
        try:
            if current is None:
                body = {"apiVersion": "v1", "kind": "Secret" if secret else "ConfigMap",
                        "metadata": {"name": name, "namespace": namespace,
                                     "labels": dict(labels or {NAMES.key("managed"): "true"})},
                        "data": {key: stored}}
                if secret:
                    body["type"] = "Opaque"
                write("POST", collection, body)
            else:
                body = dict(current, data={**(current.get("data") or {}), key: stored})
                write("PUT", path, body)    # current's metadata carries the resourceVersion read
            return json.loads(text)
        except urllib.error.HTTPError as error:
            # 409: saved or created meanwhile. 404 on the update: deleted
            # meanwhile, so the next round creates it.
            if error.code != 409 and not (error.code == 404 and current is not None):
                raise
            error.close()
    raise RuntimeError(f"{name} kept changing while it was being saved, so this change was not saved; try again")


def require_workload_target(ns, name):
    # An admin's request with no name is let through as before; one that
    # names a workload names a valid one.
    if HOSTACCESS.role() in (None, "admin") and not name:
        return
    request_name(ns, "namespace", namespace=True)
    request_name(name, "workload name")
    if HOSTACCESS.role() not in (None, "admin"):
        HOSTACCESS.require_target(kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"), ns)


def _ksend(method, path, body=None, ctype="application/json", timeout=15, *, shutdown_bypass=False):
    path = api_path(path)
    # Fence the common Kubernetes transport, including callers that already
    # own another feature's write lock. Local navigation and recovery reviews
    # remain available. The shutdown coordinator and the narrowly scoped AUTH
    # binding bypass this fence; authentication still uses the other guards.
    if not shutdown_bypass and FENCE_READY:
        shutdown = cluster_shutdown().state()
        if shutdown and shutdown["phase"] not in ("released", "failed"):
            raise ValueError("Cluster shutdown is active; open Cluster → Shut down cluster for progress or recovery")
    return transport(method, path, body, ctype, timeout)


def transport(method, path, body=None, ctype=None, timeout=15):
    """One request to the API server: its JSON answer, or urllib's HTTPError.
    kget and _ksend reach the cluster only through here, after their own
    checks, so it is the one place tests put a fake API (tests/fakes/kube.py)."""
    headers = {"Content-Type": ctype} if ctype else {}
    data = json.dumps(body).encode() if body is not None else None
    raw = _request(method, path, data, headers, timeout).decode()
    # A write may answer with no body; a read always has one.
    return json.loads(raw) if raw.strip() or method == "GET" else {}


# The API server keeps a connection open between requests (#705): a page that
# makes thirty reads opens one connection and does one TLS handshake, not thirty.
# Idle connections wait in a small pool shared by every thread; a thread takes
# one, or opens one, and gives it back when the answer has been read in full.
# An idle connection over a minute old is not reused (the API server closes idle
# ones at 90 s), and any failure of a connection drops the whole idle pool: an
# API server that went away without a reset would otherwise cost a full timeout
# for every pooled connection in turn.
_IDLE_MAX = 8
_IDLE_SECONDS = 60
_idle = []
_idle_lock = threading.Lock()
_READS = ("GET", "HEAD")
_STALE = (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError,
          BrokenPipeError, ConnectionAbortedError)


def close_connections():
    """Forget every idle connection (a failed connection; tests)."""
    with _idle_lock:
        idle, _idle[:] = list(_idle), []
    for _, connection, _ in idle:
        connection.close()


def _open(timeout):
    target = urllib.parse.urlsplit(API)
    if target.scheme == "http":
        return http.client.HTTPConnection(target.hostname, target.port or 80, timeout=timeout)
    return http.client.HTTPSConnection(target.hostname, target.port or 443, timeout=timeout, context=CTX)


def _take(timeout):
    """An idle connection to this API with this TLS context, or None."""
    key, now, connection, expired = (API, id(CTX)), time.monotonic(), None, []
    with _idle_lock:
        for index in range(len(_idle) - 1, -1, -1):
            if now - _idle[index][2] > _IDLE_SECONDS:
                expired.append(_idle.pop(index)[1])
            elif connection is None and _idle[index][0] == key:
                connection = _idle.pop(index)[1]
    for old in expired:
        old.close()
    if connection is None:
        return None
    connection.timeout = timeout
    if connection.sock is not None:
        connection.sock.settimeout(timeout)
    return connection


def _give_back(connection):
    with _idle_lock:
        if len(_idle) < _IDLE_MAX:
            _idle.append(((API, id(CTX)), connection, time.monotonic()))
            return
    connection.close()


def _request(method, path, data, headers, timeout):
    """One request to the API server on a kept-open connection: the answer's
    bytes, or urllib's HTTPError for any answer that is not a success. A
    redirect is an HTTPError too, never followed with the token. A connection
    the server closed while it sat idle is replaced and a read asked once more
    on a new one; a write is sent once only, always on a fresh connection, since
    whether a dropped write took effect cannot be known.

    Failures reach callers as urlopen's did, with two differences: a timeout is
    the socket's TimeoutError (connecting or reading), and a connection dropped
    mid-answer (RemoteDisconnected, IncompleteRead and other http.client errors)
    is a URLError wrapping it. Every caller catches OSError or Exception, or
    URLError, TimeoutError and ConnectionError, which cover both."""
    headers = {**headers, "Authorization": f"Bearer {TOKEN}"}
    pooled = _take(timeout) if method in _READS else None
    connection = pooled or _open(timeout)
    try:
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
        except _STALE:
            connection.close()
            close_connections()
            if pooled is None:
                raise
            connection = _open(timeout)
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
        payload = response.read()
    except BaseException as error:
        connection.close()
        close_connections()
        if isinstance(error, http.client.HTTPException):
            raise urllib.error.URLError(error) from error
        raise
    if response.will_close:
        connection.close()
    else:
        _give_back(connection)
    if not 200 <= response.status < 300:
        raise urllib.error.HTTPError(API + path, response.status, response.reason, response.headers,
                                     io.BytesIO(payload))
    return payload
