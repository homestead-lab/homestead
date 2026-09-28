"""A shell on a node itself, from the browser.

Kubernetes reaches a node only through a pod on it. So a shell is a small
privileged helper pod pinned to the node - busybox, which carries nsenter -
sharing the host's process, network and IPC namespaces; the console then
runs nsenter into PID 1's namespaces, which is the host as SSH would give
it: its own files, its tools, its login shell.

One helper serves every open session on a node. It sleeps for at most
eight hours, and is deleted as soon as its last session closes. Only an
admin opens one, and each session is written to the console audit log with
the node's name.
"""
import threading
import time
import urllib.error

import homestead_names as NAMES

kget = ksend = None
NS = "lab"
IMAGE = "busybox"
LIFETIME = 8 * 3600
TASK = "node-shell"
_sessions, _lock = {}, threading.Lock()
# Into the host's namespaces, then its own login shell: bash where there is
# one, else sh.
HOST_SHELL = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c",
              "export TERM=xterm-256color; cd ~ 2>/dev/null; command -v bash >/dev/null && exec bash -l || exec sh -l"]


def bind(_kget, _ksend, namespace):
    global kget, ksend, NS
    kget, ksend, NS = _kget, _ksend, namespace


def pod_name(node):
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in node.lower()).strip("-")
    return f"homestead-shell-{safe}"[:63].rstrip("-")


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def body(node):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name(node), "namespace": NS,
                         "labels": {**NAMES.labels(TASK), NAMES.key("node"): node[:63]}},
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
    found = _get(f"/api/v1/nodes/{node}")
    if not found:
        raise ValueError(f"there is no node {node}")
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (found.get("status") or {}).get("conditions") or [])
    if not ready:
        raise ValueError(f"{node} is not Ready, so nothing can run on it to open a shell")
    path = f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}"
    pod = _get(path)
    phase = ((pod or {}).get("status") or {}).get("phase")
    if pod and (phase in ("Succeeded", "Failed") or (pod.get("metadata") or {}).get("deletionTimestamp")):
        # An old helper that has run its time: a fresh one.
        try:
            ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
        except urllib.error.HTTPError:
            pass
        for _ in range(20):
            if not _get(path):
                break
            sleep(0.5)
        pod = None
    if not pod:
        ksend("POST", f"/api/v1/namespaces/{NS}/pods", body(node))
    for _ in range(int(wait * 2)):
        pod = _get(path) or {}
        status = pod.get("status") or {}
        if status.get("phase") == "Running":
            return {"node": node, "namespace": NS, "pod": pod_name(node), "container": "shell", "command": HOST_SHELL}
        waiting = next((((c.get("state") or {}).get("waiting") or {}) for c in status.get("containerStatuses") or []
                        if (c.get("state") or {}).get("waiting")), {})
        if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff"):
            raise ValueError(f"{node} cannot pull {IMAGE} for the shell: {waiting.get('message', '')[:200]}")
        sleep(0.5)
    raise ValueError(f"the shell helper on {node} did not start within {wait} seconds")


def session_started(node):
    with _lock:
        _sessions[node] = _sessions.get(node, 0) + 1


def session_ended(node):
    """The last session on a node closed: its helper goes."""
    with _lock:
        _sessions[node] = max(0, _sessions.get(node, 0) - 1)
        last = _sessions[node] == 0
    if last:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
        except urllib.error.HTTPError:
            pass
