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
           "review_digest": hashlib.sha256(token.encode()).hexdigest(),
           "review_expires": int(token.split(".", 1)[0]), "dispatch_protocol": 1,
           "phase": "prepared", "phase_at": time.time(), "retain_resources": True, "writes": []}
    job = ops.start(kind, f"{'Create' if kind == 'vm-create' else 'Save'} VM {name}",
                    {"kind": "VirtualMachine", "namespace": namespace, "name": name}, "/vms", ref,
                    "Configuration intent recorded; no resource request has been sent yet")
    ident = job["id"]
    with POWER.worker_lock(ident, ops):
        # Recovery can win the lock between publishing intent and dispatching.
        # This state check fences that case before even image preparation.
        ops.record_phase(ident, "writing", 0, "Preparing reviewed VM configuration; writes are recorded individually")
        writer = WRITE.ResourceWriter(send, WRITE.operation_recorder(ops, ident))
        try:
            result = commit(writer)
            writer.check()  # a low-level warning must not hide a failed write
            if not writer.count:
                raise ValueError("VM commit did not produce a verified resource receipt")
            with ops._lock:
                items = ops._read()
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
                            "Configuration writes acknowledged by Kubernetes; guest readiness and application health are not verified")
                ops._write(items)
                return {**result, "operation": ops._public(item)}
        except Exception:
            # Preserve partial/uncertain receipts, never delete resources or
            # retry a mutation. Raw errors may contain Secret/config content.
            try:
                with ops._lock:
                    items = ops._read()
                    item = next(row for row in items if row["id"] == ident)
                    if item["status"] not in ops.TERMINAL:
                        item["ref"].update(phase="failed", retain_resources=bool(writer.count))
                        ops._finish(item, "failed", item.get("progress", 0),
                                    "VM save stopped; inspect retained write receipts. Nothing was retried." if writer.count else
                                    "VM save stopped before any resource write; obtain a fresh review.")
                        ops._write(items)
            except Exception:
                pass  # existing durable intent remains the recovery authority
            if not writer.count:
                raise  # preserve structured admission failures before writes
            raise ValueError(f"VM save failed or its outcome is uncertain. Inspect job {ident}; resources were retained and nothing was retried.") from None


def status(item):
    return "running", item.get("progress", 0), (
        "VM configuration dispatch is in progress or was interrupted. Inspect outcome if it has stopped; no resource request is automatically resent.")


def cancel_plan(item):
    return {"mode": "forget", "can": False, "why_not": "Inspect the VM save outcome instead; partial writes cannot safely be cancelled or forgotten",
            "keeps": ["All VM resources and write receipts remain unchanged"], "needs": "admin"}


def cancel_run(item, options):
    raise ValueError("VM configuration jobs require outcome inspection, not cancellation")
