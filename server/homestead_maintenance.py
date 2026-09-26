"""Read-only drain inventory. Eviction API remains authoritative for PDBs."""
import hashlib
import json
from homestead_topology import selected


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


def inventory(get, pods):
    budgets = items(get, "/apis/policy/v1/poddisruptionbudgets")
    rows, blockers, storage = [], [], []
    for pod in pods:
        meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
        if not drainable(pod):
            continue
        ns, name = meta.get("namespace", "default"), meta.get("name", "?")
        identity = ns + "/" + name
        owner = next((o for o in meta.get("ownerReferences") or [] if o.get("controller") is True), {})
        if not owner:
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
            rows.append({"pod": identity, "budget": ns + "/" + bm.get("name", "?"),
                         "allowed": allowed if fresh else None, "unhealthy_allowed": unhealthy_allowed})
            if not unhealthy_allowed and (not fresh or not isinstance(allowed, int) or allowed < 1):
                blockers.append(identity + ": disruption budget " + bm.get("name", "?") +
                                (" status is stale/unknown" if not fresh else " permits no verified eviction"))
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
    return {"budgets": rows, "local_storage": storage, "blockers": blockers}
