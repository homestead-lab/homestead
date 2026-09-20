#!/usr/bin/env python3
"""
Homestead - a friendly homelab control plane for Harvester, Rancher and Longhorn.
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
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HOMESTEAD_VERSION = os.environ.get("HOMESTEAD_VERSION", os.environ.get("HARVUI_VERSION", "2.1.0"))

DEFAULT_APP_SETTINGS = {
    "thresholds": {
        "cpu": {"warning": 70, "critical": 88},
        "memory": {"warning": 70, "critical": 88},
        "disk": {"warning": 75, "critical": 90},
        "temperature": {"warning": 70, "critical": 85},
    },
    "updates": {
        "policy": "approval_required",
        "notify_available": True,
        "notify_failures": True,
        "maintenance": {"days": [0, 1, 2, 3, 4, 5, 6],
                        "start": "02:00", "duration_minutes": 120},
    },
    "smart": {
        "temperature": {"warning": 55, "critical": 65},
        "reallocated_warning": 1,
        "pending_critical": 1,
        "uncorrectable_critical": 1,
        "notify_failures": True,
    },
}

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


def validate_app_settings(value):
    """Validate and normalize cluster-wide UI and workload-update policy."""
    incoming = (value or {}).get("thresholds") or {}
    out = json.loads(json.dumps(DEFAULT_APP_SETTINGS))
    for metric, defaults in out["thresholds"].items():
        supplied = incoming.get(metric) or {}
        warning = int(supplied.get("warning", defaults["warning"]))
        critical = int(supplied.get("critical", defaults["critical"]))
        upper = 120 if metric == "temperature" else 100
        if warning < 1 or critical > upper or warning >= critical:
            unit = "°C" if metric == "temperature" else "%"
            raise ValueError(f"{metric} thresholds must be ordered between 1 and {upper}{unit}")
        out["thresholds"][metric] = {"warning": warning, "critical": critical}
    smart_in = (value or {}).get("smart") or {}
    smart_temp = smart_in.get("temperature") or out["smart"]["temperature"]
    temp_warning = int(smart_temp.get("warning", out["smart"]["temperature"]["warning"]))
    temp_critical = int(smart_temp.get("critical", out["smart"]["temperature"]["critical"]))
    if temp_warning < 1 or temp_critical > 120 or temp_warning >= temp_critical:
        raise ValueError("drive temperature thresholds must be ordered between 1 and 120°C")
    out["smart"]["temperature"] = {"warning": temp_warning, "critical": temp_critical}
    for key in ("reallocated_warning", "pending_critical", "uncorrectable_critical"):
        count = int(smart_in.get(key, out["smart"][key]))
        if count < 1 or count > 1_000_000:
            raise ValueError(f"{key} must be between 1 and 1000000")
        out["smart"][key] = count
    notify = smart_in.get("notify_failures", out["smart"]["notify_failures"])
    if not isinstance(notify, bool):
        raise ValueError("SMART notify_failures must be true or false")
    out["smart"]["notify_failures"] = notify
    update_in = (value or {}).get("updates") or {}
    policy = str(update_in.get("policy", out["updates"]["policy"]))
    if policy not in ("notify_only", "approval_required", "maintenance_window"):
        raise ValueError("update policy must be notify_only, approval_required, or maintenance_window")
    out["updates"]["policy"] = policy
    for key in ("notify_available", "notify_failures"):
        supplied = update_in.get(key, out["updates"][key])
        if not isinstance(supplied, bool):
            raise ValueError(f"{key} must be true or false")
        out["updates"][key] = supplied
    maintenance = update_in.get("maintenance") or {}
    start = str(maintenance.get("start", out["updates"]["maintenance"]["start"]))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start):
        raise ValueError("maintenance start must use 24-hour HH:MM UTC")
    try:
        duration = int(maintenance.get("duration_minutes",
                                       out["updates"]["maintenance"]["duration_minutes"]))
        days = sorted(set(int(day) for day in maintenance.get(
            "days", out["updates"]["maintenance"]["days"])))
    except (TypeError, ValueError):
        raise ValueError("maintenance days and duration are invalid")
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("maintenance days must contain values from 0 (Monday) to 6 (Sunday)")
    if duration < 15 or duration > 1440:
        raise ValueError("maintenance duration must be between 15 and 1440 minutes")
    out["updates"]["maintenance"] = {
        "days": days, "start": start, "duration_minutes": duration}
    return out


def update_policy_status(settings=None, now=None):
    """Return whether a manual managed update may start at the current UTC time."""
    update = (settings or get_app_settings()).get("updates") or DEFAULT_APP_SETTINGS["updates"]
    policy = update.get("policy", "approval_required")
    maintenance = update.get("maintenance") or DEFAULT_APP_SETTINGS["updates"]["maintenance"]
    current = time.gmtime(time.time() if now is None else now)
    hour, minute = (int(part) for part in maintenance["start"].split(":"))
    start_minute = hour * 60 + minute
    current_minute = current.tm_hour * 60 + current.tm_min
    duration = int(maintenance["duration_minutes"])
    # A window may cross midnight. In that case early minutes belong to the
    # previous configured day, not the current one.
    today_open = current.tm_wday in maintenance["days"] and (
        start_minute <= current_minute < min(1440, start_minute + duration))
    previous_day = (current.tm_wday - 1) % 7
    carry = max(0, start_minute + duration - 1440)
    carry_open = previous_day in maintenance["days"] and current_minute < carry
    window_open = bool(today_open or carry_open)
    if policy == "notify_only":
        allowed, reason = False, "Cluster policy is notify only; an admin must change it before installing."
    elif policy == "maintenance_window" and not window_open:
        allowed, reason = False, (f"Updates are limited to the {maintenance['start']} UTC "
                                  f"maintenance window ({duration} minutes).")
    else:
        allowed, reason = True, "Explicit operator approval is required before rollout."
    return {"policy": policy, "allows_install": allowed, "requires_approval": True,
            "reason": reason, "window_open": window_open,
            "maintenance": json.loads(json.dumps(maintenance))}


def enforce_update_policy(body, settings=None, now=None):
    status = update_policy_status(settings, now)
    if not status["allows_install"]:
        raise PermissionError(status["reason"])
    if body.get("approved") is not True:
        raise PermissionError("explicit approval is required before installing an image update")
    return status


def get_app_settings():
    try:
        cm = kget(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/harvui-settings")
        raw = json.loads((cm.get("data") or {}).get("settings.json", "{}"))
        return validate_app_settings(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return validate_app_settings({})
        raise
    except (ValueError, TypeError, json.JSONDecodeError):
        return validate_app_settings({})


def save_app_settings(value):
    settings = validate_app_settings(value)
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "harvui-settings", "namespace": DEFAULT_NS,
                         "labels": {"harvui.io/managed": "true"}},
            "data": {"settings.json": json.dumps(settings, indent=2)}}
    try:
        current = kget(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/harvui-settings")
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        ksend("PUT", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/harvui-settings", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps", body)
    _cache.pop("settings", None)
    return settings


def app_settings_payload():
    settings = json.loads(json.dumps(cached("settings", 15, get_app_settings)))
    try:
        kube = kget("/version").get("gitVersion", "")
    except Exception:
        kube = ""
    settings["info"] = {"version": HOMESTEAD_VERSION, "namespace": DEFAULT_NS,
                        "storage_class": STORAGE_CLASS, "vip": LB_IP,
                        "kubernetes": kube}
    return settings


# ---------------------------------------------------------------- collectors
_TEMP_CACHE = {"at": 0, "data": {}}


def node_temps():
    """Temperatures from the optional harvui-nodeprobe DaemonSet.

    Absent probe is not an error — it just means no thermal data, which the UI
    reports rather than showing a blank gauge.
    """
    if time.time() - _TEMP_CACHE["at"] < 20:
        return _TEMP_CACHE["data"]
    out = {}
    try:
        pods = kget(f"/api/v1/namespaces/{DEFAULT_NS}/pods"
                    "?labelSelector=app%3Dharvui-nodeprobe").get("items", [])
    except Exception:
        pods = []
    for p in pods:
        ip = p.get("status", {}).get("podIP")
        node = p.get("spec", {}).get("nodeName")
        if not ip or not node or p.get("status", {}).get("phase") != "Running":
            continue
        payload = {"node": node, "thermal": [], "hwmon": [], "devices": {},
                   "disks": [], "sensors": 0, "smart_helper": {"available": False}}
        try:
            with urllib.request.urlopen(f"http://{ip}:9099/", timeout=4) as r:
                payload.update(json.loads(r.read().decode()))
        except Exception:
            pass
        try:
            with urllib.request.urlopen(f"http://{ip}:9100/", timeout=20) as r:
                smart = json.loads(r.read().decode())
            rows = {row.get("name"): row for row in smart.get("disks", [])}
            for disk in payload.get("disks", []):
                disk["smart"] = rows.get(disk.get("name"))
            payload["smart_helper"] = {"available": True, "disks": len(rows)}
        except Exception as error:
            payload["smart_helper"] = {"available": False,
                "reason": "SMART helper unavailable; install or update deploy/nodeprobe.yaml"}
        out[node] = payload
    _TEMP_CACHE.update(at=time.time(), data=out)
    return out


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


def smart_disk_issues(report, settings=None):
    """Classify actionable SMART findings using cluster-wide thresholds."""
    if not report or not report.get("available"):
        return []
    cfg = settings or DEFAULT_APP_SETTINGS["smart"]
    issues = []
    if str(report.get("health") or "").lower() == "failed":
        issues.append({"severity": "critical", "reason": "SMART overall-health check failed"})
    temperature = report.get("temperature_c")
    if temperature is not None:
        severity = ("critical" if float(temperature) >= cfg["temperature"]["critical"] else
                    "degraded" if float(temperature) >= cfg["temperature"]["warning"] else "")
        if severity:
            issues.append({"severity": severity,
                           "reason": f"drive temperature is {temperature}°C"})
    reallocated = int(report.get("reallocated") or 0)
    pending = int(report.get("pending") or 0)
    uncorrectable = int(report.get("uncorrectable") or 0)
    media = int(report.get("media_errors") or 0)
    if reallocated >= cfg["reallocated_warning"]:
        issues.append({"severity": "degraded", "reason": f"{reallocated} reallocated sector(s)"})
    if pending >= cfg["pending_critical"]:
        issues.append({"severity": "critical", "reason": f"{pending} pending sector(s)"})
    if uncorrectable >= cfg["uncorrectable_critical"]:
        issues.append({"severity": "critical",
                       "reason": f"{uncorrectable} uncorrectable sector(s)"})
    if media:
        issues.append({"severity": "critical", "reason": f"{media} NVMe media error(s)"})
    return issues


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

    temps = node_temps()
    smart_cfg = get_app_settings().get("smart") or DEFAULT_APP_SETTINGS["smart"]
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
                     for p in npods if p["metadata"]["namespace"] not in SYS_NS
                     and not p["metadata"].get("labels", {}).get("harvui.io/task")
                     and (p["metadata"].get("labels", {}).get("app") or "") not in
                         ("harvui-nodeprobe", "image-prepull")})
        probed = (temps.get(name) or {}).get("devices") or {}
        annotations = n["metadata"].get("annotations", {}) or {}
        try:
            labels, auto_hardware = HW.reconcile_node(name, labels, annotations, probed)
        except Exception:
            auto_hardware = {x for x in annotations.get(HW.AUTO_ANNOTATION, "").split(",") if x}
        hardware_inventory = HW.inventory(labels, probed, auto_hardware)
        hardware = {x["id"]: x["available"] for x in hardware_inventory}
        temp_payload = temps.get(name)
        disk_issues = []
        for disk in (temp_payload or {}).get("disks", []):
            for issue in smart_disk_issues(disk.get("smart"), smart_cfg):
                disk_issues.append({**issue, "disk": disk.get("name", "unknown")})
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
            "igpu": hardware["igpu"],
            "hardware": hardware,
            "hardware_inventory": hardware_inventory,
            "workloads": wl,
            "kernel": n["status"].get("nodeInfo", {}).get("kernelVersion", ""),
            "os": n["status"].get("nodeInfo", {}).get("osImage", ""),
            "schedulable": not n.get("spec", {}).get("unschedulable", False),
            "addresses": {a["type"]: a["address"] for a in n["status"].get("addresses", [])},
            "allocatable": n["status"].get("allocatable", {}),
            "labels": labels,
            "info": n["status"].get("nodeInfo", {}),
            "conditions": [{"type": c["type"], "status": c["status"], "reason": c.get("reason", "")}
                           for c in n["status"].get("conditions", [])],
            "created": n["metadata"].get("creationTimestamp", ""),
            **node_stats(name),
            "temps": temp_payload,
            "disk_issues": disk_issues,
            "smart_notify": smart_cfg.get("notify_failures", True),
        })
    return out


def get_volumes():
    try:
        vols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return []
    try:
        pvcs = {(p["metadata"]["namespace"], p["metadata"]["name"]): p
                for p in kget("/api/v1/persistentvolumeclaims").get("items", [])}
    except Exception:
        pvcs = {}
    out = []
    for v in vols:
        st = v.get("status", {})
        sp = v.get("spec", {})
        ks = st.get("kubernetesStatus", {}) or {}
        wls = ks.get("workloadsStatus") or []
        pvc_obj = pvcs.get((ks.get("namespace", ""), ks.get("pvcName", "")), {})
        pvc_spec = pvc_obj.get("spec", {}) or {}
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
            "used_pct": round((int(st.get("actualSize", 0) or 0) / max(int(sp.get("size", 0) or 0), 1)) * 100, 1),
            "access_modes": pvc_spec.get("accessModes", []) or [],
            "storage_class": pvc_spec.get("storageClassName", ""),
            "pvc": v["metadata"].get("annotations", {}).get("longhorn.io/volume-scheduling-error", "") or "",
        })
    return sorted(out, key=lambda x: x["name"])


def pod_container_rows(pod):
    """Return an explicit, UI-safe view of init and app containers in a pod."""
    spec = pod.get("spec", {}) or {}
    status = pod.get("status", {}) or {}
    groups = [
        ("init", spec.get("initContainers", []) or [], status.get("initContainerStatuses", []) or []),
        ("app", spec.get("containers", []) or [], status.get("containerStatuses", []) or []),
    ]
    rows = []
    for kind, containers, statuses in groups:
        by_name = {x.get("name", ""): x for x in statuses}
        for container in containers:
            name = container.get("name", "")
            cs = by_name.get(name, {})
            state_obj = cs.get("state", {}) or {}
            waiting = state_obj.get("waiting") or {}
            terminated = state_obj.get("terminated") or {}
            if waiting:
                state = waiting.get("reason") or "waiting"
                message = waiting.get("message") or ""
            elif terminated:
                state = terminated.get("reason") or "terminated"
                message = terminated.get("message") or ""
            elif state_obj.get("running"):
                state, message = "running", ""
            else:
                state, message = "pending", ""
            rows.append({
                "name": name,
                "kind": kind,
                "image": container.get("image", ""),
                "ready": bool(cs.get("ready", False)),
                "state": state,
                "message": str(message)[:220],
                "restarts": int(cs.get("restartCount", 0) or 0),
            })
    return rows


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
        pspec = d["spec"]["template"]["spec"]
        annotations = d["metadata"].get("annotations", {}) or {}
        hardware = HW.workload_features(pspec, annotations)
        pod_rows = []
        transition_ages = []
        fatal_waits = []
        fatal_reasons = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff",
                         "CreateContainerConfigError", "CreateContainerError"}
        for p in mine:
            conditions = {c.get("type"): c for c in p.get("status", {}).get("conditions", []) or []}
            ready = conditions.get("Ready", {}).get("status") == "True"
            waits = []
            for cs in p.get("status", {}).get("containerStatuses", []) or []:
                waiting = (cs.get("state", {}).get("waiting") or {})
                if waiting:
                    reason = waiting.get("reason", "Waiting")
                    waits.append({"container": cs.get("name", ""), "reason": reason,
                                  "message": (waiting.get("message") or "")[:220]})
                    if reason in fatal_reasons:
                        fatal_waits.append(f"{p['metadata']['name']}: {reason}")
            if not ready:
                transition_ages.append(age_secs(p["metadata"].get("creationTimestamp")))
            containers = pod_container_rows(p)
            pod_rows.append({"name": p["metadata"]["name"], "phase": p["status"].get("phase"),
                             "node": p["spec"].get("nodeName", ""), "ready": ready,
                             "waiting": waits,
                             "uptime": age_secs(p["status"].get("startTime")),
                             "containers": containers,
                             "container_count": len([c for c in containers if c["kind"] == "app"]),
                             "restarts": sum(c.get("restartCount", 0) for c in
                                             p["status"].get("containerStatuses", []) or [])})
        progress_errors = [c.get("message") or c.get("reason") or "rollout failed"
                           for c in st.get("conditions", []) or []
                           if c.get("type") == "Progressing" and c.get("status") == "False"]
        template_annotations = d["spec"].get("template", {}).get("metadata", {}).get("annotations", {}) or {}
        rollout_at = (template_annotations.get("harvui.io/update-rollout-at") or
                      template_annotations.get("harvui.io/restartedAt"))
        if rollout_at:
            transition_ages.append(age_secs(rollout_at))
        if not transition_ages:
            transition_ages.append(age_secs(d["metadata"].get("creationTimestamp")))
        out.append({
            "ns": ns, "name": name, "kind": "Deployment", "uptime": uptime,
            "ready": st.get("readyReplicas", 0) or 0,
            "desired": d["spec"].get("replicas", 0) or 0,
            "available": st.get("availableReplicas", 0) or 0,
            "updated": st.get("updatedReplicas", 0) or 0,
            "unavailable": st.get("unavailableReplicas", 0) or 0,
            "generation": d["metadata"].get("generation", 0) or 0,
            "observed_generation": st.get("observedGeneration", 0) or 0,
            "transition_age": min(transition_ages),
            "problems": fatal_waits + progress_errors,
            "images": [c["image"] for c in pspec.get("containers", [])],
            "nodes": sorted({p["spec"].get("nodeName", "") for p in mine if p["spec"].get("nodeName")}),
            "pods": pod_rows,
            "pod_count": len(pod_rows),
            "container_count": sum(p["container_count"] for p in pod_rows),
            "cpu": round(cpu, 3), "mem_mb": round(mem / 1024**2, 1),
            "ports": ports,
            "gpu": "igpu" in hardware,
            "hardware": hardware,
            "icon": display_icon(annotations),
        })
    return sorted(out, key=lambda x: (x["ns"], x["name"]))


def classify_cluster_health(nodes, workloads, volumes, startup_grace=300):
    """Separate real availability faults from normal workload transitions."""
    issues, activities = [], []
    for node in nodes:
        if node.get("status") != "Ready":
            issues.append({"severity": "critical", "kind": "Node",
                           "name": node.get("name", "unknown"),
                           "reason": f"node is {node.get('status') or 'not ready'}"})
        for disk in (node.get("disk_issues") or []) if node.get("smart_notify", True) else []:
            issues.append({"severity": disk.get("severity", "degraded"), "kind": "Disk",
                           "name": f"{node.get('name', 'unknown')}/{disk.get('disk', 'unknown')}",
                           "reason": disk.get("reason", "SMART warning")})
    for volume in volumes:
        robustness = str(volume.get("robustness", "") or "").lower()
        label = volume.get("pvc_name") or volume.get("name") or "unknown"
        if robustness == "faulted":
            issues.append({"severity": "critical", "kind": "Volume", "name": label,
                           "reason": "Longhorn reports the volume faulted"})
        elif robustness == "degraded":
            issues.append({"severity": "degraded", "kind": "Volume", "name": label,
                           "reason": "Longhorn is rebuilding or missing a replica"})
    for workload in workloads:
        desired = int(workload.get("desired", 0) or 0)
        ready = int(workload.get("ready", 0) or 0)
        generation = int(workload.get("generation", 0) or 0)
        observed = int(workload.get("observed_generation", 0) or 0)
        updated = int(workload.get("updated", 0) or 0)
        transitioning = (ready < desired or updated < desired or observed < generation or
                          int(workload.get("unavailable", 0) or 0) > 0)
        if desired == 0 or not transitioning:
            continue
        name = workload.get("name", "unknown")
        namespace = workload.get("ns", "")
        resource = f"{namespace}/{name}" if namespace else name
        problems = workload.get("problems") or []
        if problems:
            issues.append({"severity": "degraded", "kind": "Workload", "name": resource,
                           "reason": str(problems[0])[:260]})
            continue
        age = int(workload.get("transition_age", startup_grace + 1) or 0)
        if age <= startup_grace:
            updating = int(workload.get("available", 0) or 0) > 0 and (
                updated < desired or observed < generation)
            activities.append({"state": "updating" if updating else "starting",
                               "kind": "Workload", "name": resource,
                               "reason": f"{ready}/{desired} replicas ready"})
        else:
            issues.append({"severity": "degraded", "kind": "Workload", "name": resource,
                           "reason": f"only {ready}/{desired} replicas ready after {age // 60}m"})
    health = "critical" if any(x["severity"] == "critical" for x in issues) else (
        "degraded" if issues else "healthy")
    activity = "updating" if any(x["state"] == "updating" for x in activities) else (
        "starting" if activities else "idle")
    state = health if health != "healthy" else (activity if activity != "idle" else "healthy")
    if issues:
        summary = "; ".join(f"{x['kind']} {x['name']}: {x['reason']}" for x in issues[:4])
    elif activities:
        summary = "; ".join(f"{x['kind']} {x['name']}: {x['reason']}" for x in activities[:4])
    else:
        summary = "All nodes, workloads, and attached volumes are healthy"
    return {"health": health, "health_state": state, "health_summary": summary,
            "health_issues": issues, "activities": activities}


def get_overview():
    nodes = get_nodes()
    wl = get_workloads()
    vols = get_volumes()
    pods = kget("/api/v1/pods").get("items", [])
    sysp = [p for p in pods if p["metadata"]["namespace"] in SYS_NS]
    usrp = [p for p in pods if p["metadata"]["namespace"] not in SYS_NS]
    deg = [v for v in vols if v["robustness"] == "degraded"]
    flt = [v for v in vols if v["robustness"] == "faulted"]
    health = classify_cluster_health(nodes, wl, vols)
    tcap = sum(n["cpu_cap"] for n in nodes) or 1
    tuse = sum(n["cpu_used"] for n in nodes)
    mcap = sum(n["mem_cap_gb"] for n in nodes) or 1
    muse = sum(n["mem_used_gb"] for n in nodes)
    return {
        **health,
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
        ev = kget("/api/v1/events?limit=160")
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
        "count": e.get("count", 1),
    } for e in items[:120]]


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
    try:
        dep_meta = {(d["metadata"]["namespace"], d["metadata"]["name"]):
                    d["metadata"].get("annotations", {}) or {}
                    for d in kget("/apis/apps/v1/deployments").get("items", [])}
    except Exception:
        dep_meta = {}

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
            "icon": display_icon(dep_meta.get((p["metadata"]["namespace"], app), {})),
            "hardware": HW.workload_features(p["spec"], dep_meta.get((p["metadata"]["namespace"], app), {})),
            "gpu": any("dri" in (m.get("mountPath") or "")
                       for c in p["spec"].get("containers", []) for m in (c.get("volumeMounts") or [])),
            "claims": claims, "ports": ports_by_app.get(app, []),
        })
    for v in vmis:
        nm = v["metadata"]["name"]
        wls.append({"id": "w:vm-" + nm, "name": nm, "kind": "vm", "ns": v["metadata"]["namespace"],
                    "node": v.get("status", {}).get("nodeName", ""), "phase": v.get("status", {}).get("phase", ""),
                    "uptime": 0, "cpu": 0, "mem_mb": 0,
                    "image": "", "gpu": False, "hardware": [], "claims": [], "ports": []})

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
        "healthy": len([v for v in vols if v["state"] == "attached" and v["robustness"] == "healthy"]),
        "degraded": len([v for v in vols if v["state"] == "attached" and v["robustness"] == "degraded"]),
        "faulted": len([v for v in vols if v["state"] == "attached" and v["robustness"] == "faulted"]),
        "unknown": len([v for v in vols if v["state"] != "attached"]),
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
    hardware = set(cfg.get("hardware") or [])
    if cfg.get("gpu"):
        hardware.add("igpu")
    devices = {f["id"]: HW.mount_spec(f) for f in HW.features()}
    unknown = hardware - set(devices)
    if unknown:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
    for hw in hardware:
        if hw not in devices:
            continue
        dev = devices[hw]
        podspec.setdefault("nodeSelector", {})[dev["label"]] = "true"
        podspec["containers"][0].setdefault("securityContext", {})["privileged"] = True
        podspec["containers"][0].setdefault("volumeMounts", []).append(
            {"name": dev["name"], "mountPath": dev["container_path"]})
        podspec.setdefault("volumes", []).append(
            {"name": dev["name"], "hostPath": {"path": dev["host_path"], "type": dev["path_type"]}})
    if cfg.get("network_mode") == "host":
        podspec["hostNetwork"] = True
        podspec["dnsPolicy"] = "ClusterFirstWithHostNet"
    if cfg.get("node"):
        podspec.setdefault("nodeSelector", {})["kubernetes.io/hostname"] = cfg["node"]
    podspec["tolerations"] = [
        {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 15},
        {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 15},
    ]
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": {"app": name, "harvui.io/managed": "true"},
                     "annotations": ({**({"harvui.io/icon": cfg.get("icon", "")} if cfg.get("icon") else {}),
                                      **({"harvui.io/icon-source": cfg.get("icon_source", cfg.get("icon", ""))} if cfg.get("icon") else {}),
                                      **({"harvui.io/hardware": ",".join(sorted(hardware))} if hardware else {})})},
        "spec": {"replicas": int(cfg.get("replicas", 1)), "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name, "lab-workload": "true"}}, "spec": podspec}},
    }
    svc = None
    exposed = [p for p in cfg.get("ports") or [] if p.get("expose")]
    if exposed and cfg.get("network_mode") != "host":
        mode = cfg.get("vip_mode", "shared")
        vip = cfg.get("lb_ip") if mode == "manual" else (LB_IP if mode == "shared" else "")
        svc_type = "ClusterIP" if cfg.get("network_mode") == "internal" else "LoadBalancer"
        svc = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name, "harvui.io/managed": "true"},
                         "annotations": {"kube-vip.io/loadbalancerIPs": vip} if vip and svc_type == "LoadBalancer" else {}},
            "spec": {"type": svc_type, "selector": {"app": name},
                      "ports": [{"name": (p.get("name") or f"p{p['container']}")[:15],
                                 "port": int(p.get("host") or p["container"]),
                                 "targetPort": int(p["container"]),
                                 "protocol": str(p.get("protocol", "TCP")).upper()} for p in exposed]},
        }
    return dep, svc


def create_pvc(ns, name, size_gb, sc=None, access_mode="ReadWriteOnce"):
    body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": ns, "labels": {"harvui.io/managed": "true"}},
            "spec": {"accessModes": [access_mode],
                     "storageClassName": sc or STORAGE_CLASS,
                     "resources": {"requests": {"storage": f"{size_gb}Gi"}}}}
    return ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", body)


def create_volume(cfg):
    name = (cfg.get("name") or "").strip().lower()
    ns = cfg.get("namespace") or DEFAULT_NS
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", name):
        raise ValueError("volume name must be lowercase letters, numbers and dashes")
    size = int(cfg.get("size_gb", 10))
    if size < 1:
        raise ValueError("volume size must be at least 1 GB")
    mode = cfg.get("access_mode", "ReadWriteOnce")
    if mode not in ("ReadWriteOnce", "ReadWriteMany"):
        raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
    out = create_pvc(ns, name, size, cfg.get("storage_class") or STORAGE_CLASS, mode)
    for k in list(_cache):
        if k.startswith(("vol", "stor", "flow")):
            _cache.pop(k, None)
    return {"ok": True, "name": name, "namespace": ns, "pvc": out}


def edit_volume(cfg):
    """Grow a PVC and optionally change Longhorn replica count."""
    ns, name = cfg.get("namespace") or DEFAULT_NS, cfg["name"]
    pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    if cfg.get("size_gb"):
        pvc["spec"]["resources"]["requests"]["storage"] = f"{int(cfg['size_gb'])}Gi"
        ksend("PUT", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}", pvc)
    reps = cfg.get("replicas")
    vol_name = pvc.get("spec", {}).get("volumeName")
    if reps is not None and vol_name:
        ksend("PATCH", f"/apis/longhorn.io/v1beta2/volumes/{vol_name}",
              {"spec": {"numberOfReplicas": int(reps)}}, ctype="application/merge-patch+json")
    for k in list(_cache):
        if k.startswith(("vol", "stor", "flow")):
            _cache.pop(k, None)
    return {"ok": True, "name": name}


def set_node_hardware(cfg):
    name = cfg["node"]
    selected = set(cfg.get("features") or [])
    # Backward-compatible body accepted from pre-v1.3 clients.
    if cfg.get("igpu"): selected.add("igpu")
    if cfg.get("coral_pcie"): selected.add("coral_pcie")
    if cfg.get("coral_usb"): selected.add("coral_usb")
    known = {f["id"] for f in HW.features()}
    if selected - known:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(selected - known)))
    labels = {f["label"]: "true" if f["id"] in selected else "false" for f in HW.features()}
    # These are deliberate overrides, so remove them from the auto-managed set.
    ksend("PATCH", f"/api/v1/nodes/{name}", {"metadata": {"labels": labels,
          "annotations": {HW.AUTO_ANNOTATION: None}}},
          ctype="application/merge-patch+json")
    for k in list(_cache):
        if k.startswith(("nodes", "ov")):
            _cache.pop(k, None)
    return {"ok": True, "node": name}


# ---------------------------------------------------------------- app store
CA_FEED = "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json"


def fetch_appstore():
    def go():
        req = urllib.request.Request(CA_FEED, headers={"User-Agent": "Homestead/2.0"})
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
            "image": app["repo"], "icon": app.get("icon") or "",
            "ports": ports, "env": envs, "volumes": vols}


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
    if not public and not password:
        raise ValueError("a password is required for a private share")
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
        share_password = s.get("password") or password
        if not guest and not share_password:
            raise ValueError(f"share {s['name']} needs a password before Samba can be updated")
        if share_password:
            users[owner] = share_password
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
import harvui_auth as AUTH
import harvui_longhorn as LH
import harvui_place as PLACE
import harvui_hardware as HW
import harvui_updates as UPDATES
import harvui_operations as OPS
import harvui_console as CONSOLE
import harvui_icons as ICONS
import harvui_volumes as VOLUMES
import harvui_smart as SMART
HW.bind(kget, ksend, DEFAULT_NS, _cache)
LC.bind(kget, ksend, SYS_NS, _cache, HW.features)
IMP.bind(kget, ksend, create_pvc, build_deployment, DEFAULT_NS, _cache, HW.features)
AUTH.bind(kget, ksend, DEFAULT_NS)
LH.bind(kget, ksend, _cache)
PLACE.bind(kget, ksend, lambda: cached("nodes", 5, get_nodes), _cache, HW.features)
UPDATES.bind(kget, ksend, DEFAULT_NS, DATA_DIR, SYS_NS)
SMART.bind(kget, DEFAULT_NS, AUTH.internal_signing_key)
OPS.bind(kget, DATA_DIR, UPDATES.progress, SMART.progress)
VOLUMES.bind(kget, ksend, LH.snapshots, LH.backups, _cache, SYS_NS, DEFAULT_NS)
CONSOLE_PROXY = CONSOLE.ConsoleProxy(API, TOKEN, CTX, DATA_DIR, SYS_NS, {DEFAULT_NS}, kget)


def display_icon(annotations):
    reference = (annotations or {}).get("harvui.io/icon", "")
    try:
        return ICONS.data_url(reference, DATA_DIR)
    except (FileNotFoundError, ValueError, OSError):
        return ""

# Browser routes serve the same authenticated application shell. Keep this an
# explicit allowlist: an unknown path must not accidentally shadow an API 404.
SPA_ROUTES = frozenset({
    "/", "/architecture", "/nodes", "/deploy", "/containers", "/vms",
    "/app-store", "/shares", "/volumes", "/image-cache", "/data-protection",
    "/schedules", "/import", "/events", "/settings",
})


def is_spa_route(path):
    clean = (path or "/").rstrip("/") or "/"
    return clean in SPA_ROUTES


# Paths reachable without a session. Everything else needs one.
PUBLIC = {"/healthz", "/style.css", "/index.html",
          "/api/auth/login", "/api/auth/state", "/api/auth/setup"}


def is_public_path(path):
    # Cached icons contain only size/type-validated images fetched from public
    # URLs. Serving their content-addressed paths without a session lets
    # browsers load them as subresources even when cookies are restricted.
    return path in PUBLIC or is_asset_path(path) or path.startswith("/api/icons/")


def is_asset_path(path):
    """Allow only flat, bundled SVG assets; never user-controlled filesystem paths."""
    return bool(re.fullmatch(r"/assets/[A-Za-z0-9][A-Za-z0-9._-]*\.svg", path or ""))

# Role needed per route. Rules:
#   * any GET needs at least "viewer"
#   * any mutation defaults to "operator"
#   * routes below override that, and everything sensitive is "admin"
# Enforced here, server-side. The UI hides what you cannot do as a courtesy,
# but a viewer who hand-crafts the request still gets a 403.
ADMIN_ROUTES = {
    "/api/auth/users", "/api/auth/users/delete", "/api/auth/role",
    "/api/node/power", "/api/node/drain", "/api/node/cordon", "/api/node/hardware",
    "/api/sources", "/api/sources/delete", "/api/sources/browse",
    "/api/sources/containers", "/api/sources/inspect", "/api/import",
    "/api/shares", "/api/shares/delete",
    "/api/images/cleanup",
    "/api/volumes/delete",
    "/api/node/smart/test",
    "/api/lh/target", "/api/lh/job/delete", "/api/lh/snapshot/delete",
}
# things a signed-in user may always do to their own account
SELF_ROUTES = {"/api/auth/logout", "/api/auth/password", "/api/auth/signout-everywhere"}


def needed_role(path, method):
    if path in SELF_ROUTES:
        return "viewer"
    if path == "/api/hardware/features" and method != "GET":
        return "admin"
    if path == "/api/settings" and method != "GET":
        return "admin"
    if path == "/api/console":
        return "operator"
    if path in ADMIN_ROUTES:
        return "admin"
    return "viewer" if method == "GET" else "operator"


def persist_icon_config(cfg):
    """Replace a remote logo with a persistent same-origin cache reference."""
    if "icon" not in cfg:
        return cfg
    source = str(cfg.get("icon") or "").strip()
    if not source:
        cfg["icon"] = ""
        cfg["icon_source"] = ""
        return cfg
    cfg["icon"] = ICONS.persist(source, DATA_DIR)
    cfg["icon_source"] = source
    return cfg


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    _extra_headers = None

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (self._extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, {"error": "not found"})

    def _icon(self, request_path):
        try:
            path, ctype = ICONS.resolve(request_path, DATA_DIR)
            with open(path, "rb") as handle:
                body = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send(404, {"error": "icon not found"})

    # ---------------------------------------------------------- auth
    def _cookies(self):
        raw = self.headers.get("Cookie") or ""
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _set_cookie(self, token, clear=False):
        if clear:
            self._extra_headers.append(
                ("Set-Cookie", f"{AUTH.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"))
        else:
            self._extra_headers.append(
                ("Set-Cookie", f"{AUTH.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                               f"Max-Age={AUTH.SESSION_TTL}"))

    def _who(self):
        return AUTH.verify_token(self._cookies().get(AUTH.COOKIE))

    def _guard(self, path):
        """Returns None when the request may proceed, or sends the refusal."""
        if is_spa_route(path) or is_public_path(path) or (path.startswith("/js/") and path.endswith(".js")):
            return None
        who = self._who()
        if not who:
            self._send(401, {"error": "not signed in", "auth": False})
            return True
        # CSRF: the cookie is SameSite=Strict, and mutations additionally require a
        # header that a cross-site form cannot set.
        if self.command in ("POST", "DELETE", "PUT", "PATCH"):
            if self.headers.get("X-HarvUI-Auth") != "1":
                self._send(403, {"error": "missing X-HarvUI-Auth header"})
                return True
        self.user, self.role = who["user"], who["role"]
        need = needed_role(path, self.command)
        if not AUTH.allows(self.role, need):
            self._send(403, {"error": f"your role ({self.role}) cannot do this — {need} required",
                             "role": self.role, "needed": need})
            return True
        return None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def do_GET(self):
        self._extra_headers = []
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if self._guard(p):
                return
            if p == "/api/console":
                return CONSOLE_PROXY.handle(self, self.user, q)
            if p.startswith("/api/icons/"):
                return self._icon(p)
            if is_spa_route(p) or p == "/index.html":
                return self._file(f"{WEBROOT}/index.html", "text/html; charset=utf-8")
            if p.startswith("/js/") and p.endswith(".js") and ".." not in p:
                return self._file(f"{WEBROOT}/js/{os.path.basename(p)}", "application/javascript")
            if is_asset_path(p):
                return self._file(f"{WEBROOT}/assets/{os.path.basename(p)}", "image/svg+xml")
            if p == "/app.js":
                return self._file(f"{WEBROOT}/app.js", "application/javascript")
            if p == "/style.css":
                return self._file(f"{WEBROOT}/style.css", "text/css")
            if p == "/healthz":
                return self._send(200, {"ok": True})
            if p == "/api/auth/state":
                who = self._who()
                return self._send(200, {"setup": AUTH.needs_setup(),
                                        "user": who["user"] if who else None,
                                        "role": who["role"] if who else None,
                                        "roles": list(AUTH.ROLES)})
            if p == "/api/auth/users":
                return self._send(200, AUTH.list_users())
            if p == "/api/settings":
                return self._send(200, app_settings_payload())
            if p == "/api/overview":
                return self._send(200, cached("ov", 5, get_overview))
            if p == "/api/nodes":
                return self._send(200, cached("nodes", 5, get_nodes))
            if p == "/api/workloads":
                return self._send(200, cached("wl", 5, get_workloads))
            if p == "/api/image-updates":
                force = (q.get("force") or ["0"])[0].lower() in ("1", "true", "yes")
                report = json.loads(json.dumps(cached(
                    "image-updates" if not force else
                    "image-updates-force:" + str(int(time.time() / 10)),
                    600 if not force else 8, lambda: UPDATES.scan(force))))
                report["policy"] = update_policy_status()
                return self._send(200, report)
            if p == "/api/image-updates/progress":
                return self._send(200, UPDATES.progress(q["ns"][0], q["name"][0]))
            if p == "/api/operations":
                return self._send(200, OPS.list_operations())
            if p == "/api/volumes":
                return self._send(200, cached("vol", 8, get_volumes))
            if p == "/api/volumes/delete-plan":
                return self._send(200, VOLUMES.deletion_plan(
                    (q.get("ns") or [DEFAULT_NS])[0], (q.get("name") or [""])[0]))
            if p == "/api/events":
                return self._send(200, cached("ev", 10, get_events))
            if p == "/api/storage":
                return self._send(200, cached("stor", 10, get_storage))
            if p == "/api/node":
                return self._send(200, cached("node:" + (q.get("name") or [""])[0], 5,
                                  lambda: next((n for n in get_nodes()
                                                if n["name"] == (q.get("name") or [""])[0]), {})))
            if p == "/api/node/smart":
                node = (q.get("node") or [""])[0]
                disk = (q.get("disk") or [""])[0]
                return self._send(200, SMART.disk(node, disk) if disk else SMART.inventory(node))
            if p == "/api/history":
                with _lock:
                    return self._send(200, {k: list(v) for k, v in HIST.items()})
            if p == "/api/flow":
                return self._send(200, cached("flow2", 8, get_flow2))
            if p == "/api/shares":
                return self._send(200, list_shares())
            if p == "/api/move/plan":
                return self._send(200, PLACE.plan(
                    q["ns"][0], q["name"][0],
                    float((q.get("cpu") or [0])[0]), float((q.get("mem") or [0])[0])))
            if p == "/api/node/impact":
                node = (q.get("node") or [""])[0]
                if not node:
                    return self._send(400, {"error": "node is required"})
                return self._send(200, cached("impact:" + node, 5, lambda: PLACE.impact(node)))
            if p == "/api/quorum":
                r = LC.quorum_report(); r["power_enabled"] = LC.NODE_POWER_ENABLED
                return self._send(200, r)
            if p == "/api/vms":
                return self._send(200, cached("vms", 5, IMP.list_vms))
            if p == "/api/vmimages":
                return self._send(200, cached("vmimg", 30, IMP.list_vm_images))
            if p == "/api/images":
                return self._send(200, cached("imgcache", 30, IMP.image_cache))
            if p == "/api/hardware/features":
                return self._send(200, cached("hardware:features", 15, HW.features))
            if p == "/api/schedules":
                return self._send(200, cached("cron", 8, IMP.list_jobs))
            if p == "/api/lh/overview":
                return self._send(200, cached("lhov", 8, LH.overview))
            if p == "/api/lh/snapshots":
                vol = (q.get("volume") or [None])[0]
                return self._send(200, LH.snapshots(vol))
            if p == "/api/lh/backups":
                vol = (q.get("volume") or [None])[0]
                return self._send(200, LH.backups(vol))
            if p == "/api/sources":
                return self._send(200, IMP.list_sources())
            if p == "/api/imports":
                return self._send(200, IMP.import_status())
            if p == "/api/workload":
                ns, nm = q["ns"][0], q["name"][0]
                d = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{nm}")
                c = d["spec"]["template"]["spec"]["containers"][0]
                pspec = d["spec"]["template"]["spec"]
                hardware = HW.workload_features(pspec, d["metadata"].get("annotations", {}) or {})
                return self._send(200, {
                    "ns": ns, "name": nm, "image": c.get("image", ""),
                    "replicas": d["spec"].get("replicas", 1),
                    "cpu": c.get("resources", {}).get("requests", {}).get("cpu", ""),
                    "memory": c.get("resources", {}).get("requests", {}).get("memory", ""),
                    "env": {e["name"]: e.get("value", "") for e in c.get("env", []) or []},
                    "ports": [{"container": x.get("containerPort"), "name": x.get("name", "")}
                              for x in c.get("ports", []) or []],
                    "gpu": "igpu" in hardware,
                    "hardware": hardware,
                    "icon": d["metadata"].get("annotations", {}).get(
                        "harvui.io/icon-source",
                        d["metadata"].get("annotations", {}).get("harvui.io/icon", "")),
                    "node": pspec.get("nodeSelector", {}).get("kubernetes.io/hostname", ""),
                    "seed_configs": LC.seed_configs(ns, d),
                    "volumes": [{"path": m.get("mountPath"), "source": next(
                        (v.get("persistentVolumeClaim", {}).get("claimName", "")
                          for v in pspec.get("volumes", [])
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
                ns = q["ns"][0]
                pod = (q.get("pod") or [""])[0]
                job = (q.get("job") or [""])[0]
                if job and not pod:
                    matches = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{urllib.parse.quote(job)}").get("items", [])
                    pod = matches[0]["metadata"]["name"] if matches else ""
                if not pod:
                    return self._send(404, {"error": "Logs are not available yet because no pod exists."})
                tail = max(20, min(1000, int((q.get("tail") or [300])[0])))
                req = urllib.request.Request(
                    f"{API}/api/v1/namespaces/{ns}/pods/{pod}/log?tailLines={tail}&timestamps=true",
                    headers={"Authorization": f"Bearer {TOKEN}"})
                with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
                    return self._send(200, r.read().decode("utf-8", "replace"), "text/plain; charset=utf-8")
            return self._send(404, {"error": "no route"})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": e.read().decode("utf-8", "replace")[:500]})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        self._extra_headers = []
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            if self._guard(p):
                return
            b = self._body()
            addr = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if p == "/api/auth/setup":
                AUTH.create_user(b.get("username"), b.get("password"), first_only=True)
                tok = AUTH.issue_token((b.get("username") or "").strip().lower())
                self._set_cookie(tok)
                return self._send(200, {"ok": True, "user": b.get("username")})
            if p == "/api/auth/login":
                try:
                    tok = AUTH.login(b.get("username"), b.get("password"), addr)
                except PermissionError as e:
                    return self._send(401, {"error": str(e)})
                self._set_cookie(tok)
                return self._send(200, {"ok": True, "user": (b.get("username") or "").strip().lower()})
            if p == "/api/auth/logout":
                self._set_cookie("", clear=True)
                return self._send(200, {"ok": True})
            if p == "/api/auth/password":
                AUTH.change_password(self.user, b.get("old"), b.get("new"))
                self._set_cookie(AUTH.issue_token(self.user))
                return self._send(200, {"ok": True})
            if p == "/api/auth/users":
                AUTH.create_user(b.get("username"), b.get("password"),
                                 role=b.get("role", "operator"))
                return self._send(200, {"ok": True, "users": AUTH.list_users()})
            if p == "/api/auth/role":
                AUTH.set_role(b["username"], b["role"], self.user)
                return self._send(200, {"ok": True, "users": AUTH.list_users()})
            if p == "/api/auth/users/delete":
                AUTH.delete_user(b.get("username"), self.user)
                return self._send(200, {"ok": True, "users": AUTH.list_users()})
            if p == "/api/auth/signout-everywhere":
                AUTH.logout_everywhere(self.user)
                self._set_cookie("", clear=True)
                return self._send(200, {"ok": True})
            if p == "/api/settings":
                return self._send(200, {"ok": True, **save_app_settings(b)})
            if p == "/api/deploy":
                persist_icon_config(b)
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
                op = OPS.start("deployment", f"Deploy {dep['metadata']['name']}",
                               {"kind": "Deployment", "name": dep["metadata"]["name"], "namespace": ns},
                               "/containers", {"namespace": ns, "name": dep["metadata"]["name"]})
                return self._send(200, {"ok": True, "name": dep["metadata"]["name"], "operation": op})
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
            if p == "/api/image-updates/apply":
                enforce_update_policy(b)
                result = UPDATES.apply_update(b["ns"], b["name"])
                result["operation"] = OPS.start(
                    "image-update", f"Update {b['name']}",
                    {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
                    "/containers?" + urllib.parse.urlencode({"q": b["name"]}),
                    {"namespace": b["ns"], "name": b["name"]})
                _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("image-updates", None)
                return self._send(200, result)
            if p == "/api/image-updates/rollback":
                result = UPDATES.rollback(b["ns"], b["name"])
                result["operation"] = OPS.start(
                    "image-rollback", f"Roll back {b['name']}",
                    {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
                    "/containers?" + urllib.parse.urlencode({"q": b["name"]}),
                    {"namespace": b["ns"], "name": b["name"]})
                _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("image-updates", None)
                return self._send(200, result)
            if p == "/api/shares":
                s = create_share(b["name"], int(b.get("size_gb", 10)), b.get("user", "lab"),
                                 b.get("password"), b.get("public", False))
                return self._send(200, {"ok": True, "shares": s})
            if p == "/api/shares/delete":
                return self._send(200, {"ok": True, "shares": delete_share(b["name"])})
            if p == "/api/appstore/install":
                cfg = template_to_cfg(b["app"])
                cfg.update(b.get("overrides") or {})
                persist_icon_config(cfg)
                dep, svc = build_deployment(cfg)
                ns = dep["metadata"]["namespace"]
                ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
                if svc:
                    try:
                        ksend("POST", f"/api/v1/namespaces/{ns}/services", svc)
                    except urllib.error.HTTPError as e:
                        if e.code != 409: raise
                _cache.pop("wl", None)
                op = OPS.start("deployment", f"Install {cfg['name']}",
                               {"kind": "Deployment", "name": cfg["name"], "namespace": ns},
                               "/containers", {"namespace": ns, "name": cfg["name"]})
                return self._send(200, {"ok": True, "name": cfg["name"], "operation": op})
            if p == "/api/edit":
                persist_icon_config(b)
                return self._send(200, LC.edit_workload(b))
            if p == "/api/move":
                node = b.get("node")
                if b.get("auto"):
                    pl = PLACE.plan(b["ns"], b["name"], b.get("cpu", 0), b.get("mem_mb", 0))
                    node = pl["recommended"]
                    if not node:
                        return self._send(409, {"error": "no host can take this workload — "
                                                "check hardware requirements", "plan": pl})
                result = PLACE.move(b["ns"], b["name"], node, b.get("pin", False))
                result["operation"] = OPS.start(
                    "deployment", f"Move {b['name']}",
                    {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
                    "/containers", {"namespace": b["ns"], "name": b["name"]})
                return self._send(200, result)
            if p == "/api/node/cordon":
                return self._send(200, LC.set_cordon(b["node"], b.get("cordon", True)))
            if p == "/api/node/hardware":
                return self._send(200, set_node_hardware(b))
            if p == "/api/node/smart/test":
                result = SMART.start_test(b.get("node"), b.get("disk"), b.get("test"))
                result["operation"] = OPS.start(
                    "smart-test", f"SMART {result['test']} test · {result['disk']}",
                    {"kind": "Disk", "name": result["disk"], "namespace": result["node"]},
                    "/nodes?node=" + urllib.parse.quote(result["node"]),
                    {"node": result["node"], "disk": result["disk"],
                     "test": result["test"], "expected_seconds": result["expected_seconds"],
                     "baseline": result["baseline"], "started_epoch": result["started_epoch"]},
                    result["message"])
                _TEMP_CACHE["at"] = 0
                _cache.pop("node:" + result["node"], None)
                return self._send(200, result)
            if p == "/api/hardware/features":
                return self._send(200, HW.save_features(b.get("features")))
            if p == "/api/node/drain":
                impact = PLACE.impact(b["node"])
                if impact["stranded"] and not b.get("allow_stranded"):
                    return self._send(409, {"error": "some workloads have no eligible failover host",
                                            "impact": impact})
                return self._send(200, LC.drain(b["node"], b.get("grace", 30), b.get("system", False)))
            if p == "/api/node/power":
                if b.get("confirm") != b.get("node"):
                    return self._send(400, {"error": "confirmation must repeat the node name"})
                impact = PLACE.impact(b["node"])
                if impact["stranded"] and not b.get("allow_stranded"):
                    return self._send(409, {"error": "some workloads have no eligible failover host",
                                            "impact": impact})
                try:
                    return self._send(200, LC.node_power(b["node"], b["action"], b.get("drain", True)))
                except PermissionError as e:
                    return self._send(409, {"error": str(e)})
            if p == "/api/vm/migrate":
                ns = b.get("ns", DEFAULT_NS)
                result = LC.vm_migrate(ns, b["name"], b.get("target"))
                if result.get("migration"):
                    result["operation"] = OPS.start(
                        "vm-migration", f"Migrate {b['name']}",
                        {"kind": "VirtualMachine", "name": b["name"], "namespace": ns},
                        "/vms", {"namespace": ns, "name": result["migration"]})
                return self._send(200, result)
            if p == "/api/vm/power":
                return self._send(200, LC.vm_power(b.get("ns", DEFAULT_NS), b["name"], b["action"]))
            if p == "/api/vm/create":
                return self._send(200, IMP.create_vm(b))
            if p == "/api/images/prepull":
                result = IMP.prepull(b["image"], b.get("nodes"))
                result["operation"] = OPS.start(
                    "image-pull", f"Pull {b['image']}",
                    {"kind": "Image", "name": b["image"], "namespace": DEFAULT_NS},
                    "/image-cache", {"namespace": DEFAULT_NS, "name": result["daemonset"]})
                return self._send(200, result)
            if p == "/api/images/cleanup":
                result = IMP.cleanup_image(b.get("digest"), b.get("nodes"))
                result["operation"] = OPS.start(
                    "image-cleanup", f"Clean cached image {result['digest'][:19]}…",
                    {"kind": "Image", "name": result["image"], "namespace": DEFAULT_NS},
                    "/image-cache", {"namespace": DEFAULT_NS, "pods": result["pods"]})
                return self._send(200, result)
            if p == "/api/volumes/create":
                return self._send(200, create_volume(b))
            if p == "/api/volumes/edit":
                return self._send(200, edit_volume(b))
            if p == "/api/volumes/delete":
                result = VOLUMES.delete(b)
                result["operation"] = OPS.start(
                    "volume-delete", f"Delete volume {result['name']}",
                    {"kind": "PersistentVolumeClaim", "name": result["name"],
                     "namespace": result["namespace"]},
                    "/volumes", {"namespace": result["namespace"], "name": result["name"],
                                  "action": result["action"], "pv": result["pv"],
                                  "volume": result["longhorn_volume"]}, result["message"])
                return self._send(200, result)
            if p == "/api/lh/job":
                return self._send(200, LH.save_job(b))
            if p == "/api/lh/job/delete":
                return self._send(200, LH.delete_job(b["name"]))
            if p == "/api/lh/assign":
                return self._send(200, LH.bulk_assign(
                    b["volumes"], b["name"], b.get("kind", "group"), b.get("enabled", True)))
            if p == "/api/lh/snapshot":
                return self._send(200, LH.create_snapshot(b["volume"], b.get("name")))
            if p == "/api/lh/snapshot/delete":
                return self._send(200, LH.delete_snapshot(b["name"]))
            if p == "/api/lh/backup":
                result = LH.create_backup(b["volume"], b.get("name"))
                if result.get("backup"):
                    result["operation"] = OPS.start(
                        "backup", f"Back up {b['volume']}",
                        {"kind": "Volume", "name": b["volume"], "namespace": "longhorn-system"},
                        "/data-protection", {"namespace": "longhorn-system", "name": result["backup"]})
                return self._send(200, result)
            if p == "/api/lh/target":
                return self._send(200, LH.set_backup_target(
                    b["url"], b.get("secret", ""), b.get("poll", "5m")))
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
            if p == "/api/sources/containers":
                return self._send(200, {"containers": IMP.source_containers(b["name"])})
            if p == "/api/sources/inspect":
                return self._send(200, IMP.inspect_source_container(b["name"], b["container"]))
            if p == "/api/import":
                persist_icon_config(b)
                result = IMP.import_container(b)
                result["operation"] = OPS.start(
                    "import", f"Import {b['name']}",
                    {"kind": "Job", "name": result["job"], "namespace": DEFAULT_NS},
                    "/import", {"namespace": DEFAULT_NS, "name": result["job"]})
                return self._send(200, result)
            if p == "/api/operations/dismiss":
                return self._send(200, OPS.dismiss(b["id"]))
            if p == "/api/preview":
                dep, svc = build_deployment(b)
                return self._send(200, {"deployment": dep, "service": svc})
            return self._send(404, {"error": "no route"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": e.read().decode("utf-8", "replace")[:600]})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_DELETE(self):
        self._extra_headers = []
        u = urllib.parse.urlparse(self.path)
        if self._guard(urllib.parse.urlparse(self.path).path):
            return
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
    print(f"Homestead listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
