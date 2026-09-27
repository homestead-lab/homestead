"""Joint restart admission for storage moves, including controller backlog.

Read-only: project destination PVC references, retain all scheduled/terminating
pods, and model the unscheduled demand from every affected controller. Uses the
existing container/VM resource models and bounded joint-placement search.
"""
import copy

import homestead_batch_capacity as BATCH
import homestead_place as PLACE
import homestead_rollout_capacity as ROLLOUT
import homestead_vm_batch as VM_BATCH
import homestead_storage_journal as JOURNAL
import homestead_reclass_handoff as HANDOFF


def _count(value, default=1):
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("Workload parallelism must be an integer between zero and 100")
    return value


def _nonnegative(value, default=0):
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2147483647:
        raise ValueError("Workload ordinal/completions value is invalid")
    return value


def _project(template, mapping):
    template = copy.deepcopy(template)
    for volume in template.get("spec", {}).get("volumes", []):
        pvc = volume.get("persistentVolumeClaim")
        if pvc and pvc.get("claimName") in mapping:
            pvc["claimName"] = mapping[pvc["claimName"]]
        dv = volume.get("dataVolume")
        if dv and dv.get("name") in mapping:
            volume.pop("dataVolume")
            volume["persistentVolumeClaim"] = {"claimName": mapping[dv["name"]]}
    return template


def plan(item, proposals, read, nodes, threshold):
    ref, ns = item["ref"], item["ref"]["namespace"]
    by_path = {p["path"]: p for p in proposals}
    if len(by_path) != len(proposals):
        raise ValueError("Restart proposals contain duplicate targets")
    # Before cutover, the new storage is still under its temporary name. Use its
    # real PVC/PV constraints, never the old class or invented free attachment.
    mapping = {} if ref.get("cutover_complete") else {ref["claim"]: ref["temp"]}
    pods = copy.deepcopy(ROLLOUT.items(read, "/api/v1/pods"))
    if any(not isinstance(p, dict) or not p.get("metadata", {}).get("uid") for p in pods):
        raise ValueError("Pod identity inventory is incomplete")
    rows, vms, created, headroom = [], [], {}, {}
    warnings = set()
    writer = JOURNAL.Journal(item, read, None, None)
    writer.check()
    def add(name, template, replicas):
        rows.append({"name": name, "replicas": replicas,
                     "deployment": {"metadata": {"name": name, "namespace": ns},
                                    "spec": {"replicas": replicas, "template": _project(template, mapping)}}})
    def deficit(owned, wanted, template):
        nonlocal pods
        scheduled = [p for p in owned if p.get("spec", {}).get("nodeName") and not p["metadata"].get("deletionTimestamp")]
        for pod in scheduled:
            host = pod["spec"]["nodeName"]
            estimate = max(PLACE._pod_memory(pod["spec"])[0], PLACE._pod_memory(template["spec"])[0])
            headroom[host] = headroom.get(host, 0) + max(0, estimate - PLACE._pod_request(pod["spec"], "memory")) / 1024**3
        # Unassigned owned pods are replaced by a synthetic deficit, not added
        # twice. Terminating pods are never treated as freed reservations.
        pending = {p["metadata"]["uid"] for p in owned if not p.get("spec", {}).get("nodeName") and not p["metadata"].get("deletionTimestamp")}
        pods = [p for p in pods if p["metadata"]["uid"] not in pending]
        return max(0, wanted - len(scheduled))
    for consumer in ref["consumers"]:
        kind, name = consumer["kind"], consumer["name"]
        path = HANDOFF._path(ns, kind, name)
        receipt = HANDOFF._receipt(item, "restart:" + path)
        if receipt:
            current = writer.observe(receipt)
            HANDOFF._restored(consumer, current)
            obj = copy.deepcopy(current)
        else:
            proposal = by_path.pop(path, None)
            if not proposal or proposal.get("kind") != kind:
                raise ValueError("An affected workload is missing from restart admission")
            obj = copy.deepcopy(proposal["object"])
            current = read(path)
            if JOURNAL.identity(obj) != JOURNAL.identity(current):
                raise ValueError("A restart proposal changed before admission")
        if obj["metadata"].get("namespace") != ns or obj["metadata"].get("name") != name:
            raise ValueError("Restart workload identity does not match the job")
        uid = JOURNAL.identity(current)["uid"]
        own = [p for p in pods if p["metadata"].get("namespace") == ns and
               ROLLOUT.controller(p).get("uid") == uid and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        if kind == "VirtualMachine":
            if obj["spec"].get("runStrategy") == "Halted":
                continue
            obj["spec"]["template"] = _project(obj["spec"]["template"], mapping)
            vms.append({"namespace": ns, "name": name, "vm": obj, "claims": [], "downloads": []})
            if receipt:
                created[name] = {"uid": uid}
            continue
        if kind == "Deployment":
            owned, known = ROLLOUT.owned_pods(current, pods, read, ns)
            if not known:
                raise ValueError("Deployment pod ownership is incomplete during restart admission")
            template = obj["spec"]["template"]
            count = _count(obj["spec"].get("replicas"))
            add("Deployment/" + name, template, deficit(owned, count, template))
        elif kind == "StatefulSet":
            spec = obj["spec"]
            if spec.get("persistentVolumeClaimRetentionPolicy", {}).get("whenScaled", "Retain") != "Retain":
                raise ValueError("StatefulSet scaling can delete its claims; change its retention policy before moving storage")
            count, start = _count(spec.get("replicas")), _nonnegative(spec.get("ordinals", {}).get("start"))
            for ordinal in range(start, start + count):
                pod_name = f"{name}-{ordinal}"
                template = copy.deepcopy(spec["template"])
                template.setdefault("metadata", {}).setdefault("labels", {}).update({"statefulset.kubernetes.io/pod-name": pod_name,
                                                                                    "apps.kubernetes.io/pod-index": str(ordinal)})
                volumes = template["spec"].setdefault("volumes", [])
                for definition in spec.get("volumeClaimTemplates", []):
                    volume_name = definition["metadata"]["name"]
                    claim_name = f"{volume_name}-{name}-{ordinal}"
                    # Existing controller-generated storage must be readable;
                    # this move does not silently provision unrelated disks.
                    read(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim_name}")
                    volumes[:] = [v for v in volumes if v.get("name") != volume_name]
                    volumes.append({"name": volume_name, "persistentVolumeClaim": {"claimName": claim_name}})
                ordinal_pods = [p for p in own if p["metadata"]["name"] == pod_name]
                add("StatefulSet/" + pod_name, template, deficit(ordinal_pods, 1, template))
        elif kind == "CronJob":
            spec = obj["spec"]
            jobs = ROLLOUT.items(read, f"/apis/batch/v1/namespaces/{ns}/jobs")
            active = [j for j in jobs if ROLLOUT.controller(j).get("kind") == "CronJob" and ROLLOUT.controller(j).get("uid") == uid and
                      not any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in j.get("status", {}).get("conditions", []))]
            for job in active:
                job_uid = JOURNAL.identity(job)["uid"]
                owned = [p for p in pods if p["metadata"].get("namespace") == ns and ROLLOUT.controller(p).get("kind") == "Job" and
                         ROLLOUT.controller(p).get("uid") == job_uid and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
                job_spec = job["spec"]
                parallel = min(_count(job_spec.get("parallelism")), _nonnegative(job_spec.get("completions"), 100))
                add("Job/" + job["metadata"]["name"], job_spec["template"], deficit(owned, parallel, job_spec["template"]))
            if not spec.get("suspend", False) and (not active or spec.get("concurrencyPolicy", "Allow") != "Forbid"):
                template = spec["jobTemplate"]["spec"]
                add("CronJob/" + name, template["template"], min(_count(template.get("parallelism")), _nonnegative(template.get("completions"), 100)))
            if not spec.get("suspend", False):
                warnings.add(f"{name}: scheduled jobs are checked for one next execution, not guaranteed future capacity; Allow/Replace can overlap old or terminating jobs")
        else:
            raise ValueError("Unsupported controller in storage restart admission")
    if by_path:
        raise ValueError("Restart admission includes workloads outside this job")
    uses, writable_guests = {}, []
    for row in rows:
        if row["replicas"]:
            for volume in row["deployment"]["spec"]["template"]["spec"].get("volumes", []):
                claim = volume.get("persistentVolumeClaim", {}).get("claimName")
                if claim:
                    uses.setdefault(claim, set()).add(row["name"])
    for vm in vms:
        spec = vm["vm"]["spec"]["template"]["spec"]
        disks = {d.get("name"): d for d in spec.get("domain", {}).get("devices", {}).get("disks", [])}
        for volume in spec.get("volumes", []):
            pvc = volume.get("persistentVolumeClaim", {})
            claim = pvc.get("claimName") or volume.get("dataVolume", {}).get("name")
            disk = disks.get(volume.get("name"), {})
            if claim:
                uses.setdefault(claim, set()).add("VM/" + vm["name"])
                if not (pvc.get("readOnly") or disk.get("disk", {}).get("readOnly") or disk.get("lun", {}).get("readOnly")):
                    writable_guests.append((vm["name"], claim))
    if vms:
        result = VM_BATCH.plan(vms, read, nodes, created=created, threshold=threshold,
                               extra_entries=rows, pod_snapshot=pods, starting_headroom=headroom)
    else:
        nodes = copy.deepcopy(nodes)
        for node in nodes:
            node["batch_starting_headroom_gb"] = headroom.get(node["name"], 0)
        result = BATCH.plan(rows, ns, pods, nodes, {}, threshold, read=read)
    result["warnings"] = sorted(set(result["warnings"]) | warnings)
    conflicts = [f"{name}: writable VM disk {claim} would also be used by another restarting workload; RWX does not make concurrent guest-disk writers safe"
                 for name, claim in writable_guests if len(uses[claim]) > 1]
    if conflicts:
        result.update(blocked=True, status="blocked", blockers=sorted(set(result.get("blockers", [])) | set(conflicts)))
    return result
