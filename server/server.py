#!/usr/bin/env python3
"""
HarvUI - a friendly control panel for Harvester / Longhorn / KubeVirt.
Pure Python stdlib: no pip install at runtime, so it starts even with no internet.
"""
import json, os, re, ssl, sys, time, threading, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
TOKEN = open(f"{SA}/token").read().strip() if os.path.exists(f"{SA}/token") else ""
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt") if os.path.exists(f"{SA}/ca.crt") else ssl._create_unverified_context()
WEBROOT = os.environ.get("WEBROOT", "/web")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
SMB_NAMESPACE = os.environ.get("SMB_NAMESPACE", "lab")
DEFAULT_NS = os.environ.get("DEFAULT_NS", "lab")
STORAGE_CLASS = os.environ.get("STORAGE_CLASS", "longhorn-r2")
LB_IP = os.environ.get("LB_IP", "")

SYS_NS = {
    "kube-system", "kube-public", "kube-node-lease", "harvester-system", "harvester-public",
    "longhorn-system", "cattle-system", "cattle-dashboards", "cattle-fleet-system",
    "cattle-fleet-local-system", "cattle-fleet-clusters-system", "cattle-monitoring-system",
    "cattle-logging-system", "cattle-provisioning-capi-system", "cattle-ui-plugin-system",
    "cattle-capi-system", "cattle-turtles-system", "fleet-local", "local", "cdi", "kube-ovn",
}

_cache = {}
_lock = threading.Lock()

# rolling time-series so the UI can draw real sparklines (not decoration)
HIST_MAX = 120
HIST = {"t": [], "cpu": [], "mem": [], "wl_pods": [], "sys_pods": [], "vol_bad": [],
        "net_rx": [], "net_tx": []}
_RATE = {}   # key -> (counter, timestamp) for per-node byte counters


def rate(key, value, now=None):
    now = now or time.time()
    prev = _RATE.get(key)
    _RATE[key] = (value, now)
    if not prev or now <= prev[1] or value < prev[0]:
        return 0.0
    return (value - prev[0]) / (now - prev[1])


def _sampler():
    while True:
        try:
            o = get_overview()
            with _lock:
                HIST["t"].append(int(time.time()))
                HIST["cpu"].append(o["cpu_pct"])
                HIST["mem"].append(o["mem_pct"])
                HIST["wl_pods"].append(o["workload_pods"])
                HIST["sys_pods"].append(o["system_pods"])
                HIST["vol_bad"].append(o["vol_degraded"] + o["vol_faulted"])
                HIST["net_rx"].append(round(sum(n.get("rx_mbps", 0) for n in o["nodes"]), 2))
                HIST["net_tx"].append(round(sum(n.get("tx_mbps", 0) for n in o["nodes"]), 2))
                for k in HIST:
                    if len(HIST[k]) > HIST_MAX:
                        HIST[k] = HIST[k][-HIST_MAX:]
        except Exception:
            pass
        time.sleep(30)


def kget(path, timeout=10):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ksend(method, path, body=None, ctype="application/json", timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": ctype})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and time.time() - e[0] < ttl:
            return e[1]
    try:
        v = fn()
    except Exception as ex:
        with _lock:
            e = _cache.get(key)
        if e:
            return e[1]
        raise ex
    with _lock:
        _cache[key] = (time.time(), v)
    return v


def age_secs(ts):
    if not ts:
        return 0
    try:
        t = time.strptime(ts.replace("Z", "GMT"), "%Y-%m-%dT%H:%M:%S%Z")
        import calendar
        return max(0, int(time.time() - calendar.timegm(t)))
    except Exception:
        return 0


def parse_cpu(s):
    if not s: return 0.0
    s = str(s)
    if s.endswith("n"): return float(s[:-1]) / 1e9
    if s.endswith("u"): return float(s[:-1]) / 1e6
    if s.endswith("m"): return float(s[:-1]) / 1e3
    try: return float(s)
    except: return 0.0


def parse_mem(s):
    if not s: return 0
    s = str(s)
    mult = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
            "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    for suf, m in mult.items():
        if s.endswith(suf):
            try: return int(float(s[:-len(suf)]) * m)
            except: return 0
    try: return int(float(s))
    except: return 0


# ---------------------------------------------------------------- collectors
def node_stats(name):
    """Per-node network + filesystem counters from the kubelet summary API."""
    try:
        s = kget(f"/api/v1/nodes/{name}/proxy/stats/summary", timeout=8)
    except Exception:
        return {}
    nd = s.get("node", {}) or {}
    net = nd.get("network", {}) or {}
    ifaces = net.get("interfaces") or []
    pick = next((i for i in ifaces if i.get("name") == "mgmt-br"), None) or            next((i for i in ifaces if (i.get("rxBytes") or 0) > 0), None) or {}
    rx, tx = pick.get("rxBytes") or 0, pick.get("txBytes") or 0
    now = time.time()
    fs = nd.get("fs", {}) or {}
    runtime = (s.get("node", {}).get("runtime", {}) or {}).get("imageFs", {}) or {}
    return {
        "net_iface": pick.get("name", ""),
        "rx_mbps": round(rate(f"{name}:rx", rx, now) * 8 / 1e6, 2),
        "tx_mbps": round(rate(f"{name}:tx", tx, now) * 8 / 1e6, 2),
        "rx_total_gb": round(rx / 1024**3, 1),
        "tx_total_gb": round(tx / 1024**3, 1),
        "fs_used_gb": round((fs.get("usedBytes") or 0) / 1024**3, 1),
        "fs_cap_gb": round((fs.get("capacityBytes") or 0) / 1024**3, 1),
        "fs_pct": round((fs.get("usedBytes") or 0) / (fs.get("capacityBytes") or 1) * 100, 1),
        "img_used_gb": round((runtime.get("usedBytes") or 0) / 1024**3, 1),
    }


def get_nodes():
    nodes = kget("/api/v1/nodes")
    try:
        metrics = {m["metadata"]["name"]: m for m in kget("/apis/metrics.k8s.io/v1beta1/nodes").get("items", [])}
    except Exception:
        metrics = {}
    pods = kget("/api/v1/pods")
    try:
        vmis = kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
    except Exception:
        vmis = []

    out = []
    for n in nodes.get("items", []):
        name = n["metadata"]["name"]
        labels = n["metadata"].get("labels", {})
        cap = n["status"]["capacity"]
        conds = {c["type"]: c["status"] for c in n["status"].get("conditions", [])}
        roles = sorted([k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")])
        m = metrics.get(name, {})
        ucpu = parse_cpu(m.get("usage", {}).get("cpu"))
        umem = parse_mem(m.get("usage", {}).get("memory"))
        ccpu = float(cap.get("cpu", 1))
        cmem = parse_mem(cap.get("memory"))
        npods = [p for p in pods.get("items", []) if p.get("spec", {}).get("nodeName") == name]
        wl = sorted({p["metadata"].get("labels", {}).get("app") or p["metadata"]["name"].rsplit("-", 2)[0]
                     for p in npods if p["metadata"]["namespace"] not in SYS_NS})
        out.append({
            "name": name,
            "status": "Ready" if conds.get("Ready") == "True" else "NotReady",
            "roles": roles or ["worker"],
            "cpu_pct": round(ucpu / ccpu * 100, 1) if ccpu else 0,
            "cpu_used": round(ucpu, 2), "cpu_cap": ccpu,
            "mem_pct": round(umem / cmem * 100, 1) if cmem else 0,
            "mem_used_gb": round(umem / 1024**3, 1), "mem_cap_gb": round(cmem / 1024**3, 1),
            "pods": len(npods),
            "pods_sys": len([p for p in npods if p["metadata"]["namespace"] in SYS_NS]),
            "pods_wl": len([p for p in npods if p["metadata"]["namespace"] not in SYS_NS]),
            "vms": len([v for v in vmis if v.get("status", {}).get("nodeName") == name]),
            "igpu": labels.get("hardware/igpu") == "true",
            "workloads": wl,
            "kernel": n["status"].get("nodeInfo", {}).get("kernelVersion", ""),
            "os": n["status"].get("nodeInfo", {}).get("osImage", ""),
            "schedulable": not n.get("spec", {}).get("unschedulable", False),
            "addresses": {a["type"]: a["address"] for a in n["status"].get("addresses", [])},
            "info": n["status"].get("nodeInfo", {}),
            "conditions": [{"type": c["type"], "status": c["status"], "reason": c.get("reason", "")}
                           for c in n["status"].get("conditions", [])],
            "created": n["metadata"].get("creationTimestamp", ""),
            **node_stats(name),
        })
    return out


def get_volumes():
    try:
        vols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return []
    out = []
    for v in vols:
        st = v.get("status", {})
        sp = v.get("spec", {})
        ks = st.get("kubernetesStatus", {}) or {}
        wls = ks.get("workloadsStatus") or []
        out.append({
            "name": v["metadata"]["name"],
            "pvc_name": ks.get("pvcName", ""),
            "namespace": ks.get("namespace", ""),
            "attached_to": ", ".join(sorted({w.get("workloadName") or w.get("podName") or ""
                                             for w in wls if w.get("workloadName") or w.get("podName")})),
            "pod_status": ", ".join(sorted({w.get("podStatus", "") for w in wls if w.get("podStatus")})),
            "last_used": ks.get("lastPodRefAt") or ks.get("lastPVCRefAt") or "",
            "last_used_secs": age_secs(ks.get("lastPodRefAt") or ks.get("lastPVCRefAt") or ""),
            "created": v["metadata"].get("creationTimestamp", ""),
            "state": st.get("state", "?"),
            "robustness": st.get("robustness", "?"),
            "node": st.get("currentNodeID", ""),
            "size_gb": round(int(sp.get("size", 0) or 0) / 1024**3, 1),
            "replicas": sp.get("numberOfReplicas", 0),
            "actual_gb": round(int(st.get("actualSize", 0) or 0) / 1024**3, 2),
            "pvc": v["metadata"].get("annotations", {}).get("longhorn.io/volume-scheduling-error", "") or "",
        })
    return sorted(out, key=lambda x: x["name"])


def get_workloads():
    deps = kget("/apis/apps/v1/deployments").get("items", [])
    pods = kget("/api/v1/pods").get("items", [])
    try:
        pm = {(m["metadata"]["namespace"], m["metadata"]["name"]): m
              for m in kget("/apis/metrics.k8s.io/v1beta1/pods").get("items", [])}
    except Exception:
        pm = {}
    try:
        svcs = kget("/api/v1/services").get("items", [])
    except Exception:
        svcs = []

    out = []
    for d in deps:
        ns, name = d["metadata"]["namespace"], d["metadata"]["name"]
        if ns in SYS_NS:
            continue
        sel = d["spec"].get("selector", {}).get("matchLabels", {})
        mine = [p for p in pods if p["metadata"]["namespace"] == ns and
                all(p["metadata"].get("labels", {}).get(k) == v for k, v in sel.items())]
        cpu = mem = 0.0
        for p in mine:
            m = pm.get((ns, p["metadata"]["name"]))
            if m:
                for c in m.get("containers", []):
                    cpu += parse_cpu(c.get("usage", {}).get("cpu"))
                    mem += parse_mem(c.get("usage", {}).get("memory"))
        ports = []
        for s in svcs:
            if s["metadata"]["namespace"] != ns: continue
            ssel = s["spec"].get("selector") or {}
            if ssel and all(sel.get(k) == v for k, v in ssel.items()):
                ip = ""
                ing = s.get("status", {}).get("loadBalancer", {}).get("ingress", [])
                if ing: ip = ing[0].get("ip", "")
                for pt in s["spec"].get("ports", []):
                    ports.append({"port": pt.get("port"), "ip": ip, "name": pt.get("name", "")})
        starts = [p["status"].get("startTime") for p in mine if p["status"].get("startTime")]
        uptime = max([age_secs(x) for x in starts], default=0) if starts else 0
        st = d.get("status", {})
        out.append({
            "ns": ns, "name": name, "uptime": uptime,
            "ready": st.get("readyReplicas", 0) or 0,
            "desired": d["spec"].get("replicas", 0) or 0,
            "images": [c["image"] for c in d["spec"]["template"]["spec"].get("containers", [])],
            "nodes": sorted({p["spec"].get("nodeName", "") for p in mine if p["spec"].get("nodeName")}),
            "pods": [{"name": p["metadata"]["name"], "phase": p["status"].get("phase"),
                      "node": p["spec"].get("nodeName", ""),
                      "uptime": age_secs(p["status"].get("startTime")),
                      "restarts": sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []) or [])}
                     for p in mine],
            "cpu": round(cpu, 3), "mem_mb": round(mem / 1024**2, 1),
            "ports": ports,
            "gpu": d["spec"]["template"]["spec"].get("nodeSelector", {}).get("hardware/igpu") == "true",
        })
    return sorted(out, key=lambda x: (x["ns"], x["name"]))


def get_overview():
    nodes = get_nodes()
    wl = get_workloads()
    vols = get_volumes()
    pods = kget("/api/v1/pods").get("items", [])
    sysp = [p for p in pods if p["metadata"]["namespace"] in SYS_NS]
    usrp = [p for p in pods if p["metadata"]["namespace"] not in SYS_NS]
    deg = [v for v in vols if v["robustness"] == "degraded"]
    flt = [v for v in vols if v["robustness"] == "faulted"]
    down = [n for n in nodes if n["status"] != "Ready"]
    health = "critical" if (down or flt) else ("degraded" if (deg or any(p["status"].get("phase") in ("Failed", "Pending") for p in usrp)) else "healthy")
    tcap = sum(n["cpu_cap"] for n in nodes) or 1
    tuse = sum(n["cpu_used"] for n in nodes)
    mcap = sum(n["mem_cap_gb"] for n in nodes) or 1
    muse = sum(n["mem_used_gb"] for n in nodes)
    return {
        "health": health,
        "nodes": nodes,
        "nodes_ready": len([n for n in nodes if n["status"] == "Ready"]),
        "nodes_total": len(nodes),
        "workloads": len(wl),
        "workload_pods": len(usrp),
        "system_pods": len(sysp),
        "volumes": len(vols), "vol_degraded": len(deg), "vol_faulted": len(flt),
        "cpu_pct": round(tuse / tcap * 100, 1),
        "mem_pct": round(muse / mcap * 100, 1),
        "cpu_used": round(tuse, 2), "cpu_cap": tcap,
        "mem_used_gb": round(muse, 1), "mem_cap_gb": round(mcap, 1),
        "top_cpu": sorted(wl, key=lambda x: -x["cpu"])[:6],
        "top_mem": sorted(wl, key=lambda x: -x["mem_mb"])[:6],
        "lb_ip": LB_IP,
    }


def get_events():
    try:
        ev = kget("/api/v1/events?limit=60")
    except Exception:
        return []
    items = ev.get("items", [])
    items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
    return [{
        "ns": e["metadata"]["namespace"],
        "obj": e.get("involvedObject", {}).get("name", ""),
        "kind": e.get("involvedObject", {}).get("kind", ""),
        "reason": e.get("reason", ""),
        "msg": (e.get("message") or "")[:160],
        "type": e.get("type", "Normal"),
        "time": e.get("lastTimestamp") or e.get("eventTime") or "",
    } for e in items[:40]]


def get_flow2():
    """Architecture view: node(replica copies) -> volume -> workload(+ports) -> VIP."""
    pods = [p for p in kget("/api/v1/pods").get("items", [])
            if p["metadata"]["namespace"] not in SYS_NS]
    svcs = [s for s in kget("/api/v1/services").get("items", [])
            if s["metadata"]["namespace"] not in SYS_NS]
    try:
        lhvols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        lhvols = []
    try:
        lhreps = kget("/apis/longhorn.io/v1beta2/replicas").get("items", [])
    except Exception:
        lhreps = []
    try:
        vmis = kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
    except Exception:
        vmis = []

    # --- volumes, keyed by their PVC name where possible
    vols, vol_by_pvc = [], {}
    for v in lhvols:
        st = v.get("status", {}) or {}
        ks = st.get("kubernetesStatus", {}) or {}
        pvc = ks.get("pvcName") or ""
        vid = v["metadata"]["name"]
        entry = {
            "id": "v:" + vid, "name": pvc or vid[:18], "raw": vid, "pvc": pvc,
            "replicas": v.get("spec", {}).get("numberOfReplicas", 0),
            "robustness": st.get("robustness", "unknown"),
            "state": st.get("state", ""),
            "size_gb": round(int(v.get("spec", {}).get("size", 0) or 0) / 1024**3, 1),
            "attached": st.get("currentNodeID", ""),
        }
        vols.append(entry)
        if pvc:
            vol_by_pvc[pvc] = entry

    # --- replica copies grouped by node
    nodes = {}
    for r in lhreps:
        sp = r.get("spec", {}) or {}
        nid, vn = sp.get("nodeID"), sp.get("volumeName")
        if not nid or not vn:
            continue
        v = next((x for x in vols if x["raw"] == vn), None)
        nodes.setdefault(nid, []).append({
            "vol": v["name"] if v else vn[:16], "vid": "v:" + vn,
            "running": (r.get("status", {}) or {}).get("currentState") == "running",
        })
    if not nodes:  # fallback when replica CRs are unreadable
        for v in vols:
            if v["attached"]:
                nodes.setdefault(v["attached"], []).append(
                    {"vol": v["name"], "vid": v["id"], "running": True})

    # --- ports & VIPs per app
    ports_by_app, vips = {}, {}
    for s in svcs:
        app = (s["spec"].get("selector") or {}).get("app")
        if not app:
            continue
        ing = s.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
        vip = ing[0].get("ip") if ing else None
        for prt in s["spec"].get("ports", []) or []:
            rec = {"port": prt.get("port"), "name": prt.get("name") or "tcp", "vip": vip}
            ports_by_app.setdefault(app, []).append(rec)
            if vip:
                vips.setdefault(vip, []).append({"port": prt.get("port"), "app": app})

    # --- per-pod live metrics for the architecture cards
    try:
        pmet = {}
        for m in kget("/apis/metrics.k8s.io/v1beta1/pods").get("items", []):
            c = sum(parse_cpu(x.get("usage", {}).get("cpu")) for x in m.get("containers", []))
            mm = sum(parse_mem(x.get("usage", {}).get("memory")) for x in m.get("containers", []))
            pmet[(m["metadata"]["namespace"], m["metadata"]["name"])] = (c, mm)
    except Exception:
        pmet = {}

    # --- workloads
    seen, wls = set(), []
    for p in pods:
        app = p["metadata"].get("labels", {}).get("app") or p["metadata"]["name"].rsplit("-", 2)[0]
        if app in seen:
            continue
        seen.add(app)
        claims = []
        for vol in p["spec"].get("volumes", []) or []:
            cn = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if cn:
                claims.append({"pvc": cn, "vid": vol_by_pvc[cn]["id"] if cn in vol_by_pvc else ""})
        cu, mu = pmet.get((p["metadata"]["namespace"], p["metadata"]["name"]), (0, 0))
        wls.append({
            "id": "w:" + app, "name": app, "kind": "container",
            "node": p["spec"].get("nodeName", ""),
            "phase": p["status"].get("phase", ""),
            "uptime": age_secs(p["status"].get("startTime")),
            "cpu": round(cu, 3), "mem_mb": round(mu / 1048576, 1),
            "ns": p["metadata"]["namespace"],
            "image": (p["spec"].get("containers") or [{}])[0].get("image", ""),
            "gpu": any("dri" in (m.get("mountPath") or "")
                       for c in p["spec"].get("containers", []) for m in (c.get("volumeMounts") or [])),
            "claims": claims, "ports": ports_by_app.get(app, []),
        })
    for v in vmis:
        nm = v["metadata"]["name"]
        wls.append({"id": "w:vm-" + nm, "name": nm, "kind": "vm", "ns": v["metadata"]["namespace"],
                    "node": v.get("status", {}).get("nodeName", ""), "phase": v.get("status", {}).get("phase", ""),
                    "uptime": 0, "cpu": 0, "mem_mb": 0,
                    "image": "", "gpu": False, "claims": [], "ports": []})

    return {
        "nodes": [{"id": "n:" + k, "name": k, "copies": sorted(v, key=lambda x: x["vol"])}
                  for k, v in sorted(nodes.items())],
        "volumes": sorted(vols, key=lambda x: x["name"]),
        "workloads": sorted(wls, key=lambda x: x["name"]),
        "vips": [{"id": "i:" + ip, "ip": ip, "ports": sorted(p, key=lambda x: x["port"])}
                 for ip, p in vips.items()],
    }


def get_flow():
    """The real data path: replicas -> volume -> PVC -> workload -> port -> VIP."""
    pods = [p for p in kget("/api/v1/pods").get("items", [])
            if p["metadata"]["namespace"] not in SYS_NS]
    svcs = [s for s in kget("/api/v1/services").get("items", [])
            if s["metadata"]["namespace"] not in SYS_NS]
    try:
        lhvols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        lhvols = []
    try:
        vmis = kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
    except Exception:
        vmis = []

    nodecol, volcol, pvccol, wlcol, portcol, vipcol = {}, {}, {}, {}, {}, {}
    links = {}
    meta = {}

    def link(a, b, v=1):
        links[(a, b)] = links.get((a, b), 0) + v

    # longhorn volume -> pvc, and replica placement -> volume
    pvc_of_vol = {}
    for v in lhvols:
        name = v["metadata"]["name"]
        st = v.get("status", {})
        ks = st.get("kubernetesStatus", {}) or {}
        pvc = ks.get("pvcName")
        reps = v.get("spec", {}).get("numberOfReplicas", 0)
        rob = st.get("robustness", "?")
        short = (pvc or name)[:22]
        volcol[short] = reps
        meta["v:" + short] = {"robustness": rob, "replicas": reps,
                              "size_gb": round(int(v.get("spec", {}).get("size", 0) or 0) / 1024**3, 1),
                              "node": st.get("currentNodeID", "")}
        for nid in {r.get("nodeID") for r in (st.get("replicaStatus") or []) if r.get("nodeID")} or \
                   ({st.get("currentNodeID")} if st.get("currentNodeID") else set()):
            nodecol[nid] = nodecol.get(nid, 0) + 1
            link("n:" + nid, "v:" + short)
        if pvc:
            pvc_of_vol[pvc] = short
            pvccol[pvc] = pvccol.get(pvc, 0) + 1
            link("v:" + short, "c:" + pvc)

    # workloads (pods + VMs) and what they mount
    for p in pods:
        app = p["metadata"].get("labels", {}).get("app") or p["metadata"]["name"].rsplit("-", 2)[0]
        wlcol[app] = wlcol.get(app, 0) + 1
        meta["w:" + app] = {"kind": "container", "node": p["spec"].get("nodeName", ""),
                            "phase": p["status"].get("phase", "")}
        for vol in p["spec"].get("volumes", []) or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                pvccol.setdefault(claim, 1)
                link("c:" + claim, "w:" + app)
    for v in vmis:
        nm = v["metadata"]["name"]
        wlcol["VM " + nm] = 1
        meta["w:VM " + nm] = {"kind": "vm", "node": v.get("status", {}).get("nodeName", "")}

    # workload -> port -> vip
    for s in svcs:
        sel = s["spec"].get("selector") or {}
        app = sel.get("app")
        if not app:
            continue
        ing = s.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
        vip = ing[0].get("ip") if ing else None
        for prt in s["spec"].get("ports", []) or []:
            label = f"{prt.get('name') or 'tcp'}:{prt.get('port')}"
            portcol[label] = prt.get("port")
            link("w:" + app, "t:" + label)
            if vip:
                vipcol[vip] = vipcol.get(vip, 0) + 1
                link("t:" + label, "i:" + vip)
                meta["i:" + vip] = {"type": s["spec"].get("type", "")}

    def col(d, pfx):
        return [{"id": pfx + k, "label": k, "value": v, "meta": meta.get(pfx + k, {})}
                for k, v in sorted(d.items(), key=lambda x: (-(x[1] if isinstance(x[1], int) else 0), x[0]))]

    return {
        "columns": [
            {"title": "Nodes (replicas)", "items": col(nodecol, "n:")},
            {"title": "Longhorn volumes", "items": col(volcol, "v:")},
            {"title": "Claims (PVC)", "items": col(pvccol, "c:")},
            {"title": "Workloads", "items": col(wlcol, "w:")},
            {"title": "Ports", "items": col(portcol, "t:")},
            {"title": "VIP", "items": col(vipcol, "i:")},
        ],
        "links": [{"from": a, "to": b, "value": v} for (a, b), v in links.items()],
        "total": len(pods) + len(vmis),
    }


def get_storage():
    """Cluster storage rollup for the dashboard: capacity, usage, replica health."""
    vols = get_volumes()
    try:
        lhnodes = kget("/apis/longhorn.io/v1beta2/nodes").get("items", [])
    except Exception:
        lhnodes = []
    cap = avail = 0
    disks = []
    for n in lhnodes:
        for did, d in (n.get("status", {}).get("diskStatus", {}) or {}).items():
            c = d.get("storageMaximum") or 0
            a = d.get("storageAvailable") or 0
            cap += c
            avail += a
            disks.append({"node": n["metadata"]["name"],
                          "cap_gb": round(c / 1024**3, 1),
                          "avail_gb": round(a / 1024**3, 1),
                          "sched_gb": round((d.get("storageScheduled") or 0) / 1024**3, 1)})
    prov = sum(v["size_gb"] for v in vols)
    used = sum(v["actual_gb"] for v in vols)
    return {
        "cap_gb": round(cap / 1024**3, 1),
        "avail_gb": round(avail / 1024**3, 1),
        "used_gb": round((cap - avail) / 1024**3, 1),
        "used_pct": round((cap - avail) / cap * 100, 1) if cap else 0,
        "provisioned_gb": round(prov, 1),
        "actual_gb": round(used, 1),
        "volumes": len(vols),
        "healthy": len([v for v in vols if v["robustness"] == "healthy"]),
        "degraded": len([v for v in vols if v["robustness"] == "degraded"]),
        "faulted": len([v for v in vols if v["robustness"] == "faulted"]),
        "attached": len([v for v in vols if v["state"] == "attached"]),
        "disks": disks,
    }


# ---------------------------------------------------------------- mutations
def build_deployment(cfg):
    name = cfg["name"]
    ns = cfg.get("namespace", DEFAULT_NS)
    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    mounts, volumes = [], []
    for i, v in enumerate(cfg.get("volumes") or []):
        vn = f"vol{i}"
        mounts.append({"name": vn, "mountPath": v["path"]})
        if v.get("type") == "host":
            volumes.append({"name": vn, "hostPath": {"path": v["source"]}})
        else:
            volumes.append({"name": vn, "persistentVolumeClaim": {"claimName": v["source"]}})
    ports = [{"containerPort": int(p["container"]), "name": (p.get("name") or f"p{p['container']}")[:15]}
             for p in cfg.get("ports") or []]
    c = {"name": name, "image": cfg["image"], "imagePullPolicy": "IfNotPresent"}
    if env: c["env"] = env
    if ports: c["ports"] = ports
    if mounts: c["volumeMounts"] = mounts
    res = {}
    if cfg.get("cpu"): res.setdefault("requests", {})["cpu"] = cfg["cpu"]
    if cfg.get("memory"): res.setdefault("requests", {})["memory"] = cfg["memory"]
    if res: c["resources"] = res
    if cfg.get("privileged"): c["securityContext"] = {"privileged": True}

    podspec = {"containers": [c]}
    if volumes: podspec["volumes"] = volumes
    if cfg.get("gpu"):
        podspec["nodeSelector"] = {"hardware/igpu": "true"}
        podspec["containers"][0].setdefault("securityContext", {})["privileged"] = True
        podspec["containers"][0].setdefault("volumeMounts", []).append({"name": "dri", "mountPath": "/dev/dri"})
        podspec.setdefault("volumes", []).append({"name": "dri", "hostPath": {"path": "/dev/dri", "type": "Directory"}})
    if cfg.get("node"):
        podspec.setdefault("nodeSelector", {})["kubernetes.io/hostname"] = cfg["node"]
    podspec["tolerations"] = [
        {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 15},
        {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 15},
    ]
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": {"app": name, "harvui.io/managed": "true"}},
        "spec": {"replicas": int(cfg.get("replicas", 1)), "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name, "lab-workload": "true"}}, "spec": podspec}},
    }
    svc = None
    exposed = [p for p in cfg.get("ports") or [] if p.get("expose")]
    if exposed:
        svc = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name, "harvui.io/managed": "true"},
                         "annotations": {"kube-vip.io/loadbalancerIPs": cfg.get("lb_ip") or LB_IP} if (cfg.get("lb_ip") or LB_IP) else {}},
            "spec": {"type": "LoadBalancer", "selector": {"app": name},
                     "ports": [{"name": (p.get("name") or f"p{p['container']}")[:15],
                                "port": int(p.get("host") or p["container"]),
                                "targetPort": int(p["container"]), "protocol": "TCP"} for p in exposed]},
        }
    return dep, svc


def create_pvc(ns, name, size_gb, sc=None):
    body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": ns, "labels": {"harvui.io/managed": "true"}},
            "spec": {"accessModes": ["ReadWriteOnce"],
                     "storageClassName": sc or STORAGE_CLASS,
                     "resources": {"requests": {"storage": f"{size_gb}Gi"}}}}
    return ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", body)


# ---------------------------------------------------------------- app store
CA_FEED = "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json"


def fetch_appstore():
    def go():
        req = urllib.request.Request(CA_FEED, headers={"User-Agent": "HarvUI/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        apps = data.get("applist", data if isinstance(data, list) else [])
        out = []
        for a in apps:
            repo = a.get("Repository") or ""
            if not repo or not a.get("Name"):
                continue
            out.append({
                "name": a.get("Name"),
                "repo": repo,
                "icon": a.get("Icon") or "",
                "desc": re.sub(r"\s+", " ", (a.get("Description") or ""))[:300],
                "cat": a.get("Category") or "",
                "web": a.get("Project") or a.get("Support") or "",
                "ports": [str(p) for p in ([a.get("Network", {})] if isinstance(a.get("Network"), dict) else [])],
                "config": a.get("Config") or [],
            })
        return out
    return cached("appstore", 3600, go)


def template_to_cfg(app):
    """Turn an Unraid CA template entry into our deploy config."""
    ports, envs, vols = [], {}, []
    cfgs = app.get("config") or []
    if isinstance(cfgs, dict):
        cfgs = [cfgs]
    for c in cfgs:
        if not isinstance(c, dict):
            continue
        typ = (c.get("@attributes", {}) or {}).get("Type") or c.get("Type") or ""
        tgt = (c.get("@attributes", {}) or {}).get("Target") or c.get("Target") or ""
        val = c.get("value") or (c.get("@attributes", {}) or {}).get("Default") or ""
        if typ == "Port" and tgt:
            try:
                ports.append({"container": int(tgt), "host": int(val or tgt), "expose": True,
                              "name": f"p{tgt}"})
            except ValueError:
                pass
        elif typ == "Variable" and tgt:
            envs[tgt] = val
        elif typ == "Path" and tgt:
            vols.append({"path": tgt, "source": "", "type": "pvc"})
    return {"name": re.sub(r"[^a-z0-9-]", "-", app["name"].lower()).strip("-")[:40],
            "image": app["repo"], "ports": ports, "env": envs, "volumes": vols}


# ---------------------------------------------------------------- SMB shares
def _shares_from_deployment():
    """Read shares already defined on the samba deployment so we never clobber them."""
    out = []
    try:
        dep = kget(f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments/samba")
    except Exception:
        return out
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]
    claim_by_path = {}
    for m in c.get("volumeMounts", []) or []:
        for v in spec.get("volumes", []) or []:
            if v["name"] == m["name"] and v.get("persistentVolumeClaim"):
                claim_by_path[m["mountPath"]] = v["persistentVolumeClaim"]["claimName"]
    args = c.get("args", []) or []
    for i, a in enumerate(args):
        if a == "-s" and i + 1 < len(args):
            parts = args[i + 1].split(";")
            if len(parts) < 2:
                continue
            name, path = parts[0], parts[1]
            guest = (parts[4] if len(parts) > 4 else "no").lower() == "yes"
            users = parts[5] if len(parts) > 5 else "lab"
            out.append({"name": name, "pvc": claim_by_path.get(path, ""), "path": path,
                        "size_gb": 0, "user": users, "public": guest, "created": "existing"})
    return [s for s in out if s["pvc"]]


def list_shares():
    known = []
    try:
        cm = kget(f"/api/v1/namespaces/{SMB_NAMESPACE}/configmaps/harvui-shares")
        known = json.loads(cm.get("data", {}).get("shares.json", "[]"))
    except Exception:
        known = []
    # merge in anything defined directly on the deployment that we don't track yet
    names = {s["name"] for s in known}
    for s in _shares_from_deployment():
        if s["name"] not in names:
            known.append(s)
    return known


def save_shares(shares):
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "harvui-shares", "namespace": SMB_NAMESPACE},
            "data": {"shares.json": json.dumps(shares, indent=2)}}
    try:
        kget(f"/api/v1/namespaces/{SMB_NAMESPACE}/configmaps/harvui-shares")
        return ksend("PUT", f"/api/v1/namespaces/{SMB_NAMESPACE}/configmaps/harvui-shares", body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/configmaps", body)
        raise


def create_share(name, size_gb, user, password, public):
    pvc = f"share-{name}"
    try:
        create_pvc(SMB_NAMESPACE, pvc, size_gb)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
    shares = [s for s in list_shares() if s["name"] != name]
    shares.append({"name": name, "pvc": pvc, "path": f"/shares/{name}", "size_gb": size_gb,
                   "user": user or "lab", "password": password, "public": bool(public),
                   "created": time.strftime("%Y-%m-%d %H:%M")})
    save_shares(shares)
    apply_samba(shares, password)
    return shares


def delete_share(name):
    shares = list_shares()
    keep = [s for s in shares if s["name"] != name]
    if len(keep) == len(shares):
        return keep
    save_shares(keep)
    apply_samba(keep)
    return keep


def apply_samba(shares, password=None):
    """Rewrite the samba deployment so each share is a mounted PVC + -s arg.

    dperson/samba -s format is:  name;path;browse;readonly;guest;users
    Getting that order wrong silently produces a share nobody can reach.
    """
    dep = kget(f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments/samba")
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]
    args = ["-p"]
    users = {}
    mounts, volumes = [], []
    for i, s in enumerate(shares):
        if not s.get("pvc"):
            continue
        mp = s.get("path") or f"/shares/{s['name']}"
        vn = f"sh{i}"
        mounts.append({"name": vn, "mountPath": mp})
        volumes.append({"name": vn, "persistentVolumeClaim": {"claimName": s["pvc"]}})
        guest = "yes" if s.get("public") else "no"
        owner = s.get("user") or "lab"
        # name ; path ; browse ; readonly ; guest ; users
        args += ["-s", f"{s['name']};{mp};yes;no;{guest};{owner}"]
        users[owner] = s.get("password") or password or "LabPass2026"
    for u, pw in users.items():
        args += ["-u", f"{u};{pw}"]
    args += ["-g", "server min protocol = SMB2"]
    c["args"] = args
    c["volumeMounts"] = mounts
    spec["volumes"] = volumes
    return ksend("PUT", f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments/samba", dep)


def raw_get(path, timeout=20):
    """Plain-text GET against the API (pod logs and similar)."""
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# expose a tiny shim module so helper modules can reach raw_get without a cycle
import types as _types
_shim = _types.ModuleType("harvui_shim")
_shim.raw_get = raw_get
sys.modules["harvui_shim"] = _shim

import harvui_lifecycle as LC
import harvui_imports as IMP
LC.bind(kget, ksend, SYS_NS, _cache)
IMP.bind(kget, ksend, create_pvc, build_deployment, DEFAULT_NS, _cache)


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._file(f"{WEBROOT}/index.html", "text/html; charset=utf-8")
            if p.startswith("/js/") and p.endswith(".js") and ".." not in p:
                return self._file(f"{WEBROOT}/{os.path.basename(p)}", "application/javascript")
            if p == "/app.js":
                return self._file(f"{WEBROOT}/app.js", "application/javascript")
            if p == "/style.css":
                return self._file(f"{WEBROOT}/style.css", "text/css")
            if p == "/healthz":
                return self._send(200, {"ok": True})
            if p == "/api/overview":
                return self._send(200, cached("ov", 5, get_overview))
            if p == "/api/nodes":
                return self._send(200, cached("nodes", 5, get_nodes))
            if p == "/api/workloads":
                return self._send(200, cached("wl", 5, get_workloads))
            if p == "/api/volumes":
                return self._send(200, cached("vol", 8, get_volumes))
            if p == "/api/events":
                return self._send(200, cached("ev", 10, get_events))
            if p == "/api/storage":
                return self._send(200, cached("stor", 10, get_storage))
            if p == "/api/node":
                return self._send(200, cached("node:" + (q.get("name") or [""])[0], 5,
                                  lambda: next((n for n in get_nodes()
                                                if n["name"] == (q.get("name") or [""])[0]), {})))
            if p == "/api/history":
                with _lock:
                    return self._send(200, {k: list(v) for k, v in HIST.items()})
            if p == "/api/flow":
                return self._send(200, cached("flow2", 8, get_flow2))
            if p == "/api/shares":
                return self._send(200, list_shares())
            if p == "/api/quorum":
                return self._send(200, LC.quorum_report())
            if p == "/api/vms":
                return self._send(200, cached("vms", 5, IMP.list_vms))
            if p == "/api/vmimages":
                return self._send(200, cached("vmimg", 30, IMP.list_vm_images))
            if p == "/api/images":
                return self._send(200, cached("imgcache", 30, IMP.image_cache))
            if p == "/api/schedules":
                return self._send(200, cached("cron", 8, IMP.list_jobs))
            if p == "/api/sources":
                return self._send(200, IMP.list_sources())
            if p == "/api/imports":
                return self._send(200, IMP.import_status())
            if p == "/api/workload":
                ns, nm = q["ns"][0], q["name"][0]
                d = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{nm}")
                c = d["spec"]["template"]["spec"]["containers"][0]
                return self._send(200, {
                    "ns": ns, "name": nm, "image": c.get("image", ""),
                    "replicas": d["spec"].get("replicas", 1),
                    "cpu": c.get("resources", {}).get("requests", {}).get("cpu", ""),
                    "memory": c.get("resources", {}).get("requests", {}).get("memory", ""),
                    "env": {e["name"]: e.get("value", "") for e in c.get("env", []) or []},
                    "ports": [{"container": x.get("containerPort"), "name": x.get("name", "")}
                              for x in c.get("ports", []) or []],
                    "gpu": d["spec"]["template"]["spec"].get("nodeSelector", {}).get("hardware/igpu") == "true",
                    "node": d["spec"]["template"]["spec"].get("nodeSelector", {}).get("kubernetes.io/hostname", ""),
                    "volumes": [{"path": m.get("mountPath"), "source": next(
                        (v.get("persistentVolumeClaim", {}).get("claimName", "")
                         for v in d["spec"]["template"]["spec"].get("volumes", [])
                         if v["name"] == m["name"]), "")}
                        for m in c.get("volumeMounts", []) or []],
                })
            if p == "/api/namespaces":
                return self._send(200, sorted(n["metadata"]["name"] for n in kget("/api/v1/namespaces")["items"]))
            if p == "/api/storageclasses":
                return self._send(200, sorted(s["metadata"]["name"] for s in kget("/apis/storage.k8s.io/v1/storageclasses")["items"]))
            if p == "/api/pvcs":
                ns = (q.get("ns") or [DEFAULT_NS])[0]
                items = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims")["items"]
                return self._send(200, [{"name": i["metadata"]["name"],
                                         "size": i["spec"]["resources"]["requests"]["storage"],
                                         "status": i["status"].get("phase")} for i in items])
            if p == "/api/appstore":
                term = (q.get("q") or [""])[0].lower().strip()
                cat = (q.get("cat") or [""])[0].lower().strip()
                try:
                    apps = fetch_appstore()
                except Exception as e:
                    return self._send(502, {"error": f"app feed unavailable: {e}"})
                if term:
                    apps = [a for a in apps if term in a["name"].lower() or term in a["desc"].lower()]
                if cat:
                    apps = [a for a in apps if cat in (a["cat"] or "").lower()]
                return self._send(200, {"total": len(apps), "apps": apps[:60]})
            if p == "/api/logs":
                ns, pod = q["ns"][0], q["pod"][0]
                req = urllib.request.Request(
                    f"{API}/api/v1/namespaces/{ns}/pods/{pod}/log?tailLines=200",
                    headers={"Authorization": f"Bearer {TOKEN}"})
                with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
                    return self._send(200, r.read().decode("utf-8", "replace"), "text/plain; charset=utf-8")
            return self._send(404, {"error": "no route"})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": e.read().decode("utf-8", "replace")[:500]})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            b = self._body()
            if p == "/api/deploy":
                dep, svc = build_deployment(b)
                ns = dep["metadata"]["namespace"]
                for v in b.get("volumes") or []:
                    if v.get("type") == "pvc" and v.get("create"):
                        try:
                            create_pvc(ns, v["source"], v.get("size_gb", 5))
                        except urllib.error.HTTPError as e:
                            if e.code != 409: raise
                ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
                if svc:
                    try:
                        ksend("POST", f"/api/v1/namespaces/{ns}/services", svc)
                    except urllib.error.HTTPError as e:
                        if e.code != 409: raise
                _cache.pop("wl", None); _cache.pop("ov", None)
                return self._send(200, {"ok": True, "name": dep["metadata"]["name"]})
            if p == "/api/scale":
                ns, name, n = b["ns"], b["name"], int(b["replicas"])
                ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale",
                      {"spec": {"replicas": n}}, ctype="application/merge-patch+json")
                _cache.pop("wl", None)
                return self._send(200, {"ok": True})
            if p == "/api/restart":
                ns, name = b["ns"], b["name"]
                ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                      {"spec": {"template": {"metadata": {"annotations":
                       {"harvui.io/restartedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ")}}}}},
                      ctype="application/merge-patch+json")
                _cache.pop("wl", None)
                return self._send(200, {"ok": True})
            if p == "/api/shares":
                s = create_share(b["name"], int(b.get("size_gb", 10)), b.get("user", "lab"),
                                 b.get("password"), b.get("public", False))
                return self._send(200, {"ok": True, "shares": s})
            if p == "/api/shares/delete":
                return self._send(200, {"ok": True, "shares": delete_share(b["name"])})
            if p == "/api/appstore/install":
                cfg = template_to_cfg(b["app"])
                cfg.update(b.get("overrides") or {})
                dep, svc = build_deployment(cfg)
                ns = dep["metadata"]["namespace"]
                ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
                if svc:
                    try:
                        ksend("POST", f"/api/v1/namespaces/{ns}/services", svc)
                    except urllib.error.HTTPError as e:
                        if e.code != 409: raise
                _cache.pop("wl", None)
                return self._send(200, {"ok": True, "name": cfg["name"]})
            if p == "/api/edit":
                return self._send(200, LC.edit_workload(b))
            if p == "/api/move":
                return self._send(200, LC.move_workload(b["ns"], b["name"], b.get("node")))
            if p == "/api/node/cordon":
                return self._send(200, LC.set_cordon(b["node"], b.get("cordon", True)))
            if p == "/api/node/drain":
                return self._send(200, LC.drain(b["node"], b.get("grace", 30), b.get("system", False)))
            if p == "/api/node/power":
                if b.get("confirm") != b.get("node"):
                    return self._send(400, {"error": "confirmation must repeat the node name"})
                try:
                    return self._send(200, LC.node_power(b["node"], b["action"], b.get("drain", True)))
                except PermissionError as e:
                    return self._send(409, {"error": str(e)})
            if p == "/api/vm/migrate":
                return self._send(200, LC.vm_migrate(b.get("ns", DEFAULT_NS), b["name"], b.get("target")))
            if p == "/api/vm/power":
                return self._send(200, LC.vm_power(b.get("ns", DEFAULT_NS), b["name"], b["action"]))
            if p == "/api/vm/create":
                return self._send(200, IMP.create_vm(b))
            if p == "/api/images/prepull":
                return self._send(200, IMP.prepull(b["image"], b.get("nodes")))
            if p == "/api/schedules":
                IMP.save_job(b); return self._send(200, {"ok": True})
            if p == "/api/schedules/delete":
                return self._send(200, IMP.del_job(b["name"]))
            if p == "/api/schedules/run":
                IMP.run_job_now(b["name"]); return self._send(200, {"ok": True})
            if p == "/api/sources":
                return self._send(200, {"ok": True, "sources": IMP.add_source(
                    b["name"], b["host"], b["user"], b.get("password"),
                    b.get("kind", "unraid"), b.get("base_path", "/mnt/user/appdata"))})
            if p == "/api/sources/delete":
                return self._send(200, {"ok": True, "sources": IMP.del_source(b["name"])})
            if p == "/api/sources/browse":
                return self._send(200, {"entries": IMP.browse_source(b["name"], b.get("path"))})
            if p == "/api/import":
                return self._send(200, IMP.import_container(b))
            if p == "/api/preview":
                dep, svc = build_deployment(b)
                return self._send(200, {"deployment": dep, "service": svc})
            return self._send(404, {"error": "no route"})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": e.read().decode("utf-8", "replace")[:600]})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        parts = [x for x in u.path.split("/") if x]
        try:
            if len(parts) == 4 and parts[:2] == ["api", "workload"]:
                ns, name = parts[2], parts[3]
                ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
                try:
                    ksend("DELETE", f"/api/v1/namespaces/{ns}/services/{name}")
                except urllib.error.HTTPError:
                    pass
                _cache.pop("wl", None); _cache.pop("ov", None)
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "no route"})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": e.read().decode("utf-8", "replace")[:500]})
        except Exception as e:
            return self._send(500, {"error": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=_sampler, daemon=True).start()
    print(f"HarvUI listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
