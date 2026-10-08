"""A cluster built the way a person builds one: Homestead's own installer,
unattended, from the release under test - a new cluster on the first host,
then each other host joined as a server, so etcd has a quorum to lose.

The installer fetches its scripts and Homestead's manifest from the release
tag and pulls the image from GHCR, so what is tested is what was published.
"""
import os
import threading
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
    out = node.ssh(f"curl -sfL --retry 6 --retry-all-errors --retry-delay 5 {RAW}/v{version}/scripts/install.sh -o /tmp/install.sh && "
                   f"sudo env {env} sh /tmp/install.sh --install --text < /dev/null > /tmp/install.log 2>&1; "
                   f"code=$?; tail -40 /tmp/install.log; exit $code", timeout=timeout)
    log.debug(out)


def kubeconfig(lab, distro, directory, first=None, name="kubeconfig"):
    first = first or lab.nodes[0]
    path = "/etc/rancher/k3s/k3s.yaml" if distro == "k3s" else "/etc/rancher/rke2/rke2.yaml"
    text = first.ssh(f"sudo cat {path}", quiet=True).replace("127.0.0.1", first.ip)
    target = Path(directory) / name
    target.write_text(text)
    os.chmod(target, 0o600)
    return str(target)


# k3s's and RKE2's own installers fetch from GitHub's release downloads
# without retrying, and those answer 500 now and then: the whole install is
# asked again, twice, before it counts as failed.
_RETRY = ("{{ ok=0; for try in 1 2 3; do {run} > {log} 2>&1 && {{ ok=1; break; }}; sleep 15; done; "
          "[ $ok = 1 ] || {{ tail -40 {log}; exit 1; }}; }}")


def _bare_k3s(node, first=None, token="", role="server"):
    version = f"INSTALL_K3S_VERSION={os.environ['HS_K8S_VERSION']} " if os.environ.get("HS_K8S_VERSION") else ""
    if first is None:
        mode = f"server --cluster-init --node-ip {node.ip}"
        join = ""
    else:
        mode = f"{role} --node-ip {node.ip}"
        join = f"K3S_URL=https://{first.ip}:6443 K3S_TOKEN={token} "
    run = f"sudo env {version}{join}INSTALL_K3S_EXEC='{mode}' sh /tmp/k3s-install.sh"
    node.ssh("curl -sfL --retry 6 --retry-all-errors --retry-delay 5 https://get.k3s.io -o /tmp/k3s-install.sh && "
             + _RETRY.format(run=run, log="/tmp/k3s-install.log"), timeout=1200)


def _bare_rke2(node, first=None, token="", role="server"):
    version = f"INSTALL_RKE2_VERSION={os.environ['HS_K8S_VERSION']} " if os.environ.get("HS_K8S_VERSION") else ""
    kind = "agent" if role == "agent" else "server"
    config = f"node-ip: {node.ip}\\n" + (f"server: https://{first.ip}:9345\\ntoken: {token}\\n" if first else "")
    run = f"sudo env {version}INSTALL_RKE2_TYPE={kind} sh /tmp/rke2-install.sh"
    node.ssh("curl -sfL --retry 6 --retry-all-errors --retry-delay 5 https://get.rke2.io -o /tmp/rke2-install.sh && "
             + _RETRY.format(run=run, log="/tmp/rke2-install.log") + " && "
             f"sudo mkdir -p /etc/rancher/rke2 && printf '{config}' | sudo tee /etc/rancher/rke2/config.yaml >/dev/null && "
             f"sudo systemctl enable --now rke2-{kind} >> /tmp/rke2-install.log 2>&1 "
             f"|| {{ tail -40 /tmp/rke2-install.log; exit 1; }}", timeout=1800)


def build_bare(lab, distro, directory, nodes=None, agents=0, name="kubeconfig"):
    """A cluster as someone built it before they met Homestead: k3s or RKE2
    from its own installer (get.k3s.io, get.rke2.io), at its stable channel
    unless HS_K8S_VERSION pins it, and nothing else - for the installer to
    add Homestead to. The first of `nodes` (every host, unless given) makes
    it, the rest join, the last `agents` of them as workers."""
    nodes = nodes or lab.nodes
    first = nodes[0]
    bare = _bare_k3s if distro == "k3s" else _bare_rke2
    log.info(f"{first.name}: {distro} from its own installer, new cluster")
    bare(first)
    token = first.ssh(f"sudo cat /var/lib/rancher/{distro}/server/node-token", quiet=True).strip()
    for i, node in enumerate(nodes[1:], start=1):
        node.role = "agent" if i >= len(nodes) - agents else "server"
        log.info(f"{node.name}: {distro} from its own installer, joining as {node.role}")
        bare(node, first, token, node.role)
    config = kubeconfig(lab, distro, directory, first, name)
    log.info(f"Cluster ready without Homestead: {len(nodes)} {distro} host(s); kubeconfig {config}")
    return config


def build_separate(lab, distro, version, directory):
    """One single-host cluster per VM, each with its own address - for moves
    between clusters. Returns each cluster's kubeconfig and Homestead address."""
    clusters = []
    for i, node in enumerate(lab.nodes):
        vip = f"{NET}.{100 + i}"
        run_installer(node, distro, version, "new", {"HS_VIP": vip})
        clusters.append((kubeconfig(lab, distro, directory, node, f"kubeconfig-{node.name}"), vip, [node]))
    log.info(f"{len(clusters)} separate {distro} clusters ready")
    return clusters


def build(lab, distro, version, directory, agents=0, extra=None):
    """The first host makes the cluster; the rest join as servers, and the
    last `agents` of them as workers."""
    first = lab.nodes[0]
    run_installer(first, distro, version, "new", extra)
    token = first.ssh(f"sudo cat /var/lib/rancher/{distro}/server/node-token", quiet=True).strip()
    joins, failed = [], []
    # Every host on the first one's exact version, as Homestead's own join
    # commands pin it. Left to choose, a joining RKE2 server reads the release
    # channel again, and while update.rke2.io was briefly down the first took
    # the newest release (1.37) and the rest the real stable (1.36): an older
    # server never leaves etcd's learner list, and the cluster never forms.
    pinned = dict(extra or {})
    if not os.environ.get("HS_K8S_VERSION"):
        found = first.ssh(f"{distro} --version 2>/dev/null | head -n 1", check=False, quiet=True).split()
        if len(found) >= 3 and found[2].startswith("v"):
            pinned["HS_K8S_VERSION"] = found[2]
            log.info(f"Joining hosts install {distro} {found[2]}, as {first.name} has")

    def join(node):
        try:
            run_installer(node, distro, version, node.role, dict(pinned, HS_SERVER=first.ip, HS_TOKEN=token))
        except Exception as error:
            failed.append(f"{node.name}: {error}")

    for i, node in enumerate(lab.nodes[1:], start=1):
        node.role = "agent" if i >= len(lab.nodes) - agents else "server"
    # Workers join all at once; etcd members one at a time - but only the
    # join itself: the next starts once the last one's service is up, while
    # the rest of its installer carries on alongside.
    service = "k3s" if distro == "k3s" else "rke2-server"
    for node in [n for n in lab.nodes[1:] if n.role == "server"] + [n for n in lab.nodes[1:] if n.role == "agent"]:
        thread = threading.Thread(target=join, args=(node,), daemon=True)
        thread.start()
        joins.append(thread)
        if node.role == "server":
            deadline = time.time() + 900
            while thread.is_alive() and time.time() < deadline:
                if node.ssh(f"systemctl is-active {service} 2>/dev/null || true", check=False, quiet=True).strip() == "active":
                    break
                time.sleep(5)
    for thread in joins:
        thread.join(timeout=2400)
    if failed:
        raise RuntimeError("joining failed: " + "; ".join(failed))
    config = kubeconfig(lab, distro, directory)
    log.info(f"Cluster ready: {len(lab.nodes)} {distro} host(s); kubeconfig {config}")
    return config
