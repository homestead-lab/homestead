"""Stop k3s and RKE2 re-applying what the installer left behind.

Installers before 2.8.225 dropped Homestead's manifest - and Longhorn's
HelmChart, and KubeVirt's and CDI's operators and settings - into the
server's auto-deploy folder (/var/lib/rancher/<k3s|rke2>/server/manifests).
k3s applies every file there again each time it starts, so a reboot put
Homestead back to the version it was installed at, and would take Longhorn,
KubeVirt and CDI back too, undoing upgrades and settings made since.

Homestead manages all of those now, so on each server node the leader marks
those files with a .skip beside them, which k3s reads as "leave this file
alone". Nothing already deployed is removed - k3s only cleans up after a
component disabled with --disable, never after a skipped file - and the
files themselves are left in place. Each node is done once; what was done is
kept in /data and logged.
"""
import json
import os
import time

import homestead_shared as SHARED

kget = None
hostrun = None           # homestead_hostrun
platform = None          # force -> homestead_platform.detect
DATA_DIR = "/data"
FILES = ("homestead.yaml", "longhorn.yaml", "kubevirt-operator.yaml", "kubevirt-cr.yaml",
         "cdi-operator.yaml", "cdi-cr.yaml")
DIRS = ("/var/lib/rancher/k3s/server/manifests", "/var/lib/rancher/rke2/server/manifests")
CONTROL = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")


def bind(_kget, _hostrun, _platform, data_dir="/data"):
    global kget, hostrun, platform, DATA_DIR
    kget, hostrun, platform, DATA_DIR = _kget, _hostrun, _platform, data_dir


def _path():
    return os.path.join(DATA_DIR, "manifests-skipped.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def script():
    """Mark each installer file that is there and not marked yet; print it."""
    files = " ".join(FILES)
    dirs = " ".join(DIRS)
    return (f"for d in {dirs}; do [ -d \"$d\" ] || continue; "
            f"for f in {files}; do "
            "if [ -f \"$d/$f\" ] && [ ! -e \"$d/$f.skip\" ]; then touch \"$d/$f.skip\" && echo \"$d/$f\"; fi; "
            "done; done; echo done")


def servers():
    out = []
    for node in (kget("/api/v1/nodes") or {}).get("items", []):
        labels = (node.get("metadata") or {}).get("labels") or {}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (node.get("status") or {}).get("conditions") or [])
        if any(key in labels for key in CONTROL):
            out.append((node["metadata"]["name"], ready))
    return out


def tick():
    """Each Ready server node not done yet: mark its installer files. Returns
    {node: [files marked]} for the nodes done now."""
    p = platform(True) or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        return {}
    state = _load()
    done_now = {}
    for node, ready in servers():
        if not ready or node in state:
            continue
        out, err = hostrun.run(node, script(), timeout=60)
        if "done" not in out.split():
            raise ValueError(f"could not check {node}'s auto-deploy folder: {(err or out)[:200]}")
        marked = [line for line in out.splitlines() if line.startswith("/")]
        state[node] = {"at": int(time.time()), "marked": marked}
        done_now[node] = marked
        SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    return done_now
