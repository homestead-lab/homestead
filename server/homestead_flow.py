"""The Architecture page's picture: which nodes hold each volume's copies,
which workloads and VMs use those volumes, and the ports and VIPs that reach
them - read from the cluster as it is now."""
import homestead_hardware as HW
import homestead_names as NAMES
import homestead_networking as NETWORK
import homestead_routes
import homestead_vms as VMS

# server.py's: the Kubernetes read, the namespaces that are the platform's
# own, a workload's logo, the copy count of a volume part-way through a move,
# and its readers of quantities and timestamps.
kget = None
SYS_NS = set()
display_icon = lambda annotations: ""
moving_copy = lambda volume: 0
parse_cpu = parse_mem = lambda value: 0
age_secs = lambda timestamp: 0


def bind(_kget, sys_ns, _display_icon, _moving_copy, _parse_cpu, _parse_mem, _age_secs):
    global kget, SYS_NS, display_icon, moving_copy, parse_cpu, parse_mem, age_secs
    kget, SYS_NS = _kget, sys_ns
    display_icon, moving_copy = _display_icon, _moving_copy
    parse_cpu, parse_mem, age_secs = _parse_cpu, _parse_mem, _age_secs


def get_flow2():
    """Architecture view: node(replica copies) -> volume -> workload(+ports) -> VIP."""
    pods = [p for p in kget("/api/v1/pods").get("items", [])
            if p["metadata"]["namespace"] not in SYS_NS
            # File browsers, import/copy jobs and the other short-lived pods
            # Homestead creates are implementation details, not architecture.
            # The task label is shared by all of those helpers and survives a
            # rename, unlike matching one current name such as
            # homestead-files-*.
            and not NAMES.label_of(p.get("metadata") or {}, "task")]
    svcs = [s for s in kget("/api/v1/services").get("items", [])
            if s["metadata"]["namespace"] not in SYS_NS]
    try:
        lhvols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        lhvols = []
    try:
        lhreps = kget("/apis/longhorn.io/v1beta2/replicas").get("items", [])
    except Exception:
        lhreps = []
    try:
        vmis = kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
    except Exception:
        vmis = []
    try:
        vms = kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
    except Exception:
        vms = []
    try:
        dep_meta = {(d["metadata"]["namespace"], d["metadata"]["name"]):
                    d["metadata"].get("annotations", {}) or {}
                    for d in kget("/apis/apps/v1/deployments").get("items", [])}
    except Exception:
        dep_meta = {}

    # --- volumes, keyed by their PVC name where possible
    vols, vol_by_pvc = [], {}
    for v in lhvols:
        st = v.get("status", {}) or {}
        ks = st.get("kubernetesStatus", {}) or {}
        pvc = ks.get("pvcName") or ""
        vid = v["metadata"]["name"]
        entry = {
            "id": "v:" + vid, "name": pvc or vid[:18], "raw": vid, "pvc": pvc,
            "replicas": moving_copy(v) or v.get("spec", {}).get("numberOfReplicas", 0),
            "robustness": "healthy" if moving_copy(v) else st.get("robustness", "unknown"),
            "state": st.get("state", ""),
            "size_gb": round(int(v.get("spec", {}).get("size", 0) or 0) / 1024**3, 1),
            "attached": st.get("currentNodeID", ""),
        }
        vols.append(entry)
        if pvc:
            vol_by_pvc[pvc] = entry

    # --- replica copies grouped by node
    nodes = {}
    for r in lhreps:
        sp = r.get("spec", {}) or {}
        nid, vn = sp.get("nodeID"), sp.get("volumeName")
        if not nid or not vn:
            continue
        v = next((x for x in vols if x["raw"] == vn), None)
        nodes.setdefault(nid, []).append({
            "vol": v["name"] if v else vn[:16], "vid": "v:" + vn,
            "running": (r.get("status", {}) or {}).get("currentState") == "running",
        })
    if not nodes:  # fallback when replica CRs are unreadable
        for v in vols:
            if v["attached"]:
                nodes.setdefault(v["attached"], []).append(
                    {"vol": v["name"], "vid": v["id"], "running": True})

    # --- ports & VIPs per app. The address a Service asks for, not only the
    # one it carries: a VIP kube-vip answers for but never recorded is where
    # the app is meant to be, and the page says why it is not reachable there.
    try:
        network = homestead_routes.cached("network", 5, NETWORK.inventory)
    except Exception:
        network = {}
    places = network.get("addresses") or {"nodes": [], "addresses": []}
    wanted = {(row["namespace"], row["name"]): (row.get("requested_ips") or row.get("assigned_ips") or [None])[0]
              for row in network.get("services") or [] if row.get("type") == "LoadBalancer"}

    def address_of(s):
        ing = s.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
        return wanted.get((s["metadata"]["namespace"], s["metadata"]["name"])) or (ing[0].get("ip") if ing else None)

    ports_by_app, vips = {}, {}
    for s in svcs:
        app = (s["spec"].get("selector") or {}).get("app")
        if not app:
            continue
        vip = address_of(s)
        for prt in s["spec"].get("ports", []) or []:
            rec = {"port": prt.get("port"), "name": prt.get("name") or "tcp", "vip": vip}
            ports_by_app.setdefault(app, []).append(rec)
            if vip:
                vips.setdefault(vip, []).append({"port": prt.get("port"), "app": app})

    def vm_ports(ns, name, labels):
        """A VM's ports: those of each Service in its namespace that selects
        it - by the labels on its pods, such as Harvester's vmName - and not
        by app, which is how a container's are found."""
        out = []
        for s in svcs:
            selector = s["spec"].get("selector") or {}
            if (s["metadata"]["namespace"] != ns or not selector or "app" in selector
                    or any(labels.get(k) != v for k, v in selector.items())):
                continue
            vip = address_of(s)
            for prt in s["spec"].get("ports", []) or []:
                out.append({"port": prt.get("port"), "name": prt.get("name") or "tcp", "vip": vip})
                if vip:
                    vips.setdefault(vip, []).append({"port": prt.get("port"), "app": name})
        return out

    # --- per-pod live metrics for the architecture cards
    try:
        pmet = {}
        for m in kget("/apis/metrics.k8s.io/v1beta1/pods").get("items", []):
            c = sum(parse_cpu(x.get("usage", {}).get("cpu")) for x in m.get("containers", []))
            mm = sum(parse_mem(x.get("usage", {}).get("memory")) for x in m.get("containers", []))
            pmet[(m["metadata"]["namespace"], m["metadata"]["name"])] = (c, mm)
    except Exception:
        pmet = {}

    # --- workloads
    seen, wls, launchers = set(), [], {}
    for p in pods:
        labels = p["metadata"].get("labels", {}) or {}
        # A VM runs in a virt-launcher pod; it is shown as the VM, below, not
        # as a container - and every VM's launcher is not one app.
        if labels.get("kubevirt.io") or labels.get("vm.kubevirt.io/name"):
            if labels.get("kubevirt.io") == "virt-launcher" and p["status"].get("phase") == "Running":
                launchers[(p["metadata"]["namespace"], labels.get("vm.kubevirt.io/name")
                           or labels.get("kubevirt.io/domain", ""))] = p
            continue
        app = labels.get("app") or p["metadata"]["name"].rsplit("-", 2)[0]
        if app in seen:
            continue
        seen.add(app)
        claims = []
        for vol in p["spec"].get("volumes", []) or []:
            cn = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if cn:
                claims.append({"pvc": cn, "vid": vol_by_pvc[cn]["id"] if cn in vol_by_pvc else ""})
        cu, mu = pmet.get((p["metadata"]["namespace"], p["metadata"]["name"]), (0, 0))
        wls.append({
            "id": "w:" + app, "name": app, "kind": "container",
            "node": p["spec"].get("nodeName", ""),
            "phase": p["status"].get("phase", ""),
            "uptime": age_secs(p["status"].get("startTime")),
            "cpu": round(cu, 3), "mem_mb": round(mu / 1048576, 1),
            "ns": p["metadata"]["namespace"],
            "image": (p["spec"].get("containers") or [{}])[0].get("image", ""),
            "icon": display_icon(dep_meta.get((p["metadata"]["namespace"], app), {})),
            "hardware": HW.workload_features(p["spec"], dep_meta.get((p["metadata"]["namespace"], app), {})),
            "gpu": any("dri" in (m.get("mountPath") or "")
                       for c in p["spec"].get("containers", []) for m in (c.get("volumeMounts") or [])),
            "claims": claims, "ports": ports_by_app.get(app, []),
        })
    # --- virtual machines, running or not: a stopped VM's disks are still here
    vmi_by = {(v["metadata"]["namespace"], v["metadata"]["name"]): v for v in vmis}
    known = [(v, vmi_by.get((v["metadata"]["namespace"], v["metadata"]["name"]), {})) for v in vms]
    named = {(v["metadata"]["namespace"], v["metadata"]["name"]) for v in vms}
    # An instance made without a VirtualMachine is shown by itself.
    known += [({"metadata": v["metadata"], "spec": {"template": {"metadata": {"labels": (v["metadata"].get("labels") or {})},
                                                                "spec": v.get("spec") or {}}}}, v)
              for key, v in vmi_by.items() if key not in named]
    for vm, vmi in known:
        ns, nm = vm["metadata"]["namespace"], vm["metadata"]["name"]
        if ns in SYS_NS:
            continue
        # Its disks as it runs now (hot-plugged ones too), else as defined.
        volumes = ((vmi.get("spec") or {}).get("volumes")
                   or (((vm.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or [])
        addresses = [a for i in (vmi.get("status") or {}).get("interfaces") or []
                     for a in (i.get("ipAddresses") or [i.get("ipAddress")]) if a and ":" not in a]
        row = {"disks": [{"claim": VMS._volume_claim(v)} for v in volumes], "ip": addresses[0] if addresses else "",
               "status": VMS._status(vm, vmi), "node": (vmi.get("status") or {}).get("nodeName", ""),
               "running": (vmi.get("status") or {}).get("phase") == "Running"}
        launcher = launchers.get((ns, nm))
        cu, mu = pmet.get((ns, launcher["metadata"]["name"]), (0, 0)) if launcher else (0, 0)
        pod_labels = dict((launcher or {}).get("metadata", {}).get("labels") or {})
        pod_labels.update(((vm.get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels") or {})
        wls.append({
            "id": "w:vm-" + nm, "name": nm, "kind": "vm", "ns": ns,
            "node": row.get("node") or (vmi.get("status") or {}).get("nodeName", ""),
            "phase": (vmi.get("status") or {}).get("phase", "") or "Stopped",
            "state": str(row.get("status") or ""),
            "running": bool(row.get("running")), "ip": row.get("ip", ""),
            "uptime": age_secs((launcher or {}).get("status", {}).get("startTime")) if launcher else 0,
            "cpu": round(cu, 3), "mem_mb": round(mu / 1048576, 1),
            "image": "", "icon": "", "gpu": False, "hardware": [],
            "claims": [{"pvc": d["claim"], "vid": vol_by_pvc[d["claim"]]["id"] if d["claim"] in vol_by_pvc else ""}
                       for d in row.get("disks") or [] if d.get("claim")],
            "ports": vm_ports(ns, nm, pod_labels),
        })

    # Every node, with its own addresses and the VIPs it answers for - a node
    # holding no replica still holds addresses.
    place = {row["ip"]: row for row in places["addresses"]}
    hosts = {row["name"]: row for row in places["nodes"]}
    names = sorted(set(nodes) | set(hosts))
    return {
        "nodes": [{"id": "n:" + k, "name": k, "copies": sorted(nodes.get(k, []), key=lambda x: x["vol"]),
                   "ips": (hosts.get(k) or {}).get("ips", []), "vips": (hosts.get(k) or {}).get("vips", [])}
                  for k in names],
        "volumes": sorted(vols, key=lambda x: x["name"]),
        "workloads": sorted(wls, key=lambda x: x["name"]),
        "vips": [{"id": "i:" + ip, "ip": ip, "ports": sorted(p, key=lambda x: x["port"]),
                  "kind": (place.get(ip) or {}).get("kind", "vip"), "node": (place.get(ip) or {}).get("node", ""),
                  "state": (place.get(ip) or {}).get("state", "ok"), "reason": (place.get(ip) or {}).get("reason", "")}
                 for ip, p in sorted(vips.items(), key=lambda item: (
                     (place.get(item[0]) or {}).get("kind") != "node",
                     tuple(int(x) if x.isdigit() else 999 for x in item[0].split("."))))],
    }


def view():
    """The picture, kept eight seconds under server.py's key "flow2" - which it
    drops when a Service or VIP changes."""
    return homestead_routes.cached("flow2", 8, get_flow2)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/flow"): ("viewer", lambda request: view()),
}
