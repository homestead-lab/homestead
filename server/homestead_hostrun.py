"""One checked script on a node's host, and its output.

Some fixes can only be made on the machine itself - a file k3s reads at
start, a disk to format and mount. Kubernetes reaches a node only through a
pod, so this is the node shell's helper (homestead_nodeshell) in miniature:
a privileged Alpine pod pinned to the node, sharing the host's namespaces,
that runs one script through nsenter into PID 1 - the host as SSH gives it -
and is deleted when the script ends. Only code in Homestead calls it, with
scripts written here, never text from a request.

A run that changes a host - a disk prepared, its network or OS changed -
first takes that host's Lease (coordination.k8s.io), so two changes never
run on one host at once: the second is refused, saying what holds it. A
read takes nothing and is never held back. The lease is renewed while the
run lasts and given back when it ends; one left by a replica that stopped
runs out and is taken over (#597).
"""
import datetime
import hashlib
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse

import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_pod_exec as POD_EXEC

kget = ksend = None
# (namespace, pod, argv, timeout=, container=) -> (stdout bytes, stderr text);
# server.py binds homestead_pod_exec.run, whose answer also carries exit_code.
exec_in = None
NS = "lab"
IMAGE = "alpine:3.24"
# nsenter leaves the script in this pod's cgroup, so what it runs on the host -
# apt-get refreshing its lists, mkfs, update-initramfs - shares this limit. At
# 64Mi the kernel killed apt-get part-way through reading a host's OS.
MEMORY_LIMIT = "1Gi"
TASK = "host-run"
HOST = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c"]
# A refused exec (an error with retry set) is tried this many times, this far
# apart: a minute and a half for the API server to reach a host just back. On
# RKE2 it reaches a kubelet through the host's agent tunnel, which reconnects
# well after the host reports Ready; half a minute was not enough (e2e, bonds).
EXEC_TRIES = 18
EXEC_PAUSE = 5
# A change's lease runs out this long after its last renewal, renewed this
# often while its script runs: a replica that stops frees the host within a
# minute. A hold over several runs (hold()) sets its own length.
LEASE_SECONDS = 60
RENEW_EVERY = 20
IDENTITY = os.environ.get("HOSTNAME", "") or f"homestead-{os.getpid()}"


class Busy(ValueError):
    """Another change holds the host's lease."""


def bind(_kget, _ksend, _exec_in, namespace):
    global kget, ksend, exec_in, NS
    kget, ksend, exec_in, NS = _kget, _ksend, _exec_in, namespace


def _safe(node):
    return "".join(c if c.isalnum() or c == "-" else "-" for c in node.lower()).strip("-")


def pod_name(node):
    """A helper's name: the node it runs on, and a part of its own, so two
    runs on one host never share (or delete) one helper (#597)."""
    return f"homestead-host-{_safe(node)}"[:56].rstrip("-") + "-" + secrets.token_hex(3)


def lease_name(node):
    """The one lease a host's changes take turns with. A part of the name's
    hash keeps two long names that differ only past the cut apart."""
    digest = hashlib.sha256(node.encode()).hexdigest()[:6]
    return f"homestead-host-change-{_safe(node)}"[:56].rstrip("-") + "-" + digest


def _lease_path(node=""):
    base = f"/apis/coordination.k8s.io/v1/namespaces/{NS}/leases"
    return f"{base}/{lease_name(node)}" if node else base


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def hold(node, what, seconds=LEASE_SECONDS, holder=""):
    """Take node's change lease for what, or renew it when holder has it
    already (keeping the length it was taken for). Returns the holder;
    raises Busy when another change holds it and it has not run out."""
    holder = holder or f"{IDENTITY}/{secrets.token_hex(4)}"
    now = datetime.datetime.now(datetime.timezone.utc)
    lease = KUBE.get_optional(_lease_path(node), read=kget)
    if lease is None:
        body = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                "metadata": {"name": lease_name(node), "namespace": NS,
                             "labels": {**NAMES.labels(TASK), NAMES.key("node"): node[:63].rstrip("-._")},
                             "annotations": {NAMES.key("change"): what}},
                "spec": {"holderIdentity": holder, "leaseDurationSeconds": int(seconds),
                         "acquireTime": _stamp(now), "renewTime": _stamp(now), "leaseTransitions": 0}}
        try:
            ksend("POST", _lease_path(), body)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise Busy(f"another change is starting on {node}: try again in a moment") from None
            raise
        return holder
    spec = lease.get("spec") or {}
    current = spec.get("holderIdentity") or ""
    renewed = _parse(spec.get("renewTime")) or _parse(spec.get("acquireTime"))
    expired = not current or not renewed or (now - renewed).total_seconds() > int(spec.get("leaseDurationSeconds") or 0)
    meta = lease.setdefault("metadata", {})
    if current != holder:
        if not expired:
            raise Busy(f"another change is running on {node}: "
                       f"{NAMES.annotation_of(meta, 'change') or 'started by ' + current.split('/')[0]}; "
                       "try again once it has finished")
        spec.update(holderIdentity=holder, leaseDurationSeconds=int(seconds), acquireTime=_stamp(now),
                    leaseTransitions=int(spec.get("leaseTransitions") or 0) + 1)
        meta.setdefault("annotations", {})[NAMES.key("change")] = what
    spec["renewTime"] = _stamp(now)
    lease["spec"] = spec
    try:
        # resourceVersion is kept, so two runs taking a lease that ran out at
        # once cannot both have it: the second is refused with a conflict.
        ksend("PUT", _lease_path(node), lease)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise Busy(f"another change is starting on {node}: try again in a moment") from None
        raise
    return holder


def let_go(node, holder):
    """Give node's change lease back, if holder still has it. Never raises:
    a lease not given back runs out by itself."""
    try:
        lease = KUBE.get_optional(_lease_path(node), read=kget)
        if lease and (lease.get("spec") or {}).get("holderIdentity") == holder:
            lease["spec"]["holderIdentity"] = ""
            ksend("PUT", _lease_path(node), lease)
    except Exception:
        pass


def body(node, lifetime=900, name=""):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name or pod_name(node), "namespace": NS,
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


def _clear_ended():
    """Helpers of runs that ended without removing theirs (the run's process
    stopped, or the pod reached its deadline) are deleted - never one still
    running, which is another run's. Housekeeping: a failed read skips it."""
    selector = urllib.parse.quote(NAMES.key("task") + "=" + TASK)
    try:
        pods = (kget(f"/api/v1/namespaces/{NS}/pods?labelSelector={selector}") or {}).get("items") or []
    except Exception:
        return
    for pod in pods:
        meta = pod.get("metadata") or {}
        if ((pod.get("status") or {}).get("phase") in ("Succeeded", "Failed") and meta.get("name", "").startswith("homestead-host-")
                and NAMES.label_of(meta, "task") == TASK):
            try:
                ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{meta['name']}",
                      {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0,
                       **({"preconditions": {"uid": meta["uid"]}} if meta.get("uid") else {})})
            except urllib.error.HTTPError:
                pass


def run(node, script, timeout=120, wait=60, sleep=time.sleep, stdin=b"", change="", holder=""):
    """Run script as root on node's host; (stdout text, stderr text), with
    the script's exit_code beside them (homestead_pod_exec.Output). A script
    still running after timeout seconds raises homestead_pod_exec.ExecTimeout,
    with what it had printed; its helper is removed. The
    helper is made for this run and removed after it; another run on the
    same host has its own, which this one never touches (#597). stdin, when
    given, is what the script reads: a secret goes there, never on its
    command line.

    change says what the script changes on the host, in words shown to a
    change refused meanwhile; a read leaves it empty. A change takes the
    host's lease for the run (Busy if another change holds it) and gives it
    back after, or, with holder from hold(), runs inside that hold and leaves
    it held."""
    if not change and not holder:
        return _run(node, script, timeout, wait, sleep, stdin)
    own = not holder
    holder = hold(node, change, holder=holder)
    stop = threading.Event()

    def renew():
        while not stop.wait(RENEW_EVERY):
            try:
                hold(node, change, holder=holder)
            except Exception:
                pass

    renewer = threading.Thread(target=renew, name=f"host-lease-{node}"[:40], daemon=True)
    renewer.start()
    try:
        return _run(node, script, timeout, wait, sleep, stdin)
    finally:
        stop.set()
        renewer.join()
        if own:
            let_go(node, holder)


def _run(node, script, timeout, wait, sleep, stdin):
    _clear_ended()
    name = pod_name(node)
    path = f"/api/v1/namespaces/{NS}/pods/{name}"
    ksend("POST", f"/api/v1/namespaces/{NS}/pods", body(node, lifetime=max(300, timeout + wait + 60), name=name))
    try:
        for _ in range(int(wait * 2)):
            status = (KUBE.get_optional(path, read=kget) or {}).get("status") or {}
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
                answer = exec_in(NS, name, HOST + [script], timeout=timeout, container="host",
                                 **({"stdin": stdin} if stdin else {}))
                out, err = answer
                return POD_EXEC.Output(out.decode("utf-8", "replace"), err, getattr(answer, "exit_code", None),
                                       getattr(answer, "cut", False))
            except POD_EXEC.ExecTimeout as error:
                # Never tried again: the script may have changed the host.
                raise POD_EXEC.ExecTimeout(
                    f"The script on {node} did not finish within {timeout:g} seconds, so Homestead stopped waiting "
                    f"and removes its helper; what it changed on {node} before then stays changed",
                    error.stdout.decode("utf-8", "replace"), error.stderr, timeout) from error
            except ConnectionError as error:
                if not getattr(error, "retry", False) or attempt == EXEC_TRIES - 1:
                    raise
                sleep(EXEC_PAUSE)
    finally:
        _delete(path)
