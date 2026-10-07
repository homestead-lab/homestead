"""Each host's network ports: link, speed, errors, flaps and bonds. Read only.

The node probe reads them from the host's own /sys (probe.port_facts): carrier,
negotiated speed and duplex, MTU, MAC, driver, the error counters a port keeps
from boot, and a bond's and its members' state. Counters only mean something
as a change, so the leader keeps an hour of samples per host on the data
volume and the rates here are what changed over that hour. A counter that
goes down, or a host that booted since, starts a new hour.

What a port carries is what makes a fault matter: the host's own uplink (the
default route's interface), a LAN network that rides it, or a bond it is a
member of. A port with no cable that carries nothing is just a spare.

Conditions are raised as alerts (homestead_alerts) and listed on the host's
Network section and the Networking page. Nothing here changes a host.
"""
import json
import os
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
SAMPLE_EVERY = 300          # one stored sample per host every five minutes
WINDOW = 3600               # rates are over the last hour
MIN_WINDOW = 900            # and say nothing until a quarter of it is known
KEEP_SPEED = 30 * 86400     # a slower link is news for a month, then it is how it is
FORGET_HOST = 86400         # a host no probe has seen for a day is dropped
ERRORS_IN_HOUR = 100        # receive and transmit errors
FLAPS_IN_HOUR = 3           # carrier changes: each up or down is one
NO_PARTNER_FOR = 120        # an 802.3ad bond the switch has not answered for this long
ZERO_MAC = "00:00:00:00:00:00"
_lock = SHARED.SharedLock("host-ports")


def bind(data_dir="/data"):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, "host-ports.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    SHARED.write_json(_path(), state, indent=None, sort_keys=True)


def _ports(probe):
    """The probe's interfaces, if it is new enough to read ports; else None."""
    rows = (probe or {}).get("interfaces") or []
    return rows if any("counters" in row for row in rows) else None


def _counters(rows):
    return {row["name"]: {key: value for key, value in (row.get("counters") or {}).items() if isinstance(value, int)}
            for row in rows}


def observe(probes, now=None):
    """Keep this probe round's counters and best speeds. The leader calls it."""
    now = now or time.time()
    with _lock:
        state = _load()
        before = json.dumps(state, sort_keys=True)
        for node, probe in (probes or {}).items():
            rows = _ports(probe)
            if rows is None:
                continue
            host = state.setdefault(node, {})
            uptime = (probe or {}).get("uptime_s")
            boot = round(now - uptime) if isinstance(uptime, (int, float)) else None
            samples = host.get("samples") or []
            if boot and host.get("boot") and abs(boot - host["boot"]) > 120:
                samples = []                       # it restarted: counters began again
            if not samples or now - samples[-1]["t"] >= SAMPLE_EVERY:
                samples.append({"t": round(now), "c": _counters(rows)})
            host["samples"] = [s for s in samples if s["t"] >= now - WINDOW - SAMPLE_EVERY]
            if boot and (not host.get("boot") or abs(boot - host["boot"]) > 120):
                host["boot"] = boot                # uptime jitters a second; only a restart moves it
            if now - (host.get("seen") or 0) >= SAMPLE_EVERY:
                host["seen"] = round(now)          # only forgetting a host reads it, a day on
            best, no_partner = host.setdefault("best", {}), host.setdefault("no_partner", {})
            for row in rows:
                speed, name = row.get("speed_mbps"), row["name"]
                if row.get("carrier") and speed:
                    was = best.get(name)
                    if (not was or speed > was["speed"] or now - was["at"] > KEEP_SPEED
                            or speed == was["speed"] and now - was["at"] >= SAMPLE_EVERY):
                        best[name] = {"speed": speed, "at": round(now)}
                bond = row.get("bond") or {}
                if bond.get("mode") == "802.3ad" and (bond.get("ad_partner_mac") or ZERO_MAC) == ZERO_MAC:
                    no_partner.setdefault(name, round(now))
                else:
                    no_partner.pop(name, None)
            names = {row["name"] for row in rows}
            host["best"] = {k: v for k, v in best.items() if k in names}
            host["no_partner"] = {k: v for k, v in no_partner.items() if k in names}
        for node in [n for n, host in state.items() if now - (host.get("seen") or 0) > FORGET_HOST]:
            state.pop(node)
        if json.dumps(state, sort_keys=True) != before:
            _save(state)                           # a sample, a speed or a partner changed; else no write
    return state


def _rates(name, current, samples, now):
    """What each counter did over the hour: (counts, seconds) or (None, seconds)."""
    base = next((s for s in samples if name in s.get("c", {})), None)
    if not base:
        return None, 0
    seconds = now - base["t"]
    if seconds < MIN_WINDOW:
        return None, seconds
    then = base["c"][name]
    out = {}
    for key, value in current.items():
        if key in then:
            if value < then[key]:
                return None, 0               # it went down: the port was reset
            out[key] = value - then[key]
    return out, seconds


def _chain(name, by_name):
    """name and what it is in, outward: enp1s0, bond0, br0."""
    out, seen = [], set()
    while name and name not in seen and name in by_name:
        out.append(name)
        seen.add(name)
        name = by_name[name].get("master") or ""
    return out


def _carries(rows, uplink, networks):
    """What each interface carries, from the uplink and LAN networks down to
    the NICs under them: a bridge carries what rides it, a bond what its
    bridge carries, and a NIC what its bond does. A VLAN rides its parent."""
    by_name = {row["name"]: row for row in rows}
    direct = {}
    if uplink:
        direct.setdefault(uplink, []).append("host address")
    for iface, names in (networks or {}).items():
        parent = iface.split(".", 1)[0] if iface not in by_name or by_name[iface].get("kind") == "vlan" else iface
        for network in names:
            direct.setdefault(iface, []).append(network)
            if parent != iface:
                direct.setdefault(parent, []).append(f"{network} (VLAN {iface.split('.', 1)[1]})")
    out = {}
    for row in rows:
        found = []
        for link in _chain(row["name"], by_name):
            found += [what for what in direct.get(link, []) if what not in found]
        out[row["name"]] = found
    return out


def _link(row):
    if row.get("admin_up") is False or (row.get("carrier") is None and not row.get("up")):
        return "off"
    if row.get("carrier") is None:
        return "up" if row.get("up") else "off"
    return "up" if row["carrier"] else "down"


def _speed(mbps):
    if not mbps:
        return ""
    return f"{mbps / 1000:g} Gb/s" if mbps >= 1000 else f"{mbps} Mb/s"


def host(node, probe, saved=None, context=None, now=None):
    """One host's ports, with rates, what each carries, and its conditions."""
    now = now or time.time()
    rows = _ports(probe)
    if rows is None:
        return {"node": node, "available": False, "ports": [], "conditions": [],
                "reason": "The node probe on this host is older than port status; it updates with Homestead."
                if probe else "No node probe answers on this host."}
    saved, context = saved or {}, context or {}
    samples = saved.get("samples") or []
    uplink = context.get("uplink") or (probe or {}).get("default_interface") or ""
    carries = _carries(rows, uplink, context.get("networks"))
    by_name = {row["name"]: row for row in rows}
    ports, conditions = [], []

    def condition(iface, kind, severity, title, body, signals=None):
        conditions.append({"key": f"{node}:{iface}:{kind}", "node": node, "iface": iface, "kind": kind,
                           "severity": severity, "title": title, "body": body, "signals": signals or {}})

    for row in rows:
        if row.get("kind") not in ("nic", "bond", "bridge", "vlan"):
            continue
        name = row["name"]
        current = _counters([row]).get(name, {})
        counts, seconds = _rates(name, current, samples, now)
        errors = None if counts is None else counts.get("rx_errors", 0) + counts.get("tx_errors", 0)
        flaps = None if counts is None else counts.get("carrier_changes", 0)
        drops = None if counts is None else counts.get("rx_dropped", 0) + counts.get("tx_dropped", 0)
        best = (saved.get("best") or {}).get(name) or {}
        port = {"name": name, "kind": row.get("kind"), "link": _link(row), "speed_mbps": row.get("speed_mbps"),
                "duplex": row.get("duplex"), "mtu": row.get("mtu"), "mac": row.get("mac", ""),
                "driver": row.get("driver", ""), "master": row.get("master", ""),
                "carries": carries.get(name, []), "uplink": bool(uplink) and uplink in _chain(name, by_name),
                "errors": errors, "flaps": flaps, "drops": drops, "window_s": round(seconds),
                "crc": None if counts is None else counts.get("rx_crc_errors", 0),
                "was_mbps": best.get("speed") if best.get("speed", 0) > (row.get("speed_mbps") or 0) and row.get("carrier") else None,
                "bond": row.get("bond"), "bond_member": row.get("bond_member")}
        ports.append(port)

    by_port = {p["name"]: p for p in ports}
    # Bonds first: a member slower than its peers is said once, as that.
    for port in sorted(ports, key=lambda p: p["kind"] != "bond"):
        name, kind, link = port["name"], port["kind"], port["link"]
        if kind == "bond":
            bond = port["bond"] or {}
            members = [by_port[m] for m in bond.get("slaves") or [] if m in by_port]
            good = [m for m in members if m["link"] == "up" and (m.get("bond_member") or {}).get("mii_status", "up") == "up"]
            if members and len(good) < len(members):
                lost = ", ".join(m["name"] for m in members if m not in good)
                condition(name, "members", "critical" if not good else "degraded",
                          f"{name} on {node} has {'no working members' if not good else f'{len(good)} of {len(members)} members working'}",
                          f"{lost} {'has' if lost.count(',') == 0 else 'have'} no link. "
                          + ("Everything the bond carries is down." if not good else
                             "The bond carries on through the rest, with no spare if another fails."),
                          {"working": len(good)})
            since = (saved.get("no_partner") or {}).get(name)
            if bond.get("mode") == "802.3ad" and since and now - since >= NO_PARTNER_FOR and good:
                condition(name, "lacp", "degraded", f"The switch is not aggregating {name} on {node}",
                          "This 802.3ad bond has had no LACP partner for over two minutes: the switch ports are "
                          "not one LACP group, so the bond uses one member at a time. Group the ports on the switch, "
                          "or change the bond to active-backup.")
            if good and len(members) > 1:
                fastest = max((m["speed_mbps"] or 0) for m in good)
                for member in good:
                    if member["speed_mbps"] and member["speed_mbps"] < fastest:
                        condition(member["name"], "slower", "degraded",
                                  f"{member['name']} on {node} runs slower than the rest of {name}",
                                  f"It negotiated {_speed(member['speed_mbps'])}; its bond peers run at {_speed(fastest)}. "
                                  "Usually a cable or a switch port.", {"speed": member["speed_mbps"]})
            continue
        if kind != "nic":
            continue
        member = port["bond_member"]
        if link == "down" and port["carries"] and not member:
            condition(name, "down", "critical", f"{name} on {node} has no link",
                      f"It carries {', '.join(port['carries'][:3])}. Check its cable and the switch port.")
        if link == "up" and port["was_mbps"] and port["carries"] and not any(
                c["iface"] == name and c["kind"] == "slower" for c in conditions):
            condition(name, "slower", "degraded", f"{name} on {node} runs slower than it did",
                      f"It negotiated {_speed(port['speed_mbps'])}; it ran at {_speed(port['was_mbps'])} before. "
                      "Usually a cable or a switch port.", {"speed": port["speed_mbps"]})
        if port["errors"] is not None and port["errors"] >= ERRORS_IN_HOUR:
            condition(name, "errors", "degraded", f"{name} on {node} is seeing errors",
                      f"{port['errors']} errors in the last {round(port['window_s'] / 60)} minutes"
                      + (f", {port['crc']} of them CRC errors" if port.get("crc") else "")
                      + ". Usually a cable, a switch port or a failing NIC.",
                      {"band": min(10, port["errors"] // ERRORS_IN_HOUR)})
        if port["flaps"] is not None and port["flaps"] >= FLAPS_IN_HOUR:
            condition(name, "flaps", "degraded", f"{name} on {node} keeps losing its link",
                      f"Its link went up or down {port['flaps']} times in the last {round(port['window_s'] / 60)} minutes. "
                      "Usually a loose cable or a port negotiating badly.", {"band": min(10, port["flaps"] // FLAPS_IN_HOUR)})
    return {"node": node, "available": True, "uplink": uplink, "ports": ports, "conditions": conditions}


def report(probes, contexts=None, now=None):
    """Every host's ports, and the conditions across them."""
    now = now or time.time()
    saved, contexts = _load(), contexts or {}
    hosts = {node: host(node, probe, saved.get(node), contexts.get(node), now)
             for node, probe in sorted((probes or {}).items())}
    conditions = [c for h in hosts.values() for c in h["conditions"]]
    # The same LAN network on different MTUs drops large frames between hosts.
    mtus = {}
    for node, row in hosts.items():
        by_port = {port["name"]: port for port in row["ports"]}
        for iface, networks in ((contexts.get(node) or {}).get("networks") or {}).items():
            if (by_port.get(iface) or {}).get("mtu"):
                for network in networks:
                    mtus.setdefault(network, {})[node] = by_port[iface]["mtu"]
    for network, by_node in sorted(mtus.items()):
        if len(set(by_node.values())) > 1:
            conditions.append({"key": f"*:{network}:mtu", "node": "", "iface": network, "kind": "mtu", "severity": "info",
                               "title": f"{network} has different MTUs on different hosts",
                               "body": ", ".join(f"{n} {m}" for n, m in sorted(by_node.items()))
                               + ". Frames larger than the smallest are dropped between hosts.", "signals": {}})
    return {"hosts": hosts, "conditions": conditions,
            "counts": {"ports": sum(1 for h in hosts.values() for p in h["ports"] if p["kind"] == "nic"),
                       "hosts": len(hosts)}}


def alert_facts(rep):
    """Port conditions worth a notification: degraded and critical ones."""
    # A host the probe cannot read keeps what was raised on it until it can.
    facts = [{"unknown_prefix": f"ports:{node}:"} for node, row in ((rep or {}).get("hosts") or {}).items()
             if not row.get("available")]
    for c in (rep or {}).get("conditions") or []:
        if c["severity"] not in ("degraded", "critical") or not c.get("node"):
            continue
        facts.append({"key": f"ports:{c['key']}", "category": "outage" if c["severity"] == "critical" else "degraded",
                      "severity": c["severity"], "title": c["title"], "body": c["body"],
                      "resolved": f"{c['iface']} on {c['node']} is back to normal",
                      "href": f"/nodes?node={c['node']}&section=network", "signals": c.get("signals") or {}})
    return facts
