"""Replacement reclass stages under construction; not yet a registered resolver.

Reuse reclass manifests and the shared operation store. Do not enable this engine
until cutover, restart admission, cancellation and reviewed recovery are wired.
"""
import copy
import re

import homestead_reclass as RC
import homestead_storage_journal as JOURNAL
import homestead_rollout_capacity as ROLLOUT


def _receipt(item, step):
    return next((e for e in item["ref"].get("storage_writes", []) if e["step"] == step), None)


def _path(ns, kind, name):
    groups = {"Deployment": "apps/v1", "StatefulSet": "apps/v1", "CronJob": "batch/v1", "VirtualMachine": "kubevirt.io/v1"}
    plurals = {"Deployment": "deployments", "StatefulSet": "statefulsets", "CronJob": "cronjobs", "VirtualMachine": "virtualmachines"}
    return f"/apis/{groups[kind]}/namespaces/{ns}/{plurals[kind]}/{name}"


def stopped(item, writer):
    """Verify every confirmed stop, including the workload's copy hold."""
    ref = item["ref"]
    known = set()
    for consumer in ref["consumers"]:
        path = _path(ref["namespace"], consumer["kind"], consumer["name"])
        entry = _receipt(item, "stop:" + ("PUT" if consumer["kind"] == "VirtualMachine" else "PATCH") + ":" + path)
        if not entry:
            raise JOURNAL.Held("A workload stop has no confirmed receipt")
        obj = writer.observe(entry)
        spec = obj["spec"]
        if consumer["kind"] in ("Deployment", "StatefulSet"):
            if spec.get("replicas") != 0:
                raise JOURNAL.Held("A workload restarted during the storage move")
        elif consumer["kind"] == "CronJob":
            if spec.get("suspend") is not True:
                raise JOURNAL.Held("A scheduled workload was resumed during the storage move")
        elif spec.get("runStrategy") != "Halted":
            raise JOURNAL.Held("A VM restarted during the storage move")
        if consumer["kind"] != "CronJob" and obj["metadata"].get("annotations", {}).get("homestead.io/storage-copy-job") != item["id"]:
            raise JOURNAL.Held("A workload's storage-copy hold was changed")
        known.add((consumer["kind"], consumer["name"]))
    observed = {(c["kind"], c["name"]) for c in RC.consumers(ref["namespace"], ref["claim"])}
    if observed != known:
        raise JOURNAL.Held("The set of workloads using the source changed during the move")


def quiescent(item, writer):
    """No pod right now is not proof that a controller finished stopping.

    Wait for observed scale-down, including child ReplicaSets, active CronJob
    executions and VM instances. Never delete those controllers to force a stop.
    Pod/claim checks remain separate and are repeated immediately before writes.
    """
    stopped(item, writer)
    ns = item["ref"]["namespace"]
    def zero_observed(obj):
        meta, status = obj.get("metadata", {}), obj.get("status", {})
        generation, observed = meta.get("generation"), status.get("observedGeneration")
        return (type(generation) is int and generation > 0 and type(observed) is int and observed >= generation and
                all(status.get(key, 0) == 0 for key in ("replicas", "readyReplicas", "availableReplicas")))
    for consumer in item["ref"]["consumers"]:
        kind, name = consumer["kind"], consumer["name"]
        path = _path(ns, kind, name)
        obj = writer.observe(_receipt(item, "stop:" + ("PUT" if kind == "VirtualMachine" else "PATCH") + ":" + path))
        uid = JOURNAL.identity(obj)["uid"]
        if kind in ("Deployment", "StatefulSet"):
            if not zero_observed(obj):
                return False
            if kind == "Deployment":
                sets = RC._items(f"/apis/apps/v1/namespaces/{ns}/replicasets")
                for child in sets:
                    owner = ROLLOUT.controller(child)
                    if owner.get("kind") == "Deployment" and owner.get("uid") == uid:
                        if child.get("spec", {}).get("replicas", 1) != 0 or not zero_observed(child):
                            return False
        elif kind == "CronJob":
            for job in RC._items(f"/apis/batch/v1/namespaces/{ns}/jobs"):
                owner = ROLLOUT.controller(job)
                if owner.get("kind") == "CronJob" and owner.get("uid") == uid and not any(
                        c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
                        for c in job.get("status", {}).get("conditions", [])):
                    return False
        elif RC._get(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}") is not None:
            return False
    return True


def claim(item, name, writer, *, source=False):
    """Pin dynamic binding once, but never accept a replaced/rebound PVC or PV."""
    ref = item["ref"]
    path = f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{name}"
    obj = RC.kget(path)
    dest = JOURNAL.target("PUT", path, obj, ref["namespace"])
    writer._check_target(obj, dest)
    meta = JOURNAL.identity(obj)
    if source:
        expected = ref["review_fences"].get(path, {})
    else:
        entry = _receipt(item, "copy-volume")
        if not entry or entry.get("state") != "accepted":
            raise JOURNAL.Held("The new volume has no verified creation receipt")
        expected = entry["after"]
    if meta["uid"] != expected.get("uid") or obj["metadata"].get("deletionTimestamp"):
        raise JOURNAL.Held("A copy volume was replaced or is deleting")
    spec = obj.get("spec", {})
    expected_class = ref["from_class"] if source else ref["target"]
    if (spec.get("storageClassName") != expected_class or spec.get("volumeMode", "Filesystem") != ref["mode"] or
            spec.get("accessModes") != ref["claim_spec"]["accessModes"] or
            RC._bytes(spec.get("resources", {}).get("requests", {}).get("storage")) != RC._bytes(ref["size"])):
        raise JOURNAL.Held("A copy volume's requested storage configuration changed")
    volume = spec.get("volumeName")
    pinned = ref.setdefault("copy_claims", {}).get(name)
    if pinned and pinned.get("pv") and volume != pinned["pv"]:
        raise JOURNAL.Held("A copy volume was rebound to different storage")
    if source and volume != ref["old_pv"]:
        raise JOURNAL.Held("The source no longer uses its reviewed backing volume")
    if volume:
        pv_path = f"/api/v1/persistentvolumes/{volume}"
        pv = RC.kget(pv_path)
        writer._check_target(pv, JOURNAL.target("PUT", pv_path, pv, ref["namespace"]))
        pv_meta = JOURNAL.identity(pv)
        pv_expected = ref["review_fences"].get(pv_path, {}) if source else (pinned or {})
        if source and not pv_expected.get("uid"):
            raise JOURNAL.Held("The source backing volume was not identity-checked in the review")
        owner = pv.get("spec", {}).get("claimRef", {})
        if (pv["metadata"].get("deletionTimestamp") or
                (pv_expected.get("uid" if source else "pv_uid") and pv_meta["uid"] != pv_expected["uid" if source else "pv_uid"]) or
                (owner.get("uid"), owner.get("name"), owner.get("namespace")) != (meta["uid"], name, ref["namespace"])):
            raise JOURNAL.Held("A backing volume identity or ownership changed")
        csi = pv.get("spec", {}).get("csi", {})
        ref["copy_claims"][name] = {"uid": meta["uid"], "pv": volume, "pv_uid": pv_meta["uid"],
                                   "csi_driver": csi.get("driver"), "csi_handle": csi.get("volumeHandle")}
    elif source:
        raise JOURNAL.Held("The source backing volume is unavailable")
    else:
        ref["copy_claims"][name] = {"uid": meta["uid"], "pv": None, "pv_uid": None}
    return obj


def pods(item):
    ref = item["ref"]
    names = {ref["claim"], ref["temp"]}
    return [p for p in RC._items(f"/api/v1/namespaces/{ref['namespace']}/pods")
            if names & RC._claims_in(p.get("spec"))]


def _owned(pod, uid):
    return any(o.get("kind") == "Job" and o.get("uid") == uid and o.get("controller") is True
               for o in pod.get("metadata", {}).get("ownerReferences", []))


def _copy_progress(item, job_uid, copy_pods):
    """Best-effort progress from an exact Job-owned pod, never a label match."""
    for pod in copy_pods:
        if pod.get("status", {}).get("phase") not in ("Running", "Succeeded"):
            continue
        identity = JOURNAL.identity(pod)
        path = f"/api/v1/namespaces/{item['ref']['namespace']}/pods/{pod['metadata']['name']}"
        try:
            output = str(RC.ktext(path + "/log?container=copy&tailLines=40") or "")
            current = RC.kget(path)
        except Exception:
            continue  # inaccessible logs are not a fabricated percentage
        if JOURNAL.identity(current)["uid"] != identity["uid"] or not _owned(current, job_uid):
            raise JOURNAL.Held("The copy progress pod was replaced; inspect its Job before continuing")
        progress = RC.copy_progress(output, RC._bytes(item["ref"]["size"]))
        item["copy"] = progress
        if progress["verifying"]:
            return 70, "Checking the copied data against the original"
        if progress["percent"]:
            return 15 + int(progress["percent"] * .5), f"Copying {progress['percent']}%" + (f" at {progress['speed']}" if progress["speed"] else "")
    item["copy"] = {"percent": None, "speed": "", "verifying": False, "verified": False, "unavailable": True}
    return 35, ("Copy running; waiting for reported progress and checksum verification" if any(
        p.get("status", {}).get("phase") == "Running" for p in copy_pods) else
        "Waiting for the copy pod and volume attachments; progress is unavailable")


def copy_stage(item, checkpoint, admission, *, journal_factory=JOURNAL.Journal):
    """Make and observe the copy; never stop, roll back or restart on failure.

    Called only after journaled_stop. The caller owns the operations lock and
    turns Held into an inspectable recovery hold, not an automatic retry.
    """
    ref = item["ref"]
    writer = journal_factory(item, RC.kget, RC.ksend, checkpoint)
    writer.check()
    if not quiescent(item, writer):
        return "running", 8, "Waiting for workload controllers to finish stopping before copying"
    claim(item, ref["claim"], writer, source=True)
    known_job = _receipt(item, "copy-job")
    job_uid = known_job.get("after", {}).get("uid") if known_job else None
    using = pods(item)
    foreign = [p for p in using if p.get("status", {}).get("phase") not in RC.FINISHED and not (job_uid and _owned(p, job_uid))]
    if foreign:
        if known_job:
            raise JOURNAL.Held("Another pod is using the copy's data; inspect the workloads and copy before continuing")
        return "running", 8, "Waiting for all source pods to terminate before copying"
    created = _receipt(item, "copy-volume")
    if not created:
        body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": ref["temp"], "namespace": ref["namespace"], "labels": RC.NAMES.labels("reclass", ref["claim"])},
                "spec": {**ref["claim_spec"], "storageClassName": ref["target"],
                         "resources": {"requests": {"storage": ref["size"]}}}}
        writer.write("copy-volume", "POST", f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims", body)
        return "running", 12, "New copy volume created; original retained"
    claim(item, ref["temp"], writer)
    if not known_job:
        name, body = RC.job_body(ref["namespace"], ref)
        body["metadata"].setdefault("annotations", {})["homestead.io/storage-copy-job"] = item["id"]
        # Keep evidence until explicit cleanup. TTL deletion would remove the
        # only verified copy Job before an interrupted handoff is inspected.
        body["spec"].pop("ttlSecondsAfterFinished", None)
        helper = {"metadata": {"namespace": ref["namespace"], "name": name},
                  "spec": {"replicas": 1, "template": body["spec"]["template"]}}
        if admission(helper).get("blocked") is not False:
            raise JOURNAL.Held("The copy helper cannot fit current placement or storage constraints")
        # Admission can take time. Recheck the stopped workloads and both claims
        # afterwards, immediately before the durable create intent.
        if not quiescent(item, writer):
            return "running", 8, "Waiting for workload controllers to finish stopping before copying"
        claim(item, ref["claim"], writer, source=True)
        claim(item, ref["temp"], writer)
        if any(p.get("status", {}).get("phase") not in RC.FINISHED for p in pods(item)):
            raise JOURNAL.Held("A workload began using the copy data during admission")
        writer.write("copy-job", "POST", f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs", body)
        return "running", 20, "Copy Job created; waiting for copy and checksum verification"
    job = writer.observe(known_job)
    state = job.get("status", {})
    failed = any(c.get("type") == "Failed" and c.get("status") == "True" for c in state.get("conditions", []))
    complete = any(c.get("type") == "Complete" and c.get("status") == "True" for c in state.get("conditions", []))
    if failed:
        raise JOURNAL.Held("The copy Job failed; both volumes and workload holds are retained")
    copy_pods = [p for p in using if _owned(p, job_uid)]
    if not complete or any(p.get("status", {}).get("phase") not in RC.FINISHED for p in copy_pods):
        progress, message = _copy_progress(item, job_uid, copy_pods)
        return "running", progress, message
    if not copy_pods or not all(ref["copy_claims"][n].get("pv") for n in (ref["claim"], ref["temp"])):
        raise JOURNAL.Held("Copy verification evidence or bound volume identity is unavailable")
    verified = False
    for pod in copy_pods:
        if pod.get("status", {}).get("phase") != "Succeeded":
            continue
        path = f"/api/v1/namespaces/{ref['namespace']}/pods/{pod['metadata']['name']}"
        before = JOURNAL.identity(pod)
        log = str(RC.ktext(path + "/log?container=copy&tailLines=40") or "")
        after = RC.kget(path)
        if JOURNAL.identity(after)["uid"] != before["uid"] or not _owned(after, job_uid):
            raise JOURNAL.Held("The copy log's pod identity changed during verification")
        if "==> verified" in log.splitlines():
            verified = True
    if not verified:
        raise JOURNAL.Held("The copy Job completed without a verified checksum result")
    ref["copy_verified"] = {"job_uid": job_uid, "claims": copy.deepcopy(ref["copy_claims"])}
    checkpoint(item)
    return "running", 75, "Copy verified; both volumes retained for guarded cutover"


def restart_proposals(item, writer, *, remaining=False):
    """Build request-local proposed objects; never put workload secrets in history."""
    out = []
    for consumer in item["ref"]["consumers"]:
        path = _path(item["ref"]["namespace"], consumer["kind"], consumer["name"])
        if remaining and _receipt(item, "restart:" + path):
            _restored(consumer, writer.observe(_receipt(item, "restart:" + path)))
            continue
        method = "PUT" if consumer["kind"] == "VirtualMachine" else "PATCH"
        entry = _receipt(item, "stop:" + method + ":" + path)
        if not entry:
            raise JOURNAL.Held("A restart has no verified stop receipt")
        obj = copy.deepcopy(writer.observe(entry))
        kind = consumer["kind"]
        if kind in ("Deployment", "StatefulSet"):
            obj["spec"]["replicas"] = consumer["replicas"]
        elif kind == "CronJob":
            obj["spec"]["suspend"] = consumer.get("suspend", False)
        else:
            obj["spec"]["runStrategy"] = consumer["run_strategy"]
        obj["metadata"].get("annotations", {}).pop("homestead.io/storage-copy-job", None)
        obj.pop("status", None)
        obj["metadata"].pop("managedFields", None)
        out.append({"kind": kind, "path": path, "object": obj})
    return out


def _restored(consumer, obj):
    spec, kind = obj["spec"], consumer["kind"]
    if (obj["metadata"].get("annotations", {}).get("homestead.io/storage-copy-job") or
            (kind in ("Deployment", "StatefulSet") and spec.get("replicas") != consumer["replicas"]) or
            (kind == "CronJob" and spec.get("suspend", False) != consumer.get("suspend", False)) or
            (kind == "VirtualMachine" and spec.get("runStrategy") != consumer["run_strategy"])):
        raise JOURNAL.Held("A restored workload does not match its reviewed restart settings")


def _pv_shape(obj):
    """Binding/reclaim state is checked separately; backing storage must not change."""
    spec = copy.deepcopy(obj.get("spec", {}))
    spec.pop("claimRef", None)
    spec.pop("persistentVolumeReclaimPolicy", None)
    return JOURNAL.digest(spec)


def _checked_pv(writer, name, uid, shape):
    path = f"/api/v1/persistentvolumes/{name}"
    obj = RC.kget(path)
    writer._check_target(obj, JOURNAL.target("PUT", path, obj, writer.ref["namespace"]))
    if (JOURNAL.identity(obj)["uid"] != uid or obj["metadata"].get("deletionTimestamp") or _pv_shape(obj) != shape):
        raise JOURNAL.Held("A backing volume changed during cutover; neither copy will be restarted")
    return obj


def _claim_for_cutover(item, writer, name, original_uid, delete_step, create_step=None):
    """Absence is allowed only after our exact delete has an acceptance receipt."""
    path = f"/api/v1/namespaces/{item['ref']['namespace']}/persistentvolumeclaims/{name}"
    obj = writer._get(path)
    deleted, created = _receipt(item, delete_step), _receipt(item, create_step) if create_step else None
    if obj is None:
        if not deleted or created:
            raise JOURNAL.Held("A cutover claim disappeared without the expected receipt")
        return None
    writer._check_target(obj, JOURNAL.target("PUT", path, obj, item["ref"]["namespace"]))
    expected_uid = created["after"]["uid"] if created else original_uid
    if JOURNAL.identity(obj)["uid"] != expected_uid:
        raise JOURNAL.Held("A cutover claim name now belongs to another object")
    if obj["metadata"].get("deletionTimestamp") and (not deleted or created):
        raise JOURNAL.Held("A claim was deleted outside this cutover")
    return obj


def cutover_stage(item, checkpoint, admission, *, journal_factory=JOURNAL.Journal):
    """One fenced cutover write per poll. Retain both PVs before removing claims.

    A replacement claim is explicitly prebound; the copied PV is never made
    generally available to unrelated claims. An uncertain write stops the entire
    journal. No finalizer removal, rollback, cleanup of data, or restart occurs.
    admission reviews all proposed restarts before the destructive phase begins;
    restart admission must run again after binding and immediately before launch.
    """
    ref = item["ref"]
    writer = journal_factory(item, RC.kget, RC.ksend, checkpoint)
    writer.check()
    if not quiescent(item, writer):
        return "running", 75, "Waiting for workload controllers to finish stopping before cutover"
    proof = ref.get("copy_verified")
    if not proof or proof.get("claims") != ref.get("copy_claims"):
        raise JOURNAL.Held("Cutover needs verified copy and volume-identity evidence")
    state = ref.get("cutover")
    if not state:
        result = copy_stage(item, checkpoint, lambda _: {"blocked": True})
        if result[1] != 75:
            return result
        if admission(restart_proposals(item, writer)).get("blocked") is not False:
            raise JOURNAL.Held("Post-copy workload placement needs review before cutover")
        # Recheck after slow admission: no writes may rely on the earlier read.
        if not quiescent(item, writer):
            return "running", 75, "Waiting for workload controllers to finish stopping before cutover"
        source = claim(item, ref["claim"], writer, source=True)
        copied = claim(item, ref["temp"], writer)
        if any(p.get("status", {}).get("phase") not in RC.FINISHED for p in pods(item)):
            raise JOURNAL.Held("A pod began using the data before cutover")
        sc_path = f"/apis/storage.k8s.io/v1/storageclasses/{ref['target']}"
        sc = RC.kget(sc_path)
        if JOURNAL.identity(sc) != ref["review_fences"].get(sc_path) or sc["metadata"].get("deletionTimestamp"):
            raise JOURNAL.Held("The destination storage class changed after review")
        old_pv = RC.kget(f"/api/v1/persistentvolumes/{ref['old_pv']}")
        new_pv = RC.kget(f"/api/v1/persistentvolumes/{copied['spec']['volumeName']}")
        state = {"source_uid": JOURNAL.identity(source)["uid"], "temp_uid": JOURNAL.identity(copied)["uid"],
                 "old_pv": ref["old_pv"], "new_pv": copied["spec"]["volumeName"],
                 "old_uid": JOURNAL.identity(old_pv)["uid"], "new_uid": JOURNAL.identity(new_pv)["uid"],
                 "old_shape": _pv_shape(old_pv), "new_shape": _pv_shape(new_pv),
                 "source_shape": JOURNAL.shape(source), "temp_shape": JOURNAL.shape(copied),
                 "reclaim": sc.get("reclaimPolicy", "Delete")}
        if (state["old_uid"], state["new_uid"]) != (proof["claims"][ref["claim"]]["pv_uid"], proof["claims"][ref["temp"]]["pv_uid"]):
            raise JOURNAL.Held("Backing volume identities changed while preparing cutover")
        if state["old_uid"] == state["new_uid"] or state["reclaim"] not in ("Delete", "Retain"):
            raise JOURNAL.Held("The destination is not a distinct volume with a supported reclaim policy")
        ref["cutover"] = state
        checkpoint(item)
    old = _checked_pv(writer, state["old_pv"], state["old_uid"], state["old_shape"])
    new = _checked_pv(writer, state["new_pv"], state["new_uid"], state["new_shape"])
    source = _claim_for_cutover(item, writer, ref["claim"], state["source_uid"], "delete-source", "replacement")
    temporary = _claim_for_cutover(item, writer, ref["temp"], state["temp_uid"], "delete-temporary")
    if ((source is not None and not _receipt(item, "replacement") and JOURNAL.shape(source) != state["source_shape"]) or
            (temporary is not None and JOURNAL.shape(temporary) != state["temp_shape"])):
        raise JOURNAL.Held("A claim's configuration changed during cutover")
    owner = old.get("spec", {}).get("claimRef", {})
    if (owner.get("uid"), owner.get("name"), owner.get("namespace")) != (state["source_uid"], ref["claim"], ref["namespace"]):
        raise JOURNAL.Held("The original backing volume was claimed by something else")
    owner = new.get("spec", {}).get("claimRef", {})
    replacement = _receipt(item, "replacement")
    if not _receipt(item, "reserve-new"):
        valid_owner = (owner.get("uid"), owner.get("name"), owner.get("namespace")) == (state["temp_uid"], ref["temp"], ref["namespace"])
    else:
        valid_owner = ((owner.get("name"), owner.get("namespace")) == (ref["claim"], ref["namespace"]) and
                       owner.get("uid") in ((None, "") if not replacement else (None, "", replacement["after"]["uid"])))
    if not valid_owner:
        raise JOURNAL.Held("The copied backing volume has unexpected claim ownership")
    for step, obj in (("retain-original", old), ("retain-copy", new)):
        if _receipt(item, step) and obj["spec"].get("persistentVolumeReclaimPolicy") != "Retain" and not (step == "retain-copy" and _receipt(item, "restore-policy")):
            raise JOURNAL.Held("A backing volume's Retain protection was changed")
    using = pods(item)
    if any(p.get("status", {}).get("phase") not in RC.FINISHED for p in using):
        raise JOURNAL.Held("A pod is using cutover data; workloads remain held for inspection")
    def patch(step, obj, body):
        path = f"/api/v1/persistentvolumes/{obj['metadata']['name']}"
        writer.write(step, "PATCH", path, body, expected=JOURNAL.identity(obj), ctype="application/merge-patch+json")
    if not _receipt(item, "retain-original"):
        patch("retain-original", old, {"spec": {"persistentVolumeReclaimPolicy": "Retain"}})
        return "running", 77, "Original volume protected with Retain"
    if not _receipt(item, "retain-copy"):
        patch("retain-copy", new, {"spec": {"persistentVolumeReclaimPolicy": "Retain"}})
        return "running", 78, "Copied volume protected with Retain"
    if not _receipt(item, "mark-original"):
        patch("mark-original", old, {"metadata": {"annotations": {RC.OLD_COPY: f"{ref['namespace']}/{ref['claim']}"}}})
        return "running", 79, "Original recorded as the retained recovery copy"
    if old["metadata"].get("annotations", {}).get(RC.OLD_COPY) != f"{ref['namespace']}/{ref['claim']}":
        raise JOURNAL.Held("The original recovery-copy marker changed")
    job_entry = _receipt(item, "copy-job")
    if not job_entry or job_entry["after"]["uid"] != proof["job_uid"]:
        raise JOURNAL.Held("Cutover's copy Job receipt does not match its proof")
    if not _receipt(item, "delete-copy-job"):
        job = writer.observe(job_entry)
        writer.write("delete-copy-job", "DELETE", job_entry["target"]["path"], {"propagationPolicy": "Foreground"}, expected=JOURNAL.identity(job))
        return "running", 80, "Removing the verified copy helper; waiting for its pods to disappear"
    if writer.observe(_receipt(item, "delete-copy-job")) is not None or using:
        return "running", 81, "Waiting for the copy helper and all pods naming these claims to be removed"
    if not _receipt(item, "delete-temporary"):
        writer.write("delete-temporary", "DELETE", f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{ref['temp']}", expected=JOURNAL.identity(temporary))
        return "running", 83, "Releasing the temporary claim; its copied data remains protected"
    if temporary is not None:
        return "running", 84, "Waiting for the temporary claim's finalizers; no forced removal"
    if not _receipt(item, "delete-source"):
        writer.write("delete-source", "DELETE", f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{ref['claim']}", expected=JOURNAL.identity(source))
        return "running", 85, "Releasing the original claim; original data remains retained"
    if source is not None and not replacement:
        return "running", 86, "Waiting for the original claim's finalizers; no forced removal"
    if not _receipt(item, "reserve-new"):
        patch("reserve-new", new, {"spec": {"claimRef": {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
              "namespace": ref["namespace"], "name": ref["claim"], "uid": None, "resourceVersion": None}}})
        return "running", 87, "Copied volume reserved for the original claim name"
    if not replacement:
        body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": ref["claim"], "namespace": ref["namespace"], "labels": ref.get("labels") or {}, "annotations": ref.get("annotations") or {}},
                "spec": {**ref["claim_spec"], "storageClassName": ref["target"], "volumeName": state["new_pv"],
                         "resources": {"requests": {"storage": ref["size"]}}}}
        writer.write("replacement", "POST", f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims", body)
        return "running", 88, "Replacement claim created for the verified copied volume"
    spec = source.get("spec", {})
    if (spec.get("volumeName") != state["new_pv"] or spec.get("storageClassName") != ref["target"] or
            spec.get("accessModes") != ref["claim_spec"]["accessModes"] or spec.get("volumeMode", "Filesystem") != ref["mode"] or
            RC._bytes(spec.get("resources", {}).get("requests", {}).get("storage")) != RC._bytes(ref["size"])):
        raise JOURNAL.Held("The replacement claim does not point at the copied volume")
    if source.get("status", {}).get("phase") != "Bound" or owner.get("uid") != replacement["after"]["uid"]:
        return "running", 89, "Waiting for exact replacement claim and backing volume binding"
    if not _receipt(item, "restore-policy"):
        patch("restore-policy", new, {"spec": {"persistentVolumeReclaimPolicy": state["reclaim"]}})
        return "running", 90, "Replacement bound; destination reclaim policy restored"
    if new["spec"].get("persistentVolumeReclaimPolicy") != state["reclaim"]:
        raise JOURNAL.Held("Destination reclaim policy changed after cutover")
    ref["cutover_complete"] = True
    checkpoint(item)
    return "running", 91, "Storage cutover complete; workloads still stopped pending fresh restart admission"


def bound_destination(item, writer):
    """Storage checks repeated before every restart, including after slow admission."""
    ref, state = item["ref"], item["ref"].get("cutover")
    if not ref.get("cutover_complete") or not state or not _receipt(item, "restore-policy"):
        raise JOURNAL.Held("Storage cutover is not complete; workloads must remain held")
    old = _checked_pv(writer, state["old_pv"], state["old_uid"], state["old_shape"])
    new = _checked_pv(writer, state["new_pv"], state["new_uid"], state["new_shape"])
    receipt = _receipt(item, "replacement")
    if not receipt or receipt.get("state") != "accepted":
        raise JOURNAL.Held("Replacement claim creation is not confirmed")
    current = _claim_for_cutover(item, writer, ref["claim"], state["source_uid"], "delete-source", "replacement")
    spec = current.get("spec", {})
    if (current.get("status", {}).get("phase") != "Bound" or spec.get("volumeName") != state["new_pv"] or
            spec.get("storageClassName") != ref["target"] or spec.get("accessModes") != ref["claim_spec"]["accessModes"] or
            spec.get("volumeMode", "Filesystem") != ref["mode"] or
            RC._bytes(spec.get("resources", {}).get("requests", {}).get("storage")) != RC._bytes(ref["size"])):
        raise JOURNAL.Held("Replacement storage binding or configuration is no longer ready")
    for obj, uid, name in ((old, state["source_uid"], ref["claim"]), (new, receipt["after"]["uid"], ref["claim"])):
        owner = obj["spec"].get("claimRef", {})
        if (owner.get("uid"), owner.get("name"), owner.get("namespace")) != (uid, name, ref["namespace"]):
            raise JOURNAL.Held("Backing storage ownership changed before restart")
    if (old["spec"].get("persistentVolumeReclaimPolicy") != "Retain" or
            old["metadata"].get("annotations", {}).get(RC.OLD_COPY) != f"{ref['namespace']}/{ref['claim']}" or
            new["spec"].get("persistentVolumeReclaimPolicy") != state["reclaim"]):
        raise JOURNAL.Held("Storage protection changed before restart")


def _started_owner(pod, item):
    """Follow exact controller UIDs, not app labels, for already restarted pods."""
    ns = item["ref"]["namespace"]
    accepted = {e["after"]["uid"] for e in item["ref"].get("storage_writes", [])
                if e["step"].startswith("restart:") and e.get("state") == "accepted"}
    versions = {"ReplicaSet": ("apps/v1", "replicasets"), "Job": ("batch/v1", "jobs"),
                "VirtualMachineInstance": ("kubevirt.io/v1", "virtualmachineinstances")}
    obj = pod
    for _ in range(4):
        owners = [o for o in obj.get("metadata", {}).get("ownerReferences", []) if o.get("controller") is True]
        if len(owners) != 1 or not owners[0].get("uid"):
            return False
        owner = owners[0]
        if owner["uid"] in accepted:
            return True
        if owner.get("kind") not in versions:
            return False
        version, plural = versions[owner["kind"]]
        name = owner.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(JOURNAL.NAME, name):
            return False
        obj = RC.kget(f"/apis/{version}/namespaces/{ns}/{plural}/{name}")
        if JOURNAL.identity(obj)["uid"] != owner["uid"] or obj["metadata"].get("namespace") != ns:
            return False
    return False


def restart_stage(item, checkpoint, admission, *, journal_factory=JOURNAL.Journal):
    """Restore one controller per poll, with batch admission and no rollback.

    The admission callback must account for all remaining proposed workloads and
    already-running pods. A partial restart is not undone if a later one blocks:
    the first application may already have written to the new copy.
    """
    ref = item["ref"]
    writer = journal_factory(item, RC.kget, RC.ksend, checkpoint)
    writer.check()
    bound_destination(item, writer)
    expected = {(c["kind"], c["name"]) for c in ref["consumers"]}
    if {(c["kind"], c["name"]) for c in RC.consumers(ref["namespace"], ref["claim"])} != expected:
        raise JOURNAL.Held("Volume consumers changed before restart")
    proposals = restart_proposals(item, writer, remaining=True)
    for pod in pods(item):
        if pod.get("status", {}).get("phase") not in RC.FINISHED and not _started_owner(pod, item):
            raise JOURNAL.Held("An unreviewed pod is using the replacement data")
    if proposals:
        if admission(proposals).get("blocked") is not False:
            raise JOURNAL.Held("Remaining workloads need a fresh placement review; already restarted workloads were not rolled back")
        bound_destination(item, writer)
        fresh = restart_proposals(item, writer, remaining=True)
        if JOURNAL.digest(proposals) != JOURNAL.digest(fresh):
            raise JOURNAL.Held("Workload configuration changed during restart admission")
        for pod in pods(item):
            if pod.get("status", {}).get("phase") not in RC.FINISHED and not _started_owner(pod, item):
                raise JOURNAL.Held("An unreviewed pod appeared during restart admission")
        selected = fresh[0]
        saved = writer.write("restart:" + selected["path"], "PUT", selected["path"], selected["object"],
                             expected=JOURNAL.identity(selected["object"]))
        consumer = next(c for c in ref["consumers"] if _path(ref["namespace"], c["kind"], c["name"]) == selected["path"])
        _restored(consumer, saved)
        return "running", 93, "One workload restored on the copied volume; checking the rest before their next writes"
    waiting = []
    for consumer in ref["consumers"]:
        path = _path(ref["namespace"], consumer["kind"], consumer["name"])
        obj = writer.observe(_receipt(item, "restart:" + path))
        status = obj.get("status", {})
        kind = consumer["kind"]
        if kind in ("Deployment", "StatefulSet"):
            replicas = consumer["replicas"]
            ready = (status.get("observedGeneration", -1) >= obj["metadata"].get("generation", 0) and
                     status.get("replicas", 0) == replicas and status.get("readyReplicas", 0) == replicas)
            if kind == "Deployment":
                ready = ready and status.get("availableReplicas", 0) == replicas and status.get("updatedReplicas", 0) == replicas
            if not ready:
                waiting.append(consumer["name"])
        elif kind == "VirtualMachine" and consumer["run_strategy"] != "Halted":
            if status.get("printableStatus") != "Running" or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                waiting.append(consumer["name"])
    if waiting:
        return "running", 96, "Waiting for workload readiness: " + ", ".join(waiting)
    ref.update(handoff_phase="done", retain_resources=False)
    checkpoint(item)
    return "succeeded", 100, "Storage moved and workloads ready; original data retained. Verify application data separately."
