"""
Authentication for Homestead.

Design notes, because the choices matter more than the code:

* Passwords are PBKDF2-HMAC-SHA256 with a per-user salt and 600k iterations
  (OWASP's 2023 floor). stdlib only — no bcrypt/argon2 dependency.
* Sessions are *stateless signed tokens*, not server-side session objects.
  Homestead's pod restarts on every deploy; server-side sessions would log
  everyone out each time. The signing key lives in the same Secret as the
  users, so tokens survive a restart but die if the Secret is rotated.
* Every user record carries a `ver`. Bumping it invalidates that user's
  existing tokens — that is how "log out everywhere" and password changes work.
* Login is rate-limited per client address. It is a lab tool, but an
  unauthenticated endpoint that does 600k PBKDF2 rounds is a free DoS
  otherwise.
* Roles are enforced **server-side, per route**. Hiding a button is a courtesy,
  not a control — a viewer who crafts the request by hand still gets a 403.
"""
import base64
import copy
import homestead_names as NAMES
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import homestead_sessions as SESSIONS

kget = ksend = None
NS = "lab"
def SECRET_NAME():
    """The credentials Secret, under whichever name this install already has."""
    return NAMES.object_name("auth", NS, kind="secrets")

ITERATIONS = 600_000
COOKIE = "homestead_session"

# A session has two clocks. The idle window is how long a session survives with
# nothing happening, and it restarts on use - so nobody is signed out in the
# middle of working, which is the whole complaint with a fixed expiry. The
# absolute window is how long a session may live at all, however busy, and it
# does not restart: a cookie copied off a machine stops working eventually
# whatever the thief does with it.
SESSION_TTL = int(os.environ.get("SESSION_TTL_HOURS", "12")) * 3600
REMEMBER_TTL = int(os.environ.get("SESSION_REMEMBER_DAYS", "30")) * 86400
ABSOLUTE_TTL = int(os.environ.get("SESSION_MAX_DAYS", "90")) * 86400
# Refreshed halfway through rather than on every request, so an active browser
# is not handed a new cookie several times a second.
REFRESH_AFTER = 0.5


def idle_ttl(remember):
    return REMEMBER_TTL if remember else SESSION_TTL

# viewer < operator < admin
ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}


def rank(role):
    return ROLE_RANK.get(role or "viewer", 0)


def allows(user_role, needed):
    return rank(user_role) >= rank(needed)

_store_cache = {"at": 0, "data": None}
_attempts = {}          # "ip:<addr>" or "user:<name>" -> [timestamp, ...]
MAX_ATTEMPTS = 8
# Per account, across every address: an address can be changed, a name cannot.
MAX_USER_ATTEMPTS = 20
ATTEMPT_WINDOW = 300
MAX_TRACKED = 10000
MAX_STALE_SECONDS = 30
_rate_lock = threading.RLock()
_password_slots = threading.BoundedSemaphore(4)


def bind(_kget, _ksend, _ns):
    global kget, ksend, NS
    kget, ksend, NS = _kget, _ksend, _ns
    NAMES.bind(_kget)


# ------------------------------------------------------------------ store
class StoreUnavailable(Exception):
    """The accounts could not be read, which is not the same as there being
    none. A slow or unreachable API server must never look like a fresh
    install: that offers first-admin setup to whoever asks, and saving it
    would replace every real account."""


def _unavailable(error, allow_stale=True):
    # The last good read stands in while the cluster is slow to answer; its
    # time is left alone, so the next request tries the cluster again.
    if (allow_stale and _store_cache["data"] is not None and
            time.time() - _store_cache["at"] <= MAX_STALE_SECONDS):
        return _store_cache["data"]
    unavailable = StoreUnavailable("Homestead cannot read its accounts from the cluster right now. "
                                   "The Kubernetes API may be slow or unreachable.")
    unavailable.cause = unavailable_cause(error)
    raise unavailable


def unavailable_cause(error):
    """What stopped the read, said plainly; the raw error otherwise."""
    reason = str(error) or type(error).__name__
    lowered = reason.lower()
    if "errno -3" in lowered or "name resolution" in lowered or "name or service not known" in lowered:
        return "The cluster's DNS did not answer. The host running CoreDNS may be down."
    if "timed out" in lowered or "timeout" in lowered:
        return "The Kubernetes API did not answer in time."
    if "connection refused" in lowered:
        return "The Kubernetes API refused the connection."
    return reason[:160]


class StoreConflict(ValueError):
    """The accounts changed on another replica between reading and saving."""


def _load(force=False, max_age=10, allow_stale=True):
    if not force and _store_cache["data"] is not None and time.time() - _store_cache["at"] < max_age:
        return _store_cache["data"]
    try:
        sec = kget(f"/api/v1/namespaces/{NS}/secrets/{SECRET_NAME()}")
        raw = base64.b64decode(sec.get("data", {}).get("store.json", "") or "e30=")
        data = json.loads(raw.decode() or "{}")
        # The version read, so a save can say what it changed - and never write
        # over a change another replica made since (a revoked key coming back).
        data["_rv"] = (sec.get("metadata") or {}).get("resourceVersion")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return _unavailable(e, allow_stale)
        data = {}                       # no Secret yet: genuinely a fresh install
    except Exception as e:
        return _unavailable(e, allow_stale)
    data.setdefault("users", {})
    data.setdefault("signing_key", "")
    _store_cache.update(at=time.time(), data=data)
    return data


def _save(data):
    stored = {k: v for k, v in data.items() if k != "_rv"}
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET_NAME(), "namespace": NS},
            "data": {"store.json": base64.b64encode(json.dumps(stored).encode()).decode()}}
    if data.get("_rv"):
        body["metadata"]["resourceVersion"] = data["_rv"]
    try:
        # A missing record must use create, never an unconditional replacement
        # after another replica has completed setup in the meantime.
        answer = ksend("PUT" if data.get("_rv") else "POST",
                      f"/api/v1/namespaces/{NS}/secrets/{SECRET_NAME()}" if data.get("_rv") else f"/api/v1/namespaces/{NS}/secrets", body)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            _store_cache.update(at=0, data=None)
            raise StoreConflict("the accounts were changed elsewhere at the same moment; try again") from None
        raise
    data["_rv"] = ((answer or {}).get("metadata") or {}).get("resourceVersion")
    _store_cache.update(at=time.time(), data=data)


def _signing_key(data=None):
    data = data or _load()
    if not data.get("signing_key"):
        data["signing_key"] = secrets.token_urlsafe(48)
        _save(data)
    return data["signing_key"].encode()


def internal_signing_key():
    """Key shared only with trusted in-cluster helpers; never returned by HTTP."""
    return _signing_key()


def smart_signing_key():
    return hmac.new(_signing_key(), b"homestead-smart-helper-v1", hashlib.sha256).digest()


def review_signing_key():
    """Read-only, domain-separated key for expiring capacity acknowledgements.

    Login/setup must have initialized the account Secret already. A preview
    must never create/repair credentials, and all Homestead replicas must agree.
    """
    key = _load().get("signing_key")
    if not key:
        raise StoreUnavailable("The deployment review key is unavailable; sign in again before reviewing.")
    return hmac.new(key.encode(), b"homestead-capacity-review-v1", hashlib.sha256).digest()


def needs_setup():
    return not _load(force=True).get("users")


def user_count():
    return len(_load().get("users", {}))


def list_users():
    return [{"name": u, "role": v.get("role", "admin"), "created": v.get("created", ""),
             "last_login": v.get("last_login", "")}
            for u, v in sorted(_load().get("users", {}).items())]


# Personal layouts live with the account, not a session or a browser. The Secret's
# resourceVersion makes the write atomic across Homestead replicas.
_DASHBOARD_WIDTHS = {"compute": (4, 6, 8, 12), "throughput": (4, 6, 8, 12),
                     "storage": (4, 6, 8, 12), "nodes": (4, 6, 8, 12), "cpu": (4, 6, 8, 12),
                     "memory": (4, 6, 8, 12), "history": (8, 12), "portal": (4, 6, 8, 12),
                     **{key: (4, 6, 8, 12) for key in ("health", "workloads", "containers", "vms", "backups", "updates", "jobs", "custom", "custom2", "custom3", "custom4", "monitoring")}}


def _dashboard_layout(layout):
    if (not isinstance(layout, dict) or set(layout) != {"version", "items"}
            or type(layout["version"]) is not int or layout["version"] != 1
            or not isinstance(layout["items"], list) or len(layout["items"]) > len(_DASHBOARD_WIDTHS)):
        raise ValueError("Invalid dashboard layout")
    seen = set()
    for item in layout["items"]:
        if (not isinstance(item, dict) or not {"id", "width", "height"} <= set(item)
                or set(item) - {"id", "width", "height", "display", "column", "newRow", "groups", "status", "title", "format", "content"}):
            raise ValueError("Invalid dashboard widget")
        name = item["id"]
        if (not isinstance(name, str) or name not in _DASHBOARD_WIDTHS or name in seen
                or type(item["width"]) is not int or item["width"] not in _DASHBOARD_WIDTHS[name]
                or type(item["height"]) is not int or item["height"] not in (0, 240, 360, 520)):
            raise ValueError("Invalid dashboard widget or size")
        displays = {"nodes": ("compact", "detailed"), "portal": ("compact", "tiles")}
        if "display" in item and item["display"] not in displays.get(name, ()):
            raise ValueError("Invalid widget display")
        if "column" in item and (type(item["column"]) is not int or not 1 <= item["column"] <= 13 - item["width"]):
            raise ValueError("Invalid widget column")
        if "newRow" in item and type(item["newRow"]) is not bool:
            raise ValueError("Invalid widget row")
        if "groups" in item and (name not in ("containers", "portal") or not isinstance(item["groups"], list)
                or len(item["groups"]) > 100 or any(not isinstance(g, str) or len(g) > 40 for g in item["groups"])
                or len(set(item["groups"])) != len(item["groups"])):
            raise ValueError("Invalid widget groups")
        if "status" in item and (name not in ("containers", "vms") or item["status"] not in ("running", "stopped", "attention")):
            raise ValueError("Invalid widget status")
        custom_fields = {"title", "format", "content"} & set(item)
        if custom_fields and name not in ("custom", "custom2", "custom3", "custom4"):
            raise ValueError("Custom content is only supported by custom widgets")
        for key, limit in (("title", 80), ("content", 16384)):
            if key in item and (not isinstance(item[key], str) or len(item[key]) > limit):
                raise ValueError("Invalid custom widget " + key)
        if "format" in item and item["format"] not in ("text", "html"):
            raise ValueError("Invalid custom widget format")
        seen.add(name)
    return copy.deepcopy(layout)


def dashboard_preferences(username):
    data = _load(force=True, allow_stale=False)
    user = data["users"].get(username)
    if user is None:
        raise PermissionError("Account no longer exists; sign in again")
    return copy.deepcopy(user.get("dashboard", {"revision": None, "layout": None}))


def save_dashboard_preferences(username, body):
    if not isinstance(body, dict) or set(body) != {"revision", "layout"}:
        raise ValueError("Supply a dashboard layout and its revision")
    layout = _dashboard_layout(body["layout"])
    revision = body["revision"]
    if revision is not None and (not isinstance(revision, str) or len(revision) > 64):
        raise ValueError("Invalid dashboard revision")
    for attempt in range(3):
        data = copy.deepcopy(_load(force=True, allow_stale=False))
        user = data["users"].get(username)
        if user is None:
            raise PermissionError("Account no longer exists; sign in again")
        current = user.get("dashboard", {"revision": None, "layout": None})
        if current["revision"] != revision:
            raise StoreConflict("Your dashboard changed in another session. Cancel and reopen the editor to load the latest layout.")
        saved = {"revision": secrets.token_hex(16), "layout": layout}
        user["dashboard"] = saved
        try:
            _save(data)
            return copy.deepcopy(saved)
        except StoreConflict:
            # An unrelated account/password update can safely be merged after
            # rereading. A changed dashboard revision is rejected above.
            if attempt == 2:
                raise


def role_of(username):
    return _load().get("users", {}).get(username, {}).get("role", "viewer")


def set_role(username, role, acting_as):
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    data = _load(force=True)
    u = data.get("users", {}).get(username)
    if not u:
        raise ValueError("no such user")
    if username == acting_as and role != "admin":
        raise PermissionError("you cannot remove your own admin role")
    admins = [n for n, v in data["users"].items() if v.get("role", "admin") == "admin"]
    if admins == [username] and role != "admin":
        raise PermissionError("cannot demote the only administrator")
    u["role"] = role
    _save(data)
    SESSIONS.revoke(username)
    return {"ok": True, "user": username, "role": role}


# ------------------------------------------------------------------ passwords
def _hash(password, salt):
    return base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), ITERATIONS)
    ).decode()


def create_user(username, password, first_only=False, role="operator"):
    username = (username or "").strip().lower()
    if not username or not username.isascii() or len(username) < 3 or len(username) > 32:
        raise ValueError("username must be 3-32 ASCII characters")
    if not password or len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    data = _load(force=True)
    if first_only and data.get("users"):
        raise PermissionError("setup has already been completed")
    if first_only:
        role = "admin"          # whoever sets the system up owns it
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if username in data.get("users", {}):
        raise ValueError("that username already exists")
    salt = base64.b64encode(secrets.token_bytes(16)).decode()
    data.setdefault("users", {})[username] = {
        "salt": salt, "hash": _hash(password, salt), "ver": 1, "role": role,
        "id": secrets.token_urlsafe(24),
        "created": time.strftime("%Y-%m-%d %H:%M"), "last_login": "",
    }
    _signing_key(data)
    _save(data)
    return {"ok": True, "user": username, "role": role}


def change_password(username, old, new):
    data = _load(force=True)
    u = data.get("users", {}).get(username)
    if not u or not hmac.compare_digest(_hash(old, u["salt"]), u["hash"]):
        raise PermissionError("current password is incorrect")
    if not new or len(new) < 10:
        raise ValueError("new password must be at least 10 characters")
    salt = base64.b64encode(secrets.token_bytes(16)).decode()
    u.update(salt=salt, hash=_hash(new, salt), ver=u.get("ver", 1) + 1)
    _save(data)
    SESSIONS.revoke(username)
    return {"ok": True, "note": "other sessions signed out"}


def delete_user(username, acting_as):
    data = _load(force=True)
    if username not in data.get("users", {}):
        raise ValueError("no such user")
    if len(data["users"]) == 1:
        raise PermissionError("cannot delete the only account")
    if username == acting_as:
        raise PermissionError("cannot delete the account you are signed in as")
    admins = [n for n, v in data["users"].items() if v.get("role", "admin") == "admin"]
    if admins == [username]:
        raise PermissionError("cannot delete the only administrator")
    data["users"].pop(username)
    # Their API keys go with them: a key must never pass to someone given
    # the same name later.
    for kid in [k for k, rec in (data.get("api_keys") or {}).items() if rec.get("owner") == username]:
        data["api_keys"].pop(kid)
    _save(data)
    SESSIONS.revoke(username)
    return {"ok": True}


# ------------------------------------------------------------------ rate limit
def _rate_ok(key, limit=MAX_ATTEMPTS):
    with _rate_lock:
        now = time.time()
        hits = [t for t in _attempts.get(key, []) if now - t < ATTEMPT_WINDOW]
        if hits:
            _attempts[key] = hits
        else:
            _attempts.pop(key, None)
        return len(hits) < limit


def _rate_hit(key):
    with _rate_lock:
        if len(_attempts) >= MAX_TRACKED and key not in _attempts:
            raise PermissionError("too many attempts — wait a few minutes")
        _attempts.setdefault(key, []).append(time.time())


def _reserve_attempt(username, addr, shared=True):
    """Count in-flight attempts before any password work; never evict live limits."""
    keys = (f"ip:{addr}", f"user:{username}")
    with _rate_lock:
        now = time.time()
        for key in list(_attempts):
            hits = [t for t in _attempts[key] if now - t < ATTEMPT_WINDOW]
            if hits:
                _attempts[key] = hits
            else:
                del _attempts[key]
        if (not _rate_ok(keys[0]) or not _rate_ok(keys[1], MAX_USER_ATTEMPTS) or
                len(_attempts) + sum(k not in _attempts for k in keys) > MAX_TRACKED):
            raise PermissionError("too many attempts — wait a few minutes")
        if shared:
            _shared_attempt(keys, now, reserve=True)
        for key in keys:
            _attempts.setdefault(key, []).append(now)
    return keys, now, shared


def _shared_attempt(keys, stamp, reserve):
    """Persist admission before hashing so replicas and restarts share the limit."""
    for attempt in range(4):
        data = copy.deepcopy(_load(force=True, allow_stale=False))
        limits = data.setdefault("login_limits", {})
        now = time.time()
        for key in list(limits):
            hits = [t for t in limits[key] if now - t < ATTEMPT_WINDOW]
            if hits:
                limits[key] = hits
            else:
                del limits[key]
        hashed = [hashlib.sha256(key.encode()).hexdigest() for key in keys]
        if reserve:
            if (len(limits) + sum(k not in limits for k in hashed) > 1000 or
                    len(limits.get(hashed[0], [])) >= MAX_ATTEMPTS or
                    len(limits.get(hashed[1], [])) >= MAX_USER_ATTEMPTS):
                raise PermissionError("too many attempts — wait a few minutes")
            for key in hashed:
                limits.setdefault(key, []).append(stamp)
        else:
            for key in hashed:
                hits = limits.get(key, [])
                if stamp in hits:
                    hits.remove(stamp)
                if not hits:
                    limits.pop(key, None)
        try:
            _save(data)
            return
        except StoreConflict:
            continue
    raise PermissionError("too many attempts — account store is busy; try again")


def _release_attempt(reservation):
    keys, stamp, shared = reservation
    with _rate_lock:
        for key in keys:
            hits = _attempts.get(key, [])
            if stamp in hits:
                hits.remove(stamp)
            if not hits:
                _attempts.pop(key, None)
        try:
            if shared:
                _shared_attempt(keys, stamp, reserve=False)
        except (StoreUnavailable, StoreConflict, PermissionError, urllib.error.HTTPError):
            # Keeping a reservation is conservative; it expires in five minutes.
            pass


def _check_password(username, password, addr, read_only=False):
    reservation = _reserve_attempt(username, addr, shared=not read_only)
    if not _password_slots.acquire(blocking=False):
        _release_attempt(reservation)
        raise PermissionError("too many attempts — wait a few minutes")
    try:
        data = _load(force=True, allow_stale=False)
        u = data.get("users", {}).get(username)
        salt = u["salt"] if u else base64.b64encode(b"\0" * 16).decode()
        calc = _hash(password or "", salt)
        if not u or not hmac.compare_digest(calc, u["hash"]):
            raise PermissionError("incorrect username or password")
        _release_attempt(reservation)
        return data, u
    except StoreUnavailable:
        _release_attempt(reservation)
        raise
    finally:
        _password_slots.release()


def _account_id(data, username, user):
    # Existing records have a unique random password salt. Bind new-format
    # sessions to it without writing during read-only data-move recovery.
    return user.get("id") or hmac.new(data["signing_key"].encode(),
        ("homestead-account-v2:" + username + ":" + user["salt"]).encode(),
        hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ tokens
def _sign(payload_b64, key):
    return base64.urlsafe_b64encode(
        hmac.new(key, payload_b64.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def issue_token(username, remember=False, started=None, identity=None):
    """A signed session. `started` carries the original sign-in time forward
    through every refresh, so the absolute window cannot be extended by use."""
    data = _load()
    u = data["users"][username]
    if identity is not None and identity != (_account_id(data, username, u), u.get("ver", 1)):
        raise PermissionError("the account changed during sign-in; sign in again")
    now = int(time.time())
    payload = {"u": username, "v": u.get("ver", 1), "account": _account_id(data, username, u),
               "format": 2, "r": u.get("role", "admin"),
               "iat": int(started or now), "rem": bool(remember),
               "exp": now + idle_ttl(remember)}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{raw}.{_sign(raw, _signing_key(data))}"


def verify_token(token, force=False):
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    data = _load(force=force, allow_stale=not force)
    if not data.get("signing_key"):
        return None
    if not hmac.compare_digest(sig, _sign(raw, _signing_key(data))):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if (not isinstance(payload, dict) or payload.get("format") != 2 or
            not isinstance(payload.get("u"), str) or
            not isinstance(payload.get("exp"), (int, float)) or
            not isinstance(payload.get("iat"), (int, float))):
        return None
    now = time.time()
    if payload.get("exp", 0) < now:
        return None          # idle too long
    remember = bool(payload.get("rem"))
    started = int(payload.get("iat") or 0)
    # Format-2 tokens always carry their original issue time.
    if started and now - started > ABSOLUTE_TTL:
        return None          # alive too long, however active
    u = data.get("users", {}).get(payload.get("u"))
    if (not u or u.get("ver", 1) != payload.get("v") or
            payload.get("account") != _account_id(data, payload["u"], u)):
        return None          # password changed or user removed
    window = idle_ttl(remember)
    # role is re-read from the store, never trusted from the token, so a
    # demotion takes effect immediately rather than at the next sign-in
    return {"user": payload["u"], "role": u.get("role", "admin"),
            "remember": remember, "started": started or int(now),
            "expires": int(payload["exp"]),
            "stale": (payload["exp"] - now) < window * REFRESH_AFTER}


def login(username, password, addr, remember=False):
    username = (username or "").strip().lower()
    data, u = _check_password(username, password, addr)
    identity = (_account_id(data, username, u), u.get("ver", 1))
    data = copy.deepcopy(_load(force=True, allow_stale=False))
    u = data.get("users", {}).get(username)
    if not u or identity != (_account_id(data, username, u), u.get("ver", 1)):
        raise PermissionError("the account changed during sign-in; sign in again")
    u["last_login"] = time.strftime("%Y-%m-%d %H:%M")
    _save(data)
    return issue_token(username, remember=remember, identity=identity)


def login_read_only(username, password, addr, remember=False):
    """Sign in without writing: while a data move holds every write, an admin
    must still be able to sign in to give the move up. The last sign-in time
    is not recorded; attempts are limited as always (they are kept in memory)."""
    username = (username or "").strip().lower()
    data, u = _check_password(username, password, addr, read_only=True)
    return issue_token(username, remember=remember, identity=(_account_id(data, username, u), u.get("ver", 1)))


def logout_everywhere(username):
    data = _load(force=True)
    u = data.get("users", {}).get(username)
    if u:
        u["ver"] = u.get("ver", 1) + 1
        _save(data)
        SESSIONS.revoke(username)
    return {"ok": True}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/auth/users"): ("admin", lambda request: list_users()),
}
