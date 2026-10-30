"""The addresses Homestead may hand out: the VIP registry ConfigMap, its default
address, and adding, removing, relabelling and changing a VIP (#623, from
homestead_networking).
"""
import ipaddress
import json
import time
import urllib.error
import homestead_kube as KUBE
import homestead_platform as PLATFORM
import homestead_network_state as STATE
import homestead_network_inventory as INV


# kube-vip announces whatever address a Service asks for; nothing on a plain
# Harvester install hands addresses out. A Harvester IP pool is one source of
# free ones. This is the other: addresses reserved here, by hand, for
# Homestead to give to Services - named, so the picker says what each is for.
VIP_MAP = "homestead-vips"
MAX_ADD = 64


class _NothingAdded(Exception):
    """add_vips found nothing to add: nothing is saved."""


def _vip_map_path():
    return f"/api/v1/namespaces/{STATE.DEFAULT_NAMESPACE}/configmaps/{VIP_MAP}"


# What the inventory's "unread" calls the registry when it cannot be read.
UNREAD = "VIP registry"


def registered():
    """Your VIPs, [] when the registry was never saved. One that cannot be
    read raises: it is not an empty one (the default VIP would fall back to
    the shared address, and reserved addresses would look free)."""
    try:
        cm = STATE.kget(_vip_map_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise RuntimeError(f"Your VIPs could not be read ({str(error)[:160]})") from error
    except Exception as error:
        raise RuntimeError(f"Your VIPs could not be read ({str(error)[:160]})") from error
    try:
        rows = json.loads((cm.get("data") or {}).get("vips.json") or "[]")
    except ValueError:
        rows = []
    return [row for row in rows if isinstance(row, dict) and row.get("ip")]


def shared_vip():
    """A workload default, independent of Homestead's own access address."""
    return next((row["ip"] for row in registered() if row.get("default")), STATE.SHARED_VIP)


def set_default_vip(ip):
    ip = STATE._ipv4(ip, "default workload VIP")
    state = INV.inventory()
    if ip in state["node_ips"]:
        raise ValueError("A node address cannot be the default workload VIP")
    INV.check_address(ip, state)
    if PLATFORM.detect().get("load_balancer") not in ("kube-vip", "metallb"):
        raise ValueError("Install kube-vip or MetalLB before choosing a workload VIP; ServiceLB uses node addresses")
    def choose(rows):
        if not any(row["ip"] == ip for row in rows):
            raise ValueError("Add this reserved address to Your VIPs first")
        for row in rows:
            row["default"] = row["ip"] == ip
        return rows
    _update_registered(choose)
    return {"ok": True, "ip": ip, "detail": "Default workload VIP saved; existing Services are unchanged"}


def _update_registered(change):
    """Change Your VIPs as they are stored when the change is saved (#621):
    change(rows) gets the stored rows and returns the new ones, and runs again
    on the newer rows if another change was saved meanwhile. It may raise to
    stop; nothing is saved. A registry that cannot be read is an error, never
    an empty one to save over."""
    def apply(stored):
        if stored is not None and not isinstance(stored, list):
            raise ValueError("Your VIPs could not be read (vips.json is not a list); nothing was changed")
        rows = [row for row in (stored or []) if isinstance(row, dict) and row.get("ip")]
        return sorted(change(rows), key=lambda r: int(ipaddress.ip_address(r["ip"])))
    return KUBE.update_json(_vip_map_path(), "vips.json", apply, read=STATE.kget, write=STATE.ksend,
                            labels={"homestead.io/managed": "true"}, indent=1)


def add_vips(cfg, ipam_records=None):
    """Reserve addresses for Services: one, or a range, with a label."""
    start = STATE._ipv4(cfg.get("ip") or cfg.get("start"), "address")
    end = STATE._ipv4(cfg.get("end") or start, "last address")
    first, last = int(ipaddress.ip_address(start)), int(ipaddress.ip_address(end))
    if last < first:
        raise ValueError("the last address comes before the first")
    if last - first + 1 > MAX_ADD:
        raise ValueError(f"at most {MAX_ADD} addresses at once")
    label = " ".join(str(cfg.get("label") or "").split())[:60]
    state = STATE._complete(INV.inventory(), "nothing was added")
    nodes = set(state["node_ips"])
    records = ipam_records or {}
    added, skipped = [], []

    def add(rows):
        added.clear(), skipped.clear()      # again from the top if another change was saved meanwhile
        have = {row["ip"] for row in rows}
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
        if not added:
            raise _NothingAdded()
        return rows
    try:
        _update_registered(add)
    except _NothingAdded:
        pass
    return {"ok": bool(added), "added": added, "skipped": skipped,
            "detail": (f"{len(added)} address{'es' if len(added) != 1 else ''} added" if added else "nothing added")
                      + (f"; {len(skipped)} skipped: " + "; ".join(skipped[:4]) if skipped else "")}


def remove_vip(ip):
    ip = STATE._ipv4(ip, "address")
    if any(row["ip"] == ip and row.get("default") for row in registered()):
        raise ValueError("Choose another default workload VIP before removing this address")
    state = STATE._complete(INV.inventory(), "nothing was removed")
    users = next((row for row in state["vips"] if row["ip"] == ip), None)
    if users:
        names = sorted({f"{l['namespace']}/{l['service']}" for l in users["listeners"]})
        raise ValueError(f"{ip} is in use by {', '.join(names)}; remove or move those first")
    def remove(rows):
        if any(row["ip"] == ip and row.get("default") for row in rows):
            raise ValueError("Choose another default workload VIP before removing this address")
        return [row for row in rows if row["ip"] != ip]
    _update_registered(remove)
    return {"ok": True, "detail": f"{ip} is no longer reserved for Homestead"}


VIP_KEYS = ("kube-vip.io/loadbalancerIPs", "kube-vip.io/leaseName",
            "metallb.universe.tf/loadBalancerIPs", "metallb.universe.tf/allow-shared-ip")


def change_vip(old, new, apply=False):
    """A reserved VIP given a new address, and - with it - every Service on it,
    so nothing attached is left behind on the old one. Only its annotations
    change: a Service keeps its name, ports and load-balancer class, and
    kube-vip (or MetalLB) moves it to the new address, with a moment's gap for
    open connections. Checked first as a whole: nothing moves if any Service
    could not, or the new address is not free. Returns what moves."""
    old, new = STATE._ipv4(old, "current address"), STATE._ipv4(new, "new address")
    if old == new:
        raise ValueError("the new address is the same as the current one")
    state = STATE._complete(INV.inventory(), "nothing was moved")
    rows = registered()
    entry = next((row for row in rows if row["ip"] == old), None)
    if entry is None:
        raise ValueError(f"{old} is not one of Your VIPs")
    if new in state["node_ips"]:
        raise ValueError(f"{new} is a node's own address")
    if any(row["ip"] == new for row in rows):
        raise ValueError(f"{new} is already one of Your VIPs; move the Services with Edit on each instead")
    INV.check_address(new, state)
    if any(new in (row.get("external_ips") or []) for row in state["services"]):
        raise ValueError(f"{new} is already used by a Service")
    moving = [row for row in state["services"]
              if old in (row.get("requested_ips") or []) + (row.get("external_ips") or [])]
    system = [f"{row['namespace']}/{row['name']}" for row in moving if row.get("system")]
    if system:
        raise ValueError(f"{', '.join(system)} on {old} belong to the cluster; Homestead does not move them")
    plan = {"old": old, "new": new, "default": bool(entry.get("default")), "label": entry.get("label", ""),
            "services": [{"namespace": row["namespace"], "name": row["name"],
                          "ports": [f"{p['port']}/{p['protocol']}" for p in row.get("ports") or []],
                          "targets": row.get("targets") or []} for row in moving]}
    if not apply:
        return plan
    annotations = PLATFORM.vip_annotations(new)
    for row in moving:
        path = f"/api/v1/namespaces/{row['namespace']}/services/{row['name']}"
        service = STATE.kget(path)
        meta = service.setdefault("metadata", {})
        current = meta.setdefault("annotations", {})
        for key in VIP_KEYS:
            current.pop(key, None)
        current.update(annotations)
        if (service.get("spec") or {}).get("loadBalancerIP") == old:
            service["spec"]["loadBalancerIP"] = new
        STATE.ksend("PUT", path, service)
    def move(rows):
        # The Services have moved: the registry follows, even if the entry
        # changed meanwhile (kept, with its new label or default).
        now = next((row for row in rows if row["ip"] == old), None) or dict(entry)
        there = next((row for row in rows if row["ip"] == new), None)
        rows = [row for row in rows if row["ip"] not in (old, new)]
        if there is not None:
            # Added meanwhile, say in another tab: one entry for the address,
            # keeping a label either one had.
            now = {**there, **{k: v for k, v in now.items() if v not in ("", None)}}
        now.update(ip=new, previous=old)
        if now.get("default"):
            for row in rows:
                row.pop("default", None)
        return rows + [now]
    _update_registered(move)
    plan["detail"] = (f"{old} is now {new}" + (f"; {len(moving)} Service{'s' if len(moving) != 1 else ''} moved with it"
                                               if moving else "") + ("; still the default workload VIP" if entry.get("default") else ""))
    return plan


def set_vip_label(ip, label):
    ip = STATE._ipv4(ip, "address")
    def relabel(rows):
        for row in rows:
            if row["ip"] == ip:
                row["label"] = " ".join(str(label or "").split())[:60]
        return rows
    _update_registered(relabel)
    return {"ok": True}
