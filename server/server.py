#!/usr/bin/env python3
"""
Homestead - a friendly homelab control plane for Harvester, Rancher and Longhorn.
Pure Python stdlib: no pip install at runtime, so it starts even with no internet.
"""
import copy, html, json, os, re, secrets, signal, ssl, sys, time, threading, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Imported ahead of the feature modules because settings are read during start.
import homestead_names as NAMES
import homestead_memory as MEMORY
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_rollout_capacity as ROLLOUT_CAPACITY

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
TOKEN = open(f"{SA}/token").read().strip() if os.path.exists(f"{SA}/token") else ""
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt") if os.path.exists(f"{SA}/ca.crt") else ssl._create_unverified_context()
WEBROOT = os.environ.get("WEBROOT", "/web")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
SMB_NAMESPACE = os.environ.get("SMB_NAMESPACE", "lab")
DEFAULT_NS = os.environ.get("DEFAULT_NS", "lab")
# Empty (as the Helm chart leaves it): the cluster's default class, read once
# the API is reachable - see _resolve_storage_class below.
STORAGE_CLASS = os.environ.get("STORAGE_CLASS", "longhorn-r2")
LB_IP = os.environ.get("LB_IP", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HOMESTEAD_VERSION = os.environ.get("HOMESTEAD_VERSION", "2.8.164")

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
    # What to call this installation, shown under the Homestead wordmark. Blank
    # means nothing is shown: better than a word that describes nobody's setup.
    "site_name": "",
    # Where the App Store reads its catalogue: any feed in the Community
    # Applications format. Blank means the public Community Applications feed.
    "catalog_url": "",
}

SYS_NS = {
    "kube-system", "kube-public", "kube-node-lease", "harvester-system", "harvester-public",
    "longhorn-system", "cattle-system", "cattle-dashboards", "cattle-fleet-system",
    "cattle-fleet-local-system", "cattle-fleet-clusters-system", "cattle-monitoring-system",
    "cattle-logging-system", "cattle-provisioning-capi-system", "cattle-ui-plugin-system",
    "cattle-capi-system", "cattle-turtles-system", "fleet-local", "local", "cdi", "kube-ovn",
    "kubevirt", "system-upgrade",
}
# Namespaces of the platform Homestead's add-ons install on k3s and RKE2, and
# what each belongs to. Their containers are listed with Homestead's own,
# hidden until asked for, and are upgraded with the part they belong to: an
# operator owns them and puts back any image changed by hand. (On Harvester
# the same parts run in harvester-system, which is not listed at all.)
PLATFORM_NS = {"kubevirt": "KubeVirt", "cdi": "CDI", "system-upgrade": "system-upgrade-controller"}

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


# Each background task says when it last did its work, and what went wrong
# if it did not, so Settings › About can say whether Homestead is healthy
# rather than only whether it answers.
HEART = {}
_heart_lock = threading.Lock()


def beat(name, every, error=None, leader_only=False):
    now = time.time()
    with _heart_lock:
        row = HEART.setdefault(name, {"every": every, "leader_only": leader_only, "last_ok": 0, "error": "", "error_at": 0})
        row["seen"] = now
        if error is None:
            row["last_ok"] = now
            row["error"] = ""
        else:
            row["error"], row["error_at"] = str(error)[:200], now


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
            # Each VM's disk traffic is a count, and a rate needs two readings.
            try:
                VMUSAGE.sample()
            except Exception:
                pass
            beat("sampler", 30)
        except Exception as error:
            beat("sampler", 30, error)
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
    site = str((value or {}).get("site_name", out["site_name"]) or "").strip()
    if len(site) > 40:
        raise ValueError("site name must be 40 characters or fewer")
    out["site_name"] = site
    url = str((value or {}).get("catalog_url", out["catalog_url"]) or "").strip()
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or len(url) > 500:
            raise ValueError("the catalogue address must be a plain http:// or https:// URL")
    out["catalog_url"] = url
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


def _settings_map():
    return NAMES.object_name("settings", DEFAULT_NS)


def get_app_settings():
    try:
        cm = kget(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{_settings_map()}")
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
    name = _settings_map()
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": DEFAULT_NS,
                         "labels": {NAMES.key("managed"): "true"}},
            "data": {"settings.json": json.dumps(settings, indent=2)}}
    try:
        current = kget(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{name}")
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        ksend("PUT", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{name}", body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps", body)
    _cache.pop("settings", None)
    # A different catalogue source is a different catalogue.
    for key in [k for k in _cache if k.startswith("appstore")]:
        _cache.pop(key, None)
    return settings


def app_settings_payload():
    settings = json.loads(json.dumps(cached("settings", 15, get_app_settings)))
    try:
        kube = kget("/version").get("gitVersion", "")
    except Exception:
        kube = ""
    settings["info"] = {"version": HOMESTEAD_VERSION, "namespace": DEFAULT_NS,
                        "storage_class": STORAGE_CLASS, "vip": LB_IP,
                        "kubernetes": kube, "node_probe": PROBE.status(),
                        "permissions": SELF.status()}
    return settings


# ---------------------------------------------------------------- collectors
_TEMP_CACHE = {"at": 0, "data": {}}


def node_temps():
    """Temperatures from the optional homestead-nodeprobe DaemonSet.

    Absent probe is not an error — it just means no thermal data, which the UI
    reports rather than showing a blank gauge.
    """
    if time.time() - _TEMP_CACHE["at"] < 20:
        return _TEMP_CACHE["data"]
    out = {}
    pods = NAMES.nodeprobe_pods(DEFAULT_NS)
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
        except Exception:
            payload["smart_helper"] = {"available": False,
                "reason": "SMART helper unavailable; install or update deploy/nodeprobe.yaml"}
        out[node] = payload
    _TEMP_CACHE.update(at=time.time(), data=out)
    return out


def reconcile_hardware(fresh=False):
    """Every node's hardware labels, from what its probe sees now.

    The scheduler places a workload that needs a Coral by these labels, so
    they have to follow a device plugged in later - not wait until someone
    opens the Nodes page."""
    if fresh:
        _TEMP_CACHE["at"] = 0
    temps = node_temps()
    found = {}
    for n in kget("/api/v1/nodes").get("items", []):
        name = n["metadata"]["name"]
        nfs_facts = (temps.get(name) or {}).get("nfs") or {}
        if isinstance(nfs_facts.get("server"), bool):
            wanted = "true" if nfs_facts["server"] else None
            if (n["metadata"].get("labels") or {}).get(NFS.HOST_LABEL) != wanted:
                ksend("PATCH", f"/api/v1/nodes/{name}", {"metadata": {"labels": {NFS.HOST_LABEL: wanted}}},
                      ctype="application/merge-patch+json")
        devices = (temps.get(name) or {}).get("devices")
        if devices is None:
            continue                 # no probe here: nothing to say either way
        labels, auto = HW.reconcile_node(name, n["metadata"].get("labels", {}) or {},
                                         n["metadata"].get("annotations", {}) or {}, devices)
        found[name] = sorted(x["id"] for x in HW.inventory(labels, devices, auto) if x["detected"])
    if fresh:
        _cache.pop("nodes", None); _cache.pop("ov", None)
    return found


def _hardware_loop():
    while True:
        if LEADER.is_leader():
            try:
                reconcile_hardware()
                beat("hardware", 30, leader_only=True)
            except Exception as error:
                beat("hardware", 30, error, leader_only=True)
                print(f"hardware: {str(error)[:160]}", flush=True)
        time.sleep(30)


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


def smart_disk_health(report, settings=None):
    """What the findings add up to, in one word plus why.

    smartctl's own overall-health bit says PASSED until a drive is at death's
    door: a disk with hundreds of reallocated sectors still passes it. The
    counters are where the warning lives, so the verdict is drawn from those
    against the configured thresholds, and says which ones it was.
    """
    if not report:
        return {"state": "unavailable", "issues": [], "life_pct": None,
                "life_basis": "", "spare_pct": None, "stale_probe": False,
                "summary": "no SMART data for this drive"}
    if not report.get("available"):
        return {"state": "unavailable", "issues": [], "life_pct": None,
                "life_basis": "", "spare_pct": None, "stale_probe": False,
                "summary": report.get("unavailable_reason")
                or "this drive or its USB bridge does not expose SMART data"}
    issues = smart_disk_issues(report, settings)
    # A probe from before wear reporting sends no "wear" key at all, which is
    # not the same as a drive that has nothing to report. Saying "unsupported"
    # for both sends people to look at the drive instead of the probe.
    stale_probe = "wear" not in report
    wear = report.get("wear") or {}
    life = wear.get("life_pct")
    spare, floor = wear.get("spare_pct"), wear.get("spare_floor_pct")
    # A drive that has spent its endurance is worn out whatever else it says.
    if life is not None and int(life) <= 10:
        issues.append({"severity": "critical",
                       "reason": f"only {int(life)}% of rated life remains"})
    elif life is not None and int(life) <= 25:
        issues.append({"severity": "degraded",
                       "reason": f"{int(life)}% of rated life remains"})
    if spare is not None and floor is not None and int(spare) <= int(floor):
        issues.append({"severity": "critical",
                       "reason": f"spare blocks are down to {int(spare)}%, "
                                 f"at the drive's floor of {int(floor)}%"})
    state = ("critical" if any(x["severity"] == "critical" for x in issues)
             else "attention" if issues else "healthy")
    if not issues:
        summary = "no reported defects"
        if str(report.get("health") or "").lower() == "passed":
            summary = "passed, with no reported defects"
    else:
        summary = "; ".join(x["reason"] for x in issues)
    return {"state": state, "issues": issues, "summary": summary,
            "life_pct": None if life is None else int(life),
            "life_basis": wear.get("basis", ""),
            "stale_probe": stale_probe,
            "spare_pct": None if spare is None else int(spare)}


def node_duties(pods):
    """What falls to one node rather than another: the load-balancer addresses
    it announces, and the shared volumes it serves.

    kube-vip elects one node to answer for load-balanced addresses - on
    Harvester, the management VIP hosts join through among them - and records
    the winner in a Lease: plndr-svcs-lock for every Service at once,
    kubevip-<service> where each is elected on its own, plndr-cp-lock for the
    control-plane address. Longhorn serves each shared (RWX) volume through a
    share-manager pod on one node; that node going down pauses the volume
    until the pod starts elsewhere.
    """
    duties = {}

    def note(node, key, value):
        if node and value not in duties.setdefault(node, {"vips": [], "rwx": [], "control_plane_vip": False})[key]:
            duties[node][key].append(value)
    try:
        leases = kget("/apis/coordination.k8s.io/v1/namespaces/kube-system/leases").get("items", [])
    except Exception:
        leases = []
    try:
        network = cached("network", 5, NETWORK.inventory)
    except Exception:
        network = {}
    platform = network.get("platform_addresses") or {}
    by_service = {}
    for row in network.get("services") or []:
        if row.get("type") == "LoadBalancer":
            by_service.setdefault(row["name"], []).extend(row.get("external_ips") or [])
    for lease in leases:
        name = lease["metadata"]["name"]
        holder = str((lease.get("spec") or {}).get("holderIdentity") or "")
        if not holder:
            continue
        if name == "plndr-cp-lock":
            duties.setdefault(holder, {"vips": [], "rwx": [], "control_plane_vip": False})["control_plane_vip"] = True
        elif name == "plndr-svcs-lock":
            for ips in by_service.values():
                for ip in ips:
                    note(holder, "vips", ip)
        elif name.startswith("kubevip-"):
            for ip in by_service.get(name[len("kubevip-"):], []):
                note(holder, "vips", ip)
    for node in duties.values():
        node["management_vip"] = [ip for ip in node["vips"] if ip in platform]
    try:
        claims = {row["name"]: row.get("pvc_name") or row["name"] for row in cached("volmap", 30, get_volumes)}
    except Exception:
        claims = {}
    for pod in pods.get("items", []) if isinstance(pods, dict) else pods:
        meta = pod.get("metadata") or {}
        if meta.get("namespace") == "longhorn-system" and meta.get("name", "").startswith("share-manager-")                 and (pod.get("status") or {}).get("phase") == "Running":
            volume = meta["name"][len("share-manager-"):]
            note((pod.get("spec") or {}).get("nodeName", ""), "rwx", claims.get(volume, volume))
    return duties


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

    try:
        duties = node_duties(pods)
    except Exception:
        duties = {}
    temps = node_temps()
    try:
        disk_lines = DISKS.summary()
    except Exception:
        disk_lines = {}
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
                     and not NAMES.label_of(p["metadata"], "task")
                     and (p["metadata"].get("labels", {}).get("app") or "") not in
                         (NAMES.NODEPROBE, "image-prepull")})
        probed = (temps.get(name) or {}).get("devices") or {}
        annotations = n["metadata"].get("annotations", {}) or {}
        try:
            labels, auto_hardware = HW.reconcile_node(name, labels, annotations, probed)
        except Exception:
            auto_hardware = {x for x in NAMES.read(annotations, "auto-hardware").split(",") if x}
        hardware_inventory = HW.inventory(labels, probed, auto_hardware)
        hardware = {x["id"]: x["available"] for x in hardware_inventory}
        temp_payload = temps.get(name)
        disk_issues = []
        for disk in (temp_payload or {}).get("disks", []):
            disk["health"] = smart_disk_health(disk.get("smart"), smart_cfg)
            for issue in disk["health"]["issues"]:
                disk_issues.append({**issue, "disk": disk.get("name", "unknown")})
        out.append({
            "name": name,
            "status": "Ready" if conds.get("Ready") == "True" else "NotReady",
            "roles": roles or ["worker"],
            "cpu_pct": round(ucpu / ccpu * 100, 1) if ccpu else 0,
            "cpu_used": round(ucpu, 2), "cpu_cap": ccpu,
            "mem_pct": round(umem / cmem * 100, 1) if cmem else 0,
            "mem_used_gb": round(umem / 1024**3, 1), "mem_cap_gb": round(cmem / 1024**3, 1),
            "mem_metrics_available": bool((m.get("usage") or {}).get("memory")),
            "pods": len(npods),
            "pods_sys": len([p for p in npods if p["metadata"]["namespace"] in SYS_NS]),
            "pods_wl": len([p for p in npods if p["metadata"]["namespace"] not in SYS_NS]),
            "vms": len([v for v in vmis if v.get("status", {}).get("nodeName") == name]),
            "igpu": hardware["igpu"],
            "disks": disk_lines.get(name, []),
            "hardware": hardware,
            "hardware_inventory": hardware_inventory,
            "workloads": wl,
            "kernel": n["status"].get("nodeInfo", {}).get("kernelVersion", ""),
            "os": n["status"].get("nodeInfo", {}).get("osImage", ""),
            "schedulable": not n.get("spec", {}).get("unschedulable", False),
            "addresses": {a["type"]: a["address"] for a in n["status"].get("addresses", [])},
            "allocatable": n["status"].get("allocatable", {}),
            "taints": n.get("spec", {}).get("taints", []),
            "labels": labels,
            "info": n["status"].get("nodeInfo", {}),
            "conditions": [{"type": c["type"], "status": c["status"], "reason": c.get("reason", "")}
                           for c in n["status"].get("conditions", [])],
            "created": n["metadata"].get("creationTimestamp", ""),
            # A new boot ID is a reboot; Ready's last change is how long it
            # has been up as far as Kubernetes is concerned; the probe knows
            # how long the host itself has been running.
            "boot_id": n["status"].get("nodeInfo", {}).get("bootID", ""),
            "ready_since": next((c.get("lastTransitionTime", "") for c in n["status"].get("conditions", [])
                                 if c.get("type") == "Ready" and c.get("status") == "True"), ""),
            "uptime_s": (temp_payload or {}).get("uptime_s"),
            **node_stats(name),
            "temps": temp_payload,
            "disk_issues": disk_issues,
            "smart_notify": smart_cfg.get("notify_failures", True),
            "duties": duties.get(name) or {"vips": [], "rwx": [], "control_plane_vip": False, "management_vip": []},
        })
    return out


def _volume_health_reason(volume):
    """Why Longhorn is unhappy with a volume, in its own words.

    "degraded" on its own sends people to the Longhorn UI to find out what it
    means. The conditions carry the answer - most often that a replica cannot
    be scheduled because no node has room for it.
    """
    status = volume.get("status", {}) or {}
    annotations = volume.get("metadata", {}).get("annotations", {}) or {}
    conditions = []
    for condition in status.get("conditions", []) or []:
        conditions.append({"type": condition.get("type", ""),
                           "status": condition.get("status", ""),
                           "reason": condition.get("reason", ""),
                           "message": (condition.get("message") or "")[:300]})
    # Longhorn's volume conditions do not share a polarity: Scheduled is False
    # when replicas cannot be placed, while WaitForBackingImage is False in the
    # ordinary case of a volume that has no backing image to wait for. Reading
    # every False as trouble reported a detached volume as broken.
    failing = [row for row in conditions
               if (row["status"] == "False" and row["type"] == "Scheduled")
               or (row["status"] == "True" and row["type"] in ("TooManySnapshots",
                                                               "WaitForBackingImage"))]
    scheduling = annotations.get("longhorn.io/volume-scheduling-error", "") or ""
    robustness = str(status.get("robustness", "") or "").lower()
    reason = ""
    if failing:
        first = failing[0]
        reason = first["message"] or first["reason"] or f"{first['type']} is failing"
    elif scheduling:
        reason = scheduling
    elif robustness == "degraded":
        # Longhorn reports no condition while it is simply catching up.
        reason = "a replica is rebuilding; the volume is readable and writable meanwhile"
    elif robustness == "faulted":
        reason = "every replica is unusable, so the volume cannot be attached"
    return reason[:300], conditions, scheduling


def _engine_progress(engines):
    """Rebuilds and restores in flight, per volume, from Longhorn's engines.

    The volume object only says "degraded" or "restoreRequired"; how far a
    replica rebuild or a backup restore has got is on the engine, keyed by
    replica address. The slowest replica is the one the volume waits on.
    """
    out = {}
    for engine in engines:
        volume = (engine.get("spec", {}) or {}).get("volumeName") or \
            (engine.get("metadata", {}).get("labels", {}) or {}).get("longhornvolume", "")
        if not volume:
            continue
        status = engine.get("status", {}) or {}
        entry = out.setdefault(volume, {})
        rebuilding = [row for row in (status.get("rebuildStatus") or {}).values()
                      if row and (row.get("isRebuilding") or row.get("error"))]
        if rebuilding:
            entry["rebuild"] = {
                "pct": min(int(row.get("progress") or 0) for row in rebuilding),
                "replicas": len(rebuilding),
                "error": next((str(row["error"])[:300] for row in rebuilding if row.get("error")), ""),
            }
        restoring = [row for row in (status.get("restoreStatus") or {}).values()
                     if row and (row.get("isRestoring") or row.get("error"))]
        if restoring:
            entry["restore"] = {
                "pct": min(int(row.get("progress") or 0) for row in restoring),
                "error": next((str(row["error"])[:300] for row in restoring if row.get("error")), ""),
            }
    return {name: entry for name, entry in out.items() if entry}


def claim_references():
    """Every claim something is defined to use, running or not: (namespace,
    claim) -> ["Deployment/plex", ...].

    Longhorn knows only the pods using a volume now, so a stopped container's
    volume looked the same as one nothing uses at all - and only the second
    is safe to think about deleting.
    """
    refs = {}

    def note(ns, claims, what):
        for claim in claims:
            if claim:
                refs.setdefault((ns, claim), []).append(what)

    def claims_of(podspec):
        return [(v.get("persistentVolumeClaim") or {}).get("claimName") for v in (podspec or {}).get("volumes") or []]

    for kind, path, spec_of in (
            ("Deployment", "/apis/apps/v1/deployments", lambda o: o["spec"]["template"]["spec"]),
            ("StatefulSet", "/apis/apps/v1/statefulsets", lambda o: o["spec"]["template"]["spec"]),
            ("DaemonSet", "/apis/apps/v1/daemonsets", lambda o: o["spec"]["template"]["spec"]),
            ("CronJob", "/apis/batch/v1/cronjobs", lambda o: o["spec"]["jobTemplate"]["spec"]["template"]["spec"])):
        try:
            items = kget(path).get("items", [])
        except Exception:
            continue
        for obj in items:
            try:
                note(obj["metadata"]["namespace"], claims_of(spec_of(obj)), f"{kind}/{obj['metadata']['name']}")
            except (KeyError, TypeError):
                continue
        if kind == "StatefulSet":
            # A StatefulSet's own claims are made from its template, named
            # <template>-<set>-<n>; they belong to it too.
            for obj in items:
                name, ns = obj["metadata"]["name"], obj["metadata"]["namespace"]
                for template in (obj.get("spec") or {}).get("volumeClaimTemplates") or []:
                    prefix = f"{(template.get('metadata') or {}).get('name', '')}-{name}-"
                    refs.setdefault(("__prefix__", ns, prefix), []).append(f"StatefulSet/{name}")
    try:
        vms = kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
    except Exception:
        vms = []
    for vm in vms:
        volumes = ((((vm.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or [])
        claims = [(v.get("persistentVolumeClaim") or {}).get("claimName") or (v.get("dataVolume") or {}).get("name")
                  for v in volumes]
        note(vm["metadata"]["namespace"], claims, f"VM/{vm['metadata']['name']}")
    return refs


def _references_for(refs, ns, claim):
    found = list(refs.get((ns, claim)) or [])
    for key, owners in refs.items():
        if key[0] == "__prefix__" and key[1] == ns and claim.startswith(key[2]):
            found.extend(owners)
    return sorted(set(found))


def get_volumes():
    try:
        vols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return []
    try:
        refs = claim_references()
    except Exception:
        refs = None
    try:
        progress = _engine_progress(
            kget("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines").get("items", []))
    except Exception:
        progress = {}
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
        health_reason, conditions, scheduling_error = _volume_health_reason(v)
        # Kept after its claim went - an old copy from a storage class change,
        # or a claim deleted with its data retained. Longhorn still names the
        # claim it had, which may now be another volume's.
        unclaimed = str(ks.get("pvStatus") or "") == "Released" or bool(
            ks.get("pvcName") and not pvc_obj and pvcs) or bool(
            pvc_obj and (pvc_obj.get("spec") or {}).get("volumeName") not in ("", None, v["metadata"]["name"]))
        out.append({
            "name": v["metadata"]["name"],
            "pvc_name": ks.get("pvcName", ""),
            "namespace": ks.get("namespace", ""),
            "attached": sorted({w.get("workloadName") or w.get("podName") or ""
                                for w in wls if w.get("workloadName") or w.get("podName")}),
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
            "engine": str(sp.get("dataEngine") or "v1").lower(),
            "actual_gb": round(int(st.get("actualSize", 0) or 0) / 1024**3, 2),
            "used_pct": round((int(st.get("actualSize", 0) or 0) / max(int(sp.get("size", 0) or 0), 1)) * 100, 1),
            "access_modes": pvc_spec.get("accessModes", []) or [],
            "storage_class": pvc_spec.get("storageClassName", ""),
            "health_reason": health_reason,
            "conditions": [row for row in conditions if row["status"] == "False"],
            "scheduling_error": scheduling_error,
            # What is defined to use it, running or not; None when that could
            # not be read, so nothing is called orphaned on a guess.
            "used_by": (_references_for(refs, ks.get("namespace", ""), ks.get("pvcName", ""))
                        if refs is not None and ks.get("pvcName") and pvc_obj and not unclaimed else None),
            "unclaimed": unclaimed,
            "rebuild": progress.get(v["metadata"]["name"], {}).get("rebuild"),
            # A restored volume keeps restoreRequired until its data is all
            # in, even between engine updates that carry no restore rows. A
            # disaster-recovery standby keeps it for good, so is left out.
            "restore": progress.get(v["metadata"]["name"], {}).get("restore") or (
                {"pct": 0, "error": ""}
                if st.get("restoreRequired") and not st.get("isStandby") else None),
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


OWN_GROUP = "Homestead"


def _never_started(pod):
    """Nothing ever ran in it: every container still waiting, or none reported."""
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return all(not (cs.get("state") or {}).get("running") and not (cs.get("state") or {}).get("terminated")
               and not (cs.get("lastState") or {}).get("terminated") for cs in statuses)


def clear_unstarted_pods(ns, name, stopping):
    """Remove pods of a container that never started, so they cannot pile up.

    A pod stuck before its first start - an image that will not pull, a
    volume or network that will not attach - often never finishes stopping,
    and every Stop and Start left one more behind. Nothing ran in it, so
    removing it at once loses nothing. When stopping, every such pod goes;
    when starting, those still stuck stopping from before."""
    try:
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        selector = ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not selector:
            return []
        query = urllib.parse.quote(",".join(f"{k}={v}" for k, v in selector.items()), safe="")
        pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector={query}").get("items", [])
    except Exception:
        return []
    removed = []
    for pod in pods:
        meta = pod.get("metadata") or {}
        stuck_stopping = meta.get("deletionTimestamp") and age_secs(meta["deletionTimestamp"]) > 20
        if not _never_started(pod) or not (stopping or stuck_stopping):
            continue
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/pods/{meta['name']}?gracePeriodSeconds=0")
            removed.append(meta["name"])
        except Exception:
            pass
    return removed


def is_self(ns, name):
    """The Deployment this Homestead runs as."""
    return (ns, name) == (SELF.NS, NAMES.BRAND)


def is_managed_smb(ns, name):
    return (ns, name) in ((SMB_NAMESPACE, NAMES.object_name("smb")), (SMB_NAMESPACE, "samba"))


def is_managed_nfs(ns, name):
    return (ns, name) == (SMB_NAMESPACE, NFS.NAME)


def guard_managed_smb(ns, name):
    if is_managed_smb(ns, name):
        raise ValueError("Homestead manages SMB and its mounts from Network Shares. "
                         "Change shares or SMB settings there.")
    if is_managed_nfs(ns, name):
        raise ValueError("Homestead manages NFS and its mounts from Network Shares and Settings > Cluster > Add-ons.")


def guard_smb_object(kind, ns, name):
    """Keep the general Kubernetes editor from bypassing Network Shares."""
    if ns != SMB_NAMESPACE:
        return
    kind = str(kind or "").lower()
    if kind in ("deployment", "deployments", "service", "services"):
        guard_managed_smb(ns, name)
    if (kind in ("configmap", "configmaps") and name == SHARES.CONFIGMAP()) or (
            kind in ("secret", "secrets") and name == SHARES.SECRET()):
        raise ValueError("Homestead manages SMB share settings from Network Shares.")


def workload_start_plan(ns, name, replicas=1):
    ns, name = _dns_name(ns, "namespace"), _dns_name(name, "workload name")
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    return PLACE.start_plan(ns, name, replicas, threshold)


def self_stop_warning():
    return (f"Stopping Homestead takes this page down with it, and nothing here can start it again: "
            f"it stays down until someone runs  kubectl -n {SELF.NS} scale deployment/{NAMES.BRAND} --replicas=1  "
            "on the cluster. Restart it instead if it needs a fresh start.")


def guard_self(ns, name, stopping=False, confirmed=False, renaming=False, deleting=False, moving=False):
    """Changes that would take Homestead down from its own page.

    Stopping is allowed once it has been said out loud and confirmed; deleting,
    renaming and moving its storage never are from here - each needs the page
    running to finish, and would leave it gone halfway.
    """
    if not is_self(ns, name):
        return
    if deleting:
        raise ValueError("Homestead cannot delete itself from its own page - that removes this page for good. "
                         "Use helm uninstall, or kubectl, if that is what you want")
    if renaming:
        raise ValueError("Homestead cannot rename itself: its permissions, Service and data are tied to its name")
    if moving:
        raise ValueError("Homestead's own data moves from Settings > About > Redundancy, which restarts it safely; "
                         "a storage move here would stop the page doing the move")
    if stopping and not confirmed:
        raise ValueError(self_stop_warning())


def own_group(ns, name):
    """Homestead's own containers - itself, the Samba that serves shares, the
    backup store moves go through - sit together, apart from your apps,
    unless someone put them in a group of their own."""
    own = {(SELF.NS, NAMES.BRAND), (SMB_NAMESPACE, NAMES.object_name("smb")),
           (SMB_NAMESPACE, NFS.NAME),
           (SMB_NAMESPACE, "samba"), (DEFAULT_NS, OBJECTS.NAME)}
    return OWN_GROUP if (ns, name) in own or ns in PLATFORM_NS else ""


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
        if ns in SYS_NS and ns not in PLATFORM_NS:
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
        # On its own LAN address it answers on its container ports there,
        # with no Service in between.
        lan_ip = (LAN.read(d) or {}).get("address", "")
        if lan_ip and not ports:
            ports = [{"port": cp.get("containerPort"), "ip": lan_ip, "name": cp.get("name", "")}
                     for c in pspec.get("containers", []) or [] for cp in c.get("ports", []) or []
                     if cp.get("containerPort")]
        # The port chosen as the app's own - its web UI, usually - comes
        # first, so the card's first link is the one people want.
        try:
            primary = int(NAMES.read(annotations, "primary-port") or 0)
        except ValueError:
            primary = 0
        if primary:
            ports.sort(key=lambda row: row.get("port") != primary)
            for row in ports:
                row["primary"] = row.get("port") == primary
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
            # Pending and unscheduled says nothing; the scheduler said why.
            unplaced = ""
            placed = conditions.get("PodScheduled") or {}
            if placed.get("status") == "False" and not p["metadata"].get("deletionTimestamp"):
                own = []
                if pspec.get("hostNetwork") and NETWORK.servicelb_present():
                    wanted = {cp.get("containerPort") for c in pspec.get("containers", []) or []
                              for cp in c.get("ports", []) or []}
                    own = sorted({row["port"] for row in ports if row.get("port") in wanted})
                unplaced = UPDATES.explain_unplaced(placed.get("message", ""), own)
                if age_secs(p["metadata"].get("creationTimestamp")) > 60:
                    fatal_waits.append(f"{p['metadata']['name']} cannot be placed: {unplaced}")
            containers = pod_container_rows(p)
            # A pod still fetching its image says how far it has got: events
            # say Pulling, containerd says how many bytes.
            pull = {}
            if not ready and not p["metadata"].get("deletionTimestamp") and any(
                    w["reason"] in ("ContainerCreating", "PodInitializing") for w in waits):
                try:
                    pull = UPDATES.pull_state(ns, p["metadata"]["name"]) or {}
                    if pull.get("state") == "pulling":
                        pull.update(_pull_progress(p["spec"].get("nodeName", ""), pull.get("image", "")))
                    else:
                        pull = {}
                except Exception:
                    pull = {}
            pod_rows.append({"name": p["metadata"]["name"], "hostname": p["spec"].get("hostname", ""),
                             "phase": p["status"].get("phase"),
                             "node": p["spec"].get("nodeName", ""), "ready": ready,
                             # Told to stop, and not stopped yet.
                             "terminating": bool(p["metadata"].get("deletionTimestamp")),
                             "unplaced": unplaced,
                             "pull": pull,
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
        rollout_at = (NAMES.read(template_annotations, "update-rollout-at") or
                      NAMES.read(template_annotations, "restartedAt"))
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
            "group": NAMES.read(annotations, "group") or own_group(ns, name),
            "self": is_self(ns, name),
            "managed_smb": is_managed_smb(ns, name),
            "managed_nfs": is_managed_nfs(ns, name),
            "platform": PLATFORM_NS.get(ns, ""),
            "failover": FAILOVER.mode_of(pspec),
            "lan": (LAN.read(d) or {}).get("address", ""),
        })
    return sorted(out, key=lambda x: (x["ns"], x["name"]))


def workload_group(value):
    """A group's name as stored: its own words, trimmed, one line."""
    group = " ".join(str(value or "").split())
    if len(group) > 40:
        raise ValueError("a group name is at most 40 characters")
    return group


def set_workload_groups(b):
    """Puts workloads in a group, or out of every group with a blank name.

    The group is an annotation on each Deployment, so it travels with the
    workload and needs no list of its own: a group exists while something is
    in it."""
    group = workload_group(b.get("group"))
    items = b.get("items") or []
    if not items:
        raise ValueError("choose at least one workload")
    patch = {"metadata": {"annotations": {NAMES.key("group"): group or None}}}
    targets = [(_dns_name(item.get("ns"), "namespace"), _dns_name(item.get("name"), "workload name"))
               for item in items]
    for ns, name in targets:
        guard_managed_smb(ns, name)
        ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", patch,
              ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    count = len(items)
    return {"ok": True, "group": group,
            "detail": f"{count} workload{'s' if count != 1 else ''} " + (f"moved to {group}" if group else "ungrouped")}


def protection_issues(jobs, target):
    """Protection that has quietly stopped working: a snapshot or backup job
    whose last run failed, or backup jobs with nowhere they can write to.
    Nothing else says so until the copy is needed."""
    issues = []
    guarding = [j for j in jobs or [] if str(j.get("task", "")).split("-")[0] in ("snapshot", "backup")
                and j.get("task") not in ("snapshot-cleanup", "snapshot-delete") and j.get("covers")]
    for job in guarding:
        if job.get("last_failed"):
            issues.append({"severity": "degraded", "kind": "Backup", "name": job["name"],
                           "reason": f"its last {job['task'].split('-')[0]} run failed; its pod's log in longhorn-system says why"})
    if any(j["task"].startswith("backup") for j in guarding):
        target = target or {}
        if not target.get("configured"):
            issues.append({"severity": "degraded", "kind": "Backup", "name": "backup target",
                           "reason": "backup jobs are set up but there is no backup target, so every backup fails"})
        elif not target.get("available"):
            issues.append({"severity": "degraded", "kind": "Backup", "name": "backup target",
                           "reason": f"{target.get('url', 'the backup target')} cannot be reached"
                                     + (f": {target['reason']}" if target.get("reason") else "")})
    return issues


def classify_cluster_health(nodes, workloads, volumes, startup_grace=300, protection=None):
    """Separate real availability faults from normal workload transitions."""
    issues, activities = list(protection or []), []
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
    try:
        protection = protection_issues(cached("lhjobs-health", 60, LH.list_jobs),
                                       cached("lhtarget-health", 60, LH.backup_target))
    except Exception:
        protection = []
    health = classify_cluster_health(nodes, wl, vols, protection=protection)
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
            if p["metadata"]["namespace"] not in SYS_NS
            # File browsers, import/copy jobs and the other short-lived pods
            # Homestead creates are implementation details, not architecture.
            # The task label is shared by all of those helpers and survives a
            # rename, unlike matching one current name such as
            # homestead-files-*.
            and not NAMES.label_of(p.get("metadata") or {}, "task")]
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
        vms = kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
    except Exception:
        vms = []
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

    def vm_ports(ns, name, labels):
        """A VM's ports: those of each Service in its namespace that selects
        it - by the labels on its pods, such as Harvester's vmName - and not
        by app, which is how a container's are found."""
        out = []
        for s in svcs:
            selector = s["spec"].get("selector") or {}
            if (s["metadata"]["namespace"] != ns or not selector or "app" in selector
                    or any(labels.get(k) != v for k, v in selector.items())):
                continue
            ing = s.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
            vip = ing[0].get("ip") if ing else None
            for prt in s["spec"].get("ports", []) or []:
                out.append({"port": prt.get("port"), "name": prt.get("name") or "tcp", "vip": vip})
                if vip:
                    vips.setdefault(vip, []).append({"port": prt.get("port"), "app": name})
        return out

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
    seen, wls, launchers = set(), [], {}
    for p in pods:
        labels = p["metadata"].get("labels", {}) or {}
        # A VM runs in a virt-launcher pod; it is shown as the VM, below, not
        # as a container - and every VM's launcher is not one app.
        if labels.get("kubevirt.io") or labels.get("vm.kubevirt.io/name"):
            if labels.get("kubevirt.io") == "virt-launcher" and p["status"].get("phase") == "Running":
                launchers[(p["metadata"]["namespace"], labels.get("vm.kubevirt.io/name")
                           or labels.get("kubevirt.io/domain", ""))] = p
            continue
        app = labels.get("app") or p["metadata"]["name"].rsplit("-", 2)[0]
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
    # --- virtual machines, running or not: a stopped VM's disks are still here
    vmi_by = {(v["metadata"]["namespace"], v["metadata"]["name"]): v for v in vmis}
    known = [(v, vmi_by.get((v["metadata"]["namespace"], v["metadata"]["name"]), {})) for v in vms]
    named = {(v["metadata"]["namespace"], v["metadata"]["name"]) for v in vms}
    # An instance made without a VirtualMachine is shown by itself.
    known += [({"metadata": v["metadata"], "spec": {"template": {"metadata": {"labels": (v["metadata"].get("labels") or {})},
                                                                "spec": v.get("spec") or {}}}}, v)
              for key, v in vmi_by.items() if key not in named]
    for vm, vmi in known:
        ns, nm = vm["metadata"]["namespace"], vm["metadata"]["name"]
        if ns in SYS_NS:
            continue
        # Its disks as it runs now (hot-plugged ones too), else as defined.
        volumes = ((vmi.get("spec") or {}).get("volumes")
                   or (((vm.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or [])
        addresses = [a for i in (vmi.get("status") or {}).get("interfaces") or []
                     for a in (i.get("ipAddresses") or [i.get("ipAddress")]) if a and ":" not in a]
        row = {"disks": [{"claim": VMS._volume_claim(v)} for v in volumes], "ip": addresses[0] if addresses else "",
               "status": VMS._status(vm, vmi), "node": (vmi.get("status") or {}).get("nodeName", ""),
               "running": (vmi.get("status") or {}).get("phase") == "Running"}
        launcher = launchers.get((ns, nm))
        cu, mu = pmet.get((ns, launcher["metadata"]["name"]), (0, 0)) if launcher else (0, 0)
        pod_labels = dict((launcher or {}).get("metadata", {}).get("labels") or {})
        pod_labels.update(((vm.get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels") or {})
        wls.append({
            "id": "w:vm-" + nm, "name": nm, "kind": "vm", "ns": ns,
            "node": row.get("node") or (vmi.get("status") or {}).get("nodeName", ""),
            "phase": (vmi.get("status") or {}).get("phase", "") or "Stopped",
            "state": str(row.get("status") or ""),
            "running": bool(row.get("running")), "ip": row.get("ip", ""),
            "uptime": age_secs((launcher or {}).get("status", {}).get("startTime")) if launcher else 0,
            "cpu": round(cu, 3), "mem_mb": round(mu / 1048576, 1),
            "image": "", "icon": "", "gpu": False, "hardware": [],
            "claims": [{"pvc": d["claim"], "vid": vol_by_pvc[d["claim"]]["id"] if d["claim"] in vol_by_pvc else ""}
                       for d in row.get("disks") or [] if d.get("claim")],
            "ports": vm_ports(ns, nm, pod_labels),
        })

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
        # Detached is a resting state, not a mystery: nothing is mounting the
        # volume, so there is no live replica health to report.
        "detached": len([v for v in vols if v["state"] != "attached"]),
        "unknown": len([v for v in vols if v["state"] != "attached"]),
        "attached": len([v for v in vols if v["state"] == "attached"]),
        "reasons": [{"name": v.get("pvc_name") or v["name"], "robustness": v["robustness"],
                     "reason": v["health_reason"]}
                    # Only volumes something is actually using: a detached one
                    # has no live health, so it has nothing to explain.
                    for v in vols if v.get("health_reason") and v["robustness"] != "healthy"
                    and v["state"] == "attached"][:8],
        "disks": disks,
    }


# ---------------------------------------------------------------- mutations
def new_claims(volumes):
    """The volumes a deploy creates: one per name, however many folders of it
    are mounted, sized for the largest any of them asks."""
    claims = {}
    for v in volumes:
        if v.get("type") != "pvc" or not v.get("create"):
            continue
        name = _dns_name(v.get("source"), "volume name")
        size = max(1, int(v.get("size_gb") or 5))
        if name in claims:
            claims[name]["size_gb"] = max(claims[name]["size_gb"], size)
            continue
        claims[name] = {"name": name, "size_gb": size,
                        "storage_class": v.get("storage_class") or STORAGE_CLASS,
                        "access_mode": v.get("access_mode") or "ReadWriteOnce"}
    return list(claims.values())


def build_deployment(cfg):
    name = _dns_name(cfg.get("workload_name") or cfg.get("name"), "workload name")
    container_name = _dns_name(cfg.get("container_name") or cfg.get("name"), "container name")
    ns = cfg.get("namespace", DEFAULT_NS)
    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    mounts, volumes, named, owned = [], [], {}, []
    for i, v in enumerate(cfg.get("volumes") or []):
        if v.get("type") == "pod":
            raise ValueError("existing pod volumes can only be used when joining an existing workload")
        # Several folders of one claim share a single volume entry and differ
        # only by subPath, which is how an import lands one volume per app.
        key = (v.get("type") or "pvc", v.get("source") or "")
        reusable = key[0] in ("pvc", "host") and key[1]
        vn = named.get(key) if reusable else None
        if not vn:
            vn = f"vol{len(volumes)}"
            if reusable:
                named[key] = vn
            if v.get("type") == "host":
                volumes.append({"name": vn, "hostPath": {"path": v["source"]}})
            elif v.get("type") == "emptyDir":
                # A RAM-backed scratch volume is what a tmpfs mount becomes:
                # same speed, same volatility, and bounded so it cannot eat the
                # node's memory.
                empty = {}
                if str(v.get("medium", "")).lower() == "memory":
                    empty["medium"] = "Memory"
                if v.get("size_limit"):
                    empty["sizeLimit"] = str(v["size_limit"])
                volumes.append({"name": vn, "emptyDir": empty})
            else:
                volumes.append({"name": vn, "persistentVolumeClaim": {"claimName": v["source"]}})
        mount = {"name": vn, "mountPath": v["path"]}
        owner = (cfg.get("volume_owners") or {}).get(v.get("path"))
        if owner and v.get("type") == "pvc" and v.get("create"):
            owned.append((vn, str(v.get("sub_path") or ""), *owner))
        if v.get("sub_path"):
            mount["subPath"] = str(v["sub_path"]).strip("/")
        if v.get("read_only"):
            mount["readOnly"] = True
        mounts.append(mount)
    ports = [{"containerPort": int(p["container"]),
              "name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
              "protocol": str(p.get("protocol", "TCP")).upper()}
             for p in cfg.get("ports") or []]
    c = {"name": container_name, "image": UPDATES.with_tag(cfg["image"]), "imagePullPolicy": "IfNotPresent"}
    if env: c["env"] = env
    if ports: c["ports"] = ports
    if mounts: c["volumeMounts"] = mounts
    res = {}
    if cfg.get("cpu"): res.setdefault("requests", {})["cpu"] = cfg["cpu"]
    memory_request = str(cfg.get("memory") or "").strip()
    memory_limit = str(cfg.get("memory_limit") or "").strip()
    MEMORY.validate(memory_request, memory_limit, container_name)
    if memory_request: res.setdefault("requests", {})["memory"] = memory_request
    if memory_limit: res.setdefault("limits", {})["memory"] = memory_limit
    if res: c["resources"] = res
    if cfg.get("privileged"): c["securityContext"] = {"privileged": True}
    apply_container_settings(c, cfg)

    podspec = {"containers": [c]}
    if volumes: podspec["volumes"] = volumes
    if owned:
        # A new volume is root's; the image may run as a user of its own.
        podspec["initContainers"] = [VOLOWNER.init_container(owned)]
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
    if any(key in cfg for key in ("privileged", "cap_add", "tun")):
        PRIV.apply(podspec["containers"][0], podspec, cfg, hardware=bool(hardware))
    if cfg.get("fs_group") is not None:
        # The kubelet gives the volume to this group and makes it group
        # writable, which is what lets a container that is not root write to
        # appdata it did not create.
        podspec.setdefault("securityContext", {})["fsGroup"] = int(cfg["fs_group"])
    if cfg.get("network_mode") == "host":
        podspec["hostNetwork"] = True
        podspec["dnsPolicy"] = "ClusterFirstWithHostNet"
    if cfg.get("node"):
        podspec.setdefault("nodeSelector", {})["kubernetes.io/hostname"] = cfg["node"]
    FAILOVER.apply(podspec, cfg.get("failover") or "move")
    lan_address = cfg.get("lan") if cfg.get("network_mode") == "lan" else None
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": {"app": name, NAMES.key("managed"): "true"},
                     "annotations": ({**({NAMES.key("icon"): cfg.get("icon", "")} if cfg.get("icon") else {}),
                                      **({NAMES.key("icon-source"): cfg.get("icon_source", cfg.get("icon", ""))} if cfg.get("icon") else {}),
                                      **({NAMES.key("hardware"): ",".join(sorted(hardware))} if hardware else {})})},
        "spec": {"replicas": int(cfg.get("replicas", 1)), "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name, "lab-workload": "true"}}, "spec": podspec}},
    }
    svc = None
    exposed = [p for p in cfg.get("ports") or [] if p.get("expose")]
    if exposed and cfg.get("network_mode") not in ("host", "lan"):
        mode = cfg.get("vip_mode", "shared")
        vip = cfg.get("lb_ip") if mode in ("manual", "automatic") else (LB_IP if mode == "shared" else "")
        svc_type = "ClusterIP" if cfg.get("network_mode") == "internal" else "LoadBalancer"
        svc = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name, NAMES.key("managed"): "true"},
                         "annotations": PLATFORM.vip_annotations(vip) if svc_type == "LoadBalancer" else {}},
            "spec": {"type": svc_type, "selector": {"app": name},
                     **(PLATFORM.vip_spec(vip) if svc_type == "LoadBalancer" else {}),
                      "ports": [{"name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
                                 "port": int(p.get("host") or p["container"]),
                                 "targetPort": int(p["container"]),
                                 "protocol": str(p.get("protocol", "TCP")).upper()} for p in exposed]},
        }
    if lan_address:
        LAN.apply_to_template(dep, ns, name, lan_address)
    return dep, svc


def edit_lan(ns, name, wanted):
    """Give a container its own LAN address, change it, or take it away."""
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    current = LAN.read(dep)
    if not wanted:
        if not current:
            return ""
        LAN.apply_to_template(dep, ns, name, None)
        dep["metadata"].pop("managedFields", None)
        ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
        LAN.remove_nad(ns, name)
        return f"{name} no longer has a LAN address of its own"
    lan = LAN.clean(wanted)
    if current == lan:
        return ""
    if not current or current.get("address") != lan["address"]:
        problem = vm_address_problem(lan["address"])
        if problem:
            raise ValueError(problem)
    LAN.ensure_nad(ns, name, lan)
    LAN.apply_to_template(dep, ns, name, lan)
    dep["metadata"].pop("managedFields", None)
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    record_lan(ns, name, lan)
    return f"{name} answers on {lan['address']} on the LAN"


def prepare_lan(cfg):
    """A container's own LAN address, checked as free and its network made,
    before the Deployment that joins it."""
    if cfg.get("network_mode") != "lan":
        return cfg
    lan = LAN.clean(cfg.get("lan") or {})
    problem = vm_address_problem(lan["address"])
    if problem:
        raise ValueError(problem)
    cfg["lan"] = lan
    return cfg


def record_lan(ns, name, lan):
    try:
        IPAM.save_record({"ip": lan["address"], "name": name, "kind": "static", "category": "server",
                          "owner": "homestead", "note": f"container {ns}/{name}, on {lan['network']}"})
    except Exception:
        pass


def apply_container_settings(container, cfg):
    """Command, working directory, user and capabilities, when a config has them.

    Compose's entrypoint and command are Kubernetes' command and args: one
    replaces the image's ENTRYPOINT, the other its CMD.
    """
    for key, field in (("command", "command"), ("args", "args")):
        value = cfg.get(key)
        if value:
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"{key} must be a list of words")
            container[field] = list(value)
    if cfg.get("working_dir"):
        container["workingDir"] = str(cfg["working_dir"])
    security = container.setdefault("securityContext", {})
    for key, field in (("run_as_user", "runAsUser"), ("run_as_group", "runAsGroup")):
        if cfg.get(key) is not None and cfg.get(key) != "":
            security[field] = int(cfg[key])
    if cfg.get("cap_add"):
        caps = [str(x).upper() for x in cfg["cap_add"]]
        if not all(re.fullmatch(r"[A-Z_]{2,40}", cap) for cap in caps):
            raise ValueError("capabilities are names like NET_ADMIN")
        security.setdefault("capabilities", {})["add"] = caps
    if not security:
        container.pop("securityContext", None)
    return container


def _dns_name(value, label="name"):
    value = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(f"{label} must use lowercase letters, numbers and dashes")
    return value


def longhorn_state_by_pvc(namespace):
    """Longhorn's live view of each claim: health, and where it is attached.

    A claim that is Bound tells you nothing about whether a second pod on
    another node can mount it, which is exactly the question a storage picker
    is asking.
    """
    try:
        volumes = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return {}
    state = {}
    for volume in volumes:
        status = volume.get("status", {}) or {}
        kubernetes = status.get("kubernetesStatus", {}) or {}
        if kubernetes.get("namespace") != namespace or not kubernetes.get("pvcName"):
            continue
        workloads = sorted({row.get("workloadName") or row.get("podName") or ""
                            for row in kubernetes.get("workloadsStatus") or []
                            if row.get("workloadName") or row.get("podName")})
        state[kubernetes["pvcName"]] = {
            "robustness": status.get("robustness", ""),
            "node": status.get("currentNodeID", ""),
            "migratable": bool((volume.get("spec", {}) or {}).get("migratable")),
            "migrating_to": (volume.get("spec", {}) or {}).get("migrationNodeID", ""),
            "workloads": workloads,
        }
    return state


def _pvc_rows(namespace):
    """Every claim in a namespace, with the Longhorn facts a picker needs."""
    live = longhorn_state_by_pvc(namespace)
    rows = []
    for item in kget(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims").get("items", []):
        spec, status = item.get("spec", {}), item.get("status", {})
        name = item["metadata"]["name"]
        facts = live.get(name, {})
        rows.append({
            "name": name,
            "size": (status.get("capacity", {}) or {}).get("storage") or
                    (spec.get("resources", {}).get("requests", {}) or {}).get("storage", ""),
            "status": status.get("phase", "Unknown"),
            "access_modes": spec.get("accessModes", []) or [],
            "storage_class": spec.get("storageClassName", ""),
            "robustness": facts.get("robustness", ""),
            "node": facts.get("node", ""),
            "migratable": facts.get("migratable", False),
            "workloads": facts.get("workloads", []),
        })
    return sorted(rows, key=lambda row: row["name"])


SAMBA_IMAGE = os.environ.get("SAMBA_IMAGE", "dperson/samba:latest")
SMB_NAME = NAMES.object_name("smb")


def _optional_smb(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _smb_path(kind, name):
    return f"/api{'/v1' if kind == 'services' else 's/apps/v1'}/namespaces/{SMB_NAMESPACE}/{kind}/{name}"


def _smb_new_object(old, name):
    """Prepare a copied Kubernetes object for POST under its new name."""
    previous_address = next((item.get("ip", "") for item in
                             ((old.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []), "")
    body = copy.deepcopy(old)
    body.pop("status", None)
    meta = body.setdefault("metadata", {})
    for field in ("resourceVersion", "uid", "generation", "creationTimestamp", "managedFields",
                  "selfLink", "deletionTimestamp", "deletionGracePeriodSeconds", "ownerReferences"):
        meta.pop(field, None)
    meta["name"] = name
    meta.setdefault("labels", {})["app"] = name
    meta["labels"][NAMES.key("managed")] = "true"
    if body.get("kind") == "Service":
        body["spec"]["selector"] = {"app": name}
        annotations = meta.setdefault("annotations", {})
        if (previous_address and body["spec"].get("type") == "LoadBalancer"
                and not any(key in annotations for key in
                            ("kube-vip.io/loadbalancerIPs", "metallb.universe.tf/loadBalancerIPs"))
                and not body["spec"].get("loadBalancerIP")):
            annotations.update(PLATFORM.vip_annotations(previous_address))
        for field in ("clusterIP", "clusterIPs", "ipFamilies", "healthCheckNodePort"):
            body["spec"].pop(field, None)
        for port in body["spec"].get("ports", []) or []:
            port.pop("nodePort", None)
    else:
        body["spec"]["selector"] = {"matchLabels": {"app": name}}
        body["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})["app"] = name
        body["spec"]["template"]["spec"]["containers"][0]["name"] = name
    return body


def _migrate_samba(legacy):
    with SHARES.LOCK:
        return _migrate_samba_locked(legacy)


def _migrate_samba_locked(legacy):
    """Recreate the legacy workload under Homestead's name and preserve its VIP.

    The replacement starts at zero replicas so RWO claims are never mounted by
    both pods. If cutover fails, the old Deployment and Service are restored.
    """
    old_dep_path = _smb_path("deployments", "samba")
    new_dep_path = _smb_path("deployments", SMB_NAME)
    old_svc_path = _smb_path("services", "samba")
    new_svc_path = _smb_path("services", SMB_NAME)
    old_service = _optional_smb(old_svc_path)
    rows, credentials, *_ = SHARES._state()
    new_dep = _smb_new_object(legacy, SMB_NAME)
    replicas = int((legacy.get("spec") or {}).get("replicas", 1) or 0)
    new_dep["spec"]["replicas"] = 0
    new_dep = SHARES.configured_deployment(new_dep, rows, credentials)
    if old_service:
        new_service = _smb_new_object(old_service, SMB_NAME)
    else:
        cfg = {"name": SMB_NAME, "container_name": SMB_NAME, "image": SAMBA_IMAGE,
               "namespace": SMB_NAMESPACE, "ports": [{"container": 445, "name": "smb", "expose": True}],
               "vip_mode": "automatic"}
        cfg = NETWORK.prepare_deploy(cfg)
        _, new_service = build_deployment(cfg)
    made_dep = removed_service = made_service = stopped_old = False
    try:
        ksend("POST", f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments", new_dep)
        made_dep = True
        if old_service:
            ksend("DELETE", old_svc_path)
            removed_service = True
        ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", new_service)
        made_service = True
        if replicas:
            ksend("PATCH", old_dep_path, {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
            stopped_old = True
            ksend("PATCH", new_dep_path, {"spec": {"replicas": replicas}},
                  ctype="application/merge-patch+json")
            deadline = time.time() + SHARES.ROLLOUT_TIMEOUT
            while time.time() < deadline:
                live = _optional_smb(new_dep_path) or {}
                status = live.get("status") or {}
                if (int(status.get("readyReplicas", 0) or 0) >= replicas and
                        int(status.get("updatedReplicas", 0) or 0) >= replicas and
                        int(status.get("observedGeneration", 0) or 0) >=
                        int((live.get("metadata") or {}).get("generation", 0) or 0)):
                    break
                time.sleep(1.5)
            else:
                raise ValueError("homestead-smb did not become ready; the former Samba workload was restored")
        ksend("DELETE", old_dep_path)
    except Exception as error:
        recovery = []
        for action in (
            lambda: ksend("PATCH", new_dep_path, {"spec": {"replicas": 0}},
                          ctype="application/merge-patch+json") if made_dep else None,
            lambda: ksend("DELETE", new_svc_path) if made_service else None,
            lambda: ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services",
                          _smb_new_object(old_service, "samba")) if removed_service else None,
            lambda: ksend("PATCH", old_dep_path, {"spec": {"replicas": replicas}},
                          ctype="application/merge-patch+json") if stopped_old else None,
            lambda: ksend("DELETE", new_dep_path) if made_dep else None,
        ):
            try:
                action()
            except Exception as failure:
                recovery.append(str(failure)[:120])
        if recovery:
            raise RuntimeError(f"SMB migration failed: {error}; recovery needs attention: "
                               + "; ".join(recovery)) from error
        raise
    _cache.pop("wl", None); _cache.pop("network", None)
    return kget(new_dep_path)


def install_samba(address=""):
    """The Samba server shares are served from, made when the first share is:
    SMB on port 445 at an address of its own - the one chosen, or the next
    free one. Shares are its arguments, which Homestead writes, so it starts
    with none."""
    existing = _optional_smb(_smb_path("deployments", SMB_NAME))
    if existing:
        return existing
    legacy = _optional_smb(_smb_path("deployments", "samba"))
    if legacy:
        return _migrate_samba(legacy)
    cfg = {"name": SMB_NAME, "container_name": SMB_NAME, "image": SAMBA_IMAGE, "namespace": SMB_NAMESPACE,
           "ports": [{"container": 445, "name": "smb", "protocol": "TCP", "expose": True}],
           "vip_mode": "manual" if address else "automatic", "lb_ip": address,
           "args": ["-p", "-g", "server min protocol = SMB2"]}
    cfg = NETWORK.prepare_deploy(cfg)
    dep, svc = build_deployment(cfg)
    created = ksend("POST", f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments", dep)
    if svc:
        try:
            ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", svc)
        except Exception:
            ksend("DELETE", _smb_path("deployments", SMB_NAME))
            raise
    _cache.pop("wl", None); _cache.pop("network", None)
    return created


def rollout_review_context(current):
    meta = current.get("metadata") or {}
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("workload identity/version is unavailable; refresh before changing its pod")
    return {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}


def deploy_capacity_plan(config, existing=None):
    """Read-only new-workload or shared-pod rollout preview."""
    cfg = analyze_deploy_intent(copy.deepcopy(config))
    ns = _dns_name(cfg.get("namespace") or DEFAULT_NS, "namespace")
    cfg["namespace"] = ns
    joining = cfg.get("target_mode") == "existing"
    if joining:
        target = _dns_name(cfg.get("target_workload"), "existing workload")
        if existing is None:
            existing = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        dep, _ = build_sidecar_deployment(cfg, existing)
    elif cfg.get("target_mode", "new") == "new":
        dep, _ = build_deployment(cfg)
    else:
        raise ValueError("deployment target must be new or existing")
    claims = {row["name"]: row for row in new_claims(cfg.get("volumes") or [])}
    for row in claims.values():
        if row["access_mode"] not in ("ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany", "ReadWriteOncePod"):
            raise ValueError("invalid volume access mode")
        # StorageClass names may contain dots, unlike our workload names.
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", row["storage_class"]):
            raise ValueError("invalid storage class name")
    pspec = dep["spec"]["template"]["spec"]
    if not joining and claims and not pspec.get("initContainers"):
        # Owner discovery reads the image later and may add this init stage.
        # Include its request now without fetching layers or writing a helper.
        pspec["initContainers"] = [VOLOWNER.init_container([])]
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    if joining:
        return ROLLOUT_CAPACITY.plan(existing, dep, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
                                     threshold, planned_claims=claims)
    return PLACE.manifest_plan(dep, ns, dep["metadata"]["name"], dep["spec"]["replicas"],
                               threshold, planned_claims=claims)


def reviewed_deploy(b):
    """Deploy/App Store endpoint guard; Compose needs a whole-batch review."""
    b = ensure_profile_compatible(analyze_deploy_intent(copy.deepcopy(b)))
    if b.get("target_mode", "new") not in ("new", "existing"):
        raise ValueError("deployment target must be new or existing")
    current = None
    context = None
    if b.get("target_mode") == "existing":
        ns = _dns_name(b.get("namespace") or DEFAULT_NS, "namespace")
        target = _dns_name(b.get("target_workload"), "existing workload")
        current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        context = rollout_review_context(current)
    # Bind the original input and, for a shared pod, the fresh controller
    # version. The final PUT keeps this resourceVersion for optimistic locking.
    plan = deploy_capacity_plan(b, existing=current)
    CAPACITY_REVIEW.enforce(b, plan, context)
    return run_deploy(b, reviewed_current=current) if current is not None else run_deploy(b)


def run_deploy(b, *, reviewed_current=None):
    """Create (or join) a workload. HTTP Deploy uses reviewed_deploy first."""
    b = analyze_deploy_intent(b)
    ns = b.get("namespace") or DEFAULT_NS
    target = b.get("target_workload") if b.get("target_mode") == "existing" else b.get("workload_name") or b.get("name")
    guard_managed_smb(ns, target)
    b = ensure_profile_compatible(b)
    persist_icon_config(b)
    b = NETWORK.prepare_deploy(b)
    b = apply_deploy_bindings(b)
    b = apply_generated_secrets(b)
    b = prepare_lan(b)
    ns = b.get("namespace") or DEFAULT_NS
    target_mode = b.get("target_mode", "new")
    if target_mode == "new":
        b = VOLOWNER.prepare(b)
    if target_mode == "existing":
        target = _dns_name(b.get("target_workload"), "existing workload")
        current = copy.deepcopy(reviewed_current) if reviewed_current is not None else kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        dep, svc = build_sidecar_deployment(b, current)
    elif target_mode == "new":
        dep, svc = build_deployment(b)
        target = dep["metadata"]["name"]
    else:
        raise ValueError("deployment target must be new or existing")
    reused = []
    for claim in new_claims(b.get("volumes") or []):
        if ensure_claim(ns, claim["name"], claim["size_gb"], claim["storage_class"], claim["access_mode"]):
            reused.append(claim["name"])
    b["_reused_claims"] = reused
    if b.get("network_mode") == "lan" and target_mode == "new":
        LAN.ensure_nad(ns, target, b["lan"])
    if target_mode == "existing":
        ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{target}", dep)
    else:
        ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
    if b.get("network_mode") == "lan" and target_mode == "new":
        record_lan(ns, target, b["lan"])
    if svc:
        ksend("POST", f"/api/v1/namespaces/{ns}/services", svc)
    _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("network", None)
    container_name = _dns_name(b.get("container_name") or b.get("name"), "container name")
    action = f"Add {container_name} to {target}" if target_mode == "existing" else f"Deploy {target}"
    op = OPS.start("deployment", action,
                   {"kind": "Deployment", "name": target, "namespace": ns},
                   "/containers", {"namespace": ns, "name": target,
                                   # What cancelling it undoes: a new one is removed,
                                   # an existing one goes back to its last version.
                                   "undo": "rollout" if target_mode == "existing" else "delete"})
    return {"ok": True, "name": target, "container": container_name, "operation": op,
            "reused_volumes": b.get("_reused_claims") or []}


def compose_report(b):
    """Read a pasted Compose file against what this namespace already holds."""
    ns = str(b.get("namespace") or DEFAULT_NS)
    _dns_name(ns, "namespace")
    text = str(b.get("text") or "")
    if len(text) > 256 * 1024:
        raise ValueError("a Compose file over 256 KB is more than Homestead will read")
    try:
        workloads = {d["metadata"]["name"] for d in
                     kget(f"/apis/apps/v1/namespaces/{ns}/deployments").get("items", [])}
    except Exception:
        workloads = set()
    try:
        claims = {c["metadata"]["name"] for c in
                  kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])}
    except Exception:
        claims = set()
    vip_mode = b.get("vip_mode") if b.get("vip_mode") in ("shared", "auto", "nodes") else "shared"
    if NETWORK.node_addresses_only():
        # On k3s every service shares the nodes' addresses, so ports must differ.
        vip_mode = "shared"
    return COMPOSE.convert(text, b.get("variables") or "", ns, workloads, claims,
                           HW.features(), vip_mode)


def compose_apply(b):
    """Create every chosen service of a Compose file, dependencies first.

    Read again here rather than trusted from the page, so what is created is
    what was checked. It stops at the first failure and says what was made.
    """
    report = compose_report(b)
    chosen = set(b.get("services") or [row["name"] for row in report["services"]])
    rows = {row["name"]: row for row in report["services"] if row["name"] in chosen}
    if not rows:
        raise ValueError("choose at least one service to create")
    problems = report["errors"] + [dict(e, service=name) for name, row in rows.items() for e in row["errors"]]
    if problems:
        first = problems[0]
        where = f"{first['service']}: " if first.get("service") else ""
        raise ValueError(f"fix the file first: {where}{first['message']}"
                         + (f" (line {first['line']})" if first.get("line") else ""))
    created = []
    for name in report["order"]:
        if name not in rows:
            continue
        try:
            result = run_deploy(copy.deepcopy(rows[name]["config"]))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            return {"ok": False, "created": created, "failed": name, "error": f"HTTP {error.code}: {detail}"}
        except Exception as error:
            return {"ok": False, "created": created, "failed": name, "error": str(error)}
        created.append(result["name"])
    return {"ok": True, "created": created}


def deploy_options(ns):
    """Return the live choices needed by the deployment editor."""
    deployments = []
    for item in kget(f"/apis/apps/v1/namespaces/{ns}/deployments").get("items", []):
        pspec = item.get("spec", {}).get("template", {}).get("spec", {})
        volumes = []
        for volume in pspec.get("volumes", []) or []:
            row = {"name": volume.get("name", ""), "kind": "other", "source": ""}
            if volume.get("persistentVolumeClaim"):
                row.update({"kind": "pvc", "source": volume["persistentVolumeClaim"].get("claimName", "")})
            elif volume.get("hostPath"):
                row.update({"kind": "host", "source": volume["hostPath"].get("path", "")})
            elif volume.get("emptyDir") is not None:
                row["kind"] = "emptyDir"
            elif volume.get("configMap"):
                row.update({"kind": "configMap", "source": volume["configMap"].get("name", "")})
            elif volume.get("secret"):
                row.update({"kind": "secret", "source": volume["secret"].get("secretName", "")})
            volumes.append(row)
        deployments.append({
            "name": item["metadata"]["name"],
            "containers": [c.get("name", "") for c in pspec.get("containers", []) or []],
            "volumes": volumes,
        })
    classes = storage_classes()
    return {"deployments": sorted(deployments, key=lambda x: x["name"]),
            "pvcs": _pvc_rows(ns),
            "storage_classes": selectable_storage_classes(classes),
            "shared_storage_classes": shared_storage_classes(classes),
            "storage_class_facts": storage_class_facts(classes)}


def share_storage_options():
    """Volumes a new share can be created on, in the namespace Samba runs in."""
    ns = SMB_NAMESPACE
    classes = storage_classes()
    node = ""
    try:
        node = SHARES._samba_node()
    except Exception:
        node = ""
    try:
        samba = bool(SHARES._deployment_state()[2])
    except Exception:
        samba = False
    return {"namespace": ns, "node": node, "pvcs": _pvc_rows(ns), "samba_installed": samba,
            "storage_classes": selectable_storage_classes(classes),
            "shared_storage_classes": shared_storage_classes(classes),
            "storage_class_facts": storage_class_facts(classes)}


def _unique_volume_name(base, used):
    base = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:55] or "volume"
    candidate, suffix = base, 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = base[:63 - len(tail)].rstrip("-") + tail
        suffix += 1
    used.add(candidate)
    return candidate


def build_sidecar_deployment(cfg, current):
    """Add one container to an existing Deployment pod template.

    Kubernetes cannot modify a running Pod. Updating the controller template causes a
    reviewed rollout, so all containers in the workload restart together.
    """
    container_name = _dns_name(cfg.get("container_name") or cfg.get("name"), "container name")
    target = _dns_name(cfg.get("target_workload"), "existing workload")
    if current.get("metadata", {}).get("name") != target:
        raise ValueError("existing workload does not match the selected Deployment")
    if cfg.get("network_mode") == "host":
        raise ValueError("host networking cannot be enabled while joining an existing workload")

    updated = json.loads(json.dumps(current))
    pspec = updated.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    containers = pspec.setdefault("containers", [])
    if any(c.get("name") == container_name for c in containers):
        raise ValueError(f"container {container_name} already exists in {target}")

    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    ports = [{"containerPort": int(p["container"]),
              "name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
              "protocol": str(p.get("protocol", "TCP")).upper()}
             for p in cfg.get("ports") or [] if p.get("container")]
    container = {"name": container_name, "image": UPDATES.with_tag(cfg["image"]), "imagePullPolicy": "IfNotPresent"}
    if env:
        container["env"] = env
    if ports:
        container["ports"] = ports
    resources = {}
    if cfg.get("cpu"):
        resources.setdefault("requests", {})["cpu"] = cfg["cpu"]
    memory_request = str(cfg.get("memory") or "").strip()
    memory_limit = str(cfg.get("memory_limit") or "").strip()
    MEMORY.validate(memory_request, memory_limit, container_name)
    if memory_request:
        resources.setdefault("requests", {})["memory"] = memory_request
    if memory_limit:
        resources.setdefault("limits", {})["memory"] = memory_limit
    if resources:
        container["resources"] = resources
    if cfg.get("privileged"):
        container["securityContext"] = {"privileged": True}

    pod_volumes = pspec.setdefault("volumes", [])
    existing_by_name = {v.get("name"): v for v in pod_volumes}
    used = set(existing_by_name)
    mounts = []
    for index, item in enumerate(cfg.get("volumes") or []):
        mount_path = (item.get("path") or "").strip()
        if not mount_path:
            continue
        if item.get("type") == "pod":
            volume_name = item.get("source", "")
            if volume_name not in existing_by_name:
                raise ValueError(f"pod volume {volume_name or '(blank)'} does not exist in {target}")
        else:
            source = (item.get("source") or "").strip()
            if not source and item.get("type") != "emptyDir":
                raise ValueError(f"storage source is required for {mount_path}")
            volume_name = ""
            if item.get("type") not in ("host", "emptyDir"):
                volume_name = next((v.get("name") for v in pod_volumes
                                    if v.get("persistentVolumeClaim", {}).get("claimName") == source), "")
            if not volume_name:
                volume_name = _unique_volume_name(f"hs-{container_name}-{index + 1}", used)
                if item.get("type") == "host":
                    pod_volumes.append({"name": volume_name, "hostPath": {"path": source}})
                elif item.get("type") == "emptyDir":
                    pod_volumes.append({"name": volume_name, "emptyDir": {}})
                else:
                    pod_volumes.append({"name": volume_name,
                                        "persistentVolumeClaim": {"claimName": source}})
        mount = {"name": volume_name, "mountPath": mount_path}
        if item.get("sub_path") and item.get("type") != "emptyDir":
            mount["subPath"] = str(item["sub_path"]).strip("/")
        if item.get("read_only"):
            mount["readOnly"] = True
        mounts.append(mount)

    hardware = set(cfg.get("hardware") or [])
    if cfg.get("gpu"):
        hardware.add("igpu")
    devices = {feature["id"]: HW.mount_spec(feature) for feature in HW.features()}
    unknown = hardware - set(devices)
    if unknown:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
    for feature_id in hardware:
        device = devices[feature_id]
        pspec.setdefault("nodeSelector", {})[device["label"]] = "true"
        container.setdefault("securityContext", {})["privileged"] = True
        volume_name = next((v.get("name") for v in pod_volumes
                            if v.get("hostPath", {}).get("path") == device["host_path"]), "")
        if not volume_name:
            volume_name = _unique_volume_name(device["name"], used)
            pod_volumes.append({"name": volume_name, "hostPath": {
                "path": device["host_path"], "type": device["path_type"]}})
        mounts.append({"name": volume_name, "mountPath": device["container_path"]})
    if mounts:
        container["volumeMounts"] = mounts
    containers.append(container)

    selector = current.get("spec", {}).get("selector", {}).get("matchLabels", {})
    service_cfg = dict(cfg)
    service_cfg["name"] = container_name
    # Only the Service portion is needed here. Pod storage and hardware have
    # already been merged above and pod-volume references are join-only.
    service_cfg["volumes"] = []
    service_cfg["hardware"] = []
    service_cfg["gpu"] = False
    _, service = build_deployment(service_cfg)
    if service:
        service["spec"]["selector"] = selector
    annotation_name = ("sidecar-" + container_name)[:63].rstrip("-")
    updated.setdefault("metadata", {}).setdefault("annotations", {})[
        NAMES.key(annotation_name)] = cfg.get("image", "")
    return updated, service


# Harvester keeps these for VM images and VM state; they are not general
# purpose storage and Harvester's own UI marks them internal.
INTERNAL_STORAGE_CLASSES = {"longhorn-static", "vmstate-persistence"}


def _internal_class(meta):
    annotations = meta.get("annotations", {}) or {}
    return (meta.get("name", "") in INTERNAL_STORAGE_CLASSES or
            str(annotations.get("harvesterhci.io/is-reserved-storageclass", "")).lower() == "true")


def storage_classes():
    """Every StorageClass, with the one fact that decides if RWX will work.

    A class with migratable=true hands out two-controller volumes so a VM disk
    can live-migrate. Longhorn's CSI driver refuses to filesystem-mount those,
    so a ReadWriteMany claim created on such a class can never be mounted by a
    pod - it binds happily and then strands whatever tries to use it.
    """
    rows = []
    for item in kget("/apis/storage.k8s.io/v1/storageclasses").get("items", []):
        meta = item.get("metadata", {}) or {}
        parameters = item.get("parameters", {}) or {}
        annotations = meta.get("annotations", {}) or {}
        rows.append({
            "name": meta.get("name", ""),
            "provisioner": item.get("provisioner", ""),
            "parameters": parameters,
            "replicas": parameters.get("numberOfReplicas", ""),
            # Longhorn's data engine: v1 (iSCSI, the default) or v2 (SPDK).
            "engine": ((str(parameters.get("dataEngine") or "v1").lower())
                       if item.get("provisioner") == "driver.longhorn.io" else ""),
            "migratable": str(parameters.get("migratable", "")).lower() == "true",
            "encrypted": str(parameters.get("encrypted", "")).lower() == "true",
            "data_locality": parameters.get("dataLocality", ""),
            # Longhorn places replicas only on disks and nodes with every tag.
            "disk_tags": [t for t in str(parameters.get("diskSelector") or "").split(",") if t],
            "node_tags": [t for t in str(parameters.get("nodeSelector") or "").split(",") if t],
            "expandable": bool(item.get("allowVolumeExpansion")),
            "reclaim": item.get("reclaimPolicy", "Delete"),
            "default": annotations.get("storageclass.kubernetes.io/is-default-class") == "true",
            "internal": _internal_class(meta),
            "made_for": _class_made_for(meta.get("name", ""), parameters),
        })
    rows = sorted(rows, key=lambda row: row["name"])
    _note_default_class(rows)
    return rows


def _class_made_for(name, parameters):
    """A class made for one thing, not for choosing: a Harvester image's
    (its disks start as that image - named longhorn-image-* or lh-<uuid>,
    with the image as its backing image) or a restore's (Homestead's
    homestead-restore-*, reading one backup). Neither belongs in a picker."""
    if name.startswith("homestead-restore-") or parameters.get("fromBackup"):
        return "restore"
    if parameters.get("backingImage") or name.startswith("longhorn-image-"):
        return "image"
    return ""


def class_selectable(row):
    return not row.get("internal") and not row.get("made_for")


# The class new volumes go on when none is chosen. The cluster's own default
# wins - making a class the default in Volumes is how you say which - then
# the STORAGE_CLASS Homestead was installed with, then Longhorn's usual one.
ENV_STORAGE_CLASS = os.environ.get("STORAGE_CLASS", "longhorn-r2")


def _note_default_class(rows):
    global STORAGE_CLASS
    usable = [row for row in rows if class_selectable(row)]
    names = {row["name"] for row in usable}
    chosen = (next((row["name"] for row in usable if row["default"]), "")
              or (ENV_STORAGE_CLASS if ENV_STORAGE_CLASS in names else "")
              or next((n for n in ("longhorn-r2", "harvester-longhorn", "longhorn") if n in names), "")
              or STORAGE_CLASS)
    if chosen and chosen != STORAGE_CLASS:
        STORAGE_CLASS = chosen
        for module in ("LC", "LH"):
            if module in globals():
                globals()[module].STORAGE_CLASS = chosen


def cleanup_restore_classes():
    """Delete the classes restores left behind once nothing waits on them.

    A restore makes a class that reads one backup; the claim made from it
    keeps its volume when the class goes, so the class is only needed until
    the claim is bound. Left behind, they filled every class picker."""
    try:
        pending = {(p.get("spec") or {}).get("storageClassName")
                   for p in kget("/api/v1/persistentvolumeclaims").get("items", [])
                   if (p.get("status") or {}).get("phase") != "Bound"}
        items = kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])
    except Exception:
        return []
    removed = []
    for item in items:
        name = item["metadata"]["name"]
        if name.startswith("homestead-restore-") and name not in pending:
            try:
                ksend("DELETE", f"/apis/storage.k8s.io/v1/storageclasses/{name}")
                removed.append(name)
            except Exception:
                pass
    for key in list(_cache):
        if key.startswith(("stor", "sc")):
            _cache.pop(key, None)
    return removed


DEFAULT_CLASS_ANNOTATION = "storageclass.kubernetes.io/is-default-class"
LONGHORN_PROVISIONER = "driver.longhorn.io"


def storage_class_usage():
    """How many claims each class is backing, so deletion can be guarded."""
    counts = {}
    for item in kget("/api/v1/persistentvolumeclaims").get("items", []):
        name = (item.get("spec", {}) or {}).get("storageClassName") or ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def storage_class_inventory():
    usage = storage_class_usage()
    rows = storage_classes()
    for row in rows:
        row["in_use"] = usage.get(row["name"], 0)
    return rows


def create_storage_class(cfg):
    """Create a Longhorn StorageClass.

    Kubernetes treats a StorageClass as immutable apart from its default flag
    and expansion setting, so Homestead offers create and delete rather than an
    edit that would silently do nothing.
    """
    name = _dns_name(cfg.get("name"), "storage class name")
    if any(row["name"] == name for row in storage_classes()):
        raise ValueError(f"storage class {name} already exists")
    replicas = int(cfg.get("replicas", 2) or 2)
    if not 1 <= replicas <= 5:
        raise ValueError("replica count must be between 1 and 5")
    stale = int(cfg.get("stale_replica_timeout", 30) or 30)
    if not 1 <= stale <= 2880:
        raise ValueError("stale replica timeout must be between 1 and 2880 minutes")
    reclaim = str(cfg.get("reclaim_policy") or "Delete")
    if reclaim not in ("Delete", "Retain"):
        raise ValueError("reclaim policy must be Delete or Retain")
    # Written explicitly, the way Harvester writes its own classes: a blank
    # parameter and an explicit false behave the same in Longhorn but do not
    # read the same to anyone comparing two classes.
    parameters = {"numberOfReplicas": str(replicas), "staleReplicaTimeout": str(stale),
                  "migratable": "true" if cfg.get("migratable") else "false",
                  "encrypted": "true" if cfg.get("encrypted") else "false"}
    engine = str(cfg.get("engine") or "v1").lower()
    if engine not in ("v1", "v2"):
        raise ValueError("the data engine is v1 or v2")
    warning = ""
    disk_tags, node_tags = DISKS.clean_tags(cfg.get("disk_tags")), DISKS.clean_tags(cfg.get("node_tags"))
    if disk_tags:
        parameters["diskSelector"] = ",".join(disk_tags)
    if node_tags:
        parameters["nodeSelector"] = ",".join(node_tags)
    if disk_tags or node_tags:
        # Every replica needs a node of its own with a disk that fits; fewer
        # than that and a volume runs a copy short, or does not start at all.
        reach = DISKS.tag_reach(disk_tags, node_tags)
        wanted = " and ".join(x for x in (f"a disk tagged {', '.join(disk_tags)}" if disk_tags else "",
                                          f"the node tags {', '.join(node_tags)}" if node_tags else "") if x)
        if not reach:
            warning += f"; no node has {wanted} yet, so its volumes will not schedule until one does"
        elif len(reach) < replicas:
            warning += (f"; only {', '.join(reach)} ha{'s' if len(reach) == 1 else 've'} {wanted}, fewer than "
                        f"its {replicas} replicas, so its volumes will run degraded")
    if engine == "v2":
        parameters["dataEngine"] = "v2"
        v2 = v2_engine_status()
        if not v2["enabled"]:
            warning = "; V2 is switched off in Longhorn, so its volumes will not schedule until it is on"
        elif not v2["ready_nodes"]:
            warning = "; no node has a V2 disk and hugepages yet, so its volumes will not schedule"
    body = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": name, "labels": {NAMES.key("managed"): "true"},
                         "annotations": {DEFAULT_CLASS_ANNOTATION: "true"} if cfg.get("default") else {}},
            "provisioner": str(cfg.get("provisioner") or LONGHORN_PROVISIONER),
            "parameters": parameters,
            "allowVolumeExpansion": bool(cfg.get("expandable", True)),
            "reclaimPolicy": reclaim,
            "volumeBindingMode": "Immediate"}
    if cfg.get("default"):
        _clear_default_class(name)
    ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", body)
    return {"ok": True, "name": name, "classes": storage_class_inventory(),
            "message": (f"Storage class {name} created" +
                        (" and made the default" if cfg.get("default") else "") + warning)}


V2_HUGEPAGES_MB = 2048


def _quantity_mb(value):
    """A Kubernetes memory quantity in MiB: "2Gi", "1024Mi", "0"."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]i?)?", str(value or "0").strip())
    if not match:
        return 0
    number, unit = float(match.group(1)), match.group(2) or ""
    scale = {"": 1 / 1024**2, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024**2,
             "K": 1000 / 1024**2, "M": 1000**2 / 1024**2, "G": 1000**3 / 1024**2, "T": 1000**4 / 1024**2}[unit]
    return int(number * scale)


def v2_engine_status():
    """Whether Longhorn's V2 data engine can take volumes, node by node.

    V2 (SPDK) needs three things: the engine switched on - on Harvester by its
    own longhorn-v2-data-engine-enabled setting, which drives Longhorn's - and,
    on each node that will hold a replica, a disk handed to Longhorn as a block
    device and 2 GiB of hugepages. Without them a V2 volume never schedules,
    which is worth knowing before creating a class for it."""
    def setting(path):
        try:
            item = kget(path)
            return str(item.get("value") or item.get("default") or "").lower() == "true"
        except Exception:
            return None

    enabled = setting("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/settings/v2-data-engine")
    harvester = setting("/apis/harvesterhci.io/v1beta1/settings/longhorn-v2-data-engine-enabled")
    try:
        hugepages = {node["metadata"]["name"]: _quantity_mb(
            ((node.get("status", {}) or {}).get("allocatable", {}) or {}).get("hugepages-2Mi"))
            for node in kget("/api/v1/nodes").get("items", [])}
    except Exception:
        hugepages = {}
    nodes = []
    try:
        longhorn_nodes = kget("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes").get("items", [])
    except Exception:
        longhorn_nodes = []
    try:
        probes = node_temps()
    except Exception:
        probes = {}
    for node in longhorn_nodes:
        name = node["metadata"]["name"]
        disks = ((node.get("spec", {}) or {}).get("disks", {}) or {}).values()
        block = [disk for disk in disks if str(disk.get("diskType", "")).lower() == "block"
                 and disk.get("allowScheduling", True)]
        pages = hugepages.get(name, 0)
        facts = (probes.get(name) or {}).get("v2") or {}
        modules = facts.get("modules") or {}
        cpu = True if facts.get("arch") in ("aarch64", "arm64") else facts.get("sse4_2")
        # Each check: True, False, or None where the node probe has not said.
        checks = {"cpu": cpu,
                  "modules": all(modules.values()) if modules else None,
                  "hugepages": pages >= V2_HUGEPAGES_MB,
                  "disk": bool(block)}
        missing = ([] if block else ["a V2 (block) disk"]) + (
            [] if pages >= V2_HUGEPAGES_MB else [f"{V2_HUGEPAGES_MB // 1024} GiB of hugepages (has {pages} MiB)"]) + (
            [f"kernel modules {', '.join(m for m, ok in modules.items() if not ok)}"] if checks["modules"] is False else []) + (
            ["a CPU with SSE4.2"] if cpu is False else [])
        nodes.append({"name": name, "block_disks": len(block), "hugepages_mb": pages,
                      "ready": not missing, "missing": missing, "checks": checks,
                      "missing_modules": [m for m, ok in modules.items() if not ok]})
    ready = sum(1 for node in nodes if node["ready"])
    platform = PLATFORM.detect()
    try:
        longhorn = COMPONENTS.longhorn_version()
    except Exception:
        longhorn = ""
    return {"enabled": bool(enabled), "harvester_setting": harvester, "nodes": nodes,
            "ready_nodes": ready, "total_nodes": len(nodes),
            "distribution": platform.get("distribution", ""), "longhorn_version": longhorn,
            # V2 is Longhorn's to run from 1.8; before that it is an experiment.
            "longhorn_ok": bool(COMPONENTS.parse(longhorn)) and COMPONENTS.parse(longhorn)[:2] >= (1, 8)}


def _clear_default_class(keep):
    for row in storage_classes():
        if row["default"] and row["name"] != keep:
            ksend("PATCH", f"/apis/storage.k8s.io/v1/storageclasses/{row['name']}",
                  {"metadata": {"annotations": {DEFAULT_CLASS_ANNOTATION: "false"}}},
                  ctype="application/merge-patch+json")


def set_default_storage_class(name):
    name = _dns_name(name, "storage class name")
    row = next((item for item in storage_classes() if item["name"] == name), None)
    if not row:
        raise ValueError(f"storage class {name} does not exist")
    if row["internal"]:
        raise ValueError(f"{name} is reserved by Harvester and cannot be the default")
    _clear_default_class(name)
    ksend("PATCH", f"/apis/storage.k8s.io/v1/storageclasses/{name}",
          {"metadata": {"annotations": {DEFAULT_CLASS_ANNOTATION: "true"}}},
          ctype="application/merge-patch+json")
    return {"ok": True, "classes": storage_class_inventory(),
            "message": f"{name} is now the default storage class"}


def delete_storage_class(name):
    name = _dns_name(name, "storage class name")
    rows = storage_class_inventory()
    row = next((item for item in rows if item["name"] == name), None)
    if not row:
        raise ValueError(f"storage class {name} does not exist")
    if row["internal"]:
        raise ValueError(f"{name} is reserved by Harvester and must not be deleted")
    if row["default"]:
        raise ValueError(f"{name} is the default class; make another class the default first")
    if row["in_use"]:
        raise ValueError(f"{name} still backs {row['in_use']} claim"
                         f"{'s' if row['in_use'] != 1 else ''}; existing volumes keep working, "
                         "but the class cannot be removed while claims reference it")
    ksend("DELETE", f"/apis/storage.k8s.io/v1/storageclasses/{name}")
    return {"ok": True, "classes": storage_class_inventory(),
            "message": f"Storage class {name} deleted; existing volumes are untouched"}


def vm_default_class(rows=None):
    """The class a new VM disk lands on: the cluster's default, else Longhorn's
    usual one, else none named - the API server then applies its own default."""
    rows = [row for row in (rows if rows is not None else storage_classes()) if class_selectable(row)]
    for row in rows:
        if row["default"]:
            return row["name"]
    names = [row["name"] for row in rows]
    return next((name for name in ("longhorn-r2", "harvester-longhorn", "longhorn") if name in names), "")


def vm_create_options():
    """What the New VM form can offer on this cluster."""
    platform = PLATFORM.detect()
    rows = storage_classes()
    return {"harvester": platform.get("harvester", False), "cdi": platform.get("cdi", False),
            "distribution": platform.get("distribution", ""),
            "storage_classes": selectable_storage_classes(rows),
            "storage_class_facts": storage_class_facts(rows),
            "default_class": vm_default_class(rows),
            "images": IMP.list_vm_images() if platform.get("harvester") else [],
            "networks": vm_networks(),
            "network_details": vm_network_details(),
            "vm_network_options": NETWORK.vm_network_options(node_temps()),
            "subnets": vm_subnets(),
            "nodes": sorted(n["metadata"]["name"] for n in kget("/api/v1/nodes").get("items", []))}


def vm_networks():
    """The pod network and every network attachment (Multus) a VM can join."""
    try:
        items = kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions").get("items", [])
    except Exception:
        items = []
    return ["pod"] + sorted(f"{i['metadata']['namespace']}/{i['metadata']['name']}" for i in items)


def vm_network_details():
    """Each VM network, and whether it puts a VM on the LAN with an address
    of its own - a bridge, as Harvester's VM networks are."""
    try:
        items = kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions").get("items", [])
    except Exception:
        items = []
    out = []
    for item in items:
        try:
            config = json.loads((item.get("spec") or {}).get("config") or "{}")
        except ValueError:
            config = {}
        labels = item["metadata"].get("labels") or {}
        out.append({"name": f"{item['metadata']['namespace']}/{item['metadata']['name']}",
                    "type": config.get("type", ""), "vlan": config.get("vlan"),
                    "bridge": config.get("bridge", "") or config.get("master", ""),
                    "kind": labels.get("network.harvesterhci.io/type", ""),
                    # On the LAN: a bridge carries VMs and containers; macvlan
                    # gives containers a MAC of their own but cannot carry a VM.
                    "lan": config.get("type") in ("bridge", "macvlan"),
                    "vms": config.get("type") == "bridge"})
    return sorted(out, key=lambda row: row["name"])


def vm_subnets():
    """The subnets IP addresses knows, with the free addresses outside DHCP."""
    try:
        view = IPAM.view()
    except Exception:
        return []
    return [{"cidr": s["cidr"], "name": s.get("name", ""), "gateway": s.get("gateway", ""),
             "dhcp_start": s.get("dhcp_start", ""), "dhcp_end": s.get("dhcp_end", ""),
             "free": s.get("free_list") or s.get("next_free") or []} for s in view.get("subnets") or []]


def vm_address_problem(ip):
    """Why a VM cannot have this address, or "": something already has it."""
    import ipaddress
    try:
        view = IPAM.view()
    except Exception:
        view = {"subnets": []}
    for subnet in view.get("subnets") or []:
        if ipaddress.ip_address(ip) not in ipaddress.ip_network(subnet["cidr"]):
            continue
        if subnet.get("gateway") == ip:
            return f"{ip} is the gateway of {subnet['cidr']}"
        row = next((r for r in subnet.get("rows") or [] if r["ip"] == ip), None)
        if row and (row.get("name") or row.get("kind") or row.get("cluster")):
            return f"{ip} is taken: {row.get('name') or row.get('cluster') or row.get('kind')} in IP addresses"
        if row and (row.get("scan") or {}).get("up"):
            return f"{ip} answered the last scan: something is already there"
        start, end = subnet.get("dhcp_start"), subnet.get("dhcp_end")
        if start and end and int(ipaddress.ip_address(start)) <= int(ipaddress.ip_address(ip)) <= int(ipaddress.ip_address(end)):
            return f"{ip} is inside the DHCP range {start}-{end}: the router may hand it to something else"
    try:
        network = cached("network", 5, NETWORK.inventory)
    except Exception:
        network = {}
    if ip in (network.get("node_ips") or []) or ip in (network.get("platform_addresses") or {}):
        return f"{ip} is one of the cluster's own addresses"
    if any(v.get("ip") == ip for v in network.get("vips") or []):
        return f"{ip} is a Service's address"
    # A few ports a machine usually has, quickly: a free address answers none.
    if IPAM._probe(ip, ports=(22, 80, 443, 3389, 6443), timeout=0.4).get("up"):
        return f"{ip} answers on the network: something is already there"
    return ""


def create_vm_with_address(cfg):
    """A VM, and - when it has an address of its own - that address checked
    as free first and recorded in IP addresses after, under the VM's name."""
    static = cfg.get("static_ip") or None
    if static:
        problem = vm_address_problem(str(static.get("address") or "").strip())
        if problem:
            raise ValueError(problem)
    result = IMP.create_vm(cfg, PLATFORM.detect(), vm_default_class())
    if result.get("address"):
        try:
            IPAM.save_record({"ip": result["address"], "name": cfg.get("name", ""), "kind": "static",
                              "category": "server", "mac": result.get("mac", ""), "owner": "homestead",
                              "note": f"VM {cfg.get('namespace') or 'lab'}/{cfg.get('name', '')}"
                                      + (f" · {cfg['ipam_note']}" if cfg.get("ipam_note") else "")})
        except Exception:
            pass
    return result


def selectable_storage_classes(rows=None):
    """Classes a person may pick for their own workloads - the default first."""
    rows = [row for row in (rows if rows is not None else storage_classes()) if class_selectable(row)]
    return [row["name"] for row in sorted(rows, key=lambda row: (row["name"] != STORAGE_CLASS, row["name"]))]


def storage_class_facts(rows=None):
    """The handful of class facts worth showing next to a class picker."""
    return {row["name"]: {"replicas": row["replicas"], "engine": row["engine"], "migratable": row["migratable"],
                          "encrypted": row["encrypted"], "expandable": row["expandable"],
                          "reclaim": row["reclaim"], "default": row["default"]}
            for row in (rows if rows is not None else storage_classes()) if class_selectable(row)}


def shared_storage_classes(rows=None):
    """Classes that can actually serve ReadWriteMany to a pod."""
    return [row["name"] for row in (rows if rows is not None else storage_classes())
            if class_selectable(row) and not row["migratable"]]


def create_pvc(ns, name, size_gb, sc=None, access_mode="ReadWriteOnce"):
    sc = sc or STORAGE_CLASS
    if access_mode == "ReadWriteMany":
        chosen = next((row for row in storage_classes() if row["name"] == sc), None)
        if chosen and chosen["migratable"]:
            usable = ", ".join(shared_storage_classes()) or "none in this cluster"
            raise ValueError(
                f"storage class {sc} creates live-migratable volumes for VM disks, and "
                "Longhorn cannot mount those into a pod. Shared (ReadWriteMany) storage "
                f"needs a class without migratable=true — available: {usable}")
    body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": ns, "labels": {NAMES.key("managed"): "true"}},
            "spec": {"accessModes": [access_mode],
                     "storageClassName": sc,
                     "resources": {"requests": {"storage": f"{size_gb}Gi"}}}}
    return ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", body)


def ensure_claim(ns, name, size_gb, sc=None, access_mode="ReadWriteOnce"):
    """A deploy's new volume - or the one of that name already there, when
    nothing uses it.

    A failed install leaves its volumes behind (deleting a container keeps
    its data), and installing again then stopped at "already exists". One
    that nothing refers to is taken as it is, data and all, and said so; one
    another container, VM or job uses is refused by name.
    """
    try:
        existing = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        existing = None
    if not existing:
        create_pvc(ns, name, size_gb, sc, access_mode)
        return ""
    try:
        users = _references_for(claim_references(), ns, name)
    except Exception:
        users = ["something"]
    if users:
        raise ValueError(f"a volume named {name} already exists in {ns} and {', '.join(users)} uses it; "
                         "give this app's volume another name, or choose the existing one to share it")
    return name


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
    """Grow a PVC and optionally change Longhorn replica count.

    Only what changed is sent. Longhorn's volumes live in its namespace; the
    replica count was written to a path without it, which Kubernetes answers
    "404 page not found" - after the size had already been changed, so every
    save of the form reported a failure that had half happened.
    """
    ns, name = cfg.get("namespace") or DEFAULT_NS, cfg["name"]
    pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    done = []
    if cfg.get("size_gb"):
        wanted = int(cfg["size_gb"])
        requested_mb = _quantity_mb(((pvc.get("spec") or {}).get("resources") or {})
                                    .get("requests", {}).get("storage", "0"))
        now_gb = -(-requested_mb // 1024) if requested_mb else 0
        if now_gb and wanted < now_gb:
            raise ValueError(f"{name} is {now_gb} GB; volumes can grow but not shrink")
        if wanted != now_gb:
            ksend("PATCH", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}",
                  {"spec": {"resources": {"requests": {"storage": f"{wanted}Gi"}}}},
                  ctype="application/merge-patch+json")
            done.append(f"growing to {wanted} GB")
    reps = cfg.get("replicas")
    vol_name = pvc.get("spec", {}).get("volumeName")
    if reps is not None and vol_name:
        reps = int(reps)
        if not 1 <= reps <= 5:
            raise ValueError("replica count must be between 1 and 5")
        path = f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/{vol_name}"
        try:
            current = int(((kget(path).get("spec") or {}).get("numberOfReplicas")) or 0)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            current = None          # not a Longhorn volume: nothing to set
        if current is not None and current != reps:
            ksend("PATCH", path, {"spec": {"numberOfReplicas": reps}}, ctype="application/merge-patch+json")
            done.append(f"{reps} cop{'y' if reps == 1 else 'ies'}")
    for k in list(_cache):
        if k.startswith(("vol", "stor", "flow")):
            _cache.pop(k, None)
    return {"ok": True, "name": name, "detail": (f"{name}: " + ", ".join(done)) if done else f"{name} is unchanged"}


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
CA_FEED = os.environ.get(
    "COMMUNITY_CATALOG_URL",
    "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json",
)


def category_values(value):
    """Flatten inconsistent feed category shapes into stable display strings."""
    found = []

    def visit(item):
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            preferred = [item.get(key) for key in ("name", "label", "category", "Category")]
            usable = [child for child in preferred if child not in (None, "")]
            for child in usable or item.values():
                visit(child)
        elif item is not None:
            for part in re.split(r"[\s,|]+", str(item).strip()):
                if part and part not in found:
                    found.append(part)

    visit(value)
    return found


# Markup in catalogue text: HTML tags, and forum codes such as [b], [/span] and
# [span style='color: red'] - a known tag name, so "[1]" or "[x86]" survive.
CATALOG_MARKUP = re.compile(
    r"<[^>]*>|\[/?(?:b|i|u|s|br|p|hr|img|url|span|color|size|font|center|left|right|quote|code|list|li|\*|h[1-6])"
    r"(?:[= ][^\]]*)?\]", re.I)


def catalog_text(value, limit=None):
    """Turn catalogue HTML fragments and entities into compact readable text."""
    text = str(value or "")
    # Some catalogue fields contain encoded markup (and occasionally encode it
    # twice), so decode before removing tags.
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"(?i)<\s*br\s*/?\s*>|\[\s*br\s*/?\s*\]", "\n", text)
    text = re.sub(r"(?i)</\s*(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    # HTML, and the forum's [b]/[span style=...] codes that templates use too.
    text = re.sub(CATALOG_MARKUP, "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n+\s*", " · ", text)
    text = re.sub(r"(?:\s*·\s*)+", " · ", text).strip(" ·")
    return text[:limit] if limit is not None else text


def catalog_paragraphs(value, limit=8000):
    """Catalogue text with its paragraphs kept, for reading in full.

    Overviews mix HTML, entities and the forum's [b]/[br] codes; each becomes
    plain text with line breaks where the author put them."""
    text = str(value or "")
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"(?i)\[\s*br\s*/?\s*\]|<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(CATALOG_MARKUP, "", text)
    text = html.unescape(text).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def catalog_reason(value):
    """A spotlight's reason, which the feed gives as a dict - sometimes as its text."""
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            import ast
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            pass
    if isinstance(value, dict):
        value = value.get("en_US") or next(iter(value.values()), "")
    return catalog_text(value)


def catalog_links(value):
    if isinstance(value, str):
        value = [value]
    return [str(x) for x in (value or []) if isinstance(x, str) and x.startswith(("http://", "https://"))][:6]


def appstore_key(app):
    return f"{app.get('name')}|{app.get('repo')}"


# What a list of apps needs; the full record comes one app at a time.
APPSTORE_HEAVY = ("overview", "config", "screenshots", "readme", "comment", "requires")


def appstore_summary(app):
    return {k: v for k, v in app.items() if k not in APPSTORE_HEAVY}


def search_appstore(apps, term):
    """Return catalogue matches with name relevance ahead of description hits."""
    needle = str(term or "").strip().lower()
    if not needle:
        return list(apps)

    def relevance(app):
        name = str(app.get("name") or "").lower()
        description = str(app.get("desc") or "").lower()
        if name == needle:
            return 0
        if name.startswith(needle):
            return 1
        if re.search(rf"(?:^|[^a-z0-9]){re.escape(needle)}", name):
            return 2
        if needle in name:
            return 3
        if needle in description:
            return 4
        return None

    ranked = []
    for position, app in enumerate(apps):
        score = relevance(app)
        if score is not None:
            ranked.append((score, position, app))
    return [app for _, _, app in sorted(ranked, key=lambda row: (row[0], row[1]))]


def appstore_number(value):
    """Coerce optional feed statistics without letting malformed rows break browsing."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def unique_appstore_apps(apps):
    """Hide duplicate templates that point at the same named container image."""
    seen, out = set(), []
    for app in apps:
        key = (str(app.get("name") or "").strip().lower(),
               str(app.get("repo") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(app)
    return out


def rank_appstore(apps, mode="popular"):
    """Rank cached catalogue rows using only statistics supplied by the feed."""
    rows = unique_appstore_apps(apps)
    if mode == "spotlight":
        return sorted((app for app in rows if app.get("spotlight")),
                      key=lambda app: -app["spotlight"]["date"])
    if mode == "recent":
        # Templates added in one feed update share a timestamp; the feed's own
        # order among them is the one Community Applications shows.
        key = lambda app: -appstore_number(app.get("first_seen"))
    elif mode == "trending":
        key = lambda app: (-appstore_number(app.get("top_trending")),
                           -appstore_number(app.get("trending")),
                           -appstore_number(app.get("top_performing")),
                           -appstore_number(app.get("downloads")),
                           str(app.get("name") or "").lower())
    else:
        key = lambda app: (-appstore_number(app.get("top_performing")),
                           -appstore_number(app.get("trending")),
                           -appstore_number(app.get("top_trending")),
                           -appstore_number(app.get("downloads")),
                           str(app.get("name") or "").lower())
    return sorted(rows, key=key)


def appstore_spotlight(apps):
    """Choose the strongest feed-ranked app for the catalogue spotlight."""
    ranked = rank_appstore(apps, "popular")
    return next((app for app in ranked if appstore_number(app.get("top_performing")) > 0),
                next((app for app in ranked if appstore_number(app.get("trending")) > 0),
                     ranked[0] if ranked else None))


def catalog_source():
    """The catalogue feed in use: the one set in Settings, else the default."""
    try:
        custom = cached("settings", 15, get_app_settings).get("catalog_url") or ""
    except Exception:
        custom = ""
    return custom or CA_FEED


def fetch_appstore():
    source = catalog_source()

    def go():
        req = urllib.request.Request(source, headers={
            "User-Agent": f"Homestead/{HOMESTEAD_VERSION} (+https://github.com/wjcloudy/homestead)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        apps = data.get("applist", data if isinstance(data, list) else [])
        out = []
        for a in apps:
            repo = a.get("Repository") or ""
            if not repo or not a.get("Name"):
                continue
            # Containers only, as Community Applications shows them: a plugin
            # or language pack is Unraid's own software, and blacklisted,
            # deprecated or hidden templates are not offered there either.
            if (a.get("Plugin") or a.get("PluginURL") or a.get("Language") or a.get("LanguagePack")
                    or a.get("Blacklist") or a.get("Deprecated") or a.get("hideFromCA")
                    or str(repo).lower().endswith(".plg")):
                continue
            spotlight = None
            if appstore_number(a.get("RecommendedDate")):
                stamp = int(appstore_number(a.get("RecommendedDate")))
                spotlight = {"date": stamp, "month": time.strftime("%b %Y", time.gmtime(stamp)),
                             "reason": catalog_reason(a.get("RecommendedReason")),
                             "who": catalog_text(a.get("RecommendedWho") or "")}
            maintainer = a.get("Maintainer") or a.get("Author") or ""
            if isinstance(maintainer, dict):
                maintainer = maintainer.get("Name") or next((v for v in maintainer.values() if isinstance(v, str)), "")
            maintainer = str(maintainer or re.sub(r"'s Repository$", "", str(a.get("Repo") or ""))).strip()
            categories = category_values(a.get("CategoryList") or a.get("Category") or "")
            item = {
                "name": a.get("Name"),
                "repo": repo,
                "icon": a.get("Icon") or "",
                "desc": catalog_text(a.get("Overview") or a.get("Description") or "", 300),
                "cat": categories[0] if categories else "",
                "categories": categories,
                "web": a.get("Project") or a.get("Support") or "",
                "network": a.get("Network") or "bridge",
                "webui": a.get("WebUI") or "",
                "config": a.get("Config") or [],
                "downloads": int(appstore_number(a.get("downloads"))),
                "stars": int(appstore_number(a.get("stars"))),
                "trending": appstore_number(a.get("trending")),
                "top_trending": appstore_number(a.get("topTrending")),
                "top_performing": appstore_number(a.get("topPerforming")),
                "first_seen": int(appstore_number(a.get("FirstSeen"))),
                "last_update": int(appstore_number(a.get("LastUpdate"))),
                "spotlight": spotlight,
                "maintainer": catalog_text(maintainer, 80),
                "official": bool(a.get("Official") or a.get("LTOfficial")),
                "beta": str(a.get("Beta") or "").lower() in ("true", "1", "yes"),
                "privileged": str(a.get("Privileged") or "").lower() == "true",
                "extra_params": catalog_text(a.get("ExtraParams") or "", 600),
                "links": {k: v for k, v in {
                    "project": a.get("Project"), "support": a.get("Support"), "registry": a.get("Registry"),
                    "readme": a.get("ReadMe") or a.get("Readme"), "video": a.get("Video"),
                    "discord": a.get("Discord"), "github": a.get("GitHub"), "web": a.get("WebPage"),
                }.items() if isinstance(v, str) and v.startswith(("http://", "https://"))},
                "overview": catalog_paragraphs(a.get("Overview") or a.get("Description") or ""),
                "screenshots": catalog_links(a.get("Screenshot")),
                "requires": catalog_text(a.get("Requires") or "", 600),
                "comment": catalog_text(a.get("CAComment") or a.get("ModeratorComment") or "", 600),
                "license": catalog_text(a.get("License") or a.get("Licence") or "", 80),
            }
            item["deploy"] = template_to_cfg(item)
            out.append(item)
        return out
    return cached(f"appstore:{source}", 21600, go)


def appdata_folders(vols):
    """Gives each path an app keeps in its appdata volume a folder of its own.

    An Unraid template maps each path to its own host folder; here they share
    one volume, as an import lays them out, each mounted from its own folder
    with subPath. A single path takes the whole volume. The volume is sized
    for everything in it."""
    shared = [v for v in vols if v.get("type") == "pvc" and v.get("create")]
    if len(shared) < 2:
        return vols
    taken, total = set(), 0
    access = "ReadWriteMany" if any(v.get("access_mode") == "ReadWriteMany" for v in shared) else "ReadWriteOnce"
    for v in shared:
        base = re.sub(r"[^a-z0-9._-]+", "-", str(v["path"]).rstrip("/").rsplit("/", 1)[-1].lower()).strip("-.") or "data"
        folder, n = base, 2
        while folder in taken:
            folder, n = f"{base}-{n}", n + 1
        taken.add(folder)
        v["sub_path"] = folder
        total += int(v.get("size_gb") or 5)
        v["access_mode"] = access
    for v in shared:
        v["size_gb"] = total
    return vols


def template_to_cfg(app):
    """Turn an Unraid CA template entry into our deploy config."""
    ports, envs, env_meta, vols, devices = [], {}, [], [], []
    cfgs = app.get("config") or []
    if isinstance(cfgs, dict):
        cfgs = [cfgs]
    if not isinstance(cfgs, list):
        cfgs = []
    name = re.sub(r"[^a-z0-9-]", "-", app["name"].lower()).strip("-")[:40]
    for c in cfgs:
        if not isinstance(c, dict):
            continue
        attrs = c.get("@attributes", {}) or {}
        typ = str(attrs.get("Type") or c.get("Type") or "").strip().lower()
        tgt = str(attrs.get("Target") or c.get("Target") or "").strip()
        val = c.get("value")
        if val is None or val == "":
            val = attrs.get("Default") or c.get("Default") or ""
        val = str(val)
        label = catalog_text(attrs.get("Name") or c.get("Name") or tgt)
        description = catalog_text(attrs.get("Description") or c.get("Description") or "")
        required = str(attrs.get("Required") or c.get("Required") or "false").lower() == "true"
        mode = str(attrs.get("Mode") or c.get("Mode") or "")
        read_only = mode.lower() == "ro"
        if typ == "port" and tgt:
            try:
                protocol = mode.upper() if mode.lower() in ("tcp", "udp") else "TCP"
                ports.append({"container": int(tgt), "host": int(val or tgt), "expose": True,
                              "name": f"p{tgt}-{protocol.lower()}", "protocol": protocol,
                              "label": label, "description": description, "required": required})
            except (TypeError, ValueError):
                pass
        elif typ == "variable" and tgt:
            options = [option.strip() for option in val.split("|") if option.strip()] if "|" in val else []
            if options:
                val = options[0]
            masked = str(attrs.get("Mask", "false")).lower() == "true"
            secret_value = masked or bool(re.search(r"(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|APIKEY)$", tgt, re.I))
            generate = secret_value and (bool(val) or required or (masked and "password" in tgt.lower()))
            if generate:
                # Public catalogue defaults must never become deployed credentials.
                val = ""
            envs[tgt] = val
            env_meta.append({"key": tgt, "label": label, "description": description,
                             "required": required, "masked": secret_value,
                             "generate": generate, "options": options})
        elif typ == "path" and tgt:
            system_bind = tgt in ("/etc/localtime", "/var/run/docker.sock") and val == tgt
            clean_path = tgt.rstrip("/").lower() or "/"
            context = " ".join((clean_path, label, description, val)).lower()
            tokens = set(re.split(r"[^a-z0-9]+", context))
            cache_tokens = {"cache", "caches", "transcode", "transcoding", "temp", "temporary", "tmp"}
            media_tokens = {"media", "movie", "movies", "tv", "music", "photo", "photos",
                            "video", "videos", "recording", "recordings", "download", "downloads"}
            config_path = (clean_path == "/config" or clean_path.endswith("/config") or
                           "appdata" in str(val).lower())
            role = ("system" if system_bind else "cache" if tokens & cache_tokens else
                    "config" if config_path else "media" if tokens & media_tokens else
                    "data" if clean_path == "/data" or clean_path.endswith("/data") else "config")
            volume_type = "host" if system_bind else "emptyDir" if role == "cache" else "pvc"
            create = not system_bind and role not in ("cache", "media")
            size = 50 if role == "data" else 5
            access_mode = ("ReadWriteMany" if {"rwx", "shared", "multinode", "multi-node"} & tokens
                           else "ReadWriteOnce")
            source = (val if system_bind else "" if role in ("cache", "media") else f"{name}-appdata")
            vols.append({"path": tgt, "source": source,
                         "type": volume_type, "create": create, "role": role,
                         "access_mode": access_mode, "size_gb": size,
                         "read_only": read_only, "label": label, "description": description,
                         "required": required, "template_source": val})
        elif typ == "device":
            devices.append({"host_path": val or tgt, "container_path": tgt or val,
                            "label": label, "description": description, "required": required})
    appdata_folders(vols)
    network = str(app.get("network") or "bridge").strip().lower()
    network_mode = "host" if network == "host" else "loadbalancer"
    vip_mode = "auto" if network not in ("bridge", "host", "default", "") else "shared"

    # Host-mode templates frequently omit port rows. Preserve the useful WebUI
    # listener so users can switch to a Service without re-reading the template.
    if not ports:
        match = re.search(r"\[PORT:(\d+)\]", str(app.get("webui") or ""), re.I)
        if match:
            port = int(match.group(1))
            ports.append({"container": port, "host": port, "expose": network != "host",
                          "name": f"p{port}-tcp", "protocol": "TCP",
                          "label": "Web interface", "description": "Inferred from the template WebUI URL",
                          "required": True})
    # What Unraid lets it do to its host: Privileged, and the capabilities and
    # tunnel device its extra parameters ask for (VPN containers need them).
    privileges = PRIV.from_docker(app.get("extra_params", ""), app.get("privileged"),
                                  devices=[d["host_path"] for d in devices])
    devices = [d for d in devices if PRIV.TUN not in (d["host_path"] + d["container_path"])]
    cfg = {"name": name,
            "image": app["repo"], "icon": app.get("icon") or "",
            "ports": ports, "env": envs, "env_meta": env_meta, "volumes": vols,
            **privileges,
            "template_devices": devices, "template_network": network,
            "network_mode": network_mode, "vip_mode": vip_mode,
            "env_bindings": {}}
    return analyze_deploy_intent(cfg)


def analyze_deploy_intent(cfg):
    """Derive portable Kubernetes guidance from template semantics, never app names."""
    updated = dict(cfg)
    updated["ports"] = [dict(item) for item in (cfg.get("ports") or [])]
    updated["env_bindings"] = dict(cfg.get("env_bindings") or {})
    notes, dependencies, intents = [], [], []
    exposed = [item for item in updated["ports"] if item.get("expose", True)]
    dns = any(int(item.get("container") or 0) == 53 for item in exposed)
    if dns and updated.get("network_mode") != "host":
        updated["network_mode"], updated["vip_mode"] = "loadbalancer", "auto"
        intents.append("network")
        notes.append("A dedicated automatic VIP is selected because this template exposes DNS port 53.")
        bind_names = {"SERVERIP", "SERVER_IP", "LOCAL_IPV4", "FTLCONF_LOCAL_IPV4"}
        for key in (updated.get("env") or {}):
            if re.sub(r"[^A-Z0-9_]", "", key.upper()) in bind_names:
                updated["env_bindings"][key] = "vip"
        if updated["env_bindings"]:
            notes.append("Address variables are filled from the allocated VIP at deploy time.")
    for port in updated["ports"]:
        if int(port.get("container") or 0) == 67 and str(port.get("protocol") or "TCP").upper() == "UDP":
            port["expose"], port["required"] = False, False
            port["description"] = "Optional: expose only when this workload provides DHCP"
            if "network" not in intents:
                intents.append("network")
            notes.append("DHCP port 67 stays disabled unless you explicitly expose it.")

    volumes = updated.get("volumes") or []

    def inferred_role(item):
        if item.get("role"):
            return item["role"]
        path = str(item.get("path") or "").rstrip("/").lower() or "/"
        context = " ".join(str(item.get(key) or "") for key in
                           ("path", "source", "template_source", "label", "description")).lower()
        tokens = set(re.split(r"[^a-z0-9]+", context))
        if path in ("/etc/localtime", "/var/run/docker.sock", "/run/containerd/containerd.sock"):
            return "system"
        if tokens & {"cache", "caches", "transcode", "transcoding", "temp", "temporary", "tmp"}:
            return "cache"
        if path == "/config" or path.endswith("/config") or "appdata" in context:
            return "config"
        if tokens & {"media", "movie", "movies", "tv", "music", "photo", "photos", "video",
                     "videos", "recording", "recordings", "download", "downloads"}:
            return "media"
        if path == "/data" or path.endswith("/data"):
            return "data"
        return "config"

    media = [item.get("path") for item in volumes if inferred_role(item) == "media"]
    cache = [item.get("path") for item in volumes if inferred_role(item) == "cache"]
    data_paths = [item.get("path") for item in volumes if inferred_role(item) == "data"]
    if media:
        intents.append("storage")
        notes.append("Choose existing/shared media storage for: " + ", ".join(filter(None, media)) + ".")
    if cache:
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Temporary pod storage is selected for cache/transcode paths: " + ", ".join(filter(None, cache)) + ".")
    if data_paths:
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Persistent data paths start as editable Longhorn claims; choose RWX when multiple replicas or workloads must attach: " + ", ".join(filter(None, data_paths)) + ".")
    if any(item.get("access_mode") == "ReadWriteMany" for item in volumes):
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Template wording indicates shared storage; review the proposed RWX claim and size.")

    generated = [item.get("key") for item in (updated.get("env_meta") or []) if item.get("generate")]
    if generated:
        intents.append("security")
        notes.append("Public defaults for secret fields are discarded and generated locally.")
    option_fields = [item.get("key") for item in (updated.get("env_meta") or []) if item.get("options")]
    if option_fields:
        notes.append("Enumerated template values are presented as selectors instead of literal option strings.")

    env_keys = {re.sub(r"[^A-Z0-9_]", "", key.upper()) for key in (updated.get("env") or {})}
    if any(key.endswith(("DB_HOST", "DATABASE_HOST", "MYSQL_HOST", "POSTGRES_HOST")) for key in env_keys):
        dependencies.append({"kind": "database", "name": "External database endpoint",
                             "required": True, "managed": False})
    if any(key.endswith(("REDIS_HOST", "CACHE_HOST")) for key in env_keys):
        dependencies.append({"kind": "cache", "name": "External cache endpoint",
                             "required": True, "managed": False})

    runtime_socket = next((item for item in volumes
                           if item.get("path") in ("/var/run/docker.sock", "/run/containerd/containerd.sock")), None)
    blocked = bool(runtime_socket)
    if blocked:
        intents.append("safety")
        dependencies.append({"kind": "runtime", "name": "Host container-runtime control",
                             "required": True, "managed": False})
        notes.append("This template requests a host container-runtime socket and can create or control other containers; it needs a Kubernetes-specific deployment design.")
    if dependencies:
        intents.append("dependency")
        notes.append("Review the external services listed below before deployment.")
    if updated.get("template_devices"):
        intents.append("hardware")
        notes.append("Imported device paths are matched to reusable hardware features; verify eligible hosts before deploying.")
    network = str(updated.get("template_network") or "bridge").lower()
    if network == "host":
        intents.append("network")
        notes.append("The source requests host networking; review node port collisions and failover, or switch to a Service VIP.")
    elif network not in ("bridge", "default", "") and not dns:
        intents.append("network")
        notes.append("The source custom network is represented by a dedicated Kubernetes VIP.")
    if not notes:
        notes.append("Review the imported ports, variables, and storage choices before deploying.")

    intents = list(dict.fromkeys(intents)) or ["template"]
    level = "dependency" if blocked or dependencies else "guided" if dns else "review"
    label = ("Needs Kubernetes design" if blocked else "Dependency review" if dependencies else
             "Dedicated VIP" if dns else "Storage review" if "storage" in intents else "Template review")
    updated["app_profile"] = {"intent": intents[0], "intents": intents, "level": level,
                              "label": label, "notes": notes, "dependencies": dependencies,
                              "blocked": blocked}
    return updated


def apply_deploy_bindings(cfg):
    """Resolve values that depend on the reviewed cluster-side network plan."""
    bindings = cfg.get("env_bindings") or {}
    if not bindings:
        return cfg
    updated = dict(cfg)
    updated["env"] = dict(cfg.get("env") or {})
    for key, binding in bindings.items():
        if binding == "vip":
            vip = cfg.get("lb_ip") or ""
            if not vip:
                raise ValueError(f"{key} requires a dedicated Service VIP")
            updated["env"][key] = vip
    return updated


def apply_generated_secrets(cfg):
    """Fill catalogue password defaults without trusting a public feed value."""
    generate = {item.get("key") for item in (cfg.get("env_meta") or [])
                if item.get("generate") and item.get("key")}
    if not generate:
        return cfg
    updated = dict(cfg)
    updated["env"] = dict(cfg.get("env") or {})
    for key in generate:
        if not updated["env"].get(key):
            updated["env"][key] = secrets.token_urlsafe(18)
    return updated


def ensure_profile_compatible(cfg):
    profile = cfg.get("app_profile") or {}
    if profile.get("blocked"):
        raise ValueError(profile.get("label") or "this App Store template is not directly compatible")
    return cfg


def redact_deployment_preview(deployment, cfg=None):
    """Return a manifest safe to display, including for existing shared pods."""
    if not deployment:
        return deployment
    sensitive = {item.get("key") for item in ((cfg or {}).get("env_meta") or [])
                 if item.get("masked") and item.get("key")}
    safe = copy.deepcopy(deployment)
    containers = safe.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        for item in container.get("env") or []:
            key = str(item.get("name") or "")
            if key in sensitive or re.search(r"(?:PASSWORD|PASS|TOKEN|SECRET|API_?KEY|PRIVATE_?KEY)$", key, re.I):
                if "value" in item:
                    item["value"] = "••••••"
    return safe


def raw_get(path, timeout=20):
    """Plain-text GET against the API (pod logs and similar)."""
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# expose a tiny shim module so helper modules can reach raw_get without a cycle
import types as _types
_shim = _types.ModuleType("homestead_shim")
_shim.raw_get = raw_get
sys.modules["homestead_shim"] = _shim

import homestead_lifecycle as LC
import homestead_imports as IMP
import homestead_auth as AUTH
import homestead_longhorn as LH
import homestead_place as PLACE
import homestead_hardware as HW
import homestead_updates as UPDATES
import homestead_operations as OPS
import homestead_vmusage as VMUSAGE
import homestead_cancel as CANCEL
import homestead_joblogs as JOBLOGS
import homestead_console as CONSOLE
import homestead_files as FILES
import homestead_icons as ICONS
import homestead_volumes as VOLUMES
import homestead_smart as SMART
import homestead_shares as SHARES
import homestead_nfs as NFS
import homestead_networking as NETWORK
import homestead_cluster as CLUSTER
import homestead_probe as PROBE
import homestead_objectstore as OBJECTS
import homestead_move as MOVE
import homestead_move_source as MOVE_SOURCE
import homestead_move_engine as MOVE_ENGINE
import homestead_compose as COMPOSE
import homestead_onboard as ONBOARD
import homestead_cfaccess as CFACCESS
import homestead_push as PUSH
import homestead_alerts as ALERTS
import homestead_self as SELF
import homestead_namespaces as NSMOD
import homestead_restructure as RESTRUCTURE
import homestead_volowner as VOLOWNER
import homestead_affinity as AFFINITY
import homestead_failover as FAILOVER
import homestead_k3scluster as K3SC
import homestead_lan as LAN
import homestead_pullwatch as PULLWATCH
import homestead_portal as PORTAL
import homestead_upgrades as UPGRADES
import homestead_vmconsole as VMCONSOLE
import homestead_shared as SHARED
import homestead_leader as LEADER
import homestead_ipam as IPAM
import homestead_helm as HELM
import homestead_mqtt as MQTT
import homestead_history as HISTORY
import homestead_platform as PLATFORM
import homestead_addons as ADDONS
import homestead_components as COMPONENTS
import homestead_resources as RESOURCES
import homestead_vms as VMS
import homestead_lhcapacity as LHCAP
import homestead_disks as DISKS
import homestead_power as POWER
import homestead_privileges as PRIV
NAMES.bind(kget)
PROBE.bind(kget, ksend, DEFAULT_NS)
OBJECTS.bind(kget, ksend, create_pvc, DEFAULT_NS)
MOVE.bind(kget, ksend, DEFAULT_NS, HOMESTEAD_VERSION)
HW.bind(kget, ksend, DEFAULT_NS, _cache)
def _resolve_storage_class():
    """An empty STORAGE_CLASS means the cluster's default. It cannot stay
    empty: a claim asking for class "" asks for no class at all, and never
    binds on a cluster whose volumes all come from a provisioner."""
    if STORAGE_CLASS:
        return STORAGE_CLASS
    try:
        return vm_default_class() or "longhorn-r2"
    except Exception:
        return "longhorn-r2"


STORAGE_CLASS = _resolve_storage_class()
LC.bind(kget, ksend, SYS_NS, _cache, HW.features, create_pvc, STORAGE_CLASS)
IMP.bind(kget, ksend, create_pvc, build_deployment, DEFAULT_NS, _cache, HW.features)
IMP.SCAN_DIR = DATA_DIR       # each node's full image list, kept for every replica
AUTH.bind(kget, ksend, DEFAULT_NS)
CAPACITY_REVIEW.bind(AUTH.review_signing_key)
LH.bind(kget, ksend, _cache, STORAGE_CLASS)
PLACE.bind(kget, ksend, lambda: cached("nodes", 5, get_nodes), _cache, HW.features)
POWER.bind(kget, PLACE.impact, LC.quorum_report, lambda: LC.NODE_POWER_ENABLED)
UPDATES.bind(kget, ksend, DEFAULT_NS, DATA_DIR, SYS_NS, SMB_NAMESPACE)
SMART.bind(kget, DEFAULT_NS, AUTH.internal_signing_key)
OPS.bind(kget, DATA_DIR, UPDATES.progress, SMART.progress)
RESTRUCTURE.bind(kget, ksend, raw_get)
AFFINITY.bind(kget)
FAILOVER.bind(kget, ksend)
LAN.bind(kget, ksend)
PULLWATCH.bind(kget, ksend, raw_get, lambda image, arch: UPDATES.image_layers(image, arch), DEFAULT_NS)


def _pull_progress(node, image):
    PULLWATCH.sweep()
    return PULLWATCH.progress(node, image)


UPDATES.pull_progress = _pull_progress
def _remove_cluster_vm(ns, node):
    """One VM of a k3s cluster whose build stopped part-way: gone with its
    disks, and its address free again."""
    VMS.delete(ns, node["name"], with_disks=True)
    CANCEL.forget_addresses({node["address"]: node["name"]})


K3SC.bind(kget, lambda cfg: create_vm_with_address(cfg), lambda ip: vm_address_problem(ip), _remove_cluster_vm)
OPS.RESOLVERS["k3s-cluster"] = K3SC.status
OPS.RESOLVERS["node-power"] = POWER.status
OPS.CANCELLERS["node-power"] = (lambda item: {"can": False, "why_not":
    "A host power command cannot be cancelled after it has been sent"}, lambda item, options: "")
MOVE_ENGINE.after_finish = cleanup_restore_classes


def _restore_then_tidy(item, _resolve=OPS.RESOLVERS["volume-restore"]):
    """A restore, and - once it has finished - the class it read the backup
    through removed, as nothing needs it after the claim is bound."""
    result = _resolve(item)
    if result and result[0] in ("succeeded", "failed"):
        cleanup_restore_classes()
    return result


OPS.RESOLVERS["volume-restore"] = _restore_then_tidy
UPGRADES.bind(kget)
PORTAL.bind(kget, ksend, DEFAULT_NS, lambda: cached("wl", 5, get_workloads),
            lambda source: ICONS.persist(source, DATA_DIR), lambda reference: ICONS.data_url(reference, DATA_DIR))
OPS.RESOLVERS["restructure"] = RESTRUCTURE.resolve
import homestead_reclass as RECLASS
import homestead_revert as REVERT
OPS.RESOLVERS["reclass"] = RECLASS.resolve
OPS.RESUMABLE["reclass"] = RECLASS.resumable
OPS.RESOLVERS["protect-run"] = LH.run_status
MOVE_SOURCE.bind(kget, ksend, LH, DEFAULT_NS)
MOVE_ENGINE.bind(kget, ksend, LH, MOVE, NETWORK, OPS, DATA_DIR, DEFAULT_NS)
# A move reads in the Activity tray like every other long job.
OPS.RESOLVERS["move"] = MOVE_ENGINE.op_state
ONBOARD.bind(kget, ksend, DEFAULT_NS, OPS)
# Published through a Cloudflare Tunnel behind Access: name the Access team and
# application, and a request that came through Cloudflare without Access's
# signature is refused, whatever the Access policy says.
CFACCESS.configure(os.environ.get("CF_ACCESS_TEAM_DOMAIN", ""), os.environ.get("CF_ACCESS_AUD", ""))
PUSH.bind(DATA_DIR, os.environ.get("PUSH_CONTACT", ""))


def _own_namespace():
    try:
        with open(f"{SA}/namespace", encoding="utf-8") as handle:
            return handle.read().strip() or DEFAULT_NS
    except OSError:
        return DEFAULT_NS


SELF.bind(kget, ksend, _own_namespace(), HOMESTEAD_VERSION, DATA_DIR)
SHARED.bind(DATA_DIR)
LEADER.bind(kget, ksend, _own_namespace())
IPAM.bind(kget, ksend, DEFAULT_NS, lambda: cached("network", 5, NETWORK.inventory))
HELM.bind(kget, ksend)
MQTT.bind(kget, ksend, DEFAULT_NS, lambda: mqtt_snapshot(), LEADER.is_leader)
HISTORY.bind(DATA_DIR)
PLATFORM.bind(kget)
ADDONS.bind(kget, ksend, PLATFORM.detect, node_temps)
COMPONENTS.bind(kget, ksend, PLATFORM.detect, lambda cfg: HELM.upgrade(cfg), ADDONS)
OPS.RESOLVERS["platform-upgrade"] = COMPONENTS.status
OPS.CANCELLERS["platform-upgrade"] = (COMPONENTS.cancel_plan, COMPONENTS.cancel_run)


def _harvester_upgrade_status(item):
    """A Harvester upgrade Homestead started, followed as the Cluster page
    follows any: Harvester's own steps, then each node."""
    name = item["ref"].get("upgrade", "")
    row = next((row for row in UPGRADES.upgrades() if row["name"] == name), None)
    if not row:
        return "running", 1, "Waiting for Harvester to take the upgrade"
    step = next((s["label"] for s in row["steps"] if s["state"] in ("running", "failed")), "")
    message = row["message"] or (f"{step}" if step else f"Harvester {row['version']}")
    return row["state"], row["progress"], message


OPS.RESOLVERS["harvester-upgrade"] = _harvester_upgrade_status


def ktable(path, timeout=20):
    """A list as the API server prints it: the columns kubectl get shows."""
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json;as=Table;v=v1;g=meta.k8s.io,application/json"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        return json.loads(r.read().decode())


RESOURCES.bind(kget, ksend, ktable)
VMS.bind(kget, ksend, RESOURCES.events_for)
VMUSAGE.bind(kget)
VMS.platform, VMS.images = PLATFORM.detect, IMP.list_vm_images
LHCAP.bind(kget, ksend, v2_engine_status)
RECLASS.bind(kget, ksend, raw_get, storage_classes, LHCAP.status, _own_namespace())
REVERT.bind(kget, ksend, RECLASS, is_self)
OPS.RESOLVERS["snapshot-revert"] = REVERT.resolve
OPS.CANCELLERS["snapshot-revert"] = (REVERT.cancel_plan, REVERT.cancel_run)
DISKS.bind(kget, ksend, node_temps)
OPS.RESOLVERS["disk-retire"] = DISKS.retire_step
OPS.RESUMABLE["disk-retire"] = DISKS.retire_resumable
OPS.RESOLVERS["helm"] = HELM.job_status


def delete_workload(ns, name):
    """A workload deleted, with every Service that points at it and its own
    LAN network; the Services removed are returned."""
    guard_self(ns, name, deleting=True)
    guard_managed_smb(ns, name)
    try:
        LAN.remove_nad(ns, name)       # its own LAN network, if it had one
    except Exception:
        pass
    # Every Service selecting these pods, not just the one sharing the
    # workload's name: a sidecar or a hand-made listener is named
    # differently and would otherwise keep its VIP port forever.
    services = set(NETWORK.workload_service_names(ns, name)) | {name}
    ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    removed = []
    for service in sorted(services):
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/services/{service}")
            removed.append(service)
        except urllib.error.HTTPError:
            pass
    _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("network", None)
    return removed


# Every job can be cancelled; what that does for each kind is said there.
CANCEL.bind(kget, ksend, delete_workload, lambda: cleanup_restore_classes())
CANCEL.register(OPS)
# What each job has to show for itself: a pod's log, a VM's console.
JOBLOGS.bind(kget, raw_get)
JOBLOGS.register(OPS)
NSMOD.bind(kget, ksend, DEFAULT_NS, _own_namespace())
ALERTS.bind(DATA_DIR)


# ------------------------------------------------------------- alerts
UPDATE_SCAN_EVERY = UPDATES.FRESH_FOR
_last_update_scan = [0.0]


def _alert_sources():
    """What each source sees now; None where it could not look."""
    results = {}

    def take(name, fn):
        try:
            results[name] = fn()
        except Exception:
            results[name] = None

    take("health", lambda: ALERTS.health_facts(cached("ov", 10, get_overview)))
    take("jobs", lambda: ALERTS.job_facts(OPS.list_operations()))
    take("joins", lambda: ALERTS.join_facts(kget("/api/v1/nodes").get("items", [])))
    take("capacity", lambda: LHCAP.alert_facts(cached("lhcap", 15, LHCAP.status)))
    take("disks", lambda: DISKS.alert_facts(cached("disks", 15, DISKS.inventory)))
    take("platform", lambda: ALERTS.upgrade_facts(UPGRADES.report(
        ((cached("cluster", 15, CLUSTER.inventory) or {}).get("versions") or {}).get("harvester", ""))))
    # Twice a day whether or not anyone is looking - the Containers header and
    # the update notifications both read the result.
    if time.time() - _last_update_scan[0] > UPDATE_SCAN_EVERY:
        _last_update_scan[0] = time.time()
        try:
            UPDATES.report()
        except Exception:
            pass
    latest = UPDATES._LATEST.get("report")
    results["updates"] = ALERTS.update_facts(latest) if latest else None
    return results


def push_alerts(fresh):
    """Wakes every device that wants one of these alerts, and belongs to a user still here."""
    if not fresh:
        return None
    kinds = {entry["category"] for entry in fresh}
    users = {u["name"] for u in AUTH.list_users()}
    urgent = any(e["severity"] == "critical" and e["phase"] == "raised" for e in fresh)
    return PUSH.send(lambda row: row["user"] in users and kinds & set(row["categories"]),
                     urgency="high" if urgent else "normal")


def alerts_pending(user, endpoint):
    """What a device has not been shown yet, for its service worker after a push."""
    row = PUSH.mine(user, endpoint) if endpoint else None
    wanted = set(row["categories"]) if row else set()
    active = len([a for a in ALERTS.active(wanted) if a.get("announced", 0) > 0])
    if not row:
        return {"alerts": [], "active": active, "known": False}
    got = ALERTS.log(after=row.get("cursor", 0), categories=wanted | {"test"}, limit=12)
    mine = PUSH.tag(endpoint)
    alerts = [a for a in got["alerts"] if a["category"] != "test" or a.get("to") == mine]
    PUSH.advance(user, endpoint, got["latest"])
    return {"alerts": alerts, "active": active, "known": True}


def _alerts_loop():
    while True:
        # One replica raises alerts, or every notification arrives twice.
        if LEADER.is_leader():
            try:
                push_alerts(ALERTS.observe(_alert_sources()))
                beat("alerts", 20, leader_only=True)
            except Exception as error:
                beat("alerts", 20, error, leader_only=True)
                print(f"alerts: {str(error)[:160]}", flush=True)
        time.sleep(20)


def _history_loop():
    """Long-term stats, on the leader, every five minutes, browser or not."""
    last = 0
    while True:
        # Checked often, recorded every STEP: a new leader starts at once.
        if LEADER.is_leader() and time.time() - last >= HISTORY.STEP:
            try:
                HISTORY.record(cached("ov", 10, get_overview))
                last = time.time()
                beat("history", HISTORY.STEP, leader_only=True)
            except Exception as error:
                beat("history", HISTORY.STEP, error, leader_only=True)
                print(f"history: {str(error)[:160]}", flush=True)
        time.sleep(30)


def _moves_loop():
    """The move engine, on the leader only: a move's next step is taken once."""
    while True:
        if LEADER.is_leader():
            try:
                MOVE_ENGINE.tick_all()
                beat("moves", MOVE_ENGINE.TICK_SECONDS, leader_only=True)
            except Exception as error:
                beat("moves", MOVE_ENGINE.TICK_SECONDS, error, leader_only=True)
        time.sleep(MOVE_ENGINE.TICK_SECONDS)


def mqtt_snapshot():
    """The numbers hv-exporter published, counted the way it counted them."""
    o = cached("ov", 10, get_overview)
    pods = kget("/api/v1/pods").get("items", [])
    try:
        vmis = kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
    except Exception:
        vmis = []
    system = lambda p: p["metadata"]["namespace"] in SYS_NS
    running = [p for p in pods if (p.get("status") or {}).get("phase") == "Running"]
    bad = [p for p in pods if (p.get("status") or {}).get("phase") in ("Failed", "Pending")]
    namespaces = {}
    for p in pods:
        if not system(p):
            namespaces[p["metadata"]["namespace"]] = namespaces.get(p["metadata"]["namespace"], 0) + 1
    ready = o.get("nodes_ready", 0)
    total = o.get("nodes_total", 0)
    notready = total - ready
    degraded, faulted = o.get("vol_degraded", 0), o.get("vol_faulted", 0)
    wl_bad = sum(1 for p in bad if not system(p))
    sys_bad = sum(1 for p in bad if system(p))
    health = ("critical" if notready or faulted else "degraded" if degraded or wl_bad or sys_bad else "healthy")
    nodes = []
    for node in o.get("nodes") or []:
        name = node["name"]
        mine = [p for p in running if (p.get("spec") or {}).get("nodeName") == name]
        nodes.append({"name": name, "cpu_pct": node.get("cpu_pct", 0), "mem_pct": node.get("mem_pct", 0),
                      "mem_gb": node.get("mem_used_gb", 0), "rx_mbps": round(node.get("rx_mbps", 0) or 0, 2),
                      "tx_mbps": round(node.get("tx_mbps", 0) or 0, 2), "pods": len(mine),
                      "vms": sum(1 for v in vmis if (v.get("status") or {}).get("nodeName") == name),
                      "wl": ", ".join(node.get("workloads") or []) or "none", "status": node.get("status", "NotReady")})
    return {"cluster": {"nodes_ready": ready, "nodes_total": total, "nodes_notready": notready,
                        "vol_total": o.get("volumes", 0), "vol_degraded": degraded, "vol_faulted": faulted,
                        "pods_system": sum(1 for p in running if system(p)),
                        "pods_workload": sum(1 for p in running if not system(p)),
                        "pods_sys_bad": sys_bad, "pods_wl_bad": wl_bad,
                        "vms_running": sum(1 for v in vmis if (v.get("status") or {}).get("phase") == "Running"),
                        "health": health, "wl_summary": " ".join(f"{ns}:{n}" for ns, n in sorted(namespaces.items())),
                        "cpu_pct": o.get("cpu_pct", 0), "mem_pct": o.get("mem_pct", 0)},
            "nodes": nodes}


def hv_exporter_present():
    """hv-exporter, if it still runs: it publishes the same topics."""
    for ns in (DEFAULT_NS, "lab"):
        try:
            kget(f"/apis/apps/v1/namespaces/{ns}/deployments/hv-exporter")
            return ns
        except Exception:
            continue
    return ""


MAX_REPLICAS = 3


def homestead_data_volume(dep=None):
    """Homestead's data claim, and whether pods on several nodes can mount it.

    Copies of Homestead on different nodes all mount this one claim, so it has
    to be ReadWriteMany on a class Longhorn serves through its share manager.
    A migratable class - Harvester's own, and longhorn-r2 - hands out a VM-disk
    volume that one node attaches, and a pod on a second node waits forever."""
    ns, name = SELF.NS, NAMES.BRAND
    dep = dep or kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    volumes = (dep["spec"]["template"]["spec"].get("volumes") or [])
    claim = next(((v.get("persistentVolumeClaim") or {}).get("claimName") for v in volumes
                  if v.get("name") == "data" and v.get("persistentVolumeClaim")), "")
    if not claim:
        return {"pvc": "", "shareable": False, "reason": "Homestead keeps no data claim", "candidates": []}
    pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    spec = pvc.get("spec") or {}
    klass = spec.get("storageClassName") or ""
    modes = spec.get("accessModes") or []
    rows = storage_classes()
    row = next((r for r in rows if r["name"] == klass), None)
    size = str(((pvc.get("status") or {}).get("capacity") or {}).get("storage")
               or ((spec.get("resources") or {}).get("requests") or {}).get("storage") or "2Gi")
    if "ReadWriteMany" not in modes:
        reason = f"{claim} is ReadWriteOnce: one node at a time can mount it"
    elif row and row.get("migratable"):
        reason = (f"{claim} is on {klass}, a migratable class: Longhorn gives it a VM-disk volume that "
                  "only one node can mount, so a copy on a second node would never start")
    else:
        reason = ""
    shared = shared_storage_classes(rows)
    # Every class it could move to, and whether copies on several nodes could
    # then share it: the move is not only for redundancy.
    classes = [{"name": r["name"], "shareable": r["name"] in shared} for r in rows
               if class_selectable(r) and r["name"] != klass]
    # Data volumes an earlier move left behind: kept until deleted, so they
    # are said out loud rather than found later as a mystery.
    try:
        kept = [p["metadata"]["name"] for p in kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])
                if p["metadata"]["name"].startswith(f"{name}-data") and p["metadata"]["name"] != claim]
    except Exception:
        kept = []
    return {"pvc": claim, "storage_class": klass, "access_modes": modes, "size": size,
            "shareable": not reason, "reason": reason, "candidates": shared, "classes": classes, "kept": kept}


ROLLING = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}


def own_strategy(shareable):
    """How Homestead replaces itself. Rolling - the new copy up before the old
    one goes - only when every node can mount the data volume; otherwise the
    two overlap on one volume, and on a migratable class Longhorn takes that
    for a VM migration and refuses the mount ("invalid controller count")."""
    return dict(ROLLING) if shareable else {"type": "Recreate"}


def fit_own_strategy():
    """An update from this page changes only the image, so a Deployment that
    was once set to roll keeps rolling. Put right at start-up what the data
    volume can take. The strategy is not part of the pod template, so this
    starts no rollout."""
    try:
        ns, name = SELF.NS, NAMES.BRAND
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        want = own_strategy(homestead_data_volume(dep)["shareable"])
        if (dep["spec"].get("strategy") or {}).get("type") != want["type"]:
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                  {"spec": {"strategy": {"type": want["type"], "rollingUpdate": want.get("rollingUpdate")}}},
                  ctype="application/merge-patch+json")
            print(f"own update strategy set to {want['type']}", flush=True)
    except Exception as error:
        print(f"could not check Homestead's own update strategy: {error}", flush=True)


def move_homestead_data(storage_class):
    """Copies Homestead's data to a new claim on another class, then points it there.

    The copy runs as a job on the node that has the current volume attached,
    since that is the only node that can mount it; Homestead keeps running
    throughout and restarts once, onto the new claim. The old claim is kept.

    The copy is of the moment it runs. Jobs record each step on this volume,
    so one still running would come back after the restart from an older
    step - a storage class change mid-swap, say - which is why nothing else
    may be running."""
    info = homestead_data_volume()
    target_row = next((c for c in info.get("classes") or [] if c["name"] == storage_class), None)
    if storage_class == info.get("storage_class"):
        raise ValueError(f"{info['pvc']} is on {storage_class} already")
    if not target_row:
        raise ValueError(f"storage class {storage_class} is not one Homestead's data can move to")
    ns = SELF.NS
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{NAMES.BRAND}")
    if not target_row["shareable"] and int((dep.get("spec") or {}).get("replicas") or 1) > 1:
        raise ValueError(f"{storage_class} gives a volume one node can mount, and {dep['spec']['replicas']} copies "
                         "of Homestead run: set Redundancy to one copy first")
    busy = [o for o in OPS.list_operations() if o.get("status") not in OPS.TERMINAL
            and o.get("kind") != "self-data-move"]
    if busy:
        raise ValueError(f"{len(busy)} job{'s are' if len(busy) != 1 else ' is'} still running ({busy[0].get('title', '')}"
                         f"{' and more' if len(busy) > 1 else ''}). The copy is taken as it stands and Homestead restarts "
                         "onto it, so a job running meanwhile would lose its later steps: let them finish first")
    names = {i["metadata"]["name"] for i in kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])}
    target = f"{NAMES.BRAND}-data-shared" if target_row["shareable"] else f"{NAMES.BRAND}-data-moved"
    n = 2
    while target in names:
        target, n = f"{NAMES.BRAND}-data-shared-{n}", n + 1
    size_gb = max(1, int(-(-parse_mem(info["size"]) // 1024**3)))
    selector = urllib.parse.quote(f"app={NAMES.BRAND}", safe="")
    pods = [p for p in kget(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}").get("items", [])
            if (p.get("status") or {}).get("phase") == "Running" and (p.get("spec") or {}).get("nodeName")]
    if not pods:
        raise ValueError("no running Homestead pod shows which node holds the data volume")
    node = pods[0]["spec"]["nodeName"]
    create_pvc(ns, target, size_gb, storage_class, "ReadWriteMany" if target_row["shareable"] else "ReadWriteOnce")
    job = f"{NAMES.BRAND}-data-move-{secrets.token_hex(3)}"
    body = {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": job, "namespace": ns, "labels": NAMES.labels("data-move")},
            "spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 86400,
                     "template": {"metadata": {"labels": NAMES.labels("data-move")},
                                  "spec": {"restartPolicy": "Never", "nodeName": node,
                                           "containers": [{"name": "copy", "image": "alpine:3.20",
                                                           "command": ["sh", "-c", "set -e; cp -a /old/. /new/; sync; echo copied"],
                                                           "securityContext": {"runAsUser": 0},
                                                           "volumeMounts": [{"name": "old", "mountPath": "/old", "readOnly": True},
                                                                            {"name": "new", "mountPath": "/new"}]}],
                                           "volumes": [{"name": "old", "persistentVolumeClaim": {"claimName": info["pvc"], "readOnly": True}},
                                                       {"name": "new", "persistentVolumeClaim": {"claimName": target}}]}}}}
    ksend("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", body)
    op = OPS.start("self-data-move", f"Move Homestead's data to {target}",
                   {"kind": "PersistentVolumeClaim", "name": target, "namespace": ns}, "/settings",
                   {"namespace": ns, "job": job, "old": info["pvc"], "new": target,
                    "storage_class": storage_class, "shareable": target_row["shareable"]}, "Copying")
    return {"ok": True, "operation": op, "detail": f"copying {info['pvc']} to {target} on {storage_class}; Homestead restarts onto it when done"}


def _data_move_status(item):
    """Waits for the copy, then points Homestead at the new claim. Run again
    after the switch - by the new pods, from the copied record - it finds the
    claim already switched and finishes."""
    ref = item["ref"]
    ns, name = ref["namespace"], NAMES.BRAND
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    volumes = dep["spec"]["template"]["spec"].get("volumes") or []
    data = next((v for v in volumes if v.get("name") == "data"), None)
    if data and (data.get("persistentVolumeClaim") or {}).get("claimName") == ref["new"]:
        where = ("which every node can mount" if ref.get("shareable", True)
                 else f"on {ref.get('storage_class', 'its new class')}")
        return "succeeded", 100, (f"Homestead keeps its data on {ref['new']}, {where}; "
                                  f"{ref['old']} is kept - delete it from Volumes once all is well")
    try:
        status = kget(f"/apis/batch/v1/namespaces/{ns}/jobs/{ref['job']}").get("status") or {}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "failed", 50, "the copy job went missing; Homestead still uses its old data claim"
        raise
    if status.get("failed") and not status.get("active") and not status.get("succeeded"):
        return "failed", 60, f"the copy failed; Homestead still uses {ref['old']}. The {ref['job']} job's log says why"
    if not status.get("succeeded"):
        return "running", 40 if status.get("active") else 15, "Copying Homestead's data"
    data["persistentVolumeClaim"]["claimName"] = ref["new"]
    # How it replaces itself follows the new volume: rolling only when
    # several nodes can mount it, otherwise the old copy goes first.
    dep["spec"]["strategy"] = own_strategy(ref.get("shareable", True))
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    return "running", 90, f"Copied; Homestead is restarting onto {ref['new']}"


OPS.RESOLVERS["self-data-move"] = _data_move_status


LOOP_WORDS = {"sampler": "Live charts", "alerts": "Alerts and notifications", "history": "Long-term stats",
              "hardware": "Hardware detection", "moves": "Cluster moves", "samba": "Network shares"}


def samba_state():
    """The Samba server shares are served from: whether it runs, and where."""
    dep = _optional_smb(_smb_path("deployments", SMB_NAME)) or _optional_smb(
        _smb_path("deployments", "samba"))
    try:
        rows, credentials, *_ = SHARES._state()
        configured = {row["name"] for row in rows}
        config_error = ""
    except Exception as error:
        configured = set()
        credentials = {}
        rows = []
        config_error = str(error)[:160]
    if not dep:
        return {"installed": False, "enabled": False, "shares": len(configured), "image": SAMBA_IMAGE,
                "name": SMB_NAME, "served_shares": [], "in_sync": not configured and not config_error,
                **({"error": config_error} if config_error else {})}
    name = dep["metadata"]["name"]
    status = dep.get("status") or {}
    address = ""
    try:
        svc = _optional_smb(_smb_path("services", SMB_NAME)) or _optional_smb(
            _smb_path("services", "samba")) or {}
        address = next((i.get("ip", "") for i in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []), "") \
            or ((svc.get("metadata") or {}).get("annotations") or {}).get("kube-vip.io/loadbalancerIPs", "") \
            or ((svc.get("metadata") or {}).get("annotations") or {}).get("metallb.universe.tf/loadBalancerIPs", "") \
            or ((svc.get("spec") or {}).get("loadBalancerIP") or "")
    except Exception:
        pass
    containers = ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or [{}]
    container = containers[0]
    paths = {mount.get("mountPath") for mount in container.get("volumeMounts", []) or []}
    args = container.get("args") or []
    served = {arg.split(";", 1)[0] for index, flag in enumerate(args[:-1]) if flag == "-s"
              for arg in [args[index + 1]] if ";" in arg and arg.split(";", 2)[1] in paths}
    try:
        recovery = SHARES.RECOVERY.read(dep)
        offline = SHARES.RECOVERY.offline_shares(dep, rows)
    except Exception as error:
        recovery, offline = {}, []
        config_error = str(error)[:160]
    expected_names = configured - {item["name"] for item in offline}
    in_sync = served == expected_names and not config_error and name == SMB_NAME
    if in_sync:
        try:
            expected = SHARES.configured_deployment(dep, rows, credentials)
            expected_spec = expected["spec"]["template"]["spec"]
            live_spec = dep["spec"]["template"]["spec"]
            expected_container = expected_spec["containers"][0]
            in_sync = (all(container.get(field) == expected_container.get(field)
                           for field in ("args", "volumeMounts"))
                       and live_spec.get("volumes", []) == expected_spec.get("volumes", [])
                       and container.get("image") == SAMBA_IMAGE)
        except Exception as error:
            config_error = str(error)[:160]
            in_sync = False
    desired = int((dep.get("spec") or {}).get("replicas", 1) or 0)
    return {"installed": True, "enabled": desired > 0, "desired": desired, "name": name,
            "ready": int(status.get("readyReplicas", 0) or 0), "address": address,
            "shares": len(configured), "served_shares": sorted(served), "in_sync": in_sync,
            "offline_shares": offline, "partial": bool(offline),
            "recovery_pending": recovery.get("pending", {}), "recovery_warning": recovery.get("warning", ""),
            "recovery_failures": recovery.get("restore_failures", {}),
            "image": container.get("image", ""), **({"error": config_error} if config_error else {})}


def set_samba(enabled, address=""):
    """Samba on or off. Off stops serving; every share, its volume and its
    password are kept for when it is switched back on. On installs it first
    if the cluster has none, with the shares already defined."""
    state = samba_state()
    name = state["name"]
    if not enabled:
        if state["installed"]:
            ksend("PATCH", _smb_path("deployments", name), {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
        _cache.pop("wl", None)
        return {"ok": True, "detail": "Samba is stopping; the shares, their volumes and passwords are kept"}
    if not state["installed"]:
        install_samba(address)
        rows, credentials, *_ = SHARES._state()
        if rows:
            SHARES.apply_samba(rows, credentials)
        _cache.pop("wl", None)
        return {"ok": True, "detail": "Samba is being installed" + (f" with {len(rows)} share{'s' if len(rows) != 1 else ''}" if rows else "")}
    if name == "samba":
        install_samba()
        name = SMB_NAME
    ksend("PATCH", _smb_path("deployments", name), {"spec": {"replicas": 1}},
          ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "detail": "Samba is starting"}


def remove_samba():
    """Uninstall only the SMB workload and address; never touch share data."""
    with SHARES.LOCK:
        removed = []
        for name in (SMB_NAME, "samba"):
            for kind in ("deployments", "services"):
                path = _smb_path(kind, name)
                if _optional_smb(path) is not None:
                    ksend("DELETE", path)
                    removed.append(f"{kind}/{name}")
        _cache.pop("wl", None)
        _cache.pop("network", None)
    return {"ok": True, "removed": removed,
            "detail": "SMB server removed. Share definitions, passwords, PVCs and their data were kept."}


def repair_samba(address=""):
    """Apply the saved inventory now, with the same rollout guard as edits."""
    with SHARES.LOCK:
        install_samba(address)
        result = SHARES.reconcile_samba(SAMBA_IMAGE, retry_recovery=True)
    _cache.pop("wl", None)
    return {"ok": True, "detail": "SMB mappings checked; recovered storage must pass the stability check before shares return" if result["state"] == "current"
            else "SMB is being repaired from the saved shares", "server": samba_state()}


def nfs_state():
    """The opt-in NFSv4 gateway is independent of the SMB server and PVCs."""
    dep = _optional_smb(_smb_path("deployments", NFS.NAME))
    svc = _optional_smb(_smb_path("services", NFS.NAME))
    rows, *_ = SHARES._state()
    names = [row["name"] for row in rows if row.get("nfs_clients")]
    desired = int(((dep or {}).get("spec") or {}).get("replicas", 0) or 0)
    ingress = ((((svc or {}).get("status") or {}).get("loadBalancer") or {}).get("ingress") or [])
    address = next((item.get("ip", "") for item in ingress if item.get("ip")), "")
    annotations = (((svc or {}).get("metadata") or {}).get("annotations") or {})
    address = address or annotations.get("kube-vip.io/loadbalancerIPs", "") or annotations.get(
        "metallb.universe.tf/loadBalancerIPs", "")
    return {"installed": bool(dep), "enabled": bool(dep) and desired > 0,
            "name": NFS.NAME, "image": NFS.IMAGE, "exports": names, "address": address,
            "ready": int(((dep or {}).get("status") or {}).get("readyReplicas", 0) or 0),
            "desired": desired, "recovery": nfs_recovery(rows, dep)}


def nfs_recovery(rows, deployment=None):
    errors, volumes = [], []
    def read(path, default, label):
        try:
            return kget(path)
        except Exception:
            errors.append(f"{label} could not be checked.")
            return default
    nodes = read("/api/v1/nodes", {}, "Node availability").get("items", [])
    lh = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
    replicas = read(f"{lh}/replicas", {}, "Storage replicas").get("items")
    policy = read(f"{lh}/settings/node-down-pod-deletion-policy", {}, "Longhorn node recovery").get("value")
    for pvc in sorted({row["pvc"] for row in rows if row.get("nfs_clients") and row.get("pvc")}):
        claim = read(f"/api/v1/namespaces/{SMB_NAMESPACE}/persistentvolumeclaims/{pvc}", {}, pvc)
        name = (claim.get("spec") or {}).get("volumeName")
        volume = read(f"{lh}/volumes/{name}", None, f"{pvc} replication") if name else None
        volumes.append({"pvc": pvc, "volume": volume})
    try:
        platform = PLATFORM.detect()
    except Exception:
        platform = {}
        errors.append("Load balancer availability could not be checked.")
    podspec = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return NFS.recovery_report(nodes, volumes, replicas, platform, policy, errors, podspec)


def _nfs_inventory(current=None):
    rows, _, config, _, _ = SHARES._state()
    identified = NFS.export_ids(rows, current)
    if identified != rows:
        SHARES._save_config(identified, config)
    return identified


def _nfs_exports(rows):
    return NFS.exports(rows, SHARES._pvc)


def _nfs_deployment(rows, address=""):
    selected = _nfs_exports(rows)
    if not selected:
        raise ValueError("choose at least one NFS export in Network Shares first")
    if PLATFORM.detect().get("load_balancer") == "servicelb":
        raise ValueError("NFS needs a dedicated VIP that preserves client IPs; install kube-vip under Cluster Add-ons first")
    cfg = {"name": NFS.NAME, "container_name": NFS.NAME, "namespace": SMB_NAMESPACE,
           "image": NFS.IMAGE, "cpu": "50m", "memory": "128Mi",
           "ports": [{"container": 2049, "name": "nfs", "protocol": "TCP", "expose": True}],
           "vip_mode": "manual" if address else "automatic", "lb_ip": address, "exclusive_vip": True}
    cfg = NETWORK.prepare_deploy(cfg)
    dep, svc = build_deployment(cfg)
    dep = NFS.configure(dep, selected)
    if svc:
        # NFS's CIDR rules must see the real client address, not a node SNAT.
        svc["spec"]["externalTrafficPolicy"] = "Local"
        svc["metadata"].setdefault("annotations", {})[NAMES.key("exclusive-vip")] = "true"
    return dep, svc


@SHARES.serialized
def reconcile_nfs(rows=None):
    """Keep a running NFS gateway's exports aligned with the saved inventory."""
    dep_path = _smb_path("deployments", NFS.NAME)
    current = _optional_smb(dep_path)
    if not current:
        return {"state": "absent"}
    if rows is None:
        rows = _nfs_inventory(current)
    selected = _nfs_exports(rows)
    if not selected:
        if int((current.get("spec") or {}).get("replicas", 0) or 0):
            ksend("PATCH", dep_path, {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
        return {"state": "off", "exports": 0}
    wanted = NFS.configure(current, selected)
    if NFS.same_pod_config(current, wanted):
        return {"state": "current", "exports": len(selected)}
    ksend("PUT", dep_path, wanted)
    _cache.pop("wl", None)
    return {"state": "updated", "exports": len(selected)}


def set_nfs(enabled, address=""):
    with SHARES.LOCK:
        path = _smb_path("deployments", NFS.NAME)
        current = _optional_smb(path)
        if not enabled:
            if current:
                ksend("PATCH", path, {"spec": {"replicas": 0}},
                      ctype="application/merge-patch+json")
            _cache.pop("wl", None)
            return {"ok": True, "detail": "NFS stopped; exports, shares and every PVC were kept"}
        rows = _nfs_inventory(current)
        _nfs_exports(rows)
        if not any(row.get("nfs_clients") for row in rows):
            raise ValueError("choose at least one NFS export in Network Shares first")
        if not current:
            dep, svc = _nfs_deployment(rows, address)
            ksend("POST", f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments", dep)
            try:
                ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", svc)
            except Exception:
                ksend("DELETE", path)
                raise
        else:
            reconcile_nfs(rows)
            if _optional_smb(_smb_path("services", NFS.NAME)) is None:
                _, svc = _nfs_deployment(rows, address)
                ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", svc)
            ksend("PATCH", path, {"spec": {"replicas": 1}},
                  ctype="application/merge-patch+json")
        _cache.pop("wl", None)
        return {"ok": True, "detail": "NFS is starting; exports use NFSv4/TCP on port 2049"}


def remove_nfs():
    """Remove only the NFS address and daemon, never a claim or share record."""
    with SHARES.LOCK:
        current = _optional_smb(_smb_path("deployments", NFS.NAME))
        if current:
            _nfs_inventory(current)
        removed = []
        for kind in ("deployments", "services"):
            path = _smb_path(kind, NFS.NAME)
            if _optional_smb(path) is not None:
                ksend("DELETE", path)
                removed.append(f"{kind}/{NFS.NAME}")
        _cache.pop("wl", None)
        _cache.pop("network", None)
    return {"ok": True, "removed": removed,
            "detail": "NFS server removed. Export settings, SMB shares, PVCs and data were kept."}


def set_nfs_export(name, clients, read_only=True):
    """Save one explicit export; an empty client network removes that export."""
    with SHARES.LOCK:
        rows, _, config_obj, _, _ = SHARES._state()
        rows = NFS.export_ids(rows, _optional_smb(_smb_path("deployments", NFS.NAME)))
        row = next((item for item in rows if item.get("name") == name), None)
        if row is None:
            raise ValueError("share not found")
        previous = copy.deepcopy(rows)
        if clients:
            row["nfs_clients"] = NFS.client_network(clients)
            row["nfs_read_only"] = bool(read_only)
        else:
            row.pop("nfs_clients", None)
            row.pop("nfs_read_only", None)
        _nfs_exports(rows)
        config_path = f"/api/v1/namespaces/{SMB_NAMESPACE}/configmaps/{SHARES.CONFIGMAP()}"
        written = SHARES._save_config(rows, config_obj)
        try:
            reconcile_nfs(rows)
        except Exception:
            SHARES._restore_object(config_path, config_obj, written)
            try:
                reconcile_nfs(previous)
            except Exception:
                pass
            raise
        return {"ok": True, "exports": nfs_state()["exports"],
                "detail": f"NFS export for {name} {'set' if clients else 'removed'}; its PVC was kept"}


def self_health():
    """Homestead's own health: the API it depends on, its copies and leader,
    each background task, the node probe, Samba and its permissions."""
    started = time.time()
    try:
        kget("/version")
        api = {"ok": True, "ms": int((time.time() - started) * 1000)}
    except Exception as error:
        api = {"ok": False, "ms": int((time.time() - started) * 1000), "error": str(error)[:160]}
    leading = LEADER.is_leader()
    now = time.time()
    loops = []
    with _heart_lock:
        rows = {k: dict(v) for k, v in HEART.items()}
    for name, word in LOOP_WORDS.items():
        row = rows.get(name)
        if not row:
            state = "standby" if name != "sampler" and not leading else "starting"
        elif row["leader_only"] and not leading:
            state = "standby"
        elif row["error"] and row["error_at"] >= row["last_ok"]:
            state = "failing"
        elif now - row["last_ok"] > max(3 * row["every"], 120):
            state = "late"
        else:
            state = "ok"
        loops.append({"name": name, "label": word, "state": state,
                      "last_ok": int(row["last_ok"]) if row and row["last_ok"] else 0,
                      "error": (row or {}).get("error", ""), "every": (row or {}).get("every", 0)})
    try:
        replicas = homestead_replicas()
    except Exception as error:
        replicas = {"error": str(error)[:160]}
    probe = dict(PROBE.status())
    try:
        ds = kget(f"/apis/apps/v1/namespaces/{DEFAULT_NS}/daemonsets/{NAMES.NODEPROBE}")
        st = ds.get("status") or {}
        probe.update(installed=True, desired=int(st.get("desiredNumberScheduled", 0) or 0),
                     ready=int(st.get("numberReady", 0) or 0))
    except Exception:
        probe.update(installed=False, desired=0, ready=0)
    try:
        temps = node_temps()
        probe["reporting"] = len(temps)
        probe["smart"] = sum(1 for t in temps.values() if (t.get("smart_helper") or {}).get("available"))
    except Exception:
        probe["reporting"] = probe["smart"] = 0
    try:
        samba = samba_state()
    except Exception as error:
        samba = {"error": str(error)[:160]}
    try:
        backups = OBJECTS.status()
    except Exception:
        backups = {}
    mqtt = {}
    try:
        mqtt = dict(MQTT.STATUS)
    except Exception:
        pass
    # Homestead's shared address, and any app, on the cluster's own address:
    # host joining (RKE2's 9345) and the dashboard answer there.
    try:
        network = cached("network", 5, NETWORK.inventory)
        addresses = {"lb_ip": LB_IP, "problem": (network.get("shared_vip") or {}).get("problem", ""),
                     "clashes": network.get("platform_clashes") or [],
                     "platform": sorted(network.get("platform_addresses") or {})}
    except Exception as error:
        addresses = {"lb_ip": LB_IP, "error": str(error)[:160]}
    return {"version": HOMESTEAD_VERSION, "addresses": addresses, "api": api, "leader": leading, "identity": LEADER.IDENTITY,
            "replicas": replicas, "loops": loops, "probe": probe, "samba": samba,
            "permissions": dict(SELF.LAST), "backups": {k: backups.get(k) for k in ("deployed", "ready", "endpoint")},
            "mqtt": {k: mqtt.get(k) for k in ("state", "detail", "error", "last_publish")}}


def homestead_replicas():
    """How many Homesteads run, where, and which one leads."""
    ns, name = SELF.NS, NAMES.BRAND
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    selector = ",".join(f"{k}={v}" for k, v in sorted(((dep["spec"].get("selector") or {}).get("matchLabels") or {}).items()))
    pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector={urllib.parse.quote(selector, safe='')}").get("items", [])
    try:
        holder = (kget(f"/apis/coordination.k8s.io/v1/namespaces/{ns}/leases/{LEADER.NAME}").get("spec") or {}).get("holderIdentity", "")
    except Exception:
        holder = ""
    rows = []
    for pod in pods:
        conditions = {c.get("type"): c.get("status") for c in (pod.get("status") or {}).get("conditions") or []}
        rows.append({"name": pod["metadata"]["name"], "node": (pod.get("spec") or {}).get("nodeName", ""),
                     "ready": conditions.get("Ready") == "True", "leader": pod["metadata"]["name"] == holder,
                     "this": pod["metadata"]["name"] == LEADER.IDENTITY,
                     "terminating": bool(pod["metadata"].get("deletionTimestamp"))})
    nodes = len({row["node"] for row in rows if row["node"] and row["ready"]})
    try:
        data = homestead_data_volume(dep)
    except Exception as error:
        data = {"pvc": "", "shareable": False, "reason": f"could not read the data claim: {str(error)[:120]}", "candidates": []}
    return {"desired": int(dep["spec"].get("replicas", 1) or 0), "pods": sorted(rows, key=lambda row: row["name"]),
            "leader": holder, "spread_nodes": nodes, "max": MAX_REPLICAS, "data": data}


def set_homestead_replicas(count):
    """Runs this many Homesteads, spread over different nodes where it can.

    More than one means a node failure leaves another already serving: the
    Service drops the dead one and the leader lease moves within seconds.
    Rolling updates replace one at a time, so an update never takes it down."""
    count = int(count)
    if not 1 <= count <= MAX_REPLICAS:
        raise ValueError(f"run between 1 and {MAX_REPLICAS} copies of Homestead")
    if count > 1:
        data = homestead_data_volume()
        if not data["shareable"]:
            raise ValueError(f"{data['reason']}. Move Homestead's data to a shareable volume first (Settings, Redundancy).")
    ns, name = SELF.NS, NAMES.BRAND
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    dep["spec"]["replicas"] = count
    dep["spec"]["strategy"] = own_strategy(homestead_data_volume(dep)["shareable"])
    labels = (dep["spec"].get("selector") or {}).get("matchLabels") or {"app": name}
    spec = dep["spec"]["template"]["spec"]
    affinity = spec.setdefault("affinity", {})
    spread = {"weight": 100, "podAffinityTerm": {"labelSelector": {"matchLabels": dict(labels)},
                                                 "topologyKey": "kubernetes.io/hostname"}}
    anti = affinity.setdefault("podAntiAffinity", {})
    preferred = [term for term in anti.get("preferredDuringSchedulingIgnoredDuringExecution") or [] if term != spread]
    anti["preferredDuringSchedulingIgnoredDuringExecution"] = preferred + [spread]
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    return {"ok": True, "desired": count,
            "detail": f"Homestead runs as {count} cop{'ies' if count != 1 else 'y'}" +
                      (", spread over different nodes" if count > 1 else "")}
# Join plans are gone; a job one left in Activity says so rather than erroring.
OPS.RESOLVERS["onboard"] = lambda item: ("cancelled", item.get("progress", 0),
                                         "Join plans were replaced by the install guide")
# A cleanup is recorded once it has happened, so it reads as done straight away.
OPS.RESOLVERS["cluster-cleanup"] = lambda item: ("succeeded", 100, item.get("message", ""))
VOLUMES.bind(kget, ksend, LH.snapshots, LH.backups, _cache, SYS_NS, DEFAULT_NS)
SHARES.bind(kget, ksend, create_pvc, SMB_NAMESPACE, _cache)
SHARES.install = install_samba
NETWORK.bind(kget, ksend, SYS_NS, DEFAULT_NS, LB_IP)
CLUSTER.bind(kget, SYS_NS, lambda: cached("nodes", 5, get_nodes))
CONSOLE_PROXY = CONSOLE.ConsoleProxy(API, TOKEN, CTX, DATA_DIR, SYS_NS, {DEFAULT_NS}, kget)
VM_CONSOLE = VMCONSOLE.VmConsole(CONSOLE_PROXY, SYS_NS, kget)
FILES.bind(kget, ksend, urllib.parse.urlparse(API), TOKEN, CTX, SYS_NS)


# Logos whose cached file is missing here - a workload moved from another
# cluster before moves brought logos, or a data volume that was replaced -
# fetched again in the background: reference -> what it is cached as now.
_ICON_HEALED = {}
_ICON_TRIED = {}
ICON_RETRY_SECONDS = 15 * 60


def _heal_icon(reference, annotations):
    moved_from = (NAMES.read(annotations, "moved-from") or "").split("/", 1)[0]
    found = ""
    if moved_from:
        try:
            found = MOVE_ENGINE.carry_icon(moved_from, dict(annotations))
        except Exception:
            found = ""
    if not found:
        source = NAMES.read(annotations, "icon-source") or ""
        if source.startswith(("http://", "https://")):
            try:
                found = ICONS.persist(source, DATA_DIR)
            except Exception:
                found = ""
    if found:
        _ICON_HEALED[reference] = found


def display_icon(annotations):
    reference = NAMES.read(annotations, "icon")
    try:
        return ICONS.data_url(_ICON_HEALED.get(reference, reference), DATA_DIR)
    except FileNotFoundError:
        if str(reference or "").startswith("/api/icons/") and                 time.time() - _ICON_TRIED.get(reference, 0) > ICON_RETRY_SECONDS:
            _ICON_TRIED[reference] = time.time()
            threading.Thread(target=_heal_icon, args=(reference, dict(annotations or {})),
                             daemon=True, name="icon-heal").start()
        return ""
    except (ValueError, OSError):
        return ""


def _service_listeners(deployment, services):
    """Map a container port onto the Service port that publishes it."""
    selector = (deployment["spec"].get("selector", {}) or {}).get("matchLabels", {}) or {}
    listeners = {}
    for service in services or []:
        chosen = (service.get("spec", {}) or {}).get("selector") or {}
        if not chosen or not all(selector.get(key) == value for key, value in chosen.items()):
            continue
        for port in (service.get("spec", {}) or {}).get("ports", []) or []:
            try:
                target = int(port.get("targetPort", port.get("port")))
                listeners[(str(port.get("protocol") or "TCP").upper(), target)] = int(port["port"])
            except (TypeError, ValueError):
                continue
    return listeners


def workload_edit_payload(ns, name, deployment, hardware_definitions=None, services=None):
    """Return pod-level settings plus an editable record for every app container."""
    pspec = deployment["spec"]["template"]["spec"]
    if services is None:
        try:
            services = kget(f"/api/v1/namespaces/{ns}/services").get("items", [])
        except Exception:
            services = []
    listeners = _service_listeners(deployment, services)
    annotations = deployment["metadata"].get("annotations", {}) or {}
    definitions = hardware_definitions if hardware_definitions is not None else HW.features()
    volumes = {volume.get("name"): volume for volume in pspec.get("volumes", []) or []}

    def source_label(volume):
        if volume.get("persistentVolumeClaim"):
            return volume["persistentVolumeClaim"].get("claimName", "")
        for field, label in (("configMap", "ConfigMap"), ("secret", "Secret")):
            if volume.get(field):
                return f"{label} {volume[field].get('name', '')}".strip()
        if volume.get("hostPath"):
            return volume["hostPath"].get("path", "host path")
        if "emptyDir" in volume:
            return "temporary storage"
        return "Kubernetes volume"

    def storage_kind(volume):
        """Map a pod volume onto the storage picker's vocabulary.

        Only claims, host paths and emptyDir are editable as storage. ConfigMap
        and Secret volumes are Kubernetes wiring that the editor shows but does
        not offer to repoint.
        """
        if volume.get("persistentVolumeClaim"):
            return "existing", volume["persistentVolumeClaim"].get("claimName", "")
        if volume.get("hostPath"):
            return "host", volume["hostPath"].get("path", "")
        if "emptyDir" in volume:
            empty = volume.get("emptyDir") or {}
            if str(empty.get("medium", "")).lower() == "memory":
                return "memory", str(empty.get("sizeLimit", "") or "")
            return "ephemeral", ""
        for field in ("configMap", "secret"):
            if volume.get(field):
                return field, volume[field].get("name", volume[field].get("secretName", ""))
        return "other", ""

    def env_reference(item):
        ref = item.get("valueFrom", {}) or {}
        for field, label in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
            if ref.get(field):
                value = ref[field]
                return f"{label} {value.get('name', '')} · {value.get('key', '')}".strip(" ·")
        if ref.get("fieldRef"):
            return f"Pod field · {ref['fieldRef'].get('fieldPath', '')}".strip(" ·")
        if ref.get("resourceFieldRef"):
            return f"Resource field · {ref['resourceFieldRef'].get('resource', '')}".strip(" ·")
        return "Managed Kubernetes reference"

    containers = []
    for container in pspec.get("containers", []) or []:
        mounts, hardware = [], []
        for mount in container.get("volumeMounts", []) or []:
            volume = volumes.get(mount.get("name"), {})
            kind, value = storage_kind(volume)
            host_path = (volume.get("hostPath") or {}).get("path", "").rstrip("/")
            mount_path = str(mount.get("mountPath") or "").rstrip("/")
            device = False
            for feature in definitions:
                expected_host = feature["host_path"].rstrip("/")
                if (host_path == expected_host or host_path.startswith(expected_host + "/")) and mount_path == feature["container_path"].rstrip("/"):
                    device = True
                    if feature["id"] not in hardware:
                        hardware.append(feature["id"])
            mounts.append({"name": mount.get("name", ""), "path": mount.get("mountPath", ""),
                           "source": source_label(volume), "read_only": bool(mount.get("readOnly", False)),
                           "sub_path": mount.get("subPath", ""),
                           "kind": kind, "value": value,
                           "managed": device or kind in ("configMap", "secret", "other")})
        literals = {item["name"]: item.get("value", "") for item in container.get("env", []) or []
                    if item.get("name") and "valueFrom" not in item}
        refs = [{"name": item["name"], "source": env_reference(item)}
                for item in container.get("env", []) or [] if item.get("name") and item.get("valueFrom")]
        requests = (container.get("resources", {}) or {}).get("requests", {}) or {}
        limits = (container.get("resources", {}) or {}).get("limits", {}) or {}
        containers.append({
            "original_name": container.get("name", ""), "name": container.get("name", ""),
            "image": container.get("image", ""), "cpu": requests.get("cpu", ""), "memory": requests.get("memory", ""),
            "memory_limit": limits.get("memory", ""),
            "env": literals, "env_refs": refs,
            "ports": [{"container": port.get("containerPort"), "name": port.get("name", ""),
                       "protocol": port.get("protocol", "TCP"),
                       "host": listeners.get((str(port.get("protocol") or "TCP").upper(),
                                              port.get("containerPort")), port.get("containerPort")),
                       "expose": (str(port.get("protocol") or "TCP").upper(),
                                  port.get("containerPort")) in listeners}
                      for port in container.get("ports", []) or []],
            "hardware": hardware,
            "volumes": [m for m in mounts if m["name"] != PRIV.TUN_VOLUME],
            "privileges": PRIV.read(container, pspec),
        })
    detected = HW.workload_features(pspec, annotations, definitions)
    assigned = {feature for container in containers for feature in container["hardware"]}
    if containers:
        containers[0]["hardware"].extend(feature for feature in detected if feature not in assigned)
    first = containers[0] if containers else {"name": "", "image": "", "cpu": "", "memory": "", "memory_limit": "", "env": {}, "ports": [], "volumes": []}
    reusable = []
    device_paths = {feature["host_path"].rstrip("/") for feature in definitions}
    for volume in pspec.get("volumes", []) or []:
        kind, value = storage_kind(volume)
        if kind not in ("existing", "host", "ephemeral", "memory"):
            continue
        if kind == "host" and any(value.rstrip("/") == device or value.rstrip("/").startswith(device + "/")
                                  for device in device_paths):
            continue
        reusable.append({"name": volume.get("name", ""),
                         "kind": {"existing": "pvc", "ephemeral": "emptyDir"}.get(kind, kind),
                         "source": value})
    replicas = deployment["spec"].get("replicas", 1) or 0
    try:
        parked = int(annotations.get(LC.AUTOSTART_REPLICAS, "") or 0)
    except ValueError:
        parked = 0
    return {
        "ns": ns, "name": name, "pod_hostname": pspec.get("hostname", ""),
        "replicas": replicas, "autostart": replicas > 0,
        "start_replicas": replicas or parked or 1, "containers": containers,
        "pod_volumes": reusable,
        "hardware": detected, "icon": NAMES.read(annotations, "icon-source") or NAMES.read(annotations, "icon"),
        "node": pspec.get("nodeSelector", {}).get("kubernetes.io/hostname", ""),
        "placement": AFFINITY.public(deployment),
        "failover": FAILOVER.mode_of(pspec),
        "lan": LAN.read(deployment),
        "network_mode": "host" if pspec.get("hostNetwork") else "",
        "has_service": bool(listeners),
        "seed_configs": LC.seed_configs(ns, deployment),
        "container_name": first["name"], "image": first["image"], "cpu": first["cpu"], "memory": first["memory"],
        "memory_limit": first["memory_limit"],
        "env": first["env"], "ports": first["ports"], "volumes": first["volumes"], "gpu": "igpu" in detected,
    }

# Browser routes serve the same authenticated application shell. Keep this an
# explicit allowlist: an unknown path must not accidentally shadow an API 404.
SPA_ROUTES = frozenset({
    "/", "/architecture", "/nodes", "/deploy", "/containers", "/vms",
    "/app-store", "/shares", "/volumes", "/image-cache", "/data-protection", "/portal", "/helm", "/resources",
    "/schedules", "/import", "/events", "/networking", "/system/cluster", "/settings",
})


def is_spa_route(path):
    clean = (path or "/").rstrip("/") or "/"
    return clean in SPA_ROUTES


def is_page_path(path):
    """A browser address that is not a known page: the app says so itself.

    An API path or anything that looks like a file keeps its plain 404, so a
    missing endpoint or script is never answered with a web page.
    """
    clean = path or "/"
    last = clean.rstrip("/").rsplit("/", 1)[-1]
    return (not clean.startswith("/api/") and clean != "/api"
            and "." not in last and ".." not in clean and len(clean) < 200)


# The largest honest request is a 1 MiB file edit, JSON-encoded.
MAX_BODY = 8 * 1024 * 1024

# What every response says about how it may be used. Inline handlers are how
# the pages are written, so scripts may be inline - but only from here: no
# script, frame or form from elsewhere, and no framing of Homestead at all.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
])


# Paths reachable without a session. Everything else needs one.
PUBLIC = {"/healthz", "/style.css", "/index.html", "/sw.js", "/manifest.webmanifest",
          "/api/auth/login", "/api/auth/state", "/api/auth/setup"}


def is_public_path(path):
    # Cached icons contain only size/type-validated images fetched from public
    # URLs. Serving their content-addressed paths without a session lets
    # browsers load them as subresources even when cookies are restricted.
    return path in PUBLIC or is_asset_path(path) or path.startswith("/api/icons/")


VENDOR_TYPES = {".js": "application/javascript", ".css": "text/css", ".ttf": "font/ttf",
                ".json": "application/json", ".svg": "image/svg+xml", ".map": "application/json",
                ".md": "text/markdown; charset=utf-8"}


def is_vendor_path(path):
    """A file from a vendored library, addressed by its own relative path."""
    path = path or ""
    return (bool(re.fullmatch(r"/vendor/[A-Za-z0-9][A-Za-z0-9/._-]*", path)) and
            ".." not in path and os.path.splitext(path)[1] in VENDOR_TYPES)


def is_asset_path(path):
    """Allow only flat, bundled SVG assets; never user-controlled filesystem paths."""
    return bool(re.fullmatch(r"/assets/[A-Za-z0-9][A-Za-z0-9._-]*\.svg", path or "")) or is_icon_png(path)


def is_icon_png(path):
    """The installed app's icons: flat, bundled PNGs."""
    return bool(re.fullmatch(r"/icons/[a-z0-9][a-z0-9-]*\.png", path or ""))


def is_app_identity(path):
    """What a browser reads to install the app: its manifest and icons."""
    return path == "/manifest.webmanifest" or is_icon_png(path)

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
    "/api/sources/containers", "/api/sources/inspect", "/api/sources/measure",
    "/api/import", "/api/imports/delete", "/api/imports/cleanup-plan",
    "/api/vm-disks/import",
    "/api/shares", "/api/shares/edit", "/api/shares/delete", "/api/shares/options",
    "/api/shares/nfs", "/api/self/nfs", "/api/addons/nfs/remove",
    "/api/storage/classes/default", "/api/storage/classes/delete", "/api/storage/classes/cleanup",
    "/api/network/service/delete",
    "/api/images/cleanup", "/api/images/scan", "/api/images/forget-rollback", "/api/images/vm/delete",
    "/api/volumes/delete", "/api/volumes/chown",
    # A class change stops workloads and swaps their volume underneath them.
    "/api/volumes/reclass/start", "/api/volumes/old-copies/remove", "/api/self/samba",
    "/api/addons/smb/remove",
    "/api/shares/repair",
    # Carrying a stopped job on runs its remaining steps - a swap, for one.
    "/api/operations/resume",
    "/api/network/vips/add", "/api/network/vips/remove", "/api/network/vips/label", "/api/network/vm-networks",
    "/api/files/list", "/api/files/read", "/api/files/write", "/api/files/close",
    "/api/node/smart/test",
    # Installing the probe stands a privileged container on every node.
    "/api/node/probe/install", "/api/node/probe/remove",
    # Object storage holds every backup, and its keys.
    "/api/objectstore/deploy", "/api/objectstore/longhorn", "/api/objectstore/remove",
    # A cluster's credentials, and what they reach.
    "/api/move/clusters/add", "/api/move/clusters/remove", "/api/move/remote",
    "/api/move/clusters/check", "/api/move/clusters/readiness", "/api/move/clusters/storage",
    # Joining and removing hosts: the join token, disk wipes, a DHCP responder.
    "/api/onboard/guide", "/api/cluster/cleanup", "/api/cluster/removal",
    "/api/cluster/remove-node", "/api/cluster/cleanup/run",
    # The source side of a move stops workloads, hands over definitions -
    # a VM's cloud-init Secrets among them - and the keys to the bucket.
    "/api/move/definition", "/api/move/target", "/api/move/source-status", "/api/move/source",
    # The destination side creates, restores and removes.
    "/api/move/plan", "/api/move/start", "/api/move/moves/retry",
    "/api/move/moves/abandon", "/api/move/moves/finish", "/api/move/moves/dismiss",
    "/api/lh/target", "/api/lh/job/delete", "/api/lh/snapshot/delete", "/api/lh/snapshot/revert",
    "/api/lh/restore", "/api/lh/backup/delete", "/api/lh/group/delete",
    # Installing Longhorn or KubeVirt changes the cluster itself.
    "/api/addons/longhorn", "/api/addons/kubevirt", "/api/addons/kubevirt/emulation",
    "/api/addons/multus", "/api/addons/kube-vip",
    # Upgrading the platform: the cluster, Longhorn, KubeVirt, CDI.
    "/api/cluster/components/upgrade", "/api/cluster/upgrades/start",
    # Homestead's own permissions, and the namespaces apps live in.
    "/api/self/permissions", "/api/namespaces/create", "/api/namespaces/delete",
}
# things a signed-in user may always do to their own account
SELF_ROUTES = {"/api/auth/logout", "/api/auth/password", "/api/auth/signout-everywhere",
               # Notifications on your own devices, and what they are shown.
               "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test",
               "/api/push/status", "/api/alerts/pending"}


def needed_role(path, method):
    if path in SELF_ROUTES:
        return "viewer"
    if path == "/api/hardware/features" and method != "GET":
        return "admin"
    if path == "/api/settings" and method != "GET":
        return "admin"
    if path == "/api/storage/classes" and method != "GET":
        return "admin"
    if path in ("/api/portal", "/api/self/replicas", "/api/self/data/move", "/api/ipam/unifi", "/api/mqtt", "/api/mqtt/test") and method != "GET":
        return "admin"
    # A chart can make anything anywhere in the cluster, and so can raw YAML;
    # a secret's values are for admins only.
    if path in ("/api/helm/install", "/api/helm/upgrade", "/api/helm/uninstall", "/api/resources/save", "/api/vm/delete",
                "/api/longhorn/settings", "/api/disks/add", "/api/disks/scheduling", "/api/disks/evict", "/api/disks/remove",
                "/api/disks/tags", "/api/disks/node-tags",
                # Replacing a failed disk deletes replicas and takes the disk out.
                "/api/disks/retire", "/api/disks/retire/plan",
                "/api/resources/delete", "/api/resources/create", "/api/resources/reveal"):
        return "admin"
    if path in ("/api/console", "/api/vm/console"):
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
        # A file that says how it may be cached says so alone: two Cache-Control
        # headers are read together, and no-store beside max-age wins.
        if not any(k.lower() == "cache-control" for k, _ in (self._extra_headers or [])):
            self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for k, v in (self._extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self._over_tls():
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _via_cloudflare(self):
        """Whether this request came in through Cloudflare, which always says so."""
        return bool(self.headers.get("Cf-Connecting-Ip") or self.headers.get("Cf-Ray"))

    def _client_ip(self):
        """Who is asking. X-Forwarded-For is whatever the client wrote, so it is not
        used; Cloudflare's own header is, and it replaces anything sent in it."""
        if self._via_cloudflare() and self.headers.get("Cf-Connecting-Ip"):
            return self.headers.get("Cf-Connecting-Ip").strip()
        return self.client_address[0] if self.client_address else ""

    def _file(self, path, ctype, cache=""):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._send(404, {"error": "not found"})
        if cache:
            self._extra_headers = list(getattr(self, "_extra_headers", [])) + [("Cache-Control", cache)]
        self._send(200, body, ctype)

    def _icon(self, request_path):
        try:
            path, ctype = ICONS.resolve(request_path, DATA_DIR)
            with open(path, "rb") as handle:
                body = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self._security_headers()
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

    def _over_tls(self):
        """Whether this request reached us encrypted, directly or via a proxy."""
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return forwarded == "https" or bool(getattr(self.connection, "context", None))

    def _set_cookie(self, token, clear=False, max_age=None):
        # Secure only behind TLS: Homestead is usually served over plain HTTP
        # on a LAN address, and a Secure cookie there would never be sent back.
        secure = "; Secure" if self._over_tls() else ""
        if clear:
            self._extra_headers.append(
                ("Set-Cookie",
                 f"{AUTH.COOKIE}=; Path=/; HttpOnly; SameSite=Strict{secure}; Max-Age=0"))
        else:
            age = AUTH.SESSION_TTL if max_age is None else max_age
            self._extra_headers.append(
                ("Set-Cookie", f"{AUTH.COOKIE}={token}; Path=/; HttpOnly; "
                               f"SameSite=Strict{secure}; Max-Age={age}"))

    def _move(self, call):
        """Run a move step, answering a refusal with 409 rather than 500.

        The Homestead on the other end waits out a 5xx and stops on a 4xx, so
        "no, it is still running" has to arrive as the second kind.
        """
        try:
            return self._send(200, call())
        except (ValueError, PermissionError) as error:
            return self._send(409, {"error": str(error)})
        except MOVE.Unreachable as error:
            return self._send(502, {"error": str(error)})

    def _who(self):
        return AUTH.verify_token(self._cookies().get(AUTH.COOKIE))

    def _guard(self, path):
        """Returns None when the request may proceed, or sends the refusal."""
        # The app's name and icons are fetched by the browser's installer, which
        # may not send Access's cookie; they say nothing about the cluster.
        if CFACCESS.enabled() and self._via_cloudflare() and not is_app_identity(path):
            token = self.headers.get("Cf-Access-Jwt-Assertion") or self._cookies().get("CF_Authorization", "")
            try:
                CFACCESS.verify(token)
            except ValueError as error:
                self._send(403, {"error": f"Cloudflare Access did not sign this request: {error}"})
                return True
        if (is_spa_route(path) or is_page_path(path) or is_public_path(path) or is_vendor_path(path) or
                (path.startswith("/js/") and path.endswith(".js"))):
            return None
        who = self._who()
        if not who:
            self._send(401, {"error": "not signed in", "auth": False})
            return True
        # Used recently enough to be worth extending: the idle clock restarts,
        # the absolute one does not, so working never signs anyone out.
        if who.get("stale"):
            self._set_cookie(AUTH.issue_token(who["user"], who["remember"], who["started"]),
                             max_age=AUTH.idle_ttl(who["remember"]))
        # CSRF: the cookie is SameSite=Strict, and mutations additionally require a
        # header that a cross-site form cannot set.
        if self.command in ("POST", "DELETE", "PUT", "PATCH"):
            if self.headers.get("X-Homestead-Auth") != "1":
                self._send(403, {"error": "missing X-Homestead-Auth header"})
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
            if p == "/api/vm/console":
                return VM_CONSOLE.handle(self, self.user, q)
            if p.startswith("/api/icons/"):
                return self._icon(p)
            if is_spa_route(p) or p == "/index.html" or is_page_path(p):
                return self._file(f"{WEBROOT}/index.html", "text/html; charset=utf-8")
            if p.startswith("/js/") and p.endswith(".js") and ".." not in p:
                return self._file(f"{WEBROOT}/js/{os.path.basename(p)}", "application/javascript")
            if is_asset_path(p) and not is_icon_png(p):
                return self._file(f"{WEBROOT}/assets/{os.path.basename(p)}", "image/svg+xml")
            if is_vendor_path(p):
                # Vendored paths carry no version, so the browser checks back
                # each time; the installed app's worker keeps them per release.
                return self._file(f"{WEBROOT}{p}", VENDOR_TYPES[os.path.splitext(p)[1]], cache="no-cache")
            if p == "/app.js":
                return self._file(f"{WEBROOT}/app.js", "application/javascript")
            if p == "/sw.js":
                # Never cached by the browser's HTTP cache: a new release has to
                # reach the worker that decides what else is cached.
                return self._file(f"{WEBROOT}/sw.js", "application/javascript", cache="no-cache")
            if p == "/manifest.webmanifest":
                return self._file(f"{WEBROOT}/manifest.webmanifest", "application/manifest+json",
                                  cache="no-cache")
            if is_icon_png(p):
                return self._file(f"{WEBROOT}/icons/{os.path.basename(p)}", "image/png",
                                  cache="public, max-age=86400")
            if p == "/api/push/key":
                return self._send(200, {"key": PUSH.public_key(), "categories": PUSH.CATEGORIES,
                                        "defaults": PUSH.DEFAULT_CATEGORIES})
            if p == "/api/alerts":
                return self._send(200, {"active": [a for a in ALERTS.active() if a.get("announced", 0) > 0],
                                        "log": [a for a in ALERTS.log(limit=30)["alerts"] if a["category"] != "test"],
                                        "devices": PUSH.devices(self.user)})
            if p == "/style.css":
                return self._file(f"{WEBROOT}/style.css", "text/css")
            if p == "/healthz":
                return self._send(200, {"ok": True})
            if p == "/api/auth/state":
                who = self._who()
                return self._send(200, {"setup": AUTH.needs_setup(),
                                        "user": who["user"] if who else None,
                                        "role": who["role"] if who else None,
                                        "remember": bool(who and who.get("remember")),
                                        "session_expires": who.get("expires") if who else None,
                                        "session_started": who.get("started") if who else None,
                                        "session_max_days": AUTH.ABSOLUTE_TTL // 86400,
                                        "roles": list(AUTH.ROLES)})
            if p == "/api/auth/users":
                return self._send(200, AUTH.list_users())
            if p == "/api/settings":
                return self._send(200, app_settings_payload())
            if p == "/api/overview":
                return self._send(200, cached("ov", 5, get_overview))
            if p == "/api/nodes":
                return self._send(200, cached("nodes", 5, get_nodes))
            if p == "/api/nodes/uptime":
                return self._send(200, cached("uptime", 60, HISTORY.uptime))
            if p == "/api/workloads":
                return self._send(200, cached("wl", 5, get_workloads))
            if p == "/api/portal":
                return self._send(200, {"links": PORTAL.view(), "icons": list(PORTAL.BUILTIN)})
            if p == "/api/portal/status":
                return self._send(200, PORTAL.status(force=(q.get("force") or [""])[0] == "1"))
            if p == "/api/portal/candidates":
                return self._send(200, PORTAL.candidates())
            if p == "/api/network":
                return self._send(200, cached("network", 5, NETWORK.inventory))
            if p == "/api/cluster":
                return self._send(200, cached("cluster", 15, CLUSTER.inventory))
            if p == "/api/self/replicas":
                return self._send(200, homestead_replicas())
            if p == "/api/ipam":
                return self._send(200, IPAM.view())
            if p == "/api/images/vm":
                return self._send(200, cached("vmimages", 15, IMP.vm_image_cache))
            if p in ("/api/resources/list", "/api/resources/object", "/api/resources/reveal"):
                arg = lambda key: (q.get(key) or [""])[0]
                if p == "/api/resources/list":
                    return self._send(200, RESOURCES.list_objects(arg("group"), arg("version"), arg("resource"), arg("ns")))
                return self._send(200, RESOURCES.get_object(arg("group"), arg("version"), arg("resource"), arg("ns"),
                                                            arg("name"), reveal=p.endswith("reveal")))
            if p == "/api/resources/kinds":
                return self._send(200, RESOURCES.discover(force=(q.get("force") or [""])[0] == "1"))
            if p == "/api/resources/events":
                return self._send(200, RESOURCES.events_for((q.get("ns") or [""])[0], (q.get("name") or [""])[0],
                                                            (q.get("uid") or [""])[0]))
            if p == "/api/platform":
                return self._send(200, PLATFORM.detect(force=(q.get("force") or [""])[0] == "1"))
            if p == "/api/addons":
                return self._send(200, ADDONS.status())
            if p == "/api/platform/join":
                return self._send(200, PLATFORM.join_guide())
            if p == "/api/mqtt":
                return self._send(200, {**MQTT.public(), "hv_exporter": hv_exporter_present(),
                                        "sensors": {"cluster": len(MQTT.CLUSTER_SENSORS), "node": len(MQTT.NODE_SENSORS)}})
            if p == "/api/mqtt/preview":
                snap = mqtt_snapshot()
                return self._send(200, {"states": [{"topic": t, "payload": v} for t, v in MQTT.states(MQTT.load(), snap)]})
            if p == "/api/helm":
                return self._send(200, cached("helm", 10, HELM.releases))
            if p == "/api/helm/release":
                return self._send(200, HELM.release((q.get("ns") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/helm/search":
                return self._send(200, HELM.search((q.get("q") or [""])[0]))
            if p == "/api/helm/chart":
                return self._send(200, HELM.chart((q.get("repo") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/cluster/components":
                if (q.get("force") or [""])[0] == "1":
                    _cache.pop("components", None)
                    return self._send(200, COMPONENTS.report(force=True))
                return self._send(200, cached("components", 60, COMPONENTS.report))
            if p == "/api/cluster/upgrades":
                current = ((cached("cluster", 15, CLUSTER.inventory) or {}).get("versions") or {}).get("harvester", "")
                return self._send(200, UPGRADES.report(current, force=(q.get("force") or [""])[0] == "1"))
            # What this cluster offers another one. Read-only, and the half
            # of a move the far cluster calls.
            if p == "/api/move/inventory":
                return self._send(200, MOVE.inventory())
            if p == "/api/move/clusters":
                return self._send(200, MOVE.list_clusters())
            if p == "/api/onboard/guide":
                return self._move(ONBOARD.guide)
            if p == "/api/cluster/cleanup":
                return self._move(ONBOARD.cleanup_report)
            if p == "/api/cluster/removal":
                return self._move(lambda: ONBOARD.removal_plan((q.get("node") or [""])[0]))
            # Which release this is, asked by another Homestead before a move.
            if p == "/api/move/hello":
                return self._send(200, MOVE.hello())
            if p == "/api/move/moves":
                return self._send(200, MOVE_ENGINE.moves())
            if p == "/api/move/definition":
                return self._move(lambda: MOVE_SOURCE.definition(
                    (q.get("kind") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/move/source-status":
                return self._move(lambda: MOVE_SOURCE.status(
                    (q.get("kind") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/move/target":
                return self._move(MOVE_SOURCE.target)
            if p == "/api/objectstore":
                return self._send(200, OBJECTS.status())
            if p == "/api/image-updates/scan-progress":
                # Read while a scan is in flight, so it needs no session cache
                # and must not be served from one.
                return self._send(200, UPDATES.scan_progress())
            if p == "/api/image-updates":
                force = (q.get("force") or ["0"])[0].lower() in ("1", "true", "yes")
                report = json.loads(json.dumps(UPDATES.report(force)))
                report["policy"] = update_policy_status()
                return self._send(200, report)
            if p == "/api/image-updates/progress":
                return self._send(200, UPDATES.progress(q["ns"][0], q["name"][0]))
            if p == "/api/operations":
                return self._send(200, OPS.list_operations())
            if p == "/api/operations/log":
                return self._send(200, OPS.log((q.get("id") or [""])[0]))
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
                if not disk:
                    return self._send(200, SMART.inventory(node))
                report = SMART.disk(node, disk)
                # The same verdict the node card shows, so one drive cannot be
                # healthy in the list and something else in its own detail.
                report["health_assessment"] = smart_disk_health(
                    report, get_app_settings().get("smart"))
                return self._send(200, report)
            if p == "/api/history/long":
                return self._send(200, HISTORY.series((q.get("range") or ["24h"])[0]))
            if p == "/api/history":
                with _lock:
                    return self._send(200, {k: list(v) for k, v in HIST.items()})
            if p == "/api/flow":
                return self._send(200, cached("flow2", 8, get_flow2))
            if p == "/api/shares":
                return self._send(200, SHARES.list_shares())
            if p == "/api/shares/server":
                return self._send(200, samba_state())
            if p == "/api/shares/nfs/server":
                return self._send(200, nfs_state())
            if p == "/api/files/list":
                return self._send(200, FILES.list_files(
                    (q.get("namespace") or [DEFAULT_NS])[0], (q.get("pvc") or [""])[0],
                    (q.get("path") or [""])[0]))
            if p == "/api/files/read":
                return self._send(200, FILES.read_file(
                    (q.get("namespace") or [DEFAULT_NS])[0], (q.get("pvc") or [""])[0],
                    (q.get("path") or [""])[0]))
            if p == "/api/volumes/ownership":
                return self._send(200, IMP.ownership_hint(
                    (q.get("namespace") or [DEFAULT_NS])[0], (q.get("name") or [""])[0]))
            if p == "/api/shares/options":
                return self._send(200, share_storage_options())
            if p == "/api/move/plan":
                return self._send(200, PLACE.plan(
                    q["ns"][0], q["name"][0],
                    float((q.get("cpu") or [0])[0]), float((q.get("mem") or [0])[0])))
            if p == "/api/node/impact":
                node = (q.get("node") or [""])[0]
                if not node:
                    return self._send(400, {"error": "node is required"})
                return self._send(200, cached("impact:" + node, 5, lambda: PLACE.impact(node)))
            if p == "/api/node/power/plan":
                return self._send(200, POWER.plan((q.get("node") or [""])[0],
                                                  (q.get("action") or [""])[0]))
            if p == "/api/workloads/start-plan":
                return self._send(200, workload_start_plan(
                    (q.get("ns") or [""])[0], (q.get("name") or [""])[0],
                    int((q.get("replicas") or [1])[0])))
            if p == "/api/quorum":
                r = LC.quorum_report(); r["power_enabled"] = LC.NODE_POWER_ENABLED
                return self._send(200, r)
            if p == "/api/vms":
                return self._send(200, cached("vms", 5, VMS.list_vms))
            if p == "/api/vm":
                return self._send(200, VMS.detail((q.get("ns") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/vm/create-options":
                return self._send(200, vm_create_options())
            if p == "/api/vmimages":
                return self._send(200, cached("vmimg", 30, IMP.list_vm_images))
            if p == "/api/vm-disks":
                return self._send(200, IMP.list_vm_disks())
            if p == "/api/vm-disks/import-plan":
                return self._send(200, IMP.vm_disk_import_plan(
                    (q.get("ns") or [DEFAULT_NS])[0], (q.get("name") or [""])[0]))
            if p == "/api/images":
                return self._send(200, cached("imgcache", 30, IMP.image_cache))
            if p == "/api/hardware/features":
                return self._send(200, cached("hardware:features", 15, HW.features))
            if p == "/api/schedules":
                return self._send(200, cached("cron", 8, IMP.list_jobs))
            if p == "/api/lh/overview":
                return self._send(200, cached("lhov", 8, LH.overview))
            if p == "/api/lh/snapshot/revert/plan":
                return self._send(200, REVERT.plan((q.get("volume") or [""])[0], (q.get("snapshot") or [""])[0]))
            if p == "/api/lh/snapshots":
                vol = (q.get("volume") or [None])[0]
                return self._send(200, LH.snapshots(vol))
            if p == "/api/lh/backups":
                vol = (q.get("volume") or [None])[0]
                return self._send(200, LH.backups(vol))
            if p == "/api/lh/backupvolumes":
                return self._send(200, cached("lhbackupvols", 15, LH.backup_volumes))
            if p == "/api/lh/restore/plan":
                return self._send(200, LH.restore_plan(
                    (q.get("backup") or [""])[0],
                    (q.get("ns") or [DEFAULT_NS])[0],
                    (q.get("name") or [""])[0]))
            if p == "/api/sources":
                return self._send(200, IMP.list_sources())
            if p == "/api/imports":
                return self._send(200, IMP.import_status())
            if p == "/api/workload":
                ns, nm = q["ns"][0], q["name"][0]
                d = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{nm}")
                return self._send(200, workload_edit_payload(ns, nm, d))
            if p == "/api/namespaces":
                # Places to put an app: Harvester's, Rancher's and Kubernetes'
                # own namespaces are left out unless all are asked for.
                return self._send(200, NSMOD.names((q.get("all") or [""])[0] == "1"))
            if p == "/api/namespaces/manage":
                return self._send(200, NSMOD.inventory())
            if p == "/api/storageclasses":
                classes = storage_classes()
                if (q.get("facts") or [""])[0] == "1":
                    return self._send(200, {"names": selectable_storage_classes(classes),
                                            "shared": shared_storage_classes(classes),
                                            "facts": storage_class_facts(classes)})
                return self._send(200, selectable_storage_classes(classes))
            if p == "/api/storage/classes":
                return self._send(200, storage_class_inventory())
            if p == "/api/self/health":
                return self._send(200, self_health())
            if p == "/api/volumes/old-copies":
                return self._send(200, RECLASS.old_copies())
            if p == "/api/storage/v2":
                return self._send(200, v2_engine_status())
            if p == "/api/disks":
                return self._send(200, cached("disks", 10, DISKS.inventory))
            if p == "/api/longhorn/capacity":
                return self._send(200, cached("lhcap", 15, LHCAP.status))
            if p == "/api/pvcs":
                ns = (q.get("ns") or [DEFAULT_NS])[0]
                items = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims")["items"]
                return self._send(200, [{"name": i["metadata"]["name"],
                                         "size": (i.get("status", {}).get("capacity", {}) or {}).get("storage") or
                                                 i["spec"]["resources"]["requests"]["storage"],
                                         "status": i.get("status", {}).get("phase", "Unknown"),
                                         "access_modes": i.get("spec", {}).get("accessModes", []),
                                         "storage_class": i.get("spec", {}).get("storageClassName", "")}
                                        for i in items])
            if p == "/api/deploy/options":
                ns = (q.get("ns") or [DEFAULT_NS])[0]
                return self._send(200, deploy_options(ns))
            if p == "/api/appstore":
                term = (q.get("q") or [""])[0].lower().strip()
                cat = (q.get("cat") or [""])[0].lower().strip()
                sort_mode = (q.get("sort") or ["home"])[0].lower().strip()
                if sort_mode not in {"home", "spotlight", "popular", "trending", "recent"}:
                    sort_mode = "home"
                try:
                    apps = fetch_appstore()
                except Exception as e:
                    return self._send(502, {"error": f"app feed unavailable: {e}"})
                source = {"url": catalog_source(), "default": catalog_source() == CA_FEED}
                if sort_mode == "home" and not term and not cat:
                    # The catalogue's front page, as Community Applications lays it out.
                    return self._send(200, {"sort": "home", "total": len(apps), "source": source, "sections": {
                        mode: [appstore_summary(a) for a in rank_appstore(apps, mode)[:count]]
                        for mode, count in (("spotlight", 4), ("recent", 8), ("trending", 8), ("popular", 8))}})
                if sort_mode == "home":
                    sort_mode = "popular"
                if term:
                    apps = search_appstore(apps, term)
                else:
                    apps = rank_appstore(apps, sort_mode)
                if cat:
                    apps = [a for a in apps if any(cat in value.lower() for value in a.get("categories", []))]
                spotlight = None if term else appstore_spotlight(apps)
                limit = 60 if term else 30
                return self._send(200, {"total": len(apps), "apps": [appstore_summary(a) for a in apps[:limit]],
                                        "sort": "search" if term else sort_mode, "source": source,
                                        "spotlight": appstore_summary(spotlight) if spotlight else None})
            if p == "/api/appstore/app":
                key = (q.get("key") or [""])[0]
                try:
                    app = next((a for a in fetch_appstore() if appstore_key(a) == key), None)
                except Exception as e:
                    return self._send(502, {"error": f"app feed unavailable: {e}"})
                if not app:
                    return self._send(404, {"error": "that app is no longer in the catalogue"})
                return self._send(200, {k: v for k, v in app.items() if k != "config"})
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
                container = (q.get("container") or [""])[0]
                if not container:
                    # A pod with more than one container has to be told which;
                    # asking without a name is a 400 from Kubernetes.
                    try:
                        spec = kget(f"/api/v1/namespaces/{ns}/pods/{pod}").get("spec", {}) or {}
                        names = [c.get("name") for c in spec.get("containers", []) or []]
                        container = names[0] if names else ""
                    except Exception:
                        container = ""
                query = f"tailLines={tail}&timestamps=true" + (
                    f"&container={urllib.parse.quote(container)}" if container else "")
                req = urllib.request.Request(f"{API}/api/v1/namespaces/{ns}/pods/{pod}/log?{query}",
                                             headers={"Authorization": f"Bearer {TOKEN}"})
                try:
                    with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
                        return self._send(200, r.read().decode("utf-8", "replace"), "text/plain; charset=utf-8")
                except urllib.error.HTTPError as error:
                    return self._send(409 if error.code in (400, 404) else error.code,
                                      {"error": logs_refusal(error, pod, container)})
            return self._send(404, {"error": "no route"})
        except AUTH.StoreUnavailable as e:
            # Not an empty account store: the cluster did not answer.
            return self._send(503, {"error": str(e), "unavailable": True})
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
            if int(self.headers.get("Content-Length") or 0) > MAX_BODY:
                return self._send(413, {"error": "that request is larger than Homestead accepts"})
            b = self._body()
            addr = self._client_ip()
            if p == "/api/auth/setup":
                # Whoever finishes setup becomes the first administrator, so it is
                # not offered to the internet, however the hostname is protected.
                if self._via_cloudflare():
                    return self._send(403, {"error": "finish setting Homestead up from your LAN; "
                                                     "setup is not offered through the tunnel"})
                AUTH.create_user(b.get("username"), b.get("password"), first_only=True)
                remember = bool(b.get("remember"))
                tok = AUTH.issue_token((b.get("username") or "").strip().lower(), remember)
                self._set_cookie(tok, max_age=AUTH.idle_ttl(remember))
                return self._send(200, {"ok": True, "user": b.get("username")})
            if p == "/api/auth/login":
                try:
                    remember = bool(b.get("remember"))
                    tok = AUTH.login(b.get("username"), b.get("password"), addr, remember)
                except PermissionError as e:
                    return self._send(401, {"error": str(e)})
                self._set_cookie(tok, max_age=AUTH.idle_ttl(remember))
                return self._send(200, {"ok": True, "remember": remember,
                                        "user": (b.get("username") or "").strip().lower()})
            if p == "/api/auth/logout":
                self._set_cookie("", clear=True)
                return self._send(200, {"ok": True})
            if p == "/api/push/subscribe":
                try:
                    return self._send(200, PUSH.subscribe(
                        self.user, b.get("subscription"), b.get("categories"), b.get("device", ""),
                        b.get("replaces", ""), cursor=ALERTS.log(limit=0)["latest"]))
                except ValueError as error:
                    return self._send(400, {"error": str(error)})
            if p == "/api/namespaces/create":
                return self._move(lambda: NSMOD.create(b.get("name")))
            if p == "/api/namespaces/delete":
                return self._move(lambda: NSMOD.delete(b.get("name"), str(b.get("confirm") or "")))
            if p == "/api/self/permissions":
                return self._send(200, SELF.reconcile())
            if p == "/api/push/unsubscribe":
                return self._send(200, PUSH.unsubscribe(self.user, str(b.get("endpoint") or "")))
            if p == "/api/push/status":
                row = PUSH.mine(self.user, str(b.get("endpoint") or ""))
                return self._send(200, {"known": bool(row), "tag": PUSH.tag(row["endpoint"]) if row else "",
                                        "categories": (row or {}).get("categories", []),
                                        "last_ok": (row or {}).get("last_ok", 0),
                                        "failures": (row or {}).get("failures", 0)})
            if p == "/api/push/test":
                endpoint = str(b.get("endpoint") or "")
                if not PUSH.mine(self.user, endpoint):
                    return self._send(404, {"error": "this device is not set up for notifications"})
                ALERTS.note({"key": f"test:{int(time.time())}", "category": "test", "severity": "info",
                             "title": "Homestead notifications work",
                             "body": "This device will be told when something needs you.",
                             "href": "/settings", "to": PUSH.tag(endpoint)})
                result = PUSH.send(lambda row: row["endpoint"] == endpoint, urgency="high")
                status = (result["statuses"] or [0])[0]
                if not result["sent"]:
                    return self._send(502, {"error": f"the push service refused the push (HTTP {status})"
                                            if status else "the push service could not be reached"})
                return self._send(200, {"ok": True})
            if p == "/api/alerts/pending":
                return self._send(200, alerts_pending(self.user, str(b.get("endpoint") or "")))
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
                return self._send(200, reviewed_deploy(b))
            if p == "/api/compose/parse":
                return self._send(200, compose_report(b))
            if p == "/api/compose/apply":
                return self._send(200, compose_apply(b))
            if p == "/api/portal":
                return self._send(200, PORTAL.save(b.get("links")))
            if p == "/api/self/replicas":
                return self._send(200, set_homestead_replicas(b.get("replicas")))
            if p == "/api/self/data/move":
                return self._send(200, move_homestead_data(b.get("storage_class", "")))
            if p == "/api/cluster/components/upgrade":
                result = COMPONENTS.upgrade(str(b.get("component") or ""), str(b.get("to") or ""))
                for key in ("components", "helm", "platform"):
                    _cache.pop(key, None)
                result["operation"] = OPS.start(
                    "platform-upgrade", f"Upgrade {result['name']} to {result['to']}",
                    {"kind": "Cluster" if result["component"] == "cluster" else "HelmChart", "name": result["name"],
                     "namespace": ""}, "/system/cluster",
                    {"component": result["component"], "name": result["name"], "from": result["from"],
                     "to": result["to"], "started": time.time(),
                     "phase": "controller" if result["component"] == "cluster" else ""},
                    result["detail"])
                return self._send(200, result)
            if p == "/api/cluster/upgrades/start":
                version = str(b.get("version") or "")
                name = COMPONENTS.start_harvester(version, UPGRADES.offered())
                _cache.pop("cluster", None)
                operation = OPS.start("harvester-upgrade", f"Upgrade Harvester to {version}",
                                      {"kind": "Upgrade", "name": name, "namespace": "harvester-system"},
                                      "/system/cluster", {"upgrade": name, "version": version},
                                      "Harvester checks the cluster, then prepares each node")
                return self._send(200, {"ok": True, "upgrade": name, "operation": operation,
                                        "detail": f"Harvester is upgrading to {version}"})
            if p in ("/api/addons/longhorn", "/api/addons/kubevirt", "/api/addons/multus", "/api/addons/kube-vip"):
                what = p.rsplit("/", 1)[1]
                result = {"longhorn": ADDONS.install_longhorn, "kubevirt": ADDONS.install_kubevirt,
                          "multus": ADDONS.install_multus, "kube-vip": ADDONS.install_kube_vip}[what](b)
                for key in ("helm", "platform"):
                    _cache.pop(key, None)
                result["operation"] = OPS.start("helm", f"Install {({'longhorn': 'Longhorn', 'kubevirt': 'KubeVirt', 'kube-vip': 'kube-vip'}).get(what, 'Multus')}",
                                                {"kind": "HelmChart", "name": result["name"], "namespace": ADDONS.CONTROLLER_NS},
                                                "/settings", {"namespace": ADDONS.CONTROLLER_NS, "name": result["job"],
                                                              "action": "install"},
                                                "Waiting for the Helm controller")
                return self._send(200, result)
            if p == "/api/addons/kubevirt/emulation":
                _cache.pop("platform", None)
                return self._send(200, ADDONS.set_kubevirt_emulation(bool(b.get("enabled"))))
            if p in ("/api/helm/install", "/api/helm/upgrade", "/api/helm/uninstall"):
                action = p.rsplit("/", 1)[1]
                result = (HELM.install(b) if action == "install" else HELM.upgrade(b) if action == "upgrade"
                          else HELM.uninstall(b.get("namespace", ""), b.get("name", "")))
                _cache.pop("helm", None)
                name = result.get("name") or b.get("name", "")
                job = f"helm-{'delete' if action == 'uninstall' else 'install'}-{name}"
                result["operation"] = OPS.start("helm", f"Helm {action} {name}",
                                                {"kind": "HelmChart", "name": name, "namespace": HELM.CONTROLLER_NS},
                                                "/helm", {"namespace": HELM.CONTROLLER_NS, "name": job,
                                                          "action": action},
                                                "Waiting for the Helm controller")
                return self._send(200, result)
            if p == "/api/resources/save":
                guard_smb_object(b.get("resource"), b.get("ns"), b.get("name"))
                return self._send(200, RESOURCES.save_object(b.get("group", ""), b.get("version", ""), b.get("resource", ""),
                                                             b.get("ns", ""), b.get("name", ""), b.get("yaml", "")))
            if p == "/api/resources/delete":
                guard_smb_object(b.get("resource"), b.get("ns"), b.get("name"))
                return self._send(200, RESOURCES.delete_object(b.get("group", ""), b.get("version", ""), b.get("resource", ""),
                                                               b.get("ns", ""), b.get("name", "")))
            if p == "/api/resources/create":
                for document in re.split(r"^---[ \t]*$", b.get("yaml", ""), flags=re.M):
                    if document.strip():
                        obj = RESOURCES._parse(document)
                        meta = obj.get("metadata") or {}
                        guard_smb_object(obj.get("kind"), meta.get("namespace") or b.get("ns") or DEFAULT_NS,
                                         meta.get("name"))
                return self._send(200, RESOURCES.create_objects(b.get("yaml", ""), b.get("ns") or DEFAULT_NS))
            if p == "/api/mqtt":
                return self._send(200, MQTT.save(b))
            if p == "/api/mqtt/test":
                return self._send(200, MQTT.test(b))
            if p == "/api/ipam/subnets":
                return self._send(200, IPAM.save_subnets(b.get("subnets")))
            if p == "/api/ipam/record":
                return self._send(200, IPAM.save_record(b))
            if p == "/api/ipam/import":
                return self._send(200, IPAM.import_csv(b.get("csv", "")))
            if p == "/api/ipam/bulk":
                return self._send(200, IPAM.bulk(b.get("ips"), b.get("changes")))
            if p == "/api/ipam/scan":
                return self._send(200, IPAM.scan(b.get("subnet")))
            if p == "/api/ipam/unifi":
                return self._send(200, IPAM.save_unifi(b))
            if p == "/api/ipam/unifi/sync":
                return self._send(200, IPAM.sync_unifi())
            if p == "/api/workloads/group":
                return self._send(200, set_workload_groups(b))
            if p == "/api/scale":
                ns, name, n = b["ns"], b["name"], int(b["replicas"])
                if n < 0 or n > 100:
                    raise ValueError("replicas must be between 0 and 100")
                guard_managed_smb(ns, name)
                guard_self(ns, name, stopping=n == 0, confirmed=b.get("confirm_self") is True)
                if n > 0:
                    plan = workload_start_plan(ns, name, n)
                    if plan["blocked"]:
                        return self._send(409, {"error": "not enough eligible capacity for the requested replicas", "plan": plan})
                    if plan["requires_confirmation"] and b.get("confirm_capacity") is not True:
                        return self._send(409, {"error": "review node memory before starting", "plan": plan})
                if n:
                    clear_unstarted_pods(ns, name, stopping=False)
                ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale",
                      {"spec": {"replicas": n}}, ctype="application/merge-patch+json")
                if not n:
                    clear_unstarted_pods(ns, name, stopping=True)
                _cache.pop("wl", None)
                return self._send(200, {"ok": True})
            if p == "/api/restart":
                ns, name = b["ns"], b["name"]
                guard_managed_smb(ns, name)
                ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                      {"spec": {"template": {"metadata": {"annotations":
                       {NAMES.key("restartedAt"): time.strftime("%Y-%m-%dT%H:%M:%SZ")}}}}},
                      ctype="application/merge-patch+json")
                _cache.pop("wl", None)
                return self._send(200, {"ok": True})
            if p == "/api/image-updates/apply":
                guard_managed_smb(b.get("ns"), b.get("name"))
                enforce_update_policy(b)
                result = UPDATES.apply_update(b["ns"], b["name"])
                result["operation"] = OPS.start(
                    "image-update", f"Update {b['name']}",
                    {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
                    "/containers?" + urllib.parse.urlencode({"find": b["name"]}),
                    {"namespace": b["ns"], "name": b["name"]})
                _cache.pop("wl", None); _cache.pop("ov", None); UPDATES.invalidate()
                return self._send(200, result)
            if p == "/api/image-updates/rollback":
                guard_managed_smb(b.get("ns"), b.get("name"))
                result = UPDATES.rollback(b["ns"], b["name"])
                result["operation"] = OPS.start(
                    "image-rollback", f"Roll back {b['name']}",
                    {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
                    "/containers?" + urllib.parse.urlencode({"find": b["name"]}),
                    {"namespace": b["ns"], "name": b["name"]})
                _cache.pop("wl", None); _cache.pop("ov", None); UPDATES.invalidate()
                return self._send(200, result)
            if p == "/api/files/write":
                warning = FILES.check_syntax(b.get("path"), b.get("content"))
                if warning and not b.get("ignore_syntax"):
                    return self._send(400, {"error": warning, "syntax": True})
                return self._send(200, FILES.write_file(
                    b.get("namespace") or DEFAULT_NS, b.get("pvc"), b.get("path"), b.get("content")))
            if p == "/api/files/close":
                return self._send(200, FILES.close_session(
                    b.get("namespace") or DEFAULT_NS, b.get("pvc")))
            if p == "/api/volumes/chown":
                return self._send(200, IMP.chown_claim(
                    b.get("namespace") or DEFAULT_NS, b.get("name"), b.get("uid"), b.get("gid")))
            if p == "/api/sources/measure":
                return self._send(200, IMP.measure_source_paths(
                    b.get("name"), b.get("paths") or [], b.get("seconds", 25)))
            if p == "/api/imports/delete":
                plan = IMP.import_cleanup_plan(b.get("name"))
                made = {row["name"] for row in plan["volumes"] if row["created"]}
                # The request names claims; asking for all of them is the old
                # boolean, which an import with one volume still sends.
                wanted = [str(claim) for claim in (b.get("remove_volumes") or [])]
                if b.get("remove_volume") and not wanted:
                    wanted = [row["name"] for row in plan["volumes"] if row["created"]]
                borrowed = [claim for claim in wanted if claim not in made]
                if borrowed:
                    raise ValueError(f"this import copied into {', '.join(sorted(borrowed))} "
                                     "without creating it, so it will not delete it")
                if b.get("remove_workload") and plan["workload"]:
                    guard_managed_smb(plan["namespace"], plan["workload"])
                result = IMP.delete_import(b.get("name"))
                removed = []
                if b.get("remove_workload") and plan["workload"]:
                    ns, name = plan["namespace"], plan["workload"]
                    services = set(NETWORK.workload_service_names(ns, name)) | {name}
                    ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
                    for service in sorted(services):
                        try:
                            ksend("DELETE", f"/api/v1/namespaces/{ns}/services/{service}")
                        except urllib.error.HTTPError:
                            pass
                    removed.append(f"workload {name}")
                    # Its pod holds the same claims the caller may be deleting
                    # next, so wait for it to go rather than leaving them
                    # Terminating behind the pvc-protection finaliser.
                    IMP.wait_for_pods_gone(ns, f"app={name}")
                    _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("network", None)
                for claim in wanted:
                    ksend("DELETE", f"/api/v1/namespaces/{plan['namespace']}/"
                                    f"persistentvolumeclaims/{claim}")
                    removed.append(f"volume {claim}")
                    _cache.pop("vol", None); _cache.pop("stor", None)
                # A claim something still mounts only gets a deletion stamp, so
                # say it is releasing rather than reporting it gone.
                releasing = []
                for claim in wanted:
                    try:
                        live = kget(f"/api/v1/namespaces/{plan['namespace']}/"
                                    f"persistentvolumeclaims/{claim}")
                    except Exception:
                        continue
                    if (live.get("metadata", {}) or {}).get("deletionTimestamp"):
                        releasing.append(claim)
                result["releasing"] = releasing
                if removed:
                    result["message"] = result["message"] + " with " + " and ".join(removed)
                if result.get("releasing"):
                    result["message"] += (" — " + ", ".join(result["releasing"])
                                          + " will finish deleting once released")
                result["removed"] = removed
                return self._send(200, result)
            if p == "/api/imports/cleanup-plan":
                return self._send(200, IMP.import_cleanup_plan(b.get("name")))
            if p == "/api/network/service/delete":
                guard_managed_smb(b.get("namespace"), b.get("name"))
                result = NETWORK.delete_service(b.get("namespace"), b.get("name"), b.get("force"))
                _cache.pop("network", None)
                return self._send(200, result)
            if p == "/api/storage/classes":
                return self._send(200, create_storage_class(b))
            if p == "/api/storage/classes/default":
                return self._send(200, set_default_storage_class(b.get("name")))
            if p == "/api/storage/classes/delete":
                return self._send(200, delete_storage_class(b.get("name")))
            if p == "/api/shares":
                result = SHARES.create_share(
                    b["name"], b.get("size_gb", 10), b.get("user", "lab"),
                    b.get("password"), b.get("public", False), b.get("read_only", False),
                    b.get("pvc"), b.get("sub_path", ""), b.get("storage_class"),
                    b.get("access_mode"), b.get("new_name", ""), str(b.get("samba_ip") or "").strip())
                deployment = result.pop("deployment", None)
                if deployment:
                    result["operation"] = OPS.start(
                        "deployment", f"Create share {b['name']}",
                        {"kind": "Deployment", "name": SMB_NAME, "namespace": SMB_NAMESPACE},
                        "/shares", {"namespace": SMB_NAMESPACE, "name": SMB_NAME, "undo": "keep"},
                        "Restarting Samba with the new share")
                return self._send(200, {"ok": True, **result})
            if p == "/api/shares/edit":
                result = SHARES.edit_share(
                    b["name"], b.get("size_gb"), b.get("user", "lab"),
                    b.get("password"), b.get("public", False), b.get("read_only", False))
                deployment = result.pop("deployment", None)
                if deployment:
                    result["operation"] = OPS.start(
                        "deployment", f"Update share {b['name']}",
                        {"kind": "Deployment", "name": SMB_NAME, "namespace": SMB_NAMESPACE},
                        "/shares", {"namespace": SMB_NAMESPACE, "name": SMB_NAME, "undo": "keep"},
                        "Restarting Samba with updated access")
                return self._send(200, {"ok": True, **result})
            if p == "/api/shares/delete":
                share = next((row for row in SHARES.list_shares() if row.get("name") == b["name"]), None)
                old_export = (share or {}).get("nfs_clients", "")
                if old_export:
                    set_nfs_export(b["name"], "")
                try:
                    result = SHARES.delete_share(b["name"])
                except Exception:
                    if old_export:
                        set_nfs_export(b["name"], old_export, share.get("nfs_read_only", True))
                    raise
                deployment = result.pop("deployment", None)
                if deployment:
                    result["operation"] = OPS.start(
                        "deployment", f"Remove share {b['name']}",
                        {"kind": "Deployment", "name": SMB_NAME, "namespace": SMB_NAMESPACE},
                        "/shares", {"namespace": SMB_NAMESPACE, "name": SMB_NAME, "undo": "keep"},
                        "Restarting Samba without the removed share")
                return self._send(200, {"ok": True, **result})
            if p == "/api/shares/nfs":
                return self._send(200, set_nfs_export(
                    b.get("name", ""), str(b.get("clients") or "").strip(),
                    b.get("read_only") is not False))
            if p == "/api/appstore/install":
                cfg = template_to_cfg(b["app"])
                cfg.update(b.get("overrides") or {})
                cfg = analyze_deploy_intent(cfg)
                guard_managed_smb(cfg.get("namespace") or DEFAULT_NS,
                                  cfg.get("workload_name") or cfg.get("name"))
                cfg = ensure_profile_compatible(cfg)
                persist_icon_config(cfg)
                cfg = NETWORK.prepare_deploy(cfg)
                cfg = apply_deploy_bindings(cfg)
                cfg = apply_generated_secrets(cfg)
                cfg = prepare_lan(cfg)
                cfg = VOLOWNER.prepare(cfg)
                dep, svc = build_deployment(cfg)
                ns = dep["metadata"]["namespace"]
                if cfg.get("network_mode") == "lan":
                    LAN.ensure_nad(ns, dep["metadata"]["name"], cfg["lan"])
                reused = []
                for volume in cfg.get("volumes") or []:
                    if volume.get("type") == "pvc" and volume.get("create"):
                        if ensure_claim(ns, _dns_name(volume.get("source"), "volume name"),
                                        volume.get("size_gb", 5),
                                        volume.get("storage_class") or STORAGE_CLASS,
                                        volume.get("access_mode") or "ReadWriteOnce"):
                            reused.append(volume.get("source"))
                ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
                if svc:
                    ksend("POST", f"/api/v1/namespaces/{ns}/services", svc)
                _cache.pop("wl", None)
                op = OPS.start("deployment", f"Install {cfg['name']}",
                               {"kind": "Deployment", "name": cfg["name"], "namespace": ns},
                               "/containers", {"namespace": ns, "name": cfg["name"], "undo": "delete"})
                return self._send(200, {"ok": True, "name": cfg["name"], "operation": op, "reused_volumes": reused,
                                        **({"detail": f"kept the existing {', '.join(reused)} - nothing was using "
                                                      f"{'it' if len(reused) == 1 else 'them'}, so its data carries on"}
                                           if reused else {})})
            if p == "/api/edit":
                guard_managed_smb(b.get("ns", ""), b.get("name", ""))
                persist_icon_config(b)
                # Paths moved to other storage bring their data: the edit is
                # saved stopped and a job copies before it starts again.
                moves = RESTRUCTURE.copies(b)
                guard_self(b.get("ns", ""), b.get("name", ""),
                           stopping=("autostart" in b and not b["autostart"]) or
                                    ("replicas" in b and int(b.get("replicas") or 0) == 0),
                           confirmed=b.get("confirm_self") is True,
                           renaming=bool(b.get("workload_name")) and b.get("workload_name") != b.get("name"),
                           moving=bool(moves))
                result = LC.edit_workload(b, hold=bool(moves))
                if moves:
                    result["operation"] = OPS.start(
                        "restructure", f"Move {b['name']}'s data",
                        {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]}, "/containers",
                        {"namespace": b["ns"], "name": b["name"], "moves": moves,
                         "replicas": result.get("held_replicas", 0), "phase": "stopping"},
                        f"Stopping {b['name']} to copy {len(moves)} location{'s' if len(moves) != 1 else ''}")
                if "lan" in b:
                    lan_message = edit_lan(b["ns"], result.get("name") or b["name"], b.get("lan"))
                    if lan_message:
                        result["lan"] = lan_message
                        if b.get("lan"):
                            b["network_mode"] = "lan"
                ports = [port for container in b.get("containers") or []
                         for port in container.get("ports") or []]
                # manage_ports marks a client that owns the whole port list, so
                # removing the last port removes the Service too. Older clients
                # are recognised by a port carrying expose, and a body with no
                # ports at all from one of those is left alone.
                if b.get("manage_ports") or any("expose" in port for port in ports):
                    message = NETWORK.sync_workload_ports(
                        b["ns"], result.get("name") or b["name"], ports,
                        network_mode=b.get("network_mode"))
                    if message:
                        result["network"] = message
                        _cache.pop("network", None)
                return self._send(200, result)
            if p == "/api/move":
                guard_managed_smb(b.get("ns"), b.get("name"))
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
                    "/containers", {"namespace": b["ns"], "name": b["name"], "undo": "rollout"})
                return self._send(200, result)
            if p == "/api/node/cordon":
                return self._send(200, LC.set_cordon(b["node"], b.get("cordon", True)))
            if p == "/api/node/hardware":
                return self._send(200, set_node_hardware(b))
            if p == "/api/move/clusters/add":
                return self._send(200, MOVE.add_cluster(
                    b.get("name"), b.get("url"), b.get("user"), b.get("password")))
            if p == "/api/move/clusters/remove":
                return self._send(200, MOVE.remove_cluster(b.get("name")))
            if p == "/api/move/remote":
                return self._send(200, MOVE.remote_inventory(b.get("name")))
            if p == "/api/move/clusters/check":
                return self._move(lambda: MOVE.check_cluster(b.get("name")))
            if p == "/api/move/clusters/readiness":
                return self._move(lambda: MOVE.readiness(b.get("name")))
            if p == "/api/move/clusters/storage":
                return self._move(lambda: MOVE.setup_storage(b.get("name"), b.get("size_gb") or 100, b.get("lb_ip") or ""))
            if p == "/api/cluster/remove-node":
                return self._move(lambda: ONBOARD.remove_node(b.get("node"), bool(b.get("accept_loss")),
                                                              bool(b.get("gone"))))
            if p == "/api/cluster/cleanup/run":
                return self._move(lambda: ONBOARD.cleanup(b.get("kind"), b.get("name") or "", bool(b.get("force"))))
            if p == "/api/move/source":
                action, kind, name = b.get("action"), b.get("kind"), b.get("name")
                actions = {"quiesce": lambda: MOVE_SOURCE.quiesce(kind, name),
                           "backup": lambda: MOVE_SOURCE.backup(kind, name),
                           "release": lambda: MOVE_SOURCE.release(kind, name),
                           "remove": lambda: MOVE_SOURCE.remove(kind, name,
                                                                bool(b.get("volumes")))}
                if action not in actions:
                    return self._send(400, {"error": "unknown move action"})
                return self._move(actions[action])
            if p in ("/api/move/plan", "/api/move/start"):
                if p.endswith("/start") and (b.get("kind") or "container") == "container":
                    guard_managed_smb(b.get("namespace") or DEFAULT_NS, b.get("name"))
                call = MOVE_ENGINE.plan if p.endswith("plan") else MOVE_ENGINE.start
                return self._move(lambda: call(
                    b.get("cluster"), b.get("kind") or "container", b.get("name"),
                    b.get("namespace") or DEFAULT_NS, b.get("address_mode") or "shared",
                    b.get("address") or ""))
            if p == "/api/move/moves/retry":
                return self._move(lambda: MOVE_ENGINE.retry(b.get("id")))
            if p == "/api/move/moves/abandon":
                return self._move(lambda: MOVE_ENGINE.abandon(b.get("id")))
            if p == "/api/move/moves/dismiss":
                return self._send(200, MOVE_ENGINE.dismiss(b.get("id") or None))
            if p == "/api/move/moves/finish":
                return self._move(lambda: MOVE_ENGINE.finish(b.get("id"), bool(b.get("volumes"))))
            if p == "/api/objectstore/deploy":
                return self._send(200, OBJECTS.deploy(b))
            if p == "/api/objectstore/longhorn":
                return self._send(200, OBJECTS.point_longhorn(replace=bool(b.get("replace", True))))
            if p == "/api/objectstore/remove":
                return self._send(200, OBJECTS.remove(bool(b.get("keep_data", True))))
            if p == "/api/node/probe/install":
                return self._send(200, PROBE.install(HOMESTEAD_VERSION))
            if p == "/api/node/probe/remove":
                return self._send(200, PROBE.remove())
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
            if p == "/api/hardware/rescan":
                return self._send(200, {"ok": True, "nodes": reconcile_hardware(fresh=True)})
            if p == "/api/node/drain":
                impact = PLACE.impact(b["node"])
                if impact["stranded"] and not b.get("allow_stranded"):
                    return self._send(409, {"error": "some workloads have no eligible failover host",
                                            "impact": impact})
                return self._send(200, LC.drain(b["node"], b.get("grace", 30), b.get("system", False)))
            if p == "/api/node/power":
                if b.get("confirm") != b.get("node"):
                    return self._send(400, {"error": "confirmation must repeat the node name"})
                power_plan = POWER.plan(b["node"], b["action"])
                if not power_plan["ready"]:
                    return self._send(409, {"error": "; ".join(power_plan["blockers"]), "plan": power_plan})
                if b.get("review_token") != power_plan["review_token"]:
                    return self._send(409, {"error": "host impact changed; review the plan again", "plan": power_plan})
                if power_plan["stranded"] and not b.get("allow_stranded"):
                    return self._send(409, {"error": "some workloads have no eligible failover host",
                                            "plan": power_plan})
                if power_plan["requires_data_ack"] and not b.get("allow_data_risk"):
                    return self._send(409, {"error": "acknowledge the volume risk before host power control",
                                            "plan": power_plan})
                try:
                    result = LC.node_power(b["node"], b["action"], True)
                    result["operation"] = OPS.start(
                        "node-power", f"{b['action']} {b['node']}", {"kind": "Node", "name": b["node"]},
                        "/nodes?node=" + urllib.parse.quote(b["node"]),
                        {"node": b["node"], "action": b["action"], "boot_id": power_plan["boot_id"],
                         "volumes": [v["name"] for v in power_plan["volumes"]],
                         "helper_pod": result.get("helper_pod", ""), "started_epoch": time.time()},
                        "Host cordoned and drained; waiting for its power transition")
                    return self._send(200, result)
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
                _cache.pop("vms", None)
                return self._send(200, VMS.power(b.get("ns", DEFAULT_NS), b.get("name", ""), b.get("action", "")))
            if p == "/api/vm/edit":
                _cache.pop("vms", None)
                return self._send(200, VMS.edit(b.get("ns", DEFAULT_NS), b.get("name", ""), b))
            if p == "/api/vm/delete":
                _cache.pop("vms", None)
                return self._send(200, VMS.delete(b.get("ns", DEFAULT_NS), b.get("name", ""), bool(b.get("disks"))))
            if p == "/api/disks/retire/plan":
                return self._send(200, DISKS.retire_plan(b.get("node", ""), b.get("disk", "")))
            if p == "/api/disks/retire":
                op = DISKS.retire_start(b, OPS)
                for key in ("disks", "lhcap", "nodes", "ov"):
                    _cache.pop(key, None)
                return self._send(200, {"ok": True, "operation": op})
            if p in ("/api/disks/add", "/api/disks/scheduling", "/api/disks/evict", "/api/disks/remove"):
                action = p.rsplit("/", 1)[1]
                result = (DISKS.add(b) if action == "add"
                          else DISKS.set_scheduling(b.get("node", ""), b.get("disk", ""), b.get("allow", True)) if action == "scheduling"
                          else DISKS.evict(b.get("node", ""), b.get("disk", ""), b.get("on", True)) if action == "evict"
                          else DISKS.remove(b.get("node", ""), b.get("disk", "")))
                for key in ("disks", "lhcap", "nodes", "ov"):
                    _cache.pop(key, None)
                return self._send(200, result)
            if p in ("/api/disks/tags", "/api/disks/node-tags"):
                result = (DISKS.set_disk_tags(b.get("node", ""), b.get("disk", ""), b.get("tags") or [])
                          if p.endswith("/tags") and not p.endswith("node-tags")
                          else DISKS.set_node_tags(b.get("node", ""), b.get("tags") or []))
                _cache.pop("disks", None)
                return self._send(200, result)
            if p == "/api/longhorn/settings":
                _cache.pop("lhcap", None)
                return self._send(200, LHCAP.save(b))
            if p == "/api/vm/k3s-cluster/plan":
                return self._send(200, K3SC.review(b))
            if p == "/api/vm/k3s-cluster":
                return self._send(200, {"ok": True, "operation": K3SC.start(b, OPS)})
            if p == "/api/vm/create":
                _cache.pop("vms", None)
                return self._send(200, create_vm_with_address(b))
            if p == "/api/vm-disks/import":
                result = IMP.import_vm_disk(b)
                result["operation"] = OPS.start(
                    "vm-disk-import", f"Import VM disk {result['name']}",
                    {"kind": "DataVolume", "name": result["name"],
                     "namespace": result["namespace"]},
                    "/import", {"namespace": result["namespace"], "name": result["name"]})
                return self._send(200, result)
            if p == "/api/images/prepull/stop":
                return self._send(200, IMP.stop_prepull(b.get("name")))
            if p == "/api/images/prepull":
                result = IMP.prepull(b["image"], b.get("nodes"))
                result["operation"] = OPS.start(
                    "image-pull", f"Pull {b['image']}",
                    {"kind": "Image", "name": b["image"], "namespace": DEFAULT_NS},
                    "/image-cache", {"namespace": DEFAULT_NS, "name": result["daemonset"]})
                return self._send(200, result)
            if p == "/api/images/scan":
                return self._send(200, IMP.start_image_scan())
            if p == "/api/images/vm/delete":
                _cache.pop("vmimages", None)
                return self._send(200, IMP.delete_vm_image(b.get("namespace", ""), b.get("name", ""), b.get("confirm", "")))
            if p == "/api/images/forget-rollback":
                return self._send(200, IMP.forget_rollback(b.get("namespace") or DEFAULT_NS, b.get("name", "")))
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
            if p == "/api/self/samba":
                return self._send(200, set_samba(bool(b.get("enabled")), str(b.get("address") or "").strip()))
            if p == "/api/self/nfs":
                return self._send(200, set_nfs(bool(b.get("enabled")), str(b.get("address") or "").strip()))
            if p == "/api/addons/smb/remove":
                if b.get("confirm") != SMB_NAME:
                    raise ValueError(f"type {SMB_NAME} to remove the SMB server")
                return self._send(200, remove_samba())
            if p == "/api/addons/nfs/remove":
                if b.get("confirm") != NFS.NAME:
                    raise ValueError(f"type {NFS.NAME} to remove the NFS server")
                return self._send(200, remove_nfs())
            if p == "/api/shares/repair":
                return self._send(200, repair_samba(str(b.get("address") or "").strip()))
            if p == "/api/network/vips/add":
                _cache.pop("network", None)
                return self._send(200, NETWORK.add_vips(b, IPAM.load()[0].get("records") or {}))
            if p == "/api/network/vips/remove":
                _cache.pop("network", None)
                return self._send(200, NETWORK.remove_vip(b.get("ip", "")))
            if p == "/api/network/vips/label":
                _cache.pop("network", None)
                return self._send(200, NETWORK.set_vip_label(b.get("ip", ""), b.get("label", "")))
            if p == "/api/network/vm-networks":
                _cache.pop("network", None)
                return self._send(200, NETWORK.create_vm_network(dict(b, _probes=node_temps())))
            if p == "/api/storage/classes/cleanup":
                removed = cleanup_restore_classes()
                return self._send(200, {"ok": True, "removed": removed,
                                        "detail": f"removed {len(removed)} class{'es' if len(removed) != 1 else ''} left by restores"
                                                  if removed else "nothing to remove: every restore class is still in use"})
            if p == "/api/workloads/failover":
                for item in b.get("items") or []:
                    guard_managed_smb(item.get("ns"), item.get("name"))
                result = FAILOVER.set_many(b.get("items") or [])
                _cache.pop("wl", None)
                return self._send(200, result)
            if p == "/api/workload/primary-port":
                ns, name = b.get("ns") or DEFAULT_NS, _dns_name(b.get("name"), "workload name")
                guard_managed_smb(ns, name)
                port = int(b.get("port") or 0)
                ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                      {"metadata": {"annotations": {NAMES.key("primary-port"): str(port) if port else None}}},
                      ctype="application/merge-patch+json")
                _cache.pop("wl", None)
                return self._send(200, {"ok": True, "detail": f"{name} opens on port {port} first" if port
                                        else f"{name} shows its ports in their own order"})
            if p == "/api/volumes/reclass/plan":
                ns = b.get("namespace") or DEFAULT_NS
                review = RECLASS.plan(ns, b.get("claim", ""), b.get("target", ""))
                # An earlier move of this volume stopped part-way is finished
                # by carrying it on, not by starting another over the top.
                stopped = RECLASS.stopped_attempt(ns, b.get("claim", ""), OPS)
                if stopped:
                    review["stopped"] = stopped
                    review["blockers"].insert(0, f"an earlier move of {b.get('claim', '')} stopped part-way "
                                                 f"({stopped.get('message', '')[:160]}); carry that one on instead")
                    review["ok"] = False
                return self._send(200, review)
            if p == "/api/volumes/reclass/start":
                op = RECLASS.start(b.get("namespace") or DEFAULT_NS, b.get("claim", ""), b.get("target", ""), OPS)
                _cache.pop("vol", None)
                return self._send(200, {"ok": True, "operation": op})
            if p == "/api/volumes/old-copies/remove":
                _cache.pop("vol", None)
                return self._send(200, RECLASS.remove_old_copy(b.get("pv", "")))
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
            if p == "/api/lh/job/run":
                result = LH.run_job(b.get("name"))
                result["operation"] = OPS.start(
                    "protect-run", f"Run {b['name']} now", {"kind": "Job", "name": result["job"],
                                                            "namespace": result["namespace"]},
                    "/data-protection", {"namespace": result["namespace"], "name": result["job"]}, "Starting")
                return self._send(200, result)
            if p == "/api/lh/assign":
                return self._send(200, LH.bulk_assign(
                    b["volumes"], b["name"], b.get("kind", "group"), b.get("enabled", True)))
            if p == "/api/lh/snapshot":
                return self._send(200, LH.create_snapshot(b["volume"], b.get("name")))
            if p == "/api/lh/snapshot/delete":
                return self._send(200, LH.delete_snapshot(b["name"]))
            if p == "/api/lh/snapshot/revert":
                operation = REVERT.start(str(b.get("volume") or ""), str(b.get("snapshot") or ""), OPS)
                return self._send(200, {"ok": True, "operation": operation,
                                        "detail": "Rolling back: what uses it stops first, then starts again"})
            if p == "/api/lh/backup":
                result = LH.create_backup(b["volume"], b.get("name"))
                if result.get("backup"):
                    result["operation"] = OPS.start(
                        "backup", f"Back up {b['volume']}",
                        {"kind": "Volume", "name": b["volume"], "namespace": "longhorn-system"},
                        "/data-protection", {"namespace": "longhorn-system", "name": result["backup"]})
                return self._send(200, result)
            if p == "/api/lh/restore":
                plan = LH.restore_plan(
                    b.get("backup"), b.get("namespace", DEFAULT_NS), b.get("name"))
                if plan.get("conflict"):
                    return self._send(409, {"error": plan["conflict"]["message"], "plan": plan})
                result = LH.restore_backup(b)
                result["operation"] = OPS.start(
                    "volume-restore", f"Restore {result['name']}",
                    {"kind": "PersistentVolumeClaim", "name": result["name"],
                     "namespace": result["namespace"]},
                    "/volumes?" + urllib.parse.urlencode({"find": result["name"]}),
                    {"namespace": result["namespace"], "name": result["name"],
                     "backup": result["backup"]},
                    "Waiting for Longhorn to provision the restored volume")
                return self._send(200, result)
            if p == "/api/lh/target":
                return self._send(200, LH.set_backup_target(
                    b["url"], b.get("secret", ""), b.get("poll", "5m"), b.get("keys")))
            if p == "/api/lh/group":
                return self._send(200, LH.save_group(b))
            if p == "/api/lh/group/delete":
                return self._send(200, LH.delete_group(b.get("name", "")))
            if p == "/api/lh/backup/delete":
                return self._send(200, LH.delete_backup(b.get("name", "")))
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
                guard_managed_smb(DEFAULT_NS, b.get("name"))
                persist_icon_config(b)
                b = NETWORK.prepare_deploy(b)
                result = IMP.import_container(b)
                result["operation"] = OPS.start(
                    "import", f"Import {b['name']}",
                    {"kind": "Job", "name": result["job"], "namespace": DEFAULT_NS},
                    "/import", {"namespace": DEFAULT_NS, "name": result["job"]})
                return self._send(200, result)
            if p == "/api/operations/resume":
                return self._send(200, OPS.resume(b.get("id", "")))
            if p == "/api/operations/cancel-plan":
                return self._send(200, OPS.cancel_plan(b.get("id", "")))
            if p == "/api/operations/cancel":
                # The route lets any operator in; what the job's own cancel
                # does - delete VMs, stop a volume move - may need more.
                try:
                    result = OPS.cancel(b.get("id", ""), b.get("options") or {}, b.get("confirm", ""),
                                        allowed=lambda need: AUTH.allows(self.role, need), by=self.user)
                except PermissionError as error:
                    return self._send(403, {"error": str(error), "role": self.role})
                for key in ("wl", "ov", "network", "vms", "vol", "helm", "disks", "lhcap", "flow2"):
                    _cache.pop(key, None)
                return self._send(200, result)
            if p == "/api/operations/dismiss":
                if b.get("all"):
                    return self._send(200, OPS.dismiss_finished())
                return self._send(200, OPS.dismiss(b["id"]))
            if p == "/api/network/plan":
                return self._send(200, NETWORK.service_plan(b))
            if p == "/api/network/services":
                guard_managed_smb(b.get("namespace") or DEFAULT_NS, b.get("name"))
                result = NETWORK.create_service(b)
                _cache.pop("network", None); _cache.pop("flow2", None)
                result["operation"] = OPS.start(
                    "network-service", f"Expose {result['name']}",
                    {"kind": "Service", "name": result["name"], "namespace": result["namespace"]},
                    "/networking", {"namespace": result["namespace"], "name": result["name"]},
                    "Waiting for the Service address and endpoints")
                return self._send(200, result)
            if p == "/api/preview":
                b = analyze_deploy_intent(b)
                current = None
                context = None
                if b.get("target_mode") == "existing":
                    ns = _dns_name(b.get("namespace") or DEFAULT_NS, "namespace")
                    target = _dns_name(b.get("target_workload"), "existing workload")
                    current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
                    context = rollout_review_context(current)
                capacity = deploy_capacity_plan(b, existing=current)
                capacity_token = CAPACITY_REVIEW.issue(b, context)
                b = NETWORK.prepare_deploy(b)
                b = apply_deploy_bindings(b)
                b = apply_generated_secrets(b)
                if b.get("target_mode") == "existing":
                    ns = b.get("namespace") or DEFAULT_NS
                    target = _dns_name(b.get("target_workload"), "existing workload")
                    dep, svc = build_sidecar_deployment(b, current)
                    return self._send(200, {"deployment": redact_deployment_preview(dep, b), "service": svc,
                                            "capacity": capacity, "capacity_token": capacity_token,
                                            "app_profile": b.get("app_profile"),
                                            "impact": {"mode": "existing", "workload": target,
                                                       "containers": [c.get("name") for c in current.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])],
                                                       "message": "Saving updates the Deployment template and restarts every container in its pods."}})
                dep, svc = build_deployment(b)
                return self._send(200, {"deployment": redact_deployment_preview(dep, b), "service": svc,
                                        "capacity": capacity, "capacity_token": capacity_token,
                                        "app_profile": b.get("app_profile"),
                                        "impact": {"mode": "new", "workload": dep["metadata"]["name"],
                                                   "message": "Creates a new independently managed Deployment."}})
            return self._send(404, {"error": "no route"})
        except CAPACITY_REVIEW.Rejected as e:
            return self._send(409, {"error": str(e), "capacity": e.plan, "review_required": True})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except AUTH.StoreUnavailable as e:
            # Not an empty account store: the cluster did not answer.
            return self._send(503, {"error": str(e), "unavailable": True})
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
                return self._send(200, {"ok": True, "services": delete_workload(parts[2], parts[3])})
            return self._send(404, {"error": "no route"})
        except AUTH.StoreUnavailable as e:
            # Not an empty account store: the cluster did not answer.
            return self._send(503, {"error": str(e), "unavailable": True})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": e.read().decode("utf-8", "replace")[:500]})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def _reconcile_permissions():
    """Bring Homestead's own ClusterRole up to this release, before anything needs it."""
    try:
        result = SELF.reconcile()
    except Exception as error:
        print(f"permissions: not checked ({str(error)[:120]})", flush=True)
        return
    print(f"permissions: {result['state']} - {result['detail']}", flush=True)
    try:
        adopted = SELF.adopt_old_keys()
        if adopted.get("changed"):
            print(f"moved {adopted['changed']} objects' keys to {NAMES.DOMAIN}", flush=True)
    except Exception as error:
        print(f"old keys: not moved ({str(error)[:120]})", flush=True)


def logs_refusal(error, pod, container):
    """What Kubernetes' refusal to give logs means, in words rather than its JSON."""
    try:
        message = json.loads(error.read().decode("utf-8", "replace")).get("message", "")
    except Exception:
        message = ""
    who = f"{container} in {pod}" if container else pod
    if "waiting to start" in message or "ContainerCreating" in message or "PodInitializing" in message:
        return f"{who} has not started yet, so there are no logs. It shows here once it is running."
    if "not found" in message and "pods" in message:
        return f"{pod} no longer exists; the workload has probably replaced it. Reopen the logs."
    if "terminated" in message:
        return f"{who} has stopped and Kubernetes kept no logs from it."
    return f"Kubernetes could not return the logs for {who}" + (f": {message}" if message else ".")


def _upgrade_node_probe():
    """Finish the upgrade the image cannot finish by itself.

    Deliberately not fatal and deliberately quiet: a cluster where the probe
    is absent, or where Homestead lacks the rights to touch it, is a cluster
    that simply has no probe - not a reason to refuse to start.
    """
    try:
        result = PROBE.reconcile(HOMESTEAD_VERSION)
    except Exception as error:
        print(f"node probe: not updated ({str(error)[:120]})", flush=True)
        return
    if result["state"] in ("updated", "error"):
        print(f"node probe: {result['detail']}", flush=True)


def _samba_loop():
    """One leader keeps SMB's mounts and image aligned with saved settings."""
    while True:
        if LEADER.is_leader():
            try:
                state = samba_state()
                if state.get("name") == "samba" and state.get("installed"):
                    install_samba()
                result = SHARES.reconcile_samba(SAMBA_IMAGE)
                beat("samba", 60, leader_only=True)
                if result.get("state") == "repaired":
                    print("network shares: restored SMB settings from the share inventory", flush=True)
            except Exception as error:
                beat("samba", 60, error, leader_only=True)
                print(f"network shares: {str(error)[:180]}", flush=True)
            try:
                nfs_result = reconcile_nfs()
                if nfs_result.get("state") == "updated":
                    print("network shares: restored NFS exports from the share inventory", flush=True)
            except Exception as error:
                print(f"NFS exports: {str(error)[:180]}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=_sampler, daemon=True).start()
    threading.Thread(target=_reconcile_permissions, daemon=True).start()
    threading.Thread(target=_upgrade_node_probe, daemon=True).start()
    # Which replica leads: the one that raises alerts and advances moves.
    threading.Thread(target=LEADER.run, daemon=True).start()
    # Moves carry on across restarts: their state is on disk, and this resumes it.
    threading.Thread(target=_moves_loop, daemon=True).start()
    # Join plans from 2.8.68-2.8.164 each kept a join token in a Secret.
    threading.Thread(target=ONBOARD.tidy_old_plans, daemon=True).start()
    threading.Thread(target=_alerts_loop, daemon=True).start()
    threading.Thread(target=MQTT.run, daemon=True).start()
    threading.Thread(target=_history_loop, daemon=True).start()
    threading.Thread(target=fit_own_strategy, daemon=True).start()
    threading.Thread(target=_hardware_loop, daemon=True).start()
    threading.Thread(target=_samba_loop, daemon=True).start()
    # On a rolling update or a drain, hand the lease over now rather than
    # leaving the others to wait out its expiry.
    signal.signal(signal.SIGTERM, lambda *_: (LEADER.release(), os._exit(0)))
    print(f"Homestead listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
