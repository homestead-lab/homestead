"""Fail-closed host power review and durable reboot/shutdown observation."""
import hashlib
import json
import time
import urllib.error

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
kget = impact = quorum = None
power_enabled = lambda: False


def bind(_kget, _impact, _quorum, _power_enabled):
    global kget, impact, quorum, power_enabled
    kget, impact, quorum, power_enabled = _kget, _impact, _quorum, _power_enabled


def _items(path, absent_ok=False):
    try:
        return kget(path).get("items", [])
    except urllib.error.HTTPError as error:
        if absent_ok and error.code == 404:
            return None
        raise


def _ready(node):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in ((node.get("status") or {}).get("conditions") or []))


def plan(node, action):
    if action not in ("reboot", "poweroff"):
        raise ValueError("action must be reboot or poweroff")
    node_obj = kget(f"/api/v1/nodes/{node}")
    place = impact(node)
    control = quorum()
    replicas = _items(f"{LH}/replicas", absent_ok=True)
    volumes = _items(f"{LH}/volumes", absent_ok=True) if replicas is not None else None
    pods = _items("/api/v1/pods")
    vmis = _items("/apis/kubevirt.io/v1/virtualmachineinstances", absent_ok=True) or []
    storage_unknown = replicas is None or volumes is None
    by_volume = {}
    for row in replicas or []:
        spec, status = row.get("spec") or {}, row.get("status") or {}
        name = spec.get("volumeName")
        if name:
            by_volume.setdefault(name, []).append((spec.get("nodeID"),
                status.get("currentState") == "running" and not spec.get("failedAt")))
    volume_obj = {row.get("metadata", {}).get("name"): row for row in volumes or []}
    affected = []
    for name, copies in by_volume.items():
        if not any(host == node for host, _ in copies):
            continue
        elsewhere = sum(healthy and host != node for host, healthy in copies)
        volume = volume_obj.get(name) or {}
        k8s = (volume.get("status") or {}).get("kubernetesStatus") or {}
        claim = "/".join(x for x in (k8s.get("namespace"), k8s.get("pvcName")) if x) or name
        affected.append({"name": name, "claim": claim, "healthy_elsewhere": elsewhere,
                         "risk": "unavailable" if not elsewhere else "single-copy" if elsewhere == 1 else "resync",
                         "robustness": (volume.get("status") or {}).get("robustness", "unknown")})
    affected.sort(key=lambda row: ({"unavailable": 0, "single-copy": 1, "resync": 2}[row["risk"]], row["claim"]))
    vm_rows = sorted({(v.get("metadata") or {}).get("namespace", "") + "/" +
                      (v.get("metadata") or {}).get("name", "") for v in vmis
                      if (v.get("status") or {}).get("nodeName") == node})
    pods_here = [p for p in pods if (p.get("spec") or {}).get("nodeName") == node]
    blockers = []
    if not power_enabled():
        blockers.append("Host power control is disabled (ENABLE_NODE_POWER is off)")
    if node in control.get("members", []) and control.get("can_lose", 0) < 1:
        blockers.append("Shutting down this etcd member would lose quorum")
    if not _ready(node_obj):
        blockers.append("The host is not Ready; investigate it before issuing a new power command")
    if vm_rows:
        blockers.append("Running VMs are on this host; migrate or stop them and review again")
    if storage_unknown:
        blockers.append("Storage replica inventory is unavailable; volume impact cannot be verified")
    warnings = []
    if storage_unknown:
        warnings.append("Longhorn replica inventory is unavailable; volume safety cannot be confirmed")
    if place.get("stranded"):
        warnings.append(f"{len(place['stranded'])} workload(s) have no eligible failover host")
    if affected:
        warnings.append(f"{len(affected)} volume(s) lose a replica until this host returns or Longhorn rebuilds")
    if not affected and not storage_unknown:
        warnings.append("Non-Longhorn or host-local volumes are not covered by replica checks")
    boot_id = ((node_obj.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
    review = {"node": node, "action": action, "boot_id": boot_id,
              "workloads": sorted((w.get("ns", ""), w.get("name", ""), bool(w.get("stranded")))
                                  for w in place.get("workloads", [])),
              "volumes": [(v["name"], v["risk"], v["healthy_elsewhere"], v["robustness"]) for v in affected],
              "pods": sorted(((p.get("metadata") or {}).get("namespace", ""),
                              (p.get("metadata") or {}).get("name", "")) for p in pods_here),
              "storage_unknown": storage_unknown, "vms": vm_rows,
              "quorum": control.get("can_lose", 0)}
    token = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[:20]
    return {"node": node, "action": action, "review_token": token, "boot_id": boot_id,
            "quorum": control, "workloads": place.get("workloads", []),
            "stranded": place.get("stranded", []), "volumes": affected,
            "vms": vm_rows, "pods": len(pods_here), "storage_unknown": storage_unknown,
            "requires_data_ack": storage_unknown or any(v["risk"] in ("unavailable", "single-copy") for v in affected),
            "blockers": blockers, "warnings": warnings, "ready": not blockers}


def status(item):
    """Observe a power command without treating a lost API connection as success."""
    ref = item["ref"]
    now = time.time()
    node = kget(f"/api/v1/nodes/{ref['node']}")
    up = _ready(node)
    boot = ((node.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
    if not up:
        ref["saw_down"] = True
        ref.setdefault("down_at", now)
        if ref["action"] == "poweroff" and now - ref["down_at"] >= 15:
            return "succeeded", 100, "Host is NotReady; physical power state cannot be verified through Kubernetes"
        return "running", 60, "Host is NotReady; waiting to confirm shutdown or return"
    if ref["action"] == "poweroff" and ref.get("saw_down"):
        return "failed", 90, "Host returned Ready after shutdown was requested; check its power state"
    if ref["action"] == "reboot" and ref.get("saw_down") and boot and boot != ref.get("boot_id"):
        ref.setdefault("returned_at", now)
        volume_names = ref.get("volumes") or []
        if not volume_names:
            return "succeeded", 100, "Host returned Ready with a new boot ID"
        volumes = _items(f"{LH}/volumes", absent_ok=True)
        if volumes is None:
            if now - ref["returned_at"] > 1800:
                return "failed", 85, "Host rebooted, but Longhorn health could not be verified within 30 minutes"
            return "running", 85, "Host rebooted; Longhorn volume health is unavailable"
        by_name = {v.get("metadata", {}).get("name"): v for v in volumes}
        pending = [name for name in volume_names if
                   (by_name.get(name, {}).get("status") or {}).get("robustness") != "healthy"]
        if pending:
            if now - ref["returned_at"] > 1800:
                return "failed", 90, ("Host rebooted, but volumes did not become healthy within 30 minutes: " +
                                      ", ".join(pending[:4]))
            return "running", 90, f"Host is Ready; waiting for {len(pending)} volume(s) to become healthy: " + ", ".join(pending[:4])
        return "succeeded", 100, "Host is Ready and affected Longhorn volumes are healthy"
    if now - ref.get("started_epoch", now) > 600:
        return "failed", 30, "Host did not complete the requested power transition within 10 minutes"
    if ref.get("saw_down"):
        return "running", 75, "Host returned, but Kubernetes has not reported a new boot ID yet"
    if ref.get("helper_pod"):
        try:
            helper = kget(f"/api/v1/namespaces/lab/pods/{ref['helper_pod']}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            phase = (helper.get("status") or {}).get("phase", "Pending")
            conditions = (helper.get("status") or {}).get("containerStatuses") or []
            waiting = [((c.get("state") or {}).get("waiting") or {}) for c in conditions]
            reason = next((c.get("reason") for c in waiting if c.get("reason")), "")
            if phase == "Failed" or reason in ("ErrImagePull", "ImagePullBackOff", "CreateContainerError", "CreateContainerConfigError"):
                return "failed", 20, f"Host power helper failed ({reason or phase}); host remains cordoned"
            return "running", 35 if phase == "Running" else 20, f"Power helper {phase.lower()}{': ' + reason if reason else ''}; waiting for host transition"
    return "running", 20, "Waiting for the host to leave Ready"
