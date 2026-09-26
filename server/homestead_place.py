"""
Placement: work out what a workload actually needs, which hosts can satisfy it,
and move it there without silently pinning it forever.

Two things this gets right that a plain nodeSelector does not:

1. **Hardware binds.** A workload that mounts an iGPU, accelerator, capture
   device or USB bus can start on the wrong host and then fail at
   runtime, which is much harder to debug than refusing the move. We read the
   requirement off the pod spec and the availability off the node probe.

2. **Pin vs prefer.** A nodeSelector guarantees placement but permanently
   disables failover — the pod can never move again, which quietly undoes HA.
   So the default is a weighted nodeAffinity *preference*: the scheduler puts it
   where you asked, but it is still free to move if the host dies. A hard pin is
   available, but you have to ask for it, and only stays if it was already pinned.
"""
import json
import homestead_names as NAMES
import homestead_pod_resources as RESOURCES
import homestead_dependencies as DEPENDENCIES
import time
import urllib.error

kget = ksend = None
get_nodes = None
_cache = {}
hardware_features = lambda: []

DEVICE_HINTS = {
    "/dev/dri": ("dri", "Intel/AMD iGPU"),
    "/dev/apex": ("apex", "PCIe accelerator"),
    "/dev/bus/usb": ("usb", "USB device"),
    "/dev/video": ("video", "Video capture"),
    "/dev/ttyusb": ("tty", "USB serial"),
    "/dev/ttyacm": ("tty", "USB serial"),
}


def bind(_kget, _ksend, _get_nodes, _cache_ref, _hardware_features=None):
    global kget, ksend, get_nodes, _cache, hardware_features
    kget, ksend, get_nodes, _cache = _kget, _ksend, _get_nodes, _cache_ref
    if _hardware_features:
        hardware_features = _hardware_features


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


# ------------------------------------------------------------------ requirements
def requirements(dep):
    """What this workload binds to the host."""
    spec = dep["spec"]["template"]["spec"]
    reqs = {"devices": [], "features": [], "resources": {}, "labels": {},
            "pinned": None, "preferred": None}

    defs = hardware_features()
    by_label = {f["label"]: f for f in defs}

    for v in spec.get("volumes", []) or []:
        hp = (v.get("hostPath") or {}).get("path")
        if not hp:
            continue
        low = hp.lower().rstrip("/")
        for feature in defs:
            path = feature["host_path"].lower().rstrip("/")
            if low == path or low.startswith(path + "/"):
                if feature["id"] not in reqs["features"]:
                    reqs["features"].append(feature["id"])
                reqs["devices"].append({"path": hp, "kind": "feature", "id": feature["id"],
                                        "label": feature["name"]})
                break
        else:
            for prefix, (kind, label) in DEVICE_HINTS.items():
                if low.startswith(prefix):
                    reqs["devices"].append({"path": hp, "kind": kind, "label": label})
                    break
            else:
                if low.startswith("/dev"):
                    reqs["devices"].append({"path": hp, "kind": "dev", "label": "Host device"})

    for c in spec.get("containers", []) or []:
        for bucket in ("limits", "requests"):
            for k, v in ((c.get("resources") or {}).get(bucket) or {}).items():
                # device plugins look like vendor.com/thing
                if "/" in k and not k.startswith(("kubernetes.io", "requests.")):
                    reqs["resources"][k] = v

    sel = dict(spec.get("nodeSelector") or {})
    reqs["pinned"] = sel.pop("kubernetes.io/hostname", None)
    for key, val in sel.items():
        feature = by_label.get(key)
        if feature and val == "true":
            if feature["id"] not in reqs["features"]:
                reqs["features"].append(feature["id"])
        else:
            reqs["labels"][key] = val

    aff = ((spec.get("affinity") or {}).get("nodeAffinity") or {}) \
        .get("preferredDuringSchedulingIgnoredDuringExecution") or []
    for a in aff:
        for m in (a.get("preference", {}).get("matchExpressions") or []):
            if m.get("key") == "kubernetes.io/hostname" and m.get("values"):
                reqs["preferred"] = m["values"][0]
    return reqs


def _node_devices(n):
    t = n.get("temps") or {}
    return t.get("devices") or {}


def satisfies(node, reqs):
    """Returns (ok, [reasons it cannot run here])."""
    bad = []
    devs = _node_devices(node)
    have_probe = bool(devs)

    for fid in reqs.get("features", []):
        if not (node.get("hardware") or {}).get(fid):
            name = next((f["name"] for f in hardware_features() if f["id"] == fid), fid)
            bad.append(f"no {name}")

    for d in reqs["devices"]:
        kind = d["kind"]
        if kind == "feature":
            continue
        if kind == "dri":
            if node.get("igpu") or (node.get("labels") or {}).get("hardware/igpu") == "true":
                continue
            if have_probe and devs.get("dri"):
                continue
            bad.append(f"no iGPU for {d['path']}")
        elif kind == "apex":
            if not have_probe:
                bad.append(f"cannot confirm {d['path']} (node probe not installed)")
            elif not devs.get("apex"):
                bad.append(f"no accelerator device ({d['path']})")
        elif kind in ("video", "tty"):
            if have_probe and not devs.get(kind):
                bad.append(f"no {kind} device for {d['path']}")
        elif kind == "usb":
            if have_probe and not devs.get("usb"):
                bad.append("no USB devices present")

    for k, v in (reqs["resources"] or {}).items():
        alloc = (node.get("allocatable") or {}).get(k)
        if alloc in (None, "0"):
            bad.append(f"does not advertise {k}")

    for k, v in (reqs["labels"] or {}).items():
        if (node.get("labels") or {}).get(k) != v:
            bad.append(f"missing label {k}={v}")

    if node.get("status") != "Ready":
        bad.append(f"node is {node.get('status')}")
    if node.get("schedulable") is False:
        bad.append("cordoned")
    return (not bad), bad


def plan(ns, name, wl_cpu=0.0, wl_mem_mb=0.0):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    reqs = requirements(dep)
    nodes = get_nodes()
    here = reqs["pinned"] or reqs["preferred"] or ""

    # where is it actually running right now
    try:
        sel = dep["spec"].get("selector", {}).get("matchLabels", {})
        pods = kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
        running = [p["spec"].get("nodeName") for p in pods
                   if all(p["metadata"].get("labels", {}).get(k) == v for k, v in sel.items())
                   and p["status"].get("phase") == "Running"]
        current = running[0] if running else here
    except Exception:
        current = here

    cands = []
    for n in nodes:
        ok, why = satisfies(n, reqs)
        cpu_after = round(min(100, (n["cpu_used"] + wl_cpu) / max(n["cpu_cap"], 0.001) * 100), 1)
        mem_after = round(min(100, (n["mem_used_gb"] + wl_mem_mb / 1024) / max(n["mem_cap_gb"], 0.001) * 100), 1)
        # lower is better: weighted headroom, with a nudge away from hot hosts
        temp = ((n.get("temps") or {}).get("cpu_c")) or 0
        score = cpu_after * 0.5 + mem_after * 0.5 + (max(0, temp - 70) * 0.8)
        cands.append({
            "name": n["name"], "ok": ok, "why": why,
            "current": n["name"] == current,
            "cpu_after": cpu_after, "mem_after": mem_after,
            "cpu_pct": n["cpu_pct"], "mem_pct": n["mem_pct"],
            "igpu": n.get("igpu"), "temp_c": (n.get("temps") or {}).get("cpu_c"),
            "devices": _node_devices(n), "hardware": n.get("hardware", {}),
            "hardware_inventory": n.get("hardware_inventory", []), "pods_wl": n.get("pods_wl", 0),
            "score": round(score, 1),
        })

    viable = [c for c in cands if c["ok"] and not c["current"]]
    viable.sort(key=lambda c: c["score"])
    best = viable[0]["name"] if viable else None
    return {
        "ns": ns, "name": name, "current": current,
        "was_pinned": bool(reqs["pinned"]),
        "requirements": reqs,
        "candidates": sorted(cands, key=lambda c: (not c["ok"], c["score"])),
        "recommended": best,
        "probe": any(_node_devices(n) for n in nodes),
    }


def _memory_bytes(value):
    return RESOURCES.quantity(value)


def _pod_memory(spec):
    return RESOURCES.memory_estimate(spec)


def _cpu_millicores(value):
    return RESOURCES.quantity(value, "cpu")


def _pod_request(spec, resource):
    return RESOURCES.pod_request(spec, resource)


def _selector_match(requirement, value, present):
    operator = requirement.get("operator")
    choices = requirement.get("values") or []
    if operator == "In":
        return present and value in choices
    if operator == "NotIn":
        return not present or value not in choices
    if operator == "Exists":
        return present
    if operator == "DoesNotExist":
        return not present
    if operator in ("Gt", "Lt"):
        try:
            number, target = int(value), int(choices[0])
        except (ValueError, TypeError, IndexError):
            return False
        return present and (number > target if operator == "Gt" else number < target)
    return False


def _required_affinity_matches(spec, node):
    required = (((spec.get("affinity") or {}).get("nodeAffinity") or {})
                .get("requiredDuringSchedulingIgnoredDuringExecution") or {})
    if not required:
        return True
    terms = required.get("nodeSelectorTerms") or []
    labels = node.get("labels") or {}
    fields = {"metadata.name": node.get("name", "")}
    for term in terms:
        expressions = term.get("matchExpressions") or []
        field_expressions = term.get("matchFields") or []
        if not expressions and not field_expressions:
            continue  # Kubernetes treats an empty term as matching no nodes.
        if (all(_selector_match(expr, labels.get(expr.get("key")), expr.get("key") in labels)
                for expr in expressions) and
                all(_selector_match(expr, fields.get(expr.get("key")), expr.get("key") in fields)
                    for expr in field_expressions)):
            return True
    return False


def _tolerates(taint, tolerations):
    for toleration in tolerations:
        if toleration.get("effect") and toleration["effect"] != taint.get("effect"):
            continue
        operator = toleration.get("operator") or "Equal"
        if operator == "Exists" and (not toleration.get("key") or toleration["key"] == taint.get("key")):
            return True
        if (operator == "Equal" and toleration.get("key") == taint.get("key") and
                (toleration.get("value") or "") == (taint.get("value") or "")):
            return True
        if operator in ("Gt", "Lt") and toleration.get("key") == taint.get("key"):
            try:
                taint_value, wanted = int(taint.get("value")), int(toleration.get("value"))
            except (ValueError, TypeError):
                continue
            if (taint_value > wanted if operator == "Gt" else taint_value < wanted):
                return True
    return False


def _start_scheduler_check(spec, node):
    """Hard scheduling constraints relevant to a proposed pod start."""
    reasons, cautions = [], []
    if spec.get("nodeName") and spec["nodeName"] != node.get("name"):
        reasons.append(f"assigned to {spec['nodeName']}")
    if not _required_affinity_matches(spec, node):
        reasons.append("required node affinity does not match")
    for taint in node.get("taints") or []:
        # .spec.nodeName bypasses the scheduler's NoSchedule check, but not
        # NoExecute eviction. It is rare in a Deployment, yet can be present.
        if spec.get("nodeName") and taint.get("effect") == "NoSchedule":
            continue
        if taint.get("effect") in ("NoSchedule", "NoExecute") and not _tolerates(taint, spec.get("tolerations") or []):
            reasons.append(f"untolerated {taint.get('key', 'unknown')} taint")
    allocatable = node.get("allocatable") or {}
    for resource, label in (("memory", "memory"), ("cpu", "CPU")):
        request = _pod_request(spec, resource)
        capacity = (_memory_bytes if resource == "memory" else _cpu_millicores)(allocatable.get(resource))
        if request and capacity and request > capacity:
            reasons.append(f"{label} request exceeds node allocatable")
        elif request and not capacity:
            cautions.append(f"node allocatable {label} is unavailable")
    if spec.get("resourceClaims"):
        cautions.append("dynamic resource claims need scheduler review")
    if ((spec.get("affinity") or {}).get("podAffinity") or
            (spec.get("affinity") or {}).get("podAntiAffinity") or
            any(c.get("whenUnsatisfiable") == "DoNotSchedule" for c in spec.get("topologySpreadConstraints") or [])):
        cautions.append("pod affinity or topology spread may narrow placement")
    return reasons, cautions


def _reservation_snapshot():
    try:
        result = kget("/api/v1/pods")
        if not isinstance(result.get("items"), list) or (result.get("metadata") or {}).get("continue"):
            raise ValueError("incomplete pod list")
        booked, pending, resize, dra = RESOURCES.reservations(result["items"])
        warnings = []
        if pending:
            warnings.append(f"{pending} unscheduled pod(s) also compete for capacity")
        if resize:
            warnings.append("in-place resizing uses the higher observed resource reservation")
        if dra:
            warnings.append("existing dynamic resource allocations are not fully modelled")
        return booked, True, warnings, result["items"]
    except Exception:
        return {}, False, ["existing pod reservations are unavailable; free scheduler capacity is unknown"], None


def _resource_fit(spec, node, booked, known, additional):
    reasons, warnings, limits = [], [], [additional]
    resources = RESOURCES.resource_names(spec) | {"pods"}
    for resource in sorted(resources):
        request = 1 if resource == "pods" else _pod_request(spec, resource)
        if not request:
            continue
        allocatable = node.get("allocatable") or {}
        if resource not in allocatable:
            if "/" in resource or resource.startswith("hugepages-"):
                reasons.append(f"node does not advertise requested {resource}")
                limits.append(0)
            else:
                warnings.append(f"node allocatable {resource} is unavailable")
            continue
        available = RESOURCES.quantity(allocatable[resource], resource)
        free = max(0, available - booked.get(resource, 0))
        limits.append(free // request)
        if request > free:
            reasons.append(f"{resource} request exceeds {'remaining scheduler capacity' if known else 'node allocatable'}")
    # This is an upper bound under checked requests, not a scheduler guarantee.
    return min(limits), reasons, warnings


def start_plan(ns, name, replicas=1, warning_percent=88):
    """Preview RAM pressure before increasing a Deployment's replica count.

    A soft node preference is not a placement guarantee. Every eligible host
    is assessed; the UI must never label the start safe based on only the best
    candidate. Unknown metrics or an unbounded container require acknowledgement.
    """
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    current = int((dep.get("spec") or {}).get("replicas", 1) or 0)
    wanted = int(replicas)
    if wanted < 0 or wanted > 100:
        raise ValueError("replicas must be between 0 and 100")
    additional = max(0, wanted - current)
    reqs = requirements(dep)
    pod_spec = dep["spec"]["template"]["spec"]
    memory, unbounded = _pod_memory(pod_spec)
    reservations, reservations_known, snapshot_warnings, pods = _reservation_snapshot() if additional else ({}, False, [], None)
    dependencies = DEPENDENCIES.Snapshot(pod_spec, ns, kget, pods) if additional else None
    candidates = []
    for node in get_nodes():
        ok, reasons = satisfies(node, reqs)
        scheduler_reasons, scheduler_cautions = _start_scheduler_check(pod_spec, node)
        booked = reservations.get(node["name"], {})
        slots, fit_reasons, fit_warnings = _resource_fit(pod_spec, node, booked, reservations_known, additional)
        if dependencies:
            dependency_reasons, dependency_warnings, dependency_slots = dependencies.check(node, _required_affinity_matches)
            fit_reasons.extend(dependency_reasons)
            fit_warnings.extend(dependency_warnings)
            if dependency_slots is not None:
                slots = min(slots, dependency_slots)
        scheduler_reasons.extend(fit_reasons)
        scheduler_cautions.extend(fit_warnings + snapshot_warnings)
        reasons.extend(scheduler_reasons)
        ok = ok and not scheduler_reasons
        if reqs["pinned"] and node["name"] != reqs["pinned"]:
            ok = False
            reasons.append(f"pinned to {reqs['pinned']}")
        capacity = float(node.get("mem_cap_gb") or 0)
        used = float(node.get("mem_used_gb") or 0)
        reserved_gb = booked.get("memory", 0) / 1024**3
        proposed_here = min(additional, slots) if ok else 0
        projected = max(used, reserved_gb) + proposed_here * memory / 1024**3
        percent = round(projected / capacity * 100, 1) if capacity else None
        metrics = bool(node.get("mem_metrics_available", True))
        warnings = []
        if ok and additional:
            warnings.extend(scheduler_cautions)
            if not capacity or not metrics:
                warnings.append("live memory usage is unavailable")
            if unbounded:
                warnings.append("memory is not limited for " + ", ".join(unbounded))
            if not memory:
                warnings.append("no memory estimate is configured")
            if percent is not None and metrics and memory and percent >= warning_percent:
                warnings.append(f"projected RAM reaches {percent}% (warning at {warning_percent}%)")
            reserve = max(1.0, capacity * 0.1)
            if capacity and metrics and memory and capacity - projected < reserve:
                warnings.append(f"less than {round(reserve, 1)} GiB host reserve remains")
        candidates.append({"name": node["name"], "eligible": ok, "reasons": reasons,
                           "used_gb": round(used, 1), "capacity_gb": capacity,
                           "projected_gb": round(projected, 1), "projected_percent": percent,
                           "metrics_available": metrics, "warnings": warnings,
                           "reservations_known": reservations_known, "reserved_gb": round(reserved_gb, 2) if reservations_known else None,
                           "allocatable_gb": round(_memory_bytes((node.get("allocatable") or {}).get("memory")) / 1024**3, 2),
                           "reserved_cpu_percent": round(booked.get("cpu", 0) / 10, 1) if reservations_known else None,
                           "request_slots": slots if reservations_known else None,
                           "max_additional_pods": slots,
                           "projected_pods": proposed_here})
    eligible = [node for node in candidates if node["eligible"]]
    warnings = sorted({message for node in eligible for message in node["warnings"]})
    if additional and not eligible:
        warnings.append("no ready host satisfies this workload's placement requirements")
    total_bound = sum(node["max_additional_pods"] for node in eligible)
    if dependencies and dependencies.same_node:
        total_bound = max((node["max_additional_pods"] for node in eligible), default=0)
        if additional > 1:
            warnings.append("replicas sharing a ReadWriteOnce claim must fit together on one host")
    if dependencies and dependencies.single_pod:
        total_bound = min(1, total_bound)
    total_slots = total_bound if reservations_known else None
    insufficient = bool(additional and total_bound < additional)
    if insufficient and eligible:
        warnings.append(f"the requested {additional} additional replicas exceed the {total_bound} slots allowed by checked resources, ports and storage")
    return {"namespace": ns, "name": name, "current": current, "requested": wanted,
            "additional": additional, "pod_memory_gb": round(memory / 1024**3, 2),
            "pod_request_gb": round(_pod_request(pod_spec, "memory") / 1024**3, 2),
            "pod_cpu_request_percent": round(_pod_request(pod_spec, "cpu") / 10, 1),
            "reservations_known": reservations_known, "resource_slots": total_slots,
            "unbounded": unbounded, "warning_percent": warning_percent,
            "candidates": candidates, "warnings": warnings,
            "requires_confirmation": bool(additional and warnings),
            "blocked": bool(additional and (not eligible or insufficient))}


def impact(node):
    """Describe what happens to user workloads if *node* is evacuated/lost.

    This is deliberately a preflight rather than a best-effort warning.  The
    same hardware and label checks used by the manual move planner determine
    whether each running Deployment has somewhere else it can start.
    """
    nodes = get_nodes()
    others = [n for n in nodes if n["name"] != node]
    pods = kget("/api/v1/pods").get("items", [])
    seen, workloads = set(), []
    system_prefixes = ("kube-", "cattle-", "harvester-", "longhorn-")
    for pod in pods:
        if pod.get("spec", {}).get("nodeName") != node:
            continue
        ns = pod.get("metadata", {}).get("namespace", "")
        if ns.startswith(system_prefixes):
            continue
        labels = pod.get("metadata", {}).get("labels", {}) or {}
        name = labels.get("app")
        if not name or (ns, name) in seen:
            continue
        seen.add((ns, name))
        try:
            dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        reqs = requirements(dep)
        eligible, blocked = [], []
        for candidate in others:
            ok, why = satisfies(candidate, reqs)
            if ok:
                eligible.append(candidate["name"])
            else:
                blocked.append({"name": candidate["name"], "why": why})
        workloads.append({
            "ns": ns, "name": name, "hardware": reqs.get("features", []),
            "devices": reqs.get("devices", []), "eligible": eligible,
            "blocked": blocked, "stranded": not eligible,
        })
    stranded = [w for w in workloads if w["stranded"]]
    return {"node": node, "workloads": workloads,
            "movable": [w for w in workloads if not w["stranded"]],
            "stranded": stranded, "safe": not stranded}


# ------------------------------------------------------------------ move
def move(ns, name, node, pin=False):
    """Move a workload.

    pin=False (default) uses a weighted nodeAffinity preference, so the pod
    lands where asked but can still be rescheduled if the host dies. pin=True
    uses a hard nodeSelector, which guarantees placement and disables failover.
    node=None clears both.
    """
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    spec = dep["spec"]["template"]["spec"]

    sel = dict(spec.get("nodeSelector") or {})
    sel.pop("kubernetes.io/hostname", None)
    aff = spec.get("affinity") or {}
    na = aff.get("nodeAffinity") or {}
    prefs = [p for p in (na.get("preferredDuringSchedulingIgnoredDuringExecution") or [])
             if not any(m.get("key") == "kubernetes.io/hostname"
                        for m in p.get("preference", {}).get("matchExpressions", []))]

    mode = "unpinned"
    if node and pin:
        sel["kubernetes.io/hostname"] = node
        mode = "pinned"
    elif node:
        prefs.append({"weight": 100, "preference": {"matchExpressions": [
            {"key": "kubernetes.io/hostname", "operator": "In", "values": [node]}]}})
        mode = "preferred"

    if sel:
        spec["nodeSelector"] = sel
    else:
        spec.pop("nodeSelector", None)
    if prefs:
        na["preferredDuringSchedulingIgnoredDuringExecution"] = prefs
        aff["nodeAffinity"] = na
        spec["affinity"] = aff
    else:
        na.pop("preferredDuringSchedulingIgnoredDuringExecution", None)
        if na:
            aff["nodeAffinity"] = na
        else:
            aff.pop("nodeAffinity", None)
        if aff:
            spec["affinity"] = aff
        else:
            spec.pop("affinity", None)

    dep["spec"].setdefault("strategy", {})["type"] = "Recreate"
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        NAMES.key("movedAt")] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _bust("wl", "ov", "flow", "nodes", "impact:")
    return {"ok": True, "moved": name, "to": node or "any node", "mode": mode}
