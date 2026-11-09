"""Read-only interlock between an imported application and its copy Job."""

JOB = "homestead.io/import-job"
DISPATCH = "homestead.io/import-dispatch"
JOB_UID = "homestead.io/import-job-uid"


def pending(deployment, namespace, read):
    meta = deployment.get("metadata") or {}
    if (meta.get("annotations") or {}).get(DISPATCH):
        return "The import setup is incomplete or uncertain. Check its entry in Recent jobs before starting. No request is repeated automatically."
    if (meta.get("annotations") or {}).get("homestead.io/storage-copy-job"):
        return "A storage copy holds this workload stopped. Inspect its Recent jobs entry before starting or changing it."
    if "homestead.io/restructure-replicas" in (meta.get("annotations") or {}):
        return "An older storage copy hold remains. Check the copy and data in Kubernetes before removing the hold manually. Stopping tracking does not release it."
    name = (meta.get("annotations") or {}).get(JOB)
    if not name or not meta.get("uid"):
        return ""  # an uncreated preview has no copy to wait for yet
    try:
        job = read(f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}")
    except Exception:
        return "Import copy result is unavailable. Do not start until the copy is verified or the failed import is recreated."
    owned = any(ref.get("kind") == "Deployment" and ref.get("uid") == meta["uid"]
                for ref in (job.get("metadata") or {}).get("ownerReferences") or [])
    if not owned:
        return "The import copy Job does not belong to this app, so recovery needs a review."
    expected = (meta.get("annotations") or {}).get(JOB_UID)
    if expected and job.get("metadata", {}).get("uid") != expected:
        return "The import Job was replaced, so its completion is not trusted. Check the import."
    if not any(c.get("type") == "Complete" and c.get("status") == "True"
               for c in (job.get("status") or {}).get("conditions") or []):
        return "Import copy has not completed successfully. Keep the application stopped and inspect the import job."
    if expected:
        try:
            pods = read("/api/v1/pods")
            if not isinstance(pods.get("items"), list) or pods.get("metadata", {}).get("continue"):
                raise ValueError("Incomplete pods")
            if any(p.get("metadata", {}).get("namespace") == namespace and
                   p.get("status", {}).get("phase") not in ("Succeeded", "Failed") and any(
                       o.get("kind") == "Job" and o.get("controller") is True and o.get("uid") == expected
                       for o in p.get("metadata", {}).get("ownerReferences", [])) for p in pods["items"]):
                return "The import copy still has active pods. Wait before starting the application."
        except Exception:
            return "Import pod termination cannot be verified. Keep the application stopped."
    return ""
