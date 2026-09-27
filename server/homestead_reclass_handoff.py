"""Replacement reclass stages under construction; not yet a registered resolver.

Reuse reclass manifests and the shared operation store. Do not enable this engine
until cutover, restart admission, cancellation and reviewed recovery are wired.
"""
import copy

import homestead_reclass as RC
import homestead_storage_journal as JOURNAL


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
        ref["copy_claims"][name] = {"uid": meta["uid"], "pv": volume, "pv_uid": pv_meta["uid"]}
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


def copy_stage(item, checkpoint, admission):
    """Make and observe the copy; never stop, roll back or restart on failure.

    Called only after journaled_stop. The caller owns the operations lock and
    turns Held into an inspectable recovery hold, not an automatic retry.
    """
    ref = item["ref"]
    writer = JOURNAL.Journal(item, RC.kget, RC.ksend, checkpoint)
    writer.check()
    stopped(item, writer)
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
        stopped(item, writer)
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
        return "running", 35, "Copying and checking data; waiting for the copy Job and its pods to finish"
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
