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
