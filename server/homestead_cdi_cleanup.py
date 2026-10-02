"""Reclaim one import's CDI work space, including Retain-policy PVs.

CDI rebinds a prime PV to the final claim. That PV is data, not disposable
work space. Receipts follow claim UIDs rather than relying on name prefixes.
"""
import secrets
import urllib.error

import homestead_transfer_cleanup as RESOURCES

API = "/apis/cdi.kubevirt.io/v1beta1"
STAMP = "homestead.io/cdi-import-id"
RETAIN_WORKER = "cdi.kubevirt.io/storage.pod.retainAfterCompletion"


def receipt(names):
    return {"version": 1, "id": secrets.token_hex(16),
            "disks": {name: {"claims": {}, "pvs": {}, "pods": {}} for name in names}}


def dispatch(read, send, ops, kind, title, resource, href, ref, bodies, next_phase):
    """Journal creation intent before POST; a lost reply is recovered by stamp."""
    with ops.dispatch_guard():
        work = receipt([body["metadata"]["name"] for body in bodies])
        for disk in work["disks"].values():
            disk["not_dispatched"] = True
        ref.update(cdi_cleanup=work, phase="creating")
        operation = ops.start(kind, title, resource, href, ref, "Making its disks")
        try:
            for body in bodies:
                name = body["metadata"]["name"]
                body["metadata"].setdefault("annotations", {})[STAMP] = work["id"]
                # Keep the ownership chain available even when CDI completes
                # between polls. Homestead removes these workers in cleanup.
                body["metadata"]["annotations"][RETAIN_WORKER] = "true"
                work["disks"][name]["not_dispatched"] = False
                ops.record_phase(operation["id"], "creating", 1, "Making its disks", cdi_cleanup=work)
                try:
                    made = send("POST", f"{API}/namespaces/{ref['namespace']}/datavolumes", body)
                except urllib.error.HTTPError as error:
                    if error.code in (400, 403, 409, 422):
                        work["disks"][name]["not_created"] = True
                    raise
                if (made or {}).get("metadata", {}).get("uid"):
                    work["disks"][name]["dv_uid"] = made["metadata"]["uid"]
                # Capture the first target UID before another disk is dispatched;
                # that UID also identifies retired prime/scratch claims after GC.
                observe(read, ref["namespace"], work)
                operation = ops.record_phase(operation["id"], "creating", 1, "Making its disks", cdi_cleanup=work)
        except Exception:
            return ops.record_phase(operation["id"], "cleanup", 1,
                                    "Could not create the import disks; cleaning up", cdi_cleanup=work,
                                    cleanup_outcome="failed", cleanup_detail="Could not create the import disks")
        return ops.record_phase(operation["id"], next_phase, 2, "Waiting for CDI", cdi_cleanup=work)


def owner(obj, kind, uid):
    return any(row.get("kind") == kind and row.get("uid") == uid and row.get("controller") is True
               for row in (obj.get("metadata") or {}).get("ownerReferences") or [])


def _identity(obj):
    meta = obj.get("metadata") or {}
    if not all(meta.get(key) for key in ("name", "uid", "resourceVersion")):
        raise ValueError("Import cleanup cannot verify a resource identity")
    return meta


def _claim_owned(disk, row, claim):
    if row["role"] == "target":
        return owner(claim, "DataVolume", disk.get("dv_uid"))
    if row["role"] == "prime":
        return owner(claim, "PersistentVolumeClaim", disk.get("target_uid"))
    return any(owner(claim, "Pod", uid) for uid in disk["pods"])


def observe(read, ns, work):
    """Capture live ownership and retired prime claims before any deletion."""
    claims = RESOURCES.items(read, f"/api/v1/namespaces/{ns}/persistentvolumeclaims")
    pods = RESOURCES.items(read, f"/api/v1/namespaces/{ns}/pods")
    pvs = RESOURCES.items(read, "/api/v1/persistentvolumes")
    for name, disk in work["disks"].items():
        if disk.get("not_created") or disk.get("not_dispatched"):
            continue
        dv = RESOURCES.optional(read, f"{API}/namespaces/{ns}/datavolumes/{name}")
        if dv:
            meta = _identity(dv)
            if (meta.get("annotations") or {}).get(STAMP) != work["id"] or (
                    disk.get("dv_uid") and disk["dv_uid"] != meta["uid"]):
                raise ValueError("Import disk was replaced; its resources were preserved")
            disk["dv_uid"] = meta["uid"]
        if not disk.get("dv_uid"):
            continue  # A journaled creation may never have reached Kubernetes.
        target = next((row for row in claims if row["metadata"].get("name") == name), None)
        if target:
            if not owner(target, "DataVolume", disk["dv_uid"]):
                raise ValueError("Import claim ownership changed; its resources were preserved")
            meta = _identity(target)
            old = disk.get("target_uid")
            if old and old != meta["uid"]:
                raise ValueError("Import claim was replaced; its resources were preserved")
            disk["target_uid"] = meta["uid"]
            disk["claims"][meta["uid"]] = {"name": name, "role": "target"}
        target_uid = disk.get("target_uid")
        prime_name = "prime-" + target_uid if target_uid else ""
        for claim in claims:
            meta = claim["metadata"]
            if prime_name and meta.get("name") == prime_name:
                if not owner(claim, "PersistentVolumeClaim", target_uid):
                    raise ValueError("CDI staging claim ownership changed; it was preserved")
                meta = _identity(claim)
                disk["claims"][meta["uid"]] = {"name": meta["name"], "role": "prime"}
        for pod in pods:
            labels = (pod["metadata"].get("labels") or {})
            worker = (labels.get("app") == "containerized-data-importer" or
                      labels.get("cdi.kubevirt.io") in ("cdi-upload-server", "importer"))
            if worker and any(owner(pod, "PersistentVolumeClaim", uid) for uid in disk["claims"]):
                meta = _identity(pod)
                disk["pods"][meta["uid"]] = meta["name"]
        for claim in claims:
            meta = claim["metadata"]
            if any(owner(claim, "Pod", uid) for uid in disk["pods"]):
                # A pod's other claims are not scratch. CDI uses this exact suffix.
                if meta.get("name") not in {row["name"] + "-scratch" for row in disk["claims"].values()
                                            if row["role"] != "scratch"}:
                    continue
                meta = _identity(claim)
                disk["claims"][meta["uid"]] = {"name": meta["name"], "role": "scratch"}
        for claim in claims:
            known = disk["claims"].get(claim["metadata"].get("uid"))
            if known and not _claim_owned(disk, known, claim):
                raise ValueError("Import work claim ownership changed; it was preserved")
        for pv in pvs:
            spec = pv.get("spec") or {}
            claim = spec.get("claimRef") or {}
            if claim.get("namespace") != ns or not claim.get("uid"):
                continue
            known = disk["claims"].get(claim["uid"])
            # CDI may finish and GC work claims between polls or during a restart.
            # The reserved prime name contains THIS target's immutable UID. Only
            # adopt released Longhorn PVs with no surviving claim, never a prefix.
            retired = (prime_name and claim.get("name") in (prime_name, prime_name + "-scratch")
                       and (pv.get("status") or {}).get("phase") == "Released"
                       and (spec.get("csi") or {}).get("driver") == "driver.longhorn.io"
                       and not any(c["metadata"].get("uid") == claim["uid"] or
                                   c["metadata"].get("name") == claim["name"] for c in claims))
            if not known and not retired:
                continue
            if known and known["name"] != claim.get("name"):
                raise ValueError("Import volume claim identity changed; it was preserved")
            if retired:
                known = {"name": claim["name"], "role": "scratch" if claim["name"].endswith("-scratch") else "prime"}
                disk["claims"][claim["uid"]] = known
            meta = _identity(pv)
            entry = {"uid": meta["uid"], "claim_uid": claim["uid"], "claim_name": claim["name"],
                     "role": known["role"], "csi": spec.get("csi") or {}}
            old = disk["pvs"].get(meta["name"])
            if old and (old["uid"] != meta["uid"] or old["csi"] != entry["csi"]):
                raise ValueError("Import backing volume was replaced; it was preserved")
            disk["pvs"][meta["name"]] = entry


def cleanup(read, send, ns, work, keep_disks=True):
    """Wait for normal finalizers and CSI reclamation; never force deletion."""
    pending = False
    claims = RESOURCES.items(read, f"/api/v1/namespaces/{ns}/persistentvolumeclaims")
    pods = RESOURCES.items(read, f"/api/v1/namespaces/{ns}/pods")
    for disk in work["disks"].values():
        for claim in claims:
            known = disk["claims"].get(claim["metadata"].get("uid"))
            if known and not _claim_owned(disk, known, claim):
                raise ValueError("Import work claim ownership changed; cleanup stopped")
    disposable = {row["name"] for disk in work["disks"].values() for row in disk["claims"].values()
                  if row["role"] != "target" or not keep_disks}
    if not keep_disks:
        disposable.update(name for name, disk in work["disks"].items() if disk.get("dv_uid"))
    workers = {uid for disk in work["disks"].values() for uid in disk["pods"]}
    if any(pod["metadata"].get("uid") not in workers and
           any((v.get("persistentVolumeClaim") or {}).get("claimName") in disposable
               for v in (pod.get("spec") or {}).get("volumes") or []) for pod in pods):
        raise ValueError("Another pod uses import work space; its storage was preserved")
    for resource in ("virtualmachines", "virtualmachineinstances"):
        for vm in RESOURCES.items(read, f"/apis/kubevirt.io/v1/namespaces/{ns}/{resource}", missing=True):
            spec = vm.get("spec") or {}
            spec = (spec.get("template") or {}).get("spec") or spec
            if any((v.get("dataVolume") or {}).get("name") in disposable or
                   (v.get("persistentVolumeClaim") or {}).get("claimName") in disposable
                   for v in spec.get("volumes") or []):
                raise ValueError("A VM now uses an import disk; its storage was preserved")
    for row in work.get("aux", []):
        obj = RESOURCES.optional(read, row["path"])
        if obj:
            meta = _identity(obj)
            if (meta.get("annotations") or {}).get(STAMP) != work["id"] or (
                    row.get("uid") and meta["uid"] != row["uid"]):
                raise ValueError("Import copy resource was replaced; cleanup stopped")
            pending |= not RESOURCES.delete(read, send, row["path"], obj)
    for name, disk in work["disks"].items():
        if disk.get("not_created") or disk.get("not_dispatched"):
            continue
        target = next((c for c in claims if c["metadata"].get("uid") == disk.get("target_uid")), None)
        if keep_disks and (not target or (target.get("status") or {}).get("phase") != "Bound"):
            raise ValueError("Completed disk claim is not bound; its storage was preserved")
        protected_pv = (target.get("spec") or {}).get("volumeName") if target and keep_disks else None
        if not keep_disks:
            dv_path = f"{API}/namespaces/{ns}/datavolumes/{name}"
            dv = RESOURCES.optional(read, dv_path)
            if dv:
                if dv["metadata"].get("uid") != disk.get("dv_uid") or (
                        dv["metadata"].get("annotations") or {}).get(STAMP) != work["id"]:
                    raise ValueError("Import disk was replaced; cleanup stopped")
                pending |= not RESOURCES.delete(read, send, dv_path, dv)
        for uid, pod_name in disk["pods"].items():
            path = f"/api/v1/namespaces/{ns}/pods/{pod_name}"
            pod = RESOURCES.optional(read, path)
            if pod and pod["metadata"].get("uid") == uid:
                if not any(owner(pod, "PersistentVolumeClaim", claim_uid) for claim_uid in disk["claims"]):
                    raise ValueError("Import worker ownership changed; cleanup stopped")
                pending |= not RESOURCES.delete(read, send, path, pod)
        # Re-read after requesting worker deletion. Even a terminating pod can
        # hold a mount; do not change the storage policy until it has disappeared.
        pods = RESOURCES.items(read, f"/api/v1/namespaces/{ns}/pods")
        for uid, row in disk["claims"].items():
            if row["role"] == "target" and keep_disks:
                continue
            path = f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{row['name']}"
            claim = RESOURCES.optional(read, path)
            if not claim:
                continue
            if claim["metadata"].get("uid") != uid:
                current = disk["claims"].get(claim["metadata"].get("uid"))
                if row["role"] == "scratch" and current == row:
                    continue  # CDI recreated scratch during THIS import attempt.
                # A retry's claim cannot be used to reclaim this attempt's PV.
                raise ValueError("Import work claim was replaced; cleanup stopped")
            if any((v.get("persistentVolumeClaim") or {}).get("claimName") == row["name"]
                   for pod in pods for v in (pod.get("spec") or {}).get("volumes") or []):
                pending = True
                continue
            if row["role"] == "prime" and keep_disks and (claim.get("status") or {}).get("phase") != "Lost":
                pending = True
                continue
            pending |= not RESOURCES.delete(read, send, path, claim)
        claims = RESOURCES.items(read, f"/api/v1/namespaces/{ns}/persistentvolumeclaims")
        for pv_name, row in disk["pvs"].items():
            if pv_name == protected_pv or (row["role"] == "target" and keep_disks):
                continue
            path = "/api/v1/persistentvolumes/" + pv_name
            pv = RESOURCES.optional(read, path)
            if not pv:
                csi = row["csi"]
                if csi.get("driver") == "driver.longhorn.io" and csi.get("volumeHandle"):
                    pending |= RESOURCES.optional(read, RESOURCES.LH + "/volumes/" + csi["volumeHandle"]) is not None
                continue
            meta = _identity(pv)
            spec = pv.get("spec") or {}
            claim = spec.get("claimRef") or {}
            if meta["uid"] != row["uid"] or (spec.get("csi") or {}) != row["csi"]:
                raise ValueError("Import backing volume was replaced; cleanup stopped")
            if keep_disks and claim.get("uid") == disk.get("target_uid"):
                continue  # Prime PV has been promoted to the completed disk.
            if (claim.get("uid"), claim.get("namespace"), claim.get("name")) != (
                    row["claim_uid"], ns, row["claim_name"]):
                raise ValueError("Import backing volume was rebound; cleanup stopped")
            if any((c.get("spec") or {}).get("volumeName") == pv_name or
                   c["metadata"].get("uid") == row["claim_uid"] for c in claims):
                pending = True
                continue
            if any((v.get("persistentVolumeClaim") or {}).get("claimName") == row["claim_name"]
                   for pod in pods for v in (pod.get("spec") or {}).get("volumes") or []):
                pending = True
                continue
            if (pv.get("status") or {}).get("phase") != "Released":
                pending = True
                continue
            if spec.get("persistentVolumeReclaimPolicy") == "Retain" and not meta.get("deletionTimestamp"):
                send("PATCH", path, [
                    {"op": "test", "path": "/metadata/uid", "value": row["uid"]},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
                    {"op": "test", "path": "/spec/claimRef", "value": claim},
                    {"op": "test", "path": "/status/phase", "value": "Released"},
                    {"op": "replace", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Delete"},
                ], ctype="application/json-patch+json")
            pending = True  # CSI owns deletion. Absence, not a patch, is completion.
    return not pending


def finish(item, read, send, checkpoint):
    """Cleanup remains active and durable until resources really disappear."""
    ref = item["ref"]
    outcome = ref.get("cleanup_outcome", "failed")
    try:
        observe(read, ref["namespace"], ref["cdi_cleanup"])
        checkpoint(item)
        complete = cleanup(read, send, ref["namespace"], ref["cdi_cleanup"],
                           keep_disks=ref.get("cleanup_keep_disks", outcome == "succeeded"))
    except Exception as error:
        ref["retain_resources"] = True
        # Kubernetes errors may contain a source URL. Show safe validation text
        # only; the job keeps its receipts and retries transient API failures.
        reason = str(error) if isinstance(error, ValueError) else "Kubernetes could not confirm cleanup"
        return "running", 99, ref.get("cleanup_detail", "Import ended") + "; cleanup pending: " + reason
    ref["retain_resources"] = not complete
    if not complete:
        return "running", 99, ref.get("cleanup_detail", "Import ended") + "; removing temporary import storage"
    ref["phase"] = "done" if outcome == "succeeded" else "failed"
    ref["detail"] = ref.get("cleanup_detail", "Import ended") + "; temporary import storage cleared"
    return outcome, 100, ref["detail"]


def track(item, read, checkpoint):
    """An unavailable inventory must not end the job and lose reclamation."""
    try:
        observe(read, item["ref"]["namespace"], item["ref"]["cdi_cleanup"])
        checkpoint(item)
    except Exception as error:
        item["ref"]["retain_resources"] = True
        reason = str(error) if isinstance(error, ValueError) else "Kubernetes could not verify import storage"
        return "running", item.get("progress", 2), "Import paused: " + reason
    return None
