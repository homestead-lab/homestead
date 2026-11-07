"""Bonds on a k3s or RKE2 host: two or more NICs that carry its network as
one, so a cable, port or NIC can fail without the host going quiet.

On Harvester the network controller makes bonds (homestead_uplinks). On k3s
and RKE2 the host's own netplan does, so Homestead edits netplan - the way the
host bridge does (homestead_host_bridge), with the same care, one host at a
time:

1. look (read-only): the interface the default route leaves by and what it
   stands on - a plain NIC, a bridge over one NIC, or a bond already - every
   NIC with its link, and netplan's files. Only netplan rendering through
   systemd-networkd (Ubuntu Server's way) is changed;
2. review: what the change writes, and what is refused before anything is
   sent - a NIC in use elsewhere, one without link, the last member with link
   taken out, 802.3ad without the switch confirmed, another host mid-change,
   the other servers not all Ready;
3. change: netplan's files are copied aside and a rollback is armed first, as
   a systemd timer on the host (put the copies back, apply). The new files
   must pass `netplan generate`; they are applied, detached, a few seconds
   later;
4. check, from Homestead: the address on the interface that carries it, the
   default route by it, the gateway answering, every member in the bond with
   link, and for 802.3ad the switch answering LACP. Only then is the rollback
   disarmed. A check that fails puts the old network back at once; a host
   that never answers puts itself back when its timer runs out.

The bond takes the MAC address of the NIC that carried the host, and asks
DHCP as that MAC, so the router hands out the same address. When the
interface the host's address is on changes name (a NIC becomes bond0),
kube-vip's pod there restarts, and on a cluster of several hosts k3s or RKE2
restarts so flannel follows - as after a bridge conversion. Under a bridge,
or changing a bond, the name does not change and nothing restarts.
"""
import calendar
import contextlib
import datetime
import hashlib
import json
import re
import secrets
import time
import urllib.error

import homestead_hostrun as HOSTRUN
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_pod_exec as POD_EXEC
import homestead_routes as ROUTER

hostrun = kget = ksend = None
platform = lambda force=False: {}
BOND = "bond0"
ROLLBACK_SECONDS = 240
LACP_WAIT = 60
# How long the host has to be Ready again after k3s or RKE2 restarts on the
# new interface. A host the new network cut off from the cluster never is, so
# the job fails then, naming the host, instead of running (and holding every
# other host's network change back) for ever.
RESTART_SECONDS = 600
# How long a change holds its host (homestead_hostrun.hold) if its job never
# ends: past the rollback, the LACP wait and a k3s restart. The job lets go
# when it ends (#597).
HOLD_SECONDS = ROLLBACK_SECONDS + LACP_WAIT + RESTART_SECONDS
BACKUP_ROOT = "/var/lib/homestead"
KIND = "host-bond"
UNIT = "homestead-bond-rollback"
LACP_UNIT = "homestead-bond-lacp"
# Left by the host when it put its network back because the switch did not
# answer LACP: an 802.3ad bond with no partner can carry nothing, so Homestead
# may not reach the host to see it - the host checks for itself.
LACP_FAILED = "/run/homestead-bond-lacp-failed"
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,15}")
MODES = ("active-backup", "802.3ad", "balance-alb", "balance-tlb")

INSPECT_SCRIPT = r"""IF=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
command -v netplan >/dev/null && N=1 || N=0
systemctl is-active -q systemd-networkd && W=1 || W=0
systemctl is-active -q NetworkManager && M=1 || M=0
command -v systemd-run >/dev/null && R=1 || R=0
A=0; for u in homestead-bond-rollback homestead-bridge-rollback; do systemctl is-active -q $u.timer && A=1; done
S=""; for s in k3s k3s-agent rke2-server rke2-agent; do systemctl is-active -q $s && S="$S $s"; done
grep -qs '^flannel-iface:' /etc/rancher/k3s/config.yaml /etc/rancher/rke2/config.yaml && F=1 || F=0
GW=$(ip -4 route show default | awk '{print $3; exit}')
python3 - "$IF" "$N" "$W" "$M" "$R" "$A" "$S" "$F" "$GW" <<'PY'
import glob, json, os, subprocess, sys
iface, netplan, networkd, nm, run, armed, services, flannel, gw = sys.argv[1:10]
def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None
nics = []
for name in sorted(os.listdir("/sys/class/net")):
    base = "/sys/class/net/" + name
    if not os.path.exists(base + "/device") or os.path.isdir(base + "/wireless"):
        continue
    master = os.path.basename(os.path.realpath(base + "/master")) if os.path.exists(base + "/master") else ""
    speed = read(base + "/speed")
    # A NIC nothing has turned on (a spare netplan does not name) cannot say
    # whether it has link: None, not False.
    flags = read(base + "/flags") or "0x0"
    up = int(flags, 16) & 1 if flags.startswith("0x") else 0
    carrier = read(base + "/carrier") if up else None
    nics.append({"name": name, "mac": (read(base + "/address") or "").lower(), "carrier": None if carrier is None else carrier == "1",
                 "speed": int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else None, "master": master})
kind = "bridge" if os.path.isdir(f"/sys/class/net/{iface}/bridge") else "bond" if os.path.isdir(f"/sys/class/net/{iface}/bonding") \
    else "nic" if os.path.exists(f"/sys/class/net/{iface}/device") else "other"
addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface], capture_output=True, text=True).stdout if iface else ""
address, dhcp = "", False
for line in addr.splitlines():
    parts = line.split()
    cidr = parts[3] if len(parts) > 3 else ""
    if cidr and not cidr.endswith("/32"):
        address, dhcp = address or cidr, dhcp or " dynamic " in line
files, pyyaml = {}, True
try:
    import yaml
except ImportError:
    pyyaml = False
if pyyaml:
    for path in sorted(glob.glob("/etc/netplan/*.yaml")):
        try:
            net = (yaml.safe_load(open(path)) or {}).get("network") or {}
        except Exception:
            files[path] = {"bad": True}
            continue
        files[path] = {"renderer": net.get("renderer") or "",
                       "ethernets": {k: {"match": (v or {}).get("match") or {}, "set-name": (v or {}).get("set-name") or "",
                                         "addressed": any(x in (v or {}) for x in ("addresses", "dhcp4", "dhcp6")) and
                                         bool((v or {}).get("addresses") or (v or {}).get("dhcp4") or (v or {}).get("dhcp6"))}
                                     for k, v in (net.get("ethernets") or {}).items()},
                       "bonds": {k: {"interfaces": (v or {}).get("interfaces") or [], "parameters": (v or {}).get("parameters") or {}}
                                 for k, v in (net.get("bonds") or {}).items()},
                       "bridges": {k: {"interfaces": (v or {}).get("interfaces") or []} for k, v in (net.get("bridges") or {}).items()},
                       "cloud_init": "cloud-init" in open(path).read()}
bond = {}
if kind == "bond":
    b = f"/sys/class/net/{iface}/bonding"
    bond = {"mode": (read(b + "/mode") or "").split(" ")[0], "slaves": (read(b + "/slaves") or "").split(),
            "active": read(b + "/active_slave") or ""}
under = []
if kind == "bridge":
    under = sorted(os.listdir(f"/sys/class/net/{iface}/brif")) if os.path.isdir(f"/sys/class/net/{iface}/brif") else []
print("FACTS " + json.dumps({"iface": iface, "kind": kind, "address": address, "dhcp": dhcp, "gateway": gw,
                             "netplan": netplan == "1", "networkd": networkd == "1", "nm": nm == "1", "systemd_run": run == "1",
                             "pyyaml": pyyaml, "armed": armed == "1", "services": services.split(), "flannel_iface": flannel == "1",
                             "nics": nics, "files": files, "bond": bond, "bridge_ports": under,
                             "bond_exists": os.path.exists("/sys/class/net/__BOND__"),
                             # Left behind, empty, when netplan stopped naming it.
                             "bond_empty": os.path.exists("/sys/class/net/__BOND__") and
                                           not (read("/sys/class/net/__BOND__/bonding/slaves") or "").strip()}))
PY
echo END""".replace("__BOND__", BOND)


def bind(_hostrun, _kget, _ksend, _platform=None):
    """The host runner and cluster; for the routes, the platform: Harvester
    makes its bonds itself."""
    global hostrun, kget, ksend, platform
    hostrun, kget, ksend = _hostrun, _kget, _ksend
    if _platform is not None:
        platform = _platform


def parse(out):
    line = next((l for l in out.splitlines() if l.startswith("FACTS ")), "")
    try:
        facts = json.loads(line[6:]) if line else {}
    except ValueError:
        facts = {}
    facts["complete"] = bool(facts) and "END" in out.split()
    return facts


def _where(facts, iface):
    """The netplan file and section that define an interface, if any."""
    for path, f in (facts.get("files") or {}).items():
        for section in ("bonds", "bridges"):
            if iface in (f.get(section) or {}):
                return path, section
        for key, eth in (f.get("ethernets") or {}).items():
            if iface in (key, eth.get("set-name"), (eth.get("match") or {}).get("name")):
                return path, "ethernets"
    return "", ""


def _eth_key(facts, path, nic):
    for key, eth in ((facts.get("files") or {}).get(path, {}).get("ethernets") or {}).items():
        if nic in (key, eth.get("set-name"), (eth.get("match") or {}).get("name")):
            return key
    return ""


def describe(facts):
    """What carries the host now: shape (nic | bridge | bridge-bond | bond),
    the bond's name and members if there is one, and the netplan file."""
    iface, kind = facts.get("iface", ""), facts.get("kind", "")
    nics = {n["name"]: n for n in facts.get("nics") or []}
    out = {"iface": iface, "shape": "", "bond": "", "members": [], "mode": "", "carrier_nic": "", "bridge": "", "file": ""}
    if kind == "nic":
        out.update(shape="nic", carrier_nic=iface, file=_where(facts, iface)[0])
    elif kind == "bridge":
        ports = facts.get("bridge_ports") or []
        bond = next((p for p in ports if p not in nics), "")
        out.update(bridge=iface, file=_where(facts, iface)[0])
        if bond:
            path, section = _where(facts, bond)
            spec = ((facts.get("files") or {}).get(path, {}).get("bonds") or {}).get(bond) or {}
            out.update(shape="bridge-bond", bond=bond, members=list(spec.get("interfaces") or []),
                       mode=str((spec.get("parameters") or {}).get("mode") or "active-backup"))
        elif len(ports) == 1:
            out.update(shape="bridge", carrier_nic=ports[0])
        else:
            out.update(shape="other")
    elif kind == "bond":
        path, _ = _where(facts, iface)
        spec = ((facts.get("files") or {}).get(path, {}).get("bonds") or {}).get(iface) or {}
        out.update(shape="bond", bond=iface, file=path, members=list(spec.get("interfaces") or (facts.get("bond") or {}).get("slaves") or []),
                   mode=str((spec.get("parameters") or {}).get("mode") or (facts.get("bond") or {}).get("mode") or "active-backup"))
    else:
        out["shape"] = "other"
    return out


def _host_problem(facts, shape):
    if not facts.get("complete"):
        return "the host did not finish describing its network"
    if not facts.get("iface"):
        return "the host has no IPv4 default route to follow"
    if not facts.get("netplan") or not facts.get("networkd") or facts.get("nm") or any(
            f.get("renderer") not in ("", "networkd") for f in (facts.get("files") or {}).values()):
        return "Homestead changes hosts whose network netplan sets up through systemd-networkd (Ubuntu Server's way); this one differs"
    if not facts.get("pyyaml") or not facts.get("systemd_run"):
        return "the host lacks python3-yaml or systemd-run, which the change needs"
    if any(f.get("bad") for f in (facts.get("files") or {}).values()):
        return "a file in /etc/netplan could not be read as YAML"
    if facts.get("armed"):
        return "a network change on this host is still waiting to be checked"
    if shape["shape"] == "other":
        return f"{facts['iface']} is not a NIC, a bridge over one NIC, or a bond; Homestead does not change it"
    if not shape["file"]:
        return f"{facts['iface']} is not set up in /etc/netplan"
    if not facts.get("address") or not facts.get("gateway"):
        return f"{facts['iface']} has no IPv4 address or gateway to keep"
    return ""


def _params(mode, primary):
    params = {"mode": mode, "mii-monitor-interval": 100}
    if mode == "active-backup" and primary:
        params["primary"] = primary
    if mode == "802.3ad":
        params.update({"lacp-rate": "fast", "transmit-hash-policy": "layer3+4"})
    return params


def plan(node, req, facts, busy=None, servers_ready=None):
    """What a change writes, and what stands in its way. Writes nothing.

    req: {action: create | change | remove, members, mode, primary, allow_down, lacp_confirmed}
    busy: another host's network change in progress (its title), or "".
    servers_ready: (ready, total) of the cluster's other servers, or None.
    """
    shape = describe(facts)
    action = str(req.get("action") or ("change" if shape["bond"] else "create"))
    refusals, warnings = [], []
    reason = _host_problem(facts, shape)
    if reason:
        refusals.append(reason)
    refusals += guard_refusals(node, busy, servers_ready)
    nics = {n["name"]: n for n in facts.get("nics") or []}
    bond = shape["bond"] or BOND
    current = list(shape["members"])
    if action == "create":
        if shape["bond"]:
            refusals.append(f"{node} is on {shape['bond']} already: change it instead")
        elif shape["shape"] not in ("nic", "bridge"):
            pass
        elif facts.get("bond_exists") and not facts.get("bond_empty"):
            refusals.append(f"{node} has a {BOND} already, not carrying its address; Homestead does not reuse it")
    elif action in ("change", "remove") and not shape["bond"]:
        refusals.append(f"{node} has no bond to {action}")
    members = [str(m) for m in dict.fromkeys(req.get("members") or ([] if action != "remove" else current))]
    if action == "create" and shape["carrier_nic"] and shape["carrier_nic"] not in members:
        members.insert(0, shape["carrier_nic"])
    mode = str(req.get("mode") or shape["mode"] or "active-backup")
    if mode not in MODES:
        refusals.append(f"{mode} is not a bond mode Homestead sets up; choose active-backup or 802.3ad")
    keep = ""
    if action == "remove":
        up = [m for m in current if (nics.get(m) or {}).get("carrier")]
        keep = str(req.get("keep") or (facts.get("bond") or {}).get("active") or (up[0] if up else ""))
        if keep not in current:
            refusals.append("choose the NIC that keeps the host's network")
        elif not (nics.get(keep) or {}).get("carrier"):
            refusals.append(f"{keep} has no link: keep a NIC that does")
        members = [keep] if keep else []
    else:
        if len(members) < 2:
            refusals.append("a bond needs two or more NICs")
        for m in members:
            nic = nics.get(m)
            if not nic:
                refusals.append(f"{node} has no NIC {m}")
                continue
            if nic["master"] and nic["master"] not in (bond, shape["bridge"]) and m != shape["carrier_nic"]:
                refusals.append(f"{m} is in {nic['master']} already")
            path, section = _where(facts, m)
            if path and section == "ethernets" and m not in current and m != shape["carrier_nic"]:
                key = _eth_key(facts, path, m)
                if ((facts.get("files") or {}).get(path, {}).get("ethernets") or {}).get(key, {}).get("addressed"):
                    refusals.append(f"{m} has an address of its own in {path}: it carries something else")
            if nic["carrier"] is None and m not in current:
                warnings.append(f"{m} is switched off, so its link cannot be seen yet: the bond turns it on, "
                                "and the checks afterwards say whether it has link")
            elif not nic["carrier"] and m not in current:
                if req.get("allow_down"):
                    warnings.append(f"{m} has no link; the bond starts without it")
                else:
                    refusals.append(f"{m} has no link: plug it in, or confirm you want it in the bond anyway")
        if not any((nics.get(m) or {}).get("carrier") for m in members):
            refusals.append("none of these NICs has link: the host would go quiet")
        dropped = [m for m in current if m not in members]
        active = (facts.get("bond") or {}).get("active", "")
        if active and active in dropped:
            refusals.append(f"{active} is the member carrying traffic now; take it out on its own after another becomes active")
        if mode == "802.3ad" and not req.get("lacp_confirmed"):
            refusals.append("802.3ad needs the switch ports to be one LACP group: confirm they are, or choose active-backup, "
                            "which works on any switch")
        speeds = {nics[m]["speed"] for m in members if m in nics and nics[m]["carrier"] and nics[m]["speed"]}
        if len(speeds) > 1:
            warnings.append(f"the NICs run at different speeds ({', '.join(f'{s} Mb/s' for s in sorted(speeds))}); "
                            "the bond is as fast as the one carrying traffic")
    primary = str(req.get("primary") or (shape["carrier_nic"] if action == "create" else "") or
                  (members[0] if members else ""))
    if mode == "active-backup" and primary and primary not in members:
        refusals.append(f"{primary} is not one of the members")
    renamed = action == "create" and shape["shape"] == "nic" or action == "remove" and shape["shape"] == "bond"
    if renamed and facts.get("flannel_iface"):
        refusals.append(f"k3s or RKE2 is set to a flannel interface by name; the host's interface becomes "
                        f"{bond if action == 'create' else keep}, so change flannel-iface by hand first")
    if renamed and len(facts.get("services") or []):
        warnings.append(f"the host's address moves to {bond if action == 'create' else keep}: kube-vip restarts there, "
                        f"and on a cluster of several hosts {facts['services'][0]} restarts so flannel follows. Containers keep running")
    if any(f.get("cloud_init") for f in (facts.get("files") or {}).values()):
        warnings.append("cloud-init wrote this host's netplan; it is told not to write it again at boot")
    carrier_mac = (nics.get(shape["carrier_nic"] or primary) or {}).get("mac", "")
    spec = {"action": action, "shape": shape["shape"], "file": shape["file"], "bond": bond, "members": members,
            "params": _params(mode, primary if mode == "active-backup" else ""), "mac": carrier_mac,
            "carrier_key": _eth_key(facts, shape["file"], shape["carrier_nic"]) if shape["carrier_nic"] else "",
            "bridge": shape["bridge"], "keep": keep, "dhcp": bool(facts.get("dhcp")),
            # networkd cannot change a live bond's mode: the bond is deleted
            # just before netplan makes it again, in the same detached step.
            # Going back to one NIC deletes it too: netplan leaves a bond it no
            # longer names in place, empty, and the next bond would be refused.
            "recreate": action == "remove" or action == "change" and mode != (shape["mode"] or mode)
                        or action == "create" and bool(facts.get("bond_empty"))}
    after = (shape["bridge"] or (keep if action == "remove" else bond))
    out = {"node": node, "action": action, "shape": shape, "bond": bond, "members": members, "mode": mode, "primary": primary,
           "keep": keep, "address": facts.get("address", ""), "carries_on": after, "renamed": renamed,
           "nics": facts.get("nics") or [], "refusals": list(dict.fromkeys(refusals)), "warnings": list(dict.fromkeys(warnings)),
           "spec": spec, "rollback_seconds": ROLLBACK_SECONDS}
    out["digest"] = hashlib.sha256(json.dumps({k: out[k] for k in ("node", "action", "members", "mode", "primary", "keep", "spec")},
                                              sort_keys=True).encode()).hexdigest()[:16]
    return out


EDIT = r'''
import json, os, sys, yaml
spec = json.loads(sys.argv[1])
MOVE = ("addresses", "dhcp4", "dhcp6", "dhcp4-overrides", "dhcp6-overrides", "gateway4", "gateway6", "routes",
        "routing-policy", "nameservers", "accept-ra", "ipv6-privacy", "link-local", "dhcp-identifier", "critical",
        "ipv6-address-generation", "ipv6-address-token", "ipv6-mtu", "macaddress")
path = spec["file"]
data = yaml.safe_load(open(path)) or {}
net = data["network"]
eths, bonds, bridges = net.setdefault("ethernets", {}), net.setdefault("bonds", {}), net.get("bridges") or {}
bond, members, action = spec["bond"], spec["members"], spec["action"]
def quiet(name):
    entry = eths.setdefault(name, {})
    for key in ("dhcp4", "dhcp6"):
        entry[key] = False
if action == "create":
    cfg = {"interfaces": members, "parameters": spec["params"]}
    if spec["mac"]:
        cfg["macaddress"] = spec["mac"]
    # The NIC's addressing moves to the bond before the members are quietened,
    # or the bond would take DHCP off with it.
    moved = {}
    if spec["shape"] == "nic":
        eth = eths[spec["carrier_key"]]
        moved = {k: eth.pop(k) for k in list(eth) if k in MOVE and k != "macaddress"}
    for m in members:
        quiet(m)
    if spec["shape"] == "nic":
        quiet(spec["carrier_key"])
        cfg.update(moved)
        if cfg.get("dhcp4") or cfg.get("dhcp6"):
            cfg["dhcp-identifier"] = "mac"
    else:
        cfg.update({"dhcp4": False, "dhcp6": False})
        br = bridges[spec["bridge"]]
        br["interfaces"] = [bond if i == spec["carrier_key"] or i in members else i for i in br.get("interfaces") or []]
        br["interfaces"] = list(dict.fromkeys(br["interfaces"]))
    bonds[bond] = cfg
elif action == "change":
    for m in members:
        quiet(m)
    bonds[bond]["interfaces"] = members
    bonds[bond]["parameters"] = spec["params"]
elif action == "remove":
    keep = spec["keep"]
    cfg = bonds.pop(bond)
    quiet(keep)
    if spec["shape"] == "bond":
        moved = {k: v for k, v in cfg.items() if k in MOVE and k != "macaddress"}
        eths[keep].update(moved)
    else:
        br = bridges[spec["bridge"]]
        br["interfaces"] = [keep if i == bond else i for i in br.get("interfaces") or []]
if not bonds:
    net.pop("bonds", None)
with open(path + ".homestead", "w") as out:
    yaml.safe_dump(data, out, default_flow_style=False, sort_keys=False)
os.chmod(path + ".homestead", 0o600)
os.replace(path + ".homestead", path)
'''


def change_script(spec, stamp):
    backup = f"{BACKUP_ROOT}/netplan-{stamp}"
    # 802.3ad: the host itself checks for a partner a minute after the change
    # and, with none, puts its old network back at once and says why.
    lacp_watch = "" if (spec.get("params") or {}).get("mode") != "802.3ad" else (
        f"systemd-run --unit={LACP_UNIT} --on-active={3 + LACP_WAIT} /bin/sh -c '"
        f"P=$(cat /sys/class/net/{spec['bond']}/bonding/ad_partner_mac 2>/dev/null); "
        f"if [ -z \"$P\" ] || [ \"$P\" = 00:00:00:00:00:00 ]; then touch {LACP_FAILED}; systemctl start {UNIT}.service; fi' >/dev/null\n")
    arg = json.dumps(spec).replace("'", "'\"'\"'")
    return f"""set -e
mkdir -p {backup}
cp -a /etc/netplan/. {backup}/
python3 - '{arg}' <<'PY'
{EDIT}
PY
if grep -qs cloud-init /etc/netplan/*.yaml {backup}/*.yaml; then
  mkdir -p /etc/cloud/cloud.cfg.d
  echo "network: {{config: disabled}}" > /etc/cloud/cloud.cfg.d/99-homestead-bond.cfg
fi
if ! netplan generate 2>/tmp/homestead-netplan.err; then
  rm -f /etc/netplan/*.yaml; cp -a {backup}/. /etc/netplan/; rm -f /etc/cloud/cloud.cfg.d/99-homestead-bond.cfg
  echo "ERR netplan did not accept the new configuration ($(head -c 200 /tmp/homestead-netplan.err)); the old one is back"
  exit 1
fi
rm -f {LACP_FAILED}
systemd-run --unit={UNIT} --on-active={ROLLBACK_SECONDS} /bin/sh -c 'systemctl stop {UNIT}.timer {LACP_UNIT}.timer 2>/dev/null; rm -f /etc/netplan/*.yaml; cp -a {backup}/. /etc/netplan/; rm -f /etc/cloud/cloud.cfg.d/99-homestead-bond.cfg; ip link delete {spec["bond"]} 2>/dev/null; netplan apply' >/dev/null
systemd-run --unit=homestead-bond-apply --on-active=3 /bin/sh -c '{f"ip link delete {spec['bond']} 2>/dev/null; " if spec.get("recreate") else ""}netplan apply' >/dev/null
{lacp_watch}echo "OK {backup}"
"""


def check_script(carries_on, address, bond, members, mode):
    ip = address.split("/")[0]
    member_checks = "\n".join(
        f'[ "$(basename "$(readlink /sys/class/net/{m}/master 2>/dev/null)")" = "{bond}" ] && echo "IN {m}"\n'
        f'[ "$(cat /sys/class/net/{m}/bonding_slave/mii_status 2>/dev/null)" = "up" ] && echo "UP {m}"' for m in members)
    lacp = (f'P=$(cat /sys/class/net/{bond}/bonding/ad_partner_mac 2>/dev/null); '
            f'[ -n "$P" ] && [ "$P" != "00:00:00:00:00:00" ] && echo "PARTNER"') if mode == "802.3ad" else ""
    return f"""T={carries_on}
ip -4 -o addr show dev $T 2>/dev/null | grep -q " {ip}/" && echo "ADDR"
ip -4 route show default | grep -q "dev $T" && echo "ROUTE"
GW=$(ip -4 route show default | awk '{{print $3; exit}}')
[ -n "$GW" ] && ping -c 1 -W 2 "$GW" >/dev/null 2>&1 && echo "GATEWAY"
{member_checks}
{lacp}
systemctl is-active -q {UNIT}.timer && echo "ARMED"
[ -f {LACP_FAILED} ] && echo "LACPFAILED"
echo END"""


CONFIRM_SCRIPT = f"systemctl stop {UNIT}.timer && {{ systemctl stop {LACP_UNIT}.timer 2>/dev/null; true; }} && echo CONFIRMED"
ROLLBACK_NOW = f"systemctl start {UNIT}.service >/dev/null 2>&1 & echo STARTED"


def inspect(node):
    out, err = hostrun.run(node, INSPECT_SCRIPT, timeout=60)
    facts = parse(out)
    if not facts.get("complete"):
        facts["error"] = (err or out or "the host did not answer")[-300:]
    return facts


def summary(node, facts):
    """What the dialog shows before anything is chosen."""
    shape = describe(facts)
    return {"node": node, "shape": shape, "address": facts.get("address", ""), "dhcp": facts.get("dhcp", False),
            "nics": facts.get("nics") or [], "problem": _host_problem(facts, shape) or facts.get("error", ""),
            "modes": list(MODES), "rollback_seconds": ROLLBACK_SECONDS}


def start(node, req, ops, busy=None, servers_ready=None):
    """Look again, review again, change, and hand the rest to a job. Admin only."""
    facts = inspect(node)
    p = plan(node, req, facts, busy, servers_ready)
    if p["refusals"]:
        raise ValueError(f"{node}: {p['refusals'][0]}")
    if req.get("digest") and req["digest"] != p["digest"]:
        raise ValueError(f"{node}'s network changed since this was reviewed: review it again")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    what = {"create": f"Bond {' + '.join(p['members'])} on {node}", "change": f"Change {p['bond']} on {node}",
            "remove": f"Remove {p['bond']} on {node}, back to {p['keep']}"}[p["action"]]
    # The host is held until the job ends, not just for this run: netplan
    # applies after it, and the network may yet roll back.
    lease = hostrun.hold(node, what, seconds=HOLD_SECONDS)
    try:
        out, err = hostrun.run(node, change_script(p["spec"], stamp), timeout=120, change=what, holder=lease)
    except POD_EXEC.ExecTimeout as error:
        ref = _unconfirmed_ref(node, what, lease, error)
        return {**ops.start(KIND, what, {"kind": "Node", "name": node}, f"/nodes?node={node}&section=network", ref,
                            _unconfirmed(ref)), "unconfirmed": True}
    except Exception:
        hostrun.let_go(node, lease)
        raise
    done = next((line for line in out.splitlines() if line.startswith("OK ")), "")
    if not done:
        hostrun.let_go(node, lease)
        reason = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
        raise ValueError(f"{node} was not changed: {reason}")
    ref = {"node": node, "action": p["action"], "bond": p["bond"], "members": p["members"], "mode": p["mode"],
           "carries_on": p["carries_on"], "address": p["address"], "renamed": p["renamed"], "backup": done[3:],
           "services": facts.get("services") or [], "stage": "applied", "since": time.time(),
           "change": what, "lease": lease}
    return ops.start(KIND, what, {"kind": "Node", "name": node}, f"/nodes?node={node}&section=network", ref,
                     f"Applied; checking {node} - it puts its old network back in {ROLLBACK_SECONDS // 60} minutes unless confirmed")


# A change script still running at its limit (homestead_pod_exec.ExecTimeout)
# may have changed the network part-way: its job keeps the host held and
# needs attention until a person cancels it, renewing the hold this often.
RENEW_UNCONFIRMED = 60


def _unconfirmed(ref):
    node = ref["node"]
    held = (f"{node} stays held, so no other change starts there" if not ref.get("renew_error") else
            f"Homestead could not renew its hold on {node} ({ref['renew_error']}), so another change may start "
            f"there once the hold runs out")
    return (f"{ref['change']} needs attention: the script on {node} did not finish within {ref['timeout']:g} seconds, "
            f"so Homestead cannot tell whether it changed {node}'s network. {held}. Check its network (if the change got as far as arming its rollback, {node} puts its "
            f"old network back by itself within {ROLLBACK_SECONDS // 60} minutes), then cancel this job to let go of {node}")


def _unconfirmed_ref(node, what, lease, error):
    return {"node": node, "stage": "unconfirmed", "since": time.time(), "renewed": time.time(),
            "change": what, "lease": lease, "timeout": error.timeout or 0}


def _still_unconfirmed(ref, now):
    if now - ref.get("renewed", 0) >= RENEW_UNCONFIRMED:
        try:
            hostrun.hold(ref["node"], ref["change"], seconds=HOLD_SECONDS, holder=ref.get("lease", ""))
            ref["renewed"] = now
            ref.pop("renew_error", None)
        except Exception as error:
            ref["renew_error"] = str(error)[:160] or type(error).__name__    # asked again next round
    return "running", 20, _unconfirmed(ref)


def status(item, now=None):
    """The job's next step: (status, progress, message). The host is let go
    of when the job ends."""
    try:
        result = _step(item, now)
    except urllib.error.HTTPError as error:
        if error.code == 404:    # the job fails on it (homestead_operations)
            _let_go(item["ref"])
        raise
    if result[0] in ("succeeded", "failed"):
        _let_go(item["ref"])
    return result


def _let_go(ref):
    if ref.get("lease"):
        hostrun.let_go(ref["node"], ref.pop("lease"))


def _held(ref):
    """What a follow-up change runs with: inside the job's hold."""
    return {"change": ref.get("change") or f"Network change on {ref['node']}", "holder": ref.get("lease", "")}


def _not_back(ref, interface):
    """Why a change failed whose host was not Ready again in time after k3s or
    RKE2 restarted on its new interface, and what to check on that host."""
    node = ref["node"]
    service = next((s for s in ref.get("services") or [] if s.startswith(("k3s", "rke2"))), "k3s")
    return (f"{node} was not Ready again {RESTART_SECONDS // 60} minutes after {service} restarted on {interface}. "
            f"On {node}, check `journalctl -u {service}` and that {interface} carries {ref['address']}. "
            "If it becomes Ready later, nothing more is needed: the new network is kept. "
            f"The old configuration is in {ref['backup']}")


# Proof the kubelet came back after k3s or RKE2 restarted. Ready alone stays
# True about 40 s after the kubelet's last heartbeat, so a host the new network
# cut off still reads Ready at first. When the restart is asked for, the step
# keeps what the kubelet last reported (restart_baseline); it then needs a
# Lease renewal more than BACK_AFTER seconds after that renewal, or a Ready
# heartbeat or transition after that heartbeat. Both sides come from the host's
# own clock, so a host clock ahead of or behind Homestead's changes nothing.
# systemd-run restarts the service 2 s on, and the old kubelet may renew once
# more (every 10 s) while it stops: that renewal is inside BACK_AFTER.
BACK_AFTER = 15
NODE_LEASE = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/{}"


def kube_time(text):
    """Seconds since the epoch of a Kubernetes Time or MicroTime
    (2026-01-01T12:00:00Z, 2026-01-01T12:00:00.123456Z); None if none."""
    text = str(text or "")
    try:
        whole = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    fraction = re.match(r"\.(\d+)", text[19:])
    return whole + (float("0." + fraction[1]) if fraction else 0)


def _ready(read, node):
    conditions = (read(f"/api/v1/nodes/{node}").get("status") or {}).get("conditions") or []
    return next((c for c in conditions if c.get("type") == "Ready"), {})


def _heard(ready):
    """Ready's heartbeat and transition times that are there."""
    return [t for t in (kube_time(ready.get("lastHeartbeatTime")), kube_time(ready.get("lastTransitionTime")))
            if t is not None]


def _renewed(read, node):
    """When the host's kubelet last renewed its Lease, or None. Any failure
    to read it (not found, forbidden, unreachable) gives None: the step then
    goes by Ready's times, never by less."""
    try:
        return kube_time((read(NODE_LEASE.format(node)).get("spec") or {}).get("renewTime"))
    except Exception:
        return None


def restart_baseline(read, node):
    """What the host's kubelet last reported, read right after the restart
    was asked for: renew0, its Lease's renewTime, and hb0, the later of
    Ready's heartbeat and transition. None for what could not be read."""
    try:
        heard = _heard(_ready(read, node))
    except Exception:
        heard = []
    return {"renew0": _renewed(read, node), "hb0": max(heard) if heard else None}


def kubelet_back(read, node, ref):
    """Whether node is Ready and its kubelet was heard from after the restart
    the step asked for (ref: its renew0, hb0 and since). read is the caller's
    bound kget; homestead_host_bridge uses this too."""
    ready = _ready(read, node)
    if ready.get("status") != "True":
        return False
    heard, renewed = _heard(ready), _renewed(read, node)
    renew0, hb0 = ref.get("renew0"), ref.get("hb0")
    if renew0 is not None and renewed is not None:
        return renewed > renew0 + BACK_AFTER
    if hb0 is not None:
        return any(t > hb0 for t in heard)
    # No baseline: nothing was readable when the restart was asked for, or the
    # job began before Homestead kept one. Only then compare with when the step
    # started, on Homestead's clock - a host clock far ahead can mislead this.
    after = ref["since"] + BACK_AFTER
    return any(t > after for t in heard) or (renewed is not None and renewed > after)


# kube-vip's pod and the host count, after the rollback is disarmed. Neither is
# taken on trust: until each is proved the job keeps trying, for as long as it
# waits for a restart, and then fails saying what was not done. ref["kube_vip"]
# remembers it: {"pods": [the pods found first], "error": ...} until done, and
# {"done": True} once it is. A job saved restarting by a version without it
# has none, and is not sent back to kube-vip.
KUBE_VIP_PODS = "/api/v1/namespaces/kube-system/pods"
SERVER_LABELS = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")


def _said(error):
    return str(error)[:160] or type(error).__name__


def _pod_key(pod):
    return pod["metadata"].get("uid") or pod["metadata"]["name"]


def restart_kube_vip(read, send, node, ref):
    """Delete the kube-vip pods the job first found on node; True once none is
    left to delete. A reply can be lost after the delete landed, so a retry goes
    by what the cluster shows: a pod gone or going counts, and a replacement,
    even under the same name, is left alone."""
    state = ref.get("kube_vip") or {}
    if state.get("done"):
        return True
    try:
        found = read(f"{KUBE_VIP_PODS}?fieldSelector=spec.nodeName%3D{node}").get("items", [])
        pods = [pod for pod in found if pod["metadata"]["name"].startswith("kube-vip")]
        if "pods" not in state:
            state = {"pods": [_pod_key(pod) for pod in pods]}
            ref["kube_vip"] = state
        for pod in pods:
            if _pod_key(pod) in state["pods"] and not pod["metadata"].get("deletionTimestamp"):
                try:
                    send("DELETE", f"{KUBE_VIP_PODS}/{pod['metadata']['name']}")
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
    except Exception as error:
        ref["kube_vip"] = {**state, "error": _said(error)}
        return False
    ref["kube_vip"] = {"done": True}
    return True


def kube_vip_settled(read, send, node, ref):
    """For the wait after the restart: try again if kube-vip is still unproved."""
    return "kube_vip" not in ref or restart_kube_vip(read, send, node, ref)


def _servers(nodes, node):
    """The cluster's other control-plane servers: (their names, those not Ready)."""
    names, down = [], []
    for found in nodes:
        meta = found.get("metadata") or {}
        if meta.get("name") == node or not any(k in (meta.get("labels") or {}) for k in SERVER_LABELS):
            continue
        names.append(meta["name"])
        if not any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in (found.get("status") or {}).get("conditions") or []):
            down.append(meta["name"])
    return names, down


def before_restart(read, node):
    """What the restart of k3s or RKE2 on node goes by, read fresh each round:
    (hosts, why the list has no answer, the other servers not Ready). Restarting
    takes node quiet as the change did, so it waits for what network_guard does. The
    host itself is in the list, so an empty one is no answer."""
    try:
        nodes = read("/api/v1/nodes").get("items", [])
    except Exception as error:
        return None, _said(error), []
    return (len(nodes), "", _servers(nodes, node)[1]) if nodes else (None, "the list came back empty", [])


def _restart_undone(node, service, unread, down, refused):
    """Why the restart was not sent, and what a person does about it."""
    undone = []
    if unread:
        undone.append(f"Homestead could not read the cluster's hosts ({unread}), so it did not restart {service} on {node}, "
                      "and flannel may still carry pod traffic by the old interface. If the cluster has other hosts, run "
                      f"`systemctl restart {service}` on {node}.")
    if down:
        words = guard_refusals(node, "", (0, len(down)))[0]
        undone.append(f"Homestead did not restart {service} on {node}, and flannel may still carry pod traffic by the old interface. "
                      f"{words} ({', '.join(down)}), then run `systemctl restart {service}` on {node}.")
    if refused:
        undone.append(f"{node} did not schedule the restart of {service} ({refused}), so it did not restart, and flannel may still "
                      f"carry pod traffic by the old interface. Run `systemctl restart {service}` on {node}.")
    return undone


def _kube_vip_undone(ref, interface):
    state = ref.get("kube_vip") or {}
    if "kube_vip" not in ref or state.get("done"):
        return []
    return [f"Homestead could not restart kube-vip on {ref['node']} ({state.get('error', 'no answer')}), so it may still announce "
            f"its addresses on the old interface. Delete the kube-vip pod on {ref['node']}, and it starts again on {interface}."]


def not_finished(ref, progress, elapsed, interface, seconds, service="", unread="", down=(), refused=""):
    """The next step of a job with kube-vip or the restart still not done:
    (status, progress, message). It fails past seconds, saying what is undone."""
    node, vip_undone = ref["node"], _kube_vip_undone(ref, interface)
    if elapsed > seconds:
        return ("failed", 100, f"{node} carries {ref['address']} on {interface} and keeps it, but the change is not finished. "
                + " ".join(vip_undone + _restart_undone(node, service, unread, down, refused)))
    reach = (["the cluster's hosts"] if unread else []) + ([f"kube-vip on {node}"] if vip_undone else [])
    doing = []
    if reach:
        doing.append(f"still trying to reach {' and '.join(reach)} ({unread or ref['kube_vip'].get('error', 'no answer')})")
    if down:
        doing.append(f"waiting for {', '.join(down)} to be Ready before it restarts {service}")
    if refused:
        doing.append(f"still trying to schedule the restart of {service} ({refused})")
    return "running", progress, f"{node} is on its new network; {' and '.join(doing)}, for up to {max(0, int(seconds - elapsed))}s more"


def not_back_with_kube_vip(reason, ref, interface):
    """A host not back by the deadline, with kube-vip still undone if it is."""
    return " ".join([reason, *_kube_vip_undone(ref, interface)])


def _step(item, now=None):
    ref, now = item["ref"], time.time() if now is None else now
    node, elapsed = ref["node"], now - ref.get("since", now)
    if ref["stage"] == "unconfirmed":
        return _still_unconfirmed(ref, now)
    if ref["stage"] == "applied":
        if elapsed < 15:
            return "running", 20, f"Applying on {node}"
        try:
            out, _ = hostrun.run(node, check_script(ref["carries_on"], ref["address"], ref["bond"],
                                                    ref["members"] if ref["action"] != "remove" else [], ref["mode"]), timeout=45)
        except Exception:
            out = ""
        seen = set(out.split("\n"))
        words = set(out.split())
        bonded = ref["action"] == "remove" or all(f"IN {m}" in seen for m in ref["members"])
        if "LACPFAILED" in words:
            ref.update(stage="done")
            return ("failed", 100, f"The switch did not answer LACP on {ref['bond']} within {LACP_WAIT} seconds, so {node} "
                    "put its old network back by itself. Group the switch ports as one LACP (802.3ad) group, or choose active-backup")
        if {"ADDR", "ROUTE", "GATEWAY", "ARMED"} <= words and bonded:
            needs_partner = ref["action"] != "remove" and ref["mode"] == "802.3ad"
            if needs_partner and "PARTNER" not in words:
                if elapsed < LACP_WAIT + 15:
                    return "running", 40, f"{node} is up on {ref['bond']}; waiting for the switch to answer LACP"
                hostrun.run(node, ROLLBACK_NOW, timeout=30, **_held(ref))
                ref.update(stage="rolled-back", since=now)
                return ("running", 60, f"The switch did not answer LACP on {ref['bond']} in {LACP_WAIT} seconds, so {node} "
                        "is putting its old network back now")
            out, _ = hostrun.run(node, CONFIRM_SCRIPT, timeout=45, **_held(ref))
            if "CONFIRMED" not in out:
                return "running", 50, f"{node} is up; confirming"
            ref.update(stage="confirmed", since=now)
            down = [m for m in ref["members"] if f"UP {m}" not in seen] if ref["action"] != "remove" else []
            note = f"; {', '.join(down)} has no link yet" if down else ""
            return "running", 70, f"{node} carries {ref['address']} on {ref['carries_on']}; the rollback is disarmed{note}"
        if elapsed > ROLLBACK_SECONDS + 60:
            return ("failed", 100, f"{node} did not come up with {ref['address']} on {ref['carries_on']}, so it put its old "
                    f"network back by itself. The configuration tried is kept in {ref['backup']} beside the old one")
        missing = {"ADDR": f"{ref['address']} on {ref['carries_on']}", "ROUTE": f"the default route by {ref['carries_on']}",
                   "GATEWAY": "the gateway answering"}
        waiting = [w for k, w in missing.items() if k not in words]
        if not bonded:
            waiting.append("every member in " + ref["bond"])
        return "running", 30, f"Waiting for {', '.join(waiting or ['an answer from the host'])}; {node} puts itself back in {max(0, int(ROLLBACK_SECONDS - elapsed))}s"
    if ref["stage"] == "rolled-back":
        if elapsed < 30:
            return "running", 80, f"{node} is putting its old network back"
        return ("failed", 100, f"The switch did not answer LACP, so {node} put its old network back. Group the switch ports "
                "as one LACP (802.3ad) group, or choose active-backup")
    if ref["stage"] == "confirmed":
        if not ref.get("renamed"):
            ref.update(stage="done")
            return "succeeded", 100, f"{node} carries its network on {ref['carries_on']}"
        vip = restart_kube_vip(kget, ksend, node, ref)
        service = next((s for s in ref.get("services") or [] if s.startswith(("k3s", "rke2"))), "")
        hosts, unread, down = before_restart(kget, node) if service else (1, "", [])
        refused = ""
        if hosts and hosts > 1 and not down:
            try:
                out, err = hostrun.run(node, f"systemd-run --on-active=2 systemctl restart {service} >/dev/null && echo OK", timeout=45,
                                       **_held(ref))
                refused = "" if "OK" in out.split() else (err or out or "no answer")[-160:]
            except POD_EXEC.ExecTimeout:
                pass    # it may have been scheduled: never twice; the Ready check below follows
            if not refused:
                ref.update(stage="restarting", since=now, **restart_baseline(kget, node))
                return "running", 85, f"Restarting {service} on {node}, so flannel carries pod traffic by {ref['carries_on']}"
        if not vip or unread or down or refused:
            return not_finished(ref, 70, elapsed, ref["carries_on"], RESTART_SECONDS, service, unread, down, refused)
        ref.update(stage="done")
        return "succeeded", 100, f"{node} carries its network on {ref['carries_on']}"
    if ref["stage"] == "restarting":
        vip = kube_vip_settled(kget, ksend, node, ref)
        if elapsed > 30 and kubelet_back(kget, node, ref):
            if not vip:
                return not_finished(ref, 92, elapsed, ref["carries_on"], RESTART_SECONDS)
            ref.update(stage="done")
            return "succeeded", 100, f"{node} carries its network on {ref['carries_on']}"
        if elapsed > RESTART_SECONDS:
            return "failed", 100, not_back_with_kube_vip(_not_back(ref, ref["carries_on"]), ref, ref["carries_on"])
        return "running", 92, f"Waiting for {node} to be Ready again"
    return "succeeded", 100, f"{node} carries its network on {ref['carries_on']}"


def guard_refusals(node, busy, servers_ready):
    """Why network_guard holds a change on node back: the same words for a
    bond and a bridge (homestead_host_bridge), since both go quiet for a
    moment and restart k3s or RKE2 there."""
    refusals = []
    if busy:
        refusals.append(f"another host's network is changing ({busy}): one host at a time")
    if servers_ready and servers_ready[1] and servers_ready[0] < servers_ready[1]:
        refusals.append(f"{servers_ready[1] - servers_ready[0]} of the other servers is not Ready: if {node} went quiet too, "
                        "the cluster could lose its quorum. Wait until they are Ready")
    return refusals


# The one Lease a host network change (bond or bridge) holds while it starts:
# from before network_guard reads the job store until its job row is written,
# which is only after netplan is applied. Replicas share it, so two changes
# sent at once to two hosts cannot both pass the guard. One left by a replica
# that stopped runs out after CLAIM_SECONDS and is taken over.
CLAIM = "homestead-network-change"
CLAIM_SECONDS = 600
STARTING = "another host's network change is starting: try again in a moment"


def _claim_path(name=""):
    base = f"/apis/coordination.k8s.io/v1/namespaces/{HOSTRUN.NS}/leases"
    return f"{base}/{name}" if name else base


def _when(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def claim_network(node, what):
    """Take the cluster's network-change Lease for a change on node. Returns
    the holder; raises ValueError while another change holds it and it has
    not run out. A write that loses a race (409) is refused the same way."""
    holder = f"{HOSTRUN.IDENTITY}/{secrets.token_hex(4)}"
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    lease = KUBE.get_optional(_claim_path(CLAIM), read=kget)
    try:
        if lease is None:
            ksend("POST", _claim_path(), {
                "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                "metadata": {"name": CLAIM, "namespace": HOSTRUN.NS, "labels": NAMES.labels("network-change"),
                             "annotations": {NAMES.key("change"): what}},
                "spec": {"holderIdentity": holder, "leaseDurationSeconds": CLAIM_SECONDS,
                         "acquireTime": stamp, "renewTime": stamp, "leaseTransitions": 0}})
            return holder
        spec = lease.get("spec") or {}
        renewed = _when(spec.get("renewTime")) or _when(spec.get("acquireTime"))
        if spec.get("holderIdentity") and renewed and \
                (now - renewed).total_seconds() <= int(spec.get("leaseDurationSeconds") or 0):
            raise ValueError(f"{node}: {STARTING}")
        spec.update(holderIdentity=holder, leaseDurationSeconds=CLAIM_SECONDS, acquireTime=stamp, renewTime=stamp,
                    leaseTransitions=int(spec.get("leaseTransitions") or 0) + 1)
        lease["spec"] = spec
        lease.setdefault("metadata", {}).setdefault("annotations", {})[NAMES.key("change")] = what
        # resourceVersion is kept: of two taking it at once, one gets a 409.
        ksend("PUT", _claim_path(CLAIM), lease)
        return holder
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise ValueError(f"{node}: {STARTING}") from None
        raise


def release_network(holder):
    """Give the Lease back if holder still has it. Never raises: one not
    given back runs out by itself."""
    try:
        lease = KUBE.get_optional(_claim_path(CLAIM), read=kget)
        if lease and (lease.get("spec") or {}).get("holderIdentity") == holder:
            lease["spec"]["holderIdentity"] = ""
            ksend("PUT", _claim_path(CLAIM), lease)
    except Exception:
        pass


@contextlib.contextmanager
def network_change(node, what):
    """Hold the Lease, and give network_guard's answer for node. Leave the
    block once the job row is written (ops.start), or by a refusal or error:
    the Lease is given back either way, and the job row blocks the rest."""
    holder = claim_network(node, what)
    try:
        yield network_guard(node)
    finally:
        release_network(holder)


def network_guard(node):
    """What a host network change waits for: another host's change still
    being checked, and the other servers' readiness (ready, total)."""
    busy = next((op.get("title", "") for op in OPS.snapshot()
                 if op.get("kind") in (KIND, "host-bridge") and op.get("status") not in OPS.TERMINAL), "")
    servers, down = _servers(kget("/api/v1/nodes").get("items", []), node)
    return busy, (len(servers) - len(down), len(servers))


def _host(body):
    if (platform() or {}).get("harvester"):
        raise ValueError("on Harvester, bonds are cluster network uplinks: Networking > Ports and uplinks")
    node = str(body.get("node") or "")
    if not node:
        raise ValueError("which host?")
    return node


def review(body):
    """What a bond change would write, and what refuses it, before anything is sent."""
    node = _host(body)
    busy, servers = network_guard(node)
    found = plan(node, body, inspect(node), busy, servers)
    found.pop("spec", None)
    return found


def change(body):
    """The bond change, confirmed by the host's name, as a job."""
    node = _host(body)
    if str(body.get("confirm") or "").strip() != node:
        raise ValueError(f"type the host's name, {node}, to confirm")
    with network_change(node, f"Bond change on {node}") as (busy, servers):
        op = start(node, body, OPS, busy, servers)
    ROUTER.forget("ports", "network")
    if op.get("unconfirmed"):
        return {"ok": False, "operation": op, "detail": f"{node}'s network change needs attention; see its job under Jobs"}
    return {"ok": True, "operation": op, "detail": f"{body.get('node')}'s network is changing; follow it under Jobs"}


ROUTES = {
    ("POST", "/api/node/bond/inspect"): ("admin", lambda request: summary(str(request.body.get("node") or ""),
                                                                          inspect(str(request.body.get("node") or "")))),
    ("POST", "/api/node/bond/preview"): ("admin", lambda request: review(request.body)),
    ("POST", "/api/node/bond"): ("admin", lambda request: change(request.body)),
}


def confirmed_keeps(node):
    """What stopping a job says once the host's rollback is disarmed, for a
    bond and a bridge: nothing else would restart kube-vip, k3s or RKE2."""
    return [f"{node} keeps its new network: its rollback is already disarmed",
            f"Homestead stops restarting kube-vip and k3s or RKE2 on {node}: restart any it had not restarted by hand, "
            "or VIPs and flannel may stay on the old interface",
            f"Other changes on {node} can start again at once"]


def cancel_plan(item):
    """Stopping tracking it also lets go of the host, so other changes there
    need not wait for the hold to run out (#597)."""
    node = item["ref"]["node"]
    if item["ref"].get("stage") == "unconfirmed":
        # Its script ran past its limit and its helper was removed: how far it
        # got, and whether a rollback was armed, is not known.
        return {"mode": "forget", "needs": "admin",
                "keeps": [f"Homestead cannot tell how far the change got on {node}; check its network first",
                          f"Other changes on {node} can start again at once"]}
    if item["ref"].get("stage") == "confirmed":
        # Its rollback is disarmed; the job alone was going to do what is left.
        return {"mode": "forget", "needs": "admin", "keeps": confirmed_keeps(node)}
    return {"mode": "forget", "needs": "admin",
            "keeps": [f"The change carries on on {node}: unless it was confirmed, {node} puts its old network back by itself",
                      f"Other changes on {node} can start again at once"]}


def cancel_run(item, options):
    _let_go(item["ref"])
    return f"Stopped tracking it; other changes on {item['ref']['node']} can start again"




def register(ops):
    """The job store's entry for a host's bond (#622)."""
    ops.RESOLVERS[KIND] = status
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
