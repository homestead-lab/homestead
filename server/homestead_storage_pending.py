"""A node that has just joined is steered clear of until its storage is ready.

A node is Ready as soon as its kubelet is, minutes before Longhorn has
started on it and registered its CSI driver there. A pod with a Longhorn
volume scheduled in that gap cannot attach it ("CSINode ... does not contain
driver driver.longhorn.io"), and waits - Homestead's own update once sat
Pending on a node that had joined a minute earlier.

So a new node carries a PreferNoSchedule taint until driver.longhorn.io is
registered on it. Only a preference: while other nodes have room, pods go
there instead, and Longhorn's own DaemonSet pods - which could never tolerate
a taint of Homestead's - still start. The installer's join gives the taint at
registration, leaving no gap at all; tick() gives it to a node that joined
another way, and takes it off every node once its driver is there, or when the
cluster has no Longhorn to wait for.
"""
import calendar
import time
import urllib.error

kget = ksend = None
KEY = "homestead.io/storage-pending"
EFFECT = "PreferNoSchedule"
DRIVER = "driver.longhorn.io"
NEW_FOR = 20 * 60          # how long after joining a node counts as new


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


def _optional(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _age(stamp, now):
    try:
        return now - calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return float("inf")


def registered(name):
    """Whether Longhorn's CSI driver is registered on the node yet."""
    csinode = _optional(f"/apis/storage.k8s.io/v1/csinodes/{name}") or {}
    return any(d.get("name") == DRIVER for d in (csinode.get("spec") or {}).get("drivers") or [])


def wanted(node, longhorn, now):
    """True to add the taint, False to take it off, None to leave it."""
    meta = node.get("metadata") or {}
    tainted = any(t.get("key") == KEY for t in (node.get("spec") or {}).get("taints") or [])
    if not longhorn:
        return False if tainted else None
    ready = registered(meta.get("name", ""))
    if tainted:
        return False if ready else None
    if not ready and _age(meta.get("creationTimestamp"), now) < NEW_FOR:
        return True
    return None


def _set(node, add):
    meta = node["metadata"]
    taints = [t for t in (node.get("spec") or {}).get("taints") or [] if t.get("key") != KEY]
    if add:
        taints.append({"key": KEY, "value": "longhorn", "effect": EFFECT})
    # Guarded by the version read, so a taint Kubernetes adds meanwhile is
    # never overwritten: the patch fails, and the next pass reads again.
    ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta.get("resourceVersion", "")}]
    ops.append({"op": "add" if "taints" not in (node.get("spec") or {}) else "replace", "path": "/spec/taints", "value": taints})
    ksend("PATCH", f"/api/v1/nodes/{meta['name']}", ops, ctype="application/json-patch+json")


def tick(now=None):
    """Taint new nodes whose storage is not ready, untaint ready ones.
    Returns [(node, what was done)] for the log."""
    now = now or time.time()
    longhorn = _optional(f"/apis/storage.k8s.io/v1/csidrivers/{DRIVER}") is not None
    done = []
    for node in kget("/api/v1/nodes").get("items", []):
        change = wanted(node, longhorn, now)
        if change is None:
            continue
        try:
            _set(node, change)
            done.append((node["metadata"]["name"], "steered clear of until Longhorn is ready there" if change
                         else "Longhorn ready: takes pods again"))
        except urllib.error.HTTPError as error:
            if error.code not in (409, 422):
                raise
    return done
