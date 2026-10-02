"""Exclusive passthrough pools: configurations may overlap, allocations may not."""
import copy
from urllib.parse import quote

import homestead_pod_resources as R

TERMINAL = ("Succeeded", "Failed")


def requests(spec):
    devices = (spec.get("domain") or {}).get("devices") or {}
    result = {}
    for device in (devices.get("gpus") or []) + (devices.get("hostDevices") or []):
        resource = device.get("deviceName")
        if resource:
            result[resource] = result.get(resource, 0) + 1
    return result


def pending_nodes(spec):
    """Keep a pending reservation on its explicitly selected host."""
    node = (spec.get("nodeSelector") or {}).get("kubernetes.io/hostname")
    return [node] if node else None


def observe(vm, spec, read, nodes, pods, intents=(), exclude_vmi_uid=None):
    wanted = requests(spec)
    result = {"requests": wanted, "pods": pods, "holders": [], "pending": [], "blockers": []}
    if not wanted:
        return result
    if pods is None:
        result["blockers"].append("Passthrough device use could not be checked; no start is allowed")
        return result
    try:
        inventory = read("/apis/kubevirt.io/v1/virtualmachineinstances")
        if not isinstance(inventory.get("items"), list) or (inventory.get("metadata") or {}).get("continue"):
            raise ValueError("Incomplete instance inventory")
        instances = inventory["items"]
    except Exception:
        result["blockers"].append("Passthrough device use could not be checked; refresh permissions/connectivity before starting")
        return result
    result["pods"] = list(pods)
    active = [i for i in instances if (i.get("status") or {}).get("phase") not in TERMINAL]
    by_uid = {i.get("metadata", {}).get("uid"): i for i in instances if i.get("metadata", {}).get("uid")}
    represented = {}
    for pod in pods:
        if pod.get("status", {}).get("phase") in TERMINAL:
            continue
        owner = next((o for o in pod.get("metadata", {}).get("ownerReferences", [])
                      if o.get("controller") is True and o.get("kind") == "VirtualMachineInstance"
                      and o.get("apiVersion", "").split("/")[0] == "kubevirt.io"), {})
        instance = by_uid.get(owner.get("uid"))
        meta = (instance or pod).get("metadata") or {}
        label = ("VM " if instance else "pod ") + f"{meta.get('namespace', '')}/{meta.get('name', '')}"
        counts = {r: R.pod_request(pod.get("spec") or {}, r) for r in wanted}
        if any(counts.values()):
            consumed = represented.setdefault(owner.get("uid"), {})
            for r, count in counts.items():
                consumed[r] = consumed.get(r, 0) + count
            node = pod.get("spec", {}).get("nodeName")
            result["holders" if node else "pending"].append({"node": node, "nodes": pending_nodes(pod.get("spec") or {}),
                "label": label, "requests": counts})
    for instance in active:
        meta, status = instance.get("metadata") or {}, instance.get("status") or {}
        if meta.get("uid") == exclude_vmi_uid:
            continue
        counts = {r: max(0, n - represented.get(meta.get("uid"), {}).get(r, 0))
                  for r, n in requests(instance.get("spec") or {}).items() if r in wanted}
        counts = {r: n for r, n in counts.items() if n}
        if not counts:
            continue
        node = status.get("nodeName") or instance.get("spec", {}).get("nodeName")
        holder = {"node": node, "nodes": pending_nodes(instance.get("spec") or {}),
                  "label": f"VM {meta.get('namespace', '')}/{meta.get('name', '')}", "requests": counts}
        result["holders" if node else "pending"].append(holder)
        if node:
            # A VMI can reserve its device before the launcher is visible.
            result["pods"].append({"metadata": copy.deepcopy(meta), "status": {"phase": "Pending"},
                "spec": {"nodeName": node, "containers": [{"resources": {"requests": {r: str(n) for r, n in counts.items()}}}]}})
    own = (vm["metadata"].get("namespace"), vm["metadata"].get("name"))
    visible = {(i.get("metadata", {}).get("namespace"), i.get("metadata", {}).get("name")):
               requests(i.get("spec") or {}) for i in active}
    for item in intents:
        ref = item.get("ref") or {}
        target = (ref.get("namespace"), ref.get("name"))
        if (item.get("kind") != "vm-power" or target == own
                or ref.get("phase") not in ("prepared", "dispatching", "accepted", "uncertain")
                or not ref.get("retain_resources")):
            continue
        counts = {r: max(0, n - visible.get(target, {}).get(r, 0))
                  for r, n in (ref.get("device_requests") or {}).items() if r in wanted}
        counts = {r: n for r, n in counts.items() if n}
        if counts and target not in visible and ref.get("phase") == "accepted":
            try:
                stopped = read(f"/apis/kubevirt.io/v1/namespaces/{quote(target[0], safe='')}/virtualmachines/{quote(target[1], safe='')}")
                meta, policy = stopped.get("metadata") or {}, stopped.get("spec") or {}
                if (meta.get("uid") == ref.get("uid") and meta.get("resourceVersion") != ref.get("version")
                        and policy.get("runStrategy") == "Halted" and not stopped.get("status", {}).get("stateChangeRequests")):
                    continue  # An observed explicit stop releases a pending accepted start.
            except Exception:
                pass  # Missing evidence cannot release a retained power intent.
        if counts:
            result["pending"].append({"node": None, "nodes": ref.get("device_nodes"),
                "label": f"VM {target[0]}/{target[1]} (start pending)", "requests": counts})
    return result


def conflicts(usage, candidates, nodes):
    """Explain exhausted pools without treating identical cards as one card."""
    by_name = {node["name"]: node for node in nodes}
    reasons = []
    for candidate in candidates:
        node = candidate["name"]
        booked = [h for h in usage["holders"] if h["node"] == node]
        for resource, count in usage["requests"].items():
            capacity = R.quantity(by_name[node].get("allocatable", {}).get(resource), resource)
            used = sum(h["requests"].get(resource, 0) for h in booked)
            holders = sorted({h["label"] for h in booked if h["requests"].get(resource)})
            if holders and used + count > capacity:
                text = f"Device {resource} on {node} is in use by {', '.join(holders)}; stop the holder before starting this VM"
                candidate["reasons"].append(text)
                reasons.append(text)
    eligible = {c["name"] for c in candidates if c["eligible"]}
    for resource, count in usage["requests"].items():
        pending = [h for h in usage["pending"] if h["requests"].get(resource)
                   and (not h.get("nodes") or eligible.intersection(h["nodes"]))]
        capacity = sum(R.quantity(by_name[n].get("allocatable", {}).get(resource), resource) for n in eligible)
        used = sum(h["requests"].get(resource, 0) for h in usage["holders"] if h["node"] in eligible)
        if eligible and pending and used + sum(h["requests"][resource] for h in pending) + count > capacity:
            reasons.append(f"Device {resource} is reserved by {', '.join(sorted({h['label'] for h in pending}))}; wait for or stop that VM before starting this VM")
            for candidate in candidates:
                if candidate["eligible"]:
                    candidate["eligible"] = False
                    candidate["reasons"].append(reasons[-1])
                    candidate.update(request_slots=0, max_additional_pods=0, projected_pods=0)
    # A busy host does not block a free host offering another identical card.
    return [] if any(c["eligible"] for c in candidates) else sorted(set(reasons))
