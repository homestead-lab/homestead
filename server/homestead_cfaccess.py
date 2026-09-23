"""Checking Cloudflare Access's signature on requests that came through a tunnel.

Cloudflare Access sits in front of a published hostname and signs every request
it lets through with a JWT (the Cf-Access-Jwt-Assertion header). Checking that
signature here means a mistake in the Access policy - a bypass rule, the wrong
hostname, the application removed - cannot quietly publish Homestead: a request
that reached it through Cloudflare without Access's signature is refused.

Requests from the LAN never pass through Cloudflare and are not affected.

The tokens are RS256. Verifying one needs only SHA-256 and modular
exponentiation, which the standard library has, so no crypto package is added.
"""
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request

TEAM = ""                 # https://<team>.cloudflareaccess.com
AUDIENCES = set()         # the Access application's AUD tag(s)
CERT_TTL = 3600
_certs = {"at": 0.0, "keys": {}, "tried": 0.0}
_lock = threading.Lock()
# DER prefix of a SHA-256 DigestInfo, as PKCS#1 v1.5 signatures carry it.
_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def configure(team, audience):
    global TEAM, AUDIENCES
    team = (team or "").strip().rstrip("/")
    if team and not team.startswith("https://"):
        team = "https://" + team
    TEAM = team
    AUDIENCES = {a.strip() for a in (audience or "").split(",") if a.strip()}


def enabled():
    return bool(TEAM and AUDIENCES)


def _b64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _int(text):
    return int.from_bytes(_b64(text), "big")


def _fetch_keys():
    request = urllib.request.Request(f"{TEAM}/cdn-cgi/access/certs", headers={"User-Agent": "Homestead"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.loads(response.read().decode())
    return {k["kid"]: (_int(k["n"]), _int(k["e"])) for k in body.get("keys", [])
            if k.get("kty") == "RSA" and k.get("kid")}


def _key(kid):
    """Cloudflare's current signing keys, cached; refreshed when a new one appears."""
    now = time.time()
    with _lock:
        stale = now - _certs["at"] > CERT_TTL
        unknown = kid not in _certs["keys"] and now - _certs["tried"] > 60
        if stale or unknown:
            _certs["tried"] = now
            try:
                _certs.update(keys=_fetch_keys(), at=now)
            except Exception:
                if not _certs["keys"]:
                    raise ValueError("could not fetch Cloudflare Access signing keys")
        return _certs["keys"].get(kid)


def rs256_valid(signing_input, signature, n, e):
    """A PKCS#1 v1.5 SHA-256 signature check, in constant time at the comparison."""
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    digest = hashlib.sha256(signing_input).digest()
    padding = size - 3 - len(_SHA256_INFO) - len(digest)
    if padding < 8:
        return False
    recovered = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + _SHA256_INFO + digest
    return hmac.compare_digest(recovered, expected)


def verify(token, key_for=None, now=None):
    """The token's claims if Access signed it for this application, else ValueError."""
    if not token or token.count(".") != 2:
        raise ValueError("no Cloudflare Access token")
    head, body, sig = token.split(".")
    try:
        header, claims = json.loads(_b64(head)), json.loads(_b64(body))
        signature = _b64(sig)
    except Exception:
        raise ValueError("the Cloudflare Access token is not readable")
    if header.get("alg") != "RS256":
        raise ValueError("the Cloudflare Access token is not RS256")
    key = (key_for or _key)(header.get("kid", ""))
    if not key or not rs256_valid(f"{head}.{body}".encode(), signature, *key):
        raise ValueError("the Cloudflare Access token's signature is not valid")
    now = now or time.time()
    if claims.get("exp", 0) < now - 30:
        raise ValueError("the Cloudflare Access token has expired")
    if claims.get("nbf", 0) > now + 30:
        raise ValueError("the Cloudflare Access token is not valid yet")
    if claims.get("iss", "").rstrip("/") != TEAM:
        raise ValueError("the Cloudflare Access token is from another team")
    audience = claims.get("aud")
    audience = set(audience if isinstance(audience, list) else [audience])
    if not audience & AUDIENCES:
        raise ValueError("the Cloudflare Access token is for another application")
    return claims
