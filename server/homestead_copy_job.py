"""Storage-copy handoff with durable intents, identity fences and fresh admission.

Never replay an uncertain mutation. No volume deletion or automatic rollback.
The initial multi-object edit is not atomic; a failed setup remains inspectable.
"""
import copy
import hashlib
import json

import homestead_capacity_review as REVIEW
import homestead_restructure as COPY
import homestead_rename as IDENT
import homestead_rollout_capacity as ROLLOUT
import homestead_vm_power_job as LOCKS

KIND = "workload-copy"
MARKER = "homestead.io/storage-copy-job"


def controls(dep):
    annotations = dep.get("metadata", {}).get("annotations", {})
    return {key: annotations.get(key) for key in (MARKER, COPY.HELD, "homestead.io/autostart-replicas")}


def digest(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unheld(dep):
    result = copy.deepcopy(dep)
    result.get("metadata", {}).get("annotations", {}).pop(MARKER, None)
    result.get("metadata", {}).get("annotations", {}).pop(COPY.HELD, None)
    return result


def guards(dep, read):
    # The copy's own hold is removed only in this read-only planning object.
    IDENT.guards(unheld(dep), dep["metadata"]["name"], read)


def claims(ns, moves, read, *, pending=()):
    found = {}
    for name in sorted({m[key] for m in moves for key in ("from", "to")} - set(pending)):
        pvc = read(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
        meta = IDENT.identity(pvc)
        if meta["namespace"] != ns or meta["name"] != name:
            raise ValueError("Volume identity does not match the requested copy")
        volume = pvc.get("spec", {}).get("volumeName", "")
        pv_uid = ""
        if volume:
            pv = read(f"/api/v1/persistentvolumes/{volume}")
            pv_uid = pv.get("metadata", {}).get("uid")
            if not pv_uid or pv.get("metadata", {}).get("deletionTimestamp"):
                raise ValueError("Backing volume identity is unavailable or deleting")
        found[name] = {"uid": meta["uid"], "volume": volume, "pv_uid": pv_uid}
    return found


def same_claim(before, now):
    return (now["uid"] == before["uid"] and all(not before.get(key) or now.get(key) == before[key]
                                              for key in ("volume", "pv_uid")))


def validate_sources(config, current):
    """A client cannot substitute an unrelated claim as the old mount."""
    spec = current["spec"]["template"]["spec"]
    volumes = {v["name"]: v.get("persistentVolumeClaim", {}).get("claimName") for v in spec.get("volumes", [])}
    containers = {c["name"]: c for c in spec.get("containers", [])}
    for change in config.get("containers") or []:
        original = containers.get(change.get("original_name") or change.get("name"), {})
        mounts = {m["mountPath"]: m for m in original.get("volumeMounts", [])}
        for row in change.get("volumes") or []:
            origin = row.get("copy_from")
            if not origin:
                continue
            mount = mounts.get(row.get("path"), {})
            if (mount.get("subPathExpr") or volumes.get(mount.get("name")) != origin.get("claim") or
                    COPY._folder(mount.get("subPath"), "source") != COPY._folder(origin.get("sub_path"), "source")):
                raise ValueError("Copy source no longer matches the workload's mounted volume and folder; reopen Edit")


def helper_manifest(ns, name, moves):
    _, job = COPY.job(ns, name, moves)
    return {"metadata": {"name": name, "namespace": ns},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "template": job["spec"]["template"]}}


def start(body, context, prepared, moves, read, ops, apply):
    if not REVIEW.valid(body, context) or body.get("confirm_capacity") is not True:
        raise ValueError("Review the storage copy before saving")
    ns, name = body["ns"], body["name"]
    path = IDENT.base(ns) + "/" + name
    current = read(path)
    source = IDENT.identity(current)
    if COPY.HELD in current["metadata"].get("annotations", {}):
        raise ValueError("An existing storage-copy hold needs inspection before another copy")
    if (source["uid"], source["resourceVersion"]) != (context["uid"], context["resourceVersion"]):
        raise ValueError("Workload changed after review")
    guards(current, read)
    validate_sources(body, current)
    previous = claims(ns, moves, read, pending=[row["name"] for row in prepared["claims"]])
    ref = {"namespace": ns, "name": name, "moves": moves, "deployment": source,
           "phase": "preparing", "retain_resources": True, "claims": previous,
           "review_digest": hashlib.sha256(body["capacity_token"].encode()).hexdigest(),
           "review_expires": int(body["capacity_token"].split(".", 1)[0])}
    operation = ops.start(KIND, f"Move {name}'s data", {"kind": "Deployment", "namespace": ns, "name": name},
                          "/containers", ref, "Recording storage edit; uncertain steps are never replayed")
    ident = operation["id"]
    with LOCKS.worker_lock(ident, ops):
        try:
            ops.record_phase(ident, "preparing", 5, "Saving the edited configuration stopped; original volumes are retained")
            # Re-fence after the durable intent, before the multi-object edit.
            if IDENT.identity(read(path)) != source:
                raise ValueError("Workload changed before the edit")
            prepared["deployment"].setdefault("metadata", {}).setdefault("annotations", {})[MARKER] = ident
            result = apply()
            saved = result.pop("_held_receipt")
            meta = IDENT.identity(saved)
            if meta["uid"] != source["uid"] or meta["name"] != name or meta["namespace"] != ns:
                raise ValueError("Edited Deployment receipt did not match")
            if saved["spec"].get("replicas") != 0 or saved["metadata"].get("annotations", {}).get(MARKER) != ident:
                raise ValueError("The workload is not held stopped")
            observed = read(path)
            if (IDENT.identity(observed)["uid"] != source["uid"] or observed["spec"] != saved["spec"] or
                    controls(observed) != controls(saved)):
                raise ValueError("The workload changed during setup")
            pinned = claims(ns, moves, read)
            for claim, before in previous.items():
                if not same_claim(before, pinned[claim]):
                    raise ValueError("Volume changed during setup")
            operation = ops.record_phase(ident, "stopping", 10, "Waiting for all original pods to terminate",
                deployment=meta, spec_digest=digest(saved["spec"]), controls=controls(saved),
                claims=pinned, replicas=result["held_replicas"])
            result["operation"] = operation
            return result
        except Exception:
            try:
                ops.record_phase(ident, "failed", 5, "Storage edit outcome is unverified. Inspect the workload and volumes; nothing was retried or rolled back.")
            except Exception:
                pass
            raise ValueError(f"Storage edit stopped; inspect job {ident}. Configuration or new volumes may have been saved. No automatic rollback or cleanup was performed.") from None


def held(item, read):
    ref = item["ref"]
    dep = read(IDENT.base(ref["namespace"]) + "/" + ref["name"])
    if IDENT.identity(dep)["uid"] != ref["deployment"]["uid"] or digest(dep["spec"]) != ref["spec_digest"]:
        raise IDENT.Stopped("Workload identity or configuration changed")
    if (dep["spec"].get("replicas") != 0 or dep["metadata"].get("annotations", {}).get(MARKER) != item["id"] or
            controls(dep) != ref.get("controls")):
        raise IDENT.Stopped("The storage-copy hold changed or the workload was started elsewhere")
    guards(dep, read)
    now = claims(ref["namespace"], ref["moves"], read)
    for name, before in ref["claims"].items():
        if not same_claim(before, now[name]):
            raise IDENT.Stopped("A volume was replaced or rebound during the copy")
        if not before["volume"] and now[name]["volume"]:
            ref["claims"][name] = now[name]  # pin a newly bound WFFC volume
    return dep


def quiet(dep, read):
    pods = ROLLOUT.items(read, "/api/v1/pods")
    owned, known = ROLLOUT.owned_pods(dep, pods, read, dep["metadata"]["namespace"])
    state = dep.get("status", {})
    return known and not owned and state.get("observedGeneration", -1) >= dep["metadata"].get("generation", 0) and not state.get("replicas", 0)


def copy_finished(ref, read):
    ns = ref["namespace"]
    job = read(f"/apis/batch/v1/namespaces/{ns}/jobs/{ref['job']}")
    if IDENT.identity(job)["uid"] != ref["job_uid"]:
        raise IDENT.Stopped("The copy Job was replaced; its result is not trusted")
    if digest(job.get("spec", {}).get("template")) != ref.get("job_template_digest"):
        raise IDENT.Stopped("The copy Job configuration changed; inspect its result")
    conditions = job.get("status", {}).get("conditions", [])
    failed = any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions)
    complete = any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions)
    pods = ROLLOUT.items(read, "/api/v1/pods")
    live = [p for p in pods if p.get("metadata", {}).get("namespace") == ns and
            ROLLOUT.controller(p).get("kind") == "Job" and ROLLOUT.controller(p).get("uid") == ref["job_uid"] and
            p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    return complete and not live, failed and not live


def exclusive(ref, read):
    """No other observed pod may read/write the locations during handoff."""
    names = set(ref["claims"])
    for pod in ROLLOUT.items(read, "/api/v1/pods"):
        if pod.get("metadata", {}).get("namespace") != ref["namespace"] or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        owner = ROLLOUT.controller(pod)
        if ref.get("job_uid") and owner.get("kind") == "Job" and owner.get("uid") == ref["job_uid"]:
            continue
        if any(v.get("persistentVolumeClaim", {}).get("claimName") in names for v in pod.get("spec", {}).get("volumes", [])):
            raise IDENT.Stopped("Another active pod uses a copy volume; stop its consumers before moving this data")


def resolve(item, read, send, ops, admission):
    ref, percent = item["ref"], item.get("progress", 0)
    phase = ref.get("phase")
    if phase in ("preparing", "copy_intent", "release_intent", "recovery_intent"):
        return "running", percent, "A request may be unfinished. Inspect outcome; Homestead will not replay it."
    try:
        if phase == "starting":
            dep = read(IDENT.base(ref["namespace"]) + "/" + ref["name"])
            if IDENT.identity(dep)["uid"] != ref["deployment"]["uid"] or digest(dep["spec"]) != ref["running_digest"]:
                raise IDENT.Stopped("Workload changed while waiting for readiness")
            state, count = dep.get("status", {}), ref["replicas"]
            ready = state.get("observedGeneration", -1) >= dep["metadata"].get("generation", 0) and all(
                state.get(key, 0) == count for key in ("replicas", "readyReplicas", "availableReplicas", "updatedReplicas"))
            if ready:
                ref.update(phase="done", retain_resources=False)
                return "succeeded", 100, "Copy finished; workload is ready" if count else "Copy finished; workload stays stopped as requested"
            return "running", 90, "Copy finished; waiting for workload readiness (not an application-health check)"
        dep = held(item, read)
        if not quiet(dep, read):
            return "running", percent or 10, "Waiting for all workload pods to terminate, including terminating pods"
        exclusive(ref, read)
        if phase == "stopping":
            job_name, body = COPY.job(ref["namespace"], ref["name"], ref["moves"])
            check = admission(helper_manifest(ref["namespace"], ref["name"], ref["moves"]))
            if check.get("blocked") is not False:
                raise IDENT.Stopped("The copy helper cannot fit current placement or storage constraints")
            if not quiet(held(item, read), read):
                return "running", 10, "Waiting for workload pods to terminate after the placement check"
            exclusive(ref, read)
            ref.update(phase="copy_intent", job=job_name)
            ops._finish(item, "running", 20, f"Submitting copy Job {ref['namespace']}/{job_name}; an uncertain request will not be replayed")
            ops.checkpoint(item)  # before POST; never adopt a same-name Job on retry
            receipt = send("POST", f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs", body)
            meta = IDENT.identity(receipt)
            if (meta["namespace"], meta["name"], receipt.get("kind"), receipt.get("apiVersion")) != (ref["namespace"], job_name, "Job", "batch/v1"):
                raise IDENT.Stopped("Copy Job creation receipt did not match")
            ref.update(phase="copying", job_uid=meta["uid"], job_template_digest=digest(receipt["spec"]["template"]))
            ops.checkpoint(item)
            return "running", 30, "Copying data; original volumes are retained"
        if phase != "copying":
            raise IDENT.Stopped("Copy phase is unknown; inspect retained resources")
        complete, failed = copy_finished(ref, read)
        if failed:
            raise IDENT.Stopped("Copy failed; the destination may contain partial data")
        if not complete:
            return "running", 45, "Waiting for the copy Job to finish and release its pods"
        proposed = unheld(dep)
        proposed["spec"]["replicas"] = ref["replicas"]
        check = admission(proposed)
        if check.get("blocked") is not False:
            raise IDENT.Stopped("Data copied, but the workload cannot fit current placement or storage constraints")
        dep = held(item, read)
        if not quiet(dep, read) or not copy_finished(ref, read)[0]:
            raise IDENT.Stopped("Workload or copy state changed before restart")
        exclusive(ref, read)
        meta = IDENT.identity(dep)
        patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
                 {"op": "add", "path": "/spec/replicas", "value": ref["replicas"]}]
        for key in (MARKER, COPY.HELD):
            if key in dep["metadata"].get("annotations", {}):
                patch.append({"op": "remove", "path": "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")})
        ref.update(phase="release_intent", running_digest=digest(proposed["spec"]))
        ops._finish(item, "running", 75, "Copy completed and fresh placement checked; recording the restart request")
        ops.checkpoint(item)
        result = send("PATCH", IDENT.base(ref["namespace"]) + "/" + ref["name"], patch, ctype="application/json-patch+json")
        if IDENT.identity(result)["uid"] != meta["uid"] or digest(result["spec"]) != ref["running_digest"] or result["metadata"].get("annotations", {}).get(MARKER):
            raise IDENT.Stopped("Restart response did not confirm the reviewed configuration")
        ref["phase"] = "starting"
        ops.checkpoint(item)
        return "running", 85, "Copy finished; restart accepted, checking readiness"
    except Exception as error:
        reason = str(error) if isinstance(error, IDENT.Stopped) else "Copy step could not be verified"
        # Preserve intent phase when the response is uncertain; never infer
        # success from a same-name replacement or repeat the upstream request.
        ref["retain_resources"] = True
        return "failed", percent, reason + ". Inspect outcome before starting anything; volumes were retained and no rollback was attempted."


def legacy_status(item):
    item["ref"]["retain_resources"] = True
    return "failed", item.get("progress", 0), "Legacy storage move has no identity receipts. Inspect its workload and copy Job; no automatic restart, retry or rollback was performed."


def recovery_plan(item, read, ops):
    ref = item["ref"]
    plan = {"mode": "forget", "action": "Release hold and stop tracking", "needs": "admin", "confirm": ref["name"],
            "lead": "This releases the copy safety hold only. It does not start the app, restore storage mappings, or delete data.",
            "copy_recovery": True,
            "can": False, "why_not": "Setup or copy state cannot be verified", "keeps": [
                "All volumes and copy Jobs remain. No workload is started and no data is rolled back.",
                "Verify the intended storage and any partial data, then use Edit or a fresh Start review."],
            "options": [{"id": "ack", "label": "I checked the storage and accept any partial or late effects",
                         "detail": "This does not restore old mappings or prove the data is complete.", "default": False}]}
    try:
        with LOCKS.worker_lock(item["id"], ops, timeout=.01):
            dep = read(IDENT.base(ref["namespace"]) + "/" + ref["name"])
            if IDENT.identity(dep)["uid"] != ref["deployment"]["uid"]:
                return {**plan, "why_not": "Workload was replaced; its hold will not be changed"}
            if ref.get("job"):
                if not ref.get("job_uid"):
                    return {**plan, "why_not": "Copy creation outcome is unknown. Inspect the recorded Job in Kubernetes; this UI will not adopt or delete it by name."}
                complete, failed = copy_finished(ref, read)
                if not (complete or failed):
                    return {**plan, "why_not": "Copy is still active. Wait for its pods to finish before releasing the hold."}
            if dep["metadata"].get("annotations", {}).get(MARKER) not in (None, item["id"]):
                return {**plan, "why_not": "Another operation owns this workload's hold"}
            plan.update(can=True, why_not="")
    except Exception:
        pass
    return plan


def recovery_run(item, options, read, send, ops):
    if options.get("ack") is not True:
        raise ValueError("Acknowledge partial data and late effects first")
    with LOCKS.worker_lock(item["id"], ops, timeout=.01):
        # cancel() holds the journal's cancelling state; polling cannot advance.
        ref = item["ref"]
        dep = read(IDENT.base(ref["namespace"]) + "/" + ref["name"])
        meta = IDENT.identity(dep)
        if meta["uid"] != ref["deployment"]["uid"]:
            raise ValueError("Workload was replaced; no hold was changed")
        if ref.get("job") and (not ref.get("job_uid") or not any(copy_finished(ref, read))):
            raise ValueError("Copy completion cannot be verified; no hold was changed")
        annotations = dep["metadata"].get("annotations", {})
        if annotations.get(MARKER) not in (None, item["id"]):
            raise ValueError("Another operation owns the hold")
        if annotations.get(MARKER) == item["id"]:
            patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                     {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
            patch += [{"op": "remove", "path": "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")}
                      for key in (MARKER, COPY.HELD) if key in annotations]
            # The generic canceller has durably recorded cancelling before
            # this metadata-only CAS. A retry cannot start or delete anything.
            result = send("PATCH", IDENT.base(ref["namespace"]) + "/" + ref["name"], patch, ctype="application/json-patch+json")
            if IDENT.identity(result)["uid"] != meta["uid"] or result["metadata"].get("annotations", {}).get(MARKER):
                raise ValueError("Hold release was not confirmed; inspect before retrying")
        ref["retain_resources"] = False
        return "Safety hold released; workload replicas, storage mappings, volumes and copy Job were not changed. Review the data before starting."
