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
import copy
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

import homestead_audit as AUDIT_LOG
import homestead_fetch as FETCH
import homestead_names as NAMES
import homestead_sessions as SESSIONS

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
# A member's answer: read to at most this many bytes (the same as the
# largest request body Homestead takes), and all of it within the call's
# timeout. It was read whole, and the timeout was per wait on the socket.
ANSWER_MAX_BYTES = 8 << 20
REASON_MAX_BYTES = 64 << 10
# How long the Fleet page waits for all the members' checks together.
SUMMARY_WAIT = 6
# member id -> the thread checking it, while it runs (summary()).
_checking = {}
_checking_lock = threading.Lock()

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
         "/api/auth/preferences/dashboard",
         "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test", "/api/push/status",
         "/api/alerts/pending", "/api/alerts/delivered", "/healthz", "/readyz", "/metrics"}
ROLES = ("viewer", "operator", "admin")
# Not passed on: they describe one connection, or who is asking, which the
# signature says instead. The caller's own Authorization (an API key) is
# for this Homestead only; the member trusts the signature, never a key.
HOP = {"connection", "keep-alive", "proxy-connection", "authorization", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "cookie", "content-length"}

_site = lambda: ""
_address = lambda: ""
_lock = threading.RLock()
_cache = {"at": 0.0, "state": None, "key": None}
_nonces = {}
_status = {}


class Unreachable(Exception):
    """The other Homestead could not be asked. Worth trying again."""


def bind(_kget, _ksend, namespace, version="", site=None, address=None, earlier=None):
    """address: where this Homestead is reached now - its VIP once it has
    one. earlier: addresses it gave out before that (the node's own), which
    a stored address still showing is moved on from."""
    global kget, ksend, NS, VERSION, _site, _address, _earlier
    kget, ksend, NS, VERSION = _kget, _ksend, namespace, version
    _site = site or (lambda: "")
    _address = address or (lambda: "")
    _earlier = earlier or (lambda: ())
    _cache.update(at=0.0, state=None, key=None)


# ------------------------------------------------------------------ storage
def handle(name, taken=()):
    """A short, stable name for a member: what moves and URLs call it."""
    base = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40].strip("-") or "homestead"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


# A member's handle is part of the name a person it relays acts as here
# (fleet:<handle>:<name>), so it holds no ':'.
HANDLE = re.compile(r"[a-z0-9-]{1,40}")


def _plain_handles(rows):
    """Member records as kept: a handle that is not plain is made plain."""
    return [dict(m, handle=handle(m["handle"])) if m.get("handle") and not HANDLE.fullmatch(str(m["handle"])) else m
            for m in rows]


def _read():
    try:
        found = kget(f"/api/v1/namespaces/{NS}/configmaps/{CONFIG}")
        state = json.loads((found.get("data") or {}).get("fleet.json", "{}"))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        state = {}
    key, _ = _read_key()
    return (state if isinstance(state, dict) else {}), key


def _read_key():
    """The clusters' key as stored, and the Secret it was read from (None when there is none)."""
    try:
        secret = kget(f"/api/v1/namespaces/{NS}/secrets/{SECRET}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return b"", None
    return base64.b64decode((secret.get("data") or {}).get("key", "") or ""), secret


# In fleet.json: which key the list was saved with, so a key write that comes
# late cannot put an older list's key beside a newer list.
KEY_ID = "keyId"


def _key_id(key):
    """Names a key without giving it away."""
    return hmac.new(key, b"homestead-fleet-key-id", hashlib.sha256).hexdigest()[:16]


class _Unchanged(Exception):
    """The change has nothing to save."""


def _change(fn):
    """Change the links on what is stored when they are saved (#621).

    fn(state, key) gets the list and key as stored now and returns the
    (state, key) to save - key None keeps the stored one - or None when there
    is nothing to save. The list is written with the version read, so a change
    another copy of Homestead saved meanwhile makes this one read again and
    call fn again on that. A list or key that cannot be read stops the change:
    nothing is ever saved over them. A new key is written after the list,
    and only while the list stored is still the one saved with it.
    Returns the (state, key) stored."""
    import homestead_kube as KUBE
    with _lock:
        made = {}

        def apply(stored):
            if stored is not None and not isinstance(stored, dict):
                raise ValueError("the linked clusters' list is not a JSON object; nothing was changed")
            state = stored or {}
            key, _ = _read_key()
            result = fn(copy.deepcopy(state), key)
            if result is None:
                made.update(state=state, key=key, new=None)
                raise _Unchanged
            new_state, new_key = result
            new_state = {k: v for k, v in new_state.items() if k != KEY_ID}
            mark = _key_id(new_key) if new_key is not None else state.get(KEY_ID)
            if mark:
                new_state[KEY_ID] = mark
            made.update(state=new_state, key=key if new_key is None else new_key, new=new_key)
            return new_state
        try:
            try:
                KUBE.update_json(f"/api/v1/namespaces/{NS}/configmaps/{CONFIG}", "fleet.json", apply,
                                 read=kget, write=ksend, indent=2)
            except _Unchanged:
                pass
            if made["new"] is not None and not _store_key(made["new"]):
                # A list saved since with another key won: this copy uses those.
                made["state"], made["key"] = _read()
        except BaseException:
            _cache.update(at=0.0)       # whatever was saved, the next look reads it
            raise
        _cache.update(at=time.time(), state=made["state"], key=made["key"])
        return made["state"], made["key"]


def _store_key(key, attempts=4):
    """Write the key a list was just saved with. The Secret is written with the
    version read; a list saved since with another key wins, and its own
    writer stores that key."""
    path = f"/api/v1/namespaces/{NS}/secrets/{SECRET}"
    encoded = base64.b64encode(key).decode()
    for _ in range(attempts):
        stored_key, current = _read_key()
        try:
            listed = json.loads((kget(f"/api/v1/namespaces/{NS}/configmaps/{CONFIG}").get("data") or {})
                                .get("fleet.json") or "{}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            listed = {}
        except ValueError:
            listed = None               # not a list this copy saved
        # The list was just saved naming this key. Any other list now - one
        # with another key, one an older Homestead saved with none, or none
        # at all - has its own writer, whose key this must not replace.
        if not isinstance(listed, dict) or listed.get(KEY_ID) != _key_id(key):
            return False
        if current is not None and stored_key == key:
            return True
        try:
            if current is None:
                ksend("POST", path.rsplit("/", 1)[0], {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                                     "metadata": {"name": SECRET, "namespace": NS},
                                                     "data": {"key": encoded}})
            else:
                ksend("PUT", path, dict(current, data={**(current.get("data") or {}), "key": encoded}))
            return True
        except urllib.error.HTTPError as error:
            # 409: written or created meanwhile; 404 on the update: deleted meanwhile.
            if error.code != 409 and not (error.code == 404 and current is not None):
                raise
            error.close()
    raise RuntimeError("the linked clusters' key kept changing while it was being saved; try again")


def _settled(state):
    """The list a change is made on has this Homestead's own id."""
    if not state.get("self"):
        raise ValueError("the linked clusters' list was removed meanwhile; nothing was changed, try again")
    return state


def _load(fresh=False):
    """This Homestead's view of the links, read at most every few seconds."""
    with _lock:
        if not fresh and _cache["state"] is not None and time.time() - _cache["at"] < 5:
            return _cache["state"], _cache["key"]
        try:
            state, key = _read()
        except Exception:
            # The API server is briefly away: keep what was known.
            if _cache["state"] is not None and time.time() - _cache["at"] <= 30:
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
    """The links as stored, created alone on first use: an id and a key.
    Only when they are not there: a list or key that cannot be read stops it."""
    def fn(state, key):
        changed, new_key = False, None
        if not state.get("self"):
            state = {"self": secrets.token_hex(6), "revision": 1, "updated": time.time(), "members": []}
            changed = True
        if not any(m.get("id") == state["self"] for m in state.get("members", [])):
            state.setdefault("members", []).insert(0, _self_record(state))
            changed = True
        if not key:
            new_key = secrets.token_bytes(32)
            changed = True
        return (state, new_key) if changed else None
    return _change(fn)


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


def words(name):
    """A recorded identity as a person reads it, for text Homestead writes:
    "alice (via Office)", "Office", "API key backup", "ada approving Claude Code".
    The same rules as UI.who in web/js/ui.js; tests/fixtures/who-cases.json holds
    the cases both follow. A linked Homestead is named by its member name, or its
    handle when the member list cannot be read.

    The name may come from a linked Homestead, so it is read at most WORDS_DEPTH
    levels deep (the rest is shown as recorded), and reading it never raises into
    the text it is for: anything unexpected gives the name as recorded."""
    s = "" if name is None else str(name)
    try:
        return _words(s, 0)
    except Exception:
        return s


WORDS_DEPTH = 3


def _words(s, depth):
    if depth >= WORDS_DEPTH:
        return s
    rest = s.split(":", 1)[1] if ":" in s else s
    cut = rest.find(":")

    def linked_name(handle):
        try:
            found = next((m for m in members() if m.get("handle") == handle), None)
        except Exception:
            found = None
        return (found or {}).get("name") or handle

    if s.startswith("api-key:"):
        return f"API key {rest}"
    if s.startswith("approving:") and cut > 0:
        return f"{_words(rest[cut + 1:], depth + 1)} approving {rest[:cut]}"
    if not s.startswith("fleet:"):
        return s
    if cut < 0 or cut == len(rest) - 1:
        return linked_name(rest if cut < 0 else rest[:cut])
    return f"{_words(rest[cut + 1:], depth + 1)} (via {linked_name(rest[:cut])})"


# ------------------------------------------------------------------ signing
def _digest(sender, stamp, nonce, method, target, user, role, body):
    return "\n".join([sender, stamp, nonce, method.upper(), target, user, role,
                      hashlib.sha256(body or b"").hexdigest()]).encode()


def sign(method, target, body=b"", user="", role="admin", key=None):
    """Headers that let another member know this request is from here, for whom.
    key: sign with this one, not the one stored - the key the members still
    hold while a new one is on its way to them."""
    state, stored = _load()
    key = key or stored
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

    A person forwarded by another member is `fleet:<that member's handle>:<name>`,
    with the role they hold there. A call Homestead makes itself - a move, a
    sync - is `fleet:<handle>`, as admin. No account made since usernames
    stopped taking ':' can have such a name, so a person relayed here does not
    share alerts, setup, diagnostics or history with a local account.
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
    _claim_nonce(sender, nonce)
    with _lock:
        now = time.time()
        for seen in [n for n, until in _nonces.items() if until < now]:
            _nonces.pop(seen, None)
        if nonce in _nonces:
            raise PermissionError("signed request was already used")
        _nonces[nonce] = now + 2 * SKEW
    if role and role not in ROLES:
        raise PermissionError("unknown role")
    via = f"fleet:{origin['handle'] if HANDLE.fullmatch(origin.get('handle') or '') else sender}"
    # A name from an older Homestead, or a crafted one, may hold ':'; written as
    # the look-alike '∶' (as agent key names are), it cannot pass for another form.
    user = str(user or "").replace(":", "∶")
    return {"sender": origin, "role": role, "user": (f"{via}:{user}" if user else via) if role else ""}


class ReplayStore:
    """The nonces linked Homesteads have used, shared by every copy of this
    one through a ConfigMap whose resourceVersion is the replay boundary.

    A claim is durable before it returns, as before, so a nonce one copy
    took is refused by the other. What changed (#985): claims that arrive
    while a write is under way wait for it and then go out together in the
    next write, one GET and one PUT for the batch instead of per request;
    and the ConfigMap as this copy last wrote it is kept, so a claim reads
    it again only after another copy changed it (the PUT answers 409)."""
    LIMIT = 8000

    def __init__(self):
        self._cv = threading.Condition()
        self._pending = []          # tickets waiting for the next write
        self._writing = False
        self._current = None        # the ConfigMap as this copy last wrote or read it

    def claim(self, sender, nonce):
        ticket = {"digest": hashlib.sha256((sender + ":" + nonce).encode()).hexdigest(), "done": False, "error": None}
        with self._cv:
            self._pending.append(ticket)
            while not ticket["done"]:
                if self._writing:
                    self._cv.wait()
                    continue
                self._writing, batch, self._pending = True, self._pending, []
                self._cv.release()
                try:
                    self._write(batch)
                finally:
                    self._cv.acquire()
                    self._writing = False
                    self._cv.notify_all()
        if ticket["error"]:
            raise PermissionError(ticket["error"])

    def _write(self, batch):
        path = f"/api/v1/namespaces/{NS}/configmaps/{NAMES.BRAND}-fleet-replay"
        try:
            for _attempt in range(4):
                current = self._current
                if current is None:
                    try:
                        current = kget(path)
                    except urllib.error.HTTPError as error:
                        if error.code != 404:
                            raise PermissionError("fleet replay store is unavailable") from error
                        current = {}
                now = time.time()
                records = {k: until for k, until in json.loads((current.get("data") or {}).get("nonces", "{}")).items()
                           if until > now}
                for ticket in batch:
                    ticket["error"] = None
                    if ticket["digest"] in records:
                        ticket["error"] = "signed request was already used"
                    elif len(records) >= self.LIMIT:
                        ticket["error"] = "fleet request limit reached; try again later"
                    else:
                        records[ticket["digest"]] = now + 2 * SKEW
                if all(ticket["error"] for ticket in batch):
                    return
                meta = {"name": NAMES.BRAND + "-fleet-replay", "namespace": NS}
                if current.get("metadata", {}).get("resourceVersion"):
                    meta["resourceVersion"] = current["metadata"]["resourceVersion"]
                body = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta,
                        "data": {"nonces": json.dumps(records, separators=(",", ":"))}}
                try:
                    written = ksend("PUT" if current else "POST", path if current else path.rsplit("/", 1)[0], body)
                except urllib.error.HTTPError as error:
                    self._current = None        # changed or gone elsewhere: read it again
                    if error.code not in (404, 409):
                        raise PermissionError("fleet replay store is unavailable") from error
                    continue
                kept = written if isinstance(written, dict) and (written.get("metadata") or {}).get("resourceVersion") else None
                self._current = kept and {"metadata": kept["metadata"], "data": kept.get("data") or {}}
                return
            raise PermissionError("fleet replay store is busy; try again")
        except PermissionError as error:
            for ticket in batch:
                ticket["error"] = ticket["error"] or str(error)
        except Exception as error:
            self._current = None
            for ticket in batch:
                ticket["error"] = "fleet replay store is unavailable"
            print(f"Fleet replay store: {str(error)[:200]}", flush=True)
        finally:
            for ticket in batch:
                ticket["done"] = True


_replay = ReplayStore()


def _claim_nonce(sender, nonce):
    """Kubernetes resourceVersion provides a replay boundary across replicas."""
    _replay.claim(sender, nonce)


def console_authorizer(sender):
    state, key = _load()
    def valid():
        fresh_state, fresh_key = _read()
        return bool(key and fresh_key and hmac.compare_digest(key, fresh_key) and
                    any(m.get("id") == sender for m in fresh_state.get("members", [])))
    return valid


# ------------------------------------------------------------------ calling
def _open(request, timeout):
    context = ssl.create_default_context() if request.full_url.startswith("https") else None
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _reason(error):
    try:
        return str(json.loads(error.read(REASON_MAX_BYTES).decode("utf-8", "replace") or "{}").get("error") or "")[:240]
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
    deadline = time.monotonic() + timeout
    try:
        with _open(request, timeout) as response:
            body = FETCH.read(response, ANSWER_MAX_BYTES, deadline, "the linked cluster's answer")
            return json.loads(body.decode() or "{}"), response
    except urllib.error.HTTPError as error:
        reason = _reason(error)
        if error.code >= 500:
            raise Unreachable(reason or f"HTTP {error.code}") from error
        raise ValueError(reason or f"HTTP {error.code}") from error
    except (ValueError, Unreachable):
        raise
    except Exception as error:
        raise Unreachable(f"no answer from {url}: {str(error)[:120]}") from error


def call(ref, method, path, body=None, timeout=TIMEOUT, user="", role="admin", signing_key=None):
    """Ask another member, signed. Returns its JSON answer."""
    target = member(ref) if isinstance(ref, str) else ref
    if not target:
        raise ValueError(f"no linked cluster called {ref}")
    data = json.dumps(body).encode() if body is not None else b""
    full = urllib.parse.urlparse(target["url"].rstrip("/") + path)
    signature = sign(method, full.path + (f"?{full.query}" if full.query else ""), data, user, role, key=signing_key)
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


def _check_into(row, checks):
    try:
        checks[row["id"]] = check(row)
    finally:
        with _checking_lock:
            if _checking.get(row["id"]) is threading.current_thread():
                del _checking[row["id"]]


def _start_checks(rows, checks):
    """A thread checking each member into checks, unless one from an earlier
    page load still runs for it (a member that does not answer would gain a
    thread per page load): that one is waited for instead."""
    threads = []
    for row in rows:
        with _checking_lock:
            running = _checking.get(row["id"])
            if running is not None and running.is_alive():
                threads.append(running)
                continue
            thread = _checking[row["id"]] = threading.Thread(target=_check_into, args=(row, checks), daemon=True)
        thread.start()
        threads.append(thread)
    return threads


def _wait(threads, end):
    for thread in threads:
        thread.join(max(0, end - time.monotonic()))


def _pending(member_id):
    """A member with no answer in this page load: its last known state, or
    none, marked as still being checked while its check runs."""
    last = _status.get(member_id) or {}
    with _checking_lock:
        running = member_id in _checking
    if not running:
        return last
    return {**last, "checking": True, "error": last.get("error") or "still checking"}


def follow_address(previous=()):
    """Move this Homestead's stored address onto its VIP when the stored one
    is only the address it was given before it had one - the node's own, or
    none, or a VIP it has just left (previous). An address an admin typed
    is theirs and stays. Returns the new one, told to the other members."""
    state, _ = _load()
    mine = next((m for m in state.get("members", []) if m.get("id") == state.get("self")), None)
    now = _address()
    given = set(_earlier() or ()) | set(previous)

    def movable(mine):
        return bool(mine and now and mine.get("url") != now and (not mine.get("url") or mine["url"] in given))
    if not movable(mine):
        return ""
    # Checked again on the list as saved: an address typed meanwhile on another copy stays.
    return now if set_address(now, only_if=movable).get("saved", True) else ""


def summary(via=None):
    """Every linked cluster, with whether it answers here and what it runs."""
    state, _ = _load()
    if not state.get("self"):
        state, _ = _ensure()
    me = state["self"]
    rows = [m for m in state.get("members", []) if m.get("id") != me]
    checks = {}
    end = time.monotonic() + SUMMARY_WAIT     # one wait for all of them, not one each
    _wait(_start_checks(rows, checks), end)
    # A member that has heard of a newer list than this one: bring it here.
    seen_now = dict(checks)   # a late check may still add to checks
    ahead = max((c for c in seen_now.values() if c.get("reachable")), key=lambda c: c["revision"], default=None)
    if ahead and ahead["revision"] > int(state.get("revision") or 0):
        source = next((m for m in rows if seen_now.get(m["id"]) is ahead), None)
        try:
            adopt(call(source, "GET", "/api/fleet/state", timeout=4))
            state, _ = _load(fresh=True)
        except Exception:
            pass
        # The members only the newer list names: checked as the others were,
        # within what is left of the same wait.
        newcomers = [row for row in state.get("members", []) if row.get("id") != me and row["id"] not in seen_now]
        _wait(_start_checks(newcomers, checks), end)
    seen_now = dict(checks)
    out = []
    for m in state.get("members", []):
        mine = m.get("id") == me
        seen = {} if mine else seen_now.get(m["id"]) or _pending(m["id"])
        out.append({"id": m["id"], "handle": m.get("handle", ""), "url": m.get("url", ""),
                    "name": _self_record(state)["name"] if mine else seen.get("name") or m.get("name", ""),
                    "self": mine, "version": VERSION if mine else seen.get("version", ""),
                    "reachable": True if mine else bool(seen.get("reachable")),
                    "error": "" if mine else seen.get("error", ""),
                    "compatible": True if mine else (seen.get("protocol") in (0, PROTOCOL) or not seen.get("reachable")),
                    "checking": False if mine else bool(seen.get("checking"))})
    return {"self": me, "protocol": PROTOCOL, "linked": len(out) > 1, "members": out,
            "via": (via or {}).get("name", ""), "via_id": (via or {}).get("id", ""),
            "address": _self_record(state)["url"], "suggested_address": _address()}


def adopt(incoming, sender=None):
    """Take a newer member list from another member.

    The newer list wins. A list without this Homestead in it means it was
    unlinked: it goes back to standing alone, with a key of its own.
    """
    revision = int(incoming.get("revision") or 0)
    rows = _plain_handles([m for m in incoming.get("members") or [] if isinstance(m, dict) and m.get("id")])
    outcome = {}

    def fn(state, key):
        _settled(state)
        ours = int(state.get("revision") or 0)
        if revision < ours or (revision == ours and rows == state.get("members")):
            outcome.clear()
            outcome.update(changed=False, revision=ours)
            return None
        if not any(m["id"] == state["self"] for m in rows):
            fresh = {"self": state["self"], "revision": revision + 1, "updated": time.time(),
                     "members": [_self_record(state)]}
            outcome.clear()
            outcome.update(changed=True, left=True, revision=fresh["revision"])
            return fresh, secrets.token_bytes(32)
        outcome.clear()
        outcome.update(changed=True, revision=revision)
        return {"self": state["self"], "revision": revision, "updated": incoming.get("updated") or time.time(),
                "members": rows}, (base64.b64decode(incoming["key"]) if incoming.get("key") else None)
    with _lock:
        _ensure()
        _change(fn)
        if outcome.get("left"):
            _status.clear()
        return dict(outcome)


def _broadcast(state, skip=(), key=None, signing_key=None):
    """Tell every other member about a change. Returns who could not be told."""
    payload = {"revision": state["revision"], "updated": state["updated"], "members": state["members"]}
    if key:
        payload["key"] = base64.b64encode(key).decode()
    missed = []
    for m in state["members"]:
        if m["id"] == state["self"] or m["id"] in skip:
            continue
        try:
            call(m, "POST", "/api/fleet/sync", payload, timeout=8, signing_key=signing_key)
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


def _plain(url):
    return str(url or "").strip().lower().startswith("http://")


def _refuse_plain(sent_to, what, plain_ok):
    """Secrets cross plain http:// only when the person agreed to it (#602)."""
    plain = [m for m in sent_to if _plain(m.get("url"))]
    if plain and not plain_ok:
        names = ", ".join(f"{m.get('name') or m.get('url')} ({m.get('url')})" for m in plain)
        raise ValueError(f"{what} would go unencrypted to {names}: that is a plain http:// address. "
                         "Agree to send it unencrypted, or give it an https:// address; nothing was sent")


def join(url, username, password, own_url="", plain_ok=False):
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
    _refuse_plain([{"url": url}, {"name": "this Homestead's address", "url": own}],
                  "the admin password and the clusters' key", plain_ok)
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
        record = _plain_handles([{k: record.get(k, "") for k in ("id", "handle", "name", "added")}])[0]
        record["url"] = url
        sent = rows + [record]

        def fn(state, _key):
            # Made on the list stored now: a change another copy saved while
            # the other Homestead was asked stays, under a newer revision.
            _settled(state)
            now = [dict(m, url=own) if m["id"] == state["self"] else m
                   for m in state.get("members", []) if m["id"] != record["id"]]
            return {"self": state["self"], "revision": max(int(state.get("revision") or 0) + 1, revision),
                    "updated": time.time(), "members": now + [record]}, None
        state, _ = _change(fn)
    _status.pop(record["id"], None)
    # The new member already has the list it was sent - unless another copy
    # changed the list meanwhile: then it hears the list as saved too.
    fresh = state["revision"] == revision and state["members"] == sent
    missed = _broadcast(state, skip=(record["id"],) if fresh else ())
    return {"ok": True, "member": record, "missed": missed}


def accept(body):
    """Be linked by another Homestead: take its key and its members."""
    taken = {}

    def fn(state, _key):
        # Checked again on the list stored now: a link saved meanwhile by
        # another copy is refused the same way.
        _settled(state)
        if len(state.get("members", [])) > 1:
            raise ValueError("this Homestead is already linked; unlink it first")
        mine = _self_record(state, url=url)
        mine["handle"] = handle(mine["name"], {m.get("handle") for m in rows})
        taken["mine"] = mine
        return {"self": state["self"], "revision": int(body.get("revision") or 1), "updated": time.time(),
                "members": rows + [mine]}, base64.b64decode(body["key"])
    with _lock:
        state, _ = _ensure()
        if len(state.get("members", [])) > 1:
            raise ValueError("this Homestead is already linked; unlink it first")
        rows = _plain_handles([m for m in body.get("members") or [] if isinstance(m, dict) and m.get("id")])
        if not rows or not body.get("key"):
            raise ValueError("nothing to link to")
        url = _clean_url(body.get("url") or "")
        _change(fn)
        _status.clear()
        return taken["mine"]


def remove(ref, plain_ok=False):
    """Unlink a member. The rest get a new key; the one leaving is told first."""
    state, key = _ensure()
    target = member(ref)
    if not target:
        raise ValueError(f"no linked cluster called {ref}")
    if target["id"] == state["self"]:
        return leave(plain_ok)
    rows = [m for m in state["members"] if m["id"] != target["id"]]
    _refuse_plain([m for m in rows if m["id"] != state["self"]], "the clusters' new key", plain_ok)
    state = {"self": state["self"], "revision": int(state.get("revision") or 0) + 1,
             "updated": time.time(), "members": rows}
    try:
        call(target, "POST", "/api/fleet/sync",
             {"revision": state["revision"], "updated": state["updated"], "members": rows}, timeout=8)
        told = True
    except Exception:
        told = False
    fresh, told_revision, held = secrets.token_bytes(32), state["revision"], {}

    def fn(state, key):
        # On the list stored now: a change another copy saved meanwhile stays,
        # and the new key goes to exactly the members of the list saved.
        _settled(state)
        rows = [m for m in state.get("members", []) if m["id"] != target["id"]]
        _refuse_plain([m for m in rows if m["id"] != state["self"]], "the clusters' new key", plain_ok)
        held["key"] = key
        return {"self": state["self"], "revision": max(int(state.get("revision") or 0) + 1, told_revision),
                "updated": time.time(), "members": rows}, fresh
    saved, in_force = _change(fn)
    # Saved first, then sent, signed with the key the members still hold: the
    # key never reaches a member another copy unlinked meanwhile. When a list
    # saved since with another key won, that list's own writer sends its key.
    missed = _broadcast(saved, key=fresh, signing_key=held["key"]) if in_force == fresh else []
    _status.pop(target["id"], None)
    forget_answers(target["id"])
    return {"ok": True, "told": told, "missed": missed}


def leave(plain_ok=False):
    """Stop being linked: the others keep each other, this one stands alone."""
    state, key = _ensure()
    rows = [m for m in state["members"] if m["id"] != state["self"]]
    if not rows:
        return {"ok": True, "missed": []}
    _refuse_plain(rows, "the clusters' new key", plain_ok)
    rest = {}

    def fn(state, key):
        # The rest are the members of the list as saved: one another copy
        # unlinked meanwhile is not sent their new key.
        _settled(state)
        others = [m for m in state.get("members", []) if m["id"] != state["self"]]
        _refuse_plain(others, "the clusters' new key", plain_ok)
        revision = int(state.get("revision") or 0) + 1
        rest.update(held=key, group={"self": state["self"], "revision": revision, "updated": time.time(),
                                     "members": others})
        return {"self": state["self"], "revision": revision + 1, "updated": time.time(),
                "members": [_self_record(state)]}, secrets.token_bytes(32)
    _change(fn)
    # The rest get a key this one forgets, so it cannot speak for them after;
    # signed with the key they still hold, as this one now has its own.
    missed = _broadcast(rest["group"], key=secrets.token_bytes(32), signing_key=rest["held"]) \
        if rest["group"]["members"] else []
    _status.clear()
    forget_answers()
    return {"ok": True, "missed": missed}


def set_address(url, only_if=None):
    """Where the other members reach this Homestead. only_if(record) is checked
    on this Homestead's record as stored when it is saved; when it says no,
    nothing is saved or told and the answer says "saved": False."""
    url = _clean_url(url)
    saved = []

    def fn(state, _key):
        _settled(state)
        saved.clear()
        if only_if is not None and not only_if(next((m for m in state.get("members", [])
                                                      if m.get("id") == state["self"]), None)):
            return None
        saved.append(1)
        rows = [dict(m, url=url) if m["id"] == state["self"] else m for m in state.get("members", [])]
        return {"self": state["self"], "revision": int(state.get("revision") or 0) + 1,
                "updated": time.time(), "members": rows}, None
    with _lock:
        _ensure()
        state, _ = _change(fn)
    if not saved:
        return {"ok": True, "missed": [], "saved": False}
    return {"ok": True, "missed": _broadcast(state)}


def _renamed(state, name):
    """This Homestead's record, when the other members still have another name for it."""
    mine = next((m for m in state.get("members", []) if m.get("id") == state.get("self")), None)
    if not mine or not name or mine.get("name") == name or len(state.get("members", [])) < 2:
        return None
    return mine


def refresh_name():
    """Carry a renamed site to the other members, so the switch says it."""
    name = str(_site() or "").strip()
    if not _renamed(_load()[0], name):
        return
    renamed = []

    def fn(state, _key):
        renamed.clear()
        if not _renamed(state, name):
            return None
        renamed.append(1)
        rows = [dict(m, name=name) if m["id"] == state["self"] else m for m in state["members"]]
        return {"self": state["self"], "revision": int(state.get("revision") or 0) + 1,
                "updated": time.time(), "members": rows}, None
    state, _ = _change(fn)
    if renamed:
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


def response_head(head, cookies=(), keep_version=True):
    """The answer as the browser gets it: the other member's own cookies are
    not this origin's to set; this one's session refresh is. The member's
    version is the page's only when the member serves the page (the cluster
    picked in the top bar); an action this page sends to one linked cluster -
    updating it, above all - must not make the page think it was updated."""
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0]
    upgrade = " 101 " in f"{status} "
    kept = [line for line in lines[1:] if line and not line.lower().startswith("set-cookie:")
            and (upgrade or not line.lower().startswith("connection:"))
            and (keep_version or not line.lower().startswith("x-homestead-version:"))]
    if not upgrade:
        kept.append("Connection: close")
    kept += [f"Set-Cookie: {cookie}" for cookie in cookies]
    return ("\r\n".join([status] + kept) + "\r\n\r\n").encode("latin-1"), upgrade


def forward(handler, ref, body, user="", role="", cookies=()):
    """Relay one request from the browser to a linked member, and its answer back.

    Raises Unreachable before anything is sent to the browser, so the caller
    can still answer with an error of its own.
    """
    AUDIT_LOG.note(to=ref)      # the audit line says where it went; the member writes its own
    target = member(ref)
    if not target:
        raise Unreachable("that cluster is no longer linked")
    websocket = (handler.headers.get("Upgrade") or "").lower() == "websocket"
    if websocket:
        # A console from another site is refused here, as it would be anywhere -
        # checked as a console on this cluster checks it: against the address the
        # browser used, which a proxy or tunnel in front gives as X-Forwarded-Host.
        origin = urllib.parse.urlparse(handler.headers.get("Origin") or "").netloc.lower()
        host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or ""
        if not origin or origin != host.lower():
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
    # Named on the request itself (a header, or a console's hs_cluster), not
    # the top bar's choice: the page here is this Homestead's, not the member's.
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    named = bool(handler.headers.get("X-Homestead-Cluster") or query.get("hs_cluster"))
    out, upgrade = response_head(head, cookies, keep_version=not named)
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
    if hasattr(handler, "_console_authorizer"):
        handler.connection.settimeout(None)
        need = "admin" if urllib.parse.urlparse(handler.path).path == "/api/node/shell" else "operator"
        SESSIONS.watch(handler.user, handler._console_authorizer(need), done, handler.connection, sock)

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


def _own_name(user):
    """The name to ask another member for: a person relayed here by their own,
    as the member that relayed them would; a local account's whole."""
    user = str(user or "")
    if not user.startswith("fleet:"):
        return user
    return user.split(":", 2)[2] if user.count(":") >= 2 else ""


# Each linked Homestead's last answer, per list and per person asking (#705). The
# view of all clusters answers at once with what has arrived; a Homestead that is
# slow fills in on a later refresh instead of holding up the rest, and is asked
# again only once its earlier question has come back.
ANSWER_BUDGET = 2          # seconds a gather waits for linked Homesteads
ANSWER_KEEP = 300          # a last answer older than this is no longer shown (out of reach only)
NOT_ANSWERING = 3          # misses in a row before a Homestead is said not to be answering
_answers = {}
_answers_lock = threading.Lock()


def forget_answers(member_id=None):
    """Drop the kept answers of one member, or of every member (unlinking)."""
    with _answers_lock:
        for key in [key for key in _answers if member_id in (None, key[1])]:
            del _answers[key]


def _said(error, m):
    """What went wrong, said once: without the member's name again or its address."""
    if isinstance(error, Unreachable):
        return "it did not answer"
    text = str(error)
    if text.startswith(f"{m['name']}: "):
        text = text[len(m["name"]) + 2:]
    return text[:200]


def _ask(entry, done, m, what, user, role):
    """Ask one member, keep its answer in entry, then set done: the Event of
    the gather that asked, never a fresher one a later gather swapped in."""
    try:
        result = call(m, "GET", f"/api/{what}", timeout=12, user=_own_name(user), role=role)
        if what == "flow" and (not isinstance(result, dict) or
                any(not isinstance(result.get(key), list) for key in ("workloads", "volumes", "nodes", "vips"))):
            raise ValueError("Architecture data is unavailable")
        if what != "flow" and not isinstance(result, list):
            raise ValueError("the answer is not a list")
        with _answers_lock:
            entry.update(result=result, at=time.time(), misses=0, error="")
    except ValueError as error:
        # A refusal (HTTP 4xx) or an answer that makes no sense: the rows
        # kept from before are not shown any more.
        with _answers_lock:
            entry.update(result=None, at=0, misses=entry["misses"] + 1, error=_said(error, m))
    except Exception as error:
        # Out of reach, timed out or failing (5xx): the last rows stay, with their age.
        with _answers_lock:
            entry.update(misses=entry["misses"] + 1, error=_said(error, m))
    finally:
        with _answers_lock:
            entry["pending"] = False
        done.set()


def gather(what, local, user, role):
    """Gather tagged resource lists or Architecture graphs from linked clusters.

    Each cluster is asked as the person asking, so it shows them what their
    role lets them see there. A cluster that has not answered within
    ANSWER_BUDGET is named rather than holding up the rest: its last answer, if
    recent, is shown with its age, and it fills in on a later refresh. local
    answers for this cluster.
    """
    view = summary()
    tags = {m["id"]: {"id": m["id"], "name": m["name"], "handle": m["handle"], "self": m["self"]}
            for m in view["members"]}
    deadline = time.monotonic() + ANSWER_BUDGET
    asked = []
    for m in view["members"]:
        if m["self"] or not m["reachable"]:
            continue
        with _answers_lock:
            entry = _answers.setdefault((what, m["id"], user, role),
                                        {"result": None, "at": 0, "misses": 0, "error": "",
                                         "pending": False, "done": threading.Event()})
            start = not entry["pending"]
            if start:
                entry["pending"], entry["done"] = True, threading.Event()
            done = entry["done"]
        if start:
            threading.Thread(target=_ask, args=(entry, done, m, what, user, role), daemon=True).start()
        asked.append((m, entry, done))
    results = {}
    missing = [{"id": m["id"], "name": m["name"], "error": m.get("error") or "not answering",
                "state": "not answering"}
               for m in view["members"] if not m["self"] and not m["reachable"]]
    try:
        results[view["self"]] = local()
    except Exception as error:
        if what != "flow":
            raise
        missing.append({"id": view["self"], "name": tags[view["self"]]["name"], "error": str(error)[:200],
                        "state": "error"})
    for m, entry, done in asked:
        done.wait(max(0.0, deadline - time.monotonic()))
        with _answers_lock:
            # Whether the question this gather asked has come back: a later
            # gather may already have asked again and set "pending" anew.
            pending, misses, error = not done.is_set(), entry["misses"], entry["error"]
            result, at = entry["result"], entry["at"]
        if not pending and not misses:
            results[m["id"]] = result
            continue
        note = {"id": m["id"], "name": m["name"],
                "state": "not answering" if misses >= NOT_ANSWERING else "waiting" if pending else "error",
                "error": error or "has not answered yet"}
        # Its age now, after the wait; never below 0 should the clock step back.
        age = max(0.0, time.time() - at)
        if result is not None and age <= ANSWER_KEEP:
            note["age_s"] = int(age)
            results[m["id"]] = result
            tags[m["id"]] = {**tags[m["id"]], "age_s": note["age_s"]}
        missing.append(note)
    rows = []
    for m in view["members"]:
        result = results.get(m["id"])
        entries = [result] if what == "flow" and result is not None else result or []
        for row in entries:
            if isinstance(row, dict):
                rows.append({**row, "site": tags[m["id"]]})
    return ({"clusters": rows, "missing": missing} if what == "flow" else rows), missing


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/fleet/hello"): ("viewer", lambda request: hello()),
}
