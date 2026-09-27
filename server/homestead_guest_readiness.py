"""Self-contained, read-only guest-agent probe installed by VM batch cloud-init.

No listener, callback, SSH password, exported kubeconfig or shell command. Server
queries use that guest's local k3s CA and client certificate, with verified TLS.
This file must remain standalone: it is copied into guests without this package.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import uuid

CONFIG = "/etc/homestead/guest-readiness.json"
MARKER = "/var/lib/homestead/guest-bootstrap-verified"
TLS = "/var/lib/rancher/k3s/server/tls/"
MAX_RESPONSE = 2 * 1024 * 1024


class NotReady(ValueError):
    pass


def require(value):
    if not value:
        raise NotReady("Guest verification is incomplete")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise NotReady("Guest API redirected")


def reader():
    context = ssl.create_default_context(cafile=TLS + "server-ca.crt")
    context.load_cert_chain(TLS + "client-admin.crt", TLS + "client-admin.key")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                       urllib.request.HTTPSHandler(context=context))
    def read(path):
        require(path.startswith("/") and not path.startswith("//"))
        url = "https://127.0.0.1:6443" + path
        with opener.open(url, timeout=2) as response:
            require(response.status == 200 and response.geturl() == url)
            raw = response.read(MAX_RESPONSE + 1)
        require(len(raw) <= MAX_RESPONSE)
        return raw.strip() == b"ok" if path == "/readyz" else json.loads(raw)
    return read


def condition(obj, kind):
    values = [row.get("status") for row in (obj.get("status") or {}).get("conditions", []) if row.get("type") == kind]
    return values == ["True"]


def items(value, kind):
    require(isinstance(value, dict) and value.get("kind") == kind and not value.get("metadata", {}).get("continue"))
    rows = value.get("items")
    require(isinstance(rows, list) and len(rows) <= 100)
    names = [row.get("metadata", {}).get("name") for row in rows]
    require(all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names))
    return {name: row for name, row in zip(names, rows)}


def controller(value, kind, namespace, name, minimum=1):
    meta, state, spec = (value.get(key) or {} for key in ("metadata", "status", "spec"))
    require(value.get("kind") == kind and meta.get("namespace") == namespace and meta.get("name") == name
            and meta.get("uid") and not meta.get("deletionTimestamp"))
    require(int(meta.get("generation", 0)) > 0 and int(state.get("observedGeneration", 0)) >= int(meta["generation"]))
    if kind == "Deployment":
        desired = int(spec.get("replicas", 1))
        require(desired >= minimum and int(state.get("updatedReplicas", 0)) == desired
                and int(state.get("availableReplicas", 0)) >= desired and int(state.get("unavailableReplicas", 0)) == 0)
    else:
        desired = int(state.get("desiredNumberScheduled", 0))
        require(desired >= minimum and int(state.get("updatedNumberScheduled", 0)) == desired
                and int(state.get("numberReady", 0)) == desired and int(state.get("numberUnavailable", 0)) == 0)


def operator(value, kind, name):
    require(value.get("kind") == kind and value.get("metadata", {}).get("name") == name
            and value.get("metadata", {}).get("uid") and not value.get("metadata", {}).get("deletionTimestamp"))
    state = value.get("status") or {}
    require(state.get("phase") == "Deployed" and condition(value, "Available")
            and not condition(value, "Degraded") and not condition(value, "Progressing"))
    if kind == "KubeVirt":
        require(value.get("metadata", {}).get("namespace") == "kubevirt")
        require(state.get("observedDeploymentID") and state.get("observedDeploymentID") == state.get("targetDeploymentID"))
        require(int(state.get("observedGeneration", 0)) >= int(value["metadata"].get("generation", 1)))
    else:  # CDI embeds controller-lifecycle-operator-sdk's versioned status.
        require(state.get("observedVersion") and state.get("observedVersion") == state.get("targetVersion"))


def check(config, read, system_uuid, active, now=None, bootstrap=True):
    require(config.get("version") == 1 and config.get("role") in ("server", "agent")
            and config.get("setup") in ("k3s", "local", "homestead"))
    members = config.get("members")
    require(isinstance(members, list) and 0 < len(members) <= 100)
    expected = {row["name"]: row for row in members}
    require(len(expected) == len(members) and config.get("name") in expected)
    own = expected[config["name"]]
    require(own["role"] == config["role"] and str(uuid.UUID(system_uuid.strip())) == own["uuid"])
    require(active("k3s" if config["role"] == "server" else "k3s-agent"))
    if config["role"] == "agent":
        return  # all servers independently verify this node's API identity/lease
    require(read("/readyz") is True)
    nodes = items(read("/api/v1/nodes?limit=100"), "NodeList")
    leases = items(read("/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases?limit=100"), "LeaseList")
    if bootstrap:
        require(set(nodes) == set(expected))
    else:
        # The full topology/components are an installation check, not a lasting
        # restriction on adding/removing workers or managing add-ons afterwards.
        # Every server still verifies its own identity, lease and local /readyz.
        expected = {config["name"]: own}
        require(config["name"] in nodes)
    now = time.time() if now is None else now
    for name, expected_node in expected.items():
        node = nodes[name]
        meta, state = node.get("metadata") or {}, node.get("status") or {}
        # Typed Kubernetes lists can omit per-item TypeMeta. The enclosing
        # NodeList/LeaseList and fixed authenticated endpoint establish the type.
        require(node.get("kind", "Node") == "Node" and meta.get("uid") and not meta.get("deletionTimestamp") and condition(node, "Ready"))
        require(str(uuid.UUID(state.get("nodeInfo", {}).get("systemUUID", ""))) == expected_node["uuid"])
        require(any(row.get("type") == "InternalIP" and row.get("address") == expected_node["address"] for row in state.get("addresses", [])))
        server = any(key in (meta.get("labels") or {}) for key in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))
        require(server == (expected_node["role"] == "server"))
        lease = leases.get(name) or {}
        lm, ls = lease.get("metadata") or {}, lease.get("spec") or {}
        require(lease.get("kind", "Lease") == "Lease" and lm.get("namespace") == "kube-node-lease" and not lm.get("deletionTimestamp")
                and ls.get("holderIdentity") == name and any(owner.get("kind") == "Node" and owner.get("uid") == meta["uid"]
                    for owner in lm.get("ownerReferences", [])))
        renewed = datetime.datetime.fromisoformat(ls.get("renewTime", "").replace("Z", "+00:00"))
        require(renewed.tzinfo is not None and -30 <= now - renewed.timestamp() <= 90)
    if bootstrap and config["setup"] != "k3s":
        controller(read("/apis/apps/v1/namespaces/lab/deployments/homestead"), "Deployment", "lab", "homestead")
    if bootstrap and config["setup"] == "homestead":
        controller(read("/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-manager"), "DaemonSet", "longhorn-system", "longhorn-manager", len(expected))
        controller(read("/apis/apps/v1/namespaces/longhorn-system/deployments/longhorn-driver-deployer"), "Deployment", "longhorn-system", "longhorn-driver-deployer")
    if bootstrap and config.get("kubevirt"):
        operator(read("/apis/kubevirt.io/v1/namespaces/kubevirt/kubevirts/kubevirt"), "KubeVirt", "kubevirt")
        operator(read("/apis/cdi.kubevirt.io/v1beta1/cdis/cdi"), "CDI", "cdi")


def verify(config, read, system_uuid, active, marker, now=None):
    fingerprint = hashlib.sha256(canonical(config)).hexdigest()
    try:
        complete = marker.read_text() == fingerprint
    except FileNotFoundError:
        complete = False
    check(config, read, system_uuid, active, now=now, bootstrap=not complete)
    if not complete:
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A root-owned, config-bound completion receipt stays inside this guest.
        # Never treat a partially written marker as completed installation.
        temporary = marker.with_name(marker.name + ".tmp")
        with open(temporary, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write(fingerprint)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)


def main(args):
    def deadline(*_):
        raise NotReady("Probe deadline reached")
    try:
        # KubeVirt's exec timeout does not kill a guest command. Bound our own
        # lifetime too; reads have shorter socket timeouts and bounded bodies.
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(23)
        require(len(args) == 1 and len(args[0]) == 64)
        raw = Path(CONFIG).read_bytes()
        require(len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == args[0])
        config = json.loads(raw)
        def active(service):
            return subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", service], timeout=2,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        verify(config, reader() if config.get("role") == "server" else None,
               Path("/sys/class/dmi/id/product_uuid").read_text(), active, Path(MARKER))
        print("Guest verification passed")
        return 0
    except Exception:
        print("Guest verification pending or failed; inspect local services and /var/log/homestead-k3s.log")
        return 1
    finally:
        if hasattr(signal, "alarm"):
            signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
