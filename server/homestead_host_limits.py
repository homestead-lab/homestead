"""Kernel limits a Kubernetes host outgrows.

Every file watcher a process opens is an inotify instance, and a Linux host
allows each user 128 of them. On a k3s or RKE2 node almost everything runs as
root - kubelet, containerd, the pods - so a busy node runs out, and whatever
starts next cannot watch files: macvtap's device plugin crashed at start on
k3s-test with 161 in use. Kubernetes distributions raise the limits for this
reason; k3s and RKE2 leave them at the kernel's defaults.

On each k3s or RKE2 node the leader raises them once - never lowers them -
to 8192 instances and 524288 watches, now and in /etc/sysctl.d, so they hold
after a reboot. The installer writes the same file on a new machine.
"""
import json
import os
import time

import homestead_shared as SHARED

kget = hostrun = platform = None
DATA_DIR = "/data"
INSTANCES = 8192
WATCHES = 524288
CONF = "/etc/sysctl.d/90-homestead.conf"


def bind(_kget, _hostrun, _platform, data_dir="/data"):
    global kget, hostrun, platform, DATA_DIR
    kget, hostrun, platform, DATA_DIR = _kget, _hostrun, _platform, data_dir


def _path():
    return os.path.join(DATA_DIR, "host-limits.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def script():
    return f"""I=$(cat /proc/sys/fs/inotify/max_user_instances); W=$(cat /proc/sys/fs/inotify/max_user_watches)
NI=$I; [ "$NI" -lt {INSTANCES} ] && NI={INSTANCES}
NW=$W; [ "$NW" -lt {WATCHES} ] && NW={WATCHES}
if [ "$NI" = "$I" ] && [ "$NW" = "$W" ]; then echo "KEPT $I $W"; exit 0; fi
printf 'fs.inotify.max_user_instances = %s\\nfs.inotify.max_user_watches = %s\\n' "$NI" "$NW" > {CONF}
sysctl -q -w fs.inotify.max_user_instances=$NI fs.inotify.max_user_watches=$NW
echo "RAISED $I $W $NI $NW"
"""


def tick():
    """Each Ready node not done yet. Returns {node: "from -> to"} for those raised now."""
    p = platform(True) or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        return {}
    state = _load()
    raised = {}
    for node in (kget("/api/v1/nodes") or {}).get("items", []):
        name = node["metadata"]["name"]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (node.get("status") or {}).get("conditions") or [])
        if not ready or name in state:
            continue
        out, err = hostrun.run(name, script(), timeout=60)
        words = out.split()
        if not words or words[0] not in ("KEPT", "RAISED"):
            raise ValueError(f"could not check {name}'s inotify limits: {(err or out)[:200]}")
        state[name] = {"at": int(time.time()), "result": " ".join(words[:5])}
        if words[0] == "RAISED":
            raised[name] = f"{words[1]} instances, {words[2]} watches -> {words[3]}, {words[4]}"
        SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    return raised
