"""How a Homestead job stops an app and starts it again only when that is safe.

hold() records how the app runs - kind, namespace, name, uid, resourceVersion
and its replicas, suspend or runStrategy - and, asked to, keeps its pod
template in a Secret the app owns (never in an annotation: anyone who can see
the app sees those, and a template carries env values). It writes nothing to
the app: the caller saves the Record with the job first, then stop() stops it
in one write fenced on that uid and resourceVersion, marking it HELD_BY
<kind>:<job id> and HELD_AS how it ran. An app held by another job is refused;
one already stopped (0 replicas, suspended, Halted) is recorded so and never
written to, or started.

restart() reads the app fresh and starts it as it ran, with one fenced write
that also takes the marks away, only when it is the same object, still held
by this job and still stopped; no import or storage copy holds it (or, when
the caller passes a stricter guard, that guard does not refuse); no pod of
its own is left (terminating ones included, waited for a while); no claim it
is given is in use by another pod, and none is still attached; and, for a
Deployment or StatefulSet, it fits (the same admission a start from Homestead
gets). A Manual VM - only stopped when it was running - is set back to
Manual and started through KubeVirt, keeping its marks until that start is
asked for. What can pass - a read that failed, old pods, a volume still
attached, no room yet, a conflict, a reply that was lost - is Wait: the
next round reads again before deciding, so a start whose reply was lost is
seen as done, never sent twice. What can't is
KeptStopped, with how a person starts it (how_to). A mark with no ":" is the
format older jobs wrote (a bare job or run id) and counts as this job's when
it is this job's id.

Holding a CronJob suspends it; its runs still going are waited for, never
deleted.

pods_of() follows an app's pods through its controllers by uid, never by
name: a Deployment's through its ReplicaSets, a StatefulSet's directly, a
CronJob's through its Jobs, a VirtualMachine's through its instance (the
virt-launcher pod, owned by the instance or labelled kubevirt.io/created-by
with its uid). A pod counts until it has ended: a terminating pod still holds
its volumes and ports. When a list can't be read, the app has no uid, or a
live pod's controller isn't in the list (it may be one of the app's that is
being replaced or removed), whose pods are whose can't be told: that is
None, never an empty list."""
import base64
import json
import time
import urllib.error
import urllib.parse
from typing import NamedTuple

import homestead_import_guard as IMPORT
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_rollout_capacity as ROLLOUT

HELD_BY = NAMES.key("held-for-power")
HELD_AS = NAMES.key("held-as")
HELD_LABEL = NAMES.key("held")
JSON_PATCH = "application/json-patch+json"
POLL_SECONDS = 2

ENDED = ("Succeeded", "Failed")
CREATED_BY = "kubevirt.io/created-by"
# kind -> (the list of what sits between the app and its pods, that list's kind)
VIA = {"CronJob": ("/apis/batch/v1/namespaces/{ns}/jobs", "Job"),
       "VirtualMachine": ("/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances", "VirtualMachineInstance")}
KINDS = ("Deployment", "StatefulSet", *VIA)


def _meta(row):
    return row.get("metadata") or {}


def _live(pod):
    return (pod.get("status") or {}).get("phase") not in ENDED


def _controlled_by(row, kind, uids):
    owner = ROLLOUT.controller(row)
    return owner.get("kind") == kind and owner.get("uid") in uids


def pods_of(obj):
    """The live pods (terminating included) obj's controllers own, for a
    Deployment, StatefulSet, CronJob or VirtualMachine; None when that can't
    be told (see the top of this file)."""
    meta = _meta(obj)
    kind, ns, uid = obj.get("kind"), meta.get("namespace"), meta.get("uid")
    if kind not in KINDS or not ns or not uid:
        return None
    try:
        pods = KUBE.items(f"/api/v1/namespaces/{urllib.parse.quote(ns, safe='')}/pods", read=KUBE.kget)
        pods = [pod for pod in pods if _meta(pod).get("namespace") == ns and _live(pod)]
        if kind == "Deployment":
            owned, known = ROLLOUT.owned_pods(obj, pods, KUBE.kget, ns)
            return owned if known else None
        if kind == "StatefulSet":
            return [pod for pod in pods if _controlled_by(pod, kind, {uid})]
        path, middle = VIA[kind]
        rows = [row for row in KUBE.items(path.format(ns=urllib.parse.quote(ns, safe="")), read=KUBE.kget)
                if _meta(row).get("namespace") == ns]
        if any(ROLLOUT.controller(pod).get("kind") == middle and
               ROLLOUT.controller(pod).get("uid") not in {_meta(row).get("uid") for row in rows}
               for pod in pods):
            return None
        mine = {_meta(row)["uid"] for row in rows if _meta(row).get("uid") and _controlled_by(row, kind, {uid})}
        return [pod for pod in pods if _controlled_by(pod, middle, mine) or
                (kind == "VirtualMachine" and (_meta(pod).get("labels") or {}).get(CREATED_BY) in mine)]
    except (OSError, ValueError):
        return None


# kind -> the field that says whether it runs, and its value when stopped
FIELD = {"Deployment": "replicas", "StatefulSet": "replicas", "CronJob": "suspend", "VirtualMachine": "runStrategy"}
STOPPED = {"replicas": 0, "suspend": True, "runStrategy": "Halted"}
API_VERSION = {"Deployment": "apps/v1", "StatefulSet": "apps/v1", "CronJob": "batch/v1",
               "VirtualMachine": "kubevirt.io/v1"}
ATTACHMENTS = "/apis/storage.k8s.io/v1/volumeattachments"
SUB = "/apis/subresources.kubevirt.io/v1"
# A VM with this run strategy starts only when asked to (KubeVirt's start).
MANUAL = "Manual"


class Record(NamedTuple):
    kind: str
    namespace: str
    name: str
    uid: str
    resource_version: str
    # {"replicas": n} / {"suspend": b} / {"runStrategy": s}; a Manual VM that
    # was not running also has "running": False
    earlier: dict
    template_secret: str    # the Secret keeping its pod template, or ""


class Started(NamedTuple):
    replicas_or_state: str


class Wait(NamedTuple):
    reason: str


class KeptStopped(NamedTuple):
    reason: str
    how_to: str


class Refused(ValueError):
    """Why an app can't be held: a reason a person can read."""


def _q(text):
    return urllib.parse.quote(str(text), safe="")


def _path(kind, ns, name):
    return f"/apis/{API_VERSION[kind]}/namespaces/{_q(ns)}/{kind.lower()}s/{_q(name)}"


def _pointer(key):
    return "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")


def _why(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return str(error)[:80] or type(error).__name__


def _notes(obj):
    return _meta(obj).get("annotations") or {}


def _state(obj):
    """How it runs now, as the value of its FIELD."""
    spec = obj.get("spec") or {}
    field = FIELD[obj["kind"]]
    if field == "replicas":
        return 1 if spec.get("replicas") is None else int(spec["replicas"])
    if field == "suspend":
        return bool(spec.get("suspend"))
    return spec.get("runStrategy") or ("Always" if spec.get("running") else "Halted")


def _as_text(field, value):
    """How HELD_AS keeps it (only ever a count, true/false or a run strategy)."""
    return str(value).lower() if field == "suspend" else str(value)


def _from_text(field, text):
    text = str(text or "")
    if field == "replicas":
        return int(text) if text.isdigit() else None
    if field == "suspend":
        return {"true": True, "false": False}.get(text)
    # Older jobs kept a VM that had spec.running: true as "running".
    return "Always" if text == "running" else text or None


def _started_as(field, value):
    if field == "suspend":
        return "suspended" if value else "resumed"
    return str(value)


def _ours(mark, job_kind, job_id):
    return mark == f"{job_kind}:{job_id}" or (":" not in mark and mark == str(job_id))


def _was_stopped(record):
    """Stopped before the job: never written to, never started."""
    field = FIELD[record.kind]
    return record.earlier[field] == STOPPED[field] or record.earlier.get("running") is False


def _fence(uid, resource_version):
    return [{"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version}]


def _template(obj):
    spec = obj.get("spec") or {}
    if obj["kind"] == "CronJob":
        return ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
    return spec.get("template") or {}


def _read(record):
    """A fresh read of the app, or None once it no longer exists."""
    return KUBE.get_optional(_path(record.kind, record.namespace, record.name), read=KUBE.kget)


def _vm_running(ns, name):
    """Whether the VM has an instance that has not ended. Raises when that
    can't be read."""
    vmi = KUBE.get_optional(f"/apis/kubevirt.io/v1/namespaces/{_q(ns)}/virtualmachineinstances/{_q(name)}",
                            read=KUBE.kget)
    return vmi is not None and _live(vmi)


def _keep_template(obj):
    """Keep the pod template in a Secret the app owns, so it goes with the
    app; one kept already (a hold asked again) is the earlier one, and stays."""
    meta = _meta(obj)
    name = f"homestead-held-{meta['uid']}"
    path = f"/api/v1/namespaces/{_q(meta['namespace'])}/secrets"
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": meta["namespace"], "labels": {HELD_LABEL: "true"},
                         "ownerReferences": [{"apiVersion": API_VERSION[obj["kind"]], "kind": obj["kind"],
                                              "name": meta["name"], "uid": meta["uid"]}]},
            "data": {"template": base64.b64encode(json.dumps(_template(obj)).encode()).decode()}}
    try:
        KUBE.ksend("POST", path, body)
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
        kept = KUBE.kget(f"{path}/{_q(name)}")
        if not any(ref.get("uid") == meta["uid"] for ref in _meta(kept).get("ownerReferences") or []):
            raise Refused(f"a Secret named {name} is already there and is not {meta['name']}'s") from None
    return name


def hold(obj, job_kind, job_id, keep_template=False):
    """Record how obj (as read, with its kind set) runs, before it is
    stopped. Writes nothing to it: the caller saves the Record, then calls
    stop(). keep_template: also keep its pod template in an owned Secret.
    Raises Refused when it is held by another job or can't be held."""
    kind, meta = obj.get("kind"), _meta(obj)
    if kind not in FIELD:
        raise Refused(f"a {kind or 'object'} can't be held")
    if not all(meta.get(key) for key in ("name", "namespace", "uid", "resourceVersion")) or meta.get("deletionTimestamp"):
        raise Refused(f"{meta.get('name') or 'it'} is being removed, or isn't fully there")
    field, mark = FIELD[kind], _notes(obj).get(HELD_BY)
    if mark and not _ours(mark, job_kind, job_id):
        raise Refused(f"{meta['name']} is held by {mark}")
    earlier = {field: _from_text(field, _notes(obj).get(HELD_AS)) if mark else _state(obj)}
    if earlier[field] is None:
        raise Refused(f"{meta['name']} is held by this job, but how it ran before isn't recorded")
    if earlier[field] == MANUAL and not mark:
        # Only a Manual VM that is running is stopped, and started again.
        try:
            if not _vm_running(meta["namespace"], meta["name"]):
                earlier["running"] = False
        except Exception as error:
            raise Refused(f"Homestead could not tell whether {meta['name']} is running ({_why(error)})") from None
    secret = _keep_template(obj) if keep_template else ""
    return Record(kind, meta["namespace"], meta["name"], meta["uid"], meta["resourceVersion"], earlier, secret)


def _stopped_by(obj, record, job_kind, job_id):
    return (obj is not None and _meta(obj).get("uid") == record.uid and
            _ours(_notes(obj).get(HELD_BY) or "", job_kind, job_id) and _state(obj) == STOPPED[FIELD[record.kind]])


def stop(record, job_kind, job_id):
    """Stop the app held in record: one write, fenced on the uid and
    resourceVersion recorded, that marks it and stops it. An app that was
    stopped already is left as it is. Raises Refused when it has changed since
    the hold; any other failure raises as it is (asked again, a stop that went
    through is seen as done)."""
    if _was_stopped(record):
        return
    field = FIELD[record.kind]
    current = _read(record)
    if _stopped_by(current, record, job_kind, job_id):
        return
    if current is None or _meta(current).get("resourceVersion") != record.resource_version:
        raise Refused(f"{record.name} changed since it was recorded; hold it again")
    marks = {HELD_BY: f"{job_kind}:{job_id}", HELD_AS: _as_text(field, record.earlier[field])}
    if _meta(current).get("annotations") is None:
        ops = [{"op": "add", "path": "/metadata/annotations", "value": marks}]
    else:
        ops = [{"op": "add", "path": _pointer(key), "value": text} for key, text in marks.items()]
    if field == "runStrategy" and "running" in (current.get("spec") or {}):
        ops.append({"op": "remove", "path": "/spec/running"})  # runStrategy and running can't both be set
    ops.append({"op": "add", "path": f"/spec/{field}", "value": STOPPED[field]})
    try:
        KUBE.ksend("PATCH", _path(record.kind, record.namespace, record.name),
                   _fence(record.uid, record.resource_version) + ops, ctype=JSON_PATCH)
    except urllib.error.HTTPError as error:
        if error.code not in (409, 422):
            raise
        if not _stopped_by(_read(record), record, job_kind, job_id):
            raise Refused(f"{record.name} changed since it was recorded; hold it again") from None


def _no_room(name, check):
    reasons = []
    for row in (check.get("candidates") or []) if isinstance(check, dict) else []:
        for why in row.get("reasons") or []:
            if str(why) not in reasons:
                reasons.append(str(why))
    return f"{name} doesn't fit on the hosts yet" + (f" ({'; '.join(reasons[:2])})" if reasons else "")


def _claim_use(ns, claim):
    """(why the claim isn't free, final?), or ("", False)."""
    try:
        pvc = KUBE.get_optional(f"/api/v1/namespaces/{_q(ns)}/persistentvolumeclaims/{_q(claim)}", read=KUBE.kget)
        if pvc is None:
            return f"its claim {claim} is gone", True
        pods = KUBE.items(f"/api/v1/namespaces/{_q(ns)}/pods", read=KUBE.kget)
        attachments = KUBE.items(ATTACHMENTS, read=KUBE.kget) if (pvc.get("spec") or {}).get("volumeName") else []
    except Exception as error:
        return f"Homestead could not tell whether {claim} is free ({_why(error)})", False
    for pod in pods:
        volumes = (pod.get("spec") or {}).get("volumes") or []
        if _live(pod) and any((v.get("persistentVolumeClaim") or {}).get("claimName") == claim for v in volumes):
            return f"{claim} is in use by {_meta(pod).get('name')}", True
    volume = pvc["spec"].get("volumeName")
    for row in attachments:
        spec = row.get("spec") or {}
        if (spec.get("source") or {}).get("persistentVolumeName") == volume and (row.get("status") or {}).get("attached"):
            # No pod uses it: a volume letting go, or one that will once the
            # attacher catches up. That can pass.
            letting_go = " and letting go" if _meta(row).get("deletionTimestamp") else ""
            return f"{claim} is still attached on {spec.get('nodeName')}{letting_go}", False
    return "", False


def _blocker(current, claims):
    """What still stands in the way of starting it: (why, final?), or ("", False)."""
    name = _meta(current)["name"]
    pods = pods_of(current)
    if pods is None:
        return f"Homestead could not tell which pods {name} still has", False
    if pods:
        first = _meta(pods[0]).get("name")
        if current["kind"] == "CronJob":
            return f"a run of {name} is still going ({first})", False
        return f"{first} of {name} is still there", False
    for claim in claims:
        why, final = _claim_use(_meta(current)["namespace"], claim)
        if why:
            return why, final
    return "", False


def holds_on(obj):
    """The default restart guard: why an import or a storage copy keeps obj
    stopped, or "". Raises when what it needs can't be read."""
    failed = []

    def read(path):
        try:
            return KUBE.kget(path)
        except Exception as error:
            failed.append(error)
            raise
    meta = _meta(obj)
    why = IMPORT.pending(obj, meta.get("namespace", ""), read)
    if failed:
        raise failed[0]  # IMPORT words a failed read as a hold; it is a Wait here
    return f"{meta.get('name')} stays stopped: {why}" if why else ""


def _unmark(record, job_kind, job_id):
    """Take this job's marks off a VM that has been started (a Manual one)."""
    name = record.name
    try:
        current = _read(record)
    except Exception as error:
        return Wait(f"{name} was started; Homestead could not read it to finish ({_why(error)})")
    if current is None or _meta(current).get("uid") != record.uid:
        return Started(MANUAL)  # started; what is there now is not this job's
    notes = _notes(current)
    if not _ours(notes.get(HELD_BY) or "", job_kind, job_id):
        return Started(MANUAL)
    ops = (_fence(record.uid, _meta(current).get("resourceVersion")) + [{"op": "remove", "path": _pointer(HELD_BY)}] +
           ([{"op": "remove", "path": _pointer(HELD_AS)}] if HELD_AS in notes else []))
    try:
        KUBE.ksend("PATCH", _path(record.kind, record.namespace, name), ops, ctype=JSON_PATCH)
    except Exception as error:
        return Wait(f"{name} was started; Homestead could not take its hold off yet ({_why(error)})")
    return Started(MANUAL)


def restart(record, job_kind, job_id, wait_seconds=60, claims=(), guard=None):
    """Start again what this job stopped, as it ran, once that is safe (see
    the top of this file). guard(obj) -> reason to keep it stopped, or None/"";
    holds_on (import and storage-copy holds) when not given. A Manual VM is
    set back to Manual and started through KubeVirt, and stays marked until
    that start is asked for. -> Started, Wait (try again on a later round) or
    KeptStopped (a person decides; how_to says how). Never raises."""
    field, name = FIELD[record.kind], record.name
    earlier = record.earlier[field]
    manual = field == "runStrategy" and earlier == MANUAL

    def kept(reason):
        return KeptStopped(reason, how_to(record))
    try:
        current = _read(record)
    except Exception as error:
        return Wait(f"Homestead could not read {name} ({_why(error)})")
    if current is None:
        return kept(f"{name} is gone")
    meta = _meta(current)
    current = {**current, "kind": record.kind}
    if meta.get("uid") != record.uid:
        return kept(f"{name} was replaced by another object while it was stopped")
    if meta.get("deletionTimestamp"):
        return kept(f"{name} is being removed")
    if _was_stopped(record):
        return Started(_started_as(field, earlier))  # stopped before the job: never started
    mark, now = _notes(current).get(HELD_BY), _state(current)
    if not mark:
        if now != earlier:
            return kept(f"{name} isn't held by this job and was changed since it was recorded: "
                        f"{_as_text(field, now)} rather than {_as_text(field, earlier)}")
        if manual:
            try:
                if not _vm_running(record.namespace, name):
                    return kept(f"{name} isn't held by this job any more, and isn't running")
            except Exception as error:
                return Wait(f"Homestead could not tell whether {name} is running ({_why(error)})")
        # Running as before: a start whose reply was lost went through, or a
        # person started it as it was.
        return Started(_started_as(field, earlier))
    if not _ours(mark, job_kind, job_id):
        return kept(f"{name} is held by {mark}")
    # Set back to Manual by an earlier round: the start is what is left.
    resuming = manual and now == MANUAL
    if now != STOPPED[field] and not resuming:
        return kept(f"{name} was changed while it was stopped")
    if resuming:
        try:
            if _vm_running(record.namespace, name):
                return _unmark(record, job_kind, job_id)
        except Exception as error:
            return Wait(f"Homestead could not tell whether {name} is running ({_why(error)})")
    try:
        refusal = (guard or holds_on)(current)
    except Exception as error:
        return Wait(f"Homestead could not check what holds {name} ({_why(error)})")
    if refusal:
        return kept(refusal)
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        why, final = _blocker(current, claims)
        if final:
            return kept(why)
        if not why:
            break
        if time.monotonic() >= deadline:
            return Wait(why)
        time.sleep(POLL_SECONDS)
    if field == "replicas":
        proposed = {**current, "spec": {**(current.get("spec") or {}), "replicas": earlier}}
        try:
            import homestead_deploy_server as DEPLOY  # bound in the deploy routes
            check = DEPLOY.copy_admission(proposed)
        except Exception as error:
            return Wait(f"Homestead could not check that {name} fits ({_why(error)})")
        if not isinstance(check, dict) or check.get("blocked") is not False:
            return Wait(_no_room(name, check))
    if not resuming:
        ops = _fence(meta["uid"], meta.get("resourceVersion"))
        if not manual:  # a Manual VM keeps its marks until its start is asked for
            ops += [{"op": "remove", "path": _pointer(HELD_BY)}]
            if HELD_AS in _notes(current):
                ops.append({"op": "remove", "path": _pointer(HELD_AS)})
        if field == "runStrategy" and "running" in (current.get("spec") or {}):
            ops.append({"op": "remove", "path": "/spec/running"})
        ops.append({"op": "add", "path": f"/spec/{field}", "value": earlier})
        try:
            KUBE.ksend("PATCH", _path(record.kind, record.namespace, name), ops, ctype=JSON_PATCH)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return kept(f"{name} is gone")
            if error.code in (409, 422):
                return Wait(f"{name} changed while Homestead was starting it; it reads it again")
            return Wait(f"Homestead could not start {name} ({_why(error)})")
        except Exception as error:
            # The answer was lost: the next round reads whether it went through.
            return Wait(f"Homestead could not tell whether {name} started ({_why(error)}); it reads it again")
        if not manual:
            return Started(_started_as(field, earlier))
    try:
        KUBE.ksend("PUT", f"{SUB}/namespaces/{_q(record.namespace)}/virtualmachines/{_q(name)}/start", {})
    except Exception as error:
        return Wait(f"Homestead could not start {name} ({_why(error)}); it tries again")
    return _unmark(record, job_kind, job_id)


def how_to(record):
    """Where, in Homestead, a person starts it again."""
    if record.kind == "VirtualMachine":
        return f"Virtual machines → {record.name} → Start"
    if record.kind == "CronJob":
        return f"Schedules → {record.name} → Resume"
    n = record.earlier.get("replicas", 1)
    return f"Containers → {record.name} → Start with {n} replica" + ("" if n == 1 else "s")
