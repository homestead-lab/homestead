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
* A key reaches the MCP server (/mcp, homestead_mcp) only when it was made
  with mcp allowed, and only while an admin has it switched on; there it
  is held to the same scopes.

When a key was last used, and from where, is kept on the data volume rather
than in the Secret: a key in use must not rewrite the accounts, nor race a
change to them.
"""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time

import homestead_auth as AUTH
import homestead_client_address as CLIENTADDR
import homestead_shared as SHARED

PREFIX = "hsk_"
TOKEN = re.compile(r"hsk_([0-9a-f]{12})_([A-Za-z0-9_-]{43})")
# What each scope allows, and the role its maker must still hold for it.
SCOPES = {
    "read": ("viewer", "Read status: the cluster, nodes, containers, VMs, volumes, alerts and jobs"),
    "logs:read": ("viewer", "Read containers' logs, which can hold passwords and tokens apps print"),
    "containers:control": ("operator", "Start, stop and restart containers"),
    "containers:manage": ("operator", "Deploy, edit, scale and move containers (MCP); deleting one waits for a person"),
    "vms:control": ("operator", "Start, stop, restart, force off and reset virtual machines"),
    "vms:manage": ("operator", "Create, edit and migrate virtual machines (MCP); deleting one waits for a person"),
    "storage:manage": ("operator", "Create and resize volumes, snapshot and back them up (MCP); deleting, "
                                   "reverting or restoring waits for a person"),
    "network:manage": ("operator", "Expose apps and keep the IP address plan (MCP)"),
    "cluster:manage": ("admin", "Namespaces and raw Kubernetes objects (MCP); every change waits for an admin to approve it"),
}
# A scope brings the ones it builds on: managing an area includes
# controlling it, and every scope includes reading. Applied when a key is
# used, so keys made before keep working with what they now include.
IMPLIES = {
    "logs:read": ("read",),
    "containers:control": ("read",),
    "containers:manage": ("containers:control", "read"),
    "vms:control": ("read",),
    "vms:manage": ("vms:control", "read"),
    "storage:manage": ("read",),
    "network:manage": ("read",),
    "cluster:manage": ("read",),
}
# The key dialog's presets: ticks for the boxes below them.
PRESETS = {
    "Read only": ["read"],
    "Operate": ["read", "containers:control", "vms:control"],
    "Manage everything": ["read", "logs:read", "containers:manage", "vms:manage", "storage:manage", "network:manage"],
}
MIN_TTL = 3600
MAX_TTL = 366 * 86400
MAX_KEYS = 50
MAX_NETWORKS = 10
FAILURES_ALLOWED = 20          # wrong keys per address in AUTH.ATTEMPT_WINDOW
KEEP_EXPIRED = 30 * 86400      # an expired key is listed this long, then dropped
USAGE_EVERY = 60               # seconds between writes of one key's last use
DATA_DIR = "/data"
_usage_lock = SHARED.SharedLock("api-key-usage", directory=lambda: DATA_DIR)
_usage_written = {}


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _usage_path():
    return os.path.join(DATA_DIR, "api-key-usage.json")


def _usage():
    return SHARED.read_json(_usage_path(), {})


def _note_use(kid, addr, now):
    if now - _usage_written.get(kid, 0) < USAGE_EVERY:
        return
    _usage_written[kid] = now
    try:
        SHARED.update_file(_usage_path(), lambda usage: usage.update({kid: {"at": int(now), "ip": str(addr or "")[:64]}}),
                           _usage_lock, {})
    except (SHARED.Unreadable, OSError, TypeError):
        pass                    # a key still works; its last use is not saved over what could not be read


def _digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _name(value):
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= 60 or not name.isprintable():
        raise ValueError("give the key a name of up to 60 characters, such as Home Assistant")
    return name


def effective(scopes):
    """The scopes chosen, with the ones each includes (IMPLIES), in SCOPES' order."""
    found = set()
    for scope in scopes:
        found.add(scope)
        found.update(IMPLIES.get(scope, ()))
    return [s for s in SCOPES if s in found]


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
            "mcp": bool(rec.get("mcp")),
            "networks": list(rec.get("networks") or []), "created": rec["created"], "expires": rec["expires"],
            "expired": rec["expires"] <= now, "last_used": used.get("at"), "last_ip": used.get("ip", "")}


def create(name, scopes, ttl_seconds, networks, owner, mcp=False):
    """A new key, and its token - the only time the token exists outside the
    caller. mcp: whether it may also be used on the MCP server."""
    if not isinstance(mcp, bool):
        raise ValueError("say whether the key may be used by AI agents over MCP")
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
        raise ValueError(f"At most {MAX_KEYS} keys are allowed. Revoke one first.")
    if any(rec.get("name") == name for rec in keys.values()):
        raise ValueError(f"there is already a key named {name}")
    kid = secrets.token_hex(6)
    while kid in keys:
        kid = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    keys[kid] = {"name": name, "owner": owner, "scopes": scopes, "networks": networks,
                 "created": now, "expires": now + ttl, "hash": _digest(secret), "mcp": mcp}
    AUTH._save(data)
    return {"token": f"{PREFIX}{kid}_{secret}", "key": public(kid, keys[kid], now=now)}


def edit(kid, changes, editor):
    """A key's name, scopes, networks, MCP use or expiry changed. Its token
    stays the same, so whatever uses it carries on; a replica sees the change
    within FRESH_FOR. ttl_seconds sets a new expiry counted from now."""
    if AUTH.role_of(editor) != "admin":
        raise PermissionError("only an administrator can change API keys")
    changes = changes or {}
    data = AUTH._load(force=True)
    keys = data.get("api_keys") or {}
    rec = keys.get(str(kid or ""))
    if not rec:
        raise ValueError("no such key")
    new, said = dict(rec), []
    if "name" in changes:
        name = _name(changes["name"])
        if name != rec["name"] and any(r.get("name") == name for k, r in keys.items() if k != kid):
            raise ValueError(f"there is already a key named {name}")
        new["name"] = name
    if "scopes" in changes:
        new["scopes"] = _scopes(changes["scopes"])
    if "networks" in changes:
        new["networks"] = _networks(changes["networks"])
    if "mcp" in changes:
        if not isinstance(changes["mcp"], bool):
            raise ValueError("say whether the key may be used by AI agents over MCP")
        new["mcp"] = changes["mcp"]
    if changes.get("ttl_seconds") not in (None, ""):
        try:
            ttl = int(changes["ttl_seconds"])
        except (TypeError, ValueError):
            raise ValueError("choose how long the key lasts") from None
        if not MIN_TTL <= ttl <= MAX_TTL:
            raise ValueError("a key lasts between an hour and a year")
        new["expires"] = int(time.time()) + ttl
    for field, label in (("name", "name"), ("scopes", "scopes"), ("networks", "networks"), ("mcp", "MCP"),
                         ("expires", "expiry")):
        if new.get(field) != rec.get(field):
            said.append(label)
    keys[kid] = new
    AUTH._save(data)
    return {"key": public(kid, new), "changed": said}


def manage(action, body, user):
    """The app's three key routes, as server.py hands them over: (answer,
    sign-in history event, its detail)."""
    body = body or {}
    if action == "keys":
        made = create(body.get("name"), body.get("scopes"), body.get("ttl_seconds"), body.get("networks"), user,
                      body.get("mcp", False))
        return {"ok": True, **made}, "key-added", f"{made['key']['name']}: {', '.join(made['key']['scopes'])}"
    if action == "revoke":
        done = revoke(body.get("id"))
        return done, "key-revoked", done["name"]
    done = edit(body.get("id"), {k: body[k] for k in ("name", "scopes", "networks", "mcp", "ttl_seconds") if k in body}, user)
    return ({"ok": True, **done}, "key-changed",
            f"{done['key']['name']}: {', '.join(done['changed']) or 'nothing'} changed")


def list_keys():
    try:
        usage = _usage()
    except SHARED.Unreadable:
        usage = {}              # the keys are listed; when each was last used is not known
    now = time.time()
    keys = AUTH._load().get("api_keys") or {}
    out = sorted((public(kid, rec, usage, now) for kid, rec in keys.items()), key=lambda k: -k["created"])
    for key in out:
        # Used from a node's address: its clients cannot be told apart (#420).
        key["last_ip_node"] = bool(key["last_ip"]) and CLIENTADDR.is_node_address(key["last_ip"])
    return out


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


def verify(token, addr, now=None, force=False):
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
            message = "too many wrong API keys from this address. Wait a few minutes."
        AUTH._rate_hit(rate_key)
        raise PermissionError(message)

    match = TOKEN.fullmatch(str(token or "").strip())
    kid, secret = (match.group(1), match.group(2)) if match else ("", "")
    data = AUTH._load(max_age=FRESH_FOR, force=force, allow_stale=not force)
    rec = (data.get("api_keys") or {}).get(kid) if kid else None
    # The digest is worked out whether or not the key exists, so a wrong id
    # and a wrong secret take the same time.
    supplied = _digest(secret or "x")
    if not rec or not hmac.compare_digest(supplied, rec.get("hash", "")):
        refuse("that API key is not valid")
    if rec["expires"] <= now:
        refuse(f"the API key {rec['name']} has expired")
    if not _allowed_from(rec, addr):
        refuse(f"the API key {rec['name']} cannot be used from {addr}" + (f". {CLIENTADDR.note(addr)}" if CLIENTADDR.note(addr) else ""))
    owner = (data.get("users") or {}).get(rec["owner"])
    if not owner:
        refuse(f"the API key {rec['name']} belonged to an account that no longer exists")
    scopes = [s for s in effective(rec["scopes"]) if AUTH.allows(owner.get("role", "viewer"), SCOPES[s][0])]
    if not scopes:
        refuse(f"the API key {rec['name']} has no scope its maker's role still allows")
    _note_use(kid, addr, now)
    return {"id": kid, "name": rec["name"], "owner": rec["owner"], "scopes": scopes, "expires": rec["expires"],
            "mcp": bool(rec.get("mcp"))}


def scopes_for_role(role):
    """A person signed in to the web app reaches /api/v1 with their role's scopes."""
    return [s for s, (need, _) in SCOPES.items() if AUTH.allows(role, need)]


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/auth/keys"): ("admin", lambda request: {"keys": list_keys(), "scopes": {s: text for s, (_, text) in SCOPES.items()}, "min_ttl": MIN_TTL, "max_ttl": MAX_TTL,
                                                         "implies": {s: list(v) for s, v in IMPLIES.items()}, "presets": PRESETS,
                                                         "you": CLIENTADDR.about(request.addr)}),
}
