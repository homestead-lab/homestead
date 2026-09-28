"""What Homestead needs under it on k3s and RKE2, installed when it is not there.

Harvester brings the two network parts Homestead builds on: kube-vip, which
gives apps VIPs that move between nodes, and Multus, which gives a VM or a
container an address of its own on the LAN. k3s and RKE2 bring neither, so
Homestead installs them - the same HelmCharts as Settings > Cluster >
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
PARTS = ("kube-vip", "multus")
NAMES = {"kube-vip": "kube-vip", "multus": "Multus"}
WHY = {"kube-vip": "VIPs: an address per app that moves to another node if its node goes down",
       "multus": "LAN addresses of their own for VMs and containers, on a bridged network"}
_lock = threading.Lock()


def bind(_kget, _addons, _platform, default_ns="lab", data_dir="/data"):
    global kget, addons, platform, DEFAULT_NS, DATA_DIR
    kget, addons, platform, DEFAULT_NS, DATA_DIR = _kget, _addons, _platform, default_ns, data_dir


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


def parts(p, status):
    """Each part: whether it is there, on its way, or missing."""
    out = []
    kube_vip, multus = status.get("kube_vip") or {}, status.get("multus") or {}
    for part in PARTS:
        if part == "kube-vip":
            # MetalLB gives VIPs too: then kube-vip is not needed.
            installed = p.get("load_balancer") in ("kube-vip", "metallb")
            installing = bool(kube_vip.get("installing"))
            by = "MetalLB" if p.get("load_balancer") == "metallb" else "kube-vip"
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
            raise ValueError("Homestead installs kube-vip and Multus on k3s and RKE2; this cluster "
                             + ("has them from Harvester" if p.get("harvester") else "needs them installed by hand"))
        rows = {row["id"]: row for row in parts(p, addons.status())}
        wanted = [part for part in (which or PARTS) if part in rows and rows[part]["missing"]]
        state = _load()
        done, results = state.setdefault("done", {}), []
        for part in wanted:
            cfg = {"version": versions[part]} if versions.get(part) else {}
            try:
                result = (addons.install_kube_vip if part == "kube-vip" else addons.install_multus)(cfg)
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
    pending = [part for part in wanted if part not in done]
    if not pending:
        return []
    results = install(pending, wanted, reason="installer")
    for row in results:
        print(f"platform: {'installing' if row['ok'] else 'could not install'} {NAMES[row['id']]} "
              f"as the installer asked: {row['detail']}", flush=True)
    # Marked as seen even when already there, so it is not asked again.
    state = _load()
    for part in pending:
        state.setdefault("done", {}).setdefault(part, {"at": int(time.time()), "version": "", "reason": "present",
                                                       "error": ""})
    _save(state)
    return results
