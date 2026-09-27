"""Storage protocol capabilities of the processes sharing Homestead's data PVC.

A report is bound to a pod UID and container runtime ID, not a tag or pod name.
Old binaries do not publish this capability. Every gate reads current Kubernetes
inventory; reports from replaced containers cannot authorize the replacement.
This is an internal rollout check, not protection against external administrators
changing the shared files, forcing old software, or bypassing Homestead.
"""
import json
import os
import time

import homestead_shared as SHARED
from homestead_storage_journal import Held

FILE = "storage-runtime-v1.json"
PROTOCOL = 1
SELF_DATA_PROTOCOL = 1


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


def report(ops, read, namespace, pod_name, container_name, version, data_mount="/data", *, self_data=False, clock=time.time):
    """Each new-code process attests only its own current runtime identity."""
    pod = _pod(read, namespace, pod_name)
    container = _container(pod, container_name)
    identity = _identity(pod, container)
    claim = _claim(pod, container, data_mount)
    if not version:
        raise Held("Homestead's installed version is unavailable")
    row = {**identity, "namespace": namespace, "claim": claim, "version": version, "protocol": PROTOCOL}
    if self_data:
        # A failure here must not prevent the existing workload-storage protocol
        # from reporting. A missing self-data capability still blocks that move.
        try:
            from homestead_self_data_fence import mounted_data, require_app_readiness
            require_app_readiness(pod["spec"], container_name)
            mount = mounted_data(data_mount)
            if any(m.get("subPath") for m in container.get("volumeMounts", []) if m.get("mountPath") == data_mount):
                raise Held("Self-data moves require the whole volume")
            row["self_data"] = {"protocol": SELF_DATA_PROTOCOL, "directory": data_mount, "mount": mount, "checked_at": clock()}
        except Held:
            pass
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
            if {k: v for k, v in row.items() if k != "self_data"} != expected:
                raise Held("Finish upgrading all Homestead replicas before moving storage; a process sharing its data is older, unverified, or restarting")
            seen.add(key)
    if own_identity["pod_uid"] + "/" + container_name not in seen:
        raise Held("Homestead's current process is missing from the verified replica inventory")
    return {"protocol": PROTOCOL, "version": version, "replicas": len(seen)}


def require_self_data(ops, pods, namespace, container_name, version, claim, *, own_uid, data_mount="/data", clock=time.time):
    """Read-only compatibility/mount check for every reviewed source replica.

    No lock files, registration, resolver polling or timestamp renewal. Approval
    binds runtime/mount identities, not heartbeat timestamps. Writer draining is
    a separate execution step; this check never authorizes copying live data.
    """
    from homestead_self_data_fence import mounted_data, require_app_readiness
    members, result = _records(ops.DATA_DIR), []
    now, own_mount = clock(), mounted_data(data_mount)
    for pod in pods:
        if pod.get("metadata", {}).get("namespace") != namespace or pod.get("metadata", {}).get("deletionTimestamp"):
            raise Held("A reviewed Homestead replica is changing; refresh the move review")
        container = _container(pod, container_name)
        require_app_readiness(pod["spec"], container_name)
        mounts = [m for m in container.get("volumeMounts", []) if m.get("mountPath", "").rstrip("/") == data_mount.rstrip("/")]
        if len(mounts) != 1 or any(mounts[0].get(k) for k in ("subPath", "subPathExpr", "readOnly")) or _claim(pod, container, data_mount) != claim:
            raise Held("Every Homestead replica must use the whole reviewed source volume")
        identity = _identity(pod, container)
        row = members.get(identity["pod_uid"] + "/" + container_name, {})
        expected = {**identity, "namespace": namespace, "claim": claim, "version": version, "protocol": PROTOCOL}
        capability = row.get("self_data", {})
        checked = capability.get("checked_at")
        mount = capability.get("mount")
        if ({k: v for k, v in row.items() if k != "self_data"} != expected
                or type(capability.get("protocol")) is not int or capability["protocol"] != SELF_DATA_PROTOCOL
                or capability.get("directory") != data_mount
                or type(checked) not in (int, float) or not 0 <= now - checked <= 60
                or not isinstance(mount, dict) or set(mount) != {"mount_id", "device", "inode", "root", "filesystem"}):
            raise Held("Wait for every Homestead replica to report current data-move support; finish any upgrade first")
        if identity["pod_uid"] == own_uid and mount != own_mount:
            raise Held("This process's data mount changed; wait for a fresh runtime report")
        names = {v.get("name") for v in pod["spec"].get("volumes", []) if v.get("persistentVolumeClaim", {}).get("claimName") == claim}
        for key in ("containers", "ephemeralContainers"):
            if any(c.get("name") != container_name and any(m.get("name") in names and not m.get("readOnly")
                   for m in c.get("volumeMounts", [])) for c in pod["spec"].get(key, [])):
                raise Held("Another container can write Homestead's data; remove it before moving the volume")
        # Completed initializers will be recreated after cutover. Only the
        # shipped root ownership adjustment is understood by this protocol.
        for init in pod["spec"].get("initContainers", []):
            if not any(m.get("name") in names and not m.get("readOnly") for m in init.get("volumeMounts", [])):
                continue
            statuses = [s for s in pod.get("status", {}).get("initContainerStatuses", []) if s.get("name") == init.get("name")]
            writable_mounts = [m for m in init.get("volumeMounts", []) if m.get("name") in names and not m.get("readOnly")]
            if (init.get("name") != "data-permissions" or init.get("restartPolicy") or init.get("args") or init.get("env") or init.get("envFrom")
                    or init.get("command") != ["sh", "-c", "chown 10001:10001 /data && chmod 0770 /data"]
                    or len(writable_mounts) != 1 or writable_mounts[0].get("mountPath") != "/data"
                    or any(writable_mounts[0].get(k) for k in ("subPath", "subPathExpr"))
                    or data_mount != "/data" or len(statuses) != 1
                    or statuses[0].get("state", {}).get("terminated", {}).get("exitCode") != 0):
                raise Held("A data initializer is active or customized; its restart behavior needs review before moving data")
        result.append({**identity, "mount": mount, "protocol": SELF_DATA_PROTOCOL})
    if not result or own_uid not in {r["pod_uid"] for r in result}:
        raise Held("The current process is missing from the source data-move review")
    return sorted(result, key=lambda row: row["pod_uid"])
