"""Short-lived pods that ask one node's containerd something.

Kubernetes knows less about images than containerd does: a node's status
lists only its fifty largest images, and a pull reports no progress at all.
containerd knows both - every image it holds, and each layer it is fetching
and how far along it is - so for those Homestead starts a pod on the node that
runs the host's own crictl or ctr against containerd's socket, and reads what
it prints.

The pod runs as root, for the socket, but with every capability dropped, no
privilege escalation and a read-only root; RKE2 and Harvester keep crictl and
ctr in /var/lib/rancher/rke2/bin, and k3s is both itself, chosen by the name
it is run as.
"""
import homestead_names as NAMES
import homestead_platform as PLATFORM

SOCKET_DIR = "/run/k3s/containerd"
SOCKET = "/host/run/k3s/containerd/containerd.sock"
CRICTL = f"/usr/local/bin/crictl --runtime-endpoint unix://{SOCKET} --image-endpoint unix://{SOCKET}"
CTR = f"/usr/local/bin/ctr -a {SOCKET} -n k8s.io"
IMAGE = "python:3.12-alpine"


def binaries():
    try:
        distribution = PLATFORM.detect().get("distribution", "")
    except Exception:
        distribution = ""
    if distribution == "k3s":
        return {"crictl": "/usr/local/bin/k3s", "ctr": "/usr/local/bin/k3s"}
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
