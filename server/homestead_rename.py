"""Reviewed Deployment renames: one-shot writes, fresh admission, no blind undo."""
import copy
import hashlib
import time
import urllib.error

import homestead_capacity_review as REVIEW
import homestead_import_guard as IMPORT
import homestead_kube as KUBE
import homestead_rollout_capacity as ROLLOUT
import homestead_vm_power_job as LOCKS

KIND = "workload-rename"


class Stopped(ValueError):
    """A safe, operator-facing reason (never an API response or pod payload)."""


class StopTimedOut(Stopped):
    """The original's pods outlasted the stop wait: the only failure that may put it back."""


class Refused(Stopped):
    """The API server refused a guarded write (409/422, a test op that did not hold): nothing was written."""


def identity(obj):
    meta = obj.get("metadata") or {}
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ("name", "namespace", "uid", "resourceVersion")) or meta.get("deletionTimestamp"):
        raise Stopped("Workload identity is unavailable or being removed; refresh before renaming")
    return {key: meta[key] for key in ("name", "namespace", "uid", "resourceVersion")}


def _count(n):
    return f"{n} replica" + ("" if n == 1 else "s")


def _before(item):
    """What the original ran before the rename, as recorded when its stop began."""
    ref = item.get("ref") or {}
    before = ref.get("before_rename")
    return f"{ref.get('name')} had {_count(before['replicas'])} before the rename" if before else ""


def base(namespace):
    return f"/apis/apps/v1/namespaces/{namespace}/deployments"


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
    listing = KUBE.get_optional(path, read=read)
    if listing is None:
        listing = read(path.replace("autoscaling/v2", "autoscaling/v1"))
    if listing is not None:
        if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
            raise Stopped("Autoscaler inventory is incomplete; rename cannot start")
        if any(row.get("spec", {}).get("scaleTargetRef", {}).get("kind") == "Deployment" and
               row["spec"]["scaleTargetRef"].get("name") in (meta["name"], target) for row in listing["items"]):
            raise Stopped("Remove autoscaling for these workload names before renaming; an autoscaler could restart either copy")


def prepare(body, current, clone, read):
    if set(body) - {"ns", "name", "workload_name", "capacity_token", "confirm_capacity", "confirm_managed"}:
        raise ValueError("Rename separately from other edits. Review only the old and new workload names")
    source = identity(current)
    guards(current, body["workload_name"], read)
    if KUBE.get_optional(base(source["namespace"]) + "/" + body["workload_name"], read=read) is not None:
        raise ValueError("The new workload name already exists; choose another name")
    count = current.get("spec", {}).get("replicas", 1)
    if type(count) is not int or not 0 <= count <= 100:
        raise ValueError("Workload replica count is unsupported")
    proposed = clone(current, source["namespace"], body["workload_name"])
    old_labels = current["spec"]["template"].get("metadata", {}).get("labels", {})
    new_labels = proposed["spec"]["template"].get("metadata", {}).get("labels", {})
    if any(new_labels.get(key) != value for key, value in old_labels.items()):
        raise ValueError("Rename would change existing pod labels, possibly breaking Services; review controller-generated labels first")
    # Completion was verified against the original UID above. The completed
    # copy Job is historical evidence, not an interlock owned by the new UID.
    proposed.get("metadata", {}).get("annotations", {}).pop(IMPORT.JOB, None)
    proposed["spec"]["replicas"] = count
    return proposed, {"action": KIND, "source": source, "target": body["workload_name"]}


def dispatch(body, context, proposed, read, send, ops, admission, *, timeout=120, settle=60, sleep=time.sleep):
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
    # The original's replica count while it is stopped and nothing has asked
    # the replacement to start. Only a stop wait that runs out puts it back,
    # and only after both workloads are checked again (put_back).
    restore, stopped_id, stopped_spec, target_id, target_spec = None, None, None, None, None
    source_path, target_path = base(ns) + "/" + old, base(ns) + "/" + new

    def phase(name, percent, message, **updates):
        nonlocal progress
        progress = percent
        return ops.record_phase(ident, name, percent, message, **updates)

    def mutation(method, path, payload, label, percent, expected=None, refusable=False):
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
        except Exception as error:
            if refusable and isinstance(error, urllib.error.HTTPError) and error.code in (409, 422):
                # A definite refusal: the test ops failed, so nothing was written.
                error.close()
                entry["phase"] = "refused"
                phase("writing", percent, f"{method} {label}: refused, nothing was written", writes=copy.deepcopy(writes))
                raise Refused(f"The {method} for {label} was refused") from None
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

    def scale(obj, count, percent, refusable=False):
        meta = identity(obj)
        patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
                 {"op": "add", "path": "/spec/replicas", "value": count}]
        result = mutation("PATCH", base(ns) + "/" + meta["name"], patch, meta["name"], percent, meta, refusable)
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

    def put_back(count):
        """Scale the original back to `count` only when its old pods have gone (after a
        short bounded wait), both workloads are still exactly as this rename left them,
        nothing could start the replacement, and the original fits.
        Returns (restored, outcome); any doubt writes nothing, for a person to decide."""
        refused = f"{old} was not put back to {_count(count)}"

        def original(changed="it changed outside this rename"):
            obj = read(source_path)
            if identity(obj)["uid"] != stopped_id["uid"] or obj.get("spec") != stopped_spec:
                raise Stopped(changed)
            pods, known = ROLLOUT.owned_pods(obj, ROLLOUT.items(read, "/api/v1/pods"), read, ns)
            return obj, known and not pods

        def last_looks(changed="it changed outside this rename"):
            # Right before the write: the replacement must be unstartable, and
            # new pods beside a terminating old one could be two writers on the
            # same volume. Returns the original as read last, for its write.
            twin = read(target_path)
            twin_pods, known = ROLLOUT.owned_pods(twin, ROLLOUT.items(read, "/api/v1/pods"), read, ns)
            if (identity(twin)["uid"] != target_id["uid"] or twin.get("spec") != target_spec or
                    twin["spec"].get("replicas") != 0 or not known or twin_pods):
                raise Stopped(f"{new} changed outside this rename")
            guards(twin, old, read)
            obj, gone = original(changed)
            if not gone:
                raise Stopped("its old pods are still stopping")
            return obj

        try:
            # The stop wait has just seen old pods: give them a little longer.
            polls = int(settle // 2)
            for attempt in range(polls + 1):
                obj, gone = original()
                if gone:
                    break
                if attempt == polls:
                    return None, f"{refused}: its old pods are still stopping"
                sleep(2)
            guards(obj, new, read)
            back = copy.deepcopy(obj)
            back["spec"]["replicas"] = count
            check = admission(back)
            if not isinstance(check, dict) or check.get("blocked") is not False:
                return None, f"{refused}: {old} no longer fits"
            obj = last_looks()
        except Stopped as error:
            return None, f"{refused}: {error}"
        except Exception:
            return None, f"{refused}: the workloads could not be checked"
        try:
            try:
                scale(obj, count, progress, refusable=True)
            except Refused:
                # Nothing was written. The Deployment controller rewrites the
                # original's status (terminatingReplicas) as its last old pod
                # goes, which moves resourceVersion between our read and the
                # write. Read again: when the spec is still the stopped one we
                # wrote, send it once more against the fresh resourceVersion.
                # A spec someone else changed is never written over.
                try:
                    obj = last_looks(f"{old} was edited by someone else while it was being put back, so nothing was written over it")
                except Stopped as error:
                    return None, f"{refused}: {error}"
                except Exception:
                    return None, f"{refused}: its write was refused and the workloads could not be checked again; nothing was written"
                scale(obj, count, progress, refusable=True)
        except Refused:
            return None, f"{refused}: the API server refused the write twice; nothing was written"
        except Exception:
            # The write is journalled by mutation(); it is never retried.
            if writes and writes[-1]["phase"] == "accepted":
                return None, f"{old} was scaled to {_count(count)}, but it came back with other changes: it may be running"
            return None, f"{old} could not be put back to {_count(count)}: the request was not confirmed and was not retried"
        return {"name": old, "replicas": count}, f"{old} was put back to {_count(count)}"

    def now_state():
        """What each workload is now, read again: never says "stopped" without seeing it."""
        parts, both = [], True
        for name, path, known_id in ((old, source_path, stopped_id), (new, target_path, target_id)):
            try:
                obj = KUBE.get_optional(path, read=read)
                if obj is None:
                    parts.append(f"{name} is not there")
                    both = False
                    continue
                if identity(obj)["uid"] != (known_id or {}).get("uid"):
                    parts.append(f"{name} was replaced by another Deployment")
                    both = False
                    continue
                count = obj.get("spec", {}).get("replicas", 1)
                pods, known = ROLLOUT.owned_pods(obj, ROLLOUT.items(read, "/api/v1/pods"), read, ns)
            except Exception:
                parts.append(f"could not read {name}: inspect before starting anything")
                both = False
                continue
            if not known:
                parts.append(f"could not read {name}'s pods: inspect before starting anything")
            elif count:
                parts.append(f"{name} is set to {_count(count)}")
            elif pods:
                parts.append(f"{name} is still stopping")
            else:
                parts.append(f"{name} is stopped")
                continue
            both = False
        return "both are stopped, so a person decides" if both else "; ".join(parts)

    with LOCKS.worker_lock(ident, ops):
        try:
            phase("checking", 5, "Checking the reviewed workload identity and autoscalers")
            source = read(source_path)
            if identity(source) != context["source"]:
                raise Stopped("Workload changed after review")
            guards(source, new, read)
            if KUBE.get_optional(target_path, read=read) is not None:
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
            before = source["spec"].get("replicas", 1)
            # Recorded before the stop, so a recovery after a crash still knows it.
            phase("stopping", 25, f"Stopping {old}; volumes and service addresses are kept", target=target_id,
                  before_rename={"replicas": before, "uid": identity(source)["uid"], "new_uid": target_id["uid"]})
            guards(source, new, read)
            source = scale(source, 0, 30)
            stopped_id, stopped_spec = identity(source), copy.deepcopy(source["spec"])
            restore = before or None
            deadline = time.monotonic() + timeout
            while True:
                source, done = stopped(identity(source))
                if done:
                    break
                if time.monotonic() >= deadline:
                    raise StopTimedOut("Old pods did not finish stopping")
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
                # From here the replacement may start: never start the original
                # again beside it.
                restore = None
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
            while KUBE.get_optional(source_path, read=read) is not None:
                if time.monotonic() >= deadline:
                    raise Stopped("Original Deployment removal is still pending")
                sleep(2)
            with ops.transaction() as t:
                items = t.items
                item = next(row for row in items if row["id"] == ident)
                item["ref"].update(phase="complete", retain_resources=False)
                ops._finish(item, "succeeded", 100, "Rename complete. Volumes and service addresses were kept.")
                t.save(items)
                return {"ok": True, "renamed": True, "name": new, "renamed_from": old, "operation": ops._public(item)}
        except Exception as error:
            reason = str(error) if isinstance(error, Stopped) else "Cluster state or job storage could not be verified"
            updates = {}
            if restore is None:
                message = f"{reason}. Inspect both workload names. No automatic rollback or cleanup was performed."
                raised = f"{reason}. Inspect rename job {ident}; nothing was retried or automatically restored."
            else:
                # The replacement was never asked to start. A stop that ran out
                # of time puts the original back when put_back finds it safe;
                # any other failure (a capacity block, a workload changed or
                # replaced) leaves both stopped for a person to decide.
                restored, outcome = put_back(restore) if isinstance(error, StopTimedOut) else (
                    None, f"{old} was not put back to {_count(restore)} automatically")
                if restored:
                    outcome += f"; {new} was kept, stopped"
                    updates["restored"] = restored
                else:
                    outcome += (f"; {now_state()}. To bring the app back: Containers → {old} → "
                                f"Start with {_count(restore)}, then delete {new}")
                message = f"{reason}. {outcome}. Inspect both workload names."
                raised = f"{reason}. {outcome}. Inspect rename job {ident}."
            try:
                phase("failed", progress, message, retain_resources=bool(writes), **updates)
            except Exception:
                pass  # Keep the last durable intent; no further cluster writes.
            raise ValueError(raised) from None


def status(item, ops):
    try:
        with LOCKS.worker_lock(item["id"], ops, timeout=.01):
            before = _before(item)
            return "running", item.get("progress", 0), "Rename dispatcher is no longer active. Inspect both workload names and choose Inspect outcome; requests are never replayed." + (f" {before}." if before else "")
    except Exception:
        return "running", item.get("progress", 0), item.get("message") or "Inspect the rename if progress has stopped; requests are never replayed"


def recovery_plan(item, read, ops):
    plan = {"mode": "forget", "action": "Acknowledge and stop tracking", "needs": "admin", "confirm": item["ref"]["name"],
            "can": False, "why_not": "Rename is still running or its dispatcher cannot be checked", "keeps": [],
            "options": [{"id": "ack", "label": "I checked both workloads; an uncertain request may still take effect",
                         "detail": "This does not undo the rename, delete resources, or cancel Kubernetes requests.", "default": False}]}
    if _before(item):
        plan["keeps"].append(_before(item))
    try:
        with LOCKS.worker_lock(item["id"], ops, timeout=.01):
            for name in (item["ref"]["name"], item["ref"]["new_name"]):
                obj = KUBE.get_optional(base(item["ref"]["namespace"]) + "/" + name, read=read)
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


def register(ops):
    """The job store's entries for renaming an app (#622)."""
    ops.RESOLVERS[KIND] = lambda item: status(item, ops)
    ops.CANCELLERS[KIND] = (lambda item: recovery_plan(item, KUBE.kget, ops),
                            lambda item, options: recovery_run(item, options, KUBE.kget, ops))
    ops.CLEANUPS.add(KIND)
