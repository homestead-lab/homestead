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

A failure before the revert puts things back: the volume is detached and
everything started again, unchanged.
"""
import json
import time
import urllib.error
import urllib.request

LHNS = "longhorn-system"
API = f"/apis/longhorn.io/v1beta2/namespaces/{LHNS}"
BACKEND = "http://longhorn-backend.longhorn-system:9500/v1"
ATTACHMENT = "homestead-revert"
WAIT = {"stopping": 600, "attaching": 300, "detaching": 300, "starting": 600}

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
        raise ValueError(f"Longhorn refused it: {detail or error.reason}") from error
    return json.loads(text)


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _volume(name):
    volume = _get(f"{API}/volumes/{name}")
    if not volume:
        raise ValueError(f"there is no Longhorn volume {name}")
    return volume


def _snapshot(name, volume):
    snap = _get(f"{API}/snapshots/{name}")
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


def start(volume, snapshot, ops):
    p = plan(volume, snapshot)
    if p["blockers"]:
        raise ValueError(p["blockers"][0])
    ref = {"volume": volume, "snapshot": snapshot, "namespace": p["namespace"], "claim": p["claim"],
           "consumers": p["consumers"], "phase": "stopping", "since": time.time()}
    label = p["claim"] or volume
    return ops.start("snapshot-revert", f"Roll {label} back to a snapshot",
                     {"kind": "Volume", "name": label, "namespace": p["namespace"]}, "/data-protection", ref,
                     f"Stopping what uses {label}")


def _stop(ns, ref):
    for c in ref["consumers"]:
        if c.get("stopped"):
            continue
        name = c["name"]
        if c["kind"] in ("Deployment", "StatefulSet"):
            plural = "deployments" if c["kind"] == "Deployment" else "statefulsets"
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/{plural}/{name}", {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
        elif c["kind"] == "CronJob":
            ksend("PATCH", f"/apis/batch/v1/namespaces/{ns}/cronjobs/{name}", {"spec": {"suspend": True}},
                  ctype="application/merge-patch+json")
        elif c["kind"] == "VirtualMachine":
            ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
                  {"spec": {"runStrategy": "Halted", "running": None}}, ctype="application/merge-patch+json")
        c["stopped"] = True


def _phase(ref, phase):
    ref["phase"], ref["since"] = phase, time.time()


def _late(ref):
    return time.time() - float(ref.get("since") or 0) > WAIT.get(ref.get("phase"), 600)


def _state(volume):
    return ((_volume(volume).get("status") or {}).get("state") or "").lower()


def _put_back(ref, why):
    """Before the revert: detach, start everything again, and say why."""
    try:
        if ref.get("attached"):
            lh_call("POST", f"/volumes/{ref['volume']}?action=detach",
                    {"attachmentID": ATTACHMENT, "hostId": ref.get("node", ""), "forceDetach": False})
    finally:
        if ref.get("namespace"):
            reclass._start(ref["namespace"], ref)
    return "failed", 100, f"{why}. Nothing was changed; everything that was running is starting again"


def resolve(item):
    ref = item["ref"]
    ns, volume = ref.get("namespace", ""), ref["volume"]
    phase = ref.get("phase")
    if phase == "stopping":
        if ns:
            _stop(ns, ref)
        using = reclass._using_pods(ns, ref["claim"], helpers=True) if ns and ref.get("claim") else []
        if using or _state(volume) != "detached":
            if _late(ref):
                return _put_back(ref, f"{volume} did not detach in time"
                                      + (f" - {using[0]['metadata']['name']} still uses it" if using else ""))
            return "running", 10, f"Waiting for {ref['claim'] or volume} to be let go"
        node = _replica_node(volume)
        if not node:
            return _put_back(ref, f"no node holds a working copy of {volume}")
        lh_call("POST", f"/volumes/{volume}?action=attach",
                {"hostId": node, "disableFrontend": True, "attachedBy": "", "attacherType": "longhorn-api",
                 "attachmentID": ATTACHMENT})
        ref.update(node=node, attached=True)
        _phase(ref, "attaching")
        return "running", 30, f"Attaching {volume} to {node} for the rollback"
    if phase == "attaching":
        if _state(volume) != "attached":
            if _late(ref):
                return _put_back(ref, f"{volume} did not attach for the rollback")
            return "running", 35, f"Attaching {volume} to {ref.get('node')}"
        keep = f"before-rollback-{int(time.time())}"
        lh_call("POST", f"/volumes/{volume}?action=snapshotCreate", {"name": keep})
        ref["kept"] = keep
        lh_call("POST", f"/volumes/{volume}?action=snapshotRevert", {"name": ref["snapshot"]})
        ref["reverted"] = True
        lh_call("POST", f"/volumes/{volume}?action=detach",
                {"attachmentID": ATTACHMENT, "hostId": ref.get("node", ""), "forceDetach": False})
        ref["attached"] = False
        _phase(ref, "detaching")
        return "running", 70, f"Rolled back to {ref['snapshot']}; the state before is kept as {keep}"
    if phase == "detaching":
        if _state(volume) != "detached" and not _late(ref):
            return "running", 80, f"Detaching {volume}"
        if ns:
            reclass._start(ns, ref)
        _phase(ref, "starting")
        return "running", 85, "Starting everything again"
    if phase == "starting":
        waiting = reclass._started(ns, ref) if ns else []
        if waiting and not _late(ref):
            return "running", 90, f"Waiting for {', '.join(waiting[:3])} to be ready"
        tail = f"; {', '.join(waiting)} not ready yet" if waiting else ""
        return ("succeeded", 100, f"{ref['claim'] or volume} is back to {ref['snapshot']}. The state before is kept "
                                  f"as snapshot {ref.get('kept', '')}, to roll forward again if needed{tail}")
    return "failed", 100, f"unknown step {phase}"


def cancel_plan(item):
    ref = item["ref"]
    if ref.get("reverted"):
        return {"mode": "stop", "can": False,
                "why_not": "The volume is rolled back already; what is left is starting things again"}
    return {"mode": "rollback", "undo": ["The volume is let go and everything that was stopped is started again"],
            "keeps": ["The volume's data, which has not been changed"], "severity": "low", "needs": "admin"}


def cancel_run(item, _options):
    return _put_back(item["ref"], "Cancelled")[2]
