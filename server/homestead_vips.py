"""Where each load-balanced address lives, and whether traffic sent to it
arrives.

An address is reachable only when two things hold. A node must answer for it
on the LAN - kube-vip adds it to one node's interface and answers ARP there,
k3s's ServiceLB listens on every node's own address. And the Service must
carry it in status.loadBalancer.ingress, because that is what kube-proxy
builds its forwarding rules from: a packet for an address no Service lists
reaches the node and is refused.

kube-vip does both for a Service of its own, but not always the second: with
several Services sharing one address and one lease (2.8.175 onwards on
kube-vip 1.2.3), it announced the address and never recorded it, and it said
nothing in its log. Ping answered and every port was refused. So Homestead
checks, and where kube-vip is plainly answering for an address that a Service
asks for but does not carry, records it - what kube-vip would have written
(`keep`).

`address_map` is the picture every page draws from: each node with its own
addresses and the VIPs it answers for, every address with the ports on it,
the Services and workloads behind them, and a state that says in words what
is wrong when something is.
"""
import calendar
import threading
import time

kget = ksend = None

VIP_KEY = "kube-vip.io/loadbalancerIPs"
METALLB_KEY = "metallb.universe.tf/loadBalancerIPs"
LEASE_KEY = "kube-vip.io/leaseName"
HOST_KEY = "kube-vip.io/vipHost"

# What Homestead has recorded for kube-vip, newest last, for the pages to say.
_kept = []
_kept_lock = threading.Lock()


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


def _items(path):
    try:
        return (kget(path) or {}).get("items", []) or []
    except Exception:
        return []


def _when(stamp):
    """A Kubernetes MicroTime or Time as seconds since the epoch; 0 if none."""
    stamp = str(stamp or "")
    if not stamp:
        return 0.0
    try:
        return calendar.timegm(time.strptime(stamp.split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def live_holders(leases, now=None):
    """(namespace, name) -> the node holding each lease someone holds now.

    A lease counts while its holder renewed it within its duration, with the
    same grace kube-vip's own followers give before taking over; a released
    lease has no holder at all.
    """
    now = time.time() if now is None else now
    out = {}
    for lease in leases:
        meta, spec = lease.get("metadata") or {}, lease.get("spec") or {}
        holder = str(spec.get("holderIdentity") or "")
        if not holder:
            continue
        renewed = _when(spec.get("renewTime") or spec.get("acquireTime"))
        duration = int(spec.get("leaseDurationSeconds") or 15)
        if renewed and now - renewed > max(duration * 2, 30):
            continue
        out[(meta.get("namespace", ""), meta.get("name", ""))] = holder
    return out


def _requested(service):
    meta, spec = service.get("metadata") or {}, service.get("spec") or {}
    annotations = meta.get("annotations") or {}
    raw = annotations.get(VIP_KEY) or annotations.get(METALLB_KEY) or spec.get("loadBalancerIP") or ""
    return [ip.strip() for ip in str(raw).split(",") if ip.strip()]


def _assigned(service):
    return [row.get("ip") for row in (((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or [])
            if row.get("ip")]


def controller_of(service, platform):
    """Which load balancer answers for this Service: kube-vip, ServiceLB,
    MetalLB, or none."""
    cls = (service.get("spec") or {}).get("loadBalancerClass") or ""
    lb = platform.get("load_balancer") or ""
    vip_class = platform.get("vip_class") or ""
    if vip_class:
        # kube-vip beside ServiceLB takes its class; ServiceLB takes the rest.
        if cls == vip_class:
            return "kube-vip"
        return "servicelb" if not cls and platform.get("servicelb") else "none"
    if cls:
        return "none"
    if lb == "kube-vip":
        return "kube-vip"
    if lb == "metallb":
        return "metallb"
    return "servicelb" if platform.get("servicelb") else "none"


def _announcer(service, holders, platform):
    """The node kube-vip has answering for this Service's address, if any.

    Where each Service is elected on its own, its lease is named for it - by
    Homestead's shared-lease annotation, or kubevip-<name> - in its own
    namespace (kube-system on older releases). Where kube-vip elects once for
    every Service, plndr-svcs-lock says who answers for all of them.
    """
    meta = service.get("metadata") or {}
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    lease = (meta.get("annotations") or {}).get(LEASE_KEY) or ""
    candidates = [(ns, lease)] if lease else []
    candidates += [(ns, "kubevip-" + name), ("kube-system", "kubevip-" + name)]
    if platform.get("vip_service_election") is not True:
        candidates.append(("kube-system", "plndr-svcs-lock"))
    return next((holders[key] for key in candidates if key in holders), "")


def _ip_key(ip):
    return tuple(int(part) if part.isdigit() else 999 for part in str(ip).split("."))


def address_map(services, nodes, leases, platform, endpoints=None, targets=None, now=None):
    """Every node with its addresses, and every load-balanced address with
    the node answering for it, what listens there and whether it works.

    services, nodes and leases are the API's objects; endpoints maps
    (namespace, service) to its ready endpoint count, targets to the
    workloads it selects.
    """
    endpoints, targets = endpoints or {}, targets or {}
    holders = live_holders(leases, now)
    node_rows, node_of_ip = [], {}
    for node in nodes:
        meta, status = node.get("metadata") or {}, node.get("status") or {}
        name = meta.get("name", "")
        ips = [a["address"] for a in status.get("addresses") or []
               if a.get("type") in ("InternalIP", "ExternalIP") and a.get("address")]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
        labels = meta.get("labels") or {}
        node_rows.append({"name": name, "ips": ips, "ready": ready,
                          "control_plane": any(k in labels for k in ("node-role.kubernetes.io/control-plane",
                                                                      "node-role.kubernetes.io/master"))})
        for ip in ips:
            node_of_ip[ip] = name

    addresses = {}

    def entry(ip, kind, node=""):
        return addresses.setdefault(ip, {"ip": ip, "kind": kind, "node": node, "controller": "",
                                         "listeners": [], "services": [], "unrouted": [], "ready": 0,
                                         "announced": False, "state": "ok", "reason": ""})

    for ip, name in node_of_ip.items():
        entry(ip, "node", name)

    for service in services:
        spec = service.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        meta = service.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        controller = controller_of(service, platform)
        requested, assigned = _requested(service), _assigned(service)
        ready = int(endpoints.get((ns, name), 0) or 0)
        ports = [{"port": p.get("port"), "protocol": p.get("protocol") or "TCP"} for p in spec.get("ports") or []]
        workloads = list(targets.get((ns, name), []))
        if controller == "servicelb":
            ips = [ip for ip in assigned if ip in node_of_ip] or [ip for node in node_rows for ip in node["ips"][:1]]
            announcer = ""
        else:
            ips = requested or assigned
            announcer = _announcer(service, holders, platform) if controller == "kube-vip" else ""
            if not announcer and controller == "kube-vip" and assigned:
                # A Service kube-vip recorded, elected somewhere Homestead
                # cannot see: the node it last named is the best there is.
                announcer = (meta.get("annotations") or {}).get(HOST_KEY) or ""
        for ip in ips:
            row = entry(ip, "node" if ip in node_of_ip else "vip", node_of_ip.get(ip, ""))
            row["controller"] = row["controller"] or controller
            row["ready"] += ready
            row["services"].append({"namespace": ns, "name": name, "controller": controller,
                                    "routed": ip in assigned, "ready": ready, "workloads": workloads})
            for port in ports:
                row["listeners"].append({**port, "namespace": ns, "service": name, "workloads": workloads,
                                         "ready": ready})
            if announcer:
                row["announced"] = True
                row["node"] = row["node"] or announcer
            elif controller == "metallb" and ip in assigned:
                row["announced"] = True
            if controller == "servicelb" and ip in assigned:
                row["announced"] = True
            if ip not in assigned and controller in ("kube-vip", "metallb"):
                row["unrouted"].append({"namespace": ns, "name": name})

    for row in addresses.values():
        who = ", ".join(f"{s['namespace']}/{s['name']}" for s in row["unrouted"])
        if row["kind"] == "node":
            pending = [s for s in row["services"] if s["controller"] == "servicelb" and not s["routed"]]
            if pending:
                row["state"] = "pending"
                row["reason"] = (f"ServiceLB has not published {', '.join(s['name'] for s in pending)} here - "
                                 "another Service holds the same port on the nodes' addresses")
            continue
        if not row["services"]:
            continue
        if row["announced"] and row["unrouted"]:
            row["state"] = "unrouted"
            row["reason"] = (f"{row['node'] or 'A node'} answers for {row['ip']}, but {who} "
                             f"{'does' if len(row['unrouted']) == 1 else 'do'} not carry it, so connections to "
                             f"{'its ports' if len(row['unrouted']) == 1 else 'their ports'} are refused")
        elif row["announced"]:
            row["state"] = "ok"
        elif not row["ready"]:
            row["state"] = "idle"
            row["reason"] = ("Nothing behind it is running, so no node answers for it; it comes back "
                             "when a workload on it starts")
        else:
            row["state"] = "unannounced"
            row["reason"] = (f"{row['controller'] or 'The load balancer'} is not answering for {row['ip']} "
                             "on any node although its workloads are running - check that it is running "
                             "and takes these Services")

    for node in node_rows:
        node["vips"] = sorted((ip for ip, row in addresses.items() if row["kind"] == "vip" and row["node"] == node["name"]),
                              key=_ip_key)
    ordered = sorted(addresses.values(), key=lambda row: (row["kind"] != "node", _ip_key(row["ip"])))
    for row in ordered:
        row["listeners"].sort(key=lambda item: (item["port"] or 0, item["protocol"]))
    return {"nodes": sorted(node_rows, key=lambda n: n["name"]), "addresses": ordered,
            "problems": sum(row["state"] in ("unrouted", "unannounced", "pending") for row in ordered),
            "kept": kept()}


def repairs(services, leases, platform, now=None):
    """The status each Service should carry and does not: kube-vip Services
    whose address a node is answering for - through its own lease or that
    of another Service sharing the address - that do not list it."""
    holders = live_holders(leases, now)
    answered = {}
    for service in services:
        if (service.get("spec") or {}).get("type") != "LoadBalancer" or controller_of(service, platform) != "kube-vip":
            continue
        node = _announcer(service, holders, platform)
        if node:
            for ip in _requested(service):
                answered.setdefault(ip, node)
    out = []
    for service in services:
        spec = service.get("spec") or {}
        if spec.get("type") != "LoadBalancer" or controller_of(service, platform) != "kube-vip":
            continue
        requested = _requested(service)
        if not requested or set(requested) <= set(_assigned(service)):
            continue
        if not all(ip in answered for ip in requested):
            continue
        meta = service.get("metadata") or {}
        ports = [{"port": p.get("port"), "protocol": p.get("protocol") or "TCP"} for p in spec.get("ports") or []]
        out.append({"namespace": meta.get("namespace", ""), "name": meta.get("name", ""),
                    "node": answered[requested[0]], "ips": requested,
                    "status": {"loadBalancer": {"ingress": [{"ip": ip, "ports": ports} for ip in requested]}}})
    return out


def stranded(services, leases, slices, platform, now=None):
    """Shared leases nobody holds while a Service on them has pods ready.

    kube-vip elects once per lease. When one Service sharing it loses its
    last endpoint - a pod restarting - kube-vip gives the lease up and stops
    that election, and it did not start it again when the pod came back
    (kube-vip 1.2.3, on k3s-test: SMB, Homestead's VIP and the object store,
    all on 192.0.2.108, unreachable for an hour). Returns [(namespace,
    lease, [services])].

    Only under per-Service election: with one leader for every VIP (global
    election, plndr-svcs-lock) no Service's own lease is ever held, and
    restarting kube-vip for it would drop every VIP every ten minutes."""
    if (platform or {}).get("vip_service_election") is not True:
        return []
    holders = live_holders(leases, now)
    ready = {(s.get("metadata") or {}).get("namespace", "") + "/" + ((s.get("metadata") or {}).get("labels") or {}).get("kubernetes.io/service-name", "")
             for s in slices
             if any((e.get("conditions") or {}).get("ready") for e in s.get("endpoints") or [])}
    groups = {}
    for service in services:
        meta = service.get("metadata") or {}
        lease = (meta.get("annotations") or {}).get(LEASE_KEY) or ""
        if (not lease or (service.get("spec") or {}).get("type") != "LoadBalancer"
                or controller_of(service, platform) != "kube-vip" or not _requested(service)):
            continue
        groups.setdefault((meta.get("namespace", ""), lease), []).append(meta.get("name", ""))
    return [(ns, lease, sorted(names)) for (ns, lease), names in sorted(groups.items())
            if (ns, lease) not in holders and any(f"{ns}/{name}" in ready for name in names)]


_stranded_since = {}
_restarted = [0.0]
STRANDED_GRACE = 60          # kube-vip's own followers take over well within this
RESTART_EVERY = 600


def revive(services, leases, slices, platform, now=None):
    """Restart kube-vip when a lease has been stranded past the grace, at most
    every ten minutes: it elects again at start. Returns the leases it was for."""
    now = time.time() if now is None else now
    current = {(ns, lease): names for ns, lease, names in stranded(services, leases, slices, platform, now)}
    for key in list(_stranded_since):
        if key not in current:
            del _stranded_since[key]
    for key in current:
        _stranded_since.setdefault(key, now)
    due = [key for key, since in _stranded_since.items() if now - since >= STRANDED_GRACE]
    if not due or now - _restarted[0] < RESTART_EVERY:
        return []
    pods = [p for p in _items("/api/v1/namespaces/kube-system/pods")
            if (p.get("metadata") or {}).get("name", "").startswith("kube-vip")
            and any(o.get("kind") == "DaemonSet" for o in (p.get("metadata") or {}).get("ownerReferences") or [])]
    if not pods:
        return []
    for pod in pods:
        ksend("DELETE", f"/api/v1/namespaces/kube-system/pods/{pod['metadata']['name']}")
    _restarted[0] = now
    for key in due:
        print(f"VIPs: lease {key[0]}/{key[1]} had no holder for {int(now - _stranded_since[key])}s while "
              f"{', '.join(current[key])} had pods ready; restarted kube-vip to elect again", flush=True)
        del _stranded_since[key]
    return due


def keep(platform):
    """Record what kube-vip announced and left off its Services, and restart
    kube-vip where it stopped electing for a shared lease. Returns what was
    recorded."""
    if (platform or {}).get("load_balancer") != "kube-vip":
        return []
    services = _items("/api/v1/services")
    leases = _items("/apis/coordination.k8s.io/v1/leases")
    try:
        revive(services, leases, _items("/apis/discovery.k8s.io/v1/endpointslices"), platform)
    except Exception as error:
        print(f"VIPs: could not restart kube-vip: {str(error)[:160]}", flush=True)
    done = []
    for fix in repairs(services, leases, platform):
        path = f"/api/v1/namespaces/{fix['namespace']}/services/{fix['name']}/status"
        try:
            ksend("PATCH", path, {"status": fix["status"]}, ctype="application/merge-patch+json")
        except Exception as error:
            print(f"VIPs: could not record {', '.join(fix['ips'])} on {fix['namespace']}/{fix['name']}: "
                  f"{str(error)[:160]}", flush=True)
            continue
        print(f"VIPs: {fix['node']} answers for {', '.join(fix['ips'])} and kube-vip left it off "
              f"{fix['namespace']}/{fix['name']}; recorded it", flush=True)
        record = {"at": int(time.time()), "namespace": fix["namespace"], "name": fix["name"],
                  "ips": fix["ips"], "node": fix["node"]}
        with _kept_lock:
            _kept.append(record)
            del _kept[:-20]
        done.append(record)
    return done


def kept():
    with _kept_lock:
        return list(_kept)


def alert_facts(addresses):
    """An address no connection reaches, as a condition: one Homestead
    records itself clears before the alert's hold time, so what is left is
    what needs someone."""
    facts = []
    for row in (addresses or {}).get("addresses") or []:
        if row.get("state") not in ("unrouted", "unannounced"):
            continue
        facts.append({"key": f"address:{row['ip']}", "category": "degraded", "severity": "degraded",
                      "title": f"Service address {row['ip']} has no route",
                      "resolved": f"Service routing warning cleared: {row['ip']}",
                      "body": row.get("reason", ""), "href": "/networking"})
    return facts
