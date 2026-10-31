"""Hosts as the Nodes page shows them: each one's load, hardware, drives,
temperatures and duties, and what taking it down would do.

The temperatures, devices and SMART readings come from the optional node
probe (homestead_probe); the rest from Kubernetes. server.py binds the
cluster, its cache and the few collectors it shares with other pages
(workloads, volumes, settings); the background loops that relabel hardware
and watch ports stay there and call in here.
"""
import json
import time
import urllib.parse

import homestead_disks as DISKS
import homestead_hardware as HW
import homestead_kube_metrics as KUBE_METRICS
import homestead_host_os as HOST_OS
import homestead_impact as IMPACT
import homestead_lhcapacity as LHCAP
import homestead_node_actions as NODE_ACTIONS
import homestead_names as NAMES
import homestead_node_probes as PROBES
import homestead_network_inventory as INV
import homestead_nfs as NFS
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_ports as PORTS
import homestead_routes as ROUTER
import homestead_smart as SMART
import homestead_vm_list as VM_LIST

kget = ksend = None
DEFAULT_NS, SYS_NS = "lab", set()
DEFAULT_APP_SETTINGS = {}
get_app_settings = get_workloads = get_volumes = parse_cpu = parse_mem = _dns_name = None
# server.py's cache, under the same keys it drops when something changes.
cached = ROUTER.cached


def bind(_kget, _ksend, namespace, system_namespaces, settings, default_settings,
         workloads, volumes, cpu, memory, dns_name):
    """The cluster, and what other pages collect too:
    the app settings, workloads and volumes, and how quantities and names
    are read."""
    global kget, ksend, DEFAULT_NS, SYS_NS, get_app_settings, DEFAULT_APP_SETTINGS
    global get_workloads, get_volumes, parse_cpu, parse_mem, _dns_name
    kget, ksend, DEFAULT_NS, SYS_NS = _kget, _ksend, namespace, system_namespaces
    get_app_settings, DEFAULT_APP_SETTINGS = settings, default_settings
    get_workloads, get_volumes, parse_cpu, parse_mem, _dns_name = workloads, volumes, cpu, memory, dns_name


_RATE = {}   # key -> (counter, timestamp) for per-node byte counters


def rate(key, value, now=None):
    now = now or time.time()
    prev = _RATE.get(key)
    _RATE[key] = (value, now)
    if not prev or now <= prev[1] or value < prev[0]:
        return 0.0
    return (value - prev[0]) / (now - prev[1])


def node_temps(wait=True, unreachable=False):
    """Each host's last reading from the optional homestead-nodeprobe DaemonSet.

    Absent probe is not an error - it just means no thermal data, which the UI
    reports rather than showing a blank gauge. The reading happens in the
    background (homestead_node_probes): asking never waits on a probe once
    this replica's loop runs. A host whose probe has stopped answering is left
    out unless asked for: its last reading is too old to act on."""
    return PROBES.node_temps(DEFAULT_NS, wait=wait, unreachable=unreachable)


def start_probes():
    """This replica's background loops: the node probes (#705) and
    metrics-server (#906)."""
    PROBES.start(DEFAULT_NS)
    KUBE_METRICS.start(lambda path, **options: kget(path, **options))


def reconcile_hardware(fresh=False):
    """Every node's hardware labels, from what its probe sees now.

    The scheduler places a workload that needs a Coral by these labels, so
    they have to follow a device plugged in later - not wait until someone
    opens the Nodes page."""
    if fresh:
        try:
            PROBES.run_round()
        except Exception:
            pass             # the probe pods could not be listed: use what is known
    temps = node_temps()
    found = {}
    for n in kget("/api/v1/nodes").get("items", []):
        name = n["metadata"]["name"]
        nfs_facts = (temps.get(name) or {}).get("nfs") or {}
        if isinstance(nfs_facts.get("server"), bool):
            wanted = "true" if nfs_facts["server"] else None
            if (n["metadata"].get("labels") or {}).get(NFS.HOST_LABEL) != wanted:
                ksend("PATCH", f"/api/v1/nodes/{name}", {"metadata": {"labels": {NFS.HOST_LABEL: wanted}}},
                      ctype="application/merge-patch+json")
        devices = (temps.get(name) or {}).get("devices")
        if devices is None:
            continue                 # no probe here: nothing to say either way
        labels, auto = HW.reconcile_node(name, n["metadata"].get("labels", {}) or {},
                                         n["metadata"].get("annotations", {}) or {}, devices)
        found[name] = sorted(x["id"] for x in HW.inventory(labels, devices, auto) if x["detected"])
    if fresh:
        ROUTER.forget("nodes", "ov")
    return found


def node_stats(name):
    """Per-node network + filesystem counters from the kubelet summary API."""
    try:
        s = kget(f"/api/v1/nodes/{name}/proxy/stats/summary", timeout=8)
    except Exception:
        return {}
    nd = s.get("node", {}) or {}
    net = nd.get("network", {}) or {}
    ifaces = net.get("interfaces") or []
    pick = next((i for i in ifaces if i.get("name") == "mgmt-br"), None) or            next((i for i in ifaces if (i.get("rxBytes") or 0) > 0), None) or {}
    rx, tx = pick.get("rxBytes") or 0, pick.get("txBytes") or 0
    now = time.time()
    fs = nd.get("fs", {}) or {}
    runtime = (s.get("node", {}).get("runtime", {}) or {}).get("imageFs", {}) or {}
    return {
        "net_iface": pick.get("name", ""),
        "rx_mbps": round(rate(f"{name}:rx", rx, now) * 8 / 1e6, 2),
        "tx_mbps": round(rate(f"{name}:tx", tx, now) * 8 / 1e6, 2),
        "rx_total_gb": round(rx / 1024**3, 1),
        "tx_total_gb": round(tx / 1024**3, 1),
        "fs_used_gb": round((fs.get("usedBytes") or 0) / 1024**3, 1),
        "fs_cap_gb": round((fs.get("capacityBytes") or 0) / 1024**3, 1),
        "fs_pct": round((fs.get("usedBytes") or 0) / (fs.get("capacityBytes") or 1) * 100, 1),
        "img_used_gb": round((runtime.get("usedBytes") or 0) / 1024**3, 1),
    }


def smart_disk_issues(report, settings=None):
    """Classify actionable SMART findings using cluster-wide thresholds."""
    if not report or not report.get("available"):
        return []
    cfg = settings or DEFAULT_APP_SETTINGS["smart"]
    issues = []
    if str(report.get("health") or "").lower() == "failed":
        issues.append({"severity": "critical", "reason": "SMART overall-health check failed", "metric": "smart_failed", "value": 1})
    temperature = report.get("temperature_c")
    if temperature is not None:
        severity = ("critical" if float(temperature) >= cfg["temperature"]["critical"] else
                    "degraded" if float(temperature) >= cfg["temperature"]["warning"] else "")
        if severity:
            issues.append({"severity": severity,
                           "reason": f"Drive temperature is {temperature}°C", "metric": "temperature", "value": int(float(temperature) // 5)})
    reallocated = int(report.get("reallocated") or 0)
    pending = int(report.get("pending") or 0)
    uncorrectable = int(report.get("uncorrectable") or 0)
    media = int(report.get("media_errors") or 0)
    if reallocated >= cfg["reallocated_warning"]:
        issues.append({"severity": "degraded", "reason": f"{reallocated} reallocated sector{'s' if reallocated != 1 else ''}", "metric": "reallocated", "value": reallocated})
    if pending >= cfg["pending_critical"]:
        issues.append({"severity": "critical", "reason": f"{pending} pending sector{'s' if pending != 1 else ''}", "metric": "pending", "value": pending})
    if uncorrectable >= cfg["uncorrectable_critical"]:
        issues.append({"severity": "critical",
                       "reason": f"{uncorrectable} uncorrectable sector{'s' if uncorrectable != 1 else ''}", "metric": "uncorrectable", "value": uncorrectable})
    if media:
        issues.append({"severity": "critical", "reason": f"{media} NVMe media error{'s' if media != 1 else ''}", "metric": "media", "value": media})
    return issues


def smart_disk_health(report, settings=None):
    """What the findings add up to, in one word plus why.

    smartctl's own overall-health bit says PASSED until a drive is at death's
    door: a disk with hundreds of reallocated sectors still passes it. The
    counters are where the warning lives, so the verdict is drawn from those
    against the configured thresholds, and says which ones it was.
    """
    if not report:
        return {"state": "unavailable", "issues": [], "life_pct": None,
                "life_basis": "", "spare_pct": None, "stale_probe": False,
                "summary": "no SMART data for this drive"}
    if not report.get("available"):
        return {"state": "unavailable", "issues": [], "life_pct": None,
                "life_basis": "", "spare_pct": None, "stale_probe": False,
                "summary": report.get("unavailable_reason")
                or "this drive or its USB bridge does not expose SMART data"}
    issues = smart_disk_issues(report, settings)
    # A probe from before wear reporting sends no "wear" key at all, which is
    # not the same as a drive that has nothing to report. Saying "unsupported"
    # for both sends people to look at the drive instead of the probe.
    stale_probe = "wear" not in report
    wear = report.get("wear") or {}
    life = wear.get("life_pct")
    spare, floor = wear.get("spare_pct"), wear.get("spare_floor_pct")
    # A drive that has spent its endurance is worn out whatever else it says.
    if life is not None and int(life) <= 10:
        issues.append({"severity": "critical",
                       "reason": f"Only {int(life)}% of rated life remains", "metric": "wear", "value": 100 - int(life)})
    elif life is not None and int(life) <= 25:
        issues.append({"severity": "degraded",
                       "reason": f"{int(life)}% of rated life remains", "metric": "wear", "value": 100 - int(life)})
    if spare is not None and floor is not None and int(spare) <= int(floor):
        issues.append({"severity": "critical",
                       "reason": f"spare blocks are down to {int(spare)}%, "
                                 f"at the drive's floor of {int(floor)}%", "metric": "spare_used", "value": 100 - int(spare)})
    state = ("critical" if any(x["severity"] == "critical" for x in issues)
             else "attention" if issues else "healthy")
    if not issues:
        summary = "no reported defects"
        if str(report.get("health") or "").lower() == "passed":
            summary = "passed, with no reported defects"
    else:
        summary = "; ".join(x["reason"] for x in issues)
    return {"state": state, "issues": issues, "summary": summary,
            "life_pct": None if life is None else int(life),
            "life_basis": wear.get("basis", ""),
            "stale_probe": stale_probe,
            "spare_pct": None if spare is None else int(spare)}


def node_duties(pods, vols=None):
    """What falls to one node rather than another: the load-balancer addresses
    it announces, and the shared volumes it serves.

    kube-vip elects one node to answer for load-balanced addresses - on
    Harvester, the management VIP hosts join through among them - and records
    the winner in a Lease: plndr-svcs-lock for every Service at once,
    kubevip-<service> where each is elected on its own, plndr-cp-lock for the
    control-plane address. Longhorn serves each shared (RWX) volume through a
    share-manager pod on one node; that node going down pauses the volume
    until the pod starts elsewhere. vols: get_volumes(), when the caller has
    read it already.
    """
    duties = {}

    def note(node, key, value):
        if node and value not in duties.setdefault(node, {"vips": [], "rwx": [], "control_plane_vip": False})[key]:
            duties[node][key].append(value)
    try:
        leases = kget("/apis/coordination.k8s.io/v1/namespaces/kube-system/leases").get("items", [])
    except Exception:
        leases = []
    try:
        network = cached("network", 5, INV.inventory)
    except Exception:
        network = {}
    platform = network.get("platform_addresses") or {}
    for lease in leases:
        holder = str((lease.get("spec") or {}).get("holderIdentity") or "")
        if holder and lease["metadata"]["name"] == "plndr-cp-lock":
            duties.setdefault(holder, {"vips": [], "rwx": [], "control_plane_vip": False})["control_plane_vip"] = True
    # The addresses each node answers for, from the leases kube-vip keeps
    # beside each Service as well as its cluster-wide one (homestead_vips.py).
    for row in (network.get("addresses") or {}).get("addresses") or []:
        if row.get("kind") == "vip" and row.get("node") and row.get("announced"):
            note(row["node"], "vips", row["ip"])
    for node in duties.values():
        node["management_vip"] = [ip for ip in node["vips"] if ip in platform]
    try:
        rows = cached("volmap", 30, get_volumes) if vols is None else vols
        claims = {row["name"]: row.get("pvc_name") or row["name"] for row in rows}
    except Exception:
        claims = {}
    for pod in pods.get("items", []) if isinstance(pods, dict) else pods:
        meta = pod.get("metadata") or {}
        if meta.get("namespace") == "longhorn-system" and meta.get("name", "").startswith("share-manager-")                 and (pod.get("status") or {}).get("phase") == "Running":
            volume = meta["name"][len("share-manager-"):]
            note((pod.get("spec") or {}).get("nodeName", ""), "rwx", claims.get(volume, volume))
    return duties


def workload_names(npods, sys_ns):
    """The apps on a host, by name, and as namespace/name - the second for
    moving one from its host page without guessing its namespace (#366)."""
    names, ids = set(), set()
    for p in npods:
        meta = p["metadata"]
        app = meta.get("labels", {}).get("app") or meta["name"].rsplit("-", 2)[0]
        if meta["namespace"] in sys_ns or NAMES.label_of(meta, "task") or \
                (meta.get("labels", {}).get("app") or "") in (NAMES.NODEPROBE, "image-prepull"):
            continue
        names.add(app)
        ids.add(f"{meta['namespace']}/{_owner_name(meta) or app}")
    return sorted(names), sorted(ids)


def _owner_name(meta):
    """The controller a pod belongs to, by name: a ReplicaSet's Deployment
    (its name less the template hash), or a StatefulSet, DaemonSet or Job."""
    owner = next((o for o in meta.get("ownerReferences") or [] if o.get("controller")), None)
    if not owner:
        return ""
    name, hash_ = owner.get("name") or "", (meta.get("labels") or {}).get("pod-template-hash")
    if owner.get("kind") == "ReplicaSet" and hash_ and name.endswith("-" + hash_):
        return name[:-len(hash_) - 1]
    return name


def harvester_maintenance(node):
    """Whether Harvester has this host in its maintenance mode (#535): up to
    Harvester v1.6 an annotation, running while entering and completed once
    in; later releases a MaintenanceMode node condition, True throughout."""
    annotations = ((node or {}).get("metadata") or {}).get("annotations") or {}
    if annotations.get("harvesterhci.io/maintain-status") in ("running", "completed"):
        return True
    return any(c.get("type") == "MaintenanceMode" and c.get("status") == "True"
               for c in ((node or {}).get("status") or {}).get("conditions") or [])


def get_nodes(pods=None, vols=None):
    """Every host for the Hosts page. pods (the pod list) and vols
    (get_volumes()): when the caller has read them already (the leader's
    page snapshot)."""
    nodes = kget("/api/v1/nodes")
    # The loop's last reading, with its state: never waits on metrics-server (#906).
    usage, metrics_state = KUBE_METRICS.read("nodes", kget)
    metrics = {m["metadata"]["name"]: m for m in usage}
    pods = kget("/api/v1/pods") if pods is None else pods
    try:
        vmis = kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
    except Exception:
        vmis = []

    try:
        duties = node_duties(pods, vols)
    except Exception:
        duties = {}
    # Never waits on a probe: a host not read yet shows its probe as waiting,
    # and one that stopped answering shows its last reading with its age.
    temps = node_temps(wait=False, unreachable=True)
    probes = PROBES.status()
    try:
        disk_lines = DISKS.summary()
    except Exception:
        disk_lines = {}
    smart_cfg = get_app_settings().get("smart") or DEFAULT_APP_SETTINGS["smart"]
    try:
        # Each host's OS, as the leader last read it (homestead_host_os.py).
        host_os = HOST_OS.report()["hosts"]
    except Exception:
        host_os = {}
    out = []
    for n in nodes.get("items", []):
        name = n["metadata"]["name"]
        labels = n["metadata"].get("labels", {})
        cap = n["status"]["capacity"]
        conds = {c["type"]: c["status"] for c in n["status"].get("conditions", [])}
        roles = sorted([k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")])
        m = metrics.get(name, {})
        ucpu = parse_cpu(m.get("usage", {}).get("cpu"))
        umem = parse_mem(m.get("usage", {}).get("memory"))
        ccpu = float(cap.get("cpu", 1))
        cmem = parse_mem(cap.get("memory"))
        npods = [p for p in pods.get("items", []) if p.get("spec", {}).get("nodeName") == name]
        wl, wl_ids = workload_names(npods, SYS_NS)
        probed = (temps.get(name) or {}).get("devices") or {}
        annotations = n["metadata"].get("annotations", {}) or {}
        try:
            labels, auto_hardware = HW.reconcile_node(name, labels, annotations, probed)
        except Exception:
            auto_hardware = {x for x in NAMES.read(annotations, "auto-hardware").split(",") if x}
        hardware_inventory = HW.inventory(labels, probed, auto_hardware)
        hardware = {x["id"]: x["available"] for x in hardware_inventory}
        temp_payload = temps.get(name)
        disk_issues = []
        for disk in (temp_payload or {}).get("disks", []):
            disk["health"] = smart_disk_health(disk.get("smart"), smart_cfg)
            for issue in disk["health"]["issues"]:
                disk_issues.append({**issue, "disk": disk.get("name", "unknown"),
                                    "device_identity": (disk.get("smart") or {}).get("serial") or disk.get("serial") or ""})
        out.append({
            "name": name,
            "uid": n["metadata"].get("uid"),
            "maintenance": harvester_maintenance(n),
            "status": "Ready" if conds.get("Ready") == "True" else "NotReady",
            "roles": roles or ["worker"],
            "cpu_pct": round(ucpu / ccpu * 100, 1) if ccpu else 0,
            "cpu_used": round(ucpu, 2), "cpu_cap": ccpu,
            "mem_pct": round(umem / cmem * 100, 1) if cmem else 0,
            "mem_used_gb": round(umem / 1024**3, 1), "mem_cap_gb": round(cmem / 1024**3, 1),
            "mem_metrics_available": bool((m.get("usage") or {}).get("memory")),
            "metrics": metrics_state,
            "pods": len(npods),
            "pods_sys": len([p for p in npods if p["metadata"]["namespace"] in SYS_NS]),
            "pods_wl": len([p for p in npods if p["metadata"]["namespace"] not in SYS_NS]),
            "vms": len([v for v in vmis if v.get("status", {}).get("nodeName") == name]),
            "igpu": hardware["igpu"],
            "disks": disk_lines.get(name, []),
            "hardware": hardware,
            "hardware_inventory": hardware_inventory,
            "workloads": wl,
            "workload_ids": wl_ids,
            "kernel": n["status"].get("nodeInfo", {}).get("kernelVersion", ""),
            "os": n["status"].get("nodeInfo", {}).get("osImage", ""),
            "schedulable": not n.get("spec", {}).get("unschedulable", False),
            "addresses": {a["type"]: a["address"] for a in n["status"].get("addresses", [])},
            "allocatable": n["status"].get("allocatable", {}),
            "taints": n.get("spec", {}).get("taints", []),
            "labels": labels,
            "info": n["status"].get("nodeInfo", {}),
            "conditions": [{"type": c["type"], "status": c["status"], "reason": c.get("reason", "")}
                           for c in n["status"].get("conditions", [])],
            "created": n["metadata"].get("creationTimestamp", ""),
            # A new boot ID is a reboot; Ready's last change is how long it
            # has been up as far as Kubernetes is concerned; the probe knows
            # how long the host itself has been running.
            "boot_id": n["status"].get("nodeInfo", {}).get("bootID", ""),
            "ready_since": next((c.get("lastTransitionTime", "") for c in n["status"].get("conditions", [])
                                 if c.get("type") == "Ready" and c.get("status") == "True"), ""),
            "uptime_s": (temp_payload or {}).get("uptime_s"),
            **node_stats(name),
            "temps": temp_payload,
            "probe": probes.get(name) or {"state": "absent"},
            "disk_issues": disk_issues,
            "smart_notify": smart_cfg.get("notify_failures", True),
            "duties": duties.get(name) or {"vips": [], "rwx": [], "control_plane_vip": False, "management_vip": []},
            "host_os": (host_os.get(name) or {}).get("summary"),
        })
    return out


def _statefulsets():
    """Each StatefulSet outside the system namespaces, with its pods - their
    host, whether ready, and the claims they mount - or None when either
    couldn't be read (#440)."""
    try:
        sets = kget("/apis/apps/v1/statefulsets").get("items", [])
        pods = kget("/api/v1/pods").get("items", [])
    except Exception:
        return None
    mine = {}
    for p in pods:
        meta, spec, status = p.get("metadata") or {}, p.get("spec") or {}, p.get("status") or {}
        owner = next((o for o in meta.get("ownerReferences") or [] if o.get("kind") == "StatefulSet"), None)
        if not owner or status.get("phase") in ("Succeeded", "Failed"):
            continue
        ready = status.get("phase") == "Running" and any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
        mine.setdefault((meta.get("namespace"), owner.get("name")), []).append({
            "name": meta.get("name", ""), "node": spec.get("nodeName") or "", "ready": ready,
            "claims": [v["persistentVolumeClaim"]["claimName"] for v in spec.get("volumes") or []
                       if (v.get("persistentVolumeClaim") or {}).get("claimName")]})
    out = []
    for s in sets:
        meta, spec = s.get("metadata") or {}, s.get("spec") or {}
        ns, name = meta.get("namespace"), meta.get("name")
        if ns in SYS_NS:
            continue
        out.append({"ns": ns, "name": name, "replicas": int(spec["replicas"]) if spec.get("replicas") is not None else 1,
                    "spec": (spec.get("template") or {}).get("spec") or {}, "pods": mine.get((ns, name), [])})
    return out


def _pinned_volumes():
    """(namespace, claim) -> the hosts its PersistentVolume's required node
    affinity pins it to ([] when none), or None when they couldn't be read.
    local-path pins each volume to the host that stores it (#440)."""
    try:
        items = kget("/api/v1/persistentvolumes").get("items", [])
    except Exception:
        return None
    out = {}
    for pv in items:
        spec = pv.get("spec") or {}
        ref = spec.get("claimRef") or {}
        if not ref.get("name"):
            continue
        hosts = set()
        for term in ((spec.get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms") or []:
            for expr in term.get("matchExpressions") or []:
                if expr.get("key") == "kubernetes.io/hostname" and expr.get("operator") == "In":
                    hosts |= set(expr.get("values") or [])
        out[(ref.get("namespace"), ref["name"])] = sorted(hosts)
    return out


def _impact_sources():
    """What every host's impact preview reads, once for all hosts: the
    Deployments and VMs, the StatefulSets with their pods, where each claim's
    PersistentVolume is pinned, and Longhorn's policy for a host that is down.
    A read that fails is None, never empty or a default."""
    deployments = [d for d in kget("/apis/apps/v1/deployments").get("items", []) if d["metadata"]["namespace"] not in SYS_NS]
    try:
        raw_vms = kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
    except Exception:
        raw_vms = []
    return {"deployments": deployments, "raw_vms": raw_vms, "sets": _statefulsets(), "pvs": _pinned_volumes(),
            "node_down": LHCAP.node_down(kget)}


def node_impact(node, sources=None):
    """If this host goes down: what stops, moves and is at risk (homestead_impact).
    sources: _impact_sources(), when several hosts are previewed at once."""
    node = _dns_name(node, "host name")
    nodes = cached("nodes", 5, get_nodes)
    if not any(n["name"] == node for n in nodes):
        raise ValueError(f"there is no host {node}")
    src = sources or _impact_sources()
    try:
        # inventory()["addresses"] is VIPS.address_map(): {"nodes": [...], "addresses": [...]} (#372).
        places = (cached("network", 5, INV.inventory) or {}).get("addresses") or {}
        addresses = (places.get("addresses") or []) if isinstance(places, dict) else list(places)
    except Exception:
        addresses = []
    try:
        placement = cached("impact:" + node, 5, lambda: PLACE.impact(node))
    except Exception:
        placement = None
    return IMPACT.preview(node, cached("wl", 5, get_workloads), src["deployments"], cached("vms", 5, VM_LIST.list_vms), src["raw_vms"],
                          cached("vol", 8, get_volumes), addresses, nodes, placement,
                          sets=src["sets"], pvs=src["pvs"], node_down=src["node_down"])


def set_node_hardware(cfg):
    name = cfg["node"]
    selected = set(cfg.get("features") or [])
    # Backward-compatible body accepted from pre-v1.3 clients.
    if cfg.get("igpu"): selected.add("igpu")
    if cfg.get("coral_pcie"): selected.add("coral_pcie")
    if cfg.get("coral_usb"): selected.add("coral_usb")
    known = {f["id"] for f in HW.features()}
    if selected - known:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(selected - known)))
    labels = {f["label"]: "true" if f["id"] in selected else "false" for f in HW.features()}
    # These are deliberate overrides, so remove them from the auto-managed set.
    ksend("PATCH", f"/api/v1/nodes/{name}", {"metadata": {"labels": labels,
          "annotations": {HW.AUTO_ANNOTATION: None}}},
          ctype="application/merge-patch+json")
    ROUTER.forget_prefix("nodes")
    ROUTER.forget_prefix("ov")
    return {"ok": True, "node": name}


def port_contexts(probes):
    """What each host's ports carry beyond its own uplink: the LAN networks
    on its interfaces, read from their NetworkAttachmentDefinitions."""
    networks = {}
    try:
        nads = kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions").get("items", [])
    except Exception:
        nads = []
    for nad in nads:
        meta = nad.get("metadata") or {}
        try:
            config = json.loads((nad.get("spec") or {}).get("config") or "{}")
        except ValueError:
            config = {}
        iface = config.get("master") or config.get("bridge") or ""
        resource = (meta.get("annotations") or {}).get("k8s.v1.cni.cncf.io/resourceName", "")
        if not iface and resource.startswith("macvtap.network.kubevirt.io/"):
            iface = resource.split("/", 1)[1]
        if iface:
            networks.setdefault(iface, []).append(f"{meta.get('namespace', '')}/{meta.get('name', '')}")
    return {node: {"uplink": (probe or {}).get("default_interface") or "", "networks": networks}
            for node, probe in (probes or {}).items()}


def ports_report():
    # Every host, so one whose probe is not answering is said to be unknown,
    # not quietly well.
    probes = dict(node_temps())
    for n in kget("/api/v1/nodes").get("items", []):
        probes.setdefault(n["metadata"]["name"], None)
    return PORTS.report(probes, port_contexts(probes))


def _one(q, name):
    return (q.get(name) or [""])[0]


def _is_host(name):
    """True when name is a host in the cluster's node list. A route that keeps
    a read per host checks this first, so a name that is not one keeps
    nothing and reads nothing."""
    return bool(name) and any(n.get("name") == name for n in cached("nodes", 5, get_nodes) or [])


def _no_host():
    return ROUTER.Reply(404, {"error": "there is no host by that name"})


def _node(request):
    name = _one(request.query, "name")
    if not _is_host(name):
        return _no_host()
    return cached("node:" + name, 5, lambda: next((n for n in get_nodes() if n["name"] == name), {}))


def _ports(request):
    report = cached("ports", 20, ports_report)
    node = _one(request.query, "node")
    if node:
        return report["hosts"].get(node) or {"node": node, "available": False, "ports": [],
                                             "conditions": [], "reason": "No node probe answers on this host."}
    return report


def _smart(request):
    node, disk = _one(request.query, "node"), _one(request.query, "disk")
    if not disk:
        return SMART.inventory(node)
    report = SMART.disk(node, disk)
    # The same verdict the node card shows, so one drive cannot be healthy in
    # the list and something else in its own detail.
    report["health_assessment"] = smart_disk_health(report, get_app_settings().get("smart"))
    return report


def _impact(request):
    node = _one(request.query, "node")
    if not node:
        raise ValueError("node is required")
    if not _is_host(node):
        return _no_host()
    return cached("impact:" + node, 5, lambda: PLACE.impact(node))


def _smart_test(request):
    b = request.body
    result = SMART.start_test(b.get("node"), b.get("disk"), b.get("test"))
    result["operation"] = OPS.start(
        "smart-test", f"SMART {result['test']} test · {result['disk']}",
        {"kind": "Disk", "name": result["disk"], "namespace": result["node"]},
        "/nodes?node=" + urllib.parse.quote(result["node"]),
        {"node": result["node"], "disk": result["disk"],
         "test": result["test"], "expected_seconds": result["expected_seconds"],
         "baseline": result["baseline"], "started_epoch": result["started_epoch"]},
        result["message"])
    PROBES.read_smart_again()
    ROUTER.forget("node:" + result["node"])
    return result


def _drain(request):
    b = request.body
    impact = PLACE.impact(b["node"])
    if impact["stranded"] and not b.get("allow_stranded"):
        return ROUTER.Reply(409, {"error": "some workloads have no eligible failover host", "impact": impact})
    return NODE_ACTIONS.drain(b["node"], b.get("grace", 30), b.get("system", False))


ROUTES = {
    ("GET", "/api/nodes"): ("viewer", lambda request: ROUTER.page("nodes", get_nodes)),
    ("GET", "/api/node"): ("viewer", _node),
    ("GET", "/api/nodes/ports"): ("viewer", _ports),
    ("GET", "/api/node/smart"): ("viewer", _smart),
    ("GET", "/api/node/impact"): ("viewer", _impact),
    ("GET", "/api/nodes/impact"): ("viewer", lambda request: node_impact(_one(request.query, "node"))),
    ("POST", "/api/node/smart/test"): ("admin", _smart_test),
    ("POST", "/api/node/hardware"): ("admin", lambda request: set_node_hardware(request.body)),
    ("POST", "/api/hardware/rescan"): ("operator", lambda request: {"ok": True, "nodes": reconcile_hardware(fresh=True)}),
    ("POST", "/api/node/drain"): ("admin", _drain),
}
