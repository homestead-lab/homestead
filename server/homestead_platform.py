"""What this cluster is, so pages offer what it can actually do.

Homestead grew up on Harvester, which brings Longhorn, KubeVirt, kube-vip and
RKE2's Helm controller with it. A plain k3s or RKE2 cluster may have some of
those or none. The API server says which groups it serves and the nodes say
which distribution runs them, so each page can check before it relies on one:
the VM pages need KubeVirt, the volume pages Longhorn, and so on. What is
missing is said, with how to add it, rather than failing.
"""
import json
import re
import shlex
import time
import urllib.error
import homestead_forced as FORCED
import homestead_kube as KUBE
import homestead_multus as MULTUS
import homestead_roles as ROLES
from homestead_version import VERSION

kget = None
_cached = {"at": 0.0, "value": None, "failed_at": 0.0}
TTL = 300
RETRY = 15          # after a failed read, the last good answer is used this long before asking again


def bind(_kget):
    global kget
    kget = _kget


def _groups():
    groups = kget("/apis").get("groups")
    if not isinstance(groups, list):
        raise ValueError("Kubernetes did not say which APIs it serves")
    return {g.get("name") for g in groups if isinstance(g, dict)}


def _listed(path):
    """Every row of a list detect() decides from, or an error: an empty list
    because the read failed would say a feature is not there."""
    try:
        return KUBE.items(path, read=kget)
    except KUBE.Incomplete as error:
        raise ValueError(f"Kubernetes returned an incomplete list for {path}") from error


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


# Where kube-vip runs outside Harvester: its own manifests' name, and the
# name its Helm chart - Homestead's add-on - gives it.
def _servicelb_running():
    """ServiceLB's svclb- DaemonSets, which it makes for each LoadBalancer
    Service it serves."""
    return any((d.get("metadata") or {}).get("name", "").startswith("svclb-")
               for d in _listed("/apis/apps/v1/namespaces/kube-system/daemonsets"))


KUBE_VIP_DAEMONSETS = ("/apis/apps/v1/namespaces/kube-system/daemonsets/kube-vip-ds",
                       "/apis/apps/v1/namespaces/kube-system/daemonsets/kube-vip")
VIP_CLASS = "kube-vip.io/kube-vip-class"


def _vip_class(daemonset):
    """The load balancer class a Service needs for kube-vip to take it: set
    when kube-vip takes only Services of its class, as it does beside k3s's
    ServiceLB. Empty when it takes every Service, as on Harvester."""
    if not daemonset:
        return ""
    containers = (((daemonset.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    env = {e.get("name"): str(e.get("value") or "") for c in containers for e in c.get("env") or []}
    if env.get("lb_class_only", "").lower() != "true":
        return ""
    return env.get("lb_class_name") or VIP_CLASS


def _vip_service_election(daemonset):
    if not daemonset:
        return None
    containers = (((daemonset.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    env = {e.get("name"): str(e.get("value") or "") for c in containers for e in c.get("env") or []}
    if "svc_election" in env:
        return env["svc_election"].lower() == "true"
    return None


def _vip_shared_lease(daemonset):
    """Conservatively enable the shared-lease API on verified newer kube-vip releases."""
    containers = (((daemonset or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers", [])
    for c in containers:
        version = re.search(r":v?(\d+)\.(\d+)\.(\d+)(?:@|$)", c.get("image", ""))
        if version and tuple(map(int, version.groups())) >= (1, 2, 3):
            return True
    return False


def detect(force=False):
    """What this cluster is and has, read at most every five minutes. A read
    that fails is never kept as the answer: the last good one is used, and
    the cluster is asked again after RETRY seconds. With no good answer yet
    it raises, rather than guess that KubeVirt, Longhorn or a load balancer
    is not there."""
    now, good = time.time(), _cached["value"]
    if not force and good is not None and now - _cached["at"] < TTL:
        return good
    if not force and good is not None and now - _cached["failed_at"] < RETRY:
        return good
    try:
        value = _read()
    except Exception as error:
        _cached["failed_at"] = time.time()      # when it failed: a read can hang for a while first
        if good is not None:
            return good
        raise ValueError(f"Homestead could not read what this cluster runs ({str(error)[:160]}); "
                         "try again in a moment") from error
    _cached.update(at=now, value=value, failed_at=0.0)
    return value


def _read():
    groups = _groups()
    nodes = _listed("/api/v1/nodes")
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
    kube_vip_ds = None
    for path in KUBE_VIP_DAEMONSETS:
        try:
            kube_vip_ds = kget(path)
            break
        except urllib.error.HTTPError as error:
            if error.code != 404:         # absent is a 404; anything else is not knowing
                raise
            error.close()
    kube_vip = harvester or kube_vip_ds is not None
    metallb = "metallb.io" in groups
    # k3s's own load balancer, unless something else is doing the job. Its
    # svclb- DaemonSets only appear with the first LoadBalancer Service, so
    # a k3s cluster with none yet still has it. RKE2 has the same ServiceLB,
    # off unless asked for - Homestead's installer asks - and there its
    # DaemonSets are the evidence. Missing it there put kube-vip on every
    # Service ServiceLB also served: it announced the nodes' own addresses
    # as VIPs, and taking one away took the node's address with it.
    servicelb = not metallb and (distribution == "k3s" or (distribution == "rke2" and _servicelb_running()))
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
        # Second networks for pods, which LAN networks need.
        "multus": harvester or ("k8s.cni.cncf.io" in groups and MULTUS.inspect(kget)["ready"]),
        "metrics": "metrics.k8s.io" in groups,
        "load_balancer": load_balancer,
        # k3s's own node-address load balancer, which stays beside kube-vip
        # when kube-vip only takes the Services that ask for it by class.
        "servicelb": servicelb,
        "vip_class": "" if harvester else _vip_class(kube_vip_ds),
        "vip_service_election": _vip_service_election(kube_vip_ds),
        "vip_shared_lease": _vip_shared_lease(kube_vip_ds),
        "control_plane": sorted(control),
        "arch": sorted({((n.get("status") or {}).get("nodeInfo") or {}).get("architecture", "") for n in nodes} - {""}),
    }
    return value


# The settings a joining server must share with the ones there (k3s refuses
# or misbehaves otherwise), and the local-path folder, which a server joining
# without it would point existing local-path volumes elsewhere (#364). Flags
# that take a value, then ones that do not. Nothing else is copied: the token,
# the agent token and a datastore address with its password never are.
SHARED_VALUE_FLAGS = ("--cluster-cidr", "--service-cidr", "--cluster-dns", "--cluster-domain", "--flannel-backend",
                      "--disable", "--egress-selector-mode", "--default-local-storage-path")
SHARED_SWITCHES = ("--disable-network-policy", "--disable-kube-proxy", "--disable-cloud-controller",
                   "--disable-helm-controller", "--secrets-encryption", "--embedded-registry")
SHARED_FLAGS = SHARED_VALUE_FLAGS + SHARED_SWITCHES


def shared_server_args(nodes):
    """The cluster-wide settings a server runs with, from the first Ready
    server's k3s.io/node-args (k3s records its arguments there, the token
    hidden), shell-quoted for a command line. ([], False) when none can be
    read - the guide then names the flags to copy by hand."""
    for node in nodes:
        meta = node.get("metadata") or {}
        labels = meta.get("labels") or {}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (node.get("status") or {}).get("conditions") or [])
        if not ready or not any(k in labels for k in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")):
            continue
        try:
            args = json.loads((meta.get("annotations") or {}).get("k3s.io/node-args") or "")
        except ValueError:
            continue
        if not isinstance(args, list):
            continue
        out, i = [], 0
        while i < len(args):
            flag, value = str(args[i]), None
            if "=" in flag:
                flag, value = flag.split("=", 1)
            if flag in SHARED_SWITCHES:
                # config.yaml's "x: false" arrives as --x=false: off, not on.
                if str(value).lower() not in ("false", "0", "no"):
                    out.append(flag)
            elif flag in SHARED_VALUE_FLAGS:
                if value is None and i + 1 < len(args):
                    i += 1
                    value = str(args[i])
                if value is not None:
                    out += [flag, shlex.quote(value)]
            i += 1
        return out, True
    return [], False


def join_storage():
    """Read the current setting; a failed read must never pick a layout."""
    try:
        value = kget("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/settings/default-data-path").get("value")
    except Exception:
        value = None
    return {"storage_path": value if isinstance(value, str) else None,
            "storage_supported": value in ("/mnt/longhorn-os", "/var/lib/longhorn"),
            "installer_url": f"https://raw.githubusercontent.com/homestead-lab/homestead/v{VERSION}/scripts/install.sh"}


def join_guide():
    """How to add a machine to a k3s or RKE2 cluster: the command, the server
    address, and where the token is - Homestead cannot read it."""
    p = detect()
    server = p["control_plane"][0] if p["control_plane"] else ""
    if p["distribution"] == "k3s":
        shared, known = shared_server_args(_items("/api/v1/nodes"))
        server_join = (f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=\"v{p['version']}\" "
                       f"K3S_TOKEN=<token> sh -s - server --server https://{server or '<server>'}:6443"
                       + "".join(f" {a}" for a in shared))
        return {**join_storage(), "distribution": "k3s", "server": server, "token_file": "/var/lib/rancher/k3s/server/node-token",
                "version": p["version"],
                "agent": (f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=\"v{p['version']}\" "
                          f"K3S_URL=https://{server or '<server>'}:6443 K3S_TOKEN=<token> sh -"),
                "server_join": server_join,
                # A server holding an etcd vote and nothing else (homestead_roles).
                "tiebreaker": server_join + f" --node-taint {ROLES.KEY}=true:NoSchedule --node-label {ROLES.KEY}=true",
                "shared_settings": shared, "settings_known": known, "shared_flags": list(SHARED_FLAGS),
                # The installer's checks for a small server, run before it joins.
                "check": ("curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh"
                          " | sudo HS_ROLE=tiebreaker sh -s -- --check"),
                "longhorn": "sudo apt-get install -y open-iscsi nfs-common   # or: sudo dnf install -y iscsi-initiator-utils nfs-utils"}
    if p["distribution"] == "rke2":
        return {**join_storage(), "distribution": "rke2", "server": server, "token_file": "/var/lib/rancher/rke2/server/node-token",
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
        p = detect()
    except Exception:
        p = {}
    if p.get("load_balancer") == "metallb":
        return {"metallb.universe.tf/loadBalancerIPs": vip, "metallb.universe.tf/allow-shared-ip": "homestead"}
    annotations = {"kube-vip.io/loadbalancerIPs": vip}
    if p.get("vip_shared_lease"):
        annotations["kube-vip.io/leaseName"] = "homestead-vip-" + vip.replace(".", "-").replace(":", "-")
    return annotations


def vip_spec(vip):
    """What a Service's spec needs for its VIP: kube-vip's class, where
    kube-vip takes only that class. k3s's ServiceLB leaves a Service with a
    class alone, so the two never claim the same one."""
    if not vip:
        return {}
    try:
        cls = detect().get("vip_class") or ""
    except Exception:
        cls = ""
    return {"loadBalancerClass": cls} if cls else {}


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


def _platform_route(request):
    if (request.query.get("force") or [""])[0] == "1":
        return FORCED.run("platform", lambda: detect(force=True))
    return detect()


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/platform/join"): ("viewer", lambda request: join_guide()),
    ("GET", "/api/platform"): ("viewer", lambda request: _platform_route(request)),
}
