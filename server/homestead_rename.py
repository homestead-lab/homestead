"""Reviewed Deployment renames: one-shot writes, fresh admission, no blind undo."""
import copy
import hashlib
import time
import urllib.error

import homestead_capacity_review as REVIEW
import homestead_import_guard as IMPORT
import homestead_rollout_capacity as ROLLOUT
import homestead_vm_power_job as LOCKS

KIND = "workload-rename"


class Stopped(ValueError):
    """A safe, operator-facing reason (never an API response or pod payload)."""


def identity(obj):
    meta = obj.get("metadata") or {}
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ("name", "namespace", "uid", "resourceVersion")) or meta.get("deletionTimestamp"):
        raise Stopped("Workload identity is unavailable or being removed; refresh before renaming")
    return {key: meta[key] for key in ("name", "namespace", "uid", "resourceVersion")}


def base(namespace):
    return f"/apis/apps/v1/namespaces/{namespace}/deployments"


def optional(read, path):
    try:
        return read(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return None


def guards(current, target, read):
    meta = identity(current)
    blocker = IMPORT.pending(current, meta["namespace"], read)
    if blocker:
        raise Stopped(blocker)
    if current["metadata"].get("ownerReferences") or current["metadata"].get("annotations", {}).get("meta.helm.sh/release-name"):
        raise Stopped("Rename this managed workload through its owning controller or Helm release")
    if current.get("spec", {}).get("paused"):
        raise Stopped("Resume the paused Deployment before renaming it")
    path = f"/apis/autoscaling/v2/namespaces/{meta['namespace']}/horizontalpodautoscalers"
    listing = optional(read, path)
    if listing is None:
        listing = read(path.replace("autoscaling/v2", "autoscaling/v1"))
    if listing is not None:
        if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
            raise Stopped("Autoscaler inventory is incomplete; rename cannot start")
        if any(row.get("spec", {}).get("scaleTargetRef", {}).get("kind") == "Deployment" and
               row["spec"]["scaleTargetRef"].get("name") in (meta["name"], target) for row in listing["items"]):
            raise Stopped("Remove autoscaling for these workload names before renaming; an autoscaler could restart either copy")


def prepare(body, current, clone, read):
    if set(body) - {"ns", "name", "workload_name", "capacity_token", "confirm_capacity"}:
        raise ValueError("Rename separately from other edits. Review only the old and new workload names")
    source = identity(current)
    guards(current, body["workload_name"], read)
    if optional(read, base(source["namespace"]) + "/" + body["workload_name"]) is not None:
        raise ValueError("The new workload name already exists; choose another name")
    count = current.get("spec", {}).get("replicas", 1)
    if type(count) is not int or not 0 <= count <= 100:
        raise ValueError("Workload replica count is unsupported")
    proposed = clone(current, source["namespace"], body["workload_name"])
    # Completion was verified against the original UID above. The completed
    # copy Job is historical evidence, not an interlock owned by the new UID.
    proposed.get("metadata", {}).get("annotations", {}).pop(IMPORT.JOB, None)
    proposed["spec"]["replicas"] = count
    return proposed, {"action": KIND, "source": source, "target": body["workload_name"]}


def dispatch(body, context, proposed, read, send, ops, admission, *, timeout=120, sleep=time.sleep):
    """Synchronous worker; durable milestones stay visible in the job tray."""
    ns, old, new = body["ns"], body["name"], body["workload_name"]
    if not REVIEW.valid(body, context) or body.get("confirm_capacity") is not True:
        raise ValueError("Review the rename before starting")
    ref = {"namespace": ns, "name": old, "new_name": new, "source": context["source"],
           "target": None, "phase": "prepared", "retain_resources": True, "writes": [],
           "review_digest": hashlib.sha256(body["capacity_token"].encode()).hexdigest(),
           "review_expires": int(body["capacity_token"].split(".", 1)[0])}
    job = ops.start(KIND, f"Rename {old} to {new}", {"kind": "Deployment", "namespace": ns, "name": old},
                    "/containers", ref, "Rename reviewed; preparing the replacement")
    ident, writes, progress = job["id"], [], 0
    source_path, target_path = base(ns) + "/" + old, base(ns) + "/" + new

    def phase(name, percent, message, **updates):
        nonlocal progress
        progress = percent
        return ops.record_phase(ident, name, percent, message, **updates)

    def mutation(method, path, payload, label, percent, expected=None):
        # Only identifiers, never pod specs, env values or API errors, are stored.
        entry = {"method": method, "name": label, "phase": "intent", "before": expected}
        writes.append(entry)
        phase("writing", percent, f"{method} {label}: request recorded", writes=copy.deepcopy(writes))
        try:
            result = send(method, path, payload, **({"ctype": "application/json-patch+json"} if method == "PATCH" else {}))
            if method != "DELETE":
                receipt = identity(result)
                if receipt["namespace"] != ns or receipt["name"] != label or expected and receipt["uid"] != expected["uid"]:
                    raise ValueError("Mismatched receipt")
                if result.get("kind") != "Deployment" or result.get("apiVersion") != "apps/v1":
                    raise ValueError("Mismatched resource type")
                entry["identity"] = receipt
            entry["phase"] = "accepted"
            phase("writing", percent, f"{method} {label}: acknowledged", writes=copy.deepcopy(writes))
            return result
        except Exception:
            entry["phase"] = "uncertain"
            # If storage itself failed, the preceding durable intent remains
            # authoritative. Never perform another cluster write to undo it.
            try:
                phase("uncertain", percent, f"{label}: response not confirmed. Nothing was retried.", writes=copy.deepcopy(writes))
            except Exception:
                pass
            raise Stopped(f"The {method} response for {label} could not be confirmed") from None

    def current(path, expected):
        obj = read(path)
        if identity(obj)["uid"] != expected["uid"]:
            raise Stopped("Workload was replaced; rename stopped")
        return obj

    def scale(obj, count, percent):
        meta = identity(obj)
        patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
                 {"op": "add", "path": "/spec/replicas", "value": count}]
        result = mutation("PATCH", base(ns) + "/" + meta["name"], patch, meta["name"], percent, meta)
        expected_spec = copy.deepcopy(obj["spec"])
        expected_spec["replicas"] = count
        if result.get("spec") != expected_spec:
            raise Stopped("Scaling changed more than the replica count; inspect both workloads before continuing")
        return result

    def stopped(expected):
        obj = current(source_path, expected)
        if obj.get("spec", {}).get("replicas") != 0 or obj["spec"] != stopped_spec:
            raise Stopped("The original workload changed or restarted; inspect both copies")
        pods = ROLLOUT.items(read, "/api/v1/pods")
        owned, known = ROLLOUT.owned_pods(obj, pods, read, ns)
        # Terminating pods count. Zero controller counters are not proof that
        # old writers have released their volumes and host ports.
        state = obj.get("status") or {}
        return obj, known and not owned and int(state.get("observedGeneration", -1)) >= int(obj["metadata"].get("generation", 0)) and not state.get("replicas", 0)

    with LOCKS.worker_lock(ident, ops):
        try:
            phase("checking", 5, "Checking the reviewed workload identity and autoscalers")
            source = read(source_path)
            if identity(source) != context["source"]:
                raise Stopped("Workload changed after review")
            guards(source, new, read)
            if optional(read, target_path) is not None:
                raise Stopped("The new workload name is no longer free")
            desired = proposed["spec"]["replicas"]
            draft = copy.deepcopy(proposed)
            draft["spec"]["replicas"] = 0
            target = mutation("POST", base(ns), draft, new, 10)
            target_id = identity(target)
            target_spec = copy.deepcopy(target["spec"])
            guards(target, old, read)
            target_pods, known = ROLLOUT.owned_pods(target, ROLLOUT.items(read, "/api/v1/pods"), read, ns)
            if target_spec.get("replicas") != 0 or not known or target_pods:
                raise Stopped("The replacement is not confirmed stopped; the original was left unchanged")
            phase("stopping", 25, f"Stopping {old}; volumes and service addresses are kept", target=target_id)
            guards(source, new, read)
            source = scale(source, 0, 30)
            stopped_spec = copy.deepcopy(source["spec"])
            deadline = time.monotonic() + timeout
            while True:
                source, done = stopped(identity(source))
                if done:
                    break
                if time.monotonic() >= deadline:
                    raise Stopped("Old pods did not finish stopping")
                sleep(2)
            phase("admission", 50, "Old pods have stopped; checking current placement before starting the replacement")
            guards(source, new, read)
            target = current(target_path, target_id)
            guards(target, old, read)
            if target["spec"].get("replicas") != 0 or target["spec"] != target_spec:
                raise Stopped("Replacement was changed outside this rename")
            # Use the actual API-admitted pod, including injected requests.
            admitted = copy.deepcopy(target)
            admitted["spec"]["replicas"] = desired
            check = admission(admitted)
            if not isinstance(check, dict) or check.get("blocked") is not False:
                raise Stopped("Replacement no longer fits; review retained workloads before starting either")
            # Explicit initial RAM-warning acceptance also applies to the fresh
            # pressure estimate; it never overrides a hard placement blocker.
            source, done = stopped(identity(source))
            if not done:
                raise Stopped("The original workload is not fully stopped")
            if desired:
                target = scale(target, desired, 65)
                target_spec = copy.deepcopy(target["spec"])
                deadline = time.monotonic() + timeout
                while True:
                    target = current(target_path, target_id)
                    source, done = stopped(identity(source))
                    if not done or target.get("spec") != target_spec or target.get("spec", {}).get("replicas") != desired:
                        raise Stopped("Workloads changed while waiting for readiness")
                    state = target.get("status") or {}
                    if (int(state.get("observedGeneration", -1)) >= int(target["metadata"].get("generation", 0)) and
                            all(state.get(key) == desired for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))):
                        break
                    if time.monotonic() >= deadline:
                        raise Stopped("Replacement did not become ready")
                    phase("readiness", 75, f"Waiting for {new} to become ready")
                    sleep(2)
            source, done = stopped(identity(source))
            if not done:
                raise Stopped("Original workload is not stopped")
            source_id = identity(source)
            guards(source, new, read)
            phase("retiring", 90, "Replacement is ready or intentionally stopped; removing only the original Deployment")
            # Orphan dependents: a PVC, ConfigMap or Service owned by this
            # Deployment must never be garbage-collected as part of a rename.
            # Stopped old ReplicaSets are retained too, not guessed disposable.
            mutation("DELETE", source_path, {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Orphan",
                     "preconditions": {key: source_id[key] for key in ("uid", "resourceVersion")}}, old, 90, source_id)
            # Deletion acknowledgement is not final disappearance.
            deadline = time.monotonic() + timeout
            while optional(read, source_path) is not None:
                if time.monotonic() >= deadline:
                    raise Stopped("Original Deployment removal is still pending")
                sleep(2)
            with ops._lock:
                items = ops._read()
                item = next(row for row in items if row["id"] == ident)
                item["ref"].update(phase="complete", retain_resources=False)
                ops._finish(item, "succeeded", 100, "Rename complete. Volumes and service addresses were kept.")
                ops._write(items)
                return {"ok": True, "renamed": True, "name": new, "renamed_from": old, "operation": ops._public(item)}
        except Exception as error:
            reason = str(error) if isinstance(error, Stopped) else "Cluster state or job storage could not be verified"
            try:
                phase("failed", progress, f"{reason}. Inspect both workload names. No automatic rollback or cleanup was performed.", retain_resources=bool(writes))
            except Exception:
                pass  # Keep the last durable intent; no further cluster writes.
            raise ValueError(f"{reason}. Inspect rename job {ident}; nothing was retried or automatically restored.") from None


def status(item, ops):
    try:
        with LOCKS.worker_lock(item["id"], ops, timeout=.01):
            return "running", item.get("progress", 0), "Rename dispatcher is no longer active. Inspect both workload names and choose Inspect outcome; requests are never replayed."
    except Exception:
        return "running", item.get("progress", 0), item.get("message") or "Inspect the rename if progress has stopped; requests are never replayed"


def recovery_plan(item, read, ops):
    plan = {"mode": "forget", "action": "Acknowledge and stop tracking", "needs": "admin", "confirm": item["ref"]["name"],
            "can": False, "why_not": "Rename is still running or its dispatcher cannot be checked", "keeps": [],
            "options": [{"id": "ack", "label": "I checked both workloads; an uncertain request may still take effect",
                         "detail": "This does not undo the rename, delete resources, or cancel Kubernetes requests.", "default": False}]}
    try:
        with LOCKS.worker_lock(item["id"], ops, timeout=.01):
            for name in (item["ref"]["name"], item["ref"]["new_name"]):
                obj = optional(read, base(item["ref"]["namespace"]) + "/" + name)
                if obj is None:
                    plan["keeps"].append(f"{name}: not present")
                else:
                    meta = identity(obj)
                    plan["keeps"].append(f"{name}: UID {meta['uid']}, desired replicas {obj['spec'].get('replicas', 1)}")
            plan.update(can=True, why_not="")
    except Exception:
        pass
    return plan


def recovery_run(item, options, read, ops):
    with LOCKS.worker_lock(item["id"], ops, timeout=.01):
        if options.get("ack") is not True:
            raise ValueError("Acknowledge the retained workloads and possible late effects first")
        # Cluster resources are deliberately unchanged. The permanent approval
        # ledger still prevents reusing the old request after tracking closes.
        item["ref"]["retain_resources"] = False
        return "Resources unchanged; outcome remains unverified. No request was cancelled, retried or rolled back."
