"""How a Homestead job tells which pods an app still has.

pods_of() follows an app's pods through its controllers by uid, never by
name: a Deployment's through its ReplicaSets, a StatefulSet's directly, a
CronJob's through its Jobs, a VirtualMachine's through its instance (the
virt-launcher pod, owned by the instance or labelled kubevirt.io/created-by
with its uid). A pod counts until it has ended: a terminating pod still holds
its volumes and ports. When a list can't be read, the app has no uid, or a
live pod's controller isn't in the list (it may be one of the app's that is
being replaced or removed), whose pods are whose can't be told: that is
None, never an empty list."""
import urllib.parse

import homestead_kube as KUBE
import homestead_rollout_capacity as ROLLOUT

ENDED = ("Succeeded", "Failed")
CREATED_BY = "kubevirt.io/created-by"
# kind -> (the list of what sits between the app and its pods, that list's kind)
VIA = {"CronJob": ("/apis/batch/v1/namespaces/{ns}/jobs", "Job"),
       "VirtualMachine": ("/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances", "VirtualMachineInstance")}
KINDS = ("Deployment", "StatefulSet", *VIA)


def _meta(row):
    return row.get("metadata") or {}


def _live(pod):
    return (pod.get("status") or {}).get("phase") not in ENDED


def _controlled_by(row, kind, uids):
    owner = ROLLOUT.controller(row)
    return owner.get("kind") == kind and owner.get("uid") in uids


def pods_of(obj):
    """The live pods (terminating included) obj's controllers own, for a
    Deployment, StatefulSet, CronJob or VirtualMachine; None when that can't
    be told (see the top of this file)."""
    meta = _meta(obj)
    kind, ns, uid = obj.get("kind"), meta.get("namespace"), meta.get("uid")
    if kind not in KINDS or not ns or not uid:
        return None
    try:
        pods = KUBE.items(f"/api/v1/namespaces/{urllib.parse.quote(ns, safe='')}/pods", read=KUBE.kget)
        pods = [pod for pod in pods if _meta(pod).get("namespace") == ns and _live(pod)]
        if kind == "Deployment":
            owned, known = ROLLOUT.owned_pods(obj, pods, KUBE.kget, ns)
            return owned if known else None
        if kind == "StatefulSet":
            return [pod for pod in pods if _controlled_by(pod, kind, {uid})]
        path, middle = VIA[kind]
        rows = [row for row in KUBE.items(path.format(ns=urllib.parse.quote(ns, safe="")), read=KUBE.kget)
                if _meta(row).get("namespace") == ns]
        if any(ROLLOUT.controller(pod).get("kind") == middle and
               ROLLOUT.controller(pod).get("uid") not in {_meta(row).get("uid") for row in rows}
               for pod in pods):
            return None
        mine = {_meta(row)["uid"] for row in rows if _meta(row).get("uid") and _controlled_by(row, kind, {uid})}
        return [pod for pod in pods if _controlled_by(pod, middle, mine) or
                (kind == "VirtualMachine" and (_meta(pod).get("labels") or {}).get(CREATED_BY) in mine)]
    except (OSError, ValueError):
        return None
