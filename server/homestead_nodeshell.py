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
restarted with a terminal open. The helper carries the time it may run
until (UNTIL): CONNECT_SECONDS from when it is prepared, and IDLE_SECONDS
from each beat of a process that serves a session on it. The leader's
sweep() removes a helper past that time, and one that has ended.
"""
import calendar
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
UNTIL = NAMES.key("shell-until")
PREFIX = "homestead-shell-"
clock = time.time
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
    elif _until(pod) < clock() + CONNECT_SECONDS:
        # Kept for the browser that is about to connect to it.
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


def _until(pod):
    """The time a helper may run until: its UNTIL, or for one made before
    there was one, CONNECT_SECONDS after it was made. Unreadable is now."""
    meta = pod.get("metadata") or {}
    try:
        return float((meta.get("annotations") or {})[UNTIL])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return calendar.timegm(time.strptime(meta["creationTimestamp"], "%Y-%m-%dT%H:%M:%SZ")) + CONNECT_SECONDS
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0


def _extend(node, until):
    try:
        ksend("PATCH", f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}",
              {"metadata": {"annotations": {UNTIL: str(int(until))}}}, ctype="application/merge-patch+json")
        return True
    except Exception:
        return False   # the next beat tries again; IDLE_SECONDS allows for it


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


def sweep():
    """On the leader: remove each helper that has ended, or whose time is up
    because nobody is connected - one prepared for a browser that never came,
    or one whose sessions were in a process that has gone. A helper this
    process serves a session on stays. Deletes are fenced to the pod seen."""
    selector = urllib.parse.quote(f"{NAMES.key('task')}={TASK}")
    pods = kget(f"/api/v1/namespaces/{NS}/pods?labelSelector={selector}").get("items") or []
    with _lock:
        serving = {node for node, count in _sessions.items() if count > 0}
    removed = []
    for pod in pods:
        meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
        name = meta.get("name") or ""
        if (not name.startswith(PREFIX) or not meta.get("uid") or meta.get("deletionTimestamp")
                or NAMES.label_of(meta, "task") != TASK or spec.get("nodeName") in serving):
            continue
        ended = (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed")
        if not ended and clock() < _until(pod):
            continue
        preconditions = {"uid": meta["uid"]}
        if meta.get("resourceVersion"):
            preconditions["resourceVersion"] = meta["resourceVersion"]
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{name}",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0,
                   "preconditions": preconditions})
        except urllib.error.HTTPError as error:
            if error.code in (404, 409):   # gone, or beaten or replaced since it was read
                continue
            raise
        removed.append(name)
    return removed


def session_started(node):
    with _lock:
        _sessions[node] = _sessions.get(node, 0) + 1
    _extend(node, clock() + IDLE_SECONDS)
    _start_beater()


def session_ended(node):
    """The last session on a node closed: its helper goes."""
    with _lock:
        _sessions[node] = max(0, _sessions.get(node, 0) - 1)
        last = _sessions[node] == 0
    if last:
        try:
            pod = KUBE.get_optional(f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}", read=kget)
        except Exception:
            return   # its deadline still ends it
        if not pod or (pod.get("spec") or {}).get("nodeName") != node:
            return
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
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
