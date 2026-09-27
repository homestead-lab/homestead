"""Read-only interlock between an imported application and its copy Job."""

JOB = "homestead.io/import-job"


def pending(deployment, namespace, read):
    meta = deployment.get("metadata") or {}
    if (meta.get("annotations") or {}).get("homestead.io/storage-copy-job"):
        return "A storage copy holds this workload stopped. Inspect its Recent jobs entry before starting or changing it."
    if "homestead.io/restructure-replicas" in (meta.get("annotations") or {}):
        return "A legacy storage-copy hold remains. Inspect the copy and data in Kubernetes before manually removing the hold; stopping tracking does not release it."
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
        return "Import copy Job ownership does not match this workload; recovery needs review."
    if not any(c.get("type") == "Complete" and c.get("status") == "True"
               for c in (job.get("status") or {}).get("conditions") or []):
        return "Import copy has not completed successfully. Keep the application stopped and inspect the import job."
    return ""
