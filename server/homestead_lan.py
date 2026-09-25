"""A container with an address of its own on the LAN, beside its pod network.

A VM network bridged to the LAN (Harvester's VM networks are) gives no
addresses - its IPAM is empty, which suits a VM that runs its own DHCP client
or static config, but leaves a container's interface bare. So a container
given a LAN address gets a network of its own: a copy of the chosen one -
the same bridge and VLAN - whose IPAM is static, holding its one address. Its
pod joins that network as a second interface, lan0; the pod network stays
its default route, so everything else about it is unchanged. It answers on
its LAN address directly, without a Service - what an app that wants to be
found on the LAN (discovery, broadcasts) needs.

The copy is named <workload>-lan in the workload's namespace, and goes when
the workload does or its LAN address is taken away.
"""
import ipaddress
import json
import urllib.error

import homestead_names as NAMES

NETWORKS = "k8s.v1.cni.cncf.io/networks"
NAD_API = "/apis/k8s.cni.cncf.io/v1"
KEY = NAMES.key("lan")

kget = ksend = None


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


def nad_name(workload):
    return f"{workload[:59].rstrip('-')}-lan"


def clean(lan):
    """The LAN address asked for, checked: {network, address, prefix, gateway}."""
    network = str((lan or {}).get("network") or "")
    if "/" not in network:
        raise ValueError("choose the VM network the container's LAN address is on")
    try:
        iface = ipaddress.ip_interface(f"{str(lan.get('address') or '').strip()}/{int(lan.get('prefix') or 24)}")
    except ValueError as error:
        raise ValueError(f"{lan.get('address') or '(blank)'} is not an address like 192.0.2.70") from error
    if iface.version != 4 or iface.ip in (iface.network.network_address, iface.network.broadcast_address):
        raise ValueError(f"{iface.ip} cannot be a machine's address in {iface.network}")
    gateway = str(lan.get("gateway") or "").strip()
    if gateway and ipaddress.ip_address(gateway) not in iface.network:
        raise ValueError(f"the gateway {gateway} is outside {iface.network}")
    return {"network": network, "address": str(iface.ip), "prefix": iface.network.prefixlen, "gateway": gateway}


def nad_body(ns, workload, lan):
    """The container's own copy of the chosen network, with its address."""
    base_ns, base_name = lan["network"].split("/", 1)
    try:
        base = kget(f"{NAD_API}/namespaces/{base_ns}/network-attachment-definitions/{base_name}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError(f"there is no VM network {lan['network']}") from error
        raise
    try:
        config = json.loads((base.get("spec") or {}).get("config") or "{}")
    except ValueError:
        config = {}
    if config.get("type") != "bridge":
        raise ValueError(f"{lan['network']} is not a network bridged to the LAN, so it cannot give an address on it")
    address = {"address": f"{lan['address']}/{lan['prefix']}"}
    if lan.get("gateway"):
        address["gateway"] = lan["gateway"]
    own = {key: config[key] for key in ("cniVersion", "type", "bridge", "vlan", "mtu", "promiscMode") if key in config}
    own.update(name=nad_name(workload), ipam={"type": "static", "addresses": [address]})
    return {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
            "metadata": {"name": nad_name(workload), "namespace": ns,
                         "labels": {NAMES.key("managed"): "true", "app": workload},
                         "annotations": {KEY: json.dumps(lan)}},
            "spec": {"config": json.dumps(own)}}


def ensure_nad(ns, workload, lan):
    body = nad_body(ns, workload, lan)
    path = f"{NAD_API}/namespaces/{ns}/network-attachment-definitions"
    try:
        current = kget(f"{path}/{body['metadata']['name']}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        current = None
    if current:
        body["metadata"]["resourceVersion"] = current["metadata"].get("resourceVersion")
        ksend("PUT", f"{path}/{body['metadata']['name']}", body)
    else:
        ksend("POST", path, body)


def pod_annotations(ns, workload):
    return {NETWORKS: json.dumps([{"name": nad_name(workload), "namespace": ns, "interface": "lan0"}])}


def apply_to_template(dep, ns, workload, lan):
    """Set (lan) or take away (None) the pod's LAN interface."""
    meta = dep["spec"]["template"].setdefault("metadata", {})
    annotations = meta.setdefault("annotations", {})
    if lan:
        annotations.update(pod_annotations(ns, workload))
        dep["metadata"].setdefault("annotations", {})[KEY] = json.dumps(lan)
    else:
        annotations.pop(NETWORKS, None)
        (dep["metadata"].get("annotations") or {}).pop(KEY, None)
    return dep


def read(dep):
    try:
        return json.loads(((dep.get("metadata") or {}).get("annotations") or {}).get(KEY) or "null")
    except ValueError:
        return None


def remove_nad(ns, workload):
    try:
        ksend("DELETE", f"{NAD_API}/namespaces/{ns}/network-attachment-definitions/{nad_name(workload)}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
