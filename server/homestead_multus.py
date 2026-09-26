"""Multus runtime readiness, distinct from successful Helm/CRD installation."""
NAD_LIST = "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions"
DAEMONSETS = "/apis/apps/v1/namespaces/kube-system/daemonsets"


def inspect(kget):
    result = {"installed": False, "ready": False, "desired": 0, "available": 0,
              "state": "absent", "detail": "Multus is not installed", "issues": []}
    api_ready = False
    try:
        kget(NAD_LIST)
        api_ready = True
        result["installed"] = True
    except Exception as error:
        if getattr(error, "code", None) != 404:
            result.update(state="unknown", detail="Cannot read Multus status; check API access and permissions")
            return result
    try:
        sets = kget(DAEMONSETS).get("items", [])
    except Exception:
        result.update(state="unknown", detail="Cannot read Multus node agents; check API access and permissions")
        return result
    sets = [ds for ds in sets if (ds.get("metadata", {}).get("name") in
            ("multus", "rke2-multus", "kube-multus-ds") or
            ds.get("metadata", {}).get("labels", {}).get("app") in ("multus", "rke2-multus"))]
    result["agents_present"] = bool(sets)
    result["installed"] = result["installed"] or bool(sets)
    if not api_ready:
        result.update(state="api-missing", detail="Network attachment API is missing; install the Multus CRD dependency")
        return result
    if not sets:
        result.update(state="unknown", detail="Network attachment API exists, but no recognised Multus DaemonSet was found")
        return result
    desired = sum((ds.get("status") or {}).get("desiredNumberScheduled", 0) for ds in sets)
    available = sum((ds.get("status") or {}).get("numberAvailable", 0) for ds in sets)
    current = all((ds.get("status") or {}).get("observedGeneration", 0) >= ds.get("metadata", {}).get("generation", 1)
                  and (ds.get("status") or {}).get("updatedNumberScheduled", 0) ==
                  (ds.get("status") or {}).get("desiredNumberScheduled", 0) for ds in sets)
    ready = desired > 0 and available == desired and current
    result.update(ready=ready, desired=desired, available=available, state="ready" if ready else "not-ready",
                  detail=f"Multus available on {available}/{desired} scheduled nodes" + ("" if current else "; rollout pending"))
    if not ready:
        try:
            pods = kget("/api/v1/namespaces/kube-system/pods").get("items", [])
            owners = {ds.get("metadata", {}).get("uid") for ds in sets} - {None}
            for pod in pods:
                if not any(owner.get("uid") in owners for owner in pod.get("metadata", {}).get("ownerReferences", [])):
                    continue
                for state in (pod.get("status", {}).get("initContainerStatuses", []) +
                              pod.get("status", {}).get("containerStatuses", [])):
                    waiting = state.get("state", {}).get("waiting", {})
                    if waiting.get("reason"):
                        result["issues"].append(f"{pod['metadata']['name']}: {waiting['reason']}")
        except Exception:
            result["issues"].append("Pod diagnostics unavailable")
    return result
