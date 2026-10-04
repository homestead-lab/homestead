"""A cluster built the way a person builds one: Homestead's own installer,
unattended, from the release under test - a new cluster on the first host,
then each other host joined as a server, so etcd has a quorum to lose.

The installer fetches its scripts and Homestead's manifest from the release
tag and pulls the image from GHCR, so what is tested is what was published.
"""
import os
import time
from pathlib import Path

from . import log
from .vms import NET, VIP

RAW = "https://raw.githubusercontent.com/homestead-lab/homestead"


def installer_env(distro, version, node, role, extra=None):
    env = {"HS_ROLE": role, "HS_DIST": distro, "HS_NODE_IP": node.ip, "HS_VERSION": version,
           "HOMESTEAD_REF": f"v{version}", "HS_YES": "1",
           # Longhorn for the storage paths; KubeVirt only where VMs are tested.
           "HS_LONGHORN": "yes", "HS_KUBEVIRT": os.environ.get("E2E_KUBEVIRT", "no"),
           "HS_KUBEVIP": "yes", "HS_VIP": VIP, "HS_MULTUS": "no",
           "HS_CONSOLE": "no", "HS_LONGHORN_VOLUME": "0", "HS_NODEPROBE": "yes"}
    for key in ("HS_K8S_VERSION", "HS_LONGHORN_VERSION"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update(extra or {})
    return " ".join(f"{k}={v}" for k, v in env.items())


def run_installer(node, distro, version, role, extra=None, timeout=2400):
    env = installer_env(distro, version, node, role, extra)
    log.info(f"{node.name}: installer, {role} ({distro}, Homestead {version})")
    out = node.ssh(f"curl -sfL {RAW}/v{version}/scripts/install.sh -o /tmp/install.sh && "
                   f"sudo env {env} sh /tmp/install.sh --install --text < /dev/null > /tmp/install.log 2>&1; "
                   f"code=$?; tail -40 /tmp/install.log; exit $code", timeout=timeout)
    log.debug(out)


def kubeconfig(lab, distro, directory):
    first = lab.nodes[0]
    path = "/etc/rancher/k3s/k3s.yaml" if distro == "k3s" else "/etc/rancher/rke2/rke2.yaml"
    text = first.ssh(f"sudo cat {path}", quiet=True).replace("127.0.0.1", first.ip)
    target = Path(directory) / "kubeconfig"
    target.write_text(text)
    os.chmod(target, 0o600)
    return str(target)


def build(lab, distro, version, directory):
    first = lab.nodes[0]
    run_installer(first, distro, version, "new")
    token = first.ssh(f"sudo cat /var/lib/rancher/{distro}/server/node-token", quiet=True).strip()
    # etcd members join one at a time.
    for node in lab.nodes[1:]:
        run_installer(node, distro, version, "server", {"HS_SERVER": first.ip, "HS_TOKEN": token})
    config = kubeconfig(lab, distro, directory)
    log.info(f"Cluster ready: {len(lab.nodes)} {distro} host(s); kubeconfig {config}")
    return config
