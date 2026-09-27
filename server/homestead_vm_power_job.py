"""Durable, one-shot VM power dispatch; monitoring never sends a power request."""
import hashlib
import re
import time
import urllib.error
from urllib.parse import quote
import homestead_shared as SHARED

API = "/apis/kubevirt.io/v1"


def worker_lock(ident, ops, timeout=10):
    if not re.fullmatch(r"[0-9a-f]{24}", str(ident or "")):
        raise ValueError("Invalid power job identifier")
    return SHARED.SharedLock("vm-power-" + ident, strict=True, directory=lambda: ops.DATA_DIR, timeout=timeout)


def dispatch(body, context, ops, send, before_send):
    action = body["action"]
    if action not in ("start", "restart", "unpause"):
        raise ValueError("Only reviewed start, restart and resume use this dispatch journal")
    observations = context["observations"]
    vm, vmi = observations["vm"], observations.get("vmi") or {}
    ref = {"namespace": vm["namespace"], "name": vm["name"], "uid": vm["uid"],
           "version": vm["resourceVersion"], "previous_vmi_uid": vmi.get("uid", ""), "action": action,
           "review_digest": hashlib.sha256(body["capacity_token"].encode()).hexdigest(),
           "review_expires": int(body["capacity_token"].split(".", 1)[0]),
           "phase": "prepared", "phase_at": time.time(), "retain_resources": True, "dispatch_protocol": 1}
    job = ops.start("vm-power", f"{action.capitalize()} VM {vm['name']}",
                    {"kind": "VirtualMachine", "namespace": vm["namespace"], "name": vm["name"]},
                    "/vms", ref, "Power intent recorded; rechecking before dispatch")
    ident = job["id"]
    with worker_lock(ident, ops):
        return _dispatch_owned(ident, vm["name"], ops, send, before_send)


def _dispatch_owned(ident, name, ops, send, before_send):
    try:
        before_send()
    except Exception:
        ops.record_phase(ident, "failed", 0, "Final admission failed; no power request was sent. Review again.", retain_resources=False)
        raise
    ops.record_phase(ident, "dispatching", 10, "Sending exactly one power request; do not repeat it")
    try:
        result = send()
    except Exception as error:
        # Only explicit client refusals are known non-acceptance. A connection
        # loss/server error can happen after the API accepted the operation.
        refused = isinstance(error, urllib.error.HTTPError) and error.code in (400, 401, 403, 404, 405, 409, 422)
        phase = "failed" if refused else "uncertain"
        message = (f"KubeVirt refused the request (HTTP {error.code}); nothing was retried." if refused else
                   "The power response was lost or uncertain. Inspect the VM and this job; nothing was retried.")
        try:
            ops.record_phase(ident, phase, 10, message, retain_resources=not refused)
        except Exception:
            # The durable dispatch intent remains authoritative if outcome
            # journaling fails. Never retry either the write or the mutation.
            message = "Power outcome could not be recorded. Inspect the retained dispatch job; nothing was retried."
        raise ValueError(f"{message} Job {ident}.") from error
    try:
        operation = ops.record_phase(ident, "accepted", 25,
            "KubeVirt accepted the request; waiting for the expected VM instance to become ready")
    except Exception as error:
        raise ValueError(f"KubeVirt accepted power but its receipt could not be saved. Inspect job {ident}; do not repeat the request.") from error
    return {**result, "detail": f"Power request accepted for {name}; follow its job for readiness", "operation": operation}


def status(item, read):
    ref = item["ref"]
    phase = ref.get("phase")
    if phase in ("prepared", "dispatching", "uncertain"):
        age = time.time() - float(ref.get("phase_at") or 0)
        text = ("Power dispatch is in progress; it is never automatically resent" if age < 60 and phase != "uncertain" else
                "Dispatch outcome needs inspection. Homestead may have stopped before recording the response; no request will be resent.")
        return "running", 10 if phase != "prepared" else 0, text
    if phase != "accepted":
        return "failed", 0, "Power journal phase is unrecognized; inspect recovery records before taking action"
    ns, name = quote(ref["namespace"], safe=""), quote(ref["name"], safe="")
    try:
        vm = read(f"{API}/namespaces/{ns}/virtualmachines/{name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return "running", 25, "The target VM is missing; inspect it before stopping tracking or creating a replacement"
    meta = vm.get("metadata") or {}
    if (meta.get("namespace"), meta.get("name"), meta.get("uid")) != (ref["namespace"], ref["name"], ref["uid"]) or meta.get("deletionTimestamp"):
        return "running", 25, "The VM identity changed or is deleting; this job does not follow a replacement VM"
    try:
        vmi = read(f"{API}/namespaces/{ns}/virtualmachineinstances/{name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return "running", 35, "Waiting for KubeVirt to create the expected VM instance"
    imeta, state = vmi.get("metadata") or {}, vmi.get("status") or {}
    owners = imeta.get("ownerReferences") or []
    owned = (imeta.get("namespace"), imeta.get("name")) == (ref["namespace"], ref["name"]) and any(
        owner.get("controller") is True and owner.get("kind") == "VirtualMachine" and
        owner.get("apiVersion", "").split("/")[0] == "kubevirt.io" and
        (owner.get("uid"), owner.get("name")) == (ref["uid"], ref["name"]) for owner in owners)
    if not owned or not imeta.get("uid") or imeta.get("deletionTimestamp"):
        return "running", 35, "Waiting for a non-deleting instance owned by the reviewed VM"
    previous = ref.get("previous_vmi_uid")
    if ref["action"] == "unpause" and imeta["uid"] != previous:
        return "running", 35, "The instance changed; this job cannot confirm resuming the reviewed guest"
    if ref["action"] in ("start", "restart") and imeta["uid"] == previous:
        return "running", 40, "Waiting for the old VM instance to be replaced"
    conditions = state.get("conditions") or []
    paused = any(row.get("type") == "Paused" and row.get("status") == "True" for row in conditions)
    ready = any(row.get("type") == "Ready" and row.get("status") == "True" for row in conditions)
    if state.get("phase") == "Running" and ready and not paused:
        ref.update(retain_resources=False, observed_vmi_uid=imeta["uid"])
        return "succeeded", 100, "Expected VM instance is running and ready; guest application health is not verified"
    return "running", 60, f"VM instance is {str(state.get('phase') or 'pending')[:40]}; waiting for Running, Ready and not paused"


def cancel_plan(item):
    prepared = item["ref"].get("phase") == "prepared"
    return {"mode": "stop" if prepared else "forget", "can": prepared or item["ref"].get("phase") == "accepted",
            "why_not": "An in-flight or uncertain power request cannot safely be cancelled or forgotten",
            "keeps": ["Cancel before sending power. The dispatcher is fenced by this job's state." if prepared else
                      "The accepted power request still runs in KubeVirt. Stopping tracking does not undo it.",
                      "Its approval receipt is retained until expiry, so that approval cannot send another request."],
            "confirm": item["ref"]["name"], "needs": "operator"}


def cancel_run(item, options):
    if item["ref"].get("phase") not in ("prepared", "accepted"):
        raise ValueError("Cannot forget an uncertain power request")
    item["ref"]["retain_resources"] = False
    if item["ref"]["phase"] == "prepared":
        return "Cancelled before power dispatch; the approval remains consumed"
    return "Stopped tracking the accepted request; KubeVirt is unchanged and the approval remains consumed"
