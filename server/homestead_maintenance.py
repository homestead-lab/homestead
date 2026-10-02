"""Read-only drain inventory. Eviction API remains authoritative for PDBs."""
import hashlib
import json
from homestead_topology import selected
import homestead_pullwatch as PULLWATCH
import homestead_runtime as RUNTIME
import homestead_names as NAMES


def pod_snapshot(pods):
    return sorted((p.get("metadata", {}).get("namespace", ""), p.get("metadata", {}).get("name", ""),
                   p.get("metadata", {}).get("uid", ""),
                   hashlib.sha256(json.dumps({"spec": p.get("spec"),
                       "owners": p.get("metadata", {}).get("ownerReferences"),
                       "labels": p.get("metadata", {}).get("labels")}, sort_keys=True).encode()).hexdigest())
                  for p in pods if drainable(p))


def items(get, path):
    result = get(path)
    if not isinstance(result.get("items"), list) or (result.get("metadata") or {}).get("continue"):
        raise ValueError("incomplete inventory: " + path)
    return result["items"]


def drainable(pod):
    meta = pod.get("metadata") or {}
    return ((pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed") and
            not (meta.get("annotations") or {}).get("kubernetes.io/config.mirror") and
            not any(o.get("controller") is True and o.get("kind") == "DaemonSet"
                    for o in meta.get("ownerReferences") or []))


def longhorn_instance_manager(pod):
    """Recognize Longhorn's per-node manager by ownership, not a name prefix."""
    meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
    labels = meta.get("labels") or {}
    return (meta.get("namespace") == "longhorn-system" and bool(meta.get("uid")) and
            labels.get("longhorn.io/component") == "instance-manager" and
            labels.get("longhorn.io/managed-by") == "longhorn-manager" and
            bool(spec.get("nodeName")) and labels.get("longhorn.io/node") == spec["nodeName"] and
            any(o.get("controller") is True and o.get("kind") == "InstanceManager" and
                o.get("apiVersion") in ("longhorn.io/v1beta1", "longhorn.io/v1beta2") and
                o.get("name") == meta.get("name") and bool(o.get("uid"))
                for o in meta.get("ownerReferences") or []))


def observer(pod, namespace=None):
    namespace = namespace or PULLWATCH.NS
    if PULLWATCH.disposable(pod, namespace):
        return "image-pull progress watcher"
    if RUNTIME.readonly_observer(pod, namespace, "image-scan", r"homestead-image-scan-[0-9a-f]{10}-[0-9a-f]{1,6}",
                                 ["sh", "-c", f"{RUNTIME.CRICTL} images -o json"], 120):
        return "image-cache scan"
    return ""


def helper_blocker(pod):
    """Known mutating/session helpers need an explicit end before maintenance.

    Labels only add protection and guidance; they never authorize removal.
    This also protects controller-owned copy Jobs from an automatic restart.
    """
    labels = (pod.get("metadata") or {}).get("labels") or {}
    task = NAMES.label_of(pod.get("metadata"), "task")
    guidance = {
        "host-run": "host command may still be changing this host; wait for it to finish or inspect its interrupted operation",
        "node-shell": "host shell may still be running commands; close its sessions and inspect the helper first",
        "files": "file-browser session may be changing files; close the volume browser first",
        "probe": "source probe is active; wait for the source inspection to finish",
        "image-cleanup": "image removal is active; wait for cleanup to finish",
        "import": "import/copy is active; finish or cancel it through its operation before maintenance",
        "chown": "volume ownership changes are active; wait for them to finish",
        "reclass": "volume migration is active; finish or recover it before maintenance",
        "restructure": "workload data restructuring is active; finish or recover it before maintenance",
        "iso-copy": "ISO copy is active; wait for it to finish before maintenance",
        "vm-import": "VM import is active; finish or cancel it through its operation before maintenance",
        "node-power": "an earlier power helper is active; inspect it before retrying",
        "cluster-shutdown": "cluster shutdown is active; inspect its saved progress before another power action",
    }
    for key in ("self-data-copy", "data-preparation", "handoff-worker", "self-data-handoff"):
        if labels.get(NAMES.key(key)):
            return "Homestead data handoff is active; finish or recover the data move before maintenance"
    if labels.get(NAMES.key("longhorn-v2-setup")):
        return "Longhorn host preparation is active; wait for it to finish before maintenance"
    if labels.get(NAMES.key("snapshot-files")):
        return "snapshot browser or clone is active; close it and release its temporary mounts first"
    return guidance.get(task, "")


def inventory(get, pods, namespace=None):
    budgets = items(get, "/apis/policy/v1/poddisruptionbudgets")
    rows, blockers, storage, waiting = [], [], [], []
    for pod in pods:
        meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
        if not drainable(pod):
            continue
        ns, name = meta.get("namespace", "default"), meta.get("name", "?")
        identity = ns + "/" + name
        owner = next((o for o in meta.get("ownerReferences") or [] if o.get("controller") is True), {})
        temporary = observer(pod, namespace)
        busy = "" if temporary else helper_blocker(pod)
        if temporary:
            waiting.append(identity + ": temporary " + temporary + " will be evicted; only its report is lost")
        if busy:
            blockers.append(identity + ": " + busy)
        elif not owner and not temporary:
            blockers.append(identity + ": unmanaged pod has no controller to recreate it; handle it explicitly first")
        matching = [b for b in budgets if (b.get("metadata") or {}).get("namespace") == ns and
                    selected((b.get("spec") or {}).get("selector"), meta.get("labels") or {})]
        if len(matching) > 1:
            blockers.append(identity + ": multiple disruption budgets match; eviction cannot be verified")
        for budget in matching:
            bm, bs, policy = budget.get("metadata") or {}, budget.get("status") or {}, budget.get("spec") or {}
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
            unhealthy_allowed = status.get("phase") == "Running" and not ready and policy.get("unhealthyPodEvictionPolicy") == "AlwaysAllow"
            fresh = bm.get("generation") is not None and bs.get("observedGeneration") == bm["generation"]
            allowed = bs.get("disruptionsAllowed")
            # Longhorn protects the manager until cordon and workload eviction
            # release its engines. Only its own fresh, single budget can wait;
            # the Eviction API still has to authorize removal during the drain.
            wait_for_drain = (longhorn_instance_manager(pod) and len(matching) == 1 and
                              bm.get("name") == name and fresh and type(allowed) is int and allowed == 0)
            rows.append({"pod": identity, "budget": ns + "/" + bm.get("name", "?"),
                         "allowed": allowed if fresh else None, "unhealthy_allowed": unhealthy_allowed,
                         "wait_for_drain": wait_for_drain})
            if wait_for_drain:
                waiting.append(identity + ": Longhorn currently protects this storage pod. Homestead will cordon, "
                               "drain workloads and wait up to 2 minutes for eviction to be allowed. "
                               "If Longhorn keeps it protected, power will not be sent and the host stays cordoned")
            if not unhealthy_allowed and (not fresh or not isinstance(allowed, int) or allowed < 1):
                if not wait_for_drain:
                    blockers.append(identity + ": disruption budget " + bm.get("name", "?") +
                                    (" status is stale/unknown" if not fresh else " permits no verified eviction"))
        # Only verified observers have read-only runtime-tool/socket mounts.
        # They remain in the reviewed snapshot, PDB checks and eviction targets.
        if temporary:
            continue
        for volume in spec.get("volumes") or []:
            kind, source = "", ""
            if "emptyDir" in volume:
                kind, source = "emptyDir (deleted by drain)", volume.get("name", "?")
            elif "hostPath" in volume:
                kind, source = "host-local path (not moved)", (volume["hostPath"] or {}).get("path", "?")
            elif "persistentVolumeClaim" in volume:
                claim = (volume["persistentVolumeClaim"] or {}).get("claimName")
                pvc = get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
                pv_name = (pvc.get("spec") or {}).get("volumeName")
                if not pv_name:
                    blockers.append(identity + ": PVC " + str(claim) + " is not bound; storage impact unknown")
                    continue
                pv = get(f"/api/v1/persistentvolumes/{pv_name}")
                pv_spec = pv.get("spec") or {}
                if (pv_spec.get("csi") or {}).get("driver") == "driver.longhorn.io":
                    continue
                kind = "host-local PVC" if "local" in pv_spec or "hostPath" in pv_spec else "external PVC (availability unverified)"
                source = str(claim)
            elif "nfs" in volume:
                kind, source = "external NFS (availability unverified)", (volume["nfs"] or {}).get("server", "?")
            elif any(key not in ("name", "configMap", "secret", "projected", "downwardAPI") for key in volume):
                blockers.append(identity + ": unsupported storage source " + volume.get("name", "?"))
            if kind:
                storage.append({"pod": identity, "kind": kind, "source": source})
    return {"budgets": rows, "local_storage": storage, "blockers": blockers, "waiting": waiting}
