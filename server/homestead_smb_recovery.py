"""Conservative, read-only storage observations for partial SMB recovery.

Never detach/salvage/delete storage or infer missing data from replica count.
The Deployment keeps runtime exclusions separately from the share inventory.
"""
import copy
import json

import homestead_names as NAMES

ANNOTATION = NAMES.key("smb-recovery")
SUSPEND_SECONDS = 60
RESTORE_SECONDS = 120


def read(deployment):
    raw = ((deployment or {}).get("metadata", {}).get("annotations") or {}).get(ANNOTATION, "{}")
    value = json.loads(raw)
    if not isinstance(value, dict) or any(not isinstance(value.get(key, {}), dict) for key in ("suspended", "pending", "restore_failures")):
        raise ValueError("SMB recovery state is invalid; mounts were left unchanged")
    return value


def write(deployment, state):
    deployment.setdefault("metadata", {}).setdefault("annotations", {})[ANNOTATION] = json.dumps(state, sort_keys=True)


def observe(rows, get, namespace):
    """Only complete API snapshots can establish that a data copy is missing.

    A detached, stopped replica can still contain good data. Conversely, a
    stale Running replica on a NotReady node is not accessible. Unknown CSI
    drivers and incomplete/error responses must never remove or restore shares.
    """
    claims = {row["pvc"] for row in rows if row.get("pvc")}
    if not claims:
        return {}, ""
    try:
        def items(path):
            result = get(path)
            if not isinstance(result.get("items"), list) or (result.get("metadata") or {}).get("continue"):
                raise ValueError("incomplete API inventory")
            return result["items"]
        nodes = items("/api/v1/nodes")
        pvcs = {p["metadata"]["name"]: p for p in items(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims")}
        prefix = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
        volumes = {v["metadata"]["name"]: v for v in items(prefix + "/volumes")}
        replicas = items(prefix + "/replicas")
    except Exception:
        return {}, "Storage availability could not be verified; existing SMB exclusions and mounts are retained."
    ready = {n["metadata"]["name"] for n in nodes if not n["metadata"].get("deletionTimestamp") and
             any(c.get("type") == "Ready" and c.get("status") == "True" for c in (n.get("status") or {}).get("conditions", []))}
    result = {}
    for claim in sorted(claims):
        pvc = pvcs.get(claim)
        if pvc is None:
            result[claim] = ("unavailable", "The configured PVC is missing; no replacement volume will be created.")
            continue
        if (pvc.get("status") or {}).get("phase") != "Bound":
            continue  # New/unbound claims may still be provisioning.
        volume_name = (pvc.get("spec") or {}).get("volumeName")
        volume = volumes.get(volume_name)
        if not volume:
            continue
        status = volume.get("status") or {}
        if status.get("robustness") == "faulted":
            result[claim] = ("unavailable", "Longhorn reports a faulted volume; repair its data replicas before restoring this share.")
            continue
        copies = [r for r in replicas if (r.get("spec") or {}).get("volumeName") == volume_name and
                  not r.get("metadata", {}).get("deletionTimestamp")]
        good = [r for r in copies if (r.get("spec") or {}).get("healthyAt") and
                not (r.get("spec") or {}).get("failedAt")]
        reachable = [r for r in good if (r.get("spec") or {}).get("nodeID") in ready and
                     (r.get("status") or {}).get("currentState") not in ("error", "unknown")]
        if reachable and (status.get("robustness") in ("healthy", "degraded") or status.get("state") == "detached"):
            result[claim] = ("available", "A previously healthy data replica is on a Ready node.")
        elif good and all((r.get("spec") or {}).get("nodeID") and (r.get("spec") or {}).get("nodeID") not in ready for r in good):
            result[claim] = ("unavailable", "All known usable data replicas are on unavailable nodes; waiting for the original data to return.")
        # Zero/new/rebuilding replicas or ambiguous health: leave unchanged.
    unknown = sorted(claims - result.keys())
    return result, ("Storage recovery is unverified for " + ", ".join(unknown) +
                    "; automatic exclusions are unchanged for these volumes." if unknown else "")


def plan(previous, rows, observed, warning, now):
    """Debounce outages and recovery across polls, restarts and leader changes."""
    claims = {row["pvc"] for row in rows if row.get("pvc")}
    state = {"suspended": {k: v for k, v in previous.get("suspended", {}).items() if k in claims},
             "pending": {}, "warning": warning,
             "restore_failures": {k: v for k, v in previous.get("restore_failures", {}).items() if k in claims}}
    for claim in sorted(claims):
        status, reason = observed.get(claim, ("unknown", ""))
        suspended = claim in state["suspended"]
        action = "restore" if status == "available" and suspended else "suspend" if status == "unavailable" and not suspended else ""
        if action == "restore" and claim in state["restore_failures"]:
            continue
        if not action:
            continue  # Unknown breaks the stability window, preserving exclusions.
        candidate = copy.deepcopy(previous.get("pending", {}).get(claim, {}))
        if (candidate.get("action") != action or not isinstance(candidate.get("since"), (int, float)) or
                now < candidate["since"] or now - candidate.get("last_seen", 0) > 180):
            candidate = {"action": action, "since": now}
        candidate["last_seen"] = now
        delay = RESTORE_SECONDS if action == "restore" else SUSPEND_SECONDS
        if now - candidate["since"] >= delay:
            if action == "restore":
                state["suspended"].pop(claim, None)
            else:
                state["suspended"][claim] = reason
        else:
            state["pending"][claim] = candidate
    return state


def offline_shares(deployment, rows):
    state = read(deployment)
    return [{"name": row["name"], "pvc": row["pvc"], "reason": state["suspended"][row["pvc"]]}
            for row in rows if row.get("pvc") in state.get("suspended", {})]
