"""The network code that lived in server.py (track 1, part 3): LAN edits and
their IPAM record, the VM networks and subnets a new VM may join, the
installer's address, the VIP and UniFi loops, and MQTT's snapshot with the
preview route server.py's handler answered.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value (the same
object, for _cache and _lock)."""
import json
import time
import urllib.request, urllib.parse, urllib.error
import homestead_fleet as FLEET
import homestead_ipam as IPAM
import homestead_lan as LAN
import homestead_leader as LEADER
import homestead_mqtt as MQTT
import homestead_networking as NETWORK
import homestead_platform as PLATFORM
import homestead_self_address as SELF_ADDRESS
import homestead_vips as VIPS

ROUTE_NEEDS = ("beat", "cached", "get_overview", "kget", "ksend", "vm_address_problem")
VALUES = ("SYS_NS", "_cache", "_lock")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_network_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_network_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def _unifi_loop():
    """Hourly on the leader, while UniFi is connected: its clients and names
    as they are now, rather than as they were when someone last pressed Sync."""
    time.sleep(120)
    while True:
        if LEADER.is_leader():
            try:
                if IPAM.sync_due():
                    IPAM.sync_unifi()
                beat("unifi", 600, leader_only=True)
            except Exception as error:
                # The failure is recorded on the card (last_error); the loop carries on.
                beat("unifi", 600, leader_only=True)
                print(f"unifi: {str(error)[:160]}", flush=True)
        time.sleep(600)


def edit_lan(ns, name, wanted):
    """Give a container its own LAN address, change it, or take it away."""
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    current = LAN.read(dep)
    if not wanted:
        if not current:
            return ""
        LAN.apply_to_template(dep, ns, name, None)
        dep["metadata"].pop("managedFields", None)
        ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
        LAN.remove_nad(ns, name)
        return f"{name} no longer has a LAN address of its own"
    lan = LAN.clean(wanted)
    if current == lan:
        return ""
    if not current or current.get("address") != lan["address"]:
        problem = vm_address_problem(lan["address"])
        if problem:
            raise ValueError(problem)
    LAN.ensure_nad(ns, name, lan)
    LAN.apply_to_template(dep, ns, name, lan)
    dep["metadata"].pop("managedFields", None)
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    record_lan(ns, name, lan)
    return f"{name} answers on {lan['address']} on the LAN"


def record_lan(ns, name, lan):
    try:
        IPAM.save_record({"ip": lan["address"], "name": name, "kind": "static", "category": "server",
                          "owner": "homestead", "note": f"container {ns}/{name}, on {lan['network']}"})
    except Exception:
        pass


def vm_networks():
    """The pod network and every network attachment (Multus) a VM can join."""
    try:
        items = kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions").get("items", [])
    except Exception:
        items = []
    return ["pod"] + sorted(f"{i['metadata']['namespace']}/{i['metadata']['name']}" for i in items)


def vm_network_details(strict=False):
    """Each VM network, and whether it puts a VM on the LAN with an address
    of its own - a bridge, as Harvester's VM networks are. Strict reads let
    setup distinguish unavailable inventory from no configured networks."""
    try:
        items = kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions").get("items", [])
    except urllib.error.HTTPError as error:
        if strict and error.code != 404:
            raise
        items = []
    except Exception:
        if strict:
            raise
        items = []
    out = []
    for item in items:
        try:
            config = json.loads((item.get("spec") or {}).get("config") or "{}")
        except ValueError:
            config = {}
        labels = item["metadata"].get("labels") or {}
        out.append({"name": f"{item['metadata']['namespace']}/{item['metadata']['name']}",
                    "type": config.get("type", ""), "vlan": config.get("vlan"),
                    "bridge": config.get("bridge", "") or config.get("master", "")
                              or ((item["metadata"].get("annotations") or {}).get("k8s.v1.cni.cncf.io/resourceName") or "").rsplit("/", 1)[-1],
                    "kind": labels.get("network.harvesterhci.io/type", ""),
                    # On the LAN: a bridge carries VMs and containers; macvlan
                    # gives containers a MAC of their own but cannot carry a
                    # VM; macvtap carries VMs only.
                    "lan": config.get("type") in ("bridge", "macvlan", "macvtap"),
                    "vms": config.get("type") in ("bridge", "macvtap"),
                    "containers": config.get("type") in ("bridge", "macvlan")})
    return sorted(out, key=lambda row: row["name"])


def vm_subnets():
    """The subnets IP addresses knows, with the free addresses outside DHCP."""
    try:
        view = IPAM.view()
    except Exception:
        return []
    return [{"cidr": s["cidr"], "name": s.get("name", ""), "gateway": s.get("gateway", ""),
             "dhcp_start": s.get("dhcp_start", ""), "dhcp_end": s.get("dhcp_end", ""),
             "free": s.get("free_list") or s.get("next_free") or []} for s in view.get("subnets") or []]


def installer_vip(vip):
    """The VIP the installer was given: reserved, the apps' default, and
    Homestead's own services on it beside the nodes' addresses."""
    NETWORK.add_vips({"ip": vip, "label": "Homestead and apps"}, IPAM.load()[0].get("records") or {})
    NETWORK.set_default_vip(vip)
    moved = SELF_ADDRESS.move(vip)
    for key in ("network", "ov"):
        _cache.pop(key, None)
    _follow_fleet_address()
    return "; ".join(f"{s['label']}: {s['action']} ({s['detail']})" for s in moved["steps"]) or f"{vip} reserved"


VIP_QUICK = 5          # a look for a missing address
VIP_FULL = 30          # a full pass regardless


def _vip_loop():
    """kube-vip can answer for an address and the Service lose it from its
    status - k3s's ServiceLB empties it when the pods change - and then every
    port there is refused. The leader records it again, as kube-vip would
    have (homestead_vips.py): within seconds of it going missing (#365), and
    in a full pass every VIP_FULL seconds."""
    last_full = 0.0
    while True:
        if LEADER.is_leader():
            try:
                platform = PLATFORM.detect()
                now = time.time()
                if now - last_full >= VIP_FULL or VIPS.missing(platform):
                    last_full = now
                    if VIPS.keep(platform):
                        with _lock:
                            _cache.pop("network", None)
                            _cache.pop("flow2", None)
                beat("vips", VIP_FULL, leader_only=True)
            except Exception as error:
                beat("vips", VIP_FULL, error, leader_only=True)
                print(f"VIPs: {str(error)[:160]}", flush=True)
        time.sleep(VIP_QUICK)


def mqtt_snapshot():
    """The numbers hv-exporter published, counted the way it counted them."""
    o = cached("ov", 10, get_overview)
    pods = kget("/api/v1/pods").get("items", [])
    try:
        vmis = kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
    except Exception:
        vmis = []
    system = lambda p: p["metadata"]["namespace"] in SYS_NS
    running = [p for p in pods if (p.get("status") or {}).get("phase") == "Running"]
    bad = [p for p in pods if (p.get("status") or {}).get("phase") in ("Failed", "Pending")]
    namespaces = {}
    for p in pods:
        if not system(p):
            namespaces[p["metadata"]["namespace"]] = namespaces.get(p["metadata"]["namespace"], 0) + 1
    ready = o.get("nodes_ready", 0)
    total = o.get("nodes_total", 0)
    notready = total - ready
    degraded, faulted = o.get("vol_degraded", 0), o.get("vol_faulted", 0)
    wl_bad = sum(1 for p in bad if not system(p))
    sys_bad = sum(1 for p in bad if system(p))
    health = ("critical" if notready or faulted else "degraded" if degraded or wl_bad or sys_bad else "healthy")
    nodes = []
    for node in o.get("nodes") or []:
        name = node["name"]
        mine = [p for p in running if (p.get("spec") or {}).get("nodeName") == name]
        nodes.append({"name": name, "cpu_pct": node.get("cpu_pct", 0), "mem_pct": node.get("mem_pct", 0),
                      "mem_gb": node.get("mem_used_gb", 0), "rx_mbps": round(node.get("rx_mbps", 0) or 0, 2),
                      "tx_mbps": round(node.get("tx_mbps", 0) or 0, 2), "pods": len(mine),
                      "vms": sum(1 for v in vmis if (v.get("status") or {}).get("nodeName") == name),
                      "wl": ", ".join(node.get("workloads") or []) or "none", "status": node.get("status", "NotReady")})
    return {"cluster": {"nodes_ready": ready, "nodes_total": total, "nodes_notready": notready,
                        "vol_total": o.get("volumes", 0), "vol_degraded": degraded, "vol_faulted": faulted,
                        "vol_one_host": o.get("vol_one_host", 0),
                        "pods_system": sum(1 for p in running if system(p)),
                        "pods_workload": sum(1 for p in running if not system(p)),
                        "pods_sys_bad": sys_bad, "pods_wl_bad": wl_bad,
                        "vms_running": sum(1 for v in vmis if (v.get("status") or {}).get("phase") == "Running"),
                        "health": health, "wl_summary": " ".join(f"{ns}:{n}" for ns, n in sorted(namespaces.items())),
                        "cpu_pct": o.get("cpu_pct", 0), "mem_pct": o.get("mem_pct", 0)},
            "nodes": nodes}


def _follow_fleet_address():
    """Linked clusters reach this one on its VIP once it is there."""
    try:
        return FLEET.follow_address()
    except Exception as error:
        print(f"linked clusters: were not told the new address: {str(error)[:160]}", flush=True)
        return ""


# The routes server.py's handler answered for this code.
def _mqtt_preview(request):
    snap = mqtt_snapshot()
    return {"states": [{"topic": t, "payload": v} for t, v in MQTT.states(MQTT.load(), snap)]}


ROUTES = {
    ("GET", "/api/mqtt/preview"): ("viewer", _mqtt_preview),
}
