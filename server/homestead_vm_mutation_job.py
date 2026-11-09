"""One-shot reviewed VM configuration dispatch; never replay partial writes."""
import hashlib
import time

import homestead_vm_power_job as POWER
import homestead_vm_write as WRITE


def dispatch(kind, body, namespace, name, identity, ops, send, commit):
    if kind not in ("vm-create", "vm-edit"):
        raise ValueError("Unsupported reviewed VM mutation")
    token = body["capacity_token"]  # admission has already verified this token
    ref = {"namespace": namespace, "name": name, "identity": identity,
           "state_initialization_acknowledged": body.get("ack_state_initialization") is True and body.get("confirm_state_name") == name,
           "review_digest": hashlib.sha256(token.encode()).hexdigest(),
           "review_expires": int(token.split(".", 1)[0]), "dispatch_protocol": 1,
           "phase": "prepared", "phase_at": time.time(), "retain_resources": True, "writes": []}
    job = ops.start(kind, f"{'Create' if kind == 'vm-create' else 'Save'} VM {name}",
                    {"kind": "VirtualMachine", "namespace": namespace, "name": name}, "/vms", ref,
                    "Settings recorded. No request has been sent yet.")
    ident = job["id"]
    with POWER.worker_lock(ident, ops):
        # Recovery can win the lock between publishing intent and dispatching.
        # This state check fences that case before even image preparation.
        ops.record_phase(ident, "writing", 0, "Preparing the reviewed VM settings. Each write is recorded.")
        writer = WRITE.ResourceWriter(send, WRITE.operation_recorder(ops, ident))
        try:
            result = commit(writer)
            writer.check()  # a low-level warning must not hide a failed write
            if not writer.count:
                raise ValueError("VM commit did not produce a verified resource receipt")
            with ops.transaction() as t:
                items = t.items
                item = next(row for row in items if row["id"] == ident)
                if item["status"] in ops.TERMINAL or item["status"] == ops.CANCELLING:
                    raise ValueError("VM mutation job ended before completion could be recorded")
                vm_receipts = [entry for entry in item["ref"]["writes"] if entry["resource"] == {
                    "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "namespace": namespace, "name": name}]
                expected = result.get("vm_identity") or {}
                if (not vm_receipts or vm_receipts[-1]["phase"] != "accepted"
                        or (expected.get("namespace"), expected.get("name")) != (namespace, name)
                        or vm_receipts[-1]["identity"] != {key: expected.get(key) for key in ("uid", "resourceVersion")}):
                    raise ValueError("VM completion does not match its verified write receipt")
                item["ref"].update(phase="saved", retain_resources=False)
                ops._finish(item, "succeeded", 100,
                            "Kubernetes accepted the settings. Whether the guest is ready and its apps are healthy is not checked.")
                t.save(items)
                return {**result, "operation": ops._public(item)}
        except Exception:
            # Preserve partial/uncertain receipts and never retry a mutation;
            # raw errors may contain Secret/config content. Only a create the
            # cluster refused outright removes what it made (below).
            try:
                removed = _remove_refused_create(kind, namespace, name, ident, ops, send)
            except Exception:
                removed = None  # a clean-up begun is finished by resume_cleanups
            try:
                with ops.transaction() as t:
                    items = t.items
                    item = next(row for row in items if row["id"] == ident)
                    if item["status"] not in ops.TERMINAL:
                        item["ref"].update(phase="failed", retain_resources=bool(writer.count))
                        ops._finish(item, "failed", item.get("progress", 0),
                                    "The VM save stopped. Check the kept write records. Nothing was retried." if writer.count else
                                    "The VM save stopped before anything was written. Get a new review.")
                        t.save(items)
            except Exception:
                pass  # existing durable intent remains the recovery authority
            if not writer.count:
                raise  # preserve structured admission failures before writes
            if removed and removed["all"]:
                raise ValueError(f"The cluster refused VM {name}. What this attempt created was removed, and nothing was "
                                 f"retried. See job {ident}.") from None
            raise ValueError(f"The VM save failed, or its outcome is uncertain. Check job {ident}. The resources were kept, and nothing was retried.") from None


# What a refused create made and may remove: kinds made for this VM alone.
# A Harvester image keeps downloading (the VM can use it later) and a
# KubeVirt feature gate is the cluster's, so neither is removed.
REMOVABLE = {"PersistentVolumeClaim": "persistentvolumeclaims", "Secret": "secrets", "ConfigMap": "configmaps"}


def _remove_refused_create(kind, namespace, name, ident, ops, send):
    """When a VM create's VirtualMachine POST was refused (a definite 4xx:
    no VM was made, so nothing uses them), removes the claims, Secrets and
    ROM ConfigMaps its earlier writes made, each by the uid its POST
    answered with. The deletes go into the job as intents first, so a
    restart part way is finished by resume_cleanups. None when that is not
    the case; otherwise the job's clean-up record."""
    import homestead_kube as KUBE
    if kind != "vm-create":
        return None
    with ops.transaction() as t:
        items = t.items
        item = next((row for row in items if row.get("id") == ident), None)
        ref = (item or {}).get("ref") or {}
        writes = ref.get("writes") or []
        last = writes[-1] if writes else {}
        vm = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "namespace": namespace, "name": name}
        # The refusal's receipt finished the job as failed; one in any other
        # state is not this case.
        if ((item or {}).get("status") != "failed" or ref.get("cleanup") or last.get("resource") != vm
                or last.get("method") != "POST" or last.get("phase") != "refused"
                or not KUBE.definite_refusal(last.get("http_status"))):
            return None
        steps, kept = [], []
        for entry in writes[:-1]:
            target = entry.get("resource") or {}
            uid = (entry.get("identity") or {}).get("uid")
            if (entry.get("phase") == "accepted" and entry.get("method") == "POST" and target.get("kind") in REMOVABLE
                    and target.get("namespace") == namespace and uid):
                steps.append({"sequence": entry["sequence"], "resource": target, "uid": uid, "phase": "intent"})
            else:
                kept.append(target)
        ref["cleanup"] = {"state": "removing", "steps": steps, "kept": kept, "http_status": last.get("http_status")}
        t.save(items)
    return _run_cleanup(ident, ops, send)


def _run_cleanup(ident, ops, send):
    """Each delete still an intent is sent and its outcome written down, then
    the job says what went. The name is free again (retain_resources off)
    only when nothing the attempt made is left."""
    import homestead_kube as KUBE
    item = next((row for row in ops.stored() if isinstance(row, dict) and row.get("id") == ident), None)
    cleanup = ((item or {}).get("ref") or {}).get("cleanup") or {}
    # An admin who resolved the job meanwhile (homestead_vm_mutation_recovery,
    # phase resolved-unknown) has the last word: the clean-up stops there.
    if cleanup.get("state") != "removing" or item["ref"].get("phase") != "failed":
        return None
    namespace = item["ref"]["namespace"]
    for step in cleanup["steps"]:
        if step["phase"] != "intent":
            continue
        target = step["resource"]
        try:
            # 404: gone already (a restart after the delete was sent).
            KUBE.delete_if_present(f"/api/v1/namespaces/{namespace}/{REMOVABLE[target['kind']]}/{target['name']}",
                                   {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": step["uid"]}},
                                   send=send)
            outcome = {"phase": "removed"}
        except Exception as error:
            # A 409 is one made again under the name since; a 5xx or no
            # answer is not known to be gone. Either way it is kept.
            code = getattr(error, "code", None)
            if hasattr(error, "close"):
                error.close()
            outcome = {"phase": "kept", **({"http_status": int(code)} if isinstance(code, int) else {})}
        with ops.transaction() as t:
            items = t.items
            row = next(r for r in items if isinstance(r, dict) and r.get("id") == ident)
            if row["ref"].get("phase") != "failed":
                return None
            for saved in row["ref"]["cleanup"]["steps"]:
                if saved["sequence"] == step["sequence"]:
                    saved.update(outcome)
            t.save(items)
    with ops.transaction() as t:
        items = t.items
        row = next(r for r in items if isinstance(r, dict) and r.get("id") == ident)
        if row["ref"].get("phase") != "failed":
            return None
        cleanup = row["ref"]["cleanup"]
        gone = {step["sequence"] for step in cleanup["steps"] if step["phase"] == "removed"}
        left = [step["resource"] for step in cleanup["steps"] if step["phase"] != "removed"] + cleanup["kept"]
        for entry in row["ref"].get("writes", []):
            if entry.get("sequence") in gone:
                entry["removed"] = True
        code = cleanup.get("http_status")
        if left:
            names = ", ".join(f"{t.get('kind')} {t.get('name')}" for t in left)
            message = (f"The cluster refused the VM (HTTP {code}). What it created was removed except {names}. "
                       "Check the kept write records. Nothing was retried.")
        else:
            message = (f"The cluster refused the VM (HTTP {code}). The volume claims, Secrets and ConfigMaps this attempt "
                       "created were removed. Nothing was retried.")
        cleanup["state"] = "done"
        row["ref"].update(phase="failed", retain_resources=bool(left))
        ops._finish(row, "failed", row.get("progress", 0), message)
        t.save(items)
    return {"all": not left}


def resume_cleanups(ops, send):
    """The leader's round: a refused create's clean-up that a restart cut
    off is finished, under the job's own lock (never beside a dispatcher
    still running it)."""
    for item in ops.stored():
        if not isinstance(item, dict):
            continue
        cleanup = (item.get("ref") or {}).get("cleanup") or {}
        if item.get("kind") != "vm-create" or cleanup.get("state") != "removing":
            continue
        try:
            with POWER.worker_lock(item["id"], ops, timeout=1):
                _run_cleanup(item["id"], ops, send)
        except Exception as error:
            print(f"refused VM create {item['id']}: clean-up not finished: {str(error)[:160]}", flush=True)


def status(item):
    return "running", item.get("progress", 0), (
        "The VM change is in progress or was interrupted. If it has stopped, choose Check outcome. No request is sent again automatically.")


def cancel_plan(item):
    return {"mode": "forget", "can": False, "why_not": "Check the outcome of the VM save instead. Incomplete writes cannot safely be cancelled or ignored.",
            "keeps": ["All VM resources and write receipts remain unchanged"], "needs": "admin"}


def cancel_run(item, options):
    raise ValueError("VM configuration jobs require outcome inspection, not cancellation")


def register(ops):
    """The job store's entries for creating and editing a VM (#622)."""
    for kind in ("vm-create", "vm-edit"):
        ops.RESOLVERS[kind] = status
        ops.CANCELLERS[kind] = (cancel_plan, cancel_run)
