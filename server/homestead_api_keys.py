"""API keys: bearer tokens for scripts, Home Assistant and AI agents.

A key is for /api/v1 alone - the stable, documented API - never the routes
the web app uses, so no key can manage users or keys, open a shell on a host,
read a secret or change a setting, whatever it was given. Within /api/v1 it
can do what its scopes say and no more:

* Every key expires - an hour to a year after it is made; none lives forever.
* A key may be held to networks it can be used from (Home Assistant's
  address, say); a key copied elsewhere is then refused.
* A key acts for the admin who made it, and never beyond them: a key whose
  maker is removed stops working, and one whose maker is demoted keeps only
  the scopes their role still allows.
* The token is shown once. Only a SHA-256 hash of its secret is kept, beside
  the accounts in the same Secret - the secret is 256 random bits, so the
  hash alone is no help to anyone who reads it. Revoking deletes the record.
* Refused keys are counted per address, as wrong passwords are; past twenty
  a wrong key is answered "too many" for a while - a right one never is.

When a key was last used, and from where, is kept on the data volume rather
than in the Secret: a key in use must not rewrite the accounts, nor race a
change to them.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time

import homestead_auth as AUTH
import homestead_shared as SHARED

PREFIX = "hsk_"
TOKEN = re.compile(r"hsk_([0-9a-f]{12})_([A-Za-z0-9_-]{43})")
# What each scope allows, and the role its maker must still hold for it.
SCOPES = {
    "read": ("viewer", "Read status: the cluster, nodes, containers, VMs, alerts and jobs"),
    "containers:control": ("operator", "Start, stop and restart containers"),
    "vms:control": ("operator", "Start, stop and restart virtual machines"),
}
MIN_TTL = 3600
MAX_TTL = 366 * 86400
MAX_KEYS = 50
MAX_NETWORKS = 10
FAILURES_ALLOWED = 20          # wrong keys per address in AUTH.ATTEMPT_WINDOW
KEEP_EXPIRED = 30 * 86400      # an expired key is listed this long, then dropped
USAGE_EVERY = 60               # seconds between writes of one key's last use
DATA_DIR = "/data"
_usage_lock = threading.Lock()
_usage_written = {}


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _usage_path():
    return os.path.join(DATA_DIR, "api-key-usage.json")


def _usage():
    try:
        with open(_usage_path(), encoding="utf-8") as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def _note_use(kid, addr, now):
    if now - _usage_written.get(kid, 0) < USAGE_EVERY:
        return
    with _usage_lock:
        _usage_written[kid] = now
        usage = _usage()
        usage[kid] = {"at": int(now), "ip": str(addr or "")[:64]}
        try:
            SHARED.write_json(_usage_path(), usage)
        except (OSError, TypeError):
            pass


def _digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _name(value):
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= 60 or not name.isprintable():
        raise ValueError("give the key a name of up to 60 characters, such as Home Assistant")
    return name


def _scopes(values):
    scopes = sorted({str(s) for s in (values or [])})
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown or not scopes:
        raise ValueError("choose at least one scope: " + ", ".join(SCOPES))
    return scopes


def _networks(values):
    out = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            out.append(str(ipaddress.ip_network(text, strict=False)))
        except ValueError:
            raise ValueError(f"{text} is not an address or network, like 192.0.2.20 or 192.0.2.0/24") from None
    if len(out) > MAX_NETWORKS:
        raise ValueError(f"at most {MAX_NETWORKS} networks")
    return sorted(set(out))


def public(kid, rec, usage=None, now=None):
    """What may be shown of a key: never its hash."""
    now = now or time.time()
    used = (usage or {}).get(kid) or {}
    return {"id": kid, "name": rec["name"], "owner": rec["owner"], "scopes": list(rec["scopes"]),
            "networks": list(rec.get("networks") or []), "created": rec["created"], "expires": rec["expires"],
            "expired": rec["expires"] <= now, "last_used": used.get("at"), "last_ip": used.get("ip", "")}


def create(name, scopes, ttl_seconds, networks, owner):
    """A new key, and its token - the only time the token exists outside the caller."""
    name, scopes, networks = _name(name), _scopes(scopes), _networks(networks)
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("choose how long the key lasts") from None
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise ValueError("a key lasts between an hour and a year")
    if AUTH.role_of(owner) != "admin":
        raise PermissionError("only an administrator can make API keys")
    now = int(time.time())
    data = AUTH._load(force=True)
    keys = data.setdefault("api_keys", {})
    for kid in [k for k, rec in keys.items() if rec.get("expires", 0) + KEEP_EXPIRED < now]:
        keys.pop(kid)
    if len(keys) >= MAX_KEYS:
        raise ValueError(f"at most {MAX_KEYS} keys; revoke one first")
    if any(rec.get("name") == name for rec in keys.values()):
        raise ValueError(f"there is already a key named {name}")
    kid = secrets.token_hex(6)
    while kid in keys:
        kid = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    keys[kid] = {"name": name, "owner": owner, "scopes": scopes, "networks": networks,
                 "created": now, "expires": now + ttl, "hash": _digest(secret)}
    AUTH._save(data)
    return {"token": f"{PREFIX}{kid}_{secret}", "key": public(kid, keys[kid], now=now)}


def list_keys():
    usage, now = _usage(), time.time()
    keys = AUTH._load().get("api_keys") or {}
    return sorted((public(kid, rec, usage, now) for kid, rec in keys.items()), key=lambda k: -k["created"])


def revoke(kid):
    data = AUTH._load(force=True)
    rec = (data.get("api_keys") or {}).pop(str(kid or ""), None)
    if not rec:
        raise ValueError("no such key")
    AUTH._save(data)
    return {"ok": True, "name": rec["name"]}


def _allowed_from(rec, addr):
    networks = rec.get("networks") or []
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(str(addr or "").strip())
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(net, strict=False) for net in networks)


# A key's record is read at most this old, so a key revoked on another
# replica stops within seconds there too.
FRESH_FOR = 2


def verify(token, addr, now=None):
    """The key a bearer token is, as {id, name, owner, scopes, expires}.
    Raises PermissionError with what to tell the caller.

    Every refusal counts against the address, and past FAILURES_ALLOWED a
    wrong key is answered "too many" - but a right one is never held back:
    behind the cluster's load balancer many clients can share one address,
    and one misbehaving client must not lock Home Assistant out."""
    now = now or time.time()
    rate_key = f"api-key:{addr}"

    def refuse(message):
        if not AUTH._rate_ok(rate_key, FAILURES_ALLOWED):
            message = "too many wrong API keys from this address; wait a few minutes"
        AUTH._rate_hit(rate_key)
        raise PermissionError(message)

    match = TOKEN.fullmatch(str(token or "").strip())
    kid, secret = (match.group(1), match.group(2)) if match else ("", "")
    data = AUTH._load(max_age=FRESH_FOR)
    rec = (data.get("api_keys") or {}).get(kid) if kid else None
    # The digest is worked out whether or not the key exists, so a wrong id
    # and a wrong secret take the same time.
    supplied = _digest(secret or "x")
    if not rec or not hmac.compare_digest(supplied, rec.get("hash", "")):
        refuse("that API key is not valid")
    if rec["expires"] <= now:
        refuse(f"the API key {rec['name']} has expired")
    if not _allowed_from(rec, addr):
        refuse(f"the API key {rec['name']} cannot be used from {addr}")
    owner = (data.get("users") or {}).get(rec["owner"])
    if not owner:
        refuse(f"the API key {rec['name']} belonged to an account that no longer exists")
    scopes = [s for s in rec["scopes"] if s in SCOPES and AUTH.allows(owner.get("role", "viewer"), SCOPES[s][0])]
    if not scopes:
        refuse(f"the API key {rec['name']} has no scope its maker's role still allows")
    _note_use(kid, addr, now)
    return {"id": kid, "name": rec["name"], "owner": rec["owner"], "scopes": scopes, "expires": rec["expires"]}


def scopes_for_role(role):
    """A person signed in to the web app reaches /api/v1 with their role's scopes."""
    return [s for s, (need, _) in SCOPES.items() if AUTH.allows(role, need)]
