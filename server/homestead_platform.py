"""What this cluster is, so pages offer what it can actually do.

Homestead grew up on Harvester, which brings Longhorn, KubeVirt, kube-vip and
RKE2's Helm controller with it. A plain k3s or RKE2 cluster may have some of
those or none. The API server says which groups it serves and the nodes say
which distribution runs them, so each page can check before it relies on one:
the VM pages need KubeVirt, the volume pages Longhorn, and so on. What is
missing is said, with how to add it, rather than failing.
"""
import re
import time

kget = None
_cached = {"at": 0.0, "value": None}
TTL = 300


def bind(_kget):
    global kget
    kget = _kget


def _groups():
    try:
        return {g.get("name") for g in kget("/apis").get("groups", [])}
    except Exception:
        return set()


def _exists(path):
    try:
        kget(path)
        return True
    except Exception:
        return False


def _items(path):
    try:
        return kget(path).get("items", [])
    except Exception:
        return []


def detect(force=False):
    if not force and _cached["value"] is not None and time.time() - _cached["at"] < TTL:
        return _cached["value"]
    groups = _groups()
    nodes = _items("/api/v1/nodes")
    kubelets = [((n.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "") for n in nodes]
    harvester = "harvesterhci.io" in groups
    distribution = ("harvester" if harvester else "k3s" if any("+k3s" in k for k in kubelets)
                    else "rke2" if any("+rke2" in k for k in kubelets) else "kubernetes")
    control = []
    for node in nodes:
        labels = (node.get("metadata") or {}).get("labels") or {}
        if any(k in labels for k in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")):
            ip = next((a["address"] for a in (node.get("status") or {}).get("addresses") or []
                       if a.get("type") == "InternalIP"), "")
            if ip:
                control.append(ip)
    kube_vip = harvester or _exists("/apis/apps/v1/namespaces/kube-system/daemonsets/kube-vip-ds")
    metallb = "metallb.io" in groups
    # k3s's own load balancer, unless something else is doing the job. Its
    # svclb- DaemonSets only appear with the first LoadBalancer Service, so
    # a k3s cluster with none yet still has it.
    servicelb = distribution == "k3s" and not metallb
    load_balancer = "kube-vip" if kube_vip else "metallb" if metallb else "servicelb" if servicelb else ""
    value = {
        "distribution": distribution,
        "version": next((re.sub(r"^v", "", k) for k in kubelets if k), ""),
        "harvester": harvester,
        "longhorn": "longhorn.io" in groups,
        "kubevirt": "kubevirt.io" in groups,
        # CDI fills VM disks from images; without it a VM starts from a blank one.
        "cdi": "cdi.kubevirt.io" in groups,
        "helm_controller": "helm.cattle.io" in groups,
        "metrics": "metrics.k8s.io" in groups,
        "load_balancer": load_balancer,
        "control_plane": sorted(control),
        "arch": sorted({((n.get("status") or {}).get("nodeInfo") or {}).get("architecture", "") for n in nodes} - {""}),
    }
    _cached.update(at=time.time(), value=value)
    return value


def join_guide():
    """How to add a machine to a k3s or RKE2 cluster: the command, the server
    address, and where the token is - Homestead cannot read it."""
    p = detect()
    server = p["control_plane"][0] if p["control_plane"] else ""
    if p["distribution"] == "k3s":
        return {"distribution": "k3s", "server": server, "token_file": "/var/lib/rancher/k3s/server/node-token",
                "version": p["version"],
                "agent": (f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=\"v{p['version']}\" "
                          f"K3S_URL=https://{server or '<server>'}:6443 K3S_TOKEN=<token> sh -"),
                "server_join": (f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=\"v{p['version']}\" "
                                f"K3S_TOKEN=<token> sh -s - server --server https://{server or '<server>'}:6443"),
                "longhorn": "sudo apt-get install -y open-iscsi nfs-common   # or: sudo dnf install -y iscsi-initiator-utils nfs-utils"}
    if p["distribution"] == "rke2":
        return {"distribution": "rke2", "server": server, "token_file": "/var/lib/rancher/rke2/server/node-token",
                "version": p["version"],
                "config": f"server: https://{server or '<server>'}:9345\ntoken: <token>",
                "agent": f"curl -sfL https://get.rke2.io | INSTALL_RKE2_TYPE=agent INSTALL_RKE2_VERSION=\"v{p['version']}\" sh -\n"
                         "sudo mkdir -p /etc/rancher/rke2 && sudo nano /etc/rancher/rke2/config.yaml\n"
                         "sudo systemctl enable --now rke2-agent",
                "longhorn": "sudo apt-get install -y open-iscsi nfs-common   # or: sudo dnf install -y iscsi-initiator-utils nfs-utils"}
    return {"distribution": p["distribution"], "server": server}


# How a Service asks for its address. kube-vip - Harvester's load balancer -
# reads kube-vip.io/loadbalancerIPs, and that is all a Service gets unless
# MetalLB is what this cluster actually runs: then MetalLB's own annotations,
# with the sharing key it needs before two Services may share an address.
# Never both kinds on one cluster: two controllers claiming the same Services
# is how a cluster loses its addresses.
def vip_annotations(vip):
    if not vip:
        return {}
    try:
        metallb = detect().get("load_balancer") == "metallb"
    except Exception:
        metallb = False
    if metallb:
        return {"metallb.universe.tf/loadBalancerIPs": vip, "metallb.universe.tf/allow-shared-ip": "homestead"}
    return {"kube-vip.io/loadbalancerIPs": vip}


def metallb_pools():
    """MetalLB's address pools, in the shape Harvester's IP pools are read in."""
    pools = []
    for item in _items("/apis/metallb.io/v1beta1/ipaddresspools"):
        ranges = []
        for entry in (item.get("spec") or {}).get("addresses") or []:
            entry = str(entry)
            if "-" in entry:
                start, end = [part.strip() for part in entry.split("-", 1)]
                ranges.append({"rangeStart": start, "rangeEnd": end})
            else:
                ranges.append({"subnet": entry})
        pools.append({"metadata": {"name": item["metadata"]["name"], "labels": {}},
                      "spec": {"description": "MetalLB address pool", "ranges": ranges},
                      "status": {"conditions": [{"type": "Ready", "status": "True"}]}})
    return pools
