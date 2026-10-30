"""If this host goes down: what stops, what moves and where, what is at risk.

Worked out from what Homestead already knows, read only:

* an app on the host moves if another host can run it - one with the hardware
  it uses, that its placement allows, with a copy of each of its Longhorn
  volumes, and none stored on the host itself (local-path) - after its failover setting's delay; an app set to wait
  waits; one with nowhere to go stops until the host is back. An app with copies on other
  hosts as well keeps answering meanwhile.
* a VM moves the same way, except one with a passthrough device, which stays
  with its device. On a drain, a migratable VM moves live instead.
* a StatefulSet's pod is never started on another host while the old one
  might still run: it stops where an app would, waits until its pod is
  deleted - by hand, or by Longhorn's "Pods on a failed node" setting - and
  moves once Longhorn deletes it, after its failover setting's delay; one set
  to wait waits for its host, as an app does. Where its volumes are stored, or that
  setting, couldn't be read, it says so instead of guessing.
* a Longhorn volume whose only healthy copy is on the host is unavailable, and
  lost if that disk is; one with copies elsewhere carries on with fewer.
* an address the host answers for moves to another host within seconds.

It is a forecast, not a promise: Kubernetes decides at the time, with what is
free then. Each line says why.
"""
import homestead_failover as FAILOVER
import homestead_roles as ROLES


FAILOVER_WORDS = {
    "move": ("moves", "about 15 seconds after the host stops answering"),
    "wait": ("waits", "set to wait for its host to come back"),
    "default": ("moves", "after about 5 minutes, Kubernetes' default"),
}

# A StatefulSet's pod on a host that stops answering (#440): Kubernetes
# never starts it elsewhere while the old one might still run, so it moves
# only once something deletes it. Longhorn does, for pods holding its
# volumes, when its node-down policy (homestead_lhcapacity.NODE_DOWN) says so.
SET_DELETED_BY = ("delete-statefulset-pod", "delete-both-statefulset-and-deployment-pod")
SET_WAITS = ("waits until its pod is deleted: Kubernetes doesn't start a StatefulSet's pod elsewhere "
             "while the old one might still run.")
SET_POINTER = " Longhorn can do this for you: Settings → Hardware and storage → Longhorn storage → Pods on a failed node."
# Longhorn deletes only a pod already leaving the down host, so it moves after
# its failover delay; one set to wait never leaves, and waits for the host.
SET_MOVES = "moves once Longhorn deletes its pod, "
ORDER = {"stops": 0, "waits": 1, "unknown": 2, "moves": 3}


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


# Said when Longhorn's volumes or their copies could not be read (#1002).
COPIES_UNREAD = "Longhorn's volume copies"


def _volume_for(volumes, ns, claim):
    return next((v for v in volumes or [] if v.get("namespace") == ns and v.get("pvc_name") == claim), None)


def _storage_stops(volumes, ns, claims, node, others, pvs=None):
    """Why the storage keeps it from moving to any of others, "", or None when
    that is not known: Longhorn's volumes or copies were not read; and notes.
    pvs: (namespace, claim) -> the hosts its PersistentVolume is pinned to,
    or None when they couldn't be read. A claim pinned to node alone is stored
    on it (local-path) and never moves (#440)."""
    if pvs is not None:
        for claim in claims or []:
            if pvs.get((ns, claim)) == [node]:
                return f"its volume {claim} is stored on {node}", []
    notes, unknown = [], False
    for claim in claims or []:
        v = _volume_for(volumes, ns, claim)
        if volumes is None or (v is not None and v.get("copies") is None):
            unknown = True
            continue
        if v is None:
            notes.append(f"{claim} is not a Longhorn volume: whether other hosts can reach it is not known")
            continue
        elsewhere = {c["node"] for c in v.get("copies") or [] if c.get("healthy") and c.get("node") != node}
        if not elsewhere:
            return f"its volume {claim} has its only healthy copy on {node}", notes
    return (None if unknown else ""), notes


def _tolerated(node, spec):
    """Whether a pod spec tolerates every NoSchedule and NoExecute taint on node."""
    tolerations = (spec or {}).get("tolerations") or []
    for taint in node.get("taints") or []:
        if taint.get("effect") not in ("NoSchedule", "NoExecute"):
            continue
        if not any((t.get("operator") == "Exists" and (not t.get("key") or t.get("key") == taint.get("key"))
                    or (t.get("key") == taint.get("key") and (t.get("value") or "") == (taint.get("value") or "")))
                   and (not t.get("effect") or t.get("effect") == taint.get("effect")) for t in tolerations):
            return False
    return True


def _hardware_hosts(features, nodes, node, spec=None):
    """Other ready hosts with every hardware feature listed, whose taints the app tolerates (#373)."""
    return [n["name"] for n in nodes
            if n["name"] != node and n.get("status") == "Ready" and n.get("schedulable", True)
            and all((n.get("hardware") or {}).get(f) for f in features or []) and _tolerated(n, spec)]


def _set_row(s, node, nodes, others_ready, volumes, pvs, node_down):
    """What becomes of a StatefulSet with a pod on node: the first rule that
    matches - pinned here, its volume here, nowhere else to run, couldn't be
    checked, set to wait for its host, waits for its pod to be deleted, or
    moves once Longhorn does, after its failover delay."""
    ns, name, spec = s["ns"], s["name"], s.get("spec") or {}
    pods = s.get("pods") or []
    here = [p for p in pods if p.get("node") == node]
    claims = list(dict.fromkeys(c for p in here for c in p.get("claims") or []))
    allowed = required_hosts(spec)
    hosts = [h for h in _hardware_hosts([], nodes, node, spec) if h in others_ready]
    if allowed is not None:
        hosts = [h for h in hosts if h in allowed]
    stop, notes = _storage_stops(volumes, ns, claims, node, hosts, pvs)
    row = {"kind": "set", "ns": ns, "name": name, "icon": "", "notes": notes, "here": len(here),
           "copies": s.get("replicas", len(pods)),
           "keeps_answering": any(p.get("ready") for p in pods if p.get("node") != node)}
    longhorn = [c for c in claims if _volume_for(volumes, ns, c) is not None]
    # Longhorn deletes only pods that hold its volumes: one with none, or with
    # any other, waits for a person whatever the policy says.
    all_longhorn = bool(claims) and len(longhorn) == len(claims)
    mode = FAILOVER.mode_of(spec)
    unread = []
    if pvs is None and len(longhorn) < len(claims):
        unread.append("where its volumes are stored")
    if node_down is None and all_longhorn:
        unread.append("Longhorn's Pods on a failed node setting")
    if stop is None:
        unread.append("where its volumes' copies are")
    if allowed is not None and not [h for h in allowed if h != node]:
        row.update(outcome="stops", why=f"it may only run on {', '.join(sorted(allowed))}")
    elif stop:
        row.update(outcome="stops", why=stop)
    elif not hosts:
        reason = ("none is ready" if not others_ready
                  else f"it may only run on {', '.join(sorted(allowed))}" if allowed is not None
                  else "the others are cordoned, or have taints it doesn't tolerate")
        row.update(outcome="stops", why="no other host can run it: " + reason)
    elif mode == "wait":
        row.update(outcome="waits", why=FAILOVER_WORDS["wait"][1])
    elif unread:
        row.update(outcome="unknown", why="couldn't be checked: " + " and ".join(unread))
    elif not all_longhorn or node_down not in SET_DELETED_BY:
        row.update(outcome="waits", why=SET_WAITS + (SET_POINTER if all_longhorn else ""))
        if all_longhorn:
            row["setting"] = "node_down"
    else:
        row.update(outcome="moves", why=SET_MOVES + FAILOVER_WORDS[mode][1], to=hosts)
    return row


def preview(node, workloads, deployments, vms, raw_vms, volumes, addresses, nodes, placement=None,
            sets=None, pvs=None, node_down=None):
    """Everything on node, and what becomes of it. placement: homestead_place's
    impact for node - its hardware, device and label checks decide where each
    app could go - when there is one; else the hardware features and placement
    rules here do.
    sets: each StatefulSet with its pods (homestead_nodes._impact_sources),
    None when they couldn't be read; pvs: where each claim's PersistentVolume
    is pinned; node_down: Longhorn's policy for a host that is down, None when
    it couldn't be read."""
    placed = {(w["ns"], w["name"]): w for w in (placement or {}).get("workloads") or []}
    specs = {(d["metadata"]["namespace"], d["metadata"]["name"]): d["spec"]["template"]["spec"] for d in deployments or []}
    raw = {(v["metadata"]["namespace"], v["metadata"]["name"]): v for v in raw_vms or []}
    # A tiebreaker holds an etcd vote, not apps or VMs (homestead_roles).
    others_ready = [n["name"] for n in nodes if n["name"] != node and n.get("status") == "Ready"
                    and not ROLES.is_tiebreaker(n)]
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
            hosts = _hardware_hosts(w.get("hardware"), nodes, node, spec)
            allowed = required_hosts(spec)
            if allowed is not None:
                hosts = [h for h in hosts if h in allowed]
        stop, notes = _storage_stops(volumes, ns, w.get("claims"), node, hosts, pvs)
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
        elif stop is None:
            row.update(outcome="unknown", why="couldn't be checked: where its volumes' copies are")
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
        stop, notes = _storage_stops(volumes, ns, claims, node, hosts, pvs)
        row = {"kind": "vm", "ns": ns, "name": name, "notes": notes, "os_logo": v.get("os_logo", ""), "icon": v.get("icon", ""),
               "migratable": bool(v.get("migratable"))}
        if passthrough:
            row.update(outcome="stops", why=f"it has {', '.join(passthrough)} passed through from {node}")
        elif stop:
            row.update(outcome="stops", why=stop)
        elif not hosts:
            row.update(outcome="stops", why=f"it may only run on {', '.join(sorted(allowed))}" if allowed else "no other host is ready")
        elif stop is None:
            row.update(outcome="unknown", why="couldn't be checked: where its volumes' copies are")
        else:
            row.update(outcome="moves", to=hosts,
                       why="restarts on another host once this one is declared down" +
                           ("; on a drain it moves live, without stopping" if v.get("migratable") else ""))
        apps.append(row)
    for s in sets or []:
        if any(p.get("node") == node for p in s.get("pods") or []):
            apps.append(_set_row(s, node, nodes, others_ready, volumes, pvs, node_down))
    # Copies not read are no evidence that nothing is at risk: the counts
    # are None then, never 0 (#1002).
    copies_unread = volumes is None or any(v.get("copies") is None for v in volumes)
    at_risk, fewer = [], []
    for v in volumes or []:
        if v.get("copies") is None:
            continue
        copies = v["copies"]
        if not any(c.get("node") == node for c in copies):
            continue
        healthy_elsewhere = [c for c in copies if c.get("healthy") and c.get("node") != node]
        row = {"name": v.get("pvc_name") or v["name"], "namespace": v.get("namespace", ""), "volume": v["name"],
               "left": len(healthy_elsewhere), "wanted": int(v.get("replicas") or len(copies) or 1)}
        (at_risk if not healthy_elsewhere else fewer).append(row)
    moved_addresses = [{"ip": a["ip"], "kind": a.get("kind", ""), "services": a.get("services") or []}
                       for a in addresses or [] if a.get("node") == node and a.get("kind") != "node"]
    apps.sort(key=lambda r: (ORDER.get(r["outcome"], len(ORDER)), r["kind"], r["name"]))
    return {"node": node, "apps": apps, "at_risk": at_risk, "fewer_copies": fewer, "addresses": moved_addresses,
            "others_ready": others_ready,
            "unread": ([] if sets is not None else ["StatefulSets"]) + ([COPIES_UNREAD] if copies_unread else []),
            "counts": {**{o: sum(1 for r in apps if r["outcome"] == o) for o in ORDER},
                       "at_risk": None if copies_unread else len(at_risk),
                       "fewer_copies": None if copies_unread else len(fewer), "addresses": len(moved_addresses)}}
