"""Short-lived deployment acknowledgements bound to the reviewed input.

Tokens contain only an expiry and keyed digest, never credentials/manifests.
The server binds a read-only key provider shared across Homestead replicas.
"""
import hashlib
import hmac
import json
import time

import homestead_storage_journal as JOURNAL

_key = None
TTL = 600
CONTROL_FIELDS = {"capacity_token", "confirm_capacity", "ack_state_initialization", "confirm_state_name",
                  # A rename of an app git applies (#734): an acknowledgement, given after the review.
                  "confirm_managed"}


def bind(key_provider):
    global _key
    _key = key_provider


def review_identity(obj):
    """An object's identity for a review context: namespace, name, uid and
    resourceVersion (the write after the review still sends that), plus
    "settled", a digest of all but its status and the server's bookkeeping.
    The token is signed over settled instead of resourceVersion (review_form),
    so a controller's status write between preview and confirm does not void
    the review (#826's twin); any other change still does."""
    meta = (obj or {}).get("metadata") or {}
    result = {key: meta.get(key) for key in ("namespace", "name", "uid", "resourceVersion")}
    if meta.get("uid"):
        result["settled"] = JOURNAL.settled(obj)
    return result


def review_form(value):
    """What a token is signed over: the context, with each identity that
    carries "settled" (review_identity) less its resourceVersion. Every other
    identity keeps its version: for ConfigMaps, Secrets, claims, disks and the
    KubeVirt CR any write, status included, still needs a fresh review."""
    if isinstance(value, list):
        return [review_form(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: review_form(item) for key, item in value.items()}
    if "settled" in value and "uid" in value:
        out.pop("resourceVersion", None)
    return out


def _signature(config, expires, context=None):
    body = {key: value for key, value in config.items() if key not in CONTROL_FIELDS}
    payload = json.dumps({"config": body, "context": review_form(context)}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(_key(), (str(expires) + ":" + payload).encode(), hashlib.sha256).hexdigest()


def issue(config, context=None):
    expires = int(time.time()) + TTL
    return f"{expires}.{_signature(config, expires, context)}"


def derive_secret(config, purpose):
    """Reproduce internal batch secrets without putting them in preview bodies.

    The config must include a fresh random review ID. Domain separation keeps
    this value distinct from any public approval token.
    """
    return _signature(config, 0, {"internal_secret": purpose})


def valid(config, context=None):
    try:
        expires, signature = str(config.get("capacity_token") or "").split(".", 1)
        expires = int(expires)
        now = time.time()
        return now <= expires <= now + TTL and hmac.compare_digest(signature, _signature(config, expires, context))
    except (TypeError, ValueError):
        return False


class Rejected(ValueError):
    def __init__(self, message, plan):
        super().__init__(message)
        self.plan = plan


def enforce(config, plan, context=None):
    if plan.get("blocked"):
        raise Rejected("; ".join(plan["device_conflicts"]) if plan.get("device_conflicts") else
                       "Deployment cannot fit the checked placement constraints. Review capacity before deploying.", plan)
    if plan.get("requires_confirmation") and (config.get("confirm_capacity") is not True or not valid(config, context)):
        raise Rejected("Placement or memory needs a fresh review and explicit acknowledgement before deploying.", plan)
    state = (plan.get("vm") or {}).get("state_initialization")
    if state and (config.get("ack_state_initialization") is not True or
                  config.get("confirm_state_name") != state.get("name") or not valid(config, context)):
        raise Rejected("Initializing missing VM state requires its own acknowledgement and the exact VM name. Recover the original state volume if needed.", plan)
