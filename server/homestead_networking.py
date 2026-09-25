"""Service, VIP, port, endpoint and ingress inventory for Homestead.

Harvester's management cluster advertises LoadBalancer Services with kube-vip.
There is no kube-vip cloud-controller allocating addresses on this installation,
so Homestead always resolves automatic requests to a concrete address before it
creates a Service.  Allocation is deliberately reconciled against live cluster
state rather than trusting an IPPool's allocation counter: explicitly requested
kube-vip addresses are not recorded in the Harvester IPPool status.
"""
import ipaddress
import json
import homestead_names as NAMES
import re
import time
import urllib.error


kget = ksend = None
SYSTEM_NAMESPACES = set()
DEFAULT_NAMESPACE = "lab"
SHARED_VIP = ""
DNS_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
VIP_ANNOTATION = "kube-vip.io/loadbalancerIPs"
import homestead_platform as PLATFORM


def bind(_kget, _ksend, system_namespaces, default_namespace, shared_vip):
    global kget, ksend, SYSTEM_NAMESPACES, DEFAULT_NAMESPACE, SHARED_VIP
    kget, ksend = _kget, _ksend
    SYSTEM_NAMESPACES = set(system_namespaces or ())
    DEFAULT_NAMESPACE = default_namespace
    SHARED_VIP = shared_vip


def _items(path):
    for attempt in range(2):
        try:
            return kget(path).get("items", [])
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                return []
            if error.code == 429 and attempt == 0:
                try:
                    delay = float((error.headers or {}).get("Retry-After", "0.5"))
                except (TypeError, ValueError):
                    delay = 0.5
                time.sleep(max(0.0, min(2.0, delay)))
                continue
            raise
        except Exception:
            return []
    return []


def _name(value, label):
    value = str(value or "").strip().lower()
    if not DNS_NAME.fullmatch(value):
        raise ValueError(f"{label} must be a Kubernetes name using lowercase letters, numbers and dashes")
    return value


def _ipv4(value, label="VIP"):
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError as error:
        raise ValueError(f"{label} must be a valid IPv4 address") from error
    if address.version != 4 or address.is_multicast or address.is_unspecified or address.is_loopback:
        raise ValueError(f"{label} must be a usable unicast IPv4 address")
    return str(address)


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
                    "system": meta.get("namespace", "") in SYSTEM_NAMESPACES})
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
            or row["namespace"] == DEFAULT_NAMESPACE)


def _address_owners(raw_rows, node_ips):
    """Addresses that are not Homestead's to hand out, and whose they are.

    The platform's first: a load-balanced address a system namespace holds is
    the cluster's own - Harvester's management VIP, an ingress controller's.
    Putting an app there shares it with the thing hosts join through. Node
    addresses are left out: k3s's ServiceLB publishes every Service on them by
    design, and they are refused for a Service's own VIP separately.
    """
    platform, foreign = {}, {}
    for row in raw_rows:
        for ip in row["external_ips"]:
            if ip in node_ips:
                continue
            if row["system"] and row["type"] == "LoadBalancer":
                platform.setdefault(ip, f"{row['namespace']}/{row['name']}")
            elif not row["system"] and not _ours(row):
                foreign.setdefault(ip, f"{row['namespace']}/{row['name']}")
    try:
        vip = str(((kget(HARVESTER_VIP) or {}).get("data") or {}).get("ip") or "").strip()
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
    ip = str(ip or "").strip()
    owner = state.get("platform_addresses", {}).get(ip)
    if owner:
        return (f"{ip} is the cluster's own address ({owner}): hosts join and the dashboard "
                "answers on it, and a Service there would share it with them. Give it an "
                "address of its own")
    owner = state.get("foreign_addresses", {}).get(ip)
    if owner:
        return f"{ip} belongs to {owner}, which Homestead did not create; choose another address"
    return ""


def check_address(ip, state=None):
    problem = address_problem(ip, state)
    if problem:
        raise ValueError(problem)
    return ip


def inventory():
    services = _items("/api/v1/services")
    slices = _items("/apis/discovery.k8s.io/v1/endpointslices")
    deployments = _items("/apis/apps/v1/deployments")
    nodes = _items("/api/v1/nodes")
    ingresses = _items("/apis/networking.k8s.io/v1/ingresses")
    pools_raw = _items("/apis/loadbalancer.harvesterhci.io/v1beta1/ippools") + PLATFORM.metallb_pools()
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
        if ns not in SYSTEM_NAMESPACES:
            deployment_rows.append({"namespace": ns, "name": name, "ports": ports,
                                    "replicas": int(spec.get("replicas", 0) or 0)})

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
        requested = [value.strip() for value in str(annotations.get(VIP_ANNOTATION)
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
        if selector and ready_count == 0:
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
                         "system": ns in SYSTEM_NAMESPACES, "managed": NAMES.read(labels, "managed") == "true",
                         "cluster_ip": spec.get("clusterIP") or "", "external_ips": external,
                         "lb_class": spec.get("loadBalancerClass") or "",
                         "assigned_ips": assigned, "requested_ips": requested,
                         "vip_host": annotations.get("kube-vip.io/vipHost") or "",
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
    own = registered()
    own_free = [row["ip"] for row in own if row["ip"] not in reserved]
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

    return {"services": sorted(raw_rows, key=lambda row: (row["system"], row["namespace"], row["name"])),
            "vips": vip_rows, "conflicts": conflicts, "pools": pools,
            "platform_addresses": platform, "foreign_addresses": foreign,
            "platform_clashes": clashes,
            "shared_vip": {"ip": SHARED_VIP,
                           "problem": address_problem(SHARED_VIP, {"platform_addresses": platform})
                           if SHARED_VIP else ""},
            "registered_vips": [dict(row, free=row["ip"] not in reserved,
                                     blocked=address_problem(row["ip"], {"platform_addresses": platform,
                                                                         "foreign_addresses": foreign}),
                                     used_by=sorted({f"{l['namespace']}/{l['service']}" for v in vip_rows if v["ip"] == row["ip"]
                                                     for l in v["listeners"]})) for row in own],
            "vip_labels": {row["ip"]: row.get("label", "") for row in own},
            "available_vips": candidates[:128], "available_vip_count": len(candidates),
            "node_ips": sorted(node_ips), "node_names": node_names, "controller": controller,
            "workloads": sorted(deployment_rows, key=lambda row: (row["namespace"], row["name"])),
            "ingresses": _ingress_inventory(ingresses),
            "summary": {"services": len(raw_rows), "app_services": sum(not row["system"] for row in raw_rows),
                        "load_balancers": sum(row["type"] == "LoadBalancer" for row in raw_rows),
                        "vips": len(vip_rows), "listeners": sum(len(row["listeners"]) for row in vip_rows),
                        "unhealthy": sum(row["health"] not in ("healthy",) for row in raw_rows if not row["system"]),
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
            status = kget(path).get("status", {}) or {}
        except Exception:
            continue
        desired, ready = int(status.get("desiredNumberScheduled", 0) or 0), int(status.get("numberReady", 0) or 0)
        return {"name": name, "installed": True, "desired": desired, "ready": ready,
                "healthy": desired > 0 and ready == desired, "mode": mode}
    svclb = [d for d in _items("/apis/apps/v1/namespaces/kube-system/daemonsets")
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
    try:
        return PLATFORM.detect().get("load_balancer") == "servicelb"
    except Exception:
        return False


def servicelb_present():
    """ServiceLB is there to put a Service on the nodes' own addresses -
    alone, or beside kube-vip, which then gives VIPs to the Services that
    ask for one."""
    try:
        return bool(PLATFORM.detect().get("servicelb"))
    except Exception:
        return False


def _node_port_owner(state, namespace, service_name, ports):
    """Another LoadBalancer Service already on one of these ports - system
    ones too, like Traefik on 80 and 443 - when every Service shares the
    nodes' addresses."""
    for row in state["services"]:
        if row.get("type") != "LoadBalancer" or (row["namespace"], row["name"]) == (namespace, service_name):
            continue
        if row.get("lb_class"):
            continue  # kube-vip's, on a VIP of its own: not on the nodes' addresses
        for existing in row["ports"]:
            for port in ports:
                if int(existing["port"]) == int(port["port"]) and str(existing["protocol"]).upper() == port["protocol"]:
                    return {"namespace": row["namespace"], "service": row["name"],
                            "port": existing["port"], "protocol": str(existing["protocol"]).upper()}
    return None


def _node_port_problem(owner):
    return (f"port {owner['port']}/{owner['protocol']} is already answered by {owner['namespace']}/{owner['service']}: "
            "k3s's ServiceLB puts every Service on every node's own address, so each needs ports of its own - "
            "choose another LAN port")


def _ports(cfg):
    rows, seen = [], set()
    for raw in cfg.get("ports") or []:
        try:
            port, target = int(raw.get("port")), int(raw.get("target_port") or raw.get("target") or raw.get("port"))
        except (TypeError, ValueError) as error:
            raise ValueError("every listener needs a numeric LAN port and container target port") from error
        protocol = str(raw.get("protocol") or "TCP").upper()
        if not 1 <= port <= 65535 or not 1 <= target <= 65535:
            raise ValueError("ports must be between 1 and 65535")
        if protocol not in ("TCP", "UDP"):
            raise ValueError("protocol must be TCP or UDP")
        if (protocol, port) in seen:
            raise ValueError(f"duplicate {protocol} listener on port {port}")
        seen.add((protocol, port))
        name = str(raw.get("name") or f"p{port}-{protocol.lower()}").lower()
        name = re.sub(r"[^a-z0-9-]", "-", name).strip("-")[:15] or f"p{port}"
        rows.append({"name": name, "port": port, "targetPort": target, "protocol": protocol})
    if not rows:
        raise ValueError("add at least one service port")
    return rows


def service_plan(cfg, require_workload=True):
    state = inventory()
    namespace = _name(cfg.get("namespace") or DEFAULT_NAMESPACE, "namespace")
    if namespace in SYSTEM_NAMESPACES:
        raise PermissionError("Homestead does not create Services in system namespaces")
    service_name = _name(cfg.get("name") or cfg.get("service_name"), "service name")
    workload_name = _name(cfg.get("workload") or service_name, "workload name")
    workload = next((row for row in state["workloads"]
                     if row["namespace"] == namespace and row["name"] == workload_name), None)
    if require_workload and not workload:
        raise ValueError(f"Deployment {namespace}/{workload_name} does not exist")
    if any(row["namespace"] == namespace and row["name"] == service_name for row in state["services"]):
        raise ValueError(f"Service {namespace}/{service_name} already exists")
    ports = _ports(cfg)
    service_type = str(cfg.get("type") or "LoadBalancer")
    if service_type not in ("LoadBalancer", "ClusterIP"):
        raise ValueError("service type must be LoadBalancer or ClusterIP")
    mode = "cluster" if service_type == "ClusterIP" else str(cfg.get("vip_mode") or "automatic")
    if mode == "auto":
        mode = "automatic"
    if mode not in ("cluster", "shared", "automatic", "manual", "nodes"):
        raise ValueError("VIP mode must be shared, automatic, manual or nodes")
    if mode == "shared" and not SHARED_VIP and servicelb_present():
        # k3s has no shared Homestead VIP: shared means the nodes' addresses.
        mode = "nodes"
    if mode == "nodes" and not servicelb_present():
        raise ValueError("this cluster has no ServiceLB to put a Service on the nodes' own addresses; give it a VIP")
    warnings = []
    vip = ""
    if mode == "nodes" or (mode != "cluster" and node_addresses_only()):
        # Whatever was asked for, ServiceLB publishes it on the nodes' own
        # addresses: the shared VIP (none is set on k3s) is not needed.
        mode = "nodes"
        owner = _node_port_owner(state, namespace, service_name, ports)
        if owner:
            raise ValueError(_node_port_problem(owner))
    elif mode == "shared":
        if not SHARED_VIP:
            raise ValueError("the shared Homestead VIP is not configured")
        vip = _ipv4(SHARED_VIP, "shared VIP")
        if vip in state["platform_addresses"]:
            raise ValueError(f"Homestead's shared address (LB_IP, {vip}) is the cluster's own address "
                             f"({state['platform_addresses'][vip]}). Set LB_IP to an address of "
                             "Homestead's own, or give this app an address of its own")
    elif mode == "automatic":
        if not state["available_vips"]:
            raise ValueError("there is no free address to give it: add some under Networking › Virtual IPs "
                             "(＋ Add VIPs), or choose a specific address")
        vip = state["available_vips"][0]
    elif mode == "manual":
        vip = _ipv4(cfg.get("vip"), "specific VIP")
        if vip in state["node_ips"]:
            raise ValueError(f"{vip} is a cluster node address and cannot be used as a Service VIP")
        check_address(vip, state)
        in_pool = vip in state["available_vips"] or any(vip == row["ip"] for row in state["vips"])
        if not in_pool:
            warnings.append("This address is outside the visible Harvester IP pools; verify DHCP and static reservations before creating it.")

    owners = []
    if vip:
        for row in state["services"]:
            if vip not in row["external_ips"]:
                continue
            for existing in row["ports"]:
                if any(int(port["port"]) == int(existing["port"]) and
                       port["protocol"] == str(existing["protocol"]).upper() for port in ports):
                    owners.append({"namespace": row["namespace"], "service": row["name"],
                                   "port": existing["port"], "protocol": existing["protocol"]})
    if owners:
        owner = owners[0]
        raise ValueError(f"{vip}:{owner['port']}/{owner['protocol']} is already used by {owner['namespace']}/{owner['service']}")
    if vip and any(vip in row["external_ips"] for row in state["services"]):
        warnings.append("This VIP is shared with existing Services; only the reviewed protocol/port listeners are new.")

    declared = {(str(port.get("protocol") or "TCP").upper(), int(port.get("port") or 0))
                for port in (workload or {}).get("ports", [])}
    undeclared = [port for port in ports if (port["protocol"], int(port["targetPort"])) not in declared]
    if workload and undeclared:
        warnings.append("One or more target ports are not declared by the Deployment. Kubernetes permits this, but verify the application is listening there.")
    return {"ready": True, "namespace": namespace, "name": service_name,
            "workload": workload_name, "type": service_type, "vip_mode": mode, "vip": vip,
            "ports": ports, "warnings": warnings,
            "path": {"vip": vip or ("each node's own address" if mode == "nodes" else "cluster only"),
                     "service": f"{namespace}/{service_name}",
                     "workload": f"Deployment/{workload_name}",
                     "endpoints": (workload or {}).get("replicas", 0)},
            "available_vips": state["available_vips"][:16]}


def create_service(cfg):
    plan = service_plan(cfg)
    deployments = _items("/apis/apps/v1/deployments")
    deployment = next(item for item in deployments
                      if item.get("metadata", {}).get("namespace") == plan["namespace"] and
                      item.get("metadata", {}).get("name") == plan["workload"])
    selector = ((deployment.get("spec", {}) or {}).get("selector", {}) or {}).get("matchLabels", {}) or {}
    if not selector:
        raise ValueError("the selected Deployment has no matchLabels selector")
    annotations = {"homestead.io/vip-mode": plan["vip_mode"],
                   "homestead.io/workload": plan["workload"]}
    annotations.update(PLATFORM.vip_annotations(plan["vip"]))
    body = {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": plan["name"], "namespace": plan["namespace"],
                         "labels": {"homestead.io/managed": "true"}, "annotations": annotations},
            "spec": {"type": plan["type"], "selector": selector, "ports": plan["ports"],
                     **(PLATFORM.vip_spec(plan["vip"]) if plan["type"] == "LoadBalancer" else {})}}
    ksend("POST", f"/api/v1/namespaces/{plan['namespace']}/services", body)
    return {"ok": True, **plan,
            "message": (f"Service {plan['namespace']}/{plan['name']} created" +
                        (f" on {plan['vip']}" if plan["vip"] else ""))}


def prepare_deploy(cfg):
    """Resolve and validate a deployment's Service before any workload mutation."""
    exposed = [port for port in cfg.get("ports") or [] if port.get("expose")]
    if not exposed or cfg.get("network_mode") in ("host", "lan"):
        return cfg
    internal = cfg.get("network_mode") == "internal"
    planned = service_plan({"namespace": cfg.get("namespace") or DEFAULT_NAMESPACE,
                            "name": cfg.get("name"), "workload": cfg.get("name"),
                            "type": "ClusterIP" if internal else "LoadBalancer",
                            "vip_mode": "cluster" if internal else (cfg.get("vip_mode") or "shared"),
                            "vip": cfg.get("lb_ip"),
                            "ports": [{"name": port.get("name"),
                                       "port": port.get("host") or port.get("container"),
                                       "target_port": port.get("container"),
                                       "protocol": port.get("protocol") or "TCP"}
                                      for port in exposed]}, require_workload=False)
    updated = dict(cfg)
    updated["lb_ip"] = planned["vip"]
    updated["vip_mode"] = planned["vip_mode"]
    updated["network_warnings"] = planned["warnings"]
    return updated


def workload_services(namespace, workload):
    """Services whose selector points at this Deployment's pods."""
    deployment = kget(f"/apis/apps/v1/namespaces/{namespace}/deployments/{workload}")
    selector = ((deployment.get("spec", {}) or {}).get("selector", {}) or {}).get("matchLabels", {}) or {}
    if not selector:
        return []
    rows = []
    for service in _items(f"/api/v1/namespaces/{namespace}/services"):
        chosen = (service.get("spec", {}) or {}).get("selector") or {}
        if chosen and all(selector.get(key) == value for key, value in chosen.items()):
            rows.append(service)
    # The Service named after its workload is the one Homestead created.
    rows.sort(key=lambda row: row["metadata"]["name"] != workload)
    return rows


def _listener_owner(service, ports):
    """Another Service already answering on one of these VIP listeners."""
    state = inventory()
    mine = (service["metadata"]["namespace"], service["metadata"]["name"])
    spec = service.get("spec") or {}
    if servicelb_present() and spec.get("type") == "LoadBalancer" and not spec.get("loadBalancerClass"):
        return _node_port_owner(state, mine[0], mine[1], ports)
    addresses = set()
    for row in state["services"]:
        if (row["namespace"], row["name"]) == mine:
            addresses.update(row["external_ips"])
    if not addresses:
        return None
    for row in state["services"]:
        if (row["namespace"], row["name"]) == mine or not addresses & set(row["external_ips"]):
            continue
        for existing in row["ports"]:
            for port in ports:
                if (int(existing["port"]) == int(port["port"]) and
                        str(existing["protocol"]).upper() == port["protocol"]):
                    return {"namespace": row["namespace"], "service": row["name"],
                            "port": existing["port"], "protocol": existing["protocol"]}
    return None


def sync_workload_ports(namespace, workload, ports, network_mode=None, vip_mode="shared"):
    """Point a workload's Service at the LAN ports its containers now expose.

    Editing a container only ever changed containerPort, which Kubernetes uses
    for nothing on its own: the LAN listener lives on the Service. Exposing the
    first port creates that Service, unexposing the last one removes it, and
    everything between is an in-place port update that keeps the VIP.
    """
    namespace = _name(namespace, "namespace")
    workload = _name(workload, "workload name")
    exposed = [port for port in ports or [] if port.get("expose")]
    if network_mode in ("host", "lan"):
        # On the host's network or its own LAN address, it answers directly.
        exposed = []
    desired = _ports({"ports": [{"name": port.get("name"),
                                 "port": port.get("host") or port.get("container"),
                                 "target_port": port.get("container"),
                                 "protocol": port.get("protocol") or "TCP"}
                                for port in exposed]}) if exposed else []
    services = workload_services(namespace, workload)
    if not services:
        if not desired:
            return ""
        internal = network_mode == "internal"
        plan = create_service({"namespace": namespace, "name": workload, "workload": workload,
                               "type": "ClusterIP" if internal else "LoadBalancer",
                               "vip_mode": "cluster" if internal else vip_mode,
                               "ports": [{"name": port["name"], "port": port["port"],
                                          "target_port": port["targetPort"],
                                          "protocol": port["protocol"]} for port in desired]})
        return plan["message"]

    service = services[0]
    name = service["metadata"]["name"]
    if not desired:
        ksend("DELETE", f"/api/v1/namespaces/{namespace}/services/{name}")
        return f"Service {name} removed; its LAN address was released"

    current = [{"name": port.get("name", ""), "port": int(port.get("port")),
                "targetPort": int(port.get("targetPort", port.get("port"))),
                "protocol": str(port.get("protocol") or "TCP").upper()}
               for port in (service.get("spec", {}) or {}).get("ports", []) or []]
    if current == desired:
        return ""
    owner = _listener_owner(service, desired)
    if owner:
        if servicelb_present() and not (service.get("spec") or {}).get("loadBalancerClass"):
            raise ValueError(_node_port_problem(owner))
        raise ValueError(f"port {owner['port']}/{owner['protocol']} is already answered by "
                         f"{owner['namespace']}/{owner['service']} on this address")
    service["spec"]["ports"] = desired
    ksend("PUT", f"/api/v1/namespaces/{namespace}/services/{name}", service)
    listeners = ", ".join(f"{port['port']}→{port['targetPort']}/{port['protocol']}" for port in desired)
    return f"Service {name} now listens on {listeners}"


def delete_service(namespace, name, force=False):
    """Release a Service and the LAN listeners it owns.

    A Service outlives the workload it was created for, so this is how an
    orphaned VIP:port is reclaimed. Deleting one that still has a workload
    behind it takes that workload off the LAN, so it needs force.
    """
    namespace = _name(namespace, "namespace")
    name = _name(name, "service name")
    if namespace in SYSTEM_NAMESPACES:
        raise PermissionError("Homestead does not delete Services in system namespaces")
    row = next((item for item in inventory()["services"]
                if item["namespace"] == namespace and item["name"] == name), None)
    if not row:
        raise ValueError(f"Service {namespace}/{name} does not exist")
    if row["targets"] and not force:
        raise ValueError(f"{namespace}/{name} still serves {', '.join(row['targets'])}; "
                         "removing it takes that workload off the LAN")
    ksend("DELETE", f"/api/v1/namespaces/{namespace}/services/{name}")
    freed = [f"{ip}:{port['port']}/{port['protocol']}"
             for ip in row["external_ips"] for port in row["ports"]]
    return {"ok": True, "name": name, "namespace": namespace, "freed": freed,
            "message": (f"Service {namespace}/{name} deleted" +
                        (f", releasing {', '.join(freed)}" if freed else ""))}


def workload_service_names(namespace, workload):
    """Names of the Services that select a workload's pods, for deletion."""
    try:
        return [row["metadata"]["name"] for row in workload_services(namespace, workload)]
    except Exception:
        return []

# ---- the addresses Homestead may hand out ----------------------------------------
# kube-vip announces whatever address a Service asks for; nothing on a plain
# Harvester install hands addresses out. A Harvester IP pool is one source of
# free ones. This is the other: addresses reserved here, by hand, for
# Homestead to give to Services - named, so the picker says what each is for.
VIP_MAP = "homestead-vips"
MAX_ADD = 64


def _vip_map_path():
    return f"/api/v1/namespaces/{DEFAULT_NAMESPACE}/configmaps/{VIP_MAP}"


def registered():
    try:
        cm = kget(_vip_map_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise
    except Exception:
        return []
    try:
        rows = json.loads((cm.get("data") or {}).get("vips.json") or "[]")
    except ValueError:
        rows = []
    return [row for row in rows if isinstance(row, dict) and row.get("ip")]


def _save_registered(rows):
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": VIP_MAP, "namespace": DEFAULT_NAMESPACE, "labels": {"homestead.io/managed": "true"}},
            "data": {"vips.json": json.dumps(sorted(rows, key=lambda r: int(ipaddress.ip_address(r["ip"]))), indent=1)}}
    try:
        kget(_vip_map_path())
        ksend("PUT", _vip_map_path(), body)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{DEFAULT_NAMESPACE}/configmaps", body)


def add_vips(cfg, ipam_records=None):
    """Reserve addresses for Services: one, or a range, with a label."""
    start = _ipv4(cfg.get("ip") or cfg.get("start"), "address")
    end = _ipv4(cfg.get("end") or start, "last address")
    first, last = int(ipaddress.ip_address(start)), int(ipaddress.ip_address(end))
    if last < first:
        raise ValueError("the last address comes before the first")
    if last - first + 1 > MAX_ADD:
        raise ValueError(f"at most {MAX_ADD} addresses at once")
    label = " ".join(str(cfg.get("label") or "").split())[:60]
    state = inventory()
    nodes = set(state["node_ips"])
    rows = registered()
    have = {row["ip"] for row in rows}
    records = ipam_records or {}
    added, skipped = [], []
    for number in range(first, last + 1):
        ip = str(ipaddress.ip_address(number))
        if ip in have:
            skipped.append(f"{ip} is already on the list")
        elif ip in nodes:
            skipped.append(f"{ip} is a node's own address")
        elif ip in state["platform_addresses"]:
            skipped.append(f"{ip} is the cluster's own address ({state['platform_addresses'][ip]})")
        elif ip in state["foreign_addresses"]:
            skipped.append(f"{ip} belongs to {state['foreign_addresses'][ip]}")
        elif (records.get(ip) or {}).get("category") not in (None, "", "vip"):
            record = records[ip]
            skipped.append(f"{ip} is {record.get('name') or 'a device'} in IP addresses")
        else:
            rows.append({"ip": ip, "label": label, "added": time.strftime("%Y-%m-%d %H:%M")})
            added.append(ip)
    if added:
        _save_registered(rows)
    return {"ok": bool(added), "added": added, "skipped": skipped,
            "detail": (f"{len(added)} address{'es' if len(added) != 1 else ''} added" if added else "nothing added")
                      + (f"; {len(skipped)} skipped: " + "; ".join(skipped[:4]) if skipped else "")}


def remove_vip(ip):
    ip = _ipv4(ip, "address")
    state = inventory()
    users = next((row for row in state["vips"] if row["ip"] == ip), None)
    if users:
        names = sorted({f"{l['namespace']}/{l['service']}" for l in users["listeners"]})
        raise ValueError(f"{ip} is in use by {', '.join(names)}; remove or move those first")
    rows = [row for row in registered() if row["ip"] != ip]
    _save_registered(rows)
    return {"ok": True, "detail": f"{ip} is no longer reserved for Homestead"}


def set_vip_label(ip, label):
    ip = _ipv4(ip, "address")
    rows = registered()
    for row in rows:
        if row["ip"] == ip:
            row["label"] = " ".join(str(label or "").split())[:60]
    _save_registered(rows)
    return {"ok": True}


# ---- VM networks ---------------------------------------------------------------
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


def _multus():
    try:
        kget(NAD_API)
        return True
    except Exception:
        return False


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
        items = kget(CLUSTER_NETWORKS).get("items", [])
    except Exception:
        return {"harvester": False, "cluster_networks": [], "multus": _multus(),
                "interfaces": host_interfaces(probes), "multus_help": MULTUS_HELP}
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
        return config, iface, True
    master = f"{iface}.{vlan}" if vlan else iface
    if vlan and known and master not in known:
        # macvlan rides a host interface; a VLAN needs the host's own for it.
        raise ValueError(f"the hosts have no {master} interface for VLAN {vlan} on {iface}: add it on each host "
                         f"(ip link add link {iface} name {master} type vlan id {vlan}), or use a host bridge")
    return {"cniVersion": "0.3.1", "name": name, "type": "macvlan", "master": master, "mode": "bridge", "ipam": {}}, master, False


def create_vm_network(cfg):
    options = vm_network_options(cfg.get("_probes"))
    name = _name(cfg.get("name"), "network name")
    namespace = _name(cfg.get("namespace") or "default", "namespace")
    labels = {}
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
        config, carrier, vms = _host_network_config(cfg, options, name)
        vlan = _vlan(cfg)
        where = f"{'VLAN ' + vlan + ' on ' if vlan and config['type'] == 'bridge' else ''}{carrier}"
        joins = "VMs and containers" if vms else "containers (a VM needs a host bridge)"
    path = f"{NAD_API}/namespaces/{namespace}/network-attachment-definitions"
    try:
        kget(f"{path}/{name}")
        raise ValueError(f"a LAN network {namespace}/{name} already exists")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    ksend("POST", path, {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
                         "metadata": {"name": name, "namespace": namespace, "labels": labels},
                         "spec": {"config": json.dumps(config)}})
    return {"ok": True, "name": f"{namespace}/{name}",
            "detail": f"LAN network {namespace}/{name} made, on {where}; {joins} can join it now"}
