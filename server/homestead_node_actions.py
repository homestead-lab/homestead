"""
Node actions: the etcd quorum report, the checks before cordoning, draining,
rebooting or shutting down a host, and the actions themselves.

Split by feature from homestead_lifecycle (#623), moved unchanged. Names that
server.py binds there are read through it (L.name), so a replacement there
still applies; read and replace the names defined here, here.
"""
import time
import urllib.error
import homestead_names as NAMES
import homestead_maintenance as MAINTENANCE


# --------------------------------------------------------------- node guards
def quorum_report():
    """How many etcd members we have, and whether we can afford to lose one."""
    nodes = L.kget("/api/v1/nodes").get("items", [])
    etcd, ready_etcd = [], []
    for n in nodes:
        labels = n["metadata"].get("labels", {})
        if "node-role.kubernetes.io/etcd" not in labels:
            continue
        name = n["metadata"]["name"]
        etcd.append(name)
        conds = {c["type"]: c["status"] for c in n["status"].get("conditions", [])}
        if conds.get("Ready") == "True":
            ready_etcd.append(name)
    total = len(etcd)
    need = total // 2 + 1
    return {"members": etcd, "ready": ready_etcd, "total": total,
            "quorum_needs": need, "can_lose": max(0, len(ready_etcd) - need)}


def node_action_check(node, action):
    """Refuse anything that would break etcd quorum. Returns (ok, reason, report)."""
    rep = quorum_report()
    is_etcd = node in rep["members"]
    if action in ("cordon", "uncordon"):
        return True, "", rep
    if not is_etcd:
        return True, "", rep
    if rep["can_lose"] < 1:
        return (False,
                f"Refusing: {len(rep['ready'])} of {rep['total']} etcd members are ready and "
                f"quorum needs {rep['quorum_needs']}. Taking {node} down would lose the cluster.",
                rep)
    return True, "", rep


def set_cordon(node, unschedulable):
    L.ksend("PATCH", f"/api/v1/nodes/{node}",
          {"spec": {"unschedulable": bool(unschedulable)}},
          ctype="application/merge-patch+json")
    L._bust("nodes", "ov", "node:", "impact:")
    return {"ok": True, "node": node, "cordoned": bool(unschedulable)}


LONGHORN_DRAIN_WAIT = 300   # seconds a drain waits on Longhorn's instance managers alone


def drain(node, grace=30, include_system=False, reviewed_pods=None, wait=False, progress=None, resumed=False):
    """Evict workload pods off a node. DaemonSets and mirror pods are skipped
    because the scheduler will simply recreate them on the same node."""
    pods = MAINTENANCE.items(L.kget, "/api/v1/pods")
    if reviewed_pods is not None:
        here = [p for p in pods if (p.get("spec") or {}).get("nodeName") == node]
        now = [tuple(row) for row in MAINTENANCE.pod_snapshot(here)]
        reviewed = [tuple(row) for row in reviewed_pods]
        # A resumed drain finds some reviewed pods already gone; none may be new.
        changed = not set(now) <= set(reviewed) if resumed else now != reviewed
        if changed or any(not row[2] for row in reviewed):
            raise ValueError("Host pods changed after review; power was not sent. Review the host again")
    evicted, skipped, targets = [], [], {}
    for p in pods:
        if p["spec"].get("nodeName") != node:
            continue
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        owners = p["metadata"].get("ownerReferences") or []
        if any(o.get("kind") == "DaemonSet" and o.get("controller") is True for o in owners):
            skipped.append(f"{ns}/{name} (daemonset)")
            continue
        if p["metadata"].get("annotations", {}).get("kubernetes.io/config.mirror"):
            skipped.append(f"{ns}/{name} (static)")
            continue
        if ns in L.SYS_NS and not include_system:
            skipped.append(f"{ns}/{name} (system)")
            continue
        targets[f"{ns}/{name}"] = p
    # Evict consumers first so Longhorn can detach their engines and release
    # its instance-manager budget. Never delete pods or edit that budget.
    targets = dict(sorted(targets.items(), key=lambda row: (MAINTENANCE.longhorn_instance_manager(row[1]), row[0])))
    frozen = MAINTENANCE.pod_snapshot([p for p in pods if (p.get("spec") or {}).get("nodeName") == node])
    started = time.monotonic()
    while targets:
        retrying = []
        for identity, p in targets.items():
            if identity in evicted or (p.get("metadata") or {}).get("deletionTimestamp"):
                continue
            ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
            try:
                L.ksend("POST", f"/api/v1/namespaces/{ns}/pods/{name}/eviction",
                      {"apiVersion": "policy/v1", "kind": "Eviction",
                       "metadata": {"name": name, "namespace": ns},
                       "deleteOptions": {"gracePeriodSeconds": int(grace),
                                         **({"preconditions": {"uid": p["metadata"]["uid"]}} if p["metadata"].get("uid") else {})}})
                evicted.append(identity)
            except urllib.error.HTTPError as e:
                if wait and e.code == 429 and MAINTENANCE.longhorn_instance_manager(p):
                    retrying.append(identity)
                elif wait and e.code == 404:
                    # It may have left between LIST and POST. The next complete
                    # inventory must confirm that before power can be sent.
                    evicted.append(identity)
                else:
                    skipped.append(f"{identity} (HTTP {e.code})")
        if not wait or any("(HTTP " in item for item in skipped):
            break
        live = [p for p in MAINTENANCE.items(L.kget, "/api/v1/pods")
                if (p.get("spec") or {}).get("nodeName") == node]
        changed = [row for row in MAINTENANCE.pod_snapshot([p for p in live if not MAINTENANCE.own_host_command(p)])
                   if row not in frozen]
        if changed:
            # Which: a pod that arrived, or one whose spec or labels changed -
            # by name, so the next look starts from it rather than a guess.
            before = {(row[0], row[1]) for row in frozen}
            which = ", ".join(f"{ns}/{name} {'changed' if (ns, name) in before else 'arrived'}"
                              for ns, name, *_ in changed[:4])
            raise ValueError(f"Host remains cordoned; pods changed during drain ({which}). "
                             "Power was not sent; review the host again")
        remaining = {p["metadata"]["namespace"] + "/" + p["metadata"]["name"]: p for p in live
                     if MAINTENANCE.drainable(p)}
        targets = {name: remaining[name] for name in targets if name in remaining}
        if not targets:
            break
        waiting = [name for name in retrying if name in targets]
        reason = ("Longhorn still prevents eviction" if waiting else "Pods have not left the host")
        names = ", ".join(sorted(waiting or targets)[:6])
        # Longhorn lets its instance managers go once their engines have
        # moved off; V2 engines take longer than V1. When nothing but those
        # is left, waiting longer is safe - it never forces anything.
        limit = LONGHORN_DRAIN_WAIT if waiting and len(waiting) == len(targets) else 120
        if time.monotonic() >= started + limit:
            raise ValueError(f"Host remains cordoned; drain timed out after {limit // 60} minutes. {reason}: {names}. "
                             "Power was not sent; inspect Longhorn volumes, disruption budgets and pod events")
        if progress:
            progress("draining", 10, ("Waiting for Longhorn to allow storage pod eviction: " if waiting else
                                     "Waiting for drained pods to leave: ") + names + "; power has not been sent")
        time.sleep(2)
    L._bust("wl", "ov", "nodes", "flow", "impact:")
    return {"ok": True, "node": node, "evicted": evicted, "skipped": skipped}


def node_power(node, action, drain_first=True, before_send=None, reviewed_pods=None, progress=None, force=False, planned_outage=False,
               resumed=False, hold=None, send=None):
    """Reboot or shut down a host.

    Kubernetes cannot do this. We schedule a one-shot privileged pod pinned to
    the node that enters the host's namespaces and asks systemd. The pod is the
    only way in without SSH credentials.
    """
    if not L.NODE_POWER_ENABLED:
        raise PermissionError(
            "Host power control is disabled. It needs a privileged helper pod that enters the "
            "host namespaces. This installation has disabled it. Set ENABLE_NODE_POWER=true on the Homestead "
            "Deployment to turn it on. Cordon and drain work regardless.")
    if action not in ("reboot", "poweroff"):
        raise ValueError("action must be reboot or poweroff")
    if before_send is None or (not force and not planned_outage and (reviewed_pods is None or not drain_first)):
        raise ValueError("A reviewed drain and fresh pre-power check are required; power was not sent")
    report = progress or (lambda *args, **kwargs: None)
    steps = []
    if planned_outage:
        if force:
            raise ValueError("A planned outage cannot also be a forced action")
        report("verifying", 15, "Rechecking the planned single-host cluster outage")
        # The callback requires a fresh matching one-host inventory and review
        # token. Eviction would remove Homestead before it could send power.
        rep = quorum_report()
        before_send()
        if hold and hold():
            steps.append("stopped the apps and VMs that wait for the host")
        steps.append("planned whole-cluster outage: no cordon or drain")
        # send: the caller's own last step - on a single host, a helper that
        # waits for Homestead to stop before it powers the host.
        return (send or _send_power)(node, action, steps, rep, report)
    if force:
        # Overridden by an admin: no cordon or drain - which on a one-node
        # cluster would evict Homestead before it could send anything, and
        # leave the host cordoned after it came back. The host's own systemd
        # still stops everything in order.
        rep = quorum_report()
        report("verifying", 15, "Forced: checking the host has not changed since the review")
        before_send()
        steps.append("forced: no cordon or drain")
        return _send_power(node, action, steps, rep, report)
    ok, why, rep = node_action_check(node, action)
    if not ok:
        raise PermissionError(why)

    report("cordoning", 5, "Cordoning host; power has not been sent")
    set_cordon(node, True)
    steps.append("cordoned")
    held = bool(hold and hold())
    if held:
        steps.append("stopped what waits for the host and moved its VMs")
    if drain_first:
        report("draining", 10, "Evicting workload and system pods through disruption budgets; power has not been sent")
        # What was stopped or moved has gone from the reviewed pods; nothing may be new.
        d = drain(node, include_system=True, reviewed_pods=reviewed_pods, wait=True, progress=report, resumed=resumed or held)
        steps.append(f"drained {len(d['evicted'])} pod(s)")
        refused = [item for item in d["skipped"] if "(HTTP " in item]
        if refused:
            raise ValueError("Host remains cordoned; drain was refused for " + ", ".join(refused[:6]) +
                             ". Resolve the pod or disruption budget before retrying power control")

    # A long drain can outlive the reviewed quorum, VM and replica state.
    report("verifying", 15, "Drain completed; rechecking quorum, VMs, pods and volume replicas before power")
    ok, why, rep = node_action_check(node, action)
    if not ok:
        raise PermissionError("Host remains cordoned; power was not sent: " + why)
    before_send()
    return _send_power(node, action, steps, rep, report)


def _send_power(node, action, steps, rep, report):
    cmd = "systemctl reboot" if action == "reboot" else "systemctl poweroff"
    pod_name = f"homestead-{action}-{node.split('.')[0][-12:]}-{int(time.time()) % 100000}"
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": "lab",
                     "labels": NAMES.labels("node-power")},
        "spec": {
            "nodeName": node, "hostPID": True, "hostIPC": True, "hostNetwork": True,
            "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
            "tolerations": [{"operator": "Exists"}],
            "containers": [{
                "name": "power",
                "image": "alpine:3.24",
                "command": ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--",
                            "sh", "-c", f"sleep 3; {cmd}"],
                "securityContext": {"privileged": True},
            }],
        },
    }
    # Persist the helper identity BEFORE submitting. A restart observes it;
    # it never guesses whether POST succeeded and sends a second command.
    report("sending", 20, "Submitting power helper; command outcome must be observed",
           helper_pod=pod_name, helper_namespace="lab", started_epoch=time.time())
    receipt = L.ksend("POST", "/api/v1/namespaces/lab/pods", body)
    meta = (receipt or {}).get("metadata") or {}
    if not meta.get("uid") or meta.get("name") != pod_name or meta.get("namespace") != "lab":
        raise ValueError("Power helper creation was not confirmed; inspect the recorded helper before retrying")
    report("observing", 20, "Power helper submitted; observing host transition", helper_uid=meta["uid"])
    steps.append(f"scheduled {action} helper ({pod_name})")
    L._bust()
    return {"ok": True, "node": node, "action": action, "steps": steps,
            "helper_pod": pod_name, "quorum": rep}


# homestead_lifecycle holds the bound state and imports this module, so it is imported last.
import homestead_lifecycle as L  # noqa: E402
