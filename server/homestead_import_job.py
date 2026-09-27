"""One-shot import setup and UID-bound copy observation, without replay/cleanup.

Only identities, hashes and receipt metadata reach the durable journal. Import
manifests can contain application credentials and must stay request-local.
"""
import copy
import hashlib
import urllib.error

import homestead_copy_job as COPY
import homestead_capacity_review as REVIEW
import homestead_import_guard as GUARD
import homestead_rename as IDENT
import homestead_rollout_capacity as ROLLOUT
import homestead_vm_power_job as LOCKS
import homestead_vm_write as WRITE

KIND = "import-create"


def planned(prepared, ns):
    out = [{"apiVersion": "v1", "kind": "PersistentVolumeClaim", "namespace": ns, "name": v["name"]}
           for v in prepared["volumes"]]
    for key in ("deployment", "service", "job"):
        obj = prepared.get(key)
        if obj:
            out.append({"apiVersion": obj["apiVersion"], "kind": obj["kind"],
                        "namespace": ns, "name": obj["metadata"]["name"]})
    return out


def pin(ns, names, read):
    # Reuse PVC/PV identity verification; these synthetic pairs only enumerate
    # claims and never generate a local-copy manifest or script.
    return COPY.claims(ns, [{"from": name, "to": name} for name in names], read)


def verify_claims(ns, expected, read):
    now = pin(ns, expected, read)
    if any(not COPY.same_claim(before, now[name]) for name, before in expected.items()):
        raise ValueError("An import volume was replaced or rebound; inspect before continuing")
    return now


def consumers(ns, claims, read):
    COPY.exclusive({"namespace": ns, "claims": claims}, read)


def dispatch(body, prepared, context, read, send, ops, create_claim, admission):
    if not REVIEW.valid(body, context) or body.get("confirm_capacity") is not True:
        raise ValueError("Review the import before creating resources")
    ns, name = context["namespace"], body["name"]
    prepared = copy.deepcopy(prepared)
    targets = planned(prepared, ns)
    if not prepared.get("deployment") and not prepared.get("job") and not any(v["create"] for v in prepared["volumes"]):
        raise ValueError("Choose data to import or a workload to create")
    borrowed = [v["name"] for v in prepared["volumes"] if not v["create"]]
    initial = pin(ns, borrowed, read)
    for claim, receipt in initial.items():
        reviewed = context["claims"].get(claim) or {}
        if (receipt["uid"], receipt["volume"]) != (reviewed.get("uid"), reviewed.get("spec", {}).get("volumeName", "")):
            raise ValueError("Borrowed volume changed after review")
    ref = {"namespace": ns, "name": name, "planned": targets, "writes": [], "dispatch_protocol": 1,
           "phase": "prepared", "retain_resources": True, "claims": {v["name"]: initial.get(v["name"]) for v in prepared["volumes"]},
           "review_digest": hashlib.sha256(body["capacity_token"].encode()).hexdigest(),
           "review_expires": int(body["capacity_token"].split(".", 1)[0])}
    op = ops.start(KIND, f"Import {name}", {"kind": "Deployment", "namespace": ns, "name": name}, "/import", ref,
                   "Import intent recorded; no Kubernetes resource has been created")
    ident, dep, job = op["id"], None, None
    with LOCKS.worker_lock(ident, ops):
        writer = WRITE.ResourceWriter(send, WRITE.operation_recorder(ops, ident))
        try:
            ops.record_phase(ident, "writing", 5, "Creating reviewed import resources; each request is recorded before sending")
            verify_claims(ns, initial, read)
            if prepared["job"]:
                consumers(ns, ref["claims"], read)
            created_claims = {}
            for volume in prepared["volumes"]:
                if volume["create"]:
                    saved = create_claim(ns, volume["name"], volume["size_gb"], volume["storage_class"], volume["access_mode"], send=writer)
                    created_claims[volume["name"]] = IDENT.identity(saved)["uid"]
            pinned = pin(ns, ref["claims"], read)
            if any(pinned[n]["uid"] != uid for n, uid in created_claims.items()):
                raise ValueError("Created volume was replaced during setup")
            if any(not COPY.same_claim(before, pinned[n]) for n, before in initial.items()):
                raise ValueError("Borrowed volume changed during setup")
            ops.record_phase(ident, "writing", 20, "Volume identities recorded; preparing the workload", claims=pinned)
            if prepared["deployment"]:
                obj = prepared["deployment"]
                if prepared["job"]:
                    obj["metadata"].setdefault("annotations", {})[GUARD.DISPATCH] = ident
                if obj["spec"].get("replicas", 1):
                    if admission(obj).get("blocked") is not False:
                        raise ValueError("Imported workload no longer fits current placement")
                dep = writer("POST", IDENT.base(ns), obj)
            if prepared["service"]:
                writer("POST", f"/api/v1/namespaces/{ns}/services", prepared["service"])
            if prepared["job"]:
                obj = prepared["job"]
                obj["metadata"].setdefault("annotations", {})[GUARD.DISPATCH] = ident
                if dep:
                    obj["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "Deployment", "name": name, "uid": dep["metadata"]["uid"]}]
                helper = {"metadata": {"namespace": ns, "name": name}, "spec": {"replicas": 1, "template": obj["spec"]["template"]}}
                if admission(helper).get("blocked") is not False:
                    raise ValueError("Import helper no longer fits current placement")
                verify_claims(ns, pinned, read)
                consumers(ns, pinned, read)
                if dep:
                    current = read(IDENT.base(ns) + "/" + name)
                    if (IDENT.identity(current)["uid"] != dep["metadata"]["uid"] or current["spec"] != dep["spec"] or
                            current["spec"].get("replicas") != 0 or current["metadata"].get("annotations", {}).get(GUARD.DISPATCH) != ident):
                        raise ValueError("Imported workload changed before copying")
                job = writer("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", obj)
                ops.record_phase(ident, "writing", 30, "Copy Job identity confirmed", job=job["metadata"]["name"],
                    job_uid=job["metadata"]["uid"], job_template_digest=COPY.digest(job["spec"]["template"]))
                if dep:
                    current = read(IDENT.base(ns) + "/" + name)
                    if IDENT.identity(current)["uid"] != dep["metadata"]["uid"] or current["spec"] != dep["spec"]:
                        raise ValueError("Imported workload changed before recording the copy receipt")
                    annotations = current["metadata"].setdefault("annotations", {})
                    if annotations.get(GUARD.DISPATCH) != ident:
                        raise ValueError("Import setup hold changed")
                    annotations.pop(GUARD.DISPATCH)
                    annotations[GUARD.JOB_UID] = job["metadata"]["uid"]
                    writer("PUT", IDENT.base(ns) + "/" + name, current)
            writer.check()
            with ops._lock:
                items = ops._read()
                item = next(i for i in items if i["id"] == ident)
                item["ref"].update(phase="copying" if job else "saved", retain_resources=bool(job))
                ops._finish(item, "running" if job else "succeeded", 35 if job else 100,
                    "Copy Job created; application stays stopped until a fresh Start review" if job else
                    "Import resources saved; application readiness has not been verified")
                ops._write(items)
                return {**prepared["result"], "operation": ops._public(item)}
        except Exception:
            try:
                ops.record_phase(ident, "failed", 20, "Import setup stopped or its response is uncertain. Inspect retained resources; nothing was retried or deleted.")
            except Exception:
                pass
            raise ValueError(f"Import setup needs inspection in Recent jobs ({ident}). Resources were retained; no retry, rollback or deletion was performed.") from None


def status(item, read):
    ref = item["ref"]
    if ref.get("phase") != "copying":
        return "running", item.get("progress", 0), "Import setup is active or interrupted. Inspect outcome; no write will be automatically repeated."
    try:
        now = verify_claims(ref["namespace"], ref["claims"], read)
        ref["claims"] = now
        complete, failed = COPY.copy_finished(ref, read)
        if failed:
            raise ValueError("Copy failed")
        if complete:
            ref.update(phase="done", retain_resources=False)
            return "succeeded", 100, "Copy completed; review capacity and start the application when ready"
        return "running", 50, "Copying files; inspect the copy log for per-folder progress. Application remains stopped."
    except Exception:
        ref.update(phase="failed", retain_resources=True)
        return "failed", item.get("progress", 0), "Import copy failed or its resource identity cannot be verified. Inspect retained resources; application start remains guarded."


def recovery_blockers(item, read):
    """Releasing claim reservations must not race a confirmed/uncertain writer."""
    entries = [e for e in item["ref"].get("writes", []) if e["resource"]["kind"] == "Job"]
    if not entries:
        return []  # dispatcher lock fences unsent future Job creation
    receipt = entries[-1]
    if receipt.get("phase") != "accepted":
        return ["Copy Job creation is uncertain. Inspect it in Kubernetes; this UI cannot adopt a same-name Job or release its volume reservation."]
    target, uid = receipt["resource"], receipt["identity"]["uid"]
    job = read(f"/apis/batch/v1/namespaces/{target['namespace']}/jobs/{target['name']}")
    if IDENT.identity(job)["uid"] != uid:
        return ["Copy Job was replaced. Its identity must be inspected manually; it is not adopted."]
    terminal = any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in job.get("status", {}).get("conditions", []))
    live = any(p.get("metadata", {}).get("namespace") == target["namespace"] and
               ROLLOUT.controller(p).get("kind") == "Job" and ROLLOUT.controller(p).get("uid") == uid and
               p.get("status", {}).get("phase") not in ("Succeeded", "Failed") for p in ROLLOUT.items(read, "/api/v1/pods"))
    return [] if terminal and not live else ["The import copy is still active. Wait for the Job and its pods to finish before resolving tracking."]


def cancel_plan(item):
    return {"mode": "forget", "can": False, "why_not": "Inspect the import outcome instead; partial writes cannot safely be cancelled or forgotten",
            "keeps": ["All imported data, resources and write receipts"], "needs": "admin"}


def cancel_run(item, options):
    raise ValueError("Inspect the import outcome instead of cancelling its tracking")


def journalled(ns, name, job, ops):
    """Fence legacy name-based cleanup even when the Job is missing/replaced."""
    if job.get("metadata", {}).get("annotations", {}).get(GUARD.DISPATCH):
        return True
    with ops._lock:
        return any(i.get("kind") == KIND and any(r.get("kind") == "Job" and
            (r.get("namespace"), r.get("name")) == (ns, name) for r in i.get("ref", {}).get("planned", [])) for i in ops._read())


def remove_completed_job(ns, name, job, read, send, ops):
    """Remove only a confirmed, successfully finished helper, never its data."""
    ident = job.get("metadata", {}).get("annotations", {}).get(GUARD.DISPATCH)
    if not ident:
        raise ValueError("Import Job identity is missing or replaced; inspect Recent jobs. Nothing was deleted.")
    with LOCKS.worker_lock(ident, ops, timeout=0), ops._lock:
        items = ops._read()
        item = next((i for i in items if i["id"] == ident and i["kind"] == KIND), None)
        if not item or item.get("status") != "succeeded" or item["ref"].get("retain_resources"):
            raise ValueError("Wait for a verified successful import before removing its Job. Inspect incomplete imports in Recent jobs; all data is retained.")
        ref = item["ref"]
        if (ns, name) != (ref["namespace"], ref.get("job")) or job["metadata"].get("uid") != ref.get("job_uid"):
            raise ValueError("Import Job identity changed; nothing was deleted")
        complete, failed = COPY.copy_finished(ref, read)
        if not complete or failed:
            raise ValueError("Import completion and pod termination could not be verified")
        # Preserve proof before deleting the exact helper. CAS protects edits;
        # a missing/replaced workload is not touched or adopted.
        for owner in job["metadata"].get("ownerReferences", []):
            if owner.get("kind") != "Deployment":
                continue
            path = IDENT.base(ns) + "/" + owner["name"]
            try:
                dep = read(path)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    continue
                raise
            if IDENT.identity(dep)["uid"] != owner.get("uid"):
                raise ValueError("Import workload was replaced; nothing was deleted")
            annotations = dep["metadata"].setdefault("annotations", {})
            if annotations.get(GUARD.JOB) == name:
                if annotations.get(GUARD.JOB_UID) != ref["job_uid"] or annotations.get(GUARD.DISPATCH):
                    raise ValueError("Import completion receipt changed; nothing was deleted")
                annotations.pop(GUARD.JOB)
                annotations.pop(GUARD.JOB_UID)
                annotations["homestead.io/import-completed-job"] = ref["job_uid"]
                send("PUT", path, dep)
        send("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}",
             {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": ref["job_uid"]}, "propagationPolicy": "Background"})
        ref["helper_removed"] = True
        ops._write(items)
    return {"ok": True, "journalled": True, "name": name, "pods_removed": [], "prepulls_removed": [],
            "message": "Completed import Job removed; workload and all volumes retained"}
