"""Domain-separated authentication for the optional allocation collector.

Its independent Secret is not an account/session signing key. A response MAC
binds the exact bytes to the request nonce and the intended node and Pod UID.
This authenticates traffic on the trusted cluster network; it is not encryption.
"""
import hashlib
import hmac
import json
import re
import secrets
import time

PURPOSE = b"homestead.allocation.v1\x00"
MAX_BODY = 4 * 1024**2


def key_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Allocation authentication key is unavailable")
    return bytes.fromhex(value)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def request(node, pod_uid, *, now=None):
    return {"node": node, "pod_uid": pod_uid, "nonce": secrets.token_hex(24),
            "time": int(time.time() if now is None else now)}


def validate(value, node, pod_uid, *, now=None):
    if not isinstance(value, dict) or set(value) != {"node", "pod_uid", "nonce", "time"}:
        return False
    return (bool(node and pod_uid) and value["node"] == node and value["pod_uid"] == pod_uid
            and type(value["time"]) is int and abs((time.time() if now is None else now) - value["time"]) <= 30
            and isinstance(value["nonce"], str) and bool(re.fullmatch(r"[0-9a-f]{48}", value["nonce"])))


def signature(key, body, *, reply_to=None):
    prefix = PURPOSE + (b"request\x00" if reply_to is None else b"response\x00" + hashlib.sha256(reply_to).digest())
    return hmac.new(key_bytes(key), prefix + body, hashlib.sha256).hexdigest()


def verify(key, body, supplied, *, reply_to=None):
    try:
        return isinstance(supplied, str) and hmac.compare_digest(signature(key, body, reply_to=reply_to), supplied)
    except (ValueError, TypeError):
        return False
