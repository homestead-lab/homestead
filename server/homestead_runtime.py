"""Short-lived pods that ask one node's containerd something.

Kubernetes knows less about images than containerd does: a node's status
lists only its fifty largest images, and a pull reports no progress at all.
containerd knows both - every image it holds, and each layer it is fetching
and how far along it is - so for those Homestead starts a pod on the node that
runs the host's own crictl or ctr against containerd's socket, and reads what
it prints.

The pod runs as root, for the socket, but with every capability dropped, no
privilege escalation and a read-only root; RKE2 and Harvester keep crictl and
ctr in /var/lib/rancher/rke2/bin. On k3s they are names of one binary, but not
of /usr/local/bin/k3s: that is a launcher that first unpacks k3s into
/var/lib/rancher/k3s/data, which a pod does not have, and fails with
"extracting data". The unpacked copy under data/current/bin is the binary
itself, and answers to the name it is run as.
"""
import re

import homestead_names as NAMES
import homestead_platform as PLATFORM

SOCKET_DIR = "/run/k3s/containerd"
SOCKET = "/host/run/k3s/containerd/containerd.sock"
CRICTL = f"/usr/local/bin/crictl --runtime-endpoint unix://{SOCKET} --image-endpoint unix://{SOCKET}"
CTR = f"/usr/local/bin/ctr -a {SOCKET} -n k8s.io"
IMAGE = "python:3.12-alpine"
K3S_BIN = "/var/lib/rancher/k3s/data/current/bin"


def binaries():
    try:
        distribution = PLATFORM.detect().get("distribution", "")
    except Exception:
        distribution = ""
    if distribution == "k3s":
        return {"crictl": K3S_BIN + "/crictl", "ctr": K3S_BIN + "/ctr"}
    return {"crictl": "/var/lib/rancher/rke2/bin/crictl", "ctr": "/var/lib/rancher/rke2/bin/ctr"}


def pod(name, namespace, node, script, task, annotations=None, memory="48Mi", deadline=None, app="homestead-runtime"):
    """A pod on node that runs script with crictl and ctr to hand."""
    tools = binaries()
    spec = {"nodeName": node, "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
            "tolerations": [{"operator": "Exists"}],
            "containers": [{
                "name": "runtime", "image": IMAGE, "command": ["sh", "-c", script],
                # Its own words become the failure the job tray shows.
                "terminationMessagePolicy": "FallbackToLogsOnError",
                "resources": {"requests": {"cpu": "5m", "memory": "16Mi"}, "limits": {"memory": memory}},
                "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False,
                                    "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": [
                    {"name": "crictl", "mountPath": "/usr/local/bin/crictl", "readOnly": True},
                    {"name": "ctr", "mountPath": "/usr/local/bin/ctr", "readOnly": True},
                    {"name": "runtime", "mountPath": "/host" + SOCKET_DIR, "readOnly": True},
                ],
            }],
            "volumes": [
                {"name": "crictl", "hostPath": {"path": tools["crictl"], "type": "File"}},
                {"name": "ctr", "hostPath": {"path": tools["ctr"], "type": "File"}},
                {"name": "runtime", "hostPath": {"path": SOCKET_DIR, "type": "Directory"}},
            ]}
    if deadline:
        spec["activeDeadlineSeconds"] = int(deadline)
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": dict({"app": app}, **NAMES.labels(task)),
                         "annotations": dict(annotations or {})},
            "spec": spec}


def readonly_observer(pod, namespace, task, name_pattern, expected_command, deadline):
    """Verify a known read-only observer, never a label-only drain exception.

    Permit API defaults and old service-account projections, but no application
    storage, writable runtime mounts, sidecars or modified observer commands.
    """
    meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
    labels = meta.get("labels") or {}
    if (meta.get("namespace") != namespace or not meta.get("uid") or
            not re.fullmatch(name_pattern, meta.get("name", "")) or
            labels.get(NAMES.key("task")) != task or labels.get("app") != "homestead-runtime" or
            not spec.get("nodeName") or spec.get("restartPolicy") != "Never" or
            spec.get("activeDeadlineSeconds") != deadline or
            spec.get("initContainers") or spec.get("ephemeralContainers") or
            any(spec.get(k) for k in ("hostPID", "hostIPC", "hostNetwork"))):
        return False
    containers = spec.get("containers") or []
    if len(containers) != 1:
        return False
    container = containers[0]
    command = container.get("command") or []
    if (container.get("name") != "runtime" or container.get("image") != IMAGE or
            any(container.get(k) for k in ("args", "env", "envFrom", "lifecycle", "volumeDevices")) or
            len(command) != 3 or command[:2] != ["sh", "-c"] or not isinstance(command[2], str)):
        return False
    if command != expected_command:
        return False
    security = container.get("securityContext") or {}
    if (security.get("privileged") or security.get("allowPrivilegeEscalation") is not False or
            security.get("readOnlyRootFilesystem") is not True or
            security.get("capabilities") != {"drop": ["ALL"]}):
        return False
    paths = {"crictl": {K3S_BIN + "/crictl", "/var/lib/rancher/rke2/bin/crictl"},
             "ctr": {K3S_BIN + "/ctr", "/var/lib/rancher/rke2/bin/ctr"},
             "runtime": {SOCKET_DIR}}
    volumes = spec.get("volumes") or []
    mounts = container.get("volumeMounts") or []
    if len({v.get("name") for v in volumes}) != len(volumes) or len({m.get("name") for m in mounts}) != len(mounts):
        return False
    for name, allowed in paths.items():
        volume = next((v for v in volumes if v.get("name") == name), {})
        host = volume.get("hostPath") or {}
        mount = next((m for m in mounts if m.get("name") == name), {})
        destination = "/host" + SOCKET_DIR if name == "runtime" else "/usr/local/bin/" + name
        if (set(volume) != {"name", "hostPath"} or host.get("path") not in allowed or
                host.get("type") != ("Directory" if name == "runtime" else "File") or
                mount.get("mountPath") != destination or mount.get("readOnly") is not True or
                mount.get("subPath") or mount.get("subPathExpr")):
            return False
    extras = {v["name"]: v for v in volumes if v.get("name") not in paths}
    if any(set(v) != {"name", "projected"} for v in extras.values()):
        return False
    return (len(mounts) == len(volumes) and all(m.get("name") in paths or
            (m.get("name") in extras and m.get("readOnly") is True and
             m.get("mountPath") == "/var/run/secrets/kubernetes.io/serviceaccount" and
             not m.get("subPath") and not m.get("subPathExpr")) for m in mounts))
