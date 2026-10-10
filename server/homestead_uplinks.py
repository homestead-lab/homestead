"""Harvester uplinks: which NICs carry each cluster network, bonded or not.

On Harvester a cluster network (ClusterNetwork) is a set of LAN networks that
share an uplink, and a VlanConfig says which NICs make that uplink on which
hosts and how they are bonded. Harvester's network controller then builds the
bond (<network>-bo) and bridge (<network>-br) on each host it matches and
reports each one in a VlanStatus. So Homestead never touches a host here: it
writes one object, Harvester makes the change, and Homestead follows the
VlanStatus of each host until it is ready.

The mgmt network is the exception. Its uplink is set when Harvester installs
and Harvester refuses a VlanConfig for it, so it is shown, never changed.

Homestead makes one VlanConfig per host (<network>-<host>), selected by host
name, so a host's NICs are its own. A VlanConfig made in Harvester's dashboard
for several hosts is shown and can be changed too; the review names every host
it reaches.

Harvester refuses changing or removing a VlanConfig while VMs on its networks
run on the hosts it reaches; the review says which, before anything is sent.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import homestead_operations as OPS
import homestead_routes

kget = ksend = None
API = "/apis/network.harvesterhci.io/v1beta1"
NADS = "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions"
CN_LABEL = "network.harvesterhci.io/clusternetwork"
MANAGED = "homestead.io/managed"
MODES = ("active-backup", "802.3ad", "balance-tlb", "balance-alb", "balance-xor", "balance-rr", "broadcast")
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,10}[a-z0-9])?$")   # <name>-br fits Linux's 15 characters
NIC = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")                    # a Linux interface name
WAIT = 300                    # how long a host has to report its uplink ready
KIND = "harvester-uplink"
# What the node probes report (server.py's node_temps): each host's NICs.
node_probes = lambda: {}


def bind(_kget, _ksend, _node_probes=None):
    global kget, ksend, node_probes
    kget, ksend = _kget, _ksend
    node_probes = _node_probes or node_probes


def _items(path):
    try:
        return (kget(path) or {}).get("items", []) or []
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise


def applies():
    try:
        kget(f"{API}/clusternetworks")
        return True
    except Exception:
        return False


def _ready(conditions):
    row = next((c for c in conditions or [] if str(c.get("type", "")).lower() == "ready"), None)
    return (row or {}).get("status") == "True", (row or {}).get("message") or (row or {}).get("reason") or ""


def _matches(selector, labels):
    return all((labels or {}).get(k) == v for k, v in (selector or {}).items())


def _nodes():
    return {n["metadata"]["name"]: n["metadata"].get("labels") or {} for n in _items("/api/v1/nodes")}


def _config_row(vc, nodes, statuses):
    spec, meta = vc.get("spec") or {}, vc.get("metadata") or {}
    uplink = spec.get("uplink") or {}
    bond = uplink.get("bondOptions") or {}
    matched = sorted(n for n, labels in nodes.items() if _matches(spec.get("nodeSelector"), labels))
    status = {}
    for node in matched:
        found = statuses.get((meta.get("name"), node))
        ready, message = _ready(((found or {}).get("status") or {}).get("conditions"))
        status[node] = {"ready": ready if found else None, "message": message if found else "not reported yet"}
    return {"name": meta.get("name", ""), "cluster_network": spec.get("clusterNetwork", ""), "nodes": matched,
            "selector": spec.get("nodeSelector") or {}, "nics": list(uplink.get("nics") or []),
            "mode": bond.get("mode") or "active-backup", "miimon": bond.get("miimon"),
            "mtu": (uplink.get("linkAttributes") or {}).get("mtu"),
            "homestead": (meta.get("labels") or {}).get(MANAGED) == "true",
            "resource_version": meta.get("resourceVersion", ""), "status": status}


def _host_nics(probes):
    """Each host's NICs from the node probe, and the bond each is in now."""
    out = {}
    for node, probe in (probes or {}).items():
        rows = (probe or {}).get("interfaces") or []
        out[node] = [{"name": r["name"], "link": "up" if r.get("carrier") else "down" if r.get("carrier") is False else "off",
                      "speed_mbps": r.get("speed_mbps"), "master": r.get("master", "")}
                     for r in rows if r.get("kind") == "nic"]
    return out


def inventory(probes=None):
    """Every cluster network, its uplinks and each host's state, and each
    host's NICs with what uses them."""
    if not applies():
        return {"applies": False, "networks": [], "hosts": {}}
    nodes = _nodes()
    statuses = {}
    for vs in _items(f"{API}/vlanstatuses"):
        s = vs.get("status") or {}
        statuses[(s.get("vlanConfig"), s.get("node"))] = vs
    configs = [_config_row(vc, nodes, statuses) for vc in _items(f"{API}/vlanconfigs")]
    networks = []
    for cn in sorted(_items(f"{API}/clusternetworks"), key=lambda c: (c["metadata"]["name"] != "mgmt", c["metadata"]["name"])):
        name = cn["metadata"]["name"]
        mine = [c for c in configs if c["cluster_network"] == name]
        covered = {n for c in mine for n in c["nodes"]}
        ready, message = _ready((cn.get("status") or {}).get("conditions"))
        networks.append({"name": name, "mgmt": name == "mgmt", "ready": ready, "message": message,
                         "configs": mine, "uncovered": [] if name == "mgmt" else sorted(set(nodes) - covered),
                         "lan_networks": _lan_networks(name)})
    hosts = _host_nics(probes)
    # Who holds each NIC: the bond it is in on the host, or a VlanConfig that
    # names it for that host and has not been built yet.
    for node, nics in hosts.items():
        claimed = {nic: c["cluster_network"] for c in configs if node in c["nodes"] for nic in c["nics"]}
        for nic in nics:
            master = nic["master"]
            nic["used_by"] = (master[:-3] if master.endswith(("-bo", "-br")) else master) or claimed.get(nic["name"], "")
    return {"applies": True, "networks": networks, "hosts": hosts, "modes": list(MODES)}


def _lan_networks(cn):
    try:
        rows = _items(f"{NADS}?labelSelector=" + urllib.parse.quote(f"{CN_LABEL}={cn}", safe=""))
    except Exception:
        rows = []
    return sorted(f"{r['metadata']['namespace']}/{r['metadata']['name']}" for r in rows)


def _vms_in_the_way(cn, nodes):
    """Running VMs on these hosts with a network on this cluster network:
    Harvester refuses to change the uplink under them."""
    networks = set(_lan_networks(cn))
    if not networks or not nodes:
        return []
    found = []
    for vmi in _items("/apis/kubevirt.io/v1/virtualmachineinstances"):
        meta, spec = vmi.get("metadata") or {}, vmi.get("spec") or {}
        if (vmi.get("status") or {}).get("nodeName") not in nodes:
            continue
        ns = meta.get("namespace", "")
        for net in spec.get("networks") or []:
            name = (net.get("multus") or {}).get("networkName", "")
            if name and (name if "/" in name else f"{ns}/{name}") in networks:
                found.append(f"{ns}/{meta.get('name', '')}")
                break
    return sorted(found)


def _config_name(cn, node):
    base = re.sub(r"[^a-z0-9-]", "-", f"{cn}-{node}".lower()).strip("-")
    return base[:63].rstrip("-")


def _int(value, default=None):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value} is not a number")


def preview(cfg, probes=None):
    """What a change would write and what stands in its way. Writes nothing.

    cfg action:
      create  {cluster_network, new_network, nodes, nics, mode, miimon, mtu, allow_down, lacp_confirmed}
      change  {config, nics, mode, miimon, mtu, allow_down, lacp_confirmed}
      remove  {config}
    """
    inv = inventory(probes)
    if not inv["applies"]:
        raise ValueError("this is not a Harvester cluster: bonds on k3s and RKE2 hosts are made on the host itself")
    action = str(cfg.get("action") or "create")
    networks = {n["name"]: n for n in inv["networks"]}
    configs = {c["name"]: c for n in inv["networks"] for c in n["configs"]}
    refusals, warnings, writes = [], [], []
    if action in ("change", "remove"):
        config = configs.get(str(cfg.get("config") or ""))
        if not config:
            raise ValueError(f"Harvester has no uplink {cfg.get('config')}")
        cn, nodes = config["cluster_network"], config["nodes"]
    else:
        config = None
        cn = str(cfg.get("cluster_network") or "").strip().lower()
        nodes = sorted({str(n) for n in cfg.get("nodes") or []})
        if cn == "mgmt":
            refusals.append("mgmt's uplink is set when Harvester installs, and Harvester does not change it afterwards. "
                            "Follow Harvester's own procedure to change the management NICs.")
        elif cfg.get("new_network"):
            if not NAME.match(cn):
                refusals.append("a cluster network name is 1 to 12 lower-case letters, digits or hyphens, so its bridge "
                                f"({cn or 'name'}-br) fits Linux's 15-character limit")
            elif cn in networks:
                refusals.append(f"Harvester already has a cluster network {cn}; add an uplink to it instead")
        elif cn not in networks:
            refusals.append(f"Harvester has no cluster network {cn or '(none chosen)'}")
        known = set(inv["hosts"]) | {n for net in inv["networks"] for c in net["configs"] for n in c["nodes"]} | \
            {n for net in inv["networks"] for n in net["uncovered"]}
        if not nodes:
            refusals.append("choose at least one host")
        for node in nodes:
            if node not in known:
                refusals.append(f"there is no host {node}")
            other = next((c for c in (networks.get(cn) or {}).get("configs", []) if node in c["nodes"]), None)
            if other:
                refusals.append(f"{node} already has an uplink for {cn} ({other['name']}); change that one instead")
    if action == "remove":
        vms = _vms_in_the_way(cn, nodes)
        if vms:
            refusals.append(f"Harvester will not take the uplink away while these VMs on {cn}'s networks run on "
                            f"{', '.join(nodes)}: {', '.join(vms)}. Stop or move them first.")
        lan = (networks.get(cn) or {}).get("lan_networks") or []
        if lan:
            warnings.append(f"{', '.join(lan)} lose their way out on {', '.join(nodes)} until another uplink is made")
        writes.append({"verb": "DELETE", "path": f"{API}/vlanconfigs/{config['name']}"})
        plan = {"action": action, "cluster_network": cn, "config": config["name"], "nodes": nodes, "nics": config["nics"],
                "mode": config["mode"], "refusals": refusals, "warnings": warnings, "writes": writes}
        plan["digest"] = _digest(plan)
        return plan

    nics = [str(n) for n in dict.fromkeys(cfg.get("nics") or (config or {}).get("nics") or [])]
    mode = str(cfg.get("mode") or (config or {}).get("mode") or "active-backup")
    miimon = _int(cfg.get("miimon"), (config or {}).get("miimon") or 100)
    mtu = _int(cfg.get("mtu"), (config or {}).get("mtu") or 0)
    if not nics:
        refusals.append("choose at least one NIC")
    for nic in nics:
        if not NIC.match(nic):
            refusals.append(f"{nic[:40]} is not a NIC name")
    if mode not in MODES:
        refusals.append(f"{mode} is not a bond mode Harvester knows")
    if mode == "802.3ad" and len(nics) > 1 and not cfg.get("lacp_confirmed"):
        refusals.append("802.3ad needs the switch ports to be one LACP group: confirm they are, or choose active-backup, "
                        "which works on any switch")
    if mtu and not 576 <= mtu <= 9000:
        refusals.append(f"an MTU of {mtu} is outside 576 to 9000; leave it empty for Harvester's default (1500)")
    if mode != "active-backup" and len(nics) > 1:
        warnings.append(f"{mode} spreads traffic over the NICs and needs the switch to agree; active-backup does not")
    for node in nodes:
        have = {n["name"]: n for n in inv["hosts"].get(node) or []}
        if not have:
            warnings.append(f"the node probe has not reported {node}'s NICs, so they could not be checked")
            continue
        for nic in nics:
            row = have.get(nic)
            if not row:
                refusals.append(f"{node} has no NIC {nic}")
                continue
            if row["used_by"] and row["used_by"] != cn:
                refusals.append(f"{nic} on {node} already carries {row['used_by']}"
                                + (": mgmt's NICs are Harvester's own" if row["used_by"] == "mgmt" else ""))
            if row["link"] != "up" and not cfg.get("allow_down"):
                refusals.append(f"{nic} on {node} has no link: plug it in, or confirm you want it in the bond anyway")
            elif row["link"] != "up":
                warnings.append(f"{nic} on {node} has no link; the bond starts without it")
        speeds = {have[n]["speed_mbps"] for n in nics if n in have and have[n]["speed_mbps"] and have[n]["link"] == "up"}
        if len(speeds) > 1:
            warnings.append(f"on {node} the NICs run at different speeds ({', '.join(f'{s} Mb/s' for s in sorted(speeds))})")
    if action == "change":
        vms = _vms_in_the_way(cn, nodes)
        if vms:
            refusals.append(f"Harvester will not change the uplink while these VMs on {cn}'s networks run on "
                            f"{', '.join(nodes)}: {', '.join(vms)}. Stop or move them first.")
    uplink = {"nics": nics}
    if mtu:
        uplink["linkAttributes"] = {"mtu": mtu}
    uplink["bondOptions"] = {"mode": mode, "miimon": miimon}
    if action == "create":
        if cfg.get("new_network"):
            writes.append({"verb": "POST", "path": f"{API}/clusternetworks",
                           "body": {"apiVersion": "network.harvesterhci.io/v1beta1", "kind": "ClusterNetwork",
                                    "metadata": {"name": cn, "labels": {MANAGED: "true"}}}})
        for node in nodes:
            writes.append({"verb": "POST", "path": f"{API}/vlanconfigs",
                           "body": {"apiVersion": "network.harvesterhci.io/v1beta1", "kind": "VlanConfig",
                                    "metadata": {"name": _config_name(cn, node), "labels": {MANAGED: "true"}},
                                    "spec": {"clusterNetwork": cn, "nodeSelector": {"kubernetes.io/hostname": node},
                                             "uplink": uplink}}})
    else:
        if len(nodes) > 1:
            warnings.append(f"{config['name']} is the uplink of {len(nodes)} hosts ({', '.join(nodes)}): they all change")
        writes.append({"verb": "PATCH", "path": f"{API}/vlanconfigs/{config['name']}",
                       "body": {"metadata": {"resourceVersion": config["resource_version"]}, "spec": {"uplink": uplink}}})
    plan = {"action": action, "cluster_network": cn, "config": (config or {}).get("name", ""), "nodes": nodes,
            "nics": nics, "mode": mode, "miimon": miimon, "mtu": mtu or None, "new_network": bool(cfg.get("new_network")),
            "lan_networks": (networks.get(cn) or {}).get("lan_networks") or [],
            "refusals": list(dict.fromkeys(refusals)), "warnings": list(dict.fromkeys(warnings)), "writes": writes}
    plan["digest"] = _digest(plan)
    return plan


def _digest(plan):
    keep = {k: plan[k] for k in ("action", "cluster_network", "config", "nodes", "nics", "mode", "writes") if k in plan}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def apply(cfg, ops, probes=None):
    """Review again, write what was reviewed, and follow it as a job. Admin only."""
    plan = preview(cfg, probes)
    if plan["refusals"]:
        raise ValueError(plan["refusals"][0])
    if cfg.get("digest") and cfg["digest"] != plan["digest"]:
        raise ValueError("the hosts' networks changed since this was reviewed: review it again")
    for write in plan["writes"]:
        body = write.get("body")
        if write["verb"] == "PATCH":
            ksend("PATCH", write["path"], body, ctype="application/merge-patch+json")
        elif body is None:
            ksend(write["verb"], write["path"])
        else:
            ksend(write["verb"], write["path"], body)
    cn, nodes = plan["cluster_network"], plan["nodes"]
    names = [w["body"]["metadata"]["name"] for w in plan["writes"] if w["verb"] == "POST" and w["body"]["kind"] == "VlanConfig"] \
        or [plan["config"]]
    verb = {"create": "Make", "change": "Change", "remove": "Remove"}[plan["action"]]
    what = f"{' + '.join(plan['nics'])} ({plan['mode']})" if plan["action"] != "remove" else ""
    title = f"{verb} {cn}'s uplink on {', '.join(nodes)}" + (f": {what}" if what else "")
    ref = {"action": plan["action"], "cluster_network": cn, "configs": names, "nodes": nodes, "since": time.time()}
    return ops.start(KIND, title, {"kind": "VlanConfig", "name": names[0]}, "/networking", ref,
                     f"Sent to Harvester; waiting for {', '.join(nodes)} to report {cn}'s uplink")


def status(item, now=None):
    """Each host's VlanStatus for the uplink: (status, progress, message)."""
    ref, now = item["ref"], time.time() if now is None else now
    elapsed = now - ref.get("since", now)
    rows = {}
    for vs in _items(f"{API}/vlanstatuses"):
        s = vs.get("status") or {}
        if s.get("vlanConfig") in ref["configs"]:
            rows[s.get("node")] = _ready(s.get("conditions"))
    nodes = ref["nodes"]
    if ref["action"] == "remove":
        left = [n for n in nodes if n in rows]
        if not left:
            return "succeeded", 100, f"{ref['cluster_network']}'s uplink is gone from {', '.join(nodes)}"
        if elapsed > WAIT:
            return "failed", 100, f"Harvester has not taken the uplink away from {', '.join(left)} after {WAIT // 60} minutes"
        return "running", 50, f"Harvester is taking the uplink away from {', '.join(left)}"
    ready = [n for n in nodes if rows.get(n, (False, ""))[0]]
    if len(ready) == len(nodes) and elapsed > 10:
        return "succeeded", 100, f"{ref['cluster_network']}'s uplink is ready on {', '.join(nodes)}"
    waiting = [n for n in nodes if n not in ready]
    said = "; ".join(f"{n}: {rows[n][1]}" for n in waiting if n in rows and rows[n][1])
    if elapsed > WAIT:
        return ("failed", 100, f"{', '.join(waiting)} did not report {ref['cluster_network']}'s uplink ready in "
                f"{WAIT // 60} minutes" + (f" ({said})" if said else "") +
                ". Harvester keeps trying; check the NICs and the VlanStatus in Harvester's dashboard")
    progress = 10 + int(80 * len(ready) / max(1, len(nodes)))
    return "running", progress, f"Waiting for {', '.join(waiting)}" + (f": {said}" if said else "")


def _apply_route(request):
    op = apply(request.body, OPS, node_probes())
    homestead_routes.forget("ports", "network")
    return {"ok": True, "operation": op, "detail": "Sent to Harvester; follow each host in the job tray"}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/network/uplinks"): ("viewer", lambda request: inventory(node_probes())),
    ("POST", "/api/network/uplinks/preview"): ("admin", lambda request: preview(request.body, node_probes())),
    ("POST", "/api/network/uplinks/apply"): ("admin", _apply_route),
}
