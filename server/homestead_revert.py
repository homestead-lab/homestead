"""Rolling a volume back to one of its snapshots.

Longhorn reverts only a volume nothing is using, attached to a node with no
block device on it (maintenance mode), and that is only done through its own
API. So, step by step, each advanced by the operations poll:

  1. what mounts the claim is stopped, and how each ran is recorded - as a
     storage class change does;
  2. once the volume has detached, it is attached in maintenance mode;
  3. the state it is in now is kept as a snapshot of its own, so the
     rollback can itself be undone by rolling back to that;
  4. the volume is reverted to the chosen snapshot;
  5. it is detached, and everything is started again the way it was.

What is started again is checked first, each time (_start_again): the same
object as was stopped (its UID, recorded with how it ran before the stop),
still mounting the same claim, bound to the same volume; no pod still on the
claim, terminating ones included; no copy or import holding it; and, for a
Deployment or StatefulSet, room for it. The write is conditional on the
object read. Any doubt leaves it stopped: the step waits a while, then the
job ends saying which app was not started, why, and how to start it from
Homestead. Something already gone when it was to be stopped (a 404, not a
read that failed) is left gone, and never started again.

A failure before the revert puts things back: the volume is detached and
everything started again, unchanged. So does a failure of Longhorn's API up
to the revert: a connection that fails is tried again a few times, then the
job ends rather than keep the app stopped. A revert whose answer is lost may
still be running in Longhorn: the volume is held until Longhorn's engine
shows whether it happened, and if that cannot be told it stays held and the
app stopped, saying so. Before anything is stopped, Longhorn's API must answer.

What is about to be sent - the kept snapshot's name, then the revert - is
saved first (OPS.checkpoint), so a leader that takes over mid-step never
makes a second snapshot or sends the revert twice: a revert that may have
gone out is only ever confirmed. Confirming assumes a revert cannot still
start once the confirming wait (5 minutes) is over - the request itself
gives up after 30 s - so a chain still on the kept snapshot then means it
did not happen.
"""
import json
import time
import urllib.error
import urllib.request

import homestead_kube as KUBE
import homestead_longhorn_access as ACCESS
import homestead_import_guard as IMPORT
import homestead_operations as OPS
import homestead_self as SELF
import homestead_storage_guard as STORAGE_GUARD

LHNS = "longhorn-system"
API = f"/apis/longhorn.io/v1beta2/namespaces/{LHNS}"
BACKEND = ACCESS.URL
ATTACHMENT = "homestead-revert"
WAIT = {"stopping": 600, "attaching": 300, "confirming": 300, "detaching": 300, "restarting": 300, "starting": 600}
# A step whose connection failed (refused, reset, timed out) is tried again on
# a later round, this far apart, this many times in all; longhorn-backend
# spreads requests over the manager pods, so one can blip.
TRIES, RETRY_AFTER = 3, 5


class Refused(ValueError):
    """Longhorn answered, and turned the request down (an HTTP 4xx)."""


class ServerError(RuntimeError):
    """Longhorn answered with an error of its own (an HTTP 5xx): the request
    may have done its work anyway - an engine call can time out while the
    replicas carry on."""

kget = ksend = None
reclass = None          # homestead_reclass, for what uses a claim
is_self = lambda ns, name: False
lh_call = None          # (method, path, body) -> dict, Longhorn's own API


def bind(_kget, _ksend, _reclass, _is_self=None, _lh_call=None):
    global kget, ksend, reclass, is_self, lh_call
    kget, ksend, reclass = _kget, _ksend, _reclass
    is_self = _is_self or is_self
    lh_call = _lh_call or _backend


def _backend(method, path, body=None):
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(f"{BACKEND}{path}", data=data if method == "POST" else None, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode() or "{}"
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        if error.code >= 500:
            raise ServerError(f"Longhorn failed: {detail or error.reason}") from error
        raise Refused(f"Longhorn refused it: {detail or error.reason}") from error
    return json.loads(text)


def _volume(name):
    volume = KUBE.get_optional(f"{API}/volumes/{name}", read=kget)
    if not volume:
        raise ValueError(f"there is no Longhorn volume {name}")
    return volume


def _snapshot(name, volume):
    snap = KUBE.get_optional(f"{API}/snapshots/{name}", read=kget)
    if not snap or (snap.get("spec") or {}).get("volume") != volume:
        raise ValueError(f"{volume} has no snapshot {name}")
    return snap


def _replica_node(volume):
    """A node holding a working copy, to attach the volume to."""
    for replica in (kget(f"{API}/replicas").get("items") or []):
        spec, status = replica.get("spec") or {}, replica.get("status") or {}
        if spec.get("volumeName") == volume and spec.get("nodeID") and not spec.get("failedAt") \
                and status.get("currentState") in ("running", "stopped", ""):
            return spec["nodeID"]
    return ""


def plan(volume, snapshot):
    """What rolling back would stop, before anything is changed."""
    vol = _volume(volume)
    snap = _snapshot(snapshot, volume)
    k8s = (vol.get("status") or {}).get("kubernetesStatus") or {}
    ns, claim = k8s.get("namespace", ""), k8s.get("pvcName", "")
    users = reclass.consumers(ns, claim) if ns and claim else []
    blockers = []
    for user in users:
        if user["kind"] == "DaemonSet":
            blockers.append(f"{user['name']} is a DaemonSet, which cannot be stopped for this; remove its claim first")
        if is_self(ns, user["name"]):
            blockers.append("this is Homestead's own data: rolling it back would stop Homestead part-way")
    known = {user["name"] for user in users}
    loose = [pod["metadata"]["name"] for pod in (reclass._using_pods(ns, claim, helpers=False) if claim else [])
             if not any(pod["metadata"]["name"].startswith(name) for name in known)]
    if loose:
        blockers.append(f"{', '.join(loose[:3])} use{'s' if len(loose) == 1 else ''} it and belong{'s' if len(loose) == 1 else ''} "
                        "to nothing Homestead can stop and start again; stop it first")
    status = snap.get("status") or {}
    return {"volume": volume, "snapshot": snapshot, "namespace": ns, "claim": claim,
            "created": status.get("creationTime", ""), "consumers": users, "blockers": blockers,
            "ready": not blockers and bool(status.get("readyToUse", True))}


def _path(ns, c):
    kind = c["kind"]
    if kind in ("Deployment", "StatefulSet"):
        return f"/apis/apps/v1/namespaces/{ns}/{'deployments' if kind == 'Deployment' else 'statefulsets'}/{c['name']}"
    if kind == "CronJob":
        return f"/apis/batch/v1/namespaces/{ns}/cronjobs/{c['name']}"
    return f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{c['name']}"


def _claim_path(ns, claim):
    return f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}"


def _record(ns, claim, consumers):
    """Which objects are about to be stopped, and which claim: saved with the
    job before anything is changed, so only these are ever started again."""
    for c in consumers:
        obj = KUBE.get_optional(_path(ns, c), read=kget)
        uid = ((obj or {}).get("metadata") or {}).get("uid")
        if not uid:
            raise ValueError(f"{c['name']} is no longer there; open the rollback again. Nothing was changed")
        c["uid"] = uid
    pvc = KUBE.get_optional(_claim_path(ns, claim), read=kget)
    uid = ((pvc or {}).get("metadata") or {}).get("uid")
    if not uid:
        raise ValueError(f"the claim {claim} is no longer there; open the rollback again. Nothing was changed")
    return uid


def start(volume, snapshot, ops):
    p = plan(volume, snapshot)
    if p["blockers"]:
        raise ValueError(p["blockers"][0])
    # Before anything is stopped: every later step goes through Longhorn's API.
    ACCESS.require(kget, ksend, SELF.NS, f"Rolling {p['claim'] or volume} back")
    claim_uid = _record(p["namespace"], p["claim"], p["consumers"]) if p["namespace"] and p["claim"] else ""
    ref = {"volume": volume, "snapshot": snapshot, "namespace": p["namespace"], "claim": p["claim"],
           "claim_uid": claim_uid, "consumers": p["consumers"], "phase": "stopping", "since": time.time()}
    label = p["claim"] or volume
    return ops.start("snapshot-revert", f"Roll {label} back to a snapshot",
                     {"kind": "Volume", "name": label, "namespace": p["namespace"]}, "/data-protection", ref,
                     f"Stopping what uses {label}")


STOPPED = {"Deployment": {"replicas": 0}, "StatefulSet": {"replicas": 0}, "CronJob": {"suspend": True},
           "VirtualMachine": {"runStrategy": "Halted", "running": None}}


def _stop(ns, ref):
    """Stop what is not stopped yet: "" once all are, else why one is not.
    One Kubernetes no longer has (a 404) is gone, not stopped: it is never
    started again. Any other failure is tried again on a later round."""
    for c in ref["consumers"]:
        if c.get("stopped"):
            continue
        try:
            ksend("PATCH", _path(ns, c), {"spec": STOPPED[c["kind"]]}, ctype="application/merge-patch+json")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                return f"{c['name']} could not be stopped ({_why(error)})"
            c["gone"] = True
        except Exception as error:
            return f"{c['name']} could not be stopped ({_why(error)})"
        c["stopped"] = True
    return ""


def _phase(ref, phase):
    ref["phase"], ref["since"] = phase, time.time()


def _late(ref):
    return time.time() - float(ref.get("since") or 0) > WAIT.get(ref.get("phase"), 600)


def _state(volume):
    return ((_volume(volume).get("status") or {}).get("state") or "").lower()


def _why(error):
    return str(getattr(error, "reason", None) or error)


def _connection(error):
    """A failure on the connection - refused, reset, timed out - rather than
    an answer: the request may be tried again."""
    return isinstance(error, OSError) and not isinstance(error, urllib.error.HTTPError)


def _never_sent(error):
    """Whether a failed request certainly changed nothing: Longhorn turned it
    down, or the connection was refused before it went out."""
    return isinstance(error, Refused) or isinstance(getattr(error, "reason", error), ConnectionRefusedError)


def _again(ref, step, error=None):
    """Whether a failed step is tried again on a later round."""
    if error is not None and not _connection(error):
        return False
    tries = ref.setdefault("tries", {})
    tries[step] = tries.get(step, 0) + 1
    if tries[step] >= TRIES:
        return False
    ref["retry_at"] = time.time() + RETRY_AFTER
    return True


def _pausing(ref):
    return time.time() < float(ref.get("retry_at") or 0)


def _drop_ticket(ref):
    """Remove the rollback's attachment ticket through Kubernetes, not
    Longhorn's API: "" once it is gone, else why not."""
    try:
        ksend("PATCH", f"{API}/volumeattachments/{ref['volume']}", {"spec": {"attachmentTickets": {ATTACHMENT: None}}},
              ctype="application/merge-patch+json")
    except urllib.error.HTTPError as error:
        if error.code != 404:                       # no attachment record: nothing holds it
            return _why(error)
    except Exception as error:
        return _why(error)
    ref["attached"] = False
    return ""


def _let_go(ref):
    """Detach the volume from the rollback. ("", None) once done, else why not
    and the error Longhorn's API gave."""
    try:
        lh_call("POST", f"/volumes/{ref['volume']}?action=detach",
                {"attachmentID": ATTACHMENT, "hostId": ref.get("node", ""), "forceDetach": False})
    except Exception as error:
        dropped = _drop_ticket(ref)
        return (f"{_why(error)}; {dropped}", error) if dropped else ("", None)
    ref["attached"] = False
    return "", None


def _held(ref, stuck):
    return (f". Homestead could not let go of {ref['volume']} ({stuck}): until the Longhorn attachment ticket "
            f"{ATTACHMENT} on it is removed, its app cannot start (see What Homestead changes)") if stuck else ""


def _names(ref):
    return ", ".join(c["name"] for c in ref.get("consumers") or []) or "what used it"


def _count(n):
    return f"{n} replica" + ("" if n == 1 else "s")


def _how(c):
    """Where, in Homestead, a person starts it again."""
    if c["kind"] in ("Deployment", "StatefulSet"):
        return f"Containers → {c['name']} → Start with {_count(c.get('replicas', 1))}"
    if c["kind"] == "VirtualMachine":
        return f"Virtual machines → {c['name']} → Start"
    # Homestead shows a paused schedule but has no control to resume one.
    return (f"Scheduled jobs shows {c['name']} as paused; resume it by setting its spec.suspend back to false "
            f"(kubectl, or the Git source it comes from)")


def _admission(obj):
    """The capacity check a start from Homestead gets: a dict whose "blocked"
    is False when it fits. Bound in the deploy routes, so imported late."""
    import homestead_deploy_server as DEPLOY
    return DEPLOY.copy_admission(obj)


def _claims(spec):
    return {(v.get("persistentVolumeClaim") or {}).get("claimName") or (v.get("dataVolume") or {}).get("name")
            for v in (spec or {}).get("volumes") or []} - {None, ""}


def _mounts(c, obj, claim):
    spec = obj.get("spec") or {}
    if c["kind"] == "CronJob":
        pod = ((((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec"))
    else:
        pod = (spec.get("template") or {}).get("spec")
    if claim in _claims(pod):
        return True
    return c["kind"] == "StatefulSet" and any(
        claim.startswith(f"{(t.get('metadata') or {}).get('name')}-{c['name']}-")
        for t in spec.get("volumeClaimTemplates") or [])


def _as_before(c, spec):
    if c["kind"] in ("Deployment", "StatefulSet"):
        return spec.get("replicas", 1) == c.get("replicas", 1)
    if c["kind"] == "CronJob":
        return bool(spec.get("suspend")) == bool(c.get("suspend"))
    return spec.get("runStrategy") == c.get("run_strategy")


def _as_left(c, spec):
    if c["kind"] in ("Deployment", "StatefulSet"):
        return spec.get("replicas") == 0
    if c["kind"] == "CronJob":
        return spec.get("suspend") is True
    return spec.get("runStrategy") == "Halted"


def _running(c):
    if c["kind"] in ("Deployment", "StatefulSet"):
        return {"replicas": c.get("replicas", 1)}
    if c["kind"] == "CronJob":
        return {"suspend": bool(c.get("suspend"))}
    return {"runStrategy": c.get("run_strategy")}


def _claim_doubt(ns, ref, rolled_back):
    """Why the claim is not safe to start anything on, or ""."""
    claim, volume = ref.get("claim", ""), ref["volume"]
    try:
        pvc = KUBE.get_optional(_claim_path(ns, claim), read=kget)
    except Exception as error:
        return f"Homestead could not read the claim {claim} ({_why(error)})"
    meta = (pvc or {}).get("metadata") or {}
    if pvc is None or meta.get("deletionTimestamp"):
        return f"the claim {claim} is gone or being removed"
    if ref.get("claim_uid") and meta.get("uid") != ref["claim_uid"]:
        return f"the claim {claim} was replaced"
    bound = (pvc.get("spec") or {}).get("volumeName")
    if bound != volume:
        return f"the claim {claim} is now bound to {bound or 'nothing'}, not {volume}"
    if rolled_back and not ref.get("stuck"):
        # The rollback's own ticket, if it could not be removed, is named in
        # the message; anything else holding the volume is not this job's.
        try:
            state = _state(volume)
        except Exception as error:
            return f"Homestead could not read {volume} ({_why(error)})"
        if state != "detached":
            return f"{volume} is {state or 'in an unknown state'}, not detached"
    return ""


OWNERS = ("/apis/apps/v1/namespaces/{ns}/replicasets", "/apis/batch/v1/namespaces/{ns}/jobs",
          "/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances")


def _ancestors(ns, pods):
    """Each pod's owners and their owners, by UID: a Deployment's pod through
    its ReplicaSet, a CronJob's through its Job, a VM's through its instance.
    Raises when they cannot be read: then whose a pod is, is unknown."""
    up = {}
    for path in OWNERS:
        listing = KUBE.get_optional(path.format(ns=ns), read=kget)
        if listing is None:                         # not installed (KubeVirt): nothing owned through it
            continue
        if not isinstance(listing.get("items"), list) or (listing.get("metadata") or {}).get("continue"):
            raise ValueError("the list of what owns the pods is incomplete")
        for row in listing["items"]:
            meta = row.get("metadata") or {}
            up[meta.get("uid")] = {o.get("uid") for o in meta.get("ownerReferences") or [] if o.get("uid")}
    found = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        direct = {o.get("uid") for o in meta.get("ownerReferences") or [] if o.get("uid")}
        found[meta.get("name", "")] = direct.union(*(up.get(uid, set()) for uid in direct))
    return found


def _no_room(name, check):
    reasons = []
    for row in check.get("candidates") or [] if isinstance(check, dict) else []:
        for why in row.get("reasons") or []:
            if why not in reasons:
                reasons.append(str(why))
    return f"{name} no longer fits on the hosts" + (f" ({'; '.join(reasons[:2])})" if reasons else "")


def _start_again(ns, ref, rolled_back=True):
    """Start again what this job stopped, each only once it checks out (see
    the top of this file). Returns {(kind, name): why} for what is left
    stopped; empty once everything that can start has been asked to. Never raises."""
    left = {}
    consumers = ref.get("consumers") or []
    todo = [c for c in consumers if c.get("stopped") and not c.get("gone") and not c.get("started")]
    if not todo or not ns:
        return left
    doubt = _claim_doubt(ns, ref, rolled_back)
    pods, owners = [], {}
    try:
        pods = reclass._using_pods(ns, ref["claim"], helpers=True) if ref.get("claim") else []
        owners = _ancestors(ns, pods) if pods else {}
    except Exception as error:
        doubt = doubt or f"Homestead could not tell what still uses {ref.get('claim')} ({_why(error)})"
    for c in todo:
        name, key = c["name"], (c["kind"], c["name"])
        try:
            obj = KUBE.get_optional(_path(ns, c), read=kget)
        except Exception as error:
            left[key] = f"Homestead could not read {name} ({_why(error)})"
            continue
        if obj is None:
            c["gone"] = True                # removed while stopped: nothing to start
            continue
        meta, spec = obj.get("metadata") or {}, obj.get("spec") or {}
        if c.get("uid") and meta.get("uid") != c["uid"]:
            left[key] = f"{name} was replaced by another object while it was stopped"
            continue
        if meta.get("deletionTimestamp"):
            left[key] = f"{name} is being removed"
            continue
        if not _mounts(c, obj, ref["claim"]):
            left[key] = f"{name} no longer uses {ref['claim']}"
            continue
        if _as_before(c, spec):
            # Running as before already, or a start whose reply was lost went
            # through: its own new pods are not old writers.
            c.update(started=True, seen_uid=meta.get("uid"))
            continue
        if not _as_left(c, spec):
            left[key] = f"{name} was changed while it was stopped"
            continue
        if doubt:
            left[key] = doubt
            continue
        # Never beside a pod still on the volume, terminating ones included:
        # two writers on the same data. After a revert that is any pod but
        # those of what this job has started again; before one (nothing was
        # changed) only the app's own.
        mine = {meta.get("uid")}
        started = {x.get("seen_uid") or x.get("uid") for x in consumers if x.get("started")} - {None}
        # A pod whose owner cannot be told belongs to no one: it blocks too.
        blocking = [p for p, up in owners.items() if not up or (not up & started if rolled_back else up & mine)]
        if blocking:
            left[key] = f"{blocking[0]} still uses {ref['claim']}"
            continue
        hold = IMPORT.pending(obj, ns, kget)
        if hold:
            left[key] = f"{name}: {hold}"
            continue
        if c["kind"] in ("Deployment", "StatefulSet") and c.get("replicas"):
            proposed = {**obj, "spec": {**spec, "replicas": c["replicas"]}}
            try:
                check = _admission(proposed)
            except Exception as error:
                left[key] = f"Homestead could not check that {name} fits ({_why(error)})"
                continue
            if not isinstance(check, dict) or check.get("blocked") is not False:
                left[key] = _no_room(name, check)
                continue
        try:
            ksend("PATCH", _path(ns, c), {"metadata": {"resourceVersion": meta.get("resourceVersion")},
                                          "spec": _running(c)}, ctype="application/merge-patch+json")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                c["gone"] = True
            else:
                left[key] = f"Homestead could not start {name} ({_why(error)})"
            continue
        except Exception as error:
            # Its answer was lost: the next round reads whether it went through.
            left[key] = f"Homestead could not start {name} ({_why(error)})"
            continue
        c.update(started=True, seen_uid=meta.get("uid"))
    return left


def _left(ref, left):
    """What a person reads about apps left stopped: each, why, and how to start it."""
    rows = {(c["kind"], c["name"]): c for c in ref.get("consumers") or []}
    return "; ".join(f"{key[1]} was left stopped: {why}. Once that is resolved: {_how(rows[key])}"
                     for key, why in left.items())


def _gone(ref):
    gone = [c["name"] for c in ref.get("consumers") or [] if c.get("gone")]
    return f"; {', '.join(gone)} {'was' if len(gone) == 1 else 'were'} no longer there, and {'is' if len(gone) == 1 else 'are'} not made again" if gone else ""


def _put_back(ref, why, stuck="", final=False):
    """Before the revert, or once it is known not to have happened: let go of
    the volume, start everything again, and say why - and what may have changed.
    A start that does not check out is tried again on a few later rounds
    ("putting-back"), unless final (a cancel ends the job at once)."""
    try:
        if ref.get("attached"):
            stuck = _let_go(ref)[0]
    finally:
        # Nothing was changed on the volume, so a ticket still on it is no
        # reason to keep the apps off it - the checks still apply.
        left = _start_again(ref.get("namespace", ""), ref, rolled_back=False)
    ref["put_back"] = {"why": why, "stuck": stuck}
    if left and not final and _again(ref, "put back"):
        _phase(ref, "putting-back")
        return "running", 95, f"{why}. Putting things back; {next(iter(left.values()))}"
    return _put_back_end(ref, left)


def _put_back_end(ref, left):
    why, stuck = ref["put_back"]["why"], ref["put_back"]["stuck"]
    started = (_left(ref, left) if left else "everything that was running is starting again") + _gone(ref)
    if ref.get("reverting"):
        changed = (f"Longhorn may have rolled it back before failing: the state before is kept as snapshot "
                   f"{ref.get('kept', '')}, to roll back to if needed")
    elif ref.get("kept"):
        changed = f"Nothing was rolled back; the state it is in is also kept as snapshot {ref['kept']}"
    else:
        changed = "Nothing was changed"
    return "failed", 100, f"{why}. {changed}; {started}{_held(ref, stuck)}"


def _attach(ref, volume):
    lh_call("POST", f"/volumes/{volume}?action=attach",
            {"hostId": ref["node"], "disableFrontend": True, "attachedBy": "", "attacherType": "longhorn-api",
             "attachmentID": ATTACHMENT})


def _snapshot_made(volume, name):
    """Whether Longhorn made a snapshot whose request failed on the way back."""
    try:
        snap = KUBE.get_optional(f"{API}/snapshots/{name}", read=kget)
    except Exception:
        return False
    return bool(snap) and (snap.get("spec") or {}).get("volume") == volume


def _head_parent(volume, kept):
    """The snapshot the volume's live data sits on, from Longhorn's engine:
    the rolled-back-to snapshot once a revert is done. Trusted only once the
    engine's chain lists the kept snapshot - the list lags, and before that
    it can still show the head on the newest snapshot. "" when unknown."""
    try:
        engines = kget(f"{API}/engines?labelSelector=longhornvolume%3D{volume}").get("items") or []
    except Exception:
        return ""
    for engine in engines:
        chain = (engine.get("status") or {}).get("snapshots") or {}
        parent = (chain.get("volume-head") or {}).get("parent")
        if parent and kept in chain:
            return parent
    return ""


def _reverted(ref):
    ref["reverted"] = True
    _phase(ref, "detaching")
    stuck, error = _let_go(ref)
    if stuck and not _again(ref, "detach", error):     # else tried again while detaching
        ref["stuck"] = stuck
    return "running", 70, f"Rolled back to {ref['snapshot']}; the state before is kept as {ref.get('kept', '')}"


def resolve(item):
    ref = item["ref"]
    ns, volume = ref.get("namespace", ""), ref["volume"]
    phase = ref.get("phase")
    if ref.get("revert_sent") and not ref.get("reverted") and phase not in ("confirming", "putting-back"):
        # (A put-back runs only once the revert is known not to have happened.)
        # A leader before this one may have sent the revert: confirm it, never
        # send it again - and before any step's own checks, which could put
        # things back while Longhorn may still be reverting.
        ref.setdefault("revert_error", "Homestead restarted before Longhorn answered")
        _phase(ref, "confirming")
        return "running", 60, f"Checking whether Longhorn rolled {volume} back"
    if phase == "putting-back":
        if _pausing(ref):
            return "running", 95, "Putting things back"
        left = _start_again(ns, ref, rolled_back=False)
        if left and _again(ref, "put back"):
            return "running", 95, f"Putting things back; {next(iter(left.values()))}"
        return _put_back_end(ref, left)
    if phase == "stopping":
        unstopped = _stop(ns, ref) if ns else ""
        if unstopped:
            if _late(ref):
                return _put_back(ref, unstopped)
            return "running", 10, f"Stopping what uses {ref['claim'] or volume}: {unstopped}; trying again"
        using = reclass._using_pods(ns, ref["claim"], helpers=True) if ns and ref.get("claim") else []
        if using or _state(volume) != "detached":
            if _late(ref):
                return _put_back(ref, f"{volume} did not detach in time"
                                      + (f" - {using[0]['metadata']['name']} still uses it" if using else ""))
            return "running", 10, f"Waiting for {ref['claim'] or volume} to be let go"
        node = _replica_node(volume)
        if not node:
            return _put_back(ref, f"no node holds a working copy of {volume}")
        ref["node"] = node
        try:
            _attach(ref, volume)
        except Exception as error:
            if _again(ref, "attach", error):
                # Longhorn may hold it already; the same ticket is asked for again.
                ref.update(attached=True, reattach=True)
                _phase(ref, "attaching")
                return "running", 25, f"Longhorn's API did not answer ({_why(error)}); trying again"
            # Longhorn may have taken the ticket before failing: drop it
            # through Kubernetes, which needs no answer from Longhorn's API.
            return _put_back(ref, f"Longhorn's API did not attach {volume} for the rollback: {_why(error)}",
                             _drop_ticket(ref))
        ref["attached"] = True
        _phase(ref, "attaching")
        return "running", 30, f"Attaching {volume} to {node} for the rollback"
    if phase == "attaching":
        if _pausing(ref):
            return "running", 30, f"Trying Longhorn's API again for {volume}"
        if ref.get("reattach"):
            try:
                _attach(ref, volume)
            except Exception as error:
                if _again(ref, "attach", error):
                    return "running", 25, f"Longhorn's API did not answer ({_why(error)}); trying again"
                ref["attached"] = False         # its API is not answering: let go through Kubernetes
                return _put_back(ref, f"Longhorn's API did not attach {volume} for the rollback: {_why(error)}",
                                 _drop_ticket(ref))
            ref["reattach"] = False
        if _state(volume) != "attached":
            if _late(ref):
                return _put_back(ref, f"{volume} did not attach for the rollback")
            return "running", 35, f"Attaching {volume} to {ref.get('node')}"
        if not ref.get("kept"):
            if ref.get("keep") and _snapshot_made(volume, ref["keep"]):
                ref["kept"] = ref["keep"]               # made before a restart lost the answer
            else:
                if not ref.get("keep"):
                    ref["keep"] = f"before-rollback-{int(time.time())}"
                    OPS.checkpoint(item)                # one name, whoever sends it
                keep = ref["keep"]
                try:
                    lh_call("POST", f"/volumes/{volume}?action=snapshotCreate", {"name": keep})
                except Exception as error:
                    if not _snapshot_made(volume, keep):
                        if _again(ref, "snapshotCreate", error):
                            return "running", 40, f"Longhorn's API did not answer ({_why(error)}); trying again"
                        return _put_back(ref, f"Longhorn's API did not keep the present state of {volume}: {_why(error)}")
                ref["kept"] = keep
        ref.update(reverting=True, revert_sent=True)
        OPS.checkpoint(item)                            # saved before it goes out
        try:
            lh_call("POST", f"/volumes/{volume}?action=snapshotRevert", {"name": ref["snapshot"]})
        except Exception as error:
            if _never_sent(error):
                ref.update(reverting=False, revert_sent=False)
                return _put_back(ref, f"Longhorn did not roll {volume} back: {_why(error)}")
            # The request went out: Longhorn may still be reverting. Never let
            # go of the volume, or start what uses it, until that is known.
            ref["revert_error"] = _why(error)
            _phase(ref, "confirming")
            return "running", 60, f"Longhorn's API did not answer the rollback ({_why(error)}); checking whether it happened"
        return _reverted(ref)
    if phase == "confirming":
        parent = _head_parent(volume, ref.get("kept", ""))
        if parent == ref["snapshot"]:
            return _reverted(ref)
        if not _late(ref):
            return "running", 65, f"Checking whether Longhorn rolled {volume} back"
        why = f"Longhorn's API did not answer the rollback ({ref.get('revert_error', '')})"
        if parent and parent == ref.get("kept"):
            ref["reverting"] = False
            return _put_back(ref, f"{why}, and Longhorn did not roll {volume} back")
        return ("failed", 100, f"{why}, and whether {volume} was rolled back cannot be told. To keep its data "
                               f"consistent it stays attached in maintenance mode and {_names(ref)} stay stopped. "
                               f"Check its snapshots in Longhorn; the state before is kept as snapshot "
                               f"{ref.get('kept', '')}. Then remove the Longhorn attachment ticket {ATTACHMENT} on "
                               f"it (see What Homestead changes) and start {_names(ref)} again")
    if phase == "detaching":
        if ref.get("attached") and not ref.get("stuck"):
            if _pausing(ref) and not _late(ref):
                return "running", 75, f"Letting go of {volume}"
            stuck, error = _let_go(ref)
            if stuck and _again(ref, "detach", error) and not _late(ref):
                return "running", 75, f"Letting go of {volume}: {stuck}"
            ref["stuck"] = stuck
        if _state(volume) != "detached" and not _late(ref):
            return "running", 80, f"Detaching {volume}"
        _phase(ref, "restarting")
        phase = "restarting"
    if phase == "restarting":
        left = _start_again(ns, ref)
        if not left:
            _phase(ref, "starting")
            return "running", 85, "Starting everything again"
        if not _late(ref):
            (_, name), why = next(iter(left.items()))
            return "running", 82, f"Waiting to start {name} again: {why}"
        return ("failed", 100, f"Rolled back to {ref['snapshot']}; the state before is kept as snapshot "
                               f"{ref.get('kept', '')}. {_left(ref, left)}{_gone(ref)}{_held(ref, ref.get('stuck', ''))}")
    if phase == "starting":
        running = [c for c in ref.get("consumers") or [] if not c.get("gone")]
        waiting = reclass._started(ns, {**ref, "consumers": running}) if ns else []
        if waiting and not _late(ref):
            return "running", 90, f"Waiting for {', '.join(waiting[:3])} to be ready"
        tail = f"; {', '.join(waiting)} not ready yet" if waiting else ""
        return ("succeeded", 100, f"{ref['claim'] or volume} is back to {ref['snapshot']}. The state before is kept "
                                  f"as snapshot {ref.get('kept', '')}, to roll forward again if needed{tail}"
                                  f"{_gone(ref)}{_held(ref, ref.get('stuck', ''))}")
    return "failed", 100, f"unknown step {phase}"


CONFIRMING = "The rollback is being confirmed; Homestead will finish or stop it safely"


def _uncancellable(ref):
    if ref.get("phase") == "putting-back":
        return "Homestead is putting everything back already"
    if ref.get("reverted"):
        return "The volume is rolled back already; what is left is starting things again"
    if ref.get("reverting") or ref.get("revert_sent") or ref.get("phase") == "confirming":
        return CONFIRMING           # Longhorn may still be reverting: letting go now could corrupt it
    return ""


def cancel_plan(item):
    why = _uncancellable(item["ref"])
    if why:
        return {"mode": "stop", "can": False, "why_not": why}
    return {"mode": "rollback", "undo": ["The volume is let go and everything that was stopped is started again"],
            "keeps": ["The volume's data, which has not been changed"], "severity": "low", "needs": "admin"}


def cancel_run(item, _options):
    why = _uncancellable(item["ref"])
    if why:
        raise ValueError(why)
    return _put_back(item["ref"], "Cancelled", final=True)[2]


def start_guarded(b):
    """start, holding the volume's storage fence: rolling back stops and
    starts what uses it and writes through Longhorn's own API too."""
    with STORAGE_GUARD.volume(OPS, kget, b.get("volume")):
        operation = start(str(b.get("volume") or ""), str(b.get("snapshot") or ""), OPS)
    return {"ok": True, "operation": operation,
            "detail": "Rolling back: what uses it stops first, then starts again"}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/lh/snapshot/revert/plan"): ("viewer", lambda request: plan((request.query.get("volume") or [""])[0], (request.query.get("snapshot") or [""])[0])),
    ("POST", "/api/lh/snapshot/revert"): ("admin", lambda request: start_guarded(request.body)),
}
