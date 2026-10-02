"""Readable upstream errors for API responses, without Kubernetes envelopes."""
import json
import re


def _longhorn_expansion(message):
    if not ("validator.longhorn.io" in message and "CheckReplicasSizeExpansion" in message
            and "cannot schedule" in message and "Scheduling space condition failed" in message):
        return None
    growth = re.search(r"cannot schedule (\d+) more bytes", message)
    extra = f"the extra {int(growth[1]) / 1024 ** 3:g} GiB" if growth else "the requested growth"
    fields = {key: int(value) for key, value in re.findall(
        r"\b(StorageMaximum|StorageReserved|StorageScheduled|OverProvisioningPercentage):(\d+)\b", message)}
    allocation = False
    if growth and len(fields) == 4:
        limit = (fields["StorageMaximum"] - fields["StorageReserved"]) * fields["OverProvisioningPercentage"] / 100
        allocation = fields["StorageScheduled"] + int(growth[1]) > limit
    reason = ("would exceed its storage allocation limit" if allocation else
              "does not have enough capacity under Longhorn's storage limits")
    return (f"Longhorn cannot enlarge this volume: a replica disk {reason} with {extra}. "
            "Free unused Longhorn volumes on that disk, move replicas to disks with capacity, or add storage, then retry. "
            "Cluster-wide free space does not guarantee room on each replica disk.")


def message(error, limit=600):
    """Read a bounded body, decode Status first, then limit the displayed text."""
    raw = error.read(65536).decode("utf-8", "replace")
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    text = (body["message"] if isinstance(body, dict) and body.get("kind") == "Status"
            and isinstance(body.get("message"), str) else raw)
    text = _longhorn_expansion(text) or text
    return (text.strip() or str(error))[:limit]
