"""Durable, one-shot VM power dispatch; monitoring never sends a power request."""
import calendar
import hashlib
import re
import time
import urllib.error
from urllib.parse import quote
import homestead_shared as SHARED
import homestead_kube as KUBE

API = "/apis/kubevirt.io/v1"
# virt-handler's event when QEMU exits under a running instance.
CRASHED = "VirtualMachineInstance crashed"
FULL_TEXT = 8000   # KubeVirt's own words kept for the job's log


def worker_lock(ident, ops, timeout=10):
    if not re.fullmatch(r"[0-9a-f]{24}", str(ident or "")):
        raise ValueError("Invalid power job identifier")
    return SHARED.SharedLock("vm-power-" + ident, strict=True, directory=lambda: ops.DATA_DIR, timeout=timeout)


def dispatch(body, context, ops, send, before_send, run_strategy=""):
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
           "phase": "prepared", "phase_at": time.time(), "retain_resources": True, "dispatch_protocol": 1,
           # As the job found it, so that a start that crashes can put it back.
           "run_strategy": str(run_strategy or "")[:40]}
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


def status(item, read, send=None):
    """Where the power request has got to. A start, restart or reset whose
    instance crashes stops here, at once, with KubeVirt's reason: otherwise the
    job would wait for ever while KubeVirt recreates the instance (November
    2026: a kernel's AppArmor change made QEMU exit at every start)."""
    result = _status(item, read)
    ref = item["ref"]
    if result[0] != "running" or ref.get("phase") != "accepted" or ref.get("action") not in ("start", "restart", "reset"):
        return result
    try:
        vm = read(f"{API}/namespaces/{quote(ref['namespace'], safe='')}/virtualmachines/{quote(ref['name'], safe='')}")
    except urllib.error.HTTPError:
        return result
    meta = vm.get("metadata") or {}
    if (meta.get("uid"), meta.get("name")) != (ref["uid"], ref["name"]):
        return result
    rows = _crash(ref, vm, read)
    if rows is None:
        return result
    return _fail_fast(ref, vm, rows, read, send)


def _when(stamp):
    try:
        return calendar.timegm(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def _crash(ref, vm, read):
    """KubeVirt's events for this VM since the request was accepted, if its
    instance crashed in that time (a crash event, or KubeVirt's backoff
    between restarts), else None."""
    since = float(ref.get("phase_at") or 0) - 5   # the API's clock, in whole seconds
    selector = quote(f"involvedObject.name={ref['name']}", safe="")
    try:
        rows = read(f"/api/v1/namespaces/{quote(ref['namespace'], safe='')}/events?fieldSelector={selector}").get("items") or []
    except Exception:
        rows = []   # unreadable events: the VM's own status still decides
    rows = [row for row in rows
            if (row.get("involvedObject") or {}).get("kind") in ("VirtualMachineInstance", "VirtualMachine")
            and _when(row.get("lastTimestamp") or row.get("eventTime")) >= since]
    rows.sort(key=lambda row: row.get("lastTimestamp") or row.get("eventTime") or "")
    looping = (vm.get("status") or {}).get("printableStatus") == "CrashLoopBackOff"
    if not looping and not any(CRASHED in str(row.get("message") or "") for row in rows):
        return None
    return rows


def _cause(text, kernel):
    """A cause for a failure Homestead knows, or ""."""
    if "Permission denied" in text and ("kick_thread" in text or "signal" in text.lower()):
        on = f" The host runs kernel {kernel}." if kernel else ""
        return ("QEMU was refused permission to signal its own threads. This is how a kernel's AppArmor change "
                f"shows itself (Ubuntu's 7.0.0-43, November 2026).{on} Setting kernel.apparmor_restrict_unprivileged_unconfined "
                "to 0 fixes it, and Homestead does so on hosts that run VMs. Check node health shows it.")
    return ""


def _kernel(rows, read):
    """The kernel of the host KubeVirt reported the failure from, or ""."""
    hosts = [str((row.get("source") or {}).get("host") or "") for row in rows]
    host = next((name for name in reversed(hosts) if name), "")
    if not host:
        return ""
    try:
        node = read(f"/api/v1/nodes/{quote(host, safe='')}") or {}
    except Exception:
        return ""
    return str(((node.get("status") or {}).get("nodeInfo") or {}).get("kernelVersion") or "")[:80]


def _put_back(ref, vm, send):
    """Return a start's run strategy to the one the job found, once, fenced
    on the VM's version so that a person's own change since is never
    overwritten. A restart or reset found the VM running, so there is nothing
    to put back."""
    stay = "KubeVirt keeps trying to start it. Stop the VM to end the retries."
    if ref.get("action") != "start" or ref.get("run_strategy") != "Halted":
        return stay
    spec, meta = vm.get("spec") or {}, vm.get("metadata") or {}
    if "runStrategy" in spec:
        path, now, before = "/spec/runStrategy", spec["runStrategy"], "Halted"
    else:
        path, now, before = "/spec/running", bool(spec.get("running")), False
    if now == before:
        return "The VM is set to stay off."
    if send is None:
        return stay
    try:
        send("PATCH", f"{API}/namespaces/{quote(ref['namespace'], safe='')}/virtualmachines/{quote(ref['name'], safe='')}", [
            {"op": "test", "path": "/metadata/uid", "value": ref["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": meta.get("resourceVersion")},
            {"op": "test", "path": path, "value": now},
            {"op": "replace", "path": path, "value": before}], ctype="application/json-patch+json")
    except Exception as error:
        code = f" (HTTP {error.code})" if isinstance(error, urllib.error.HTTPError) else ""
        return f"Homestead could not set it back to stay off{code}. Stop the VM to end the retries."
    return "It is set back to stay off, as it was before the start."


def _fail_fast(ref, vm, rows, read, send):
    failures = []
    for row in rows:
        words = " ".join(str(row.get("message") or "").split())
        if row.get("reason") == "SyncFailed" and words and words not in failures:
            failures.append(words)
    text = "\n".join(failures)[-FULL_TEXT:]
    if "crash" not in ref:   # once: a later read of the ended job changes nothing
        ref["crash"] = {"text": text, "cause": _cause(text, _kernel(rows, read) if text else ""),
                        "put_back": _put_back(ref, vm, send)}
    ref["retain_resources"] = False
    first = (text.split(". ")[0][:160].rstrip(".") + ".") if text else ""
    reason = ref["crash"]["cause"] or (f"KubeVirt says: {first}" if first else "KubeVirt gave no reason.")
    more = " Open the log for KubeVirt's full message." if text else ""
    return "failed", 60, f"The VM crashed while starting, so this job stopped. {reason} {ref['crash']['put_back']}{more}"


def log_sources(item):
    """Why a start stopped: KubeVirt's full messages, as the job kept them."""
    crash = (item.get("ref") or {}).get("crash")
    if not crash:
        return []
    return [{"title": "Why the VM stopped", "text": crash.get("text") or "",
             "note": "" if crash.get("text") else "KubeVirt recorded no failure message. The VM's events may say more."}]


def _status(item, read):
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
    ops.RESOLVERS["vm-power"] = lambda item: status(item, KUBE.kget, KUBE.ksend)
    ops.CANCELLERS["vm-power"] = (cancel_plan, cancel_run)
    ops.LOGGERS["vm-power"] = log_sources
