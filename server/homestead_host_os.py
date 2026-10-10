"""Each k3s or RKE2 host's own operating system: what it runs, what it needs.

Harvester looks after its hosts' OS itself. On k3s and RKE2 the hosts are
ordinary Linux machines someone would otherwise SSH into to keep up to date,
so Homestead looks for them - through the same short-lived host helper as
disk set-up (homestead_hostrun) - and reports per host:

- the distribution and kernel, and how long since it booted;
- package updates waiting, and how many of them are security fixes, read
  from the package lists the host already keeps (Ubuntu refreshes them
  daily); "Check now" refreshes them first;
- whether it needs a restart to finish an update;
- systemd units that failed, whether its clock is synchronised, and how full
  its root filesystem is;
- its disks' partition tables - each partition's start, size, filesystem
  and mount - which the Disks card draws.

The leader reads each Ready host every six hours; alerts say what needs
someone (homestead_alerts). Installing updates is the host's own package
manager, started detached on the host (systemd-run) so a helper pod ending
cannot cut it off, and followed as a job. A kernel update then says the host
needs a restart, which Host actions does with its drain review.
"""
import json
import os
import re
import threading
import time

import homestead_shared as SHARED

kget = hostrun = platform = None
DATA_DIR = "/data"
EVERY = 6 * 3600
UNIT = "homestead-os-upgrade"
LOG = "/var/log/homestead-os-upgrade.log"
STATUS = "/var/lib/homestead/os-upgrade.status"
_lock = SHARED.SharedLock("host-os")

# Read-only unless REFRESH is 1, which refreshes the package lists first -
# what apt's own daily timer does.
SCRIPT = r"""export LC_ALL=C
if [ -r /etc/os-release ]; then . /etc/os-release; fi
echo "OS ${PRETTY_NAME:-${ID:-Linux}}"
echo "OSID ${ID:-} ${VERSION_ID:-}"
echo "KERNEL $(uname -r)"
echo "UP $(cut -d. -f1 /proc/uptime)"
[ -f /var/run/reboot-required ] && echo "REBOOT $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null | cut -c1-300)"
if command -v apt-get >/dev/null 2>&1; then
  echo "PKG apt"
  [ "$REFRESH" = 1 ] && DEBIAN_FRONTEND=noninteractive apt-get -qq -o DPkg::Lock::Timeout=60 update >/dev/null 2>&1 && echo "REFRESHED"
  echo "LISTS $(ls -t /var/lib/apt/lists/*_Packages 2>/dev/null | head -n 1 | xargs -r stat -c %Y 2>/dev/null)"
  apt-get -s -o Debug::NoLocking=1 upgrade --with-new-pkgs 2>/dev/null | awk '/^Inst /{s=($0 ~ /-security/) ? "security" : ""; print "UPD " $2 "|" s}' | head -n 800
elif command -v dnf >/dev/null 2>&1; then
  echo "PKG dnf"
  [ "$REFRESH" = 1 ] && dnf -q makecache >/dev/null 2>&1 && echo "REFRESHED"
  dnf -q -C updateinfo list --security 2>/dev/null | awk 'NF>=3 {print "SEC " $3}' | head -n 800
  dnf -q -C check-update 2>/dev/null | awk 'NF==3 && $1 ~ /\./ {print "UPD " $1 "|"}' | head -n 800
  command -v needs-restarting >/dev/null 2>&1 && ! needs-restarting -r >/dev/null 2>&1 && echo "REBOOT"
elif command -v zypper >/dev/null 2>&1; then
  echo "PKG zypper"
  [ "$REFRESH" = 1 ] && zypper -q refresh >/dev/null 2>&1 && echo "REFRESHED"
  zypper -q --no-refresh list-patches --category security 2>/dev/null | awk -F'|' 'NR>2 && NF>3 {print "SECP"}' | head -n 800
  zypper -q --no-refresh list-updates 2>/dev/null | awk -F'|' 'NR>2 && NF>3 {gsub(/ /, "", $3); print "UPD " $3 "|"}' | head -n 800
  zypper -q needs-rebooting >/dev/null 2>&1; [ $? = 102 ] && echo "REBOOT"
else echo "PKG none"; fi
systemctl --failed --no-legend --plain 2>/dev/null | awk 'NF {print "FAILED " $1}' | head -n 50
echo "NTP $(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
df -Pk / 2>/dev/null | awk 'NR==2 {print "ROOT " $2 " " $3}'
[ -f "$STATUS" ] && echo "LAST $(cat "$STATUS" 2>/dev/null) $(stat -c %Y "$STATUS" 2>/dev/null)"
systemctl is-active --quiet __UNIT__ 2>/dev/null && echo "UPGRADING"
# Updates the host installs by itself, and whether Homestead holds them off.
if command -v unattended-upgrade >/dev/null 2>&1; then
  U=$(apt-config dump 2>/dev/null | awk -F'"' '/^APT::Periodic::Unattended-Upgrade /{v=$2} END{print v}')
  B=$(apt-config dump 2>/dev/null | awk -F'"' '/^Unattended-Upgrade::Automatic-Reboot /{v=$2} END{print v}')
  echo "AUTO unattended-upgrades ${U:-0} ${B:-false} $(systemctl is-enabled apt-daily-upgrade.timer 2>/dev/null || echo unknown)"
elif systemctl is-enabled --quiet dnf-automatic-install.timer 2>/dev/null || systemctl is-enabled --quiet dnf-automatic.timer 2>/dev/null; then
  echo "AUTO dnf-automatic 1 unknown enabled"
fi
[ -f __HOLD__ ] && echo "HELD"
lsblk -P -b -o NAME,PKNAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,PTTYPE,PARTLABEL,LABEL,TRAN,VENDOR,MODEL 2>/dev/null | sed 's/^/BLK /'
for p in /sys/class/block/*/partition; do [ -e "$p" ] || continue; d=${p%/partition}; echo "START ${d##*/} $(cat "$d/start" 2>/dev/null)"; done
echo END
""".replace("$STATUS", STATUS).replace("__UNIT__", UNIT).replace("__HOLD__", "/etc/apt/apt.conf.d/99-homestead-hold")

# Ubuntu's automatic updates, held off: this file switches them off, and
# removing it gives the host back its own settings.
HOLD = "/etc/apt/apt.conf.d/99-homestead-hold"
HOLD_TEXT = ('// Homestead installs updates on this host itself (Nodes > OS updates), one host at a time.\n'
             '// Remove this file, or choose Ubuntu in those settings, to let unattended-upgrades run again.\n'
             'APT::Periodic::Unattended-Upgrade "0";\n'
             'Unattended-Upgrade::Automatic-Reboot "false";\n')

# The whole upgrade runs on the host, detached: a systemd unit of its own,
# which a helper pod being deleted cannot stop halfway through dpkg. apt's
# upgrade --with-new-pkgs, not dist-upgrade: new kernels come in, but no
# package is ever removed - open-iscsi, say, which Longhorn cannot do without.
UPGRADE = {
    "apt": ("apt-get -o DPkg::Lock::Timeout=300 update && DEBIAN_FRONTEND=noninteractive apt-get -y "
            "-o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade --with-new-pkgs"),
    "dnf": "dnf -y upgrade --refresh",
    "zypper": "zypper --non-interactive refresh && zypper --non-interactive update",
}
BLK = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def bind(_kget, _hostrun, _platform, data_dir="/data"):
    global kget, hostrun, platform, DATA_DIR
    kget, hostrun, platform, DATA_DIR = _kget, _hostrun, _platform, data_dir


def applies(p=None):
    p = p if p is not None else (platform(True) or {})
    return not p.get("harvester") and p.get("distribution") in ("k3s", "rke2")


def _path():
    return os.path.join(DATA_DIR, "host-os.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    SHARED.write_json(_path(), state, indent=1, sort_keys=True)


def _unescape(value):
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)


def parse(out, now=None):
    """The script's lines, as facts."""
    facts = {"os": "", "id": "", "version": "", "kernel": "", "uptime_s": None, "package_manager": "",
             "lists_at": None, "refreshed": False, "updates": [], "security": 0, "reboot": False, "reboot_for": "",
             "failed_units": [], "ntp": None, "root_total_gb": 0, "root_used_pct": 0, "upgrading": False,
             "last_upgrade": None, "disks": [], "complete": False,
             "auto": {"tool": "", "on": False, "reboots": False, "held": False}}
    blocks, starts, security_names, security_patches = [], {}, set(), 0
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        if key == "OS":
            facts["os"] = value
        elif key == "OSID":
            facts["id"], _, facts["version"] = value.partition(" ")
        elif key == "KERNEL":
            facts["kernel"] = value
        elif key == "UP" and value.isdigit():
            facts["uptime_s"] = int(value)
        elif key == "REBOOT":
            facts["reboot"], facts["reboot_for"] = True, value
        elif key == "PKG":
            facts["package_manager"] = "" if value == "none" else value
        elif key == "REFRESHED":
            facts["refreshed"] = True
        elif key == "LISTS" and value.isdigit():
            facts["lists_at"] = int(value)
        elif key == "UPD":
            name, _, kind = value.partition("|")
            facts["updates"].append({"name": name, "security": kind == "security"})
        elif key == "SEC":
            security_names.add(value)
        elif key == "SECP":
            security_patches += 1
        elif key == "FAILED" and value:
            facts["failed_units"].append(value)
        elif key == "NTP":
            facts["ntp"] = {"yes": True, "no": False}.get(value)
        elif key == "ROOT":
            total, _, used = value.partition(" ")
            if total.isdigit() and used.isdigit() and int(total):
                facts["root_total_gb"] = round(int(total) / 1024 ** 2, 1)
                facts["root_used_pct"] = round(int(used) * 100 / int(total))
        elif key == "LAST":
            code, _, at = value.partition(" ")
            facts["last_upgrade"] = {"ok": code == "0", "code": code, "at": int(at) if at.isdigit() else None}
        elif key == "AUTO":
            parts = (value.split() + ["", "", "", ""])[:4]
            facts["auto"].update(tool=parts[0], on=parts[1] not in ("0", "") and parts[3] not in ("disabled", "masked"),
                                 reboots=parts[2].lower() == "true")
        elif key == "HELD":
            facts["auto"]["held"] = True
        elif key == "UPGRADING":
            facts["upgrading"] = True
        elif key == "BLK":
            blocks.append({k: _unescape(v) for k, v in BLK.findall(value)})
        elif key == "START":
            name, _, sector = value.partition(" ")
            if sector.isdigit():
                starts[name] = int(sector)
        elif key == "END":
            facts["complete"] = True
    for row in facts["updates"]:
        # dnf names a package with its architecture; its advisory list does too.
        row["security"] = row["security"] or row["name"] in security_names
    facts["security"] = sum(1 for row in facts["updates"] if row["security"]) or security_patches
    facts["disks"] = layout(blocks, starts)
    return facts


def layout(blocks, starts):
    """Each whole disk and its partitions, in the order they sit on it."""
    disks = {}
    for row in blocks:
        if (row.get("TRAN", "").lower() == "iscsi" or row.get("VENDOR", "").strip().upper() in ("IET", "LIO-ORG")
                or "VIRTUAL-DISK" in row.get("MODEL", "").upper()):
            continue  # workload volume attachments, not local host disks
        if row.get("TYPE") == "disk" and not row.get("NAME", "").startswith(("loop", "zram", "ram")):
            disks[row["NAME"]] = {"name": row["NAME"], "size": int(row.get("SIZE") or 0), "table": row.get("PTTYPE", ""),
                                  "fstype": row.get("FSTYPE", ""), "mount": row.get("MOUNTPOINT", ""), "partitions": []}
    for row in blocks:
        disk = disks.get(row.get("PKNAME", ""))
        if row.get("TYPE") != "part" or disk is None:
            continue
        # What sits on the partition - an LVM volume, say - is where its mount is.
        children = [c for c in blocks if c.get("PKNAME") == row["NAME"]]
        mounts = [row.get("MOUNTPOINT", "")] + [c.get("MOUNTPOINT", "") for c in children]
        disk["partitions"].append({
            "name": row["NAME"], "start": starts.get(row["NAME"], 0) * 512, "size": int(row.get("SIZE") or 0),
            "fstype": row.get("FSTYPE", ""), "label": row.get("PARTLABEL") or row.get("LABEL", ""),
            "mounts": [m for m in mounts if m][:4],
            "holds": sorted({c.get("TYPE", "") for c in children if c.get("TYPE")}),
        })
    for disk in disks.values():
        disk["partitions"].sort(key=lambda p: p["start"])
        used = sum(p["size"] for p in disk["partitions"])
        disk["free"] = max(0, disk["size"] - used) if disk["partitions"] else 0
    return sorted(disks.values(), key=lambda d: d["name"])


def read(node, refresh=False, now=None):
    """Look at node's OS now and keep what was seen."""
    out, err = hostrun.run(node, f"REFRESH={1 if refresh else 0}\n" + SCRIPT, timeout=240 if refresh else 90)
    facts = parse(out)
    if not facts["complete"]:
        # The facts are one per line, so the last one says how far it got.
        last = next((line.split(" ", 1)[0] for line in reversed(out.splitlines()) if line.strip()), "")
        raise ValueError(f"could not read {node}'s operating system: the check stopped part-way"
                         + (f", after {last}" if last else "")
                         + (f": {err.strip()[-160:]}" if (err or "").strip() else
                            "; the host's kernel log (dmesg) says whether it ran out of memory"))
    facts["at"] = int(now or time.time())
    # The boot this read saw: a later restart makes its "restart needed" old news.
    try:
        facts["boot_id"] = ((kget(f"/api/v1/nodes/{node}") or {}).get("status") or {}).get("nodeInfo", {}).get("bootID", "")
    except Exception:
        facts["boot_id"] = ""
    with _lock:
        state = _load()
        state[node] = facts
        _save(state)
    return facts


def stored(node):
    """What was last read of node's OS, without looking again."""
    return _load().get(node)


def summary(facts):
    """The one line and tone a host's OS gets in a list."""
    if not facts:
        return {"tone": "", "text": "not read yet"}
    if facts.get("upgrading"):
        return {"tone": "info", "text": "installing updates"}
    words, tone = [], "ok"
    if facts.get("security"):
        words.append(f"{facts['security']} security update{'s' if facts['security'] != 1 else ''}")
        tone = "warn"
    elif facts.get("updates"):
        words.append(f"{len(facts['updates'])} update{'s' if len(facts['updates']) != 1 else ''}")
        tone = "info"
    if facts.get("reboot"):
        words.append("restart needed")
        tone = "warn"
    if facts.get("failed_units"):
        words.append(f"{len(facts['failed_units'])} failed service{'s' if len(facts['failed_units']) != 1 else ''}")
        tone = "warn"
    if facts.get("root_used_pct", 0) >= 90:
        words.append(f"root {facts['root_used_pct']}% full")
        tone = "bad"
    return {"tone": tone, "text": ", ".join(words) or "up to date"}


def _boots():
    """Each node's current boot ID, as Kubernetes reports it."""
    try:
        return {item["metadata"]["name"]: ((item.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
                for item in (kget("/api/v1/nodes") or {}).get("items", [])}
    except Exception:
        return {}


def current(facts, boot_id):
    """facts, minus a restart the host has had since they were read: a
    manual restart otherwise left "restart needed" until the next read,
    up to six hours later. The next round reads the host again."""
    if facts and facts.get("reboot") and boot_id and facts.get("boot_id") and boot_id != facts["boot_id"]:
        return {**facts, "reboot": False, "reboot_for": "", "restarted_since_read": True}
    return facts


def report(node=None):
    """What is known of each host, or of one, with how old it is."""
    p = platform(True) or {}
    state, boots = _load(), _boots()
    rows = {}
    for name, facts in state.items():
        if node is None or name == node:
            facts = current(facts, boots.get(name, ""))
            rows[name] = {**facts, "summary": summary(facts)}
    return {"applies": applies(p), "hosts": rows, "every_s": EVERY, "checking": checking()}


# Every Ready host read again now, one at a time, refreshing its package
# lists first (#371): what a host has had installed by hand shows without
# waiting up to EVERY for the next round. This replica's own, in a thread -
# each read can take minutes.
_CHECK = {"running": False, "hosts": [], "done": [], "failed": {}, "at": 0}
_check_lock = threading.Lock()


def checking():
    with _check_lock:
        return checking_locked()


def _ready_hosts():
    return [item["metadata"]["name"] for item in (kget("/api/v1/nodes") or {}).get("items", [])
            if any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in (item.get("status") or {}).get("conditions") or [])]


def check_all(reader=None, wait=False):
    """Start reading every Ready host again; returns what is being checked.
    One check at a time: a second request joins the one under way."""
    reader = reader or (lambda name: read(name, refresh=True))
    with _check_lock:
        if _CHECK["running"]:
            return checking_locked()
        hosts = sorted(_ready_hosts())
        _CHECK.update(running=bool(hosts), hosts=hosts, done=[], failed={}, at=time.time())
        started = checking_locked()

    def run():
        for name in hosts:
            try:
                reader(name)
            except Exception as error:
                with _check_lock:
                    _CHECK["failed"][name] = str(error)[:160]
            with _check_lock:
                _CHECK["done"].append(name)
        with _check_lock:
            _CHECK["running"] = False

    if hosts:
        thread = threading.Thread(target=run, name="host-os-check", daemon=True)
        thread.start()
        if wait:
            thread.join()
    return started


def checking_locked():
    return {**_CHECK, "hosts": list(_CHECK["hosts"]), "done": list(_CHECK["done"]), "failed": dict(_CHECK["failed"])}


def tick(now=None):
    """Each Ready host whose facts are older than EVERY, read again. Returns
    the hosts read; one that cannot be read waits for the next round."""
    if not applies():
        return []
    now = now or time.time()
    state = _load()
    read_now = []
    for item in (kget("/api/v1/nodes") or {}).get("items", []):
        name = item["metadata"]["name"]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (item.get("status") or {}).get("conditions") or [])
        known = state.get(name) or {}
        boot = ((item.get("status") or {}).get("nodeInfo") or {}).get("bootID", "")
        restarted = bool(boot and known.get("boot_id") and boot != known["boot_id"])
        if not ready or (now - known.get("at", 0) < EVERY and not restarted):
            continue
        try:
            read(name, now=now)
            read_now.append(name)
        except Exception as error:
            print(f"host OS: {name}: {str(error)[:160]}", flush=True)
    return read_now


def set_hold(node, hold):
    """Hold Ubuntu's automatic updates off on node, or let them go again."""
    if hold:
        script = f"mkdir -p /etc/apt/apt.conf.d && printf '%s' '{HOLD_TEXT}' > {HOLD} && echo DONE"
    else:
        script = f"rm -f {HOLD} && echo DONE"
    out, err = hostrun.run(node, script, timeout=45)
    if "DONE" not in out:
        raise ValueError(f"could not {'hold' if hold else 'release'} {node}'s automatic updates: {(err or out)[-160:]}")
    with _lock:
        state = _load()
        if node in state:
            auto = state[node].setdefault("auto", {})
            auto["held"] = bool(hold)
            _save(state)


def upgrade_begin(node, facts=None):
    """Start installing every waiting update on node, detached on the host."""
    facts = facts or read(node)
    manager = facts.get("package_manager")
    if manager not in UPGRADE:
        raise ValueError(f"{node} has no package manager Homestead knows (apt, dnf or zypper)")
    if facts.get("upgrading"):
        raise ValueError(f"{node} is installing updates already")
    command = UPGRADE[manager]
    script = (f"mkdir -p {os.path.dirname(STATUS)} && rm -f {STATUS} && "
              f"systemd-run --unit={UNIT} --collect --quiet sh -c "
              f"'( {command} ) > {LOG} 2>&1; echo $? > {STATUS}' && echo STARTED")
    out, err = hostrun.run(node, script, timeout=60)
    if "STARTED" not in out:
        raise ValueError(f"updates on {node} did not start: {(err or out)[-200:]}")
    return manager


def upgrade_state(node):
    """{running, code, last}: whether node is still installing, and how it ended."""
    out, _ = hostrun.run(node, f"systemctl is-active --quiet {UNIT} && echo RUNNING; "
                               f"[ -f {STATUS} ] && echo \"CODE $(cat {STATUS})\"; tail -n 1 {LOG} 2>/dev/null | cut -c1-160",
                         timeout=45)
    lines = out.splitlines()
    code = next((line[5:].strip() for line in lines if line.startswith("CODE ")), None)
    last = next((line for line in reversed(lines) if line != "RUNNING" and not line.startswith("CODE ")), "")
    # No exit code written yet is still running, whatever systemd says: the
    # unit writes it as its last act.
    return {"running": code is None, "code": code, "last": last}


def upgrade_start(node, ops):
    """Install every waiting update on node, detached on the host, as a job."""
    facts = read(node)
    manager = upgrade_begin(node, facts)
    count = len(facts.get("updates") or [])
    return ops.start("host-os", f"Install {count or 'the'} update{'s' if count != 1 else ''} on {node}",
                     {"kind": "Node", "name": node}, "/nodes", {"node": node, "since": time.time(), "manager": manager},
                     f"{manager} is installing updates on {node}")


def status(item, now=None):
    """The upgrade job's state, from the host: (status, progress, message)."""
    ref, now = item["ref"], now or time.time()
    node = ref["node"]
    if now - ref.get("since", now) < 20:
        return "running", 10, f"Installing updates on {node}"
    now_state = upgrade_state(node)
    code = now_state["code"]
    if code is None:
        if now - ref.get("since", now) > 7200:
            return "failed", 100, f"No result from {node}'s update after two hours; see {LOG} there"
        last = now_state["last"]
        return "running", 50, f"Installing on {node}: {last}" if last else f"Installing updates on {node}"
    try:
        facts = read(node)
    except Exception:
        facts = {}
    if code != "0":
        return "failed", 100, f"The package manager on {node} stopped with code {code}; its output is in {LOG} there"
    return "succeeded", 100, (f"{node} is up to date; it needs a restart to finish - use Host actions"
                              if facts.get("reboot") else f"{node} is up to date")


def alert_facts(state=None):
    """What needs someone, per host, as alert conditions."""
    if state is None:
        boots = _boots()
        state = {node: current(facts, boots.get(node, "")) for node, facts in _load().items()}
    facts = []
    for node, host in state.items():
        if host.get("security"):
            facts.append({"key": f"hostos:{node}:security", "category": "updates", "severity": "degraded",
                          "title": f"{node} has {host['security']} security update{'s' if host['security'] != 1 else ''}",
                          "resolved": f"No security updates reported on {node}",
                          "body": "Review host updates in Settings › Updates.", "href": "/settings?tab=updates",
                          "signals": {"security_updates": host["security"]}})
        auto = host.get("auto") or {}
        if auto.get("on") and auto.get("reboots") and not auto.get("held"):
            facts.append({"key": f"hostos:{node}:autoreboot", "category": "updates", "severity": "info",
                          "title": f"{node} restarts itself for updates",
                          "resolved": f"{node} no longer restarts itself for updates",
                          "body": ("unattended-upgrades has Automatic-Reboot on, so it restarts without a drain. "
                                   "Review host update and restart settings in Settings › Updates."),
                          "href": "/settings?tab=updates"})
        if host.get("reboot"):
            facts.append({"key": f"hostos:{node}:reboot", "category": "updates", "severity": "info",
                          "title": f"{node} needs a restart to finish an update", "resolved": f"Restart requirement cleared on {node}",
                          "body": host.get("reboot_for") or "", "href": "/settings?tab=updates"})
        if host.get("failed_units"):
            facts.append({"key": f"hostos:{node}:failed", "category": "degraded", "severity": "degraded",
                          "title": f"{node}: {', '.join(host['failed_units'][:3])} failed",
                          "resolved": f"No failed services reported on {node}", "body": "Review failed services on the host.", "signals": {unit: 1 for unit in host["failed_units"]},
                          "href": "/nodes"})
        if host.get("root_used_pct", 0) >= 90:
            facts.append({"key": f"hostos:{node}:root", "category": "degraded", "severity": "degraded",
                          "title": f"{node}'s root filesystem is {host['root_used_pct']}% full",
                          "resolved": f"Root filesystem usage is below 90% on {node}", "href": "/nodes",
                          "signals": {"used_percent_band": int(host["root_used_pct"] // 5)}})
    return facts
