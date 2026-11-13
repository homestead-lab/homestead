"""Read-only inspection and explicit release of an interrupted VM save journal.

Releasing tracking is not cleanup, retry, rollback or proof that writes failed.
The dispatcher fence cannot cancel a request already received by Kubernetes.
"""
import copy
import json
import time
from urllib.parse import quote

import homestead_capacity_review as REVIEW
import homestead_vm_mutation_job as JOB
import homestead_vm_power_recovery as RECOVERY
import homestead_vm_write as WRITE
import homestead_import_job as IMPORT


def _snapshot(item, read, actor):
    ref = item.get("ref") or {}
    batch = item.get("kind") == "k3s-cluster"
    importing = item.get("kind") == IMPORT.KIND
    if (item.get("kind") not in ("vm-create", "vm-edit", "k3s-cluster", IMPORT.KIND) or not ref.get("retain_resources")
            or item.get("status") in ("succeeded", "cancelled", "cancelling")
            or ref.get("phase") not in (("provisioning", "awaiting-ready", "failed") if batch else ("prepared", "writing", "failed", "copying") if importing else ("prepared", "writing", "failed"))):
        raise ValueError("This job has no unresolved configuration dispatch")
    if ref.get("dispatch_protocol") != (2 if batch else 1):
        raise ValueError("This older job has no dispatcher fence that can be checked. Its record must be recovered manually.")
    names = [row["name"] for row in ref["nodes"]] if batch else [ref["name"]]
    if not names or len(names) != len(set(names)):
        raise ValueError("Recovery target list is malformed")
    targets = [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "namespace": ref["namespace"], "name": name} for name in names]
    if importing:
        targets = copy.deepcopy(ref["planned"])
    entries = ref.get("writes", [])
    for entry in entries:
        if entry["resource"] not in targets:
            targets.append(entry["resource"])
    observed, blockers = [], []
    for target in targets:
        version, kind, ns, name = (target[key] for key in ("apiVersion", "kind", "namespace", "name"))
        plural = next((plural for (api, plural), resource_kind in WRITE.RESOURCES.items() if (api, resource_kind) == (version, kind)), None)
        if not plural or not WRITE.NAME.fullmatch(ns) or not WRITE.NAME.fullmatch(name):
            raise ValueError("Recovery target is malformed")
        prefix = "/api/v1" if version == "v1" else "/apis/" + version
        value = RECOVERY._object(read, f"{prefix}/namespaces/{quote(ns, safe='')}/{plural}/{quote(name, safe='')}")
        identity = RECOVERY._identity(value, ns, name)
        if value is not None and (value.get("kind"), value.get("apiVersion")) != (kind, version):
            raise ValueError("Recovery resource type could not be verified")
        deleting = bool((value or {}).get("metadata", {}).get("deletionTimestamp"))
        if deleting:
            blockers.append(f"{kind} {ns}/{name} is deleting. Wait for it to settle, then review again.")
        receipts = [entry for entry in entries if entry["resource"] == target]
        last = receipts[-1] if receipts else {}
        expected = last.get("identity") or last.get("before") or (ref.get("identity") if target == targets[0] else None)
        if importing and kind == "PersistentVolumeClaim" and not expected:
            expected = ref.get("claims", {}).get(name)
        observed.append({"resource": target, "current": identity, "expected": expected,
                         "relationship": "not found" if not identity else "identity unproven" if not expected else
                         "same identity" if identity["uid"] == expected["uid"] else "replacement; not adopted",
                         "last_write": last.get("phase", "not dispatched"), "deleting": deleting})
    warnings = ["This only resolves the tracking. It changes no VM, image, disk or Secret, and anything already created stays so it can be checked.",
                "The Homestead dispatcher is no longer active, but Kubernetes may still apply a previously sent request late. Current state does not prove its original outcome.",
                "Accepted receipts prove only that individual resource writes were acknowledged, not that the full save completed or the guest is healthy.",
                "This releases the job's block on future VM reviews. It never retries, adopts replacements or rolls back resources. The old approval remains consumed."]
    if batch:
        warnings.append("All planned VMs are shown, including those not sent. Guest installation may continue. This does not check k3s health or free IP addresses, and it does not resume the rest of the batch.")
    if importing:
        blockers += IMPORT.recovery_blockers(item, read)
        warnings = ["This records an unknown outcome only. It does not retry the import, start the app, remove its safety hold or delete any workload, Service, Job or volume.",
                    "Inspect partial files and retained resources before a new review. Accepted write receipts do not prove data integrity or a complete import. A previously sent request can still take effect late."]
    plan = {"id": item["id"], "blocked": bool(blockers), "blockers": blockers, "warnings": warnings,
            "requires_confirmation": True, "confirm": ref["name"], "resource": {"namespace": ref["namespace"], "name": ref["name"]},
            "action": item["kind"], "dispatch_phase": ref["phase"], "resources": observed}
    context = {"action": "vm-mutation-recovery", "actor": actor, "job_id": item["id"], "ref": copy.deepcopy(ref), "resources": observed}
    return plan, context


def preview(ident, ops, read, actor):
    try:
        with RECOVERY._exclusive(ident, ops), ops.transaction():
            plan, context = _snapshot(RECOVERY._job(ops.stored(), ident), read, actor)
            return {"plan": plan, "capacity_token": REVIEW.issue({"id": ident}, context)}
    except RECOVERY.DispatcherBusy:
        return {"plan": {"id": ident, "blocked": True, "blockers": ["The configuration dispatcher is active. Wait before inspecting its outcome."],
                         "warnings": [], "requires_confirmation": True}, "capacity_token": None}


def resolve(body, ops, read, actor):
    ident = str(body.get("id") or "")
    try:
        with RECOVERY._exclusive(ident, ops), ops.transaction() as t:
            items = t.items
            item = RECOVERY._job(items, ident)
            plan, context = _snapshot(item, read, actor)
            REVIEW.enforce({"id": ident, "capacity_token": body.get("capacity_token"), "confirm_capacity": body.get("confirm_capacity")}, plan, context)
            if body.get("acknowledge_unknown") is not True or (item["kind"] != IMPORT.KIND and body.get("confirm") != plan["confirm"]):
                raise ValueError("Type the reviewed name and tick each acknowledgement. They cover retained resources, "
                                 "the unknown outcome and possible late effects")
            ref = item["ref"]
            ref["recovery"] = {"by": actor, "at": time.time(), "previous_phase": ref["phase"],
                               "resources": copy.deepcopy(plan["resources"]), "outcome": "unknown", "late_effect_acknowledged": True}
            ref.update(phase="resolved-unknown", retain_resources=False)
            if item["kind"] == "k3s-cluster":
                item.update(tracking_stopped=True, tracking_stopped_at=ops._now())
            importing = item["kind"] == IMPORT.KIND
            ops._finish(item, "failed", item.get("progress", 0),
                        f"{actor} checked the unfinished {'import' if importing else 'VM save'}, and its outcome is still unknown. The resources are kept. Nothing was retried or rolled back, and a late effect is still possible.")
            t.save(items)
            return {"ok": True, "detail": "Tracking resolved as unknown, and nothing was sent to the cluster. The resources and import holds stay. Any new action needs a new review." if importing else "Tracking resolved as unknown; no cluster change was sent. Resources remain and any new VM action requires a fresh review.",
                    "operation": ops._public(item)}
    except RECOVERY.DispatcherBusy:
        raise ValueError("The dispatcher is running, so nothing was resolved. Wait and review again.") from None


# Removing what a failed VM create made (an admin's reviewed removal): only
# what this attempt provably made goes, each delete fenced on the identity the
# review showed (the token signs it, so a change since the review refuses the
# removal), written into the job before any is sent so a restart finishes it
# (JOB.resume_cleanups) and never removes more. Claims, Secrets and ConfigMaps
# that any VM still uses stay, unless that VM goes in the same review.
_REMOVED_HERE = ("VirtualMachine", *JOB.REMOVABLE)
_KIND_WORDS = {"VirtualMachine": "VM", "PersistentVolumeClaim": "Volume"}
CREATED = "Created by this attempt"


ANY = "*"  # a use that could not be read: every object of that kind counts as used
HOOKS = "hooks.kubevirt.io/hookSidecars"


def _secret_or_config(used, source):
    for key, kind, field in (("secret", "Secret", "secretName"), ("configMap", "ConfigMap", "name")):
        name = (source.get(key) or {}).get(field) or (source.get(key) or {}).get("name")
        if name:
            used.add((kind, name))


def _instance_uses(spec, annotations):
    """What a VM template's or a VM instance's spec names: {(kind, name)} of
    the claims, Secrets and ConfigMaps its volumes, access credentials and
    hook sidecars use (a DataVolume's claim has the DataVolume's name)."""
    used = set()
    for volume in spec.get("volumes") or []:
        for key, field in (("persistentVolumeClaim", "claimName"), ("dataVolume", "name"), ("memoryDump", "claimName")):
            if (volume.get(key) or {}).get(field):
                used.add(("PersistentVolumeClaim", volume[key][field]))
        claim = ((volume.get("ephemeral") or {}).get("persistentVolumeClaim") or {}).get("claimName")
        if claim:
            used.add(("PersistentVolumeClaim", claim))
        _secret_or_config(used, volume)
        _secret_or_config(used, volume.get("sysprep") or {})
        for key in ("cloudInitNoCloud", "cloudInitConfigDrive"):
            for field in ("secretRef", "networkDataSecretRef"):
                name = ((volume.get(key) or {}).get(field) or {}).get("name")
                if name:
                    used.add(("Secret", name))
    for credential in spec.get("accessCredentials") or []:
        for key in ("sshPublicKey", "userPassword"):
            name = ((((credential.get(key) or {}).get("source") or {}).get("secret")) or {}).get("secretName")
            if name:
                used.add(("Secret", name))
    # A ROM ConfigMap is used only through a hook sidecar (homestead_passthrough).
    hooks = (annotations or {}).get(HOOKS)
    if hooks:
        try:
            for sidecar in json.loads(hooks):
                if (sidecar.get("configMap") or {}).get("name"):
                    used.add(("ConfigMap", sidecar["configMap"]["name"]))
        except (ValueError, TypeError, AttributeError):
            used.add(("ConfigMap", ANY))
    return used


def _uses(vm):
    """What a VM names, in its template, its disk templates' sources and its
    hotplug requests."""
    spec = vm.get("spec") or {}
    template = spec.get("template") or {}
    annotations = {**((vm.get("metadata") or {}).get("annotations") or {}), **((template.get("metadata") or {}).get("annotations") or {})}
    used = _instance_uses(template.get("spec") or {}, annotations)
    for disk in spec.get("dataVolumeTemplates") or []:
        source = ((disk.get("spec") or {}).get("source") or {}).get("pvc") or {}
        if source.get("name"):
            used.add(("PersistentVolumeClaim", source["name"]))
    for request in (vm.get("status") or {}).get("volumeRequests") or []:
        source = ((request.get("addVolumeOptions") or {}).get("volumeSource") or {})
        for key, field in (("persistentVolumeClaim", "claimName"), ("dataVolume", "name")):
            if (source.get(key) or {}).get(field):
                used.add(("PersistentVolumeClaim", source[key][field]))
    return used


def _pod_uses(pod):
    """What a pod mounts or reads: its volumes (projected ones included), its
    containers' env and envFrom, and its image pull Secrets."""
    spec = pod.get("spec") or {}
    used = set()
    for volume in spec.get("volumes") or []:
        if (volume.get("persistentVolumeClaim") or {}).get("claimName"):
            used.add(("PersistentVolumeClaim", volume["persistentVolumeClaim"]["claimName"]))
        _secret_or_config(used, volume)
        for source in (volume.get("projected") or {}).get("sources") or []:
            _secret_or_config(used, source)
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []) + (spec.get("ephemeralContainers") or []):
        for env in container.get("env") or []:
            for key, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
                name = ((env.get("valueFrom") or {}).get(key) or {}).get("name")
                if name:
                    used.add((kind, name))
        for source in container.get("envFrom") or []:
            for key, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
                if (source.get(key) or {}).get("name"):
                    used.add((kind, source[key]["name"]))
    for secret in spec.get("imagePullSecrets") or []:
        if secret.get("name"):
            used.add(("Secret", secret["name"]))
    return used


def _listed(read, path):
    """Every item of a list, or None when it cannot be read whole (an error,
    a malformed answer, or a list cut short with continue)."""
    try:
        listing = RECOVERY._object(read, path)
    except ValueError:
        return None
    items = (listing or {}).get("items")
    if not isinstance(items, list) or ((listing or {}).get("metadata") or {}).get("continue"):
        return None
    return items


def _users(read, namespace, going):
    """[(words, uses)] for every VM, VM instance and pod in the namespace but
    the going VM and what it owns (its instance, the instance's launcher pod
    and that pod's hotplug pods, by owner uid), or None when one of the three
    lists cannot be read."""
    ns = quote(namespace, safe="")
    vms = _listed(read, f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines")
    instances = _listed(read, f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances")
    pods = _listed(read, f"/api/v1/namespaces/{ns}/pods")
    if vms is None or instances is None or pods is None:
        return None
    owned = {going} if going else set()
    for _ in range(3):  # VM -> instance -> launcher pod -> hotplug pod
        for row in instances + pods:
            meta = row.get("metadata") or {}
            if any(ref.get("uid") in owned for ref in meta.get("ownerReferences") or []):
                owned.add(meta.get("uid"))
    found = []
    for rows, words, uses in ((vms, "VM", _uses), (instances, "running VM",
                              lambda vmi: _instance_uses(vmi.get("spec") or {}, (vmi.get("metadata") or {}).get("annotations"))),
                              (pods, "pod", _pod_uses)):
        for row in rows:
            meta = row.get("metadata") or {}
            if meta.get("uid") not in owned:
                found.append((f"{words} {meta.get('name')}", uses(row)))
    return found


def _used_by(users, target):
    key = (target["kind"], target["name"])
    return next((words for words, used in users if key in used or (target["kind"], ANY) in used), None)


def _removal_snapshot(item, read, actor):
    ref = item.get("ref") or {}
    if item.get("kind") == "vm-create" and (ref.get("cleanup") or {}).get("state") == "removing":
        raise ValueError("A removal of this job is being finished. Wait, then review again.")
    if (item.get("kind") != "vm-create" or item.get("status") != "failed" or ref.get("phase") != "failed"
            or not ref.get("retain_resources") or ref.get("dispatch_protocol") != 1):
        raise ValueError("This job has nothing to remove. Only a failed VM create that kept what it made can be reviewed here.")
    namespace, name = ref["namespace"], ref["name"]
    vm_target = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "namespace": namespace, "name": name}
    posts = {}
    for entry in ref.get("writes") or []:
        if entry.get("method") == "POST":
            posts.setdefault(tuple(entry["resource"][key] for key in ("apiVersion", "kind", "namespace", "name")), entry)
    # The VM of the job's name is read whatever the record says: a failure
    # before its POST leaves no row, and a VM made since by other means would
    # then use these claims unseen.
    posts.setdefault(tuple(vm_target[key] for key in ("apiVersion", "kind", "namespace", "name")),
                     {"resource": vm_target, "phase": "not dispatched"})
    rows, blockers = [], []
    for entry in posts.values():
        target = entry["resource"]
        version, kind, ns, object_name = (target[key] for key in ("apiVersion", "kind", "namespace", "name"))
        plural = next((plural for (api, plural), resource_kind in WRITE.RESOURCES.items() if (api, resource_kind) == (version, kind)), None)
        if not plural or ns != namespace or not WRITE.NAME.fullmatch(object_name):
            raise ValueError("Recovery target is malformed")
        prefix = "/api/v1" if version == "v1" else "/apis/" + version
        value = RECOVERY._object(read, f"{prefix}/namespaces/{quote(ns, safe='')}/{plural}/{quote(object_name, safe='')}")
        current = RECOVERY._identity(value, ns, object_name)
        if current and kind == "VirtualMachine":
            # A VM's status moves with every controller write: its review signs
            # what must not change ("settled") instead of its resourceVersion,
            # and its delete is fenced on its uid.
            current = REVIEW.review_identity(value)
        if value is not None and (value.get("kind"), value.get("apiVersion")) != (kind, version):
            raise ValueError("Recovery resource type could not be verified")
        if (value or {}).get("metadata", {}).get("deletionTimestamp"):
            blockers.append(f"{_KIND_WORDS.get(kind, kind)} {object_name} is being removed. Wait until it is gone, then review again.")
        sent = entry.get("phase") != "not dispatched"
        ours = sent and value is not None and JOB.made_by_attempt(value, entry, item["id"])
        known = bool((entry.get("identity") or {}).get("uid")) or entry.get("phase") == "refused"
        if value is None:
            relationship = "Not found"
        elif not sent:
            relationship = "Not made by this attempt"
        elif not ours:
            relationship = "Another object of that name" if known else "Not shown to be this attempt's"
        elif kind not in _REMOVED_HERE:
            relationship = "Not removed by this review"
        else:
            relationship = CREATED
        rows.append({"resource": target, "current": current, "expected": entry.get("identity"),
                     "last_write": entry.get("phase"), "relationship": relationship,
                     "remove": relationship == CREATED,
                     # Still there and possibly this attempt's: it keeps the job's hold on the name.
                     "holds": value is not None and relationship not in ("Another object of that name", CREATED, "Not made by this attempt"),
                     "value": value})
    vm = next(row for row in rows if row["resource"] == vm_target)
    warnings = ["Only what this attempt created is removed: the object its create answered for, or a VM with this "
                "job's id. Anything else of that name stays."]
    if vm["current"] and not vm["remove"]:
        # Claims, Secrets and ConfigMaps a VM of that name may use stay with it.
        blockers.append(f"A VM named {name} exists, and nothing on it shows that this attempt created it. It is left "
                        "as it is, and so are the volumes, Secrets and ConfigMaps it may use.")
        for row in rows:
            if row["remove"]:
                row.update(remove=False, holds=True)
    elif vm["remove"]:
        warnings.insert(0, f"The VM {name} was created by this attempt, and it is removed with the rest. "
                           "The data on its volumes is lost.")
        # What the VM alone owns (commit_vm makes it the owner of the Secret
        # and the ROM ConfigMaps) is deleted by the garbage collector with
        # it, whatever this review says: it is shown as going with the VM,
        # never as kept, and gets no delete of its own.
        for row in rows:
            owners = ((row["value"] or {}).get("metadata") or {}).get("ownerReferences") or []
            if row is not vm and owners and all(owner.get("uid") == vm["current"]["uid"] for owner in owners):
                row.update(relationship="Goes with the VM", remove=False, holds=False, with_vm=True)
        # Disks KubeVirt makes from the VM's dataVolumeTemplates are owned by
        # the VM and go with it; they are not in the record, so they are shown.
        for template in (vm["value"].get("spec") or {}).get("dataVolumeTemplates") or []:
            disk = (template.get("metadata") or {}).get("name")
            if disk:
                rows.append({"resource": {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "namespace": namespace, "name": disk},
                             "current": None, "expected": None, "last_write": "not dispatched",
                             "relationship": "Goes with the VM", "remove": False, "holds": False, "with_vm": True, "value": None})
        warnings.append("Disks made from the VM's own templates are not in this job's record. KubeVirt removes the ones "
                        "the VM owns (Goes with the VM) with it, and Harvester keeps its template disks. Check Volumes "
                        "once the VM is gone.")
    if any(row["remove"] or row.get("with_vm") for row in rows):
        # Anything else in the namespace that uses one of these keeps it: a VM,
        # a VM instance or a pod. A list that cannot be read keeps them all:
        # nothing is removed on a guess.
        users = _users(read, namespace, vm["current"]["uid"] if vm["remove"] else None)
        if users is None:
            blockers.append(f"The VMs and pods in {namespace} could not be read, so it is not known which of these they "
                            "use. Nothing is removed. Review again.")
        for row in rows:
            by = _used_by(users or [], row["resource"])
            if not by:
                continue
            if row.get("with_vm"):
                # What goes with the VM cannot be fenced: the VM stays.
                row["relationship"] = f"Goes with the VM, and used by {by}"
                what = {"PersistentVolumeClaim": "disk"}.get(row["resource"]["kind"], row["resource"]["kind"])
                blockers.append(f"The {what} {row['resource']['name']} goes with the VM {name}, and {by} uses it. "
                                "Nothing is removed.")
            elif row["remove"] and row["resource"]["kind"] != "VirtualMachine":
                row.update(remove=False, holds=True, relationship=f"Used by {by}")
    for row in rows:
        row.pop("value")
    rows.sort(key=lambda row: row["resource"]["kind"] != "VirtualMachine")  # the VM goes first
    plan = {"id": item["id"], "blocked": bool(blockers), "blockers": blockers, "warnings": warnings,
            "requires_confirmation": True, "confirm": name, "resource": {"namespace": namespace, "name": name},
            "action": "vm-create-removal", "resources": rows}
    context = {"action": "vm-create-removal", "actor": actor, "job_id": item["id"], "ref": copy.deepcopy(ref), "resources": rows}
    return plan, context


BUSY = "Another check or removal of this job is running, or the VM create is. Wait, then review again."


def removal_preview(ident, ops, read, actor):
    try:
        with RECOVERY._exclusive(ident, ops), ops.transaction():
            plan, context = _removal_snapshot(RECOVERY._job(ops.stored(), ident), read, actor)
            return {"plan": plan, "capacity_token": None if plan["blocked"] else REVIEW.issue({"id": ident}, context)}
    except RECOVERY.DispatcherBusy:
        return {"plan": {"id": ident, "blocked": True, "blockers": [BUSY], "warnings": [], "requires_confirmation": True},
                "capacity_token": None}


def remove(body, ops, read, actor, send=None):
    """Removes what the reviewed plan said goes, and nothing else: the review
    is read again only to prove nothing changed (the token), and each delete
    is fenced on the identity the review showed: a VM on its uid, a claim,
    Secret or ConfigMap on its uid and resourceVersion."""
    import homestead_kube as KUBE
    send = send or KUBE.ksend
    ident = str(body.get("id") or "")
    try:
        with RECOVERY._exclusive(ident, ops):
            with ops.transaction() as t:
                items = t.items
                item = RECOVERY._job(items, ident)
                plan, context = _removal_snapshot(item, read, actor)
                if plan["blocked"]:
                    raise ValueError(" ".join(plan["blockers"]) + " Nothing was removed.")
                if not REVIEW.valid({"id": ident, "capacity_token": body.get("capacity_token")}, context):
                    raise ValueError("What this attempt created changed since the review, or the review expired. "
                                     "Nothing was removed. Review again.")
                if body.get("confirm") != plan["confirm"]:
                    raise ValueError("Type the VM name to confirm. Nothing was removed.")
                steps = [{"sequence": next(entry["sequence"] for entry in item["ref"]["writes"]
                                           if entry["resource"] == row["resource"] and entry.get("method") == "POST"),
                          "resource": row["resource"], "uid": row["current"]["uid"], "phase": "intent",
                          **({} if row["resource"]["kind"] == "VirtualMachine" else {"resourceVersion": row["current"]["resourceVersion"]})}
                         for row in plan["resources"] if row["remove"]]
                item["ref"]["cleanup"] = {"state": "removing", "origin": "reviewed", "by": actor, "at": time.time(),
                                          "steps": steps, "kept": [row["resource"] for row in plan["resources"] if row["holds"]],
                                          # Owned by the VM: gone with it, kept if it stays.
                                          "with_vm": [row["resource"] for row in plan["resources"]
                                                      if row.get("with_vm") and row["current"]]}
                t.save(items)
            outcome = JOB._run_cleanup(ident, ops, send) or {}
    except RECOVERY.DispatcherBusy:
        raise ValueError(BUSY.replace("Wait, then review again.", "Nothing was removed. Wait, then review again.")) from None
    item = RECOVERY._job(ops.stored(), ident)
    return {"ok": bool(outcome.get("all")), "removed": outcome.get("removed", 0), "detail": item.get("message", ""),
            "operation": ops._public(item)}
