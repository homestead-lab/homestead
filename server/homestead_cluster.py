"""Read-only Harvester/Kubernetes platform health and node-onboarding guidance."""
import math
import re
import threading
import time
from datetime import datetime, timezone
import homestead_alerts as ALERTS
import homestead_roles as ROLES
import homestead_routes


kget = None
SYS_NS = set()
nodes_provider = lambda: []
# server.py's dashboard summary (get_overview): it gathers from every part of Homestead.
overview = lambda: {}


SERVICE_DEFS = (
    ("api", "Kubernetes API", r"(^|[-_])kube-apiserver([-_]|$)", True),
    ("etcd", "etcd", r"(^|[-_])etcd([-_]|$)", True),
    ("controller", "Controller manager", r"kube-controller-manager", True),
    ("scheduler", "Scheduler", r"kube-scheduler", True),
    ("dns", "Cluster DNS", r"(^|[-_])(?:rke2-)?coredns([-_]|$)", True),
    ("harvester", "Harvester", r"(^|[-_])harvester(?:[-_]|$)|harvester-webhook", True),
    ("longhorn", "Longhorn", r"longhorn-manager|longhorn-driver-deployer", True),
    ("rancher", "Rancher", r"(^|[-_])rancher([-_]|$)", False),
    ("kubevip", "kube-vip", r"kube-vip", False),
    ("kubevirt", "KubeVirt", r"virt-api|virt-controller|virt-operator|virt-handler", False),
    ("cdi", "Containerized Data Importer", r"cdi-apiserver|cdi-deployment|cdi-operator", False),
)


def bind(_kget, _sys_ns, _nodes_provider, _overview=None):
    global kget, SYS_NS, nodes_provider, overview
    kget, SYS_NS, nodes_provider = _kget, set(_sys_ns), _nodes_provider
    overview = _overview or overview


def _iso_age(value, now=None):
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(0, int((datetime.fromtimestamp(now or time.time(), timezone.utc) - parsed).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _condition_map(node):
    return {row.get("type"): row for row in (node.get("status", {}).get("conditions") or [])}


def _node_rows(raw_nodes, summaries):
    metrics = {row.get("name"): row for row in (summaries or [])}
    rows = []
    for node in raw_nodes:
        metadata, status = node.get("metadata", {}), node.get("status", {})
        name = metadata.get("name", "unknown")
        labels = metadata.get("labels") or {}
        conditions = _condition_map(node)
        roles = sorted(key.split("/", 1)[1] for key in labels
                       if key.startswith("node-role.kubernetes.io/")) or ["worker"]
        ready = (conditions.get("Ready") or {}).get("status") == "True"
        pressure = [kind for kind in ("MemoryPressure", "DiskPressure", "PIDPressure",
                                      "NetworkUnavailable")
                    if (conditions.get(kind) or {}).get("status") == "True"]
        summary = metrics.get(name) or {}
        rows.append({
            "name": name, "ready": ready, "status": "Ready" if ready else "NotReady",
            "roles": roles, "schedulable": not node.get("spec", {}).get("unschedulable", False),
            "pressure": pressure, "cpu_pct": summary.get("cpu_pct"),
            "memory_pct": summary.get("mem_pct"), "disk_pct": summary.get("fs_pct"),
            "pods": summary.get("pods"), "version": (status.get("nodeInfo") or {}).get("kubeletVersion", ""),
            "os": (status.get("nodeInfo") or {}).get("osImage", ""),
            "created": metadata.get("creationTimestamp", ""),
        })
    return sorted(rows, key=lambda row: row["name"])


def _pod_ready(pod):
    status = pod.get("status") or {}
    states = status.get("containerStatuses") or []
    return status.get("phase") == "Running" and bool(states) and all(x.get("ready") for x in states)


def _service_rows(pods):
    groups = {key: [] for key, _, _, _ in SERVICE_DEFS}
    for pod in pods:
        meta = pod.get("metadata") or {}
        phase = (pod.get("status") or {}).get("phase")
        # Replaced static pods and completed system jobs can remain visible in
        # the API briefly. They are history, not part of current availability.
        if meta.get("deletionTimestamp") or phase in ("Succeeded", "Failed"):
            continue
        labels = meta.get("labels") or {}
        haystack = " ".join((meta.get("name", ""), labels.get("app", ""),
                             labels.get("k8s-app", ""), labels.get("component", ""))).lower()
        for key, _, pattern, _ in SERVICE_DEFS:
            if re.search(pattern, haystack):
                groups[key].append(pod)
                break
    out = []
    for key, label, _, required in SERVICE_DEFS:
        members = groups[key]
        ready = sum(1 for pod in members if _pod_ready(pod))
        if not members:
            state = "unknown"
        elif ready == len(members):
            state = "healthy"
        elif ready == 0:
            state = "critical"
        else:
            state = "attention"
        out.append({"id": key, "name": label, "pods": len(members), "ready": ready,
                    "state": state, "required": required,
                    "namespaces": sorted({(pod.get("metadata") or {}).get("namespace", "")
                                           for pod in members if (pod.get("metadata") or {}).get("namespace")})})
    return out


def _certificate_report(csrs, now=None):
    entries, pending, failed, expiring = [], 0, 0, 0
    for csr in csrs:
        meta, spec, status = csr.get("metadata", {}), csr.get("spec", {}), csr.get("status", {})
        conditions = status.get("conditions") or []
        condition = next((row.get("type") for row in reversed(conditions)
                          if row.get("type") in ("Approved", "Denied", "Failed")), "Pending")
        age = _iso_age(meta.get("creationTimestamp"), now)
        seconds = int(spec.get("expirationSeconds") or 0)
        remaining = max(0, seconds - age) if seconds and condition == "Approved" else None
        if condition == "Pending": pending += 1
        if condition in ("Denied", "Failed"): failed += 1
        if remaining is not None and remaining < 30 * 86400: expiring += 1
        entries.append({"name": meta.get("name", "unknown"), "signer": spec.get("signerName", ""),
                        "state": condition.lower(), "age_seconds": age,
                        "estimated_remaining_seconds": remaining})
    old_pending = sum(1 for row in entries if row["state"] == "pending" and row["age_seconds"] > 600)
    state = "critical" if failed else "attention" if old_pending or expiring else "healthy"
    return {"state": state, "total": len(entries), "pending": pending, "failed": failed,
            "expiring": expiring, "entries": sorted(entries, key=lambda row: row["age_seconds"], reverse=True)[:12],
            "note": "Issued certificate lifetime is estimated from each CSR request. Private keys and certificate bodies are never returned."}


def _warning_rows(events, now=None):
    rows = []
    for event in events:
        if event.get("type") != "Warning":
            continue
        meta, obj = event.get("metadata", {}), event.get("involvedObject", {})
        namespace = meta.get("namespace") or obj.get("namespace") or "cluster"
        if namespace not in SYS_NS and obj.get("kind") not in ("Node", "Namespace", "ComponentStatus"):
            continue
        stamp = (event.get("eventTime") or event.get("lastTimestamp") or
                 (event.get("series") or {}).get("lastObservedTime") or meta.get("creationTimestamp", ""))
        age = _iso_age(stamp, now)
        if age > 86400:
            continue
        rows.append({"namespace": namespace, "object": obj.get("name", "cluster"),
                     "kind": obj.get("kind", ""), "reason": event.get("reason", "Warning"),
                     "message": event.get("message", ""), "count": event.get("count", 1),
                     "time": stamp, "age_seconds": age})
    return sorted(rows, key=lambda row: row["age_seconds"])[:20]


def _harvester_version(explicit, nodes):
    if explicit:
        return str(explicit).lstrip("v")
    for node in nodes:
        match = re.search(r"Harvester(?:\s+HCI)?\s+v?([0-9][^\s]*)", node.get("os", ""), re.I)
        if match:
            return match.group(1)
    return ""


def build_report(kubernetes_version, raw_nodes, node_summaries, pods, events, csrs,
                 harvester_version="", now=None, unavailable=None):
    nodes = _node_rows(raw_nodes, node_summaries)
    services = _service_rows(pods)
    certificates = _certificate_report(csrs, now)
    warnings = _warning_rows(events, now)
    control = [row for row in nodes if "control-plane" in row["roles"] or "master" in row["roles"]]
    etcd = [row for row in nodes if "etcd" in row["roles"]]
    # Each member and what it is: a tiebreaker holds a vote and nothing else.
    vote_only = ROLES.tiebreakers(raw_nodes or [])
    members = sorted(({"name": row["name"], "ready": row["ready"], "role": "tiebreaker" if row["name"] in vote_only else "server"}
                      for row in etcd), key=lambda m: m["name"])
    etcd_ready = sum(1 for row in etcd if row["ready"])
    quorum_needed = math.floor(len(etcd) / 2) + 1 if etcd else 0
    quorum_margin = etcd_ready - quorum_needed if etcd else None
    quorum_state = ("unknown" if not etcd else "critical" if etcd_ready < quorum_needed
                    else "attention" if quorum_margin == 0 else "healthy")
    pressure = [{"node": row["name"], "conditions": row["pressure"]} for row in nodes if row["pressure"]]
    unready = [row["name"] for row in nodes if not row["ready"]]
    unhealthy_services = [row for row in services
                          if row["required"] and row["pods"] and row["state"] != "healthy"]
    critical = bool(unready or quorum_state == "critical" or
                    any(row["state"] == "critical" for row in unhealthy_services) or
                    certificates["state"] == "critical")
    attention = bool(pressure or quorum_state == "attention" or unhealthy_services or
                     certificates["state"] == "attention" or warnings)
    state = "critical" if critical else "attention" if attention else "healthy"
    if critical:
        summary = "The platform needs attention before maintenance or node changes."
    elif attention:
        summary = "The platform is online, with resilience or warning items to review."
    else:
        summary = "Control plane, quorum, nodes, and observed core services look healthy."
    if len(etcd) < 3 or len(etcd) % 2 == 0:
        recommended_role = "Control plane + etcd"
        role_reason = (f"The cluster has {len(etcd)} etcd member{'s' if len(etcd) != 1 else ''}. "
                       "An odd three-member control plane provides a useful one-node failure margin.")
        if len(etcd) == 2:
            role_reason += " A small machine can be the third vote as a tiebreaker: it runs no apps and holds no storage."
    else:
        recommended_role = "Worker"
        role_reason = "The control plane already has an odd etcd membership; add compute capacity without changing quorum."
    return {
        "generated_at": int(now or time.time()), "state": state, "summary": summary,
        "versions": {"kubernetes": str(kubernetes_version or "").lstrip("v"),
                     "harvester": _harvester_version(harvester_version, nodes)},
        "control_plane": {"total": len(control), "ready": sum(1 for row in control if row["ready"]),
                          "etcd_total": len(etcd), "etcd_ready": etcd_ready,
                          "quorum_needed": quorum_needed, "quorum_margin": quorum_margin, "members": members,
                          "state": quorum_state},
        "nodes": nodes,
        "capacity": {"pressure": pressure, "unready": unready,
                     "cordoned": [row["name"] for row in nodes if not row["schedulable"]]},
        "services": services, "certificates": certificates, "warnings": warnings,
        "unavailable": unavailable or [],
        "onboarding": {"recommended_role": recommended_role, "reason": role_reason,
                       "checks": [
                           "Reserve a unique hostname and management-network address.",
                           "Verify DNS, gateway, and time synchronization from the new host.",
                           "Match the running Harvester release before joining it.",
                           "Confirm the install disk is empty and data disks are intentionally assigned.",
                           "Review hardware features after join so workloads can use the new host.",
                           "Run drain and failover preflight before relying on the node for resilience.",
                       ]},
    }


# The whole pod list, kept by the leader's page snapshot each cycle and
# forgotten by a write to pods (homestead_kube.SHOWN_KEYS); kept no longer
# than the Cluster page keeps its own answer.
PODS_KEY = "pods"
PODS_SECONDS = 15
VERSION_KEY = "harvester-version"


def _version_setting():
    # Harvester exposes this setting on supported releases. Older releases may
    # omit it, in which case the node OS image remains an equally useful source.
    try:
        setting = kget("/apis/harvesterhci.io/v1beta1/settings/server-version")
    except Exception:
        setting = {}
    return setting.get("value") or (setting.get("status") or {}).get("value") or ""


def harvester_version():
    """The Harvester version inventory() reports, read alone: the setting,
    else the hosts' OS image. For the upgrade alert and the Upgrades page,
    which need nothing else of the inventory (#978)."""
    explicit = _version_setting()
    if explicit:
        return _harvester_version(explicit, [])
    nodes = kget("/api/v1/nodes").get("items", [])
    return _harvester_version("", [{"os": ((n.get("status") or {}).get("nodeInfo") or {}).get("osImage", "")}
                                   for n in nodes])


def inventory():
    unavailable = []

    def read(label, path, default):
        try:
            return kget(path)
        except Exception as error:
            unavailable.append({"section": label, "reason": str(error)[:180]})
            return default

    version = read("Kubernetes version", "/version", {})
    raw_nodes = read("Nodes", "/api/v1/nodes", {"items": []}).get("items", [])
    try:
        # The leader's page snapshot lists every pod each cycle and keeps
        # the list (#980): the system services are read from it.
        pods = homestead_routes.cached(PODS_KEY, PODS_SECONDS, lambda: kget("/api/v1/pods")).get("items", [])
    except Exception as error:
        unavailable.append({"section": "System services", "reason": str(error)[:180]})
        pods = []
    pods = [pod for pod in pods if (pod.get("metadata") or {}).get("namespace") in SYS_NS]
    # Only Warnings are shown (_warning_rows): the API server filters, so the
    # list is a small part of every event (#980).
    events = read("Cluster warnings", "/api/v1/events?fieldSelector=type%3DWarning", {"items": []}).get("items", [])
    csrs = read("Certificate requests", "/apis/certificates.k8s.io/v1/certificatesigningrequests",
                {"items": []}).get("items", [])
    harvester = _version_setting()
    try:
        summaries = nodes_provider()
    except Exception as error:
        unavailable.append({"section": "Node utilisation", "reason": str(error)[:180]})
        summaries = []
    return build_report(version.get("gitVersion", ""), raw_nodes, summaries, pods, events,
                        csrs, harvester, unavailable=unavailable)


# How long a degraded volume rebuilding a copy with no error is "repairing",
# not a fault (#941): a host reboot (a nightly OS update) rebuilds each copy
# that was on it, which takes minutes. Past this it is degraded again.
REPAIR_GRACE = 30 * 60
# Busiest first: the one activity the pill names when there are several.
ACTIVITY_ORDER = ("maintenance", "repairing", "updating", "starting")


def _span(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"


def classify_cluster_health(nodes, workloads, volumes, startup_grace=300, protection=None,
                            maintenance=(), overran=(), now=None, short_of_hosts=()):
    """Separate real availability faults from normal transitions.

    maintenance: hosts Homestead put in maintenance, which are an activity,
    not a fault, as alerts leave them out (#535); overran: hosts whose
    maintenance ended at its job's limit, which alerts raise at once (urgent).
    now: the clock a volume's rebuild is timed by (#941).
    short_of_hosts: the Longhorn volumes Match copies to hosts offers to
    lower (homestead_app_copies, #841); their warning says why they are
    degraded and points to the offer, so the two never disagree."""
    now = time.time() if now is None else now
    issues, activities = list(protection or []), []
    for node in nodes:
        name = node.get("name", "unknown")
        if name in maintenance:
            # Ready or not: a drain says so too, not only the reboot.
            activities.append({"state": "maintenance", "kind": "Node", "name": name,
                               "reason": "in maintenance: Homestead is working on this host"})
        elif node.get("status") != "Ready":
            issues.append({"severity": "critical", "kind": "Node", "name": name,
                           "reason": f"Kubernetes reports {node.get('status') or 'not ready'}. Review this host.",
                           **({"urgent": True} if name in overran else {})})
        for disk in (node.get("disk_issues") or []) if node.get("smart_notify", True) else []:
            issues.append({"severity": disk.get("severity", "degraded"), "kind": "Disk",
                           "name": f"{node.get('name', 'unknown')}/{disk.get('disk', 'unknown')}",
                           "reason": disk.get("reason", "SMART warning"),
                           "device_identity": disk.get("device_identity", ""),
                           **({"metric": disk["metric"], "value": disk["value"]} if "metric" in disk else {})})
    for volume in volumes:
        robustness = str(volume.get("robustness", "") or "").lower()
        label = volume.get("pvc_name") or volume.get("name") or "unknown"
        if robustness == "faulted":
            issues.append({"severity": "critical", "kind": "Volume", "name": label,
                           "reason": "Longhorn reports the volume faulted"})
        elif robustness == "degraded" and volume.get("name") in short_of_hosts:
            copies = int(volume.get("replicas") or 0)
            issues.append({"severity": "degraded", "kind": "Volume", "name": label, "one_host": True,
                           "reason": f"This cluster has one host, so Longhorn keeps 1 of the {copies} copies this "
                                     "volume asks for. An admin can match its copies to hosts: Cluster Health, "
                                     "Match copies to hosts"})
        elif robustness == "degraded":
            rebuild = volume.get("rebuild") or {}
            since = volume.get("degraded_since")
            repairing = bool(rebuild) and not rebuild.get("error") and since is not None
            if repairing and now - since <= REPAIR_GRACE:
                activities.append({"state": "repairing", "kind": "Volume", "name": label,
                                   "reason": f"rebuilding a copy, {int(rebuild.get('pct') or 0)}% done"})
            elif repairing:
                issues.append({"severity": "degraded", "kind": "Volume", "name": label,
                               "reason": f"still rebuilding a copy after {_span(now - since)} "
                                         f"({int(rebuild.get('pct') or 0)}% done)",
                               "for_s": int(now - since - REPAIR_GRACE)})
            else:
                issues.append({"severity": "degraded", "kind": "Volume", "name": label,
                               "reason": "Longhorn is rebuilding or missing a replica"})
    for workload in workloads:
        desired = int(workload.get("desired", 0) or 0)
        ready = int(workload.get("ready", 0) or 0)
        generation = int(workload.get("generation", 0) or 0)
        observed = int(workload.get("observed_generation", 0) or 0)
        updated = int(workload.get("updated", 0) or 0)
        transitioning = (ready < desired or updated < desired or observed < generation or
                          int(workload.get("unavailable", 0) or 0) > 0)
        if desired == 0 or not transitioning:
            continue
        name = workload.get("name", "unknown")
        namespace = workload.get("ns", "")
        resource = f"{namespace}/{name}" if namespace else name
        problems = workload.get("problems") or []
        if problems:
            issues.append({"severity": "degraded", "kind": "Workload", "name": resource,
                           "reason": str(problems[0])[:260]})
            continue
        age = int(workload.get("transition_age", startup_grace + 1) or 0)
        if age <= startup_grace:
            updating = int(workload.get("available", 0) or 0) > 0 and (
                updated < desired or observed < generation)
            activities.append({"state": "updating" if updating else "starting",
                               "kind": "Workload", "name": resource,
                               "reason": f"{ready}/{desired} replicas ready"})
        else:
            issues.append({"severity": "degraded", "kind": "Workload", "name": resource,
                           "reason": f"only {ready}/{desired} replicas ready, not ready for {_span(age)}",
                           "for_s": age - startup_grace})
    return verdict(issues, activities)


def verdict(issues, activities):
    """The verdict from the issues and activities that count."""
    health = "critical" if any(x["severity"] == "critical" for x in issues) else (
        "degraded" if issues else "healthy")
    activity = next((state for state in ACTIVITY_ORDER if any(x["state"] == state for x in activities)), "idle")
    state = health if health != "healthy" else (activity if activity != "idle" else "healthy")
    if issues:
        summary = "; ".join(f"{x['kind']} {x['name']}: {x['reason']}" for x in issues[:4])
    elif activities:
        summary = "; ".join(f"{x['kind']} {x['name']}: {x['reason']}" for x in activities[:4])
    else:
        summary = "All nodes, workloads, and attached volumes are healthy"
    return {"health": health, "health_state": state, "health_summary": summary,
            "health_issues": issues, "activities": activities}


class HealthHold:
    """The verdict's hold (#941), the one alerts use (ALERTS.HOLD, 60 s): an
    issue counts once it has lasted the hold, and keeps counting for the hold
    after it is gone, so a 30 s blip never turns the pill and a fault that
    flickers does not flicker it. One that goes before it counts starts over
    when it comes back, as an alert does, so one that keeps coming and going
    faster than the hold is not counted, as it is not alerted. An urgent
    issue (a host whose maintenance overran) counts at once, as its alert is
    raised at once. Critical is held too: alerts hold a host not ready for
    60 s as well.

    An issue that carries its own clock (for_s: a workload past its start-up
    grace, a rebuild past REPAIR_GRACE) has lasted that long already, so it is
    not held again. The others are timed here, on the replica that builds the
    overview (the leader's page snapshot): after a leader change, or on a
    replica that builds it rarely, they are held one hold from when it first
    sees them. When it has not looked for longer than the hold, every row
    starts over, as nothing says what happened in between."""

    def __init__(self, hold=None):
        self.hold = ALERTS.HOLD if hold is None else hold
        self.rows = {}
        self.last = None   # when it last looked
        self.lock = threading.Lock()

    def apply(self, report, now=None):
        """report with the verdict from the issues that count; current_issues
        is this cycle's own, which alerts read, as they hold them themselves."""
        current = report.get("health_issues") or []
        with self.lock:
            now = time.time() if now is None else now
            if self.last is not None and now - self.last > self.hold:
                self.rows.clear()
            self.last = now
            seen = {}
            for issue in current:
                seen.setdefault(f"{issue.get('kind', '')}:{issue.get('name', '')}", []).append(issue)
            for key, rows in seen.items():
                row = self.rows.setdefault(key, {"since": now})
                own = max((x["for_s"] for x in rows if isinstance(x.get("for_s"), (int, float))), default=0)
                row.update(issues=rows, gone=None, since=min(row["since"], now - max(0, own)))
                if now - row["since"] >= self.hold or any(x.get("urgent") for x in rows):
                    row["counted"] = True
            for key in [key for key in self.rows if key not in seen]:
                row = self.rows[key]
                row["gone"] = now if row["gone"] is None else row["gone"]
                if not row.get("counted") or now - row["gone"] >= self.hold:
                    del self.rows[key]
            counted = [issue for row in self.rows.values() if row.get("counted") for issue in row["issues"]]
        return {**report, **verdict(counted, report.get("activities") or []), "current_issues": current}


HOLD = HealthHold()


def health_brief():
    """The sidebar pill's verdict on every page (#940), from the overview the
    Dashboard reads: the leader's snapshot part while it is fresh, else the
    kept read, else None. It never builds the overview, so the alerts it rides
    with never wait on the cluster. as_of and age_s say when it was read."""
    dated = homestead_routes.page_kept("ov")
    if dated is None:
        return None
    as_of, value = dated
    issues = value.get("health_issues") or []
    critical = sum(1 for issue in issues if issue.get("severity") == "critical")
    return {"state": value.get("health_state") or value.get("health") or "unknown",
            "summary": value.get("health_summary") or "",
            "critical": critical, "degraded": len(issues) - critical,
            "as_of": round(as_of, 3), "age_s": max(0, int(time.time() - as_of))}


ALERTS.HEALTH[0] = lambda: health_brief()


def events():
    try:
        ev = kget("/api/v1/events?limit=160")
    except Exception:
        return []
    items = ev.get("items", [])
    items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
    return [{
        "ns": e["metadata"]["namespace"],
        "obj": e.get("involvedObject", {}).get("name", ""),
        "kind": e.get("involvedObject", {}).get("kind", ""),
        "reason": e.get("reason", ""),
        "msg": (e.get("message") or "")[:160],
        "type": e.get("type", "Normal"),
        "time": e.get("lastTimestamp") or e.get("eventTime") or "",
        "count": e.get("count", 1),
    } for e in items[:120]]


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/cluster"): ("viewer", lambda request: homestead_routes.cached("cluster", 15, inventory)),
    ("GET", "/api/overview"): ("viewer", lambda request: homestead_routes.page_aged("ov", overview)),
    ("GET", "/api/events"): ("viewer", lambda request: homestead_routes.cached("ev", 10, events)),
}
