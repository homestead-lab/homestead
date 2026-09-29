"""Homestead's own front doors on a VIP: its web page, the backup store and
SMB - the services Homestead runs for itself.

Harvester gives Homestead a VIP when it is installed. A fresh k3s install
has none, so those services start on the nodes' own addresses (ServiceLB):
an address that stops answering when that node is down. Once a VIP is
reserved, Homestead moves in beside the node addresses rather than instead
of them: each service gets a second Service, <name>-vip, on the VIP with the
same ports, and the old one keeps working - nothing is recreated, nothing
drops. (Changing an existing Service from ServiceLB to kube-vip would mean
recreating it: its load-balancer class cannot change.)

The backup store's Longhorn target follows it onto the VIP, since backups
should outlive any one node. NFS is not here: it always has a VIP of its own
already, as it must see the client's own address.
"""

kget = network = objects = None
WEB_NS = "lab"
WEB = "homestead"
WEB_PORT = 8088
WEB_TARGET = 8080
SMB_NS = "lab"
SMB = "homestead-smb"


def bind(_kget, _network, _objects, web_ns, web_target, smb_ns, smb_name):
    global kget, network, objects, WEB_NS, WEB_TARGET, SMB_NS, SMB
    kget, network, objects = _kget, _network, _objects
    WEB_NS, WEB_TARGET, SMB_NS, SMB = web_ns, int(web_target), smb_ns, smb_name


def _components():
    return [{"id": "web", "label": "Homestead's web page", "namespace": WEB_NS, "workload": WEB},
            {"id": "objectstore", "label": "Backup storage (S3)", "namespace": objects.NS, "workload": objects.NAME},
            {"id": "smb", "label": "Network shares (SMB)", "namespace": SMB_NS, "workload": SMB}]


def _vip_of(row, node_ips):
    return next((ip for ip in (row.get("requested_ips") or []) + (row.get("external_ips") or []) if ip not in node_ips), "")


def report(state=None):
    """Each of Homestead's own services: whether it runs, and where it answers."""
    state = state or network.inventory()
    node_ips = set(state.get("node_ips") or [])
    workloads = {(w["namespace"], w["name"]) for w in state.get("workloads") or []}
    rows = []
    for c in _components():
        services = [s for s in state.get("services") or [] if s["namespace"] == c["namespace"]
                    and c["workload"] in (s.get("targets") or []) and s.get("type") == "LoadBalancer"]
        vips = [(s, _vip_of(s, node_ips)) for s in services]
        on_vip = [(s, ip) for s, ip in vips if ip]
        on_nodes = [s for s, ip in vips if not ip and s.get("external_ips")]
        ports = (services[0]["ports"] if services else
                 [{"name": "http", "port": WEB_PORT, "target_port": WEB_TARGET, "protocol": "TCP"}] if c["id"] == "web" else [])
        rows.append({**c, "present": (c["namespace"], c["workload"]) in workloads,
                     "vip": on_vip[0][1] if on_vip else "", "vip_service": on_vip[0][0]["name"] if on_vip else "",
                     "node_addresses": sorted({ip for s in on_nodes for ip in s.get("external_ips") or []}),
                     "ports": [{"name": p.get("name") or "", "port": p.get("port"), "target_port": p.get("target_port"),
                                "protocol": p.get("protocol") or "TCP"} for p in ports]})
    web = next(r for r in rows if r["id"] == "web")
    return {"components": rows, "shared_vip": (state.get("shared_vip") or {}).get("ip", ""),
            "url": f"http://{web['vip']}:{WEB_PORT}" if web["vip"] else "",
            "on_vip": all(r["vip"] for r in rows if r["present"])}


def _config(row, vip, state):
    names = {s["name"] for s in state.get("services") or [] if s["namespace"] == row["namespace"]}
    name, n = f"{row['workload']}-vip", 2
    while name in names:
        name, n = f"{row['workload']}-vip-{n}", n + 1
    return {"namespace": row["namespace"], "workload": row["workload"], "workload_kind": "Deployment", "name": name,
            "type": "LoadBalancer", "vip_mode": "manual", "vip": vip,
            "ports": [{"name": p["name"] or f"port-{i + 1}", "port": p["port"], "target_port": p["target_port"] or p["port"],
                       "protocol": p["protocol"]} for i, p in enumerate(row["ports"])]}


def plan(vip):
    """What moving onto vip would add, checked as each Service would be."""
    state = network.inventory()
    steps = []
    for row in report(state)["components"]:
        if not row["present"]:
            continue
        if row["vip"] == vip:
            steps.append({"id": row["id"], "label": row["label"], "action": "kept", "detail": f"already on {vip}"})
            continue
        if not row["ports"]:
            steps.append({"id": row["id"], "label": row["label"], "action": "skipped", "detail": "it has no ports to publish"})
            continue
        cfg = _config(row, vip, state)
        try:
            checked = network.service_plan(cfg)
            cfg["reviewed_vip"] = checked["vip"]
            steps.append({"id": row["id"], "label": row["label"], "action": "add", "config": cfg,
                          "detail": f"{', '.join(str(p['port']) for p in cfg['ports'])} on {vip}, as {cfg['name']}",
                          "warnings": checked.get("warnings") or []})
        except (ValueError, PermissionError) as error:
            steps.append({"id": row["id"], "label": row["label"], "action": "refused", "detail": str(error)[:240]})
    return {"vip": vip, "steps": steps}


def move(vip):
    """Add each service's Service on vip, and point Longhorn's backups there."""
    done = plan(vip)
    for step in done["steps"]:
        if step["action"] != "add":
            continue
        try:
            network.create_service(step["config"])
            step["action"] = "added"
        except (ValueError, PermissionError) as error:
            step.update(action="refused", detail=str(error)[:240])
        step.pop("config", None)
        if step["id"] == "objectstore" and step["action"] == "added":
            try:
                objects.point_longhorn()
                step["detail"] += "; Longhorn's backup target follows it"
            except Exception as error:
                step["detail"] += f"; Longhorn's backup target was not changed: {str(error)[:120]}"
    for step in done["steps"]:
        step.pop("config", None)
    return done
