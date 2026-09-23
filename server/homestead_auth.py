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
import homestead_names as NAMES
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error

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


def bind(_kget, _ksend, _ns):
    global kget, ksend, NS
    kget, ksend, NS = _kget, _ksend, _ns
    NAMES.bind(_kget)


# ------------------------------------------------------------------ store
def _load(force=False):
    if not force and _store_cache["data"] is not None and time.time() - _store_cache["at"] < 10:
        return _store_cache["data"]
    try:
        sec = kget(f"/api/v1/namespaces/{NS}/secrets/{SECRET_NAME()}")
        raw = base64.b64decode(sec.get("data", {}).get("store.json", "") or "e30=")
        data = json.loads(raw.decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        data = {}
    except Exception:
        data = {}
    data.setdefault("users", {})
    data.setdefault("signing_key", "")
    _store_cache.update(at=time.time(), data=data)
    return data


def _save(data):
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET_NAME(), "namespace": NS},
            "data": {"store.json": base64.b64encode(json.dumps(data).encode()).decode()}}
    try:
        kget(f"/api/v1/namespaces/{NS}/secrets/{SECRET_NAME()}")
        ksend("PUT", f"/api/v1/namespaces/{NS}/secrets/{SECRET_NAME()}", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{NS}/secrets", body)
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


def needs_setup():
    return not _load(force=True).get("users")


def user_count():
    return len(_load().get("users", {}))


def list_users():
    return [{"name": u, "role": v.get("role", "admin"), "created": v.get("created", ""),
             "last_login": v.get("last_login", "")}
            for u, v in sorted(_load().get("users", {}).items())]


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
    _save(data)
    return {"ok": True}


# ------------------------------------------------------------------ rate limit
def _rate_ok(key, limit=MAX_ATTEMPTS):
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < ATTEMPT_WINDOW]
    if hits:
        _attempts[key] = hits
    else:
        _attempts.pop(key, None)
    return len(hits) < limit


def _rate_hit(key):
    if len(_attempts) > MAX_TRACKED:
        # Many addresses at once is an attack in itself; forget the oldest.
        for stale in sorted(_attempts, key=lambda k: _attempts[k][-1] if _attempts[k] else 0)[:MAX_TRACKED // 2]:
            _attempts.pop(stale, None)
    _attempts.setdefault(key, []).append(time.time())


# ------------------------------------------------------------------ tokens
def _sign(payload_b64, key):
    return base64.urlsafe_b64encode(
        hmac.new(key, payload_b64.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def issue_token(username, remember=False, started=None):
    """A signed session. `started` carries the original sign-in time forward
    through every refresh, so the absolute window cannot be extended by use."""
    data = _load()
    u = data["users"][username]
    now = int(time.time())
    payload = {"u": username, "v": u.get("ver", 1), "r": u.get("role", "admin"),
               "iat": int(started or now), "rem": bool(remember),
               "exp": now + idle_ttl(remember)}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{raw}.{_sign(raw, _signing_key(data))}"


def verify_token(token):
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    data = _load()
    if not data.get("signing_key"):
        return None
    if not hmac.compare_digest(sig, _sign(raw, _signing_key(data))):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    now = time.time()
    if payload.get("exp", 0) < now:
        return None          # idle too long
    remember = bool(payload.get("rem"))
    started = int(payload.get("iat") or 0)
    # A session from before this field existed is treated as starting now: it
    # still expires, just on the idle clock it was issued with.
    if started and now - started > ABSOLUTE_TTL:
        return None          # alive too long, however active
    u = data.get("users", {}).get(payload.get("u"))
    if not u or u.get("ver", 1) != payload.get("v"):
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
    if not _rate_ok(f"ip:{addr}") or not _rate_ok(f"user:{username}", MAX_USER_ATTEMPTS):
        raise PermissionError("too many attempts — wait a few minutes")
    data = _load(force=True)
    u = data.get("users", {}).get(username)
    # do the work either way so a missing user is not faster than a wrong password
    salt = u["salt"] if u else base64.b64encode(b"\0" * 16).decode()
    calc = _hash(password or "", salt)
    if not u or not hmac.compare_digest(calc, u["hash"]):
        _rate_hit(f"ip:{addr}")
        _rate_hit(f"user:{username}")
        raise PermissionError("incorrect username or password")
    u["last_login"] = time.strftime("%Y-%m-%d %H:%M")
    _save(data)
    return issue_token(username, remember=remember)


def logout_everywhere(username):
    data = _load(force=True)
    u = data.get("users", {}).get(username)
    if u:
        u["ver"] = u.get("ver", 1) + 1
        _save(data)
    return {"ok": True}
