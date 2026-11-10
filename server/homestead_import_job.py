"""One-shot import setup and UID-bound copy observation, without replay/cleanup.

Only identities, hashes and receipt metadata reach the durable journal. Import
manifests can contain application credentials and must stay request-local.
"""
import copy
import hashlib
import urllib.error

import homestead_copy_job as COPY
import homestead_env_secrets as ENVSEC
import homestead_capacity_review as REVIEW
import homestead_import_guard as GUARD
import homestead_kube as KUBE
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
    if prepared.get("source_secret"):
        out.append({"apiVersion": "v1", "kind": "Secret", "namespace": ns, "name": prepared["source_secret"]["copy"]})
    return out


def source_copy(spec, ns, job, read):
    """The source's password, beside a copy that runs in another namespace.
    Owned by the copy Job, so it goes with it; the import page removes it
    as soon as the copy has finished."""
    original = read(f"/api/v1/namespaces/{spec['namespace']}/secrets/{spec['name']}")
    if not (original.get("data") or {}).get("password"):
        raise ValueError("The source's password is unavailable. Check the source before importing.")
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": spec["copy"], "namespace": ns,
                         "labels": {"homestead.io/import-source-copy": job["metadata"]["name"]},
                         "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": job["metadata"]["name"],
                                              "uid": job["metadata"]["uid"]}]},
            "data": {"password": original["data"]["password"]}}


def pin(ns, names, read):
    # Reuse PVC/PV identity verification; these synthetic pairs only enumerate
    # claims and never generate a local-copy manifest or script.
    return COPY.claims(ns, [{"from": name, "to": name} for name in names], read)


def verify_claims(ns, expected, read):
    now = pin(ns, expected, read)
    if any(not COPY.same_claim(before, now[name]) for name, before in expected.items()):
        raise ValueError("An import volume was replaced or bound again. Check it before continuing.")
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
           # The claims this import creates, so a removal can tell them from borrowed ones.
           "created": [v["name"] for v in prepared["volumes"] if v["create"]],
           "phase": "prepared", "retain_resources": True, "claims": {v["name"]: initial.get(v["name"]) for v in prepared["volumes"]},
           "review_digest": hashlib.sha256(body["capacity_token"].encode()).hexdigest(),
           "review_expires": int(body["capacity_token"].split(".", 1)[0])}
    op = ops.start(KIND, f"Import {name}", {"kind": "Deployment", "namespace": ns, "name": name}, "/import", ref,
                   "Import recorded. No Kubernetes resource has been created yet.")
    ident, dep, job = op["id"], None, None
    with LOCKS.worker_lock(ident, ops):
        writer = WRITE.ResourceWriter(send, WRITE.operation_recorder(ops, ident))
        try:
            ops.record_phase(ident, "writing", 5, "Creating the reviewed import resources. Each request is recorded before it is sent.")
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
            ops.record_phase(ident, "writing", 20, "Volumes recorded. Preparing the app.", claims=pinned)
            if prepared["deployment"]:
                obj = prepared["deployment"]
                if prepared["job"]:
                    obj["metadata"].setdefault("annotations", {})[GUARD.DISPATCH] = ident
                if obj["spec"].get("replicas", 1):
                    if admission(obj).get("blocked") is not False:
                        raise ValueError("Imported workload no longer fits current placement")
                # Imported passwords and keys go into the app's Secret, not its manifest.
                ENVSEC.externalize(ns, obj, read, writer)
                dep = writer("POST", IDENT.base(ns), obj)
            if prepared["service"]:
                # The import's stamp, so a removal can tell this Service from one made by hand.
                prepared["service"]["metadata"].setdefault("annotations", {})[GUARD.DISPATCH] = ident
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
                # A copy pod that never schedules, or a copy that hangs, ends
                # at a deadline sized from the volumes it fills.
                sizes = [int(v["size_gb"]) if v.get("size_gb") else None for v in prepared["volumes"]]
                gib = None if None in sizes else sum(sizes)
                obj["spec"]["activeDeadlineSeconds"] = COPY.deadline(gib)
                job = writer("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", obj)
                if prepared.get("source_secret"):
                    # The copy waits for its password, then starts.
                    writer("POST", f"/api/v1/namespaces/{ns}/secrets", source_copy(prepared["source_secret"], ns, job, read))
                ops.record_phase(ident, "writing", 30, "Copy Job identity confirmed", job=job["metadata"]["name"],
                    job_uid=job["metadata"]["uid"], job_template_digest=COPY.digest(job["spec"]["template"]), copy_gib=gib)
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
            with ops.transaction() as t:
                items = t.items
                item = next(i for i in items if i["id"] == ident)
                item["ref"].update(phase="copying" if job else "saved", retain_resources=bool(job))
                ops._finish(item, "running" if job else "succeeded", 35 if job else 100,
                    "Copy Job created. The app stays stopped until a new Start review." if job else
                    "Import resources saved. Whether the app is ready has not been checked.")
                t.save(items)
                return {**prepared["result"], "operation": ops._public(item)}
        except Exception:
            try:
                ops.record_phase(ident, "failed", 20, "The import setup stopped, or its response is uncertain. Check the kept resources. Nothing was retried or deleted.")
            except Exception:
                pass
            raise ValueError(f"The import setup needs to be checked in Recent jobs ({ident}). The resources were kept. Nothing was retried, rolled back or deleted.") from None


REMOVABLE = ("failed", "cancelled")


def _item(ns, name, job, ops):
    """The import's journal entry: by the stamp on its Job, else by the Job it planned."""
    ident = (job.get("metadata") or {}).get("annotations", {}).get(GUARD.DISPATCH) if job else None
    with ops.transaction() as t:
        return next((i for i in t.items if i.get("kind") == KIND and (i["id"] == ident or (not ident and any(
            r.get("kind") == "Job" and (r.get("namespace"), r.get("name")) == (ns, name)
            for r in i.get("ref", {}).get("planned", []))))), None)


def _stamped(obj, ident, ref):
    """Whether this import made the object: it carries the import's stamp. A name alone is not proof."""
    notes = (obj.get("metadata") or {}).get("annotations") or {}
    return notes.get(GUARD.DISPATCH) == ident or bool(ref.get("job_uid") and notes.get(GUARD.JOB_UID) == ref["job_uid"])


def _find(path, read):
    try:
        return read(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def cleanup_plan(ns, name, job, read, ops):
    """What removing a failed or cancelled import deletes, for its Remove review.
    Only objects carrying the import's stamp. Volumes are listed apart, and only
    those this import created."""
    plan = {"job": name, "namespace": ns, "workload": "", "volume": "", "volumes": [], "known": True,
            "journalled": True, "removable": False, "services": []}
    item = _item(ns, name, job, ops)
    if not item or item.get("status") not in REMOVABLE:
        return plan
    ref, ident = item["ref"], item["id"]
    plan["removable"] = True
    app = _find(IDENT.base(ns) + "/" + ref["name"], read)
    if app and _stamped(app, ident, ref):
        plan["workload"] = ref["name"]
    svc = _find(f"/api/v1/namespaces/{ns}/services/{ref['name']}", read)
    if svc and _stamped(svc, ident, ref):
        plan["services"] = [ref["name"]]
    for claim in ref.get("created", []):
        pinned = (ref.get("claims") or {}).get(claim) or {}
        found = _find(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}", read)
        if found and pinned.get("uid") and IDENT.identity(found)["uid"] == pinned["uid"]:
            plan["volumes"].append({"name": claim, "created": True})
    return plan


def remove_failed(ns, name, job, read, send, ops, volumes=()):
    """Remove a failed or cancelled import: its stamped app, Service and copy Job,
    each by UID. Volumes only when named and this import's own (same UID it made).
    Anything without the stamp, or replaced since, is left alone."""
    item = _item(ns, name, job, ops)
    if not item or item.get("status") not in REMOVABLE:
        raise ValueError("Only a failed or cancelled import can be removed here. Check Recent jobs. All data is kept.")
    ident = item["id"]
    with LOCKS.worker_lock(ident, ops, timeout=0), ops.transaction() as t:
        item = next(i for i in t.items if i["id"] == ident)
        ref, removed = item["ref"], []
        wanted = [str(v) for v in volumes]
        pinned = ref.get("claims") or {}
        for claim in wanted:
            if claim not in ref.get("created", []) or not (pinned.get(claim) or {}).get("uid"):
                raise ValueError(f"This import did not create {claim}, so it is not deleted here.")
        def drop(path, label):
            found = _find(path, read)
            if found is None:
                return None
            send("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": IDENT.identity(found)["uid"]},
                                  "propagationPolicy": "Background"})
            removed.append(label)
        app = _find(IDENT.base(ns) + "/" + ref["name"], read)
        if app and _stamped(app, ident, ref):
            drop(IDENT.base(ns) + "/" + ref["name"], f"app {ref['name']}")
        svc = _find(f"/api/v1/namespaces/{ns}/services/{ref['name']}", read)
        if svc and _stamped(svc, ident, ref):
            drop(f"/api/v1/namespaces/{ns}/services/{ref['name']}", f"service {ref['name']}")
        copy_job = _find(f"/apis/batch/v1/namespaces/{ns}/jobs/{name}", read)
        if copy_job and _stamped(copy_job, ident, ref):
            drop(f"/apis/batch/v1/namespaces/{ns}/jobs/{name}", f"copy job {name}")
        for claim in wanted:
            found = _find(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}", read)
            if found is None:
                continue
            if IDENT.identity(found)["uid"] != pinned[claim]["uid"]:
                raise ValueError(f"{claim} was replaced after this import, so it is not deleted.")
            drop(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}", f"volume {claim}")
        ref.update(retain_resources=False, helper_removed=True)
        t.save(t.items)
    kept = [c for c in ref.get("created", []) if c not in wanted]
    return {"ok": True, "journalled": True, "name": name, "removed": removed, "pods_removed": [], "prepulls_removed": [],
            "message": "Import removed." + (f" Kept volumes: {', '.join(kept)}." if kept else "")}


def status(item, read):
    ref = item["ref"]
    if ref.get("phase") != "copying":
        return "running", item.get("progress", 0), "The import setup is running or was interrupted. Choose Check outcome. Nothing is written again automatically."
    try:
        now = verify_claims(ref["namespace"], ref["claims"], read)
        ref["claims"] = now
        complete, failed = COPY.copy_finished(ref, read)
        if failed:
            why = COPY.failure(read(f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs/{ref['job']}"),
                               ref.get("copy_gib"))
            ref.update(phase="failed", retain_resources=True)
            app = (f" {ref['name']} was created stopped for this import and stays stopped, since its data is incomplete."
                   if any(t.get("kind") == "Deployment" for t in ref.get("planned", [])) else "")
            return "failed", item.get("progress", 0), (
                f"{why}.{app} Its volumes and the copy Job were kept. Remove the import and run it again.")
        if complete:
            ref.update(phase="done", retain_resources=False)
            return "succeeded", 100, "Copy finished. Review the capacity and start the app when ready."
        return "running", 50, "Copying files. The copy log shows the progress of each folder. The app stays stopped."
    except Exception:
        ref.update(phase="failed", retain_resources=True)
        return "failed", item.get("progress", 0), "The import copy failed, or its resources cannot be confirmed. Check the kept resources. The app is not started until that is done."


def recovery_blockers(item, read):
    """Releasing claim reservations must not race a confirmed/uncertain writer."""
    entries = [e for e in item["ref"].get("writes", []) if e["resource"]["kind"] == "Job"]
    if not entries:
        return []  # dispatcher lock fences unsent future Job creation
    receipt = entries[-1]
    if receipt.get("phase") != "accepted":
        return ["Whether the copy Job was created is uncertain. Check it in Kubernetes. Homestead cannot take over a Job with the same name or release its volume reservation."]
    target, uid = receipt["resource"], receipt["identity"]["uid"]
    job = read(f"/apis/batch/v1/namespaces/{target['namespace']}/jobs/{target['name']}")
    if IDENT.identity(job)["uid"] != uid:
        return ["The copy Job was replaced. Check it manually. Homestead does not take it over."]
    terminal = any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in job.get("status", {}).get("conditions", []))
    live = any(p.get("metadata", {}).get("namespace") == target["namespace"] and
               ROLLOUT.controller(p).get("kind") == "Job" and ROLLOUT.controller(p).get("uid") == uid and
               p.get("status", {}).get("phase") not in ("Succeeded", "Failed") for p in ROLLOUT.items(read, "/api/v1/pods"))
    return [] if terminal and not live else ["The import copy is still active. Wait for the Job and its pods to finish before resolving tracking."]


def cancel_plan(item):
    """While the copy runs it can be stopped. Read from the job alone: the job
    tray asks on every poll."""
    ref = item.get("ref") or {}
    if ref.get("phase") != "copying" or not ref.get("job") or not ref.get("job_uid"):
        return {"mode": "forget", "can": False, "needs": "admin",
                "why_not": "Check the import outcome instead. Its setup is not at a step that can be stopped, and incomplete writes cannot safely be ignored.",
                "keeps": ["All imported data, resources and write receipts"]}
    return {"mode": "stop", "action": "Stop the copy", "needs": "admin",
            "undo": ["The copy Job is deleted, which ends its pod and the source password copy it owns",
                     "The copy's log goes with the Job"],
            "keeps": ["The new volumes keep whatever was copied so far, which is incomplete",
                      "The imported application stays stopped: Start refuses it, since its copy did not finish"]}


def cancel_run(item, options, read=None, send=None):
    """Delete the copy Job this import created, by its UID, and nothing else.
    Safe to run again: a Job already gone is the goal, a Job that is not ours or
    has finished is refused so the import reports the real outcome."""
    read, send = read or KUBE.kget, send or KUBE.ksend
    ref = item["ref"]
    ns, name, uid = ref["namespace"], ref.get("job"), ref.get("job_uid")
    if ref.get("phase") != "copying" or not name or not uid:
        raise ValueError("Inspect the import outcome instead of cancelling its tracking")
    path = f"/apis/batch/v1/namespaces/{ns}/jobs/{name}"
    for _ in range(3):
        try:
            job = read(path)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            break
        if IDENT.identity(job)["uid"] != uid:
            raise ValueError("The copy Job was replaced, so Homestead does not delete it. Check it in Kubernetes.")
        if any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
               for c in job.get("status", {}).get("conditions", [])):
            raise ValueError("The copy has already finished. Wait for the import to report its outcome.")
        try:
            send("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions",
                                  "preconditions": {"uid": uid, "resourceVersion": job["metadata"]["resourceVersion"]},
                                  "propagationPolicy": "Background"})
            break
        except urllib.error.HTTPError as error:
            if error.code == 404:
                break
            if error.code != 409:
                raise
            # The Job changed since the read: look again, so a copy that has
            # just finished is not deleted and a status update is not a refusal.
    else:
        raise ValueError("The copy changed while it was being stopped. Try again.")
    ref.update(phase="failed", retain_resources=True)
    app = (f" {ref['name']} was created stopped for this import and stays stopped, since its data is incomplete."
           if any(t.get("kind") == "Deployment" for t in ref.get("planned", [])) else "")
    return ("Copy stopped. The Job was deleted and its pod is stopping." + app +
            f" Its volumes were kept, with whatever was copied so far. To import again, delete {ref['name']} and its volumes, then import again.")


def journalled(ns, name, job, ops):
    """Fence legacy name-based cleanup even when the Job is missing/replaced."""
    if job.get("metadata", {}).get("annotations", {}).get(GUARD.DISPATCH):
        return True
    with ops.transaction():
        return any(i.get("kind") == KIND and any(r.get("kind") == "Job" and
            (r.get("namespace"), r.get("name")) == (ns, name) for r in i.get("ref", {}).get("planned", [])) for i in ops.stored())


def remove_completed_job(ns, name, job, read, send, ops):
    """Remove only a confirmed, successfully finished helper, never its data."""
    ident = job.get("metadata", {}).get("annotations", {}).get(GUARD.DISPATCH)
    if not ident:
        raise ValueError("The import Job is missing or was replaced. Check Recent jobs. Nothing was deleted.")
    with LOCKS.worker_lock(ident, ops, timeout=0), ops.transaction() as t:
        items = t.items
        item = next((i for i in items if i["id"] == ident and i["kind"] == KIND), None)
        if not item or item.get("status") != "succeeded" or item["ref"].get("retain_resources"):
            raise ValueError("Wait for a confirmed successful import before removing its Job. Check unfinished imports in Recent jobs. All data is kept.")
        ref = item["ref"]
        if (ns, name) != (ref["namespace"], ref.get("job")) or job["metadata"].get("uid") != ref.get("job_uid"):
            raise ValueError("The import Job changed, so nothing was deleted")
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
                raise ValueError("The imported app was replaced, so nothing was deleted")
            annotations = dep["metadata"].setdefault("annotations", {})
            if annotations.get(GUARD.JOB) == name:
                if annotations.get(GUARD.JOB_UID) != ref["job_uid"] or annotations.get(GUARD.DISPATCH):
                    raise ValueError("The import's completion record changed, so nothing was deleted")
                annotations.pop(GUARD.JOB)
                annotations.pop(GUARD.JOB_UID)
                annotations["homestead.io/import-completed-job"] = ref["job_uid"]
                send("PUT", path, dep)
        send("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}",
             {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": ref["job_uid"]}, "propagationPolicy": "Background"})
        ref["helper_removed"] = True
        t.save(items)
    return {"ok": True, "journalled": True, "name": name, "pods_removed": [], "prepulls_removed": [],
            "message": "Finished import Job removed. The app and all volumes are kept."}


def register(ops):
    """The job store's entries for creating an app from an import (#622)."""
    ops.RESOLVERS[KIND] = lambda item: status(item, KUBE.kget)
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
