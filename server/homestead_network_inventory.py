"""The network picture: every Service, VIP, port, endpoint and ingress, who owns
each address, and whether an address is free to use (#623, from
homestead_networking).
"""
import ipaddress
import homestead_names as NAMES
import homestead_platform as PLATFORM
import homestead_vips as VIPS
import homestead_network_state as STATE


def _endpoint_index(slices):
    out = {}
    for item in slices:
        meta = item.get("metadata", {}) or {}
        ns = meta.get("namespace", "")
        service = (meta.get("labels", {}) or {}).get("kubernetes.io/service-name", "")
        if not service:
            continue
        entry = out.setdefault((ns, service), {"ready": [], "not_ready": [], "ports": []})
        for port in item.get("ports", []) or []:
            row = {"name": port.get("name") or "", "port": port.get("port"),
                   "protocol": port.get("protocol") or "TCP"}
            if row not in entry["ports"]:
                entry["ports"].append(row)
        for endpoint in item.get("endpoints", []) or []:
            conditions = endpoint.get("conditions", {}) or {}
            row = {"addresses": endpoint.get("addresses", []) or [],
                   "node": endpoint.get("nodeName") or "",
                   "target_kind": (endpoint.get("targetRef") or {}).get("kind", ""),
                   "target": (endpoint.get("targetRef") or {}).get("name", ""),
                   "terminating": bool(conditions.get("terminating", False))}
            ready = conditions.get("ready") is not False and not row["terminating"]
            entry["ready" if ready else "not_ready"].append(row)
    return out


def _access_url(ip, port, name, protocol):
    if not ip or str(protocol).upper() != "TCP":
        return "", False
    port = int(port)
    label = str(name or "").lower()
    if port in (443, 8443, 9443) or "https" in label:
        return f"https://{ip}:{port}", True
    if port in (80, 3000, 5000, 8000, 8080, 8088, 8123) or label in ("http", "web", "ui"):
        return f"http://{ip}:{port}", True
    if port == 1883 or "mqtt" in label:
        return f"mqtt://{ip}:{port}", False
    if port == 8883:
        return f"mqtts://{ip}:{port}", False
    if port == 445 or "smb" in label:
        return f"smb://{ip}", False
    if port == 53 or "dns" in label:
        return f"dns://{ip}:{port}", False
    return f"{ip}:{port}", False


def _pool_inventory(objects, reserved):
    pools, candidates = [], []
    for item in objects:
        meta, spec, status = item.get("metadata", {}) or {}, item.get("spec", {}) or {}, item.get("status", {}) or {}
        ranges = []
        for row in spec.get("ranges", []) or []:
            start, end = row.get("rangeStart"), row.get("rangeEnd")
            values = []
            try:
                if start and end:
                    first, last = ipaddress.ip_address(start), ipaddress.ip_address(end)
                    if first.version == last.version == 4 and int(last) >= int(first):
                        # UI candidate discovery is intentionally bounded even if a broad pool is configured.
                        values = [str(ipaddress.ip_address(number))
                                  for number in range(int(first), min(int(last), int(first) + 4095) + 1)]
                elif row.get("subnet"):
                    network = ipaddress.ip_network(row["subnet"], strict=False)
                    if network.version == 4 and network.num_addresses <= 4096:
                        values = [str(address) for address in network.hosts()]
            except ValueError:
                values = []
            ranges.append({"start": start or "", "end": end or "", "subnet": row.get("subnet") or "",
                           "gateway": row.get("gateway") or "", "candidate_count": len(values)})
            candidates.extend(address for address in values if address not in reserved)
        ready = next((condition.get("status") == "True" for condition in status.get("conditions", []) or []
                      if condition.get("type") == "Ready"), None)
        pools.append({"name": meta.get("name", ""), "description": spec.get("description", ""),
                      "global": (meta.get("labels", {}) or {}).get(
                          "loadbalancer.harvesterhci.io/global-ip-pool") == "true",
                      "ready": ready, "total": int(status.get("total", 0) or 0),
                      "reported_available": int(status.get("available", 0) or 0), "ranges": ranges})
    return pools, sorted(set(candidates), key=lambda value: int(ipaddress.ip_address(value)))


def _ingress_inventory(ingresses):
    out = []
    for item in ingresses:
        meta, spec, status = item.get("metadata", {}) or {}, item.get("spec", {}) or {}, item.get("status", {}) or {}
        addresses = [row.get("ip") or row.get("hostname") for row in
                     (status.get("loadBalancer", {}) or {}).get("ingress", []) or []]
        rules = []
        for rule in spec.get("rules", []) or []:
            for path in ((rule.get("http") or {}).get("paths", []) or []):
                service = ((path.get("backend") or {}).get("service") or {})
                port = service.get("port") or {}
                rules.append({"host": rule.get("host") or "*", "path": path.get("path") or "/",
                              "service": service.get("name") or "",
                              "port": port.get("number") or port.get("name") or ""})
        out.append({"namespace": meta.get("namespace", ""), "name": meta.get("name", ""),
                    "class": spec.get("ingressClassName") or "", "addresses": addresses, "rules": rules,
                    "system": meta.get("namespace", "") in STATE.SYSTEM_NAMESPACES})
    return sorted(out, key=lambda row: (row["namespace"], row["name"]))


# Harvester keeps its management address - the one its dashboard and host
# joining (RKE2's 9345) answer on - in this ConfigMap, whichever Service
# currently carries it.
HARVESTER_VIP = "/api/v1/namespaces/harvester-system/configmaps/vip"


def _ours(row):
    """A Service Homestead made, or may share an address with.

    The label is what Homestead writes now. Its own Service, and anything in
    the namespace apps are deployed to, count too: releases before the label
    made those.
    """
    return (row["managed"] or (row.get("selector") or {}).get("app") == "homestead"
            or row["namespace"] == STATE.DEFAULT_NAMESPACE)


SHARE_KEY = "share-vip"     # homestead.io/share-vip: "true" - share the address, without being Homestead's


def _shares(row, ip, platform_info):
    """A Service another tool made - Flux, Argo CD, a chart - that shares a
    VIP the way Homestead's own do, not one that takes it: kube-vip announces
    them together and only a port both claim conflicts, which is reported as
    that. It uses Homestead's shared lease for the address; or kube-vip here
    elects once for every Service, so all on an address go together; or it
    says so (homestead.io/share-vip). Who created it does not matter (#306)."""
    annotations = row.get("annotations") or {}
    if NAMES.read(annotations, SHARE_KEY) == "true":
        return True
    if platform_info.get("load_balancer") != "kube-vip":
        return False
    if row.get("vip_lease") == "homestead-vip-" + ip.replace(".", "-").replace(":", "-"):
        return True
    return not platform_info.get("vip_shared_lease") and platform_info.get("vip_service_election") is not True


def _address_owners(raw_rows, node_ips):
    """Addresses that are not Homestead's to hand out, and whose they are.

    The platform's first: a load-balanced address a system namespace holds is
    the cluster's own - Harvester's management VIP, an ingress controller's.
    Putting an app there shares it with the thing hosts join through. Node
    addresses are left out: k3s's ServiceLB publishes every Service on them by
    design, and they are refused for a Service's own VIP separately. Then an
    address a Service holds that neither Homestead made nor shares (_shares).
    """
    try:
        platform_info = PLATFORM.detect() or {}
    except Exception:
        platform_info = {}
    platform, foreign = {}, {}
    for row in raw_rows:
        for ip in row["external_ips"]:
            if ip in node_ips:
                continue
            if row["system"] and row["type"] == "LoadBalancer":
                platform.setdefault(ip, f"{row['namespace']}/{row['name']}")
            elif not row["system"] and not _ours(row) and not _shares(row, ip, platform_info):
                foreign.setdefault(ip, f"{row['namespace']}/{row['name']}")
    try:
        vip = str(((STATE.kget(HARVESTER_VIP) or {}).get("data") or {}).get("ip") or "").strip()
    except Exception:
        vip = ""
    if vip and vip not in node_ips:
        platform.setdefault(vip, "Harvester's management address")
    for ip in platform:
        foreign.pop(ip, None)
    return platform, foreign


def address_problem(ip, state=None):
    """Why an address cannot be given to a Service here, or "" if it can."""
    state = state or inventory()
    if STATE._unread(state):
        return f"{STATE._unread(state)}, so whether {ip} is free is not known; try again in a moment"
    ip = str(ip or "").strip()
    owner = state.get("platform_addresses", {}).get(ip)
    if owner:
        return (f"{ip} is the cluster's own address ({owner}): hosts join and the dashboard "
                "answers on it, and a Service there would share it with them. Give it an "
                "address of its own")
    owner = state.get("foreign_addresses", {}).get(ip)
    if owner:
        return (f"{ip} is held by {owner}, which Homestead did not create and which does not share it: it uses "
                f"its own kube-vip lease, so kube-vip would announce the address twice. Give that Service the lease "
                f"homestead-vip-{ip.replace('.', '-').replace(':', '-')} or the annotation homestead.io/share-vip: \"true\" "
                "to share it, or choose another address")
    return ""


def check_address(ip, state=None):
    problem = address_problem(ip, state)
    if problem:
        raise ValueError(problem)
    return ip


def cluster_address_problem(ip, state):
    """Why a VM's static address clashes with the cluster's own or a
    Service's, or "" - from an inventory ({} when none could be had)."""
    if not state or STATE._unread(state):
        return (f"{STATE._unread(state) or 'could not read the cluster'}, so whether {ip} is free is not known; "
                "try again in a moment")
    if ip in (state.get("node_ips") or []) or ip in (state.get("platform_addresses") or {}):
        return f"{ip} is one of the cluster's own addresses"
    if any(v.get("ip") == ip for v in state.get("vips") or []):
        return f"{ip} is a Service's address"
    return ""


def inventory(leaving=None):
    """leaving: (namespace, name) of a Service about to be replaced, whose
    addresses count as free (set_workload_address)."""
    # What these four say decides a free address and port. One that cannot be
    # read is named in "unread": the Networking page shows the rest, and
    # anything that would choose an address refuses (_unread).
    unread = []

    def strict(path, what):
        try:
            return STATE._items(path, strict=True)
        except Exception as error:
            unread.append(what)
            print(f"Networking: could not read {what}: {str(error)[:160]}", flush=True)
            return []

    services = strict("/api/v1/services", "Services")
    slices = strict("/apis/discovery.k8s.io/v1/endpointslices", "EndpointSlices")
    deployments = strict("/apis/apps/v1/deployments", "Deployments")
    nodes = strict("/api/v1/nodes", "Nodes")
    ingresses = STATE._items("/apis/networking.k8s.io/v1/ingresses")
    pools_raw = STATE._items("/apis/loadbalancer.harvesterhci.io/v1beta1/ippools") + PLATFORM.metallb_pools()
    endpoint_by_service = _endpoint_index(slices)

    deployment_rows, deployment_labels = [], {}
    for deployment in deployments:
        meta, spec = deployment.get("metadata", {}) or {}, deployment.get("spec", {}) or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        labels = ((spec.get("template", {}) or {}).get("metadata", {}) or {}).get("labels", {}) or {}
        deployment_labels[(ns, name)] = labels
        ports = []
        for container in (((spec.get("template", {}) or {}).get("spec", {}) or {}).get("containers", []) or []):
            for port in container.get("ports", []) or []:
                ports.append({"container": container.get("name", ""), "name": port.get("name") or "",
                              "port": port.get("containerPort"), "protocol": port.get("protocol") or "TCP"})
        if ns not in STATE.SYSTEM_NAMESPACES:
            deployment_rows.append({"namespace": ns, "name": name, "ports": ports,
                                    "kind": "Deployment", "selector": labels,
                                    "replicas": int(spec.get("replicas", 0) or 0)})

    # A stable KubeVirt name label follows the guest when its launcher pod moves.
    for vm in STATE._items("/apis/kubevirt.io/v1/virtualmachines"):
        meta = vm.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        template = (vm.get("spec") or {}).get("template") or {}
        spec = template.get("spec") or {}
        pod_names = {n["name"] for n in spec.get("networks", []) if "pod" in n}
        interfaces = ((spec.get("domain") or {}).get("devices") or {}).get("interfaces") or []
        if ns in STATE.SYSTEM_NAMESPACES or not any(i.get("name") in pod_names and "masquerade" in i for i in interfaces):
            continue
        labels = (template.get("metadata") or {}).get("labels") or {}
        if not labels:
            continue
        deployment_labels[(ns, "VirtualMachine/" + name)] = labels
        forwarded = [port for iface in interfaces if iface.get("name") in pod_names
                     for port in iface.get("ports") or []]
        deployment_rows.append({"namespace": ns, "name": name, "kind": "VirtualMachine", "selector": labels,
                                "ports": forwarded,
                                "replicas": 1 if (vm.get("status") or {}).get("ready") else 0})

    node_ips, node_names = set(), {}
    for node in nodes:
        for address in (node.get("status", {}) or {}).get("addresses", []) or []:
            if address.get("type") in ("InternalIP", "ExternalIP") and address.get("address"):
                node_ips.add(address["address"])
                node_names[address["address"]] = (node.get("metadata") or {}).get("name", "")

    raw_rows = []
    for service in services:
        meta, spec, status = service.get("metadata", {}) or {}, service.get("spec", {}) or {}, service.get("status", {}) or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        annotations, labels = meta.get("annotations", {}) or {}, meta.get("labels", {}) or {}
        assigned = [row.get("ip") or row.get("hostname") for row in
                    (status.get("loadBalancer", {}) or {}).get("ingress", []) or []]
        requested = [value.strip() for value in str(annotations.get(STATE.VIP_ANNOTATION)
                                                     or annotations.get("metallb.universe.tf/loadBalancerIPs") or "").split(",")
                     if value.strip()]
        external = [value for value in assigned + requested + (spec.get("externalIPs", []) or []) if value]
        external = list(dict.fromkeys(external))
        endpoint = endpoint_by_service.get((ns, name), {"ready": [], "not_ready": [], "ports": []})
        selector = spec.get("selector") or {}
        targets = []
        if selector:
            targets = [deployment for (dns, deployment), pod_labels in deployment_labels.items()
                       if dns == ns and all(pod_labels.get(key) == value for key, value in selector.items())]
        service_ports = []
        for port in spec.get("ports", []) or []:
            ip = external[0] if external else ""
            access, browser = _access_url(ip, port.get("port"), port.get("name"), port.get("protocol", "TCP"))
            service_ports.append({"name": port.get("name") or "", "port": port.get("port"),
                                  "target_port": port.get("targetPort"),
                                  "node_port": port.get("nodePort"),
                                  "protocol": port.get("protocol") or "TCP",
                                  "access": access, "browser": browser})
        service_type = spec.get("type", "ClusterIP")
        ready_count, not_ready_count = len(endpoint["ready"]), len(endpoint["not_ready"])
        replicas = {(ns, row["name"] if row["kind"] == "Deployment" else "VirtualMachine/" + row["name"]): row["replicas"]
                    for row in deployment_rows if row["namespace"] == ns}
        stopped = bool(targets) and all(replicas.get((ns, t), 1) == 0 for t in targets)
        if selector and ready_count == 0 and stopped:
            # Its app is stopped on purpose: nothing should answer, and
            # nothing needs attention. It answers again when the app starts.
            health, reason = "stopped", "Its app is stopped; it answers again when the app starts"
        elif selector and ready_count == 0:
            health, reason = "unavailable", "No ready endpoints match the Service selector"
        elif service_type == "LoadBalancer" and not assigned:
            health, reason = "pending", ("Waiting for ServiceLB to publish it on the nodes - another Service on "
                                         "the same port holds it back"
                                         if servicelb_present() and not spec.get("loadBalancerClass")
                                         else "Waiting for kube-vip to advertise the requested address")
        elif not_ready_count:
            health, reason = "degraded", f"{not_ready_count} endpoint(s) are not ready"
        else:
            health, reason = "healthy", f"{ready_count} ready endpoint(s)" if selector else "Selectorless Service"
        raw_rows.append({"namespace": ns, "name": name, "type": service_type,
                         "uid": meta.get("uid", ""), "resource_version": meta.get("resourceVersion", ""),
                         "vip_mode": annotations.get("homestead.io/vip-mode", ""),
                         "system": ns in STATE.SYSTEM_NAMESPACES, "managed": NAMES.read(labels, "managed") == "true",
                         "cluster_ip": spec.get("clusterIP") or "", "external_ips": external,
                         "lb_class": spec.get("loadBalancerClass") or "",
                         "exclusive_vip": NAMES.read(annotations, "exclusive-vip") == "true" or
                             (name == NAMES.object_name("nfs") and spec.get("externalTrafficPolicy") == "Local"),
                         "assigned_ips": assigned, "requested_ips": requested,
                         "vip_host": annotations.get("kube-vip.io/vipHost") or "",
                         "vip_lease": annotations.get("kube-vip.io/leaseName") or "",
                         "annotations": annotations,
                         "selector": selector, "targets": targets,
                         # A Service whose selector matches no Deployment still owns
                         # its VIP and port: that is how a deleted workload leaves a
                         # listener behind.
                         "orphaned": bool(selector) and not targets,
                         "ports": service_ports,
                         "endpoints": endpoint, "ready_endpoints": ready_count,
                         "not_ready_endpoints": not_ready_count, "health": health, "reason": reason})

    # A shared VIP may be reused only when protocol+port tuples remain unique.
    listeners = {}
    for row in raw_rows:
        for vip in row["external_ips"]:
            for port in row["ports"]:
                listeners.setdefault((vip, str(port["protocol"]).upper(), int(port["port"])), []).append(
                    {"namespace": row["namespace"], "service": row["name"]})
    conflicts = [{"ip": ip, "protocol": protocol, "port": port, "owners": owners}
                 for (ip, protocol, port), owners in listeners.items() if len(owners) > 1]
    conflict_keys = {(row["ip"], row["protocol"], row["port"]) for row in conflicts}
    for row in raw_rows:
        if any((vip, str(port["protocol"]).upper(), int(port["port"])) in conflict_keys
               for vip in row["external_ips"] for port in row["ports"]):
            row["health"], row["reason"] = "conflict", "Another Service claims the same VIP, protocol and port"

    platform, foreign = _address_owners(raw_rows, node_ips)
    reserved = set(node_ips) | set(platform)
    for row in raw_rows:
        if (row["namespace"], row["name"]) == leaving:
            continue
        reserved.update(row["external_ips"])
        if row["cluster_ip"] and row["cluster_ip"] != "None":
            reserved.add(row["cluster_ip"])
    # Apps already sitting on the cluster's own address - the setting that
    # sends host joining to the wrong place - said out loud.
    clashes = [{"namespace": row["namespace"], "service": row["name"], "ip": ip, "owner": platform[ip]}
               for row in raw_rows if not row["system"]
               for ip in row["external_ips"] if ip in platform]
    pools, candidates = _pool_inventory(pools_raw, reserved)
    # Addresses reserved here come first: they are the ones someone chose.
    try:
        own, vips_unread = REG.registered(), ""
    except Exception as error:
        # Unread is not "no VIPs": nothing is offered as free, and there is
        # no default rather than the shared fallback.
        own, vips_unread = None, str(error)
        unread.append(REG.UNREAD)
        print(f"Networking: {vips_unread}", flush=True)
    default_vip = None if own is None else next((row["ip"] for row in own if row.get("default")), STATE.SHARED_VIP)
    own_free = [row["ip"] for row in own or [] if row["ip"] not in reserved]
    candidates = own_free + [ip for ip in candidates if ip not in set(own_free)]

    vip_rows = []
    for vip in sorted({ip for row in raw_rows for ip in row["external_ips"]},
                      key=lambda value: tuple(int(part) if part.isdigit() else 999 for part in value.split("."))):
        owners = []
        for row in raw_rows:
            if vip not in row["external_ips"]:
                continue
            for port in row["ports"]:
                owners.append({"namespace": row["namespace"], "service": row["name"],
                               "port": port["port"], "protocol": port["protocol"],
                               "access": port["access"], "browser": port["browser"],
                               "health": row["health"]})
        vip_rows.append({"ip": vip, "shared": len({(owner['namespace'], owner['service']) for owner in owners}) > 1,
                         "services": len({(owner['namespace'], owner['service']) for owner in owners}),
                         "listeners": sorted(owners, key=lambda owner: (owner["port"], owner["protocol"]))})

    controller = _controller()

    # Which node answers for each address and whether traffic reaches it:
    # a Service can wait on kube-vip for two different reasons, and only one
    # of them is kube-vip not having started.
    try:
        platform_info = PLATFORM.detect()
    except Exception:
        platform_info = {}
    addresses = VIPS.address_map(services, nodes, STATE._items("/apis/coordination.k8s.io/v1/leases"), platform_info,
                                 endpoints={(r["namespace"], r["name"]): r["ready_endpoints"] for r in raw_rows},
                                 targets={(r["namespace"], r["name"]): r["targets"] for r in raw_rows})
    by_ip = {row["ip"]: row for row in addresses["addresses"]}
    for row in raw_rows:
        if row["health"] != "pending":
            continue
        place = next((by_ip[ip] for ip in row["requested_ips"] if ip in by_ip), None)
        if place and place["state"] in ("unrouted", "unannounced"):
            row["reason"] = place["reason"]
            row["health"] = "unreachable" if place["state"] == "unrouted" else row["health"]
    for row in vip_rows:
        place = by_ip.get(row["ip"]) or {}
        row.update(node=place.get("node", ""), state=place.get("state", ""), reason=place.get("reason", ""),
                   kind=place.get("kind", "vip"))

    return {"services": sorted(raw_rows, key=lambda row: (row["system"], row["namespace"], row["name"])),
            "unread": unread, "vips": vip_rows, "conflicts": conflicts, "pools": pools,
            "platform_addresses": platform, "foreign_addresses": foreign,
            "platform_clashes": clashes,
            "shared_vip": {"ip": default_vip,
                           "problem": address_problem(default_vip, {"platform_addresses": platform})
                           if default_vip else ""},
            "registered_vips": [dict(row, free=row["ip"] not in reserved,
                                     blocked=address_problem(row["ip"], {"platform_addresses": platform,
                                                                         "foreign_addresses": foreign}),
                                     used_by=sorted({f"{l['namespace']}/{l['service']}" for v in vip_rows if v["ip"] == row["ip"]
                                                     for l in v["listeners"]})) for row in own]
                               if own is not None else None,
            "vips_unread": vips_unread,
            "vip_labels": {row["ip"]: row.get("label", "") for row in own or []},
            "available_vips": candidates[:128], "available_vip_count": len(candidates),
            "node_ips": sorted(node_ips), "node_names": node_names, "controller": controller,
            "workloads": sorted(deployment_rows, key=lambda row: (row["namespace"], row["name"])),
            "ingresses": _ingress_inventory(ingresses),
            "addresses": addresses,
            "summary": {"services": len(raw_rows), "app_services": sum(not row["system"] for row in raw_rows),
                        "load_balancers": sum(row["type"] == "LoadBalancer" for row in raw_rows),
                        "vips": len(vip_rows), "listeners": sum(len(row["listeners"]) for row in vip_rows),
                        "unhealthy": sum(row["health"] not in ("healthy", "stopped") for row in raw_rows if not row["system"]),
                        "ready_endpoints": sum(row["ready_endpoints"] for row in raw_rows)}}


def _controller():
    """Whichever load balancer this cluster runs: kube-vip on Harvester, or
    MetalLB, or k3s's built-in ServiceLB on a plain cluster."""
    candidates = (
        ("kube-vip", "/apis/apps/v1/namespaces/harvester-system/daemonsets/kube-vip", "ARP Service controller · explicit VIP allocation"),
        ("kube-vip", "/apis/apps/v1/namespaces/kube-system/daemonsets/kube-vip-ds", "ARP Service controller · explicit VIP allocation"),
        ("kube-vip", "/apis/apps/v1/namespaces/kube-system/daemonsets/kube-vip", "ARP Service controller · VIPs beside the nodes' own addresses"),
        ("MetalLB", "/apis/apps/v1/namespaces/metallb-system/daemonsets/speaker", "MetalLB speakers · addresses from its pools"),
    )
    for name, path, mode in candidates:
        try:
            status = STATE.kget(path).get("status", {}) or {}
        except Exception:
            continue
        desired, ready = int(status.get("desiredNumberScheduled", 0) or 0), int(status.get("numberReady", 0) or 0)
        return {"name": name, "installed": True, "desired": desired, "ready": ready,
                "healthy": desired > 0 and ready == desired, "mode": mode}
    svclb = [d for d in STATE._items("/apis/apps/v1/namespaces/kube-system/daemonsets")
             if (d.get("metadata") or {}).get("name", "").startswith("svclb-")]
    if svclb:
        desired = sum(int((d.get("status") or {}).get("desiredNumberScheduled", 0) or 0) for d in svclb)
        ready = sum(int((d.get("status") or {}).get("numberReady", 0) or 0) for d in svclb)
        return {"name": "ServiceLB", "installed": True, "desired": desired, "ready": ready,
                "healthy": ready == desired, "mode": "k3s ServiceLB · each Service on the nodes' own addresses"}
    return {"name": "none", "installed": False, "desired": 0, "ready": 0, "healthy": False,
            "mode": "no load balancer: LoadBalancer Services stay pending"}


def node_addresses_only():
    """k3s's ServiceLB: every LoadBalancer Service is published on every
    node's own address, and a VIP asked for is ignored. There is no address
    to choose, only a port, which must be free across all of them."""
    return PLATFORM.detect().get("load_balancer") == "servicelb"


def servicelb_present():
    """ServiceLB is there to put a Service on the nodes' own addresses -
    alone, or beside kube-vip, which then gives VIPs to the Services that
    ask for one."""
    return bool(PLATFORM.detect().get("servicelb"))


# Last, so that importing this module first works: homestead_vip_registry
# imports this one.
import homestead_vip_registry as REG  # noqa: E402
