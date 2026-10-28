"""The VM and logo code that lived in server.py (track 1, part 5): what a new
VM may be created with and creating one at its address, VM power, a k3s
cluster's VMs, VM image disks, the VM store loop, VM logos, the logo
choices and Helm charts' logos, and the logo routes server.py's handler
answered.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value (the same
object, for _cache)."""
import time
import homestead_api_v1 as API_V1
import homestead_cancel as CANCEL
import homestead_deploy_appstore as APPSTORE
import homestead_helm as HELM
import homestead_icons as ICONS
import homestead_vm_create as VM_CREATE
import homestead_ipam as IPAM
import homestead_isos as ISOS
import homestead_lan as LAN
import homestead_leader as LEADER
import homestead_logos as LOGOS
import homestead_names as NAMES
import homestead_network_vms as VMNET
import homestead_network_inventory as INV
import homestead_nodes as NODES
import homestead_platform as PLATFORM
import homestead_routes as MODULE_ROUTES
import homestead_vm_hardware as VM_HARDWARE
import homestead_vm_review as VM_REVIEW
import homestead_vms as VMS
import homestead_vmstore as VMSTORE
import homestead_workloads as WORKLOADS

ROUTE_NEEDS = ("beat", "cached", "class_selectable", "_dns_name", "kget", "ksend",
               "selectable_storage_classes", "self_data_activity", "storage_class_facts",
               "storage_classes", "vm_network_details", "vm_networks", "vm_subnets")
VALUES = ("DATA_DIR", "_cache")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_vm_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_vm_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def set_vm_logos(b):
    """Gives VMs a logo: an image (kept like a workload's), one of the OS
    logos, or neither - back to the OS the VM reports. Only the VM's own
    annotations change, so it does not restart."""
    items = b.get("items") or []
    if not items:
        raise ValueError("choose at least one VM")
    if len(items) > 200:
        raise ValueError("at most 200 VMs at a time")
    plans = []
    for item in items:
        ns, name = _dns_name(item.get("ns"), "namespace"), _dns_name(item.get("name"), "VM name")
        source, os_key = str(item.get("icon") or "").strip(), str(item.get("os") or "").strip()
        if source and not source.startswith(("https://", "http://")):
            raise ValueError("a logo is a public http:// or https:// address")
        if os_key and os_key not in LOGOS.OS_KEYS:
            raise ValueError(f"there is no OS logo {os_key[:40]}")
        plans.append((ns, name, source, os_key))
    done, failed = 0, []
    for ns, name, source, os_key in plans:
        try:
            cached_ref = ICONS.persist(source, DATA_DIR) if source else ""
            ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
                  {"metadata": {"annotations": {NAMES.key("icon"): cached_ref or None,
                                                NAMES.key("icon-source"): source or None,
                                                NAMES.key("logo-os"): os_key or None}}},
                  ctype="application/merge-patch+json")
            done += 1
        except Exception as e:
            failed.append(f"{name}: {e}")
    _cache.pop("vms", None)
    detail = (f"{done} logo{'s' if done != 1 else ''} saved" if any(s or o for *_, s, o in plans)
              else "back to its OS's logo") + (f"; {len(failed)} could not be: " + "; ".join(failed[:3]) if failed else "")
    return {"ok": not failed, "done": done, "failed": failed, "detail": detail}


def helm_chart_logos():
    """{(namespace, release): {name, icon}} for every Helm release whose chart names a logo."""
    try:
        rows = cached("helm-logos", 60, HELM.releases)
    except Exception:
        return {}
    return {(r["namespace"], r["name"]): {"name": r.get("chart") or r["name"], "icon": r.get("icon", "")}
            for r in rows if r.get("icon")}


def logo_choices(ns, name, term, kind="workload"):
    """The picker's tiles for one workload: its chart's logo and its image's
    match, then a search. A VM has no image, so it is the search alone."""
    workload = None if kind == "vm" else next(
        (w for w in cached("wl", 5, WORKLOADS.get_workloads) if w.get("ns") == ns and w.get("name") == name), None)
    images = (workload or {}).get("images") or []
    chart = helm_chart_logos().get((ns, (workload or {}).get("helm_release"))) if workload else None
    return {"tiles": LOGOS.suggest(APPSTORE.fetch_appstore(), images, term if term is not None else name, APPSTORE.search_appstore,
                                   chart=chart), "images": images}


def vm_create_options():
    """What the New VM form can offer on this cluster."""
    platform = PLATFORM.detect()
    rows = storage_classes()
    nodes = kget("/api/v1/nodes").get("items", [])
    return {"harvester": platform.get("harvester", False), "cdi": platform.get("cdi", False),
            "distribution": platform.get("distribution", ""),
            "storage_classes": selectable_storage_classes(rows),
            "storage_class_facts": storage_class_facts(rows),
            "default_class": vm_default_class(rows),
            "images": VM_CREATE.list_vm_images() if platform.get("harvester") else [],
            "store": VMSTORE.choices() if (platform.get("harvester") or platform.get("cdi")) else [],
            "networks": vm_networks(),
            "network_details": vm_network_details(),
            "vm_network_options": VMNET.vm_network_options(NODES.node_temps()),
            "subnets": vm_subnets(),
            "nodes": sorted(n["metadata"]["name"] for n in nodes),
            # The Hardware tab: CPU models every node offers, and KubeVirt's features.
            "cpu_models": VM_HARDWARE.cpu_models(nodes),
            "kubevirt_gates": kubevirt_gates(),
            # A new VM's hardware as the form starts it: a plain VM, as made here.
            "hardware_base": VM_HARDWARE.read({"spec": {"template": {"spec": {
                "domain": {"cpu": {"cores": 1}},
                **({"evictionStrategy": "LiveMigrate"} if platform.get("harvester") else {})}}}}),
            # ISOs a CD-ROM can hold now.
            "isos": [{"name": v["name"], "file": v["file"]} for v in ISOS.volumes() if v["state"] == "ready"]}


def kubevirt_gates():
    try:
        kv = (kget("/apis/kubevirt.io/v1/kubevirts").get("items") or [{}])[0]
    except Exception:
        return []
    return sorted(((((kv.get("spec") or {}).get("configuration") or {}).get("developerConfiguration") or {})
                   .get("featureGates") or []))


def create_vm_with_address(cfg):
    """A VM, and - when it has an address of its own - that address checked
    as free first and recorded in IP addresses after, under the VM's name."""
    static = cfg.get("static_ip") or None
    if static:
        problem = vm_address_problem(str(static.get("address") or "").strip())
        if problem:
            raise ValueError(problem)
    result = VM_CREATE.create_vm(cfg, PLATFORM.detect(), vm_default_class())
    if result.get("address"):
        try:
            IPAM.save_record({"ip": result["address"], "name": cfg.get("name", ""), "kind": "static",
                              "category": "server", "mac": result.get("mac", ""), "owner": "homestead",
                              "note": f"VM {cfg.get('namespace') or 'lab'}/{cfg.get('name', '')}"
                                      + (f" · {cfg['ipam_note']}" if cfg.get("ipam_note") else "")})
        except Exception:
            pass
    return result


def api_vm_power(ns, name, action):
    """Start, stop, restart, force off or reset a VM for /api/v1 (and MQTT): stop
    and force off as the app does; start, restart and reset through the same
    capacity review, taken as acknowledged when it
    is not blocked - the key's scope is the consent the app asks a person for.
    A start that needs a person (missing TPM or EFI state) is refused."""
    if action not in ("start", "stop", "restart", "force-off", "reset"):
        raise ValueError("start, stop, restart, force-off or reset")
    # The API says force-off; the app and KubeVirt call it force-stop.
    action = "force-stop" if action == "force-off" else action
    body = {"ns": _dns_name(ns, "namespace"), "name": _dns_name(name, "VM name"), "action": action}
    if action in ("stop", "force-stop"):
        result = VM_REVIEW.reviewed_vm_power(body)
        _cache.pop("vms", None)
        return {"warnings": [], "job": ((result or {}).get("operation") or {}).get("id") if isinstance(result, dict) else None}
    # As the app's route does before a review: an older VM's read-only ISO
    # would stop KubeVirt starting it.
    ISOS.unlock(body["ns"])
    preview = VM_REVIEW.preview_vm_power(body)
    plan = preview["capacity"]
    if plan.get("blocked"):
        raise API_V1.ApiError(409, "it cannot start: " + "; ".join(plan.get("blockers") or ["not enough room"]),
                              blockers=list(plan.get("blockers") or []))
    if (plan.get("vm") or {}).get("state_initialization"):
        raise API_V1.ApiError(409, "starting it needs its TPM or EFI state set up first; do that in the app")
    result = VM_REVIEW.reviewed_vm_power({**body, "capacity_token": preview["capacity_token"], "confirm_capacity": True})
    _cache.pop("vms", None)
    job = (result.get("operation") or {}).get("id") if isinstance(result, dict) else None
    return {"warnings": list(plan.get("warnings") or []), "job": job}


def _remove_cluster_vm(ns, node):
    """One VM of a k3s cluster whose build stopped part-way: gone with its
    disks, and its address free again."""
    VMS.delete(ns, node["name"], with_disks=True)
    CANCEL.forget_addresses({node["address"]: node["name"]})


def _vm_image_disks():
    try:
        return {f"{row['namespace']}/{row['name']}": row.get("disks") or [] for row in VM_CREATE.vm_image_cache()["images"]}
    except Exception:
        return {}


def _vmstore_loop():
    """Kept VM images brought up to date, on the leader only; each image is
    asked about at most twice a day."""
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    VMSTORE.refresh()
                    # ISO copies no VM has used for a while (homestead_isos.py).
                    for name in ISOS.tidy():
                        print(f"ISO library: removed {name}, unused for {ISOS.keep_days()} days", flush=True)
                beat("vmstore", 3600, leader_only=True)
            except Exception as error:
                beat("vmstore", 3600, error, leader_only=True)
        time.sleep(3600)


# The routes server.py's handler answered for this code.
def _logos(request):
    q = request.query
    try:
        return logo_choices((q.get("ns") or [""])[0], (q.get("name") or [""])[0],
                            (q.get("q") or [None])[0], (q.get("kind") or ["workload"])[0])
    except Exception as e:
        return MODULE_ROUTES.Reply(502, {"error": f"app feed unavailable: {e}"})


def _logos_missing(request):
    try:
        apps = APPSTORE.fetch_appstore()
    except Exception as e:
        return MODULE_ROUTES.Reply(502, {"error": f"app feed unavailable: {e}"})
    return {"apps": LOGOS.missing(cached("wl", 5, WORKLOADS.get_workloads), apps, helm_chart_logos())}


ROUTES = {
    ("GET", "/api/logos"): ("viewer", _logos),
    ("GET", "/api/logos/missing"): ("viewer", _logos_missing),
}


def prepare_lan(cfg):
    """A container's own LAN address, checked as free and its network made,
    before the Deployment that joins it."""
    if cfg.get("network_mode") != "lan":
        return cfg
    lan = LAN.clean(cfg.get("lan") or {})
    problem = vm_address_problem(lan["address"])
    if problem:
        raise ValueError(problem)
    cfg["lan"] = lan
    return cfg


def vm_default_class(rows=None):
    """The class a new VM disk lands on: the cluster's default, else Longhorn's
    usual one, else none named - the API server then applies its own default."""
    rows = [row for row in (rows if rows is not None else storage_classes()) if class_selectable(row)]
    for row in rows:
        if row["default"]:
            return row["name"]
    names = [row["name"] for row in rows]
    return next((name for name in ("longhorn-r2", "harvester-longhorn", "longhorn") if name in names), "")


def vm_address_problem(ip):
    """Why a VM cannot have this address, or "": something already has it."""
    import ipaddress
    try:
        view = IPAM.view()
    except Exception:
        view = {"subnets": []}
    for subnet in view.get("subnets") or []:
        if ipaddress.ip_address(ip) not in ipaddress.ip_network(subnet["cidr"]):
            continue
        if subnet.get("gateway") == ip:
            return f"{ip} is the gateway of {subnet['cidr']}"
        row = next((r for r in subnet.get("rows") or [] if r["ip"] == ip), None)
        if row and (row.get("name") or row.get("kind") or row.get("cluster")):
            return f"{ip} is taken: {row.get('name') or row.get('cluster') or row.get('kind')} in IP addresses"
        if row and (row.get("scan") or {}).get("up"):
            return f"{ip} answered the last scan: something is already there"
        start, end = subnet.get("dhcp_start"), subnet.get("dhcp_end")
        if start and end and int(ipaddress.ip_address(start)) <= int(ipaddress.ip_address(ip)) <= int(ipaddress.ip_address(end)):
            return f"{ip} is inside the DHCP range {start}-{end}: the router may hand it to something else"
    try:
        network = cached("network", 5, INV.inventory)
    except Exception:
        network = {}
    problem = INV.cluster_address_problem(ip, network)
    if problem:
        return problem
    # A few ports a machine usually has, quickly: a free address answers none.
    if IPAM._probe(ip, ports=(22, 80, 443, 3389, 6443), timeout=0.4).get("up"):
        return f"{ip} answers on the network: something is already there"
    return ""
