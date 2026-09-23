"""Read-only Harvester/Kubernetes platform health and node-onboarding guidance."""
import math
import re
import time
from datetime import datetime, timezone


kget = None
SYS_NS = set()
nodes_provider = lambda: []


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


def bind(_kget, _sys_ns, _nodes_provider):
    global kget, SYS_NS, nodes_provider
    kget, SYS_NS, nodes_provider = _kget, set(_sys_ns), _nodes_provider


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
    else:
        recommended_role = "Worker"
        role_reason = "The control plane already has an odd etcd membership; add compute capacity without changing quorum."
    return {
        "generated_at": int(now or time.time()), "state": state, "summary": summary,
        "versions": {"kubernetes": str(kubernetes_version or "").lstrip("v"),
                     "harvester": _harvester_version(harvester_version, nodes)},
        "control_plane": {"total": len(control), "ready": sum(1 for row in control if row["ready"]),
                          "etcd_total": len(etcd), "etcd_ready": etcd_ready,
                          "quorum_needed": quorum_needed, "quorum_margin": quorum_margin,
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
    pods = read("System services", "/api/v1/pods", {"items": []}).get("items", [])
    pods = [pod for pod in pods if (pod.get("metadata") or {}).get("namespace") in SYS_NS]
    events = read("Cluster warnings", "/api/v1/events", {"items": []}).get("items", [])
    csrs = read("Certificate requests", "/apis/certificates.k8s.io/v1/certificatesigningrequests",
                {"items": []}).get("items", [])
    # Harvester exposes this setting on supported releases. Older releases may
    # omit it, in which case the node OS image remains an equally useful source.
    try:
        setting = kget("/apis/harvesterhci.io/v1beta1/settings/server-version")
    except Exception:
        setting = {}
    harvester = setting.get("value") or (setting.get("status") or {}).get("value") or ""
    try:
        summaries = nodes_provider()
    except Exception as error:
        unavailable.append({"section": "Node utilisation", "reason": str(error)[:180]})
        summaries = []
    return build_report(version.get("gitVersion", ""), raw_nodes, summaries, pods, events,
                        csrs, harvester, unavailable=unavailable)
