"""Which Homestead does the work that must happen once.

Every replica answers the browser. Some work belongs to one at a time: the
loop that raises alerts and sends push notifications (two would send each
twice), and the engine that advances moves between clusters. The replicas
elect a leader with a Kubernetes Lease, as Kubernetes' own controllers do:
the leader renews it every few seconds, and if it stops - its node died - the
lease runs out and another replica takes it within about fifteen seconds.

A single Homestead simply holds the lease itself.
"""
import datetime
import os
import threading
import time
import urllib.error

kget = ksend = None
NS = "lab"
NAME = "homestead-leader"
IDENTITY = os.environ.get("HOSTNAME", "") or f"homestead-{os.getpid()}"
DURATION = 15
RENEW_EVERY = 5
_state = {"leader": False, "holder": "", "since": 0.0, "error": ""}
_lock = threading.Lock()


def bind(_kget, _ksend, namespace):
    global kget, ksend, NS
    kget, ksend, NS = _kget, _ksend, namespace


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _path(name=""):
    base = f"/apis/coordination.k8s.io/v1/namespaces/{NS}/leases"
    return f"{base}/{name}" if name else base


def attempt(now=None):
    """One round: take or keep the lease if it is ours to have. Returns
    whether this replica leads."""
    now = now or _now()
    spec = {"holderIdentity": IDENTITY, "leaseDurationSeconds": DURATION,
            "renewTime": _stamp(now)}
    try:
        lease = kget(_path(NAME))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        body = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                "metadata": {"name": NAME, "namespace": NS},
                "spec": {**spec, "acquireTime": _stamp(now), "leaseTransitions": 0}}
        try:
            ksend("POST", _path(), body)
        except urllib.error.HTTPError as conflict:
            if conflict.code == 409:
                return _set(False, "")
            raise
        return _set(True, IDENTITY)
    current = lease.get("spec", {}) or {}
    holder = current.get("holderIdentity") or ""
    renewed = _parse(current.get("renewTime")) or _parse(current.get("acquireTime"))
    duration = int(current.get("leaseDurationSeconds") or DURATION)
    expired = not holder or not renewed or (now - renewed).total_seconds() > duration
    if holder != IDENTITY and not expired:
        return _set(False, holder)
    lease["spec"] = {**current, **spec}
    if holder != IDENTITY:
        lease["spec"]["acquireTime"] = _stamp(now)
        lease["spec"]["leaseTransitions"] = int(current.get("leaseTransitions") or 0) + 1
    try:
        # resourceVersion is kept, so two replicas taking an expired lease at
        # once cannot both win: the second is refused with a conflict.
        ksend("PUT", _path(NAME), lease)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            return _set(False, holder)
        raise
    return _set(True, IDENTITY)


def _set(leading, holder):
    with _lock:
        if leading and not _state["leader"]:
            _state["since"] = time.time()
        _state.update(leader=leading, holder=holder, error="")
    return leading


def is_leader():
    return _state["leader"]


def status():
    with _lock:
        return {"identity": IDENTITY, "leader": _state["leader"], "holder": _state["holder"] or IDENTITY
                if _state["leader"] else _state["holder"], "error": _state["error"]}


def run():
    while True:
        try:
            attempt()
        except Exception as error:
            # Cannot reach the API: stop acting as leader rather than risk two.
            with _lock:
                _state.update(leader=False, error=str(error)[:160])
        time.sleep(RENEW_EVERY)


def release():
    """On shutdown: hand the lease over at once rather than after it expires."""
    if not _state["leader"]:
        return
    try:
        lease = kget(_path(NAME))
        if (lease.get("spec") or {}).get("holderIdentity") == IDENTITY:
            lease["spec"]["holderIdentity"] = ""
            ksend("PUT", _path(NAME), lease)
    except Exception:
        pass
    _set(False, "")
