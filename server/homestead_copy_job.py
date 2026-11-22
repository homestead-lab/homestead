"""Storage-copy handoff with durable intents, identity fences and fresh admission.

Never replay an uncertain mutation. No volume deletion or automatic rollback.
The initial multi-object edit is not atomic; a failed setup remains inspectable.
"""
import base64
import copy
import hashlib
import json
import time
import urllib.error

import homestead_cancel_rerun as CANCEL_RERUN
import homestead_capacity_review as REVIEW
import homestead_names as NAMES
import homestead_pod_resources as SIZE
import homestead_restructure as COPY
import homestead_rename as IDENT
import homestead_rollout_capacity as ROLLOUT
import homestead_vm_power_job as LOCKS

KIND = "workload-copy"
MARKER = "homestead.io/storage-copy-job"
# The Deployment's pod template and replicas from before the edit, kept in a
# Secret beside it so a failed copy can undo the edit. Not on the Deployment,
# whose annotations a viewer sees in full, and not in the journal: a template
# can carry credentials in plain env values. The journal keeps the Secret's
# name, uid and the record's digest, so a changed record is not applied. The
# Secret is owned by the Deployment, so it goes with it, and is removed once
# the copy succeeded or the edit was undone.
EARLIER_PREFIX = "homestead-copy-earlier-"
EARLIER_LABEL = "homestead.io/storage-copy-earlier"
# How long a copy Job may run, pod scheduling and image pull included, before
# Kubernetes stops it (activeDeadlineSeconds). A floor of 30 minutes covers a
# slow pull and a small copy; each GiB to copy adds 2 minutes, about 8.5 MiB/s,
# which a slow disk or a replicated network volume still manages. The 48-hour
# ceiling (reached at about 1.4 TiB) bounds how long an app whose copy pod never
# schedules stays stopped; a copy of unknown size gets the ceiling.
DEADLINE_FLOOR = 30 * 60
SECONDS_PER_GIB = 120
DEADLINE_CEILING = 48 * 3600
# Answers to the copy Job's create that say no Job was created. A 409 (a Job of
# that name is there), a 5xx or no answer at all say nothing about it.
NOT_CREATED = {400, 403, 404, 422, 429}
# How long after the create was recorded a Job read by name as missing (404)
# still counts as perhaps on its way: the answer was lost, so the create may
# still be done. The client gives up after 15 seconds and the API server ends a
# request after 60 by default; 120 leaves room for a write finishing late.
CREATE_SETTLE = 120


class KeptStopped(IDENT.Stopped):
    """A failed copy whose app stays stopped; the message already says why and what was kept."""


def deadline(gib):
    """Seconds a copy of gib GiB may take; None is a size not known."""
    if gib is None:
        return DEADLINE_CEILING
    return max(DEADLINE_FLOOR, min(DEADLINE_CEILING, DEADLINE_FLOOR + SECONDS_PER_GIB * max(0, int(gib))))


def claim_gib(pvc):
    """A claim's size in whole GiB (what it holds at most), or None when not known."""
    return SIZE.storage_gib(pvc.get("status", {}).get("capacity", {}).get("storage") or
                            pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")) or None


def copy_gib(ns, moves, read):
    """The data a copy carries, at most: each source claim it reads, once. None if any size is not known."""
    sizes = [claim_gib(read(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}"))
             for name in sorted({m["from"] for m in moves if m.get("data", True) is not False})]
    return None if None in sizes else sum(sizes)


def _duration(seconds):
    if seconds < 3600:
        return f"{-(-seconds // 60)} minutes"
    hours = round(seconds / 3600, 1)
    return f"{hours:g} hour" + ("" if hours == 1 else "s")


def failure(job, gib=None):
    """Why a failed copy Job failed, in words for the person whose app it is."""
    for c in job.get("status", {}).get("conditions", []):
        if c.get("type") == "Failed" and c.get("status") == "True":
            if c.get("reason") == "DeadlineExceeded":
                seconds = int(job.get("spec", {}).get("activeDeadlineSeconds") or 0)
                size = f"{gib} GiB" if gib else "data of unknown size"
                limit = f" within {_duration(seconds)}" if seconds else " within its time limit"
                return f"Copying {size} did not finish{limit}: its pod could not be scheduled or the copy hung"
            return "The copy failed" + (f" ({c['reason']})" if c.get("reason") else "")
    return "The copy failed"


def earlier(dep):
    """The record a failed copy undoes the edit with: the template and replicas as JSON."""
    return json.dumps({"template": dep["spec"]["template"], "replicas": dep["spec"].get("replicas", 1)},
                      sort_keys=True, separators=(",", ":"))


def earlier_name(uid):
    return EARLIER_PREFIX + uid


def _earlier_path(ref):
    return f"/api/v1/namespaces/{ref['namespace']}/secrets/{ref['earlier_secret']}"


def earlier_secret(ns, name, uid, text):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": earlier_name(uid), "namespace": ns,
                         "labels": {EARLIER_LABEL: NAMES.fitted(name), "app.kubernetes.io/managed-by": "homestead"},
                         "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": name, "uid": uid}]},
            "data": {"earlier": base64.b64encode(text.encode()).decode()}}


def read_earlier(ref, read):
    """The recorded text, None when it is gone, replaced or does not match its digest."""
    if not ref.get("earlier_secret") or not ref.get("earlier_uid"):
        return None
    try:
        secret = read(_earlier_path(ref))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if IDENT.identity(secret)["uid"] != ref["earlier_uid"]:
        return None
    text = base64.b64decode((secret.get("data") or {}).get("earlier") or "").decode("utf-8", "replace")
    return text if _sha(text) == ref.get("earlier_digest") else None


def drop_earlier(ref, send):
    """Remove the record once it is no longer needed; a note when that failed."""
    if not ref.get("earlier_secret") or not ref.get("earlier_uid") or ref.get("earlier_dropped"):
        return ""
    try:
        send("DELETE", _earlier_path(ref), {"apiVersion": "v1", "kind": "DeleteOptions",
                                            "preconditions": {"uid": ref["earlier_uid"]}})
    except urllib.error.HTTPError as error:
        if error.code != 404:
            return (f" Its record of before, Secret {ref['earlier_secret']}, could not be removed "
                    f"(HTTP {error.code}). Delete it when it is no longer needed.")
    except Exception:
        return (f" Its record of before, Secret {ref['earlier_secret']}, could not be removed. "
                "Delete it when it is no longer needed.")
    ref["earlier_dropped"] = True
    return ""


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _removals(annotations):
    return [{"op": "remove", "path": "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")}
            for key in (MARKER, COPY.HELD) if key in annotations]


def _starter():
    """Who the request starting this copy is for, so its later steps are theirs (#621)."""
    import homestead_changes as CHANGES
    return getattr(CHANGES.REQUEST, "user", None) or ""


def _as(who, write):
    """Run write as who's: the jobs loop serves no request, so the change mark
    would say the job. A copy journalled with no starter stays the job's; a
    starter whose account is gone by then signs a mark that no longer checks
    out, read as outside Homestead, as for any write by a removed account."""
    import homestead_changes as CHANGES
    was = getattr(CHANGES.REQUEST, "user", None)
    CHANGES.REQUEST.user = who or None
    try:
        return write()
    finally:
        CHANGES.REQUEST.user = was


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
                raise ValueError("The copy source no longer matches the app's mounted volume and folder. Open Edit again.")


def helper_manifest(ns, name, moves):
    _, job = COPY.job(ns, name, moves)
    return {"metadata": {"name": name, "namespace": ns},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "template": job["spec"]["template"]}}


def start(body, context, prepared, moves, read, ops, apply, send=None):
    if send is None:
        import homestead_kube as KUBE
        send = KUBE.ksend
    if not REVIEW.valid(body, context) or body.get("confirm_capacity") is not True:
        raise ValueError("Review the storage copy before saving")
    ns, name = body["ns"], body["name"]
    path = IDENT.base(ns) + "/" + name
    current = read(path)
    source = IDENT.identity(current)
    if COPY.HELD in current["metadata"].get("annotations", {}):
        raise ValueError("An existing storage-copy hold needs inspection before another copy")
    if (source["uid"], source["resourceVersion"]) != (context["uid"], context["resourceVersion"]):
        raise ValueError("The app changed after the review")
    guards(current, read)
    validate_sources(body, current)
    previous = claims(ns, moves, read, pending=[row["name"] for row in prepared["claims"]])
    ref = {"namespace": ns, "name": name, "moves": moves, "deployment": source,
           "phase": "preparing", "retain_resources": True, "claims": previous,
           "review_digest": hashlib.sha256(body["capacity_token"].encode()).hexdigest(),
           "review_expires": int(body["capacity_token"].split(".", 1)[0]), "started_by": _starter()}
    record = earlier(current)
    ref["earlier_digest"] = _sha(record)
    not_started = ""
    operation = ops.start(KIND, f"Move {name}'s data", {"kind": "Deployment", "namespace": ns, "name": name},
                          "/containers", ref, "Recording the storage edit. An uncertain step is never repeated.")
    ident = operation["id"]
    with LOCKS.worker_lock(ident, ops):
        try:
            ops.record_phase(ident, "preparing", 5, "Saving the edited settings stopped. The original volumes are kept.")
            # Re-fence after the durable intent, before the multi-object edit.
            if IDENT.identity(read(path)) != source:
                raise ValueError("Workload changed before the edit")
            # The record of before is saved first: without it a failed copy
            # could not undo the edit, so no copy starts.
            try:
                kept = send("POST", f"/api/v1/namespaces/{ns}/secrets", earlier_secret(ns, name, source["uid"], record))
            except Exception:
                not_started = (f"The storage copy did not start: Homestead could not save {name}'s configuration from "
                               f"before the edit (Secret {earlier_name(source['uid'])}), which it needs to undo the edit "
                               f"if the copy fails. Nothing was changed, and {name} is still running as it was.")
                raise
            ops.record_phase(ident, "preparing", 5, "Settings from before the edit recorded. Saving the edit stopped.",
                             earlier_secret=kept["metadata"]["name"], earlier_uid=IDENT.identity(kept)["uid"])
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
                ops.record_phase(ident, "failed", 5, not_started or "The outcome of the storage edit is not confirmed. Check the app and volumes. Nothing was retried or rolled back.")
            except Exception:
                pass
            if not_started:
                raise ValueError(not_started) from None
            raise ValueError(f"The storage edit stopped. Check job {ident}. Settings or new volumes may have been saved. Nothing was rolled back or cleaned up automatically.") from None


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


def copy_finished(ref, read, job=None):
    """(complete, failed) of the Job whose uid was recorded, its pods ended. job
    is the Job when the caller has just read it."""
    ns = ref["namespace"]
    if job is None:
        job = read(f"/apis/batch/v1/namespaces/{ns}/jobs/{ref['job']}")
    if IDENT.identity(job)["uid"] != ref["job_uid"]:
        raise IDENT.Stopped("The copy Job was replaced, so its result is not trusted")
    if digest(job.get("spec", {}).get("template")) != ref.get("job_template_digest"):
        raise IDENT.Stopped("The copy Job settings changed, so check its result")
    conditions = job.get("status", {}).get("conditions", [])
    failed = any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions)
    complete = any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions)
    pods = ROLLOUT.items(read, "/api/v1/pods")
    live = [p for p in pods if p.get("metadata", {}).get("namespace") == ns and
            ROLLOUT.controller(p).get("kind") == "Job" and ROLLOUT.controller(p).get("uid") == ref["job_uid"] and
            p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    return complete and not live, failed and not live


def _read_job(ref, read):
    """The copy Job by its recorded name, or None when there is none (404)."""
    try:
        return read(f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs/{ref['job']}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _settling(ref):
    """A create with no answer recorded may still be done at the API server."""
    return (ref.get("phase") == "copy_intent" and not ref.get("job_uid") and
            time.time() - float(ref.get("intent_at") or 0) < CREATE_SETTLE)


def _checking(ns, name):
    return f"Homestead is checking whether copy Job {ns}/{name} was created. It does not send the request again."


def _copy_pods(ref, read, uids):
    """Pods of this app's copy that have not ended: owned by a Job of the copy,
    or carrying the copy's labels. A pod whose Job was deleted with
    --cascade=orphan has no owner left, only its labels."""
    want = NAMES.labels("restructure", ref["name"])
    found = []
    for pod in ROLLOUT.items(read, "/api/v1/pods"):
        meta = pod.get("metadata", {})
        if meta.get("namespace") != ref["namespace"] or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        owner, labels = ROLLOUT.controller(pod), meta.get("labels") or {}
        if (owner.get("kind") == "Job" and owner.get("uid") in uids) or all(labels.get(k) == v for k, v in want.items()):
            found.append(meta.get("name", ""))
    return sorted(found)


def copy_stopped(ref, read, job):
    """"" when nothing of the copy can still write to its volumes, otherwise
    what still runs. job is the Job read by name, None when it is gone. This
    never says the copy finished, only that it stopped."""
    uids = {uid for uid in (ref.get("job_uid"), ref.get("unowned_uid")) if uid}
    if job is not None:
        conditions = job.get("status", {}).get("conditions", [])
        if not any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in conditions):
            return f"Copy Job {ref['namespace']}/{ref['job']} is still active"
        uids.add(IDENT.identity(job)["uid"])
    live = _copy_pods(ref, read, uids)
    return f"Copy pod {', '.join(live)} is still running" if live else ""


def _without_record(item, job, read, send, ops, admission):
    """A copy Job with no usable record: gone after it was created, or created
    (or not) with the answer lost. Its result is never used. Once nothing of the
    copy runs, the edit is undone as for a failed copy."""
    ref = item["ref"]
    name = f"{ref['namespace']}/{ref['job']}"
    if ref["phase"] == "copy_intent" and job is not None:
        # Never taken over by name: waited for, and its result not used.
        ref.update(phase="copy_unowned", unowned_uid=IDENT.identity(job)["uid"])
        ops.checkpoint(item)
    if ref["phase"] == "copying":
        why = f"Copy Job {name} is gone, so it is not known whether the copy finished"
    elif ref["phase"] == "copy_unowned":
        why = f"Homestead has no record of creating copy Job {name}, so its result is not used"
    else:
        why = f"Homestead could not confirm that copy Job {name} was created, and no Job of that name exists"
    still = copy_stopped(ref, read, job)
    if still:
        return "running", item.get("progress") or 20, f"{why}. {still}. Homestead waits for it to end."
    ref["job_gone"] = job is None
    return restore(item, read, send, ops, admission, why)


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
            raise IDENT.Stopped("Another running pod uses a copy volume. Stop what uses it before moving this data.")


def resolve(item, read, send, ops, admission):
    ref, percent = item["ref"], item.get("progress", 0)
    phase = ref.get("phase")
    if phase == "restore_intent":
        return resumed_restore(item, read, send)
    if phase in ("preparing", "release_intent", "recovery_intent"):
        return "running", percent, "A request may be unfinished. Choose Check outcome. Homestead does not send it again."
    try:
        if phase in ("copy_intent", "copy_unowned", "copying"):
            if _settling(ref):
                return "running", 20, _checking(ref["namespace"], ref["job"])
            job = _read_job(ref, read)  # once per poll
            # Before exclusive(), which fails for good on a copy pod left without its Job.
            if phase != "copying" or job is None:
                return _without_record(item, job, read, send, ops, admission)
        if phase == "starting":
            dep = read(IDENT.base(ref["namespace"]) + "/" + ref["name"])
            if IDENT.identity(dep)["uid"] != ref["deployment"]["uid"] or digest(dep["spec"]) != ref["running_digest"]:
                raise IDENT.Stopped("Workload changed while waiting for readiness")
            state, count = dep.get("status", {}), ref["replicas"]
            ready = state.get("observedGeneration", -1) >= dep["metadata"].get("generation", 0) and all(
                state.get(key, 0) == count for key in ("replicas", "readyReplicas", "availableReplicas", "updatedReplicas"))
            if ready:
                ref.update(phase="done", retain_resources=False)
                note = drop_earlier(ref, send)
                return "succeeded", 100, ("Copy finished. The app is ready." if count else "Copy finished. The app stays stopped, as requested.") + note
            return "running", 90, "Copy finished. Waiting for the app to be ready (this is not a check of the app's health)."
        dep = held(item, read)
        if not quiet(dep, read):
            return "running", percent or 10, "Waiting for all workload pods to terminate, including terminating pods"
        exclusive(ref, read)
        if phase == "stopping":
            job_name, body = COPY.job(ref["namespace"], ref["name"], ref["moves"])
            gib = copy_gib(ref["namespace"], ref["moves"], read)
            body["spec"]["activeDeadlineSeconds"] = deadline(gib)
            check = admission(helper_manifest(ref["namespace"], ref["name"], ref["moves"]))
            if check.get("blocked") is not False:
                raise IDENT.Stopped("The copy helper cannot fit current placement or storage constraints")
            if not quiet(held(item, read), read):
                return "running", 10, "Waiting for workload pods to terminate after the placement check"
            exclusive(ref, read)
            ref.update(phase="copy_intent", job=job_name, copy_gib=gib, intent_at=time.time())
            ops._finish(item, "running", 20, f"Starting copy Job {ref['namespace']}/{job_name}. An uncertain request is not repeated.")
            ops.checkpoint(item)  # before POST; never adopt a same-name Job on retry
            try:
                receipt = send("POST", f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs", body)
            except Exception as error:
                if not (isinstance(error, urllib.error.HTTPError) and error.code in NOT_CREATED):
                    # No answer, or one that does not say no Job was created: read by name later.
                    return "running", 20, _checking(ref["namespace"], job_name)
                ref["job_gone"] = True
                return restore(item, read, send, ops, admission,
                               f"Kubernetes refused to create the copy Job (HTTP {error.code}), so nothing was copied")
            meta = IDENT.identity(receipt)
            if (meta["namespace"], meta["name"], receipt.get("kind"), receipt.get("apiVersion")) != (ref["namespace"], job_name, "Job", "batch/v1"):
                raise IDENT.Stopped("Copy Job creation receipt did not match")
            ref.update(phase="copying", job_uid=meta["uid"], job_template_digest=digest(receipt["spec"]["template"]))
            ops.checkpoint(item)
            return "running", 30, "Copying data. The original volumes are kept."
        if phase != "copying":
            raise IDENT.Stopped("The copy step is unknown. Check the kept resources.")
        complete, failed = copy_finished(ref, read, job)
        if failed:
            return restore(item, read, send, ops, admission, failure(job, ref.get("copy_gib")))
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
        patch += _removals(dep["metadata"].get("annotations", {}))
        ref.update(phase="release_intent", running_digest=digest(proposed["spec"]))
        ops._finish(item, "running", 75, "Copy finished and placement checked again. Recording the restart request.")
        ops.checkpoint(item)
        result = _as(ref.get("started_by"), lambda: send("PATCH", IDENT.base(ref["namespace"]) + "/" + ref["name"], patch,
                                                         ctype="application/json-patch+json"))
        if IDENT.identity(result)["uid"] != meta["uid"] or digest(result["spec"]) != ref["running_digest"] or result["metadata"].get("annotations", {}).get(MARKER):
            raise IDENT.Stopped("Restart response did not confirm the reviewed configuration")
        ref["phase"] = "starting"
        ops.checkpoint(item)
        return "running", 85, "Copy finished and restart accepted. Checking readiness."
    except KeptStopped as error:
        ref["retain_resources"] = True
        return "failed", percent, str(error)
    except Exception as error:
        reason = str(error) if isinstance(error, IDENT.Stopped) else "Copy step could not be verified"
        # Preserve intent phase when the response is uncertain; never infer
        # success from a same-name replacement or repeat the upstream request.
        ref["retain_resources"] = True
        return "failed", percent, reason + ". Choose Check outcome before starting anything. The volumes were kept, and nothing was rolled back."


KEPT = "The new volumes and the copy Job were kept for inspection."
GONE_KEPT = "The new volumes were kept. They may hold only part of the data."


def kept(ref):
    """What was kept, without naming a copy Job that is not there."""
    return GONE_KEPT if ref.get("job_gone") else KEPT


def undone(ref, dep):
    """Whether dep is back as it was before the edit, with no copy hold left."""
    return (IDENT.identity(dep)["uid"] == ref["deployment"]["uid"] and
            digest(dep["spec"].get("template")) == ref.get("earlier_template_digest") and
            dep["spec"].get("replicas") == ref.get("earlier_replicas") and
            not any(key in dep["metadata"].get("annotations", {}) for key in (MARKER, COPY.HELD)))


def _undone_message(ref):
    count, name = ref["earlier_replicas"], ref["name"]
    if count:
        return (f"{ref['failure']}. The edit was undone: {name} was started again with {count} "
                f"replica{'s' if count != 1 else ''} on its configuration and storage from before the edit, "
                f"which the copy only read. {kept(ref)}")
    return (f"{ref['failure']}. The edit was undone: {name} has its configuration and storage from before the "
            f"edit back and stays stopped, as it was before. {kept(ref)}")


def _unsure_message(ref):
    return (f"{ref.get('failure') or 'The copy failed'}. Undoing the edit of {ref['name']} may not have been "
            f"applied: check its pod template, its replica count and the {MARKER} mark before starting it. {kept(ref)}")


def restore(item, read, send, ops, admission, why):
    """A copy that failed, ran out of time, or whose Job is gone or was never
    confirmed (why says which): the edit is undone. The pod template and
    replicas from before it come back, so the app runs as it did, on storage
    the copy only read. Anything that makes that unsafe or unsure keeps the app
    stopped and says why. The new volumes and the Job are kept."""
    ref = item["ref"]

    def stay(reason):
        raise KeptStopped(f"{why}. {ref['name']} stays stopped: {reason}. {kept(ref)}")
    try:
        dep = held(item, read)
        if not quiet(dep, read):
            stay("its pods changed while the copy ran. Check them, then start it")
        exclusive(ref, read)
    except KeptStopped:
        raise
    except IDENT.Stopped as error:
        stay(str(error).rstrip(".").lower() + ". Check it, then start it")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        stay("the app or a volume of the copy is gone, so the edit cannot be undone. Check the app and its volumes")
    try:
        text = read_earlier(ref, read)
    except Exception:
        stay("its settings from before the edit could not be read, so the edit cannot be undone. "
             "Edit it back, then start it")
    if not text:
        stay("its settings from before the edit were not recorded, were removed or were changed, so the edit cannot be "
             "undone. Edit it back, then start it")
    saved = json.loads(text)
    written = {m["to"] for m in ref["moves"]}
    used = {v.get("persistentVolumeClaim", {}).get("claimName") for v in saved["template"].get("spec", {}).get("volumes", [])}
    if written & used:
        stay(f"the copy also wrote to {', '.join(sorted(written & used))}, which it used before the edit. "
             "Check that data, then start it")
    count = int(saved["replicas"] or 0)
    proposed = unheld(dep)
    proposed["spec"].update(template=saved["template"], replicas=count)
    if count and admission(proposed).get("blocked") is not False:
        stay("it no longer fits the current placement or storage limits as it was. Free some room, then start it")
    meta = IDENT.identity(dep)
    patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
             {"op": "add", "path": "/spec/template", "value": saved["template"]},
             {"op": "add", "path": "/spec/replicas", "value": count}] + _removals(dep["metadata"].get("annotations", {}))
    ref.update(phase="restore_intent", retain_resources=True, failure=why, earlier_replicas=count,
               earlier_template_digest=digest(saved["template"]))
    ops._finish(item, "running", item.get("progress", 45), f"{why}. Undoing the edit of {ref['name']}.")
    ops.checkpoint(item)  # before PATCH; an uncertain undo is never replayed
    path = IDENT.base(ref["namespace"]) + "/" + ref["name"]
    try:
        result = _as(ref.get("started_by"), lambda: send("PATCH", path, patch, ctype="application/json-patch+json"))
    except Exception:
        result = read(path)  # a lost reply: what the cluster has now decides
    if not undone(ref, result):
        raise KeptStopped(_unsure_message(ref))
    ref["phase"] = "restored"
    note = drop_earlier(ref, send)
    ops.checkpoint(item)
    return "failed", item.get("progress", 45), _undone_message(ref) + note


def resumed_restore(item, read, send):
    """Homestead stopped after recording the undo: look, never send it again."""
    ref = item["ref"]
    ref["retain_resources"] = True
    if undone(ref, read(IDENT.base(ref["namespace"]) + "/" + ref["name"])):
        ref["phase"] = "restored"
        return "failed", item.get("progress", 45), _undone_message(ref) + drop_earlier(ref, send)
    return "failed", item.get("progress", 45), _unsure_message(ref)


def legacy_status(item):
    item["ref"]["retain_resources"] = True
    return "failed", item.get("progress", 0), "This older storage move has no records of what it created. Check its app and copy Job. Nothing was restarted, retried or rolled back automatically."


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
        lock = LOCKS.worker_lock(item["id"], ops, timeout=.01)
        lock.__enter__()
    except TimeoutError:    # its step holds it: a cancel run again waits for as long as that takes
        return {**plan, "try_again": "busy"}
    except OSError as error:    # perhaps passing: a cancel run again waits, for a while
        return {**plan, "try_again": "error",
                "why_not": f"{plan['why_not']}: its lock could not be opened ({error.strerror or error})"}
    except Exception:
        return plan
    try:
        dep = read(IDENT.base(ref["namespace"]) + "/" + ref["name"])
        if IDENT.identity(dep)["uid"] != ref["deployment"]["uid"]:
            return {**plan, "why_not": "The app was replaced, so its hold is not changed"}
        if ref.get("job"):
            # Released once nothing of the copy runs, whatever is known of its Job:
            # the release starts nothing and changes no storage.
            if _settling(ref):
                return {**plan, "why_not": "Homestead is still checking whether the copy Job was created. Try again in 2 minutes."}
            still = copy_stopped(ref, read, _read_job(ref, read))
            if still:
                return {**plan, "why_not": f"{still}. Wait for it to end before releasing the hold."}
        if dep["metadata"].get("annotations", {}).get(MARKER) not in (None, item["id"]):
            return {**plan, "why_not": "Another operation owns this workload's hold"}
        plan.update(can=True, why_not="")
    except Exception as error:
        # A failed read says nothing about the hold: the caller waits, or shows the error.
        if CANCEL_RERUN.is_blip(error):
            raise
    finally:
        lock.__exit__(None, None, None)
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
            raise ValueError("The app was replaced, so no hold was changed")
        if ref.get("job") and (_settling(ref) or copy_stopped(ref, read, _read_job(ref, read))):
            raise ValueError("The copy has not ended, so no hold was changed")
        annotations = dep["metadata"].get("annotations", {})
        if annotations.get(MARKER) not in (None, item["id"]):
            raise ValueError("Another operation owns the hold")
        if annotations.get(MARKER) == item["id"]:
            patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                     {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
            patch += _removals(annotations)
            # The generic canceller has durably recorded cancelling before
            # this metadata-only CAS. A retry cannot start or delete anything.
            result = send("PATCH", IDENT.base(ref["namespace"]) + "/" + ref["name"], patch, ctype="application/json-patch+json")
            if IDENT.identity(result)["uid"] != meta["uid"] or result["metadata"].get("annotations", {}).get(MARKER):
                raise ValueError("The release of the hold was not confirmed. Check it before trying again.")
        ref["retain_resources"] = False
        note = drop_earlier(ref, send)
        return "Safety hold released. The app's replicas, storage settings, volumes and copy Job were not changed. Review the data before starting." + note
