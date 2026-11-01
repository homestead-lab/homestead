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

Every write tests the Node's uid and resourceVersion; a conflict is read
again and retried, up to WRITE_ATTEMPTS. A read that fails is never taken
as "free to uncordon"."""
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
# Kinds that work on every host at once, whatever host their job names.
EVERY_HOST = ("os-rollout", "cluster-shutdown", "longhorn-v2-cleanup")

PERSON = "cordoned by a person"
GONE = "host is gone"
MISSING = "no such host"


class Outcome(NamedTuple):
    done: bool
    node: str
    reason: str


def _entries(value):
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _holder(entry):
    return entry if ":" in entry else f"{BARE_MARK_KIND}:{entry}"


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


def holders(node):
    """Every job holding this host: from the cordon mark and the older marks."""
    found = set(_marked(node))
    for key, kind in OLD_MARKS.items():
        value = _annotations(node).get(key)
        if value:
            found.add(f"{kind}:{value}")
    return found


def busy_with(node, own):
    """The first job not ended, of a kind that holds hosts, working on this
    host (or on every host), other than own - by its title - or "". Fails
    closed: a job list that cannot be read says the host may be busy."""
    try:
        jobs = OPS.stored()
    except Exception:
        return "a job Homestead could not read"
    for item in jobs:
        kind, ref = item.get("kind"), item.get("ref") or {}
        if item.get("id") == own or kind not in JOB_KINDS.HOST_CORDON_KINDS or item.get("status") in OPS.TERMINAL:
            continue
        resource = item.get("resource") or {}
        named = [ref.get("node"), *(n.get("node") if isinstance(n, dict) else n for n in ref.get("nodes") or [])]
        if resource.get("kind") == "Node":
            named.append(resource.get("name"))
        every = kind in EVERY_HOST or (kind == "platform-upgrade" and ref.get("component") == "cluster")
        if every or node in named:
            return item.get("title") or kind
    return ""


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


def _set_mark(node, entries):
    """Ops that make the cordon mark these holders (removed when none)."""
    present = MARK in _annotations(node)
    if entries:
        value = ",".join(entries)
        if (node.get("metadata") or {}).get("annotations") is None:
            return [{"op": "add", "path": "/metadata/annotations", "value": {MARK: value}}]
        return [{"op": "add", "path": _pointer(MARK), "value": value}]
    return [{"op": "remove", "path": _pointer(MARK)}] if present else []


def _retry(error):
    # 409: changed under us; 422: a test op failed, so it changed since the read.
    return isinstance(error, urllib.error.HTTPError) and error.code in (409, 422)


def _patch(name, ops):
    KUBE.ksend("PATCH", _path(name), ops, ctype="application/json-patch+json")


def cordon(node, kind, job_id):
    """Cordon the host for this job and mark it as one of its holders.
    A host a person cordoned is left to them: Outcome(False, ..., PERSON),
    and the caller records that it was cordoned before. -> Outcome."""
    holder = f"{kind}:{job_id}"
    for _ in range(WRITE_ATTEMPTS):
        try:
            obj = _read(node)
        except Exception as error:
            return Outcome(False, node, f"Homestead could not read it ({str(error)[:80]})")
        if obj is None:
            return Outcome(False, node, MISSING)
        is_cordoned = bool((obj.get("spec") or {}).get("unschedulable"))
        held = holders(obj)
        if is_cordoned and holder in held:
            return Outcome(True, node, "cordoned")  # done already: a reply lost, or asked twice
        if is_cordoned and not held:
            return Outcome(False, node, PERSON)
        # On a host not cordoned, a mark left behind holds nothing: start it afresh.
        ops = _fence(obj) + _set_mark(obj, (_marked(obj) if is_cordoned else []) + [holder]) + [
            {"op": "add", "path": "/spec/unschedulable", "value": True} if "spec" in obj
            else {"op": "add", "path": "/spec", "value": {"unschedulable": True}}]
        try:
            _patch(node, ops)
            return Outcome(True, node, "cordoned")
        except Exception as error:
            if not _retry(error):
                return Outcome(False, node, f"cordoning it failed ({str(error)[:80]})")
    return Outcome(False, node, "it kept changing while Homestead cordoned it")


def release(node, kind, job_id, uid=""):
    """Give the host back for this job: take it off the holders and, when it
    was the last and no other host-holding job is on the host, uncordon it.
    uid: the Node this job cordoned, when known; a Node by that name with
    another uid is a new host, so nothing of this job's is there.
    -> Outcome; done False with the reason while the host stays cordoned
    because of something else, or Homestead could not tell."""
    holder = f"{kind}:{job_id}"
    for _ in range(WRITE_ATTEMPTS):
        try:
            obj = _read(node)
        except Exception as error:
            return Outcome(False, node, f"Homestead could not read it ({str(error)[:80]})")
        if obj is None or (uid and obj["metadata"].get("uid") != uid):
            return Outcome(True, node, GONE)
        held = holders(obj)
        if holder not in held:
            return Outcome(True, node, "not held by this job")
        old = [key for key, k in OLD_MARKS.items() if f"{k}:{_annotations(obj).get(key)}" == holder]
        others = sorted(held - {holder})
        busy = "" if others else busy_with(node, job_id)
        if busy:
            # Kept marked: with no mark the cordon would read as a person's,
            # and nothing would ever give the host back.
            return Outcome(False, node, f"still held by {busy}")
        ops = _fence(obj) + _set_mark(obj, [h for h in _marked(obj) if h != holder]) + [
            {"op": "remove", "path": _pointer(key)} for key in old]
        is_cordoned = bool((obj.get("spec") or {}).get("unschedulable"))
        if is_cordoned and not others:
            ops.append({"op": "replace", "path": "/spec/unschedulable", "value": False})
        try:
            _patch(node, ops)
        except Exception as error:
            if _retry(error):
                continue
            return Outcome(False, node, f"releasing it failed ({str(error)[:80]})")
        if others:
            return Outcome(False, node, f"still held by {others[0]}")
        return Outcome(True, node, "uncordoned" if is_cordoned else "released")
    return Outcome(False, node, "it kept changing while Homestead released it")
