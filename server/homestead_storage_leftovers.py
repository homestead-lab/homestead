"""What storage leaves behind, for Cluster Health to name (#932). Reads only.

Released volumes: a claim was deleted and its class keeps the data (reclaim
policy Retain), so the volume stays Released with its space allocated on the
hosts until someone deletes it. CDI's import scratch on a Retain class is the
usual case; homestead_cdi_cleanup reclaims only the scratch of imports
Homestead ran itself. Each one is named with its last claim, class, size,
when it was released and, for Longhorn, the hosts its copies are on: Longhorn
counts a volume's whole size on each of them, which is what its allocation
warning measures. Deleting one is the existing reviewed delete
(homestead_volumes, admin), or Remove old copy for the original a class change
kept (homestead_reclass).

Stale iSCSI sessions: Longhorn's V1 engine serves a volume as an iSCSI
target from its instance-manager pod, and the host logs in to that pod's
address. When a host is lost while an engine runs on it and the
instance-manager is replaced, the host keeps its session to the old address
once it comes back, and a later attach there fails in a loop (iscsiadm exit
32 on logout). The node probe lists each host's sessions from its /sys; a
session to a Longhorn target whose address no instance-manager pod has now
is named on its host. Judged only from a current probe reading, only once
the instance-manager pods were read, and only on a host whose own
instance-manager runs (one that is starting proves nothing). Logging a
session out needs iscsiadm on the host, which Homestead does not run: the
insight gives the steps.
"""
import json
import urllib.parse

import homestead_kube as KUBE
import homestead_memory as MEMORY
import homestead_names as NAMES
import homestead_node_probes as NODE_PROBES
import homestead_routes

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
IM_PODS = ("/api/v1/namespaces/longhorn-system/pods?labelSelector="
           + urllib.parse.quote("longhorn.io/component=instance-manager", safe=""))
LONGHORN_DRIVER = "driver.longhorn.io"
LONGHORN_TARGET = "iqn.2019-10.io.longhorn:"
OLD_COPY = NAMES.key("reclass-old")  # homestead_reclass.OLD_COPY
CACHE = "storage-leftovers"


def _error(error):
    return str(error)[:160] or type(error).__name__


def _bytes(quantity):
    try:
        return int(MEMORY.bytes_for(quantity, "size") or 0)
    except ValueError:
        return 0


def _released(pvs):
    try:
        copies = {}
        for row in KUBE.items(f"{LH}/replicas"):
            spec = row.get("spec") or {}
            if spec.get("volumeName") and spec.get("nodeID"):
                copies.setdefault(spec["volumeName"], set()).add(spec["nodeID"])
    except Exception:
        copies = None                 # hosts unknown; the volumes are still named
    try:
        # The reviewed delete finds the claim by name: a new claim of that name
        # is another volume's, and the review refuses it. None: not read.
        claims = {((c.get("metadata") or {}).get("namespace"), (c.get("metadata") or {}).get("name"))
                  for c in KUBE.items("/api/v1/persistentvolumeclaims")}
    except Exception:
        claims = None
    out = []
    for pv in pvs:
        meta, spec, status = pv.get("metadata") or {}, pv.get("spec") or {}, pv.get("status") or {}
        if status.get("phase") != "Released" or spec.get("persistentVolumeReclaimPolicy") != "Retain":
            continue
        csi, claim = spec.get("csi") or {}, spec.get("claimRef") or {}
        longhorn = csi.get("volumeHandle", "") if csi.get("driver") == LONGHORN_DRIVER else ""
        size = (spec.get("capacity") or {}).get("storage", "")
        hosts = sorted(copies.get(longhorn, ())) if longhorn and copies is not None else ([] if not longhorn else None)
        out.append({"pv": meta.get("name", ""), "namespace": claim.get("namespace", ""), "claim": claim.get("name", ""),
                    "class": spec.get("storageClassName", ""), "policy": "Retain", "size": size,
                    "size_bytes": _bytes(size),
                    "released_at": status.get("lastPhaseTransitionTime") or meta.get("creationTimestamp", ""),
                    "driver": csi.get("driver", ""), "longhorn": longhorn, "hosts": hosts,
                    "allocated_bytes": _bytes(size) * max(1, len(hosts or ())),
                    "old_copy": bool((meta.get("annotations") or {}).get(OLD_COPY)),
                    "claim_reused": None if claims is None else (claim.get("namespace"), claim.get("name")) in claims})
    return sorted(out, key=lambda r: (-r["allocated_bytes"], r["pv"]))


def _network_addresses(pod):
    """A pod's addresses on extra networks (Multus network-status): Longhorn's
    storage network gives instance-managers one. Counting them as current
    can only leave a session unnamed, never name one that is not stale."""
    raw = ((pod.get("metadata") or {}).get("annotations") or {}).get("k8s.v1.cni.cncf.io/network-status") or ""
    try:
        networks = json.loads(raw) if raw else []
    except ValueError:
        return set()
    return {ip for n in networks if isinstance(n, dict) for ip in n.get("ips") or [] if isinstance(ip, str)}


def _stale_sessions(pvs):
    probes = NODE_PROBES.node_temps(wait=False)
    readings = {node: p["iscsi"] for node, p in (probes or {}).items() if isinstance(p.get("iscsi"), list)}
    if not readings:
        return {"state": "n/a", "hosts": []}
    try:
        pods = KUBE.items(IM_PODS)
    except Exception as error:
        return {"state": "unknown", "error": _error(error), "hosts": []}
    current, running = set(), set()
    for pod in pods:
        status = pod.get("status") or {}
        # An instance-manager that has ended serves nothing, though it may still
        # report its address; one still terminating may still serve its engines.
        if status.get("phase") in ("Failed", "Succeeded"):
            continue
        current |= {a.get("ip") for a in status.get("podIPs") or [] if a.get("ip")} | ({status["podIP"]} if status.get("podIP") else set())
        current |= _network_addresses(pod)
        if status.get("phase") == "Running" and status.get("podIP"):
            running.add((pod.get("spec") or {}).get("nodeName"))
    claims = {}
    for pv in pvs or []:
        csi, claim = (pv.get("spec") or {}).get("csi") or {}, (pv.get("spec") or {}).get("claimRef") or {}
        if csi.get("driver") == LONGHORN_DRIVER and csi.get("volumeHandle"):
            claims[csi["volumeHandle"]] = (claim.get("namespace", ""), claim.get("name", ""))
    hosts = []
    for node in sorted(readings):
        if node not in running:
            continue
        stale = []
        for s in readings[node]:
            target = str(s.get("target") or "")
            if not target.startswith(LONGHORN_TARGET) or not s.get("address") or s["address"] in current:
                continue
            volume = target[len(LONGHORN_TARGET):]
            namespace, claim = claims.get(volume, ("", ""))
            stale.append({"sid": s.get("sid"), "target": target, "volume": volume, "namespace": namespace, "claim": claim,
                          "state": s.get("state", ""), "address": s["address"], "port": s.get("port"),
                          "disks": list(s.get("disks") or [])})
        if stale:
            hosts.append({"node": node, "sessions": stale})
    return {"state": "ok", "hosts": hosts}


def facts():
    """{released: {state, volumes, error?}, iscsi: {state, hosts, error?}}:
    state ok, unknown (a read failed) or, for iscsi, n/a (no probe reports
    sessions)."""
    try:
        pvs = KUBE.items("/api/v1/persistentvolumes")
        released = {"state": "ok", "volumes": _released(pvs)}
    except Exception as error:
        pvs, released = None, {"state": "unknown", "error": _error(error), "volumes": []}
    try:
        iscsi = _stale_sessions(pvs)
    except Exception as error:
        iscsi = {"state": "unknown", "error": _error(error), "hosts": []}
    return {"released": released, "iscsi": iscsi}


ROUTES = {
    ("GET", "/api/storage/leftovers"): ("viewer", lambda request: homestead_routes.cached(CACHE, 30, facts)),
}
