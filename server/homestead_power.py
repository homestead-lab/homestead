"""Fail-closed host power review and durable reboot/shutdown observation."""
import hashlib
import json
import time
import urllib.error
import homestead_maintenance as MAINTENANCE

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
kget = impact = quorum = None
power_enabled = lambda: False


def bind(_kget, _impact, _quorum, _power_enabled):
    global kget, impact, quorum, power_enabled
    kget, impact, quorum, power_enabled = _kget, _impact, _quorum, _power_enabled


def _items(path, absent_ok=False):
    try:
        return MAINTENANCE.items(kget, path)
    except urllib.error.HTTPError as error:
        if absent_ok and error.code == 404:
            return None
        raise


def _ready(node):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in ((node.get("status") or {}).get("conditions") or []))


def plan(node, action, force=False, volume_names=()):
    """What rebooting or shutting a host down would do, and what stops it.

    A verified single-host cluster uses an acknowledged planned outage.
    Other stops are checks an admin may override - quorum, VMs still
    running, a disruption budget, storage that cannot be
    read - and some are not: without the host's identity, with power control
    off, with an earlier helper still at work, or on a host that is not Ready,
    nothing can be sent safely. force turns the first kind into listed
    consequences; a forced action then skips cordon and drain and asks the
    host's own systemd to reboot or power off, as its power button would."""
    if action not in ("reboot", "poweroff"):
        raise ValueError("action must be reboot or poweroff")
    node_obj = kget(f"/api/v1/nodes/{node}")
    place = impact(node)
    control = quorum()
    replicas = _items(f"{LH}/replicas", absent_ok=True)
    volumes = _items(f"{LH}/volumes", absent_ok=True) if replicas is not None else None
    pods = _items("/api/v1/pods")
    nodes = _items("/api/v1/nodes")
    # A sole etcd member can still have worker nodes. Only a complete, matching
    # one-host inventory permits the planned whole-cluster outage path.
    planned_outage = (not force and bool(node_obj.get("metadata", {}).get("uid")) and len(nodes) == 1 and _ready(nodes[0]) and
                      nodes[0].get("metadata", {}).get("name") == node and
                      nodes[0].get("metadata", {}).get("uid") == node_obj.get("metadata", {}).get("uid") and
                      control.get("members", []) in ([], [node]))
    ready_hosts = {n.get("metadata", {}).get("name") for n in nodes if _ready(n)}
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
    for name in sorted(set(by_volume) | set(volume_names)):
        copies = by_volume.get(name, [])
        if name not in volume_names and not any(host == node for host, _ in copies):
            continue
        # Two replicas on one host are not two failure domains. A stale
        # running replica on an offline host is not a verified surviving copy.
        elsewhere = len({host for host, healthy in copies if healthy and host != node and host in ready_hosts})
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
    hard, soft = [], []            # soft: checks an admin may override
    node_uid = (node_obj.get("metadata") or {}).get("uid", "")
    if not node_uid:
        hard.append("Host identity is unavailable; refresh before issuing power control")
    if any((p.get("metadata", {}).get("labels") or {}).get("homestead.io/task") == "node-power" and
           p.get("status", {}).get("phase") not in ("Succeeded", "Failed") for p in pods_here):
        hard.append("An earlier power helper is still active on this host; inspect it before retrying")
    maintenance = {"budgets": [], "local_storage": [], "blockers": []}
    try:
        maintenance = MAINTENANCE.inventory(kget, pods_here, draining=not planned_outage)
        soft.extend(maintenance["blockers"])
    except Exception:
        soft.append("Drain/PDB or attached-storage inventory is unavailable or incomplete; review cannot be verified")
    if not power_enabled():
        hard.append("Host power control is disabled (ENABLE_NODE_POWER is off)")
    members = control.get("members", [])
    if not planned_outage and node in members and control.get("can_lose", 0) < 1:
        soft.append("This host is the cluster's only etcd member: the cluster, Homestead with it, is away until it is back"
                    if len(members) == 1 else "Shutting down this etcd member would lose quorum")
    if not _ready(node_obj) or (len(nodes) == 1 and nodes[0].get("metadata", {}).get("name") == node and not _ready(nodes[0])):
        hard.append("The host is not Ready; investigate it before issuing a new power command")
    if vm_rows:
        soft.append("Running VMs are on this host; migrate or stop them and review again")
    if storage_unknown:
        soft.append("Storage replica inventory is unavailable; volume impact cannot be verified")
    blockers = hard if force else hard + soft
    warnings = []
    if not force and not planned_outage:
        warnings.extend(maintenance.get("waiting", []))
    if planned_outage:
        warnings.append("Planned whole-cluster outage: all applications, storage and Homestead stop with this host. "
                        "Homestead does not cordon or evict pods; the host's systemd receives the power request. "
                        "Shutdown requires console or physical access to power the host on again.")
    if force and soft:
        warnings.append("Forced: no cordon or drain - pods and VMs on it stop with the host, and come back when it does "
                        "(or, on other hosts, once Kubernetes gives up on this one). Overridden: " + "; ".join(soft))
    warnings.append("DaemonSets and static pods remain on the host; their services stop during the outage. Survivor capacity and external storage dependencies are not fully simulated")
    if storage_unknown:
        warnings.append("Longhorn replica inventory is unavailable; volume safety cannot be confirmed")
    if place.get("stranded"):
        warnings.append(f"{len(place['stranded'])} workload(s) have no eligible failover host")
    if affected:
        warnings.append(f"{len(affected)} volume(s) lose a replica until this host returns or Longhorn rebuilds")
    if not affected and not storage_unknown:
        warnings.append("Non-Longhorn or host-local volumes are not covered by replica checks")
    boot_id = ((node_obj.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
    if action == "reboot" and not boot_id:
        blockers.append("The host boot ID is unavailable; reboot completion cannot be verified")
    drain_pods = MAINTENANCE.pod_snapshot(pods_here)
    review = {"node": node, "node_uid": node_uid, "action": action, "boot_id": boot_id,
              "workloads": sorted((w.get("ns", ""), w.get("name", ""), bool(w.get("stranded")))
                                  for w in place.get("workloads", [])),
              "volumes": [(v["name"], v["risk"], v["healthy_elsewhere"], v["robustness"]) for v in affected],
              "pods": sorted(((p.get("metadata") or {}).get("namespace", ""),
                              (p.get("metadata") or {}).get("name", "")) for p in pods_here),
              "storage_unknown": storage_unknown, "vms": vm_rows,
              "maintenance": maintenance,
              "drain_pods": drain_pods,
              "quorum": control.get("can_lose", 0), "force": bool(force), "planned_outage": planned_outage}
    token = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[:20]
    return {"node": node, "node_uid": node_uid, "action": action, "review_token": token, "boot_id": boot_id,
            "quorum": control, "workloads": place.get("workloads", []),
            "stranded": place.get("stranded", []), "volumes": affected,
            "vms": vm_rows, "pods": len(pods_here), "storage_unknown": storage_unknown,
            "maintenance": maintenance,
            "drain_pods": drain_pods,
            "requires_data_ack": storage_unknown or bool(maintenance["local_storage"]) or any(v["risk"] in ("unavailable", "single-copy") for v in affected),
            "blockers": blockers, "warnings": warnings, "ready": not blockers,
            "overridable": soft, "hard_blockers": hard, "force": bool(force), "planned_outage": planned_outage}


def recheck_planned_outage(original):
    """Never turn a changed or multi-host cluster into an approved outage."""
    fresh = plan(original["node"], original["action"])
    if not fresh["ready"] or not fresh["planned_outage"]:
        raise ValueError("Power was not sent: planned single-host outage is no longer safe to send; review again")
    if fresh["review_token"] != original["review_token"]:
        raise ValueError("Host or cluster impact changed since the review; power was not sent")


def recheck_forced(original):
    """A forced action sends no drain, but still never to a host that changed
    identity, rebooted, or grew a new hard stop since it was reviewed."""
    fresh = plan(original["node"], original["action"], force=True)
    if not fresh["ready"]:
        raise ValueError("Power was not sent: " + "; ".join(fresh["blockers"]))
    if fresh["boot_id"] != original["boot_id"] or fresh["node_uid"] != original["node_uid"]:
        raise ValueError("Host identity changed since the review; power was not sent")


def recheck_after_drain(original):
    """Never send power using the pre-drain storage/quorum/VM snapshot."""
    # Eviction may remove the local replica entirely. Keep checking every
    # reviewed volume, even when it no longer appears on the drained host.
    fresh = plan(original["node"], original["action"], volume_names=[v["name"] for v in original["volumes"]])
    if not fresh["ready"]:
        raise ValueError("Host remains cordoned; power was not sent: " + "; ".join(fresh["blockers"]))
    before = {v["name"]: v for v in original["volumes"]}
    for volume in fresh["volumes"]:
        old = before.get(volume["name"])
        # Stopping this host's replica can make a healthy volume degraded.
        # That reviewed redundancy loss is expected only if its verified
        # surviving copies remain available on other Ready hosts.
        expected_degradation = (old and old["healthy_elsewhere"] > 0 and old["robustness"] == "healthy" and
                                volume["robustness"] == "degraded")
        if (not old or volume["healthy_elsewhere"] < old["healthy_elsewhere"] or
                (volume["robustness"] != old["robustness"] and not expected_degradation)):
            raise ValueError("Host remains cordoned; volume impact changed during drain. Power was not sent; review again")
    if fresh["boot_id"] != original["boot_id"] or fresh["node_uid"] != original["node_uid"]:
        raise ValueError("Host identity changed during drain; power was not sent")
    remaining = [p for p in _items("/api/v1/pods") if (p.get("spec") or {}).get("nodeName") == original["node"] and MAINTENANCE.drainable(p)]
    if remaining:
        raise ValueError("Host remains cordoned; workload pods remain or appeared during drain. Power was not sent")


def status(item):
    """Observe a power command without treating a lost API connection as success."""
    ref = item["ref"]
    scheduling = "Scheduling was left unchanged" if ref.get("planned_outage") else "It remains cordoned"
    now = time.time()
    phase = ref.get("phase")
    if phase in ("reviewed", "cordoning", "draining", "verifying"):
        if now - ref.get("phase_at", ref.get("started_epoch", now)) > 240:
            return "failed", item.get("progress", 0), "Maintenance stopped before power was sent; inspect the host and its cordon state before retrying"
        return "running", item.get("progress", 0), item.get("message", "Preparing host maintenance")
    node = kget(f"/api/v1/nodes/{ref['node']}")
    if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") != ref["node_uid"]:
        return "failed", item.get("progress", 0), "Host identity changed; this replacement cannot confirm the power request. Inspect the original host; no command was retried"
    up = _ready(node)
    boot = ((node.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
    if not up:
        ref["saw_down"] = True
        ref.setdefault("down_at", now)
        if ref["action"] == "poweroff":
            # NotReady can mean a network partition, not a powered-off host.
            # Never turn that observation into a green shutdown success.
            if now - ref.get("started_epoch", now) > 600:
                return "failed", 60, "Shutdown could not be verified. Host is NotReady; check its console or physical power before retrying. " + scheduling
            return "running", 60, "Host is NotReady, not confirmed powered off. Check its console or physical power; no command will be retried"
        if now - ref.get("started_epoch", now) > 600:
            return "failed", 60, "Host did not return Ready within 10 minutes; inspect the host. " + scheduling
        return "running", 60, "Host is NotReady; waiting to confirm shutdown or return"
    if ref["action"] == "poweroff" and ref.get("saw_down"):
        return "failed", 90, "Host returned Ready after shutdown was requested; check its power state"
    if ref["action"] == "reboot" and ref.get("boot_id") and boot and boot != ref["boot_id"]:
        ref.setdefault("returned_at", now)
        volume_names = ref.get("volumes") or []
        if not volume_names:
            return "succeeded", 100, "Host returned Ready with a new boot ID. " + scheduling + "; check workloads"
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
        return "succeeded", 100, "Host is Ready and affected Longhorn volumes are healthy. " + scheduling + "; workload recovery is not yet verified"
    if now - ref.get("started_epoch", now) > 600:
        return "failed", 30, "Power transition was not verified within 10 minutes. A helper may still run; inspect its events and logs before any new request. Host remains cordoned"
    if ref.get("saw_down"):
        return "running", 75, "Host returned, but Kubernetes has not reported a new boot ID yet"
    if ref.get("helper_pod"):
        if not ref.get("helper_uid"):
            return "running", 20, "Power helper receipt is unconfirmed; observing the host only. Inspect the recorded helper before retrying"
        try:
            helper = kget(f"/api/v1/namespaces/{ref.get('helper_namespace', 'lab')}/pods/{ref['helper_pod']}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            if (helper.get("metadata") or {}).get("uid") != ref["helper_uid"]:
                return "failed", 20, "Power helper was replaced; its result cannot confirm this request. Inspect the host before retrying"
            phase = (helper.get("status") or {}).get("phase", "Pending")
            conditions = (helper.get("status") or {}).get("containerStatuses") or []
            waiting = [((c.get("state") or {}).get("waiting") or {}) for c in conditions]
            reason = next((c.get("reason") for c in waiting if c.get("reason")), "")
            if phase == "Failed":
                return "failed", 20, f"Host power helper failed ({reason or phase}); host remains cordoned"
            if reason:
                return "running", 20, f"Power helper waiting: {reason}. It may retry automatically; inspect Recent jobs logs, do not send another power request"
            return "running", 35 if phase == "Running" else 20, f"Power helper {phase.lower()}{': ' + reason if reason else ''}; waiting for host transition"
    return "running", 20, "Waiting for the host to leave Ready"
