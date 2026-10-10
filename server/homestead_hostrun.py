"""One checked script on a node's host, and its output.

Some fixes can only be made on the machine itself - a file k3s reads at
start, a disk to format and mount. Kubernetes reaches a node only through a
pod, so this is the node shell's helper (homestead_nodeshell) in miniature:
a privileged Alpine pod pinned to the node, sharing the host's namespaces,
that runs one script through nsenter into PID 1 - the host as SSH gives it -
and is deleted when the script ends. Only code in Homestead calls it, with
scripts written here, never text from a request.
"""
import time
import urllib.error

import homestead_names as NAMES

kget = ksend = None
exec_in = None           # (namespace, pod, argv, timeout=, container=) -> (stdout bytes, stderr text)
NS = "lab"
IMAGE = "alpine:3.24"
# nsenter leaves the script in this pod's cgroup, so what it runs on the host -
# apt-get refreshing its lists, mkfs, update-initramfs - shares this limit. At
# 64Mi the kernel killed apt-get part-way through reading a host's OS.
MEMORY_LIMIT = "1Gi"
TASK = "host-run"
HOST = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c"]
# A refused exec (an error with retry set) is tried this many times, this far
# apart: about half a minute for the API server to reach a host just back.
EXEC_TRIES = 6
EXEC_PAUSE = 5


def bind(_kget, _ksend, _exec_in, namespace):
    global kget, ksend, exec_in, NS
    kget, ksend, exec_in, NS = _kget, _ksend, _exec_in, namespace


def pod_name(node):
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in node.lower()).strip("-")
    return f"homestead-host-{safe}"[:63].rstrip("-")


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def body(node, lifetime=900):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name(node), "namespace": NS,
                         "labels": {**NAMES.labels(TASK), NAMES.key("node"): node[:63]}},
            "spec": {"nodeName": node, "hostPID": True, "hostIPC": True, "hostNetwork": True,
                     "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                     "activeDeadlineSeconds": lifetime, "tolerations": [{"operator": "Exists"}],
                     "containers": [{"name": "host", "image": IMAGE,
                                     "command": ["sh", "-c", f"trap 'exit 0' TERM; sleep {lifetime} & wait"],
                                     "securityContext": {"privileged": True},
                                     "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                                   "limits": {"memory": MEMORY_LIMIT}}}]}}


def _delete(path):
    try:
        ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
    except urllib.error.HTTPError:
        pass


def run(node, script, timeout=120, wait=60, sleep=time.sleep):
    """Run script as root on node's host; (stdout text, stderr text). The
    helper is made for this run and removed after it."""
    path = f"/api/v1/namespaces/{NS}/pods/{pod_name(node)}"
    if _get(path):
        # One left from a run that did not finish.
        _delete(path)
        for _ in range(20):
            if not _get(path):
                break
            sleep(0.5)
    ksend("POST", f"/api/v1/namespaces/{NS}/pods", body(node, lifetime=max(300, timeout + wait + 60)))
    try:
        for _ in range(int(wait * 2)):
            status = (_get(path) or {}).get("status") or {}
            if status.get("phase") == "Running":
                break
            waiting = next((((c.get("state") or {}).get("waiting") or {}) for c in status.get("containerStatuses") or []
                            if (c.get("state") or {}).get("waiting")), {})
            if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff"):
                raise ValueError(f"{node} cannot pull {IMAGE}: {waiting.get('message', '')[:200]}")
            sleep(0.5)
        else:
            raise ValueError(f"the host helper on {node} did not start within {wait} seconds")
        # A host just back can show its helper Running before the API server
        # reaches its kubelet: the exec is refused and nothing has run, so it
        # is tried again for a while (seen on RKE2 after a host failed).
        for attempt in range(EXEC_TRIES):
            try:
                out, err = exec_in(NS, pod_name(node), HOST + [script], timeout=timeout, container="host")
                return out.decode("utf-8", "replace"), err
            except ConnectionError as error:
                if not getattr(error, "retry", False) or attempt == EXEC_TRIES - 1:
                    raise
                sleep(EXEC_PAUSE)
    finally:
        _delete(path)
