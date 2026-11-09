"""Durable, one-shot VM power dispatch; monitoring never sends a power request."""
import hashlib
import re
import time
import urllib.error
from urllib.parse import quote
import homestead_shared as SHARED
import homestead_kube as KUBE

API = "/apis/kubevirt.io/v1"


def worker_lock(ident, ops, timeout=10):
    if not re.fullmatch(r"[0-9a-f]{24}", str(ident or "")):
        raise ValueError("Invalid power job identifier")
    return SHARED.SharedLock("vm-power-" + ident, strict=True, directory=lambda: ops.DATA_DIR, timeout=timeout)


def dispatch(body, context, ops, send, before_send):
    action = body["action"]
    if action not in ("start", "restart", "reset", "unpause"):
        raise ValueError("Only reviewed start, restart, reset and resume use this dispatch journal")
    observations = context["observations"]
    vm, vmi = observations["vm"], observations.get("vmi") or {}
    ref = {"namespace": vm["namespace"], "name": vm["name"], "uid": vm["uid"],
           "state_initialization_acknowledged": body.get("ack_state_initialization") is True and body.get("confirm_state_name") == vm["name"],
           "version": vm["resourceVersion"], "previous_vmi_uid": vmi.get("uid", ""), "action": action,
           "device_requests": dict(observations.get("device_requests") or {}),
           "device_nodes": list(observations.get("device_nodes") or []),
           "review_digest": hashlib.sha256(body["capacity_token"].encode()).hexdigest(),
           "review_expires": int(body["capacity_token"].split(".", 1)[0]),
           "phase": "prepared", "phase_at": time.time(), "retain_resources": True, "dispatch_protocol": 1}
    job = ops.start("vm-power", f"{action.capitalize()} VM {vm['name']}",
                    {"kind": "VirtualMachine", "namespace": vm["namespace"], "name": vm["name"]},
                    "/vms", ref, "Power request recorded. Checking again before sending it.")
    ident = job["id"]
    with worker_lock(ident, ops):
        return _dispatch_owned(ident, vm["name"], ops, send, before_send)


def _dispatch_owned(ident, name, ops, send, before_send):
    try:
        before_send()
    except Exception:
        ops.record_phase(ident, "failed", 0, "The final check failed, so no power request was sent. Review it again.", retain_resources=False)
        raise
    ops.record_phase(ident, "dispatching", 10, "Sending exactly one power request. Do not repeat it.")
    try:
        result = send()
    except Exception as error:
        # Only explicit client refusals are known non-acceptance. A connection
        # loss/server error can happen after the API accepted the operation.
        refused = isinstance(error, urllib.error.HTTPError) and error.code in (400, 401, 403, 404, 405, 409, 422)
        phase = "failed" if refused else "uncertain"
        message = (f"KubeVirt refused the request (HTTP {error.code}). Nothing was retried." if refused else
                   "The power response was lost or uncertain. Check the VM and this job. Nothing was retried.")
        try:
            ops.record_phase(ident, phase, 10, message, retain_resources=not refused)
        except Exception:
            # The durable dispatch intent remains authoritative if outcome
            # journaling fails. Never retry either the write or the mutation.
            message = "The power outcome could not be recorded. Check the kept job. Nothing was retried."
        raise ValueError(f"{message} Job {ident}.") from error
    try:
        operation = ops.record_phase(ident, "accepted", 25,
            "KubeVirt accepted the request. Waiting for the VM instance to become ready.")
    except Exception as error:
        raise ValueError(f"KubeVirt accepted the power request, but its record could not be saved. Check job {ident}, and do not repeat the request.") from error
    return {**result, "detail": f"Power request accepted for {name}. Follow its job to see when it is ready.", "operation": operation}


def status(item, read):
    ref = item["ref"]
    phase = ref.get("phase")
    if phase in ("prepared", "dispatching", "uncertain"):
        age = time.time() - float(ref.get("phase_at") or 0)
        text = ("The power request is being sent. It is never sent again automatically." if age < 60 and phase != "uncertain" else
                "The outcome of the request needs checking. Homestead may have stopped before recording the response. No request is sent again.")
        return "running", 10 if phase != "prepared" else 0, text
    if phase != "accepted":
        return "failed", 0, "The power record has an unknown step. Check the recovery records before doing anything."
    ns, name = quote(ref["namespace"], safe=""), quote(ref["name"], safe="")
    try:
        vm = read(f"{API}/namespaces/{ns}/virtualmachines/{name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return "running", 25, "The VM is missing. Check it before stopping tracking or creating a replacement."
    meta = vm.get("metadata") or {}
    if (meta.get("namespace"), meta.get("name"), meta.get("uid")) != (ref["namespace"], ref["name"], ref["uid"]) or meta.get("deletionTimestamp"):
        return "running", 25, "The VM changed or is being deleted. This job does not follow a replacement VM."
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
        return "running", 35, "The instance changed, so this job cannot confirm that the reviewed guest resumed"
    if ref["action"] in ("start", "restart", "reset") and imeta["uid"] == previous:
        return "running", 40, "Waiting for the old VM instance to be replaced"
    conditions = state.get("conditions") or []
    paused = any(row.get("type") == "Paused" and row.get("status") == "True" for row in conditions)
    ready = any(row.get("type") == "Ready" and row.get("status") == "True" for row in conditions)
    if state.get("phase") == "Running" and ready and not paused:
        ref.update(retain_resources=False, observed_vmi_uid=imeta["uid"])
        return "succeeded", 100, "The VM instance is running and ready. The health of the guest's apps is not checked."
    return "running", 60, f"The VM instance is {str(state.get('phase') or 'pending')[:40]}. Waiting for it to be running, ready and not paused."


def cancel_plan(item):
    prepared = item["ref"].get("phase") == "prepared"
    return {"mode": "stop" if prepared else "forget", "can": prepared or item["ref"].get("phase") == "accepted",
            "why_not": "An in-flight or uncertain power request cannot safely be cancelled or forgotten",
            "keeps": ["Cancel before sending power. The dispatcher is fenced by this job's state." if prepared else
                      "The accepted power request still runs in KubeVirt. Stopping tracking does not undo it.",
                      "Its approval fingerprint is retained permanently, even when displayed history is cleared, so it cannot send another request."],
            "confirm": item["ref"]["name"], "needs": "operator"}


def cancel_run(item, options):
    if item["ref"].get("phase") not in ("prepared", "accepted"):
        raise ValueError("Cannot forget an uncertain power request")
    item["ref"]["retain_resources"] = False
    if item["ref"]["phase"] == "prepared":
        return "Cancelled before the power request was sent. The approval stays used."
    return "Stopped tracking the accepted request. KubeVirt is not changed, and the approval stays used."


def register(ops):
    """The job store's entries for a VM power request (#622). The client is
    looked up when asked, so a test that patches homestead_kube is the one used."""
    ops.RESOLVERS["vm-power"] = lambda item: status(item, KUBE.kget)
    ops.CANCELLERS["vm-power"] = (cancel_plan, cancel_run)
