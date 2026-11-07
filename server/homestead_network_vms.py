"""VM networks: the networks a VM, or a container given a LAN address, can join,
and making one (#623, from homestead_networking).
"""
import json
import re
import urllib.error
import homestead_network_state as STATE


# A VM (or a container given a LAN address) is on the LAN through a VM network:
# on Harvester, a bridge on one of its cluster networks - mgmt is the hosts'
# own - untagged, or on a VLAN. Harvester's dashboard makes them under
# Networks > VM Networks; this makes the same object, so it shows there too.
#
# Elsewhere (k3s, RKE2, any cluster with Multus) there are no cluster
# networks, only the hosts' own interfaces: a bridge already on the hosts
# carries VMs and containers alike; a plain NIC carries containers through
# macvlan, each with a MAC of its own on the LAN. macvlan cannot carry a VM
# (KubeVirt bridges the VM's own MAC behind it), so VMs need a bridge.
CLUSTER_NETWORKS = "/apis/network.harvesterhci.io/v1beta1/clusternetworks"
NAD_API = "/apis/k8s.cni.cncf.io/v1"
MULTUS_HELP = ("Multus is not installed, so pods cannot join a second network. On k3s and RKE2, "
               "install it from Settings > Cluster > Add-ons. Elsewhere, install Multus with its own instructions")


def _macvtap():
    try:
        import homestead_macvtap as MACVTAP
        return MACVTAP.inspect()["ready"]
    except Exception:
        return False


def _multus():
    import homestead_multus as MULTUS
    return MULTUS.inspect(STATE.kget)["ready"]


def host_interfaces(probes):
    """Each interface the node probes saw, with the nodes it is on. A LAN
    network is one object for every node, so the ones on all of them lead."""
    seen, nodes = {}, sorted(probes or {})
    for node in nodes:
        for iface in (probes[node] or {}).get("interfaces") or []:
            row = seen.setdefault(iface["name"], {"name": iface["name"], "kind": iface.get("kind", ""),
                                                  "nodes": [], "master": iface.get("master", "")})
            row["nodes"].append(node)
            if iface.get("kind") == "bridge":
                row["kind"] = "bridge"
    rows = list(seen.values())
    for row in rows:
        row["everywhere"] = len(row["nodes"]) == len(nodes)
    # A NIC already in a bridge is carried by that bridge, not on its own.
    return sorted(rows, key=lambda r: (not r["everywhere"], r["kind"] != "bridge", bool(r["master"]), r["name"]))


def vm_network_options(probes=None):
    """Where a LAN network can be made: Harvester's cluster networks, or
    elsewhere the hosts' interfaces (and whether Multus is there to use them)."""
    try:
        items = STATE.kget(CLUSTER_NETWORKS).get("items", [])
    except Exception:
        return {"harvester": False, "cluster_networks": [], "multus": _multus(),
                "interfaces": host_interfaces(probes), "multus_help": MULTUS_HELP, "macvtap": _macvtap()}
    names = sorted(item["metadata"]["name"] for item in items)
    return {"harvester": True, "cluster_networks": names or ["mgmt"], "multus": True}


def _vlan(cfg):
    vlan = str(cfg.get("vlan") or "").strip()
    if vlan and (not vlan.isdigit() or not 1 <= int(vlan) <= 4094):
        raise ValueError("a VLAN ID is a number from 1 to 4094; leave it empty for the hosts' own, untagged LAN")
    return vlan


def _host_network_config(cfg, options, name):
    """The CNI config for a LAN network on the hosts' own interface."""
    if not options["multus"]:
        raise ValueError(MULTUS_HELP)
    iface = str(cfg.get("interface") or "").strip()
    if not iface or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface):
        raise ValueError("choose the host interface your LAN is on, like eth0 or br0")
    known = {row["name"]: row for row in options.get("interfaces") or []}
    row = known.get(iface) or {"kind": "bridge" if iface.startswith(("br", "vmbr", "virbr")) else "nic"}
    vlan = _vlan(cfg)
    if row.get("master") and known.get(row["master"], {}).get("kind") == "bridge":
        # The NIC is inside a bridge already: ride the bridge, as the host does.
        iface, row = row["master"], known[row["master"]]
    if row["kind"] == "bridge":
        config = {"cniVersion": "0.3.1", "name": name, "type": "bridge", "bridge": iface, "promiscMode": True, "ipam": {}}
        if vlan:
            config["vlan"] = int(vlan)
        return config, iface, True, {}
    if str(cfg.get("for") or "") == "vms":
        # A VM on the NIC itself: macvtap, which carries the VM's own address
        # (homestead_macvtap). Its device plugin offers physical links and
        # bonds, not VLAN interfaces - a VLAN for VMs wants a host bridge.
        if not options.get("macvtap"):
            raise ValueError("Install macvtap first, or use a host bridge. VMs join a LAN on a network interface through it: "
                             "it is under Settings → Cluster → Add-ons (Required components)")
        if vlan:
            raise ValueError("a VM network on a VLAN needs a host bridge; macvtap is offered on the untagged LAN only")
        import homestead_macvtap as MACVTAP
        return MACVTAP.nad_config(name), iface, True, {"k8s.v1.cni.cncf.io/resourceName": MACVTAP.resource(iface)}
    master = f"{iface}.{vlan}" if vlan else iface
    if vlan and known and master not in known:
        # macvlan rides a host interface; a VLAN needs the host's own for it.
        raise ValueError(f"the hosts have no interface for VLAN {vlan}. Add {master} on {iface} on each host "
                         f"(ip link add link {iface} name {master} type vlan id {vlan}), or use a host bridge")
    return {"cniVersion": "0.3.1", "name": name, "type": "macvlan", "master": master, "mode": "bridge", "ipam": {}}, master, False, {}


def create_vm_network(cfg):
    options = vm_network_options(cfg.get("_probes"))
    name = STATE._name(cfg.get("name"), "network name")
    namespace = STATE._name(cfg.get("namespace") or "default", "namespace")
    labels, annotations = {}, {}
    if options["harvester"]:
        cluster = str(cfg.get("cluster_network") or "mgmt")
        if cluster not in options["cluster_networks"]:
            raise ValueError(f"Harvester has no cluster network {cluster}")
        vlan = _vlan(cfg)
        config = {"cniVersion": "0.3.1", "name": name, "type": "bridge", "bridge": f"{cluster}-br",
                  "promiscMode": True, "ipam": {}}
        labels = {"network.harvesterhci.io/clusternetwork": cluster}
        if vlan:
            config["vlan"] = int(vlan)
            labels.update({"network.harvesterhci.io/type": "L2VlanNetwork", "network.harvesterhci.io/vlan-id": vlan})
        else:
            labels["network.harvesterhci.io/type"] = "UntaggedNetwork"
        where = f"VLAN {vlan} on {cluster}" if vlan else f"the untagged LAN of {cluster}"
        joins = "VMs and containers"
    else:
        config, carrier, vms, annotations = _host_network_config(cfg, options, name)
        vlan = _vlan(cfg)
        where = f"{'VLAN ' + vlan + ' on ' if vlan and config['type'] == 'bridge' else ''}{carrier}"
        joins = ("VMs (macvtap)" if config["type"] == "macvtap" else "VMs and containers" if vms
                 else "containers (a VM needs a macvtap network or a host bridge)")
    path = f"{NAD_API}/namespaces/{namespace}/network-attachment-definitions"
    try:
        STATE.kget(f"{path}/{name}")
        raise ValueError(f"a LAN network {namespace}/{name} already exists")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    STATE.ksend("POST", path, {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
                         "metadata": {"name": name, "namespace": namespace, "labels": labels,
                                      **({"annotations": annotations} if annotations else {})},
                         "spec": {"config": json.dumps(config)}})
    return {"ok": True, "name": f"{namespace}/{name}",
            "detail": f"LAN network {namespace}/{name} made, on {where}; {joins} can join it now"}
