"""IP address management: what lives at each address on the LAN.

A homelab's addresses are spread across a router's DHCP table, a few static
assignments made years ago, and the VIPs the cluster hands out. This keeps
them in one place, per subnet:

* documented addresses - a name, the MAC, what kind of assignment it is
  (static, DHCP reservation, dynamic, held for later), notes and tags;
* what the cluster uses, read live and never edited here: node addresses,
  load balancer VIPs, and Harvester's IP pool ranges;
* what a scan finds answering, and what the UniFi controller knows.

Each address is checked against its subnet's DHCP range: a static address or
a VIP inside it is one the DHCP server may also hand out, the classic cause of
a device dropping off the network for no visible reason.

The record lives in the `homestead-ipam` ConfigMap; the UniFi API key in a
Secret of its own.
"""
import base64
import concurrent.futures
import errno
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import homestead_names as NAMES

kget = ksend = None
NAMESPACE = "default"
cluster_facts = lambda: {}
KINDS = ("static", "reservation", "dhcp", "reserved", "infrastructure")
# What the thing at an address is, as opposed to how it got the address.
CATEGORIES = ("router", "switch", "access-point", "server", "nas", "iot", "cctv", "printer",
              "computer", "phone", "media", "other")
# UniFi model prefixes, for devices whose category nobody has set.
UNIFI_MODELS = (("UDM", "router"), ("UDR", "router"), ("UCG", "router"), ("UXG", "router"), ("USG", "router"),
                ("UX", "router"), ("USW", "switch"), ("US", "switch"), ("UAP", "access-point"),
                ("U6", "access-point"), ("U7", "access-point"), ("UAL", "access-point"), ("UAC", "access-point"),
                ("E7", "access-point"), ("UVC", "cctv"), ("UNVR", "nas"), ("UNAS", "nas"), ("UCK", "server"))
DATA_KEY = "ipam.json"
MAX_SUBNET_HOSTS = 1024
SCAN_PORTS = (22, 53, 80, 443, 445, 554, 1883, 3389, 5000, 5353, 8006, 8080, 8123, 8443, 9000, 9100)
SCAN_TIMEOUT = 0.5
_scans = {}
_threads = {}
_scan_lock = threading.Lock()


def bind(_kget, _ksend, namespace, _cluster_facts):
    global kget, ksend, NAMESPACE, cluster_facts
    kget, ksend, NAMESPACE, cluster_facts = _kget, _ksend, namespace, _cluster_facts


def _map():
    return NAMES.object_name("ipam")


def _secret():
    return NAMES.object_name("unifi")


def _empty():
    return {"subnets": [], "records": {}, "scans": {}, "unifi": {}, "unifi_networks": []}


def load():
    try:
        cm = kget(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return _empty(), None
        raise
    try:
        data = json.loads((cm.get("data") or {}).get(DATA_KEY) or "{}")
    except ValueError:
        data = {}
    base = _empty()
    base.update({key: value for key, value in data.items() if key in base and isinstance(value, type(base[key]))})
    return base, cm


def update(change, attempts=4):
    """Read, change and write the record, retrying if another writer got
    there first: the ConfigMap's resourceVersion refuses a stale write."""
    for _ in range(attempts):
        data, cm = load()
        result = change(data)
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": _map(), "namespace": NAMESPACE, "labels": {NAMES.key("managed"): "true"}},
                "data": {DATA_KEY: json.dumps(data, separators=(",", ":"), sort_keys=True)}}
        try:
            if cm is None:
                ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body)
            else:
                body["metadata"]["resourceVersion"] = cm["metadata"]["resourceVersion"]
                ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{_map()}", body)
            return result
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
    raise RuntimeError("the address record kept changing underneath; try again")


# ------------------------------------------------------------------ subnets
def _ip(value, label="address"):
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        raise ValueError(f"{value or '(blank)'} is not an IP {label}")
    if address.version != 4:
        raise ValueError("only IPv4 is kept here")
    return address


def _clean_subnet(row):
    try:
        network = ipaddress.ip_network(str(row.get("cidr") or "").strip(), strict=False)
    except ValueError:
        raise ValueError(f"{row.get('cidr') or '(blank)'} is not a subnet like 192.0.2.0/24")
    if network.version != 4 or network.num_addresses - 2 > MAX_SUBNET_HOSTS or network.prefixlen > 30:
        raise ValueError("a subnet is an IPv4 range from /22 to /30")
    clean = {"id": re.sub(r"[^0-9./]", "", str(network)), "cidr": str(network),
             "name": " ".join(str(row.get("name") or "").split())[:40],
             "vlan": int(row["vlan"]) if str(row.get("vlan") or "").strip().isdigit() else None,
             "gateway": "", "dhcp_start": "", "dhcp_end": "", "note": str(row.get("note") or "")[:200]}
    for key in ("gateway", "dhcp_start", "dhcp_end"):
        if str(row.get(key) or "").strip():
            address = _ip(row[key], key.replace("_", " "))
            if address not in network:
                raise ValueError(f"{address} is outside {network}")
            clean[key] = str(address)
    if bool(clean["dhcp_start"]) != bool(clean["dhcp_end"]):
        raise ValueError("a DHCP range needs both its first and last address")
    if clean["dhcp_start"] and int(_ip(clean["dhcp_start"])) > int(_ip(clean["dhcp_end"])):
        raise ValueError("the DHCP range ends before it starts")
    return clean


def save_subnets(rows):
    clean = [_clean_subnet(row) for row in rows or []]
    cidrs = [row["cidr"] for row in clean]
    if len(set(cidrs)) != len(cidrs):
        raise ValueError("a subnet is listed twice")
    for a in clean:
        for b in clean:
            if a is not b and ipaddress.ip_network(a["cidr"]).overlaps(ipaddress.ip_network(b["cidr"])):
                raise ValueError(f"{a['cidr']} and {b['cidr']} overlap")

    def change(data):
        data["subnets"] = clean
        return {"ok": True, "subnets": clean}
    return update(change)


def suggested_subnets():
    """The /24s the cluster's own nodes sit in: where most people start."""
    facts = cluster_facts() or {}
    nets = {str(ipaddress.ip_network(f"{ip}/24", strict=False)) for ip in facts.get("node_ips") or []
            if _is_v4(ip)}
    return sorted(nets)


def _is_v4(value):
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


# ------------------------------------------------------------------ records
def _clean_record(row, existing=None):
    record = dict(existing or {})
    if "name" in row:
        record["name"] = " ".join(str(row.get("name") or "").split())[:60]
    if "mac" in row:
        mac = str(row.get("mac") or "").strip().lower().replace("-", ":")
        if mac and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            raise ValueError(f"{row.get('mac')} is not a MAC address like aa:bb:cc:dd:ee:ff")
        record["mac"] = mac
    if "kind" in row:
        kind = str(row.get("kind") or "")
        if kind and kind not in KINDS:
            raise ValueError(f"the kind is one of: {', '.join(KINDS)}")
        record["kind"] = kind
    if "category" in row:
        category = str(row.get("category") or "")
        if category and category not in CATEGORIES:
            raise ValueError(f"a device category is one of: {', '.join(CATEGORIES)}")
        record["category"] = category
    if "note" in row:
        record["note"] = str(row.get("note") or "")[:300]
    if "owner" in row:
        record["owner"] = " ".join(str(row.get("owner") or "").split())[:60]
    if "tags" in row:
        record["tags"] = sorted({re.sub(r"[^a-z0-9._-]+", "-", str(tag).strip().lower()).strip("-")[:24]
                                 for tag in row.get("tags") or [] if str(tag).strip()})[:12]
    record["updated"] = int(time.time())
    return record


def save_record(row):
    ip = str(_ip(row.get("ip")))

    def change(data):
        data["records"][ip] = _clean_record(row, data["records"].get(ip))
        data["records"][ip].setdefault("sources", [])
        if "manual" not in data["records"][ip]["sources"]:
            data["records"][ip]["sources"].append("manual")
        return {"ok": True, "ip": ip}
    return update(change)


def bulk(ips, changes):
    """The same change to many addresses: a kind, tags added or taken away,
    an owner or a note."""
    targets = sorted({str(_ip(ip)) for ip in ips or []}, key=lambda v: int(ipaddress.ip_address(v)))
    if not targets:
        raise ValueError("choose at least one address")
    changes = changes or {}
    add = [t for t in changes.get("tags_add") or [] if str(t).strip()]
    drop = {re.sub(r"[^a-z0-9._-]+", "-", str(t).strip().lower()).strip("-") for t in changes.get("tags_remove") or []}

    def change(data):
        for ip in targets:
            if changes.get("forget"):
                # The last scan's answer goes too: it kept the row on the
                # page, so removing an address looked as though it did
                # nothing. Something still there is found by the next scan.
                data["records"].pop(ip, None)
                for scan in data["scans"].values():
                    (scan.get("hosts") or {}).pop(ip, None)
                continue
            current = data["records"].get(ip, {})
            row = {key: changes[key] for key in ("kind", "category", "owner", "note") if key in changes}
            tags = [t for t in current.get("tags") or [] if t not in drop] + add
            row["tags"] = tags
            data["records"][ip] = _clean_record(row, current)
            sources = data["records"][ip].setdefault("sources", [])
            if "manual" not in sources:
                sources.append("manual")
        return {"ok": True, "count": len(targets),
                "detail": f"{len(targets)} address{'es' if len(targets) != 1 else ''} " +
                          ("removed" if changes.get("forget") else "updated")}
    return update(change)


# ------------------------------------------------------------------ the view
def _in_range(ip, start, end):
    return bool(start and end) and int(_ip(start)) <= int(ip) <= int(_ip(end))


def _pool_ranges(facts):
    out = []
    for pool in facts.get("pools") or []:
        for r in pool.get("ranges") or []:
            if r.get("start") and r.get("end"):
                out.append((pool.get("name", ""), int(_ip(r["start"])), int(_ip(r["end"]))))
            elif r.get("subnet"):
                try:
                    net = ipaddress.ip_network(r["subnet"], strict=False)
                    out.append((pool.get("name", ""), int(net.network_address) + 1, int(net.broadcast_address) - 1))
                except ValueError:
                    pass
    return out


def view():
    data, _ = load()
    facts = cluster_facts() or {}
    vips = {row["ip"]: sorted({f"{l.get('namespace')}/{l.get('service')}" for l in row.get("listeners") or []})
            for row in facts.get("vips") or [] if row.get("ip")}
    nodes = set(facts.get("node_ips") or [])
    pools = _pool_ranges(facts)
    subnets = []
    for subnet in data["subnets"]:
        network = ipaddress.ip_network(subnet["cidr"])
        scan = data["scans"].get(subnet["cidr"]) or {}
        found = scan.get("hosts") or {}
        live = _scans.get(subnet["cidr"])
        rows = {}

        def row_for(ip):
            return rows.setdefault(ip, {"ip": ip, "name": "", "mac": "", "kind": "", "category": "", "note": "", "owner": "",
                                        "tags": [], "sources": [], "cluster": "", "scan": None, "unifi": None,
                                        "flags": []})

        for ip, record in data["records"].items():
            if _is_v4(ip) and ipaddress.ip_address(ip) in network:
                row = row_for(ip)
                row.update({k: v for k, v in record.items() if k in row or k in ("updated", "last_seen")})
        for ip in nodes:
            if _is_v4(ip) and ipaddress.ip_address(ip) in network:
                row_for(ip)["cluster"] = "node"
        for ip, services in vips.items():
            if _is_v4(ip) and ipaddress.ip_address(ip) in network:
                row = row_for(ip)
                row["cluster"] = row["cluster"] or "vip"
                row["services"] = services
        # Reserved for Homestead's Services, used yet or not.
        for reserved in facts.get("registered_vips") or []:
            ip = reserved.get("ip", "")
            if _is_v4(ip) and ipaddress.ip_address(ip) in network:
                row = row_for(ip)
                row["cluster"] = row["cluster"] or "vip"
                if reserved.get("label") and not row["name"]:
                    row["name"] = reserved["label"]
        for ip, host in found.items():
            if _is_v4(ip) and ipaddress.ip_address(ip) in network:
                row_for(ip)["scan"] = host

        start, end = subnet.get("dhcp_start"), subnet.get("dhcp_end")
        for ip, row in rows.items():
            address = ipaddress.ip_address(ip)
            in_dhcp = _in_range(address, start, end)
            row["in_dhcp"] = in_dhcp
            pool = next((name for name, lo, hi in pools if lo <= int(address) <= hi), "")
            row["pool"] = pool
            if in_dhcp and (row["kind"] == "static" or row["cluster"] in ("node", "vip")):
                row["flags"].append({"level": "warn", "text": "inside the DHCP range: the DHCP server may hand this address to something else"})
            if row["scan"] and row["scan"].get("up") and not (row["name"] or row["cluster"] or row["kind"]):
                row["flags"].append({"level": "info", "text": "answers on the network but is not documented"})
            if row["kind"] == "reserved" and row["scan"] and row["scan"].get("up"):
                row["flags"].append({"level": "warn", "text": "held for later, yet something answers here"})
            if subnet.get("gateway") == ip:
                row["gateway"] = True
        # Harvester handing out VIPs from the DHCP range is the same clash, before it happens.
        pool_clash = [name for name, lo, hi in pools
                      if start and end and lo <= int(_ip(end)) and hi >= int(_ip(start))
                      and ipaddress.ip_address(lo) in network]
        usable = [int(a) for a in network.hosts()]
        used = {int(ipaddress.ip_address(ip)) for ip, row in rows.items()
                if row["name"] or row["kind"] or row["cluster"] or (row["scan"] or {}).get("up")}
        taken_ranges = [(int(_ip(start)), int(_ip(end)))] if start and end else []
        taken_ranges += [(lo, hi) for _, lo, hi in pools]
        free = [a for a in usable if a not in used and not any(lo <= a <= hi for lo, hi in taken_ranges)
                and str(ipaddress.ip_address(a)) != subnet.get("gateway")]
        subnets.append({**subnet, "rows": sorted(rows.values(), key=lambda r: int(ipaddress.ip_address(r["ip"]))),
                        "usable": len(usable), "used": len(used),
                        "dhcp_size": (int(_ip(end)) - int(_ip(start)) + 1) if start and end else 0,
                        "free_static": len(free), "next_free": [str(ipaddress.ip_address(a)) for a in free[:8]],
                        "free_list": [str(ipaddress.ip_address(a)) for a in free[:64]],
                        "pool_clash": pool_clash,
                        "scan": {"at": scan.get("at", 0), "state": (live or {}).get("state") or scan.get("state") or "",
                                 "progress": (live or {}).get("progress", 100 if scan.get("at") else 0)}})
    unifi = dict(data.get("unifi") or {})
    # UniFi is optional: without an address and a key, nothing of it is shown.
    unifi["configured"] = bool(unifi.get("url") and unifi.get("has_key"))
    known = {s["cidr"] for s in data["subnets"]}
    return {"subnets": subnets, "kinds": list(KINDS), "categories": list(CATEGORIES),
            "suggested": [c for c in suggested_subnets() if c not in known],
            "unifi_networks": [n for n in data.get("unifi_networks") or [] if n.get("cidr") not in known],
            "unifi": unifi}


# ------------------------------------------------------------------ scanning
def _probe(ip, ports=SCAN_PORTS, timeout=SCAN_TIMEOUT):
    """Whether a host is there, and what it listens on. A refused connection
    is as good as an accepted one: only a live host refuses."""
    open_ports, alive, started = [], False, time.monotonic()
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            code = sock.connect_ex((ip, port))
        except OSError as error:
            code = error.errno or -1
        finally:
            sock.close()
        if code == 0:
            open_ports.append(port)
            alive = True
        elif code in (errno.ECONNREFUSED, 10061):
            alive = True
        if alive and len(open_ports) >= 6:
            break
    host = {"up": alive, "ports": open_ports}
    if alive:
        host["ms"] = int((time.monotonic() - started) * 1000)
    return host


def _rdns(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""


def scan(subnet_id, probe=None, workers=64):
    """Scans a subnet in the background; the view shows progress, and the
    result is written to the record when it is done."""
    data, _ = load()
    subnet = next((row for row in data["subnets"] if row["id"] == subnet_id), None)
    if not subnet:
        raise ValueError("no such subnet")
    cidr = subnet["cidr"]
    with _scan_lock:
        if (_scans.get(cidr) or {}).get("state") == "running":
            return {"ok": True, "detail": f"{cidr} is already being scanned"}
        _scans[cidr] = {"state": "running", "progress": 0}
    probe = probe or _probe
    hosts = [str(a) for a in ipaddress.ip_network(cidr).hosts()]

    def work():
        found = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(probe, ip): ip for ip in hosts}
                for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                    result = future.result()
                    if result.get("up"):
                        found[futures[future]] = result
                    _scans[cidr]["progress"] = int(done * 95 / len(hosts))
                names = pool.map(_rdns, list(found))
                for ip, name in zip(list(found), names):
                    if name:
                        found[ip]["rdns"] = name
            stamp = int(time.time())
            for host in found.values():
                host["seen"] = stamp

            def change(data):
                data["scans"][cidr] = {"at": stamp, "state": "done", "hosts": found}
            update(change)
            _scans[cidr] = {"state": "done", "progress": 100, "found": len(found)}
        except Exception as error:
            _scans[cidr] = {"state": "failed", "progress": 100, "error": str(error)[:160]}

    thread = _threads[cidr] = threading.Thread(target=work, daemon=True)
    thread.start()
    return {"ok": True, "detail": f"scanning {len(hosts)} addresses in {cidr}"}


# ------------------------------------------------------------------ UniFi
def _unifi_key():
    try:
        secret = kget(f"/api/v1/namespaces/{NAMESPACE}/secrets/{_secret()}")
        return base64.b64decode((secret.get("data") or {}).get("api_key", "")).decode()
    except Exception:
        return ""


def save_unifi(cfg):
    if cfg.get("forget"):
        # Disconnect: the key goes, and so does everything that shows UniFi.
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NAMESPACE}/secrets/{_secret()}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise

        def forget(data):
            data["unifi"], data["unifi_networks"] = {}, []
            return {"ok": True, "unifi": {}}
        return update(forget)
    url = str(cfg.get("url") or "").strip().rstrip("/")
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise ValueError("the UniFi console address is https://its-address, with nothing else in it")
    site = re.sub(r"[^A-Za-z0-9_-]", "", str(cfg.get("site") or "default"))[:40] or "default"
    key = str(cfg.get("api_key") or "").strip()
    if key:
        body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": _secret(), "namespace": NAMESPACE, "labels": {NAMES.key("managed"): "true"}},
                "data": {"api_key": base64.b64encode(key.encode()).decode()}}
        try:
            ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/secrets", body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/secrets/{_secret()}", body)

    def change(data):
        current = data.get("unifi") or {}
        data["unifi"] = {**current, "url": url, "site": site, "verify_tls": bool(cfg.get("verify_tls")),
                         "has_key": bool(key) or bool(current.get("has_key"))}
        return {"ok": True, "unifi": data["unifi"]}
    return update(change)


def _unifi_get(url, key, verify):
    context = ssl.create_default_context()
    if not verify:
        # Consoles ship a self-signed certificate; the address was typed by an admin.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"X-API-KEY": key, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15, context=context) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def unifi_fetch(cfg, key, get=None):
    """Clients, devices and reservations from a UniFi Network controller."""
    get = get or (lambda url: _unifi_get(url, key, cfg.get("verify_tls")))
    base = cfg["url"].rstrip("/")
    api = f"{base}/proxy/network/integration/v1"
    sites = get(f"{api}/sites").get("data") or []
    wanted = cfg.get("site") or "default"
    site = next((s for s in sites if s.get("internalReference") == wanted or s.get("name") == wanted), None) or (sites[0] if sites else None)
    if not site:
        raise ValueError("the controller lists no sites")

    def pages(path):
        rows, offset = [], 0
        while True:
            page = get(f"{api}/sites/{site['id']}/{path}?offset={offset}&limit=200")
            batch = page.get("data") or []
            rows.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page.get("totalCount") or 0):
                return rows

    clients, devices = pages("clients"), pages("devices")
    reservations, networks, notes, known = [], [], [], {}
    classic = f"{base}/proxy/network/api/s/{site.get('internalReference') or wanted}"
    try:
        # The classic API is the one that knows fixed IPs - the reserved
        # addresses - and each network's DHCP range; not every key may use it.
        users = get(f"{classic}/rest/user").get("data") or []
        reservations = [u for u in users if u.get("use_fixedip") and u.get("fixed_ip")]
        # The nickname someone gave a client on the controller, and the
        # hostname the client announced for itself.
        known = {str(u.get("mac", "")).lower(): {"alias": u.get("name") or "", "hostname": u.get("hostname") or ""}
                 for u in users if u.get("mac")}
    except Exception as error:
        notes.append(f"reserved addresses could not be read ({str(error)[:80]})")
    try:
        for conf in get(f"{classic}/rest/networkconf").get("data") or []:
            network = _unifi_network(conf)
            if network:
                networks.append(network)
    except Exception as error:
        notes.append(f"networks could not be read ({str(error)[:80]})")
    return {"clients": clients, "devices": devices, "reservations": reservations, "networks": networks, "known": known,
            "note": "; ".join(notes), "site": site.get("name") or wanted}


def _unifi_network(conf):
    """A UniFi network as a subnet: its range, gateway, VLAN and DHCP pool."""
    subnet = str(conf.get("ip_subnet") or "")
    if not subnet or "/" not in subnet:
        return None
    try:
        network = ipaddress.ip_network(subnet, strict=False)
        gateway = str(ipaddress.ip_address(subnet.split("/")[0]))
    except ValueError:
        return None
    if network.version != 4 or network.num_addresses - 2 > MAX_SUBNET_HOSTS:
        return None
    row = {"cidr": str(network), "name": str(conf.get("name") or "")[:40], "gateway": gateway,
           "vlan": int(conf["vlan"]) if conf.get("vlan_enabled") and str(conf.get("vlan") or "").isdigit() else None,
           "dhcp_start": "", "dhcp_end": ""}
    if conf.get("dhcpd_enabled") and conf.get("dhcpd_start") and conf.get("dhcpd_stop"):
        row["dhcp_start"], row["dhcp_end"] = str(conf["dhcpd_start"]), str(conf["dhcpd_stop"])
    return row


def _category_for_model(model):
    model = str(model or "").upper()
    return next((category for prefix, category in UNIFI_MODELS if model.startswith(prefix)), "")


def merge_unifi(data, fetched, now=None):
    """Adds what UniFi knows, never overwriting what a person wrote."""
    now = int(now or time.time())
    records = data["records"]
    fixed = {str(r.get("mac", "")).lower(): r for r in fetched["reservations"]}
    touched = 0

    known = fetched.get("known") or {}

    def merge(ip, mac, name, kind, extra, category=""):
        nonlocal touched
        if not _is_v4(ip):
            return
        record = records.setdefault(ip, {})
        manual = "manual" in (record.get("sources") or [])
        if category and not record.get("category"):
            record["category"] = category
        # UniFi's names are kept apart from the one a person gives an address:
        # the nickname set on the controller (or the name UniFi shows), and the
        # hostname the device announced. A sync never touches record["name"].
        names = known.get(str(mac).lower(), {})
        extra = {**extra, "name": (names.get("alias") or name or "")[:60], "hostname": (names.get("hostname") or "")[:80]}
        if mac:
            record["mac"] = mac.lower()
        if kind and not (manual and record.get("kind")):
            record["kind"] = kind
        record["unifi"] = {**extra, "seen": now}
        sources = record.setdefault("sources", [])
        if "unifi" not in sources:
            sources.append("unifi")
        touched += 1

    for device in fetched["devices"]:
        merge(device.get("ipAddress", ""), device.get("macAddress", ""), device.get("name", ""),
              "infrastructure", {"type": "device", "model": device.get("model", ""), "state": device.get("state", "")},
              _category_for_model(device.get("model")))
    for client in fetched["clients"]:
        mac = str(client.get("macAddress", "")).lower()
        ip = client.get("ipAddress", "")
        reserved = fixed.get(mac)
        kind = "reservation" if reserved and reserved.get("fixed_ip") == ip else "dhcp"
        merge(ip, mac, client.get("name", ""), kind,
              {"type": str(client.get("type", "")).lower(), "connected": client.get("connectedAt", ""), "online": True,
               "reserved": bool(reserved and reserved.get("fixed_ip") == ip)})
    online = {str(c.get("macAddress", "")).lower() for c in fetched["clients"]}
    for mac, row in fixed.items():
        if mac not in online:
            merge(row["fixed_ip"], mac, row.get("name") or row.get("hostname") or "", "reservation",
                  {"type": "reservation", "online": False, "reserved": True})
    # A reservation for a client now at another address still holds its address.
    for mac, row in fixed.items():
        record = records.get(row["fixed_ip"])
        if record is not None and record.get("mac") not in ("", mac) and "manual" not in (record.get("sources") or []):
            record.setdefault("unifi", {})["reserved_for"] = mac
    # The networks: fill in what a documented subnet lacks, and keep the rest
    # to offer as subnets.
    networks = fetched.get("networks") or []
    for subnet in data["subnets"]:
        match = next((n for n in networks if n["cidr"] == subnet["cidr"]), None)
        if not match:
            continue
        for key in ("name", "gateway", "dhcp_start", "dhcp_end", "vlan"):
            if not subnet.get(key) and match.get(key):
                subnet[key] = match[key]
    data["unifi_networks"] = networks
    return touched


def sync_unifi(get=None):
    data, _ = load()
    cfg = data.get("unifi") or {}
    if not cfg.get("url"):
        raise ValueError("set the UniFi console's address and API key first")
    key = _unifi_key()
    if not key and get is None:
        raise ValueError("the UniFi API key is missing; save it again")
    try:
        fetched = unifi_fetch(cfg, key, get)
    except urllib.error.HTTPError as error:
        message = ("the console refused the API key" if error.code in (401, 403)
                   else f"the console answered {error.code}")
        _record_sync(False, message)
        raise ValueError(message)
    except (OSError, ValueError) as error:
        _record_sync(False, str(error)[:160])
        raise ValueError(f"could not reach the UniFi console: {str(error)[:120]}")

    def change(data):
        touched = merge_unifi(data, fetched)
        data["unifi"] = {**(data.get("unifi") or {}), "last_sync": int(time.time()), "last_error": "",
                         "note": fetched["note"], "site_name": fetched["site"]}
        return touched
    touched = update(change)
    return {"ok": True, "detail": f"{touched} address{'es' if touched != 1 else ''} from UniFi, "
            f"{len(fetched['reservations'])} reserved" + (f"; {fetched['note']}" if fetched["note"] else ""),
            "clients": len(fetched["clients"]), "devices": len(fetched["devices"]),
            "reservations": len(fetched["reservations"]), "networks": len(fetched.get("networks") or [])}


def _record_sync(ok, message):
    def change(data):
        data["unifi"] = {**(data.get("unifi") or {}), "last_error": "" if ok else message, "last_attempt": int(time.time())}
    try:
        update(change)
    except Exception:
        pass


# ------------------------------------------------------------------ CSV import
CSV_COLUMNS = ("address", "name", "mac", "kind", "category", "owner", "tags", "note")


def import_csv(text):
    """Documents addresses from a spreadsheet. Each row names an address and
    any of the columns above; a blank cell leaves what is recorded alone, and
    columns Homestead does not know are skipped, so an export can be edited and
    imported back. Every row is checked before anything is written."""
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(str(text or "").lstrip("\ufeff"))))
    if not rows:
        raise ValueError("the file has no rows under its header")
    header = {str(h or "").strip().lower() for h in rows[0].keys()}
    if "address" not in header and "ip" not in header:
        raise ValueError("the first row must name the columns, with one called address")
    if len(rows) > 4096:
        raise ValueError("import at most 4096 addresses at a time")
    parsed, errors = [], []
    for number, raw in enumerate(rows, start=2):
        cells = {str(k or "").strip().lower(): str(v or "").strip() for k, v in raw.items()}
        ip = cells.get("address") or cells.get("ip")
        if not ip:
            continue
        change = {key: cells[key] for key in ("name", "mac", "kind", "category", "owner", "note") if cells.get(key)}
        if cells.get("tags"):
            change["tags"] = [t for t in re.split(r"[\s,;]+", cells["tags"]) if t]
        try:
            ip = str(_ip(ip))
            _clean_record(change)
        except ValueError as error:
            errors.append(f"row {number}: {error}")
            continue
        parsed.append((ip, change))
    if errors:
        more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
        raise ValueError("; ".join(errors[:5]) + more)
    if not parsed:
        raise ValueError("no row names an address")

    def change(data):
        created = updated = 0
        for ip, row in parsed:
            existing = data["records"].get(ip)
            record = _clean_record(row, existing)
            sources = record.setdefault("sources", [])
            if "manual" not in sources:
                sources.append("manual")
            data["records"][ip] = record
            created, updated = (created + 1, updated) if existing is None else (created, updated + 1)
        return {"ok": True, "created": created, "updated": updated,
                "detail": f"{created} address{'es' if created != 1 else ''} added, {updated} updated"}
    return update(change)
