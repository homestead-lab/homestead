"""A shell on a node itself, from the browser.

Kubernetes reaches a node only through a pod on it. So a shell is a small
privileged helper pod pinned to the node - Alpine, which carries nsenter -
sharing the host's process, network and IPC namespaces; the console then
runs nsenter into PID 1's namespaces, which is the host as SSH would give
it: its own files, its tools, its login shell.

One helper serves every open session on a node. It sleeps for at most
eight hours, and is deleted as soon as its last session closes. Only an
admin opens one, and each session is written to the console audit log with
the node's name.

It also ends when nobody is connected, even if no session ever closed in
this process: the browser left before the terminal connected, or Homestead
restarted with a terminal open. A new helper carries the time it may run
until (UNTIL): CONNECT_SECONDS after it was prepared. Each Homestead process
that serves a session on it, or is about to, keeps its own "until" in one
ConfigMap beside it (BEATS: a key per helper and process), IDLE_SECONDS
ahead at each beat. The leader's sweep() removes a helper whose latest
"until" has passed, by more than SKEW_SECONDS for clocks that differ, and
one that has ended. A helper made by the release before, which nobody
here beats, is given LEGACY_SECONDS, so a rolling upgrade does not cut a
terminal an old replica still serves.
"""
import calendar
import os
import threading
import time
import urllib.error
import urllib.parse

import homestead_kube as KUBE
import homestead_names as NAMES

kget = ksend = None
NS = "lab"
IMAGE = "alpine:3.24"
LIFETIME = 8 * 3600
TASK = "node-shell"
# The browser connects within CONNECT_SECONDS of prepare(); a process with a
# session open on the helper beats every BEAT_SECONDS, and a helper with no
# beat for IDLE_SECONDS has nobody connected.
CONNECT_SECONDS = 60
BEAT_SECONDS = 20
IDLE_SECONDS = 60
SKEW_SECONDS = 30
LEGACY_SECONDS = 600
UNTIL = NAMES.key("shell-until")
PREFIX = "homestead-shell-"
REPLICA = os.environ.get("HOSTNAME") or "homestead"
clock = time.time
_started = time.time()
_sessions, _lock = {}, threading.Lock()
_beater = None
# Into the host's namespaces, then its own login shell: bash where there is
# one, else sh.
HOST_SHELL = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c",
              "export TERM=xterm-256color; cd ~ 2>/dev/null; command -v bash >/dev/null && exec bash -l || exec sh -l"]


def bind(_kget, _ksend, namespace):
    global kget, ksend, NS
    kget, ksend, NS = _kget, _ksend, namespace


def pod_name(node):
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in node.lower()).strip("-")
    return NAMES.fitted(f"{PREFIX}{safe}")


def body(node, until=None):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name(node), "namespace": NS,
                         "labels": {**NAMES.labels(TASK), NAMES.key("node"): node[:63]},
                         "annotations": {UNTIL: str(int(clock() + CONNECT_SECONDS if until is None else until))}},
            "spec": {"nodeName": node, "hostPID": True, "hostIPC": True, "hostNetwork": True,
                     "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                     "activeDeadlineSeconds": LIFETIME,
                     "tolerations": [{"operator": "Exists"}],
                     "containers": [{"name": "shell", "image": IMAGE, "command": ["sh", "-c", f"trap 'exit 0' TERM; sleep {LIFETIME} & wait"],
                                     "securityContext": {"privileged": True},
                                     "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                                   "limits": {"memory": "64Mi"}}}]}}


def open_shell(node, wait=60, sleep=time.sleep):
    """The helper for a node, made if need be, once it runs: what the
    console connects to."""
    found = KUBE.get_optional(f"/api/v1/nodes/{node}", read=kget)
    if not found:
        raise ValueError(f"there is no node {node}")
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (found.get("status") or {}).get("conditions") or [])
    if not ready:
        raise ValueError(f"{node} is not Ready, so nothing can run on it to open a shell")
    path = f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}"
    pod = KUBE.get_optional(path, read=kget)
    if pod and (pod.get("spec") or {}).get("nodeName") != node:
        # Never a shell on another host than the one asked for.
        raise ValueError(f"the shell helper for {node} is in use for {(pod.get('spec') or {}).get('nodeName')}; "
                         "close that node's terminal and try again")
    phase = ((pod or {}).get("status") or {}).get("phase")
    if pod and (phase in ("Succeeded", "Failed") or (pod.get("metadata") or {}).get("deletionTimestamp")):
        # An old helper that has run its time: a fresh one.
        try:
            ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
        except urllib.error.HTTPError:
            pass
        for _ in range(20):
            if not KUBE.get_optional(path, read=kget):
                break
            sleep(0.5)
        pod = None
    if not pod:
        ksend("POST", f"/api/v1/namespaces/{NS}/pods", body(node))
    else:
        # Kept for the browser that is about to connect to it.
        mine = ((_records() or {}).get(pod_name(node)) or {}).get(REPLICA, 0)
        if mine < clock() + CONNECT_SECONDS:
            _extend(node, clock() + CONNECT_SECONDS)
    for _ in range(int(wait * 2)):
        pod = KUBE.get_optional(path, read=kget) or {}
        status = pod.get("status") or {}
        if status.get("phase") == "Running":
            return {"node": node, "namespace": NS, "pod": pod_name(node), "container": "shell", "command": HOST_SHELL}
        waiting = next((((c.get("state") or {}).get("waiting") or {}) for c in status.get("containerStatuses") or []
                        if (c.get("state") or {}).get("waiting")), {})
        if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff"):
            raise ValueError(f"{node} cannot pull {IMAGE} for the shell: {waiting.get('message', '')[:200]}")
        sleep(0.5)
    raise ValueError(f"the shell helper on {node} did not start within {wait} seconds")


def _beats_path():
    return f"/api/v1/namespaces/{NS}/configmaps/{NAMES.object_name('shell-sessions')}"


def _key(node, replica=None):
    return f"{pod_name(node)}.{replica or REPLICA}"


def _records():
    """Every process's "until" for each helper: {pod: {replica: until}}.
    None when there is no ConfigMap yet."""
    found = KUBE.get_optional(_beats_path(), read=kget)
    if found is None:
        return None
    out = {}
    for key, value in ((found.get("data") or {}).items()):
        pod, _, replica = key.partition(".")   # helper names have no dot
        try:
            out.setdefault(pod, {})[replica] = float(value)
        except (TypeError, ValueError):
            out.setdefault(pod, {})[replica] = 0
    return out


def _write(changes):
    """Merge changes ({key: until, or None to drop it}) into the ConfigMap."""
    path = _beats_path()
    patch = {"data": {key: (None if value is None else str(int(value))) for key, value in changes.items()}}
    try:
        ksend("PATCH", path, patch, ctype="application/merge-patch+json")
        return
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    kept = {key: value for key, value in patch["data"].items() if value is not None}
    if not kept:
        return
    try:
        ksend("POST", path.rsplit("/", 1)[0], {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": path.rsplit("/", 1)[1], "namespace": NS, "labels": NAMES.labels(TASK)},
            "data": kept})
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
        ksend("PATCH", path, patch, ctype="application/merge-patch+json")   # another process made it first


def _extend(node, until):
    try:
        _write({_key(node): until})
        return True
    except Exception as error:
        # The next beat tries again; IDLE_SECONDS allows for one that is missed.
        print(f"host shell on {node}: still in use, but that could not be recorded: {str(error)[:160]}", flush=True)
        return False


def _created(pod):
    try:
        return calendar.timegm(time.strptime((pod.get("metadata") or {})["creationTimestamp"], "%Y-%m-%dT%H:%M:%SZ"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _until(pod, records):
    """The time a helper may run until: the latest any process recorded for
    it, else its own UNTIL. A helper nobody here records - made by the
    release before, or served by an old replica in a rolling upgrade, which
    does not beat - is given LEGACY_SECONDS from when it was made: always
    when it has no UNTIL, and in this process's first LEGACY_SECONDS when
    it has one."""
    meta = pod.get("metadata") or {}
    beats = (records or {}).get(meta.get("name") or "")
    if beats:
        return max(beats.values())
    try:
        own = float((meta.get("annotations") or {})[UNTIL])
    except (KeyError, TypeError, ValueError):
        own = None
    created = _created(pod)
    legacy = None if created is None else created + LEGACY_SECONDS
    if own is None:
        return legacy if legacy is not None else 0
    if legacy is not None and clock() - _started < LEGACY_SECONDS:
        return max(own, legacy)
    return own


def _past(until):
    return clock() >= until + SKEW_SECONDS


def beat():
    """Each helper this process serves a session on: still in use."""
    with _lock:
        nodes = [node for node, count in _sessions.items() if count > 0]
    for node in nodes:
        _extend(node, clock() + IDLE_SECONDS)
    return nodes


def _beat_loop(sleep=time.sleep):
    """Beats while this process has a session open, then stops."""
    global _beater
    while True:
        sleep(BEAT_SECONDS)
        with _lock:
            if not any(count > 0 for count in _sessions.values()):
                _beater = None
                return
        beat()


def _start_beater():
    global _beater
    with _lock:
        if _beater is not None and _beater.is_alive():
            return
        _beater = threading.Thread(target=_beat_loop, name="node-shell-beat", daemon=True)
        _beater.start()


def _delete(pod):
    """Delete the helper as it was read; False when it is gone or changed."""
    meta = pod.get("metadata") or {}
    preconditions = {"uid": meta["uid"]}
    if meta.get("resourceVersion"):
        preconditions["resourceVersion"] = meta["resourceVersion"]
    try:
        ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{meta['name']}",
              {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0,
               "preconditions": preconditions})
    except urllib.error.HTTPError as error:
        if error.code in (404, 409):   # gone, or replaced since it was read
            return False
        raise
    return True


def sweep():
    """On the leader: remove each helper that has ended, or whose time is up
    because nobody is connected - one prepared for a browser that never came,
    or one whose sessions were in a process that has gone - and the records
    of helpers that are gone. A helper this process serves a session on
    stays. Deletes are fenced to the pod seen."""
    selector = urllib.parse.quote(f"{NAMES.key('task')}={TASK}")
    pods = KUBE.items(f"/api/v1/namespaces/{NS}/pods?labelSelector={selector}", read=kget)
    records = _records() or {}
    with _lock:
        serving = {node for node, count in _sessions.items() if count > 0}
    removed, present = [], set()
    for pod in pods:
        meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
        name = meta.get("name") or ""
        if not name.startswith(PREFIX) or NAMES.label_of(meta, "task") != TASK:
            continue
        present.add(name)
        if not meta.get("uid") or meta.get("deletionTimestamp") or spec.get("nodeName") in serving:
            continue
        ended = (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed")
        if not ended and not _past(_until(pod, records)):
            continue
        if _delete(pod):
            removed.append(name)
            present.discard(name)
    stale = {f"{pod}.{replica}": None for pod, beats in records.items() if pod not in present for replica in beats}
    if stale:
        _write(stale)
    return removed


def session_started(node):
    with _lock:
        _sessions[node] = _sessions.get(node, 0) + 1
    _extend(node, clock() + IDLE_SECONDS)
    _start_beater()


def session_ended(node):
    """The last session on a node closed here: its helper goes, unless
    another process still records a session on it."""
    with _lock:
        _sessions[node] = max(0, _sessions.get(node, 0) - 1)
        last = _sessions[node] == 0
    if not last:
        return
    try:
        pod = KUBE.get_optional(f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}", read=kget)
        if not pod or (pod.get("spec") or {}).get("nodeName") != node:
            return   # gone: the sweep drops its records
        _write({_key(node): None})
        records = _records() or {}
    except Exception:
        return   # the sweep, or its deadline, still ends it
    others = records.get(pod_name(node)) or {}
    if any(not _past(until) for until in others.values()):
        return   # another replica serves a terminal on it
    try:
        if _delete(pod):
            _write({_key(node, replica): None for replica in others})
    except urllib.error.HTTPError:
        pass


def prepare(node):
    """Start the node's helper and say plainly if it cannot, before the
    terminal connects - a refused WebSocket says nothing."""
    target = open_shell(node)
    return {"ok": True, "node": target["node"]}


# Its routes and who may use them (homestead_routes.py). The shell itself is
# a WebSocket, which server.py answers.
ROUTES = {
    ("POST", "/api/node/shell/prepare"): ("admin", lambda request: prepare(str(request.body.get("node") or ""))),
}
