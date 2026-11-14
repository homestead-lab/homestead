"""Fail-closed host power review and durable reboot/shutdown observation.

The same review stands for maintenance a person declares (#535 part 2), which
drains a host and sends no power."""
import hashlib
import json
import time
import urllib.error
import homestead_drain_blockers as DRAIN_BLOCKERS
import homestead_history as HISTORY
import homestead_kube as KUBE
import homestead_lhcapacity as LHCAP
import homestead_maintenance as MAINTENANCE
import homestead_operations as OPS
import homestead_os_rollout as ROLLOUT
import homestead_power_hold as HOLD
import homestead_restart_ready as RESTART_READY
import homestead_whole_copy as WHOLE

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
kget = impact = quorum = None
power_enabled = lambda: False


def bind(_kget, _impact, _quorum, _power_enabled):
    global kget, impact, quorum, power_enabled
    kget, impact, quorum, power_enabled = _kget, _impact, _quorum, _power_enabled


def _items(path, absent_ok=False):
    """The rows of a whole list; None when absent_ok and it isn't there (404)."""
    if not absent_ok:
        return MAINTENANCE.items(kget, path)
    return KUBE.get_optional(path, read=lambda where: MAINTENANCE.items(kget, where))


def _ready(node):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in ((node.get("status") or {}).get("conditions") or []))


def plan(node, action, force=False, volume_names=(), after_drain=False):
    """What rebooting or shutting a host down would do, and what stops it.

    A verified single-host cluster uses an acknowledged planned outage.
    Other stops are checks an admin may override - quorum, VMs still
    running, a disruption budget, storage that cannot be
    read - and some are not: without the host's identity, with power control
    off, with an earlier helper still at work, or on a host that is not Ready,
    nothing can be sent safely. force turns the first kind into listed
    consequences; a forced action then skips cordon and drain and asks the
    host's own systemd to reboot or power off, as its power button would.

    action "maintenance" is reviewed as a reboot is, except that power control
    isn't asked, as nothing is sent; it has no force, which would mean skipping
    the very drain it is for; and on a single host nothing is stopped."""
    if action not in ("reboot", "poweroff", "maintenance"):
        raise ValueError("action must be reboot, poweroff or maintenance")
    maintain = action == "maintenance"
    if maintain and force:
        raise ValueError("Maintenance has no force: resolve what stops it, or cordon the host by hand")
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
    # Whether a StatefulSet here may wait depends on its retention policy: read them when one runs here.
    stateful = any(o.get("kind") == "StatefulSet" for p in pods if (p.get("spec") or {}).get("nodeName") == node
                   for o in (p.get("metadata") or {}).get("ownerReferences") or [])
    hold = HOLD.candidates(node, pods, vmis,
                           {((r.get("metadata") or {}).get("namespace"), (r.get("metadata") or {}).get("name")): r
                            for r in _items("/apis/apps/v1/replicasets", absent_ok=True) or []},
                           ready_hosts, {(w.get("ns"), w.get("name")): w.get("eligible") or []
                                         for w in place.get("workloads", [])},
                           single_host=planned_outage,
                           statefulsets=_items("/apis/apps/v1/statefulsets") if stateful else ())
    if maintain and planned_outage:
        hold = []
    storage_unknown = replicas is None or volumes is None
    # Each attached volume's engine says which replicas are whole (RW) and
    # which are still being rebuilt (WO): a rebuilding copy was counted as a
    # healthy one elsewhere, and the drain then stalled on Longhorn.
    engines = _items(f"{LH}/engines", absent_ok=True) or []
    rebuilding, attached = {}, set()
    for engine in engines:
        status = engine.get("status") or {}
        if status.get("currentState") != "running":
            continue
        attached.add((engine.get("spec") or {}).get("volumeName"))
        progress = [r.get("progress") for r in (status.get("rebuildStatus") or {}).values() if r.get("isRebuilding")]
        if progress:
            rebuilding[(engine.get("spec") or {}).get("volumeName")] = min(p or 0 for p in progress)
    # One rule for a whole copy (homestead_whole_copy): a detached volume's
    # copies are stopped, not lost, so they count; hosts are checked below.
    whole = WHOLE.judge(engines, detached_ok=True, volumes=volumes or ())
    by_volume = {}
    for row in replicas or []:
        spec, status = row.get("spec") or {}, row.get("status") or {}
        name = spec.get("volumeName")
        if name:
            by_volume.setdefault(name, []).append((spec.get("nodeID"), whole(row)))
            if status.get("currentState") == "running":
                attached.add(name)
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
                         "robustness": (volume.get("status") or {}).get("robustness", "unknown"),
                         "last_copy_here": not elsewhere and any(host == node and healthy for host, healthy in copies),
                         "copies_wanted": int((volume.get("spec") or {}).get("numberOfReplicas") or 0),
                         "detached": name not in attached,
                         "state": (volume.get("status") or {}).get("state", ""),
                         **({"rebuilding_pct": rebuilding[name]} if name in rebuilding else {})})
    affected.sort(key=lambda row: ({"unavailable": 0, "single-copy": 1, "resync": 2}[row["risk"]], row["claim"]))
    vm_rows = sorted({(v.get("metadata") or {}).get("namespace", "") + "/" +
                      (v.get("metadata") or {}).get("name", "") for v in vmis
                      if (v.get("status") or {}).get("nodeName") == node})
    pods_here = [p for p in pods if (p.get("spec") or {}).get("nodeName") == node]
    hard, soft = [], []            # soft: checks an admin may override
    node_uid = (node_obj.get("metadata") or {}).get("uid", "")
    if not node_uid:
        hard.append("The host could not be identified. Refresh before restarting or shutting it down.")
    if any((p.get("metadata", {}).get("labels") or {}).get("homestead.io/task") == "node-power" and
           p.get("status", {}).get("phase") not in ("Succeeded", "Failed") for p in pods_here):
        hard.append("An earlier power helper is still running on this host. Check it before trying again.")
    maintenance = {"budgets": [], "local_storage": [], "blockers": []}
    try:
        maintenance = MAINTENANCE.inventory(kget, pods_here, draining=not planned_outage)
        soft.extend(maintenance["blockers"])
    except Exception:
        soft.append("The drain, disruption budget or attached storage information is unavailable or incomplete, so the review cannot be checked")
    if not power_enabled() and not maintain:
        hard.append("Host power control is disabled (ENABLE_NODE_POWER is off)")
    if maintain and HISTORY.declared_for(node):
        hard.append("This host is already in maintenance")
    if maintain:
        # A rollout already on this host would restart it and take the cordon off.
        try:
            if ROLLOUT.current_host() == node:
                hard.append("An OS update of every host is working on this host. Wait for it to move on, or stop it.")
        except Exception:
            hard.append("Whether an OS update is working on this host could not be read")
    members = control.get("members", [])
    if not planned_outage and node in members and control.get("can_lose", 0) < 1:
        soft.append("This host is the cluster's only etcd member: the cluster, Homestead with it, is away until it is back"
                    if len(members) == 1 else "Shutting down this etcd member would lose quorum")
    if not _ready(node_obj) or (len(nodes) == 1 and nodes[0].get("metadata", {}).get("name") == node and not _ready(nodes[0])):
        hard.append("The host is not ready. Maintenance must be declared before it goes down." if maintain else
                    "The host is not ready. Check it before a new restart or shutdown.")
    loose = [f"{i['ns']}/{i['name']}" for i in hold if i["kind"] == "VirtualMachine" and not i["options"]]
    if loose:
        soft.append("Running VMs are on this host that no VirtualMachine manages, so Homestead can neither move nor "
                    "stop them: " + ", ".join(loose) + ". Stop them by hand and review again")
    # One that can neither move nor wait: stopping it would delete its volumes (#1222).
    soft.extend(HOLD.stop_blocker(f"{i['ns']}/{i['name']}", HOLD.BEFORE)
                for i in hold if i["kind"] == "StatefulSet" and not i["options"])
    if storage_unknown:
        soft.append("The list of volume copies is unavailable, so the effect on volumes cannot be checked")
    # Before a drain only: one that finished had Longhorn's agreement. A
    # policy that cannot be read is not guessed at; the data acknowledgement stays.
    settings = None if planned_outage or after_drain else _items(f"{LH}/settings", absent_ok=True)
    if settings:
        policy = next((s.get("value") for s in settings if (s.get("metadata") or {}).get("name") == "node-drain-policy"),
                      "block-if-contains-last-replica")
        if policy in ("block-if-contains-last-replica", "block-for-eviction-if-contains-last-replica", "block-for-eviction"):
            for v in affected:
                if not v.get("last_copy_here"):
                    continue
                if "rebuilding_pct" in v:
                    soft.append(f"{v['claim']}: this host holds its only complete copy while another is rebuilt "
                                f"({v['rebuilding_pct']}% done). Longhorn does not let the host drain until that finishes, so review again then.")
                elif v.get("detached") and v.get("copies_wanted") != 1:
                    soft.append(f"{v['claim']} is detached with its only copy on this host, and Longhorn rebuilds copies only "
                                "while a volume is attached. It will not let the host drain: start what uses the volume "
                                "so a second copy is built, or restart without draining (Force)")
                elif v.get("copies_wanted") == 1:
                    soft.append(f"{v['claim']} keeps one copy, on this host. Longhorn will not let the host drain: "
                                "give it a second copy in Volumes, or restart without draining (Force)")
                else:
                    soft.append(f"{v['claim']}: this host holds its only complete copy. Longhorn will not let the host drain "
                                "until another copy is rebuilt, so review again then.")
    if maintain:
        soft = [why.replace(", or restart without draining (Force)", "") for why in soft]
    blockers = hard if force else hard + soft
    warnings = []
    if not force and not planned_outage:
        warnings.extend(maintenance.get("waiting", []))
    if action == "reboot" and not after_drain and not planned_outage:
        # The last host restart not settled yet (#1290): said, never a block.
        try:
            settled = RESTART_READY.warning(RESTART_READY.state(kget, OPS.stored(), volumes=volumes, vmis=vmis))
        except Exception:
            settled = ""
        if settled:
            warnings.append(settled)
    if maintain and planned_outage:
        warnings.append("This is the cluster's only host, so nothing is stopped or moved: no cordon and no drain. "
                        "It only records the maintenance. Anything then done to the host affects the whole cluster.")
    elif planned_outage:
        warnings.append("Planned whole-cluster outage: its apps and VMs stop first. Homestead stops them so their "
                        "volumes are released in order, then asks the host's systemd to " + ("restart" if action == "reboot" else "shut down")
                        + ". They start again when it is back. "
                        + ("" if action == "reboot" else "Turning it on again needs console or physical access."))
    if force and soft:
        warnings.append("Forced: apps and VMs on it stop with the host. There is no cordon or drain. They come back "
                        "when it does, or on other hosts once Kubernetes stops waiting for this one. Overridden: " + "; ".join(soft))
    warnings.append("DaemonSets and static pods stay on the host, so their services stop if it goes down. The capacity of the other hosts and storage outside the cluster are not fully checked."
                    if maintain else
                    "DaemonSets and static pods stay on the host, so their services stop during the outage. The capacity of the other hosts and storage outside the cluster are not fully checked.")
    if storage_unknown:
        warnings.append("The list of Longhorn volume copies is unavailable. The safety of the volumes cannot be confirmed.")
    if place.get("stranded"):
        waits = {(i["ns"], i["name"]) for i in hold if i["options"] == ["wait"]}
        loose = [w for w in place["stranded"] if (w.get("ns"), w.get("name")) not in waits]
        if loose or force:
            warnings.append(f"{len(place['stranded'])} workload(s) have no eligible failover host")
        elif not planned_outage:
            warnings.append(f"{len(place['stranded'])} workload(s) have no other host they can run on: "
                            "they stop and wait for this one")
    if affected:
        warnings.append(f"{len(affected)} volume(s) lose a replica until this host returns or Longhorn rebuilds")
    if not maintain and volumes is not None:
        # Said, never a block: a drained restart detaches them cleanly.
        try:
            lone = LHCAP.salvage_blocked(volumes, replicas, _items(f"{LH}/nodes", absent_ok=True) or [], LHCAP._own_claims())
            unclean = LHCAP.unclean_restart_warning(lone, node)
        except Exception:
            unclean = ""   # the alert says it too
        if unclean:
            warnings.append(unclean)
    if not affected and not storage_unknown:
        warnings.append("Non-Longhorn or host-local volumes are not covered by replica checks")
    boot_id = ((node_obj.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
    if action == "reboot" and not boot_id:
        blockers.append("The host's boot ID is unavailable, so the end of the restart cannot be confirmed")
    drain_pods = MAINTENANCE.pod_snapshot(pods_here)
    # What would keep the drain from finishing, said before anything is
    # cordoned: each one is acknowledged ("drain anyway"), never a stop here.
    # Force and a planned outage send no drain; after the drain they are not
    # compared - what the drain got past does not stop power.
    drain_blockers = []
    if not (force or planned_outage or after_drain):
        try:
            drain_blockers = DRAIN_BLOCKERS.for_host(node, read=kget)
        except Exception as error:  # a check that failed is said, as unread
            drain_blockers = [DRAIN_BLOCKERS.failed(error)]
    review = {"node": node, "node_uid": node_uid, "action": action, "boot_id": boot_id,
              "workloads": sorted((w.get("ns", ""), w.get("name", ""), bool(w.get("stranded")))
                                  for w in place.get("workloads", [])),
              "volumes": [(v["name"], v["risk"], v["healthy_elsewhere"], v["robustness"]) for v in affected],
              "pods": sorted(((p.get("metadata") or {}).get("namespace", ""),
                              (p.get("metadata") or {}).get("name", "")) for p in pods_here),
              "storage_unknown": storage_unknown, "vms": vm_rows,
              "hold": [(i["id"], i["options"]) for i in hold],
              "maintenance": maintenance,
              "drain_pods": drain_pods,
              "drain_blockers": DRAIN_BLOCKERS.review(drain_blockers),
              "quorum": control.get("can_lose", 0), "force": bool(force), "planned_outage": planned_outage}
    token = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[:20]
    return {"node": node, "node_uid": node_uid, "action": action, "review_token": token, "boot_id": boot_id,
            "quorum": control, "workloads": place.get("workloads", []),
            "stranded": place.get("stranded", []), "volumes": affected,
            "vms": vm_rows, "pods": len(pods_here), "storage_unknown": storage_unknown,
            "hold": hold,
            "maintenance": maintenance,
            "drain_pods": drain_pods,
            "drain_blockers": drain_blockers, "requires_blocker_ack": bool(drain_blockers),
            "requires_data_ack": storage_unknown or bool(maintenance["local_storage"]) or any(v["risk"] in ("unavailable", "single-copy") for v in affected),
            "blockers": blockers, "warnings": warnings, "ready": not blockers,
            "overridable": soft, "hard_blockers": hard, "force": bool(force), "planned_outage": planned_outage,
            "cordoned": bool((node_obj.get("spec") or {}).get("unschedulable"))}


def recheck_planned_outage(original):
    """Never turn a changed or multi-host cluster into an approved outage."""
    fresh = plan(original["node"], original["action"])
    if not fresh["ready"] or not fresh["planned_outage"]:
        raise ValueError("Nothing was sent. The planned single-host outage is no longer safe. Review it again.")
    if fresh["review_token"] != original["review_token"]:
        raise ValueError("The effect on the host or cluster changed since the review, so nothing was sent")


def recheck_forced(original):
    """A forced action sends no drain, but still never to a host that changed
    identity, rebooted, or grew a new hard stop since it was reviewed."""
    fresh = plan(original["node"], original["action"], force=True)
    if not fresh["ready"]:
        raise ValueError("Nothing was sent: " + "; ".join(fresh["blockers"]))
    if fresh["boot_id"] != original["boot_id"] or fresh["node_uid"] != original["node_uid"]:
        raise ValueError("The host changed since the review, so nothing was sent")


SETTLE = 180       # seconds a moved app's volume may take to attach on its new host


def _in_transit(volume):
    """Between hosts: an app that moved off has its volume detaching here and
    attaching there, its health unknown for that moment."""
    state = volume.get("state") or ""
    return state in ("attaching", "detaching") or (volume.get("robustness") in ("unknown", "") and state not in ("detached", ""))


# Longhorn's robustness, worst to best. Only a known state counts as better.
_ROBUSTNESS = {"faulted": 0, "degraded": 1, "healthy": 2}


def _improved(old, new):
    """A volume that finished rebuilding during the drain is safer, not changed."""
    return old in _ROBUSTNESS and new in _ROBUSTNESS and _ROBUSTNESS[new] > _ROBUSTNESS[old]


def recheck_after_drain(original, clock=time.time, sleep=time.sleep):
    """Never send power using the pre-drain storage/quorum/VM snapshot."""
    # Eviction may remove the local replica entirely. Keep checking every
    # reviewed volume, even when it no longer appears on the drained host.
    # A volume still moving with its app is given a moment to settle first.
    names = [v["name"] for v in original["volumes"]]
    deadline = clock() + SETTLE
    while True:
        fresh = plan(original["node"], original["action"], volume_names=names, after_drain=True)
        # Homestead's own short host command, started on this host while it
        # drained, holds it only until its script ends.
        own_command = fresh["blockers"] and all(MAINTENANCE.HOST_COMMAND_BLOCKER in str(b) for b in fresh["blockers"])
        if (not any(_in_transit(v) for v in fresh["volumes"]) and not own_command) or clock() >= deadline:
            break
        sleep(5)
    if not fresh["ready"]:
        raise ValueError("The host stays cordoned, and nothing was sent: " + "; ".join(fresh["blockers"]))
    before = {v["name"]: v for v in original["volumes"]}
    for volume in fresh["volumes"]:
        old = before.get(volume["name"])
        # Stopping this host's replica can make a healthy volume degraded.
        # That reviewed redundancy loss is expected only if its verified
        # surviving copies remain available on other Ready hosts.
        expected_degradation = (old and old["healthy_elsewhere"] > 0 and old["robustness"] == "healthy" and
                                volume["robustness"] == "degraded")
        stopped_to_wait = old and volume.get("state") == "detached"
        improved = old and _improved(old["robustness"], volume["robustness"])
        if (not old or volume["healthy_elsewhere"] < old["healthy_elsewhere"] or
                (volume["robustness"] != old["robustness"] and not expected_degradation and not stopped_to_wait
                 and not improved)):
            # Say which, and how - the review is for a person to act on.
            what = ("it is new since the review" if not old else
                    f"healthy copies elsewhere went from {old['healthy_elsewhere']} to {volume['healthy_elsewhere']}"
                    if volume["healthy_elsewhere"] < old["healthy_elsewhere"] else
                    f"it went from {old['robustness']} to {volume['robustness']} ({volume.get('state') or 'state unknown'})")
            raise ValueError(f"The host stays cordoned. The effect on a volume changed during the drain ({volume.get('claim') or volume['name']}: "
                             f"{what}), so nothing was sent. Review it again.")
    if fresh["boot_id"] != original["boot_id"] or fresh["node_uid"] != original["node_uid"]:
        raise ValueError("The host changed during the drain, so nothing was sent")
    remaining = [p for p in _items("/api/v1/pods") if (p.get("spec") or {}).get("nodeName") == original["node"]
                 and MAINTENANCE.drainable(p) and not MAINTENANCE.own_host_command(p)]
    if remaining:
        raise ValueError("The host stays cordoned. App pods stayed or appeared during the drain, so nothing was sent.")


# Set by server.py: whether a job's replica has gone, and carrying it on.
WORKER_GONE = None
RESUME = None
RESTORE = None
UNCORDON = None     # allow scheduling on the host again (leader only)


def _allow_scheduling(item, ref):
    """The host is back and its volumes are available: the cordon this job
    put on it comes off. Kept when the host was cordoned before the review,
    when nothing was cordoned (a single host, a forced action), or for a job
    from before this was recorded. A status while the leader has yet to."""
    if (ref.get("planned_outage") or ref.get("forced") or ref.get("uncordoned")
            or ref.get("cordoned_before") is not False):
        return None
    if UNCORDON and UNCORDON(item):
        return None
    return "running", 95, "The host is back. Uncordoning it."


def _scheduling(ref):
    if ref.get("planned_outage"):
        return "Scheduling was left unchanged"
    if ref.get("uncordoned"):
        return "Scheduling is allowed on it again"
    if ref.get("cordoned_before"):
        return "It stays cordoned, as it was before"
    return "It stays cordoned"


def _returned(item, ref, now, how):
    """The host is back: start what waited for it, then the usual checks.
    Returns a status to report now, or None to carry on to them."""
    if not ref.get("held") or ref.get("restored") is not None:
        return None
    if not RESTORE or not RESTORE(item):
        return "running", 88, f"{how}. Starting the apps and VMs that waited for it."
    return None


def _started(ref):
    restored = ref.get("restored")
    if not ref.get("held") or restored is None:
        return ""
    started, left = restored.get("started") or [], restored.get("left") or []
    return ((f". Started again: {', '.join(started[:6])}" if started else "") +
            (f". Left as they are: {', '.join(left[:6])}" if left else ""))


def status(item):
    """Observe a power command; a job that ends without its host coming back
    starts again what waited for it, on other hosts where it can run."""
    state = _status(item)
    ref = item.get("ref") or {}
    _close_maintenance(item, ref, state)
    if state[0] == "failed" and ref.get("held") and ref.get("restored") is None:
        if RESTORE and RESTORE(item, False):
            return state[0], state[1], state[2] + _started(ref)
        # Only the leader starts things; the job stays open until it has.
        return "running", state[1], state[2] + ". Starting again what waited for the host"
    return state


# Failures that mean the host stayed down past the job's limit: from then on
# its downtime is an outage, not maintenance.
LIMIT_FAILURES = ("The host did not become ready within 10 minutes", "The restart or shutdown was not confirmed within 10 minutes",
                  # Wording before the plain English pass, still on jobs saved by an older version
                  "Host did not return Ready within 10 minutes", "Power transition was not verified within 10 minutes")


def _close_maintenance(item, ref, state):
    """Close the maintenance this job opened when it sent power (#535): when the
    host is back on a new boot, or when the job ends another way. Either
    Homestead copy may observe the job, so it closes by the job's id."""
    if ref.get("action") == "maintenance":
        return    # its event outlives the job; End closes it
    if ref.get("phase") in BEFORE_SEND or not item.get("id") or ref.get("maintenance_closed"):
        return
    try:
        if ref.get("returned_at"):
            HISTORY.maintenance_close(item["id"], now=ref["returned_at"], outcome="returned")
        elif state[0] in ("succeeded", "failed", "cancelled"):
            HISTORY.maintenance_close(item["id"], outcome="limit" if str(state[2]).startswith(LIMIT_FAILURES) else "ended")
        else:
            return
        ref["maintenance_closed"] = True   # saved with the job, so later looks don't read history again
    except Exception:
        pass  # bookkeeping; never what decides the job's state


# The steps before power is sent: nothing about the host's power to observe yet.
BEFORE_SEND = ("reviewed", "cordoning", "holding", "draining", "verifying")


def _status(item):
    """Observe a power command without treating a lost API connection as success."""
    ref = item["ref"]
    scheduling = ("Scheduling was left unchanged" if ref.get("planned_outage") else
                  "Scheduling was allowed again, so what waited could start there" if ref.get("uncordoned") else "It stays cordoned")
    now = time.time()
    phase = ref.get("phase")
    if ref.get("action") == "maintenance" and phase == "maintained":
        return "succeeded", 100, item.get("message") or "In maintenance"
    if phase in BEFORE_SEND:
        # A drain can evict the very replica running it, or its container can
        # restart in place. Its job names that replica and its restart count;
        # once it has gone, the leader carries the job on.
        worker = ref.get("worker")
        if worker and WORKER_GONE and RESUME and WORKER_GONE(worker, ref.get("worker_restarts")) and RESUME(item):
            return "running", item.get("progress", 0), "Homestead restarted or moved while preparing this host. Continuing."
        # A live worker can wait a long time for Longhorn; one that recorded
        # nothing for half an hour, or an older job with no worker named, stopped.
        limit = 1800 if worker else 240
        if now - ref.get("phase_at", ref.get("started_epoch", now)) > limit:
            try:
                opened = HISTORY.declared_for(ref["node"], strict=True) if ref.get("action") == "maintenance" else None
            except Exception:
                # A bad read proves nothing, and failing starts what waits while its event may be open.
                return "running", item.get("progress", 0), "Homestead could not read whether this host's maintenance started. Checking again."
            if opened and opened.get("op") == item.get("id"):
                # It got there and stopped before saying so: what waits goes on waiting.
                return "succeeded", 100, f"In maintenance: {opened['reason']}"
            return "failed", item.get("progress", 0), (
                "Maintenance stopped before the host was drained. Check the host and whether it is cordoned before trying again."
                if ref.get("action") == "maintenance" else
                "Maintenance stopped before the restart or shutdown was sent. Check the host and whether it is cordoned before trying again.")
        return "running", item.get("progress", 0), item.get("message", "Preparing host maintenance")
    node = kget(f"/api/v1/nodes/{ref['node']}")
    if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") != ref["node_uid"]:
        return "failed", item.get("progress", 0), "The host changed, so this request cannot be confirmed. Check the original host. No command was sent again."
    up = _ready(node)
    boot = ((node.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
    if not up:
        ref["saw_down"] = True
        ref.setdefault("down_at", now)
        if ref["action"] == "poweroff" and ref.get("held") and ref.get("restored") is None:
            return "running", 70, (f"Host is off. {len(ref['held'])} app(s) and VM(s) wait for it and start again when it "
                                   "is back, or they can be started on other hosts now.")
        if ref["action"] == "poweroff" and ref.get("held"):
            return "succeeded", 100, "The host is off. What waited for it was started on other hosts where it can run" + _started(ref)
        if ref["action"] == "poweroff":
            # NotReady can mean a network partition, not a powered-off host.
            # Never turn that observation into a green shutdown success.
            if now - ref.get("started_epoch", now) > 600:
                return "failed", 60, "The shutdown could not be confirmed. The host is not ready, so check its console or power light before trying again. " + scheduling
            return "running", 60, "The host is not ready, but it is not confirmed to be off. Check its console or power light. No command is sent again."
        if now - ref.get("started_epoch", now) > 600:
            return "failed", 60, "The host did not become ready within 10 minutes. Check the host. " + scheduling
        return "running", 60, "The host is not ready. Waiting to confirm that it shut down or returned."
    if ref["action"] == "poweroff" and ref.get("boot_id") and boot and boot != ref["boot_id"]:
        # Off and on again - perhaps with Homestead, on a single host.
        waiting = _returned(item, ref, now, "Host was powered off and has started again") or _allow_scheduling(item, ref)
        if waiting:
            return waiting
        return "succeeded", 100, "Host was powered off and has started again. " + _scheduling(ref) + _started(ref)
    if ref["action"] == "poweroff" and ref.get("saw_down"):
        if ref.get("held") and ref.get("restored") is not None:
            return "succeeded", 100, "Host was powered off" + _started(ref)
        return "failed", 90, "The host became ready again after the shutdown was requested. Check whether it is on."
    if ref["action"] == "reboot" and ref.get("boot_id") and boot and boot != ref["boot_id"]:
        ref.setdefault("returned_at", now)
        waiting = _returned(item, ref, now, "Host rebooted")
        if waiting:
            return waiting
        volume_names = ref.get("volumes") or []
        if not volume_names:
            waiting = _allow_scheduling(item, ref)
            if waiting:
                return waiting
            return "succeeded", 100, "Host returned Ready with a new boot ID. " + _scheduling(ref) + "; check workloads" + _started(ref)
        volumes = _items(f"{LH}/volumes", absent_ok=True)
        if volumes is None:
            if now - ref["returned_at"] > 1800:
                return "failed", 85, "Host rebooted, but Longhorn health could not be verified within 30 minutes"
            return "running", 85, "The host restarted, but Longhorn volume health is unavailable"
        by_name = {v.get("metadata", {}).get("name"): v for v in volumes}
        # A degraded volume is readable and writable from a whole copy while
        # Longhorn rebuilds the rest - hours, for a host's worth of volumes,
        # five at a time. The host is back when nothing is unavailable.
        live = {name: (by_name[name].get("status") or {}) for name in volume_names
                if name in by_name and (by_name[name].get("status") or {}).get("state") != "detached"}
        claim = lambda name: "/".join(x for x in ((live[name].get("kubernetesStatus") or {}).get("namespace"),
                                                 (live[name].get("kubernetesStatus") or {}).get("pvcName")) if x) or name
        pending = [claim(name) for name, st in live.items() if st.get("robustness") not in ("healthy", "degraded")]
        rebuilding = [name for name, st in live.items() if st.get("robustness") == "degraded"]
        if pending:
            if now - ref["returned_at"] > 1800:
                return "failed", 90, ("Host rebooted, but these volumes did not become available within 30 minutes: " +
                                      ", ".join(pending[:4]))
            return "running", 90, f"The host is ready. Waiting for {len(pending)} volume{'' if len(pending) == 1 else 's'} to become available: " + ", ".join(pending[:4])
        waiting = _allow_scheduling(item, ref)
        if waiting:
            return waiting
        return "succeeded", 100, ("The host is ready and its volumes are available. " + _scheduling(ref) + _started(ref)
                                  + (f". Longhorn is rebuilding {len(rebuilding)} volume cop{'y' if len(rebuilding) == 1 else 'ies'} on it, "
                                     "and the Volumes page shows the progress" if rebuilding else "")
                                  + ("" if ref.get("held") else ". The recovery of the apps is not yet confirmed"))
    if now - ref.get("started_epoch", now) > 600:
        return "failed", 30, "The restart or shutdown was not confirmed within 10 minutes. A helper may still be running, so check its events and logs before any new request. The host stays cordoned."
    if ref.get("saw_down"):
        return "running", 75, "Host returned, but Kubernetes has not reported a new boot ID yet"
    if ref.get("helper_pod"):
        if not ref.get("helper_uid"):
            return "running", 20, "The power helper's record is not confirmed, so only the host is watched. Check the recorded helper before trying again."
        try:
            helper = kget(f"/api/v1/namespaces/{ref.get('helper_namespace', 'lab')}/pods/{ref['helper_pod']}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            if (helper.get("metadata") or {}).get("uid") != ref["helper_uid"]:
                return "failed", 20, "The power helper was replaced, so its result cannot confirm this request. Check the host before trying again."
            phase = (helper.get("status") or {}).get("phase", "Pending")
            conditions = (helper.get("status") or {}).get("containerStatuses") or []
            waiting = [((c.get("state") or {}).get("waiting") or {}) for c in conditions]
            reason = next((c.get("reason") for c in waiting if c.get("reason")), "")
            if phase == "Failed":
                return "failed", 20, f"The host's power helper failed ({reason or phase}). The host stays cordoned."
            if phase == "Succeeded" and ref.get("handoff"):
                return "failed", 20, ("The helper on the host did not restart or shut it down, because Homestead's data volume was not released "
                                      "or the host did not go down. Homestead was started again. See the helper's log.")
            if reason:
                return "running", 20, f"The power helper is waiting: {reason}. It may retry automatically. Check the logs in Recent jobs, and do not send another restart or shutdown."
            return "running", 35 if phase == "Running" else 20, f"Power helper {phase.lower()}{': ' + reason if reason else ''}. Waiting for the host to restart or shut down."
    return "running", 20, "Waiting for the host to leave Ready"


def register(ops):
    """The job store's entry for a host power command (#622)."""
    ops.RESOLVERS["node-power"] = status
    # Sent is sent: a host that is powering off or restarting cannot be called back.
    ops.CANCELLERS["node-power"] = (lambda item: {"can": False, "why_not":
        "A host power command cannot be cancelled after it has been sent"}, lambda item, options: "")
