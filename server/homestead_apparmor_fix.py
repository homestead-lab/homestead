"""A kernel setting that stops VMs from starting on some kernels, turned off
on hosts that run them (docs/plans/resilience-and-validated-updates.md,
section 6).

Ubuntu's kernel 7.0.0-43 (November 2026) began to turn unprivileged AppArmor
profile changes into stacked profiles, as kernel.apparmor_restrict_
unprivileged_unconfined asks, which Ubuntu sets to 1. In KubeVirt's launcher
pod that left QEMU with two AppArmor labels, so it could not signal its own
threads ("qemu:cpus_kick_thread: Permission denied") and every VM exited at
start. 7.0.0-38 ignored the setting in this case. With the setting at 0 the
same kernel starts VMs again (tested on a single-host k3s cluster, 14 November
2026: a Windows VM ran with no AppArmor denials).

On each k3s or RKE2 host that runs VMs (KubeVirt labels it kubevirt.io/
schedulable) and has the setting, the leader sets it to 0 once, now and in
/etc/sysctl.d, so that it holds after a restart. Its own file: the inotify
limits' 90-homestead.conf is written whole. It acts once per host and records
it, so a person who sets it back keeps their choice. It relaxes one hardening
rule, about unprivileged processes leaving their profile, on hosts that run
VMs only. Hosts without AppArmor's setting, and Harvester, are left alone.
"""
import os
import time

import homestead_hostrun as HOSTRUN
import homestead_shared as SHARED

kget = hostrun = platform = None
DATA_DIR = "/data"
SETTING = "kernel.apparmor_restrict_unprivileged_unconfined"
CONF = "/etc/sysctl.d/91-homestead-apparmor.conf"
LABEL = "kubevirt.io/schedulable"


def bind(_kget, _hostrun, _platform, data_dir="/data"):
    global kget, hostrun, platform, DATA_DIR
    kget, hostrun, platform, DATA_DIR = _kget, _hostrun, _platform, data_dir


def _path():
    return os.path.join(DATA_DIR, "apparmor-fix.json")


def _load():
    return SHARED.read_json(_path(), {})


def script():
    proc = "/proc/sys/" + SETTING.replace(".", "/")
    return f"""F={proc}
[ -e "$F" ] || {{ echo "ABSENT"; exit 0; }}
V=$(cat "$F")
if [ "$V" = 0 ]; then echo "KEPT $V"; exit 0; fi
printf '# Homestead: QEMU in KubeVirt cannot start VMs with this at 1 on some kernels (Ubuntu 7.0.0-43).\\n{SETTING} = 0\\n' > {CONF}
sysctl -q -w {SETTING}=0
echo "SET $V"
"""


def tick():
    """Each Ready host that runs VMs and is not done yet. Returns {node: what changed} for those set now."""
    p = platform() or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        return {}
    try:
        state = _load()
    except SHARED.Unreadable as error:
        # Nothing done from a record that cannot be read, nor saved over it.
        print(f"AppArmor setting: {error}", flush=True)
        return {}
    changed = {}
    for node in (kget("/api/v1/nodes") or {}).get("items", []):
        meta = node.get("metadata") or {}
        name = meta.get("name", "")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (node.get("status") or {}).get("conditions") or [])
        if not ready or name in state or (meta.get("labels") or {}).get(LABEL) != "true":
            continue
        try:
            out, err = hostrun.run(name, script(), timeout=60,
                                   change=f"Let VMs start on {name} on kernels that stack AppArmor profiles ({SETTING} = 0)")
        except HOSTRUN.Busy:
            continue    # another change has the host: next round, and the others go on (#597)
        words = out.split()
        if not words or words[0] not in ("ABSENT", "KEPT", "SET"):
            raise ValueError(f"Homestead could not check {name}'s AppArmor setting: {(err or out)[:200]}")
        state[name] = {"at": int(time.time()), "result": " ".join(words[:2])}
        if words[0] == "SET":
            changed[name] = f"{SETTING} {words[1]} -> 0"
        SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    return changed
