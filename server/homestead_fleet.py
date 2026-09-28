"""Linked clusters: several Homesteads managed from any one of them.

Every linked Homestead keeps the same list of members and the same key. The
key signs every call one member makes to another - a page, an API call or a
console forwarded for a person, or one Homestead asking another for a move -
so no member keeps a password for another, and a call cannot be altered or
replayed on the way.

A person signs in to one member, whichever one they can reach, and picks a
cluster from the top bar. From then on the member they signed in to relays
the whole app - pages, API, consoles - to the one they picked, and signs each
request with who they are and their role. The member that answers serves its
own pages and applies its own rules to that role, as if they had signed in
there. Only one member needs to be reachable from outside.

Linking is done once with an admin account on the other Homestead, which is
used for that one sign-in and not kept. Unlinking a member changes the key,
so the one that left can no longer speak for the rest.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import homestead_names as NAMES

kget = ksend = None
NS = "lab"
VERSION = ""
# What linked Homesteads say to each other. It goes up only when one would
# misread the other.
PROTOCOL = 1
COOKIE = "homestead_cluster"
CONFIG = "homestead-fleet"
SECRET = "homestead-fleet-key"
# How far apart two members' clocks may be, and so how long a signed request
# stays valid.
SKEW = 300
TIMEOUT = 15

H_FROM = "X-Homestead-Fleet"
H_TIME = "X-Homestead-Fleet-Time"
H_NONCE = "X-Homestead-Fleet-Nonce"
H_USER = "X-Homestead-Fleet-User"
H_ROLE = "X-Homestead-Fleet-Role"
H_SIG = "X-Homestead-Fleet-Signature"

# Answered by the Homestead a person signed in to, never passed on: their own
# account and session, their devices' notifications, and the switch itself.
LOCAL = {"/api/fleet/switch", "/api/fleet/home", "/api/auth/login", "/api/auth/logout",
         "/api/auth/setup", "/api/auth/password", "/api/auth/signout-everywhere",
         "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test", "/api/push/status",
         "/api/alerts/pending", "/healthz"}
ROLES = ("viewer", "operator", "admin")
# Not passed on: they describe one connection, or who is asking, which the
# signature says instead.
HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "cookie", "content-length"}

_site = lambda: ""
_address = lambda: ""
_lock = threading.RLock()
_cache = {"at": 0.0, "state": None, "key": None}
_nonces = {}
_status = {}


class Unreachable(Exception):
    """The other Homestead could not be asked. Worth trying again."""


def bind(_kget, _ksend, namespace, version="", site=None, address=None):
    global kget, ksend, NS, VERSION, _site, _address
    kget, ksend, NS, VERSION = _kget, _ksend, namespace, version
    _site = site or (lambda: "")
    _address = address or (lambda: "")
    _cache.update(at=0.0, state=None, key=None)


# ------------------------------------------------------------------ storage
def handle(name, taken=()):
    """A short, stable name for a member: what moves and URLs call it."""
    base = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40].strip("-") or "homestead"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def _read():
    try:
        found = kget(f"/api/v1/namespaces/{NS}/configmaps/{CONFIG}")
        state = json.loads((found.get("data") or {}).get("fleet.json", "{}"))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        state = {}
    try:
        secret = kget(f"/api/v1/namespaces/{NS}/secrets/{SECRET}")
        key = base64.b64decode((secret.get("data") or {}).get("key", "") or "")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        key = b""
    return (state if isinstance(state, dict) else {}), key


def _put(path, collection, body):
    try:
        kget(path)
        ksend("PUT", path, body)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        ksend("POST", collection, body)


def _write(state, key=None):
    _put(f"/api/v1/namespaces/{NS}/configmaps/{CONFIG}", f"/api/v1/namespaces/{NS}/configmaps",
         {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": CONFIG, "namespace": NS, "labels": {NAMES.key("managed"): "true"}},
          "data": {"fleet.json": json.dumps(state, indent=2)}})
    if key is not None:
        _put(f"/api/v1/namespaces/{NS}/secrets/{SECRET}", f"/api/v1/namespaces/{NS}/secrets",
             {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": SECRET, "namespace": NS},
              "data": {"key": base64.b64encode(key).decode()}})
    _cache.update(at=time.time(), state=state, key=key if key is not None else _cache["key"])


def _load(fresh=False):
    """This Homestead's view of the links, read at most every few seconds."""
    with _lock:
        if not fresh and _cache["state"] is not None and time.time() - _cache["at"] < 5:
            return _cache["state"], _cache["key"]
        try:
            state, key = _read()
        except Exception:
            # The API server is briefly away: keep what was known.
            if _cache["state"] is not None:
                return _cache["state"], _cache["key"]
            return {}, b""
        _cache.update(at=time.time(), state=state, key=key)
        return state, key


def _self_record(state=None, url=None):
    state = state or {}
    mine = next((m for m in state.get("members", []) if m.get("id") == state.get("self")), {})
    name = str(_site() or "").strip() or mine.get("name") or "Homestead"
    return {"id": state.get("self", ""), "handle": mine.get("handle") or handle(name),
            "name": name, "url": url or mine.get("url") or _address(),
            "added": mine.get("added") or time.strftime("%Y-%m-%d %H:%M")}


def _ensure():
    """The links as stored, created alone on first use: an id and a key."""
    with _lock:
        state, key = _load(fresh=True)
        changed = False
        if not state.get("self"):
            state = {"self": secrets.token_hex(6), "revision": 1, "updated": time.time(), "members": []}
            changed = True
        if not any(m.get("id") == state["self"] for m in state.get("members", [])):
            state.setdefault("members", []).insert(0, _self_record(state))
            changed = True
        if not key:
            key = secrets.token_bytes(32)
            changed = True
        if changed:
            _write(state, key)
        return state, key


def self_id():
    return _load()[0].get("self", "")


def members():
    return list(_load()[0].get("members", []))


def others():
    me = self_id()
    return [m for m in members() if m.get("id") != me]


def linked():
    return len(members()) > 1


def member(ref):
    """A member by its id or its handle."""
    return next((m for m in members() if ref and ref in (m.get("id"), m.get("handle"))), None)


# ------------------------------------------------------------------ signing
def _digest(sender, stamp, nonce, method, target, user, role, body):
    return "\n".join([sender, stamp, nonce, method.upper(), target, user, role,
                      hashlib.sha256(body or b"").hexdigest()]).encode()


def sign(method, target, body=b"", user="", role="admin"):
    """Headers that let another member know this request is from here, for whom."""
    state, key = _load()
    if not key or not state.get("self"):
        return {}
    stamp, nonce = str(int(time.time())), secrets.token_hex(12)
    mac = hmac.new(key, _digest(state["self"], stamp, nonce, method, target, user, role, body),
                   hashlib.sha256).digest()
    return {H_FROM: state["self"], H_TIME: stamp, H_NONCE: nonce, H_USER: user, H_ROLE: role,
            H_SIG: base64.urlsafe_b64encode(mac).decode().rstrip("=")}


def signed(headers):
    return bool(headers.get(H_FROM))


def verify(headers, method, target, body=b""):
    """Who a signed request is from and for, or PermissionError.

    A person forwarded by another member is `name@that-member`, with the role
    they hold there. A call Homestead makes itself - a move, a sync - is
    `homestead@that-member`, as admin.
    """
    sender, stamp = headers.get(H_FROM, ""), headers.get(H_TIME, "")
    nonce, user, role = headers.get(H_NONCE, ""), headers.get(H_USER, ""), headers.get(H_ROLE, "")
    state, key = _load()
    origin = next((m for m in state.get("members", []) if m.get("id") == sender), None)
    if not key or not origin or sender == state.get("self"):
        raise PermissionError("not signed by a linked Homestead")
    try:
        age = abs(time.time() - int(stamp))
    except ValueError:
        raise PermissionError("signed request has no time") from None
    if age > SKEW:
        raise PermissionError("signed request is too old, or the clocks differ by more than five minutes")
    given = headers.get(H_SIG, "")
    mac = base64.urlsafe_b64encode(hmac.new(key, _digest(sender, stamp, nonce, method, target, user, role, body),
                                            hashlib.sha256).digest()).decode().rstrip("=")
    if not nonce or not hmac.compare_digest(given, mac):
        raise PermissionError("signature does not match")
    with _lock:
        now = time.time()
        for seen in [n for n, until in _nonces.items() if until < now]:
            _nonces.pop(seen, None)
        if nonce in _nonces:
            raise PermissionError("signed request was already used")
        _nonces[nonce] = now + 2 * SKEW
    if role and role not in ROLES:
        raise PermissionError("unknown role")
    return {"sender": origin, "role": role,
            "user": f"{user or 'homestead'}@{origin.get('handle', sender)}" if role else ""}


# ------------------------------------------------------------------ calling
def _open(request, timeout):
    context = ssl.create_default_context() if request.full_url.startswith("https") else None
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _reason(error):
    try:
        return str(json.loads(error.read().decode("utf-8", "replace") or "{}").get("error") or "")[:240]
    except Exception:
        return ""


def _json(url, method, path, body=None, token="", headers=None, timeout=TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if method != "GET":
        request.add_header("X-Homestead-Auth", "1")
    if token:
        request.add_header("Cookie", f"homestead_session={token}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with _open(request, timeout) as response:
            return json.loads(response.read().decode() or "{}"), response
    except urllib.error.HTTPError as error:
        reason = _reason(error)
        if error.code >= 500:
            raise Unreachable(reason or f"HTTP {error.code}") from error
        raise ValueError(reason or f"HTTP {error.code}") from error
    except (ValueError, Unreachable):
        raise
    except Exception as error:
        raise Unreachable(f"no answer from {url}: {str(error)[:120]}") from error


def call(ref, method, path, body=None, timeout=TIMEOUT, user="", role="admin"):
    """Ask another member, signed. Returns its JSON answer."""
    target = member(ref) if isinstance(ref, str) else ref
    if not target:
        raise ValueError(f"no linked cluster called {ref}")
    data = json.dumps(body).encode() if body is not None else b""
    full = urllib.parse.urlparse(target["url"].rstrip("/") + path)
    signature = sign(method, full.path + (f"?{full.query}" if full.query else ""), data, user, role)
    try:
        payload, _ = _json(target["url"], method, path, body, headers=signature, timeout=timeout)
    except ValueError as error:
        raise ValueError(f"{target.get('name')}: {error}") from error
    except Unreachable as error:
        raise Unreachable(f"{target.get('name')}: {error}") from error
    return payload


def _clean_url(url):
    url = str(url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("an address is a http:// or https:// URL")
    return url


# ------------------------------------------------------------------ state
def hello():
    """Which Homestead this is, as a linked one asks."""
    state, _ = _load()
    mine = _self_record(state)
    return {"id": state.get("self", ""), "handle": mine["handle"], "name": mine["name"],
            "version": VERSION, "protocol": PROTOCOL, "revision": int(state.get("revision") or 0),
            "members": len(state.get("members", []))}


def shared_state():
    """The member list, for a member that fell behind. Never the key."""
    state, _ = _load()
    return {"revision": int(state.get("revision") or 0), "updated": state.get("updated", 0),
            "members": state.get("members", [])}


def check(target):
    """Whether a member answers, and what it runs. Remembered for half a minute."""
    seen = _status.get(target["id"])
    if seen and time.time() - seen["at"] < 30:
        return seen
    try:
        there = call(target, "GET", "/api/fleet/hello", timeout=4)
        row = {"at": time.time(), "reachable": True, "version": there.get("version", ""),
               "name": there.get("name") or target.get("name"), "revision": int(there.get("revision") or 0),
               "protocol": int(there.get("protocol") or 0), "error": ""}
    except Exception as error:
        row = {"at": time.time(), "reachable": False, "version": "", "name": target.get("name"),
               "revision": 0, "protocol": 0, "error": str(error)[:200]}
    _status[target["id"]] = row
    return row


def summary(via=None):
    """Every linked cluster, with whether it answers here and what it runs."""
    state, _ = _load()
    if not state.get("self"):
        state, _ = _ensure()
    me = state["self"]
    rows = [m for m in state.get("members", []) if m.get("id") != me]
    checks = {}

    def run(row):
        checks[row["id"]] = check(row)
    threads = [threading.Thread(target=run, args=(row,), daemon=True) for row in rows]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(6)
    # A member that has heard of a newer list than this one: bring it here.
    ahead = max((c for c in checks.values() if c.get("reachable")), key=lambda c: c["revision"], default=None)
    if ahead and ahead["revision"] > int(state.get("revision") or 0):
        source = next((m for m in rows if checks.get(m["id"]) is ahead), None)
        try:
            adopt(call(source, "GET", "/api/fleet/state", timeout=4))
            state, _ = _load(fresh=True)
        except Exception:
            pass
        for row in state.get("members", []):
            if row.get("id") != me and row["id"] not in checks:
                checks[row["id"]] = check(row)
    out = []
    for m in state.get("members", []):
        mine = m.get("id") == me
        seen = {} if mine else checks.get(m["id"]) or {}
        out.append({"id": m["id"], "handle": m.get("handle", ""), "url": m.get("url", ""),
                    "name": _self_record(state)["name"] if mine else seen.get("name") or m.get("name", ""),
                    "self": mine, "version": VERSION if mine else seen.get("version", ""),
                    "reachable": True if mine else bool(seen.get("reachable")),
                    "error": "" if mine else seen.get("error", ""),
                    "compatible": True if mine else (seen.get("protocol") in (0, PROTOCOL) or not seen.get("reachable"))})
    return {"self": me, "protocol": PROTOCOL, "linked": len(out) > 1, "members": out,
            "via": (via or {}).get("name", ""), "via_id": (via or {}).get("id", ""),
            "address": _self_record(state)["url"], "suggested_address": _address()}


def adopt(incoming, sender=None):
    """Take a newer member list from another member.

    The newer list wins. A list without this Homestead in it means it was
    unlinked: it goes back to standing alone, with a key of its own.
    """
    with _lock:
        state, key = _ensure()
        revision = int(incoming.get("revision") or 0)
        ours = int(state.get("revision") or 0)
        rows = [m for m in incoming.get("members") or [] if isinstance(m, dict) and m.get("id")]
        if revision < ours or (revision == ours and rows == state.get("members")):
            return {"changed": False, "revision": ours}
        if not any(m["id"] == state["self"] for m in rows):
            mine = _self_record(state)
            fresh = {"self": state["self"], "revision": revision + 1, "updated": time.time(), "members": [mine]}
            _write(fresh, secrets.token_bytes(32))
            _status.clear()
            return {"changed": True, "left": True, "revision": fresh["revision"]}
        state = {"self": state["self"], "revision": revision, "updated": incoming.get("updated") or time.time(),
                 "members": rows}
        new_key = incoming.get("key")
        _write(state, base64.b64decode(new_key) if new_key else None)
        return {"changed": True, "revision": revision}


def _broadcast(state, skip=(), key=None):
    """Tell every other member about a change. Returns who could not be told."""
    payload = {"revision": state["revision"], "updated": state["updated"], "members": state["members"]}
    if key:
        payload["key"] = base64.b64encode(key).decode()
    missed = []
    for m in state["members"]:
        if m["id"] == state["self"] or m["id"] in skip:
            continue
        try:
            call(m, "POST", "/api/fleet/sync", payload, timeout=8)
        except Exception as error:
            missed.append(f"{m.get('name')}: {str(error)[:160]}")
    return missed


# ------------------------------------------------------------------ linking
def _login(url, username, password):
    try:
        _, response = _json(url, "POST", "/api/auth/login",
                            {"username": username, "password": password, "remember": False})
    except ValueError as error:
        raise ValueError(f"that Homestead refused the sign-in: {error}") from error
    except Unreachable as error:
        raise ValueError(f"could not reach that Homestead: {error}") from error
    cookie = response.headers.get("Set-Cookie", "")
    token = cookie.split("homestead_session=", 1)[-1].split(";", 1)[0] if cookie else ""
    if not token:
        raise ValueError("that Homestead signed in but sent no session")
    return token


def join(url, username, password, own_url=""):
    """Link another Homestead to this one and every one already linked here.

    The account is used for this one sign-in; it must be an admin there. The
    other Homestead must stand alone: joining two groups is done one member
    at a time, so nothing is linked by accident.
    """
    url = _clean_url(url)
    state, key = _ensure()
    own = own_url or _self_record(state)["url"]
    if not own:
        raise ValueError("say how the other Homestead reaches this one: this one's address, with its port")
    own = _clean_url(own)
    if any(m.get("url") == url for m in state.get("members", [])):
        raise ValueError("that address is already linked")
    token = _login(url, username, password)
    try:
        there, _ = _json(url, "GET", "/api/fleet", token=token)
    except ValueError as error:
        raise ValueError(f"that Homestead cannot be linked - update it to {VERSION} first ({error})") from error
    if any(m.get("id") == there.get("self") for m in state.get("members", [])):
        raise ValueError(f"{(there.get('members') or [{}])[0].get('name') or 'that Homestead'} is already linked")
    if len(there.get("members") or []) > 1:
        raise ValueError(f"that Homestead is already linked with {len(there['members']) - 1} other cluster(s). "
                         "Unlink it there first, or link this one from there.")
    if int(there.get("protocol") or 0) != PROTOCOL:
        raise ValueError("that Homestead links clusters differently; update both to the same release first")
    with _lock:
        state, key = _ensure()
        rows = [dict(m, url=own) if m["id"] == state["self"] else m for m in state["members"]]
        revision = int(state.get("revision") or 0) + 1
        record, _ = _json(url, "POST", "/api/fleet/accept", {
            "key": base64.b64encode(key).decode(), "members": rows, "revision": revision, "url": url}, token=token)
        if not record.get("id"):
            raise ValueError("that Homestead did not say who it is")
        record = {k: record.get(k, "") for k in ("id", "handle", "name", "added")}
        record["url"] = url
        state = {"self": state["self"], "revision": revision, "updated": time.time(), "members": rows + [record]}
        _write(state)
    _status.pop(record["id"], None)
    missed = _broadcast(state, skip=(record["id"],))
    return {"ok": True, "member": record, "missed": missed}


def accept(body):
    """Be linked by another Homestead: take its key and its members."""
    with _lock:
        state, _ = _ensure()
        if len(state.get("members", [])) > 1:
            raise ValueError("this Homestead is already linked; unlink it first")
        rows = [m for m in body.get("members") or [] if isinstance(m, dict) and m.get("id")]
        if not rows or not body.get("key"):
            raise ValueError("nothing to link to")
        mine = _self_record(state, url=_clean_url(body.get("url") or ""))
        mine["handle"] = handle(mine["name"], {m.get("handle") for m in rows})
        state = {"self": state["self"], "revision": int(body.get("revision") or 1), "updated": time.time(),
                 "members": rows + [mine]}
        _write(state, base64.b64decode(body["key"]))
        _status.clear()
        return mine


def remove(ref):
    """Unlink a member. The rest get a new key; the one leaving is told first."""
    state, key = _ensure()
    target = member(ref)
    if not target:
        raise ValueError(f"no linked cluster called {ref}")
    if target["id"] == state["self"]:
        return leave()
    rows = [m for m in state["members"] if m["id"] != target["id"]]
    state = {"self": state["self"], "revision": int(state.get("revision") or 0) + 1,
             "updated": time.time(), "members": rows}
    try:
        call(target, "POST", "/api/fleet/sync",
             {"revision": state["revision"], "updated": state["updated"], "members": rows}, timeout=8)
        told = True
    except Exception:
        told = False
    fresh = secrets.token_bytes(32)
    missed = _broadcast(state, key=fresh)
    with _lock:
        _write(state, fresh)
    _status.pop(target["id"], None)
    return {"ok": True, "told": told, "missed": missed}


def leave():
    """Stop being linked: the others keep each other, this one stands alone."""
    state, key = _ensure()
    rows = [m for m in state["members"] if m["id"] != state["self"]]
    if not rows:
        return {"ok": True, "missed": []}
    revision = int(state.get("revision") or 0) + 1
    # The rest get a key this one forgets, so it cannot speak for them after.
    missed = _broadcast({"self": state["self"], "revision": revision, "updated": time.time(), "members": rows},
                        key=secrets.token_bytes(32))
    with _lock:
        _write({"self": state["self"], "revision": revision + 1, "updated": time.time(),
                "members": [_self_record(state)]}, secrets.token_bytes(32))
    _status.clear()
    return {"ok": True, "missed": missed}


def set_address(url):
    """Where the other members reach this Homestead."""
    url = _clean_url(url)
    with _lock:
        state, _ = _ensure()
        rows = [dict(m, url=url) if m["id"] == state["self"] else m for m in state["members"]]
        state = {"self": state["self"], "revision": int(state.get("revision") or 0) + 1,
                 "updated": time.time(), "members": rows}
        _write(state)
    return {"ok": True, "missed": _broadcast(state)}


def refresh_name():
    """Carry a renamed site to the other members, so the switch says it."""
    state, _ = _load()
    mine = next((m for m in state.get("members", []) if m.get("id") == state.get("self")), None)
    name = str(_site() or "").strip()
    if not mine or not name or mine.get("name") == name or len(state["members"]) < 2:
        return
    with _lock:
        rows = [dict(m, name=name) if m["id"] == state["self"] else m for m in state["members"]]
        state = {"self": state["self"], "revision": int(state.get("revision") or 0) + 1,
                 "updated": time.time(), "members": rows}
        _write(state)
    _broadcast(state)


# ------------------------------------------------------------------ relaying
def local_path(path):
    return path in LOCAL


def _connect(url, timeout=6):
    parsed = urllib.parse.urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
    if parsed.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
    return sock, parsed


def _read_head(sock, limit=65536):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise Unreachable("the other Homestead sent an oversized answer")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def request_head(method, target, host, headers, body, signature, websocket=False, origin=""):
    """The request as the other member gets it: the person's own headers,
    less their cookies and anything about this one connection, plus the
    signature that says who they are. A console's Origin, already checked
    against this Homestead, is presented as the member's own, which it checks
    as it would for any console."""
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}"]
    for name, value in headers.items():
        low = name.lower()
        if low in HOP or low.startswith("cf-") or low.startswith("x-homestead-fleet") or low.startswith("x-forwarded"):
            continue
        if low == "origin" and origin:
            continue
        lines.append(f"{name}: {value}")
    if origin:
        lines.append(f"Origin: {origin}")
    lines += ["Upgrade: websocket", "Connection: Upgrade"] if websocket else ["Connection: close"]
    if body or method in ("POST", "PUT", "PATCH", "DELETE"):
        lines.append(f"Content-Length: {len(body)}")
    lines += [f"{name}: {value}" for name, value in signature.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")


def response_head(head, cookies=()):
    """The answer as the browser gets it: the other member's own cookies are
    not this origin's to set; this one's session refresh is."""
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0]
    upgrade = " 101 " in f"{status} "
    kept = [line for line in lines[1:] if line and not line.lower().startswith("set-cookie:")
            and (upgrade or not line.lower().startswith("connection:"))]
    if not upgrade:
        kept.append("Connection: close")
    kept += [f"Set-Cookie: {cookie}" for cookie in cookies]
    return ("\r\n".join([status] + kept) + "\r\n\r\n").encode("latin-1"), upgrade


def forward(handler, ref, body, user="", role="", cookies=()):
    """Relay one request from the browser to a linked member, and its answer back.

    Raises Unreachable before anything is sent to the browser, so the caller
    can still answer with an error of its own.
    """
    target = member(ref)
    if not target:
        raise Unreachable("that cluster is no longer linked")
    websocket = (handler.headers.get("Upgrade") or "").lower() == "websocket"
    if websocket:
        # A console from another site is refused here, as it would be anywhere.
        origin = urllib.parse.urlparse(handler.headers.get("Origin") or "").netloc.lower()
        if not origin or origin != (handler.headers.get("Host") or "").lower():
            raise PermissionError("console websocket origin rejected")
    try:
        sock, parsed = _connect(target["url"])
    except Exception as error:
        raise Unreachable(f"{target.get('name')} did not answer: {str(error)[:120]}") from error
    path = parsed.path.rstrip("/") + handler.path
    try:
        sock.sendall(request_head(handler.command, path, parsed.netloc, handler.headers, body,
                                  sign(handler.command, path, body, user, role), websocket,
                                  f"{parsed.scheme}://{parsed.netloc}" if websocket else "") + body)
        sock.settimeout(None if websocket else 900)
        head, rest = _read_head(sock)
    except Unreachable:
        sock.close()
        raise
    except Exception as error:
        sock.close()
        raise Unreachable(f"{target.get('name')} did not answer: {str(error)[:120]}") from error
    if not head:
        sock.close()
        raise Unreachable(f"{target.get('name')} closed the connection")
    out, upgrade = response_head(head, cookies)
    handler.close_connection = True
    try:
        handler.wfile.write(out + rest)
        if upgrade:
            _pipe_both(handler, sock)
        else:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
        handler.wfile.flush()
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _pipe_both(handler, sock):
    """A console: bytes both ways until either end hangs up."""
    done = threading.Event()

    def browser_to_member():
        try:
            while not done.is_set():
                chunk = handler.rfile.read1(65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            done.set()
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    thread = threading.Thread(target=browser_to_member, daemon=True)
    thread.start()
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except OSError:
        pass
    finally:
        done.set()


def unreachable_page(name, error):
    """What a browser sees when the cluster it switched to does not answer."""
    from html import escape
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(name)} is not answering · Homestead</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0d;color:#eee;font:15px/1.5 system-ui,sans-serif}}
main{{max-width:440px;padding:24px}}h1{{font-size:20px;margin:0 0 8px}}p{{color:#aaa}}a{{display:inline-block;margin-top:14px;padding:9px 16px;border-radius:10px;background:#fff;color:#000;text-decoration:none;font-weight:600}}</style></head>
<body><main><h1>{escape(name)} is not answering</h1><p>This Homestead could not reach it: {escape(str(error))}</p>
<a href="/api/fleet/home">Back to this cluster</a></main></body></html>"""
