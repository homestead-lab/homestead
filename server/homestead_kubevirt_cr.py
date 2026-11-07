"""The KubeVirt custom resource, changed without undoing a change another
writer made meanwhile (#621).

KubeVirt keeps its settings in one object, whose lists (the devices VMs may be
given, the feature gates) a merge patch replaces whole. A change worked out
from an earlier read would put back that read's lists, dropping a device or a
gate someone added in between. update(fn) reads the CR, lets fn work out its
patch from what is stored, and sends it with the resourceVersion read, so
Kubernetes refuses it (409) if anything was saved in between; then it reads
again and calls fn again, as homestead_kube.update_json does for ConfigMaps."""
import copy
import urllib.error

import homestead_kube as KUBE

KUBEVIRTS = "/apis/kubevirt.io/v1/kubevirts"
MERGE = "application/merge-patch+json"


def path(kv):
    meta = kv["metadata"]
    return f"/apis/kubevirt.io/v1/namespaces/{meta['namespace']}/kubevirts/{meta['name']}"


def current():
    """The KubeVirt CR as stored, or None when there is none."""
    return ((KUBE.kget(KUBEVIRTS) or {}).get("items") or [None])[0]


def update(fn, *, attempts=4, current=current, write=None):
    """fn(kv) gets a copy of the CR as stored and returns a merge patch for it,
    or None when there is nothing to change; it may raise to stop, and nothing
    is written. Returns the patch sent, or None. Without a CR it raises
    ValueError. current() and write(method, path, body, ctype=) are a module's
    own way to read the CR and its bound ksend; this client's by default."""
    write = write or KUBE.send
    for _ in range(attempts):
        kv = current()
        if not kv:
            raise ValueError("KubeVirt is not installed")
        patch = fn(copy.deepcopy(kv))
        if not patch:
            return None
        meta = kv.get("metadata") or {}
        identity = {key: meta[key] for key in ("uid", "resourceVersion") if meta.get(key)}
        try:
            write("PATCH", path(kv), dict(patch, metadata={**(patch.get("metadata") or {}), **identity}), ctype=MERGE)
            return patch
        except urllib.error.HTTPError as error:
            if error.code != 409:    # 409: KubeVirt's settings were saved meanwhile
                raise
            error.close()
    raise RuntimeError("This change was not saved; try again. KubeVirt's settings kept changing while they were being saved")
