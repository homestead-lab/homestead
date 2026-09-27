"""Storage protocol capabilities of the processes sharing Homestead's data PVC.

A report is bound to a pod UID and container runtime ID, not a tag or pod name.
Old binaries do not publish this capability. Every gate reads current Kubernetes
inventory; reports from replaced containers cannot authorize the replacement.
This is an internal rollout check, not protection against external administrators
changing the shared files, forcing old software, or bypassing Homestead.
"""
import json
import os

import homestead_shared as SHARED
from homestead_storage_journal import Held

FILE = "storage-runtime-v1.json"
PROTOCOL = 1


def _records(directory):
    try:
        with open(os.path.join(directory, FILE), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise Held("Homestead replica capabilities could not be read; restore shared data access before moving storage") from None
    if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("members"), dict):
        raise Held("Homestead replica capabilities are invalid; inspect shared data before moving storage")
    if any(not isinstance(row, dict) for row in data["members"].values()):
        raise Held("Homestead replica capabilities are invalid; inspect shared data before moving storage")
    return data["members"]


def _pod(read, namespace, name):
    obj = read(f"/api/v1/namespaces/{namespace}/pods/{name}")
    if obj.get("metadata", {}).get("name") != name or obj.get("metadata", {}).get("namespace") != namespace:
        raise Held("Homestead's pod identity could not be verified")
    return obj


def _container(pod, name):
    rows = [c for c in pod.get("spec", {}).get("containers", []) if c.get("name") == name]
    if len(rows) != 1:
        raise Held("Homestead's running container could not be identified")
    return rows[0]


def _identity(pod, container):
    statuses = [s for s in pod.get("status", {}).get("containerStatuses", []) if s.get("name") == container["name"]]
    if len(statuses) != 1 or not statuses[0].get("state", {}).get("running"):
        raise Held("Wait for every Homestead replica to start before moving storage")
    result = {"pod_uid": pod.get("metadata", {}).get("uid"), "container": container["name"],
              "container_id": statuses[0].get("containerID"), "image_id": statuses[0].get("imageID"),
              "image": container.get("image")}
    if any(not isinstance(v, str) or not v for v in result.values()):
        raise Held("Homestead replica runtime identity is incomplete; wait for the rollout to finish")
    return result


def _claim(pod, container, directory):
    # The standard installation uses one PVC mounted at DATA_DIR. Refuse
    # ambiguous/subPathExpr layouts instead of assuming the journals are shared.
    mounts = [m for m in container.get("volumeMounts", []) if m.get("mountPath", "").rstrip("/") == directory.rstrip("/")]
    if len(mounts) != 1 or mounts[0].get("subPathExpr") or mounts[0].get("readOnly"):
        raise Held("Storage moves need Homestead's persistent data volume mounted at its data directory")
    volumes = [v for v in pod.get("spec", {}).get("volumes", []) if v.get("name") == mounts[0].get("name")]
    claim = volumes[0].get("persistentVolumeClaim", {}).get("claimName") if len(volumes) == 1 else None
    if not claim:
        raise Held("Storage moves need a persistent Homestead data claim so progress survives a restart")
    return claim


def report(ops, read, namespace, pod_name, container_name, version, data_mount="/data"):
    """Each new-code process attests only its own current runtime identity."""
    pod = _pod(read, namespace, pod_name)
    container = _container(pod, container_name)
    identity = _identity(pod, container)
    claim = _claim(pod, container, data_mount)
    if not version:
        raise Held("Homestead's installed version is unavailable")
    row = {**identity, "namespace": namespace, "claim": claim, "version": version, "protocol": PROTOCOL}
    key = identity["pod_uid"] + "/" + container_name
    with ops._lock:
        members = _records(ops.DATA_DIR)
        if members.get(key) != row:
            members[key] = row
            SHARED.write_json(os.path.join(ops.DATA_DIR, FILE), {"format": 1, "members": members}, durable=True)
    return row


def require(ops, read, namespace, pod_name, container_name, version, data_mount="/data"):
    """Read-only gate; never attest for another process or infer an old version."""
    own = _pod(read, namespace, pod_name)
    container = _container(own, container_name)
    claim = _claim(own, container, data_mount)
    own_identity = _identity(own, container)
    listing = read(f"/api/v1/namespaces/{namespace}/pods")
    if (not isinstance(listing, dict) or not isinstance(listing.get("items"), list)
            or listing.get("metadata", {}).get("continue")):
        raise Held("Homestead replica inventory is incomplete; refresh before moving storage")
    with ops._lock:
        members = _records(ops.DATA_DIR)
    seen = set()
    for pod in listing["items"]:
        if not isinstance(pod, dict) or not pod.get("metadata", {}).get("uid"):
            raise Held("Homestead replica inventory is invalid; refresh before moving storage")
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        volume_names = {v.get("name") for v in pod.get("spec", {}).get("volumes", [])
                        if v.get("persistentVolumeClaim", {}).get("claimName") == claim}
        for peer in pod.get("spec", {}).get("containers", []):
            if not any(m.get("name") in volume_names and not m.get("readOnly") for m in peer.get("volumeMounts", [])):
                continue
            identity = _identity(pod, peer)
            key = identity["pod_uid"] + "/" + peer["name"]
            row = members.get(key, {})
            expected = {**identity, "namespace": namespace, "claim": claim, "version": version, "protocol": PROTOCOL}
            if row != expected:
                raise Held("Finish upgrading all Homestead replicas before moving storage; a process sharing its data is older, unverified, or restarting")
            seen.add(key)
    if own_identity["pod_uid"] + "/" + container_name not in seen:
        raise Held("Homestead's current process is missing from the verified replica inventory")
    return {"protocol": PROTOCOL, "version": version, "replicas": len(seen)}
