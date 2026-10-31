"""Planning, creating, changing and deleting the Services that publish an app:
its ports, its address mode and its VIP (#623, from homestead_networking).
"""
import re
import time
import urllib.error
import homestead_names as NAMES
import homestead_platform as PLATFORM
import homestead_network_state as STATE
import homestead_network_inventory as INV


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
            port = int(raw.get("port"))
            target = raw.get("target_port") or raw.get("target") or raw.get("port")
            if not isinstance(target, str) or target.isdigit():
                target = int(target)
        except (TypeError, ValueError) as error:
            raise ValueError("every listener needs a numeric LAN port and container target port") from error
        protocol = str(raw.get("protocol") or "TCP").upper()
        if not 1 <= port <= 65535 or (isinstance(target, int) and not 1 <= target <= 65535):
            raise ValueError("ports must be between 1 and 65535")
        if isinstance(target, str) and (not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,13}[a-z0-9])?", target) or not re.search(r"[a-z]", target)):
            raise ValueError("target port must be a number or a valid named container port")
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
    namespace = STATE._name(cfg.get("namespace") or STATE.DEFAULT_NAMESPACE, "namespace")
    if namespace in STATE.SYSTEM_NAMESPACES:
        raise PermissionError("Homestead does not create Services in system namespaces")
    service_name = STATE._name(cfg.get("name") or cfg.get("service_name"), "service name")
    state = STATE._complete(INV.inventory(leaving=(namespace, service_name) if cfg.get("replaces") else None),
                      "nothing was changed")
    shared = state["shared_vip"]["ip"]
    workload_name = STATE._name(cfg.get("workload") or service_name, "workload name")
    kind = cfg.get("workload_kind") or "Deployment"
    if kind not in ("Deployment", "VirtualMachine"):
        raise ValueError("Workload must be a Deployment or VirtualMachine")
    workload = next((row for row in state["workloads"]
                     if row["namespace"] == namespace and row["name"] == workload_name and row.get("kind", "Deployment") == kind), None)
    if require_workload and not workload:
        raise ValueError(f"{kind} {namespace}/{workload_name} does not exist or has no supported pod network")
    if kind == "VirtualMachine" and workload:
        selector = workload.get("selector") or {}
        if not selector or any(row is not workload and row["namespace"] == namespace and
                               all(row.get("selector", {}).get(k) == v for k, v in selector.items())
                               for row in state["workloads"]):
            raise ValueError("VM template labels do not uniquely identify this workload; give it a unique label before exposing it")
    existing = next((row for row in state["services"] if row["namespace"] == namespace and row["name"] == service_name), None)
    if cfg.get("update"):
        target = "VirtualMachine/" + workload_name if kind == "VirtualMachine" else workload_name
        if not existing or not existing.get("uid") or existing["uid"] != cfg.get("uid") or existing.get("resource_version") != cfg.get("resource_version"):
            raise ValueError("Service changed or disappeared; reopen VIP settings and review again")
        if existing.get("targets") != [target] or existing.get("exclusive_vip") or service_name in (NAMES.object_name("smb"), NAMES.object_name("nfs")):
            raise ValueError("This Service is shared across workloads or managed by an add-on; change it in its owning settings")
        if existing["type"] != cfg.get("type", "LoadBalancer"):
            raise ValueError("Keep the existing Service type; create a separate Service to change reachability")
        state["services"] = [row for row in state["services"] if row is not existing]
    elif existing and cfg.get("replaces") and existing.get("uid") == cfg["replaces"]:
        # Checked before set_workload_address deletes it: plan as if it had gone.
        state["services"] = [row for row in state["services"] if row is not existing]
    elif existing:
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
    if mode == "shared" and (not shared or shared in state["node_ips"]) and INV.servicelb_present():
        # k3s has no shared Homestead VIP: shared means the nodes' addresses.
        mode = "nodes"
    if mode == "nodes" and not INV.servicelb_present():
        raise ValueError("this cluster has no ServiceLB to put a Service on the nodes' own addresses; give it a VIP")
    warnings = ["Updating this Service can interrupt existing connections; its ClusterIP and unrelated settings are preserved."] if cfg.get("update") else []
    if kind == "VirtualMachine":
        warnings.append("Guest must listen on the target ports and allow them through its firewall. A VIP does not provide VM or storage failover.")
        if workload and workload.get("ports") and any(
                not any(p.get("port") == requested["targetPort"] and (p.get("protocol") or "TCP") == requested["protocol"]
                        for p in workload["ports"]) for requested in ports):
            raise ValueError("The VM interface does not forward these ports; edit its interface ports first")
    vip = ""
    if mode == "nodes" or (mode != "cluster" and INV.node_addresses_only()):
        # Whatever was asked for, ServiceLB publishes it on the nodes' own
        # addresses: the shared VIP (none is set on k3s) is not needed.
        mode = "nodes"
        owner = _node_port_owner(state, namespace, service_name, ports)
        if owner:
            raise ValueError(_node_port_problem(owner))
    elif mode == "shared":
        if not shared:
            raise ValueError("the shared Homestead VIP is not configured")
        vip = STATE._ipv4(shared, "shared VIP")
        if vip in state["node_ips"]:
            raise ValueError("The default workload VIP is a node address; choose a separate VIP in Networking")
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
        vip = STATE._ipv4(cfg.get("vip"), "specific VIP")
        if vip in state["node_ips"]:
            raise ValueError(f"{vip} is a cluster node address and cannot be used as a Service VIP")
        # An address this Service already carries stays its own: an edit
        # elsewhere in the workload never fails on the address it has (#306).
        if not (existing and vip in (existing.get("external_ips") or [])):
            INV.check_address(vip, state)
        in_pool = vip in state["available_vips"] or any(vip == row["ip"] for row in state["vips"])
        if not in_pool:
            warnings.append("This address is outside the visible Harvester IP pools; verify DHCP and static reservations before creating it.")

    if "reviewed_vip" in cfg and cfg["reviewed_vip"] != vip:
        raise ValueError("VIP allocation changed since review; review the address and ports again")
    if cfg.get("update") and (existing.get("lb_class") or "") != (PLATFORM.vip_spec(vip).get("loadBalancerClass") or ""):
        raise ValueError("Changing this Service's load-balancer class requires replacement. Create an additional VIP Service first; the original listener is left untouched.")
    owners = []
    if vip:
        for row in state["services"]:
            if vip not in row["external_ips"]:
                continue
            p = PLATFORM.detect()
            if p.get("load_balancer") == "kube-vip" and p.get("vip_service_election") is True:
                expected = PLATFORM.vip_annotations(vip).get("kube-vip.io/leaseName")
                if not expected or row.get("vip_lease") != expected or row["namespace"] != namespace:
                    raise ValueError("Sharing this VIP requires a common kube-vip lease in the same namespace; use a dedicated VIP or migrate existing Services to a common lease first")
            if cfg.get("exclusive_vip") or row.get("exclusive_vip"):
                raise ValueError(f"{vip} is used by {row['namespace']}/{row['name']}; NFS needs its own VIP "
                                 "while SMB and NFS run on independently placed pods with Local traffic routing")
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
    declared.update((str(port.get("protocol") or "TCP").upper(), port["name"])
                    for port in (workload or {}).get("ports", []) if port.get("name"))
    undeclared = [port for port in ports if (port["protocol"], port["targetPort"]) not in declared]
    if workload and undeclared and kind == "Deployment":
        warnings.append("One or more target ports are not declared by the Deployment. Kubernetes permits this, but verify the application is listening there.")
    return {"ready": True, "namespace": namespace, "name": service_name,
            "workload": workload_name, "workload_kind": kind, "type": service_type, "vip_mode": mode, "vip": vip,
            "selector": (workload or {}).get("selector", {}),
            "ports": ports, "warnings": warnings,
            "path": {"vip": vip or ("each node's own address" if mode == "nodes" else "cluster only"),
                     "service": f"{namespace}/{service_name}",
                     "workload": f"{kind}/{workload_name}",
                     "endpoints": (workload or {}).get("replicas", 0)},
            "available_vips": state["available_vips"][:16]}


def create_service(cfg):
    plan = service_plan(cfg)
    if cfg.get("update"):
        path = f"/api/v1/namespaces/{plan['namespace']}/services/{plan['name']}"
        service = STATE.kget(path)
        meta = service.get("metadata") or {}
        if meta.get("uid") != cfg.get("uid") or meta.get("resourceVersion") != cfg.get("resource_version"):
            raise ValueError("Service changed after review; reopen VIP settings")
        # Preserve selector, ClusterIP, node ports and all unrelated metadata. Never delete/recreate.
        annotations = meta.setdefault("annotations", {})
        for key in ("kube-vip.io/loadbalancerIPs", "kube-vip.io/leaseName", "metallb.universe.tf/loadBalancerIPs", "metallb.universe.tf/allow-shared-ip"):
            annotations.pop(key, None)
        annotations.update(PLATFORM.vip_annotations(plan["vip"]))
        annotations["homestead.io/vip-mode"] = plan["vip_mode"]
        spec = service["spec"]
        old_ports = {(p["port"], p.get("protocol", "TCP")): p for p in spec.get("ports", [])}
        old_names = {(p.get("name"), p.get("protocol", "TCP")): p for p in spec.get("ports", []) if p.get("name")}
        updated_ports = []
        for requested, p in zip(cfg["ports"], plan["ports"]):
            old = old_ports.get((p["port"], p["protocol"])) or old_names.get((requested.get("name"), p["protocol"])) or {}
            # Ingress backends may refer to a Service port by name. Preserve it,
            # appProtocol and nodePort even when only the VIP/listener changes.
            updated_ports.append({**old, **p, **({"name": old["name"]} if old.get("name") else {})})
        spec["ports"] = updated_ports
        if "loadBalancerIP" in spec:
            if plan["vip"]:
                spec["loadBalancerIP"] = plan["vip"]
            else:
                spec.pop("loadBalancerIP", None)
        if spec.get("externalIPs"):
            raise ValueError("This Service also has externalIPs; use its YAML editor to review those addresses explicitly")
        STATE.ksend("PUT", path, service)
        return {"ok": True, **plan, "message": f"Service {plan['namespace']}/{plan['name']} updated; existing connections may reconnect"}
    if plan["workload_kind"] == "VirtualMachine":
        selector = plan["selector"]
    else:
        deployments = STATE._read("/apis/apps/v1/deployments", "Deployments")
        deployment = next(item for item in deployments
                          if item.get("metadata", {}).get("namespace") == plan["namespace"] and
                          item.get("metadata", {}).get("name") == plan["workload"])
        selector = ((deployment.get("spec", {}) or {}).get("selector", {}) or {}).get("matchLabels", {}) or {}
    if not selector:
        raise ValueError("the selected Deployment has no matchLabels selector")
    annotations = {"homestead.io/vip-mode": plan["vip_mode"],
                   "homestead.io/workload-kind": plan["workload_kind"],
                   "homestead.io/workload": plan["workload"]}
    annotations.update(PLATFORM.vip_annotations(plan["vip"]))
    body = {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": plan["name"], "namespace": plan["namespace"],
                         "labels": {"homestead.io/managed": "true"}, "annotations": annotations},
            "spec": {"type": plan["type"], "selector": selector, "ports": plan["ports"],
                     **(PLATFORM.vip_spec(plan["vip"]) if plan["type"] == "LoadBalancer" else {})}}
    STATE.ksend("POST", f"/api/v1/namespaces/{plan['namespace']}/services", body)
    return {"ok": True, **plan,
            "message": (f"Service {plan['namespace']}/{plan['name']} created" +
                        (f" on {plan['vip']}" if plan["vip"] else ""))}


def prepare_deploy(cfg):
    """Resolve and validate a deployment's Service before any workload mutation."""
    exposed = [port for port in cfg.get("ports") or [] if port.get("expose")]
    if not exposed or cfg.get("network_mode") in ("host", "lan"):
        return cfg
    internal = cfg.get("network_mode") == "internal"
    planned = service_plan({"namespace": cfg.get("namespace") or STATE.DEFAULT_NAMESPACE,
                            "name": cfg.get("name"), "workload": cfg.get("name"),
                            "type": "ClusterIP" if internal else "LoadBalancer",
                            "vip_mode": "cluster" if internal else (cfg.get("vip_mode") or "shared"),
                            "vip": cfg.get("lb_ip"),
                            "exclusive_vip": bool(cfg.get("exclusive_vip")),
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
    deployment = STATE.kget(f"/apis/apps/v1/namespaces/{namespace}/deployments/{workload}")
    selector = ((deployment.get("spec", {}) or {}).get("selector", {}) or {}).get("matchLabels", {}) or {}
    if not selector:
        return []
    rows = []
    for service in STATE._read(f"/api/v1/namespaces/{namespace}/services", "Services"):
        chosen = (service.get("spec", {}) or {}).get("selector") or {}
        if chosen and all(selector.get(key) == value for key, value in chosen.items()):
            rows.append(service)
    # The Service named after its workload is the one Homestead created.
    rows.sort(key=lambda row: row["metadata"]["name"] != workload)
    return rows


def _listener_owner(service, ports):
    """Another Service already answering on one of these VIP listeners."""
    state = STATE._complete(INV.inventory(), "the ports were not changed")
    mine = (service["metadata"]["namespace"], service["metadata"]["name"])
    spec = service.get("spec") or {}
    if INV.servicelb_present() and spec.get("type") == "LoadBalancer" and not spec.get("loadBalancerClass"):
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


def sync_workload_ports(namespace, workload, ports, network_mode=None, vip_mode="shared", vip=""):
    """Point a workload's Service at the LAN ports its containers now expose.

    Editing a container only ever changed containerPort, which Kubernetes uses
    for nothing on its own: the LAN listener lives on the Service. Exposing the
    first port creates that Service, unexposing the last one removes it, and
    everything between is an in-place port update that keeps the VIP.
    """
    namespace = STATE._name(namespace, "namespace")
    workload = STATE._name(workload, "workload name")
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
                               **({"vip": vip} if vip and not internal else {}),
                               "ports": [{"name": port["name"], "port": port["port"],
                                          "target_port": port["targetPort"],
                                          "protocol": port["protocol"]} for port in desired]})
        return plan["message"]

    service = services[0]
    name = service["metadata"]["name"]
    if not desired:
        STATE.ksend("DELETE", f"/api/v1/namespaces/{namespace}/services/{name}")
        return f"Service {name} removed; its LAN address was released"

    current = [{"name": port.get("name", ""), "port": int(port.get("port")),
                "targetPort": int(port.get("targetPort", port.get("port"))),
                "protocol": str(port.get("protocol") or "TCP").upper()}
               for port in (service.get("spec", {}) or {}).get("ports", []) or []]
    if current == desired:
        return ""
    owner = _listener_owner(service, desired)
    if owner:
        if INV.servicelb_present() and not (service.get("spec") or {}).get("loadBalancerClass"):
            raise ValueError(_node_port_problem(owner))
        raise ValueError(f"port {owner['port']}/{owner['protocol']} is already answered by "
                         f"{owner['namespace']}/{owner['service']} on this address")
    service["spec"]["ports"] = desired
    STATE.ksend("PUT", f"/api/v1/namespaces/{namespace}/services/{name}", service)
    listeners = ", ".join(f"{port['port']}→{port['targetPort']}/{port['protocol']}" for port in desired)
    return f"Service {name} now listens on {listeners}"


def _own_service(namespace, workload):
    """The Service Homestead made for this workload alone, or None."""
    services = workload_services(namespace, workload)
    service = services[0] if services else None
    if not service or service["metadata"]["name"] != workload:
        return None
    return service


def set_workload_address(namespace, workload, network_mode, vip_mode="shared", vip=""):
    """Put a workload's own Service where its Address step says: on the LAN
    on the chosen VIP, or cluster-only. Edit used to leave this to a separate
    dialog; Deploy always chose it with the ports. Only what differs from now
    changes - a workload keeping its own address is not given a new one."""
    namespace, workload = STATE._name(namespace, "namespace"), STATE._name(workload, "workload name")
    if network_mode not in ("loadbalancer", "internal"):
        return ""
    service = _own_service(namespace, workload)
    if not service:
        return ""
    mode = {"auto": "automatic"}.get(vip_mode or "shared", vip_mode or "shared")
    meta, spec = service["metadata"], service.get("spec") or {}
    row = next((r for r in INV.inventory()["services"] if r["namespace"] == namespace and r["name"] == meta["name"]), {})
    ports = [{"name": p.get("name", ""), "port": p.get("port"), "target_port": p.get("targetPort", p.get("port")),
              "protocol": p.get("protocol") or "TCP"} for p in spec.get("ports") or []]
    want = "ClusterIP" if network_mode == "internal" else "LoadBalancer"
    cfg = {"namespace": namespace, "name": meta["name"], "workload": workload, "workload_kind": "Deployment",
           "type": want, "vip_mode": "cluster" if want == "ClusterIP" else mode,
           **({"vip": vip} if mode == "manual" and want == "LoadBalancer" else {}), "ports": ports}
    if spec.get("type") != want:
        # Reachability changes: the old Service goes and one of the new kind
        # is made with the same ports, as Deploy would have made it. The new
        # one is checked first, so a refusal leaves the old one answering. An
        # automatic address taken while the old one goes is swapped for the
        # next free one, as before: Edit showed none to review, and refusing
        # after the DELETE would leave the app with no Service at all.
        service_plan({**cfg, "replaces": meta.get("uid")})
        path = f"/api/v1/namespaces/{namespace}/services/{meta['name']}"
        STATE.ksend("DELETE", path)
        # A LoadBalancer Service lingers while its address is released.
        for _ in range(30):
            try:
                STATE.kget(path)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    break
                raise
            time.sleep(1)
        else:
            raise ValueError(f"Service {namespace}/{meta['name']} is still being removed; save again in a moment")
        return create_service(cfg)["message"]
    if want == "ClusterIP":
        return ""
    current_ip = (row.get("requested_ips") or [""])[0]
    current_mode = row.get("vip_mode") or ""
    state = INV.inventory()
    shared = (state["shared_vip"] or {}).get("ip", "")
    # With anything unread (the VIP registry included) nothing counts as
    # unchanged: create_service's plan refuses and says what.
    unchanged = not STATE._unread(state) and (mode == "manual" and vip == current_ip
                 or mode == "shared" and (current_mode == "shared" or (shared and current_ip == shared))
                 or mode == "automatic" and current_ip and current_ip != shared and current_mode != "nodes"
                 or mode == "nodes" and current_mode == "nodes")
    if unchanged:
        return ""
    return create_service({**cfg, "update": True, "uid": meta.get("uid"),
                           "resource_version": meta.get("resourceVersion")})["message"]


def delete_service(namespace, name, force=False):
    """Release a Service and the LAN listeners it owns.

    A Service outlives the workload it was created for, so this is how an
    orphaned VIP:port is reclaimed. Deleting one that still has a workload
    behind it takes that workload off the LAN, so it needs force.
    """
    namespace = STATE._name(namespace, "namespace")
    name = STATE._name(name, "service name")
    if namespace in STATE.SYSTEM_NAMESPACES:
        raise PermissionError("Homestead does not delete Services in system namespaces")
    # A Deployments read that failed leaves every Service with no targets:
    # an app's own would look orphaned and go without force.
    row = next((item for item in STATE._complete(INV.inventory(), "nothing was deleted")["services"]
                if item["namespace"] == namespace and item["name"] == name), None)
    if not row:
        raise ValueError(f"Service {namespace}/{name} does not exist")
    if row["targets"] and not force:
        raise ValueError(f"{namespace}/{name} still serves {', '.join(row['targets'])}; "
                         "removing it takes that workload off the LAN")
    STATE.ksend("DELETE", f"/api/v1/namespaces/{namespace}/services/{name}")
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
