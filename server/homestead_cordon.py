"""How a Homestead job cordons a host and later gives it back.

The cordon mark on a Node (homestead.io/cordoned-by) names every job holding
it, as comma-separated <kind>:<job id> entries. cordon() adds the job and
cordons in one fenced patch; release() takes the job away and uncordons only
when no holder is left and no other host-holding job is on the host. A host
already cordoned with no mark was cordoned by a person: a job never takes
that over, so never uncordons it.

The marks older Homestead jobs wrote are read as holders too, until no job
started by an older Homestead can still be running: cluster-shutdown=<run id>,
upgrade-cordon=<job id>, and a cordoned-by value with no ":" (the Longhorn V2
clean-up's job id). New writes only ever use cordoned-by.

A holder whose job has ended, or is not in the job list, is stale: it holds
nothing, and any write to the Node takes it away. A cordon only stale holders
keep was left by a job that ended - often for a person to look at - so, like
a person's, no job takes it over. When the job list cannot be read, every
holder counts and nothing is joined or released.

A job that ends leaving its host cordoned for a person on purpose calls
leave_for_person(), which swaps its holder for person:<job id>. That entry is
never stale: cordon() reads the host as a person's (PERSON), and release()
- whoever calls it - lets its own holder go but never uncordons (FOR_PERSON).
Only a person gives the host back, on the Hosts page.

For adopters: PERSON and LEFT (BEFORE) both mean "was cordoned before" -
record that, and never release it. Any other done False from cordon() may
follow a write that applied but whose reply was lost: call release() before
giving up. That is safe, since release() of a holder not in the mark writes
nothing.

Every write tests the Node's uid and resourceVersion. A conflict is read again
and retried, up to WRITE_ATTEMPTS; a write refused while the Node had not
changed is not asked again. A read that fails is never "free to uncordon"."""
import urllib.error
from typing import NamedTuple

import homestead_job_kinds as JOB_KINDS
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_operations as OPS

MARK = NAMES.key("cordoned-by")
# Older marks: key -> the kind of job whose run or job id is the value.
OLD_MARKS = {NAMES.key("cluster-shutdown"): "cluster-shutdown", NAMES.key("upgrade-cordon"): "platform-upgrade"}
# An old cordoned-by value with no kind in it was written by this kind.
BARE_MARK_KIND = "longhorn-v2-cleanup"
WRITE_ATTEMPTS = 4
# Kinds that hold a host only through a mark - their own, or that of the
# node-power job they run - never just by being active.
BY_MARK_ONLY = ("os-rollout", "cluster-shutdown", "longhorn-v2-cleanup")

# A holder of this kind is a person: a job that ended leaving the host
# cordoned for someone to look at writes person:<its job id>. It is never
# live and never stale: no job takes it away or uncordons past it.
PERSON_KIND = "person"

# cordon() reasons that mean the host was cordoned before this job. Both mean
# "was cordoned before": the job records that and never releases it. Any other
# done False from cordon() may follow a write whose reply was lost (applied,
# answer gone): the adopter calls release() before giving up, which is safe -
# release() of a holder not in the mark writes nothing.
PERSON = "cordoned by a person"
LEFT = "cordoned before, by a job that has ended"
BEFORE = (PERSON, LEFT)
# release() and leave_for_person(): the host stays cordoned for a person.
FOR_PERSON = "left cordoned for a person"
GONE = "host is gone"
MISSING = "no such host"
UNREAD_JOBS = "Homestead could not read its job list"


class Outcome(NamedTuple):
    done: bool
    node: str
    reason: str


def _entries(value):
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _holder(entry):
    return entry if ":" in entry else f"{BARE_MARK_KIND}:{entry}"


def _person(holder):
    return holder.startswith(PERSON_KIND + ":")


def _annotations(node):
    return (node.get("metadata") or {}).get("annotations") or {}


def _marked(node):
    """The holders in the cordon mark itself, in order, each spelled <kind>:<id>."""
    out = []
    for entry in _entries(_annotations(node).get(MARK)):
        holder = _holder(entry)
        if holder not in out:
            out.append(holder)
    return out


def _old(node):
    """{older mark key: its holder} for the older marks on this Node."""
    return {key: f"{kind}:{_annotations(node)[key]}" for key, kind in OLD_MARKS.items() if _annotations(node).get(key)}


def holders(node, live=None):
    """Every job holding this host, from the cordon mark and the older marks.
    live: the holders whose job is still going (live_holders()); given, the
    stale ones are left out. None counts every holder."""
    found = set(_marked(node)) | set(_old(node).values())
    return found if live is None else {h for h in found if h in live or _person(h)}


def _jobs():
    try:
        return OPS.stored()
    except Exception:
        return None


def _live(jobs):
    if jobs is None:
        return None
    out = set()
    for item in jobs:
        if item.get("status") in OPS.TERMINAL:
            continue
        kind = item.get("kind")
        out.add(f"{kind}:{item.get('id')}")
        run = (item.get("ref") or {}).get("run")
        if kind == "cluster-shutdown" and run:
            out.add(f"{kind}:{run}")  # its mark holds the run id
    return out


def live_holders():
    """The holders whose job has not ended, as <kind>:<id>; None when the job
    list cannot be read."""
    return _live(_jobs())


def _busy(node, own, jobs):
    if jobs is None:
        return "a job Homestead could not read"
    for item in jobs:
        kind, ref = item.get("kind"), item.get("ref") or {}
        if (item.get("id") == own or kind not in JOB_KINDS.HOST_CORDON_KINDS or kind in BY_MARK_ONLY
                or item.get("status") in OPS.TERMINAL):
            continue
        resource = item.get("resource") or {}
        named = [ref.get("node"), *(n.get("node") if isinstance(n, dict) else n for n in ref.get("nodes") or [])]
        if resource.get("kind") == "Node":
            named.append(resource.get("name"))
        if (kind == "platform-upgrade" and ref.get("component") == "cluster") or node in named:
            return item.get("title") or kind
    return ""


def busy_with(node, own):
    """The first job not ended, of a kind that holds hosts, working on this
    host (a cluster upgrade: on every host), other than own - by its title -
    or "". Kinds in BY_MARK_ONLY hold a host only where a mark says so, which
    holders() reads. Fails closed: a job list that cannot be read says the
    host may be busy."""
    return _busy(node, own, _jobs())


def _path(name):
    return "/api/v1/nodes/" + name


def _pointer(key):
    return "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")


def _read(name):
    """A fresh read of the Node, or None once it no longer exists."""
    try:
        return KUBE.kget(_path(name))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _fence(node):
    meta = node["metadata"]
    return [{"op": "test", "path": "/metadata/uid", "value": meta.get("uid")},
            {"op": "test", "path": "/metadata/resourceVersion", "value": meta.get("resourceVersion")}]


def _marks(node, entries, drop):
    """Ops that make the cordon mark these holders (removed when none) and
    take away the older marks named in drop."""
    ops = [{"op": "remove", "path": _pointer(key)} for key in drop]
    if entries:
        value = ",".join(entries)
        if (node.get("metadata") or {}).get("annotations") is None:
            return [{"op": "add", "path": "/metadata/annotations", "value": {MARK: value}}]
        return ops + [{"op": "add", "path": _pointer(MARK), "value": value}]
    return ops + ([{"op": "remove", "path": _pointer(MARK)}] if MARK in _annotations(node) else [])


def _change(node, decide, doing, missing):
    """Read the Node fresh and write what decide(node) says, fenced, until it
    applies: decide returns an Outcome to stop, or (ops, Outcome on success).
    409 is a conflict: read again. 422 is a test op that failed - a conflict
    when the Node has changed since, else a refusal not worth asking again."""
    refused = None
    for _ in range(WRITE_ATTEMPTS):
        try:
            obj = _read(node)
        except Exception as error:
            return Outcome(False, node, f"Homestead could not read it ({str(error)[:80]})")
        if obj is None:
            return missing
        rv = obj["metadata"].get("resourceVersion")
        if refused and refused[0] == rv:
            return Outcome(False, node, f"Kubernetes refused {doing} it ({refused[1]})")
        step = decide(obj)
        if isinstance(step, Outcome):
            return step
        ops, success = step
        try:
            KUBE.ksend("PATCH", _path(node), _fence(obj) + ops, ctype="application/json-patch+json")
            return success
        except Exception as error:
            code = getattr(error, "code", None) if isinstance(error, urllib.error.HTTPError) else None
            if code == 409:
                continue
            if code == 422:
                refused = (rv, str(error)[:80])
                continue
            return Outcome(False, node, f"{doing} it failed ({str(error)[:80]})")
    return Outcome(False, node, f"it kept changing while Homestead was {doing} it")


def cordon(node, kind, job_id):
    """Cordon the host for this job and mark it as one of its holders. A host
    cordoned before - by a person (PERSON) or left by a job that ended (LEFT) -
    is left as it is, and the caller records that it was cordoned before.
    -> Outcome."""
    holder = f"{kind}:{job_id}"

    def decide(obj):
        is_cordoned = bool((obj.get("spec") or {}).get("unschedulable"))
        held = holders(obj)
        if is_cordoned and holder in held:
            return Outcome(True, node, "cordoned")  # done already: a reply lost, or asked twice
        if is_cordoned and any(_person(h) for h in held):
            return Outcome(False, node, PERSON)  # left for a person: no job takes it over
        if is_cordoned and not held:
            return Outcome(False, node, PERSON)
        if is_cordoned:
            live = live_holders()
            if live is None:
                return Outcome(False, node, UNREAD_JOBS)
            if not held & live:
                return Outcome(False, node, LEFT)
            entries = [h for h in _marked(obj) if h in live] + [holder]
            drop = [key for key, h in _old(obj).items() if h not in live]
        else:
            # On a host not cordoned, every mark left behind holds nothing.
            entries, drop = [holder], list(_old(obj))
        cordoning = ({"op": "add", "path": "/spec/unschedulable", "value": True} if "spec" in obj
                     else {"op": "add", "path": "/spec", "value": {"unschedulable": True}})
        return _marks(obj, entries, drop) + [cordoning], Outcome(True, node, "cordoned")

    return _change(node, decide, "cordoning", Outcome(False, node, MISSING))


def release(node, kind, job_id, uid=""):
    """Give the host back for this job: take it off the holders (with any
    stale ones) and, when no live holder is left and no other host-holding job
    is on the host, uncordon it. uid: the Node this job cordoned, when known;
    a Node by that name with another uid is a new host, with nothing of this
    job's on it. -> Outcome; done False with the reason while the host stays
    cordoned because of something else, or Homestead could not tell."""
    holder = f"{kind}:{job_id}"
    gone = Outcome(True, node, GONE)

    def decide(obj):
        if uid and obj["metadata"].get("uid") != uid:
            return gone
        if _person(holder):
            return Outcome(False, node, FOR_PERSON)  # only a person gives it back, on the Hosts page
        held = holders(obj)
        if holder not in held:
            return Outcome(True, node, "not held by this job")
        if any(_person(h) for h in held):
            # Left for a person: this job lets go, the cordon stays.
            entries = [h for h in _marked(obj) if h != holder]
            drop = [key for key, h in _old(obj).items() if h == holder]
            return _marks(obj, entries, drop), Outcome(False, node, FOR_PERSON)
        jobs = _jobs()
        live = _live(jobs)
        if live is None:
            return Outcome(False, node, UNREAD_JOBS)
        others = sorted((held - {holder}) & live)
        if not others:
            busy = _busy(node, job_id, jobs)
            if busy:
                # Kept marked: with no mark the cordon would read as a person's,
                # and nothing would ever give the host back.
                return Outcome(False, node, f"still held by {busy}")
        entries = [h for h in _marked(obj) if h != holder and h in live]
        drop = [key for key, h in _old(obj).items() if h == holder or h not in live]
        ops = _marks(obj, entries, drop)
        is_cordoned = bool((obj.get("spec") or {}).get("unschedulable"))
        if others:
            return ops, Outcome(False, node, f"still held by {others[0]}")
        if is_cordoned:
            ops.append({"op": "replace", "path": "/spec/unschedulable", "value": False})
        return ops, Outcome(True, node, "uncordoned" if is_cordoned else "released")

    return _change(node, decide, "releasing", gone)


def leave_for_person(node, kind, job_id):
    """A job ending with its host left cordoned for a person: swap its holder
    for person:<job id> in one fenced patch, so no later release - this
    job's, another holder's or anyone's - uncordons it; only a person does,
    on the Hosts page. A host not cordoned is left as it is. -> Outcome."""
    holder, person = f"{kind}:{job_id}", f"{PERSON_KIND}:{job_id}"

    def decide(obj):
        if not bool((obj.get("spec") or {}).get("unschedulable")):
            return Outcome(False, node, "not cordoned, so not left for a person")
        marked, old = _marked(obj), _old(obj)
        if person in marked and holder not in marked and holder not in old.values():
            return Outcome(True, node, FOR_PERSON)  # done already
        entries = []
        for h in [person if h == holder else h for h in marked] + [person]:
            if h not in entries:
                entries.append(h)
        drop = [key for key, h in old.items() if h == holder]
        return _marks(obj, entries, drop), Outcome(True, node, FOR_PERSON)

    return _change(node, decide, "leaving", Outcome(False, node, MISSING))
