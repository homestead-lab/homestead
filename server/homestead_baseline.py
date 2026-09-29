"""What Homestead needs under it on k3s and RKE2, installed when it is not there.

Harvester brings the network parts Homestead builds on: kube-vip, which
gives apps VIPs that move between nodes, Multus, which gives a VM or a
container an address of its own on the LAN, and host bridges VMs join. k3s
and RKE2 bring none of them, so Homestead installs the first two, and - once
KubeVirt is there - macvtap, which puts a VM on the LAN through a host's NIC
(homestead_macvtap). kube-vip and Multus are - the same HelmCharts as Settings > Cluster >
Add-ons, at the chart versions Homestead has tested (homestead_addons), and
upgraded afterwards under System > Cluster > Platform versions.

A new installation asks for them: the installer leaves a ConfigMap,
homestead-install, naming the parts and, if chosen, their versions, and the
leader installs whatever is missing once the cluster can say what it has.
What was done is kept in /data, so a part someone removes on purpose is not
put back. An installation from before this asks once, on Add-ons and the
Networking page, rather than changing every node's network by itself.
"""
import json
import os
import threading
import time

import homestead_shared as SHARED

kget = None
addons = None            # homestead_addons
platform = None          # force -> homestead_platform.detect
DEFAULT_NS = "lab"
DATA_DIR = "/data"
REQUEST = "homestead-install"
PARTS = ("kube-vip", "multus", "macvtap")
NAMES = {"kube-vip": "kube-vip", "multus": "Multus", "macvtap": "macvtap"}
WHY = {"kube-vip": "Virtual IP addresses for applications, with failover between nodes",
       "multus": "Dedicated LAN addresses for virtual machines and containers",
       "macvtap": "LAN addresses for virtual machines on a host's own network interface"}
macvtap = None           # homestead_macvtap
_lock = threading.Lock()


def bind(_kget, _addons, _platform, default_ns="lab", data_dir="/data", _macvtap=None):
    global kget, addons, platform, DEFAULT_NS, DATA_DIR, macvtap
    kget, addons, platform, DEFAULT_NS, DATA_DIR = _kget, _addons, _platform, default_ns, data_dir
    macvtap = _macvtap


def _path():
    return os.path.join(DATA_DIR, "baseline.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    SHARED.write_json(_path(), state, indent=1, sort_keys=True)


def applies(p):
    """Only where Homestead can install them: k3s or RKE2 with its Helm
    controller. Harvester has both; elsewhere they are installed by hand."""
    return (not p.get("harvester") and p.get("distribution") in ("k3s", "rke2")
            and bool(p.get("helm_controller")))


def _needed(p):
    """The parts this cluster needs: macvtap only where VMs run."""
    return [part for part in PARTS if part != "macvtap" or (p.get("kubevirt") and macvtap is not None)]


def parts(p, status):
    """Each part: whether it is there, on its way, or missing."""
    out = []
    kube_vip, multus = status.get("kube_vip") or {}, status.get("multus") or {}
    for part in _needed(p):
        if part == "kube-vip":
            # MetalLB gives VIPs too: then kube-vip is not needed.
            installed = p.get("load_balancer") in ("kube-vip", "metallb")
            installing = bool(kube_vip.get("installing"))
            by = "MetalLB" if p.get("load_balancer") == "metallb" else "kube-vip"
        elif part == "macvtap":
            state = macvtap.inspect()
            installed, installing, by = state["ready"], state["installed"] and not state["ready"], "macvtap"
        else:
            installed = bool(multus.get("ready") or multus.get("installed"))
            installing = bool(multus.get("installing"))
            by = "Multus"
        out.append({"id": part, "name": NAMES[part], "why": WHY[part], "installed": installed,
                    "installing": installing and not installed, "by": by if installed else "",
                    "missing": not installed and not installing})
    return out


def requested():
    """What the installer asked for, from its ConfigMap: {part: version or ""}."""
    try:
        data = (kget(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{REQUEST}") or {}).get("data") or {}
    except Exception:
        return {}
    return {part: str(data.get(f"{part}-version") or "") for part in PARTS
            if str(data.get(part, "")).lower() in ("yes", "true", "1", "install")}


def report():
    p = platform(True) or {}
    state = _load()
    if not applies(p):
        return {"applies": False, "parts": [], "missing": [], "distribution": p.get("distribution", ""),
                "harvester": bool(p.get("harvester")), "done": state.get("done", {})}
    rows = parts(p, addons.status())
    return {"applies": True, "parts": rows, "missing": [row["id"] for row in rows if row["missing"]],
            "distribution": p.get("distribution", ""), "harvester": False,
            "done": state.get("done", {}), "requested": sorted(requested())}


def install(which=None, versions=None, reason="asked"):
    """Install the missing parts named (all missing ones when None), each at
    its tested chart version unless one is given. Returns what happened."""
    versions = versions or {}
    with _lock:
        p = platform(True) or {}
        if not applies(p):
            raise ValueError("Homestead installs kube-vip, Multus and macvtap on k3s and RKE2; this cluster "
                             + ("has them from Harvester" if p.get("harvester") else "needs them installed by hand"))
        rows = {row["id"]: row for row in parts(p, addons.status())}
        wanted = [part for part in (which or PARTS) if part in rows and rows[part]["missing"]]
        installers = {"kube-vip": addons.install_kube_vip, "multus": addons.install_multus,
                      "macvtap": getattr(macvtap, "install", None)}
        state = _load()
        done, results = state.setdefault("done", {}), []
        for part in wanted:
            cfg = {"version": versions[part]} if versions.get(part) else {}
            try:
                result = installers[part](cfg)
                done[part] = {"at": int(time.time()), "version": cfg.get("version", ""), "reason": reason, "error": ""}
                results.append({"id": part, "ok": True, "detail": result.get("detail", ""), "job": result.get("job", "")})
            except Exception as error:
                done[part] = {"at": int(time.time()), "version": cfg.get("version", ""), "reason": reason,
                              "error": str(error)[:240]}
                results.append({"id": part, "ok": False, "detail": str(error)[:240]})
        _save(state)
        return results


def tick():
    """The installer asked, and something asked for is neither there nor
    tried: install it. A part tried before, or already there, is left be."""
    wanted = requested()
    if not wanted:
        return []
    p = platform(True) or {}
    if not applies(p):
        return []
    done = _load().get("done", {})
    # macvtap waits for KubeVirt: asked for with Multus, done once VMs can run.
    if "multus" in wanted and "macvtap" not in wanted:
        wanted["macvtap"] = ""
    needed = _needed(p)
    pending = [part for part in wanted if part not in done and part in needed]
    if not pending:
        return []
    results = install(pending, wanted, reason="installer")
    for row in results:
        print(f"platform: {NAMES[row['id']]} (requested at installation): "
              f"{'installing' if row['ok'] else 'installation failed'}: {row['detail']}", flush=True)
    # Marked as seen even when already there, so it is not asked again.
    state = _load()
    for part in pending:
        state.setdefault("done", {}).setdefault(part, {"at": int(time.time()), "version": "", "reason": "present",
                                                       "error": ""})
    _save(state)
    return results
