"""If this host goes down: what stops, what moves and where, what is at risk.

Worked out from what Homestead already knows, read only:

* an app on the host moves if another host can run it - one with the hardware
  it uses, that its placement allows, with a copy of each of its Longhorn
  volumes - after its failover setting's delay; an app set to wait waits; one
  with nowhere to go stops until the host is back. An app with copies on other
  hosts as well keeps answering meanwhile.
* a VM moves the same way, except one with a passthrough device, which stays
  with its device. On a drain, a migratable VM moves live instead.
* a Longhorn volume whose only healthy copy is on the host is unavailable, and
  lost if that disk is; one with copies elsewhere carries on with fewer.
* an address the host answers for moves to another host within seconds.

It is a forecast, not a promise: Kubernetes decides at the time, with what is
free then. Each line says why.
"""

FAILOVER_WORDS = {
    "move": ("moves", "about 15 seconds after the host stops answering"),
    "wait": ("waits", "set to wait for its host to come back"),
    "default": ("moves", "after about 5 minutes, Kubernetes' default"),
}


def required_hosts(podspec):
    """The hosts a pod spec may run on, if its placement names them; else None."""
    hosts = None
    selector = (podspec.get("nodeSelector") or {}).get("kubernetes.io/hostname")
    if selector:
        hosts = {selector}
    required = (((podspec.get("affinity") or {}).get("nodeAffinity") or {})
                .get("requiredDuringSchedulingIgnoredDuringExecution") or {})
    for term in required.get("nodeSelectorTerms") or []:
        for expr in term.get("matchExpressions") or []:
            if expr.get("key") == "kubernetes.io/hostname" and expr.get("operator") == "In":
                values = set(expr.get("values") or [])
                hosts = values if hosts is None else hosts & values
    return hosts


def _volume_for(volumes, ns, claim):
    return next((v for v in volumes if v.get("namespace") == ns and v.get("pvc_name") == claim), None)


def _storage_stops(volumes, ns, claims, node, others):
    """Why the storage keeps it from moving to any of others, or ""; and notes."""
    notes = []
    for claim in claims or []:
        v = _volume_for(volumes, ns, claim)
        if v is None:
            notes.append(f"{claim} is not a Longhorn volume: whether other hosts can reach it is not known")
            continue
        elsewhere = {c["node"] for c in v.get("copies") or [] if c.get("healthy") and c.get("node") != node}
        if not elsewhere:
            return f"its volume {claim} has its only healthy copy on {node}", notes
    return "", notes


def _hardware_hosts(features, nodes, node):
    """Other ready hosts with every hardware feature listed."""
    return [n["name"] for n in nodes
            if n["name"] != node and n.get("status") == "Ready" and n.get("schedulable", True)
            and all((n.get("hardware") or {}).get(f) for f in features or [])]


def preview(node, workloads, deployments, vms, raw_vms, volumes, addresses, nodes, placement=None):
    """Everything on node, and what becomes of it. placement: homestead_place's
    impact for node - its hardware, device and label checks decide where each
    app could go - when there is one; else the hardware features and placement
    rules here do."""
    placed = {(w["ns"], w["name"]): w for w in (placement or {}).get("workloads") or []}
    specs = {(d["metadata"]["namespace"], d["metadata"]["name"]): d["spec"]["template"]["spec"] for d in deployments or []}
    raw = {(v["metadata"]["namespace"], v["metadata"]["name"]): v for v in raw_vms or []}
    others_ready = [n["name"] for n in nodes if n["name"] != node and n.get("status") == "Ready"]
    apps = []
    for w in workloads or []:
        if node not in (w.get("nodes") or []) or w.get("platform") or not w.get("desired"):
            continue
        name, ns = w["name"], w["ns"]
        spec = specs.get((ns, name)) or {}
        place = placed.get((ns, name))
        if place is not None:
            hosts = list(place.get("eligible") or [])
            allowed = None
        else:
            hosts = _hardware_hosts(w.get("hardware"), nodes, node)
            allowed = required_hosts(spec)
            if allowed is not None:
                hosts = [h for h in hosts if h in allowed]
        stop, notes = _storage_stops(volumes, ns, w.get("claims"), node, hosts)
        still = len([h for h in w.get("nodes") or [] if h != node])
        row = {"kind": "app", "ns": ns, "name": name, "icon": w.get("icon", ""), "notes": notes,
               "keeps_answering": still > 0, "self": bool(w.get("self"))}
        if stop:
            row.update(outcome="stops", why=stop)
        elif allowed is not None and not hosts:
            row.update(outcome="stops", why=f"it may only run on {', '.join(sorted(allowed))}")
        elif not hosts and place is not None:
            reasons = sorted({why for b in place.get("blocked") or [] for why in (b.get("why") or [])})
            row.update(outcome="stops", why="no other host can run it: " + ("; ".join(reasons[:3]) if reasons else "none is ready"))
        elif not hosts:
            missing = [f for f in w.get("hardware") or [] if not any((n.get("hardware") or {}).get(f) for n in nodes if n["name"] != node)]
            row.update(outcome="stops", why=(f"no other host has its {', '.join(missing)}" if missing else "no other host is ready"))
        else:
            verb, when = FAILOVER_WORDS.get(w.get("failover") or "default", FAILOVER_WORDS["default"])
            row.update(outcome=verb, why=when, to=hosts)
        apps.append(row)
    for v in vms or []:
        if v.get("node") != node or not v.get("running"):
            continue
        name, ns = v["name"], v["ns"]
        source = raw.get((ns, name)) or {}
        tspec = ((source.get("spec") or {}).get("template") or {}).get("spec") or {}
        devices = (tspec.get("domain") or {}).get("devices") or {}
        passthrough = [d.get("deviceName") or d.get("name") for key in ("hostDevices", "gpus") for d in devices.get(key) or []]
        claims = [d.get("claim") for d in v.get("disks") or [] if d.get("claim")]
        hosts = [h for h in others_ready]
        allowed = required_hosts(tspec)
        if allowed is not None:
            hosts = [h for h in hosts if h in allowed]
        stop, notes = _storage_stops(volumes, ns, claims, node, hosts)
        row = {"kind": "vm", "ns": ns, "name": name, "notes": notes, "os_logo": v.get("os_logo", ""), "icon": v.get("icon", ""),
               "migratable": bool(v.get("migratable"))}
        if passthrough:
            row.update(outcome="stops", why=f"it has {', '.join(passthrough)} passed through from {node}")
        elif stop:
            row.update(outcome="stops", why=stop)
        elif not hosts:
            row.update(outcome="stops", why=f"it may only run on {', '.join(sorted(allowed))}" if allowed else "no other host is ready")
        else:
            row.update(outcome="moves", to=hosts,
                       why="restarts on another host once this one is declared down" +
                           ("; on a drain it moves live, without stopping" if v.get("migratable") else ""))
        apps.append(row)
    at_risk, fewer = [], []
    for v in volumes or []:
        copies = v.get("copies") or []
        if not any(c.get("node") == node for c in copies):
            continue
        healthy_elsewhere = [c for c in copies if c.get("healthy") and c.get("node") != node]
        row = {"name": v.get("pvc_name") or v["name"], "namespace": v.get("namespace", ""), "volume": v["name"],
               "left": len(healthy_elsewhere), "wanted": int(v.get("replicas") or len(copies) or 1)}
        (at_risk if not healthy_elsewhere else fewer).append(row)
    moved_addresses = [{"ip": a["ip"], "kind": a.get("kind", ""), "services": a.get("services") or []}
                       for a in addresses or [] if a.get("node") == node and a.get("kind") != "node"]
    order = {"stops": 0, "waits": 1, "moves": 2}
    apps.sort(key=lambda r: (order.get(r["outcome"], 3), r["kind"], r["name"]))
    return {"node": node, "apps": apps, "at_risk": at_risk, "fewer_copies": fewer, "addresses": moved_addresses,
            "others_ready": others_ready,
            "counts": {"stops": sum(1 for r in apps if r["outcome"] == "stops"),
                       "waits": sum(1 for r in apps if r["outcome"] == "waits"),
                       "moves": sum(1 for r in apps if r["outcome"] == "moves"),
                       "at_risk": len(at_risk), "fewer_copies": len(fewer), "addresses": len(moved_addresses)}}
