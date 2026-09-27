"""Explicit, identity-fenced configuration of the optional allocation sidecar."""
import base64
import copy
import re
import secrets
import urllib.error

import homestead_allocation_auth as AUTH
import homestead_names as NAMES

KEY_NAME = "homestead-allocation-auth"
CONTAINER = "allocation"
VOLUMES = ("allocation-socket", "allocation-auth", "allocation-boot", "allocation-no-token")
ANNOTATION = "homestead.io/allocation-socket"
MANAGED = "homestead.io/allocation-probe"
read = write = None
NS = "lab"


def bind(kget, ksend, namespace):
    global read, write, NS
    read, write, NS = kget, ksend, namespace


def socket_directory(value):
    # Only a dedicated leaf directory: no broad kubelet/host tree or traversal.
    if not isinstance(value, str) or len(value) > 240 or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)+pod-resources", value):
        raise ValueError("Choose the absolute kubelet pod-resources directory, without a trailing slash")
    if any(part in (".", "..") for part in value.split("/")):
        raise ValueError("The allocation socket directory cannot contain traversal")
    return value


def _path():
    return f"/apis/apps/v1/namespaces/{NS}/daemonsets/{NAMES.NODEPROBE}"


def status():
    try:
        obj = read(_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"installed": False, "enabled": False, "detail": "Install the node probe first"}
        raise ValueError("Node probe configuration is unavailable") from None
    except Exception:
        raise ValueError("Node probe configuration is unavailable") from None
    meta = obj.get("metadata") or {}
    if not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp"):
        raise ValueError("Node probe identity is unavailable or being removed")
    pod = (obj.get("spec") or {}).get("template") or {}
    directory = (pod.get("metadata", {}).get("annotations") or {}).get(ANNOTATION, "")
    containers = (pod.get("spec") or {}).get("containers") or []
    enabled = any(row.get("name") == CONTAINER for row in containers)
    return {"installed": True, "enabled": enabled, "managed": bool(directory), "directory": directory,
            "uid": meta["uid"], "resource_version": meta["resourceVersion"],
            "detail": "Collector configured; ready probe pods and verified policy are still required" if enabled else "Allocation collector is disabled"}


def _key(obj):
    path = f"/api/v1/namespaces/{NS}/secrets/{KEY_NAME}"
    meta = obj["metadata"]
    try:
        secret = read(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise ValueError("Allocation authentication could not be read") from None
        body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": KEY_NAME, "namespace": NS, "labels": {MANAGED: "true"},
                             "ownerReferences": [{"apiVersion": "apps/v1", "kind": "DaemonSet",
                                                  "name": meta["name"], "uid": meta["uid"]}]},
                "data": {"key": base64.b64encode(secrets.token_hex(32).encode()).decode()}}
        try:
            write("POST", f"/api/v1/namespaces/{NS}/secrets", body)
        except urllib.error.HTTPError as conflict:
            if conflict.code != 409:
                raise ValueError("Allocation authentication creation was not confirmed; reload before trying again") from None
        except Exception:
            raise ValueError("Allocation authentication creation was not confirmed; reload before trying again") from None
        try:
            secret = read(path)
        except Exception:
            raise ValueError("Allocation authentication could not be verified after creation") from None
    except Exception:
        raise ValueError("Allocation authentication could not be read") from None
    sm = secret.get("metadata") or {}
    owners = sm.get("ownerReferences") or []
    if sm.get("deletionTimestamp") or not sm.get("uid") or sm.get("labels", {}).get(MANAGED) != "true" or not any(
            row.get("uid") == meta["uid"] and row.get("name") == meta["name"] and row.get("kind") == "DaemonSet" and row.get("apiVersion") == "apps/v1" for row in owners):
        raise ValueError("Allocation authentication belongs to another probe; it was not replaced")
    try:
        key = base64.b64decode(secret["data"]["key"], validate=True).decode("ascii")
        AUTH.key_bytes(key)
    except Exception:
        raise ValueError("Allocation authentication is invalid; it was not replaced") from None
    return sm["uid"]


def configure(body, version):
    if type(body.get("enabled")) is not bool:
        raise ValueError("Choose whether to enable the allocation collector")
    enabled = body["enabled"]
    directory = socket_directory(body.get("directory")) if enabled else ""
    if enabled and body.get("acknowledge_host_access") is not True:
        raise ValueError("Confirm the read-only kubelet socket access and probe restart")
    try:
        obj = read(_path())
    except Exception:
        raise ValueError("Node probe configuration could not be read; nothing was changed") from None
    meta = obj.get("metadata") or {}
    if meta.get("name") != NAMES.NODEPROBE or meta.get("namespace") != NS or not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp") or (
            body.get("uid"), body.get("resource_version")) != (meta["uid"], meta["resourceVersion"]):
        raise ValueError("Node probe changed; reload its configuration before saving")
    template = copy.deepcopy(obj["spec"]["template"])
    spec = template["spec"]
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    old = [row for row in spec.get("containers", []) if row.get("name") == CONTAINER]
    occupied = [row for row in spec.get("volumes", []) if row.get("name") in VOLUMES]
    if (old or occupied) and not annotations.get(ANNOTATION):
        raise ValueError("Unmanaged allocation resources already exist; nothing was replaced")
    for row in spec.get("containers", []) + spec.get("initContainers", []) + spec.get("ephemeralContainers", []):
        if row.get("name") != CONTAINER and any(mount.get("name") in VOLUMES for mount in row.get("volumeMounts", [])):
            raise ValueError("Another container uses allocation mounts; nothing was changed")
    # Copy only this optional sidecar and its uniquely named volumes. Preserve
    # every other container, scheduling constraint, volume and annotation.
    spec["containers"] = [row for row in spec["containers"] if row.get("name") != CONTAINER]
    spec["volumes"] = [row for row in spec.get("volumes", []) if row.get("name") not in VOLUMES]
    annotations.pop(ANNOTATION, None)
    annotations.pop("homestead.io/allocation-key-uid", None)
    if enabled:
        key_uid = _key(obj)
        annotations[ANNOTATION] = directory
        annotations["homestead.io/allocation-key-uid"] = key_uid
        spec["containers"].append({"name": CONTAINER, "image": "ghcr.io/wjcloudy/homestead:" + version,
            "imagePullPolicy": "IfNotPresent", "command": ["python3", "/srv/probe/allocation_http.py"],
            "env": [{"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
                    for name, field in (("NODE_NAME", "spec.nodeName"), ("POD_UID", "metadata.uid"))],
            "ports": [{"name": "allocation", "containerPort": 9101}],
            "resources": {"requests": {"cpu": "5m", "memory": "32Mi"}, "limits": {"cpu": "250m", "memory": "96Mi"}},
            "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": "allocation"}, "timeoutSeconds": 2, "periodSeconds": 15},
            "volumeMounts": [{"name": name, "mountPath": path, "readOnly": True}
                             for name, path in zip(VOLUMES, ("/pod-resources", "/allocation-auth", "/host/boot-id",
                                                            "/var/run/secrets/kubernetes.io/serviceaccount"))]})
        spec["volumes"].extend([
            {"name": VOLUMES[0], "hostPath": {"path": directory, "type": "Directory"}},
            {"name": VOLUMES[1], "secret": {"secretName": KEY_NAME, "defaultMode": 256, "items": [{"key": "key", "path": "key"}]}},
            {"name": VOLUMES[2], "hostPath": {"path": "/proc/sys/kernel/random/boot_id", "type": "File"}},
            # Kubernetes' service-account admission skips a container that
            # already mounts this path. Do not change other containers' token
            # policy, and never pass the helper an API credential.
            {"name": VOLUMES[3], "emptyDir": {"medium": "Memory", "sizeLimit": "64Ki"}}])
    patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
             {"op": "replace", "path": "/spec/template", "value": template}]
    try:
        write("PATCH", _path(), patch, ctype="application/json-patch+json")
    except Exception:
        raise ValueError("Probe update was not confirmed; reload configuration before another change. No automatic retry or cleanup was performed") from None
    return {"enabled": enabled, "detail": "Allocation collector configuration saved; probe pods are restarting. Workloads were not changed"}
