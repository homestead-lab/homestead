"""Web Push to installed Homestead apps, without a crypto package.

A push here carries no payload: it only wakes the app's service worker, which
then asks Homestead - over its own signed-in HTTPS connection - what the alert
was. Payloads would have to be encrypted (ECDH and AES-GCM), which the
standard library cannot do; an empty push needs only the VAPID signature that
proves which server sent it, and that is ECDSA, which homestead_ecdsa does.

Subscriptions are per device, each with the kinds of alert that device wants.
A subscription's endpoint is a URL Homestead will POST to, so only the push
services browsers actually use are accepted: anything else would let a signed-in
user point Homestead at an address inside the network.
"""
import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import homestead_ecdsa as EC

DATA_DIR = "/data"
CONTACT = "https://github.com/wjcloudy/homestead"
_lock = threading.Lock()
_key_cache = {}

# The push services browsers subscribe to. Chrome and Edge on Android use FCM,
# Firefox autopush, Safari Apple's service, Edge on Windows WNS.
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com",
              "notify.windows.com")
CATEGORIES = {
    "outage": "Outages: a node down, a volume faulted, a disk failing",
    "degraded": "Degraded: a replica rebuilding, a workload not ready",
    "jobs": "Failed jobs in Activity",
    "joins": "Hosts joining the cluster",
    "updates": "Image updates available",
}
DEFAULT_CATEGORIES = ["outage", "degraded", "jobs", "joins"]


def bind(data_dir, contact=""):
    global DATA_DIR, CONTACT
    DATA_DIR = data_dir
    if contact:
        CONTACT = contact


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# ------------------------------------------------------------------ keys
def _key_path():
    return os.path.join(DATA_DIR, "vapid.json")


def private_key():
    """This server's VAPID key, made once and kept with Homestead's data."""
    with _lock:
        if "private" in _key_cache:
            return _key_cache["private"]
        try:
            with open(_key_path(), encoding="utf-8") as handle:
                private = int(json.load(handle)["private"], 16)
        except (OSError, ValueError, KeyError):
            private = EC.new_private_key()
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = _key_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"private": f"{private:064x}", "created": int(time.time())}, handle)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, _key_path())
        _key_cache["private"] = private
        _key_cache["public"] = EC.public_bytes(EC.public_key(private))
        return private


def public_key():
    """The application server key browsers subscribe with, base64url."""
    private_key()
    return _b64(_key_cache["public"])


def vapid_authorization(endpoint, now=None):
    """The Authorization header a push service checks: a short ES256 token."""
    parsed = urllib.parse.urlparse(endpoint)
    header = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"aud": f"{parsed.scheme}://{parsed.netloc}",
                              "exp": int((now or time.time()) + 12 * 3600), "sub": CONTACT},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    signature = EC.signature_bytes(EC.sign(private_key(), signing_input))
    return f"vapid t={header}.{claims}.{_b64(signature)}, k={public_key()}"


# ------------------------------------------------------------ subscriptions
def _subs_path():
    return os.path.join(DATA_DIR, "push.json")


def _read():
    try:
        with open(_subs_path(), encoding="utf-8") as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _write(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _subs_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, _subs_path())


def endpoint_allowed(endpoint):
    try:
        parsed = urllib.parse.urlparse(str(endpoint or ""))
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    return len(endpoint) <= 1024 and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def tag(endpoint):
    """A short name for one device, safe to keep beside an alert meant only for it."""
    return hashlib.sha256(str(endpoint).encode()).hexdigest()[:16]


def subscribe(user, subscription, categories=None, device="", replaces="", cursor=0):
    """Remembers a device. A browser swapping its subscription keeps its choices."""
    endpoint = str((subscription or {}).get("endpoint") or "")
    if not endpoint_allowed(endpoint):
        raise ValueError("that is not a push service this browser would use")
    with _lock:
        rows = _read()
        old = next((r for r in rows if r["endpoint"] in (endpoint, replaces) and r["user"] == user), None)
        if categories is None:
            categories = (old or {}).get("categories", DEFAULT_CATEGORIES)
        wanted = [c for c in CATEGORIES if c in categories]
        # One browser, one subscription: whoever signed in there last owns it.
        rows = [r for r in rows if r["endpoint"] != endpoint and
                not (replaces and r["endpoint"] == replaces and r["user"] == user)]
        rows.append({"user": user, "endpoint": endpoint, "categories": wanted,
                     "device": str(device or (old or {}).get("device", ""))[:120],
                     "created": (old or {}).get("created", int(time.time())),
                     "cursor": (old or {}).get("cursor", cursor), "failures": 0,
                     "last_ok": (old or {}).get("last_ok", 0)})
        _write(rows[-200:])
    return {"ok": True, "categories": wanted}


def unsubscribe(user, endpoint):
    with _lock:
        rows = _read()
        kept = [r for r in rows if not (r["endpoint"] == endpoint and r["user"] == user)]
        _write(kept)
    return {"ok": True, "removed": len(rows) - len(kept)}


def mine(user, endpoint):
    return next((r for r in _read() if r["user"] == user and r["endpoint"] == endpoint), None)


def devices(user):
    return [{"device": r["device"], "categories": r["categories"], "created": r["created"],
             "last_ok": r.get("last_ok", 0), "failures": r.get("failures", 0), "tag": tag(r["endpoint"])}
            for r in _read() if r["user"] == user]


def wanted_by(categories):
    """Whether any device at all asks for one of these kinds of alert."""
    return any(set(r["categories"]) & set(categories) for r in _read())


def advance(user, endpoint, cursor):
    """Remembers what a device has been shown, so the next push asks from there."""
    with _lock:
        rows = _read()
        for r in rows:
            if r["user"] == user and r["endpoint"] == endpoint:
                r["cursor"] = max(r.get("cursor", 0), int(cursor))
        _write(rows)


# ------------------------------------------------------------------ send
def _post(endpoint, urgency):
    request = urllib.request.Request(endpoint, data=b"", method="POST", headers={
        "Authorization": vapid_authorization(endpoint), "TTL": "86400", "Urgency": urgency,
        "Content-Length": "0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def send(wants, urgency="normal", poster=None):
    """Pushes to every device whose categories `wants` accepts; drops dead ones."""
    poster = poster or _post
    sent = gone = 0
    statuses = []
    for row in _read():
        if not wants(row):
            continue
        try:
            status = poster(row["endpoint"], urgency)
        except Exception:
            status = 0
        statuses.append(status)
        with _lock:
            rows = _read()
            for r in rows:
                if r["endpoint"] != row["endpoint"]:
                    continue
                if status in (200, 201, 202, 204):
                    r.update(failures=0, last_ok=int(time.time()))
                    sent += 1
                else:
                    r["failures"] = r.get("failures", 0) + 1
            if status in (404, 410):
                # The browser has let this subscription go.
                rows = [r for r in rows if r["endpoint"] != row["endpoint"]]
                gone += 1
            _write(rows)
    return {"sent": sent, "removed": gone, "statuses": statuses}
