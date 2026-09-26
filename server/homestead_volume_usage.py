"""Read-only filesystem usage from fresh kubelet PVC samples, never actualSize.

Longhorn actualSize is a block/snapshot footprint, not bytes used by files.
Missing, stale, raw-block and inconsistent samples deliberately remain unknown.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import math


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None


def objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def fields(value):
    return value if isinstance(value, dict) else {}


def collect(get, claims, now=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    try:
        listing = get("/api/v1/pods")
        if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
            return {}
    except Exception:
        return {}
    hosts = {}
    for pod in objects(listing["items"]):
        meta, spec = fields(pod.get("metadata")), fields(pod.get("spec"))
        if not meta.get("uid") or not spec.get("nodeName") or fields(pod.get("status")).get("phase") != "Running":
            continue
        wanted = set()
        for volume in objects(spec.get("volumes")):
            key = (meta.get("namespace"), fields(volume.get("persistentVolumeClaim")).get("claimName"))
            claim = claims.get(key)
            if not claim or claim.get("spec", {}).get("volumeMode") == "Block":
                continue
            made = timestamp(claim.get("metadata", {}).get("creationTimestamp"))
            born = timestamp(meta.get("creationTimestamp"))
            if made is not None and (born is None or made > born):
                continue  # same-name claim replacement: old mount is not evidence.
            wanted.add(key)
        if wanted:
            hosts.setdefault(spec["nodeName"], {})[meta["uid"]] = wanted

    def read(host):
        try:
            summary = get(f"/api/v1/nodes/{host}/proxy/stats/summary", timeout=4)
            return host, summary if isinstance(summary, dict) else {}
        except Exception:
            return host, {}

    result = {}
    if not hosts:
        return result
    with ThreadPoolExecutor(max_workers=min(4, len(hosts))) as pool:
        for host, summary in pool.map(read, hosts):
            for pod in objects(summary.get("pods")):
                allowed = hosts[host].get(fields(pod.get("podRef")).get("uid"), set())
                for sample in objects(pod.get("volume")):
                    ref = fields(sample.get("pvcRef"))
                    key = (ref.get("namespace"), ref.get("name"))
                    at = timestamp(sample.get("time"))
                    if key not in allowed or at is None or not -5 <= now - at <= 120:
                        continue
                    used, capacity = sample.get("usedBytes"), sample.get("capacityBytes")
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (used, capacity)):
                        continue
                    if capacity <= 0 or used < 0 or used > capacity:
                        continue
                    # Shared mounts are observations of one filesystem, not
                    # additive usage. Keep the newest; prefer larger on a tie.
                    if key in result and (at, used) <= (result[key]["sample_at"], result[key]["used_bytes"]):
                        continue
                    result[key] = {"used_bytes": used, "capacity_bytes": capacity,
                                   "used_gb": round(used / 1024**3, 2), "capacity_gb": round(capacity / 1024**3, 2),
                                   "used_pct": round(used / capacity * 100, 1),
                                   "sample_at": at, "source": "kubelet"}
    return result
