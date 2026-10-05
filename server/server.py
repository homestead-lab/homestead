#!/usr/bin/env python3
"""
Homestead - a friendly homelab control plane for Harvester, Rancher and Longhorn.
Pure Python stdlib: no pip install at runtime, so it starts even with no internet.
"""
import copy, html, json, os, re, secrets, signal, ssl, sys, time, threading, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import homestead_http as HTTP
import homestead_api_errors as API_ERRORS
import homestead_route_policy as ROUTE_POLICY
from contextlib import nullcontext
from functools import wraps

# Imported ahead of the feature modules because settings are read during start.
import homestead_names as NAMES
import homestead_memory as MEMORY
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_vm_capacity as VM_CAPACITY
import homestead_vm_claims as VM_CLAIMS
import homestead_vm_profiles as VM_PROFILES
import homestead_vm_power_job as VM_POWER_JOB
import homestead_vm_power_recovery as VM_POWER_RECOVERY
import homestead_vm_mutation_job as VM_MUTATION_JOB
import homestead_vm_mutation_recovery as VM_MUTATION_RECOVERY
import homestead_vm_batch as VM_BATCH
import homestead_batch_capacity as BATCH_CAPACITY
import homestead_volume_usage as VOLUME_USAGE
import homestead_snapshot_delete as SNAPSHOT_DELETE
import homestead_allocation_probe as ALLOCATION_PROBE
import homestead_allocation_capacity as ALLOCATION_CAPACITY
import homestead_allocation_evidence as ALLOCATION_EVIDENCE
import homestead_rename as RENAME
import homestead_copy_job as COPY_JOB
import homestead_import_job as IMPORT_JOB
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_operations as OPS
import homestead_cluster_shutdown as CLUSTER_SHUTDOWN
import homestead_diagnostics as DIAGNOSTICS
import homestead_storage_guard as STORAGE_GUARD
import homestead_storage_resize as STORAGE_RESIZE
import homestead_self_data_fence as SELF_DATA_FENCE
import homestead_self_data_worker as SELF_DATA_WORKER
import homestead_self_data_review as SELF_DATA_REVIEW
import homestead_self_data_prepare as SELF_DATA_PREPARE
import homestead_self_data_execute as SELF_DATA_EXECUTE
import homestead_self_data_route as SELF_DATA_ROUTE
import homestead_self_data_finish as SELF_DATA_FINISH

def api_origin(environ=os.environ):
    """Where the Kubernetes API answers: the address the kubelet gives every
    pod, not kubernetes.default.svc. The name needs CoreDNS, and k3s runs one
    CoreDNS replica: while its host was down, every lookup failed and
    Homestead lost the API although the API itself was up. The API's
    certificate names the Service address too."""
    host, port = environ.get("KUBERNETES_SERVICE_HOST", ""), environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        return "https://kubernetes.default.svc"
    return f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"


SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = api_origin()
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
HOMESTEAD_VERSION = os.environ.get("HOMESTEAD_VERSION", "2.8.314-dev.1")
_self_data_fence = None
_self_data_barrier = None
_self_data_boot_pending = False
_self_data_boot_failed = False

DEFAULT_APP_SETTINGS = {
    "thresholds": {
        "cpu": {"warning": 70, "critical": 88},
        "memory": {"warning": 70, "critical": 88},
        "disk": {"warning": 75, "critical": 90},
        "temperature": {"warning": 70, "critical": 85},
    },
    "updates": {
        "channel": "prod",
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
    # Rebuild missing copies of detached volumes. Longhorn's own setting where
    # it has one; this drives Homestead's stand-in where it has not.
    "longhorn": {"offline_rebuilding": True},
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
        started = time.monotonic()
        try:
            o = get_overview()
            if LEADER.is_leader():
                HISTORY.record_live(o)
            # Each VM's disk traffic is a count, and a rate needs two readings.
            try:
                VMUSAGE.sample()
            except Exception:
                pass
            beat("sampler", 30, leader_only=True)
        except Exception as error:
            beat("sampler", 30, error, leader_only=True)
        time.sleep(max(1, 30 - (time.monotonic() - started)))


# A dot segment, plain or percent-encoded, anywhere in a path.
_DOT_SEGMENT = re.compile(r"(^|/)(\.|%2e){1,2}(/|$)", re.I)


def api_path(path):
    """A Kubernetes API path, refused if a name in it could step out of the
    object it names. Names come from requests - ?name=, a body - and the API
    server is asked as Homestead, so "../" in one must never reach it."""
    head = str(path).split("?", 1)[0]
    if (_DOT_SEGMENT.search(head) or "%2f" in head.lower() or "%5c" in head.lower()
            or any(c in head for c in ("\\", " ", "\t", "\r", "\n", "#"))):
        raise ValueError("that is not a valid Kubernetes name")
    return path


KGET_RETRY = (0.5, 1.0, 2.0)  # a busy API server (priority and fairness) answers reads 429 for a moment


def kget(path, timeout=10):
    for attempt in range(len(KGET_RETRY) + 1):
        req = urllib.request.Request(API + api_path(path), headers={"Authorization": f"Bearer {TOKEN}"})
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as error:
            # A read is safe to ask again; a write's 429 (an eviction a
            # disruption budget refuses) is an answer, and ksend keeps it.
            if error.code != 429 or attempt == len(KGET_RETRY):
                raise
            try:
                delay = float((error.headers or {}).get("Retry-After") or KGET_RETRY[attempt])
            except (TypeError, ValueError):
                delay = KGET_RETRY[attempt]
            error.close()
            time.sleep(max(0.1, min(3.0, delay)))


def ksend(method, path, body=None, ctype="application/json", timeout=15):
    return _guarded_ksend(method, path, body, ctype, timeout)


def _guarded_ksend(method, path, body=None, ctype="application/json", timeout=15, *, shutdown_bypass=False):
    with self_data_activity():
        require_self_data_write()
        authorize_workload_write(method, path, body)
        return STORAGE_GUARD.send(method, path, body,
                                  lambda: _ksend(method, path, body, ctype, timeout, shutdown_bypass=shutdown_bypass), OPS, kget,
                                  own_controller=(SELF.NS, NAMES.BRAND))


def _auth_ksend(method, path, body=None, ctype="application/json", timeout=15):
    # Only AUTH receives this binding. Login rate limits and session revocation
    # must remain durable while workloads are fenced for shutdown/recovery.
    base = f"/api/v1/namespaces/{AUTH.NS}/secrets"
    name = AUTH.SECRET_NAME()
    metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
    if (method not in ("POST", "PUT") or path != (base if method == "POST" else base + "/" + name)
            or not isinstance(body, dict) or body.get("apiVersion") != "v1" or body.get("kind") != "Secret"
            or body.get("type") != "Opaque" or not isinstance(metadata, dict)
            or metadata.get("name") != name or metadata.get("namespace") != AUTH.NS
            or not isinstance(body.get("data"), dict) or set(body["data"]) != {"store.json"}
            or "stringData" in body or ctype != "application/json"):
        raise ValueError("Authentication transport only writes the account Secret")
    return _guarded_ksend(method, path, body, ctype, timeout, shutdown_bypass=True)


def authorize_workload_write(method, path, body):
    if HOSTACCESS.role() in (None, "admin"):
        return
    match = re.fullmatch(r"(/(?:api/v1|apis/apps/v1|apis/batch/v1)/namespaces/([^/]+)/(deployments|statefulsets|daemonsets|pods|jobs))(?:/([^/?]+)(?:/[^?]+)?)?", path.split("?", 1)[0])
    if not match:
        return
    base, ns, _, name = match.groups()
    if name:
        HOSTACCESS.require_target(kget(base + "/" + name), ns)
    if isinstance(body, dict) and (body.get("spec", {}).get("template") or method == "POST"):
        HOSTACCESS.require_target(body, ns)


def require_workload_target(ns, name):
    if HOSTACCESS.role() not in (None, "admin"):
        HOSTACCESS.require_target(kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"), ns)


def _ksend(method, path, body=None, ctype="application/json", timeout=15, *, shutdown_bypass=False):
    path = api_path(path)
    # Fence the common Kubernetes transport, including callers that already
    # own another feature's write lock. Local navigation and recovery reviews
    # remain available. The shutdown coordinator and the narrowly scoped AUTH
    # binding bypass this fence; authentication still uses the other guards.
    if not shutdown_bypass and "SELF" in globals():
        shutdown = cluster_shutdown().state()
        if shutdown and shutdown["phase"] not in ("released", "failed"):
            raise ValueError("Cluster shutdown is active; open Cluster → Shut down cluster for progress or recovery")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": ctype})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def require_self_data_write():
    if _self_data_fence is not None:
        _self_data_fence.require_write()


def self_data_activity():
    return _self_data_barrier.activity() if _self_data_barrier is not None else nullcontext()


def self_data_request(method):
    @wraps(method)
    def guarded(handler):
        path = urllib.parse.urlparse(handler.path).path
        # Destination's explicitly read-only boot routes have their own guard;
        # they must not create activity/feature lock files on the copied data.
        progress = getattr(handler, "command", "") == "GET" and re.fullmatch(r"/api/self/data/handoff/[a-f0-9]{24}(?:/view)?", path)
        if _self_data_boot_pending or not path.startswith("/api/") or progress or path in ("/api/auth/state", "/api/self/data/abandon"):
            return method(handler)
        try:
            with self_data_activity():
                return method(handler)
        except SELF_DATA_FENCE.Held as error:
            handler._extra_headers = []
            return handler._send(503, {"error": str(error), "data_handoff": True})
    return guarded


def self_data_file_scope(path):
    root, candidate = os.path.realpath(DATA_DIR), os.path.realpath(path)
    try:
        within = os.path.commonpath((root, candidate)) == root
    except ValueError:
        within = False
    if within:
        # Also preserve the standalone check used by test/demo integrations
        # which bind a fence without creating a Linux activity barrier.
        require_self_data_write()
        return self_data_activity()
    return nullcontext()


def self_data_file_write(path):
    if _self_data_fence is None:
        return
    root, candidate = os.path.realpath(DATA_DIR), os.path.realpath(path)
    try:
        within = os.path.commonpath((root, candidate)) == root
    except ValueError:
        within = False
    if within:
        require_self_data_write()


def initialize_self_data_fence():
    """Called before feature bindings can write defaults or start background jobs."""
    global _self_data_fence, _self_data_boot_pending, _self_data_barrier
    if not TOKEN:  # local demo/test server has no cluster or persistent handoff
        return
    with open(f"{SA}/namespace", encoding="utf-8") as handle:
        namespace = handle.read().strip()
    _self_data_fence = SELF_DATA_FENCE.Fence(kget, namespace, NAMES.BRAND,
        os.environ.get("HOSTNAME", ""), NAMES.BRAND, DATA_DIR)
    state = _self_data_fence.inspect()  # unknown/source state must not reach feature imports
    _self_data_boot_pending = not state["writable"]
    _self_data_barrier = SELF_DATA_FENCE.WriteBarrier(DATA_DIR, require_self_data_write)


def self_data_boot_status():
    """The full app has loaded, but destination writers wait for verified cutover.

    Do not run resolvers or acquire shared file locks here: both may write the
    copied store. Read the journal and existing account key before readiness.
    """
    if not _self_data_boot_pending:
        return {"writable": True}
    try:
        if _self_data_boot_failed:
            raise SELF_DATA_FENCE.Held("Destination background activation needs review")
        state = _self_data_fence.inspect()
        if state.get("mode") not in ("start", "done", "recovery"):
            raise SELF_DATA_FENCE.Held("Destination startup state changed")
        OPS._read()
        AUTH.review_signing_key()
        return state
    except Exception:
        raise SELF_DATA_FENCE.Held("Homestead cannot verify its new data volume yet. Changes and background jobs remain held") from None


OPS.WRITE_GUARD = require_self_data_write
# Set before later feature binds, some of which create persistent defaults.
import homestead_shared as SELF_DATA_SHARED
SELF_DATA_SHARED.WRITE_GUARD = self_data_file_write
SELF_DATA_SHARED.WRITE_SCOPE = self_data_file_scope
if __name__ == "__main__":
    initialize_self_data_fence()


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
    channel = update_in.get("channel", out["updates"]["channel"])
    if channel not in ("prod", "dev"):
        raise ValueError("update channel must be prod or dev")
    out["updates"]["channel"] = channel
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
    rebuild = ((value or {}).get("longhorn") or {}).get("offline_rebuilding", True)
    if not isinstance(rebuild, bool):
        raise ValueError("offline rebuilding must be true or false")
    out["longhorn"] = {"offline_rebuilding": rebuild}
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
    UPDATES.invalidate()
    # A different catalogue source is a different catalogue.
    for key in [k for k in _cache if k.startswith("appstore")]:
        _cache.pop(key, None)
    # A renamed site is carried to the linked clusters, for their switches.
    threading.Thread(target=_fleet_rename, daemon=True).start()
    return settings


def _fleet_rename():
    try:
        FLEET.refresh_name()
    except Exception:
        pass            # the next change, or their next look, carries it


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
                raw = r.read(4 * 1024**2 + 1)
                if len(raw) > 4 * 1024**2:
                    raise ValueError("node telemetry exceeds the size limit")
                payload.update(json.loads(raw.decode()))
            # Provenance is assigned by this backend, never accepted from the
            # probe response. Consumers still verify Pod/DaemonSet ownership,
            # current host boot and sample age before trusting NUMA data.
            payload["numa_source"] = {"pod": {key: p.get("metadata", {}).get(key) for key in ("namespace", "name", "uid")},
                                      "received_at": time.time()}
        except Exception:
            payload.pop("numa_source", None)
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
                with self_data_activity():
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
        issues.append({"severity": "critical", "reason": "SMART overall-health check failed", "metric": "smart_failed", "value": 1})
    temperature = report.get("temperature_c")
    if temperature is not None:
        severity = ("critical" if float(temperature) >= cfg["temperature"]["critical"] else
                    "degraded" if float(temperature) >= cfg["temperature"]["warning"] else "")
        if severity:
            issues.append({"severity": severity,
                           "reason": f"Drive temperature is {temperature}°C", "metric": "temperature", "value": int(float(temperature) // 5)})
    reallocated = int(report.get("reallocated") or 0)
    pending = int(report.get("pending") or 0)
    uncorrectable = int(report.get("uncorrectable") or 0)
    media = int(report.get("media_errors") or 0)
    if reallocated >= cfg["reallocated_warning"]:
        issues.append({"severity": "degraded", "reason": f"{reallocated} reallocated sector{'s' if reallocated != 1 else ''}", "metric": "reallocated", "value": reallocated})
    if pending >= cfg["pending_critical"]:
        issues.append({"severity": "critical", "reason": f"{pending} pending sector{'s' if pending != 1 else ''}", "metric": "pending", "value": pending})
    if uncorrectable >= cfg["uncorrectable_critical"]:
        issues.append({"severity": "critical",
                       "reason": f"{uncorrectable} uncorrectable sector{'s' if uncorrectable != 1 else ''}", "metric": "uncorrectable", "value": uncorrectable})
    if media:
        issues.append({"severity": "critical", "reason": f"{media} NVMe media error{'s' if media != 1 else ''}", "metric": "media", "value": media})
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
                       "reason": f"Only {int(life)}% of rated life remains", "metric": "wear", "value": 100 - int(life)})
    elif life is not None and int(life) <= 25:
        issues.append({"severity": "degraded",
                       "reason": f"{int(life)}% of rated life remains", "metric": "wear", "value": 100 - int(life)})
    if spare is not None and floor is not None and int(spare) <= int(floor):
        issues.append({"severity": "critical",
                       "reason": f"spare blocks are down to {int(spare)}%, "
                                 f"at the drive's floor of {int(floor)}%", "metric": "spare_used", "value": 100 - int(spare)})
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
    for lease in leases:
        holder = str((lease.get("spec") or {}).get("holderIdentity") or "")
        if holder and lease["metadata"]["name"] == "plndr-cp-lock":
            duties.setdefault(holder, {"vips": [], "rwx": [], "control_plane_vip": False})["control_plane_vip"] = True
    # The addresses each node answers for, from the leases kube-vip keeps
    # beside each Service as well as its cluster-wide one (homestead_vips.py).
    for row in (network.get("addresses") or {}).get("addresses") or []:
        if row.get("kind") == "vip" and row.get("node") and row.get("announced"):
            note(row["node"], "vips", row["ip"])
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
    try:
        # Each host's OS, as the leader last read it (homestead_host_os.py).
        host_os = HOST_OS.report()["hosts"]
    except Exception:
        host_os = {}
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
                disk_issues.append({**issue, "disk": disk.get("name", "unknown"),
                                    "device_identity": (disk.get("smart") or {}).get("serial") or disk.get("serial") or ""})
        out.append({
            "name": name,
            "uid": n["metadata"].get("uid"),
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
            "host_os": (host_os.get(name) or {}).get("summary"),
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


def other_volumes():
    """Claims on classes that are not Longhorn - k3s's local-path, NFS and
    the like - which Longhorn's list leaves out: each with its state, and why
    one is stuck when its provisioner has said."""
    longhorn = {row["name"] for row in storage_classes() if row["provisioner"] == LONGHORN_PROVISIONER}
    try:
        reasons = {}
        for event in kget("/api/v1/events?fieldSelector=reason%3DProvisioningFailed").get("items", []):
            obj = event.get("involvedObject") or {}
            if obj.get("kind") == "PersistentVolumeClaim":
                reasons[(obj.get("namespace"), obj.get("name"))] = (event.get("message") or "")[:300]
    except Exception:
        reasons = {}
    # A claim backed by a Longhorn volume is Longhorn whatever its class says:
    # restored claims name a one-off restore class that older releases removed.
    try:
        drivers = {pv["metadata"]["name"]: ((pv.get("spec") or {}).get("csi") or {}).get("driver", "")
                   for pv in kget("/api/v1/persistentvolumes").get("items", [])}
    except Exception:
        drivers = {}
    out = []
    for pvc in kget("/api/v1/persistentvolumeclaims").get("items", []):
        meta, spec, status = pvc["metadata"], pvc.get("spec") or {}, pvc.get("status") or {}
        klass = spec.get("storageClassName") or ""
        if (klass in longhorn or meta["namespace"] in SYS_NS
                or drivers.get(spec.get("volumeName") or "") == LONGHORN_PROVISIONER):
            continue
        phase = status.get("phase", "")
        out.append({"namespace": meta["namespace"], "name": meta["name"], "volume": spec.get("volumeName") or "",
                    "storage_class": klass or "(none)",
                    "phase": phase, "access_modes": spec.get("accessModes") or [],
                    "size": (status.get("capacity") or {}).get("storage")
                            or ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
                    "reason": reasons.get((meta["namespace"], meta["name"]), "") if phase != "Bound" else ""})
    return sorted(out, key=lambda row: (row["phase"] == "Bound", row["namespace"], row["name"]))


def volume_copies():
    """Longhorn volume -> where each copy of it is: host, disk folder, and the
    disk's name as Homestead set it up (/mnt/<device>), the OS disk said so."""
    base = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
    replicas = kget(f"{base}/replicas").get("items", [])
    tags = {}
    try:
        for node in kget(f"{base}/nodes").get("items", []):
            for name, disk in ((node.get("spec") or {}).get("disks") or {}).items():
                uuid = (((node.get("status") or {}).get("diskStatus") or {}).get(name) or {}).get("diskUUID")
                if uuid:
                    tags[uuid] = disk.get("tags") or []
    except Exception:
        pass
    out = {}
    for r in replicas:
        spec, status = r.get("spec") or {}, r.get("status") or {}
        if not spec.get("volumeName"):
            continue
        path = (spec.get("diskPath") or "").rstrip("/")
        disk_tags = tags.get(spec.get("diskID"), [])
        os_disk = "os" in disk_tags or path in ("/var/lib/longhorn", "/var/lib/harvester/defaultdisk")
        label = ("OS disk" if os_disk else path.rsplit("/", 1)[-1] if path.startswith("/mnt/") else path) or "?"
        out.setdefault(spec["volumeName"], []).append({
            "node": spec.get("nodeID", ""), "path": path, "disk": label, "os": os_disk,
            "healthy": status.get("currentState") == "running" and not spec.get("failedAt"),
            "whole": LHREBUILD.whole(r),
            "state": status.get("currentState", "") or ("failed" if spec.get("failedAt") else "stopped")})
    for rows in out.values():
        rows.sort(key=lambda c: (c["node"], c["disk"]))
    return out


def get_volumes():
    try:
        vols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return []
    try:
        copies = volume_copies()
    except Exception:
        copies = {}
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
    active_claims = {}
    for volume in vols:
        status = volume.get("status", {})
        ks = status.get("kubernetesStatus", {}) or {}
        key = (ks.get("namespace", ""), ks.get("pvcName", ""))
        claim = pvcs.get(key)
        if status.get("state") == "attached" and claim and claim.get("spec", {}).get("volumeName") == volume["metadata"]["name"]:
            active_claims[key] = claim
    # Cache only telemetry, keyed by claim identities/bindings; never re-label
    # an old retained copy with the replacement claim's filesystem usage.
    usage_key = "volume-filesystems:" + repr(sorted((k, p.get("metadata", {}).get("uid"), p.get("spec", {}).get("volumeName")) for k, p in active_claims.items()))
    filesystems = cached(usage_key, 20, lambda: VOLUME_USAGE.collect(kget, active_claims)) if active_claims else {}
    # A cached observation must also expire if telemetry stops arriving.
    filesystems = {key: sample for key, sample in filesystems.items()
                   if -5 <= time.time() - sample["sample_at"] <= 120}
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
        # Longhorn retains workload names after the final pod releases a
        # volume. These describe past use, not a current attachment. In
        # particular, retained CDI scratch PVs must still appear as unused.
        if unclaimed or ks.get("lastPodRefAt"):
            wls = []
        filesystem = filesystems.get((ks.get("namespace", ""), ks.get("pvcName", ""))) if not unclaimed and st.get("state") == "attached" else None
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
            "copies": copies.get(v["metadata"]["name"], []),
            # Detached with fewer whole copies than it asks for: Longhorn
            # repairs that only offline, or while something uses the volume.
            "copies_short": (lambda whole: {"whole": whole, "wanted": int(sp.get("numberOfReplicas") or 0),
                                            "offline": sp.get("offlineRebuilding") or "ignored"}
                             if st.get("state") == "detached" and whole < int(sp.get("numberOfReplicas") or 0) else None)(
                len({c["node"] for c in copies.get(v["metadata"]["name"], []) if c.get("whole")})),
            "engine": str(sp.get("dataEngine") or "v1").lower(),
            "actual_gb": round(int(st.get("actualSize", 0) or 0) / 1024**3, 2),
            "filesystem": filesystem,
            "used_pct": (filesystem or {}).get("used_pct"),
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
                # Why, in a sentence, where it is a pull that failed; the
                # whole message beside it - its cause is at the end.
                "message": (UPDATES.explain_pull(message) or str(message))[:220],
                "detail": str(message)[:1200],
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


def homestead_part(ns, name):
    """Which part of Homestead a Deployment is - itself, or a helper it runs
    and keeps in step: the SMB and NFS servers, the object store moves use.
    Empty for anything else. The Containers page hides these with the
    platform, and their updates are Homestead's, not an app's."""
    if is_self(ns, name):
        return "self"
    if is_managed_smb(ns, name):
        return "smb"
    if is_managed_nfs(ns, name):
        return "nfs"
    if (ns, name) == (OBJECTS.NS, OBJECTS.NAME):
        return "objectstore"
    return ""


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


class WorkloadRefused(Exception):
    """A start the capacity check will not allow as asked: 409, with its plan."""
    def __init__(self, message, plan):
        super().__init__(message)
        self.plan = plan


def scale_workload(ns, name, n, *, confirm_capacity=False, confirm_self=False):
    """A Deployment to n replicas, after the checks the app makes: never the
    SMB server's, never Homestead's own unless confirmed, and a start only
    where the capacity check allows it."""
    if n < 0 or n > 100:
        raise ValueError("replicas must be between 0 and 100")
    guard_managed_smb(ns, name)
    require_workload_target(ns, name)
    guard_self(ns, name, stopping=n == 0, confirmed=confirm_self)
    plan = None
    if n > 0:
        plan = workload_start_plan(ns, name, n)
        if plan["blocked"]:
            raise WorkloadRefused("not enough eligible capacity for the requested replicas", plan)
        if plan["requires_confirmation"] and not confirm_capacity:
            raise WorkloadRefused("review node memory before starting", plan)
    if n:
        clear_unstarted_pods(ns, name, stopping=False)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale",
          {"spec": {"replicas": n}}, ctype="application/merge-patch+json")
    if not n:
        clear_unstarted_pods(ns, name, stopping=True)
    _cache.pop("wl", None)
    return {"ok": True, "plan": plan}


def restart_workload(ns, name):
    guard_managed_smb(ns, name)
    require_workload_target(ns, name)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"spec": {"template": {"metadata": {"annotations":
           {NAMES.key("restartedAt"): time.strftime("%Y-%m-%dT%H:%M:%SZ")}}}}},
          ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True}


def guard_self(ns, name, stopping=False, confirmed=False, renaming=False, deleting=False, moving=False):
    """Changes that would take Homestead down from its own page.

    Stopping is allowed once it has been said out loud and confirmed; deleting,
    renaming and moving its storage never are from here - each needs the page
    running to finish, and would leave it gone halfway.
    """
    if not is_self(ns, name):
        return
    if HOSTACCESS.role() not in (None, "admin"):
        raise PermissionError("only an admin can change Homestead itself")
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
                                  "message": (UPDATES.explain_pull(waiting.get("message")) or waiting.get("message") or "")[:400]})
                    if reason in fatal_reasons:
                        why = UPDATES.explain_pull(waiting.get("message"))
                        fatal_waits.append(f"{p['metadata']['name']}: {reason}" + (f" - {why}" if why else ""))
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
            "platform": "Homestead" if homestead_part(ns, name) else PLATFORM_NS.get(ns, ""),
            "homestead": homestead_part(ns, name),
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
                           "reason": f"The last {job['task'].split('-')[0]} run failed. Review its job log."})
    if any(j["task"].startswith("backup") for j in guarding):
        target = target or {}
        if not target.get("configured"):
            issues.append({"severity": "degraded", "kind": "Backup", "name": "backup target",
                           "reason": "Backup jobs have no backup target. Configure a destination."})
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
                           "reason": f"Kubernetes reports {node.get('status') or 'not ready'}. Review this host."})
        for disk in (node.get("disk_issues") or []) if node.get("smart_notify", True) else []:
            issues.append({"severity": disk.get("severity", "degraded"), "kind": "Disk",
                           "name": f"{node.get('name', 'unknown')}/{disk.get('disk', 'unknown')}",
                           "reason": disk.get("reason", "SMART warning"),
                           "device_identity": disk.get("device_identity", ""),
                           **({"metric": disk["metric"], "value": disk["value"]} if "metric" in disk else {})})
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
        "lb_ip": NETWORK.shared_vip(),
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

    # --- ports & VIPs per app. The address a Service asks for, not only the
    # one it carries: a VIP kube-vip answers for but never recorded is where
    # the app is meant to be, and the page says why it is not reachable there.
    try:
        network = cached("network", 5, NETWORK.inventory)
    except Exception:
        network = {}
    places = network.get("addresses") or {"nodes": [], "addresses": []}
    wanted = {(row["namespace"], row["name"]): (row.get("requested_ips") or row.get("assigned_ips") or [None])[0]
              for row in network.get("services") or [] if row.get("type") == "LoadBalancer"}

    def address_of(s):
        ing = s.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
        return wanted.get((s["metadata"]["namespace"], s["metadata"]["name"])) or (ing[0].get("ip") if ing else None)

    ports_by_app, vips = {}, {}
    for s in svcs:
        app = (s["spec"].get("selector") or {}).get("app")
        if not app:
            continue
        vip = address_of(s)
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
            vip = address_of(s)
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

    # Every node, with its own addresses and the VIPs it answers for - a node
    # holding no replica still holds addresses.
    place = {row["ip"]: row for row in places["addresses"]}
    hosts = {row["name"]: row for row in places["nodes"]}
    names = sorted(set(nodes) | set(hosts))
    return {
        "nodes": [{"id": "n:" + k, "name": k, "copies": sorted(nodes.get(k, []), key=lambda x: x["vol"]),
                   "ips": (hosts.get(k) or {}).get("ips", []), "vips": (hosts.get(k) or {}).get("vips", [])}
                  for k in names],
        "volumes": sorted(vols, key=lambda x: x["name"]),
        "workloads": sorted(wls, key=lambda x: x["name"]),
        "vips": [{"id": "i:" + ip, "ip": ip, "ports": sorted(p, key=lambda x: x["port"]),
                  "kind": (place.get(ip) or {}).get("kind", "vip"), "node": (place.get(ip) or {}).get("node", ""),
                  "state": (place.get(ip) or {}).get("state", "ok"), "reason": (place.get(ip) or {}).get("reason", "")}
                 for ip, p in sorted(vips.items(), key=lambda item: (
                     (place.get(item[0]) or {}).get("kind") != "node",
                     tuple(int(x) if x.isdigit() else 999 for x in item[0].split("."))))],
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


def _build_single_deployment(cfg):
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
    # Arguments to the image's own entrypoint - cloudflared's "tunnel run",
    # say. A list of plain strings, passed as they are; never a shell.
    args = cfg.get("args") or []
    if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a) > 512 or "\x00" in a for a in args):
        raise ValueError("arguments are a list of up to 32 short strings")
    if args: c["args"] = list(args)
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

    podspec = {"containers": [c], "automountServiceAccountToken": False}
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
        vip = cfg.get("lb_ip") if mode in ("manual", "automatic") else ((cfg.get("lb_ip") or NETWORK.shared_vip()) if mode == "shared" else "")
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



def additional_container_configs(cfg):
    """Explicit extra definitions in a new pod; the original deploy stays compatible."""
    rows = cfg.get("additional_containers") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("additional containers must be a list of definitions")
    if rows and cfg.get("target_mode", "new") != "new":
        raise ValueError("add containers to an existing workload from its editor")
    if len(rows) > 31:
        raise ValueError("a new workload supports up to 32 containers")
    result = []
    for row in rows:
        allowed = {"name", "image", "env", "ports", "volumes", "hardware", "cpu", "memory", "memory_limit",
                   "privileges", "privileged", "cap_add", "tun", "volume_owners", "new", "container_name"}
        if set(row) - allowed - {"namespace", "workload_name", "network_mode", "target_mode", "app_profile", "env_bindings"}:
            raise ValueError("additional containers use their own image, resources, ports, environment, hardware and storage; pod settings are shared")
        item = {key: copy.deepcopy(value) for key, value in row.items() if key in allowed}
        item.update(namespace=cfg.get("namespace") or DEFAULT_NS,
                    workload_name=cfg.get("workload_name") or cfg.get("name"),
                    container_name=row.get("name"), name=row.get("name"),
                    network_mode=cfg.get("network_mode"), target_mode="new")
        privileges = item.pop("privileges", {}) or {}
        if not isinstance(privileges, dict) or set(privileges) - {"privileged", "cap_add", "tun"}:
            raise ValueError("invalid additional container privileges")
        item.update(privileges)
        item = ensure_profile_compatible(analyze_deploy_intent(item))
        result.append(item)
    return result


def deployment_volumes(cfg):
    return [volume for item in [cfg, *additional_container_configs(cfg)]
            for volume in item.get("volumes") or []]


def prepare_deploy_network(cfg):
    extras = additional_container_configs(cfg)
    if not extras:
        return NETWORK.prepare_deploy(cfg)
    combined = dict(cfg, ports=[port for item in [cfg, *extras] for port in item.get("ports") or []])
    planned = NETWORK.prepare_deploy(combined)
    return dict(planned, ports=cfg.get("ports") or [])


def build_deployment(cfg):
    extras = additional_container_configs(cfg)
    if not extras:
        return _build_single_deployment(cfg)
    HOSTACCESS.require_cfg([cfg, *extras])
    dep, _ = _build_single_deployment(cfg)
    changes = [dict(item, new=True, name=item["container_name"], privileges={
        key: item[key] for key in ("privileged", "cap_add", "tun") if key in item}) for item in extras]
    prepared = LC.prepare_edit({"ns": dep["metadata"]["namespace"], "name": dep["metadata"]["name"],
                                "containers": changes}, current=dep)
    dep = prepared["deployment"]
    pspec = dep["spec"]["template"]["spec"]
    used_names = {container["name"] for group in ("containers", "initContainers") for container in pspec.get(group) or []}
    for item, container in zip(extras, pspec["containers"][1:]):
        mounts = {mount["mountPath"]: mount for mount in container.get("volumeMounts") or []}
        owners = [(mounts[path]["name"], mounts[path].get("subPath", ""), *owner)
                  for path, owner in (item.get("volume_owners") or {}).items() if path in mounts]
        if owners:
            helper = VOLOWNER.init_container(owners)
            helper["name"] = LC._unique_volume_name("hs-owner-" + container["name"], used_names)
            pspec.setdefault("initContainers", []).append(helper)
    all_ports = [port for item in [cfg, *extras] for port in item.get("ports") or []]
    service_ports = [dict(port, name=port.get("name") or f"p{port.get('host') or port['container']}-{str(port.get('protocol') or 'TCP').lower()}") for port in all_ports]
    _, service = _build_single_deployment(dict(cfg, ports=service_ports))
    if service:
        names = [port["name"] for port in service["spec"]["ports"]]
        if len(names) != len(set(names)):
            raise ValueError("exposed port names must be unique across all containers")
    return dep, service


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
           "args": ["-g", "server min protocol = SMB2"],
           "env": {**SHARES.filesystem_identity(), "PERMISSIONS": ""}}
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


def import_namespace(value):
    """Where an import goes: the default, or an app namespace that exists."""
    ns = str(value or DEFAULT_NS).strip()
    if ns == DEFAULT_NS:
        return ns
    ns = _dns_name(ns, "namespace")
    if ns in SYS_NS or ns not in NSMOD.names(False):
        raise ValueError(f"namespace {ns} is not one an app can be imported into; create it under Namespaces first")
    return ns


def import_capacity_plan(body):
    """Review both sequential phases before an import creates any resources."""
    cfg = copy.deepcopy(body)
    ns = cfg["namespace"] = import_namespace(body.get("namespace"))
    guard_managed_smb(ns, cfg.get("name"))
    cfg = NETWORK.prepare_deploy(cfg)  # read-only VIP selection and validation
    prepared = IMP.prepare_import(cfg)
    if prepared["job"] and cfg.get("source_consistency") not in ("stopped", "snapshot"):
        raise ValueError("Choose whether the source writers are stopped or the copied paths are a consistent snapshot")
    inventory, pods = IMP.import_inventory(prepared, kget)
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    claims = {row["name"]: row for row in prepared["volumes"] if row["create"]}
    nodes = PLACE.get_nodes()
    phases = []
    for kind, title in (("job", "Copy files"), ("deployment", "Imported application")):
        manifest = prepared[kind]
        if manifest:
            plan = PLACE.manifest_plan(manifest, ns, manifest["metadata"]["name"], 1,
                                       threshold, planned_claims=claims, pod_snapshot=pods,
                                       nodes_snapshot=nodes)
            phases.append({"title": title, "capacity": plan})
    warnings = ["No resources are created until you confirm. Copy and application are reviewed separately, not as concurrent workloads.",
                "Imported files can replace files with the same names in existing volumes. Source consistency is not guaranteed; stop the source app or use a consistent backup.",
                "Storage provisioning, actual free filesystem space and future placement are not guaranteed by this review.",
                "The application stays stopped while copying. Starting it later requires a fresh capacity check."]
    capacity = {"blocked": any(row["capacity"]["blocked"] for row in phases),
                "requires_confirmation": True, "warnings": warnings}
    context = {"action": "import", "namespace": ns, **inventory, "prepared": prepared}
    return cfg, prepared, phases, capacity, context


def preview_import(body):
    _, prepared, phases, capacity, context = import_capacity_plan(body)
    return {"phases": phases, "capacity": capacity, "volumes": prepared["volumes"],
            "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def reviewed_import(body):
    cfg, prepared, _, capacity, context = import_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    # Logo persistence cannot change scheduling, but must follow admission.
    persist_icon_config(cfg)
    # Re-admit after the potentially slow logo fetch, not merely against the
    # inventory from before it. Registry/network/claim drift needs new review.
    _, prepared, _, capacity, context = import_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    context = copy.deepcopy(context)  # persisted logo must not mutate the signed review
    if prepared["deployment"]:
        annotations = prepared["deployment"]["metadata"].setdefault("annotations", {})
        if cfg.get("icon"):
            annotations[NAMES.key("icon")] = cfg["icon"]
            annotations[NAMES.key("icon-source")] = cfg.get("icon_source", "")
    return IMPORT_JOB.dispatch(body, prepared, context, kget, ksend, OPS, create_pvc, copy_admission)


def image_update_capacity_plan(body, action):
    """Resolve immutable images and review the whole rollout without writes."""
    if action not in ("update", "rollback"):
        raise ValueError("image action must be update or rollback")
    ns = _dns_name(body.get("ns"), "namespace")
    name = _dns_name(body.get("name"), "workload name")
    guard_managed_smb(ns, name)
    if action == "update":
        enforce_update_policy(body)
    prepared = (UPDATES.prepare_update if action == "update" else UPDATES.prepare_rollback)(ns, name)
    current, proposed = prepared["current"], prepared["proposed"]
    if current["spec"].get("paused"):
        raise ValueError("resume this paused workload before reviewing an image rollout")
    context = {"action": "image-" + action, **rollout_review_context(current),
               "proposed_spec": proposed["spec"], "before": prepared["before"],
               "annotations": proposed["metadata"].get("annotations", {})}
    cache = {f"/apis/apps/v1/namespaces/{ns}/deployments/{name}": current}
    def read(path):
        if path not in cache:
            cache[path] = kget(path)
        return copy.deepcopy(cache[path])
    def planner(*args, **kwargs):
        return PLACE.manifest_plan(*args, **kwargs, read=read)
    capacity = ROLLOUT_CAPACITY.plan(
        current, proposed, ns, read, PLACE.get_nodes, planner,
        get_app_settings()["thresholds"]["memory"]["critical"])
    # Every image change needs a fresh review, even when it comfortably fits.
    capacity["requires_confirmation"] = True
    if int(current["spec"].get("replicas", 1) or 0) == 0:
        capacity["warnings"].append("Stopped workloads have no current running image. Recovery uses a recorded digest where known; otherwise the old template tag is resolved now, not claimed to be a previously running image.")
    return prepared, capacity, context


def release_tag(dep, container_name, image=""):
    """The release a container follows: the tag Homestead tracks for it, else
    the tag in its image. An update pins the image to its digest, which drops
    the tag, so the tracked one is what says 2.8.215 rather than sha256:..."""
    tracked = UPDATES._annotation_json(dep, UPDATES.TRACKED).get(container_name, "")
    for ref in (tracked, image):
        tail = str(ref or "").split("@", 1)[0].rsplit("/", 1)[-1]
        if ":" in tail:
            return tail.rsplit(":", 1)[1]
    return ""


def preview_image_update(body):
    action = body.get("action", "update")
    prepared, capacity, context = image_update_capacity_plan(body, action)
    old = {c["name"]: c.get("image", "") for c in UPDATES._pod_containers(prepared["current"])}
    images = [{"container": c["name"], "before": old.get(c["name"], ""), "after": c.get("image", ""),
               "before_tag": release_tag(prepared["current"], c["name"], old.get(c["name"], "")),
               "after_tag": release_tag(prepared["proposed"], c["name"], c.get("image", "")),
               "rollback": prepared["before"].get(c["name"], "")}
              for c in UPDATES._pod_containers(prepared["proposed"])
              if c.get("image", "") != old.get(c["name"], "")]
    return {"capacity": capacity, "capacity_token": CAPACITY_REVIEW.issue(body, context),
            "images": images, "action": action}


def reviewed_image_update(body, action):
    if body.get("action", "update") != action:
        raise ValueError("image action changed; review again")
    prepared, capacity, context = image_update_capacity_plan(body, action)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    if action == "update":
        enforce_update_policy(body)  # Registry/preflight reads may cross a window boundary.
    return UPDATES.commit_prepared(prepared)


def move_capacity_plan(body):
    """Review the exact host-placement edit without stopping or writing anything."""
    ns, name = _dns_name(body.get("ns"), "namespace"), _dns_name(body.get("name"), "workload name")
    guard_managed_smb(ns, name)
    if body.get("auto"):
        raise ValueError("select an explicit suggested host and review it before moving")
    node = body.get("node") or None
    if node and (not isinstance(node, str) or len(node) > 253 or
                 not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in node.split("."))):
        raise ValueError("host must be a valid Kubernetes node name")
    if not isinstance(body.get("pin", False), bool):
        raise ValueError("pin must be true or false")
    cache = {}
    def read(path):
        if path not in cache:
            cache[path] = kget(path)
        return copy.deepcopy(cache[path])
    current = read(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    context = {"action": "host-move", **rollout_review_context(current)}
    if current["spec"].get("paused"):
        raise ValueError("paused workloads cannot be moved; resume and review placement again")
    if current["spec"]["template"]["spec"].get("nodeName"):
        raise ValueError("this pod template uses a direct nodeName binding; remove it before using reviewed host placement")
    # A move stops the old pods. Never interpret incomplete ownership as free
    # capacity, nor invite consent to stopping a workload on that assumption.
    pods = ROLLOUT_CAPACITY.items(read, "/api/v1/pods")
    _, known = ROLLOUT_CAPACITY.owned_pods(current, pods, read, ns)
    if not known:
        raise ValueError("pod ownership is unavailable; no move can be reviewed until inventory is complete")
    nodes = copy.deepcopy(PLACE.get_nodes())
    if node and not any(n["name"] == node for n in nodes):
        raise ValueError("selected host no longer exists; review placement again")
    proposed = copy.deepcopy(current)
    mode = PLACE.apply_node_placement(proposed, node, body.get("pin", False))
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    def planner(*args, **kwargs):
        return PLACE.manifest_plan(*args, **kwargs, read=read)
    def review(dep):
        return ROLLOUT_CAPACITY.plan(current, dep, ns, read, lambda: nodes, planner, threshold)
    capacity = review(proposed)
    # Check the chosen destination even for a soft preference, where Kubernetes
    # might otherwise hide its shortage by finding room on the current host.
    target = None
    if node:
        pinned = copy.deepcopy(proposed)
        pinned["spec"]["template"]["spec"].setdefault("nodeSelector", {})["kubernetes.io/hostname"] = node
        target = review(pinned)
        capacity["blocked"] = capacity["blocked"] or target["blocked"]
    capacity["move"] = {"node": node, "mode": mode, "target": target}
    capacity["warnings"].append("A preferred host is not guaranteed; Kubernetes may choose another eligible host, including the current host. Hard pinning prevents failover.")
    spec = proposed["spec"]["template"]["spec"]
    if any("hostPath" in v for v in spec.get("volumes", [])):
        capacity["warnings"].append("Host paths are not copied: files and devices may differ on the destination. Verify required host-local data before moving.")
    if any("emptyDir" in v for v in spec.get("volumes", [])):
        capacity["warnings"].append("Pod-local emptyDir data is lost when old pods are replaced.")
    capacity["requires_confirmation"] = True
    return proposed, capacity, context


def preview_host_move(body):
    _, capacity, context = move_capacity_plan(body)
    return {"capacity": capacity, "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def reviewed_host_move(body):
    proposed, capacity, context = move_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    meta = proposed["metadata"]
    # Preserve resourceVersion: a concurrent controller edit must conflict,
    # never be silently overwritten by a second, unreviewed fetch-and-move.
    ksend("PUT", f"/apis/apps/v1/namespaces/{meta['namespace']}/deployments/{meta['name']}", proposed)
    PLACE._bust("wl", "ov", "flow", "nodes", "impact:")
    return {"ok": True, "moved": meta["name"], "to": capacity["move"]["node"] or "any node",
            "mode": capacity["move"]["mode"]}


def capacity_manifest(config, existing=None):
    """Construct a read-only manifest and planned claims without provisioning."""
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
    claims = {row["name"]: row for row in new_claims(deployment_volumes(cfg))}
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
    return cfg, dep, claims, existing


def deploy_capacity_plan(config, existing=None):
    """Read-only new-workload or shared-pod rollout preview."""
    cfg, dep, claims, existing = capacity_manifest(config, existing)
    ns = cfg["namespace"]
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    if cfg.get("target_mode") == "existing":
        return ROLLOUT_CAPACITY.plan(existing, dep, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
                                     threshold, planned_claims=claims)
    return PLACE.manifest_plan(dep, ns, dep["metadata"]["name"], dep["spec"]["replicas"],
                               threshold, planned_claims=claims)


def hardware_device_paths():
    """The host paths an admin has offered as hardware features."""
    try:
        return {f.get("host_path") for f in HW.features() if f.get("host_path")}
    except Exception:
        return set()


def edit_capacity_plan(config):
    """Build the exact edit before seed/PVC/icon or workload writes."""
    ns, name = LC.dns_label(config["ns"], "namespace"), LC.dns_label(config["name"], "workload name")
    current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    HOSTACCESS.require_target(current, ns)
    if config.get("workload_name") and config["workload_name"] != name:
        new_name = LC.dns_label(config["workload_name"], "new workload name")
        if (config["ns"], config["name"], config["workload_name"]) != (ns, name, new_name):
            raise ValueError("Use exact lowercase Kubernetes names for the rename")
        guard_self(ns, name, renaming=True)
        proposed, context = RENAME.prepare(config, current, LC._renamed_deployment, kget)
        post_stop = copy.deepcopy(proposed)
        post_stop["spec"]["strategy"] = {"type": "Recreate"}  # rename orchestration, not a saved strategy change
        plan = ROLLOUT_CAPACITY.plan(current, post_stop, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
                                     get_app_settings()["thresholds"]["memory"]["critical"])
        plan["requires_confirmation"] = True
        plan["rename"] = {"from": name, "to": new_name}
        plan["warnings"].append("Rename stops the old pods before starting the new workload. Volumes and service addresses are kept. Failed or uncertain steps retain resources for inspection, not automatic rollback.")
        return {"deployment": proposed, "name": new_name, "claims": [], "seeds": []}, context, plan
    if config.get("remove_containers") and is_self(ns, name):
        raise ValueError("Homestead cannot remove its own containers from this page")
    prepared = LC.prepare_edit(config, current=current)
    context = {"action": "edit", **rollout_review_context(current),
               "seeds": [(path, cm.get("metadata", {}).get("resourceVersion")) for path, cm in prepared["seeds"]]}
    proposed = copy.deepcopy(prepared["deployment"])
    HOSTACCESS.require_edit(current, proposed, hardware_device_paths())
    if proposed["spec"].get("paused") and proposed["spec"].get("replicas", 1) > current["spec"].get("replicas", 1):
        raise ValueError("Increasing replicas of a paused Deployment needs a separate capacity review; resume it before editing replicas")
    if prepared["name"] != name:
        count = proposed["spec"].get("replicas", 1)
        proposed = LC._renamed_deployment(proposed, ns, prepared["name"])
        proposed["spec"].update(replicas=count, strategy={"type": "Recreate"})
    moves = RESTRUCTURE.copies(config)
    if moves:
        if prepared["name"] != name:
            raise ValueError("rename the workload and move its data in separate saves")
        if "lan" in config and (LAN.clean(config["lan"]) if config.get("lan") else None) != (LAN.read(current) or None):
            raise ValueError("Change the LAN address and move storage in separate saves")
        proposed["spec"]["strategy"] = {"type": "Recreate"}
        COPY_JOB.guards(current, kget)
        COPY_JOB.validate_sources(config, current)
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    plan = ROLLOUT_CAPACITY.plan(current, proposed, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
                                 threshold, planned_claims={row["name"]: row for row in prepared["claims"]})
    if moves:
        helper = COPY_JOB.helper_manifest(ns, name, moves)
        helper_plan = ROLLOUT_CAPACITY.plan(current, helper, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
            threshold, planned_claims={row["name"]: row for row in prepared["claims"]})
        plan["copy_helper"] = helper_plan
        plan["blocked"] = plan["blocked"] or helper_plan["blocked"]
        plan["requires_confirmation"] = True
        plan["warnings"] += ["Copy helper: " + warning for warning in helper_plan["warnings"]]
        plan["warnings"].append("Storage copy stops all containers. Placement is checked again before copying and restarting. Failed or uncertain steps keep the data and require inspection; no automatic rollback or deletion.")
        plan["warnings"].append("Destination files may be merged or overwritten. This is not a backup or filesystem free-space/consistency guarantee; avoid external writers during the copy.")
    if moves or prepared["name"] != name or proposed["spec"].get("replicas", 1):
        import_blocker = PLACE.IMPORT_GUARD.pending(current, ns, kget)
        if import_blocker:
            plan["blocked"] = True
            plan["warnings"].append(import_blocker)
    additions = [row.get("name") for row in config.get("containers") or [] if row.get("new") is True]
    removals = config.get("remove_containers") or []
    if additions or removals:
        exposed = [port for container in config.get("containers") or []
                   for port in container.get("ports") or [] if port.get("expose")]
        if exposed and config.get("manage_ports"):
            NETWORK._ports({"ports": [{"name": port.get("name"), "port": port.get("host") or port.get("container"),
                                      "target_port": port.get("container"), "protocol": port.get("protocol") or "TCP"}
                                     for port in exposed]})
        plan["container_changes"] = {"added": additions, "removed": removals}
        plan["requires_confirmation"] = True
        plan["warnings"].append("Changing the containers rolls every pod in this workload. Removed containers stop; persistent volumes and their data are retained.")
    if config.get("lan"):
        plan["warnings"].append("LAN network attachment availability is not guaranteed by the memory and placement review")
    return prepared, context, plan


def apply_reviewed_edit(b, prepared, hold=False):
    """Commit an already reviewed edit. Copy setup journals before calling this."""
    persist_icon_config(b)
    if "icon" in b:
        ann = prepared["deployment"]["metadata"].setdefault("annotations", {})
        for key in ("icon", "icon-source"):
            ann.pop(NAMES.key(key), None)
        if b["icon"]:
            ann[NAMES.key("icon")] = b["icon"]
            ann[NAMES.key("icon-source")] = b.get("icon_source", b["icon"])
    result = LC.edit_workload(b, hold=hold, prepared=prepared)
    if "lan" in b:
        lan_message = edit_lan(b["ns"], result.get("name") or b["name"], b.get("lan"))
        if lan_message:
            result["lan"] = lan_message
            if b.get("lan"):
                b["network_mode"] = "lan"
    ports = [port for container in b.get("containers") or [] for port in container.get("ports") or []]
    # The Address step, as in Deploy: how clients reach it, and on which VIP.
    address = b.get("address") if isinstance(b.get("address"), dict) else {}
    vip_mode = address.get("vip_mode") or "shared"
    if b.get("manage_ports") or any("expose" in port for port in ports):
        message = NETWORK.sync_workload_ports(b["ns"], result.get("name") or b["name"], ports,
                                              network_mode=address.get("network_mode") or b.get("network_mode"),
                                              vip_mode={"auto": "automatic"}.get(vip_mode, vip_mode),
                                              vip=address.get("lb_ip", "") if vip_mode == "manual" else "")
        if message:
            result["network"] = message
            _cache.pop("network", None)
    if address.get("network_mode") in ("loadbalancer", "internal"):
        message = NETWORK.set_workload_address(b["ns"], result.get("name") or b["name"], address["network_mode"],
                                               vip_mode, address.get("lb_ip", ""))
        if message:
            result["network"] = "; ".join(filter(None, [result.get("network"), message]))
            _cache.pop("network", None)
    return result


def copy_admission(dep):
    return PLACE.manifest_plan(dep, dep["metadata"]["namespace"], dep["metadata"]["name"], dep["spec"]["replicas"],
        get_app_settings()["thresholds"]["memory"]["critical"], read=kget,
        nodes_snapshot=PLACE.get_nodes(), pod_snapshot=ROLLOUT_CAPACITY.items(kget, "/api/v1/pods"))


def reviewed_deploy(b):
    """Deploy/App Store endpoint guard; Compose needs a whole-batch review."""
    b = ensure_profile_compatible(analyze_deploy_intent(copy.deepcopy(b)))
    HOSTACCESS.require_cfg([b, *additional_container_configs(b)])
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
    b = prepare_deploy_network(b)
    b = apply_deploy_bindings(b)
    b = apply_generated_secrets(b)
    b = prepare_lan(b)
    ns = b.get("namespace") or DEFAULT_NS
    target_mode = b.get("target_mode", "new")
    if target_mode == "new":
        b = VOLOWNER.prepare(b)
        b["additional_containers"] = [VOLOWNER.prepare(item) for item in additional_container_configs(b)]
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
    for claim in new_claims(deployment_volumes(b)):
        if ensure_claim(ns, claim["name"], claim["size_gb"], claim["storage_class"], claim["access_mode"]):
            reused.append(claim["name"])
    b["_reused_claims"] = reused
    if b.get("network_mode") == "lan" and target_mode == "new":
        LAN.ensure_nad(ns, target, b["lan"])
    masked = {item.get("key") for item in b.get("env_meta") or [] if item.get("masked")}
    ENVSEC.externalize(ns, dep, kget, ksend, masked)
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
    workloads = {d["metadata"]["name"] for d in
                 ROLLOUT_CAPACITY.items(kget, f"/apis/apps/v1/namespaces/{ns}/deployments")}
    claims = {c["metadata"]["name"] for c in
              ROLLOUT_CAPACITY.items(kget, f"/api/v1/namespaces/{ns}/persistentvolumeclaims")}
    vip_mode = b.get("vip_mode") if b.get("vip_mode") in ("shared", "auto", "nodes") else "shared"
    if NETWORK.node_addresses_only():
        # On k3s every service shares the nodes' addresses, so ports must differ.
        vip_mode = "shared"
    return COMPOSE.convert(text, b.get("variables") or "", ns, workloads, claims,
                           HW.features(), vip_mode)


def compose_preparation(b):
    """Resolve the selected batch without provisioning or persisting inputs."""
    report = compose_report(b)
    chosen = set(b.get("services") or [row["name"] for row in report["services"]])
    if chosen - {row["name"] for row in report["services"]}:
        raise ValueError("selected Compose service no longer exists; review again")
    rows = {row["name"]: row for row in report["services"] if row["name"] in chosen}
    if not rows:
        raise ValueError("choose at least one service to create")
    problems = report["errors"] + [dict(e, service=name) for name, row in rows.items() for e in row["errors"]]
    if problems:
        first = problems[0]
        where = f"{first['service']}: " if first.get("service") else ""
        raise ValueError(f"fix the file first: {where}{first['message']}"
                         + (f" (line {first['line']})" if first.get("line") else ""))
    configs, entries, claims = [], [], {}
    for name in report["order"]:
        if name not in rows:
            continue
        cfg = ensure_profile_compatible(analyze_deploy_intent(copy.deepcopy(rows[name]["config"])))
        guard_managed_smb(cfg.get("namespace") or DEFAULT_NS, cfg.get("workload_name") or cfg.get("name"))
        cfg, dep, pending, _ = capacity_manifest(cfg)
        for claim, descriptor in pending.items():
            if claim in claims and claims[claim] != descriptor:
                raise ValueError("inconsistent shared PVC settings: " + claim)
            claims[claim] = descriptor
        configs.append(cfg)
        entries.append({"name": name, "deployment": dep, "replicas": dep["spec"]["replicas"]})
    return configs, entries, claims


def compose_capacity(entries, claims, created=None):
    """Fresh joint review, including created controllers whose pods lag behind."""
    created = created or {}
    ns = entries[0]["deployment"]["metadata"]["namespace"]
    cache = {}
    def read(path):
        if path not in cache:
            try:
                cache[path] = kget(path)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                cache[path] = error
        value = cache[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)
    pods = ROLLOUT_CAPACITY.items(read, "/api/v1/pods")
    planned = copy.deepcopy(entries)
    starting_headroom = {}
    for entry in planned:
        previous = created.get(entry["name"])
        if previous is None:
            continue
        current = read(f"/apis/apps/v1/namespaces/{ns}/deployments/{entry['name']}")
        if current.get("metadata", {}).get("uid") != previous.get("metadata", {}).get("uid") or current.get("spec") != previous.get("spec"):
            raise ValueError(entry["name"] + " changed during batch creation; review the remaining services again")
        owned, known = ROLLOUT_CAPACITY.owned_pods(current, pods, read, ns)
        if not known:
            raise ValueError(entry["name"] + " pod ownership is unavailable; remaining services were not created")
        scheduled = [p for p in owned if p.get("spec", {}).get("nodeName") and not p.get("metadata", {}).get("deletionTimestamp")]
        for pod in scheduled:
            spec = pod["spec"]
            host = spec["nodeName"]
            starting_headroom[host] = starting_headroom.get(host, 0) + max(0,
                PLACE._pod_memory(spec)[0] - PLACE._pod_request(spec, "memory")) / 1024**3
        # Keep all scheduled/terminating consumers reserved. Replace only this
        # controller's unassigned pods with synthetic placements of its deficit.
        pending_ids = {id(p) for p in owned if not p.get("spec", {}).get("nodeName") and not p.get("metadata", {}).get("deletionTimestamp")}
        pods = [p for p in pods if id(p) not in pending_ids]
        entry["deployment"] = current
        entry["replicas"] = max(0, int(current["spec"].get("replicas", 1)) - len(scheduled))
    nodes = copy.deepcopy(PLACE.get_nodes())
    for node in nodes:
        node["batch_starting_headroom_gb"] = starting_headroom.get(node["name"], 0)
    return BATCH_CAPACITY.plan(planned, ns, pods, nodes, claims,
                               get_app_settings()["thresholds"]["memory"]["critical"], read=read)


def compose_preview(b):
    configs, entries, claims = compose_preparation(b)
    HOSTACCESS.require_cfg(configs)
    return {"capacity": compose_capacity(entries, claims),
            "capacity_token": CAPACITY_REVIEW.issue(b, {"action": "compose", "configs": configs})}


def compose_apply(b):
    """Guard the entire batch before any writes, then recheck its remainder."""
    configs, entries, claims = compose_preparation(b)
    HOSTACCESS.require_cfg(configs)
    plan = compose_capacity(entries, claims)
    if plan["status"] == "unknown":
        raise CAPACITY_REVIEW.Rejected("Complete batch placement could not be verified; split the batch and review again", plan)
    CAPACITY_REVIEW.enforce(b, plan, {"action": "compose", "configs": configs})
    created, controllers = [], {}
    for cfg, entry in zip(configs, entries):
        name = entry["name"]
        try:
            if created:
                fresh = compose_capacity(entries, claims, controllers)
                if fresh["blocked"]:
                    return {"ok": False, "created": created, "failed": name,
                            "error": "Remaining batch no longer fits the checked capacity; review again", "capacity": fresh}
            result = run_deploy(copy.deepcopy(cfg))
            created.append(result["name"])
            # Never assume the Deployment has pods just because POST returned.
            ns = cfg["namespace"]
            controller = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{result['name']}")
            if not controller.get("metadata", {}).get("uid"):
                raise ValueError("created workload identity is unavailable; stopped before the next service")
            controllers[name] = controller
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            return {"ok": False, "created": created, "failed": name, "error": f"HTTP {error.code}: {detail}"}
        except Exception as error:
            return {"ok": False, "created": created, "failed": name, "error": str(error)}
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
    HOSTACCESS.require_target(current, cfg.get("namespace") or DEFAULT_NS)
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
    if name == NAMES.object_name("isos"):
        # Homestead's ISO copies: one replica, not a class to put data on.
        return "iso"
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
    # k3s marks local-path the default and Longhorn's chart marks its own:
    # of several defaults, the one Homestead was installed with, then Longhorn.
    defaults = sorted((row for row in usable if row["default"]),
                      key=lambda row: (row["name"] != ENV_STORAGE_CLASS, row["provisioner"] != "driver.longhorn.io", row["name"]))
    chosen = (next((row["name"] for row in defaults), "")
              or (ENV_STORAGE_CLASS if ENV_STORAGE_CLASS in names else "")
              or next((n for n in ("longhorn-r2", "harvester-longhorn", "longhorn") if n in names), "")
              or STORAGE_CLASS)
    if chosen and chosen != STORAGE_CLASS:
        STORAGE_CLASS = chosen
        for module in ("LC", "LH"):
            if module in globals():
                globals()[module].STORAGE_CLASS = chosen


def cleanup_restore_classes():
    """Remove unused restore classes, retaining those needed for PVC expansion.

    Bound claims still need their class for resizing. Restore classes are
    excluded from regular pickers separately, so retaining them adds no clutter.
    """
    try:
        claims = kget("/api/v1/persistentvolumeclaims")
        if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
            return []
        volumes = kget("/api/v1/persistentvolumes")
        classes = kget("/apis/storage.k8s.io/v1/storageclasses")
        if any("items" not in collection or (collection.get("metadata") or {}).get("continue")
               for collection in (volumes, classes)):
            return []
        in_use = {STORAGE_RESIZE.storage_class(p) for p in claims["items"]}
        in_use.update((p.get("spec") or {}).get("storageClassName") for p in volumes["items"])
        items = classes["items"]
    except Exception:
        return []
    removed = []
    for item in items:
        name = item["metadata"]["name"]
        if name.startswith("homestead-restore-") and name not in in_use:
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
    claims = kget("/api/v1/persistentvolumeclaims")
    if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
        raise ValueError("Could not check every claim using the storage classes; try again")
    for item in claims["items"]:
        name = STORAGE_RESIZE.storage_class(item)
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
    # Kept unless asked otherwise: deleting an app should not take its data.
    reclaim = str(cfg.get("reclaim_policy") or "Retain")
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
    # Where the copies go. Longhorn's own default puts each on a different
    # host, so a one-host cluster can never place a second. "disks" lets
    # copies share a host but never a disk: a failed disk is survived there,
    # a failed host is not. (Longhorn 1.6 and later read these per class.)
    copies = str(cfg.get("copies") or "hosts")
    if copies not in ("hosts", "disks"):
        raise ValueError("copies go on different hosts or different disks")
    if copies == "disks":
        parameters.update(replicaSoftAntiAffinity="enabled", replicaDiskSoftAntiAffinity="disabled")
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


def _chosen_default_path():
    return os.path.join(DATA_DIR, "default-class.json")


def chosen_default_class():
    """The class someone made the default here, if anyone has."""
    try:
        with open(_chosen_default_path(), encoding="utf-8") as handle:
            return str((json.load(handle) or {}).get("name") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def reconcile_default_class():
    """k3s marks its local-path class as the default again every time it
    starts, and Longhorn's chart marks its own; with two defaults a claim
    that names no class lands on either. Keep one: the class chosen here,
    else a Longhorn one over local-path. Returns (kept, demoted) or None."""
    rows = storage_classes()
    defaults = [row for row in rows if row["default"]]
    if len(defaults) < 2:
        return None
    names = [row["name"] for row in defaults]
    chosen = chosen_default_class()
    keep = (chosen if chosen in names
            else next((row["name"] for row in defaults if row["provisioner"] == LONGHORN_PROVISIONER), names[0]))
    _clear_default_class(keep)
    return keep, [name for name in names if name != keep]


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
    # Kept, so a class k3s marks as default again at its next start is put
    # back. Only a convenience: the class is the default whether or not this
    # is written.
    try:
        SHARED.write_json(_chosen_default_path(), {"name": name, "at": int(time.time())})
    except OSError as error:
        print(f"storage: could not remember {name} as the chosen default: {error}", flush=True)
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
    volumes = kget("/api/v1/persistentvolumes")
    if "items" not in volumes or (volumes.get("metadata") or {}).get("continue"):
        raise ValueError("Could not check every backing volume; the StorageClass was not deleted")
    if any((pv.get("spec") or {}).get("storageClassName") == name for pv in volumes["items"]):
        raise ValueError("This StorageClass still backs a persistent volume; keep it so retained data can be recovered and resized")
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
    nodes = kget("/api/v1/nodes").get("items", [])
    return {"harvester": platform.get("harvester", False), "cdi": platform.get("cdi", False),
            "distribution": platform.get("distribution", ""),
            "storage_classes": selectable_storage_classes(rows),
            "storage_class_facts": storage_class_facts(rows),
            "default_class": vm_default_class(rows),
            "images": IMP.list_vm_images() if platform.get("harvester") else [],
            "store": VMSTORE.choices() if (platform.get("harvester") or platform.get("cdi")) else [],
            "networks": vm_networks(),
            "network_details": vm_network_details(),
            "vm_network_options": NETWORK.vm_network_options(node_temps()),
            "subnets": vm_subnets(),
            "nodes": sorted(n["metadata"]["name"] for n in nodes),
            # The Hardware tab: CPU models every node offers, and KubeVirt's features.
            "cpu_models": VM_HARDWARE.cpu_models(nodes),
            "kubevirt_gates": kubevirt_gates(),
            # A new VM's hardware as the form starts it: a plain VM, as made here.
            "hardware_base": VM_HARDWARE.read({"spec": {"template": {"spec": {
                "domain": {"cpu": {"cores": 1}},
                **({"evictionStrategy": "LiveMigrate"} if platform.get("harvester") else {})}}}}),
            # ISOs a CD-ROM can hold now.
            "isos": [{"name": v["name"], "file": v["file"]} for v in ISOS.volumes() if v["state"] == "ready"]}


def kubevirt_gates():
    try:
        kv = (kget("/apis/kubevirt.io/v1/kubevirts").get("items") or [{}])[0]
    except Exception:
        return []
    return sorted(((((kv.get("spec") or {}).get("configuration") or {}).get("developerConfiguration") or {})
                   .get("featureGates") or []))


def vm_networks():
    """The pod network and every network attachment (Multus) a VM can join."""
    try:
        items = kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions").get("items", [])
    except Exception:
        items = []
    return ["pod"] + sorted(f"{i['metadata']['namespace']}/{i['metadata']['name']}" for i in items)


def vm_network_details(strict=False):
    """Each VM network, and whether it puts a VM on the LAN with an address
    of its own - a bridge, as Harvester's VM networks are. Strict reads let
    setup distinguish unavailable inventory from no configured networks."""
    try:
        items = kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions").get("items", [])
    except urllib.error.HTTPError as error:
        if strict and error.code != 404:
            raise
        items = []
    except Exception:
        if strict:
            raise
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
                    "bridge": config.get("bridge", "") or config.get("master", "")
                              or ((item["metadata"].get("annotations") or {}).get("k8s.v1.cni.cncf.io/resourceName") or "").rsplit("/", 1)[-1],
                    "kind": labels.get("network.harvesterhci.io/type", ""),
                    # On the LAN: a bridge carries VMs and containers; macvlan
                    # gives containers a MAC of their own but cannot carry a
                    # VM; macvtap carries VMs only.
                    "lan": config.get("type") in ("bridge", "macvlan", "macvtap"),
                    "vms": config.get("type") in ("bridge", "macvtap"),
                    "containers": config.get("type") in ("bridge", "macvlan")})
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


class PowerNotSent(Exception):
    """A reviewed power request that stopped; the job it made says how far it got."""

    def __init__(self, message, operation):
        super().__init__(message)
        self.operation = operation


def active_power_job(node):
    """A reboot or shutdown job for this host that has not finished."""
    return next((item for item in OPS.list_operations()
                 if item.get("kind") == "node-power" and item.get("resource", {}).get("name") == node
                 and item.get("status") not in ("succeeded", "failed", "cancelled")), None)


def power_plan_with_job(power_plan):
    """A host whose power job is still going cannot be reviewed again: a
    second review during its drain is how the same drain started twice."""
    running = active_power_job(power_plan.get("node"))
    if running:
        power_plan = {**power_plan, "ready": False, "operation": running,
                      "blockers": ["A power job for this host is still running. Follow it in its progress view."]
                                  + list(power_plan.get("blockers") or [])}
    return power_plan


def homestead_running_on():
    """The claim Homestead's own Deployment mounts for its data, and whether
    it is ready on it now - read live, never from a cache."""
    dep = kget(f"/apis/apps/v1/namespaces/{SELF.NS}/deployments/{NAMES.BRAND}")
    spec, status = dep.get("spec") or {}, dep.get("status") or {}
    volumes = (spec.get("template") or {}).get("spec", {}).get("volumes") or []
    data = next((v for v in volumes if v.get("name") == "data"), None) or next(
        (v for v in volumes if v.get("persistentVolumeClaim")), {})
    claim = (data.get("persistentVolumeClaim") or {}).get("claimName", "")
    meta = dep.get("metadata") or {}
    ready = (int(status.get("readyReplicas") or 0) >= 1
             and int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0))
    return claim, ready


# The Homestead replica a host power job is running in. A drain can evict
# that very replica; the job then says which pod to look for, and the leader
# carries it on from another replica (resume_power_job).
POD_NAME = os.environ.get("HOSTNAME", "")
_POWER_RESUMING = set()
_POWER_RESUME_LOCK = threading.Lock()


def run_power_job(operation_id, power_plan, force=False, resumed=False):
    """Cordon, drain, recheck and send a reviewed reboot or shutdown, recording
    each phase on the job. Cordon and drain may be repeated safely; the send is
    guarded by the job's own receipts."""
    node, action = power_plan["node"], power_plan["action"]
    planned_outage = bool(power_plan.get("planned_outage")) and not force
    phase_state = {"phase": "reviewed"}

    def power_progress(phase, percent, message, **details):
        updated = OPS.record_phase(operation_id, phase, percent, message, owner=POD_NAME or None, **details)
        phase_state["phase"] = phase
        return updated

    def hold():
        return hold_for_power(operation_id, power_plan, power_progress)

    def handoff(node, action, steps, rep, report):
        return send_handoff(operation_id, power_plan, steps, rep, report)
    try:
        result = LC.node_power(node, action, True,
                               before_send=(lambda: POWER.recheck_planned_outage(power_plan)) if planned_outage else
                               (lambda: POWER.recheck_forced(power_plan)) if force
                               else (lambda: POWER.recheck_after_drain(power_plan)),
                               reviewed_pods=power_plan["drain_pods"], progress=power_progress, force=force, planned_outage=planned_outage,
                               resumed=resumed, hold=None if force else hold,
                               send=handoff if planned_outage else None)
        result["operation"] = {"id": operation_id}
        return result
    except OPS.Superseded:
        raise  # another replica owns the job; nothing to record or send here
    except Exception as e:
        uncertain = phase_state["phase"] in ("sending", "observing")
        message = ("Power submission outcome is uncertain; inspect the existing job/helper before retrying" if uncertain else
                   "Power was not sent. Inspect the host's cordon state: " + str(e))
        if uncertain:
            power_progress("observing", 20, message)
        else:
            # Power was not sent: what was stopped to wait for the host starts
            # again now. The host stays cordoned, so an app tied to it waits
            # for scheduling to be allowed.
            restored = {}
            held = next((((i.get("ref") or {}).get("held") or []) for i in OPS._read() if i.get("id") == operation_id), [])
            if held:
                try:
                    started, left = HOLD.restore(held, operation_id)
                    restored = {"restored": {"started": started, "left": left, "at": time.time()}}
                    message += (". Started again: " + ", ".join(started)) if started else ""
                    message += (". Left as they are: " + ", ".join(left)) if left else ""
                except Exception as error:
                    message += f". What was stopped could not be started again ({str(error)[:120]}); start it by hand"
            power_progress("failed", 10, message, failed_phase=phase_state["phase"], **restored)
        raise PowerNotSent(message, {"id": operation_id}) from e


def send_handoff(operation_id, power_plan, steps, rep, report):
    """The last step on a cluster's only host. Homestead's data volume is on
    that host: going down with Homestead still writing to it left the volume
    faulted. So a helper on the host takes over - Homestead's own image and
    account - and Homestead stops itself; the helper waits for its volume to
    detach, then asks systemd for the reboot or power-off, and starts
    Homestead again on the new boot (homestead_power_handoff)."""
    node, action = power_plan["node"], power_plan["action"]
    path = f"/apis/apps/v1/namespaces/{SELF.NS}/deployments/{NAMES.BRAND}"
    try:
        own, image = _self_data_helper_image(kget, SELF.NS)
        claim, _ = homestead_running_on()
        replicas = int((kget(path).get("spec") or {}).get("replicas", 1) or 1)
    except Exception as error:
        report("verifying", 15, f"Homestead cannot hand the last step to the host ({str(error)[:160]}); sending the {action} directly")
        return LC._send_power(node, action, steps, rep, report)
    spec = own.get("spec") or {}
    pod_name = f"homestead-handoff-{action}-{int(time.time()) % 100000}"
    body = {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": SELF.NS, "labels": NAMES.labels("node-power")},
            "spec": {"nodeName": node, "hostPID": True, "restartPolicy": "OnFailure",
                     "serviceAccountName": spec.get("serviceAccountName") or "default",
                     "imagePullSecrets": spec.get("imagePullSecrets") or [],
                     "tolerations": [{"operator": "Exists"}],
                     "terminationGracePeriodSeconds": 1,
                     "containers": [{"name": "handoff", "image": image,
                                     "command": ["python3", "/srv/homestead_power_handoff.py", SELF.NS, NAMES.BRAND,
                                                 operation_id, action, power_plan["boot_id"], claim],
                                     "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
                                     "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}}]}}
    # The helper's identity is on the job before it exists, as for any power helper.
    report("sending", 20, "Handing the last step to a helper on the host; power has not been sent",
           helper_pod=pod_name, helper_namespace=SELF.NS, handoff=True, started_epoch=time.time())
    receipt = ksend("POST", f"/api/v1/namespaces/{SELF.NS}/pods", body)
    meta = (receipt or {}).get("metadata") or {}
    if not meta.get("uid") or meta.get("name") != pod_name:
        raise ValueError("The host helper was not confirmed; inspect it before retrying")
    report("observing", 20, f"Homestead is stopping so its data volume detaches; the helper on the host then sends the {action}. "
           "This page is offline until the host is back", helper_uid=meta["uid"])
    # Homestead stops last, marked like anything else held for the host, so
    # the helper starts it again - and only while that mark is the job's.
    ksend("PATCH", path, {"metadata": {"annotations": {HOLD.HELD_BY: operation_id, HOLD.HELD_AS: str(replicas)}},
                          "spec": {"replicas": 0}}, ctype="application/merge-patch+json")
    steps.append(f"handed the {action} to {pod_name}; Homestead stopped so its data volume detaches")
    return {"ok": True, "node": node, "action": action, "steps": steps, "helper_pod": pod_name, "quorum": rep}


def hold_for_power(operation_id, power_plan, progress):
    """Stop what waits for the host and live-migrate the VMs that move, then
    wait for them to leave it. Safe to repeat: a resumed job finds its own
    marks. Returns whether there was anything to do."""
    node, picks = power_plan["node"], power_plan.get("choices") or {}
    items = power_plan.get("hold") or []
    waiting = [i for i in items if picks.get(i["id"]) == "wait"]
    moving = [i for i in items if i["kind"] == "VirtualMachine" and picks.get(i["id"]) == "move"]
    if not waiting and not moving:
        return False
    progress("holding", 8, f"Stopping {len(waiting)} app(s) and VM(s) to wait for the host"
             + (f", moving {len(moving)} VM(s)" if moving else "") + "; power has not been sent")
    held = [HOLD.stop(item, operation_id) for item in waiting]
    progress("holding", 8, "Waiting for them to stop; power has not been sent", held=held)
    if moving:
        # A resumed job finds some already moved: only what is still here moves.
        here = {((v.get("metadata") or {}).get("namespace"), (v.get("metadata") or {}).get("name"))
                for v in kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
                if (v.get("status") or {}).get("nodeName") == node}
        for vm in moving:
            if (vm["ns"], vm["name"]) in here:
                HOLD.migrate(vm)
    deadline = time.monotonic() + 600
    while True:
        pods = [p for p in kget("/api/v1/pods").get("items", []) if (p.get("spec") or {}).get("nodeName") == node]
        vmis = [v for v in (kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", []) if items and any(
            i["kind"] == "VirtualMachine" for i in items) else []) if (v.get("status") or {}).get("nodeName") == node]
        pending = [f"{i['ns']}/{i['name']}" for i in waiting + moving if not HOLD.gone(i, node, pods, vmis)]
        if not pending:
            return True
        if time.monotonic() >= deadline:
            raise ValueError("These did not stop or move within 10 minutes: " + ", ".join(pending[:6])
                             + ". What was stopped stays stopped until this job is released or the host is back")
        progress("holding", 8, "Waiting to stop or move: " + ", ".join(pending[:6]) + "; power has not been sent")
        time.sleep(3)


def restore_held(item, uncordon=True):
    """Start again what a power job stopped to wait for its host. Leader only,
    once; the job's resolver calls it and records what happened."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader() or ref.get("restored") is not None:
        return ref.get("restored") is not None
    if uncordon and not ref.get("planned_outage") and not ref.get("cordoned_before"):
        # What waited is for this host; it cannot start here while cordoned.
        node = kget(f"/api/v1/nodes/{urllib.parse.quote(ref['node'], safe='')}")
        if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") == ref["node_uid"]:
            LC.set_cordon(ref["node"], False)
            ref["uncordoned"] = True
    started, left = HOLD.restore(ref.get("held") or [], item["id"])
    ref["restored"] = {"started": started, "left": left, "at": time.time()}
    return True


def allow_scheduling(item):
    """Uncordon a host its power job cordoned, now it is back. Leader only,
    once; only the host the job was for, by identity."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader():
        return False
    if ref.get("uncordoned"):
        return True
    node = kget(f"/api/v1/nodes/{urllib.parse.quote(ref['node'], safe='')}")
    if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") != ref["node_uid"]:
        return False
    if (node.get("spec") or {}).get("unschedulable"):
        LC.set_cordon(ref["node"], False)
    ref["uncordoned"] = True
    return True


def release_held_power(operation_id):
    """Start what waits for a host on other hosts now, without waiting for it."""
    with OPS._lock:
        items = OPS._read()
        item = next((i for i in items if i.get("id") == operation_id), None)
        if not item or item.get("kind") != "node-power":
            raise ValueError("host power job not found")
        ref = item.get("ref") or {}
        if not ref.get("held") or ref.get("restored") is not None:
            raise ValueError("nothing is waiting for this host")
        if ref.get("phase") not in ("observing",):
            raise ValueError("wait until the power command has been sent")
        restore_held(item, uncordon=False)
        OPS._write(items)
    return {"ok": True, "detail": "Started again: " + (", ".join(ref["restored"]["started"]) or "nothing")}


def _power_in_background(operation_id, power_plan, force, resumed=False):
    def run():
        try:
            run_power_job(operation_id, power_plan, force, resumed)
        except (PowerNotSent, OPS.Superseded):
            pass  # recorded on the job, or carried on by another replica
        except Exception as error:  # never leave the job looking busy
            try:
                OPS.record_phase(operation_id, "failed", 10, f"Host power job stopped: {error}"[:400])
            except Exception:
                pass
        finally:
            with _POWER_RESUME_LOCK:
                _POWER_RESUMING.discard(operation_id)
    threading.Thread(target=run, name=f"node-power-{power_plan['node']}", daemon=True).start()


def send_reviewed_power(power_plan, force=False, background=False):
    """Cordon, drain and send a reviewed reboot or shutdown, as a job - what
    Host actions does once its review is accepted, and what an OS update of
    every host does for each host that needs a restart. background returns
    the job at once and leaves the work, and its outcome, to the job."""
    node, action = power_plan["node"], power_plan["action"]
    planned_outage = bool(power_plan.get("planned_outage")) and not force
    operation = OPS.start(
        "node-power", f"{action} {node}", {"kind": "Node", "name": node},
        "/nodes?node=" + urllib.parse.quote(node),
        {"node": node, "node_uid": power_plan["node_uid"], "action": action, "boot_id": power_plan["boot_id"],
         "choices": power_plan.get("choices") or {},
         # A host cordoned before the review stays so after it; one this job
         # cordoned is allowed scheduling again when it is back.
         "cordoned_before": bool(power_plan.get("cordoned")),
         "volumes": [v["name"] for v in power_plan["volumes"]],
         "planned_outage": planned_outage, "forced": bool(force),
         # What a resumed job needs: the reviewed plan, and which replica runs it.
         "plan": power_plan, "worker": POD_NAME,
         "phase": "reviewed", "phase_at": time.time(), "started_epoch": time.time()},
        "Planned whole-cluster outage; sending without cordon or drain" if planned_outage else
        "Forced by an admin; sending without cordon or drain" if force else "Host impact reviewed; preparing cordon and drain")
    if not background:
        result = run_power_job(operation["id"], power_plan, force)
        result["operation"] = operation
        return result
    with _POWER_RESUME_LOCK:
        _POWER_RESUMING.add(operation["id"])
    _power_in_background(operation["id"], power_plan, force)
    return {"operation": operation, "steps": [], "background": True}


def power_worker_gone(pod):
    """Whether the replica a power job named has gone - evicted by the drain."""
    try:
        found = kget(f"/api/v1/namespaces/{SELF.NS}/pods/{urllib.parse.quote(pod, safe='')}")
    except urllib.error.HTTPError as error:
        return error.code == 404
    except Exception:
        return False  # unknown is not gone
    if (found.get("metadata") or {}).get("deletionTimestamp") or \
            (found.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
        return True
    # A host shut down or lost leaves its pods listed for minutes; one whose
    # node is not Ready is not running anything.
    node = (found.get("spec") or {}).get("nodeName")
    if not node:
        return False
    try:
        conditions = (kget(f"/api/v1/nodes/{urllib.parse.quote(node, safe='')}").get("status") or {}).get("conditions") or []
    except Exception:
        return False
    return not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _power_jobs_loop():
    """The leader looks after host power jobs itself: a job's resolver runs
    when someone lists jobs, and that may be another replica, or nobody."""
    while True:
        time.sleep(15)
        try:
            if not LEADER.is_leader():
                continue
            active = [i for i in OPS._read() if i.get("kind") in ("node-power", REBALANCE.KIND, CREBALANCE.KIND)
                      and i.get("status") not in OPS.TERMINAL]
            if active:
                OPS.list_operations()   # runs each job's resolver, which resumes it if needed
        except Exception as error:
            print(f"host power jobs not checked: {str(error)[:160]}", flush=True)


def _detached_copies_loop():
    """Offline rebuilding on by default, and Homestead's stand-in for it on
    Longhorn without one; the leader only."""
    while True:
        time.sleep(60)
        try:
            if LEADER.is_leader() and (cluster_shutdown().state() or {}).get("phase") in (None, "released"):
                LHREBUILD.tick()
                _cache.pop("lhrebuild", None)
        except Exception as error:
            print(f"detached volume copies not checked: {str(error)[:160]}", flush=True)


def resume_power_job(item):
    """Carry on a host power job whose replica was evicted before it sent the
    command. Only the leader does, once; called from the job's resolver, so it
    writes nothing here - the thread claims the job, then cordons and drains
    again (both safe to repeat), rechecks and sends."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader() or not isinstance(ref.get("plan"), dict):
        return False
    with _POWER_RESUME_LOCK:
        if item["id"] in _POWER_RESUMING:
            return True
        _POWER_RESUMING.add(item["id"])

    def claim_then_run():
        try:
            OPS.record_phase(item["id"], ref.get("phase", "draining"), item.get("progress", 10),
                             "Homestead moved while preparing this host; carrying on from another replica", worker=POD_NAME)
        except Exception:
            with _POWER_RESUME_LOCK:
                _POWER_RESUMING.discard(item["id"])
            return
        _power_in_background(item["id"], ref["plan"], bool(ref.get("forced")), resumed=True)
    threading.Thread(target=claim_then_run, name=f"node-power-resume-{ref.get('node', '')}", daemon=True).start()
    return True


SYSTEM_HOST_PATHS = ("/var/lib/kubelet", "/run", "/var/run", "/dev", "/sys", "/proc", "/lib/modules",
                     "/etc/localtime", "/var/log", "/var/lib/rancher", "/etc/rancher")


SYSTEM_POD_NAMESPACES = ("kube-system", "longhorn-system", "kubevirt", "cdi", "cattle-system", "system-upgrade",
                         "cattle-fleet-system", "harvester-system")


def _system_host_path(row):
    """A host path that is the host's own plumbing - kubelet's plugin and pod
    directories, sockets, devices, logs - not data a pod keeps there. Nor
    is anything Kubernetes or Longhorn itself mounts: an instance manager
    mounts / and /var/lib/longhorn, and the volumes it serves are judged by
    their copies, not by its mounts."""
    if not str(row.get("kind", "")).startswith("host-local path"):
        return False
    if str(row.get("pod") or "").split("/", 1)[0] in SYSTEM_POD_NAMESPACES:
        return True
    path = "/" + str(row.get("source") or "").strip("/")
    return any(path == p or path.startswith(p + "/") for p in SYSTEM_HOST_PATHS)


def rollout_reboot(node, allow_single_copy=False):
    """A restart for an OS update: the same review Host actions shows, with
    nobody to accept its warnings - so what a person would have to accept
    stops it, except a volume's only copy when the settings accept that."""
    power_plan = POWER.plan(node, "reboot")
    if power_plan.get("planned_outage"):
        raise ValueError("A single-host cluster outage needs a manual review and acknowledgement in Host actions")
    if not power_plan["ready"]:
        raise ValueError("; ".join(power_plan["blockers"]))
    if power_plan["stranded"]:
        raise ValueError("some workloads have no other host to run on")
    hold = power_plan.get("hold") or []
    if any(i["kind"] == "VirtualMachine" for i in hold):
        raise ValueError("Running VMs are on this host; migrate or stop them and review again")
    waits = [f"{i['ns']}/{i['name']}" for i in hold if i["default"] != "move"]
    if waits:
        raise ValueError("These would have to stop and wait for the host: " + ", ".join(waits[:6]))
    power_plan["choices"] = HOLD.choose(hold, None)
    if power_plan["requires_data_ack"] and not allow_single_copy:
        # What a person would have to accept, said as it is. A pod's emptyDir
        # is scratch space every drain deletes - on a k3s host metrics-server
        # and Traefik have one - and is no reason to leave a host unrestarted.
        reasons = []
        single = [v["claim"] for v in power_plan.get("volumes") or [] if v.get("risk") in ("unavailable", "single-copy")]
        if single:
            reasons.append("a volume has its only healthy copy on this host: " + ", ".join(single[:4]))
        # Nor are the host's own sockets and devices data: Longhorn's CSI
        # attacher mounts /var/lib/kubelet/plugins/driver.longhorn.io.
        kept = [f"{row['pod']} ({row['source']})" for row in (power_plan.get("maintenance") or {}).get("local_storage") or []
                if not str(row.get("kind", "")).startswith("emptyDir") and not _system_host_path(row)]
        if kept:
            reasons.append("pods keep data on this host itself: " + ", ".join(kept[:4]))
        if power_plan.get("storage_unknown"):
            reasons.append("Longhorn's volumes could not be read")
        if reasons:
            raise ValueError("; ".join(reasons) + " (the settings do not accept that)")
    try:
        return send_reviewed_power(power_plan)["operation"]["id"]
    except PowerNotSent as e:
        raise ValueError(str(e)) from e


def rollout_power_job(node, since):
    """A restart of this host started since the rollout began and not failed:
    the one a drained leader left running."""
    for item in OPS.list_operations():
        ref = item.get("ref") or {}
        if (item.get("kind") == "node-power" and ref.get("node") == node and ref.get("action") == "reboot"
                and float(ref.get("started_epoch") or 0) >= float(since or 0)
                and item.get("status") not in ("failed", "cancelled")):
            return item["id"]
    return ""


def operation_item(operation_id):
    """A job-tray item as it stands now, or None."""
    return next((item for item in OPS.list_operations() if item.get("id") == operation_id), None) if operation_id else None


def welcome_state(role="admin"):
    """The first-run checklist: the few settings a new cluster wants, each
    with whether it is done. Shown to an admin until one says it is done."""
    try:
        with open(os.path.join(DATA_DIR, "welcome.json"), encoding="utf-8") as handle:
            done = bool(json.load(handle).get("done"))
    except (OSError, ValueError):
        done = False
    p = PLATFORM.detect() or {}
    steps = {}
    try:
        address = SELF_ADDRESS.report(cached("network", 5, NETWORK.inventory))
        steps["address"] = {"done": address["on_vip"], "url": address["url"], "shared_vip": address["shared_vip"],
                            "vips": len(NETWORK.registered())}
    except Exception as error:
        steps["address"] = {"done": False, "error": str(error)[:160]}
    steps["probe"] = {"done": bool(PROBE.installed())}
    try:
        steps["backups"] = {"done": bool(LH.backup_target().get("configured"))}
    except Exception:
        steps["backups"] = {"done": False}
    steps["updates"] = {"applies": not p.get("harvester") and p.get("distribution") in ("k3s", "rke2"),
                        "done": bool((OS_ROLLOUT.settings().get("schedule") or {}).get("enabled"))}
    return {"show": role == "admin" and not done, "done": done, "harvester": bool(p.get("harvester")),
            "load_balancer": p.get("load_balancer", ""), "steps": steps}


TUNNEL_IMAGES = ("cloudflare/cloudflared", "tailscale/tailscale")


def setup_state(user, role):
    """The setup guide: each step and whether the cluster shows it done.
    Looked at fresh each time; only skips are remembered. A person who is not
    an admin gets their own steps alone."""
    p = PLATFORM.detect() or {}
    kube = not p.get("harvester") and p.get("distribution") in ("k3s", "rke2")
    store = SETUP.load()
    steps = {}

    def step(name, compute):
        try:
            steps[name] = compute()
        except Exception as error:
            steps[name] = {"done": False, "applies": True, "error": str(error)[:160]}

    if role == "admin":
        def health():
            ov = cached("ov", 5, get_overview)
            issues = ov.get("health_issues") or []
            return {"done": not issues, "applies": True, "summary": ov.get("health_summary", ""),
                    "issues": [{k: x.get(k, "") for k in ("severity", "kind", "name", "reason")} for x in issues[:6]]}
        step("health", health)

        def quorum():
            q = LC.quorum_report()
            nodes = [{"name": n["name"], "ready": n.get("status") == "Ready", "roles": n.get("roles") or []}
                     for n in cached("nodes", 5, get_nodes)]
            servers = q["total"] or sum(1 for n in nodes if any(r in ("control-plane", "master", "etcd") for r in n["roles"])) or 1
            return {"done": servers != 2, "applies": True, "servers": servers, "members": q["members"],
                    "ready": q["ready"], "can_lose": q["can_lose"], "nodes": nodes}
        step("quorum", quorum)

        def clocks():
            known = {n["name"]: (HOST_OS.stored(n["name"]) or {}).get("ntp") for n in cached("nodes", 5, get_nodes)}
            told = {k: v for k, v in known.items() if v is not None}
            return {"done": bool(told) and all(told.values()), "applies": kube and bool(told),
                    "unsynced": sorted(k for k, v in told.items() if v is False)}
        step("clocks", clocks)

        def address():
            report = SELF_ADDRESS.report(cached("network", 5, NETWORK.inventory))
            return {"done": bool(report["on_vip"]), "applies": True, "url": report["url"], "service_url": report.get("service_url", ""),
                    "shared_vip": report.get("shared_vip"), "vips": len(NETWORK.registered()),
                    "load_balancer": p.get("load_balancer", ""), "harvester": bool(p.get("harvester"))}
        step("address", address)

        def lan():
            networks = [row for row in vm_network_details(strict=True) if row["lan"]]
            return {**SETUP.lan_state(networks),
                    "networks": [{key: row[key] for key in ("name", "type", "vms", "containers")} for row in networks]}
        step("lan", lan)

        def https():
            tunnels = sorted({w["name"] for w in cached("wl", 5, get_workloads)
                              if any(t in image for image in w.get("images") or [] for t in TUNNEL_IMAGES)})
            return {"done": bool(store.get("https_url")), "applies": True, "url": store.get("https_url", ""), "tunnels": tunnels}
        step("https", https)
        step("hostname", lambda: {"done": False, "applies": True})       # the browser can tell; see the page

        def disks():
            unused = [{"node": node, "device": r["device"], "size_gb": r.get("size_gb"), "kind": r.get("kind", "")}
                      for node, rows in DISKS.inventory()["nodes"].items() for r in rows
                      if r.get("role") == "unused" and not r.get("system")]
            return {"done": not unused, "applies": bool(p.get("longhorn", True)) and not p.get("harvester"), "unused": unused[:12]}
        step("disks", disks)

        def storage():
            classes = storage_classes()
            default = next((c for c in classes if c.get("default")), None)
            ready = sum(1 for n in cached("nodes", 5, get_nodes) if n.get("status") == "Ready")
            target = max(1, min(3, ready))
            copies = int(default["replicas"]) if default and str(default.get("replicas") or "").isdigit() else None
            fits = bool(default) and default.get("provisioner") == "driver.longhorn.io" and copies is not None and copies == target
            return {"done": fits, "applies": True, "default": (default or {}).get("name", ""), "copies": copies,
                    "provisioner": (default or {}).get("provisioner", ""), "nodes": ready, "target": target,
                    "candidates": [c["name"] for c in classes if c.get("provisioner") == "driver.longhorn.io"
                                   and str(c.get("replicas")) == str(target) and not c.get("made_for") and not c.get("internal")]}
        step("storage", storage)

        def smb():
            report = samba_state()
            return {"done": bool(report.get("installed") and report.get("enabled") and not report.get("error")),
                    "applies": True, "installed": bool(report.get("installed")), "enabled": bool(report.get("enabled")),
                    "address": report.get("address", ""), "shares": report.get("shares", 0),
                    **({"error": report["error"]} if report.get("error") else {})}
        step("smb", smb)
        step("backups", lambda: {"done": bool(LH.backup_target().get("configured")), "applies": True})
        step("config", lambda: {"done": bool(store.get("config_backup_at")), "applies": True, "at": store.get("config_backup_at")})
        step("osupdates", lambda: {"done": bool((OS_ROLLOUT.settings().get("schedule") or {}).get("enabled")), "applies": kube})
        step("people", lambda: {"done": sum(1 for u in AUTH.list_users() if u["role"] == "admin") >= 2, "applies": True,
                                "users": len(AUTH.list_users())})
        ipam_read = {}

        def ipam_data():
            # One read of the IP-address record for both its steps.
            if "data" not in ipam_read:
                ipam_read["data"] = IPAM.load()[0]
            return ipam_read["data"]
        step("unifi", lambda: {"done": bool((ipam_data().get("unifi") or {}).get("url")), "applies": True})

        def ipam():
            # IP addresses: subnets known, each scanned, and - with UniFi
            # connected - its devices and reservations brought in.
            data = ipam_data()
            unifi = data.get("unifi") or {}
            subnets = [{"id": s.get("id"), "cidr": s.get("cidr"), "name": s.get("name", ""),
                        "scanned": int((data.get("scans", {}).get(s.get("cidr")) or {}).get("at") or 0)}
                       for s in data.get("subnets") or []]
            connected = bool(unifi.get("url") and unifi.get("has_key"))
            synced = int(unifi.get("last_sync") or 0)
            return {"done": bool(subnets) and all(s["scanned"] for s in subnets) and (bool(synced) or not connected),
                    "applies": True, "subnets": subnets, "unifi": connected, "synced": synced}
        step("ipam", ipam)
        step("unraid", lambda: {"done": bool(IMP.list_sources()), "applies": True})
        step("homeassistant", lambda: {"done": any(not k["expired"] for k in API_KEYS.list_keys()), "applies": True})
        step("linked", lambda: {"done": bool(FLEET.summary().get("linked")), "applies": True})
        step("starter", lambda: {"done": any(not w.get("homestead") for w in cached("wl", 5, get_workloads)), "applies": True})
        step("console", lambda: {"done": bool(HOST_CONSOLE.inventory().get("enabled")), "applies": kube})
    step("notifications", lambda: {"done": bool(PUSH.devices(user)), "applies": True})
    return {"steps": steps, "skips": SETUP.skips(user), "hidden": SETUP.hidden(user), "completed": SETUP.completed(user),
            "opened": SETUP.opened(), "admin": role == "admin", "personal": list(SETUP.PERSONAL)}


def own_node():
    """The node this Homestead pod runs on."""
    try:
        return kget(f"/api/v1/namespaces/{SELF.NS}/pods/{os.environ.get('HOSTNAME', '')}")["spec"]["nodeName"]
    except Exception:
        return ""


def vm_power_capacity_plan(body):
    ns = _dns_name(body.get("ns", DEFAULT_NS), "namespace")
    name = _dns_name(body.get("name"), "VM name")
    action = body.get("action")
    if action not in ("start", "restart", "unpause"):
        raise ValueError("Only Start, Restart and Resume need a VM capacity review")
    current = kget(f"{VMS.API}/namespaces/{ns}/virtualmachines/{name}")
    rollout_review_context(current)  # require identity/version, not a name-only approval
    expanded_spec = None
    if action != "unpause":
        expanded_spec = VM_PROFILES.expand(current, kget)
    dependencies = {}
    def observed_read(path):
        capture = any(part in path for part in ("/persistentvolumeclaims/", "/persistentvolumes/", "/storageclasses/",
                                                "/datavolumes/", "/network-attachment-definitions/"))
        try:
            value = kget(path)
        except urllib.error.HTTPError as error:
            if capture and error.code == 404:
                dependencies[path] = None
            raise
        if capture:
            dependencies[path] = VM_CAPACITY.VMRES.identity(value)
        return value
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    plan = VM_CAPACITY.plan(current, observed_read, PLACE.get_nodes(), action=action, current=current,
                            warning_percent=threshold, expanded_spec=expanded_spec, power_intents=OPS._read())
    strategy = VMS._strategy(current)
    policy_after = "Always" if action == "start" and strategy == "Halted" else strategy
    plan["vm"]["policy_before"], plan["vm"]["policy_after"] = strategy, policy_after
    if strategy == "Once" and action in ("start", "restart"):
        plan["blockers"].append("This VM uses the Once run strategy. Edit its run strategy and review that change before starting it again.")
        plan["blocked"] = True
    if policy_after != strategy:
        plan["warnings"].append("Starting a Halted VM changes its KubeVirt run strategy to Always: it will be restarted after shutdown or failure until you stop it.")
    plan["warnings"].append("Power requests address the VM by name. Identity is rechecked immediately before sending, but this is not an atomic scheduler reservation or a cross-resource transaction.")
    plan["requires_confirmation"] = True
    context = {"action": "vm-power", "observations": plan["vm"]["context"],
               "policy_before": strategy, "policy_after": policy_after,
               "expanded_spec": expanded_spec, "dependencies": dependencies}
    return plan, context


def preview_vm_power(body):
    plan, context = vm_power_capacity_plan(body)
    return {"capacity": plan, "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def vm_create_configuration(body, *, preview=False):
    cfg = copy.deepcopy(body)
    cfg["namespace"] = _dns_name(cfg.get("namespace", DEFAULT_NS), "namespace")
    cfg["name"] = _dns_name(cfg.get("name"), "VM name")
    if cfg.get("isolated") is not True and not cfg.get("mac"):
        if not preview:
            raise ValueError("Review VM creation first so its generated MAC is fixed")
        cfg["mac"] = IMP._vm_mac()
    if cfg.get("store_id"):
        source = VMSTORE.source_for(str(cfg["store_id"]))
        cfg["image_id"], cfg["image_url"] = source.get("image_id", ""), source.get("image_url", "")
        cfg["disk_gb"] = max(int(cfg.get("disk_gb") or 0), source["min_gb"])
    cfg["storage_class"] = str(cfg.get("storage_class") or vm_default_class())
    return cfg


def vm_creation_capacity(prepared):
    """Read-only create evidence, including controller-created disk intentions."""
    observations = {}
    def read(path):
        try:
            value = kget(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                observations[path] = None
            raise
        # Pod reservations are checked fresh, not frozen for ten minutes. Only
        # actual dependencies/configuration bind the user's reviewed intent.
        if path != "/api/v1/pods":
            if isinstance(value.get("items"), list):
                observations[path] = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                            key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                observations[path] = VM_CAPACITY.VMRES.identity(value)
        return value
    claims = VM_CLAIMS.plans(prepared["vm"], read, prepared["claims"], prepared["downloads"])
    VM_CLAIMS.pin(prepared["vm"], claims, prepared["claims"])
    borrowed = {(volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name")
                for volume in prepared["vm"]["spec"]["template"]["spec"].get("volumes") or []} - {None, ""} - set(claims)
    borrowed_users = []
    if borrowed:
        # A stopped VM still owns its guest disk. Do not rely on active Pods
        # alone or the display-oriented best-effort import inventory.
        inventory = kget("/apis/kubevirt.io/v1/virtualmachines")
        if not isinstance(inventory.get("items"), list) or (inventory.get("metadata") or {}).get("continue"):
            raise ValueError("VM disk ownership inventory is incomplete")
        for owner in inventory["items"]:
            if owner.get("metadata", {}).get("namespace") != prepared["namespace"]:
                continue
            volumes = ((owner.get("spec", {}).get("template") or {}).get("spec") or {}).get("volumes") or []
            if any(((volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name")) in borrowed for volume in volumes):
                borrowed_users.append(owner["metadata"]["name"])
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    plan = VM_CAPACITY.plan(prepared["vm"], read, PLACE.get_nodes(), action="create", warning_percent=threshold,
                           planned_claims=claims, planned_configmaps={e["path"]: e["body"] for e in prepared.get("effects", [])
                                                                      if e["kind"] == "configmap" and e["body"]})
    plan["requires_confirmation"] = True
    if borrowed_users:
        plan["blockers"].append("Selected disk is referenced by existing VM(s), including stopped VMs: " + ", ".join(sorted(borrowed_users)))
        plan["blocked"] = True
    plan["warnings"] += ["Image import/provisioning may start before the guest. Importer and controller overhead is not fully rendered in this estimate.",
                         "If a later step fails, created images, claims or Secrets are retained for inspection; do not blindly repeat creation."]
    if prepared["vm"]["spec"].get("runStrategy") == "Halted":
        # Still show the future start plan, but a stopped VM does not allocate
        # a launcher. Storage provisioning/import can run independently.
        plan["future_start_blocked"] = plan["blocked"]
        plan["blocked"] = bool(plan["blockers"])
        plan["warnings"].append("The VM is created stopped. Displayed guest placement is for a future start and will be checked again then.")
    context = {"action": "vm-create", "prepared": copy.deepcopy(prepared), "dependencies": observations}
    return plan, claims, context


def preview_vm_create(body):
    cfg = vm_create_configuration(body, preview=True)
    prepared = IMP.prepare_vm(cfg, PLATFORM.detect(), cfg["storage_class"])
    IMP._recheck_vm_creation(prepared)
    plan, claims, context = vm_creation_capacity(prepared)
    if cfg.get("static_ip"):
        problem = vm_address_problem(str(cfg["static_ip"].get("address") or "").strip())
        if problem:
            plan["blockers"].append(problem)
            plan["blocked"] = True
    # Echo only the user's config plus normalized/generated values. Prepared
    # Secrets/cloud-init manifests are never included in the preview response.
    return {"config": cfg, "capacity": plan, "volumes": list(claims.values()),
            "capacity_token": CAPACITY_REVIEW.issue(cfg, context)}


def reviewed_vm_create(body):
    cfg = vm_create_configuration(body)
    prepared = IMP.prepare_vm(cfg, PLATFORM.detect(), cfg["storage_class"])
    IMP._recheck_vm_creation(prepared)
    plan, _, context = vm_creation_capacity(prepared)
    CAPACITY_REVIEW.enforce(cfg, plan, context)
    def check_address():
        if cfg.get("static_ip"):
            problem = vm_address_problem(str(cfg["static_ip"].get("address") or "").strip())
            if problem:
                raise ValueError(problem)
    check_address()
    def admit_after_preparation(resolved):
        check_address()
        # Downloads may resolve an image-specific class. Evaluate that exact
        # resolved manifest, retaining the original signed input/consent.
        fresh, _, _ = vm_creation_capacity(resolved)
        for path, expected in context["dependencies"].items():
            value = VM_CAPACITY._optional(kget, path) if not isinstance(expected, list) else kget(path)
            if isinstance(expected, list):
                if not isinstance(value.get("items"), list) or (value.get("metadata") or {}).get("continue"):
                    raise ValueError("VM dependency inventory became incomplete")
                actual = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                actual = VM_CAPACITY.VMRES.identity(value) if value else None
            if actual != expected:
                raise CAPACITY_REVIEW.Rejected("VM creation dependencies changed during image preparation; inspect retained resources and review again", fresh)
        CAPACITY_REVIEW.enforce(cfg, fresh, context)
    result = VM_MUTATION_JOB.dispatch("vm-create", cfg, cfg["namespace"], cfg["name"], None, OPS, IMP.ksend,
        lambda send: IMP.commit_vm(prepared, before_save=admit_after_preparation, send=send))
    if result.get("address"):
        try:
            IPAM.save_record({"ip": result["address"], "name": cfg["name"], "kind": "static",
                              "category": "server", "mac": result.get("mac", ""), "owner": "homestead",
                              "note": f"VM {cfg['namespace']}/{cfg['name']}"})
        except Exception:
            result["warning"] = " ".join(filter(None, [result.get("warning"),
                "VM created, but its IP-address record could not be saved; inspect IP addresses before reusing the address."]))
    return result


def reviewed_vm_power(body):
    if body.get("action") in ("stop", "force-stop", "pause"):
        return _reviewed_vm_power(body)
    # Serialize local dispatches through the durable intent becoming visible.
    # Kubernetes still owns the final allocation across external clients.
    with SHARED.SharedLock("vm-device-power", strict=True, directory=lambda: OPS.DATA_DIR, timeout=60):
        return _reviewed_vm_power(body)


def _reviewed_vm_power(body):
    action = body.get("action", "")
    ns = _dns_name(body.get("ns", DEFAULT_NS), "namespace")
    name = _dns_name(body.get("name"), "VM name")
    if action in ("stop", "force-stop", "pause"):
        # Recovery must remain available even if capacity/config inventory fails.
        return VMS.power(ns, name, action)
    plan, context = vm_power_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, plan, context)
    for path, expected in context["dependencies"].items():
        observed = VM_CAPACITY._optional(kget, path)
        if (VM_CAPACITY.VMRES.identity(observed) if observed else None) != expected:
            raise CAPACITY_REVIEW.Rejected("A VM storage/network dependency changed during admission; review it again", plan)
    # Re-read both identities after the inventory and token checks. Never retry
    # an API conflict by fetching and writing a new run policy.
    current = kget(f"{VMS.API}/namespaces/{ns}/virtualmachines/{name}")
    observations = context["observations"]
    if VM_CAPACITY.VMRES.identity(current) != observations["vm"]:
        raise CAPACITY_REVIEW.Rejected("The VM changed during admission; review it again", plan)
    vmi = VM_CAPACITY._optional(kget, f"{VMS.API}/namespaces/{ns}/virtualmachineinstances/{name}")
    if (VM_CAPACITY.VMRES.identity(vmi) if vmi else None) != observations.get("vmi"):
        raise CAPACITY_REVIEW.Rejected("The running VM instance changed during admission; review it again", plan)
    def before_send():
        fresh, fresh_context = vm_power_capacity_plan(body)
        CAPACITY_REVIEW.enforce(body, fresh, fresh_context)
    return VM_POWER_JOB.dispatch(body, context, OPS,
        lambda: VMS.power(ns, name, action, raw_errors=True), before_send)


def api_vm_power(ns, name, action):
    """Start, stop or restart a VM for /api/v1: stop as the app does; start and
    restart through the same capacity review, taken as acknowledged when it
    is not blocked - the key's scope is the consent the app asks a person for.
    A start that needs a person (missing TPM or EFI state) is refused."""
    if action not in ("start", "stop", "restart"):
        raise ValueError("start, stop or restart")
    body = {"ns": _dns_name(ns, "namespace"), "name": _dns_name(name, "VM name"), "action": action}
    if action == "stop":
        result = reviewed_vm_power(body)
        _cache.pop("vms", None)
        return {"warnings": [], "job": ((result or {}).get("operation") or {}).get("id") if isinstance(result, dict) else None}
    # As the app's route does before a review: an older VM's read-only ISO
    # would stop KubeVirt starting it.
    ISOS.unlock(body["ns"])
    preview = preview_vm_power(body)
    plan = preview["capacity"]
    if plan.get("blocked"):
        raise API_V1.ApiError(409, "it cannot start: " + "; ".join(plan.get("blockers") or ["not enough room"]),
                              blockers=list(plan.get("blockers") or []))
    if (plan.get("vm") or {}).get("state_initialization"):
        raise API_V1.ApiError(409, "starting it needs its TPM or EFI state set up first; do that in the app")
    result = reviewed_vm_power({**body, "capacity_token": preview["capacity_token"], "confirm_capacity": True})
    _cache.pop("vms", None)
    job = (result.get("operation") or {}).get("id") if isinstance(result, dict) else None
    return {"warnings": list(plan.get("warnings") or []), "job": job}


def api_scale(ns, name, n):
    """Start or stop a container for /api/v1, under the app's own checks."""
    try:
        result = scale_workload(ns, name, n, confirm_capacity=True, confirm_self=False)
    except WorkloadRefused as refused:
        raise API_V1.ApiError(409, str(refused), blockers=list((refused.plan or {}).get("blockers") or [])) from None
    return {"warnings": list((result.get("plan") or {}).get("warnings") or []), "job": None}


def vm_edit_capacity(prepared):
    """Admit proposed edits, not the old VMI's resource requirements.

    Metadata-only and stop/manual-policy edits remain available without a
    functioning capacity inventory. A template change is never assumed inert:
    KubeVirt LiveUpdate may apply it without an explicit restart request.
    """
    current, vm = prepared["current"], prepared["vm"]
    before, after = VMS._strategy(current), VMS._strategy(vm)
    needed = (vm["spec"]["template"] != current["spec"]["template"] or
              (vm.get("metadata", {}).get("labels") or {}) != (current.get("metadata", {}).get("labels") or {}) or
              bool(prepared["resize"] or prepared["effects"] or prepared["to_create"]) or
              (before != after and after not in ("Halted", "Manual")))
    observations, claims, expanded_spec = {}, {}, None
    def read(path):
        try:
            value = kget(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                observations[path] = None
            raise
        if path != "/api/v1/pods":
            if isinstance(value.get("items"), list):
                observations[path] = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                            key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                observations[path] = VM_CAPACITY.VMRES.identity(value)
        return value
    if needed:
        # Existing controller templates are not promises to recreate a missing
        # disk. Only new definitions get the planned-claim exception.
        proposed = copy.deepcopy(vm)
        old_claims = {VMS._volume_claim(volume) for volume in current["spec"]["template"]["spec"].get("volumes") or []}
        proposed["spec"]["dataVolumeTemplates"] = [row for row in VMS._dv_templates(vm) if row["metadata"]["name"] not in old_claims]
        VMS._set_claim_templates(proposed, [row for row in VMS._claim_templates(vm) if row["metadata"]["name"] not in old_claims])
        downloads = [effect for effect in prepared["effects"] if effect["kind"] == "image-download"]
        claims = VM_CLAIMS.plans(proposed, read, prepared["to_create"], downloads)
        # Pin only newly planned disks. Existing controller templates must not
        # be rewritten using today's storage defaults.
        VM_CLAIMS.pin(vm, claims, prepared["to_create"])
        expanded_spec = VM_PROFILES.expand(vm, kget, ksend)
        threshold = get_app_settings()["thresholds"]["memory"]["critical"]
        plan = VM_CAPACITY.plan(vm, read, PLACE.get_nodes(), action="edit", current=current,
                               warning_percent=threshold, planned_claims=claims, expanded_spec=expanded_spec,
                               planned_configmaps={e["path"]: e["body"] for e in prepared["effects"] if e["kind"] == "configmap" and e["body"]})
        plan["warnings"].append("Template and restart-policy changes may take effect immediately through KubeVirt. Saving is not a promise that the guest remains stopped or unchanged.")
        if after == "Halted":
            plan["warnings"].append("The requested policy is Halted. Resource placement shown is conservative; a separate reviewed Start is required to run it again.")
    else:
        plan = {"blocked": False, "blockers": [], "warnings": [], "vm": {"action": "edit"}}
    for effect in prepared["effects"]:
        if effect["kind"] == "replace-datavolume" and effect.get("identity"):
            plan["blockers"].append("Existing DataVolume replacement is unsafe in an edit. Add a disk with a new name/source, then detach the old disk; no old disk is deleted.")
            plan["blocked"] = True
    plan["vm"].update(admission_needed=needed, policy_before=before, policy_after=after)
    plan["warnings"].append("VM, Secret and disk changes are not a transaction. If saving fails, inspect retained resources before trying again. No automatic restart is sent by Save.")
    plan["requires_confirmation"] = True
    context = {"action": "vm-edit", "prepared": copy.deepcopy(prepared), "dependencies": observations,
               "expanded_spec": expanded_spec}
    return plan, claims, context


def prepare_vm_edit(body):
    if body.get("restart"):
        raise ValueError("Save the VM edit first, then review Restart separately against its saved resources")
    ns = _dns_name(body.get("ns", DEFAULT_NS), "namespace")
    name = _dns_name(body.get("name"), "VM name")
    prepared = VMS.prepare_edit(ns, name, body)
    VMS._recheck_edit(prepared)
    return prepared


def preview_vm_edit(body):
    prepared = prepare_vm_edit(body)
    plan, claims, context = vm_edit_capacity(prepared)
    return {"capacity": plan, "volumes": list(claims.values()),
            "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def reviewed_vm_edit(body):
    prepared = prepare_vm_edit(body)
    plan, _, context = vm_edit_capacity(prepared)
    CAPACITY_REVIEW.enforce(body, plan, context)
    def before_save(resolved):
        fresh, _, fresh_context = vm_edit_capacity(resolved)
        if fresh_context["expanded_spec"] != context["expanded_spec"]:
            raise CAPACITY_REVIEW.Rejected("VM profile expansion changed during preparation; review the proposed resources again", fresh)
        for path, expected in context["dependencies"].items():
            value = VM_CAPACITY._optional(kget, path) if not isinstance(expected, list) else kget(path)
            if isinstance(expected, list):
                if not isinstance(value.get("items"), list) or (value.get("metadata") or {}).get("continue"):
                    raise ValueError("VM edit dependency inventory became incomplete")
                actual = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                actual = VM_CAPACITY.VMRES.identity(value) if value else None
            if actual != expected:
                raise CAPACITY_REVIEW.Rejected("VM edit dependencies changed; inspect retained resources and review again", fresh)
        CAPACITY_REVIEW.enforce(body, fresh, context)
        VMS._recheck_edit(resolved)
    return VM_MUTATION_JOB.dispatch("vm-edit", body, prepared["namespace"], prepared["name"], prepared["identity"], OPS, VMS.ksend,
        lambda send: VMS.commit_edit(prepared, before_save=before_save, send=send))


def vm_cluster_configuration(body, *, preview=False):
    cfg = copy.deepcopy(body)
    cfg["namespace"] = _dns_name(cfg.get("namespace") or DEFAULT_NS, "namespace")
    built = K3SC.plan(cfg)
    cfg["name"] = built["name"]
    if preview:
        cfg["review_id"] = secrets.token_hex(16)
        cfg["macs"] = {node["name"]: IMP._vm_mac() for node in built["nodes"]}
    if not re.fullmatch(r"[a-f0-9]{32}", str(cfg.get("review_id") or "")):
        raise ValueError("Review the VM cluster first to freeze its generated identifiers")
    if not isinstance(cfg.get("macs"), dict) or set(cfg["macs"]) != {node["name"] for node in built["nodes"]}:
        raise ValueError("VM cluster MAC addresses are incomplete; review again")
    if len(set(cfg["macs"].values())) != len(built["nodes"]):
        raise ValueError("VM cluster MAC addresses must be distinct")
    cfg["storage_class"] = str(cfg.get("storage_class") or vm_default_class())
    return cfg


def vm_cluster_snapshot():
    cache, external = {}, {}
    def read(path):
        capture = bool(re.fullmatch(r"/api/v1/namespaces/[^/]+", path)) or any(part in path for part in ("/storageclasses", "/storageprofiles/", "/network-attachment-definitions/", "/kubevirts"))
        if path not in cache:
            try:
                cache[path] = kget(path)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                cache[path] = error
                if capture:
                    external[path] = None
        value = cache[path]
        if isinstance(value, Exception):
            raise value
        if capture:
            external[path] = (sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                    key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
                              if isinstance(value.get("items"), list) else VM_CAPACITY.VMRES.identity(value))
        return copy.deepcopy(value)
    # Allocation observations bracket a live RPC. Identity/policy reads must
    # bypass this batch's otherwise useful immutable inventory cache.
    read.fresh = kget
    return read, external


def prepare_vm_cluster(cfg):
    token = CAPACITY_REVIEW.derive_secret(cfg, "k3s-bootstrap-join")
    batch = K3SC.prepare(cfg, token=token, guest_checks=True)
    read, external = vm_cluster_snapshot()
    platform = PLATFORM.detect()
    prepared = []
    for config in batch["configs"]:
        item = IMP.prepare_vm(config, platform, cfg["storage_class"])
        K3SC.HEALTH.pin(item, batch["guest_health"])
        IMP._recheck_vm_creation(item)
        claims = VM_CLAIMS.plans(item["vm"], read, item["claims"], item["downloads"])
        VM_CLAIMS.pin(item["vm"], claims, item["claims"])
        prepared.append(item)
    plan = VM_BATCH.plan(prepared, read, PLACE.get_nodes(), threshold=get_app_settings()["thresholds"]["memory"]["critical"])
    context = {"action": "vm-cluster-create", "prepared": copy.deepcopy(prepared), "external": external,
               "numa_policy": copy.deepcopy(plan.get("numa_policy") or {})}
    return batch, prepared, plan, context


def preview_vm_cluster(body):
    cfg = vm_cluster_configuration(body, preview=True)
    batch, _, capacity, context = prepare_vm_cluster(cfg)
    return {**batch["plan"], "ok": not capacity["blocked"], "config": cfg, "capacity": capacity,
            "capacity_token": CAPACITY_REVIEW.issue(cfg, context)}


def reviewed_vm_cluster(body):
    cfg = vm_cluster_configuration(body)
    batch, prepared, plan, context = prepare_vm_cluster(cfg)
    CAPACITY_REVIEW.enforce(cfg, plan, context)
    receipts = {}
    def admit():
        # All remaining controllers still count, including already-created VMs
        # whose launchers have not appeared in Kubernetes yet.
        for item, config in zip(prepared, batch["configs"]):
            if item["name"] not in receipts:
                IMP._recheck_vm_creation(item)
                problem = vm_address_problem(config["static_ip"]["address"])
                if problem:
                    raise ValueError(problem)
        read, _ = vm_cluster_snapshot()
        fresh = VM_BATCH.plan(prepared, read, PLACE.get_nodes(), created=receipts,
                              threshold=get_app_settings()["thresholds"]["memory"]["critical"])
        if (fresh.get("numa_policy") or {}) != context["numa_policy"]:
            raise CAPACITY_REVIEW.Rejected("Host allocation policy changed; retain partial resources and review again", fresh)
        for path, expected in context["external"].items():
            value = VM_CAPACITY._optional(read, path) if expected is None else read(path)
            actual = (sorted([VM_CAPACITY.VMRES.identity(row) for row in VM_CAPACITY._items(read, path)],
                             key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
                      if isinstance(expected, list) else VM_CAPACITY.VMRES.identity(value) if value else None)
            if actual != expected:
                raise CAPACITY_REVIEW.Rejected("VM batch dependencies changed; retain partial resources and review again", fresh)
        CAPACITY_REVIEW.enforce(cfg, fresh, context)
    def before_node(config, made):
        receipts.update({row["name"]: row["identity"] for row in made})
        admit()
    def create_one(config, send=None):
        index = next(i for i, item in enumerate(prepared) if item["name"] == config["name"])
        def after_images(resolved):
            # Image downloads can resolve a new storage class. Pin and re-admit
            # that exact manifest before any PVC/Secret/VM mutation.
            read, _ = vm_cluster_snapshot()
            claims = VM_CLAIMS.plans(resolved["vm"], read, resolved["claims"], resolved["downloads"])
            VM_CLAIMS.pin(resolved["vm"], claims, resolved["claims"])
            prepared[index] = resolved
            admit()
        result = IMP.commit_vm(prepared[index], before_save=after_images, send=send)
        if result.get("address"):
            try:
                IPAM.save_record({"ip": result["address"], "name": config["name"], "kind": "static",
                                  "category": "server", "mac": result.get("mac", ""), "owner": "homestead",
                                  "note": f"VM {config['namespace']}/{config['name']}"})
            except Exception:
                # A failed address record is not authority to repeat creation.
                # Halt the batch with its pre-dispatch recovery intent retained.
                raise ValueError("VM created but its IP-address record could not be saved; inspect it before continuing") from None
        return result
    return K3SC.commit(batch, OPS, create_one=create_one, before_node=before_node, review=cfg, send=IMP.ksend)


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


# Provisioners that can serve one volume to pods on several nodes at once.
# k3s's local-path and most block CSI drivers cannot: a ReadWriteMany claim on
# them stays Pending for good.
SHARED_DRIVERS = {"driver.longhorn.io", "nfs.csi.k8s.io", "efs.csi.aws.com", "file.csi.azure.com", "smb.csi.k8s.io"}


def serves_many(row):
    provisioner = str(row.get("provisioner") or "")
    return provisioner in SHARED_DRIVERS or provisioner.endswith(".cephfs.csi.ceph.com")


def shared_storage_classes(rows=None):
    """Classes that can actually serve ReadWriteMany to a pod."""
    return [row["name"] for row in (rows if rows is not None else storage_classes())
            if class_selectable(row) and not row["migratable"] and serves_many(row)]


def create_pvc(ns, name, size_gb, sc=None, access_mode="ReadWriteOnce", send=None):
    sc = sc or STORAGE_CLASS
    if access_mode == "ReadWriteMany":
        chosen = next((row for row in storage_classes() if row["name"] == sc), None)
        if chosen and not serves_many(chosen):
            usable = ", ".join(shared_storage_classes()) or "none in this cluster"
            raise ValueError(f"storage class {sc} ({chosen['provisioner']}) serves one node at a time, so a shared "
                             f"(ReadWriteMany) volume on it would never be made. Use one node's access, or a class "
                             f"that shares: {usable}")
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
    return (send or ksend)("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", body)


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


def _restore_class_repair(pvc):
    """Reconstruct a missing Homestead restore class from its bound CSI volumes."""
    spec, meta = pvc.get("spec") or {}, pvc.get("metadata") or {}
    name = spec.get("storageClassName") or ""
    labels = meta.get("labels") or {}
    if (not re.fullmatch(r"homestead-restore-[0-9a-f]{16}", name)
            or labels.get("app.kubernetes.io/managed-by") != "homestead"
            or labels.get("homestead.io/restored-volume") != "true"
            or not (meta.get("annotations") or {}).get("homestead.io/restored-from-backup")):
        raise ValueError("Only missing classes from Homestead restores can be repaired here")
    claims = kget("/api/v1/persistentvolumeclaims")
    if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
        raise ValueError("Could not check every claim using this StorageClass")
    users = [c for c in claims["items"] if ((c.get("metadata") or {}).get("annotations") or {}).get(
                 "volume.beta.kubernetes.io/storage-class", (c.get("spec") or {}).get("storageClassName")) == name]
    if not meta.get("uid") or not any((c.get("metadata") or {}).get("uid") == meta["uid"] for c in users):
        raise ValueError("The claim changed; reopen the volume editor")
    parameters = None
    reclaim = None
    for claim in users:
        cs, cm = claim.get("spec") or {}, claim.get("metadata") or {}
        if (claim.get("status") or {}).get("phase") != "Bound" or cm.get("deletionTimestamp") or not cs.get("volumeName"):
            raise ValueError("Every claim using this class must be bound and not being deleted")
        pv = kget("/api/v1/persistentvolumes/" + urllib.parse.quote(cs["volumeName"], safe=""))
        ps = pv.get("spec") or {}
        ref, csi = ps.get("claimRef") or {}, ps.get("csi") or {}
        if (ps.get("storageClassName") != name or (pv.get("status") or {}).get("phase") != "Bound"
                or not cm.get("uid") or ref.get("uid") != cm["uid"]
                or ref.get("name") != cm.get("name") or ref.get("namespace") != cm.get("namespace")
                or csi.get("driver") != "driver.longhorn.io"):
            raise ValueError("The backing volume does not match the restored claim")
        attrs = {k: v for k, v in (csi.get("volumeAttributes") or {}).items()
                 if k not in ("storage.kubernetes.io/csiProvisionerIdentity", "share")}
        if not attrs.get("fromBackup") or not all(isinstance(v, str) for v in attrs.values()):
            raise ValueError("The backing volume has no usable restore parameters")
        if csi.get("fsType"):
            attrs["fsType"] = csi["fsType"]
        policy = ps.get("persistentVolumeReclaimPolicy")
        if policy not in ("Delete", "Retain") or (parameters is not None and (parameters != attrs or reclaim != policy)):
            raise ValueError("The volumes using this class have different storage settings")
        parameters, reclaim = attrs, policy
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "homestead", "homestead.io/restore-class": "true"},
                         "annotations": {"homestead.io/resize-support-repaired": "true"}},
            "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True,
            "reclaimPolicy": reclaim, "volumeBindingMode": "Immediate", "parameters": parameters}


def repair_restore_classes():
    """Put back the restore classes older releases removed while claims still
    used them. Those claims kept working, but Kubernetes will not resize a
    claim whose class is gone, and Homestead listed them as "other" volumes.
    Each class is rebuilt from its own bound volumes (_restore_class_repair),
    which refuses anything it cannot match exactly."""
    claims = kget("/api/v1/persistentvolumeclaims")
    if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
        return []
    classes = kget("/apis/storage.k8s.io/v1/storageclasses")
    if "items" not in classes or (classes.get("metadata") or {}).get("continue"):
        return []
    present = {item["metadata"]["name"] for item in classes["items"]}
    repaired = []
    for pvc in claims["items"]:
        name = (pvc.get("spec") or {}).get("storageClassName") or ""
        if not name.startswith("homestead-restore-") or name in present:
            continue
        try:
            ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", _restore_class_repair(pvc))
            repaired.append(name)
            present.add(name)
        except (ValueError, urllib.error.HTTPError) as error:
            print(f"restore class {name} not repaired: {str(error)[:160]}", flush=True)
    if repaired:
        for key in list(_cache):
            if key.startswith(("stor", "sc", "vol")):
                _cache.pop(key, None)
    return repaired


def repair_volume_class(cfg):
    ns, name = cfg.get("namespace") or DEFAULT_NS, cfg.get("name") or ""
    pvc = kget(f"/api/v1/namespaces/{urllib.parse.quote(ns, safe='')}/persistentvolumeclaims/{urllib.parse.quote(name, safe='')}")
    class_name = (pvc.get("spec") or {}).get("storageClassName") or ""
    if cfg.get("confirm") != class_name or not class_name:
        raise ValueError("Review the missing StorageClass before restoring it")
    try:
        kget("/apis/storage.k8s.io/v1/storageclasses/" + urllib.parse.quote(class_name, safe=""))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise ValueError("The StorageClass already exists; reopen the volume editor")
    body = _restore_class_repair(pvc)
    ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", body)
    for key in list(_cache):
        if key.startswith(("stor", "sc", "vol")):
            _cache.pop(key, None)
    return {"ok": True, "detail": "Resize support repaired. You can now enlarge the volume."}


def volume_edit_options(namespace, name, pvc=None):
    """Read current Kubernetes expansion prerequisites without changing storage."""
    if not name:
        raise ValueError("Choose a volume")
    ns = urllib.parse.quote(namespace or DEFAULT_NS, safe="")
    claim = urllib.parse.quote(name, safe="")
    if pvc is None:
        pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    spec = pvc.get("spec") or {}
    requested_mb = _quantity_mb((spec.get("resources") or {}).get("requests", {}).get("storage", "0"))
    result = STORAGE_RESIZE.options(pvc, kget)
    result.update(requested_gb=-(-requested_mb // 1024) if requested_mb else 0, repair_class=False)
    if result.pop("missing_class"):
        result["reason"] = "This volume's StorageClass was removed. An administrator must repair its resize support before increasing the size."
        try:
            _restore_class_repair(pvc)
            result["repair_class"] = True
        except (ValueError, urllib.error.HTTPError):
            pass
    return result


def edit_volume(cfg):
    """Grow a PVC and optionally change Longhorn replica count.

    Only what changed is sent. Longhorn's volumes live in its namespace; the
    replica count was written to a path without it, which Kubernetes answers
    "404 page not found" - after the size had already been changed, so every
    save of the form reported a failure that had half happened.
    """
    ns, name = cfg.get("namespace") or DEFAULT_NS, cfg["name"]
    pvc_path = f"/api/v1/namespaces/{urllib.parse.quote(ns, safe='')}/persistentvolumeclaims/{urllib.parse.quote(name, safe='')}"
    pvc = kget(pvc_path)
    reps = cfg.get("replicas")
    if reps is not None:
        reps = int(reps)
        if not 1 <= reps <= 5:
            raise ValueError("replica count must be between 1 and 5")
    done = []
    if cfg.get("size_gb"):
        wanted = int(cfg["size_gb"])
        requested_mb = _quantity_mb(((pvc.get("spec") or {}).get("resources") or {})
                                    .get("requests", {}).get("storage", "0"))
        now_gb = -(-requested_mb // 1024) if requested_mb else 0
        if now_gb and wanted < now_gb:
            raise ValueError(f"{name} is {now_gb} GB; volumes can grow but not shrink")
        if wanted != now_gb:
            options = volume_edit_options(ns, name, pvc)
            if not options["can_expand"]:
                raise ValueError(options["reason"])
            STORAGE_RESIZE.check_upgrade(pvc, kget)
            ksend("PATCH", pvc_path,
                  {"spec": {"resources": {"requests": {"storage": f"{wanted}Gi"}}}},
                  ctype="application/merge-patch+json")
            done.append(f"growing to {wanted} GB")
    vol_name = pvc.get("spec", {}).get("volumeName")
    if reps is not None and vol_name:
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
            "User-Agent": f"Homestead/{HOMESTEAD_VERSION} (+https://github.com/homestead-lab/homestead)",
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
import homestead_env_secrets as ENVSEC
import homestead_stale_mount as STALE_MOUNT
import homestead_imports as IMP
import homestead_auth as AUTH
import homestead_longhorn as LH
import homestead_place as PLACE
import homestead_hardware as HW
import homestead_updates as UPDATES
import homestead_vmusage as VMUSAGE
import homestead_cancel as CANCEL
import homestead_joblogs as JOBLOGS
import homestead_console as CONSOLE
import homestead_files as FILES
import homestead_snapshot_files as SNAPSHOT_FILES
import homestead_icons as ICONS
import homestead_volumes as VOLUMES
import homestead_smart as SMART
import homestead_shares as SHARES
import homestead_nfs as NFS
import homestead_networking as NETWORK
import homestead_firewall as FIREWALL
import homestead_cluster as CLUSTER
import homestead_probe as PROBE
import homestead_objectstore as OBJECTS
import homestead_move as MOVE
import homestead_fleet as FLEET
import homestead_host_access as HOSTACCESS
HOSTACCESS.bind_targets(SYS_NS)
import homestead_signins as SIGNINS
import homestead_config_backup as CONFIG
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
import homestead_vips as VIPS
import homestead_helm as HELM
import homestead_mqtt as MQTT
import homestead_history as HISTORY
import homestead_platform as PLATFORM
import homestead_addons as ADDONS
import homestead_baseline as BASELINE
import homestead_components as COMPONENTS
import homestead_macvtap as MACVTAP
import homestead_resources as RESOURCES
import homestead_vms as VMS
import homestead_isos as ISOS
import homestead_vm_hardware as VM_HARDWARE
import homestead_lhcapacity as LHCAP
import homestead_lhrebuild as LHREBUILD
import homestead_power_hold as HOLD
import homestead_rebalance as REBALANCE
import homestead_container_rebalance as CREBALANCE
import homestead_disks as DISKS
import homestead_power as POWER
import homestead_privileges as PRIV
NAMES.bind(kget)
PROBE.bind(kget, ksend, DEFAULT_NS, AUTH.smart_signing_key)
def allocation_probe_capacity(obj, template):
    listing = kget("/api/v1/pods")
    if not isinstance(listing.get("items"), list) or (listing.get("metadata") or {}).get("continue"):
        raise ValueError("Complete workload inventory is unavailable; nothing was changed")
    return ALLOCATION_CAPACITY.plan(obj, template, PLACE.get_nodes(), listing["items"],
                                    get_app_settings()["thresholds"]["memory"]["critical"])


ALLOCATION_PROBE.bind(kget, ksend, DEFAULT_NS, allocation_probe_capacity)
OBJECTS.bind(kget, ksend, create_pvc, DEFAULT_NS)
MOVE.bind(kget, ksend, DEFAULT_NS, HOMESTEAD_VERSION)
# Linked clusters: each knows the others by the address they reach it at,
# which is its own VIP unless an admin says otherwise.
def fleet_address():
    """Homestead's web page on its VIP where it has one; else the address
    it started with (on k3s, a node's own)."""
    try:
        url = SELF_ADDRESS.report(cached("network", 5, NETWORK.inventory)).get("url") or ""
    except Exception:
        url = ""
    return url or (f"http://{LB_IP}:8088" if LB_IP else "")


FLEET.bind(kget, ksend, DEFAULT_NS, HOMESTEAD_VERSION,
           site=lambda: (cached("settings", 15, get_app_settings) or {}).get("site_name", ""),
           address=fleet_address, earlier=lambda: (f"http://{LB_IP}:8088",) if LB_IP else ())
MOVE.FLEET = FLEET
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
AUTH.bind(kget, _auth_ksend, DEFAULT_NS)
CAPACITY_REVIEW.bind(AUTH.review_signing_key)
LH.bind(kget, ksend, _cache, STORAGE_CLASS)
PLACE.bind(kget, ksend, lambda: cached("nodes", 5, get_nodes), _cache, HW.features)
POWER.bind(kget, PLACE.impact, LC.quorum_report, lambda: LC.NODE_POWER_ENABLED)
HOLD.bind(kget, ksend, (SELF.NS, NAMES.BRAND), POD_NAME)
REBALANCE.bind(kget, ksend, lambda: LEADER.is_leader())


def rebalance_move(ns, name, node):
    """One container of a rebalance: the manual move's capacity check and
    placement - a preference, not a pin - or a reason to leave it."""
    body = {"ns": ns, "name": name, "node": node, "pin": False}
    proposed, capacity, _ = move_capacity_plan(body)
    if capacity.get("blocked"):
        raise ValueError("; ".join(capacity.get("blockers") or []) or f"{node} cannot take it now")
    meta = proposed["metadata"]
    ksend("PUT", f"/apis/apps/v1/namespaces/{meta['namespace']}/deployments/{meta['name']}", proposed)
    PLACE._bust("wl", "ov", "flow", "nodes", "impact:")


CREBALANCE.bind(kget, PLACE.requirements, PLACE.satisfies, rebalance_move, (SELF.NS, NAMES.BRAND), lambda: LEADER.is_leader(),
                PLACE.get_nodes)
POWER.WORKER_GONE, POWER.RESUME, POWER.RESTORE, POWER.UNCORDON = power_worker_gone, resume_power_job, restore_held, allow_scheduling
UPDATES.bind(kget, ksend, DEFAULT_NS, DATA_DIR, SYS_NS, SMB_NAMESPACE,
             channel=lambda: cached("settings", 15, get_app_settings)["updates"]["channel"])
UPDATES.PART = homestead_part
UPDATES.VERSION = lambda: HOMESTEAD_VERSION
SMART.bind(kget, DEFAULT_NS, AUTH.smart_signing_key)
OPS.bind(kget, DATA_DIR, UPDATES.progress, SMART.progress)
OPS.cdi_send = ksend
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
OPS.RESOLVERS[REBALANCE.KIND] = REBALANCE.status
OPS.RESOLVERS[CREBALANCE.KIND] = CREBALANCE.status
OPS.CANCELLERS[CREBALANCE.KIND] = (CREBALANCE.cancel_plan, CREBALANCE.cancel_run)
OPS.CANCELLERS[REBALANCE.KIND] = (REBALANCE.cancel_plan, REBALANCE.cancel_run)
OPS.RESOLVERS["cluster-shutdown"] = lambda item: cluster_shutdown().progress(item)
OPS.RESOLVERS["vm-power"] = lambda item: VM_POWER_JOB.status(item, kget)
OPS.CANCELLERS["vm-power"] = (VM_POWER_JOB.cancel_plan, VM_POWER_JOB.cancel_run)
OPS.RESOLVERS[RENAME.KIND] = lambda item: RENAME.status(item, OPS)
OPS.CANCELLERS[RENAME.KIND] = (lambda item: RENAME.recovery_plan(item, kget, OPS),
                              lambda item, options: RENAME.recovery_run(item, options, kget, OPS))
OPS.CLEANUPS.add(RENAME.KIND)
for _kind in ("vm-create", "vm-edit"):
    OPS.RESOLVERS[_kind] = VM_MUTATION_JOB.status
    OPS.CANCELLERS[_kind] = (VM_MUTATION_JOB.cancel_plan, VM_MUTATION_JOB.cancel_run)
OPS.CANCELLERS["node-power"] = (lambda item: {"can": False, "why_not":
    "A host power command cannot be cancelled after it has been sent"}, lambda item, options: "")
def cleanup_restores():
    for cleanup in (cleanup_restore_classes, LH.cleanup_restore_snapshots):
        try:
            cleanup()
        except Exception as error:
            # Cleanup may be retried; it must not change a completed restore
            # into a failed data operation. The cleanup itself fails closed.
            print(f"Restore metadata cleanup deferred: {error}", flush=True)


MOVE_ENGINE.after_finish = cleanup_restores


def _restore_then_tidy(item, _resolve=OPS.RESOLVERS["volume-restore"]):
    """Resume snapshot setup after a restart and retain the selected class."""
    ref = item["ref"]
    cfg = ref.get("restore_config")
    if cfg and not ref.get("restore_started"):
        if item.get("status") == "queued":
            cfg["snapshot_wait_started"] = time.time()
        since = cfg.setdefault("snapshot_wait_started", time.time())
        if time.time() - float(since) > 15 * 60:
            raise ValueError("CSI snapshot setup did not become ready; repair snapshot support, then resume this restore")
        existing = LH._get_or_none(f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{ref['name']}")
        if existing:
            annotation = (existing.get("metadata", {}).get("annotations") or {}).get("homestead.io/restore-id")
            if annotation != cfg["restore_id"]:
                raise ValueError("The destination PVC was created by another operation")
            ref["restore_started"] = True
        else:
            created = LH.restore_backup(cfg)
            if not created["created"]:
                return "running", 2, created["message"]
            ref["restore_started"] = True
    pvc = LH._get_or_none(f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{ref['name']}")
    if pvc:
        if cfg and (pvc.get("metadata", {}).get("annotations") or {}).get("homestead.io/restore-id") != cfg["restore_id"]:
            raise ValueError("The destination PVC was replaced by another operation")
        problem = LH.restore_problem(pvc)
        if problem:
            return "failed", 8, f"CSI restore snapshot failed: {problem}"
    result = _resolve(item)
    if result and result[0] == "succeeded" and cfg and pvc:
        result = LH.finish_restore_resize(pvc, cfg) or result
    if result and result[0] in ("succeeded", "failed"):
        cleanup_restores()
    return result


OPS.RESOLVERS["volume-restore"] = _restore_then_tidy
OPS.RESUMABLE["volume-restore"] = lambda item: not bool(item.get("ref", {}).get("restore_config"))
UPGRADES.bind(kget)
PORTAL.bind(kget, ksend, DEFAULT_NS, lambda: cached("wl", 5, get_workloads),
            lambda source: ICONS.persist(source, DATA_DIR), lambda reference: ICONS.data_url(reference, DATA_DIR))
OPS.RESOLVERS["restructure"] = COPY_JOB.legacy_status
OPS.RESOLVERS[COPY_JOB.KIND] = lambda item: COPY_JOB.resolve(item, kget, ksend, OPS, copy_admission)
OPS.CANCELLERS[COPY_JOB.KIND] = (lambda item: COPY_JOB.recovery_plan(item, kget, OPS),
                                lambda item, options: COPY_JOB.recovery_run(item, options, kget, ksend, OPS))
OPS.CLEANUPS.update((COPY_JOB.KIND, "restructure"))
OPS.RESOLVERS[IMPORT_JOB.KIND] = lambda item: IMPORT_JOB.status(item, kget)
OPS.CANCELLERS[IMPORT_JOB.KIND] = (IMPORT_JOB.cancel_plan, IMPORT_JOB.cancel_run)
import homestead_reclass as RECLASS
import homestead_storage_admission as STORAGE_ADMISSION
import homestead_storage_recovery as STORAGE_RECOVERY
import homestead_storage_workflow as STORAGE_WORKFLOW
import homestead_storage_runtime as STORAGE_RUNTIME


def storage_restart_admission(item, proposals):
    return STORAGE_ADMISSION.plan(item, proposals, kget, PLACE.get_nodes(), get_app_settings()["thresholds"]["memory"]["critical"])


def storage_helper_admission(item, manifest):
    return copy_admission(manifest)


def storage_runtime_check():
    try:
        return STORAGE_RUNTIME.require(OPS, kget, SELF.NS, SELF.POD, NAMES.BRAND, HOMESTEAD_VERSION, DATA_DIR)
    except STORAGE_WORKFLOW.JOURNAL.Held:
        raise
    except Exception:
        raise STORAGE_WORKFLOW.JOURNAL.Held("Homestead replica compatibility could not be verified; restore cluster and shared-data access before moving storage") from None


def _storage_runtime_loop():
    # Every replica reports, not only the leader. No capability is inferred on
    # behalf of an older binary that does not understand the new journal.
    while True:
        try:
            STORAGE_RUNTIME.report(OPS, kget, SELF.NS, SELF.POD, NAMES.BRAND, HOMESTEAD_VERSION, DATA_DIR, self_data=True)
            beat("storage-runtime", 20)
        except Exception as error:
            beat("storage-runtime", 20, error)
        time.sleep(20)


def storage_move_progress(item):
    if "storage_protocol" in item.get("ref", {}):
        return STORAGE_WORKFLOW.resolve(item, OPS.checkpoint, storage_helper_admission, storage_restart_admission,
                                        runtime_check=storage_runtime_check)
    # Keep pre-upgrade jobs on their existing steps; never infer receipts for
    # mutations made by an older engine. Exempt only this job from its own fence.
    with STORAGE_GUARD.dispatching(item):
        return RECLASS.resolve(item)


def storage_volume_action(volume, action):
    # Snapshot rollback also stops/restarts workloads and writes through the
    # Longhorn REST API, not just ksend. Hold the fence around the entire step.
    with STORAGE_GUARD.volume(OPS, kget, volume):
        return action()


def storage_legacy_cancel(item, options):
    if "storage_protocol" in item.get("ref", {}):
        raise ValueError("Use the storage move recovery review; legacy rollback is not supported for this job")
    with OPS._lock, STORAGE_GUARD.dispatching(item):
        return CANCEL.reclass_cancel(item, options)


import homestead_vmstore as VMSTORE
import homestead_nodeshell as NODESHELL
import homestead_hostrun as HOSTRUN
import homestead_host_limits as HOST_LIMITS
import homestead_node_parity as NODE_PARITY
import homestead_host_os as HOST_OS
import homestead_root_guard as ROOT_GUARD
import homestead_os_rollout as OS_ROLLOUT
import homestead_passthrough as PASSTHROUGH
import homestead_self_address as SELF_ADDRESS
import homestead_host_bridge as HOST_BRIDGE
import homestead_manifests as MANIFESTS
import homestead_disk_setup as DISK_SETUP
import homestead_disk_v2 as DISK_V2
import homestead_hvimage as HVIMAGE
import homestead_revert as REVERT
OPS.RESOLVERS["reclass"] = storage_move_progress
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


def _diagnostic_read(path, timeout=5):
    req = urllib.request.Request(API + api_path(path), headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as response:
        raw = response.read(DIAGNOSTICS.MAX_SOURCE_BYTES + 1)
    if "/log?" in path:
        return raw.decode("utf-8", "replace")
    return json.loads(raw)


DIAGNOSTICS.bind(DATA_DIR, _diagnostic_read, OPS, HOMESTEAD_VERSION, _own_namespace(), os.environ.get("HOSTNAME", ""))
SHARED.bind(DATA_DIR)
LEADER.bind(kget, ksend, _own_namespace())
IPAM.bind(kget, ksend, DEFAULT_NS, lambda: cached("network", 5, NETWORK.inventory))
HELM.bind(kget, ksend)
MQTT.bind(kget, ksend, DEFAULT_NS, lambda: mqtt_snapshot(), LEADER.is_leader)
HISTORY.bind(DATA_DIR)
PLATFORM.bind(kget)
ADDONS.bind(kget, ksend, PLATFORM.detect, node_temps)
MACVTAP.bind(kget, ksend, ADDONS, PLATFORM.detect)
BASELINE.bind(kget, ADDONS, PLATFORM.detect, DEFAULT_NS, DATA_DIR, MACVTAP, PROBE, HOMESTEAD_VERSION)
COMPONENTS.bind(kget, ksend, PLATFORM.detect, lambda cfg: HELM.upgrade(cfg), ADDONS)
import homestead_lhv2_upgrade as LHV2_UPGRADE
LHV2_UPGRADE.bind(kget, ksend, PLATFORM.detect, COMPONENTS.longhorn_version, COMPONENTS.parse)
STORAGE_RESIZE.upgrade_guard = LHV2_UPGRADE.ensure_resize_idle
LC.vm_migration_guard = LHV2_UPGRADE.ensure_vm_idle
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
LHREBUILD.bind(kget, ksend, lambda: (get_app_settings().get("longhorn") or {}).get("offline_rebuilding", True))
import homestead_lhv2_setup as LHV2_SETUP
LHV2_SETUP.bind(kget, ksend, v2_engine_status, OPS, DEFAULT_NS)
OPS.RESOLVERS["longhorn-v2-prepare"] = LHV2_SETUP.progress
RECLASS.bind(kget, ksend, raw_get, storage_classes, LHCAP.status, _own_namespace())
REVERT.bind(kget, ksend, RECLASS, is_self)
NODESHELL.bind(kget, ksend, DEFAULT_NS)
HOSTRUN.bind(kget, ksend, lambda *a, **k: FILES._exec(*a, **k), DEFAULT_NS)
import homestead_host_console as HOST_CONSOLE
HOST_CONSOLE.bind(kget, HOSTRUN, OPS, HOMESTEAD_VERSION, DATA_DIR)
HOST_CONSOLE.platform = lambda: PLATFORM.detect()
OPS.RESOLVERS["host-console"] = HOST_CONSOLE.status
import homestead_unraid_vms as UNRAID_VMS
UNRAID_VMS.bind(IMP, kget, ksend, OPS, lambda: PLATFORM.detect())
OPS.RESOLVERS[UNRAID_VMS.KIND] = UNRAID_VMS.status
MANIFESTS.bind(kget, HOSTRUN, PLATFORM.detect, DATA_DIR)
HOST_LIMITS.bind(kget, HOSTRUN, PLATFORM.detect, DATA_DIR)
NODE_PARITY.bind(kget, ksend, HOSTRUN, PLATFORM.detect, node_temps, DATA_DIR, (SELF.NS, NAMES.BRAND))
HOST_OS.bind(kget, HOSTRUN, PLATFORM.detect, DATA_DIR)
OPS.RESOLVERS["host-os"] = HOST_OS.status
ROOT_GUARD.bind(kget, ksend, PLATFORM.detect, node_temps, DATA_DIR)
PASSTHROUGH.bind(kget, ksend, HOSTRUN, PLATFORM.detect, DATA_DIR)
SELF_ADDRESS.bind(kget, NETWORK, OBJECTS, SELF.NS, os.environ.get("PORT", "8080"), SMB_NAMESPACE, SMB_NAME)


def installer_vip(vip):
    """The VIP the installer was given: reserved, the apps' default, and
    Homestead's own services on it beside the nodes' addresses."""
    NETWORK.add_vips({"ip": vip, "label": "Homestead and apps"}, IPAM.load()[0].get("records") or {})
    NETWORK.set_default_vip(vip)
    moved = SELF_ADDRESS.move(vip)
    for key in ("network", "ov"):
        _cache.pop(key, None)
    _follow_fleet_address()
    return "; ".join(f"{s['label']}: {s['action']} ({s['detail']})" for s in moved["steps"]) or f"{vip} reserved"


def _follow_fleet_address():
    """Linked clusters reach this one on its VIP once it is there."""
    try:
        return FLEET.follow_address()
    except Exception as error:
        print(f"linked clusters: were not told the new address: {str(error)[:160]}", flush=True)
        return ""


BASELINE.vip_setup = installer_vip
OS_ROLLOUT.bind(kget, PLATFORM.detect, HOST_OS, rollout_reboot, operation_item, LC.set_cordon, own_node, DATA_DIR,
                lambda rollout: OPS.start("os-rollout", f"Update every host's OS ({len(rollout['nodes'])} hosts)",
                                          {"kind": "Node", "name": ", ".join(rollout["nodes"])[:200]}, "/nodes",
                                          {"rollout": rollout["id"]},
                                          "In the weekly window" if rollout["reason"] == "schedule" else "Starting"),
                lambda node, since: rollout_power_job(node, since))
OPS.RESOLVERS["os-rollout"] = OS_ROLLOUT.status
DISK_SETUP.bind(HOSTRUN)
HOST_BRIDGE.bind(HOSTRUN, kget, ksend)
OPS.RESOLVERS["host-bridge"] = HOST_BRIDGE.status
DISKS.setup_module = DISK_SETUP
DISK_SETUP.longhorn_block_paths = DISKS.longhorn_block_paths
PASSTHROUGH.longhorn_block_paths = DISKS.longhorn_block_paths
DISKS.autotag_state = lambda: os.path.join(DATA_DIR, "disk-autotags.json")


def _vm_image_disks():
    try:
        return {f"{row['namespace']}/{row['name']}": row.get("disks") or [] for row in IMP.vm_image_cache()["images"]}
    except Exception:
        return {}


VMSTORE.bind(kget, ksend, DEFAULT_NS, PLATFORM.detect,
             lambda url, display, reuse: HVIMAGE.download(kget, ksend, DEFAULT_NS, url, "", None, display, reuse),
             IMP.list_vm_images, _vm_image_disks,
             lambda ns, name: ksend("DELETE", f"/apis/harvesterhci.io/v1beta1/namespaces/{ns}/virtualmachineimages/{name}"))


def _vmstore_loop():
    """Kept VM images brought up to date, on the leader only; each image is
    asked about at most twice a day."""
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    VMSTORE.refresh()
                    # ISO copies no VM has used for a while (homestead_isos.py).
                    for name in ISOS.tidy():
                        print(f"ISO library: removed {name}, unused for {ISOS.keep_days()} days", flush=True)
                beat("vmstore", 3600, leader_only=True)
            except Exception as error:
                beat("vmstore", 3600, error, leader_only=True)
        time.sleep(3600)
OPS.RESOLVERS["snapshot-revert"] = lambda item: storage_volume_action(item["ref"]["volume"], lambda: REVERT.resolve(item))
SNAPSHOT_DELETE.bind(kget, ksend)
OPS.RESOLVERS["snapshot-delete"] = lambda item: storage_volume_action(item["ref"]["volume"], lambda: SNAPSHOT_DELETE.resume_resolve(item))
OPS.RESUMABLE["snapshot-delete"] = SNAPSHOT_DELETE.resumable
OPS.RESOLVERS["share-remove"] = SHARES.removal_progress
OPS.CANCELLERS["snapshot-revert"] = (REVERT.cancel_plan,
    lambda item, options: storage_volume_action(item["ref"]["volume"], lambda: REVERT.cancel_run(item, options)))
DISKS.bind(kget, ksend, node_temps)
DISK_V2.bind(kget, ksend, PLATFORM.detect, OPS, DEFAULT_NS, _diagnostic_read)
DISK_V2.protect_mutations(DISKS)
OPS.RESOLVERS[DISK_V2.KIND] = DISK_V2.progress
OPS.CANCELLERS[DISK_V2.KIND] = (DISK_V2.cancel_plan, DISK_V2.cancel_run)
OPS.LOGGERS[DISK_V2.KIND] = DISK_V2.logs
OPS.RESOLVERS["disk-retire"] = DISKS.retire_step
OPS.RESUMABLE["disk-retire"] = DISKS.retire_resumable
OPS.RESOLVERS["helm"] = HELM.job_status
OPS.RESOLVERS["multus"] = ADDONS.multus_progress


def delete_workload(ns, name):
    """A workload deleted, with every Service that points at it and its own
    LAN network; the Services removed are returned."""
    require_workload_target(ns, name)
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
    try:
        current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    except Exception:
        current = None
    ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    if current:
        try:
            ENVSEC.remove(ns, current, kget, ksend)  # its variables' Secret goes with it
        except Exception:
            pass
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
OPS.CANCELLERS["reclass"] = (CANCEL.reclass_plan, storage_legacy_cancel)
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
    take("addresses", lambda: VIPS.alert_facts(cached("network", 5, NETWORK.inventory).get("addresses")))
    take("capacity", lambda: LHCAP.alert_facts(cached("lhcap", 15, LHCAP.status)))
    take("detached-copies", lambda: LHREBUILD.alert_facts(cached("lhrebuild", 30, LHREBUILD.status)))
    take("disks", lambda: DISKS.alert_facts(cached("disks", 15, DISKS.inventory)))
    take("hostos", HOST_OS.alert_facts)
    take("rootguard", ROOT_GUARD.alert_facts)
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
    users = {u["name"] for u in AUTH.list_users()}
    urgent = any(e["severity"] == "critical" and e["phase"] in ("raised", "worsened") for e in fresh)
    visible = {user: ALERTS.for_user(fresh, user) for user in users}
    return PUSH.send(lambda row: row["user"] in users and any(
                         e["category"] in row["categories"] for e in visible[row["user"]]),
                     urgency="high" if urgent else "normal")


def alerts_pending(user, endpoint, confirm_delivery=False):
    """What a device has not been shown yet, for its service worker after a push."""
    row = PUSH.mine(user, endpoint) if endpoint else None
    wanted = set(row["categories"]) if row else set()
    active = len([a for a in ALERTS.active(user=user) if a.get("announced", 0) > 0 and not a["acknowledged"]])
    if not row:
        return {"alerts": [], "active": active, "known": False}
    got = ALERTS.log(after=row.get("cursor", 0), categories=wanted | {"test"}, limit=300)
    mine = PUSH.tag(endpoint)
    alerts = [a for a in got["alerts"] if a["category"] != "test" or a.get("to") == mine]
    # Replace old raises with their latest outcome before displaying a backlog.
    latest = {a["key"]: a for a in alerts}
    current = {a["key"]: a for a in ALERTS.active()}
    alerts = ALERTS.for_user([a for a in latest.values() if a.get("event") or a["category"] == "test"
                             or a["phase"] == "resolved" or a["id"] == current.get(a["key"], {}).get("announced")], user)
    if not confirm_delivery:  # Compatibility with already installed workers.
        PUSH.advance(user, endpoint, got["latest"])
    return {"alerts": alerts, "active": active, "known": True, "latest": got["latest"]}


def _alerts_loop():
    while True:
        # One replica raises alerts, or every notification arrives twice.
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    push_alerts(ALERTS.observe(_alert_sources()))
                beat("alerts", 20, leader_only=True)
            except Exception as error:
                beat("alerts", 20, error, leader_only=True)
                print(f"alerts: {str(error)[:160]}", flush=True)
        time.sleep(20)


def _vip_loop():
    """kube-vip can answer for an address and leave it off the Services that
    ask for it, and then every port there is refused. The leader records it
    for kube-vip, as it would have (homestead_vips.py)."""
    while True:
        if LEADER.is_leader():
            try:
                if VIPS.keep(PLATFORM.detect()):
                    with _lock:
                        _cache.pop("network", None)
                        _cache.pop("flow2", None)
                beat("vips", 30, leader_only=True)
            except Exception as error:
                beat("vips", 30, error, leader_only=True)
                print(f"VIPs: {str(error)[:160]}", flush=True)
        time.sleep(30)


def baseline_operation(row, verb):
    """The job-tray entry for installing kube-vip, Multus or macvtap, as Add-ons makes one."""
    name = BASELINE.NAMES[row["id"]]
    chart = MACVTAP.CHART if row["id"] == "macvtap" else ADDONS.CHARTS[row["id"]]
    return OPS.start("multus" if row["id"] == "multus" else "helm", f"{verb} {name}",
                     {"kind": "HelmChart", "name": chart, "namespace": ADDONS.CONTROLLER_NS},
                     "/settings", {"namespace": ADDONS.CONTROLLER_NS, "name": row["job"], "action": "install"},
                     "Waiting for the Helm controller")


def _baseline_loop():
    """What the installer asked Homestead to put under it - kube-vip and
    Multus on k3s and RKE2 - installed by the leader once the cluster can
    say what it has (homestead_baseline.py). Asked for once: nothing to do
    after that, so it checks rarely."""
    time.sleep(20)
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    for row in BASELINE.tick():
                        if row["ok"] and row.get("job"):
                            baseline_operation(row, "Install")
                        with _lock:
                            for key in ("helm", "platform", "baseline", "components"):
                                _cache.pop(key, None)
                beat("baseline", 300, leader_only=True)
            except Exception as error:
                beat("baseline", 300, error, leader_only=True)
                print(f"platform: {str(error)[:160]}", flush=True)
        time.sleep(60)


def weekly_trim():
    """Homestead's weekly trim job kept in place (homestead_longhorn), made
    once: remembered here, so one someone deleted is not made again."""
    path = os.path.join(DATA_DIR, "weekly-trim.json")
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    try:
        result = LH.ensure_weekly_trim(bool(state.get("created")), state.get("groups"))
    except Exception as error:
        return f"weekly trim not checked: {str(error)[:160]}"
    if result is None:
        return ""
    made, change, groups = result
    # Once someone has chosen its groups (groups is None), the last ones
    # Homestead wrote stay recorded, so it keeps telling theirs apart.
    if made or (groups is not None and groups != state.get("groups")):
        SHARED.write_json(path, {**state, "created": True, "groups": groups,
                                 "at": state.get("at") or int(time.time())}, durable=True)
    return change


import homestead_storage_pending as STORAGE_PENDING
STORAGE_PENDING.bind(kget, ksend)


def _storage_pending_loop():
    """On the leader, a node that has just joined is steered clear of until
    Longhorn is ready on it. Often, and cheap: the gap is a minute or two."""
    time.sleep(20)
    while True:
        if LEADER.is_leader():
            try:
                for node, change in STORAGE_PENDING.tick():
                    print(f"platform: {node}: {change}", flush=True)
                beat("storage-pending", 15, leader_only=True)
            except Exception as error:
                beat("storage-pending", 15, error, leader_only=True)
        time.sleep(15)


def _snapshot_files_loop():
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    require_self_data_write()
                    SNAPSHOT_FILES.cleanup()
                beat("snapshot-files", 30, leader_only=True)
            except Exception as error:
                beat("snapshot-files", 30, error, leader_only=True)
        time.sleep(30)


def _files_loop():
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    require_self_data_write()
                    FILES.cleanup()
                beat("volume-files", 30, leader_only=True)
            except Exception as error:
                beat("volume-files", 30, error, leader_only=True)
        time.sleep(30)


def _host_console_loop():
    """On the leader, the host console add-on brought to its setting, a host
    or two at a time. Its own loop: each host can take minutes, and the
    host fixes (the root filesystem's guard among them) do not wait for it."""
    time.sleep(90)
    while True:
        if LEADER.is_leader():
            try:
                for node, change in HOST_CONSOLE.tick():
                    print(f"platform: {node}: {change}", flush=True)
                beat("host-console", 600, leader_only=True)
            except Exception as error:
                beat("host-console", 600, error, leader_only=True)
        time.sleep(600)


def _longhorn_copies_loop():
    """On the leader, every minute: Longhorn's default copies follow the
    number of Ready nodes as they join (homestead_node_parity.copies_tick),
    before apps made right after are given one copy."""
    time.sleep(30)
    while True:
        if LEADER.is_leader():
            try:
                note = NODE_PARITY.copies_tick()
                if note:
                    print(f"platform: {note}", flush=True)
            except Exception as error:
                print(f"platform: Longhorn copies not checked: {str(error)[:160]}", flush=True)
        time.sleep(60)


def _host_fix_loop():
    """On the leader, what k3s and RKE2 hosts need or undo at each start: inotify
    limits a busy node outgrows (homestead_host_limits.py), what a node that
    joined later needs to match the others (homestead_node_parity.py), the installer's
    auto-deploy files (homestead_manifests.py), and a second default storage
    class; and VMs still holding an ISO read-only. Checked a minute after
    starting, then every ten minutes."""
    time.sleep(60)
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    for node, change in HOST_LIMITS.tick().items():
                        print(f"platform: {node}: inotify limits raised ({change})", flush=True)
                    # A node that joined later, set up as the others are.
                    for node, change in NODE_PARITY.tick():
                        print(f"platform: {node + ': ' if node else ''}{change}", flush=True)
                    # Each host's OS - updates, restarts, failed services - every six hours.
                    HOST_OS.tick()
                    # Restored volumes whose class an older release removed.
                    try:
                        repaired = repair_restore_classes()
                        if repaired:
                            print(f"storage: restore classes put back for resizing: {', '.join(repaired)}", flush=True)
                    except Exception as error:
                        print(f"storage: restore classes not checked: {str(error)[:160]}", flush=True)
                    # Linked clusters told this Homestead's address, once it is on its VIP.
                    try:
                        moved = FLEET.follow_address()
                        if moved:
                            print(f"linked clusters: told this Homestead is at {moved}", flush=True)
                    except Exception as error:
                        print(f"linked clusters: address not checked: {str(error)[:160]}", flush=True)
                    # A weekly trim for every Longhorn volume, unless someone removed it.
                    trimmed = weekly_trim()
                    if trimmed:
                        print(f"storage: {trimmed}", flush=True)
                    # Longhorn kept from filling a host's root filesystem.
                    for node, change in ROOT_GUARD.tick():
                        print(f"storage: {node}: {change}", flush=True)
                    for node, marked in MANIFESTS.tick().items():
                        print(f"platform: {node}: k3s no longer re-applies "
                              f"{', '.join(marked) or 'no installer files (none left)'} at start", flush=True)
                    for node, disk, tags in DISKS.auto_tag():
                        print(f"storage: {node} {disk} tagged {', '.join(tags)}", flush=True)
                    # VMs made before 2.8.228 hold their ISO read-only and cannot start.
                    for name in ISOS.unlock():
                        print(f"ISO library: {name} no longer holds its ISO read-only", flush=True)
                    fixed = reconcile_default_class()
                    if fixed:
                        print(f"storage: {fixed[0]} kept as the default class; "
                              f"{', '.join(fixed[1])} no longer default", flush=True)
                        with _lock:
                            _cache.pop("classes", None)
                beat("host-fixes", 600, leader_only=True)
            except Exception as error:
                beat("host-fixes", 600, error, leader_only=True)
                print(f"platform: {str(error)[:200]}", flush=True)
        time.sleep(600)


def _os_updates_loop():
    """OS updates across the hosts (homestead_os_rollout.py), on the leader:
    Ubuntu's automatic updates held off or let go as the settings say, the
    weekly window opened, and a running update moved on a step. Its state is
    on disk, so a new leader carries on where the last one was."""
    time.sleep(45)
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    moved = OS_ROLLOUT.tick()
                if moved:
                    print(f"OS updates: {moved.get('message', '')[:200]}", flush=True)
                beat("os-updates", 20, leader_only=True)
            except Exception as error:
                beat("os-updates", 20, error, leader_only=True)
                print(f"OS updates: {str(error)[:200]}", flush=True)
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


class MissingParameter(ValueError):
    """A route asked for without what it needs: 400, saying which."""


class Query(dict):
    """A request's query string. A route reading a parameter it was not
    given gets a 400 naming it, not a 500 from a bare KeyError."""
    def __missing__(self, key):
        raise MissingParameter(f"missing parameter: {key}")


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
    # Do not request RWX merely because a non-Longhorn class has no
    # migratable flag. k3s local-path (and many block CSI drivers) cannot
    # provision it. Unknown drivers get the conservative single-node mode.
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


def self_data_handoff_status(operation):
    """Read only the control record identified by this installation's receipt.

    A missing marker is not permission to discover/adopt a same-name operation.
    This read-only route never invokes the legacy move resolver or job polling.
    """
    if not re.fullmatch(r"[a-f0-9]{24}", operation):
        return None
    try:
        if _self_data_fence is not None:
            try:
                recovery = _self_data_fence.recovery()
                if recovery["operation"] == operation and not recovery["writable"]:
                    view = SELF_DATA_WORKER.unavailable_status(operation)
                    view.update(status="preparing", can_abandon=True, requires_review=False,
                        message="Homestead is still on its original volume. You can wait for preparation or abandon it safely before the move starts.")
                    job = next((i for i in OPS._read() if i.get("kind") == SELF_DATA_EXECUTE.KIND and i.get("ref", {}).get("operation") == operation), None)
                    error = (job or {}).get("ref", {}).get("setup_error")
                    if error:
                        view.update(status="held", requires_review=True, message=error)
                    return view
            except Exception:
                pass
        completed = SELF_DATA_FINISH.read(DATA_DIR, SELF.NS, NAMES.BRAND)
        if completed and completed[1].state["operation"] == operation:
            return SELF_DATA_WORKER.progress(completed[1], time.time())
        marker = SELF_DATA_FENCE.read_marker(DATA_DIR)
        if not marker:
            job = next((i for i in OPS._read() if i.get("kind") == SELF_DATA_EXECUTE.KIND and i.get("ref", {}).get("operation") == operation), None)
            if job:
                ref = job["ref"]
                if not ref.get("anchor_uid") or ref.get("setup_error"):
                    view = SELF_DATA_WORKER.unavailable_status(operation)
                    # Setup stopped before anything was handed over: Homestead is
                    # running on its original volume, and the preparation can be
                    # given up here, as after a restart.
                    view.update(status="held" if ref.get("setup_error") else "preparing", requires_review=bool(ref.get("setup_error")),
                        can_abandon=bool(ref.get("setup_error") and ref.get("anchor_uid")), live=True,
                        message=("Setup stopped; Homestead is still on its original volume. " + ref["setup_error"]) if ref.get("setup_error")
                            else "Preparing the move. Homestead is still online.")
                    return view
                anchor = SELF_DATA_FENCE.A.Anchor(kget, None, SELF.NS, NAMES.BRAND)
                anchor.load(operation=operation, uid=ref["anchor_uid"])
                return SELF_DATA_WORKER.progress(anchor, time.time())
        if not marker or (marker["namespace"], marker["deployment"], marker["operation"]) != (SELF.NS, NAMES.BRAND, operation):
            return None
        anchor = SELF_DATA_FENCE.A.Anchor(kget, None, SELF.NS, NAMES.BRAND)
        anchor.load(operation=operation, uid=marker["anchor_uid"])
        return SELF_DATA_WORKER.progress(anchor, time.time())
    except Exception:
        return SELF_DATA_WORKER.unavailable_status(operation)


def abandon_self_data_preparation(body):
    if _self_data_fence is None:
        raise SELF_DATA_FENCE.Held("Data-move recovery is unavailable")
    recovery = _self_data_fence.recovery()
    if body.get("operation") != recovery["operation"]:
        raise SELF_DATA_FENCE.Held("The preparation changed; reload its status")
    anchor = SELF_DATA_FENCE.A.Anchor(kget, _ksend, SELF.NS, NAMES.BRAND).load(operation=recovery["operation"], uid=recovery["anchor_uid"])
    if not anchor.state.get("setup_aborted"):
        anchor.abort_setup()  # CAS disarms the publisher; never clears a published handoff.
    if not _self_data_boot_pending:
        threading.Thread(target=finish_self_data_helpers, name="data-move-cleanup", daemon=True).start()
    return {"ok": True, "message": "Preparation abandoned. Both volumes are retained."}


def cluster_shutdown(review=False):
    image = _self_data_helper_image(kget, SELF.NS)[1] if review else ""
    def busy():
        with OPS._lock:
            return ["Finish or recover job: " + j["title"] for j in SELF_DATA_PREPARE.blocking_jobs(OPS)
                    if j["kind"] != "cluster-shutdown"]
    return CLUSTER_SHUTDOWN.Shutdown(kget, lambda *a, **kw: _ksend(*a, **kw, shutdown_bypass=True), SELF.NS, SELF.POD, image,
                                      enabled=LC.NODE_POWER_ENABLED, busy=busy)


def _self_data_helper_image(read, ns):
    pod_name = _dns_name(SELF.POD, "Homestead pod")
    pod = read(f"/api/v1/namespaces/{ns}/pods/{pod_name}")
    containers = [c for c in pod.get("spec", {}).get("containers", []) if c.get("name") == NAMES.BRAND]
    statuses = [c for c in pod.get("status", {}).get("containerStatuses", []) if c.get("name") == NAMES.BRAND]
    if len(containers) != 1 or len(statuses) != 1 or not statuses[0].get("ready") or not statuses[0].get("state", {}).get("running"):
        raise SELF_DATA_FENCE.Held("The running Homestead image is not ready to be used for this move")
    digest_match = re.search(r"(?:^|@|://)(sha256:[a-f0-9]{64})$", statuses[0].get("imageID", ""))
    if not digest_match:
        raise SELF_DATA_FENCE.Held("The running Homestead image digest is unavailable; wait for it before reviewing")
    return pod, UPDATES._immutable(containers[0]["image"], digest_match.group(1))


def _require_no_data_handoff(read, ns):
    try:
        read(f"/api/v1/namespaces/{ns}/configmaps/{NAMES.BRAND}-data-handoff")
    except urllib.error.HTTPError as error:
        if error.code == 404: return
        raise
    raise SELF_DATA_FENCE.Held("An earlier data move record exists; review it before starting another move")


def self_data_preparation(body, actor, *, start=False):
    """Review or enqueue destination preparation; never switches the app PVC."""
    try:
        # No resolver polling in review. Execution recomputes under the shared
        # operations lock, before recording any preparation intent.
        def reviewed():
            _require_no_data_handoff(kget, SELF.NS)
            info = homestead_data_volume()
            row = next((r for r in info.get("classes", []) if r["name"] == body.get("storage_class")), None)
            if row is None: raise SELF_DATA_FENCE.Held("Choose an available destination storage class")
            _, image = _self_data_helper_image(kget, SELF.NS)
            return SELF_DATA_PREPARE.review(kget, SELF.NS, NAMES.BRAND, body, actor=actor, image=image,
                access_mode="ReadWriteMany" if row["shareable"] else "ReadWriteOnce",
                threshold=get_app_settings()["thresholds"]["memory"]["critical"], clock=time.time)
        if not start:
            cfg, public, context, _ = reviewed()
            return {**public, "capacity_token": CAPACITY_REVIEW.issue(cfg, context)}
        with OPS._lock:
            result = reviewed()
            # Cleanup may have finished before Jobs' next refresh. Reconcile
            # only the verified completed handoff; do not advance other jobs.
            SELF_DATA_EXECUTE.reconcile_completed(OPS, DATA_DIR, SELF.NS, NAMES.BRAND)
            operation = SELF_DATA_PREPARE.start(body, result, OPS)
            return {"ok": True, "operation": operation, "destination": result[3]["destination"],
                    "detail": "Preparing the new volume. Homestead stays on its original data until you confirm the final move."}
    except SELF_DATA_FENCE.Held:
        raise
    except Exception:
        raise SELF_DATA_FENCE.Held("Destination preparation is unavailable. Inspect jobs before retrying; no data volume was deleted") from None


def self_data_preparation_progress(item):
    try:
        return SELF_DATA_PREPARE.resolve(item, kget, ksend, OPS.checkpoint, clock=time.time)
    except SELF_DATA_FENCE.Held as error:
        item["ref"]["retain_resources"] = True
        return "failed", item.get("progress", 0), str(error)


def self_data_preparation_state():
    """Settings can reopen preparation without advancing any job or exposing refs."""
    try:
        info = homestead_data_volume()
        nodes = SELF_DATA_PREPARE.D._inventory(kget, "/api/v1/nodes")
        jobs = []
        for item in OPS._read():
            ref = item.get("ref", {})
            if (item.get("kind") != SELF_DATA_PREPARE.KIND or ref.get("namespace") != SELF.NS
                    or OPS._archived_preparation(item)):
                continue
            reusable = (item["status"] == "succeeded" and bool(ref.get("prepared")) and
                        ref["source"]["name"] == info.get("pvc") and ref["destination"] != info.get("pvc"))
            message = item.get("message", "")
            if item["status"] == "succeeded" and ref.get("prepared") and not reusable:
                message = ("Homestead currently uses this volume. The preparation is complete." if ref["destination"] == info.get("pvc")
                           else "Prepared for an earlier source volume. It cannot move Homestead's current data; the volume is retained.")
            jobs.append({"id": item["id"], "operation": ref["operation"], "destination": ref["destination"],
                "source": ref["source"]["name"], "storage_class": ref["storage_class"], "node": ref["node"],
                "status": item["status"], "progress": item.get("progress", 0), "message": message, "prepared": reusable,
                "archivable": SELF_DATA_PREPARE.can_archive(item)})
        return {"source": info.get("pvc"), "classes": info.get("classes", []), "preparations": jobs,
            "execution_ready": True, "blocking_jobs": SELF_DATA_PREPARE.blocking_jobs(OPS),
            "nodes": [{"name": n["metadata"]["name"], "ready": not n["metadata"].get("deletionTimestamp")
                and not n.get("spec", {}).get("unschedulable") and any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in n.get("status", {}).get("conditions", []))} for n in nodes]}
    except Exception:
        raise SELF_DATA_FENCE.Held("Data preparation status is unavailable. Existing jobs and volumes have not been changed") from None


OPS.RESOLVERS[SELF_DATA_PREPARE.KIND] = self_data_preparation_progress
OPS.CANCELLERS[SELF_DATA_PREPARE.KIND] = (SELF_DATA_PREPARE.cancel_plan, SELF_DATA_PREPARE.cancel_run)


def preview_self_data_move(body, actor):
    """Read-only final review for a prepared, bound destination.

    Provisioning and execution are connected separately. Never accept a worker
    image, capacity receipt, actor or cluster identity from the request body.
    """
    try:
        # Read the saved job snapshot, not list_operations(): polling resolvers
        # can change workloads or write job history during an alleged preview.
        blockers = SELF_DATA_PREPARE.blocking_jobs(OPS)
        if blockers:
            raise SELF_DATA_FENCE.Held(SELF_DATA_PREPARE.blocked_message("Finish running jobs and review retained recovery jobs before moving Homestead's data", blockers))
        ns = SELF.NS
        cache = {}
        def read(path):
            if path not in cache: cache[path] = copy.deepcopy(kget(path))
            return copy.deepcopy(cache[path])
        _require_no_data_handoff(read, ns)
        pod, image = _self_data_helper_image(read, ns)
        def runtime_check(pods, claim):
            return STORAGE_RUNTIME.require_self_data(OPS, pods, ns, NAMES.BRAND, HOMESTEAD_VERSION, claim,
                own_uid=pod["metadata"]["uid"], data_mount=DATA_DIR)
        result = SELF_DATA_REVIEW.Review(read, ns, NAMES.BRAND, actor=actor, image=image,
            threshold=get_app_settings()["thresholds"]["memory"]["critical"], source_pod=pod, data_dir=DATA_DIR,
            runtime_check=runtime_check, clock=time.time, route_check=SELF_DATA_ROUTE.review).preview(body)
        return result
    except SELF_DATA_FENCE.Held:
        raise
    except Exception:
        raise SELF_DATA_FENCE.Held("The data move review is unavailable. Nothing was changed; check cluster access and volume readiness") from None


def start_self_data_move(body, actor):
    """Persist the approved intent before launching the one-shot source setup."""
    if _self_data_barrier is None:
        raise SELF_DATA_FENCE.Held("Data moves require the mounted-data safety guard")
    ns = SELF.NS
    def reviewer(read):
        pod, image = _self_data_helper_image(read, ns)
        def runtimes(pods, claim):
            return STORAGE_RUNTIME.require_self_data(OPS, pods, ns, NAMES.BRAND, HOMESTEAD_VERSION, claim,
                own_uid=pod["metadata"]["uid"], data_mount=DATA_DIR)
        return SELF_DATA_REVIEW.Review(read, ns, NAMES.BRAND, actor=actor, image=image,
            threshold=get_app_settings()["thresholds"]["memory"]["critical"], source_pod=pod, data_dir=DATA_DIR,
            runtime_check=runtimes, clock=time.time, route_check=SELF_DATA_ROUTE.review)
    with OPS._lock:
        _require_no_data_handoff(kget, ns)
        SELF_DATA_EXECUTE.idle(OPS)
        execution = reviewer(kget).approve(body)
        job = SELF_DATA_EXECUTE.enqueue(OPS, execution)
    def recheck(approved, worker):
        def read(path):
            result = kget(path)
            if path == "/api/v1/pods":
                # Re-evaluate the same proposal, not two copies of our already
                # running helper. Observed node memory is never subtracted.
                result = {**result, "items": [p for p in result["items"] if p["metadata"]["uid"] != worker["uid"]]}
            return result
        _, _, binding, _ = reviewer(read)._snapshot(approved["config"])
        return SELF_DATA_REVIEW.recheck_binding(approved["binding"], binding)
    # Setup owns only its new anchor and helpers; the raw transport allows the
    # final anchor acknowledgement after local write fencing. No source writes
    # or Deployment changes are allowed through this callback after publication.
    task = SELF_DATA_EXECUTE.Execution(kget, _ksend, OPS, execution, job, directory=DATA_DIR,
        barrier=_self_data_barrier, recheck=recheck)
    try:
        threading.Thread(target=task.run, name="self-data-setup", daemon=True).start()
    except Exception:
        task.checkpoint(setup_error="The move setup could not start. Inspect the saved job before trying again.")
        raise SELF_DATA_FENCE.Held("Move setup could not start; no data was copied or switched") from None
    return {"ok": True, "operation": job, "handoff": execution["scope"].operation}, execution["status_token"]


OPS.RESOLVERS[SELF_DATA_EXECUTE.KIND] = lambda item: SELF_DATA_EXECUTE.resolve(item, kget, directory=DATA_DIR)


def move_homestead_data(storage_class):
    raise SELF_DATA_FENCE.Held("The live-copy mover was retired. Open Settings to prepare and confirm a verified data move.")


def _data_move_status(item):
    # Old jobs lack pre-write receipts and a quiesced-copy proof. Reading one
    # after an upgrade must not silently switch claims or report safe success.
    item["ref"]["retain_resources"] = True
    return "failed", 0, "This older data move needs manual review. Both volumes are retained; no copy, restart or claim switch was requested."


OPS.RESOLVERS["self-data-move"] = _data_move_status


LOOP_WORDS = {"sampler": "Live charts", "alerts": "Alerts and notifications", "history": "Long-term stats", "host-fixes": "Host fixes", "host-console": "Host console add-on", "storage-pending": "New nodes held until their storage is ready", "os-updates": "OS updates", "baseline": "Platform installs", "vips": "VIP keeper",
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
            in_sync = (all(SHARES.SPECS.same(container.get(field), expected_container.get(field))
                           for field in ("args", "env", "volumeMounts"))
                       and live_spec.get("volumes", []) == expected_spec.get("volumes", [])
                       and SHARES.SPECS.same(live_spec.get("initContainers", []), expected_spec.get("initContainers", []))
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
FIREWALL.bind(kget, ksend, PLATFORM.detect, _own_namespace())
VIPS.bind(kget, ksend)


def _config_restored(parts):
    """A restored part is read afresh everywhere it is cached."""
    _cache.clear()
    AUTH._store_cache.update(at=0)
    HW._cache.clear()
    IMP._cache.clear()


SIGNINS.bind(DATA_DIR)
import homestead_api_keys as API_KEYS
import homestead_api_v1 as API_V1
import homestead_setup as SETUP
API_KEYS.bind(DATA_DIR)
SETUP.bind(DATA_DIR)
API_V1.bind(nodes=lambda: cached("nodes", 5, get_nodes), workloads=lambda: cached("wl", 5, get_workloads),
            vms=lambda: cached("vms", 5, VMS.list_vms),
            alerts=lambda: [a for a in ALERTS.active() if a.get("announced", 0) > 0],
            jobs=OPS.snapshot, scale=api_scale, restart=restart_workload, vm_power=api_vm_power,
            version=lambda: HOMESTEAD_VERSION)


# Homestead's own configuration, part by part, for its backup and restore.
# Linked clusters are not a part: restoring an old shared key cuts the links.
CONFIG.bind(kget, ksend, HOMESTEAD_VERSION, [
    {"id": "settings", "label": "Settings", "detail": "Site name, health thresholds, update policy, App Store feed",
     "objects": [("configmaps", DEFAULT_NS, _settings_map())]},
    {"id": "users", "label": "Users and roles", "detail": "Every account, its role and password",
     "caution": "Replaces every account and password with the backup's, and signs everyone out - sign in again with an account from the backup.",
     "default": False, "objects": [("secrets", DEFAULT_NS, AUTH.SECRET_NAME())]},
    {"id": "hardware", "label": "Hardware features", "detail": "Device mappings: iGPU, Coral, USB and the rest",
     "objects": [("configmaps", DEFAULT_NS, HW._hardware_map())]},
    {"id": "vips", "label": "VIPs", "detail": "Your saved VIPs, their labels and the default workload VIP",
     "objects": [("configmaps", DEFAULT_NS, NETWORK.VIP_MAP)]},
    {"id": "ipam", "label": "IP addresses", "detail": "Subnets, documented addresses, and the UniFi connection",
     "objects": [("configmaps", DEFAULT_NS, IPAM._map()), ("secrets", DEFAULT_NS, IPAM._secret())]},
    {"id": "mqtt", "label": "MQTT", "detail": "The broker, its credentials and what is published",
     "objects": [("configmaps", DEFAULT_NS, MQTT._map()), ("secrets", DEFAULT_NS, MQTT._secret())]},
    {"id": "portal", "label": "Portal", "detail": "Its sections and tiles",
     "objects": [("configmaps", DEFAULT_NS, PORTAL._map())]},
    {"id": "shares", "label": "Network shares", "detail": "Shares, their options, and SMB users",
     "caution": "Brings back share definitions and SMB users; the volumes they point at must still exist.",
     "objects": [("configmaps", SMB_NAMESPACE, SHARES.CONFIGMAP()), ("secrets", SMB_NAMESPACE, SHARES.SECRET())]},
    {"id": "sources", "label": "Import sources", "detail": "Unraid and Docker hosts to import from",
     "objects": [("configmaps", DEFAULT_NS, IMP._sources_map())]},
    {"id": "vmstore", "label": "VM image store", "detail": "The cloud images kept, and whether they refresh",
     "objects": [("configmaps", DEFAULT_NS, VMSTORE.CONFIGMAP)]},
], site=lambda: (cached("settings", 15, get_app_settings) or {}).get("site_name", ""),
    after_restore=_config_restored)
CLUSTER.bind(kget, SYS_NS, lambda: cached("nodes", 5, get_nodes))
CONSOLE_PROXY = CONSOLE.ConsoleProxy(API, TOKEN, CTX, DATA_DIR, SYS_NS, {DEFAULT_NS}, kget)
VM_CONSOLE = VMCONSOLE.VmConsole(CONSOLE_PROXY, SYS_NS, kget)
FILES.bind(kget, ksend, urllib.parse.urlparse(API), TOKEN, CTX, SYS_NS)
SNAPSHOT_FILES.bind(kget, ksend, lambda: _self_data_helper_image(kget, SELF.NS)[1], SYS_NS)


ISO_CLASS = NAMES.object_name("isos")


def iso_storage_class():
    """Where ISO volumes go. On Longhorn, a class of Homestead's own: one
    replica, since the original is on the share and a lost copy is made again
    in one click, and no data locality, so the replica is not moved towards
    whichever node serves it. Like the class it is copied from it serves
    ReadWriteMany, so VMs on any node read the one copy. Elsewhere, a class
    every node can mount; else the VM default, one node at a time."""
    rows = storage_classes()
    shared = shared_storage_classes(rows)
    base = STORAGE_CLASS if STORAGE_CLASS in shared else (shared[0] if shared else "")
    row = next((r for r in rows if r["name"] == base), None)
    if row and row["provisioner"] == "driver.longhorn.io":
        if not any(r["name"] == ISO_CLASS for r in rows):
            parameters = {key: value for key, value in (row["parameters"] or {}).items()
                          if key not in ("migratable", "backingImage", "backingImageDataSourceType",
                                         "backingImageDataSourceParameters", "recurringJobSelector")}
            parameters.update(numberOfReplicas="1", dataLocality="disabled")
            body = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
                    "metadata": {"name": ISO_CLASS, "labels": {NAMES.key("managed"): "true"},
                                 "annotations": {"field.cattle.io/description":
                                                 "Homestead's ISO copies: one replica; the originals are on the shares"}},
                    "provisioner": "driver.longhorn.io", "parameters": parameters,
                    "reclaimPolicy": "Delete", "allowVolumeExpansion": True, "volumeBindingMode": "Immediate"}
            try:
                ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", body)
            except urllib.error.HTTPError as error:
                if error.code != 409:      # another replica made it first
                    raise
        return ISO_CLASS, True
    if base:
        return base, True
    return vm_default_class(rows), False


ISOS.bind(kget, ksend, SHARES.list_shares, FILES.list_files, iso_storage_class, SHARES._samba_node,
          SMB_NAMESPACE, DEFAULT_NS, FILES.IMAGE)
VMS.iso_ready = ISOS.ready_volume
IMP.iso_ready = ISOS.ready_volume


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
    # Variables kept in the workload's own Secret are edited like any other.
    kept, own_secret = ENVSEC.values(ns, deployment, kget), ENVSEC.own(deployment)
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
        own_values = kept.get(container.get("name"), {})
        literals = {item["name"]: item.get("value", "") if "valueFrom" not in item else own_values[item["name"]]
                    for item in container.get("env", []) or []
                    if item.get("name") and ("valueFrom" not in item or item["name"] in own_values)}
        refs = [{"name": item["name"], "source": env_reference(item)}
                for item in container.get("env", []) or []
                if item.get("name") and item.get("valueFrom") and item["name"] not in own_values]
        requests = (container.get("resources", {}) or {}).get("requests", {}) or {}
        limits = (container.get("resources", {}) or {}).get("limits", {}) or {}
        containers.append({
            "original_name": container.get("name", ""), "name": container.get("name", ""),
            "image": container.get("image", ""), "cpu": requests.get("cpu", ""), "memory": requests.get("memory", ""),
            "memory_limit": limits.get("memory", ""),
            "env": literals, "env_refs": refs, "secret_env": sorted(own_values),
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
    "/schedules", "/import", "/vms/import", "/events", "/networking", "/system/cluster", "/settings", "/setup",
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
    return (not clean.startswith("/api/") and clean not in ("/api", "/healthz")
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
          "/api/auth/login", "/api/auth/state", "/api/auth/setup",
          # Back to this cluster from one that stopped answering: it only
          # forgets which linked cluster this browser was looking at.
          "/api/fleet/home"}


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

# Policies are explicitly declared in homestead_route_policy.py. These sets
# document the sensitive routes and the user-owned account routes.
# Enforced here, server-side. The UI hides what you cannot do as a courtesy,
# but a viewer who hand-crafts the request still gets a 403.
ADMIN_ROUTES = {
    "/api/firewall/preview", "/api/firewall/save", "/api/firewall/delete",
    "/api/disks/v2/plan", "/api/disks/v2/start", "/api/disks/v2/status",
    "/api/disks/v2/prepare-review", "/api/disks/v2/prepare",
    "/api/longhorn/v2/plan", "/api/longhorn/v2/prepare", "/api/longhorn/v2/enable",
    "/api/auth/users", "/api/auth/users/delete", "/api/auth/role",
    # API keys: made, listed and revoked by administrators only.
    "/api/auth/keys", "/api/auth/keys/revoke",
    # The setup guide's checks that reach out, and its first opening.
    "/api/setup/https-check", "/api/setup/opened",
    # Who signed in, from where: other people's addresses and devices.
    "/api/auth/history",
    "/api/node/power", "/api/node/drain", "/api/node/cordon", "/api/node/hardware",
    "/api/sources", "/api/sources/delete", "/api/sources/browse",
    "/api/sources/scan", "/api/sources/trust",
    "/api/sources/containers", "/api/sources/inspect", "/api/sources/measure",
    # A server's VMs, one shut down there, and one copied across.
    "/api/sources/vms", "/api/sources/vms/shutdown", "/api/vms/import-unraid",
    "/api/import", "/api/import/preview", "/api/imports/delete", "/api/imports/cleanup-plan",
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
    "/api/shares/repair", "/api/shares/users", "/api/shares/users/delete",
    # Carrying a stopped job on runs its remaining steps - a swap, for one.
    "/api/operations/resume",
    "/api/operations/power-recovery/preview", "/api/operations/power-recovery/resolve",
    "/api/operations/vm-recovery/preview", "/api/operations/vm-recovery/resolve",
    "/api/operations/storage-recovery/preview", "/api/operations/storage-recovery/act",
    "/api/network/vips/add", "/api/network/vips/remove", "/api/network/vips/label", "/api/network/vips/default", "/api/network/vm-networks",
    "/api/files/list", "/api/files/read", "/api/files/write", "/api/files/close",
    "/api/snapshot-files/plan", "/api/snapshot-files/status", "/api/snapshot-files/list",
    "/api/snapshot-files/download", "/api/snapshot-files/start", "/api/snapshot-files/close",
    "/api/node/smart/test",
    # Installing the probe stands a privileged container on every node.
    "/api/node/probe/install", "/api/node/probe/remove",
    "/api/node/probe/allocation", "/api/node/probe/allocation/check",
    # Object storage holds every backup, and its keys.
    "/api/objectstore/deploy", "/api/objectstore/longhorn", "/api/objectstore/remove",
    # Starting or stopping the store that lets workloads move out.
    "/api/objectstore/transfers", "/api/move/clusters/transfers",
    # A cluster's credentials, and what they reach.
    "/api/move/clusters/add", "/api/move/clusters/remove", "/api/move/remote",
    "/api/move/clusters/check", "/api/move/clusters/readiness", "/api/move/clusters/storage", "/api/move/clusters/target",
    # Homestead's configuration: every password hash and key it holds.
    "/api/config/parts", "/api/config/backup", "/api/config/inspect", "/api/config/restore",
    # Linking clusters hands every linked Homestead admin over this one.
    "/api/fleet/join", "/api/fleet/accept", "/api/fleet/remove", "/api/fleet/leave",
    "/api/fleet/address", "/api/fleet/sync", "/api/fleet/link-legacy",
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
    "/api/addons/multus", "/api/addons/multus/repair", "/api/addons/kube-vip",
    "/api/platform/baseline/install",
    # Which share folders the ISO library reads, and deleting an ISO's volume.
    "/api/vm/isos/folders", "/api/vm/isos/delete", "/api/vm/isos/browse", "/api/vm/isos/keep",
    # Upgrading the platform: the cluster, Longhorn, KubeVirt, CDI.
    "/api/cluster/components/upgrade", "/api/cluster/upgrades/start",
    "/api/longhorn/v2/upgrade/review", "/api/longhorn/v2/upgrade/settings",
    # The VM image store downloads gigabytes into the cluster.
    "/api/vm/store/keep", "/api/vm/store/auto", "/api/vm/store/forget", "/api/vm/store/refresh",
    # Homestead's own permissions, and the namespaces apps live in.
    "/api/self/permissions", "/api/namespaces/create", "/api/namespaces/delete",
}
# things a signed-in user may always do to their own account
SELF_ROUTES = {"/api/auth/preferences/dashboard", "/api/auth/logout", "/api/auth/password", "/api/auth/signout-everywhere",
               # Which linked cluster this browser is looking at.
               "/api/fleet/switch",
               # Notifications on your own devices, and what they are shown.
               "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test",
               "/api/push/status", "/api/alerts/pending", "/api/alerts/acknowledge", "/api/alerts/delivered"}


def needed_role(path, method):
    declared = ROUTE_POLICY.role(path, method)
    if declared is None:
        raise PermissionError("this route has no declared authorization policy")
    return declared


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


# ---------------------------------------------------------------- linked clusters
# The resources the combined view gathers, and how this cluster answers locally.
FLEET_LISTS = {
    "workloads": lambda: cached("wl", 5, get_workloads),
    "vms": lambda: cached("vms", 5, VMS.list_vms),
    "nodes": lambda: cached("nodes", 5, get_nodes),
    "volumes": lambda: cached("vol", 8, get_volumes),
    "flow": lambda: cached("flow2", 8, get_flow2),
}


def fleet_all(what, user, role):
    """Gather tagged resource lists or Architecture graphs from linked clusters.

    Each cluster is asked as the person asking, so it shows them what their
    role lets them see there. A cluster that does not answer is left out and
    named, rather than holding up the rest.
    """
    local = FLEET_LISTS[what]
    view = FLEET.summary()
    tags = {m["id"]: {"id": m["id"], "name": m["name"], "handle": m["handle"], "self": m["self"]}
            for m in view["members"]}
    results, missing = {}, []

    def ask(m):
        try:
            result = FLEET.call(m, "GET", f"/api/{what}", timeout=12,
                                user=str(user or "").split("@", 1)[0], role=role)
            if what == "flow" and (not isinstance(result, dict) or
                    any(not isinstance(result.get(key), list) for key in ("workloads", "volumes", "nodes", "vips"))):
                raise ValueError("Architecture data is unavailable")
            results[m["id"]] = result
        except Exception as error:
            missing.append({"id": m["id"], "name": m["name"], "error": str(error)[:200]})
    threads = [threading.Thread(target=ask, args=(m,), daemon=True)
               for m in view["members"] if not m["self"] and m["reachable"]]
    missing += [{"id": m["id"], "name": m["name"], "error": m.get("error") or "not answering"}
                for m in view["members"] if not m["self"] and not m["reachable"]]
    for thread in threads:
        thread.start()
    try:
        results[view["self"]] = local()
    except Exception as error:
        if what != "flow":
            raise
        missing.append({"id": view["self"], "name": tags[view["self"]]["name"], "error": str(error)[:200]})
    for thread in threads:
        thread.join(15)
    rows = []
    for m in view["members"]:
        result = results.get(m["id"])
        entries = [result] if what == "flow" and result is not None else result or []
        for row in entries:
            if isinstance(row, dict):
                rows.append({**row, "site": tags[m["id"]]})
    return ({"clusters": rows, "missing": missing} if what == "flow" else rows), missing


# ---------------------------------------------------------------- HTTP
_key_refusals = {}      # address -> when a refused API key was last written down


class H(HTTP.LimitedHandler):
    protocol_version = "HTTP/1.1"
    timeout = HTTP.IDLE_SECONDS
    max_body = MAX_BODY



    def log_message(self, fmt, *a):
        pass

    _extra_headers = None

    def _send(self, code, body, ctype="application/json"):
        report_id = self.headers.get("X-Homestead-Report", "")
        if report_id and getattr(self, "_diagnostic_authorized", False) and getattr(self, "role", None) == "admin" and not self.path.startswith(("/api/diagnostics", "/api/auth/")):
            try:
                DIAGNOSTICS.server_event(report_id, self.user, {"kind": "server", "path": self.path,
                    "method": self.command, "status": code, "request": self.headers.get("X-Homestead-Request", ""),
                    "duration": round((time.monotonic() - self._diagnostic_started) * 1000)})
            except Exception:
                pass  # Recording must never change the result of a cluster action.
        if code >= 400:
            self.close_connection = True
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
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

    def _snapshot_file_download(self, namespace, session, path):
        info = SNAPSHOT_FILES.read(namespace, session, "stat", path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(info["size"]))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(path.rsplit("/", 1)[-1], safe=""))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        try:
            offset = 0
            while offset < info["size"]:
                chunk = SNAPSHOT_FILES.read(namespace, session, "chunk", path, offset)
                data = chunk["data"]
                if chunk["size"] != info["size"] or not data or offset + len(data) > info["size"]:
                    raise ValueError("Snapshot file changed during download")
                self.wfile.write(data)
                offset += len(data)
        except Exception:
            # Headers were sent: close the partial response, never append JSON
            # to a file or claim a complete download after losing the helper.
            self.close_connection = True

    def _via_cloudflare(self):
        """Whether this request came in through Cloudflare, which always says so."""
        return bool(self.headers.get("Cf-Connecting-Ip") or self.headers.get("Cf-Ray"))

    def _client_ip(self):
        """Who is asking. X-Forwarded-For is whatever the client wrote, so it is not
        used. Cloudflare's own header is - but only when Cloudflare Access is set
        up, since then a request claiming to come through Cloudflare must also
        carry Access's signature. Without Access, anyone could send the header
        to dodge the per-address sign-in limit, or write any address into the
        sign-in history."""
        if CFACCESS.enabled() and self._via_cloudflare() and self.headers.get("Cf-Connecting-Ip"):
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
        except MOVE_SOURCE.PendingRecovery as error:
            return self._send(503, {"error": str(error)})
        except (ValueError, PermissionError) as error:
            return self._send(409, {"error": str(error)})
        except MOVE.Unreachable as error:
            return self._send(502, {"error": str(error)})

    def _who(self):
        if FLEET.signed(self.headers):
            return self._fleet_identity()
        return AUTH.verify_token(self._cookies().get(AUTH.COOKIE), force=self.command in ("POST", "PUT", "PATCH", "DELETE"))

    def _console_authorizer(self, needed):
        token = self._cookies().get(AUTH.COOKIE)
        if self._fleet_from:
            return FLEET.console_authorizer(self._fleet_from)
        def check():
            who = AUTH.verify_token(token, force=True)
            return bool(who and AUTH.allows(who["role"], needed))
        return check

    def _fleet_identity(self):
        """A request relayed by a linked Homestead, checked once: for a person
        signed in there, or from that Homestead itself."""
        if self._fleet_who is False:
            try:
                found = FLEET.verify(self.headers, self.command, self.path, self._raw_body())
            except PermissionError:
                found = None
            self._fleet_who = ({"user": found["user"], "role": found["role"], "remember": False,
                                "started": None, "expires": None, "fleet": found["sender"]}
                               if found and found["role"] else None)
            self._fleet_from = (found or {}).get("sender")
        return self._fleet_who

    def _guard(self, path, enforce_role=True):
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
        if _self_data_boot_pending:
            static = (is_spa_route(path) or is_page_path(path) or is_app_identity(path) or is_asset_path(path)
                      or path in ("/style.css", "/sw.js") or is_vendor_path(path)
                      or path.startswith("/js/") and path.endswith(".js"))
            if self.command == "GET" and static:
                return None  # Bundled UI can explain an outage without touching data.
            try:
                boot_state = self_data_boot_status()
            except SELF_DATA_FENCE.Held as error:
                self._send(503, {"error": str(error), "data_handoff": True})
                return True
            # Ready means the real app loaded and can read its copied journal
            # and account key, not that normal writers have been released.
            if self.command == "GET" and path == "/healthz":
                self._send(200, {"ok": True, "data_handoff": True, "read_only": True})
                return True
            if self.command == "GET" and path == "/api/auth/state":
                detail = {"recovery": True, "operation": boot_state["operation"]} if boot_state.get("mode") == "recovery" else {}
                if detail:
                    detail["signed_in"] = bool(AUTH.verify_token(self._cookies().get(AUTH.COOKIE)))
                self._send(200, {"data_handoff": True, "setup": False, **detail})
                return True
            if self.command == "POST" and path == "/api/auth/login" and boot_state.get("mode") == "recovery":
                # Giving an unstarted move up needs an admin, and a browser
                # without a session must be able to become one. Nothing is
                # written: not the sign-in time, not the sign-in log.
                if self.headers.get("X-Homestead-Auth") != "1":
                    self._send(403, {"error": "missing X-Homestead-Auth header"})
                    return True
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    self._send(415, {"error": "application/json required"})
                    return True
                if int(self.headers.get("Content-Length") or 0) > 4096:
                    self._send(413, {"error": "that request is larger than Homestead accepts"})
                    return True
                b = self._body()
                remember = bool(b.get("remember"))
                try:
                    token = AUTH.login_read_only(b.get("username"), b.get("password"), self._client_ip(), remember)
                except PermissionError as error:
                    self._send(401, {"error": str(error)})
                    return True
                self._set_cookie(token, max_age=AUTH.idle_ttl(remember))
                self._send(200, {"ok": True, "recovery": True})
                return True
            recovery_action = self.command == "POST" and path == "/api/self/data/abandon" and boot_state.get("mode") == "recovery"
            if not recovery_action and (self.command != "GET" or not re.fullmatch(r"/api/self/data/handoff/[a-f0-9]{24}(?:/view)?", path)):
                self._send(503, {"error": "Homestead is verifying its new data volume. Wait for the move to finish before changing anything.", "data_handoff": True})
                return True
        if path != "/api/self/data/abandon" and (self.command in ("POST", "PUT", "PATCH", "DELETE") or path in ("/healthz", "/api/auth/state", "/api/console", "/api/node/shell", "/api/vm/console")):
            try:
                require_self_data_write()
            except SELF_DATA_FENCE.Held as error:
                if self.command == "GET" and path in ("/healthz", "/api/auth/state") and _self_data_fence is not None:
                    try:
                        recovery = _self_data_fence.recovery()
                        self._send(200, {"ok": True, "read_only": True, "data_handoff": True, "setup": False,
                                         "recovery": True, "operation": recovery["operation"]})
                        return True
                    except Exception:
                        pass
                self._send(503, {"error": str(error), "data_handoff": True})
                return True
        if self.command in ("POST", "PUT", "PATCH", "DELETE") and path in ("/api/auth/login", "/api/auth/setup", "/api/fleet/home"):
            if self.headers.get("X-Homestead-Auth") != "1":
                self._send(403, {"error": "missing X-Homestead-Auth header"})
                return True
            if path in ("/api/auth/login", "/api/auth/setup") and self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send(415, {"error": "application/json required"})
                return True
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "").lower()
            if origin and urllib.parse.urlparse(origin).netloc.lower() != host:
                self._send(403, {"error": "request origin rejected"})
                return True
        if (is_spa_route(path) or is_page_path(path) or is_public_path(path) or is_vendor_path(path) or
                (path.startswith("/js/") and path.endswith(".js"))):
            return None
        authorization = self.headers.get("Authorization", "")
        if authorization[:7].lower() == "bearer ":
            return self._api_key_guard(path, authorization[7:].strip())
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
        HOSTACCESS.set_role(self.role)
        if not enforce_role:
            return None
        need = needed_role(path, self.command)
        if not AUTH.allows(self.role, need):
            self._send(403, {"error": f"your role ({self.role}) cannot do this — {need} required",
                             "role": self.role, "needed": need})
            return True
        self._diagnostic_authorized = True
        return None

    def _api_key_guard(self, path, token):
        """An API key: for /api/v1 alone, checked, and the request let through
        with the key's scopes - which /api/v1 enforces per endpoint. No cookie
        is involved, so there is no cross-site form to guard against."""
        if not path.startswith("/api/v1/"):
            self._send(401, {"error": "API keys work only on /api/v1; GET /api/v1/openapi.json lists what they can do"})
            return True
        try:
            key = API_KEYS.verify(token, self._client_ip(), force=self.command in ("POST", "PUT", "PATCH", "DELETE"))
        except PermissionError as error:
            message = str(error)
            limited = message.startswith("too many")
            # One line per address a minute at most: a client retrying a bad
            # key must not wash every other entry out of the history.
            addr = self._client_ip()
            if time.time() - _key_refusals.get(addr, 0) > 60:
                if len(_key_refusals) > 1000:
                    _key_refusals.clear()       # many addresses at once: start again
                _key_refusals[addr] = time.time()
                self._signin("key-refused", "", ok=False, detail=message)
            self._send(429 if limited else 401, {"error": message})
            return True
        except AUTH.StoreUnavailable as error:
            self._send(503, {"error": str(error), "cause": getattr(error, "cause", "")})
            return True
        self.api_key = {**key, "kind": "key"}
        # API control scopes carry operator authority, never an internal/admin
        # bypass of workload target checks, even when an admin issued the key.
        self.user = f"api-key:{key['name']}"
        self.role = "operator" if any(scope.endswith(":control") for scope in key["scopes"]) else "viewer"
        HOSTACCESS.set_role(self.role)
        return None

    def _api_v1(self, method, path, query, body):
        """One /api/v1 request, for a key or for someone signed in."""
        auth = self.api_key or {"name": self.user, "kind": "session", "expires": None,
                                "scopes": API_KEYS.scopes_for_role(self.role)}
        if path == "/api/v1/openapi.json" and method == "GET":
            return self._send(200, API_V1.openapi(HOMESTEAD_VERSION, {s: text for s, (_, text) in API_KEYS.SCOPES.items()}))
        code, answer = API_V1.handle(method, path, query, body, auth)
        return self._send(code, answer)

    def _begin(self):
        """Per request: a connection can carry several, and the handler stays."""
        self.api_key = None
        self._extra_headers = []
        self._raw = None
        self._fleet_who = False
        self._fleet_from = None
        self._diagnostic_authorized = False
        self._diagnostic_started = time.monotonic()
        HOSTACCESS.set_role(None)

    def _raw_body(self):
        """The request body, read once: a signature covers it before it is parsed."""
        if self._raw is None:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY or self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                raise ValueError("invalid request size or framing")
            if n:
                with HTTP.deadline(self.connection, HTTP.BODY_SECONDS):
                    self._raw = self.rfile.read(n)
                if len(self._raw) != n:
                    self.close_connection = True
                    raise ValueError("incomplete request body")
            else:
                self._raw = b""
        return self._raw

    def _signin(self, event, user, ok=True, detail=""):
        """One line in the sign-in history, with where it came from."""
        SIGNINS.record(event, user, self._client_ip(), ok, detail, self.headers.get("User-Agent", ""),
                       "Cloudflare" if self._via_cloudflare() else "")

    def _body(self):
        raw = self._raw_body()
        return json.loads(raw.decode()) if raw else {}

    def _fleet_target(self, path):
        """The linked cluster this request is for, when it is not this one.

        A browser switched to another cluster says so with a cookie. In the
        view of every cluster at once, an action on one row names that row's
        cluster itself: a header, or for a console, which cannot send one, a
        query parameter.
        """
        headers = getattr(self, "headers", None) or {}
        # /api/v1 answers for this cluster alone: an API key never travels.
        if FLEET.signed(headers) or FLEET.local_path(path) or path.startswith("/api/v1/"):
            return ""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        named = headers.get("X-Homestead-Cluster") or (query.get("hs_cluster") or [""])[0]
        target = named or (self._cookies().get(FLEET.COOKIE, "") if headers else "")
        if not target or target == FLEET.self_id():
            return ""
        # A browser's choice of a cluster this one does not know - left by the
        # Homestead that used to answer at this address, or unlinked since -
        # is forgotten, and the page served here, not a 502 for every request.
        try:
            known = FLEET.member(target)
        except Exception:
            known = True        # cannot tell now: relay as before, rather than fail the page
        if not named and not known:
            if isinstance(getattr(self, "_extra_headers", None), list):
                self._extra_headers.append(
                    ("Set-Cookie", f"{FLEET.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"))
            return ""
        return target

    def _fleet_forward(self, target, path):
        """Relay this request to the linked cluster picked in the top bar.

        This Homestead signs the person in; the one that answers decides what
        their role lets them do there, by its own rules.
        """
        if self._guard(path, enforce_role=False):
            return
        who = self._who()
        cookies = [value for name, value in self._extra_headers if name == "Set-Cookie"]
        try:
            FLEET.forward(self, target, self._raw_body(), who["user"] if who else "",
                          who["role"] if who else "", cookies)
        except PermissionError as error:
            return self._send(403, {"error": str(error)})
        except FLEET.Unreachable as error:
            known = FLEET.member(target) or {}
            if self.command == "GET" and "text/html" in (self.headers.get("Accept") or ""):
                # 503, not 502: Cloudflare puts its own "Host Error" page in
                # place of an origin's 502, hiding the way back to this cluster.
                return self._send(503, FLEET.unreachable_page(known.get("name") or "That cluster", error),
                                  "text/html; charset=utf-8")
            return self._send(502, {"error": str(error), "cluster": known.get("name", ""), "unreachable": True})

    def _fleet_post(self, p, b):
        """Linking clusters, and picking which one this browser looks at."""
        try:
            if p == "/api/fleet/switch":
                wanted = str(b.get("id") or "")
                if not wanted or wanted == FLEET.self_id():
                    self._extra_headers.append(
                        ("Set-Cookie", f"{FLEET.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"))
                    return self._send(200, {"ok": True, "id": FLEET.self_id()})
                target = FLEET.member(wanted)
                if not target:
                    return self._send(404, {"error": "no linked cluster by that name"})
                check = FLEET.check(target)
                if not check.get("reachable"):
                    return self._send(502, {"error": f"{target.get('name')} is not answering this Homestead: "
                                                     f"{check.get('error') or 'no answer'}"})
                secure = "; Secure" if self._over_tls() else ""
                self._extra_headers.append(("Set-Cookie", f"{FLEET.COOKIE}={target['id']}; Path=/; HttpOnly; "
                                                          f"SameSite=Strict{secure}; Max-Age=2592000"))
                return self._send(200, {"ok": True, "id": target["id"], "name": target.get("name")})
            if p == "/api/fleet/join":
                return self._send(200, FLEET.join(b.get("url"), b.get("username"), b.get("password"),
                                                  b.get("own_url", "")))
            if p == "/api/fleet/accept":
                return self._send(200, FLEET.accept(b))
            if p == "/api/fleet/sync":
                if not self._fleet_from:
                    return self._send(403, {"error": "only a linked Homestead sends this"})
                return self._send(200, FLEET.adopt(b, self._fleet_from))
            if p == "/api/fleet/remove":
                return self._send(200, FLEET.remove(str(b.get("id") or "")))
            if p == "/api/fleet/leave":
                return self._send(200, FLEET.leave())
            if p == "/api/fleet/address":
                return self._send(200, FLEET.set_address(b.get("url")))
            if p == "/api/fleet/link-legacy":
                return self._send(200, MOVE.link_legacy(str(b.get("name") or ""), b.get("own_url", "")))
        except ValueError as error:
            return self._send(409, {"error": str(error)})
        except FLEET.Unreachable as error:
            return self._send(502, {"error": str(error)})
        return self._send(404, {"error": "not found"})

    @self_data_request
    def do_GET(self):
        self._begin()
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, Query(urllib.parse.parse_qs(u.query))
        target = self._fleet_target(p)
        if target:
            return self._fleet_forward(target, p)
        try:
            if self._guard(p):
                return
            if p.startswith("/api/v1/"):
                return self._api_v1("GET", p, q, None)
            if p == "/api/diagnostics":
                return self._send(200, DIAGNOSTICS.listing(self.user))
            if p == "/api/diagnostics/report":
                return self._send(200, DIAGNOSTICS.snapshot((q.get("id") or [""])[0], self.user,
                                                           (q.get("format") or ["anonymised"])[0]))
            if p == "/api/diagnostics/issue":
                return self._send(200, DIAGNOSTICS.issue((q.get("id") or [""])[0], self.user))
            if p == "/api/diagnostics/download":
                filename, ctype, body = DIAGNOSTICS.export((q.get("id") or [""])[0], self.user,
                    (q.get("format") or ["anonymised"])[0], (q.get("package") or [""])[0] == "1")
                self._extra_headers.append(("Content-Disposition", f'attachment; filename="{filename}"'))
                return self._send(200, body, ctype)
            if p == "/api/console":
                return CONSOLE_PROXY.handle(self, self.user, q)
            if p == "/api/node/shell":
                node = (q.get("node") or [""])[0]
                target = NODESHELL.open_shell(node)
                ended = {"done": False}

                def close_shell():
                    ended["done"] = True
                    NODESHELL.session_ended(node)
                NODESHELL.session_started(node)
                try:
                    return CONSOLE_PROXY.handle(self, self.user, q, node=target, on_close=close_shell)
                finally:
                    # Refused before it began (origin, upgrade): no session to count.
                    if not ended["done"]:
                        NODESHELL.session_ended(node)
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
                return self._send(200, {"active": [a for a in ALERTS.active(user=self.user) if a.get("announced", 0) > 0],
                                        "log": [a for a in ALERTS.log(limit=30)["alerts"] if a["category"] != "test"],
                                        "devices": PUSH.devices(self.user)})
            if p == "/style.css":
                return self._file(f"{WEBROOT}/style.css", "text/css")
            if p == "/healthz":
                return self._send(200, {"ok": True})
            if p == "/api/auth/state":
                who = self._who()
                return self._send(200, {"setup": AUTH.needs_setup() and not (who and who.get("fleet")),
                                        "via": ((who or {}).get("fleet") or {}).get("name", ""),
                                        "user": who["user"] if who else None,
                                        "role": who["role"] if who else None,
                                        "remember": bool(who and who.get("remember")),
                                        "session_expires": who.get("expires") if who else None,
                                        "session_started": who.get("started") if who else None,
                                        "session_max_days": AUTH.ABSOLUTE_TTL // 86400,
                                        "roles": list(AUTH.ROLES)})
            if p == "/api/fleet":
                self._who()
                return self._send(200, FLEET.summary(via=self._fleet_from))
            if p == "/api/fleet/hello":
                return self._send(200, FLEET.hello())
            if p == "/api/config/parts":
                return self._send(200, CONFIG.parts())
            if p == "/api/fleet/legacy":
                return self._send(200, MOVE.legacy_clusters())
            if p == "/api/fleet/state":
                self._who()
                if not self._fleet_from:
                    return self._send(403, {"error": "only a linked Homestead asks for this"})
                return self._send(200, FLEET.shared_state())
            if p == "/api/fleet/home":
                self._extra_headers += [("Set-Cookie", f"{FLEET.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"),
                                        ("Location", "/")]
                return self._send(302, {"ok": True})
            if p.startswith("/api/fleet/all/") and p.rsplit("/", 1)[-1] in FLEET_LISTS:
                rows, missing = fleet_all(p.rsplit("/", 1)[-1], self.user, self.role)
                if missing:
                    self._extra_headers.append(("X-Homestead-Fleet-Missing",
                                                urllib.parse.quote(json.dumps(missing))))
                return self._send(200, rows)
            if p == "/api/auth/preferences/dashboard":
                return self._send(200, AUTH.dashboard_preferences(self.user))
            if p == "/api/auth/users":
                return self._send(200, AUTH.list_users())
            if p == "/api/setup":
                # Many checks: kept for each person half a minute.
                return self._send(200, cached(f"setup:{self.role}:{self.user}", 30, lambda: setup_state(self.user, self.role)))
            if p == "/api/auth/keys":
                return self._send(200, {"keys": API_KEYS.list_keys(),
                                        "scopes": {s: text for s, (_, text) in API_KEYS.SCOPES.items()},
                                        "min_ttl": API_KEYS.MIN_TTL, "max_ttl": API_KEYS.MAX_TTL})
            if p == "/api/auth/history":
                return self._send(200, SIGNINS.history((q.get("user") or [""])[0],
                                                       (q.get("failures") or [""])[0] == "1"))
            if p == "/api/settings":
                return self._send(200, app_settings_payload())
            if p == "/api/host-console":
                return self._send(200, HOST_CONSOLE.inventory())
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
            if p == "/api/firewall":
                return self._send(200, FIREWALL.inventory())
            if p == "/api/cluster":
                return self._send(200, cached("cluster", 15, CLUSTER.inventory))
            if p == "/api/self/replicas":
                return self._send(200, homestead_replicas())
            if p == "/api/self/data/prepare":
                try:
                    return self._send(200, self_data_preparation_state())
                except SELF_DATA_FENCE.Held as error:
                    return self._send(409, {"error": str(error)})
            if p.startswith("/api/self/data/handoff/"):
                if re.fullmatch(r"/api/self/data/handoff/[a-f0-9]{24}/view", p):
                    return self._send(200, SELF_DATA_WORKER.maintenance_page(p.split("/")[-2]), "text/html; charset=utf-8")
                status = self_data_handoff_status(p[len("/api/self/data/handoff/"):])
                if status is None:
                    return self._send(404, {"error": "No recorded data move with this identity"})
                return self._send(503 if status["status"] == "unknown" else 200, status)
            if p == "/api/ipam":
                return self._send(200, IPAM.view())
            if p == "/api/ipam/free":
                return self._send(200, IPAM.free_addresses())
            if p == "/api/vm/store":
                return self._send(200, VMSTORE.view(check=(q.get("check") or [""])[0] == "1"))
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
            if p == "/api/platform/baseline":
                return self._send(200, cached("baseline", 10, BASELINE.report))
            if p == "/api/addons":
                return self._send(200, ADDONS.status())
            if p == "/api/platform/join":
                return self._send(200, PLATFORM.join_guide())
            if p == "/api/mqtt":
                return self._send(200, {**MQTT.public(),
                                        "sensors": {"cluster": len(MQTT.CLUSTER_SENSORS), "node": len(MQTT.NODE_SENSORS)}})
            if p == "/api/mqtt/preview":
                snap = mqtt_snapshot()
                return self._send(200, {"states": [{"topic": t, "payload": v} for t, v in MQTT.states(MQTT.load(), snap)]})
            if p == "/api/helm":
                return self._send(200, cached("helm", 10, HELM.releases))
            if p == "/api/helm/release":
                return self._send(200, HELM.release((q.get("ns") or [""])[0], (q.get("name") or [""])[0], include_sensitive=self.role == "admin"))
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
                return self._move(lambda: MOVE_SOURCE.in_namespace(
                    (q.get("namespace") or [""])[0], MOVE_SOURCE.definition,
                    (q.get("kind") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/move/source-status":
                return self._move(lambda: MOVE_SOURCE.in_namespace(
                    (q.get("namespace") or [""])[0], MOVE_SOURCE.status,
                    (q.get("kind") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/move/target":
                return self._move(MOVE_SOURCE.target)
            if p == "/api/objectstore":
                return self._send(200, OBJECTS.status())
            if p == "/api/objectstore/transfers":
                return self._send(200, OBJECTS.transfers())
            if p == "/api/image-updates/scan-progress":
                # Read while a scan is in flight, so it needs no session cache
                # and must not be served from one.
                return self._send(200, UPDATES.scan_progress())
            if p == "/api/image-updates":
                force = (q.get("force") or ["0"])[0].lower() in ("1", "true", "yes")
                # Settings › Homestead checks Homestead's own parts, not every app.
                only = (q.get("only") or [""])[0] == "homestead"
                report = json.loads(json.dumps(UPDATES.homestead_report() if force and only else UPDATES.report(force)))
                report["policy"] = update_policy_status()
                return self._send(200, report)
            if p == "/api/image-updates/progress":
                return self._send(200, UPDATES.progress(q["ns"][0], q["name"][0]))
            if p == "/api/operations":
                return self._send(200, OPS.list_operations())
            if p == "/api/operations/log":
                return self._send(200, OPS.log((q.get("id") or [""])[0]))
            if p == "/api/volumes/other":
                return self._send(200, cached("volother", 10, other_volumes))
            if p == "/api/volumes":
                return self._send(200, cached("vol", 8, get_volumes))
            if p == "/api/volumes/edit-options":
                return self._send(200, volume_edit_options((q.get("ns") or [DEFAULT_NS])[0],
                                                          (q.get("name") or [""])[0]))
            if p == "/api/volumes/delete-plan":
                plan = VOLUMES.deletion_plan((q.get("ns") or [DEFAULT_NS])[0],
                    (q.get("name") or [""])[0], (q.get("volume") or [""])[0])
                return self._send(200, STORAGE_GUARD.review(plan, OPS, kget))
            if p == "/api/events":
                return self._send(200, cached("ev", 10, get_events))
            if p == "/api/storage":
                return self._send(200, cached("stor", 10, get_storage))
            if p == "/api/node":
                return self._send(200, cached("node:" + (q.get("name") or [""])[0], 5,
                                  lambda: next((n for n in get_nodes()
                                                if n["name"] == (q.get("name") or [""])[0]), {})))
            if p == "/api/os-updates":
                return self._send(200, OS_ROLLOUT.report())
            if p == "/api/passthrough/inventory":
                return self._send(200, PASSTHROUGH.inventory((q.get("node") or [""])[0]))
            if p == "/api/passthrough/resources":
                return self._send(200, PASSTHROUGH.resources(with_usage=True))
            if p == "/api/self/address":
                return self._send(200, SELF_ADDRESS.report())
            if p == "/api/welcome":
                return self._send(200, welcome_state(self.role))
            if p == "/api/node/os":
                return self._send(200, HOST_OS.report((q.get("name") or [""])[0] or None))
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
            if p == "/api/node/probe/allocation":
                current = ALLOCATION_PROBE.status()
                if current["enabled"] and current.get("image") != NAMES.IMAGE + ":" + HOMESTEAD_VERSION:
                    current["detail"] = "Helper update pending. Review capacity and save settings to use this release."
                if current["installed"]:
                    try:
                        current["capacity"] = ALLOCATION_PROBE.configure({**current, "enabled": True,
                            "directory": current.get("directory") or "/var/lib/kubelet/pod-resources"}, HOMESTEAD_VERSION, preview=True)
                    except Exception:
                        current["capacity"] = {"blocked": True, "blockers": ["Capacity could not be checked. Refresh before enabling."], "warnings": []}
                return self._send(200, current)
            if p == "/api/node/probe/allocation/check":
                node = (q.get("node") or [""])[0]
                host = next((row for row in PLACE.get_nodes() if row["name"] == node), None)
                if host is None:
                    raise ValueError("Choose a current host")
                check = ALLOCATION_EVIDENCE.inspect(host, kget)
                return self._send(200, {"name": node, "verified": check["verified"], "detail": check["reason"]})
            if p == "/api/history/long":
                return self._send(200, HISTORY.series((q.get("range") or ["24h"])[0]))
            if p == "/api/history":
                # Never hold the global cache mutex while writing to a client.
                return self._send(200, HISTORY.live_series())
            if p == "/api/flow":
                return self._send(200, cached("flow2", 8, get_flow2))
            if p == "/api/shares":
                return self._send(200, SHARES.list_shares())
            if p == "/api/shares/users":
                return self._send(200, SHARES.list_users())
            if p == "/api/shares/server":
                return self._send(200, samba_state())
            if p == "/api/shares/nfs/server":
                return self._send(200, nfs_state())
            if p == "/api/files/list":
                return self._send(200, FILES.list_files(
                    (q.get("namespace") or [DEFAULT_NS])[0], (q.get("pvc") or [""])[0],
                    (q.get("path") or [""])[0]))
            if p == "/api/snapshot-files/plan":
                return self._send(200, SNAPSHOT_FILES.review((q.get("volume") or [""])[0], (q.get("snapshot") or [""])[0]))
            if p in ("/api/snapshot-files/status", "/api/snapshot-files/list", "/api/snapshot-files/download"):
                ns, session = (q.get("namespace") or [""])[0], (q.get("session") or [""])[0]
                if p.endswith("/status"):
                    return self._send(200, SNAPSHOT_FILES.status(ns, session))
                path = (q.get("path") or [""])[0]
                if p.endswith("/download"):
                    return self._snapshot_file_download(ns, session, path)
                return self._send(200, SNAPSHOT_FILES.read(ns, session, "list", path))
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
                return self._send(200, power_plan_with_job(POWER.plan((q.get("node") or [""])[0],
                                                                      (q.get("action") or [""])[0],
                                                                      force=(q.get("force") or [""])[0] == "1")))
            if p == "/api/cluster/shutdown/plan":
                return self._send(200, cluster_shutdown(review=True).review())
            if p == "/api/cluster/shutdown":
                return self._send(200, {"state": cluster_shutdown().public_state()})
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
                return self._send(200, VMS.detail((q.get("ns") or [""])[0], (q.get("name") or [""])[0], include_sensitive=self.role == "admin"))
            if p == "/api/vm/isos":
                return self._send(200, ISOS.library())
            if p == "/api/vm/isos/browse":
                return self._send(200, ISOS.browse((q.get("share") or [""])[0], (q.get("path") or [""])[0]))
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
            if p == "/api/lh/snapshot/delete-plan":
                return self._send(200, SNAPSHOT_DELETE.plan((q.get("volume") or [""])[0], (q.get("name") or [""])[0]))
            if p == "/api/lh/snapshots":
                vol = (q.get("volume") or [None])[0]
                return self._send(200, LH.snapshots(vol))
            if p == "/api/lh/snapshot-progress":
                return self._send(200, SNAPSHOT_DELETE.progress((q.get("volume") or [""])[0]))
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
            if p == "/api/longhorn/v2/plan":
                return self._send(200, LHV2_SETUP.plan())
            if p == "/api/longhorn/v2/upgrade":
                return self._send(200, LHV2_UPGRADE.plan(q.get("to", [""])[0]))
            if p == "/api/disks/v2/status":
                return self._send(200, DISK_V2.status(q.get("id", [""])[0]))
            if p == "/api/disks":
                return self._send(200, cached("disks", 10, DISKS.inventory))
            if p == "/api/longhorn/capacity":
                return self._send(200, cached("lhcap", 15, LHCAP.status))
            if p == "/api/longhorn/offline-rebuilding":
                return self._send(200, cached("lhrebuild", 30, LHREBUILD.status))
            if p == "/api/workloads/rebalance/plan":
                return self._send(200, CREBALANCE.plan([a for a in (q.get("exclude") or [""])[0].split(",") if a]))
            if p == "/api/longhorn/rebalance/plan":
                return self._send(200, REBALANCE.plan([a for a in (q.get("exclude") or [""])[0].split(",") if a]))
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
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except AUTH.StoreUnavailable as e:
            # Not an empty account store: the cluster did not answer.
            return self._send(503, {"error": str(e), "cause": getattr(e, "cause", ""), "unavailable": True})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": API_ERRORS.message(e, 500)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    @self_data_request
    def do_POST(self):
        self._begin()
        u = urllib.parse.urlparse(self.path)
        p = u.path
        target = self._fleet_target(p)
        if target:
            return self._fleet_forward(target, p)
        try:
            if self._guard(p):
                return
            if int(self.headers.get("Content-Length") or 0) > MAX_BODY:
                return self._send(413, {"error": "that request is larger than Homestead accepts"})
            b = self._body()
            if not isinstance(b, dict):
                return self._send(400, {"error": "a JSON object is required"})
            if p == "/api/diagnostics/start":
                return self._send(200, DIAGNOSTICS.create(self.user, b.get("package") is True))
            if p == "/api/diagnostics/events":
                return self._send(200, DIAGNOSTICS.append(b.get("id"), self.user, b.get("batch"), b.get("events")))
            if p == "/api/diagnostics/stop":
                return self._send(200, DIAGNOSTICS.stop(b.get("id"), self.user))
            if p == "/api/diagnostics/draft":
                return self._send(200, DIAGNOSTICS.draft(b.get("id"), self.user, b.get("title", ""), b.get("comment", "")))
            if p == "/api/diagnostics/prepare":
                return self._send(200, DIAGNOSTICS.prepare(b.get("id"), self.user, b.get("title", "Bug report"),
                    b.get("comment", ""), b.get("sources", []), b.get("seconds", 900)))
            if p == "/api/diagnostics/delete":
                return self._send(200, DIAGNOSTICS.delete(b.get("id"), self.user))
            if p in ("/api/move", "/api/move/preview", "/api/image-updates/apply", "/api/image-updates/rollback", "/api/image-updates/preview"):
                require_workload_target(b.get("ns") or DEFAULT_NS, b.get("name") or "")
            addr = self._client_ip()
            if p.startswith("/api/v1/"):
                return self._api_v1("POST", p, urllib.parse.parse_qs(u.query), b)
            if p == "/api/auth/keys":
                # Made in this Homestead's own app, by someone signed in to it:
                # never relayed from a linked cluster.
                if self._fleet_from:
                    return self._send(403, {"error": "make API keys in this Homestead's own app"})
                made = API_KEYS.create(b.get("name"), b.get("scopes"), b.get("ttl_seconds"), b.get("networks"), self.user)
                self._signin("key-added", self.user, detail=f"{made['key']['name']}: {', '.join(made['key']['scopes'])}")
                return self._send(200, {"ok": True, **made})
            if p.startswith("/api/setup/"):
                for key in [k for k in _cache if k.startswith("setup:")]:
                    _cache.pop(key, None)
            if p == "/api/setup/skip":
                return self._send(200, SETUP.skip(str(b.get("step") or ""), bool(b.get("skip", True)), self.user, self.role == "admin"))
            if p == "/api/setup/hide":
                return self._send(200, SETUP.hide(self.user, b.get("hidden", True)))
            if p == "/api/setup/complete":
                return self._send(200, SETUP.complete(self.user, b.get("completed", True)))
            if p == "/api/setup/opened":
                return self._send(200, SETUP.mark_opened())
            if p == "/api/setup/https-check":
                return self._send(200, SETUP.https_check(b.get("url")))
            if p == "/api/auth/keys/revoke":
                if self._fleet_from:
                    return self._send(403, {"error": "revoke API keys in this Homestead's own app"})
                done = API_KEYS.revoke(b.get("id"))
                self._signin("key-revoked", self.user, detail=done["name"])
                return self._send(200, done)
            if p.startswith("/api/fleet/"):
                return self._fleet_post(p, b)
            if p in ("/api/config/backup", "/api/config/inspect", "/api/config/restore"):
                try:
                    if p == "/api/config/backup":
                        made = CONFIG.backup(b.get("parts"), str(b.get("passphrase") or ""))
                        SETUP.note("config_backup_at", int(time.time()))
                        return self._send(200, made)
                    if p == "/api/config/inspect":
                        return self._send(200, CONFIG.inspect(b.get("file"), str(b.get("passphrase") or "")))
                    return self._send(200, CONFIG.restore(b.get("file"), str(b.get("passphrase") or ""), b.get("parts") or []))
                except PermissionError as error:
                    # A wrong passphrase is not a missing role: 422, not 403.
                    return self._send(422, {"error": str(error)})
                except ValueError as error:
                    return self._send(400, {"error": str(error)})
            if p == "/api/auth/setup":
                # Whoever finishes setup becomes the first administrator, so it is
                # not offered to the internet, however the hostname is protected.
                if self._via_cloudflare():
                    return self._send(403, {"error": "finish setting Homestead up from your LAN; "
                                                     "setup is not offered through the tunnel"})
                AUTH.create_user(b.get("username"), b.get("password"), first_only=True)
                self._signin("setup", (b.get("username") or "").strip().lower(), detail="the first administrator")
                remember = bool(b.get("remember"))
                tok = AUTH.issue_token((b.get("username") or "").strip().lower(), remember)
                self._set_cookie(tok, max_age=AUTH.idle_ttl(remember))
                return self._send(200, {"ok": True, "user": b.get("username")})
            if p == "/api/auth/login":
                tried = (b.get("username") or "").strip().lower()
                try:
                    remember = bool(b.get("remember"))
                    tok = AUTH.login(b.get("username"), b.get("password"), addr, remember)
                except PermissionError as e:
                    blocked = "too many" in str(e)
                    self._signin("signin-blocked" if blocked else "signin-failed", tried, ok=False, detail=str(e))
                    return self._send(401, {"error": str(e)})
                self._signin("signin", tried, detail="kept signed in" if remember else "")
                self._set_cookie(tok, max_age=AUTH.idle_ttl(remember))
                return self._send(200, {"ok": True, "remember": remember,
                                        "user": (b.get("username") or "").strip().lower()})
            if p == "/api/auth/logout":
                self._signin("signout", self.user)
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
                             "title": "Homestead test notification",
                             "body": "Notifications are enabled for this device. Choose categories in Settings › This device.",
                             "href": "/settings", "to": PUSH.tag(endpoint)})
                result = PUSH.send(lambda row: row["endpoint"] == endpoint, urgency="high")
                status = (result["statuses"] or [0])[0]
                if not result["sent"]:
                    return self._send(502, {"error": f"the push service refused the push (HTTP {status})"
                                            if status else "the push service could not be reached"})
                return self._send(200, {"ok": True})
            if p == "/api/alerts/pending":
                return self._send(200, alerts_pending(self.user, str(b.get("endpoint") or ""), b.get("confirm_delivery") is True))
            if p == "/api/alerts/acknowledge":
                try:
                    return self._send(200, ALERTS.acknowledge(self.user, b.get("key"), b.get("version"), b.get("undo") is True))
                except ALERTS.AlertChanged as error:
                    return self._send(409, {"error": str(error)})
            if p == "/api/alerts/delivered":
                endpoint, cursor = str(b.get("endpoint") or ""), b.get("latest")
                if not PUSH.mine(self.user, endpoint):
                    return self._send(404, {"error": "This notification device is not registered to your account."})
                if type(cursor) is not int or cursor < 0 or cursor > ALERTS.log(limit=0)["latest"]:
                    return self._send(400, {"error": "Invalid notification cursor."})
                PUSH.advance(self.user, endpoint, cursor)
                return self._send(200, {"ok": True})
            if p == "/api/auth/preferences/dashboard":
                try:
                    return self._send(200, AUTH.save_dashboard_preferences(self.user, b))
                except AUTH.StoreConflict as error:
                    return self._send(409, {"error": str(error)})
            if p == "/api/auth/password":
                try:
                    AUTH.change_password(self.user, b.get("old"), b.get("new"))
                except (PermissionError, ValueError) as error:
                    self._signin("password-failed", self.user, ok=False, detail=str(error))
                    raise
                self._signin("password", self.user)
                self._set_cookie(AUTH.issue_token(self.user))
                return self._send(200, {"ok": True})
            if p == "/api/auth/users":
                AUTH.create_user(b.get("username"), b.get("password"),
                                 role=b.get("role", "operator"))
                self._signin("user-added", (b.get("username") or "").strip().lower(),
                             detail=f"as {b.get('role', 'operator')}, by {self.user}")
                return self._send(200, {"ok": True, "users": AUTH.list_users()})
            if p == "/api/auth/role":
                AUTH.set_role(b["username"], b["role"], self.user)
                self._signin("role", b["username"], detail=f"now {b['role']}, by {self.user}")
                return self._send(200, {"ok": True, "users": AUTH.list_users()})
            if p == "/api/auth/users/delete":
                AUTH.delete_user(b.get("username"), self.user)
                ALERTS.forget_user(str(b.get("username") or "").strip().lower())
                self._signin("user-removed", b.get("username"), detail=f"by {self.user}")
                return self._send(200, {"ok": True, "users": AUTH.list_users()})
            if p == "/api/auth/signout-everywhere":
                AUTH.logout_everywhere(self.user)
                self._signin("signout-everywhere", self.user)
                self._set_cookie("", clear=True)
                return self._send(200, {"ok": True})
            if p == "/api/settings":
                return self._send(200, {"ok": True, **save_app_settings(b)})
            if p == "/api/deploy":
                return self._send(200, reviewed_deploy(b))
            if p == "/api/compose/parse":
                return self._send(200, compose_report(b))
            if p == "/api/compose/preview":
                return self._send(200, compose_preview(b))
            if p == "/api/compose/apply":
                return self._send(200, compose_apply(b))
            if p == "/api/portal":
                return self._send(200, PORTAL.save(b.get("links")))
            if p == "/api/self/replicas":
                return self._send(200, set_homestead_replicas(b.get("replicas")))
            if p == "/api/self/data/abandon":
                try:
                    return self._send(200, abandon_self_data_preparation(b))
                except SELF_DATA_FENCE.Held as error:
                    return self._send(409, {"error": str(error)})
            if p == "/api/self/data/move":
                try:
                    result, token = start_self_data_move(b, self.user)
                    secure = "Secure; " if self._over_tls() else ""
                    self._extra_headers.append(("Set-Cookie", f"homestead-data-move={token}; Path=/api/self/data/handoff/; HttpOnly; {secure}SameSite=Strict; Max-Age=86400"))
                    return self._send(202, result)
                except SELF_DATA_FENCE.Held as error:
                    return self._send(409, {"error": str(error), "review_required": True})
            if p == "/api/self/data/move/preview":
                try:
                    return self._send(200, preview_self_data_move(b, self.user))
                except SELF_DATA_FENCE.Held as error:
                    return self._send(409, {"error": str(error), "review_required": True})
            if p in ("/api/self/data/handoff/close", "/api/self/data/handoff/close/preview"):
                try:
                    return self._send(200, SELF_DATA_PREPARE.close_recovery(b, self.user, SELF.NS, NAMES.BRAND, OPS,
                        homestead_running_on, start=p == "/api/self/data/handoff/close"))
                except SELF_DATA_FENCE.Held as error:
                    return self._send(409, {"error": str(error), "review_required": True})
            if p in ("/api/self/data/prepare/archive", "/api/self/data/prepare/archive/preview"):
                try:
                    return self._send(200, SELF_DATA_PREPARE.archive(b, self.user, SELF.NS, NAMES.BRAND, OPS,
                        start=p == "/api/self/data/prepare/archive"))
                except SELF_DATA_FENCE.Held as error:
                    return self._send(409, {"error": str(error), "review_required": True})
            if p in ("/api/self/data/prepare", "/api/self/data/prepare/preview"):
                try:
                    return self._send(200, self_data_preparation(b, self.user, start=p == "/api/self/data/prepare"))
                except SELF_DATA_FENCE.Held as error:
                    return self._send(409, {"error": str(error), "review_required": True})
            if p == "/api/cluster/components/upgrade":
                result = COMPONENTS.upgrade(str(b.get("component") or ""), str(b.get("to") or ""), b)
                for key in ("components", "helm", "platform"):
                    _cache.pop(key, None)
                result["operation"] = OPS.start(
                    "platform-upgrade", f"Upgrade {result['name']} to {result['to']}",
                    {"kind": "Cluster" if result["component"] == "cluster" else "HelmChart", "name": result["name"],
                     "namespace": ""}, "/system/cluster",
                    {"component": result["component"], "name": result["name"], "from": result["from"],
                     "to": result["to"], "started": time.time(),
                     "phase": "controller" if result["component"] == "cluster" else "",
                     **({"held": result["held"]} if "held" in result else {}),
                     **({"v2_mode": result["v2_mode"]} if result.get("v2_mode") else {})},
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
            if p in ("/api/addons/longhorn", "/api/addons/kubevirt", "/api/addons/multus", "/api/addons/multus/repair", "/api/addons/kube-vip"):
                what = "multus" if p.endswith("/repair") else p.rsplit("/", 1)[1]
                result = {"longhorn": ADDONS.install_longhorn, "kubevirt": ADDONS.install_kubevirt,
                          "multus": ADDONS.repair_multus if p.endswith("/repair") else ADDONS.install_multus,
                          "kube-vip": ADDONS.install_kube_vip}[what](b)
                for key in ("helm", "platform"):
                    _cache.pop(key, None)
                result["operation"] = OPS.start("multus" if what == "multus" else "helm", f"{'Repair' if p.endswith('/repair') else 'Install'} {({'longhorn': 'Longhorn', 'kubevirt': 'KubeVirt', 'kube-vip': 'kube-vip'}).get(what, 'Multus')}",
                                                {"kind": "HelmChart", "name": result["name"], "namespace": ADDONS.CONTROLLER_NS},
                                                "/settings", {"namespace": ADDONS.CONTROLLER_NS, "name": result["job"],
                                                              "action": "install"},
                                                "Waiting for the Helm controller")
                return self._send(200, result)
            if p == "/api/vm/isos/folders":
                return self._send(200, ISOS.set_folders(b.get("folders") or []))
            if p == "/api/vm/isos/prepare":
                result = ISOS.prepare(str(b.get("share") or ""), str(b.get("path") or ""))
                _cache.pop("vms", None)
                return self._send(200, result)
            if p == "/api/vm/isos/keep":
                return self._send(200, ISOS.set_keep_days(b.get("days")))
            if p == "/api/vm/isos/delete":
                return self._send(200, ISOS.delete(str(b.get("name") or "")))
            if p == "/api/platform/baseline/install":
                which = [str(x) for x in (b.get("parts") or []) if str(x) in BASELINE.PARTS] or None
                results = BASELINE.install(which)
                for key in ("helm", "platform", "baseline", "components"):
                    _cache.pop(key, None)
                for row in results:
                    if row["ok"] and row.get("job"):
                        row["operation"] = baseline_operation(row, "Install")
                done = [BASELINE.NAMES[row["id"]] for row in results if row["ok"]]
                failed = [f"{BASELINE.NAMES[row['id']]}: {row['detail']}" for row in results if not row["ok"]]
                return self._send(200, {"ok": not failed, "results": results,
                                        "detail": (f"Installing {' and '.join(done)}" if done else "All required components are installed")
                                                  + (f". Failed: {'; '.join(failed)}" if failed else "")})
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
                try:
                    scale_workload(b["ns"], b["name"], int(b["replicas"]),
                                   confirm_capacity=b.get("confirm_capacity") is True,
                                   confirm_self=b.get("confirm_self") is True)
                except WorkloadRefused as refused:
                    return self._send(409, {"error": str(refused), "plan": refused.plan})
                return self._send(200, {"ok": True})
            if p == "/api/restart":
                return self._send(200, restart_workload(b["ns"], b["name"]))
            if p == "/api/image-updates/channel":
                settings = get_app_settings()
                settings["updates"]["channel"] = b.get("channel")
                return self._send(200, save_app_settings(settings))
            if p == "/api/image-updates/preview":
                return self._send(200, preview_image_update(b))
            if p == "/api/image-updates/apply":
                guard_managed_smb(b.get("ns"), b.get("name"))
                enforce_update_policy(b)
                result = reviewed_image_update(b, "update")
                result["operation"] = OPS.start(
                    "image-update", f"Update {b['name']}",
                    {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
                    "/containers?" + urllib.parse.urlencode({"find": b["name"]}),
                    {"namespace": b["ns"], "name": b["name"]})
                _cache.pop("wl", None); _cache.pop("ov", None); UPDATES.invalidate()
                return self._send(200, result)
            if p == "/api/image-updates/rollback":
                guard_managed_smb(b.get("ns"), b.get("name"))
                result = reviewed_image_update(b, "rollback")
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
                    b.get("namespace") or DEFAULT_NS, b.get("pvc"), b.get("path"), b.get("content"), b.get("revision")))
            if p == "/api/snapshot-files/start":
                return self._send(200, storage_volume_action(b.get("volume"), lambda: SNAPSHOT_FILES.start(b)))
            if p == "/api/snapshot-files/close":
                return self._send(200, SNAPSHOT_FILES.close(b.get("namespace"), b.get("session")))
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
                plan = IMP.import_cleanup_plan(b.get("name"), b.get("namespace"))
                if plan.get("journalled") and any(b.get(key) for key in ("remove_workload", "remove_volume", "remove_volumes")):
                    raise ValueError("This import retains workloads and volumes. Manage them separately after inspecting the copy.")
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
                result = IMP.delete_import(b.get("name"), b.get("namespace"))
                if result.get("journalled"):
                    return self._send(200, {**result, "removed": [], "releasing": []})
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
                return self._send(200, IMP.import_cleanup_plan(b.get("name"), b.get("namespace")))
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
                access = b.get("access_mode")
                if access == "ReadWriteMany" and (b.get("storage_class") or STORAGE_CLASS) not in shared_storage_classes():
                    # One SMB server mounts every share: a class that cannot
                    # serve many nodes (k3s's local-path) still serves it.
                    access = "ReadWriteOnce"
                result = SHARES.create_share(
                    b["name"], b.get("size_gb", 10), b.get("user", "lab"),
                    b.get("password"), b.get("public", False), b.get("read_only", False),
                    b.get("pvc"), b.get("sub_path", ""), b.get("storage_class"),
                    access, b.get("new_name", ""), str(b.get("samba_ip") or "").strip(), b.get("account_mode"))
                deployment = result.pop("deployment", None)
                if deployment:
                    result["operation"] = OPS.start(
                        "deployment", f"Create share {b['name']}",
                        {"kind": "Deployment", "name": SMB_NAME, "namespace": SMB_NAMESPACE},
                        "/shares", {"namespace": SMB_NAMESPACE, "name": SMB_NAME, "undo": "keep"},
                        "Restarting Samba with the new share")
                return self._send(200, {"ok": True, **result})
            if p == "/api/shares/users":
                result = SHARES.save_user(b.get("user"), b.get("password"), b.get("action", "create"))
                if result.pop("deployment", None):
                    result["operation"] = OPS.start(
                        "deployment", f"Update SMB user {result['user']}",
                        {"kind": "Deployment", "name": SMB_NAME, "namespace": SMB_NAMESPACE},
                        "/shares", {"namespace": SMB_NAMESPACE, "name": SMB_NAME, "undo": "keep"},
                        "Restarting Samba with updated credentials")
                return self._send(200, {"ok": True, **result})
            if p == "/api/shares/users/delete":
                return self._send(200, {"ok": True, **SHARES.delete_user(b.get("user"))})
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
                        "share-remove", f"Remove share {b['name']}",
                        {"kind": "Deployment", "name": SMB_NAME, "namespace": SMB_NAMESPACE},
                        "/shares", {"namespace": SMB_NAMESPACE, "name": SMB_NAME, "undo": "keep",
                                    "uid": deployment.get("metadata", {}).get("uid"), "since": time.time(),
                                    "removed_claims": result.get("removed_claims", [])},
                        "Restarting Samba without the removed share")
                return self._send(200, {"ok": True, **result})
            if p == "/api/shares/nfs":
                return self._send(200, set_nfs_export(
                    b.get("name", ""), str(b.get("clients") or "").strip(),
                    b.get("read_only") is not False))
            if p == "/api/appstore/install":
                cfg = template_to_cfg(b["app"])
                cfg.update(b.get("overrides") or {})
                # Older API clients must use the same review as the current UI.
                # The token is issued by /api/preview for the resolved template
                # plus overrides, not for the catalogue identifier alone.
                cfg.update(capacity_token=b.get("capacity_token"), confirm_capacity=b.get("confirm_capacity") is True)
                result = reviewed_deploy(cfg)
                reused = result.get("reused_volumes") or []
                if reused:
                    result["detail"] = f"kept the existing {', '.join(reused)} - its data carries on"
                return self._send(200, result)
            if p == "/api/edit/preview":
                guard_managed_smb(b.get("ns", ""), b.get("name", ""))
                _, context, plan = edit_capacity_plan(b)
                return self._send(200, {"capacity": plan, "capacity_token": CAPACITY_REVIEW.issue(b, context)})
            if p == "/api/edit":
                guard_managed_smb(b.get("ns", ""), b.get("name", ""))
                prepared, context, plan = edit_capacity_plan(b)
                CAPACITY_REVIEW.enforce(b, plan, context)
                if context.get("action") == RENAME.KIND:
                    def admission(dep):
                        return PLACE.manifest_plan(dep, b["ns"], dep["metadata"]["name"], dep["spec"]["replicas"],
                            get_app_settings()["thresholds"]["memory"]["critical"], read=kget,
                            nodes_snapshot=PLACE.get_nodes(), pod_snapshot=ROLLOUT_CAPACITY.items(kget, "/api/v1/pods"))
                    result = RENAME.dispatch(b, context, prepared["deployment"], kget, ksend, OPS, admission)
                    _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("network", None)
                    return self._send(200, result)
                moves = RESTRUCTURE.copies(b)
                guard_self(b.get("ns", ""), b.get("name", ""),
                           stopping=("autostart" in b and not b["autostart"]) or
                                    ("replicas" in b and int(b.get("replicas") or 0) == 0),
                           confirmed=b.get("confirm_self") is True, moving=bool(moves))
                if moves:
                    result = COPY_JOB.start(b, context, prepared, moves, kget, OPS,
                                            lambda: apply_reviewed_edit(b, prepared, hold=True))
                else:
                    result = apply_reviewed_edit(b, prepared)
                return self._send(200, result)
            if p == "/api/move/preview":
                return self._send(200, preview_host_move(b))
            if p == "/api/move":
                result = reviewed_host_move(b)
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
                return self._move(lambda: MOVE.setup_storage(b.get("name"), b.get("size_gb") or 100, b.get("lb_ip") or "",
                                                             b.get("vip_mode") or "", int(b.get("port") or 0)))
            if p == "/api/cluster/remove-node":
                return self._move(lambda: ONBOARD.remove_node(b.get("node"), bool(b.get("accept_loss")),
                                                              bool(b.get("gone"))))
            if p == "/api/cluster/cleanup/run":
                return self._move(lambda: ONBOARD.cleanup(b.get("kind"), b.get("name") or "", bool(b.get("force"))))
            if p == "/api/move/source":
                action, kind, name = b.get("action"), b.get("kind"), b.get("name")
                identity = {"transfer_id": str(b.get("transfer_id") or ""),
                            "expected_uid": str(b.get("expected_uid") or "")}
                actions = {"quiesce": lambda: MOVE_SOURCE.quiesce(kind, name, **identity,
                                                                 expected_version=str(b.get("expected_version") or "")),
                           "backup": lambda: MOVE_SOURCE.backup(kind, name, bool(b.get("retry_failed")),
                                                                 b.get("claims") if isinstance(b.get("claims"), list) else None,
                                                                 **identity, cleanup_id=str(b.get("cleanup_id") or "")),
                           "cleanup": lambda: MOVE_SOURCE.cleanup(identity["transfer_id"], identity["expected_uid"]),
                           "release": lambda: MOVE_SOURCE.release(kind, name, **identity),
                           "remove": lambda: MOVE_SOURCE.remove(
                               kind, name, bool(b.get("volumes")),
                               b.get("claims") if isinstance(b.get("claims"), list) else None)}
                if action not in actions:
                    return self._send(400, {"error": "unknown move action"})
                return self._move(lambda: MOVE_SOURCE.in_namespace(b.get("namespace"), actions[action]))
            if p in ("/api/move/plan", "/api/move/start"):
                if p.endswith("/start") and (b.get("kind") or "container") == "container":
                    guard_managed_smb(b.get("namespace") or DEFAULT_NS, b.get("name"))
                call = MOVE_ENGINE.plan if p.endswith("plan") else MOVE_ENGINE.start
                return self._move(lambda: call(
                    b.get("cluster"), b.get("kind") or "container", b.get("name"),
                    b.get("namespace") or DEFAULT_NS, b.get("address_mode") or "shared",
                    b.get("address") or "", b.get("storage_class") or "",
                    b.get("volumes") if isinstance(b.get("volumes"), dict) else None,
                    b.get("transfer_mode") or "move", b.get("source_namespace") or "", b.get("host_devices")))
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
            if p == "/api/objectstore/transfers":
                return self._move(lambda: OBJECTS.set_transfers(bool(b.get("allow")), int(b.get("size_gb") or 100),
                                                                str(b.get("lb_ip") or ""), str(b.get("vip_mode") or ""),
                                                                int(b.get("port") or 0)))
            if p == "/api/move/clusters/transfers":
                return self._move(lambda: MOVE.transfers(b.get("name"), b.get("allow"), b.get("size_gb") or 100,
                                                         b.get("lb_ip") or "", b.get("vip_mode") or "",
                                                         int(b.get("port") or 0)))
            if p == "/api/objectstore/longhorn":
                return self._send(200, OBJECTS.request_target(replace=bool(b.get("replace", True))))
            if p == "/api/move/clusters/target":
                return self._move(lambda: MOVE.remote(b.get("name"), "/api/objectstore/longhorn", {"replace": True}))
            if p == "/api/objectstore/remove":
                return self._send(200, OBJECTS.remove(bool(b.get("keep_data", True))))
            if p == "/api/node/probe/install":
                return self._send(200, PROBE.install(HOMESTEAD_VERSION))
            if p == "/api/node/probe/remove":
                return self._send(200, PROBE.remove())
            if p == "/api/node/probe/allocation":
                return self._send(200, ALLOCATION_PROBE.configure(b, HOMESTEAD_VERSION))
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
                force = b.get("force") is True
                running = active_power_job(b.get("node"))
                if running:
                    return self._send(409, {"error": "A power job for this host is still running; follow it instead of sending another",
                                            "operation": running})
                power_plan = POWER.plan(b["node"], b["action"], force=force)
                if not power_plan["ready"]:
                    return self._send(409, {"error": "; ".join(power_plan["blockers"]), "plan": power_plan})
                if b.get("review_token") != power_plan["review_token"]:
                    return self._send(409, {"error": "host impact changed; review the plan again", "plan": power_plan})
                if power_plan.get("planned_outage") and b.get("allow_cluster_outage") is not True:
                    return self._send(409, {"error": "acknowledge the whole-cluster outage before host power control",
                                            "plan": power_plan})
                power_plan["choices"] = HOLD.choose(power_plan.get("hold") or [], b.get("choices") or {})
                stranded = {(w["ns"], w["name"]) for w in power_plan["stranded"]}
                if any((i["ns"], i["name"]) in stranded and power_plan["choices"].get(i["id"]) == "move"
                       for i in power_plan.get("hold") or []) and not b.get("allow_stranded"):
                    return self._send(409, {"error": "some workloads have no eligible failover host",
                                            "plan": power_plan})
                if power_plan["requires_data_ack"] and not b.get("allow_data_risk"):
                    return self._send(409, {"error": "acknowledge the volume risk before host power control",
                                            "plan": power_plan})
                # Cordon and drain can take many minutes: the request returns
                # the job at once and the browser follows its phases.
                return self._send(202, send_reviewed_power(power_plan, force, background=True))
            if p == "/api/node/power/release":
                return self._send(200, release_held_power(str(b.get("id") or "")))
            if p == "/api/cluster/shutdown":
                return self._send(202, cluster_shutdown(review=True).start(b, OPS))
            if p == "/api/cluster/shutdown/cancel":
                return self._send(200, cluster_shutdown().cancel())
            if p == "/api/cluster/shutdown/recover":
                return self._send(200, cluster_shutdown().recover(b.get("run")))
            if p == "/api/vm/migrate":
                ns = b.get("ns", DEFAULT_NS)
                result = LC.vm_migrate(ns, b["name"], b.get("target"))
                if result.get("migration"):
                    result["operation"] = OPS.start(
                        "vm-migration", f"Migrate {b['name']}",
                        {"kind": "VirtualMachine", "name": b["name"], "namespace": ns},
                        "/vms", {"namespace": ns, "name": result["migration"]})
                return self._send(200, result)
            if p == "/api/vm/power/preview":
                if b.get("action") in ("start", "restart"):
                    ISOS.unlock(b.get("ns") or DEFAULT_NS)      # before it is reviewed, so the review sees it
                return self._send(200, preview_vm_power(b))
            if p == "/api/vm/power":
                _cache.pop("vms", None)
                return self._send(200, reviewed_vm_power(b))
            if p == "/api/vm/edit/preview":
                return self._send(200, preview_vm_edit(b))
            if p == "/api/vm/edit":
                _cache.pop("vms", None)
                return self._send(200, reviewed_vm_edit(b))
            if p == "/api/vm/delete":
                _cache.pop("vms", None)
                return self._send(200, VMS.delete(b.get("ns", DEFAULT_NS), b.get("name", ""), bool(b.get("disks"))))
            if p == "/api/disks/v2/plan":
                return self._send(200, DISK_V2.review(b))
            if p == "/api/disks/v2/start":
                return self._send(200, DISK_V2.start(b))
            if p == "/api/disks/v2/prepare-review":
                return self._send(200, DISK_V2.prepare_review(b.get("id", "")))
            if p == "/api/disks/v2/prepare":
                return self._send(200, DISK_V2.prepare(b))
            if p == "/api/disks/retire/plan":
                return self._send(200, DISKS.retire_plan(b.get("node", ""), b.get("disk", "")))
            if p == "/api/disks/retire":
                op = DISKS.retire_start(b, OPS)
                for key in ("disks", "lhcap", "nodes", "ov"):
                    _cache.pop(key, None)
                return self._send(200, {"ok": True, "operation": op})
            if p == "/api/passthrough/inspect":
                return self._send(200, PASSTHROUGH.inspect(str(b.get("node") or "")))
            if p == "/api/passthrough/vbios/capture":
                return self._send(200, PASSTHROUGH.capture_vbios(str(b.get("node") or ""), str(b.get("address") or "")))
            if p == "/api/passthrough/iommu":
                return self._send(200, PASSTHROUGH.enable_iommu(str(b.get("node") or "")))
            if p == "/api/passthrough/pci":
                node, address = str(b.get("node") or ""), str(b.get("address") or "")
                return self._send(200, PASSTHROUGH.give(node, address) if b.get("give", True)
                                  else PASSTHROUGH.take_back(node, address))
            if p == "/api/passthrough/usb":
                if b.get("harvester_name"):
                    return self._send(200, PASSTHROUGH.harvester_usb(str(b.get("node") or ""), str(b["harvester_name"]),
                                                                     b.get("allow", True) is not False))
                return self._send(200, PASSTHROUGH.allow_usb(b.get("vendor"), b.get("product"), b.get("allow", True) is not False))
            if p == "/api/os-updates/settings":
                return self._send(200, {"ok": True, "settings": OS_ROLLOUT.save_settings(b)})
            if p == "/api/os-updates/start":
                rollout = OS_ROLLOUT.start("asked")
                return self._send(200, {"ok": True, "rollout": rollout, "operation": rollout.get("operation"),
                                        "detail": f"Updating {len(rollout['nodes'])} hosts one at a time; follow it in the job tray"})
            if p == "/api/os-updates/stop":
                return self._send(200, {"ok": True, "rollout": OS_ROLLOUT.stop(),
                                        "detail": "Stopping once the host being updated is done"})
            if p == "/api/node/os/check":
                node = str(b.get("node") or "")
                if not node:
                    raise ValueError("which host?")
                facts = HOST_OS.read(node, refresh=True)
                return self._send(200, {"ok": True, "facts": {**facts, "summary": HOST_OS.summary(facts)}})
            if p == "/api/host-console":
                # The add-on on or off for every host; or one host looked at again.
                if "enabled" in b:
                    return self._send(200, HOST_CONSOLE.set_cluster(bool(b.get("enabled")), self.user or ""))
                return self._send(200, {"ok": True, "operation": HOST_CONSOLE.start(
                    str(b.get("node") or ""), str(b.get("action") or "inspect"))})
            if p == "/api/node/os/upgrade":
                node = str(b.get("node") or "")
                if not node:
                    raise ValueError("which host?")
                return self._send(200, {"ok": True, "operation": HOST_OS.upgrade_start(node, OPS),
                                        "detail": f"Installing updates on {node}; follow it in the job tray"})
            if p == "/api/node/bridge/inspect":
                return self._send(200, HOST_BRIDGE.inspect(str(b.get("node") or "")))
            if p == "/api/node/bridge":
                node = str(b.get("node") or "")
                if not node or str(b.get("confirm") or "").strip() != node:
                    raise ValueError(f"type the host's name, {node}, to confirm")
                op = HOST_BRIDGE.start(node, OPS)
                _cache.pop("network", None)
                return self._send(200, {"ok": True, "operation": op,
                                        "detail": f"{node} is moving to {HOST_BRIDGE.BRIDGE}; follow it in the job tray"})
            if p == "/api/disks/os-space":
                return self._send(200, DISKS.os_space(str(b.get("node") or "")))
            if p == "/api/disks/os-space/use":
                result = DISKS.use_os_space(b)
                with _lock:
                    for key in [k for k in _cache if k.startswith(("disk", "stor", "lhcap"))]:
                        _cache.pop(key, None)
                return self._send(200, result)
            if p == "/api/disks/inspect":
                return self._send(200, DISKS.inspect_disk(str(b.get("node") or ""), str(b.get("device") or "")))
            if p == "/api/disks/setup":
                result = DISKS.set_up(b)
                with _lock:
                    for key in [k for k in _cache if k.startswith(("disk", "stor", "lhcap"))]:
                        _cache.pop(key, None)
                return self._send(200, result)
            if p in ("/api/disks/add", "/api/disks/scheduling", "/api/disks/evict", "/api/disks/remove"):
                action = p.rsplit("/", 1)[1]
                result = (DISKS.add(b) if action == "add"
                          else DISKS.set_scheduling(b.get("node", ""), b.get("disk", ""), b.get("allow", True)) if action == "scheduling"
                          else DISKS.evict(b.get("node", ""), b.get("disk", ""), b.get("on", True)) if action == "evict"
                          else DISKS.remove(b.get("node", ""), b.get("disk", "")))
                for key in ("disks", "lhcap", "nodes", "ov"):
                    _cache.pop(key, None)
                return self._send(200, result)
            if p == "/api/disks/name":
                result = DISKS.set_disk_name(b.get("node", ""), b.get("device", ""), b.get("name", ""))
                _cache.pop("disks", None)
                return self._send(200, result)
            if p in ("/api/disks/tags", "/api/disks/node-tags"):
                result = (DISKS.set_disk_tags(b.get("node", ""), b.get("disk", ""), b.get("tags") or [])
                          if p.endswith("/tags") and not p.endswith("node-tags")
                          else DISKS.set_node_tags(b.get("node", ""), b.get("tags") or []))
                _cache.pop("disks", None)
                return self._send(200, result)
            if p == "/api/longhorn/settings":
                _cache.pop("lhcap", None)
                with OPS._lock:
                    if "v2" in b and not b["v2"] and DISK_V2.tasks():
                        raise ValueError("Finish or stop the saved V2 disk preparation task before disabling V2")
                    return self._send(200, LHCAP.save(b))
            if p == "/api/longhorn/offline-rebuilding":
                _cache.pop("lhrebuild", None)
                enabled = b.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false")
                settings = get_app_settings()
                settings["longhorn"] = {"offline_rebuilding": enabled}
                save_app_settings(settings)
                return self._send(200, LHREBUILD.save(enabled))
            if p == "/api/workloads/rebalance":
                running = next((i for i in OPS.list_operations() if i.get("kind") == CREBALANCE.KIND
                                and i.get("status") not in OPS.TERMINAL), None)
                if running:
                    return self._send(409, {"error": "A container rebalance is already running; follow it in Jobs", "operation": running})
                if b.get("restart") is not True:
                    return self._send(409, {"error": "Moving a container restarts it; confirm the restarts"})
                if b.get("moves") is not None:
                    # The moves reviewed, checked against the cluster now -
                    # not a plan made again from load that moves by the second.
                    try:
                        moves = CREBALANCE.reviewed(b["moves"], b.get("exclude") or [], b.get("review_token"))
                    except ValueError as error:
                        return self._send(409, {"error": str(error), "plan": CREBALANCE.plan(b.get("exclude") or [])})
                    plan = {"moves": moves, "excluded": sorted(b.get("exclude") or [])}
                else:
                    plan = CREBALANCE.plan(b.get("exclude") or [])
                    if plan["review_token"] != b.get("review_token"):
                        return self._send(409, {"error": "Container load changed since the review; review again", "plan": plan})
                if not plan["moves"]:
                    return self._send(409, {"error": "Nothing worth moving: no move brings the busiest host down enough", "plan": plan})
                operation = OPS.start(CREBALANCE.KIND, f"Rebalance {len(plan['moves'])} container{'' if len(plan['moves']) == 1 else 's'}",
                                      {"kind": "Deployment", "name": "rebalance"}, "/containers",
                                      {"moves": plan["moves"], "index": 0, "moved": 0, "stage": "move", "excluded": plan["excluded"]},
                                      "Starting with the first container")
                return self._send(202, {"operation": operation})
            if p == "/api/longhorn/rebalance":
                running = next((i for i in OPS.list_operations() if i.get("kind") == REBALANCE.KIND
                                and i.get("status") not in OPS.TERMINAL), None)
                if running:
                    return self._send(409, {"error": "A rebalance is already running; follow it in Jobs", "operation": running})
                if b.get("moves") is not None:
                    # The copies reviewed, checked against Longhorn now - not a
                    # plan made again from sizes that grow while apps write.
                    try:
                        moves = REBALANCE.reviewed(b["moves"], b.get("exclude") or [], b.get("review_token"))
                    except ValueError as error:
                        return self._send(409, {"error": str(error), "plan": REBALANCE.plan(b.get("exclude") or [])})
                    plan = {"moves": moves, "excluded": sorted(b.get("exclude") or [])}
                else:
                    plan = REBALANCE.plan(b.get("exclude") or [])
                    if plan["review_token"] != b.get("review_token"):
                        return self._send(409, {"error": "Volume copies changed since the review; review again", "plan": plan})
                if not plan["moves"]:
                    return self._send(409, {"error": "Nothing to move: the hosts are as even as they can be", "plan": plan})
                operation = OPS.start(REBALANCE.KIND, f"Rebalance {len(plan['moves'])} volume cop{'y' if len(plan['moves']) == 1 else 'ies'}",
                                      {"kind": "Volume", "name": "rebalance"}, "/volumes",
                                      {"moves": plan["moves"], "index": 0, "moved": 0, "stage": "add", "excluded": plan["excluded"]},
                                      "Starting with the first copy")
                return self._send(202, {"operation": operation})
            if p == "/api/longhorn/rebuild":
                _cache.pop("lhrebuild", None)
                _cache.pop("volumes", None)
                return self._send(200, LHREBUILD.rebuild_now(str(b.get("volume") or "")))
            if p == "/api/longhorn/v2/prepare":
                return self._send(200, LHV2_SETUP.prepare(b))
            if p == "/api/longhorn/v2/upgrade/review":
                return self._send(200, LHV2_UPGRADE.settings_review(b.get("enabled"), b.get("timeout"))[0])
            if p == "/api/longhorn/v2/upgrade/settings":
                return self._send(200, LHV2_UPGRADE.configure(b, OPS))
            if p == "/api/longhorn/v2/enable":
                result = LHV2_SETUP.enable(b)
                _cache.pop("lhcap", None)
                return self._send(200, result)
            if p == "/api/vm/k3s-cluster/plan":
                return self._send(200, preview_vm_cluster(b))
            if p == "/api/vm/k3s-cluster":
                return self._send(200, {"ok": True, "operation": reviewed_vm_cluster(b)})
            if p == "/api/vm/create/preview":
                return self._send(200, preview_vm_create(b))
            if p == "/api/vm/create":
                _cache.pop("vms", None)
                return self._send(200, reviewed_vm_create(b))
            if p == "/api/node/shell/prepare":
                # Starts the node's helper and says plainly if it cannot,
                # before the terminal connects - a refused WebSocket says nothing.
                target = NODESHELL.open_shell(str(b.get("node") or ""))
                return self._send(200, {"ok": True, "node": target["node"]})
            if p == "/api/vm/store/keep":
                _cache.pop("vmimages", None)
                return self._send(200, VMSTORE.keep(str(b.get("id") or ""), b.get("auto", True) is not False))
            if p == "/api/vm/store/auto":
                return self._send(200, VMSTORE.set_auto(str(b.get("id") or ""), bool(b.get("auto"))))
            if p == "/api/vm/store/forget":
                _cache.pop("vmimages", None)
                return self._send(200, VMSTORE.forget(str(b.get("id") or "")))
            if p == "/api/vm/store/refresh":
                _cache.pop("vmimages", None)
                return self._send(200, VMSTORE.refresh(force=True))
            if p == "/api/vm-disks/import":
                result = IMP.import_vm_disk(b, ops=OPS)
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
            if p == "/api/volumes/repair-class":
                return self._send(200, repair_volume_class(b))
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
            if p == "/api/network/vips/default":
                result = NETWORK.set_default_vip(b.get("ip", ""))
                for key in ("network", "ov"):
                    _cache.pop(key, None)
                return self._send(200, result)
            if p == "/api/network/vips/remove":
                _cache.pop("network", None)
                return self._send(200, NETWORK.remove_vip(b.get("ip", "")))
            if p == "/api/network/vips/change":
                result = NETWORK.change_vip(b.get("old", ""), b.get("new", ""), apply=b.get("apply") is True)
                if b.get("apply") is True:
                    for key in ("network", "ov"):
                        _cache.pop(key, None)
                    # Backups follow the store to its new address.
                    if any(s["name"].startswith(OBJECTS.NAME) and s["namespace"] == OBJECTS.NS for s in result["services"]):
                        try:
                            pointed = OBJECTS.request_target()
                            result["detail"] += "; " + pointed["detail"]
                        except Exception as error:
                            result["detail"] += f"; Longhorn's backup target was not changed: {str(error)[:120]}"
                    # Linked clusters reach this one at its new VIP, where it was on the old.
                    try:
                        if FLEET.follow_address((f"http://{str(b.get('old') or '').strip()}:{SELF_ADDRESS.WEB_PORT}",)):
                            result["detail"] += "; linked clusters were told the new address"
                    except Exception:
                        pass
                return self._send(200, result)
            if p == "/api/self/address/plan":
                return self._send(200, SELF_ADDRESS.plan(str(b.get("vip") or "").strip()))
            if p == "/api/self/address":
                vip = str(b.get("vip") or "").strip()
                result = SELF_ADDRESS.move(vip)
                if b.get("default"):
                    try:
                        NETWORK.set_default_vip(vip)
                        result["default"] = True
                    except ValueError as error:
                        result["default_error"] = str(error)
                for key in ("network", "ov"):
                    _cache.pop(key, None)
                result["fleet_address"] = _follow_fleet_address()
                return self._send(200, result)
            if p == "/api/welcome/done":
                SHARED.write_json(os.path.join(DATA_DIR, "welcome.json"),
                                  {"done": True, "by": str(self.user or ""), "at": int(time.time())})
                return self._send(200, {"ok": True})
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
                return self._send(200, STORAGE_WORKFLOW.preview({**b, "namespace": b.get("namespace") or DEFAULT_NS}, self.user, OPS,
                                                               runtime_check=storage_runtime_check))
            if p == "/api/volumes/reclass/start":
                op = STORAGE_WORKFLOW.start_reviewed({**b, "namespace": b.get("namespace") or DEFAULT_NS}, self.user, OPS,
                                                    runtime_check=storage_runtime_check)
                _cache.pop("vol", None)
                return self._send(200, {"ok": True, "operation": op})
            if p == "/api/volumes/old-copies/remove":
                _cache.pop("vol", None)
                return self._send(200, RECLASS.remove_old_copy(b.get("pv", ""), OPS))
            if p == "/api/volumes/delete":
                with OPS._lock:
                    result = VOLUMES.delete(b, validate=lambda plan: STORAGE_GUARD.review(plan, OPS, kget))
                    result["operation"] = OPS.start(
                        "volume-delete", f"Delete volume {result['name']}",
                        {"kind": "PersistentVolumeClaim", "name": result["name"],
                         "namespace": result["namespace"]},
                        "/volumes", {"namespace": result["namespace"], "name": result["name"],
                                      "action": result["action"], "pv": result["pv"],
                                      "orphan": result.get("orphan", False),
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
            if p == "/api/lh/trim":
                return self._send(200, LH.trim_volume(b.get("volume")))
            if p == "/api/lh/snapshot/delete":
                return self._send(200, {"ok": True, "operation": storage_volume_action(
                    b.get("volume"), lambda: SNAPSHOT_DELETE.start(b, OPS))})
            if p == "/api/lh/snapshot/revert":
                operation = storage_volume_action(b.get("volume"), lambda: REVERT.start(
                    str(b.get("volume") or ""), str(b.get("snapshot") or ""), OPS))
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
                with OPS._lock:
                    plan = LH.restore_plan(
                        b.get("backup"), b.get("namespace", DEFAULT_NS), b.get("name"))
                    if plan.get("conflict"):
                        return self._send(409, {"error": plan["conflict"]["message"], "plan": plan})
                    b["restore_id"] = secrets.token_hex(12)
                    b["annotations"] = {"homestead.io/restore-id": b["restore_id"]}
                    result = LH.restore_backup(b)
                    b["source_size_bytes"] = result["source_size_bytes"]
                    result["operation"] = OPS.start(
                        "volume-restore", f"Restore {result['name']}",
                        {"kind": "PersistentVolumeClaim", "name": result["name"],
                         "namespace": result["namespace"]},
                        "/volumes?" + urllib.parse.urlencode({"find": result["name"]}),
                        {"namespace": result["namespace"], "name": result["name"],
                         "backup": result["backup"], "restore_config": b,
                         "restore_started": result["created"]}, result["message"])
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
                    b.get("kind", "unraid"), b.get("base_path", "/mnt/user/appdata"), b.get("port", 22))})
            if p == "/api/sources/scan":
                return self._send(200, IMP.scan_source(b.get("name"), self.user))
            if p == "/api/sources/trust":
                return self._send(200, IMP.trust_source(b, self.user))
            if p == "/api/sources/delete":
                return self._send(200, {"ok": True, "sources": IMP.del_source(b["name"])})
            if p == "/api/sources/browse":
                return self._send(200, {"entries": IMP.browse_source(b["name"], b.get("path"))})
            if p == "/api/sources/containers":
                return self._send(200, {"containers": IMP.source_containers(b["name"])})
            if p == "/api/sources/vms":
                return self._send(200, UNRAID_VMS.listing(str(b.get("name") or "")))
            if p == "/api/sources/vms/shutdown":
                return self._send(200, UNRAID_VMS.shutdown(str(b.get("source") or ""), b.get("vm")))
            if p == "/api/vms/import-unraid":
                return self._send(200, {"ok": True, "operation": UNRAID_VMS.start(b, self.user or "")})
            if p == "/api/sources/inspect":
                return self._send(200, IMP.inspect_source_container(b["name"], b["container"]))
            if p == "/api/import/preview":
                return self._send(200, preview_import(b))
            if p == "/api/import":
                result = reviewed_import(b)
                return self._send(200, result)
            if p == "/api/operations/resume":
                return self._send(200, OPS.resume(b.get("id", "")))
            if p == "/api/operations/power-recovery/preview":
                return self._send(200, VM_POWER_RECOVERY.preview(b.get("id", ""), OPS, kget, self.user))
            if p == "/api/operations/power-recovery/resolve":
                return self._send(200, VM_POWER_RECOVERY.resolve(b, OPS, kget, self.user))
            if p == "/api/operations/vm-recovery/preview":
                return self._send(200, VM_MUTATION_RECOVERY.preview(b.get("id", ""), OPS, kget, self.user))
            if p == "/api/operations/vm-recovery/resolve":
                return self._send(200, VM_MUTATION_RECOVERY.resolve(b, OPS, kget, self.user))
            if p == "/api/operations/storage-recovery/preview":
                return self._send(200, STORAGE_RECOVERY.preview(b.get("id", ""), OPS, kget, self.user,
                    storage_helper_admission, storage_restart_admission, runtime_check=storage_runtime_check))
            if p == "/api/operations/storage-recovery/act":
                return self._send(200, STORAGE_RECOVERY.act(b, OPS, kget, self.user,
                    storage_helper_admission, storage_restart_admission, runtime_check=storage_runtime_check))
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
            if p == "/api/firewall/preview":
                return self._send(200, FIREWALL.preview(b))
            if p == "/api/firewall/save":
                return self._send(200, FIREWALL.save(b))
            if p == "/api/firewall/delete":
                return self._send(200, FIREWALL.remove(b))
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
                b = prepare_deploy_network(b)
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
            return self._send(503, {"error": str(e), "cause": getattr(e, "cause", ""), "unavailable": True})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": API_ERRORS.message(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    @self_data_request
    def do_DELETE(self):
        self._begin()
        u = urllib.parse.urlparse(self.path)
        target = self._fleet_target(u.path)
        if target:
            return self._fleet_forward(target, u.path)
        if self._guard(urllib.parse.urlparse(self.path).path):
            return
        parts = [x for x in u.path.split("/") if x]
        try:
            if len(parts) == 4 and parts[:2] == ["api", "workload"]:
                return self._send(200, {"ok": True, "services": delete_workload(parts[2], parts[3])})
            return self._send(404, {"error": "no route"})
        except AUTH.StoreUnavailable as e:
            # Not an empty account store: the cluster did not answer.
            return self._send(503, {"error": str(e), "cause": getattr(e, "cause", ""), "unavailable": True})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self._send(e.code, {"error": API_ERRORS.message(e, 500)})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def _reconcile_permissions():
    """Bring Homestead's own ClusterRole up to this release, before anything needs it."""
    try:
        with self_data_activity():
            result = SELF.reconcile()
    except Exception as error:
        print(f"permissions: not checked ({str(error)[:120]})", flush=True)
        return
    print(f"permissions: {result['state']} - {result['detail']}", flush=True)
    try:
        with self_data_activity():
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
        with self_data_activity():
            result = PROBE.reconcile(HOMESTEAD_VERSION)
    except Exception as error:
        print(f"node probe: not updated ({str(error)[:120]})", flush=True)
        return
    if result["state"] in ("updated", "error"):
        print(f"node probe: {result['detail']}", flush=True)
    try:
        for attempt in range(7):
            with self_data_activity():
                result = ALLOCATION_PROBE.reconcile(HOMESTEAD_VERSION)
            if result["state"] != "waiting" or attempt == 6:
                break
            # Background read-only waiting, never retry an uncertain PATCH.
            time.sleep(10)
        if result["state"] in ("updated", "review", "waiting"):
            print(f"VM placement checks: {result['detail']}", flush=True)
    except Exception:
        print("VM placement helper not updated; review its settings in Cluster > Add-ons", flush=True)


def _samba_loop():
    """One leader keeps SMB's mounts and image aligned with saved settings."""
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
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
                with self_data_activity():
                    nfs_result = reconcile_nfs()
                if nfs_result.get("state") == "updated":
                    print("network shares: restored NFS exports from the share inventory", flush=True)
            except Exception as error:
                print(f"NFS exports: {str(error)[:180]}", flush=True)
            try:
                with self_data_activity():
                    moved = OBJECTS.keep_in_step()
                    OBJECTS.reconcile_target()
                if moved:
                    print(f"object store: reconciled {moved}", flush=True)
            except Exception as error:
                print(f"object store: {str(error)[:180]}", flush=True)
        time.sleep(60)


def start_background_tasks():
    """Start only after normal boot or the independent move's verified completion."""
    require_self_data_write()
    threading.Thread(target=DIAGNOSTICS.run, name="diagnostics-expiry", daemon=True).start()
    threading.Thread(target=_storage_runtime_loop, daemon=True).start()
    threading.Thread(target=_sampler, daemon=True).start()
    threading.Thread(target=_reconcile_permissions, daemon=True).start()
    threading.Thread(target=_upgrade_node_probe, daemon=True).start()
    # Which replica leads: the one that raises alerts and advances moves.
    threading.Thread(target=LEADER.run, daemon=True).start()
    # Moves carry on across restarts: their state is on disk, and this resumes it.
    threading.Thread(target=_moves_loop, daemon=True).start()
    # Older join plans each kept a join token in a Secret.
    threading.Thread(target=ONBOARD.tidy_old_plans, daemon=True).start()
    threading.Thread(target=_alerts_loop, daemon=True).start()
    threading.Thread(target=_power_jobs_loop, daemon=True).start()
    threading.Thread(target=_detached_copies_loop, daemon=True).start()
    threading.Thread(target=_host_fix_loop, daemon=True).start()
    threading.Thread(target=_longhorn_copies_loop, daemon=True).start()
    threading.Thread(target=_host_console_loop, daemon=True).start()
    threading.Thread(target=_storage_pending_loop, daemon=True).start()
    threading.Thread(target=_snapshot_files_loop, daemon=True).start()
    threading.Thread(target=_files_loop, daemon=True).start()
    threading.Thread(target=_os_updates_loop, daemon=True).start()
    threading.Thread(target=_baseline_loop, daemon=True).start()
    threading.Thread(target=_vip_loop, daemon=True).start()
    threading.Thread(target=MQTT.run, daemon=True).start()
    threading.Thread(target=_history_loop, daemon=True).start()
    threading.Thread(target=fit_own_strategy, daemon=True).start()
    threading.Thread(target=_hardware_loop, daemon=True).start()
    threading.Thread(target=_samba_loop, daemon=True).start()
    threading.Thread(target=_vmstore_loop, daemon=True).start()
    threading.Thread(target=finish_self_data_helpers, name="data-move-cleanup", daemon=True).start()


def finish_self_data_helpers():
    if _self_data_fence is None: return
    while True:
        try:
            with self_data_activity():
                result = SELF_DATA_FINISH.finish(_self_data_fence, kget, ksend)
                if result["done"]:
                    SELF_DATA_EXECUTE.reconcile_completed(OPS, DATA_DIR, SELF.NS, NAMES.BRAND)
            if result["done"]: return
        except SELF_DATA_FENCE.Held as error:
            print("Data move helper cleanup needs review: " + str(error), flush=True)
            return
        except Exception:
            # The verified destination stays usable. Do not replay a delete or
            # remove either PVC to make cleanup appear successful.
            print("Data move helper cleanup needs review; both volumes are retained", flush=True)
            return
        time.sleep(2)


def finish_self_data_boot():
    """Keep the already-bound HTTP app read-only until the coordinator finishes.

    This thread never advances the coordinator and never repairs/copies stores.
    A failed read leaves the gate closed. No background target is started twice.
    """
    global _self_data_boot_pending, _self_data_boot_failed
    while _self_data_boot_pending:
        try:
            state = self_data_boot_status()
        except SELF_DATA_FENCE.Held:
            time.sleep(2)
            continue
        if state["writable"]:
            # Start is outside the retry loop: a partially failed thread launch
            # must not launch a duplicate set on the next tick.
            try:
                start_background_tasks()
            except Exception:
                _self_data_boot_failed = True
                print("Data move startup needs review; background activation did not complete", flush=True)
                return
            _self_data_boot_pending = False
            return
        time.sleep(2)


def _replace_own_pod():
    """Delete this pod so the Deployment makes one with a fresh data mount.
    Straight to the API: the usual guards read the data that is stale."""
    if not POD_NAME:
        raise ValueError("this pod's name is unknown")
    _ksend("DELETE", f"/api/v1/namespaces/{SELF.NS}/pods/{urllib.parse.quote(POD_NAME, safe='')}", shutdown_bypass=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    http_server = HTTP.BoundedHTTPServer(("0.0.0.0", port), H)
    # First, and independent of everything that reads the data: a stale data
    # mount is mended only by a new pod.
    threading.Thread(target=STALE_MOUNT.watch, args=(DATA_DIR, _replace_own_pod), name="data-mount", daemon=True).start()
    if _self_data_boot_pending:
        threading.Thread(target=finish_self_data_boot, name="data-move-startup", daemon=True).start()
    else:
        start_background_tasks()
    # On a rolling update or a drain, hand the lease over now rather than
    # leaving the others to wait out its expiry.
    signal.signal(signal.SIGTERM, lambda *_: (LEADER.release(), os._exit(0)))
    print(f"Homestead listening on :{port}", flush=True)
    http_server.serve_forever()
