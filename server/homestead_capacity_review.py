"""Short-lived deployment acknowledgements bound to the reviewed input.

Tokens contain only an expiry and keyed digest, never credentials/manifests.
The server binds a read-only key provider shared across Homestead replicas.
"""
import hashlib
import hmac
import json
import time

_key = None
TTL = 600
CONTROL_FIELDS = {"capacity_token", "confirm_capacity"}


def bind(key_provider):
    global _key
    _key = key_provider


def _signature(config, expires):
    body = {key: value for key, value in config.items() if key not in CONTROL_FIELDS}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(_key(), (str(expires) + ":" + payload).encode(), hashlib.sha256).hexdigest()


def issue(config):
    expires = int(time.time()) + TTL
    return f"{expires}.{_signature(config, expires)}"


def valid(config):
    try:
        expires, signature = str(config.get("capacity_token") or "").split(".", 1)
        expires = int(expires)
        now = time.time()
        return now <= expires <= now + TTL and hmac.compare_digest(signature, _signature(config, expires))
    except (TypeError, ValueError):
        return False


class Rejected(ValueError):
    def __init__(self, message, plan):
        super().__init__(message)
        self.plan = plan


def enforce(config, plan):
    if plan.get("blocked"):
        raise Rejected("Deployment cannot fit the checked placement constraints. Review capacity before deploying.", plan)
    if plan.get("requires_confirmation") and (config.get("confirm_capacity") is not True or not valid(config)):
        raise Rejected("Placement or memory needs a fresh review and explicit acknowledgement before deploying.", plan)
