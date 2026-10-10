"""A host's LAN interface moved into a Linux bridge, so VMs and containers
join the LAN through it and the host can reach them - what Proxmox's vmbr0
is, and what Harvester's mgmt-br is.

macvtap (homestead_macvtap) gives VMs the LAN without touching the host; a
bridge is for when the host itself must reach its VMs. It changes the host's
own network, so it is done carefully, one host at a time:

1. look (read-only): the interface the default route leaves by, its address
   and how it gets it, netplan and systemd-networkd in charge (Ubuntu
   Server's way; anything else is refused), no bridge of that name yet;
2. convert: netplan's files are copied aside, the interface's addresses,
   routes and DHCP move to br0 in the same file, and br0 takes the
   interface's MAC address - and asks DHCP as that MAC - so the router hands
   out the same address. `netplan generate` must accept it. A rollback is
   armed first as a systemd timer on the host (put the copies back, apply);
   then the new configuration is applied, detached, a few seconds later;
3. check, from Homestead: the host's address is on br0, its default route
   leaves by br0, its gateway answers. Only then is the rollback disarmed. A
   host that never answers - the address changed, the route is gone - is put
   back by its own timer, four minutes after the change;
4. kube-vip's pod on that host is restarted, to announce VIPs on br0; on a
   cluster of more than one host, k3s (or RKE2) restarts, so flannel carries
   pod traffic by br0 too. Containers keep running through both.

Nothing is done to a host whose network Homestead cannot describe.
"""
import re
import time

hostrun = kget = ksend = None
BRIDGE = "br0"
ROLLBACK_SECONDS = 240
BACKUP_ROOT = "/var/lib/homestead"
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,15}")
MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")

INSPECT_SCRIPT = r"""IF=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
echo "IFACE $IF"
if [ -n "$IF" ]; then
  echo "MAC $(cat /sys/class/net/$IF/address 2>/dev/null)"
  [ -d /sys/class/net/$IF/bridge ] && echo "ISBRIDGE"
  [ -e /sys/class/net/$IF/master ] && echo "MASTER $(basename "$(readlink /sys/class/net/$IF/master)")"
  [ -d /sys/class/net/$IF/bonding ] && echo "BOND"
  [ -d /sys/class/net/$IF/wireless ] && echo "WIRELESS"
  [ -e /sys/class/net/$IF/device ] && echo "PHYSICAL"
  ip -4 -o addr show dev "$IF" | awk '{print "ADDR " $4 " " ($0 ~ / dynamic / ? "dhcp" : "static")}'
  echo "GW $(ip -4 route show default | awk '{print $3; exit}')"
fi
[ -e /sys/class/net/__BR__ ] && echo "EXISTS"
command -v netplan >/dev/null && echo "NETPLAN"
systemctl is-active -q systemd-networkd && echo "NETWORKD"
systemctl is-active -q NetworkManager && echo "NM"
command -v systemd-run >/dev/null && echo "SYSTEMDRUN"
systemctl is-active -q homestead-bridge-rollback.timer && echo "ARMED"
if python3 -c "import yaml" 2>/dev/null; then
  echo "PYYAML"
  python3 - "$IF" "$(cat /sys/class/net/$IF/address 2>/dev/null)" <<'PY'
import glob, sys, yaml
nic, mac = sys.argv[1], sys.argv[2]
for path in sorted(glob.glob("/etc/netplan/*.yaml")):
    try:
        net = (yaml.safe_load(open(path)) or {}).get("network") or {}
    except Exception:
        print("BADFILE " + path)
        continue
    if net.get("renderer"):
        print("RENDERER " + str(net["renderer"]))
    for key, eth in (net.get("ethernets") or {}).items():
        match = (eth or {}).get("match") or {}
        if nic in (key, (eth or {}).get("set-name"), match.get("name")) or (mac and match.get("macaddress", "").lower() == mac):
            print("DEFINED " + path + " " + key)
    if "__BR__" in (net.get("bridges") or {}):
        print("EXISTS")
PY
fi
for s in k3s k3s-agent rke2-server rke2-agent; do systemctl is-active -q $s && echo "SERVICE $s"; done
grep -qs '^flannel-iface:' /etc/rancher/k3s/config.yaml /etc/rancher/rke2/config.yaml && echo "FLANNELIFACE"
echo END""".replace("__BR__", BRIDGE)


def bind(_hostrun, _kget, _ksend):
    global hostrun, kget, ksend
    hostrun, kget, ksend = _hostrun, _kget, _ksend


def parse(out):
    facts = {"interface": "", "mac": "", "addresses": [], "dhcp": False, "gateway": "", "defined": "", "netplan_id": "",
             "services": [], "flags": set(), "renderer": ""}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "IFACE":
            facts["interface"] = value if NAME.fullmatch(value or "") else ""
        elif key == "MAC":
            facts["mac"] = value.lower() if MAC.fullmatch(value.lower()) else ""
        elif key == "ADDR":
            cidr, _, how = value.partition(" ")
            facts["addresses"].append(cidr)
            facts["dhcp"] = facts["dhcp"] or how == "dhcp"
            # The host's own address, not a VIP kube-vip put beside it (a /32).
            if not cidr.endswith("/32") and (how == "dhcp" or not facts.get("primary")):
                facts["primary"] = cidr
        elif key == "GW":
            facts["gateway"] = value if IPV4.fullmatch(value) else ""
        elif key == "DEFINED" and not facts["defined"]:
            facts["defined"], _, facts["netplan_id"] = value.partition(" ")
        elif key == "SERVICE":
            facts["services"].append(value)
        elif key == "RENDERER":
            facts["renderer"] = value
        elif key == "MASTER":
            facts["master"] = value
        elif key:
            facts["flags"].add(key)
    return facts


def problem(facts):
    """Why this host cannot be converted, or ""."""
    flags = facts["flags"]
    if "END" not in flags:
        return "the host did not finish describing its network"
    if not facts["interface"]:
        return "the host has no IPv4 default route to follow"
    if "ISBRIDGE" in flags:
        return f"{facts['interface']} is a bridge already: make a LAN network on it"
    if facts.get("master"):
        return f"{facts['interface']} is in {facts['master']} already: make a LAN network on {facts['master']}"
    if "BOND" in flags or "WIRELESS" in flags or "PHYSICAL" not in flags:
        return f"{facts['interface']} is not a plain wired network interface; Homestead converts only those"
    if "EXISTS" in flags:
        return f"the host has a {BRIDGE} already"
    if "ARMED" in flags:
        return "a conversion on this host is still waiting to be checked"
    if "NETPLAN" not in flags or "NETWORKD" not in flags or "NM" in flags or facts["renderer"] not in ("", "networkd"):
        return "Homestead converts hosts whose network netplan sets up through systemd-networkd (Ubuntu Server's way); this one differs"
    if "PYYAML" not in flags or "SYSTEMDRUN" not in flags:
        return "the host lacks python3-yaml or systemd-run, which the conversion needs"
    if not facts["defined"]:
        return f"{facts['interface']} is not set up in /etc/netplan, so there is nothing to move to the bridge"
    if not facts.get("primary") or not facts["gateway"] or not facts["mac"]:
        return f"{facts['interface']} has no IPv4 address, gateway or MAC address to carry over"
    if "FLANNELIFACE" in flags:
        return "k3s or RKE2 is set to a flannel interface by name; change flannel-iface to br0 by hand, then convert"
    return ""


def inspect(node):
    out, err = hostrun.run(node, INSPECT_SCRIPT, timeout=60)
    facts = parse(out)
    reason = problem(facts) or ("" if out.strip() else (err or "the host did not answer")[:300])
    return {"node": node, "interface": facts["interface"], "mac": facts["mac"], "address": facts.get("primary", ""),
            "dhcp": facts["dhcp"], "gateway": facts["gateway"], "file": facts["defined"], "netplan_id": facts["netplan_id"],
            "services": facts["services"], "bridge": BRIDGE, "problem": reason, "rollback_seconds": ROLLBACK_SECONDS}


def convert_script(facts, stamp):
    backup = f"{BACKUP_ROOT}/netplan-{stamp}"
    return f"""set -e
[ -e /sys/class/net/{BRIDGE} ] && {{ echo "ERR the host has a {BRIDGE} already"; exit 1; }}
mkdir -p {backup}
cp -a /etc/netplan/. {backup}/
python3 - "{facts['file']}" "{facts['netplan_id']}" "{BRIDGE}" "{facts['mac']}" <<'PY'
import os, sys, yaml
path, key, bridge, mac = sys.argv[1:5]
MOVE = ("addresses", "dhcp4", "dhcp6", "dhcp4-overrides", "dhcp6-overrides", "gateway4", "gateway6", "routes",
        "routing-policy", "nameservers", "accept-ra", "ipv6-privacy", "link-local", "dhcp-identifier", "critical",
        "ipv6-address-generation", "ipv6-address-token", "ipv6-mtu")
data = yaml.safe_load(open(path)) or {{}}
net = data["network"]
eth = net["ethernets"][key]
moved = {{k: eth.pop(k) for k in list(eth) if k in MOVE}}
eth["dhcp4"] = False
eth["dhcp6"] = False
# The interface's own MAC, and DHCP asked as that MAC: the router gives
# the bridge the address the interface had.
bridge_cfg = dict(moved, interfaces=[key], macaddress=mac, parameters={{"stp": False, "forward-delay": 0}})
if bridge_cfg.get("dhcp4") or bridge_cfg.get("dhcp6"):
    bridge_cfg["dhcp-identifier"] = "mac"
net.setdefault("bridges", {{}})[bridge] = bridge_cfg
with open(path + ".homestead", "w") as out:
    yaml.safe_dump(data, out, default_flow_style=False, sort_keys=False)
os.chmod(path + ".homestead", 0o600)
os.replace(path + ".homestead", path)
PY
# cloud-init may write its network file again at boot; it is told not to.
if grep -qs cloud-init {facts['file']}; then
  mkdir -p /etc/cloud/cloud.cfg.d
  echo "network: {{config: disabled}}" > /etc/cloud/cloud.cfg.d/99-homestead-bridge.cfg
fi
if ! netplan generate 2>/tmp/homestead-netplan.err; then
  rm -f /etc/netplan/*.yaml; cp -a {backup}/. /etc/netplan/
  echo "ERR netplan did not accept the new configuration ($(head -c 200 /tmp/homestead-netplan.err)); the old one is back"
  exit 1
fi
systemd-run --unit=homestead-bridge-rollback --on-active={ROLLBACK_SECONDS} /bin/sh -c 'rm -f /etc/netplan/*.yaml; cp -a {backup}/. /etc/netplan/; rm -f /etc/cloud/cloud.cfg.d/99-homestead-bridge.cfg; netplan apply' >/dev/null
systemd-run --unit=homestead-bridge-apply --on-active=3 /usr/sbin/netplan apply >/dev/null
echo "OK {backup}"
"""


def check_script(interface, address):
    ip = address.split("/")[0]
    return f"""B={BRIDGE}
ip -4 -o addr show dev $B 2>/dev/null | grep -q " {ip}/" && echo "ADDR"
[ "$(basename "$(readlink /sys/class/net/{interface}/master 2>/dev/null)")" = "$B" ] && echo "PORT"
ip -4 route show default | grep -q "dev $B" && echo "ROUTE"
GW=$(ip -4 route show default | awk '{{print $3; exit}}')
[ -n "$GW" ] && ping -c 1 -W 2 "$GW" >/dev/null 2>&1 && echo "GATEWAY"
systemctl is-active -q homestead-bridge-rollback.timer && echo "ARMED"
echo END"""


CONFIRM_SCRIPT = "systemctl stop homestead-bridge-rollback.timer && echo CONFIRMED"


def start(node, ops):
    """Look again, convert, and hand the rest to a job. Admin only."""
    facts = inspect(node)
    if facts["problem"]:
        raise ValueError(f"{node}: {facts['problem']}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out, err = hostrun.run(node, convert_script({"file": facts["file"], "netplan_id": facts["netplan_id"],
                                                  "mac": facts["mac"]}, stamp), timeout=120)
    done = next((line for line in out.splitlines() if line.startswith("OK ")), "")
    if not done:
        reason = next((line[4:] for line in out.splitlines() if line.startswith("ERR ")), "") or (err or out)[-300:]
        raise ValueError(f"{node} was not changed: {reason}")
    ref = {"node": node, "interface": facts["interface"], "address": facts["address"], "bridge": BRIDGE,
           "backup": done[3:], "stage": "applied", "since": time.time(), "services": facts["services"]}
    return ops.start("host-bridge", f"Move {node}'s {facts['interface']} into {BRIDGE}",
                     {"kind": "Node", "name": node}, "/nodes", ref,
                     f"Applied; checking {node} on {BRIDGE} - it puts itself back in {ROLLBACK_SECONDS // 60} minutes unless confirmed")


def _node_count():
    try:
        return len(kget("/api/v1/nodes").get("items", []))
    except Exception:
        return 1


def status(item, now=None):
    """The job's next step: (status, progress, message)."""
    ref, now = item["ref"], time.time() if now is None else now
    node, elapsed = ref["node"], now - ref.get("since", now)
    if ref["stage"] == "applied":
        if elapsed < 15:
            return "running", 20, f"Applying on {node}"
        try:
            out, _ = hostrun.run(node, check_script(ref["interface"], ref["address"]), timeout=45)
        except Exception:
            out = ""
        seen = set(out.split())
        if {"ADDR", "PORT", "ROUTE", "GATEWAY", "ARMED"} <= seen:
            out, _ = hostrun.run(node, CONFIRM_SCRIPT, timeout=45)
            if "CONFIRMED" not in out:
                return "running", 40, f"{node} is up on {BRIDGE}; confirming"
            ref.update(stage="confirmed", since=now)
            return "running", 60, f"{node} is on {BRIDGE}, with {ref['address']}; the rollback is disarmed"
        if elapsed > ROLLBACK_SECONDS + 60:
            return ("failed", 100, f"{node} did not come up on {BRIDGE} with {ref['address']}, so it put its old network "
                    f"back by itself. The configuration tried is kept in {ref['backup']} beside the old one")
        missing = {"ADDR": f"{ref['address']} on {BRIDGE}", "PORT": f"{ref['interface']} in {BRIDGE}",
                   "ROUTE": f"the default route by {BRIDGE}", "GATEWAY": "the gateway answering"}
        waiting = [words for key, words in missing.items() if key not in seen] or ["an answer from the host"]
        return "running", 30, f"Waiting for {', '.join(waiting)}; {node} puts itself back in {max(0, int(ROLLBACK_SECONDS - elapsed))}s"
    if ref["stage"] == "confirmed":
        # kube-vip finds its interface when it starts: its pod here starts again.
        try:
            pods = kget("/api/v1/namespaces/kube-system/pods?fieldSelector=spec.nodeName%3D" + node).get("items", [])
            for pod in pods:
                if pod["metadata"]["name"].startswith("kube-vip"):
                    ksend("DELETE", f"/api/v1/namespaces/kube-system/pods/{pod['metadata']['name']}")
        except Exception:
            pass
        service = next((s for s in ref.get("services") or [] if s.startswith(("k3s", "rke2"))), "")
        if service and _node_count() > 1:
            hostrun.run(node, f"systemd-run --on-active=2 systemctl restart {service} >/dev/null && echo OK", timeout=45)
            ref.update(stage="restarting", since=now)
            return "running", 80, f"Restarting {service} on {node}, so flannel carries pod traffic by {BRIDGE}"
        ref.update(stage="done")
        return "succeeded", 100, f"{node} is on {BRIDGE}: make a LAN network on {BRIDGE} for VMs and containers"
    if ref["stage"] == "restarting":
        node_obj = kget(f"/api/v1/nodes/{node}")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in (node_obj.get("status") or {}).get("conditions") or [])
        if ready and elapsed > 30:
            ref.update(stage="done")
            return "succeeded", 100, f"{node} is on {BRIDGE}: make a LAN network on {BRIDGE} for VMs and containers"
        return "running", 90, f"Waiting for {node} to be Ready again"
    return "succeeded", 100, f"{node} is on {BRIDGE}"


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/node/bridge/inspect"): ("admin", lambda request: inspect(str(request.body.get("node") or ""))),
}
