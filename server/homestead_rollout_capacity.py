"""Conditional post-stop capacity and observed overlap for Deployment rollouts.

This does not drain, evict or detach anything. Ownership is resolved through
controller UIDs. Unknown ownership never becomes invented reclaimed capacity.
RollingUpdate intermediate removal/placement orders are not fully simulated.
"""
import math

import homestead_pod_resources as RESOURCES


def controller(obj):
    refs = [ref for ref in (obj.get("metadata") or {}).get("ownerReferences") or []
            if ref.get("controller") is True]
    return refs[0] if len(refs) == 1 else {}


def items(get, path):
    result = get(path)
    if not isinstance(result.get("items"), list) or (result.get("metadata") or {}).get("continue"):
        raise ValueError("incomplete inventory")
    return result["items"]


def owned_pods(current, pods, get, namespace):
    uid = (current.get("metadata") or {}).get("uid")
    if not uid or pods is None:
        return [], False
    try:
        sets = items(get, f"/apis/apps/v1/namespaces/{namespace}/replicasets")
        all_sets = {(row.get("metadata") or {}).get("uid") for row in sets
                    if (row.get("metadata") or {}).get("namespace") == namespace}
        if any((pod.get("metadata") or {}).get("namespace") == namespace and
               controller(pod).get("kind") == "ReplicaSet" and controller(pod).get("uid") not in all_sets
               for pod in pods if (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed")):
            return [], False  # deleted/new ReplicaSet race: do not invent ownership.
        owned_sets = {row["metadata"]["uid"] for row in sets
                      if (row.get("metadata") or {}).get("namespace") == namespace and
                      controller(row).get("kind") == "Deployment" and controller(row).get("uid") == uid}
        owned = [pod for pod in pods if (pod.get("metadata") or {}).get("namespace") == namespace and
                 (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed") and
                 controller(pod).get("kind") == "ReplicaSet" and controller(pod).get("uid") in owned_sets]
        return owned, True
    except Exception:
        return [], False


def fence(value, replicas, round_up):
    if isinstance(value, str) and value.endswith("%"):
        percent = int(value[:-1])
        if not 0 <= percent <= 100:
            raise ValueError("rollout percentage must be between 0 and 100")
        return (math.ceil if round_up else math.floor)(replicas * percent / 100)
    number = int(value)
    if number < 0:
        raise ValueError("rollout limits cannot be negative")
    return number


def stable(current, pods, replicas):
    meta, status = current.get("metadata") or {}, current.get("status") or {}
    try:
        observed = meta.get("generation") is not None and int(status.get("observedGeneration", -1)) >= int(meta["generation"])
    except (TypeError, ValueError):
        observed = False
    return (len(pods) == replicas and replicas > 0 and meta.get("generation") is not None and
            observed and
            all(status.get(key) == replicas for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")) and
            all(not (pod.get("metadata") or {}).get("deletionTimestamp") and
                (pod.get("status") or {}).get("phase") == "Running" and
                any(c.get("type") == "Ready" and c.get("status") == "True" for c in (pod.get("status") or {}).get("conditions") or [])
                for pod in pods))


def plan(current, proposed, namespace, get, get_nodes, planner, threshold, planned_claims=None):
    name = current["metadata"]["name"]
    replicas = int(proposed.get("spec", {}).get("replicas", 1) or 0)
    if not 0 <= replicas <= 100:
        raise ValueError("replicas must be between 0 and 100")
    strategy = (proposed.get("spec") or {}).get("strategy") or {}
    kind = strategy.get("type", "RollingUpdate")
    if kind not in ("Recreate", "RollingUpdate"):
        raise ValueError("unsupported Deployment rollout strategy")
    nodes = get_nodes()
    try:
        pods = items(get, "/api/v1/pods")
    except Exception:
        pods = None
    owned, known = owned_pods(current, pods, get, namespace)
    owned_ids = {id(pod) for pod in owned}
    # Only the conditional post-stop model releases these pods. The actual
    # overlap model keeps all reservations, host ports and PVC consumers.
    after = [pod for pod in pods if id(pod) not in owned_ids] if known else None

    def preview(count, snapshot):
        return planner(proposed, namespace, name, count, threshold, planned_claims=planned_claims,
                       pod_snapshot=snapshot, nodes_snapshot=nodes)

    result = preview(replicas, after)
    result["warnings"] = list(result["warnings"])
    warnings = result["warnings"]
    if replicas:
        warnings.append("updated pod estimates include every container, not only the added container")
        warnings.append("post-stop capacity assumes old pods have fully terminated and released ports and volumes; termination and storage detach are not guaranteed")
        warnings.append("live RAM still includes old pods; it is not subtracted from the conservative projection")
    else:
        warnings.append("this workload is stopped; the template changes but no pods are started")
    if not known:
        warnings.append("Deployment pod ownership is unverified; reclaimed resources and final placement are unknown")
    paused = bool(proposed.get("spec", {}).get("paused"))
    if paused:
        warnings.append("Deployment is paused; the template will not roll out until it is resumed")
    released, _, _, _ = RESOURCES.reservations(owned)
    details = {"strategy": kind, "replicas": replicas, "ownership_known": known,
               "owned_pods": [(p.get("metadata") or {}).get("name", "?") for p in owned],
               "release_request_gb": round(sum(row.get("memory", 0) for row in released.values()) / 1024**3, 2),
               "max_surge": 0, "max_unavailable": replicas if kind == "Recreate" else 0,
               "start_blocked": False, "intermediate_steps_verified": False}
    if kind == "Recreate":
        if replicas:
            warnings.append("Recreate stops all old pods before replacements start; all containers in this workload will be unavailable during the restart")
    elif replicas:
        rolling = strategy.get("rollingUpdate") or {}
        surge = fence(rolling.get("maxSurge", "25%"), replicas, True)
        unavailable = min(replicas, fence(rolling.get("maxUnavailable", "25%"), replicas, False))
        # Kubernetes resolves percentages that both round to zero by allowing
        # one unavailable replica, but explicit zero/zero is invalid API input.
        if surge == 0 and unavailable == 0:
            if str(rolling.get("maxSurge", "25%")).rstrip("%") == "0" and str(rolling.get("maxUnavailable", "25%")).rstrip("%") == "0":
                raise ValueError("RollingUpdate cannot set both maxSurge and maxUnavailable to zero")
            unavailable = 1
        details.update(max_surge=surge, max_unavailable=unavailable)
        warnings.append(f"RollingUpdate permits {surge} extra pod(s) and {unavailable} unavailable replica(s); terminating pods can extend the overlap")
        if surge:
            first = preview(1, pods)
            # Bound the display workload, not the actual controller setting.
            peak = preview(min(surge, replicas, 100), pods)
            details["overlap"] = peak
            details["first_pod"] = first
            warnings.extend(peak["warnings"])
            if first["blocked"]:
                if known and unavailable == 0 and stable(current, owned, replicas):
                    details["start_blocked"] = True
                    result["blocked"] = True
                    warnings.append("rollout cannot start in this snapshot: no replacement pod fits while maxUnavailable=0 keeps every healthy old replica running")
                else:
                    warnings.append("a replacement cannot currently fit alongside old pods; progress depends on allowed old-pod removal, termination and volume release")
        warnings.append("intermediate rolling-update placement and readiness are not fully simulated; a fitting first pod is not a completion guarantee")
    details["paused"] = paused
    if paused:
        details["resume_blocked"] = result["blocked"]
        result["blocked"] = False
        warnings.append("only the paused template is being saved; capacity must be checked again before resuming")
    result["rollout"] = details
    result["requires_confirmation"] = True
    return result
