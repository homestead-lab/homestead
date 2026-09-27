"""Read-only capacity check for an existing, non-surging probe DaemonSet."""
import hashlib
import json

import homestead_pod_resources as R


def plan(obj, template, nodes, pods, threshold):
    blocked, warnings, rows = [], [], []
    meta, state = obj.get("metadata", {}), obj.get("status", {})
    strategy = obj.get("spec", {}).get("updateStrategy") or {}
    surge = (strategy.get("rollingUpdate") or {}).get("maxSurge", 0)
    if strategy.get("type", "RollingUpdate") != "RollingUpdate" or surge not in (0, "0", "0%"):
        blocked.append("The probe must use a rolling update without surge before changing VM placement checks")
    expected = state.get("desiredNumberScheduled")
    if not expected or state.get("numberReady") != expected or state.get("observedGeneration") != meta.get("generation"):
        blocked.append("Wait for the current node probe rollout to finish")
    owned = [pod for pod in pods if any(owner.get("uid") == meta.get("uid") and owner.get("kind") == "DaemonSet"
             and owner.get("controller") is True for owner in pod.get("metadata", {}).get("ownerReferences", []))]
    hosts = {pod.get("spec", {}).get("nodeName") for pod in owned}
    if len(owned) != expected or len(hosts) != expected or None in hosts or any(
            pod.get("metadata", {}).get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running" for pod in owned):
        blocked.append("Node probe placement is changing; refresh after it settles")
    inventory = {node["name"]: node for node in nodes}
    booked, pending, _, _ = R.reservations(pods)
    if pending:
        warnings.append("Other workloads are waiting to start and can compete for capacity")
    spec = template["spec"]
    # Only a no-surge rollout can use the old probe's request credit. This
    # changes no workload or storage and doesn't claim scheduler reservation.
    for pod in owned:
        name = pod.get("spec", {}).get("nodeName")
        node = inventory.get(name, {})
        if node.get("status") != "Ready":
            blocked.append(f"{name or 'Unknown host'}: current host health is unavailable")
        for resource in ("cpu", "memory"):
            capacity = R.quantity(node.get("allocatable", {}).get(resource), resource)
            delta = max(0, R.pod_request(spec, resource) - R.pod_request(pod["spec"], resource))
            if not capacity or booked.get(name, {}).get(resource, 0) + delta > capacity:
                blocked.append(f"{name}: insufficient verified {resource} request capacity")
        upper = R.pod_request(spec, "memory")
        for container in spec.get("containers", []):
            upper += max(0, R.quantity(container.get("resources", {}).get("limits", {}).get("memory")) - R.request(container, "memory"))
        capacity = node.get("mem_cap_gb") or 0
        baseline = max(node.get("mem_used_gb") or 0, booked.get(name, {}).get("memory", 0) / 1024**3)
        percent = round((baseline + upper / 1024**3) / capacity * 100, 1) if capacity else None
        if not node.get("mem_metrics_available") or percent is None:
            warnings.append(f"{name}: current RAM usage is unavailable")
        elif percent >= threshold:
            warnings.append(f"{name}: projected RAM may reach {percent}% (warning at {threshold}%)")
        rows.append({"name": name, "projected_percent": percent})
    result = {"blocked": bool(blocked), "blockers": sorted(set(blocked)), "warnings": sorted(set(warnings)), "nodes": rows}
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result
